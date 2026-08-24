"""Diálogo que avisa de que hay una versión más reciente publicada en
GitHub. Cuando el tipo de instalación actual soporta auto-actualización
(`core.self_updater.can_self_update`) ofrece un botón para actualizar
en el momento; si no, cae al comportamiento de siempre: un botón para
ir a la página de descarga."""

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QMessageBox

from gui.i18n import t


class UpdateAvailableDialog(QMessageBox):
    def __init__(self, new_version: str, release_url: str, self_update_available: bool, parent=None) -> None:
        super().__init__(parent)
        self._release_url = release_url
        self.setWindowTitle(t("update_dialog_title"))
        self.setIcon(QMessageBox.Icon.Information)
        text_key = "update_dialog_text_auto" if self_update_available else "update_dialog_text"
        self.setText(t(text_key, version=new_version))

        self._update_now_button = None
        if self_update_available:
            self._update_now_button = self.addButton(t("update_dialog_update_now"), QMessageBox.ButtonRole.AcceptRole)
        download_button = self.addButton(t("update_dialog_download"), QMessageBox.ButtonRole.ActionRole)
        self.addButton(t("update_dialog_cancel"), QMessageBox.ButtonRole.RejectRole)
        self.setDefaultButton(self._update_now_button or download_button)
        self._download_button = download_button

    def update_now_clicked(self) -> bool:
        return self._update_now_button is not None and self.clickedButton() == self._update_now_button

    def exec(self) -> int:
        result = super().exec()
        if self.clickedButton() == self._download_button:
            QDesktopServices.openUrl(QUrl(self._release_url))
        return result
