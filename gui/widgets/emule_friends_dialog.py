"""Diálogo de Amigos y créditos para eMule (punto 15 del backlog): lista
todos los pares con los que se ha compartido algo (en cualquiera de los
dos sentidos), con su modificador de créditos, y permite marcarlos o
desmarcarlos como amigos por menú contextual — los amigos tienen
siempre máxima prioridad en la cola de subida, igual que hace el
eMule real."""

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QHeaderView, QLabel, QMenu, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from backends.emule_backend import EMuleBackend
from gui.i18n import t

_POLL_INTERVAL_MS = 3000

COL_NICK, COL_USER_HASH, COL_DOWNLOADED, COL_UPLOADED, COL_MODIFIER, COL_FRIEND = range(6)


def _format_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


class EMuleFriendsDialog(QDialog):
    def __init__(self, backend: EMuleBackend, parent=None) -> None:
        super().__init__(parent)
        self._backend = backend
        self.setWindowTitle(t("dlg_emule_friends_title"))
        self.setMinimumSize(720, 420)

        layout = QVBoxLayout(self)

        self._status_label = QLabel()
        self._status_label.setObjectName("dimLabel")
        self._status_label.setVisible(False)
        layout.addWidget(self._status_label)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels([
            t("col_credits_nick"), t("col_credits_user_hash"), t("col_credits_downloaded"),
            t("col_credits_uploaded"), t("col_credits_modifier"), t("col_credits_friend"),
        ])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(COL_USER_HASH, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        layout.addWidget(buttons)

        self._peers: list[dict] = []
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(_POLL_INTERVAL_MS)
        self._refresh()

    def _refresh(self) -> None:
        self._peers = sorted(
            self._backend.list_known_peers(), key=lambda p: p["modifier"], reverse=True
        )
        self._status_label.setVisible(not self._peers)
        if not self._peers:
            self._status_label.setText(t("msg_no_known_peers"))
        self._table.setRowCount(len(self._peers))
        for row, peer in enumerate(self._peers):
            self._table.setItem(row, COL_NICK, QTableWidgetItem(peer["nick"]))
            self._table.setItem(row, COL_USER_HASH, QTableWidgetItem(peer["user_hash"].hex()))
            self._table.setItem(row, COL_DOWNLOADED, QTableWidgetItem(_format_bytes(peer["downloaded"])))
            self._table.setItem(row, COL_UPLOADED, QTableWidgetItem(_format_bytes(peer["uploaded"])))
            self._table.setItem(row, COL_MODIFIER, QTableWidgetItem(f"{peer['modifier']:.2f}"))
            self._table.setItem(
                row, COL_FRIEND, QTableWidgetItem(t("friend_yes") if peer["is_friend"] else t("friend_no"))
            )

    def _on_context_menu(self, pos) -> None:
        row = self._table.rowAt(pos.y())
        if row < 0 or row >= len(self._peers):
            return
        peer = self._peers[row]
        menu = QMenu(self)
        action = menu.addAction(t("ctx_unmark_friend") if peer["is_friend"] else t("ctx_mark_friend"))
        chosen = menu.exec(self._table.viewport().mapToGlobal(pos))
        if chosen != action:
            return
        if peer["is_friend"]:
            self._backend.remove_friend(peer["user_hash"])
        else:
            self._backend.add_friend(peer["user_hash"], peer["nick"] or None)
        self._refresh()
