"""Application entry point.

    RR_MT5_Connector.exe                       open the dashboard (first run shows the setup wizard)
    RR_MT5_Connector.exe --minimized           start in the tray (used by auto-start)
    RR_MT5_Connector.exe --register URL TOKEN  one-time link to the RR AI Engine (used by the installer)
    RR_MT5_Connector.exe --autostart on|off    set Windows sign-in auto-start (used by the installer)
    RR_MT5_Connector.exe --forget              remove credentials and local data (used by the uninstaller)
    RR_MT5_Connector.exe --selftest FILE       verify the packaged build on this PC, write a JSON report
    RR_MT5_Connector.exe --version
"""
from __future__ import annotations

import argparse
import logging
import sys

from rr_connector import APP_NAME, __version__, logsetup, paths, startup
from rr_connector.store import Store
from rr_connector.vault import Vault

log = logging.getLogger("rr_connector")


class UnavailableAdapter:
    """Used when the MetaTrader5 package cannot load (non-Windows or broken install): CONNECT reports it clearly."""

    def __init__(self, reason: str):
        self.reason = reason

    def attach(self, *a, **k):
        return False

    def shutdown(self):
        pass

    def account_info(self):
        return {}

    def terminal_info(self):
        return {}

    def positions(self):
        return []


def make_adapter():
    paths.ensure_connector_package()
    try:
        from connector.adapter import MT5Adapter
        return MT5Adapter()
    except Exception as exc:  # noqa: BLE001 - ImportError off Windows, DLL errors on broken installs
        log.error("MetaTrader5 package unavailable: %s", exc)
        return UnavailableAdapter(str(exc))


def parse(argv):
    p = argparse.ArgumentParser(prog="RR_MT5_Connector", description=APP_NAME)
    p.add_argument("--minimized", action="store_true")
    p.add_argument("--register", nargs=2, metavar=("ENGINE_URL", "TOKEN"))
    p.add_argument("--autostart", choices=["on", "off"])
    p.add_argument("--forget", action="store_true")
    p.add_argument("--selftest", metavar="REPORT_FILE")
    p.add_argument("--version", action="store_true")
    return p.parse_args(argv)


def forget() -> int:
    import shutil
    vault = Vault()
    for name in ("api-key", "bridge-secret", "device-key", "config-key"):
        vault.delete(name)
    startup.set_enabled(False)
    shutil.rmtree(paths.data_dir(), ignore_errors=True)
    return 0


def selftest(report_file: str) -> int:
    """Packaged-build check (CI on windows-latest and after installation): every dependency loads, the
    Credential Manager round-trips, the encrypted store works, terminal detection runs."""
    import json
    import os
    import platform
    import tempfile
    import traceback
    checks: dict = {"version": __version__, "platform": platform.platform(), "python": platform.python_version()}
    ok = True

    def step(name, fn):
        nonlocal ok
        try:
            checks[name] = fn() or "ok"
        except Exception:  # noqa: BLE001
            ok = False
            checks[name] = "FAIL: " + traceback.format_exc(limit=3)
    os.environ["RRMT5_HOME"] = tempfile.mkdtemp(prefix="rrmt5-selftest-")
    step("metatrader5_package", lambda: getattr(__import__("MetaTrader5"), "__version__", "imported"))
    step("pyside6", lambda: __import__("PySide6").__version__)
    step("original_connector", lambda: (paths.ensure_connector_package(), __import__("connector.service"))[1] and "imported")

    def vault_rt():
        v = Vault()
        v.set("selftest", "x1")
        got = v.get("selftest")
        v.delete("selftest")
        assert got == "x1", got
        return type(v.backend).__name__
    step("credential_manager", vault_rt)

    def store_rt():
        st = Store(paths.config_db(), Vault())
        st.set("engine_url", "https://selftest.invalid")
        assert st.get("engine_url") == "https://selftest.invalid"
        st.close()
        Vault().delete("config-key")
    step("encrypted_store", store_rt)
    step("terminal_detection", lambda: [t.to_dict() for t in __import__("rr_connector.terminals", fromlist=["x"]).find_terminals()])
    step("websockets", lambda: __import__("websockets").__version__)
    checks["ok"] = ok
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(checks, f, indent=2, default=str)
    return 0 if ok else 1


def main(argv=None) -> int:
    args = parse(sys.argv[1:] if argv is None else argv)
    if args.version:
        print(__version__)
        return 0
    if args.selftest:
        return selftest(args.selftest)
    if args.forget:
        return forget()
    logsetup.setup()
    vault = Vault()
    store = Store(paths.config_db(), vault)
    if args.autostart:
        store.set("autostart", args.autostart == "on")
        startup.set_enabled(args.autostart == "on")
        if not args.register:
            return 0

    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    from rr_connector.ui import theme
    app.setStyleSheet(theme.QSS)
    app.setWindowIcon(theme.app_icon())

    lock = QLockFile(str(paths.data_dir() / "instance.lock"))
    if not lock.tryLock(5000):
        if not args.register:
            QMessageBox.information(None, APP_NAME, "RR MT5 Connector is already running (see the system tray).")
        return 0

    from rr_connector.manager import ConnectorManager
    adapter = make_adapter()
    manager = ConnectorManager(store, vault, adapter)

    if args.register:
        try:
            d = manager.register(*args.register)
            log.info("registered as agent %s", d["agent_id"])
            return 0
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(None, APP_NAME, f"Could not link this PC to the RR AI Engine:\n{exc}\n\n"
                                                "You can finish the setup when the connector starts.")
            return 1

    from rr_connector.ui.dialogs import SetupWizard
    from rr_connector.ui.main_window import MainWindow
    from rr_connector.ui.tray import Tray, confirm_exit

    window = MainWindow(manager)
    manager.on_change = window.bridge.changed.emit

    def shutdown():
        manager.stop()
        tray.hide()
        lock.unlock()
        app.quit()

    def restart():
        from PySide6.QtCore import QProcess
        manager.stop()
        lock.unlock()
        exe, extra = (sys.executable, []) if getattr(sys, "frozen", False) else (sys.executable, ["-m", "rr_connector"])
        QProcess.startDetached(exe, extra)
        tray.hide()
        app.quit()

    tray = Tray(window, restart, lambda: confirm_exit(window) and shutdown())
    window.notify = tray.notify
    tray.show()
    if isinstance(adapter, UnavailableAdapter):
        store.event("error", "mt5", f"MetaTrader5 Python package unavailable: {adapter.reason}")
        window.message.setText("The MetaTrader5 integration could not be loaded on this PC. Reinstall RR MT5 Connector "
                               "(64-bit Windows 10/11 required).")
    manager.start()
    first_run = not store.get("agent_id")
    if not (args.minimized or store.get("start_minimized")) or first_run:
        window.show()
    if first_run:
        SetupWizard(manager, window).exec()
    rc = app.exec()
    manager.stop()
    # release Qt objects in a defined order before interpreter shutdown (avoids crashes in Qt's DLL unload)
    import shiboken6
    for w in QApplication.topLevelWidgets():
        w.deleteLater()
    app.processEvents()
    tray.deleteLater()
    app.processEvents()
    shiboken6.delete(app)
    return rc
