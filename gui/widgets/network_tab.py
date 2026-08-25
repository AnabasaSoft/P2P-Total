"""Pestaña de Red: una subpestaña por cada red soportada, con su
estado de conexión y toda la información que exponga su backend
(servidor/hub, puerto de escucha, nodos conocidos, descargas
activas...), al estilo de la pestaña "Servidores"/"Estadísticas" de
aMule pero con una subpestaña dedicada por red en vez de una sola
tabla plana -- así cabe información específica de cada protocolo
(p.ej. el estado de los trackers de BitTorrent) sin amontonarla toda
en una única columna de texto."""

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QHeaderView, QLabel, QMenu, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from core.download_manager import DownloadManager
from gui.connection_manager import (
    STATUS_CONNECTED, STATUS_CONNECTING, STATUS_DISCONNECTED, STATUS_ERROR, ConnectionManager,
)
from gui.i18n import t
from gui.models_qt import NETWORK_LABEL_KEYS
from gui.theme import NETWORK_COLORS, STATUS_DOT_COLORS
from gui.widgets.accessible_table import AccessibleTableWidget
from gui.widgets.browse_host_dialog import BrowseHostDialog
from core.models import Network

_STATUS_LABEL_KEYS = {
    STATUS_DISCONNECTED: "status_disconnected",
    STATUS_CONNECTING: "status_connecting",
    STATUS_CONNECTED: "status_connected",
    STATUS_ERROR: "status_error",
}

_STAT_LABEL_KEYS = {
    "server": "stat_server",
    "username": "stat_username",
    "nickname": "stat_nickname",
    "listen_port": "stat_listen_port",
    "dht_nodes": "stat_dht_nodes",
    "known_peers": "stat_known_peers",
    "active_transfers": "stat_active_transfers",
    "connected_peers": "stat_connected_peers",
    "encrypted_peers": "stat_encrypted_peers",
    "utp_connections": "stat_utp_connections",
    "shared_files": "stat_shared_files",
    "active_uploads": "stat_active_uploads",
    "id_status": "stat_id_status",
    "kad_firewalled": "stat_kad_firewalled",
    "upload_slots": "stat_upload_slots",
    "upload_queue": "stat_upload_queue",
    "external_ip": "stat_external_ip",
    "hub_name": "stat_hub_name",
    "hub_users": "stat_hub_users",
    "server_users": "stat_server_users",
    "server_files": "stat_server_files",
}

_STAT_VALUE_KEYS = {
    "id_status": {"high": "id_status_high", "low": "id_status_low"},
    "kad_firewalled": {"open": "kad_firewalled_open", "firewalled": "kad_firewalled_yes"},
}

_POLL_INTERVAL_MS = 2000


def _format_stats(stats: dict) -> str:
    parts = []
    for key, value in stats.items():
        if value is None:
            continue
        label = t(_STAT_LABEL_KEYS.get(key, key))
        value_key = _STAT_VALUE_KEYS.get(key, {}).get(value)
        display_value = t(value_key) if value_key else value
        parts.append(f"{label}: {display_value}")
    return " · ".join(parts) if parts else t("network_not_connected")


