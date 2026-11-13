"""Render the application icon (same design as the UI) to packaging/rr_connector.ico."""
import io
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from PIL import Image  # noqa: E402
from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

app = QGuiApplication([])
from rr_connector.ui import theme  # noqa: E402

pm = theme.app_icon(size=256).pixmap(256, 256)
buf = QBuffer()
buf.open(QIODevice.WriteOnly)
pm.save(buf, "PNG")
img = Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")
img.save(HERE / "rr_connector.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(HERE / "rr_connector.png")
print("icon written", HERE / "rr_connector.ico")
