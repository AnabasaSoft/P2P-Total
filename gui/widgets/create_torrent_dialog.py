"""Diálogo para crear un `.torrent` nuevo a partir de contenido propio
(punto 37 del backlog) — compartir un archivo o carpeta local en
BitTorrent, algo que ya existía para las otras cuatro redes (punto 1)
pero que aquí requiere generar antes un fichero descriptor. Solo pide
los datos; la generación real (hasheo de piezas) y el guardado del
`.torrent` los hace `TorrentBackend.create_torrent()` en un hilo
aparte, para no bloquear la GUI con archivos grandes."""

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from backends.torrent_backend import DEFAULT_TRACKERS
from gui.i18n import t


class CreateTorrentDialog(QDialog):
    """Tras `exec()`, si el resultado es Accepted, `source_path`,
    `trackers`, `comment` y `private` quedan rellenos con lo elegido."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("dlg_create_torrent_title"))
        self.setMinimumSize(520, 420)
        self.source_path: str = ""
        self.trackers: list[str] = []
        self.comment: str = ""
        self.private: bool = False

        layout = QVBoxLayout(self)
        form = QFormLayout()

        path_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setAccessibleName(t("acc_torrent_source_path"))
        path_row.addWidget(self._path_edit)
        browse_file_button = QPushButton(t("btn_browse_file"))
        browse_file_button.clicked.connect(self._on_browse_file)
        path_row.addWidget(browse_file_button)
        browse_folder_button = QPushButton(t("btn_browse_folder"))
        browse_folder_button.clicked.connect(self._on_browse_folder)
        path_row.addWidget(browse_folder_button)
        form.addRow(t("lbl_torrent_source_path"), path_row)

        self._trackers_edit = QPlainTextEdit()
        self._trackers_edit.setPlainText("\n".join(DEFAULT_TRACKERS))
        self._trackers_edit.setAccessibleName(t("acc_torrent_trackers"))
        form.addRow(t("lbl_torrent_trackers"), self._trackers_edit)

        self._comment_edit = QLineEdit()
        self._comment_edit.setAccessibleName(t("acc_torrent_comment"))
        form.addRow(t("lbl_torrent_comment"), self._comment_edit)

        self._private_check = QCheckBox(t("chk_torrent_private"))
        form.addRow("", self._private_check)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_browse_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, t("btn_browse_file"))
        if path:
            self._path_edit.setText(path)

    def _on_browse_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, t("btn_browse_folder"))
        if path:
            self._path_edit.setText(path)

    def _on_accept(self) -> None:
        source_path = self._path_edit.text().strip()
        if not source_path:
            return
        self.source_path = source_path
        self.trackers = [line.strip() for line in self._trackers_edit.toPlainText().splitlines() if line.strip()]
        self.comment = self._comment_edit.text().strip()
        self.private = self._private_check.isChecked()
        self.accept()
