"""Diálogo modal que muestra los resultados nuevos encontrados por una
búsqueda guardada (punto 8 del backlog), reutilizando el mismo modelo
y menú contextual de descarga que la pestaña de Búsqueda."""

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QMenu,
    QMessageBox, QVBoxLayout,
)

from core.config import Category, load_config
from core.models import SearchResult
from gui.i18n import t
from gui.models_qt import SearchResultsModel, SearchResultsSortProxy
from gui.widgets.accessible_table import AccessibleTableView


class AlertResultsDialog(QDialog):
    download_requested = pyqtSignal(object, str, object)  # SearchResult, dest_path, category (str | None)

    def __init__(self, query: str, results: list[SearchResult], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("dlg_new_alerts_title", query=query))
        self.setMinimumSize(640, 400)

        layout = QVBoxLayout(self)

        self._model = SearchResultsModel(self)
        self._model.set_results(results)
        self._proxy = SearchResultsSortProxy(self)
        self._proxy.setSourceModel(self._model)
        self._table = AccessibleTableView()
        self._table.setAccessibleName(self.windowTitle())
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

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.close)
        layout.addWidget(buttons)

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
