"""Pestaña de chat (puntos 13 y 14 del backlog: `SayChatroom`/
`PrivateMessage` de Soulseek y chat de hub NMDC de DC++): salas
públicas y mensajes privados, con una sub-pestaña cerrable por cada
sala o conversación abierta, igual que la pestaña de Búsqueda hace con
cada búsqueda. Ambas redes exponen el mismo contrato genérico
(`NetworkBackend.supports_chat()`/`get_room_list()`/`join_room()`/
`say_in_room()`/`send_private_message()`/`subscribe_chat()`), aunque
DC++ no tenga salas de verdad: su hub se expone como una única "sala"
sintética (ver `DCPPBackend.get_room_list()`), así que esta pestaña no
necesita ningún caso especial por red — solo distinguir a qué backend
concreto pertenece cada sub-pestaña abierta, por si hay más de una red
con chat conectada a la vez."""

import asyncio

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout, QInputDialog, QLineEdit, QListWidget, QPushButton, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from core.models import Network
from gui.connection_manager import STATUS_CONNECTED, ConnectionManager
from gui.i18n import t
from gui.models_qt import NETWORK_LABEL_KEYS

_CHAT_NETWORKS = (Network.SOULSEEK, Network.DCPP)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class _RoomChatWidget(QWidget):
    def __init__(self, network: Network, room: str, backend, parent=None) -> None:
        super().__init__(parent)
        self.network = network
        self.room = room
        self._backend = backend

        layout = QHBoxLayout(self)
        chat_column = QVBoxLayout()
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setAccessibleName(t("acc_chat_log"))
        chat_column.addWidget(self._log)
        input_row = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setAccessibleName(t("acc_chat_message"))
        self._input.returnPressed.connect(self._on_send)
        input_row.addWidget(self._input)
        send_button = QPushButton(t("btn_send"))
        send_button.clicked.connect(self._on_send)
        input_row.addWidget(send_button)
        chat_column.addLayout(input_row)
        layout.addLayout(chat_column, 3)

        self._users = QListWidget()
        self._users.setAccessibleName(t("acc_chat_users"))
        self._users.setMaximumWidth(180)
        layout.addWidget(self._users, 1)

    def _on_send(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        asyncio.ensure_future(self._backend.say_in_room(self.room, text))

    def append_message(self, user: str, message: str) -> None:
        self._log.append(f"<b>{_escape(user)}:</b> {_escape(message)}")

    def append_system(self, text: str) -> None:
        self._log.append(f"<i>{_escape(text)}</i>")

    def set_users(self, users: list[str]) -> None:
        self._users.clear()
        self._users.addItems(sorted(users))

    def add_user(self, username: str) -> None:
        if not self._users.findItems(username, Qt.MatchFlag.MatchExactly):
            self._users.addItem(username)

    def remove_user(self, username: str) -> None:
        for item in self._users.findItems(username, Qt.MatchFlag.MatchExactly):
            self._users.takeItem(self._users.row(item))


class _PrivateChatWidget(QWidget):
    def __init__(self, network: Network, username: str, backend, parent=None) -> None:
        super().__init__(parent)
        self.network = network
        self.username = username
        self._backend = backend

        layout = QVBoxLayout(self)
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setAccessibleName(t("acc_chat_log"))
        layout.addWidget(self._log)
        input_row = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setAccessibleName(t("acc_chat_message"))
        self._input.returnPressed.connect(self._on_send)
        input_row.addWidget(self._input)
        send_button = QPushButton(t("btn_send"))
        send_button.clicked.connect(self._on_send)
        input_row.addWidget(send_button)
        layout.addLayout(input_row)

    def _on_send(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        self.append_message(True, text)
        asyncio.ensure_future(self._backend.send_private_message(self.username, text))

    def append_message(self, from_me: bool, message: str) -> None:
        who = t("chat_you") if from_me else self.username
        self._log.append(f"<b>{_escape(who)}:</b> {_escape(message)}")


class ChatTab(QWidget):
    def __init__(self, connection_manager: ConnectionManager, parent=None) -> None:
        super().__init__(parent)
        self._manager = connection_manager
        self._room_tabs: dict[tuple[Network, str], _RoomChatWidget] = {}
        self._pm_tabs: dict[tuple[Network, str], _PrivateChatWidget] = {}
        self._subscribed: set[Network] = set()

        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self._join_button = QPushButton(t("btn_join_room"))
        self._join_button.clicked.connect(self._on_join_room_clicked)
        toolbar.addWidget(self._join_button)
        self._pm_button = QPushButton(t("btn_private_message"))
        self._pm_button.clicked.connect(self._on_private_message_clicked)
        toolbar.addWidget(self._pm_button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self._sub_tabs = QTabWidget()
        self._sub_tabs.setTabsClosable(True)
        self._sub_tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        layout.addWidget(self._sub_tabs)

        self._manager.status_changed.connect(self._on_status_changed)
        for network in _CHAT_NETWORKS:
            if self._manager.is_connected(network):
                self._subscribe_to_backend(network)
        self._refresh_buttons()

    # ---- conexión ----

    def _connected_chat_networks(self) -> list[Network]:
        return [n for n in _CHAT_NETWORKS if self._manager.is_connected(n)]

    def _refresh_buttons(self) -> None:
        connected = bool(self._connected_chat_networks())
        self._join_button.setEnabled(connected)
        self._pm_button.setEnabled(connected)

    def _on_status_changed(self, network_value: str, status: str, _message: str) -> None:
        network = Network(network_value)
        if network not in _CHAT_NETWORKS:
            return
        if status == STATUS_CONNECTED:
            self._subscribe_to_backend(network)
        self._refresh_buttons()

    def _subscribe_to_backend(self, network: Network) -> None:
        backend = self._manager.get_backend(network)
        if backend is not None:
            backend.subscribe_chat(lambda event, n=network: self._on_chat_event(n, event))
            self._subscribed.add(network)

    # ---- salas ----

    def _on_join_room_clicked(self) -> None:
        networks = self._connected_chat_networks()
        if not networks:
            return
        if len(networks) == 1:
            asyncio.ensure_future(self._join_room_flow(networks[0]))
            return
        network = self._pick_network(t("dlg_join_room_network_title"), t("dlg_join_room_network_label"), networks)
        if network is not None:
            asyncio.ensure_future(self._join_room_flow(network))

    def _pick_network(self, title: str, label: str, networks: list[Network]) -> Network | None:
        labels = [t(NETWORK_LABEL_KEYS[n]) for n in networks]
        choice, ok = QInputDialog.getItem(self, title, label, labels, editable=False)
        if not ok:
            return None
        return networks[labels.index(choice)]

    async def _join_room_flow(self, network: Network) -> None:
        backend = self._manager.get_backend(network)
        if backend is None:
            return
        rooms = await backend.get_room_list()
        display_to_room = {f"{name} ({count})": name for name, count in sorted(rooms, key=lambda r: -r[1])}
        text, ok = QInputDialog.getItem(
            self, t("dlg_join_room_title"), t("dlg_join_room_label"), list(display_to_room.keys()), editable=True
        )
        if not ok or not text.strip():
            return
        room = display_to_room.get(text, text.strip())
        await self._open_room(network, room, backend)

    async def _open_room(self, network: Network, room: str, backend) -> None:
        key = (network, room)
        if key in self._room_tabs:
            self._sub_tabs.setCurrentWidget(self._room_tabs[key])
            return
        await backend.join_room(room)
        widget = _RoomChatWidget(network, room, backend)
        self._room_tabs[key] = widget
        index = self._sub_tabs.addTab(widget, room)
        self._sub_tabs.setCurrentIndex(index)

    # ---- mensajes privados ----

    def _on_private_message_clicked(self) -> None:
        networks = self._connected_chat_networks()
        if not networks:
            return
        network = networks[0] if len(networks) == 1 else self._pick_network(
            t("dlg_join_room_network_title"), t("dlg_join_room_network_label"), networks
        )
        if network is None:
            return
        username, ok = QInputDialog.getText(self, t("dlg_private_message_title"), t("dlg_private_message_label"))
        if not ok or not username.strip():
            return
        self._open_pm(network, username.strip())

    def _open_pm(self, network: Network, username: str) -> _PrivateChatWidget:
        key = (network, username)
        if key in self._pm_tabs:
            widget = self._pm_tabs[key]
            self._sub_tabs.setCurrentWidget(widget)
            return widget
        backend = self._manager.get_backend(network)
        widget = _PrivateChatWidget(network, username, backend)
        self._pm_tabs[key] = widget
        index = self._sub_tabs.addTab(widget, username)
        self._sub_tabs.setCurrentIndex(index)
        return widget

    # ---- cierre de sub-pestañas ----

    def _on_tab_close_requested(self, index: int) -> None:
        widget = self._sub_tabs.widget(index)
        self._sub_tabs.removeTab(index)
        if isinstance(widget, _RoomChatWidget):
            del self._room_tabs[(widget.network, widget.room)]
            backend = self._manager.get_backend(widget.network)
            if backend is not None:
                asyncio.ensure_future(backend.leave_room(widget.room))
        elif isinstance(widget, _PrivateChatWidget):
            del self._pm_tabs[(widget.network, widget.username)]

    # ---- eventos entrantes del backend ----

    def _on_chat_event(self, network: Network, event: dict) -> None:
        etype = event["type"]
        if etype == "room_message":
            widget = self._room_tabs.get((network, event["room"]))
            if widget is not None:
                widget.append_message(event["user"], event["message"])
        elif etype == "room_joined":
            widget = self._room_tabs.get((network, event["room"]))
            if widget is not None:
                widget.set_users(event["users"])
        elif etype == "user_joined_room":
            widget = self._room_tabs.get((network, event["room"]))
            if widget is not None:
                widget.add_user(event["username"])
                widget.append_system(t("chat_user_joined", user=event["username"]))
        elif etype == "user_left_room":
            widget = self._room_tabs.get((network, event["room"]))
            if widget is not None:
                widget.remove_user(event["username"])
                widget.append_system(t("chat_user_left", user=event["username"]))
        elif etype == "private_message":
            widget = self._open_pm(network, event["user"])
            widget.append_message(False, event["message"])
