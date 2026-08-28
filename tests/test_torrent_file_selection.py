"""Selección de archivos de un torrent multi-archivo y su persistencia
entre reinicios de la app (bug real reportado por el usuario: al cerrar
la app a mitad de descarga y volver a abrirla, la selección de archivos
se perdía -el diálogo volvía a mostrar todo marcado- y el porcentaje de
descarga parecía "reiniciarse", porque `reattach_download` volvía a
pedir TODOS los archivos del torrent en vez de solo los que se habían
elegido antes.

`TorrentBackend.set_file_priorities`/`delete_deselected_files` se
prueban aquí contra una `lt.session` real y local (mismo patrón que el
resto de `test_torrent_backend.py`, vía `create_torrent()` para tener un
torrent multi-archivo con contenido de verdad en disco sin depender de
ningún peer remoto); `DownloadManager.set_file_priorities` se prueba con
un backend simulado, para aislar la lógica de persistencia/borrado de la
comunicación real con libtorrent."""

import asyncio

import pytest

from backends.torrent_backend import PRIORITY_NORMAL, PRIORITY_SKIP, TorrentBackend
from core import database
from core.download_manager import DownloadManager
from core.backend_base import BackendRegistry
from core.models import Download, DownloadState, Network


def _index_of(files: list[dict], suffix: str) -> int:
    return next(f["index"] for f in files if f["path"].endswith(suffix))


async def _create_two_file_torrent(backend: TorrentBackend, tmp_path) -> Download:
    folder = tmp_path / "carpeta"
    folder.mkdir()
    (folder / "a.bin").write_bytes(b"a" * 20000)
    (folder / "b.bin").write_bytes(b"b" * 20000)
    dest_torrent = tmp_path / "carpeta.torrent"
    return await backend.create_torrent(str(folder), str(dest_torrent))


# ---- DownloadManager.set_file_priorities: persistencia + borrado ----

class _RecordingBackend:
    network = Network.TORRENT

    def __init__(self) -> None:
        self.set_calls: list[dict] = []
        self.deleted_calls: list[list[int]] = []

    def set_file_priorities(self, download, priorities):
        self.set_calls.append(dict(priorities))

    def delete_deselected_files(self, download, indices):
        self.deleted_calls.append(list(indices))


@pytest.fixture
def _manager_with_fake_backend(monkeypatch):
    backend = _RecordingBackend()
    monkeypatch.setattr(BackendRegistry, "get", lambda network: backend if network == Network.TORRENT else None)
    manager = DownloadManager()
    return manager, backend


def _persisted_download(title="Mi torrent") -> Download:
    d = Download(
        id=None, network=Network.TORRENT, title=title,
        source_id="magnet:?xt=urn:btih:" + "a" * 40, dest_path="/tmp",
    )
    d.id = database.insert_download(d)
    return d


def test_set_file_priorities_persists_selection_across_reload(_manager_with_fake_backend):
    manager, backend = _manager_with_fake_backend
    download = _persisted_download()

    manager.set_file_priorities(download, {0: PRIORITY_NORMAL, 1: PRIORITY_SKIP})

    assert backend.set_calls == [{0: PRIORITY_NORMAL, 1: PRIORITY_SKIP}]
    assert download.file_priorities == {0: PRIORITY_NORMAL, 1: PRIORITY_SKIP}

    reloaded = next(d for d in database.load_all_downloads() if d.id == download.id)
    assert reloaded.file_priorities == {0: PRIORITY_NORMAL, 1: PRIORITY_SKIP}


def test_set_file_priorities_deletes_only_newly_deselected_files(_manager_with_fake_backend):
    manager, backend = _manager_with_fake_backend
    download = _persisted_download()

    # Primera selección: se desmarca el archivo 1.
    manager.set_file_priorities(download, {0: PRIORITY_NORMAL, 1: PRIORITY_SKIP})
    assert backend.deleted_calls == [[1]]

    # Se vuelve a abrir el diálogo y esta vez se desmarca también el 0,
    # dejando el 1 tal cual (ya estaba desmarcado): no debe repetirse el
    # borrado del archivo 1, solo el del 0, que es el que cambia ahora.
    manager.set_file_priorities(download, {0: PRIORITY_SKIP, 1: PRIORITY_SKIP})
    assert backend.deleted_calls == [[1], [0]]


