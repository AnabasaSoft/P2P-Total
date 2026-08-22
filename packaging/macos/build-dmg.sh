#!/bin/bash
# Empaqueta el "P2P Total.app" generado por PyInstaller (packaging/p2p-total.spec,
# bloque BUNDLE) en un .dmg, usando solo herramientas ya incluidas en macOS
# (hdiutil), sin dependencias externas.
# Uso: packaging/macos/build-dmg.sh <version>
set -euo pipefail

VERSION="${1:?Uso: build-dmg.sh <version>}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

hdiutil create -volname "P2P Total" \
    -srcfolder "$ROOT_DIR/dist/P2P Total.app" \
    -ov -format UDZO \
    "$ROOT_DIR/P2P-Total-${VERSION}.dmg"
