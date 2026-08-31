"""Bug real reportado por el usuario: con dos torrents recién empezados
(15% y 2%), cerrar la app y volver a abrirla los hacía volver a 0% y
arrancar de cero en vez de retomarlos desde donde se quedaron.

Causa, en `TorrentBackend.disconnect()`: el destructor de `lt.session`
es quien vuelca a disco, de forma síncrona, los bloques ya descargados
que libtorrent todavía tenía solo en el caché de escritura en memoria
-pero ese destructor se lanzaba en un hilo daemon sin esperarlo nunca
("fire and forget"), y `gui/main_window.py` llamaba a
`QApplication.quit()` casi inmediatamente después de programar la
desconexión. Si el proceso terminaba antes de que el hilo daemon
completase el volcado, ese progreso reciente se perdía sin remedio: en
el recheck del siguiente arranque, esas piezas fallaban la
comprobación de hash y se volvían a pedir enteras.

Este test reproduce el escenario de verdad (siembra + descarga real
sobre loopback, sin DHT/trackers) y comprueba la propiedad que debía
cumplirse y no se cumplía: en cuanto `await backend.disconnect()`
termina -sin ningún `sleep` extra que disimule la carrera de verdad-,
el progreso ya descargado debe sobrevivir intacto a un reenganche
inmediato."""

import asyncio
import os

import libtorrent as lt
import pytest

from backends.torrent_backend import TorrentBackend
from core.models import Download, DownloadState, Network

_CHECKING_STATES = (
    lt.torrent_status.states.checking_resume_data,
    lt.torrent_status.states.checking_files,
    lt.torrent_status.states.queued_for_checking,
)


async def _wait_checked(handle, timeout=15.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        status = handle.status()
        if status.state not in _CHECKING_STATES:
            return status
        await asyncio.sleep(0.2)
    return handle.status()


@pytest.mark.asyncio
async def test_disconnect_preserves_partial_progress_without_extra_wait(tmp_path):
    folder = tmp_path / "contenido"
    folder.mkdir()
    (folder / "a.bin").write_bytes(os.urandom(4_000_000))
    torrent_path = tmp_path / "contenido.torrent"
    dest = tmp_path / "descarga"
    dest.mkdir()

    seed = TorrentBackend()
    await seed.connect()
    try:
        await seed.create_torrent(str(folder), str(torrent_path))
        seed_port = seed._session.listen_port()

        leech = TorrentBackend()
        await leech.connect()
        info = lt.torrent_info(str(torrent_path))
        handle = leech._session.add_torrent({"ti": info, "save_path": str(dest)})
        leech._active[str(handle.info_hash())] = {"handle": handle, "download": None}
        handle.connect_peer(("127.0.0.1", seed_port))
        # Límite bajo para poder capturar un estado parcial (15-20%) en
        # vez de que la transferencia entera termine en una sola vuelta
        # del bucle, dado que va toda por loopback.
        handle.set_download_limit(50_000)

        deadline = asyncio.get_event_loop().time() + 20.0
        while asyncio.get_event_loop().time() < deadline:
            progress = handle.status().progress
            if progress >= 0.15:
                break
            await asyncio.sleep(0.2)
        partial_bytes = handle.status().total_wanted_done
        assert partial_bytes > 0, "la descarga no llegó a avanzar nada, no se puede probar el bug"

        # "cierra la app": exactamente lo que hace closeEvent, sin ningún
        # sleep extra después que disimule la carrera real.
        await leech.disconnect()

        download = Download(
            id=1, network=Network.TORRENT, title="contenido",
            source_id=f"file:{torrent_path}", dest_path=str(dest),
            size_bytes=4_000_000, state=DownloadState.SEARCHING_SOURCES,
        )
        leech2 = TorrentBackend()
        await leech2.connect()
        try:
            await leech2.reattach_download(download)
            entry = next(iter(leech2._active.values()))
            status = await _wait_checked(entry["handle"])
            assert status.total_wanted_done >= partial_bytes, (
                f"progreso perdido al reenganchar: tenía {partial_bytes} bytes "
                f"y tras el recheck solo quedan {status.total_wanted_done}"
            )
        finally:
            await leech2.disconnect()
    finally:
        await seed.disconnect()
