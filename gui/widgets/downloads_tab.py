"""Pestaña de transferencias: tabla de descargas en curso/completadas
con barra de progreso real y menú contextual (pausar/reanudar/
cancelar/iniciar/reiniciar/abrir carpeta), al estilo de la pestaña
"Transferencias" de aMule."""

import asyncio

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QHeaderView, QInputDialog, QMenu, QMessageBox, QVBoxLayout, QWidget,
)

from core.download_manager import DownloadManager
from core.models import REAL_NETWORKS, Download, DownloadState, Network, decode_combined_source_id
from gui.connection_manager import STATUS_CONNECTED, ConnectionManager
from gui.desktop_open import open_local_path
from gui.i18n import t
from gui.models_qt import DownloadsModel, DownloadsSortProxy
from gui.widgets.accessible_table import AccessibleTableView
from gui.widgets.delegates import ProgressBarDelegate
from gui.widgets.torrent_files_dialog import TorrentFilesDialog

# Bug real reportado por el usuario: nada más arrancar (o al reconectar
# una red), una descarga persistida como DOWNLOADING mostraba bien
# "Pausar" en el menú contextual, pero en cuanto arrancaba de verdad
# pasaba brevemente (o, con torrents grandes que necesitan recomprobar
# muchos datos ya en disco, no tan brevemente) por QUEUED/
# SEARCHING_SOURCES antes de llegar a DOWNLOADING -y durante ese tramo
# "Pausar" desaparecía del menú sin motivo real: pausar tiene sentido
# en cualquiera de estos tres estados "activos", no solo mientras ya
# hay bytes bajando.
_PAUSABLE_STATES = (DownloadState.QUEUED, DownloadState.SEARCHING_SOURCES, DownloadState.DOWNLOADING)


def _aggregated_has_torrent(download: Download) -> bool:
    """Punto 44 del backlog, fase 2: `AggregatedDownloadSession.pause()`/
    `.resume()` rechazan de plano una descarga combinada que incluya
    BitTorrent (ver `core/aggregated_download.py`), así que ofrecer
    "Pausar" para ella en el menú -o desde "Pausar todo"- solo lleva a
    un error que antes se tragaba en silencio (`asyncio.ensure_future`
    sin manejo de excepción). Se decodifica el `source_id` combinado
    -ligero, vive en `core/models.py` sin arrastrar backends- para
    saberlo de antemano y no ofrecer la acción."""
    if download.network != Network.AGGREGATED:
        return False
    try:
        return Network.TORRENT in decode_combined_source_id(download.source_id)
    except (ValueError, KeyError, TypeError):
        return False


def _is_pausable(download: Download, model) -> bool:
    return (
        download.state in _PAUSABLE_STATES
        and model.is_network_connected(download.network)
        and not _aggregated_has_torrent(download)
    )


