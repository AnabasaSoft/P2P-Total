"""Ventana principal: menú (incluida la conexión por red, antes en un
panel lateral) y pestañas de Búsqueda / Transferencias / Red."""

import asyncio
import tempfile
from pathlib import Path

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QAction, QActionGroup, QIcon
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QInputDialog, QLabel, QMainWindow, QMenu, QMessageBox,
    QProgressDialog, QSystemTrayIcon, QTabWidget,
)

from backends.dcpp_backend import parse_dchub_link
from backends.emule_backend import parse_ed2k_link
from core.backend_base import BackendRegistry
from core.bandwidth_scheduler import BandwidthScheduler
from core.config import load_config, save_config
from core.models import Download, DownloadState, Network, SearchResult
from core.download_manager import DownloadManager
from core.http_client import http_download
from core.remote_control import RemoteControlServer
from core.saved_search_manager import SavedSearchManager
from core.self_updater import apply_update_and_relaunch, can_self_update, detect_install_kind, find_update_asset
from core.update_checker import check_for_update
from core.watch_folder import WatchFolderManager
from gui import theme
from gui.connection_manager import STATUS_CONNECTED, STATUS_CONNECTING, ConnectionManager
from gui.i18n import LANGUAGES, t, t_in
from gui.models_qt import NETWORK_LABEL_KEYS, _format_speed
from gui.resources import ICON_PATH
from gui.widgets.about_dialog import AboutDialog
from gui.widgets.alerts_tab import AlertsTab
from gui.widgets.chat_tab import ChatTab
from gui.widgets.create_torrent_dialog import CreateTorrentDialog
from gui.widgets.downloads_tab import DownloadsTab
from gui.widgets.emule_friends_dialog import EMuleFriendsDialog
from gui.widgets.network_tab import NetworkTab
from gui.widgets.search_tab import SearchTab
from gui.widgets.settings_dialog import SettingsDialog
from gui.widgets.stats_tab import StatsTab
from gui.widgets.torrent_files_dialog import TorrentFilesDialog
from gui.widgets.update_dialog import UpdateAvailableDialog


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(t("app_title"))
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.setAcceptDrops(True)
        startup_config = load_config()
        self.resize(startup_config.ui.window_width, startup_config.ui.window_height)
        if startup_config.ui.window_x is not None and startup_config.ui.window_y is not None:
            self.move(startup_config.ui.window_x, startup_config.ui.window_y)

        self._download_manager = DownloadManager()
        self._connection_manager = ConnectionManager(self._download_manager)
        self._connection_manager.status_changed.connect(self._on_status_changed_for_networks_menu)
        self._connection_manager.status_changed.connect(self._on_status_changed_for_statusbar)

        self._saved_search_manager = SavedSearchManager(self._download_manager)
        self._saved_search_manager.start()

        self._watch_folder_manager = WatchFolderManager(self._download_manager)
        self._watch_folder_manager.on_added(self._on_watch_folder_added)
        self._watch_folder_manager.start()

        self._bandwidth_scheduler = BandwidthScheduler(self._connection_manager.apply_global_speed_limits)
        self._bandwidth_scheduler.start()

        self._remote_control_server = RemoteControlServer(self._download_manager, self._connection_manager)
        self._remote_control_server.start()

        self._download_manager.on_verify_result(self._on_verify_result)

        self._search_tab = SearchTab(self._download_manager, self._connection_manager, self._saved_search_manager)
        self._search_tab.download_requested.connect(self._on_download_requested)
        self._downloads_tab = DownloadsTab(self._download_manager, self._connection_manager)
        self._network_tab = NetworkTab(self._connection_manager, self._download_manager)
        self._network_tab.download_requested.connect(self._on_download_requested)
        self._alerts_tab = AlertsTab(self._saved_search_manager)
        self._alerts_tab.download_requested.connect(self._on_download_requested)
        self._alerts_tab.alerts_changed.connect(self._update_alerts_tab_title)
        self._chat_tab = ChatTab(self._connection_manager)
        self._chat_tab.private_message_received.connect(self._on_private_message_for_notifications)
        self._stats_tab = StatsTab(self._connection_manager)

        tabs = QTabWidget()
        tabs.addTab(self._search_tab, t("tab_search"))
        tabs.addTab(self._downloads_tab, t("tab_downloads"))
        tabs.addTab(self._network_tab, t("tab_network"))
        self._alerts_tab_index = tabs.addTab(self._alerts_tab, t("tab_alerts"))
        tabs.addTab(self._chat_tab, t("tab_chat"))
        tabs.addTab(self._stats_tab, t("tab_stats"))
        self._tabs = tabs
        self.setCentralWidget(tabs)

        self._build_menu()
        self.statusBar().showMessage(t("statusbar_ready"))
        self._transfers_label = QLabel()
        self.statusBar().addPermanentWidget(self._transfers_label)
        self._download_manager.on_progress(self._on_progress_for_statusbar)

        self._quitting = False
        self._build_tray_icon()
        self._notified_download_ids: set[int] = set()
        self._download_manager.on_progress(self._on_progress_for_notifications)

        self._connection_manager.autoconnect_configured_networks()

        # Referencia guardada a propósito: si nadie la retiene, el bucle de
        # asyncio solo guarda una referencia débil a la tarea y podría
        # recolectarla antes de que termine (documentado así en la propia
        # documentación de `asyncio.ensure_future`).
        self._startup_update_check_task = asyncio.ensure_future(self._check_for_update())

    def _on_check_for_updates_clicked(self) -> None:
        self._manual_update_check_task = asyncio.ensure_future(self._check_for_update(silent=False))

    async def _check_for_update(self, silent: bool = True) -> None:
        config = load_config()
        try:
            info = await check_for_update(proxy=config.proxy, raise_errors=not silent)
        except Exception as exc:
            QMessageBox.warning(
                self, t("update_dialog_title"), t("update_check_failed").format(error=str(exc)),
            )
            return
        if info is None:
            if not silent:
                QMessageBox.information(self, t("update_dialog_title"), t("update_check_up_to_date"))
            return
        install_kind = detect_install_kind()
        asset = find_update_asset(info.assets, install_kind) if can_self_update(install_kind) else None
        dialog = UpdateAvailableDialog(info.version, info.release_url, asset is not None, self)
        dialog.exec()
        if asset is not None and dialog.update_now_clicked():
            await self._run_self_update(install_kind, asset, config.proxy)

    async def _run_self_update(self, install_kind, asset: dict, proxy) -> None:
        progress = QProgressDialog(t("update_downloading"), t("update_dialog_cancel"), 0, 100, self)
        progress.setWindowTitle(t("update_dialog_title"))
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()

        def on_progress(downloaded: int, total: int | None) -> None:
            if total:
                progress.setValue(min(100, int(downloaded * 100 / total)))
            QApplication.processEvents()

        tmp_dir = Path(tempfile.mkdtemp(prefix="p2p-total-update-"))
        dest_path = tmp_dir / asset["name"]
        try:
            await http_download(asset["browser_download_url"], dest_path, proxy=proxy, progress_cb=on_progress)
        except Exception:
            progress.close()
            QMessageBox.warning(self, t("update_dialog_title"), t("update_download_failed"))
            return
        progress.close()

        try:
            apply_update_and_relaunch(install_kind, dest_path)
        except Exception:
            QMessageBox.warning(self, t("update_dialog_title"), t("update_apply_failed"))
            return

        self._quitting = True
        self.close()
        QApplication.instance().quit()

    # ---- Menú ----

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu(t("menu_file"))
        open_link_action = QAction(t("menu_file_open_link"), self)
        open_link_action.triggered.connect(self._on_open_link)
        file_menu.addAction(open_link_action)
        add_torrent_action = QAction(t("menu_file_add_torrent"), self)
        add_torrent_action.triggered.connect(self._on_add_torrent_file)
        file_menu.addAction(add_torrent_action)
        create_torrent_action = QAction(t("menu_file_create_torrent"), self)
        create_torrent_action.triggered.connect(self._on_create_torrent)
        file_menu.addAction(create_torrent_action)
        file_menu.addSeparator()
        settings_action = QAction(t("menu_file_settings"), self)
        settings_action.triggered.connect(self._on_open_settings)
        file_menu.addAction(settings_action)
        file_menu.addSeparator()
        quit_action = QAction(t("menu_file_quit"), self)
        quit_action.triggered.connect(self._on_quit)
        file_menu.addAction(quit_action)

        networks_menu = menu_bar.addMenu(t("menu_networks"))
        self._network_actions: dict[Network, QAction] = {}
        for network in Network:
            action = QAction(t(NETWORK_LABEL_KEYS[network]), self, checkable=True)
            action.toggled.connect(lambda checked, n=network: self._on_network_toggled(n, checked))
            self._network_actions[network] = action
            networks_menu.addAction(action)
        networks_menu.addSeparator()
        connect_all_action = QAction(t("menu_networks_connect_all"), self)
        connect_all_action.triggered.connect(self._on_connect_all)
        networks_menu.addAction(connect_all_action)
        disconnect_all_action = QAction(t("menu_networks_disconnect_all"), self)
        disconnect_all_action.triggered.connect(self._on_disconnect_all)
        networks_menu.addAction(disconnect_all_action)
        networks_menu.addSeparator()
        emule_friends_action = QAction(t("menu_networks_emule_friends"), self)
        emule_friends_action.triggered.connect(self._on_open_emule_friends)
        networks_menu.addAction(emule_friends_action)

        view_menu = menu_bar.addMenu(t("menu_view"))
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        dark_action = QAction(t("menu_view_theme_dark"), self, checkable=True)
        light_action = QAction(t("menu_view_theme_light"), self, checkable=True)
        current_theme = load_config().ui.theme
        dark_action.setChecked(current_theme == "dark")
        light_action.setChecked(current_theme == "light")
        dark_action.triggered.connect(lambda: self._on_theme_selected("dark"))
        light_action.triggered.connect(lambda: self._on_theme_selected("light"))
        theme_group.addAction(dark_action)
        theme_group.addAction(light_action)
        view_menu.addAction(dark_action)
        view_menu.addAction(light_action)

        language_menu = view_menu.addMenu(t("menu_view_language"))
        language_group = QActionGroup(self)
        language_group.setExclusive(True)
        current_language = load_config().ui.language
        for code, label in LANGUAGES:
            action = QAction(label, self, checkable=True)
            action.setChecked(code == current_language)
            action.triggered.connect(lambda _checked, c=code: self._on_language_selected(c))
            language_group.addAction(action)
            language_menu.addAction(action)

        help_menu = menu_bar.addMenu(t("menu_help"))
        check_updates_action = QAction(t("menu_help_check_updates"), self)
        check_updates_action.triggered.connect(self._on_check_for_updates_clicked)
        help_menu.addAction(check_updates_action)
        help_menu.addSeparator()
        about_action = QAction(t("menu_help_about"), self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

    # ---- Handlers ----

    def _on_open_link(self) -> None:
        # Punto 12 del backlog: si el portapapeles ya tiene un enlace
        # reconocido (magnet:/ed2k://dchub://), se precarga en el
        # campo para que baste con Ctrl+V + Enter.
        clipboard_text = QApplication.clipboard().text().strip()
        prefill = clipboard_text if clipboard_text.startswith(("magnet:", "ed2k://", "dchub://")) else ""
        text, ok = QInputDialog.getText(
            self, t("dlg_open_link_title"), t("dlg_open_link_label"), text=prefill
        )
        if not ok or not text.strip():
            return
        self._handle_link(text.strip())

    def _handle_link(self, text: str) -> None:
        if text.startswith("magnet:"):
            asyncio.ensure_future(self._add_torrent(text))
        elif text.startswith("ed2k://"):
            asyncio.ensure_future(self._add_ed2k_link(text))
        elif text.startswith("dchub://"):
            asyncio.ensure_future(self._connect_dchub_link(text))
        else:
            QMessageBox.warning(self, t("app_title"), t("msg_unrecognized_link"))

    async def _add_ed2k_link(self, link: str) -> None:
        parsed = parse_ed2k_link(link)
        if parsed is None:
            QMessageBox.warning(self, t("app_title"), t("msg_invalid_ed2k_link"))
            return
        title, size, file_hash_hex = parsed
        backend = BackendRegistry.get(Network.EMULE)
        if backend is None or not await backend.is_connected():
            QMessageBox.warning(self, t("app_title"), t("msg_emule_not_connected"))
            return
        default_dir = load_config().default_download_dir
        try:
            Path(default_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(
                self, t("app_title"), t("msg_download_dir_error", dir=default_dir, error=str(exc))
            )
            return
        result = SearchResult(
            network=Network.EMULE, title=title, size_bytes=size,
            source_id=f"{file_hash_hex}:::{size}:::{title}",
        )
        await self._start_download(result, default_dir)

    async def _connect_dchub_link(self, link: str) -> None:
        parsed = parse_dchub_link(link)
        if parsed is None:
            QMessageBox.warning(self, t("app_title"), t("msg_invalid_dchub_link"))
            return
        host, port = parsed
        if self._connection_manager.is_connected(Network.DCPP):
            await self._connection_manager.disconnect_network(Network.DCPP)
        await self._connection_manager.connect_network(Network.DCPP, hub_override=(host, port))
        self._tabs.setCurrentWidget(self._network_tab)

    def _on_add_torrent_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, t("dlg_add_torrent_title"), "", t("dlg_add_torrent_filter"))
        if not path:
            return
        asyncio.ensure_future(self._add_torrent(path))

    def _on_create_torrent(self) -> None:
        # Punto 37 del backlog: crear un .torrent nuevo a partir de
        # contenido propio y sembrarlo de inmediato.
        dialog = CreateTorrentDialog(self)
        if not dialog.exec():
            return
        default_name = Path(dialog.source_path).name + ".torrent"
        dest_path, _ = QFileDialog.getSaveFileName(
            self, t("dlg_save_torrent_title"), default_name, t("dlg_add_torrent_filter")
        )
        if not dest_path:
            return
        asyncio.ensure_future(self._create_torrent(
            dialog.source_path, dest_path, dialog.trackers, dialog.comment, dialog.private
        ))

    async def _create_torrent(
        self, source_path: str, dest_torrent_path: str,
        trackers: list[str], comment: str, private: bool,
    ) -> None:
        backend = BackendRegistry.get(Network.TORRENT)
        if backend is None or not await backend.is_connected():
            QMessageBox.warning(self, t("app_title"), t("msg_torrent_not_connected"))
            return
        try:
            download = await self._download_manager.create_torrent(
                source_path, dest_torrent_path, trackers, comment, private
            )
        except Exception as exc:
            QMessageBox.warning(self, t("app_title"), t("msg_create_torrent_error", error=str(exc)))
            return
        self._downloads_tab.add_download(download)
        self._tabs.setCurrentWidget(self._downloads_tab)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() and any(
            url.toLocalFile().lower().endswith(".torrent") for url in event.mimeData().urls()
        ):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(".torrent"):
                asyncio.ensure_future(self._add_torrent(path))
        event.acceptProposedAction()

    async def _add_torrent(self, query: str) -> None:
        backend = BackendRegistry.get(Network.TORRENT)
        if backend is None or not await backend.is_connected():
            QMessageBox.warning(self, t("app_title"), t("msg_torrent_not_connected"))
            return
        default_dir = load_config().default_download_dir
        try:
            Path(default_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(
                self, t("app_title"), t("msg_download_dir_error", dir=default_dir, error=str(exc))
            )
            return
        try:
            results = await backend.search(query)
        except Exception as e:
            QMessageBox.warning(self, t("app_title"), str(e))
            return
        if not results:
            QMessageBox.warning(self, t("app_title"), t("msg_torrent_no_metadata"))
            return
        await self._start_download(results[0], default_dir)

    def _on_network_toggled(self, network: Network, checked: bool) -> None:
        if checked:
            asyncio.ensure_future(self._connection_manager.connect_network(network))
        else:
            asyncio.ensure_future(self._connection_manager.disconnect_network(network))

    def _on_connect_all(self) -> None:
        for network in Network:
            if not self._connection_manager.is_connected(network):
                asyncio.ensure_future(self._connection_manager.connect_network(network))

    def _on_disconnect_all(self) -> None:
        for network in Network:
            if self._connection_manager.is_connected(network):
                asyncio.ensure_future(self._connection_manager.disconnect_network(network))

    def _on_open_emule_friends(self) -> None:
        backend = self._connection_manager.get_backend(Network.EMULE)
        if backend is None:
            QMessageBox.warning(self, t("app_title"), t("msg_emule_not_connected"))
            return
        dialog = EMuleFriendsDialog(backend, self)
        dialog.exec()

    def _on_status_changed_for_networks_menu(self, network_value: str, status: str, message: str) -> None:
        network = Network(network_value)
        action = self._network_actions[network]
        action.blockSignals(True)
        action.setChecked(status == STATUS_CONNECTED)
        action.setEnabled(status != STATUS_CONNECTING)
        action.setToolTip(message)
        action.blockSignals(False)

    def _on_status_changed_for_statusbar(self, _network_value: str, _status: str, _message: str) -> None:
        n_connected = len(self._connection_manager.connected_networks())
        self.statusBar().showMessage(t("statusbar_connected_count", n=n_connected, total=len(Network)))

    def _on_progress_for_statusbar(self, _download: Download) -> None:
        n_active = self._downloads_tab.active_count()
        if n_active == 0:
            self._transfers_label.clear()
            return
        speed = _format_speed(self._downloads_tab.active_speed_bps()) or "0 B/s"
        self._transfers_label.setText(t("statusbar_transfers", n=n_active, speed=speed))

    def _on_progress_for_notifications(self, download: Download) -> None:
        # Punto 23 del backlog: aviso nativo del sistema al completar o
        # fallar una descarga, una sola vez por descarga (no en cada tick
        # de progreso) y solo si hay un icono de bandeja real disponible.
        if self._tray_icon is None or download.id is None:
            return
        if download.state not in (DownloadState.COMPLETED, DownloadState.ERROR):
            return
        if not load_config().ui.notify_on_download_finish:
            return
        if download.id in self._notified_download_ids:
            return
        self._notified_download_ids.add(download.id)
        if download.state == DownloadState.COMPLETED:
            self._tray_icon.showMessage(
                t("notify_download_completed_title"),
                t("notify_download_completed_body", title=download.title),
                QSystemTrayIcon.MessageIcon.Information,
            )
        else:
            self._tray_icon.showMessage(
                t("notify_download_failed_title"),
                t("notify_download_failed_body", title=download.title),
                QSystemTrayIcon.MessageIcon.Warning,
            )

    def _on_private_message_for_notifications(self, _network_value: str, username: str, message: str) -> None:
        # Punto 41 del backlog: aviso nativo del sistema al recibir un
        # mensaje privado de chat mientras la ventana está minimizada u
        # oculta (en la bandeja) -- si está visible y en primer plano, el
        # propio mensaje ya se ve en la pestaña Chat sin necesidad de aviso.
        if self._tray_icon is None:
            return
        if not load_config().ui.notify_on_chat_message:
            return
        if self.isVisible() and not self.isMinimized():
            return
        self._tray_icon.showMessage(
            t("notify_chat_message_title", user=username),
            message,
            QSystemTrayIcon.MessageIcon.Information,
        )

    def _on_watch_folder_added(self, filename: str, download: Download | None, error: Exception | None) -> None:
        # Punto 26 del backlog: la carpeta vigilada añadió (o intentó
        # añadir) un .torrent nuevo por su cuenta -- se refleja en la
        # pestaña Transferencias y se avisa igual que al terminar una
        # descarga (punto 23), reusando el mismo icono de bandeja.
        if download is not None:
            self._downloads_tab.add_download(download)
        if self._tray_icon is None:
            return
        if error is None:
            self._tray_icon.showMessage(
                t("notify_watch_folder_title"),
                t("notify_watch_folder_body", filename=filename),
                QSystemTrayIcon.MessageIcon.Information,
            )
        else:
            self._tray_icon.showMessage(
                t("notify_watch_folder_title"),
                t("notify_watch_folder_error_body", filename=filename, error=str(error)),
                QSystemTrayIcon.MessageIcon.Warning,
            )

    def _on_verify_result(self, download: Download, is_intact: bool | None, error: Exception | None) -> None:
        # Punto 27 del backlog: resultado de verificar el contenido ya
        # descargado, tanto a demanda (menú contextual "Verificar
        # archivo") como automático al completar si así lo indica
        # Preferencias -- mismo icono de bandeja que el resto de avisos.
        if self._tray_icon is None:
            return
        if error is not None:
            self._tray_icon.showMessage(
                t("notify_verify_title"),
                t("notify_verify_error_body", filename=download.title, error=str(error)),
                QSystemTrayIcon.MessageIcon.Warning,
            )
        elif is_intact:
            self._tray_icon.showMessage(
                t("notify_verify_title"),
                t("notify_verify_ok_body", filename=download.title),
                QSystemTrayIcon.MessageIcon.Information,
            )
        else:
            self._tray_icon.showMessage(
                t("notify_verify_title"),
                t("notify_verify_corrupt_body", filename=download.title),
                QSystemTrayIcon.MessageIcon.Warning,
            )

    def _update_alerts_tab_title(self, n_pending: int) -> None:
        title = t("tab_alerts_with_count", n=n_pending) if n_pending else t("tab_alerts")
        self._tabs.setTabText(self._alerts_tab_index, title)

    def _on_download_requested(self, result, dest_path: str, category: str | None) -> None:
        asyncio.ensure_future(self._start_download(result, dest_path, category))

    async def _start_download(self, result, dest_path: str, category: str | None = None) -> None:
        try:
            download = await self._download_manager.download(result, dest_path, category)
        except Exception as e:
            QMessageBox.warning(self, t("app_title"), str(e))
            return
        self._downloads_tab.add_download(download)
        self._tabs.setCurrentWidget(self._downloads_tab)
        if download.network == Network.TORRENT:
            await self._maybe_show_torrent_file_selection(download)

    async def _maybe_show_torrent_file_selection(self, download: Download) -> None:
        """Al añadir un torrent con más de un archivo, se abre siempre el
        diálogo de selección de archivos antes de dejarlo descargando sin
        más (petición explícita del usuario). Los metadatos pueden tardar
        unos segundos en llegar vía DHT/peers si el torrent viene de un
        magnet sin resolver antes en la búsqueda (caso de los resultados
        de texto libre de apibay.org) -de ahí la espera con sondeo, en vez
        de comprobarlo una sola vez nada más empezar la descarga."""
        deadline = asyncio.get_event_loop().time() + 15.0
        files = None
        while asyncio.get_event_loop().time() < deadline:
            files = self._download_manager.list_torrent_files(download)
            if files is not None:
                break
            await asyncio.sleep(0.3)
        if files and len(files) > 1:
            TorrentFilesDialog(self._download_manager, download, self).exec()

    def _on_open_settings(self) -> None:
        dialog = SettingsDialog(self)
        if dialog.exec():
            theme.apply_theme(QApplication.instance(), dialog.selected_theme())
            self._connection_manager.apply_global_speed_limits()
            self._remote_control_server.reload()

    def _on_theme_selected(self, theme_name: str) -> None:
        config = load_config()
        config.ui.theme = theme_name
        save_config(config)
        theme.apply_theme(QApplication.instance(), theme_name)

    def _on_language_selected(self, language_code: str) -> None:
        config = load_config()
        if config.ui.language == language_code:
            return
        config.ui.language = language_code
        save_config(config)
        # El aviso se muestra ya en el idioma recién elegido, no en el
        # que sigue activo hasta el próximo arranque -- si no, el texto
        # confirmando el cambio saldría en el idioma que se acaba de
        # dejar de usar.
        QMessageBox.information(self, t_in(language_code, "app_title"), t_in(language_code, "msg_restart_language"))

    def _on_about(self) -> None:
        AboutDialog(self).exec()

    # ---- Bandeja del sistema (punto 22 del backlog) ----

    def _build_tray_icon(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._tray_icon = None
            return
        self._tray_icon = QSystemTrayIcon(QIcon(str(ICON_PATH)), self)
        self._tray_icon.setToolTip(t("app_title"))
        tray_menu = QMenu(self)
        show_action = QAction(t("menu_tray_show"), self)
        show_action.triggered.connect(self._restore_from_tray)
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        tray_connect_all_action = QAction(t("menu_networks_connect_all"), self)
        tray_connect_all_action.triggered.connect(self._on_connect_all)
        tray_menu.addAction(tray_connect_all_action)
        tray_disconnect_all_action = QAction(t("menu_networks_disconnect_all"), self)
        tray_disconnect_all_action.triggered.connect(self._on_disconnect_all)
        tray_menu.addAction(tray_disconnect_all_action)
        tray_menu.addSeparator()
        tray_pause_all_action = QAction(t("menu_tray_pause_all"), self)
        tray_pause_all_action.triggered.connect(self._downloads_tab.pause_all)
        tray_menu.addAction(tray_pause_all_action)
        tray_resume_all_action = QAction(t("menu_tray_resume_all"), self)
        tray_resume_all_action.triggered.connect(self._downloads_tab.resume_all)
        tray_menu.addAction(tray_resume_all_action)
        tray_menu.addSeparator()
        tray_quit_action = QAction(t("menu_tray_quit"), self)
        tray_quit_action.triggered.connect(self._on_quit)
        tray_menu.addAction(tray_quit_action)
        self._tray_icon.setContextMenu(tray_menu)
        self._tray_icon.activated.connect(self._on_tray_activated)
        self._tray_icon.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self._restore_from_tray()

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_quit(self) -> None:
        self._quitting = True
        self.close()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if (
            event.type() == QEvent.Type.WindowStateChange
            and self.isMinimized()
            and self._tray_icon is not None
            and load_config().ui.minimize_to_tray_on_minimize
        ):
            self.hide()
            self._tray_icon.showMessage(t("app_title"), t("msg_tray_minimized"), QIcon(str(ICON_PATH)))

    def closeEvent(self, event) -> None:
        if not self._quitting and self._tray_icon is not None and load_config().ui.minimize_to_tray:
            event.ignore()
            self.hide()
            self._tray_icon.showMessage(t("app_title"), t("msg_tray_minimized"), QIcon(str(ICON_PATH)))
            return

        config = load_config()
        config.ui.window_width = self.width()
        config.ui.window_height = self.height()
        config.ui.window_x = self.x()
        config.ui.window_y = self.y()
        save_config(config)

        self._saved_search_manager.stop()
        self._watch_folder_manager.stop()
        self._bandwidth_scheduler.stop()
        self._remote_control_server.stop()
        for network in self._connection_manager.connected_networks():
            asyncio.ensure_future(self._connection_manager.disconnect_network(network))
        if self._tray_icon is not None:
            self._tray_icon.hide()
        super().closeEvent(event)

        # Qt normalmente cierra la app solo al cerrarse la última ventana
        # visible (quitOnLastWindowClosed), pero deja de hacerlo si la
        # ventana ya se había ocultado antes con hide() -exactamente lo
        # que pasa al minimizar a la bandeja y salir después desde su
        # menú contextual, con la ventana ya oculta en ese momento-.
        # Confirmado con un caso aislado: sin este quit() explícito,
        # app.exec()/loop.run_forever() no vuelve nunca tras ese ciclo, y
        # el proceso se queda vivo en memoria aunque el icono desaparezca
        # de la bandeja. Se llama siempre en la rama de cierre real (no en
        # la de "minimizar a la bandeja", que hace `event.ignore()` y
        # vuelve antes de llegar aquí).
        QApplication.instance().quit()
