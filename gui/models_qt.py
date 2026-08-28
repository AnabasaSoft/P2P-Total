"""
Modelos Qt (QAbstractTableModel) que envuelven los modelos de datos
comunes (core.models.SearchResult / Download) para mostrarlos en las
tablas de la GUI, sin que el resto del core tenga que saber nada de Qt.
"""

from PyQt6.QtCore import QAbstractTableModel, QMimeData, QModelIndex, QSortFilterProxyModel, Qt, pyqtSignal

from gui.i18n import t
from gui.theme import NETWORK_COLORS
from core.models import Download, DownloadState, Network, SearchResult

STATE_LABEL_KEYS = {
    DownloadState.QUEUED: "state_queued",
    DownloadState.SEARCHING_SOURCES: "state_searching_sources",
    DownloadState.DOWNLOADING: "state_downloading",
    DownloadState.PAUSED: "state_paused",
    DownloadState.COMPLETED: "state_completed",
    DownloadState.ERROR: "state_error",
    DownloadState.CANCELLED: "state_cancelled",
}

# Bug real reportado por el usuario: al arrancar sin estar conectado a
# una red, sus descargas seguían mostrando "Descargando"/"Buscando
# fuentes"/"En cola" -estados que venían de la última sesión, cuando sí
# había conexión- lo cual es imposible sin conexión. Solo estos estados
# "activos" dependen de que la red esté conectada; PAUSED/COMPLETED/
# ERROR/CANCELLED son igual de válidos con o sin conexión.
_CONNECTIVITY_DEPENDENT_STATES = {
    DownloadState.QUEUED,
    DownloadState.SEARCHING_SOURCES,
    DownloadState.DOWNLOADING,
}

NETWORK_LABEL_KEYS = {
    Network.TORRENT: "net_torrent",
    Network.SOULSEEK: "net_soulseek",
    Network.DCPP: "net_dcpp",
    Network.GNUTELLA2: "net_gnutella2",
    Network.EMULE: "net_emule",
}


def _format_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "?"
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _format_speed(speed_bps: float) -> str:
    if speed_bps <= 0:
        return ""
    return f"{_format_size(int(speed_bps))}/s"