class DownloadsTab(QWidget):
    def __init__(
        self, download_manager: DownloadManager, connection_manager: ConnectionManager, parent=None,
    ) -> None:
        super().__init__(parent)
        self._manager = download_manager

        layout = QVBoxLayout(self)

        self._model = DownloadsModel(self)
        self._proxy = DownloadsSortProxy(self)
        self._proxy.setSourceModel(self._model)
        self._table = AccessibleTableView()
        self._table.setAccessibleName(t("acc_downloads_table"))
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setDragEnabled(True)
        self._table.setAcceptDrops(True)
        self._table.setDropIndicatorShown(True)
        self._table.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._table.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._table.setItemDelegateForColumn(DownloadsModel.COL_PROGRESS, ProgressBarDelegate(self._table))
        self._table.horizontalHeader().setSectionResizeMode(
            DownloadsModel.COL_NAME, QHeaderView.ResizeMode.Stretch
        )
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self._table)

        # Punto 34.7 del backlog (accesibilidad): atajo de teclado
        # estándar (Supr) para borrar la selección sin tener que abrir
        # el menú contextual, igual que en un gestor de archivos.
        delete_shortcut = QShortcut(QKeySequence.StandardKey.Delete, self._table)
        delete_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_shortcut.activated.connect(lambda: self._confirm_and_delete(self._selected_downloads()))

        self._model.set_downloads(self._manager.load_history())
        self._manager.on_progress(self._on_progress)
        self._model.order_changed.connect(self._on_order_changed)

        # Bug real reportado por el usuario: al arrancar sin conexión a
        # una red, sus descargas seguían mostrando el estado
        # "Descargando"/"Buscando fuentes"/"En cola" persistido de la
        # última sesión conectada, lo cual es imposible sin conexión.
        for network in REAL_NETWORKS:
            self._model.set_network_connected(network, connection_manager.is_connected(network))
        connection_manager.status_changed.connect(self._on_network_status_changed)

    def _on_network_status_changed(self, network_value: str, status: str, _message: str) -> None:
        self._model.set_network_connected(Network(network_value), status == STATUS_CONNECTED)

    def add_download(self, download: Download) -> None:
        self._model.add_download(download)

    def active_count(self) -> int:
        return self._model.active_count()

    def pause_all(self) -> None:
        for download in self._model.downloads_in_order():
            if _is_pausable(download, self._model):
                asyncio.ensure_future(self._manager.pause(download))

    def resume_all(self) -> None:
        for download in self._model.downloads_in_order():
            if download.state == DownloadState.PAUSED and self._model.is_network_connected(download.network):
                asyncio.ensure_future(self._manager.resume(download))

    def active_speed_bps(self) -> float:
        return self._model.active_speed_bps()

    def _on_progress(self, download: Download) -> None:
        self._model.update_download(download)

    def _selected_downloads(self) -> list[Download]:
        rows = sorted({
            self._proxy.mapToSource(index).row()
            for index in self._table.selectionModel().selectedRows()
        })
        return [self._model.download_at(row) for row in rows]

    def _selected_source_rows(self) -> list[int]:
        return sorted({
            self._proxy.mapToSource(index).row()
            for index in self._table.selectionModel().selectedRows()
        })

    def _on_order_changed(self) -> None:
        self._manager.reorder(self._model.downloads_in_order())

    def _on_context_menu(self, pos) -> None:
        index = self._table.indexAt(pos)
        if index.isValid():
            selection = self._table.selectionModel()
            if not selection.isRowSelected(index.row(), index.parent()):
                self._table.selectRow(index.row())

        downloads = self._selected_downloads()

        menu = QMenu(self)
        pause_action = resume_action = cancel_action = delete_action = None
        restart_action = restart_from_scratch_action = None
        open_folder_action = speed_limit_action = torrent_files_action = None
        move_up_action = move_down_action = verify_action = None
        if downloads:
            if any(_is_pausable(d, self._model) for d in downloads):
                pause_action = menu.addAction(t("ctx_pause"))
            if any(
                d.state == DownloadState.PAUSED and self._model.is_network_connected(d.network) for d in downloads
            ):
                resume_action = menu.addAction(t("ctx_resume"))
            if any(d.state not in (DownloadState.COMPLETED, DownloadState.CANCELLED) for d in downloads):
                cancel_action = menu.addAction(t("ctx_cancel"))
            if any(
                d.state == DownloadState.CANCELLED and d.network != Network.AGGREGATED for d in downloads
            ):
                # Una descarga agregada (punto 44, fase 2) no guarda
                # progreso por red entre sesiones, así que "Iniciar"
                # (retomar donde se quedó) no tiene sentido para ella —
                # solo "Reiniciar" (desde cero), que sí soporta.
                restart_action = menu.addAction(t("ctx_restart"))
            if any(d.state == DownloadState.CANCELLED for d in downloads):
                restart_from_scratch_action = menu.addAction(t("ctx_restart_from_scratch"))
            delete_action = menu.addAction(t("ctx_delete"))
            speed_limit_action = menu.addAction(t("ctx_speed_limit"))
            if (
                len(downloads) == 1
                and downloads[0].network == Network.TORRENT
                and self._manager.list_torrent_files(downloads[0])
            ):
                torrent_files_action = menu.addAction(t("ctx_torrent_files"))
            if (
                len(downloads) == 1
                and downloads[0].state == DownloadState.COMPLETED
                and self._manager.supports_verify(downloads[0])
            ):
                verify_action = menu.addAction(t("ctx_verify"))
            if len(downloads) == 1:
                menu.addSeparator()
                open_folder_action = menu.addAction(t("ctx_open_folder"))
                source_rows = self._selected_source_rows()
                if source_rows[0] > 0:
                    move_up_action = menu.addAction(t("ctx_move_up"))
                if source_rows[0] < self._model.rowCount() - 1:
                    move_down_action = menu.addAction(t("ctx_move_down"))
            menu.addSeparator()

        clear_completed_action = menu.addAction(t("ctx_clear_completed"))
        clear_completed_action.setEnabled(self._model.has_completed())

        if menu.isEmpty():
            return
        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action is None:
            return
        if action == pause_action:
            for download in downloads:
                if _is_pausable(download, self._model):
                    asyncio.ensure_future(self._manager.pause(download))
        elif action == resume_action:
            for download in downloads:
                if download.state == DownloadState.PAUSED and self._model.is_network_connected(download.network):
                    asyncio.ensure_future(self._manager.resume(download))
        elif action == cancel_action:
            cancellable = [d for d in downloads if d.state not in (DownloadState.COMPLETED, DownloadState.CANCELLED)]
            self._confirm_and_cancel(cancellable)
        elif action == restart_action:
            for download in downloads:
                if download.state == DownloadState.CANCELLED and download.network != Network.AGGREGATED:
                    asyncio.ensure_future(self._manager.restart(download))
        elif action == restart_from_scratch_action:
            for download in downloads:
                if download.state == DownloadState.CANCELLED:
                    asyncio.ensure_future(self._manager.restart(download, from_scratch=True))
        elif action == delete_action:
            self._confirm_and_delete(downloads)
        elif action == open_folder_action:
            open_local_path(downloads[0].dest_path)
        elif action == speed_limit_action:
            self._on_set_speed_limit(downloads)
        elif action == torrent_files_action:
            TorrentFilesDialog(self._manager, downloads[0], self).exec()
        elif action == verify_action:
            self._manager.request_verify(downloads[0])
        elif action == move_up_action:
            self._model.move_row(self._selected_source_rows()[0], -1)
        elif action == move_down_action:
            self._model.move_row(self._selected_source_rows()[0], 1)
        elif action == clear_completed_action:
            self._model.remove_completed()
            self._manager.clear_completed()

    def _confirm_and_cancel(self, downloads: list[Download]) -> None:
        if not downloads:
            return
        box = QMessageBox(self)
        box.setWindowTitle(t("dlg_confirm_cancel_title"))
        if len(downloads) == 1:
            box.setText(t("dlg_confirm_cancel_text", name=downloads[0].title))
        else:
            box.setText(t("dlg_confirm_cancel_text_multi", n=len(downloads)))
        yes_button = box.addButton(t("btn_yes"), QMessageBox.ButtonRole.YesRole)
        box.addButton(t("btn_no"), QMessageBox.ButtonRole.NoRole)
        box.exec()
        if box.clickedButton() == yes_button:
            for download in downloads:
                asyncio.ensure_future(self._manager.cancel(download))

    def _confirm_and_delete(self, downloads: list[Download]) -> None:
        if not downloads:
            return
        box = QMessageBox(self)
        box.setWindowTitle(t("dlg_confirm_delete_title"))
        if len(downloads) == 1:
            box.setText(t("dlg_confirm_delete_text", name=downloads[0].title))
        else:
            box.setText(t("dlg_confirm_delete_text_multi", n=len(downloads)))
        yes_button = box.addButton(t("btn_yes"), QMessageBox.ButtonRole.YesRole)
        box.addButton(t("btn_no"), QMessageBox.ButtonRole.NoRole)
        box.exec()
        if box.clickedButton() == yes_button:
            for download in downloads:
                asyncio.ensure_future(self._delete_download(download))

    async def _delete_download(self, download: Download) -> None:
        await self._manager.delete(download)
        self._model.remove_by_id(download.id)

    def _on_set_speed_limit(self, downloads: list[Download]) -> None:
        current_kbps = downloads[0].speed_limit_bps // 1024
        label = f"{t('dlg_speed_limit_label')} (kB/s, 0 = {t('spin_unlimited_speed').lower()})"
        value, ok = QInputDialog.getInt(
            self, t("dlg_speed_limit_title"), label, current_kbps, 0, 1_000_000,
        )
        if not ok:
            return
        for download in downloads:
            self._manager.set_download_limit(download, value * 1024)
