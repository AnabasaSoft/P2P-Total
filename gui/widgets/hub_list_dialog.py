"""Diálogo para elegir un hub DC++ de la lista pública tipo hublist.org
(punto 11 del backlog), en vez de tener que teclear IP:puerto a mano en
Ajustes. Se puede filtrar por nombre/dirección/descripción/país y se
ordena por columnas (usuarios por defecto, descendente)."""

import asyncio

from PyQt6.QtCore import Qt, QSortFilterProxyModel
from PyQt6.QtGui import QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QTableView, QVBoxLayout,
)

from backends.dcpp_backend import HubListEntry, fetch_public_hub_list
from gui.i18n import t

COL_NAME, COL_ADDRESS, COL_USERS, COL_COUNTRY, COL_SOFTWARE, COL_DESCRIPTION = range(6)


def _format_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


class _HubListModel(QStandardItemModel):
    def __init__(self, parent=None) -> None:
        super().__init__(0, 6, parent)
        self.setHorizontalHeaderLabels([
            t("col_hub_name"), t("col_hub_address"), t("col_hub_users"),
            t("col_hub_country"), t("col_hub_software"), t("col_hub_description"),
        ])
        self._hubs: list[HubListEntry] = []

    def set_hubs(self, hubs: list[HubListEntry]) -> None:
        self._hubs = hubs
        self.setRowCount(0)
        for hub in hubs:
            name_item = QStandardItem(hub.name)
            name_item.setData(hub, Qt.ItemDataRole.UserRole)
            address_item = QStandardItem(f"{hub.host}:{hub.port}")
            users_item = QStandardItem()
            users_item.setData(hub.users, Qt.ItemDataRole.DisplayRole)
            country_item = QStandardItem(hub.country)
            software_item = QStandardItem(hub.software)
            description_item = QStandardItem(hub.description)
            self.appendRow([
                name_item, address_item, users_item, country_item, software_item, description_item,
            ])

    def hub_at(self, row: int) -> HubListEntry:
        return self.item(row, COL_NAME).data(Qt.ItemDataRole.UserRole)


class HubListDialog(QDialog):
    """Tras `exec()`, si el resultado es Accepted, `selected_hub`
    contiene el `HubListEntry` elegido por el usuario."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("dlg_hub_list_title"))
        self.setMinimumSize(760, 480)
        self.selected_hub: HubListEntry | None = None

        layout = QVBoxLayout(self)

        self._status_label = QLabel(t("msg_loading_hub_list"))
        self._status_label.setObjectName("dimLabel")
        layout.addWidget(self._status_label)

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText(t("placeholder_filter_hubs"))
        self._filter_edit.textChanged.connect(self._on_filter_changed)
        layout.addWidget(self._filter_edit)

        self._model = _HubListModel(self)
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._proxy.setFilterKeyColumn(-1)

        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(COL_DESCRIPTION, QHeaderView.ResizeMode.Stretch)
        self._table.doubleClicked.connect(lambda _: self._accept_selection())
        layout.addWidget(self._table, stretch=1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_selection)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        asyncio.ensure_future(self._load())

    async def _load(self) -> None:
        try:
            hubs = await fetch_public_hub_list()
        except Exception as exc:
            self._status_label.setText(t("msg_hub_list_error", error=str(exc)))
            return
        if not hubs:
            self._status_label.setText(t("msg_hub_list_empty"))
            return
        self._model.set_hubs(hubs)
        self._table.sortByColumn(COL_USERS, Qt.SortOrder.DescendingOrder)
        self._status_label.setText(t("status_hub_list_count", n=len(hubs)))

    def _on_filter_changed(self, text: str) -> None:
        self._proxy.setFilterFixedString(text)

    def _accept_selection(self) -> None:
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return
        source_row = self._proxy.mapToSource(indexes[0]).row()
        self.selected_hub = self._model.hub_at(source_row)
        self.accept()
