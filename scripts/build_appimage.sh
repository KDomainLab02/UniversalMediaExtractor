#!/usr/bin/env bash
# Build a single-file Linux AppImage from the PyInstaller onedir output.
#
# Usage (Linux only):
#   ./scripts/build_appimage.sh
#
# Output: dist/UniversalMediaExtractor-x86_64.AppImage
# Users run it with:  chmod +x UniversalMediaExtractor-x86_64.AppImage
#                     ./UniversalMediaExtractor-x86_64.AppImage
set -euo pipefail
cd "$(dirname "$0")/.."

APP=UniversalMediaExtractor
APPDIR=build/AppDir

# 1. onedir build (fast startup, self-contained folder)
# If a one-file binary from an earlier build exists at dist/$APP, PyInstaller
# would fail to create the onedir *folder* at the same path, so remove it.
if [ -f "dist/$APP" ]; then rm -f "dist/$APP"; fi
python build_exe.py --onedir

# 2. AppDir layout expected by appimagetool
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/icons/hicolor/256x256/apps"

cp -r "dist/$APP" "$APPDIR/usr/bin/$APP"
if [ -f assets/icon-256.png ]; then
  cp assets/icon-256.png "$APPDIR/usr/share/icons/hicolor/256x256/apps/$APP.png"
  cp assets/icon-256.png "$APPDIR/$APP.png"
fi

cat > "$APPDIR/$APP.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Universal Media Extractor
Comment=Extract videos, galleries and music from 1000+ sites
Exec=AppRun
Icon=$APP
Terminal=true
Categories=Network;Utility;
EOF

cat > "$APPDIR/AppRun" <<EOF
#!/bin/sh
HERE=\$(dirname "\$(readlink -f "\$0")")
exec "$HERE/usr/bin/$APP/$APP" "\$@"
EOF
chmod +x "$APPDIR/AppRun"

# 3. appimagetool (static tool from upstream; runs on any glibc distro)
TOOL=build/appimagetool-x86_64.AppImage
if [ ! -x "$TOOL" ]; then
  mkdir -p build
  curl -L -o "$TOOL" \
    https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x "$TOOL"
fi

mkdir -p dist
"$TOOL" --appimage-extract-and-run "$APPDIR" \
  "dist/$APP-x86_64.AppImage"

echo
echo "Done: dist/$APP-x86_64.AppImage"
