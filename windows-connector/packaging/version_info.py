"""Write the Windows version resource (file properties) for PyInstaller from rr_connector.__version__."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from rr_connector import APP_NAME, __version__  # noqa: E402

v = tuple(int(x) for x in __version__.split(".")) + (0,) * (4 - len(__version__.split(".")))
(HERE / "version_info.txt").write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'RR Trading'),
    StringStruct('FileDescription', '{APP_NAME}'),
    StringStruct('FileVersion', '{__version__}'),
    StringStruct('InternalName', 'RR_MT5_Connector'),
    StringStruct('OriginalFilename', 'RR_MT5_Connector.exe'),
    StringStruct('ProductName', '{APP_NAME}'),
    StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""", encoding="utf-8")
print(__version__)
