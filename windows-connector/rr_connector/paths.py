"""Filesystem locations.

Installed program:  C:\\Program Files\\RR MT5 Connector            (installer)
Configuration:      %LOCALAPPDATA%\\RRMT5Connector\\config.db       (encrypted values)
Execution journal:  %LOCALAPPDATA%\\RRMT5Connector\\journal.sqlite3
Logs:               %LOCALAPPDATA%\\RRMT5Connector\\logs\\
``RRMT5_HOME`` overrides the data directory (tests, portable use).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from rr_connector import APP_ID


def data_dir() -> Path:
    if os.environ.get("RRMT5_HOME"):
        base = Path(os.environ["RRMT5_HOME"])
    elif os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"]) / APP_ID
    else:
        base = Path.home() / f".{APP_ID.lower()}"
    (base / "logs").mkdir(parents=True, exist_ok=True)
    return base


def logs_dir() -> Path:
    return data_dir() / "logs"


def config_db() -> Path:
    return data_dir() / "config.db"


def journal_path() -> Path:
    return data_dir() / "journal.sqlite3"


def ensure_connector_package() -> None:
    """Make the original connector package (mt5-connector/connector) importable when running from source.
    In the frozen EXE it is bundled by PyInstaller."""
    if getattr(sys, "frozen", False):
        return
    here = Path(__file__).resolve().parent.parent.parent / "mt5-connector"
    if here.is_dir() and str(here) not in sys.path:
        sys.path.insert(0, str(here))


def executable() -> str:
    """Command used for auto-start: the frozen EXE, or ``pythonw -m rr_connector`` from source."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else py}" -m rr_connector'
