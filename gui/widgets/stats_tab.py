"""Pestaña de Estadísticas (punto 24 del backlog): totales acumulados
de subida/bajada, ratio y tiempo conectado por red, más un histórico
diario de los últimos 30 días — a diferencia de la pestaña Red, que
solo muestra el estado instantáneo de la conexión en curso y se pierde
al reconectar (ver `core.stats_tracker`, que es quien persiste estos
totales en SQLite)."""

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QHeaderView, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from core import database
from core.models import Network
from core.stats_tracker import stats_tracker
from gui.connection_manager import ConnectionManager
from gui.i18n import t
from gui.models_qt import NETWORK_LABEL_KEYS
from gui.theme import NETWORK_COLORS

_POLL_INTERVAL_MS = 2000
_HISTORY_DAYS = 30


def _format_bytes(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _format_duration(total_seconds: float) -> str:
    total_seconds = int(total_seconds)
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


class StatsTab(QWidget):
    COL_NETWORK, COL_UPLOADED, COL_DOWNLOADED, COL_RATIO, COL_CONNECTED_TIME = range(5)
    HIST_COL_DATE, HIST_COL_NETWORK, HIST_COL_DOWNLOADED, HIST_COL_UPLOADED = range(4)

    def __init__(self, connection_manager: ConnectionManager, parent=None) -> None:
        super().__init__(parent)
        self._manager = connection_manager

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(t("stats_totals_title")))
        self._totals_table = QTableWidget(len(Network), 5)
        self._totals_table.setHorizontalHeaderLabels([
            t("col_network"), t("col_stats_uploaded"), t("col_stats_downloaded"),
            t("col_stats_ratio"), t("col_stats_connected_time"),
        ])
        self._totals_table.verticalHeader().setVisible(False)
        self._totals_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._totals_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        totals_header = self._totals_table.horizontalHeader()
        totals_header.setSectionResizeMode(self.COL_NETWORK, QHeaderView.ResizeMode.ResizeToContents)
        for col in (self.COL_UPLOADED, self.COL_DOWNLOADED, self.COL_RATIO, self.COL_CONNECTED_TIME):
            totals_header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._totals_table)

        self._rows: dict[Network, int] = {}
        for row, network in enumerate(Network):
            self._rows[network] = row
            self._init_totals_row(row, network)

        layout.addWidget(QLabel(t("stats_history_title")))
        self._history_table = QTableWidget(0, 4)
        self._history_table.setHorizontalHeaderLabels([
            t("col_stats_date"), t("col_network"), t("col_stats_downloaded"), t("col_stats_uploaded"),
        ])
        self._history_table.verticalHeader().setVisible(False)
        self._history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._history_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._history_table)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(_POLL_INTERVAL_MS)
        self._refresh()

    def _init_totals_row(self, row: int, network: Network) -> None:
        name_item = QTableWidgetItem(t(NETWORK_LABEL_KEYS[network]))
        name_item.setForeground(QColor(NETWORK_COLORS[network]))
        self._totals_table.setItem(row, self.COL_NETWORK, name_item)
        for col in (self.COL_UPLOADED, self.COL_DOWNLOADED, self.COL_RATIO, self.COL_CONNECTED_TIME):
            self._totals_table.setItem(row, col, QTableWidgetItem(""))

    def _refresh(self) -> None:
        # Vuelca el tiempo conectado de las redes que sigan activas
        # ahora mismo antes de leer la base de datos, para que el
        # contador se vea avanzar en vivo sin esperar a desconectar.
        for network in self._manager.connected_networks():
            stats_tracker.flush_connected_time(network)

        stats = database.get_all_network_stats()
        for network, row in self._rows.items():
            entry = stats.get(network.value, {})
            downloaded = entry.get("total_downloaded_bytes", 0)
            uploaded = entry.get("total_uploaded_bytes", 0)
            connected_seconds = entry.get("total_connected_seconds", 0)
            ratio = (uploaded / downloaded) if downloaded else 0.0
            self._totals_table.item(row, self.COL_UPLOADED).setText(_format_bytes(uploaded))
            self._totals_table.item(row, self.COL_DOWNLOADED).setText(_format_bytes(downloaded))
            self._totals_table.item(row, self.COL_RATIO).setText(f"{ratio:.2f}")
            self._totals_table.item(row, self.COL_CONNECTED_TIME).setText(_format_duration(connected_seconds))

        self._refresh_history()

    def _refresh_history(self) -> None:
        rows = database.get_network_stats_daily(_HISTORY_DAYS)
        self._history_table.setRowCount(len(rows))
        for row_index, entry in enumerate(rows):
            network = Network(entry["network"])
            self._history_table.setItem(row_index, self.HIST_COL_DATE, QTableWidgetItem(entry["date"]))
            network_item = QTableWidgetItem(t(NETWORK_LABEL_KEYS[network]))
            network_item.setForeground(QColor(NETWORK_COLORS[network]))
            self._history_table.setItem(row_index, self.HIST_COL_NETWORK, network_item)
            self._history_table.setItem(
                row_index, self.HIST_COL_DOWNLOADED, QTableWidgetItem(_format_bytes(entry["downloaded_bytes"]))
            )
            self._history_table.setItem(
                row_index, self.HIST_COL_UPLOADED, QTableWidgetItem(_format_bytes(entry["uploaded_bytes"]))
            )
