#!/bin/bash
# Construye el AppImage a partir del build "onedir" de PyInstaller
# (dist/p2p-total, generado antes por packaging/p2p-total.spec).
# Uso: packaging/linux/build-appimage.sh <version>
set -euo pipefail

VERSION="${1:?Uso: build-appimage.sh <version>}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST_DIR="$ROOT_DIR/dist/p2p-total"
APPDIR="$ROOT_DIR/AppDir"

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib/p2p-total" "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/metainfo" "$APPDIR/usr/share/icons/hicolor"

cp -r "$DIST_DIR"/* "$APPDIR/usr/lib/p2p-total/"
ln -s ../lib/p2p-total/p2p-total "$APPDIR/usr/bin/p2p-total"
cp "$ROOT_DIR/packaging/linux/p2p-total.desktop" "$APPDIR/usr/share/applications/"
cp "$ROOT_DIR/packaging/linux/org.anabasasoft.P2PTotal.metainfo.xml" "$APPDIR/usr/share/metainfo/"
cp -r "$ROOT_DIR/packaging/linux/icons/hicolor/"* "$APPDIR/usr/share/icons/hicolor/"

# appimagetool exige el .desktop, un icono y AppRun en la raíz del AppDir.
cp "$ROOT_DIR/packaging/linux/p2p-total.desktop" "$APPDIR/"
cp "$ROOT_DIR/packaging/linux/icons/hicolor/256x256/apps/p2p-total.png" "$APPDIR/p2p-total.png"
ln -s usr/bin/p2p-total "$APPDIR/AppRun"

VERSION="$VERSION" ARCH=x86_64 ./appimagetool --appimage-extract-and-run "$APPDIR" \
    "$ROOT_DIR/P2P-Total-${VERSION}-x86_64.AppImage"
