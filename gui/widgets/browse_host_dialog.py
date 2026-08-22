"""Diálogo para Browse Host en Gnutella2 (`/BH`, punto 10 del
backlog): lista TODO lo que comparte un nodo/hub concreto (típicamente
el hub al que estamos conectados), en una tabla plana descargable
igual que la pestaña de Búsqueda (mismo menú contextual, misma señal
`download_requested`)."""

import asyncio
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMenu,
    QMessageBox, QPushButton, QTableView, QVBoxLayout,
)

from core.config import Category, load_config
from core.download_manager import DownloadManager
from core.models import Network
from gui.i18n import t
from gui.models_qt import SearchResultsModel, SearchResultsSortProxy


class BrowseHostDialog(QDialog):
    download_requested = pyqtSignal(object, str, object)  # SearchResult, dest_path, category (str | None)

    def __init__(self, manager: DownloadManager, host: str, port: int, parent=None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._host = host
        self._port = port

        self.setWindowTitle(t("dlg_browse_host_title", host=f"{host}:{port}"))
        self.setMinimumSize(720, 440)

        layout = QVBoxLayout(self)
        self._status_label = QLabel(t("msg_browsing_host", host=f"{host}:{port}"))
        self._status_label.setObjectName("dimLabel")
        layout.addWidget(self._status_label)

        self._model = SearchResultsModel(self)
        self._proxy = SearchResultsSortProxy(self)
        self._proxy.setSourceModel(self._model)
        self._table = QTableView()
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

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_button = QPushButton(t("btn_close"))
        close_button.clicked.connect(self.close)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)

        asyncio.ensure_future(self._load())

    async def _load(self) -> None:
        try:
            results = await self._manager.browse_host(Network.GNUTELLA2, self._host, self._port)
        except Exception as exc:
            self._status_label.setText(t("msg_browse_error", error=str(exc)))
            return
        if not results:
            self._status_label.setText(t("msg_browse_empty"))
            return
        self._model.set_results(results)
        self._status_label.setText(t("status_browse_host_count", n=len(results)))

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
        category_actions: dict = {}
        if categories:
            category_menu = menu.addMenu(t("ctx_download_to_category"))
            for category in categories:
                category_actions[category_menu.addAction(category.name)] = category
        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == download_action:
            self._download_selected()
        elif action in category_actions:
            self._download_selected(category=category_actions[action])

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
