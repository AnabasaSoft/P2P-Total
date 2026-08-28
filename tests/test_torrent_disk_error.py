"""Los errores reales de disco (cuota de usuario excedida, permisos,
disco lleno...) durante una descarga de BitTorrent nunca se veían en la
GUI: libtorrent se limita a marcar `status.paused = True` y dejar de
avanzar, indistinguible a simple vista de una pausa normal del usuario.
`TorrentBackend._poll_loop` ahora lee `status.errc` (el error real que
trae libtorrent para ese caso) y lo traduce a `DownloadState.ERROR` +
`error_message`, igual que ya hacían los otros cuatro backends ante sus
propios fallos de transferencia.

Se reproduce un fallo de escritura real (no simulado): se crea un
torrent a partir de contenido local, se añade esa misma descarga hacia
un directorio sin permiso de escritura, y se fuerza la escritura de la
primera pieza con `handle.add_piece()` -que va directa a disco sin
depender de un peer real ni de temporización de red, a diferencia de
un intercambio genuino entre dos sesiones por loopback, que resultó
ser propenso a fallos intermitentes en la propia CI/entorno de test
según cuándo terminaba el *handshake* del peer."""

import asyncio
import os

import libtorrent as lt
import pytest

from backends.torrent_backend import TorrentBackend
from core.models import DownloadState, Network, SearchResult


@pytest.mark.asyncio
async def test_write_failure_sets_error_state_with_message(tmp_path):
    folder = tmp_path / "contenido"
    folder.mkdir()
    (folder / "a.bin").write_bytes(os.urandom(4_000_000))
    torrent_path = tmp_path / "contenido.torrent"

    seed = TorrentBackend()
    await seed.connect()
    leech = TorrentBackend()
    await leech.connect()
    readonly_dir = tmp_path / "sin_permiso"
    readonly_dir.mkdir()
    try:
        await seed.create_torrent(str(folder), str(torrent_path))

        result = SearchResult(
            network=Network.TORRENT, title="test", size_bytes=4_000_000,
            source_id=f"file:{torrent_path}", seeds_or_sources=0,
        )
        readonly_dir.chmod(0o555)
        download_leech = await leech.start_download(result, str(readonly_dir))
        entry = leech._find_entry(download_leech)
        handle = entry["handle"]

        for _ in range(50):
            if handle.status().has_metadata:
                break
            await asyncio.sleep(0.1)
        piece = (folder / "a.bin").read_bytes()[: handle.torrent_file().piece_length()]
        handle.add_piece(0, piece, lt.add_piece_flags_t.overwrite_existing)

        deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < deadline:
            if download_leech.state == DownloadState.ERROR:
                break
            await asyncio.sleep(0.2)

        assert download_leech.state == DownloadState.ERROR
        assert download_leech.error_message
    finally:
        # Sin esto, `lt.session.__del__` intenta limpiar un directorio sin
        # permiso de escritura y el propio proceso de test se lía.
        readonly_dir.chmod(0o755)
        await seed.disconnect()
        await leech.disconnect()
