"""Dark navy trading theme, status colours and the programmatic application icon."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap

BG = "#070f1f"
PANEL = "#0d1a33"
CARD = "#112244"
LINE = "#1d3561"
TEXT = "#e7eefb"
DIM = "#8297bd"
BLUE = "#2f7dff"
CYAN = "#3fd0ff"
GREEN = "#19c37d"
RED = "#ff4d5e"
AMBER = "#f5a524"

STATE_COLOR = {"CONNECTED": GREEN, "CONNECTING": AMBER, "RECONNECTING": AMBER, "CHOOSE": AMBER,
               "DISCONNECTED": RED, "ERROR": RED, "REJECTED": RED, "STOPPED": RED, "NOT_CONFIGURED": AMBER}

QSS = f"""
* {{ font-family: "Segoe UI", "Inter", "Helvetica Neue", Arial; color: {TEXT}; }}
QMainWindow, QDialog, QWidget#root {{ background: {BG}; }}
QLabel {{ background: transparent; }}
QLabel#title {{ font-size: 19px; font-weight: 700; letter-spacing: 3px; }}
QLabel#subtitle {{ color: {DIM}; font-size: 11px; letter-spacing: 1px; }}
QLabel#section {{ color: {CYAN}; font-size: 11px; font-weight: 700; letter-spacing: 2px; }}
QLabel#cardLabel {{ color: {DIM}; font-size: 10px; font-weight: 600; letter-spacing: 1px; }}
QLabel#cardValue {{ font-size: 16px; font-weight: 600; }}
QLabel#message {{ color: {DIM}; font-size: 12px; }}
QLabel#warn {{ color: {AMBER}; font-size: 12px; }}
QFrame#card {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 10px; }}
QFrame#panel {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 14px; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: #091530; border: 1px solid {LINE}; border-radius: 8px; padding: 9px 11px; font-size: 14px;
    selection-background-color: {BLUE}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border: 1px solid {BLUE}; }}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid {LINE}; selection-background-color: {BLUE}; }}
QPushButton {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 8px; padding: 9px 16px; font-weight: 600; }}
QPushButton:hover {{ border-color: {BLUE}; }}
QPushButton:disabled {{ color: #4d5f80; border-color: #15284a; }}
QPushButton#primary {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {BLUE}, stop:1 #1f5fe0); border: none;
    font-size: 15px; letter-spacing: 2px; padding: 12px; }}
QPushButton#primary:hover {{ background: #4a8fff; }}
QPushButton#danger {{ background: #3a1220; border: 1px solid {RED}; color: #ffd5da; }}
QPushButton#danger:hover {{ background: #5a1628; }}
QPushButton#live {{ background: #2a1a05; border: 1px solid {AMBER}; color: #ffe2b0; }}
QCheckBox {{ spacing: 8px; }}
QTableWidget {{ background: {PANEL}; gridline-color: {LINE}; border: 1px solid {LINE}; border-radius: 8px; }}
QHeaderView::section {{ background: {CARD}; color: {DIM}; border: none; padding: 6px; font-weight: 600; }}
QScrollBar:vertical {{ background: {PANEL}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {LINE}; border-radius: 5px; }}
QMenu {{ background: {PANEL}; border: 1px solid {LINE}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; }}
QMenu::item:selected {{ background: {BLUE}; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {LINE}; }}
"""


def app_icon(status_color: str | None = None, size: int = 256) -> QIcon:
    """Shield + upward chart mark. A small status dot is drawn for the tray icon."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 256
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0, QColor("#1b4fd6"))
    grad.setColorAt(1, QColor("#0a1a3d"))
    p.setBrush(QBrush(grad))
    p.setPen(QPen(QColor(CYAN), 6 * s))
    p.drawRoundedRect(QRectF(14 * s, 14 * s, 228 * s, 228 * s), 52 * s, 52 * s)
    path = QPainterPath(QPointF(52 * s, 176 * s))
    for x, y in ((98, 128), (130, 150), (196, 78)):
        path.lineTo(QPointF(x * s, y * s))
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(GREEN), 18 * s, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPath(path)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(GREEN))
    p.drawEllipse(QPointF(196 * s, 78 * s), 16 * s, 16 * s)
    f = QFont("Segoe UI", int(46 * s))
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor(TEXT))
    p.drawText(QRectF(40 * s, 168 * s, 120 * s, 70 * s), Qt.AlignLeft | Qt.AlignVCenter, "RR")
    if status_color:
        p.setBrush(QColor(status_color))
        p.setPen(QPen(QColor(BG), 10 * s))
        p.drawEllipse(QPointF(206 * s, 206 * s), 40 * s, 40 * s)
    p.end()
    return QIcon(pm)
