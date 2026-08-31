"""Punto 44 del backlog (v1): `TorrentBackend.probe_torrent_metadata()`
+ `download_piece_range()` -las dos mitades de una descarga BitTorrent
restringida a un tramo de bytes, para poder combinarla con tramos de
otras redes en una descarga agregada. Mismo patrón de siembra+descarga
real por loopback (sin DHT/trackers) que ya usa
`tests/test_torrent_disconnect_flushes_progress.py`."""

import asyncio
import os

import libtorrent as lt
import pytest

from backends.torrent_backend import TorrentBackend, build_torrent_file


@pytest.mark.asyncio
async def test_probe_then_download_piece_range_fetches_only_requested_pieces(tmp_path):
    folder = tmp_path / "contenido"
    folder.mkdir()
    content = os.urandom(2_000_000)
    (folder / "a.bin").write_bytes(content)
    torrent_path = tmp_path / "contenido.torrent"

    seed = TorrentBackend()
    await seed.connect()
    leech = TorrentBackend()
    await leech.connect()
    dest = tmp_path / "descarga"
    dest.mkdir()
    try:
        await seed.create_torrent(str(folder / "a.bin"), str(torrent_path))
        seed_port = seed._session.listen_port()

        handle, name, piece_length, total_size, infohash = await leech.probe_torrent_metadata(
            str(torrent_path), str(dest)
        )
        assert name == "a.bin"
        assert total_size == 2_000_000
        assert len(infohash) == 20
        # Fase 1 (solo metadatos) no debe haber empezado a bajar nada aún.
        assert handle.status().total_wanted_done == 0

        handle.connect_peer(("127.0.0.1", seed_port))

        piece_start = 3
        range_start = piece_start * piece_length
        range_end = total_size
        progress: list[int] = []
        await leech.download_piece_range(handle, range_start, range_end, on_progress=progress.append)

        out_file = dest / "a.bin"
        written = out_file.read_bytes()
        assert written[range_start:range_end] == content[range_start:range_end]
        assert progress[-1] == range_end - range_start
    finally:
        await seed.disconnect()
        await leech.disconnect()


@pytest.mark.asyncio
async def test_probe_torrent_metadata_rejects_multi_file_torrent(tmp_path):
    folder = tmp_path / "contenido"
    folder.mkdir()
    (folder / "a.bin").write_bytes(os.urandom(50_000))
    (folder / "b.bin").write_bytes(os.urandom(50_000))
    torrent_path = tmp_path / "contenido.torrent"
    torrent_path.write_bytes(build_torrent_file(str(folder)))

    backend = TorrentBackend()
    await backend.connect()
    dest = tmp_path / "descarga"
    dest.mkdir()
    try:
        with pytest.raises(ValueError, match="un único archivo"):
            await backend.probe_torrent_metadata(str(torrent_path), str(dest))
    finally:
        await backend.disconnect()
