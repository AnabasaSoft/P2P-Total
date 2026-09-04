"""Fallo real encontrado tras completar el punto 44 del backlog (fase
2): `downloads_tab.py` ofrecía "Pausar" -y "Pausar todo"- para una
descarga agregada que incluye BitTorrent, aunque
`AggregatedDownloadSession.pause()` la rechaza siempre
(`AggregatedDownloadError`, ver `core/aggregated_download.py`). Como la
acción se dispara con `asyncio.ensure_future(self._manager.pause(...))`
sin manejo de excepción, el fallo se tragaba en silencio -sin ningún
aviso en la GUI-. Arreglado con `_aggregated_has_torrent()`/
`_is_pausable()`, que decodifican el `source_id` combinado (vive en
`core/models.py`, sin arrastrar backends) para no ofrecer la acción."""

from core.models import Download, DownloadState, Network, SearchResult, encode_combined_source_id
from gui.widgets.downloads_tab import _aggregated_has_torrent, _is_pausable


def _fake_result(network: Network, size: int = 1000) -> SearchResult:
    return SearchResult(network=network, title="x.bin", size_bytes=size, source_id="irrelevante")


def _aggregated_download(sources: dict, state=DownloadState.DOWNLOADING) -> Download:
    return Download(
        id=1, network=Network.AGGREGATED, title="x.bin",
        source_id=encode_combined_source_id(sources), dest_path="/tmp",
        size_bytes=1000, state=state,
    )


class _FakeModel:
    """Sustituye a `DownloadsModel` en estos tests: solo hace falta
    `is_network_connected`, que para `Network.AGGREGATED` siempre
    devuelve `True` en el modelo real."""

    def is_network_connected(self, network: Network) -> bool:
        return True


def test_aggregated_has_torrent_true_when_torrent_participates():
    sources = {
        Network.TORRENT: _fake_result(Network.TORRENT),
        Network.GNUTELLA2: _fake_result(Network.GNUTELLA2),
    }
    assert _aggregated_has_torrent(_aggregated_download(sources)) is True


def test_aggregated_has_torrent_false_without_torrent():
    sources = {
        Network.GNUTELLA2: _fake_result(Network.GNUTELLA2),
        Network.EMULE: _fake_result(Network.EMULE),
    }
    assert _aggregated_has_torrent(_aggregated_download(sources)) is False


def test_aggregated_has_torrent_false_for_real_networks():
    """No debe intentar decodificar `source_id` para una descarga normal
    (no agregada), cuyo `source_id` no tiene ese formato en absoluto."""
    download = Download(
        id=1, network=Network.TORRENT, title="x", source_id="magnet:?xt=...",
        dest_path="/tmp", size_bytes=1000, state=DownloadState.DOWNLOADING,
    )
    assert _aggregated_has_torrent(download) is False


def test_is_pausable_rejects_aggregated_download_with_torrent():
    sources = {
        Network.TORRENT: _fake_result(Network.TORRENT),
        Network.EMULE: _fake_result(Network.EMULE),
    }
    download = _aggregated_download(sources)
    assert _is_pausable(download, _FakeModel()) is False


def test_is_pausable_accepts_aggregated_download_without_torrent():
    sources = {
        Network.GNUTELLA2: _fake_result(Network.GNUTELLA2),
        Network.EMULE: _fake_result(Network.EMULE),
    }
    download = _aggregated_download(sources)
    assert _is_pausable(download, _FakeModel()) is True
