"""Diálogo para navegar los archivos compartidos de un usuario de
Soulseek (`BrowseUser`/`GetSharedFileList`, punto 9 del backlog): a la
izquierda la lista de carpetas que comparte, a la derecha los archivos
de la carpeta seleccionada, descargables igual que en la pestaña de
Búsqueda (mismo menú contextual, misma señal `download_requested`)."""

import asyncio
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QHBoxLayout, QHeaderView,
    QLabel, QListWidget, QMenu, QMessageBox, QPushButton, QSplitter,
    QTableView, QVBoxLayout, QWidget,
)

from core.config import Category, load_config
from core.download_manager import DownloadManager
from core.models import Network, SearchResult
from gui.i18n import t
from gui.models_qt import SearchResultsModel, SearchResultsSortProxy


class BrowseUserDialog(QDialog):
    download_requested = pyqtSignal(object, str, object)  # SearchResult, dest_path, category (str | None)

    def __init__(self, manager: DownloadManager, username: str, parent=None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._username = username
        self._folders: list[tuple[str, list[SearchResult]]] = []

        self.setWindowTitle(t("dlg_browse_user_title", username=username))
        self.setMinimumSize(720, 440)

        layout = QVBoxLayout(self)
        self._status_label = QLabel(t("msg_browsing", username=username))
        self._status_label.setObjectName("dimLabel")
        layout.addWidget(self._status_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._folder_list = QListWidget()
        self._folder_list.currentRowChanged.connect(self._on_folder_selected)
        splitter.addWidget(self._folder_list)

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
        splitter.addWidget(self._table)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, stretch=1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_button = QPushButton(t("btn_close"))
        close_button.clicked.connect(self.close)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)

        asyncio.ensure_future(self._load())

    async def _load(self) -> None:
        try:
            folders = await self._manager.browse_user(Network.SOULSEEK, self._username)
        except Exception as exc:
            self._status_label.setText(t("msg_browse_error", error=str(exc)))
            return
        if not folders:
            self._status_label.setText(t("msg_browse_empty"))
            return
        self._folders = folders
        total_files = sum(len(files) for _, files in folders)
        self._status_label.setText(t("status_browse_count", folders=len(folders), files=total_files))
        for folder_path, _files in folders:
            self._folder_list.addItem(folder_path)
        self._folder_list.setCurrentRow(0)

    def _on_folder_selected(self, row: int) -> None:
        if row < 0 or row >= len(self._folders):
            self._model.set_results([])
            return
        _folder_path, files = self._folders[row]
        self._model.set_results(files)

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
