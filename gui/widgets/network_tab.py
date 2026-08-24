"""Pestaña de Red: una fila por cada una de las redes soportadas
con su estado de conexión y los detalles que exponga su backend
(servidor/hub, puerto de escucha, nodos conocidos, descargas
activas...), al estilo de la pestaña "Servidores"/"Estadísticas" de
aMule pero unificando todas las redes en una sola tabla."""

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QHeaderView, QMenu, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

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


class NetworkTab(QWidget):
    download_requested = pyqtSignal(object, str, object)  # SearchResult, dest_path, category (str | None)

    COL_NETWORK, COL_STATUS, COL_DETAILS = range(3)

    def __init__(self, connection_manager: ConnectionManager, download_manager: DownloadManager, parent=None) -> None:
        super().__init__(parent)
        self._manager = connection_manager
        self._download_manager = download_manager

        layout = QVBoxLayout(self)

        self._table = AccessibleTableWidget(len(Network), 3)
        self._table.setAccessibleName(t("acc_network_table"))
        self._table.setHorizontalHeaderLabels(
            [t("col_network"), t("col_network_status"), t("col_network_details")]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(self.COL_NETWORK, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_DETAILS, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table)

        self._rows: dict[Network, int] = {}
        for row, network in enumerate(Network):
            self._rows[network] = row
            self._init_row(row, network)

        self._manager.status_changed.connect(self._on_status_changed)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_details)
        self._timer.start(_POLL_INTERVAL_MS)
        self._refresh_details()

    def _init_row(self, row: int, network: Network) -> None:
        name_item = QTableWidgetItem(t(NETWORK_LABEL_KEYS[network]))
        name_item.setForeground(QColor(NETWORK_COLORS[network]))
        self._table.setItem(row, self.COL_NETWORK, name_item)
        status_item = QTableWidgetItem(t("status_disconnected"))
        status_item.setForeground(QColor(STATUS_DOT_COLORS[STATUS_DISCONNECTED]))
        self._table.setItem(row, self.COL_STATUS, status_item)
        self._table.setItem(row, self.COL_DETAILS, QTableWidgetItem(t("network_not_connected")))

    def _on_status_changed(self, network_value: str, status: str, _message: str) -> None:
        network = Network(network_value)
        row = self._rows[network]
        status_item = self._table.item(row, self.COL_STATUS)
        status_item.setText(t(_STATUS_LABEL_KEYS.get(status, "status_disconnected")))
        status_item.setForeground(QColor(STATUS_DOT_COLORS.get(status, STATUS_DOT_COLORS[STATUS_DISCONNECTED])))
        self._refresh_details()

    def _refresh_details(self) -> None:
        for network, row in self._rows.items():
            backend = self._manager.get_backend(network)
            stats = backend.get_stats() if backend is not None else {}
            self._table.item(row, self.COL_DETAILS).setText(_format_stats(stats))

    def _on_context_menu(self, pos) -> None:
        # Browse Host (punto 10 del backlog, solo Gnutella2): explorar
        # TODO lo que comparte el hub al que estamos conectados ahora
        # mismo, que es el único host G2 concreto del que ya conocemos
        # la dirección sin tener que buscar antes.
        row = self._table.rowAt(pos.y())
        if row < 0:
            return
        network = next((n for n, r in self._rows.items() if r == row), None)
        if network != Network.GNUTELLA2:
            return
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
        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == browse_action:
            dialog = BrowseHostDialog(self._download_manager, host, port, self)
            dialog.download_requested.connect(self.download_requested.emit)
            dialog.exec()
