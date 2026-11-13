"""System tray icon and menu (the connector keeps running when the window is closed)."""
from __future__ import annotations

from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from rr_connector import APP_NAME
from rr_connector.ui import theme
from rr_connector.ui.main_window import ENGINE_TEXT, MT5_TEXT, MainWindow, money


class Tray(QSystemTrayIcon):
    def __init__(self, window: MainWindow, on_restart, on_exit):
        super().__init__(theme.app_icon(theme.AMBER))
        self.w, self.m = window, window.m
        self.setToolTip(APP_NAME)
        menu = QMenu()
        menu.addAction("Open Dashboard", self.open)
        menu.addSeparator()
        menu.addAction("Connect MT5", self.connect_mt5)
        menu.addAction("Disconnect MT5", self.m.disconnect_mt5)
        menu.addAction("RR AI Engine Status", self.engine_status)
        menu.addAction("Account Information", self.account_info)
        menu.addAction("View Logs", lambda: (self.open(), self.w.show_logs()))
        menu.addSeparator()
        menu.addAction("Restart Connector", on_restart)
        menu.addAction("Exit", on_exit)
        self.menu = menu
        self.setContextMenu(menu)
        self.activated.connect(lambda r: self.open() if r in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        window.bridge.changed.connect(self.update_icon)

    def open(self):
        self.w.showNormal()
        self.w.raise_()
        self.w.activateWindow()

    def connect_mt5(self):
        self.open()
        if self.m.snapshot.get("mt5") != "CONNECTED":
            self.w.on_connect()

    def engine_status(self):
        s = self.m.snapshot
        self.showMessage("RR AI Engine", f"{ENGINE_TEXT.get(s.get('engine'), s.get('engine'))}\n{s.get('engine_detail') or ''}",
                         QSystemTrayIcon.Information, 6000)

    def account_info(self):
        s = self.m.snapshot
        a = s.get("account") or {}
        if not a:
            self.showMessage("MetaTrader 5", MT5_TEXT.get(s.get("mt5"), "Not connected"), QSystemTrayIcon.Warning, 5000)
            return
        cur = a.get("currency") or ""
        self.showMessage(f"{s.get('mode')} {a.get('login')} @ {a.get('server')}",
                         f"Balance {money(a.get('balance'), cur)}\nEquity {money(a.get('equity'), cur)}\n"
                         f"Free margin {money(a.get('margin_free'), cur)} · 1:{a.get('leverage')}",
                         QSystemTrayIcon.Information, 8000)

    def update_icon(self, s: dict):
        ok = s.get("mt5") == "CONNECTED" and s.get("engine") == "CONNECTED"
        bad = s.get("mt5") in ("ERROR", "DISCONNECTED") or s.get("engine") in ("REJECTED", "DISCONNECTED")
        self.setIcon(theme.app_icon(theme.GREEN if ok else theme.RED if bad else theme.AMBER))
        a = s.get("account") or {}
        self.setToolTip(f"{APP_NAME}\nMT5: {MT5_TEXT.get(s.get('mt5'), s.get('mt5'))}"
                        + (f" ({s.get('mode')} {a.get('login')})" if a else "")
                        + f"\nRR AI Engine: {ENGINE_TEXT.get(s.get('engine'), s.get('engine'))}")

    def notify(self, title: str, msg: str, error: bool = False):
        self.showMessage(title, msg or " ", QSystemTrayIcon.Warning if error else QSystemTrayIcon.Information, 5000)


def confirm_exit(parent) -> bool:
    return QMessageBox.question(parent, APP_NAME, "Exit RR MT5 Connector?\n\nThe RR AI Engine will lose its connection to "
                                                  "MetaTrader 5 until the connector is started again.") == QMessageBox.Yes


_ = QApplication
