"""
Crea un .torrent a partir de un archivo/carpeta local y lo siembra
(seed) usando el mismo TorrentBackend del proyecto. Pensado para
aislar problemas de red: al no depender de ningún tracker externo,
sirve para comprobar que la sesión de libtorrent, la DHT local y la
transferencia de datos funcionan bien en tu máquina/LAN, sin que
interfieran temas de DNS o Internet.

Cómo probarlo (dos terminales):

  Terminal 1 (semilla):
    python create_test_torrent.py /ruta/a/un/archivo.bin
    -> imprime el magnet link, y se queda sembrando (Ctrl+C para parar)

  Terminal 2 (descarga, puede ser la misma máquina u otra en la LAN):
    python main.py download '<magnet-impreso-arriba>' ./descarga-test

Al no llevar tracker, el descubrimiento del peer sembrador se hace vía
LSD (Local Service Discovery, multicast en la LAN) o DHT si ambas
máquinas están en redes distintas con DHT accesible.
"""

import sys
import time

import libtorrent as lt


def create_and_seed(source_path: str) -> None:
    fs = lt.file_storage()
    lt.add_files(fs, source_path)

    if fs.num_files() == 0:
        print(f"No se encontraron archivos en: {source_path}")
        sys.exit(1)

    t = lt.create_torrent(fs)
    t.set_creator("p2p-manager test torrent")
    # Sin trackers a propósito: queremos aislar DHT/LSD del tema de DNS
    lt.set_piece_hashes(t, "/".join(source_path.split("/")[:-1]) or ".")

    torrent_data = lt.bencode(t.generate())
    torrent_path = source_path + ".torrent"
    with open(torrent_path, "wb") as f:
        f.write(torrent_data)
    print(f"Torrent creado: {torrent_path}")

    info = lt.torrent_info(torrent_data)
    infohash = str(info.info_hash())
    magnet = f"magnet:?xt=urn:btih:{infohash}&dn={info.name()}"
    print(f"\nMagnet (sin trackers, solo DHT/LSD):\n{magnet}\n")

    settings = {
        "listen_interfaces": "0.0.0.0:6881,[::]:6881",
        "enable_dht": True,
        "enable_lsd": True,
    }
    session = lt.session(settings)

    params = {
        "ti": info,
        "save_path": "/".join(source_path.split("/")[:-1]) or ".",
    }
    handle = session.add_torrent(params)

    print("Sembrando... (Ctrl+C para parar)")
    try:
        while True:
            status = handle.status()
            print(
                f"\r peers={status.num_peers}  "
                f"uploaded={status.total_upload / 1024:.1f} KB  "
                f"estado={status.state}",
                end="", flush=True,
            )
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nSeed detenido.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python create_test_torrent.py <ruta-a-archivo-o-carpeta>")
        sys.exit(1)
    create_and_seed(sys.argv[1])
