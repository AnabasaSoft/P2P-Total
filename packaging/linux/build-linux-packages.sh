#!/bin/bash
# Construye el .deb y el .rpm a partir del build "onedir" de PyInstaller
# (dist/p2p-total) usando fpm (https://fpm.readthedocs.io/), que evita tener
# que escribir a mano debian/control+rules y un fichero .spec de rpmbuild.
# Uso: packaging/linux/build-linux-packages.sh <version>
set -euo pipefail

VERSION="${1:?Uso: build-linux-packages.sh <version>}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST_DIR="$ROOT_DIR/dist/p2p-total"
STAGE_DIR="$ROOT_DIR/stage"

rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR/usr/lib/p2p-total" "$STAGE_DIR/usr/bin" \
         "$STAGE_DIR/usr/share/applications" "$STAGE_DIR/usr/share/metainfo" \
         "$STAGE_DIR/usr/share/icons/hicolor"

cp -r "$DIST_DIR"/* "$STAGE_DIR/usr/lib/p2p-total/"
cat > "$STAGE_DIR/usr/bin/p2p-total" <<'EOF'
#!/bin/sh
exec /usr/lib/p2p-total/p2p-total "$@"
EOF
chmod +x "$STAGE_DIR/usr/bin/p2p-total"
cp "$ROOT_DIR/packaging/linux/p2p-total.desktop" "$STAGE_DIR/usr/share/applications/"
cp "$ROOT_DIR/packaging/linux/org.anabasasoft.P2PTotal.metainfo.xml" "$STAGE_DIR/usr/share/metainfo/"
cp -r "$ROOT_DIR/packaging/linux/icons/hicolor/"* "$STAGE_DIR/usr/share/icons/hicolor/"

COMMON_ARGS=(
    -s dir -C "$STAGE_DIR"
    -n p2p-total -v "$VERSION"
    --license "GPL-3.0-or-later"
    --url "https://github.com/AnabasaSoft/P2P-Total"
    --maintainer "AnabasaSoft"
    --description "Cliente P2P multi-red: BitTorrent, Soulseek, DC++, Gnutella2 y eMule/Kad"
    --category net
    usr
)

fpm -t deb -p "$ROOT_DIR/p2p-total_${VERSION}_amd64.deb" "${COMMON_ARGS[@]}"
fpm -t rpm -p "$ROOT_DIR/p2p-total-${VERSION}-1.x86_64.rpm" "${COMMON_ARGS[@]}"
