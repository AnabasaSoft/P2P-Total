"""
Estilo visual de la GUI: tonos grises con acentos de color, al estilo
de clientes P2P clásicos como aMule o Shareaza (paneles grises,
pestañas planas, tabla de transferencias con barras de progreso,
lista de redes con un "piloto" de color por estado de conexión).

Un único QSS generado a partir de una paleta de colores, para que el
tema claro y el oscuro compartan exactamente la misma estructura de
reglas y solo cambien los colores.
"""

from PyQt6.QtWidgets import QApplication

from core.models import Network
from gui.connection_manager import STATUS_CONNECTED, STATUS_CONNECTING, STATUS_DISCONNECTED, STATUS_ERROR

# Color identificativo de cada red, reutilizado en el panel de redes,
# la tabla de resultados y la de transferencias — mismo concepto que
# los iconos de color por red de Shareaza.
NETWORK_COLORS = {
    Network.TORRENT: "#e67e22",     # naranja
    Network.SOULSEEK: "#2f8fd6",    # azul
    Network.DCPP: "#27ae60",        # verde
    Network.GNUTELLA2: "#16a085",   # verde azulado
    Network.EMULE: "#e0b30f",       # amarillo/dorado
    Network.AGGREGATED: "#9b59b6",  # morado, distinto de las cinco redes reales (punto 44, fase 2)
}

# Mismo "piloto" de color por estado de conexión que usaba el antiguo
# panel lateral, reutilizado ahora por la pestaña Red y el menú Redes.
STATUS_DOT_COLORS = {
    STATUS_DISCONNECTED: "#7a7a7a",
    STATUS_CONNECTING: "#e0b30f",
    STATUS_CONNECTED: "#2ecc71",
    STATUS_ERROR: "#e74c3c",
}

_DARK_PALETTE = {
    "window": "#2e2f30",
    "panel": "#363738",
    "panel_alt": "#3d3e40",
    "border": "#232425",
    "text": "#e0e0e0",
    "text_dim": "#9a9a9a",
    "accent": "#3daee9",
    "accent_dark": "#2f8fc0",
    "input_bg": "#232425",
    "selection": "#3daee9",
    "selection_text": "#ffffff",
    "header": "#232425",
    "alt_row": "#333435",
}

_LIGHT_PALETTE = {
    "window": "#eeeeee",
    "panel": "#f5f5f5",
    "panel_alt": "#e8e8e8",
    "border": "#c6c6c6",
    "text": "#1e1e1e",
    "text_dim": "#6a6a6a",
    "accent": "#2f8fd6",
    "accent_dark": "#1f6fae",
    "input_bg": "#ffffff",
    "selection": "#2f8fd6",
    "selection_text": "#ffffff",
    "header": "#e2e2e2",
    "alt_row": "#eaeaea",
}


def _build_qss(p: dict) -> str:
    return f"""
QWidget {{
    background-color: {p['window']};
    color: {p['text']};
    font-size: 10.5pt;
}}

QMainWindow, QDialog {{
    background-color: {p['window']};
}}

QMenuBar {{
    background-color: {p['panel']};
    border-bottom: 1px solid {p['border']};
}}
QMenuBar::item:selected {{
    background-color: {p['accent']};
    color: {p['selection_text']};
}}
QMenu {{
    background-color: {p['panel']};
    border: 1px solid {p['border']};
}}
QMenu::item:selected {{
    background-color: {p['accent']};
    color: {p['selection_text']};
}}

QToolBar {{
    background-color: {p['panel']};
    border-bottom: 1px solid {p['border']};
    spacing: 4px;
    padding: 3px;
}}

QStatusBar {{
    background-color: {p['panel']};
    border-top: 1px solid {p['border']};
}}

QTabWidget::pane {{
    border: 1px solid {p['border']};
    background-color: {p['panel']};
    top: -1px;
}}
QTabBar::tab {{
    background-color: {p['panel_alt']};
    color: {p['text_dim']};
    border: 1px solid {p['border']};
    border-bottom: none;
    padding: 6px 14px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background-color: {p['panel']};
    color: {p['text']};
    border-bottom: 2px solid {p['accent']};
}}
QTabBar::tab:hover:!selected {{
    color: {p['text']};
}}

QGroupBox {{
    border: 1px solid {p['border']};
    border-radius: 4px;
    margin-top: 10px;
    padding-top: 8px;
    font-weight: bold;
    color: {p['text_dim']};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
}}

QListWidget, QTreeWidget, QTableWidget, QTableView {{
    background-color: {p['panel']};
    alternate-background-color: {p['alt_row']};
    border: 1px solid {p['border']};
    gridline-color: {p['border']};
    selection-background-color: {p['selection']};
    selection-color: {p['selection_text']};
}}
QHeaderView::section {{
    background-color: {p['header']};
    color: {p['text_dim']};
    border: none;
    border-right: 1px solid {p['border']};
    border-bottom: 1px solid {p['border']};
    padding: 4px 6px;
}}

QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit {{
    background-color: {p['input_bg']};
    border: 1px solid {p['border']};
    border-radius: 3px;
    padding: 4px 6px;
    selection-background-color: {p['selection']};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
    border: 1px solid {p['accent']};
}}
QComboBox::drop-down {{
    border: none;
    width: 18px;
}}

QPushButton {{
    background-color: {p['panel_alt']};
    border: 1px solid {p['border']};
    border-radius: 3px;
    padding: 5px 14px;
}}
QPushButton:hover {{
    border: 1px solid {p['accent']};
}}
QPushButton:pressed {{
    background-color: {p['accent']};
    color: {p['selection_text']};
}}
QPushButton:disabled {{
    color: {p['text_dim']};
}}
QPushButton#primary {{
    background-color: {p['accent']};
    color: {p['selection_text']};
    font-weight: bold;
    border: none;
}}
QPushButton#primary:hover {{
    background-color: {p['accent_dark']};
}}

QProgressBar {{
    border: 1px solid {p['border']};
    border-radius: 3px;
    background-color: {p['input_bg']};
    text-align: center;
    color: {p['text']};
}}
QProgressBar::chunk {{
    background-color: {p['accent']};
    border-radius: 2px;
}}

QDockWidget {{
    color: {p['text_dim']};
    font-weight: bold;
    titlebar-close-icon: none;
}}
QDockWidget::title {{
    background-color: {p['panel_alt']};
    padding: 4px;
    border-bottom: 1px solid {p['border']};
}}

QCheckBox::indicator, QRadioButton::indicator {{
    width: 13px;
    height: 13px;
}}

QScrollBar:vertical {{
    background-color: {p['panel']};
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background-color: {p['panel_alt']};
    border: 1px solid {p['border']};
    min-height: 24px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical:hover {{
    background-color: {p['accent']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background-color: {p['panel']};
    height: 12px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background-color: {p['panel_alt']};
    border: 1px solid {p['border']};
    min-width: 24px;
    border-radius: 4px;
}}
QScrollBar::handle:horizontal:hover {{
    background-color: {p['accent']};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

QLabel#dimLabel {{
    color: {p['text_dim']};
}}
QLabel#sectionTitle {{
    color: {p['text_dim']};
    font-weight: bold;
    padding: 4px 0;
}}
"""


THEMES = {
    "dark": _build_qss(_DARK_PALETTE),
    "light": _build_qss(_LIGHT_PALETTE),
}


def apply_theme(app: QApplication, theme_name: str) -> None:
    app.setStyleSheet(THEMES.get(theme_name, THEMES["dark"]))
