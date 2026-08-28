"""Pestaña de búsqueda: caja de texto, filtro de redes y una pestaña
independiente por cada búsqueda realizada (con su propia tabla de
resultados, ordenación y menú contextual de descarga), al estilo de
pestañas de navegador -- cada una con una "X" para cerrarla."""

import asyncio
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from core.config import Category, load_config
from core.download_manager import DownloadManager
from core.file_types import matches_file_type
from core.models import Network, SearchResult
from core.saved_search_manager import SavedSearchManager
from gui.connection_manager import ConnectionManager, STATUS_CONNECTED
from gui.i18n import t
from gui.models_qt import NETWORK_LABEL_KEYS, SearchResultsModel, SearchResultsSortProxy
from gui.widgets.accessible_table import AccessibleTableView
from gui.widgets.browse_user_dialog import BrowseUserDialog

_TAB_TITLE_MAX_LEN = 20

_FILE_TYPE_ORDER = ["all", "archive", "audio", "cdimage", "picture", "program", "document", "video"]
_FILE_TYPE_LABEL_KEYS = {
    "all": "filetype_all",
    "archive": "filetype_archive",
    "audio": "filetype_audio",
    "cdimage": "filetype_cdimage",
    "picture": "filetype_picture",
    "program": "filetype_program",
    "document": "filetype_document",
    "video": "filetype_video",
}


