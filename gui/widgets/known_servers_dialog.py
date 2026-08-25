"""Diálogo genérico de "servidores conocidos", al estilo de la pestaña
"Servidores" de aMule: una tabla con nombre/dirección/usuarios/
ficheros/ping de cada servidor, filtro de texto, y clic derecho (o
doble clic) sobre la fila deseada para conectar directamente -sin
tener que guardar nada en Preferencias primero, a diferencia del flujo
existente de `HubListDialog` en el diálogo de Ajustes.

Reutilizado por eMule (servidores eD2k, con usuarios/ficheros/ping
reales cuando el propio server.met los trae) y Gnutella2 (hubs
conocidos vía caché local + GWebCache, sin usuarios/ficheros/ping
porque el protocolo real no expone ese dato para G2 -mismo límite ya
documentado en la subpestaña de detalles del punto 35 del backlog).
Soulseek queda fuera porque solo existe un servidor central real, y
BitTorrent no tiene el concepto de "servidor" fuera de los trackers
por torrent que ya muestra la pestaña Red."""

import asyncio
from collections.abc import Awaitable, Callable

from PyQt6.QtCore import Qt, QSortFilterProxyModel
from PyQt6.QtGui import QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QLabel, QLineEdit, QMenu, QTableView, QVBoxLayout,
)

from gui.i18n import t

COL_NAME, COL_ADDRESS, COL_USERS, COL_FILES, COL_PING, COL_DESCRIPTION = range(6)

_NO_VALUE = "—"


class _ServerListModel(QStandardItemModel):
    def __init__(self, parent=None) -> None:
        super().__init__(0, 6, parent)
        self.setHorizontalHeaderLabels([
            t("col_server_name"), t("col_server_address"), t("col_server_users"),
            t("col_server_files"), t("col_server_ping"), t("col_server_description"),
        ])
        self._entries: list[dict] = []

    def set_entries(self, entries: list[dict]) -> None:
        self._entries = entries
        self.setRowCount(0)
        for entry in entries:
            host, port = entry["host"], entry["port"]
            name_item = QStandardItem(entry.get("name") or f"{host}:{port}")
            name_item.setData((host, port), Qt.ItemDataRole.UserRole)
            address_item = QStandardItem(f"{host}:{port}")
            users_item = QStandardItem()
            files_item = QStandardItem()
            ping_item = QStandardItem()
            users = entry.get("users")
            files = entry.get("files")
            ping = entry.get("ping")
            if users is not None:
                users_item.setData(users, Qt.ItemDataRole.DisplayRole)
            else:
                users_item.setText(_NO_VALUE)
            if files is not None:
                files_item.setData(files, Qt.ItemDataRole.DisplayRole)
            else:
                files_item.setText(_NO_VALUE)
            if ping is not None:
                ping_item.setData(ping, Qt.ItemDataRole.DisplayRole)
            else:
                ping_item.setText(_NO_VALUE)
            description_item = QStandardItem(entry.get("description") or _NO_VALUE)
            self.appendRow([name_item, address_item, users_item, files_item, ping_item, description_item])

    def host_port_at(self, row: int) -> tuple[str, int]:
        return self.item(row, COL_NAME).data(Qt.ItemDataRole.UserRole)


class KnownServersDialog(QDialog):
    """Tras `exec()`, si el resultado es Accepted, `selected_server`
    contiene el `(host, port)` elegido -por clic derecho > Conectar,
    doble clic, o el botón Aceptar tras seleccionar una fila."""

    def __init__(self, loader: Callable[[], Awaitable[list[dict]]], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("dlg_known_servers_title"))
        self.setMinimumSize(760, 480)
        self.selected_server: tuple[str, int] | None = None
        self._loader = loader

        layout = QVBoxLayout(self)

        self._status_label = QLabel(t("msg_loading_servers"))
        self._status_label.setObjectName("dimLabel")
        layout.addWidget(self._status_label)

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText(t("placeholder_filter_servers"))
        self._filter_edit.setAccessibleName(t("acc_server_filter"))
        self._filter_edit.textChanged.connect(self._on_filter_changed)
        layout.addWidget(self._filter_edit)

        self._model = _ServerListModel(self)
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
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self._table, stretch=1)

        hint = QLabel(t("hint_connect_server"))
        hint.setObjectName("dimLabel")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_selection)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        asyncio.ensure_future(self._load())

    async def _load(self) -> None:
        try:
            entries = await self._loader()
        except Exception as exc:
            self._status_label.setText(t("msg_servers_error", error=str(exc)))
            return
        if not entries:
            self._status_label.setText(t("msg_servers_empty"))
            return
        self._model.set_entries(entries)
        self._table.sortByColumn(COL_USERS, Qt.SortOrder.DescendingOrder)
        self._status_label.setText(t("status_servers_count", n=len(entries)))

    def _on_filter_changed(self, text: str) -> None:
        self._proxy.setFilterFixedString(text)

    def _on_context_menu(self, pos) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        self._table.selectRow(index.row())
        menu = QMenu(self)
        connect_action = menu.addAction(t("ctx_connect_server"))
        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == connect_action:
            self._accept_selection()

    def _accept_selection(self) -> None:
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return
        source_row = self._proxy.mapToSource(indexes[0]).row()
        self.selected_server = self._model.host_port_at(source_row)
        self.accept()
