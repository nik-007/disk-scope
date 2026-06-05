#!/usr/bin/env bash
# Install a DiskScope launcher into the desktop application menu.
set -euo pipefail
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
cat > "$APPS/diskscope.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=DiskScope
GenericName=Disk Usage Analyzer
Comment=Fast directory size analyzer with safe deletion
Exec=$DIR/diskscope %f
Icon=$DIR/diskscope.svg
Terminal=false
Categories=Utility;
Keywords=disk;usage;size;analyzer;directory;cleanup;
StartupNotify=true
DESKTOP
update-desktop-database "$APPS" 2>/dev/null || true
echo "Installed: $APPS/diskscope.desktop"
echo "Run with ./diskscope or launch 'DiskScope' from your application menu."