class SearchResultsModel(QAbstractTableModel):
    COL_NETWORK, COL_TITLE, COL_SIZE, COL_SOURCES = range(4)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._results: list[SearchResult] = []

    def set_results(self, results: list[SearchResult]) -> None:
        self.beginResetModel()
        self._results = results
        self.endResetModel()

    def add_result(self, result: SearchResult) -> None:
        """Añade un único resultado ya llegado (búsqueda en curso, aún sin
        terminar el tiempo de espera) sin resetear los que ya había."""
        row = len(self._results)
        self.beginInsertRows(QModelIndex(), row, row)
        self._results.append(result)
        self.endInsertRows()

    def merge_source(self, row: int, result: SearchResult) -> None:
        """Un resultado nuevo resulta ser el mismo fichero (mismo título y
        tamaño) que uno ya mostrado en `row`: en vez de añadir una fila
        duplicada, se guarda como fuente alternativa y se actualiza el
        número de fuentes de esa fila."""
        existing = self._results[row]
        existing.alt_source_ids.append(result.source_id)
        existing.alt_source_ids.extend(result.alt_source_ids)
        existing.seeds_or_sources += result.seeds_or_sources
        index = self.index(row, self.COL_SOURCES)
        self.dataChanged.emit(index, index)

    def result_at(self, row: int) -> SearchResult:
        return self._results[row]

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._results)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 4

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return {
                self.COL_NETWORK: t("col_network"),
                self.COL_TITLE: t("col_title"),
                self.COL_SIZE: t("col_size"),
                self.COL_SOURCES: t("col_sources"),
            }[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        result = self._results[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == self.COL_NETWORK:
                return t(NETWORK_LABEL_KEYS[result.network])
            if col == self.COL_TITLE:
                return result.title
            if col == self.COL_SIZE:
                return _format_size(result.size_bytes)
            if col == self.COL_SOURCES:
                return str(result.seeds_or_sources)
        elif role == Qt.ItemDataRole.ForegroundRole and col == self.COL_NETWORK:
            from PyQt6.QtGui import QColor
            return QColor(NETWORK_COLORS[result.network])
        elif role == Qt.ItemDataRole.TextAlignmentRole and col in (self.COL_SIZE, self.COL_SOURCES):
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None


class SearchResultsSortProxy(QSortFilterProxyModel):
    """Ordena por el valor real (bytes, número de fuentes...) en vez de
    por el texto ya formateado que se muestra en la celda."""

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        model = self.sourceModel()
        col = left.column()
        left_result = model.result_at(left.row())
        right_result = model.result_at(right.row())
        if col == SearchResultsModel.COL_SIZE:
            return left_result.size_bytes < right_result.size_bytes
        if col == SearchResultsModel.COL_SOURCES:
            return left_result.seeds_or_sources < right_result.seeds_or_sources
        return super().lessThan(left, right)


class DownloadsModel(QAbstractTableModel):
    COL_NETWORK, COL_NAME, COL_PROGRESS, COL_STATE, COL_SPEED, COL_PEERS, COL_CATEGORY = range(7)
    PROGRESS_ROLE = Qt.ItemDataRole.UserRole + 1
    STATE_ROLE = Qt.ItemDataRole.UserRole + 2
    _MIME_TYPE = "application/x-p2ptotal-download-rows"

    # Emitida cuando el orden de las filas cambia (arrastrar filas o
    # subir/bajar desde el menú contextual): la pestaña la escucha para
    # persistir la nueva prioridad de cola en la base de datos.
    order_changed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._downloads: list[Download] = []
        # Ninguna red está conectada todavía al construir la ventana
        # principal (el auto-conectar configurado en Preferencias se
        # lanza después, ver `ConnectionManager.autoconnect_configured_
        # networks`), así que hasta que llegue el primer aviso real de
        # conexión hay que asumir que ninguna lo está.
        self._connected: dict[Network, bool] = {n: False for n in Network}

    def set_network_connected(self, network: Network, connected: bool) -> None:
        if self._connected.get(network) == connected:
            return
        self._connected[network] = connected
        for row, d in enumerate(self._downloads):
            if d.network == network:
                index = self.index(row, self.COL_STATE)
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole])

    def set_downloads(self, downloads: list[Download]) -> None:
        self.beginResetModel()
        self._downloads = downloads
        self.endResetModel()

    def add_download(self, download: Download) -> None:
        row = len(self._downloads)
        self.beginInsertRows(QModelIndex(), row, row)
        self._downloads.append(download)
        self.endInsertRows()

    def update_download(self, download: Download) -> None:
        for row, d in enumerate(self._downloads):
            if d.id is not None and d.id == download.id:
                self._downloads[row] = download
                self.dataChanged.emit(
                    self.index(row, 0), self.index(row, self.columnCount() - 1)
                )
                return

    def download_at(self, row: int) -> Download:
        return self._downloads[row]

    def remove_at(self, row: int) -> None:
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._downloads[row]
        self.endRemoveRows()

    def remove_by_id(self, download_id: int | None) -> None:
        for row, d in enumerate(self._downloads):
            if d.id == download_id:
                self.remove_at(row)
                return

    def downloads_in_order(self) -> list[Download]:
        return list(self._downloads)

    def move_row(self, row: int, delta: int) -> None:
        """Sube (`delta=-1`) o baja (`delta=1`) una fila una posición. No
        hace nada si ya está en un extremo (no-op seguro por los límites
        de `move_rows_to`)."""
        if delta == 0:
            return
        dest_row = row + delta if delta < 0 else row + delta + 1
        dest_row = max(0, min(dest_row, len(self._downloads)))
        self.move_rows_to([row], dest_row)

    def move_rows_to(self, rows: list[int], dest_row: int) -> None:
        """Mueve `rows` (índices actuales) para que queden justo antes de
        la posición `dest_row` del orden original. Usa un reset completo
        del modelo por simplicidad: la operación es manual y poco
        frecuente, no merece la pena la contabilidad fina de
        `beginMoveRows`."""
        rows = sorted(set(r for r in rows if 0 <= r < len(self._downloads)))
        if not rows:
            return
        moving = [self._downloads[r] for r in rows]
        remaining = [d for i, d in enumerate(self._downloads) if i not in rows]
        shift = sum(1 for r in rows if r < dest_row)
        insert_at = max(0, dest_row - shift)
        new_order = remaining[:insert_at] + moving + remaining[insert_at:]
        if new_order == self._downloads:
            return
        self.beginResetModel()
        self._downloads = new_order
        self.endResetModel()
        self.order_changed.emit()

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        base = super().flags(index)
        if not index.isValid():
            return base | Qt.ItemFlag.ItemIsDropEnabled
        return base | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction

    def mimeTypes(self) -> list[str]:
        return [self._MIME_TYPE]

    def mimeData(self, indexes) -> QMimeData:
        rows = sorted({index.row() for index in indexes})
        mime = QMimeData()
        mime.setData(self._MIME_TYPE, ",".join(str(r) for r in rows).encode())
        return mime

    def dropMimeData(self, data, action, row, column, parent) -> bool:
        if action != Qt.DropAction.MoveAction or not data.hasFormat(self._MIME_TYPE):
            return False
        source_rows = [int(x) for x in bytes(data.data(self._MIME_TYPE)).decode().split(",")]
        dest_row = row if row != -1 else (parent.row() if parent.isValid() else len(self._downloads))
        self.move_rows_to(source_rows, dest_row)
        # Ya hemos movido las filas a mano; si devolviéramos True, Qt
        # intentaría además borrar las filas de origen por su cuenta
        # (semántica de "acción de mover" del propio framework).
        return False

    def has_completed(self) -> bool:
        return any(d.state == DownloadState.COMPLETED for d in self._downloads)

    def active_count(self) -> int:
        return sum(1 for d in self._downloads if d.state == DownloadState.DOWNLOADING)

    def active_speed_bps(self) -> float:
        return sum(d.speed_bps for d in self._downloads if d.state == DownloadState.DOWNLOADING)

    def remove_completed(self) -> None:
        self.beginResetModel()
        self._downloads = [d for d in self._downloads if d.state != DownloadState.COMPLETED]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._downloads)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 7

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return {
                self.COL_NETWORK: t("col_network"),
                self.COL_NAME: t("col_name"),
                self.COL_PROGRESS: t("col_progress"),
                self.COL_STATE: t("col_state"),
                self.COL_SPEED: t("col_speed"),
                self.COL_PEERS: t("col_peers"),
                self.COL_CATEGORY: t("col_category"),
            }[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        d = self._downloads[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == self.COL_NETWORK:
                return t(NETWORK_LABEL_KEYS[d.network])
            if col == self.COL_NAME:
                return d.title
            if col == self.COL_STATE:
                if d.state in _CONNECTIVITY_DEPENDENT_STATES and not self._connected.get(d.network, False):
                    return t("state_disconnected")
                return t(STATE_LABEL_KEYS[d.state])
            if col == self.COL_SPEED:
                return _format_speed(d.speed_bps)
            if col == self.COL_PEERS:
                return str(self._peer_count(d))
            if col == self.COL_CATEGORY:
                return d.category or ""
        elif role == self.PROGRESS_ROLE and col == self.COL_PROGRESS:
            return d.progress
        elif role == self.STATE_ROLE and col == self.COL_PROGRESS:
            return d.state
        elif role == Qt.ItemDataRole.ForegroundRole and col == self.COL_NETWORK:
            from PyQt6.QtGui import QColor
            return QColor(NETWORK_COLORS[d.network])
        elif role == Qt.ItemDataRole.TextAlignmentRole and col == self.COL_PEERS:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None

    @staticmethod
    def _peer_count(d: Download) -> int:
        """BitTorrent es la única red con un enjambre real (varios
        peers a la vez); el resto de backends conectan con una única
        fuente por descarga, así que basta con derivarlo del estado."""
        if d.network == Network.TORRENT:
            return d.connected_peers
        return 1 if d.state == DownloadState.DOWNLOADING else 0


class DownloadsSortProxy(QSortFilterProxyModel):
    """Ordena por el valor real (progreso, velocidad, número de pares...)
    en vez de por el texto ya formateado que se muestra en la celda."""

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        model = self.sourceModel()
        col = left.column()
        left_download = model.download_at(left.row())
        right_download = model.download_at(right.row())
        if col == DownloadsModel.COL_PROGRESS:
            return left_download.progress < right_download.progress
        if col == DownloadsModel.COL_SPEED:
            return left_download.speed_bps < right_download.speed_bps
        if col == DownloadsModel.COL_PEERS:
            return DownloadsModel._peer_count(left_download) < DownloadsModel._peer_count(right_download)
        return super().lessThan(left, right)
