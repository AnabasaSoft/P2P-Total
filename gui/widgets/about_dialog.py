"""Diálogo "Acerca de..." con el logo grande de la aplicación en la
parte superior. QMessageBox.about() no permite maquetar un logo
centrado y bien dimensionado (solo admite un icono pequeño a la
izquierda del texto), de ahí este diálogo propio."""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

from core.version import VERSION
from gui.i18n import t
from gui.resources import LOGO_PATH

GITHUB_URL = "https://github.com/AnabasaSoft/P2P-Total"
CONTACT_EMAIL = "anabasasoft@gmail.com"


class AboutDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("about_title"))
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        logo_label = QLabel()
        pixmap = QPixmap(str(LOGO_PATH))
        if not pixmap.isNull():
            pixmap = pixmap.scaledToWidth(320, Qt.TransformationMode.SmoothTransformation)
            logo_label.setPixmap(pixmap)
        logo_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(logo_label)

        text_label = QLabel(t("about_text"))
        text_label.setWordWrap(True)
        text_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(text_label)

        version_label = QLabel(t("about_version", version=VERSION))
        version_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(version_label)

        links_label = QLabel(
            f'<div align="center">{t("about_github_label")}: '
            f'<a href="{GITHUB_URL}">{GITHUB_URL}</a><br>'
            f'{t("about_email_label")}: '
            f'<a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a></div>'
        )
        links_label.setTextFormat(Qt.TextFormat.RichText)
        links_label.setOpenExternalLinks(True)
        links_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(links_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
