# PyInstaller spec — builds dist/RR_MT5_Connector/RR_MT5_Connector.exe (one-folder, windowed, 64-bit).
# Run from windows-connector/:  pyinstaller packaging/rr_mt5_connector.spec --noconfirm
import os
import sys
from PyInstaller.utils.hooks import collect_submodules

HERE = os.path.abspath(os.path.join(SPECPATH, ".."))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
CONNECTOR = os.path.join(ROOT, "mt5-connector")      # original connector package (reused, not copied)
WIN = sys.platform == "win32"

hidden = collect_submodules("rr_connector") + collect_submodules("connector") + collect_submodules("keyring.backends")
hidden += ["websockets.asyncio.client", "anyio._backends._asyncio", "psutil"]
if WIN:
    hidden += ["MetaTrader5", "win32api", "win32timezone", "win32ctypes.core"]

a = Analysis(
    [os.path.join(HERE, "run_connector.py")],
    pathex=[HERE, CONNECTOR],
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "pandas", "scipy", "IPython", "pytest",
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml", "PySide6.QtQuick",
              "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtPdf"],
    noarchive=False,
)
pyz = PYZ(a.pure)
icon = os.path.join(SPECPATH, "rr_connector.ico")
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="RR_MT5_Connector",
    console=False,                      # GUI application (no console window)
    icon=icon if os.path.exists(icon) else None,
    version=os.path.join(SPECPATH, "version_info.txt") if os.path.exists(os.path.join(SPECPATH, "version_info.txt")) else None,
    upx=False,                          # UPX-packed binaries trigger antivirus false positives
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="RR_MT5_Connector")
