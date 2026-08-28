"""Diálogo de selección de archivos de un torrent multi-archivo, con
casilla para activar la descarga secuencial (piezas en orden de
principio a fin en vez de rarest-first), útil para poder ir
reproduciendo un vídeo mientras el resto del torrent se sigue
descargando.

Se abre automáticamente al añadir un torrent con más de un archivo
(`MainWindow._maybe_show_torrent_file_selection`) y también a demanda
desde el menú contextual de la pestaña Transferencias. Los archivos que
se desmarquen y se acepten se borran del disco si ya tenían contenido
descargado (petición explícita del usuario: no basta con dejar de
descargarlos, tienen que desaparecer), vía
`DownloadManager.set_file_priorities` -> `backend.delete_deselected_files`."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from backends.torrent_backend import PRIORITY_NORMAL, PRIORITY_SKIP
from core.download_manager import DownloadManager
from core.models import Download
from gui.i18n import t
from gui.models_qt import _format_size

_COL_NAME = 0
_COL_SIZE = 1


class TorrentFilesDialog(QDialog):
    def __init__(self, manager: DownloadManager, download: Download, parent=None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._download = download
        self._files = manager.list_torrent_files(download) or []

        self.setWindowTitle(t("dlg_torrent_files_title"))
        self.setMinimumSize(480, 360)

        layout = QVBoxLayout(self)

        warning = QLabel(t("lbl_torrent_files_delete_warning"), self)
        warning.setWordWrap(True)
        layout.addWidget(warning)

        self._table = QTableWidget(len(self._files), 2, self)
        self._table.setHorizontalHeaderLabels([t("col_file_name"), t("col_file_size")])
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.horizontalHeader().setSectionResizeMode(_COL_NAME, self._table.horizontalHeader().ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row, file_info in enumerate(self._files):
            name_item = QTableWidgetItem(file_info["path"])
            name_item.setFlags(name_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = file_info["priority"] != PRIORITY_SKIP
            name_item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self._table.setItem(row, _COL_NAME, name_item)
            self._table.setItem(row, _COL_SIZE, QTableWidgetItem(_format_size(file_info["size_bytes"])))
        layout.addWidget(self._table)

        self._sequential_check = QCheckBox(t("chk_sequential_download"), self)
        self._sequential_check.setChecked(manager.is_sequential_download(download))
        layout.addWidget(self._sequential_check)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        priorities = {}
        for row, file_info in enumerate(self._files):
            checked = self._table.item(row, _COL_NAME).checkState() == Qt.CheckState.Checked
            priorities[file_info["index"]] = PRIORITY_NORMAL if checked else PRIORITY_SKIP
        self._manager.set_file_priorities(self._download, priorities)
        self._manager.set_sequential_download(self._download, self._sequential_check.isChecked())
        self.accept()
