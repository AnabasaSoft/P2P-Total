"""Bug real reportado por el usuario: nada más arrancar sin estar
conectado a BitTorrent, el menú contextual de una descarga persistida
mostraba correctamente "Pausar" (el estado guardado en la base de datos
era DOWNLOADING de la sesión anterior); pero en cuanto la descarga
arrancaba de verdad (al conectar la red), el botón "Pausar" desaparecía
del menú -pese a que la descarga seguía avanzando con normalidad.

Causa: al arrancar, `DownloadsTab` carga las descargas para la GUI con
su propia llamada a `database.load_all_downloads()`
(`DownloadManager.load_history()`); más tarde, al conectar la red,
`DownloadManager.reattach_active_downloads()` hace OTRA llamada
independiente a `database.load_all_downloads()` y pasa ESE objeto (uno
completamente distinto en memoria, aunque comparta `id` con el de la
GUI) a `TorrentBackend.reattach_download()`, que es quien lo guarda en
`self._active` y a quien `_poll_loop` actualiza y notifica en
adelante. Mientras el emparejamiento por `id` en
`DownloadsModel.update_download()` funcione, la fila de la GUI acaba
reflejando el estado real -este test comprueba justo eso de extremo a
extremo, con una sesión de libtorrent real."""

import asyncio
import os

import libtorrent as lt
import pytest

from backends.torrent_backend import TorrentBackend
from core import database
from core.backend_base import BackendRegistry
from core.download_manager import DownloadManager
from core.models import DownloadState, Network, SearchResult
from gui.models_qt import DownloadsModel


@pytest.mark.asyncio
async def test_gui_row_reaches_downloading_after_reattach_on_reconnect(tmp_path):
    folder = tmp_path / "contenido"
    folder.mkdir()
    (folder / "a.bin").write_bytes(os.urandom(500_000))
    torrent_path = tmp_path / "contenido.torrent"
    dest = tmp_path / "descarga"
    dest.mkdir()

    seed = TorrentBackend()
    await seed.connect()
    old_leech = TorrentBackend()
    await old_leech.connect()
    try:
        await seed.create_torrent(str(folder), str(torrent_path))

        # --- sesión anterior: arranca la descarga (queda persistida) ---
        old_manager = DownloadManager()
        BackendRegistry.register(old_leech)
        result = SearchResult(
            network=Network.TORRENT, title="contenido", size_bytes=500_000,
            source_id=f"file:{torrent_path}", seeds_or_sources=0,
        )
        download = await old_manager.download(result, str(dest))
        assert download.state == DownloadState.SEARCHING_SOURCES

        # "cierra la app": la sesión de libtorrent desaparece, pero el
        # último estado (SEARCHING_SOURCES) queda en la base de datos.
        await old_leech.disconnect()

        # --- "reabre la app", sin conectar todavía ---
        gui_model = DownloadsModel()
        gui_model.set_downloads(database.load_all_downloads())
        assert gui_model.download_at(0).state == DownloadState.SEARCHING_SOURCES

        # --- conecta la red: aquí es donde entran en juego dos objetos
        # Download distintos con el mismo id (ver docstring del módulo) ---
        new_manager = DownloadManager()
        new_manager.on_progress(gui_model.update_download)
        new_leech = TorrentBackend()
        await new_leech.connect()
        new_manager.register_backend(new_leech)
        await new_manager.reattach_active_downloads(Network.TORRENT)

        deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < deadline:
            if gui_model.download_at(0).state == DownloadState.DOWNLOADING:
                break
            await asyncio.sleep(0.2)

        assert gui_model.download_at(0).state == DownloadState.DOWNLOADING
    finally:
        await seed.disconnect()
        await new_leech.disconnect()