class SearchResultsPanel(QWidget):
    """Contenido de una pestaña de resultados: modelo, proxy de
    ordenación, tabla y menú contextual de descarga de una única
    búsqueda. Se crea una instancia nueva por cada búsqueda lanzada."""

    download_requested = pyqtSignal(object, str, object)  # SearchResult, dest_path, category (str | None)

    def __init__(self, manager: DownloadManager, parent=None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._search_task: asyncio.Task | None = None
        self._query = ""
        self._networks: list[Network] = []
        self._file_type = "all"
        self._min_size_bytes = 0
        self._max_size_bytes = 0
        # Clave (red, título, tamaño) -> fila, para fusionar en la misma
        # fila un fichero encontrado varias veces (varias fuentes del
        # mismo fichero, o el mismo fichero al "seguir buscando"), tanto
        # dentro de una búsqueda como entre la búsqueda inicial y las
        # ampliaciones posteriores.
        self._merge_index: dict[tuple[Network, str, int], int] = {}
        # Mientras el menú contextual está abierto, los resultados que van
        # llegando en segundo plano (streaming de Soulseek) se guardan aquí
        # en vez de aplicarse al modelo al momento: insertar filas con la
        # ordenación activada (`setSortingEnabled(True)`) fuerza un
        # reordenamiento del proxy, y ese cambio de layout mientras
        # `QMenu.exec()` sigue abierto hace que el menú se cierre solo (a
        # veces incluso antes de llegar a mostrarse). Se aplican de golpe
        # en cuanto el menú se cierra.
        self._menu_open = False
        self._pending_results: list[SearchResult] = []
        # Si la búsqueda termina mientras el menú sigue abierto, el
        # mensaje de estado final ("N resultados" y el botón "Seguir
        # buscando") también se retrasa hasta el vaciado del buffer.
        self._pending_finalize = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)

        status_row = QHBoxLayout()
        self._status_label = QLabel("")
        self._status_label.setObjectName("dimLabel")
        status_row.addWidget(self._status_label, stretch=1)
        self._continue_button = QPushButton(t("btn_continue_search"))
        self._continue_button.setVisible(False)
        self._continue_button.clicked.connect(self._on_continue_clicked)
        status_row.addWidget(self._continue_button)
        layout.addLayout(status_row)

        self._model = SearchResultsModel(self)
        self._proxy = SearchResultsSortProxy(self)
        self._proxy.setSourceModel(self._model)
        self._table = AccessibleTableView()
        self._table.setAccessibleName(t("acc_search_results_table"))
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(
            SearchResultsModel.COL_TITLE, QHeaderView.ResizeMode.Stretch
        )
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.doubleClicked.connect(lambda _: self._download_selected())
        layout.addWidget(self._table, stretch=1)

    def run_search(self, query: str, networks: list[Network], file_type: str = "all",
                    min_size_bytes: int = 0, max_size_bytes: int = 0) -> None:
        self._query = query
        self._networks = networks
        self._file_type = file_type
        self._min_size_bytes = min_size_bytes
        self._max_size_bytes = max_size_bytes
        self._merge_index = {}
        self._model.set_results([])
        self._continue_button.setVisible(False)
        self._search_task = asyncio.ensure_future(self._do_search())

    def _on_continue_clicked(self) -> None:
        """Vuelve a lanzar la misma búsqueda (mismos términos y redes) al
        acabarse el tiempo de espera, sin borrar lo ya encontrado: los
        resultados nuevos se añaden a la tabla existente, fusionándose
        con los que ya hubiera del mismo fichero."""
        self._continue_button.setVisible(False)
        self._search_task = asyncio.ensure_future(self._do_search())

    def cancel(self) -> None:
        """Se llama al cerrar la pestaña: si la búsqueda seguía en
        curso (streaming de Soulseek, por ejemplo), se cancela la
        tarea en vez de dejarla actualizando un modelo ya destruido."""
        if self._search_task is not None and not self._search_task.done():
            self._search_task.cancel()

    def _add_or_merge(self, result: SearchResult) -> None:
        # Varios resultados pueden ser el mismo fichero compartido por
        # varias fuentes (típico en Soulseek: decenas de usuarios con el
        # mismo archivo exacto), o el mismo fichero encontrado otra vez al
        # "seguir buscando". En vez de mostrar una fila por fuente, se
        # fusionan en una sola fila por (red, título, tamaño) y el número
        # de fuentes se suma en la columna "Fuentes" — así al pedir la
        # descarga de esa fila, el backend puede probar varias fuentes y
        # quedarse con la primera que empiece a transferir datos (ver
        # SoulseekBackend.start_download).
        if not matches_file_type(result.title, self._file_type):
            return
        if self._min_size_bytes and result.size_bytes < self._min_size_bytes:
            return
        if self._max_size_bytes and result.size_bytes > self._max_size_bytes:
            return
        key = (result.network, result.title, result.size_bytes)
        row = self._merge_index.get(key)
        if row is not None:
            self._model.merge_source(row, result)
            return
        self._merge_index[key] = self._model.rowCount()
        self._model.add_result(result)

    async def _do_search(self) -> None:
        query, networks = self._query, self._networks
        self._status_label.setText(t("msg_searching"))

        # Soulseek soporta streaming: cada resultado se añade a la tabla
        # (o se fusiona en la fila existente) nada más llegar, sin esperar
        # a que termine el tiempo de espera de la búsqueda. Se descartan
        # los resultados sin hueco libre, igual que antes.
        def on_soulseek_result(result: SearchResult) -> None:
            if not result.extra.get("has_free_slots"):
                return
            if self._menu_open:
                self._pending_results.append(result)
                return
            self._add_or_merge(result)
            self._status_label.setText(t("msg_searching_count", n=self._model.rowCount()))

        config = load_config()
        try:
            results = await self._manager.search_all(
                query,
                networks=networks,
                on_result=on_soulseek_result,
                torrent_timeout=config.torrent.search_timeout,
                torrent_max_results=config.torrent.max_results or None,
                soulseek_timeout=config.soulseek.search_timeout,
                soulseek_max_results=config.soulseek.max_results or None,
                gnutella2_timeout=config.gnutella2.search_timeout,
                gnutella2_max_results=config.gnutella2.max_results or None,
                emule_timeout=config.emule.search_timeout,
                emule_max_results=config.emule.max_results or None,
                dcpp_timeout=config.dcpp.search_timeout,
                dcpp_max_results=config.dcpp.max_results or None,
                file_type=self._file_type,
            )
        except asyncio.CancelledError:
            return

        # Las redes que no son Soulseek no soportan streaming todavía:
        # sus resultados llegan de golpe al terminar y se añaden (o
        # fusionan) ahora, salvo que el menú contextual esté abierto en
        # ese preciso instante (ver on_soulseek_result más arriba).
        for result in results:
            if result.network != Network.SOULSEEK:
                if self._menu_open:
                    self._pending_results.append(result)
                else:
                    self._add_or_merge(result)

        if self._menu_open:
            self._pending_finalize = True
            return

        self._finalize_search_status()

    def _finalize_search_status(self) -> None:
        total = self._model.rowCount()
        self._status_label.setText(t("msg_no_results") if total == 0 else t("status_results_count", n=total))
        # El tiempo de búsqueda configurado se ha agotado: se ofrece
        # ampliarla en vez de tener que repetirla a mano en una pestaña
        # nueva (aplica a todas las redes, no solo a la que soporta
        # streaming).
        self._continue_button.setVisible(True)

    def _on_context_menu(self, pos) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        selection = self._table.selectionModel()
        if not selection.isRowSelected(index.row(), index.parent()):
            self._table.selectRow(index.row())
        menu = QMenu(self)
        download_action = menu.addAction(t("ctx_download"))
        categories = load_config().categories
        category_menu = None
        category_actions: dict = {}
        if categories:
            category_menu = menu.addMenu(t("ctx_download_to_category"))
            for category in categories:
                category_actions[category_menu.addAction(category.name)] = category
        copy_action = menu.addAction(t("ctx_copy_source_id"))
        selected_rows = self._selected_rows()
        copy_action.setEnabled(len(selected_rows) == 1)
        browse_action = None
        if len(selected_rows) == 1:
            single_result = self._model.result_at(selected_rows[0])
            if single_result.network == Network.SOULSEEK and single_result.extra.get("username"):
                browse_action = menu.addAction(t("ctx_browse_user"))

        self._menu_open = True
        try:
            action = menu.exec(self._table.viewport().mapToGlobal(pos))
        finally:
            self._menu_open = False
            self._flush_pending_results()

        if action == download_action:
            self._download_selected()
        elif action in category_actions:
            self._download_selected(category=category_actions[action])
        elif action == copy_action:
            result = self._model.result_at(selected_rows[0])
            QApplication.clipboard().setText(result.source_id)
        elif action == browse_action:
            result = self._model.result_at(selected_rows[0])
            dialog = BrowseUserDialog(self._manager, result.extra["username"], self)
            dialog.download_requested.connect(self.download_requested.emit)
            dialog.exec()

    def _flush_pending_results(self) -> None:
        had_pending = bool(self._pending_results)
        if had_pending:
            pending, self._pending_results = self._pending_results, []
            for result in pending:
                self._add_or_merge(result)
        if self._pending_finalize:
            self._pending_finalize = False
            self._finalize_search_status()
        elif had_pending:
            self._status_label.setText(t("msg_searching_count", n=self._model.rowCount()))

    def _selected_rows(self) -> list[int]:
        return sorted({
            self._proxy.mapToSource(index).row()
            for index in self._table.selectionModel().selectedRows()
        })

    def _download_selected(self, category: Category | None = None) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        dest_dir = category.dest_dir if category is not None else load_config().default_download_dir
        try:
            Path(dest_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(
                self, t("app_title"), t("msg_download_dir_error", dir=dest_dir, error=str(exc))
            )
            return
        for row in rows:
            result = self._model.result_at(row)
            self.download_requested.emit(result, dest_dir, category.name if category is not None else None)


class SearchTab(QWidget):
    download_requested = pyqtSignal(object, str, object)  # SearchResult, dest_path, category (str | None)

    def __init__(self, download_manager: DownloadManager, connection_manager: ConnectionManager,
                 saved_search_manager: SavedSearchManager, parent=None) -> None:
        super().__init__(parent)
        self._manager = download_manager
        self._connections = connection_manager
        self._saved_searches = saved_search_manager

        layout = QVBoxLayout(self)

        search_row = QHBoxLayout()
        self._query_edit = QLineEdit()
        self._query_edit.setPlaceholderText(t("search_placeholder"))
        self._query_edit.setAccessibleName(t("acc_search_query"))
        self._query_edit.returnPressed.connect(self._on_search_clicked)
        search_row.addWidget(self._query_edit, stretch=1)
        search_row.addWidget(QLabel(t("lbl_file_type")))
        self._file_type_combo = QComboBox()
        for key in _FILE_TYPE_ORDER:
            self._file_type_combo.addItem(t(_FILE_TYPE_LABEL_KEYS[key]), key)
        search_row.addWidget(self._file_type_combo)
        self._search_button = QPushButton(t("btn_search"))
        self._search_button.setObjectName("primary")
        self._search_button.clicked.connect(self._on_search_clicked)
        search_row.addWidget(self._search_button)
        self._history_button = QPushButton(t("btn_search_history"))
        self._history_button.clicked.connect(self._on_history_clicked)
        search_row.addWidget(self._history_button)
        self._save_alert_button = QPushButton(t("btn_save_as_alert"))
        self._save_alert_button.clicked.connect(self._on_save_alert_clicked)
        search_row.addWidget(self._save_alert_button)
        layout.addLayout(search_row)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel(t("lbl_networks_filter")))
        self._network_checks: dict[Network, QCheckBox] = {}
        for network in Network:
            box = QCheckBox(t(NETWORK_LABEL_KEYS[network]))
            box.setEnabled(False)
            self._network_checks[network] = box
            filter_row.addWidget(box)
        filter_row.addStretch(1)
        filter_row.addWidget(QLabel(t("lbl_min_size")))
        self._min_size_spin = QSpinBox()
        self._min_size_spin.setRange(0, 1_000_000)
        self._min_size_spin.setSuffix(" MB")
        self._min_size_spin.setSpecialValueText(t("spin_unlimited_speed"))
        self._min_size_spin.setAccessibleName(t("lbl_min_size"))
        filter_row.addWidget(self._min_size_spin)
        filter_row.addWidget(QLabel(t("lbl_max_size")))
        self._max_size_spin = QSpinBox()
        self._max_size_spin.setRange(0, 1_000_000)
        self._max_size_spin.setSuffix(" MB")
        self._max_size_spin.setSpecialValueText(t("spin_unlimited_speed"))
        self._max_size_spin.setAccessibleName(t("lbl_max_size"))
        filter_row.addWidget(self._max_size_spin)
        layout.addLayout(filter_row)

        self._status_label = QLabel("")
        self._status_label.setObjectName("dimLabel")
        layout.addWidget(self._status_label)

        self._results_tabs = QTabWidget()
        self._results_tabs.setTabsClosable(True)
        self._results_tabs.setDocumentMode(True)
        self._results_tabs.setMovable(True)
        self._results_tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        layout.addWidget(self._results_tabs, stretch=1)

        connection_manager.status_changed.connect(self._on_connection_status_changed)

    def _on_connection_status_changed(self, network_value: str, status: str, _message: str) -> None:
        network = Network(network_value)
        box = self._network_checks[network]
        connected = status == STATUS_CONNECTED
        box.setEnabled(connected)
        box.setChecked(connected)

    def _selected_networks(self) -> list[Network]:
        return [n for n, box in self._network_checks.items() if box.isChecked() and box.isEnabled()]

    def _on_search_clicked(self) -> None:
        query = self._query_edit.text().strip()
        if not query:
            return
        networks = self._selected_networks()
        if not networks:
            self._status_label.setText(t("msg_select_networks"))
            return
        self._status_label.setText("")

        file_type = self._file_type_combo.currentData()
        self._manager.save_search(query, networks, file_type)
        min_size_bytes = self._min_size_spin.value() * 1024 * 1024
        max_size_bytes = self._max_size_spin.value() * 1024 * 1024

        panel = SearchResultsPanel(self._manager, self)
        panel.download_requested.connect(self.download_requested.emit)
        index = self._results_tabs.addTab(panel, self._tab_title(query))
        self._results_tabs.setTabToolTip(index, query)
        self._results_tabs.setCurrentIndex(index)
        panel.run_search(query, networks, file_type, min_size_bytes, max_size_bytes)

    def _on_save_alert_clicked(self) -> None:
        query = self._query_edit.text().strip()
        if not query:
            return
        networks = self._selected_networks()
        if not networks:
            self._status_label.setText(t("msg_select_networks"))
            return
        interval, ok = QInputDialog.getInt(
            self, t("dlg_save_alert_title"), t("dlg_save_alert_interval_label"), 30, 1, 1440
        )
        if not ok:
            return
        file_type = self._file_type_combo.currentData()
        self._saved_searches.add(query, networks, file_type, interval)
        self._status_label.setText(t("msg_alert_saved", n=interval))

    @staticmethod
    def _tab_title(query: str) -> str:
        if len(query) <= _TAB_TITLE_MAX_LEN:
            return query
        return query[: _TAB_TITLE_MAX_LEN - 1] + "…"

    def _on_tab_close_requested(self, index: int) -> None:
        widget = self._results_tabs.widget(index)
        self._results_tabs.removeTab(index)
        if isinstance(widget, SearchResultsPanel):
            widget.cancel()
        if widget is not None:
            widget.deleteLater()

    def _on_history_clicked(self) -> None:
        """Historial de búsquedas persistente entre sesiones (punto 7 del
        backlog): las búsquedas se guardan en `core/database.py` al
        lanzarlas y aquí se ofrecen en un menú para repetirlas, incluso
        tras cerrar y volver a abrir la GUI."""
        history = self._manager.load_search_history()
        menu = QMenu(self)
        entry_actions: dict = {}
        if not history:
            empty_action = menu.addAction(t("msg_history_empty"))
            empty_action.setEnabled(False)
        else:
            for entry in history:
                networks_label = ", ".join(t(NETWORK_LABEL_KEYS[n]) for n in entry.networks)
                label = t(
                    "history_entry_label",
                    query=entry.query,
                    networks=networks_label,
                    date=entry.searched_at.strftime("%d/%m %H:%M"),
                )
                entry_actions[menu.addAction(label)] = entry
            menu.addSeparator()
        clear_action = menu.addAction(t("ctx_clear_history"))
        clear_action.setEnabled(bool(history))
        action = menu.exec(self._history_button.mapToGlobal(self._history_button.rect().bottomLeft()))
        if action is None:
            return
        if action == clear_action:
            self._manager.clear_search_history()
        elif action in entry_actions:
            self._apply_history_entry(entry_actions[action])

    def _apply_history_entry(self, entry) -> None:
        self._query_edit.setText(entry.query)
        file_type_index = self._file_type_combo.findData(entry.file_type)
        if file_type_index != -1:
            self._file_type_combo.setCurrentIndex(file_type_index)
        for network, box in self._network_checks.items():
            box.setChecked(box.isEnabled() and network in entry.networks)
        self._on_search_clicked()
