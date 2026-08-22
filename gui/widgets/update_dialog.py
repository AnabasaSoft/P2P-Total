"""Diálogo que avisa de que hay una versión más reciente publicada en
GitHub, con un botón para ir a descargarla y otro para descartar el
aviso."""

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QMessageBox

from gui.i18n import t


class UpdateAvailableDialog(QMessageBox):
    def __init__(self, new_version: str, release_url: str, parent=None) -> None:
        super().__init__(parent)
        self._release_url = release_url
        self.setWindowTitle(t("update_dialog_title"))
        self.setIcon(QMessageBox.Icon.Information)
        self.setText(t("update_dialog_text", version=new_version))
        download_button = self.addButton(t("update_dialog_download"), QMessageBox.ButtonRole.AcceptRole)
        self.addButton(t("update_dialog_cancel"), QMessageBox.ButtonRole.RejectRole)
        self.setDefaultButton(download_button)
        self._download_button = download_button

    def exec(self) -> int:
        result = super().exec()
        if self.clickedButton() == self._download_button:
            QDesktopServices.openUrl(QUrl(self._release_url))
        return result
