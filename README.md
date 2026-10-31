# M3U Playlist Manager

A small GTK4 desktop app for editing IPTV `.m3u` / `.m3u8` playlists. Written in Python, built for Debian 12 with the MATE desktop with a dark theme.

## Features

- Open a playlist and edit it in a table: **reorder** (drag and drop or Move Up/Down), **add**, **edit** and **remove** channels
- Channel logos shown as images in the table (downloaded in the background and cached)
- Every change is **autosaved** back to the file; a `.bak` copy of the file as it was when opened is kept next to it
- Keeps the playlist header, unknown attributes and extra `#` lines (e.g. `#EXTVLCOPT`) intact
- **Push** uploads the playlist to a server with `scp`, and **From Server…** downloads one from the same folder to edit

## Requirements

```
sudo apt install python3-gi gir1.2-gtk-4.0 openssh-client
```

`openssh-client` is only needed for Push / From Server. Both use key based authentication (no password prompts).

## Run

```
python3 m3u_manager.py [playlist.m3u]
```

## Install in the desktop menu (optional)

```
sh install.sh              # adds a menu entry, icon and "Open With" for .m3u files
sh install.sh --uninstall  # removes it again
```

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| Ctrl+O | Open file |
| Ctrl+Shift+O | Open from server |
| Ctrl+S / Ctrl+Shift+S | Save / Save As |
| Ctrl+N | Add channel |
| Ctrl+E or double-click | Edit channel |
| Delete | Remove selected channels |
| Alt+Up / Alt+Down | Move selected channels |
| Ctrl+U | Push to server |

## Configuration

The server is set by `PUSH_TARGET` near the top of `m3u_manager.py`, in the same form as an `scp` destination. 

You will need to edit the file to update the destination to match your SFTP host and point to the folder where the m3u files are stored. 

```python
PUSH_TARGET = "user@host:/path/to/folder"
```

## Files the app creates

- `<playlist>.bak` next to the playlist: copy of the file as it was when opened
- `~/.cache/m3u-manager/logos/`: cached channel logos
- `~/.local/share/m3u-manager/remote/`: local working copies of playlists downloaded from the server

## MIT Licensed