def test_set_file_priorities_no_deletion_when_nothing_deselected(_manager_with_fake_backend):
    manager, backend = _manager_with_fake_backend
    download = _persisted_download()

    manager.set_file_priorities(download, {0: PRIORITY_NORMAL, 1: PRIORITY_NORMAL})

    assert backend.deleted_calls == []


# ---- TorrentBackend contra una sesión libtorrent real y local ----

@pytest.mark.asyncio
async def test_delete_deselected_files_removes_content_and_survives_recheck(tmp_path):
    backend = TorrentBackend()
    await backend.connect()
    try:
        download = await _create_two_file_torrent(backend, tmp_path)
        files = backend.list_files(download)
        # libtorrent puede intercalar archivos ".pad/" de relleno junto a
        # los dos reales (ver test_build_torrent_file_from_folder_includes_
        # all_files en test_torrent_backend.py), así que no se asume un
        # num_files() exacto -solo que los dos ficheros reales están.
        assert files is not None
        index_b = _index_of(files, "b.bin")
        save_path = backend._find_entry(download)["handle"].status().save_path
        path_b = next(f for f in files if f["index"] == index_b)["path"]

        backend.set_file_priorities(download, {index_b: PRIORITY_SKIP})
        backend.delete_deselected_files(download, [index_b])

        import os
        full_path = os.path.join(save_path, path_b)
        assert not os.path.exists(full_path)
        # El otro archivo (seleccionado) debe seguir intacto en disco.
        index_a = _index_of(files, "a.bin")
        path_a = next(f for f in files if f["index"] == index_a)["path"]
        assert os.path.exists(os.path.join(save_path, path_a))
    finally:
        await backend.disconnect()


@pytest.mark.asyncio
async def test_reattach_download_restores_file_priorities_for_local_torrent_file(tmp_path):
    """Núcleo del bug reportado: tras "reiniciar la app" (sesión libtorrent
    nueva), un torrent con selección de archivos guardada debe reenganchar
    con esa misma selección aplicada de inmediato, no con todo marcado."""
    backend = TorrentBackend()
    await backend.connect()
    try:
        download = await _create_two_file_torrent(backend, tmp_path)
        files = backend.list_files(download)
        index_b = _index_of(files, "b.bin")
        index_a = _index_of(files, "a.bin")
        download.file_priorities = {index_a: PRIORITY_NORMAL, index_b: PRIORITY_SKIP}
    finally:
        await backend.disconnect()

    # "Reinicio de la app": sesión y backend completamente nuevos.
    new_backend = TorrentBackend()
    await new_backend.connect()
    try:
        await new_backend.reattach_download(download)
        entry = new_backend._find_entry(download)
        assert entry is not None
        priorities = entry["handle"].file_priorities()
        assert priorities[index_b] == PRIORITY_SKIP
        assert priorities[index_a] == PRIORITY_NORMAL
    finally:
        await new_backend.disconnect()


@pytest.mark.asyncio
async def test_poll_loop_applies_pending_file_priorities_once_metadata_available(tmp_path):
    """Caso magnet (sin `ti` disponible de entrada al reenganchar): la
    selección queda pendiente hasta que `_poll_loop` detecta que ya hay
    metadatos, y se aplica una sola vez."""
    backend = TorrentBackend()
    await backend.connect()
    try:
        download = await _create_two_file_torrent(backend, tmp_path)
        files = backend.list_files(download)
        index_b = _index_of(files, "b.bin")
        entry = backend._find_entry(download)
        info_hash = str(entry["handle"].info_hash())

        # Metadatos ya disponibles (torrent recién creado), pero se simula
        # el caso "todavía pendiente de aplicar" tal y como lo dejaría
        # `reattach_download` para un magnet.
        backend._pending_file_priorities[info_hash] = {index_b: PRIORITY_SKIP}

        await asyncio.sleep(1.5)  # más de una vuelta de _poll_loop (1 s)

        assert info_hash not in backend._pending_file_priorities
        assert entry["handle"].file_priorities()[index_b] == PRIORITY_SKIP
    finally:
        await backend.disconnect()
