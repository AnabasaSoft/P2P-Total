"""core/models.py: sobre todo la propiedad calculada Download.progress."""

from core.models import Download, DownloadState, Network


def _download(**kwargs) -> Download:
    base = dict(id=1, network=Network.TORRENT, title="t", source_id="s", dest_path="/tmp/x")
    base.update(kwargs)
    return Download(**base)


def test_progress_completed_is_always_one():
    d = _download(state=DownloadState.COMPLETED, size_bytes=0, downloaded_bytes=0)
    assert d.progress == 1.0


def test_progress_zero_size_and_not_completed_is_zero():
    d = _download(state=DownloadState.DOWNLOADING, size_bytes=0, downloaded_bytes=0)
    assert d.progress == 0.0


def test_progress_normal_ratio():
    d = _download(state=DownloadState.DOWNLOADING, size_bytes=200, downloaded_bytes=50)
    assert d.progress == 0.25


def test_progress_clamped_to_one_even_if_downloaded_overshoots():
    d = _download(state=DownloadState.DOWNLOADING, size_bytes=100, downloaded_bytes=150)
    assert d.progress == 1.0


def test_network_values_are_stable_strings():
    # La GUI y config.json persisten estos valores tal cual (son
    # str-Enum): cambiar el string de un miembro existente rompería
    # config.json/downloads.db ya guardados por usuarios reales.
    assert Network.TORRENT.value == "torrent"
    assert Network.SOULSEEK.value == "soulseek"
    assert Network.DCPP.value == "dcpp"
    assert Network.GNUTELLA2.value == "gnutella2"
    assert Network.EMULE.value == "emule"
