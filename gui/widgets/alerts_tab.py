"""Pestaña de alertas (punto 8 del backlog): lista las búsquedas
guardadas (creadas desde el botón "🔔 Guardar como alerta" de la
pestaña de Búsqueda), con su estado y cuántos resultados nuevos tiene
cada una pendientes de revisar, y permite activar/desactivar,
comprobar ya mismo, ver las novedades o eliminarlas."""

import asyncio

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QHeaderView, QMenu, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.models import SavedSearch
from gui.i18n import t
from gui.models_qt import NETWORK_LABEL_KEYS
from gui.widgets.accessible_table import AccessibleTableWidget
from gui.widgets.alert_results_dialog import AlertResultsDialog

_COL_QUERY, _COL_NETWORKS, _COL_INTERVAL, _COL_ENABLED, _COL_LAST_CHECKED, _COL_NEW = range(6)


class AlertsTab(QWidget):
    download_requested = pyqtSignal(object, str, object)  # SearchResult, dest_path, category (str | None)
    alerts_changed = pyqtSignal(int)  # nº total de resultados nuevos pendientes, para el título de la pestaña

    def __init__(self, saved_search_manager, parent=None) -> None:
        super().__init__(parent)
        self._manager = saved_search_manager
        self._manager.on_alert(self._on_alert)

        layout = QVBoxLayout(self)
        self._table = AccessibleTableWidget(0, 6, self)
        self._table.setAccessibleName(t("tab_alerts"))
        self._table.setHorizontalHeaderLabels([
            t("col_query"), t("col_networks"), t("col_interval"),
            t("col_enabled"), t("col_last_checked"), t("col_new_alerts"),
        ])
        self._table.horizontalHeader().setSectionResizeMode(_COL_QUERY, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.doubleClicked.connect(lambda _: self._view_new_alerts(self._selected_search()))
        layout.addWidget(self._table)

        self.refresh()

    def refresh(self) -> None:
        searches = self._manager.load()
        counts = self._manager.alert_counts()
        self._table.setRowCount(len(searches))
        total_new = 0
        for row, saved_search in enumerate(searches):
            n_new = counts.get(saved_search.id, 0)
            total_new += n_new
            networks_label = ", ".join(t(NETWORK_LABEL_KEYS[n]) for n in saved_search.networks)
            last_checked = (
                t("lbl_never_checked") if saved_search.last_checked_at is None
                else saved_search.last_checked_at.strftime("%d/%m %H:%M")
            )
            query_item = QTableWidgetItem(saved_search.query)
            query_item.setData(Qt.ItemDataRole.UserRole, saved_search)
            self._table.setItem(row, _COL_QUERY, query_item)
            self._table.setItem(row, _COL_NETWORKS, QTableWidgetItem(networks_label))
            self._table.setItem(row, _COL_INTERVAL, QTableWidgetItem(t("lbl_minutes_short", n=saved_search.interval_minutes)))
            self._table.setItem(row, _COL_ENABLED, QTableWidgetItem(t("lbl_yes") if saved_search.enabled else t("lbl_no")))
            self._table.setItem(row, _COL_LAST_CHECKED, QTableWidgetItem(last_checked))
            self._table.setItem(row, _COL_NEW, QTableWidgetItem(str(n_new)))
        self.alerts_changed.emit(total_new)

    def _on_alert(self, _saved_search: SavedSearch, _new_results: list) -> None:
        self.refresh()

    def _selected_search(self) -> SavedSearch | None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return None
        return self._table.item(rows[0].row(), _COL_QUERY).data(Qt.ItemDataRole.UserRole)

    def _on_context_menu(self, pos) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        self._table.selectRow(index.row())
        saved_search: SavedSearch = self._table.item(index.row(), _COL_QUERY).data(Qt.ItemDataRole.UserRole)

        menu = QMenu(self)
        toggle_action = menu.addAction(t("ctx_disable_alert") if saved_search.enabled else t("ctx_enable_alert"))
        run_now_action = menu.addAction(t("ctx_run_now"))
        view_action = menu.addAction(t("ctx_view_new_alerts"))
        menu.addSeparator()
        delete_action = menu.addAction(t("ctx_delete_saved_search"))

        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == toggle_action:
            self._manager.set_enabled(saved_search, not saved_search.enabled)
            self.refresh()
        elif action == run_now_action:
            asyncio.ensure_future(self._run_now(saved_search))
        elif action == view_action:
            self._view_new_alerts(saved_search)
        elif action == delete_action:
            self._manager.remove(saved_search)
            self.refresh()

    async def _run_now(self, saved_search: SavedSearch) -> None:
        await self._manager.check_now(saved_search)
        self.refresh()

    def _view_new_alerts(self, saved_search: SavedSearch | None) -> None:
        if saved_search is None:
            return
        results = self._manager.load_alerts(saved_search)
        dialog = AlertResultsDialog(saved_search.query, results, self)
        dialog.download_requested.connect(self.download_requested.emit)
        dialog.exec()
        self._manager.dismiss_alerts(saved_search)
        self.refresh()
