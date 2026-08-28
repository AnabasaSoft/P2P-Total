"""Bug real reportado por el usuario: al arrancar sin conexión a una red
(p.ej. BitTorrent), sus descargas seguían mostrando "Descargando"/
"Buscando fuentes"/"En cola" -el estado persistido de la última sesión en
la que sí hubo conexión-, lo cual es imposible sin conexión: debía poner
"Sin conectar" en su lugar.

`DownloadsModel` ahora conoce qué redes están conectadas
(`set_network_connected`, alimentado por las señales `status_changed` de
`ConnectionManager`) y sobrescribe la columna de estado para los estados
"activos" que no tienen sentido sin conexión, dejando intactos los que sí
(pausado, completado, error, cancelado)."""

from gui.models_qt import DownloadsModel
from core.models import Download, DownloadState, Network


def _download(state: DownloadState, network: Network = Network.TORRENT) -> Download:
    return Download(
        id=1, network=network, title="x", source_id="s", dest_path="/tmp",
        size_bytes=100, state=state,
    )


def test_active_state_shows_disconnected_when_network_not_connected():
    model = DownloadsModel()
    model.set_downloads([_download(DownloadState.DOWNLOADING)])
    index = model.index(0, DownloadsModel.COL_STATE)

    assert model.data(index) == "Sin conectar"


def test_active_state_shows_real_state_once_connected():
    model = DownloadsModel()
    model.set_downloads([_download(DownloadState.DOWNLOADING)])
    index = model.index(0, DownloadsModel.COL_STATE)

    model.set_network_connected(Network.TORRENT, True)

    assert model.data(index) == "Descargando"


def test_disconnection_after_load_reverts_display_to_disconnected():
    model = DownloadsModel()
    model.set_downloads([_download(DownloadState.SEARCHING_SOURCES)])
    index = model.index(0, DownloadsModel.COL_STATE)
    model.set_network_connected(Network.TORRENT, True)
    assert model.data(index) == "Buscando fuentes"

    model.set_network_connected(Network.TORRENT, False)

    assert model.data(index) == "Sin conectar"


def test_connectivity_independent_states_are_never_overridden():
    model = DownloadsModel()
    model.set_downloads([_download(DownloadState.PAUSED)])
    index = model.index(0, DownloadsModel.COL_STATE)

    assert model.data(index) == "Pausado"


def test_other_networks_are_unaffected():
    model = DownloadsModel()
    model.set_downloads([_download(DownloadState.DOWNLOADING, network=Network.SOULSEEK)])
    index = model.index(0, DownloadsModel.COL_STATE)

    model.set_network_connected(Network.TORRENT, True)

    assert model.data(index) == "Sin conectar"
