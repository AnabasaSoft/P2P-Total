#!/bin/bash
# Construye el .flatpak autónomo a partir del build "onedir" de PyInstaller
# (dist/p2p-total, generado antes por packaging/p2p-total.spec) y del
# manifiesto packaging/linux/org.anabasasoft.P2PTotal.yaml. Requiere tener
# ya instalados flatpak, flatpak-builder y los runtimes
# org.freedesktop.Platform//23.08 + org.freedesktop.Sdk//23.08.
# Uso: packaging/linux/build-flatpak.sh <version>
set -euo pipefail

VERSION="${1:?Uso: build-flatpak.sh <version>}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_ID="org.anabasasoft.P2PTotal"

[ -d "$ROOT_DIR/dist/p2p-total" ] || {
  echo "No existe dist/p2p-total (falta ejecutar antes 'pyinstaller packaging/p2p-total.spec')" >&2
  exit 1
}

BUILD_DIR="$ROOT_DIR/flatpak-build"
REPO_DIR="$ROOT_DIR/flatpak-repo"
rm -rf "$BUILD_DIR" "$REPO_DIR"

flatpak-builder --user --force-clean --repo="$REPO_DIR" "$BUILD_DIR" \
  "$ROOT_DIR/packaging/linux/$APP_ID.yaml"

flatpak build-bundle "$REPO_DIR" \
  "$ROOT_DIR/P2P-Total-$VERSION-x86_64.flatpak" "$APP_ID" \
  --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo
