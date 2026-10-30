# Rhythmbox-Session-Restore
This Rhythmbox Plugin will restore your currently playing track upon restarting the program.

# Features
- Remembers the last song, playback source or playlist, and playback position.
- Restores the saved song's title, artist, album, and artwork in Rhythmbox's native header.
- Resumes playback at the saved position when Play is pressed.
- Saves playback progress every 10 seconds for crash recovery.
- Saves a final checkpoint during normal shutdown for more accurate restoration.
- Works independently, with optional integration with Album Shuffle, Folder Shuffle, and Now Playing Sidebar.

## Usage Notes and Limitations
This plugin will work indepedently to remember the current track and track location. It will display the track information in Rhythmbox's main song bar. It does not currently display the exact track location in the main playback bar until playback is started. I recommend that you use my [Now Playing Sidebar](https://github.com/username227/Rhythmbox-Now-Playing-Sidebar) Plugin for additional built-in functionality, which includes a proper display of exact track location.

The plugin has also been designed for proper integration with my shuffle plugins, [Album Shuffle](https://github.com/username227/Rhythmbox-Album-Shuffle) and [Folder Shuffle](https://github.com/username227/Rhythmbox-Folder-Shuffle) but also works independently of them.

# Installation
For a **user-level installation**, copy the plugin folder into:

`~/.local/share/rhythmbox/plugins/`

For a **system-wide installation**, the location depends on your distribution:

- **Arch Linux:** `/usr/lib/rhythmbox/plugins/`
- **Debian/Ubuntu/Mint (x86_64):** `/usr/lib/x86_64-linux-gnu/rhythmbox/plugins/`
- **Debian/Ubuntu/Mint (ARM64):** `/usr/lib/aarch64-linux-gnu/rhythmbox/plugins/`

For **Flatpak**, use:

`~/.var/app/org.gnome.Rhythmbox3/data/rhythmbox/plugins/`

Restart Rhythmbox, open **Preferences → Plugins**, and enable the plugin.

# Additional Plugins
Please check out my entire Rhythmbox Plugin Collection:
- [Album Shuffle](https://github.com/username227/Rhythmbox-Album-Shuffle)
- [Folder Shuffle](https://github.com/username227/Rhythmbox-Folder-Shuffle)
- [AlbumArtist Filter](https://github.com/username227/Rhythmbox-AlbumArtist-Filter)
- [Now Playing Sidebar](https://github.com/username227/Rhythmbox-Now-Playing-Sidebar)

# Full Disclosure
  I am not a developer and am not formally trained in computer programming. AI was used to assist in the writing of the code based on my specifications, testing, and feedback. I have tested various development versions on  Arch Linux with Rhythmbox 3.5.1. I have not tested the plugin on other distributions. Reviews and improvements to the code base, as well as additional feature requests, are welcome.

## Please Note
  The plugin is python-based. The issues regarding the breaking of the python-based Rhythmbox plugins was well known, and they were broken in the past for a significant amount of time. Those upstream issues appear to have been rectified now. However, future changes of  Rhythmbox or its dependencies could cause changes that affect this plugin, particularly if those changes break python-loader. Hopefully this will not happen, but if it does, this plugin could break.
