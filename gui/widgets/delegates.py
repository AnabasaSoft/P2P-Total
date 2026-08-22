"""Delegado de tabla que dibuja una barra de progreso real en la celda,
al estilo de la columna "% completado" de aMule/qBittorrent: color
según el estado de la descarga (verde = descargando, azul = completado,
naranja = pausado, rojo = error/cancelado, gris = en cola) en vez del
único color de acento del tema del sistema, más un degradado suave para
imitar el aspecto "brillante" clásico de aMule."""

from PyQt6.QtWidgets import QStyledItemDelegate
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QColor, QLinearGradient, QPainter

from gui.models_qt import DownloadsModel
from core.models import DownloadState

# Color base por estado (se ilumina/oscurece con un degradado al pintar).
_STATE_COLORS = {
    DownloadState.QUEUED: QColor("#9e9e9e"),
    DownloadState.SEARCHING_SOURCES: QColor("#42a5f5"),
    DownloadState.DOWNLOADING: QColor("#43a047"),
    DownloadState.PAUSED: QColor("#fb8c00"),
    DownloadState.COMPLETED: QColor("#1e88e5"),
    DownloadState.ERROR: QColor("#e53935"),
    DownloadState.CANCELLED: QColor("#757575"),
}


class ProgressBarDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option, index) -> None:
        progress = index.data(DownloadsModel.PROGRESS_ROLE)
        if progress is None:
            super().paint(painter, option, index)
            return

        state = index.data(DownloadsModel.STATE_ROLE)
        base_color = _STATE_COLORS.get(state, QColor("#9e9e9e"))

        rect = QRectF(option.rect.adjusted(2, 2, -2, -2))
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Fondo (surco vacío).
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#e0e0e0"))
        painter.drawRoundedRect(rect, 3, 3)

        # Relleno con degradado (más claro arriba, más oscuro abajo).
        fill_width = rect.width() * max(0.0, min(1.0, progress))
        if fill_width > 0:
            fill_rect = QRectF(rect.x(), rect.y(), fill_width, rect.height())
            gradient = QLinearGradient(fill_rect.topLeft(), fill_rect.bottomLeft())
            gradient.setColorAt(0.0, base_color.lighter(130))
            gradient.setColorAt(1.0, base_color.darker(115))
            painter.setBrush(gradient)
            painter.drawRoundedRect(fill_rect, 3, 3)

        # Borde y porcentaje centrado.
        painter.setPen(QColor("#9e9e9e"))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 3, 3)
        painter.setPen(QColor("#000000"))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"{int(progress * 100)}%")

        painter.restore()
