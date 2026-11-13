"""Launch at Windows sign-in (per user, HKCU Run key — no administrator rights needed)."""
from __future__ import annotations

import sys

from rr_connector import APP_NAME, paths

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def command() -> str:
    return f"{paths.executable()} --minimized"


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return bool(winreg.QueryValueEx(k, APP_NAME)[0])
    except OSError:
        return False


def set_enabled(enabled: bool) -> bool:
    """Returns True when the setting was applied (always False off Windows)."""
    if sys.platform != "win32":
        return False
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass
    return True
