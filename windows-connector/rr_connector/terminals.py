"""MetaTrader 5 terminal auto-detection.

Sources, merged and de-duplicated by install folder:
1. Uninstall registry entries (HKLM, HKLM\\WOW6432Node, HKCU) published by MetaQuotes / broker installers;
2. ``terminal64.exe`` in the common install roots (Program Files, Program Files (x86), %LOCALAPPDATA%\\Programs);
3. ``%APPDATA%\\MetaQuotes\\Terminal\\<id>\\origin.txt`` — every terminal that has run on this user account;
4. running ``terminal64.exe`` processes (gives the running status and catches portable installs).

Every source is injectable so the logic is unit-tested on any OS.
"""
from __future__ import annotations

import ntpath  # Windows path semantics on every OS (deterministic tests)
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

EXE = "terminal64.exe"
UNINSTALL_KEYS = [
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]


@dataclass
class Terminal:
    folder: str
    exe: str
    name: str = "MetaTrader 5"
    version: str = ""
    running: bool = False
    pid: int | None = None
    sources: list[str] = field(default_factory=list)
    # filled after attaching (connect step)
    account_login: int | None = None
    account_server: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _norm(p: str) -> str:
    return ntpath.normcase(ntpath.normpath(p.strip().strip('"')))


# ---- source readers (Windows implementations) --------------------------------------------------
def registry_entries() -> list[dict]:
    if sys.platform != "win32":
        return []
    import winreg
    hives = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
    out = []
    for hive, path in UNINSTALL_KEYS:
        try:
            root = winreg.OpenKey(hives[hive], path)
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, i)) as k:
                        vals = {}
                        for name in ("DisplayName", "Publisher", "InstallLocation", "DisplayVersion", "DisplayIcon"):
                            try:
                                vals[name] = str(winreg.QueryValueEx(k, name)[0])
                            except OSError:
                                pass
                        out.append(vals)
                except OSError:
                    continue
    return out


def common_roots() -> list[Path]:
    roots = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        if os.environ.get(var):
            roots.append(Path(os.environ[var]))
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "Programs")
    return roots


def appdata_origins() -> list[str]:
    base = Path(os.environ.get("APPDATA", "")) / "MetaQuotes" / "Terminal"
    out = []
    if base.is_dir():
        for d in base.iterdir():
            f = d / "origin.txt"
            if f.is_file():
                raw = f.read_bytes()
                for enc in ("utf-16", "utf-8-sig", "mbcs" if sys.platform == "win32" else "latin-1"):
                    try:
                        out.append(raw.decode(enc).strip())
                        break
                    except (UnicodeDecodeError, LookupError):
                        continue
    return out


def running_processes() -> list[dict]:
    try:
        import psutil
    except ImportError:
        return []
    out = []
    for p in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if (p.info.get("name") or "").lower() == EXE and p.info.get("exe"):
                out.append({"pid": p.info["pid"], "exe": p.info["exe"]})
        except Exception:  # noqa: BLE001 - access denied / vanished
            continue
    return out


def file_version(exe: str) -> str:
    if sys.platform != "win32":
        return ""
    try:
        import win32api  # pywin32
        info = win32api.GetFileVersionInfo(exe, "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:  # noqa: BLE001
        return ""


# ---- detection ------------------------------------------------------------------------------------
def find_terminals(registry: Callable[[], list[dict]] = registry_entries,
                   roots: Callable[[], Iterable[Path]] = common_roots,
                   origins: Callable[[], list[str]] = appdata_origins,
                   processes: Callable[[], list[dict]] = running_processes,
                   exists: Callable[[str], bool] = os.path.isfile,
                   version_of: Callable[[str], str] = file_version) -> list[Terminal]:
    found: dict[str, Terminal] = {}

    def add(folder: str, source: str, name: str = "", version: str = "") -> Terminal | None:
        if not folder:
            return None
        exe = ntpath.join(folder.strip().strip('"'), EXE)
        if not exists(exe):
            return None
        key = _norm(folder)
        t = found.get(key)
        if t is None:
            t = found[key] = Terminal(folder=ntpath.normpath(folder.strip().strip('"')), exe=ntpath.normpath(exe))
        if source not in t.sources:
            t.sources.append(source)
        if name and t.name == "MetaTrader 5":
            t.name = name
        if version and not t.version:
            t.version = version
        return t

    for e in registry():
        label = f"{e.get('DisplayName', '')} {e.get('Publisher', '')}"
        if "metatrader 5" not in label.lower() and "metaquotes" not in label.lower():
            continue
        folder = e.get("InstallLocation") or ""
        if not folder and e.get("DisplayIcon"):
            folder = ntpath.dirname(e["DisplayIcon"].split(",")[0].strip('"'))
        add(folder, "registry", e.get("DisplayName", ""), e.get("DisplayVersion", ""))
    for root in roots():
        try:
            for child in Path(root).iterdir():
                add(str(child), "folder", child.name)
        except OSError:
            continue
    for folder in origins():
        add(folder, "appdata")
    for p in processes():
        t = add(ntpath.dirname(p["exe"]), "process")
        if t:
            t.running, t.pid = True, p["pid"]
    for t in found.values():
        t.version = version_of(t.exe) or t.version
    # running terminals first, then most sources (most certain), then name
    return sorted(found.values(), key=lambda t: (not t.running, -len(t.sources), t.name.lower()))
