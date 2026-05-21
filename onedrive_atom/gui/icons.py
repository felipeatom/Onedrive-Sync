"""App icons: SVG-based app icon + programmatic state icons for the tray."""

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap

try:
    from PyQt6.QtSvg import QSvgRenderer as _QSvgRenderer
    _SVG_AVAILABLE = True
except ImportError:
    _QSvgRenderer = None  # type: ignore
    _SVG_AVAILABLE = False

_SVG_PATH = Path(__file__).parent.parent / "resources" / "icons" / "onedrive-sync.svg"


def _render_svg(size: int) -> QPixmap:
    """Render the SVG at the given size. Returns an empty pixmap if unavailable."""
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    if not _SVG_AVAILABLE or not _SVG_PATH.exists():
        return px
    renderer = _QSvgRenderer(str(_SVG_PATH))
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    renderer.render(p)
    p.end()
    return px


def app_icon() -> QIcon:
    """Full-resolution app icon from the SVG file."""
    if _SVG_AVAILABLE and _SVG_PATH.exists():
        icon = QIcon()
        for size in (16, 22, 32, 48, 64, 128, 256):
            icon.addPixmap(_render_svg(size))
        return icon
    # Fallback if SVG or QtSvg not available
    return _cloud_icon(QColor("#0078d4"), 64)


def _cloud_icon(color: QColor, size: int = 22) -> QIcon:
    """Simple programmatic cloud shape for tray state indicators."""
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(color)
    p.setPen(Qt.PenStyle.NoPen)
    s = size
    p.drawEllipse(2,      s // 3,      s // 2,      s // 2)
    p.drawEllipse(s // 4, s // 5,      s // 2 + 2,  s // 2 + 2)
    p.drawEllipse(s // 2, s // 3,      s // 2 - 2,  s // 2 - 2)
    p.drawRect(2,         s // 2,      s - 4,        s // 3)
    p.end()
    return QIcon(px)


def _tray_icon(color: QColor) -> QIcon:
    """
    Tray icon: SVG rendered at 22px with a colored status dot in the corner.
    Falls back to plain programmatic cloud if SVG unavailable.
    """
    size = 22
    if not _SVG_AVAILABLE or not _SVG_PATH.exists():
        return _cloud_icon(color, size)

    px = _render_svg(size)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    dot_r = size // 6
    p.setBrush(color)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(size - dot_r * 2 - 1, size - dot_r * 2 - 1, dot_r * 2, dot_r * 2)

    p.end()
    return QIcon(px)


def icon_synced(size: int = 22) -> QIcon:
    return _tray_icon(QColor("#00b050"))   # green dot = all good


def icon_syncing(size: int = 22) -> QIcon:
    return _tray_icon(QColor("#ffaa00"))   # amber dot = in progress


def icon_paused(size: int = 22) -> QIcon:
    return _tray_icon(QColor("#888888"))   # gray dot = paused


def icon_error(size: int = 22) -> QIcon:
    return _tray_icon(QColor("#d13438"))   # red dot = error


def icon_offline(size: int = 22) -> QIcon:
    return _tray_icon(QColor("#aaaaaa"))   # light gray = offline


def icon_account(size: int = 32) -> QIcon:
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#0078d4"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(size // 4, size // 8, size // 2, size // 2)
    p.drawEllipse(size // 8, size // 2, size * 3 // 4, size // 2)
    p.end()
    return QIcon(px)


def icon_folder(size: int = 16) -> QIcon:
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#ffb900"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, size // 4, size, size * 3 // 4, 2, 2)
    p.drawRoundedRect(0, size // 8, size // 2, size // 4 + 2, 2, 2)
    p.end()
    return QIcon(px)
