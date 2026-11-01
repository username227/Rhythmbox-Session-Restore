"""Rhythmbox 3.5.x Session Restore. Standalone; no other plugins required.

Never changes a playlist's membership/order. Owns playback/source/seek recovery only.
"""
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from gi.repository import GObject, GLib, Gtk, Peas, RB, GdkPixbuf

LOG = logging.getLogger('rhythmbox-session-restore')
STATE_DIR = Path(os.environ.get('XDG_STATE_HOME', os.path.expanduser('~/.local/state'))) / 'rhythmbox-session-restore'
STATE_FILE = STATE_DIR / 'session.json'
PENDING_FILE = STATE_DIR / 'pending-resume.json'
CHECKPOINT_SECONDS = 10
MAX_RESTORE_SECONDS = 600  # Large/slow network libraries may need time to load.


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=str(path.parent),
                                         prefix='.' + path.name + '.', delete=False) as f:
            name = f.name
            json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def read_json(path):
    try:
        with path.open(encoding='utf-8') as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        LOG.exception('Session Restore: cannot read %s', path)
        return None


class SessionRestore(GObject.Object, Peas.Activatable):
    __gtype_name__ = 'IndependentSessionRestorePlugin'
    object = GObject.Property(type=GObject.Object)

    def __init__(self):
        super().__init__()
        self.shell = self.player = self.db = None
        self.handlers = []
        self.timer_id = self.restore_timer_id = self.seek_timer_id = None
        self.saved = None
        self.restoring = False
        self.started_restore = False
        self.restore_deadline = 0
        self.seek_attempts = 0
        self.last_observed_position = None
        self.last_observed_at = None
        self.last_checkpoint_at = 0
        self.current_uri = None
        self.last_good_snapshot = None
        self.restore_not_before = 0
        self.awaiting_play = False
        self.pending_seek_uri = None
        self.pending_seek_position = 0
        self.resume_banner = None
        self.native_header_label = None
        self.original_mute = None
        self.held_mute = False
        self.last_wait_reason = None
        self.preview_art_store = None
        self.preview_art_widget = None
        self.preview_art_generation = 0
        self.correcting_first_play = False
        self.first_play_correction_id = None

    def do_activate(self):
        self.shell = self.object
        self.player = self.shell.props.shell_player
        self.db = self.shell.props.db
        self.saved = read_json(STATE_FILE)
        self._clear_pending_status()
        # Enabling the plugin mid-session must not hijack a song already playing.
        existing = self.player.get_playing_entry()
        self.restoring = bool(self.saved and self.saved.get('uri') and existing is None)
        # Do not use a fixed startup delay.  Wait until the window is mapped
        # and the saved database entry and source are actually ready.
        self.restore_not_before = 0
        self.restore_deadline = time.monotonic() + MAX_RESTORE_SECONDS
        for signal, callback in (
                ('playing-song-changed', self.on_song_changed),
                ('playing-changed', self.on_play_changed),
                ('playing-source-changed', self.on_source_changed)):
            self.handlers.append((self.player, self.player.connect(signal, callback)))
        self.handlers.append((self.db, self.db.connect('load-complete', self.on_load_complete)))
        # Rhythmbox's rb_shell_quit() hides the window BEFORE stopping the
        # playback backend. Capture the precise position at that boundary;
        # plugin deactivation occurs too late if the backend has been stopped.
        # Hiding to the background also produces an extra harmless checkpoint.
        try:
            window = self.shell.props.window
            if window is not None:
                self.handlers.append((window, window.connect('hide', self.on_window_hide)))
        except Exception:
            LOG.exception('Session Restore: cannot monitor window hide for final checkpoint')
        # We poll elapsed position at 2 s for manual seeks; write at most every 10 s
        # during ordinary playback. Signal-triggered changes are written immediately.
        self.timer_id = GLib.timeout_add_seconds(2, self.on_tick)
        if self.restoring:
            self.restore_timer_id = GLib.timeout_add(500, self.try_restore)
            GLib.idle_add(self._try_restore_once)
        if existing is not None:
            self.checkpoint(force=True)
        LOG.warning('Session Restore enabled: %s',
                    'recovering saved session' if self.restoring else 'monitoring playback')

    def do_deactivate(self):
        # Undo the temporary silence if the plugin is disabled mid-restoration.
        self._clear_resume_banner()
        self._clear_pending_status()
        self._release_mute()
        self.preview_art_generation += 1
        self.preview_art_store = None
        self.preview_art_widget = None
        # Do not overwrite the snapshot if startup restoration never succeeded.
        if not self.restoring and not self.awaiting_play:
            self.checkpoint(force=True)
        for obj, handler in self.handlers:
            try:
                obj.disconnect(handler)
            except (TypeError, RuntimeError):
                pass
        self.handlers = []
        for source_id in (self.timer_id, self.restore_timer_id, self.seek_timer_id,
                          self.first_play_correction_id):
            if source_id is not None:
                try:
                    GLib.source_remove(source_id)
                except Exception:
                    pass
        self.timer_id = self.restore_timer_id = self.seek_timer_id = None
        self.first_play_correction_id = None

    def on_window_hide(self, *_args):
        # Called before rb_shell_player_stop during a normal application quit.
        # No song/seek changes are performed and pending restores are protected
        # by checkpoint()'s existing guards.
        self.checkpoint(force=True)

    def on_load_complete(self, *_args):
        if self.restoring:
            GLib.idle_add(self._try_restore_once)

    def _try_restore_once(self):
        self.try_restore()
        return GLib.SOURCE_REMOVE

    def _playing_state(self):
        try:
            result = self.player.get_playing()
            if isinstance(result, tuple):
                return bool(result[-1])
            return bool(result)
        except Exception:
            return False

    def _playing_time(self):
        try:
            result = self.player.get_playing_time()
            if isinstance(result, tuple):
                return max(0, int(result[-1]))
            return max(0, int(result))
        except Exception:
            return 0

    def _source_description(self, source):
        if source is None:
            return {'kind': 'other', 'name': ''}
        try:
            name = source.props.name
            # Compare source names as GI may return different Python wrappers
            # for the same underlying GObject. Never silently call a playlist Music.
            for playlist in self.shell.props.playlist_manager.get_playlists():
                if playlist.props.name == name:
                    return {'kind': 'playlist', 'name': name}
            library = self.shell.props.library_source
            if source == library or name == library.props.name:
                return {'kind': 'library', 'name': name}
            return {'kind': 'other', 'name': name}
        except Exception:
            LOG.exception('Session Restore: unable to classify playback source')
            return {'kind': 'other', 'name': ''}

    def _snapshot(self):
        try:
            entry = self.player.get_playing_entry()
            if entry is None:
                return None
            uri = entry.get_string(RB.RhythmDBPropType.LOCATION)
            if not uri:
                return None
            source = self.player.get_playing_source()
            return {'version': 1, 'uri': uri, 'position': self._playing_time(),
                    'source': self._source_description(source),
                    'was_playing': self._playing_state(), 'saved_at': int(time.time())}
        except Exception:
            LOG.exception('Session Restore: unable to snapshot current playback')
            return None

    def checkpoint(self, force=False):
        if self.restoring or self.awaiting_play:
            return
        snapshot = self._snapshot()
        if snapshot is None:
            return  # Stopping/clearing song must not erase a valid checkpoint.
        now = time.monotonic()
        if not force and now - self.last_checkpoint_at < CHECKPOINT_SECONDS:
            return
        if not force and self.last_good_snapshot is not None:
            old = self.last_good_snapshot
            if (snapshot['uri'] == old['uri'] and snapshot['position'] == old['position']
                    and snapshot['source'] == old['source'] and snapshot['was_playing'] == old['was_playing']):
                return
        try:
            atomic_json(STATE_FILE, snapshot)
        except OSError:
            LOG.exception('Session Restore: checkpoint write failed')
            return
        self.last_good_snapshot = snapshot
        self.last_checkpoint_at = now

    def on_song_changed(self, _player, entry):
        if self.restoring:
            if not self.started_restore and entry is not None:
                self._cancel_pending_restore()
            else:
                return
        if self.awaiting_play and entry is not None:
            uri = entry.get_string(RB.RhythmDBPropType.LOCATION)
            if uri == self.pending_seek_uri:
                if self.seek_timer_id is None:
                    self.seek_attempts = 0
                    self.seek_timer_id = GLib.timeout_add(200, self._finish_deferred_seek)
                return
            # The native Play action can ignore the highlighted entry and
            # choose the play-order's next item. Correct this *only after*
            # the user has pressed Play, never during startup. Keep mute held
            # until the saved entry has actually started and been sought.
            if not self.correcting_first_play and self.first_play_correction_id is None:
                LOG.warning('Session Restore: Play chose %s instead of saved %s; correcting playback',
                            uri, self.pending_seek_uri)
                self.first_play_correction_id = GLib.idle_add(self._correct_first_play)
            return
        if entry is not None:
            self.current_uri = entry.get_string(RB.RhythmDBPropType.LOCATION)
            self.checkpoint(force=True)

    def _correct_first_play(self):
        self.first_play_correction_id = None
        if not self.awaiting_play or not self.pending_seek_uri or self.correcting_first_play:
            return GLib.SOURCE_REMOVE
        try:
            entry = self.db.entry_lookup_by_location(self.pending_seek_uri)
            source = self._source_for_saved(self.saved)
            if entry is None or source is None:
                raise RuntimeError('saved entry or playlist is no longer available')
            self.correcting_first_play = True
            # We are responding to the user's actual Play action, not starting
            # audio on startup. The mute is held until our seek is complete.
            self.player.play_entry(entry, source)
            current = self.player.get_playing_entry()
            actual = (current.get_string(RB.RhythmDBPropType.LOCATION)
                      if current is not None else '(not established)')
            LOG.warning('Session Restore: first-Play correction requested %s; player reports %s',
                        self.pending_seek_uri, actual)
            if self.seek_timer_id is None:
                self.seek_attempts = 0
                self.seek_timer_id = GLib.timeout_add(200, self._finish_deferred_seek)
        except Exception:
            LOG.exception('Session Restore: unable to correct first-Play track')
            # Never strand playback muted if this correction fails.
            self.awaiting_play = False
            self.pending_seek_uri = None
            self._clear_pending_status()
            self._release_mute()
            self._restore_visible_source_activation()
        finally:
            self.correcting_first_play = False
        return GLib.SOURCE_REMOVE

    def on_play_changed(self, *_args):
        if not self.restoring and not self.awaiting_play:
            self.checkpoint(force=True)

    def on_source_changed(self, *_args):
        if not self.restoring and not self.awaiting_play:
            self.checkpoint(force=True)

    def on_tick(self):
        if self.restoring or self.awaiting_play:
            return GLib.SOURCE_CONTINUE
        snapshot = self._snapshot()
        if snapshot is None:
            return GLib.SOURCE_CONTINUE
        now = time.monotonic()
        position = snapshot['position']
        # A discontinuity in elapsed time is almost certainly a manual seek.
        if self.last_observed_position is not None and snapshot['uri'] == self.current_uri:
            expected = self.last_observed_position + (now - self.last_observed_at if snapshot['was_playing'] else 0)
            if abs(position - expected) > 4:
                self.checkpoint(force=True)
        self.current_uri = snapshot['uri']
        self.last_observed_position = position
        self.last_observed_at = now
        self.checkpoint()
        return GLib.SOURCE_CONTINUE

    def _clear_pending_status(self):
        try:
            PENDING_FILE.unlink(missing_ok=True)
        except OSError:
            LOG.exception('Session Restore: unable to remove display-only state')

    def _publish_pending_status(self, uri, position):
        # Optional, read-only metadata for user-interface plugins. The seek
        # remains entirely owned by Session Restore.
        try:
            atomic_json(PENDING_FILE, {'version': 1, 'pid': os.getpid(),
                                       'uri': uri, 'position': int(position)})
        except OSError:
            LOG.exception('Session Restore: unable to publish display-only state')

    def _cancel_pending_restore(self):
        self.restoring = False
        self.started_restore = False
        if self.restore_timer_id is not None:
            GLib.source_remove(self.restore_timer_id)
            self.restore_timer_id = None

    def _source_for_saved(self, state):
        source_info = state.get('source') or {}
        name = source_info.get('name')
        if source_info.get('kind') == 'playlist':
            for playlist in self.shell.props.playlist_manager.get_playlists():
                if playlist.props.name == name:
                    return playlist
            return None
        if source_info.get('kind') == 'library':
            return self.shell.props.library_source
        # Other source types may not be present. Never misrepresent a saved
        # playlist as library playback; other non-playlist sources can fall back.
        if name:
            try:
                selected = self.shell.props.selected_page
                if selected and selected.props.name == name:
                    return selected
            except Exception:
                pass
        return self.shell.props.library_source

    def _wait_for_shuffle_readiness(self, state):
        """Optional integration; absence of shuffle plugins never blocks restore."""
        source = state.get('source') or {}
        name = source.get('name')
        options = {'Continuous Album Shuffle': ('albumshuffle', 'album'),
                   'Continuous Folder Shuffle': ('foldershuffle', 'folder')}
        if name not in options:
            return False
        module, mode = options[name]
        try:
            engine = Peas.Engine.get_default()
            info = engine.get_plugin_info(module)
            if info is None or not info.is_loaded():
                return False  # Session Restore is fully standalone.
            root = Path(os.environ.get('XDG_STATE_HOME', os.path.expanduser('~/.local/state'))) / 'rhythmbox-shuffle-state'
            progress = read_json(root / (mode + '-progress.json'))
            if not progress or not progress.get('active'):
                return False
            ready = read_json(root / (mode + '-ready.json'))
            # No same-process readiness marker means an old progress snapshot
            # (or a shuffle plugin without handshake support). Never deadlock.
            if not ready or ready.get('pid') != os.getpid():
                return False
            return not (ready.get('generation') == progress.get('generation') and
                        ready.get('active'))
        except Exception:
            LOG.exception('Session Restore: shuffle readiness check failed, continuing independently')
            return False

    def _playlist_contains(self, playlist, uri):
        model = playlist.get_query_model()
        if model is None:
            return False
        for row in model:
            if row[0].get_string(RB.RhythmDBPropType.LOCATION) == uri:
                return True
        return False

    def _waiting(self, reason):
        if reason != self.last_wait_reason:
            LOG.warning('Session Restore: waiting: %s', reason)
            self.last_wait_reason = reason
        return GLib.SOURCE_CONTINUE

    def try_restore(self):
        if not self.restoring or self.started_restore:
            return GLib.SOURCE_REMOVE
        if self.shell.props.window is None or not self.shell.props.window.get_mapped():
            return self._waiting('Rhythmbox window not ready yet')
        if time.monotonic() > self.restore_deadline:
            LOG.warning('Session Restore: library/playlist unavailable; retaining saved state for next run')
            # Keep the file; allow normal playback after timeout.
            self._cancel_pending_restore()
            return GLib.SOURCE_REMOVE
        try:
            state = self.saved
            entry = self.db.entry_lookup_by_location(state['uri'])
            if entry is None:
                return self._waiting('song not in Rhythmbox database yet')
            source = self._source_for_saved(state)
            if source is None:
                return self._waiting('saved playlist not loaded yet: %s' %
                                     ((state.get('source') or {}).get('name') or '(unnamed)'))
            if (state.get('source') or {}).get('kind') == 'playlist' and not self._playlist_contains(source, state['uri']):
                return self._waiting('saved song not present in playlist yet: %s' % source.props.name)
            if self._wait_for_shuffle_readiness(state):
                return self._waiting('shuffle plugin has not finished recovering its sequence')
            self.last_wait_reason = None
            # If the user started playback during startup, never replace it.
            if self.player.get_playing_entry() is not None:
                self._cancel_pending_restore()
                return GLib.SOURCE_REMOVE
            self.started_restore = True
            # Selection is all that is needed. Rhythmbox's native Play button
            # uses the selected row when there is no playing entry. Never call
            # play_entry() at startup: doing so starts a real audio stream.
            self._select_and_highlight(source, state['uri'])
            self.pending_seek_uri = state['uri']
            self.pending_seek_position = max(0, int(state.get('position', 0)))
            # No stream is opened here. Hold audio muted before the eventual
            # user-initiated Play, so the first moments cannot be heard before
            # a delayed asynchronous seek succeeds.
            self._hold_mute()
            self._publish_pending_status(state['uri'], self.pending_seek_position)
            self._show_native_header_preview(entry)
            self.awaiting_play = True
            self._request_native_art_preview(entry)
            self.restoring = False
            self.started_restore = False
            if self.restore_timer_id is not None:
                GLib.source_remove(self.restore_timer_id)
                self.restore_timer_id = None
            LOG.warning('Session Restore: selected %s in %s at %ds; waiting for Play',
                        state['uri'], source.props.name, self.pending_seek_position)
        except Exception:
            self.started_restore = False
            LOG.exception('Session Restore: selection failed; will retry')
        return GLib.SOURCE_REMOVE if self.awaiting_play else GLib.SOURCE_CONTINUE

    def _select_and_highlight(self, source, uri):
        """Align Rhythmbox's playback source and visible selection before Play.

        The display-page tree's selection does not guarantee that the shell
        player's *selected source* has changed.  playpause() consults that
        selected source first, then its selected row, then the play order.
        Do not mutate the play order or play_entry() at startup: the latter
        opens audio, and the former risks changing shuffle continuation.
        """
        try:
            self.shell.props.display_page_tree.select(source)
            # Explicitly synchronize the player as the original Rhythmbox
            # Remember the Rhythm plugin did.  This only sets the selected
            # source; it does NOT establish a playing entry or start audio.
            self.player.set_selected_source(source)
            view = source.get_entry_view()
            entry = self.db.entry_lookup_by_location(uri)
            if view is None or entry is None or not view.get_entry_contained(entry):
                LOG.warning('Session Restore: saved song not selectable in %s; first-Play correction remains enabled',
                            source.props.name)
                return
            view.select_none()
            view.select_entry(entry)
            view.scroll_to_entry(entry)
            # A pending Play Queue still has precedence inside Rhythmbox.
            # Never clear or reorder it automatically; first-Play correction
            # remains a fallback in that case and on unusual theme/UI setups.
            LOG.warning('Session Restore: preselected saved entry in %s for native Play',
                        source.props.name)
        except Exception:
            LOG.exception('Session Restore: unable to preselect restored entry; first-Play correction remains enabled')

    def _restore_visible_source_activation(self):
        """After resume, reconnect native double-click handling to the visible page.

        Before initial Play, the player must select the saved playlist so that
        Rhythmbox resumes the correct entry. The library view may remain visible.
        Rhythmbox connects entry-activated only for the player's selected source,
        so leaving that source on the hidden shuffle playlist makes Music rows
        respond visually to double-clicks without starting playback.

        Updating only the *selected* source after playback has started does not
        change the current playing source, playlist, or active play order.
        """
        try:
            visible = self.shell.props.selected_page
            if visible is None or not isinstance(visible, RB.Source):
                return
            if self.player.get_playing_entry() is None:
                return
            self.player.set_selected_source(visible)
            LOG.warning('Session Restore: restored track activation in visible source %s',
                        visible.props.name)
        except Exception:
            LOG.exception('Session Restore: could not restore visible-source playback controls')

    def _finish_deferred_seek(self):
        # Called ONLY after the user presses Play and Rhythmbox identifies the
        # selected song. Never open or pause the stream ourselves.
        self.seek_attempts += 1
        try:
            entry = self.player.get_playing_entry()
            if entry is None:
                raise RuntimeError('player entry is not established yet')
            uri = entry.get_string(RB.RhythmDBPropType.LOCATION)
            if uri != self.pending_seek_uri:
                if not self.correcting_first_play and self.first_play_correction_id is None:
                    self.first_play_correction_id = GLib.idle_add(self._correct_first_play)
                raise RuntimeError('wrong entry selected by player; correction pending')
            if not self._playing_state():
                raise RuntimeError('waiting for actual playback')
            position = self.pending_seek_position
            if position:
                accepted = self.player.set_playing_time(position)
                if accepted is False:
                    raise RuntimeError('seek not yet accepted')
                observed = self._playing_time()
                if abs(observed - position) > 3:
                    raise RuntimeError('seek not yet established')
            self.awaiting_play = False
            self.pending_seek_uri = None
            self.seek_timer_id = None
            self._clear_resume_banner()
            self._clear_pending_status()
            self._release_mute()
            self._restore_visible_source_activation()
            self.current_uri = uri
            self.last_observed_position = self._playing_time()
            self.last_observed_at = time.monotonic()
            self.checkpoint(force=True)
            LOG.warning('Session Restore: user started Play; sought %s to %ds', uri, position)
            return GLib.SOURCE_REMOVE
        except Exception as exc:
            if self.seek_attempts in (1, 5, 15):
                LOG.warning('Session Restore: waiting to seek after Play (%s)', exc)
            if self.seek_attempts >= 40:
                LOG.warning('Session Restore: seek unavailable (%s); leaving playback under normal Rhythmbox control', exc)
                self.awaiting_play = False
                self.pending_seek_uri = None
                self.seek_timer_id = None
                self._clear_resume_banner()
                self._clear_pending_status()
                self._release_mute()
                self._restore_visible_source_activation()
                self.checkpoint(force=True)
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

    def _hold_mute(self):
        if self.held_mute:
            return
        try:
            status = self.player.get_mute()
            self.original_mute = bool(status[-1] if isinstance(status, tuple) else status)
            if not self.original_mute:
                self.player.set_mute(True)
            self.held_mute = True
        except Exception:
            LOG.exception('Session Restore: cannot temporarily silence playback')

    def _release_mute(self):
        if not self.held_mute:
            return
        try:
            self.player.set_mute(bool(self.original_mute))
        except Exception:
            LOG.exception('Session Restore: unable to restore original mute state')
        finally:
            self.held_mute = False
            self.original_mute = None

    def _show_native_header_preview(self, entry):
        """Show pending track information in Rhythmbox's existing header label.

        This deliberately changes only the GTK label.  It does NOT claim a
        playing entry or open an audio stream; the real header takes over as
        soon as the user presses Play.
        """
        try:
            def walk(widget):
                yield widget
                if isinstance(widget, Gtk.Container):
                    for child in widget.get_children():
                        yield from walk(child)

            title = entry.get_string(RB.RhythmDBPropType.TITLE) or ''
            artist = entry.get_string(RB.RhythmDBPropType.ARTIST) or ''
            album = entry.get_string(RB.RhythmDBPropType.ALBUM) or ''
            for widget in walk(self.shell.props.window):
                if not isinstance(widget, Gtk.Widget):
                    continue
                if GObject.type_name(widget.__gtype__) != 'RBHeader':
                    continue
                for child in walk(widget):
                    if isinstance(child, Gtk.Label) and child.get_text() == 'Not Playing':
                        # Mirror RBHeader's native title/detail typography.
                        # Keep both lines inside its existing not_playing label:
                        # on real playback RBHeader replaces that label with its
                        # actual song/details widgets, without leaving an overlay.
                        safe_title = GLib.markup_escape_text(title)
                        safe_artist = GLib.markup_escape_text(artist)
                        safe_album = GLib.markup_escape_text(album)
                        markup = '<big><b>' + safe_title + '</b></big>'
                        if artist:
                            details = 'by <i>' + safe_artist + '</i>'
                            if album:
                                details += ' from <i>' + safe_album + '</i>'
                        elif album:
                            details = '<i>' + safe_album + '</i>'
                        else:
                            details = ''
                        if details:
                            markup += '\n' + details
                        child.set_markup(markup)
                        child.set_tooltip_text('%s\n%s' % (artist, album))
                        self.native_header_label = child
                        LOG.warning('Session Restore: populated native header preview for %s', title)
                        return
            LOG.warning('Session Restore: native header placeholder not found (resume still works)')
        except Exception:
            LOG.exception('Session Restore: could not set native header preview')

    def _request_native_art_preview(self, entry):
        """Request cached artwork for the native header before an audio stream exists.

        Optional UI-only enhancement. Missing GI support, cache failures, and
        asynchronous callbacks can never interfere with session restoration.
        """
        try:
            def walk(widget):
                yield widget
                if isinstance(widget, Gtk.Container):
                    for child in widget.get_children():
                        yield from walk(child)

            image = None
            for widget in walk(self.shell.props.window):
                if GObject.type_name(widget.__gtype__) == 'RBHeader':
                    for child in walk(widget):
                        if GObject.type_name(child.__gtype__) == 'RBFadingImage':
                            image = child
                            break
                    break
            if image is None or not callable(getattr(image, 'set_pixbuf', None)):
                LOG.warning('Session Restore: header artwork setter not exposed by GI; leaving native art unchanged')
                return
            if self.preview_art_store is None:
                self.preview_art_store = RB.ExtDB(name='album-art')
            self.preview_art_widget = image
            self.preview_art_generation += 1
            generation = self.preview_art_generation
            key = entry.create_ext_db_key(RB.RhythmDBPropType.ALBUM)
            self.preview_art_store.request(
                key, lambda _key, _store_key, _filename, data:
                self._native_art_received(generation, data))
        except Exception:
            LOG.exception('Session Restore: native artwork preview not available')

    def _native_art_received(self, generation, data):
        if (generation != self.preview_art_generation or
                not self.awaiting_play or data is None or
                not isinstance(data, GdkPixbuf.Pixbuf)):
            return
        try:
            if self.preview_art_widget is not None:
                self.preview_art_widget.set_pixbuf(data)
        except Exception:
            LOG.exception('Session Restore: unable to populate native header artwork')

    def _show_resume_banner(self, source, position):
        # Unlike the native progress slider, this can show the saved position
        # without starting playback. It disappears after seeking succeeds.
        self._clear_resume_banner()
        try:
            window = self.shell.props.window
            root = window.get_child()
            if not isinstance(root, Gtk.Box):
                LOG.warning('Session Restore: no suitable window container for resume message')
                return
            minutes, seconds = divmod(position, 60)
            hours, minutes = divmod(minutes, 60)
            stamp = f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes}:{seconds:02}'
            label = Gtk.Label(label=f'Session Restore — Ready to resume at {stamp} · Press Play')
            label.set_xalign(0.5)
            label.set_margin_top(5)
            label.set_margin_bottom(5)
            label.get_style_context().add_class('dim-label')
            root.pack_start(label, False, False, 0)
            root.reorder_child(label, 0)
            label.show()
            self.resume_banner = label
        except Exception:
            LOG.exception('Session Restore: cannot display resume position')

    def _clear_resume_banner(self):
        if self.resume_banner is not None:
            try:
                self.resume_banner.destroy()
            except Exception:
                pass
            self.resume_banner = None
