"""Diálogo "Acerca de..." con el logo grande de la aplicación en la
parte superior. QMessageBox.about() no permite maquetar un logo
centrado y bien dimensionado (solo admite un icono pequeño a la
izquierda del texto), de ahí este diálogo propio."""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

from gui.i18n import t
from gui.resources import LOGO_PATH


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

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
