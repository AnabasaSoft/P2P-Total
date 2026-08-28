"""Bug real reportado por el usuario: al reintentar un torrent que
parecía atascado (volviendo a pulsar "Descargar" sobre el mismo
resultado de búsqueda), se creaba una segunda fila en Transferencias
con el mismo `source_id`. En BitTorrent, ambas comparten `info_hash`,
así que `TorrentBackend._active` (indexado por `info_hash`) acababa
sobrescribiendo en silencio la entrada activa de la primera descarga
con la de la segunda: la primera dejaba de recibir actualizaciones de
`_poll_loop` y quedaba congelada para siempre en un estado (ni
DOWNLOADING ni PAUSED) que ya no ofrecía pausar/reanudar en el menú
contextual.

`DownloadManager.download()` ahora rechaza añadir una segunda descarga
con el mismo `network`+`source_id` mientras la primera siga activa."""

import pytest

from core import database
from core.backend_base import BackendRegistry
from core.download_manager import DownloadManager
from core.models import Download, DownloadState, Network, SearchResult


class _FakeBackend:
    network = Network.TORRENT

    def __init__(self) -> None:
        self.start_calls = 0

    async def start_download(self, result: SearchResult, dest_path: str) -> Download:
        self.start_calls += 1
        return Download(
            id=None, network=Network.TORRENT, title=result.title,
            source_id=result.source_id, dest_path=dest_path,
            size_bytes=result.size_bytes, state=DownloadState.SEARCHING_SOURCES,
        )


@pytest.fixture
def _manager_with_fake_backend(monkeypatch):
    backend = _FakeBackend()
    monkeypatch.setattr(BackendRegistry, "get", lambda network: backend if network == Network.TORRENT else None)
    return DownloadManager(), backend


def _result(source_id="magnet:?xt=urn:btih:" + "a" * 40) -> SearchResult:
    return SearchResult(network=Network.TORRENT, title="t", size_bytes=100, source_id=source_id, seeds_or_sources=0)


@pytest.mark.asyncio
async def test_download_rejects_duplicate_of_active_download(_manager_with_fake_backend):
    manager, backend = _manager_with_fake_backend
    result = _result()

    first = await manager.download(result, "/tmp")
    assert backend.start_calls == 1

    with pytest.raises(RuntimeError):
        await manager.download(result, "/tmp")
    assert backend.start_calls == 1  # no se ha llegado a arrancar la segunda


@pytest.mark.asyncio
async def test_download_allows_retry_after_first_finished(_manager_with_fake_backend):
    manager, backend = _manager_with_fake_backend
    result = _result()

    first = await manager.download(result, "/tmp")
    database.update_download_progress(first.id, first.size_bytes, DownloadState.COMPLETED)

    second = await manager.download(result, "/tmp")
    assert backend.start_calls == 2
    assert second.id != first.id


@pytest.mark.asyncio
async def test_download_allows_different_source(_manager_with_fake_backend):
    manager, backend = _manager_with_fake_backend

    await manager.download(_result("magnet:?xt=urn:btih:" + "a" * 40), "/tmp")
    await manager.download(_result("magnet:?xt=urn:btih:" + "b" * 40), "/tmp")
    assert backend.start_calls == 2