class _NetworkPage(QWidget):
    """Contenido de una subpestaña: estado de conexión + detalles del
    backend, más la tabla de trackers para BitTorrent."""

    COL_TR_TORRENT, COL_TR_URL, COL_TR_STATUS, COL_TR_SEEDS, COL_TR_PEERS = range(5)

    def __init__(self, network: Network, parent=None) -> None:
        super().__init__(parent)
        self.network = network
        layout = QVBoxLayout(self)

        self.status_label = QLabel(t("status_disconnected"))
        layout.addWidget(self.status_label)

        self.details_label = QLabel(t("network_not_connected"))
        self.details_label.setWordWrap(True)
        self.details_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.details_label)

        self.trackers_table: AccessibleTableWidget | None = None
        if network == Network.TORRENT:
            self.trackers_label = QLabel(t("network_tab_trackers"))
            layout.addWidget(self.trackers_label)

            table = AccessibleTableWidget(0, 5)
            table.setAccessibleName(t("acc_tracker_table"))
            table.setHorizontalHeaderLabels([
                t("col_tracker_torrent"), t("col_tracker_url"), t("col_tracker_status"),
                t("col_tracker_seeds"), t("col_tracker_peers"),
            ])
            table.verticalHeader().setVisible(False)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
            header = table.horizontalHeader()
            header.setSectionResizeMode(self.COL_TR_TORRENT, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(self.COL_TR_URL, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(self.COL_TR_STATUS, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(self.COL_TR_SEEDS, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(self.COL_TR_PEERS, QHeaderView.ResizeMode.ResizeToContents)
            layout.addWidget(table)
            self.trackers_table = table

        layout.addStretch()

    def set_status(self, status: str) -> None:
        self.status_label.setText(t(_STATUS_LABEL_KEYS.get(status, "status_disconnected")))
        color = STATUS_DOT_COLORS.get(status, STATUS_DOT_COLORS[STATUS_DISCONNECTED])
        self.status_label.setStyleSheet(f"color: {color}; font-weight: bold;")

    def set_details(self, stats: dict) -> None:
        self.details_label.setText(_format_stats(stats))

    def set_trackers(self, entries: list[dict]) -> None:
        if self.trackers_table is None:
            return
        table = self.trackers_table
        table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            table.setItem(row, self.COL_TR_TORRENT, QTableWidgetItem(entry["title"]))
            table.setItem(row, self.COL_TR_URL, QTableWidgetItem(entry["url"]))
            status_key = "tracker_working_yes" if entry["working"] else "tracker_working_no"
            table.setItem(row, self.COL_TR_STATUS, QTableWidgetItem(t(status_key)))
            seeds = entry["seeds"] if entry["seeds"] >= 0 else "?"
            peers = entry["peers"] if entry["peers"] >= 0 else "?"
            table.setItem(row, self.COL_TR_SEEDS, QTableWidgetItem(str(seeds)))
            table.setItem(row, self.COL_TR_PEERS, QTableWidgetItem(str(peers)))


class NetworkTab(QWidget):
    download_requested = pyqtSignal(object, str, object)  # SearchResult, dest_path, category (str | None)

    def __init__(self, connection_manager: ConnectionManager, download_manager: DownloadManager, parent=None) -> None:
        super().__init__(parent)
        self._manager = connection_manager
        self._download_manager = download_manager

        layout = QVBoxLayout(self)
        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)

        self._pages: dict[Network, _NetworkPage] = {}
        for network in Network:
            page = _NetworkPage(network)
            self._pages[network] = page
            self._tabs.addTab(page, t(NETWORK_LABEL_KEYS[network]))
            self._tabs.setTabIcon(self._tabs.indexOf(page), _network_color_icon(network))

        # Explorar hub (punto 10 del backlog, solo Gnutella2): único
        # botón contextual que sigue teniendo sentido tras el rediseño,
        # ligado directamente a la subpestaña de G2 (ya no hace falta
        # calcular sobre qué fila se hizo clic, solo hay una red por
        # página).
        g2_page = self._pages[Network.GNUTELLA2]
        g2_page.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        g2_page.customContextMenuRequested.connect(self._on_g2_context_menu)

        self._manager.status_changed.connect(self._on_status_changed)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_details)
        self._timer.start(_POLL_INTERVAL_MS)
        self._refresh_details()

    def _on_status_changed(self, network_value: str, status: str, _message: str) -> None:
        network = Network(network_value)
        self._pages[network].set_status(status)
        self._refresh_details()

    def _refresh_details(self) -> None:
        for network, page in self._pages.items():
            backend = self._manager.get_backend(network)
            stats = backend.get_stats() if backend is not None else {}
            page.set_details(stats)
        torrent_page = self._pages.get(Network.TORRENT)
        if torrent_page is not None:
            torrent_page.set_trackers(self._download_manager.list_active_torrent_trackers())

    def _on_g2_context_menu(self, pos) -> None:
        network = Network.GNUTELLA2
        backend = self._manager.get_backend(network)
        stats = backend.get_stats() if backend is not None else {}
        server = stats.get("server")
        if not server or ":" not in server:
            return
        host, port_str = server.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            return

        menu = QMenu(self)
        browse_action = menu.addAction(t("ctx_browse_host", host=server))
        action = menu.exec(self._pages[network].mapToGlobal(pos))
        if action == browse_action:
            dialog = BrowseHostDialog(self._download_manager, host, port, self)
            dialog.download_requested.connect(self.download_requested.emit)
            dialog.exec()


def _network_color_icon(network: Network) -> QIcon:
    """Un pequeño icono de color sólido por red para la pestaña, ya que
    QTabWidget no admite colorear el propio texto de la pestaña como sí
    hacía la columna COL_NETWORK de la tabla plana anterior."""
    pixmap = QPixmap(10, 10)
    pixmap.fill(QColor(NETWORK_COLORS[network]))
    return QIcon(pixmap)
