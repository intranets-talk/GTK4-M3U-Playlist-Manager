#!/bin/sh
# Installs M3U Playlist Manager for the current user (no root needed):
#   - the program as ~/.local/bin/m3u-manager
#   - an application menu entry (Applications > Sound & Video)
#   - an icon
#   - "Open With" support for .m3u / .m3u8 files in Caja
# Run it from the folder that contains m3u_manager.py:   sh install.sh
# Remove again with:                                      sh install.sh --uninstall
set -e

here=$(cd "$(dirname "$0")" && pwd)
bin="$HOME/.local/bin"
apps="$HOME/.local/share/applications"
icons="$HOME/.local/share/icons/hicolor/scalable/apps"

if [ "$1" = "--uninstall" ]; then
    rm -f "$bin/m3u-manager" "$apps/m3u-manager.desktop" "$icons/m3u-manager.svg"
    update-desktop-database "$apps" 2>/dev/null || true
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
    echo "Removed."
    exit 0
fi

if [ ! -f "$here/m3u_manager.py" ]; then
    echo "m3u_manager.py not found next to install.sh" >&2
    exit 1
fi

mkdir -p "$bin" "$apps" "$icons"
install -m 755 "$here/m3u_manager.py" "$bin/m3u-manager"

cat > "$icons/m3u-manager.svg" <<'SVG_EOF'
<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">
  <rect x="8" y="18" width="112" height="78" rx="9" fill="#2b2f36" stroke="#6b7280" stroke-width="4"/>
  <rect x="17" y="27" width="94" height="60" rx="3" fill="#1b1f27"/>
  <g fill="#61afef">
    <rect x="25" y="36" width="12" height="9" rx="2"/>
    <rect x="43" y="38" width="56" height="5" rx="2.5"/>
    <rect x="25" y="53" width="12" height="9" rx="2"/>
    <rect x="43" y="55" width="40" height="5" rx="2.5"/>
  </g>
  <g fill="#98c379">
    <rect x="25" y="70" width="12" height="9" rx="2"/>
    <rect x="43" y="72" width="48" height="5" rx="2.5"/>
  </g>
  <rect x="50" y="100" width="28" height="7" fill="#6b7280"/>
  <rect x="34" y="107" width="60" height="8" rx="4" fill="#6b7280"/>
</svg>
SVG_EOF

cat > "$apps/m3u-manager.desktop" <<DESKTOP_EOF
[Desktop Entry]
Type=Application
Name=M3U Playlist Manager
Comment=Edit and push IPTV m3u playlists
Exec=$bin/m3u-manager %F
Icon=m3u-manager
Terminal=false
Categories=AudioVideo;Video;Utility;
MimeType=audio/x-mpegurl;audio/mpegurl;application/vnd.apple.mpegurl;application/x-mpegurl;
StartupNotify=true
DESKTOP_EOF

update-desktop-database "$apps" 2>/dev/null || true
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

echo "Installed. Find it in the Applications menu (Sound & Video), or run: m3u-manager"
case ":$PATH:" in
    *":$bin:"*) ;;
    *) echo "Note: $bin is not in your PATH yet - log out and in again to use 'm3u-manager' from a terminal." ;;
esac
