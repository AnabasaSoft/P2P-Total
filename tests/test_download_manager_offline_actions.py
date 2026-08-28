"""Bug real reportado por el usuario: pulsar "Pausar"/"Reanudar" en el
menú contextual de una descarga cuya red no está conectada no hacía
nada visible. Causa: `DownloadManager.pause`/`resume`/`cancel` pedían el
backend al `BackendRegistry` sin comprobar si era `None` -a diferencia
de `restart`, que sí lo comprobaba-, así que sobre una red desconectada
acababan llamando a un método sobre `None`; como se invocan siempre
envueltos en `asyncio.ensure_future()`, ese `AttributeError` no llegaba
a verse en ningún sitio (aparte de un aviso de consola de "Task
exception was never retrieved"). Ahora las tres lanzan el mismo
`RuntimeError` explícito que ya lanzaba `restart`."""

import pytest

from core.backend_base import BackendRegistry
from core.download_manager import DownloadManager
from core.models import Download, DownloadState, Network


@pytest.fixture(autouse=True)
def _no_backend_registered(monkeypatch):
    monkeypatch.setattr(BackendRegistry, "get", lambda network: None)


def _download() -> Download:
    return Download(
        id=1, network=Network.TORRENT, title="x", source_id="magnet:?xt=urn:btih:" + "a" * 40,
        dest_path="/tmp", size_bytes=100, state=DownloadState.DOWNLOADING,
    )


@pytest.mark.asyncio
async def test_pause_raises_clear_error_when_network_disconnected():
    manager = DownloadManager()
    with pytest.raises(RuntimeError):
        await manager.pause(_download())


@pytest.mark.asyncio
async def test_resume_raises_clear_error_when_network_disconnected():
    manager = DownloadManager()
    with pytest.raises(RuntimeError):
        await manager.resume(_download())


@pytest.mark.asyncio
async def test_cancel_raises_clear_error_when_network_disconnected():
    manager = DownloadManager()
    with pytest.raises(RuntimeError):
        await manager.cancel(_download())
