"""Gráfica de velocidad en tiempo real (punto 42 del backlog): dibuja
con `QPainter` puro -sin añadir dependencia de QtCharts- la evolución
de la velocidad agregada de bajada/subida de las cinco redes en los
últimos minutos, al estilo del gráfico de la pestaña de estadísticas
de qBittorrent/Transmission. `StatsTab` alimenta las muestras a partir
del delta de los totales ya persistidos en tiempo real por
`core.stats_tracker` (mismos datos que ya usa la tabla de totales, sin
tocar ningún backend)."""

from collections import deque

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from gui.models_qt import _format_speed

_DOWNLOAD_COLOR = QColor("#2ecc71")
_UPLOAD_COLOR = QColor("#e67e22")
_GRID_COLOR = QColor("#5a5b5c")

# A una muestra por cada refresco de StatsTab (2 s), 150 muestras cubren
# los últimos 5 minutos de historial visible en la gráfica.
MAX_SAMPLES = 150


class SpeedGraphWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(140)
        self._download_samples: deque[float] = deque(maxlen=MAX_SAMPLES)
        self._upload_samples: deque[float] = deque(maxlen=MAX_SAMPLES)

    def add_sample(self, download_bps: float, upload_bps: float) -> None:
        self._download_samples.append(max(0.0, download_bps))
        self._upload_samples.append(max(0.0, upload_bps))
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            self._paint(painter)
        finally:
            painter.end()

    def _paint(self, painter: QPainter) -> None:
        full_rect = QRectF(self.rect())
        # Hueco al pie para la leyenda con las velocidades actuales.
        graph_rect = full_rect.adjusted(4, 4, -4, -20)

        painter.setPen(_GRID_COLOR)
        painter.drawRect(graph_rect)
        for fraction in (0.25, 0.5, 0.75):
            y = graph_rect.bottom() - graph_rect.height() * fraction
            painter.drawLine(int(graph_rect.left()), int(y), int(graph_rect.right()), int(y))

        peak = max([*self._download_samples, *self._upload_samples, 1.0])
        self._draw_series(painter, graph_rect, self._download_samples, peak, _DOWNLOAD_COLOR)
        self._draw_series(painter, graph_rect, self._upload_samples, peak, _UPLOAD_COLOR)

        current_download = self._download_samples[-1] if self._download_samples else 0.0
        current_upload = self._upload_samples[-1] if self._upload_samples else 0.0
        legend_y = int(full_rect.bottom()) - 6
        painter.setPen(_DOWNLOAD_COLOR)
        painter.drawText(4, legend_y, f"↓ {_format_speed(current_download)}")
        upload_text = f"↑ {_format_speed(current_upload)}"
        painter.setPen(_UPLOAD_COLOR)
        text_width = painter.fontMetrics().horizontalAdvance(upload_text)
        painter.drawText(int(full_rect.right()) - text_width - 4, legend_y, upload_text)

    @staticmethod
    def _draw_series(painter: QPainter, rect: QRectF, samples: "deque[float]", peak: float, color: QColor) -> None:
        if len(samples) < 2:
            return
        painter.setPen(QPen(color, 2))
        # La muestra más reciente siempre pegada al borde derecho, para
        # que la gráfica se desplace hacia la izquierda al llegar datos
        # nuevos, igual que el gráfico de qBittorrent/Transmission.
        step_x = rect.width() / (MAX_SAMPLES - 1)
        offset = MAX_SAMPLES - len(samples)
        points = [
            (rect.left() + (offset + i) * step_x, rect.bottom() - (value / peak) * rect.height())
            for i, value in enumerate(samples)
        ]
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))
