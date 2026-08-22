"""Diálogo de Preferencias: carpeta de descargas, idioma, tema y la
configuración específica de cada red (equivalente en GUI a
`python main.py config`)."""

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from core.config import (
    Category, Config, disable_portable_mode, enable_portable_mode, is_portable_mode, load_config, save_config,
)
from gui.i18n import LANGUAGES, t
from gui.widgets.hub_list_dialog import HubListDialog


def _hostport_text(host: str, port: int) -> str:
    return f"{host}:{port}" if host else ""


def _parse_hostport(text: str, default_port: int) -> tuple[str, int]:
    text = text.strip()
    if not text:
        return "", default_port
    host, _, port_str = text.partition(":")
    return host, int(port_str) if port_str else default_port


class SettingsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("settings_title"))
        self.setMinimumWidth(460)
        self._config = load_config()

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        tabs.addTab(self._build_general_tab(), t("settings_tab_general"))
        tabs.addTab(self._build_torrent_tab(), t("settings_tab_torrent"))
        tabs.addTab(self._build_soulseek_tab(), t("settings_tab_soulseek"))
        tabs.addTab(self._build_dcpp_tab(), t("settings_tab_dcpp"))
        tabs.addTab(self._build_gnutella2_tab(), t("settings_tab_gnutella2"))
        tabs.addTab(self._build_emule_tab(), t("settings_tab_emule"))
        tabs.addTab(self._build_proxy_tab(), t("settings_tab_proxy"))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(t("btn_save"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(t("btn_cancel"))
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ---- Pestañas ----

    def _build_general_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)

        dir_row = QHBoxLayout()
        self._download_dir_edit = QLineEdit(self._config.default_download_dir)
        browse_button = QPushButton(t("btn_browse"))
        browse_button.clicked.connect(self._on_browse_download_dir)
        dir_row.addWidget(self._download_dir_edit)
        dir_row.addWidget(browse_button)
        form.addRow(t("lbl_download_dir"), dir_row)

        watch_row = QHBoxLayout()
        self._watch_dir_edit = QLineEdit(self._config.watched_torrent_dir)
        watch_browse_button = QPushButton(t("btn_browse"))
        watch_browse_button.clicked.connect(self._on_browse_watch_dir)
        watch_clear_button = QPushButton(t("btn_clear"))
        watch_clear_button.clicked.connect(lambda: self._watch_dir_edit.clear())
        watch_row.addWidget(self._watch_dir_edit)
        watch_row.addWidget(watch_browse_button)
        watch_row.addWidget(watch_clear_button)
        watch_label = QLabel(t("lbl_watch_folder"))
        watch_label.setToolTip(t("watch_folder_tooltip"))
        form.addRow(watch_label, watch_row)

        share_col = QVBoxLayout()
        self._shared_dirs_list = QListWidget()
        self._shared_dirs_list.addItems(self._config.shared_folders)
        self._shared_dirs_list.setMaximumHeight(90)
        share_col.addWidget(self._shared_dirs_list)
        share_buttons_row = QHBoxLayout()
        share_add_button = QPushButton(t("btn_add_folder"))
        share_add_button.clicked.connect(self._on_add_shared_dir)
        share_remove_button = QPushButton(t("btn_remove_folder"))
        share_remove_button.clicked.connect(self._on_remove_shared_dir)
        share_buttons_row.addWidget(share_add_button)
        share_buttons_row.addWidget(share_remove_button)
        share_col.addLayout(share_buttons_row)
        form.addRow(t("lbl_shared_folder"), share_col)

        category_col = QVBoxLayout()
        self._categories_list = QListWidget()
        for category in self._config.categories:
            self._add_category_item(category)
        self._categories_list.setMaximumHeight(90)
        category_col.addWidget(self._categories_list)
        category_buttons_row = QHBoxLayout()
        category_add_button = QPushButton(t("btn_add_category"))
        category_add_button.clicked.connect(self._on_add_category)
        category_remove_button = QPushButton(t("btn_remove_folder"))
        category_remove_button.clicked.connect(self._on_remove_category)
        category_buttons_row.addWidget(category_add_button)
        category_buttons_row.addWidget(category_remove_button)
        category_col.addLayout(category_buttons_row)
        form.addRow(t("lbl_categories"), category_col)

        self._language_combo = QComboBox()
        for code, label in LANGUAGES:
            self._language_combo.addItem(label, userData=code)
        self._language_combo.setCurrentIndex(
            max(0, [c for c, _ in LANGUAGES].index(self._config.ui.language))
        )
        form.addRow(t("lbl_language"), self._language_combo)

        self._theme_combo = QComboBox()
        self._theme_combo.addItem(t("theme_dark"), userData="dark")
        self._theme_combo.addItem(t("theme_light"), userData="light")
        self._theme_combo.setCurrentIndex(0 if self._config.ui.theme == "dark" else 1)
        form.addRow(t("lbl_theme"), self._theme_combo)

        self._download_limit_spin = QSpinBox()
        self._download_limit_spin.setRange(0, 1_000_000)
        self._download_limit_spin.setSuffix(" kB/s")
        self._download_limit_spin.setSpecialValueText(t("spin_unlimited_speed"))
        self._download_limit_spin.setValue(self._config.global_download_limit_kbps)
        form.addRow(t("lbl_global_download_limit"), self._download_limit_spin)

        self._upload_limit_spin = QSpinBox()
        self._upload_limit_spin.setRange(0, 1_000_000)
        self._upload_limit_spin.setSuffix(" kB/s")
        self._upload_limit_spin.setSpecialValueText(t("spin_unlimited_speed"))
        self._upload_limit_spin.setValue(self._config.global_upload_limit_kbps)
        form.addRow(t("lbl_global_upload_limit"), self._upload_limit_spin)

        self._auto_retry_attempts_spin = QSpinBox()
        self._auto_retry_attempts_spin.setRange(0, 100)
        self._auto_retry_attempts_spin.setSpecialValueText(t("spin_no_retry"))
        self._auto_retry_attempts_spin.setValue(self._config.auto_retry_max_attempts)
        form.addRow(t("lbl_auto_retry_attempts"), self._auto_retry_attempts_spin)

        self._auto_retry_delay_spin = QDoubleSpinBox()
        self._auto_retry_delay_spin.setRange(1.0, 3600.0)
        self._auto_retry_delay_spin.setSuffix(" s")
        self._auto_retry_delay_spin.setValue(self._config.auto_retry_delay_seconds)
        form.addRow(t("lbl_auto_retry_delay"), self._auto_retry_delay_spin)

        self._minimize_to_tray_check = QCheckBox()
        self._minimize_to_tray_check.setChecked(self._config.ui.minimize_to_tray)
        form.addRow(t("lbl_minimize_to_tray"), self._minimize_to_tray_check)

        self._minimize_to_tray_on_minimize_check = QCheckBox()
        self._minimize_to_tray_on_minimize_check.setChecked(self._config.ui.minimize_to_tray_on_minimize)
        form.addRow(t("lbl_minimize_to_tray_on_minimize"), self._minimize_to_tray_on_minimize_check)

        self._notify_on_finish_check = QCheckBox()
        self._notify_on_finish_check.setChecked(self._config.ui.notify_on_download_finish)
        form.addRow(t("lbl_notify_on_finish"), self._notify_on_finish_check)

        self._auto_verify_check = QCheckBox()
        self._auto_verify_check.setChecked(self._config.auto_verify_on_complete)
        self._auto_verify_check.setToolTip(t("auto_verify_tooltip"))
        form.addRow(t("chk_auto_verify"), self._auto_verify_check)

        config_io_row = QHBoxLayout()
        export_button = QPushButton(t("btn_export_config"))
        export_button.clicked.connect(self._on_export_config)
        import_button = QPushButton(t("btn_import_config"))
        import_button.clicked.connect(self._on_import_config)
        config_io_row.addWidget(export_button)
        config_io_row.addWidget(import_button)
        form.addRow(t("lbl_config_file"), config_io_row)

        self._portable_check = QCheckBox()
        self._portable_check.setChecked(is_portable_mode())
        self._portable_check.setToolTip(t("portable_tooltip"))
        form.addRow(t("lbl_portable_mode"), self._portable_check)

        return widget

    def _build_torrent_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)

        self._torrent_max_results_spin = QSpinBox()
        self._torrent_max_results_spin.setRange(0, 100000)
        self._torrent_max_results_spin.setSpecialValueText(t("spin_unlimited"))
        self._torrent_max_results_spin.setValue(self._config.torrent.max_results)
        form.addRow(t("lbl_max_results"), self._torrent_max_results_spin)

        self._torrent_timeout_spin = QDoubleSpinBox()
        self._torrent_timeout_spin.setRange(1.0, 300.0)
        self._torrent_timeout_spin.setSuffix(" s")
        self._torrent_timeout_spin.setValue(self._config.torrent.search_timeout)
        form.addRow(t("lbl_search_timeout"), self._torrent_timeout_spin)

        return widget

    def _build_soulseek_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        self._slsk_user_edit = QLineEdit(self._config.soulseek.username)
        self._slsk_pass_edit = QLineEdit(self._config.soulseek.password)
        self._slsk_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(t("lbl_username"), self._slsk_user_edit)
        form.addRow(t("lbl_password"), self._slsk_pass_edit)

        self._slsk_port_spin = QSpinBox()
        self._slsk_port_spin.setRange(1025, 65535)
        self._slsk_port_spin.setValue(self._config.soulseek.listen_port)
        form.addRow(t("lbl_listen_port"), self._slsk_port_spin)

        self._slsk_max_results_spin = QSpinBox()
        self._slsk_max_results_spin.setRange(0, 100000)
        self._slsk_max_results_spin.setSpecialValueText(t("spin_unlimited"))
        self._slsk_max_results_spin.setValue(self._config.soulseek.max_results)
        form.addRow(t("lbl_max_results"), self._slsk_max_results_spin)

        self._slsk_timeout_spin = QDoubleSpinBox()
        self._slsk_timeout_spin.setRange(1.0, 300.0)
        self._slsk_timeout_spin.setSuffix(" s")
        self._slsk_timeout_spin.setValue(self._config.soulseek.search_timeout)
        form.addRow(t("lbl_search_timeout"), self._slsk_timeout_spin)

        return widget

    def _build_dcpp_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        self._dcpp_nick_edit = QLineEdit(self._config.dcpp.nickname)
        self._dcpp_hub_edit = QLineEdit(
            _hostport_text(self._config.dcpp.default_hub_host, self._config.dcpp.default_hub_port)
        )
        dcpp_hub_button = QPushButton(t("btn_choose_from_hub_list"))
        dcpp_hub_button.clicked.connect(self._on_choose_dcpp_hub)
        dcpp_hub_row = QHBoxLayout()
        dcpp_hub_row.addWidget(self._dcpp_hub_edit)
        dcpp_hub_row.addWidget(dcpp_hub_button)
        self._dcpp_hub_pass_edit = QLineEdit(self._config.dcpp.default_hub_password)
        self._dcpp_hub_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._dcpp_port_spin = QSpinBox()
        self._dcpp_port_spin.setRange(1025, 65535)
        self._dcpp_port_spin.setValue(self._config.dcpp.listen_port)
        form.addRow(t("lbl_nickname"), self._dcpp_nick_edit)
        form.addRow(t("lbl_default_hub"), dcpp_hub_row)
        form.addRow(t("lbl_hub_password"), self._dcpp_hub_pass_edit)
        form.addRow(t("lbl_listen_port"), self._dcpp_port_spin)

        self._dcpp_max_results_spin = QSpinBox()
        self._dcpp_max_results_spin.setRange(0, 100000)
        self._dcpp_max_results_spin.setSpecialValueText(t("spin_unlimited"))
        self._dcpp_max_results_spin.setValue(self._config.dcpp.max_results)
        form.addRow(t("lbl_max_results"), self._dcpp_max_results_spin)

        self._dcpp_timeout_spin = QDoubleSpinBox()
        self._dcpp_timeout_spin.setRange(1.0, 300.0)
        self._dcpp_timeout_spin.setSuffix(" s")
        self._dcpp_timeout_spin.setValue(self._config.dcpp.search_timeout)
        form.addRow(t("lbl_search_timeout"), self._dcpp_timeout_spin)

        return widget

    def _build_gnutella2_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        self._g2_hub_edit = QLineEdit(
            _hostport_text(self._config.gnutella2.default_hub_host, self._config.gnutella2.default_hub_port)
        )
        form.addRow(t("lbl_default_hub"), self._g2_hub_edit)

        self._g2_port_spin = QSpinBox()
        self._g2_port_spin.setRange(1025, 65535)
        self._g2_port_spin.setValue(self._config.gnutella2.listen_port)
        form.addRow(t("lbl_listen_port"), self._g2_port_spin)

        self._g2_max_results_spin = QSpinBox()
        self._g2_max_results_spin.setRange(0, 100000)
        self._g2_max_results_spin.setSpecialValueText(t("spin_unlimited"))
        self._g2_max_results_spin.setValue(self._config.gnutella2.max_results)
        form.addRow(t("lbl_max_results"), self._g2_max_results_spin)

        self._g2_timeout_spin = QDoubleSpinBox()
        self._g2_timeout_spin.setRange(1.0, 300.0)
        self._g2_timeout_spin.setSuffix(" s")
        self._g2_timeout_spin.setValue(self._config.gnutella2.search_timeout)
        form.addRow(t("lbl_search_timeout"), self._g2_timeout_spin)

        return widget

    def _build_emule_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        self._emule_nick_edit = QLineEdit(self._config.emule.nickname)
        self._emule_server_edit = QLineEdit(
            _hostport_text(self._config.emule.default_server_host, self._config.emule.default_server_port)
        )
        self._emule_listen_spin = QSpinBox()
        self._emule_listen_spin.setRange(1025, 65535)
        self._emule_listen_spin.setValue(self._config.emule.listen_port)
        self._emule_kad_spin = QSpinBox()
        self._emule_kad_spin.setRange(1025, 65535)
        self._emule_kad_spin.setValue(self._config.emule.kad_udp_port)
        form.addRow(t("lbl_nickname"), self._emule_nick_edit)
        form.addRow(t("lbl_default_server"), self._emule_server_edit)
        form.addRow(t("lbl_listen_port"), self._emule_listen_spin)
        form.addRow(t("lbl_kad_udp_port"), self._emule_kad_spin)

        self._emule_max_results_spin = QSpinBox()
        self._emule_max_results_spin.setRange(0, 100000)
        self._emule_max_results_spin.setSpecialValueText(t("spin_unlimited"))
        self._emule_max_results_spin.setValue(self._config.emule.max_results)
        form.addRow(t("lbl_max_results"), self._emule_max_results_spin)

        self._emule_timeout_spin = QDoubleSpinBox()
        self._emule_timeout_spin.setRange(1.0, 300.0)
        self._emule_timeout_spin.setSuffix(" s")
        self._emule_timeout_spin.setValue(self._config.emule.search_timeout)
        form.addRow(t("lbl_search_timeout"), self._emule_timeout_spin)

        self._emule_obfuscation_combo = QComboBox()
        self._emule_obfuscation_combo.addItem(t("obfuscation_disabled"), userData="disabled")
        self._emule_obfuscation_combo.addItem(t("obfuscation_enabled"), userData="enabled")
        self._emule_obfuscation_combo.addItem(t("obfuscation_required"), userData="required")
        self._emule_obfuscation_combo.setCurrentIndex(
            {"disabled": 0, "enabled": 1, "required": 2}.get(self._config.emule.obfuscation, 1)
        )
        self._emule_obfuscation_combo.setToolTip(t("obfuscation_tooltip"))
        form.addRow(t("lbl_obfuscation"), self._emule_obfuscation_combo)

        return widget

    def _build_proxy_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)

        self._proxy_enabled_check = QCheckBox()
        self._proxy_enabled_check.setChecked(self._config.proxy.enabled)
        form.addRow(t("lbl_proxy_enabled"), self._proxy_enabled_check)

        self._proxy_kind_combo = QComboBox()
        self._proxy_kind_combo.addItem(t("proxy_kind_socks5"), userData="socks5")
        self._proxy_kind_combo.addItem(t("proxy_kind_http"), userData="http")
        self._proxy_kind_combo.setCurrentIndex(1 if self._config.proxy.kind == "http" else 0)
        form.addRow(t("lbl_proxy_kind"), self._proxy_kind_combo)

        self._proxy_host_edit = QLineEdit(self._config.proxy.host)
        form.addRow(t("lbl_proxy_host"), self._proxy_host_edit)

        self._proxy_port_spin = QSpinBox()
        self._proxy_port_spin.setRange(1, 65535)
        self._proxy_port_spin.setValue(self._config.proxy.port)
        form.addRow(t("lbl_proxy_port"), self._proxy_port_spin)

        self._proxy_user_edit = QLineEdit(self._config.proxy.username)
        form.addRow(t("lbl_proxy_username"), self._proxy_user_edit)

        self._proxy_pass_edit = QLineEdit(self._config.proxy.password)
        self._proxy_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(t("lbl_proxy_password"), self._proxy_pass_edit)

        note = QLabel(t("lbl_proxy_note"))
        note.setWordWrap(True)
        form.addRow(note)

        return widget

    # ---- Acciones ----

    def _on_browse_download_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, t("btn_browse"), self._download_dir_edit.text())
        if directory:
            self._download_dir_edit.setText(directory)

    def _on_browse_watch_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, t("btn_browse"), self._watch_dir_edit.text())
        if directory:
            self._watch_dir_edit.setText(directory)

    def _on_add_shared_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, t("btn_add_folder"))
        if directory:
            existing = [self._shared_dirs_list.item(i).text() for i in range(self._shared_dirs_list.count())]
            if directory not in existing:
                self._shared_dirs_list.addItem(directory)

    def _on_remove_shared_dir(self) -> None:
        for item in self._shared_dirs_list.selectedItems():
            self._shared_dirs_list.takeItem(self._shared_dirs_list.row(item))

    def _add_category_item(self, category: Category) -> None:
        item = QListWidgetItem(f"{category.name} — {category.dest_dir}")
        item.setData(Qt.ItemDataRole.UserRole, category)
        self._categories_list.addItem(item)

    def _on_add_category(self) -> None:
        name, ok = QInputDialog.getText(self, t("btn_add_category"), t("lbl_category_name"))
        if not ok or not name.strip():
            return
        existing = [
            self._categories_list.item(i).data(Qt.ItemDataRole.UserRole).name
            for i in range(self._categories_list.count())
        ]
        if name.strip() in existing:
            return
        directory = QFileDialog.getExistingDirectory(self, t("btn_add_category"))
        if not directory:
            return
        self._add_category_item(Category(name=name.strip(), dest_dir=directory))

    def _on_remove_category(self) -> None:
        for item in self._categories_list.selectedItems():
            self._categories_list.takeItem(self._categories_list.row(item))

    def _on_choose_dcpp_hub(self) -> None:
        dialog = HubListDialog(self)
        if dialog.exec() and dialog.selected_hub is not None:
            hub = dialog.selected_hub
            self._dcpp_hub_edit.setText(_hostport_text(hub.host, hub.port))

    def _collect_config_from_widgets(self) -> Config:
        """Vuelca en `self._config` (y lo devuelve) el valor actual de
        todos los campos del diálogo -- lo usan tanto `_on_save` como
        `_on_export_config` (punto 25 del backlog: la exportación saca
        lo editado en el diálogo, no necesariamente lo último guardado
        en disco)."""
        config = self._config

        config.default_download_dir = self._download_dir_edit.text().strip() or config.default_download_dir
        config.watched_torrent_dir = self._watch_dir_edit.text().strip()
        config.shared_folders = [
            self._shared_dirs_list.item(i).text() for i in range(self._shared_dirs_list.count())
        ]
        config.categories = [
            self._categories_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self._categories_list.count())
        ]

        config.ui.language = self._language_combo.currentData()
        config.ui.theme = self._theme_combo.currentData()
        config.global_download_limit_kbps = self._download_limit_spin.value()
        config.global_upload_limit_kbps = self._upload_limit_spin.value()
        config.auto_retry_max_attempts = self._auto_retry_attempts_spin.value()
        config.auto_retry_delay_seconds = self._auto_retry_delay_spin.value()
        config.ui.minimize_to_tray = self._minimize_to_tray_check.isChecked()
        config.ui.minimize_to_tray_on_minimize = self._minimize_to_tray_on_minimize_check.isChecked()
        config.ui.notify_on_download_finish = self._notify_on_finish_check.isChecked()
        config.auto_verify_on_complete = self._auto_verify_check.isChecked()

        config.torrent.max_results = self._torrent_max_results_spin.value()
        config.torrent.search_timeout = self._torrent_timeout_spin.value()

        config.soulseek.username = self._slsk_user_edit.text().strip()
        config.soulseek.password = self._slsk_pass_edit.text()
        config.soulseek.listen_port = self._slsk_port_spin.value()
        config.soulseek.max_results = self._slsk_max_results_spin.value()
        config.soulseek.search_timeout = self._slsk_timeout_spin.value()

        config.dcpp.nickname = self._dcpp_nick_edit.text().strip()
        host, port = _parse_hostport(self._dcpp_hub_edit.text(), 411)
        config.dcpp.default_hub_host, config.dcpp.default_hub_port = host, port
        config.dcpp.default_hub_password = self._dcpp_hub_pass_edit.text()
        config.dcpp.listen_port = self._dcpp_port_spin.value()
        config.dcpp.max_results = self._dcpp_max_results_spin.value()
        config.dcpp.search_timeout = self._dcpp_timeout_spin.value()

        host, port = _parse_hostport(self._g2_hub_edit.text(), 6346)
        config.gnutella2.default_hub_host, config.gnutella2.default_hub_port = host, port
        config.gnutella2.listen_port = self._g2_port_spin.value()
        config.gnutella2.max_results = self._g2_max_results_spin.value()
        config.gnutella2.search_timeout = self._g2_timeout_spin.value()

        config.emule.nickname = self._emule_nick_edit.text().strip() or config.emule.nickname
        host, port = _parse_hostport(self._emule_server_edit.text(), 4661)
        config.emule.default_server_host, config.emule.default_server_port = host, port
        config.emule.listen_port = self._emule_listen_spin.value()
        config.emule.kad_udp_port = self._emule_kad_spin.value()
        config.emule.max_results = self._emule_max_results_spin.value()
        config.emule.search_timeout = self._emule_timeout_spin.value()
        config.emule.obfuscation = self._emule_obfuscation_combo.currentData()

        config.proxy.enabled = self._proxy_enabled_check.isChecked()
        config.proxy.kind = self._proxy_kind_combo.currentData()
        config.proxy.host = self._proxy_host_edit.text().strip()
        config.proxy.port = self._proxy_port_spin.value()
        config.proxy.username = self._proxy_user_edit.text().strip()
        config.proxy.password = self._proxy_pass_edit.text()

        return config

    def _on_export_config(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, t("btn_export_config"), "config.json", "JSON (*.json)")
        if not path:
            return
        try:
            save_config(self._collect_config_from_widgets(), Path(path))
        except OSError as exc:
            QMessageBox.warning(self, t("settings_title"), t("msg_export_config_error") % str(exc))
            return
        QMessageBox.information(self, t("settings_title"), t("msg_export_config_done") % path)

    def _on_import_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, t("btn_import_config"), "", "JSON (*.json)")
        if not path:
            return
        try:
            imported = load_config(Path(path))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.warning(self, t("settings_title"), t("msg_import_config_error") % str(exc))
            return
        # Se guarda ya en la ubicación real y se cierra el diálogo sin
        # aplicar `self.accept()` normal: lo importado sustituye a lo que
        # hubiera editado en este mismo diálogo, así que no tendría
        # sentido "combinarlo" con `_on_save`.
        save_config(imported)
        QMessageBox.information(self, t("settings_title"), t("msg_import_config_done"))
        self.reject()

    def _on_save(self) -> None:
        original_language = self._config.ui.language
        portable_was = is_portable_mode()

        config = self._collect_config_from_widgets()
        save_config(config)

        portable_now = self._portable_check.isChecked()
        if portable_now and not portable_was:
            enable_portable_mode(config)
        elif not portable_now and portable_was:
            disable_portable_mode()

        messages = []
        if config.ui.language != original_language:
            messages.append(t("msg_restart_language"))
        if portable_now != portable_was:
            messages.append(t("msg_restart_portable"))
        if messages:
            QMessageBox.information(self, t("settings_title"), "\n\n".join(messages))

        self.accept()

    def selected_theme(self) -> str:
        return self._theme_combo.currentData()
