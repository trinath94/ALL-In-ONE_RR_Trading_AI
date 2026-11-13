"""Main dashboard: two-field connection panel + live status cards. Closing hides to the tray."""
from __future__ import annotations

import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from rr_connector import APP_NAME, __version__
from rr_connector.manager import ConnectorManager
from rr_connector.mt5link import ConnectError
from rr_connector.ui import theme
from rr_connector.ui.dialogs import LiveDialog, LogsDialog, SettingsDialog, SetupWizard, run_bg

ENGINE_TEXT = {"CONNECTED": "Connected", "CONNECTING": "Connecting…", "DISCONNECTED": "Reconnecting…",
               "REJECTED": "Rejected — re-link this PC", "NOT_CONFIGURED": "Not linked — run setup", "STOPPED": "Stopped"}
MT5_TEXT = {"CONNECTED": "Connected", "CONNECTING": "Connecting…", "RECONNECTING": "Reconnecting…",
            "DISCONNECTED": "Disconnected", "ERROR": "Not connected", "CHOOSE": "Choose a terminal"}


class Bridge(QObject):
    changed = Signal(dict)


def money(v, cur="") -> str:
    return "—" if v is None else f"{v:,.2f} {cur}".strip()


class Card(QFrame):
    def __init__(self, label: str):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(4)
        lab = QLabel(label.upper())
        lab.setObjectName("cardLabel")
        self.value = QLabel("—")
        self.value.setObjectName("cardValue")
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(lab)
        lay.addWidget(self.value)

    def set(self, text: str, color: str | None = None) -> None:
        self.value.setText(text)
        self.value.setStyleSheet(f"color: {color};" if color else "")


class Pill(QLabel):
    def set(self, name: str, state: str, text: str) -> None:
        c = theme.STATE_COLOR.get(state, theme.DIM)
        self.setText(f"<span style='color:{c};font-size:15px'>●</span>&nbsp;<b>{name}</b>&nbsp;"
                     f"<span style='color:{theme.DIM}'>{text}</span>")


class MainWindow(QMainWindow):
    def __init__(self, manager: ConnectorManager, tray_notify=None):
        super().__init__()
        self.m = manager
        self.notify = tray_notify or (lambda title, msg, error=False: None)
        self.bridge = Bridge()
        self.bridge.changed.connect(self.render, Qt.QueuedConnection)
        self._last = {"mt5": None, "engine": None}
        self._hide_hint = False
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(theme.app_icon())
        self.resize(1180, 760)
        self.setMinimumSize(980, 640)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 18, 22, 16)
        outer.setSpacing(14)

        # header
        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(theme.app_icon().pixmap(46, 46))
        titles = QVBoxLayout()
        t = QLabel("RR MT5 CONNECTOR")
        t.setObjectName("title")
        st = QLabel("MetaTrader 5 ⇄ RR AI Trading Engine")
        st.setObjectName("subtitle")
        titles.addWidget(t)
        titles.addWidget(st)
        head.addWidget(logo)
        head.addLayout(titles)
        head.addStretch()
        self.mode_badge = QLabel("")
        self.mode_badge.setAlignment(Qt.AlignCenter)
        self.mode_badge.setMinimumWidth(190)
        head.addWidget(self.mode_badge)
        head.addSpacing(10)
        pills = QVBoxLayout()
        self.pill_mt5, self.pill_engine = Pill(), Pill()
        pills.addWidget(self.pill_mt5)
        pills.addWidget(self.pill_engine)
        head.addLayout(pills)
        outer.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(16)
        outer.addLayout(body, 1)

        # connection panel
        panel = QFrame()
        panel.setObjectName("panel")
        panel.setFixedWidth(360)
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(22, 20, 22, 20)
        pl.setSpacing(10)
        sec = QLabel("MT5 BROKER SERVER")
        sec.setObjectName("section")
        pl.addWidget(sec)
        pl.addWidget(QLabel("Server Address"))
        self.address = QLineEdit(manager.store.get("mt5_address") or "")
        self.address.setPlaceholderText("Enter MT5 Server Address")
        pl.addWidget(self.address)
        pl.addWidget(QLabel("Server Port"))
        self.port = QLineEdit(str(manager.store.get("mt5_port") or 443))
        self.port.setPlaceholderText("Enter MT5 Server Port")
        pl.addWidget(self.port)
        self.terminal_pick = QComboBox()
        self.terminal_pick.setVisible(False)
        pl.addWidget(self.terminal_pick)
        self.connect_btn = QPushButton("CONNECT")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.setCursor(Qt.PointingHandCursor)
        self.connect_btn.clicked.connect(self.on_connect)
        pl.addSpacing(6)
        pl.addWidget(self.connect_btn)
        self.message = QLabel("Log in to your account in MetaTrader 5, enter the broker server and click CONNECT. "
                              "Your trading password is never requested.")
        self.message.setObjectName("message")
        self.message.setWordWrap(True)
        pl.addWidget(self.message)
        self.warnings = QLabel("")
        self.warnings.setObjectName("warn")
        self.warnings.setWordWrap(True)
        pl.addWidget(self.warnings)
        pl.addStretch()
        self.live_btn = QPushButton("Enable live trading…")
        self.live_btn.setObjectName("live")
        self.live_btn.clicked.connect(self.on_live)
        self.live_btn.setVisible(False)
        pl.addWidget(self.live_btn)
        self.stop_btn = QPushButton("■  EMERGENCY STOP")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.clicked.connect(self.on_emergency)
        pl.addWidget(self.stop_btn)
        row = QHBoxLayout()
        for text, fn in (("Logs", self.show_logs), ("Settings", self.show_settings)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        pl.addLayout(row)
        body.addWidget(panel)

        # status cards
        right = QVBoxLayout()
        grid = QGridLayout()
        grid.setSpacing(12)
        names = ["MT5 Connection Status", "Broker Name", "Trading Account Number", "MT5 Server Name", "Account Currency",
                 "Balance", "Equity", "Free Margin", "Leverage", "Demo / Live Account", "MT5 Terminal Status",
                 "RR AI Engine Status", "Last Heartbeat", "Connection Latency", "Server Validation"]
        self.cards: dict[str, Card] = {}
        for i, n in enumerate(names):
            c = Card(n)
            c.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self.cards[n] = c
            grid.addWidget(c, i // 3, i % 3)
        right.addLayout(grid)
        act = QFrame()
        act.setObjectName("card")
        al = QVBoxLayout(act)
        al.setContentsMargins(14, 10, 14, 10)
        lab = QLabel("RECENT ACTIVITY")
        lab.setObjectName("cardLabel")
        al.addWidget(lab)
        self.activity = QLabel("")
        self.activity.setObjectName("message")
        self.activity.setTextFormat(Qt.RichText)
        self.activity.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.activity.setWordWrap(True)
        al.addWidget(self.activity, 1)
        right.addWidget(act, 1)
        foot = QLabel(f"v{__version__} · outbound-only connection · trading decisions and risk limits stay in the RR AI Engine")
        foot.setObjectName("subtitle")
        right.addWidget(foot)
        body.addLayout(right, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.render(dict(self.m.snapshot)))
        self.timer.start(1000)
        self.render(dict(self.m.snapshot))

    # ---- actions -------------------------------------------------------------------------------------
    def on_connect(self):
        if self.m.snapshot.get("mt5") == "CONNECTED":
            self.m.disconnect_mt5()
            return
        exe = self.terminal_pick.currentData() if self.terminal_pick.isVisible() else None
        self.connect_btn.setEnabled(False)
        self.message.setText("Connecting to MetaTrader 5…")
        run_bg(lambda: self.m.connect_mt5(self.address.text(), self.port.text(), exe), self._connected)

    def _connected(self, att, err):
        self.connect_btn.setEnabled(True)
        if isinstance(err, ConnectError) and err.code == "CHOOSE_TERMINAL":
            self.terminal_pick.clear()
            for t in err.choices:
                self.terminal_pick.addItem(f"{t.name} — {t.account_login} @ {t.account_server}", t.exe)
            self.terminal_pick.setVisible(True)
            self.message.setText(str(err))
        elif err:
            self.message.setText(f"<span style='color:{theme.RED}'>{err}</span>")
        else:
            self.terminal_pick.setVisible(False)
            self.message.setText(att.server_check.get("detail", "Connected"))

    def on_live(self):
        if self.m.snapshot.get("live_authorized"):
            if QMessageBox.question(self, APP_NAME, "Revoke live trading authorisation on this PC?") == QMessageBox.Yes:
                self.m.revoke_live()
            return
        LiveDialog(self.m, self).exec()

    def on_emergency(self):
        if self.m.snapshot.get("local_kill_switch"):
            if QMessageBox.question(self, APP_NAME, "Release the local emergency stop on this PC?\n(The RR AI Engine "
                                                    "kill switch must be reset in the engine itself.)") == QMessageBox.Yes:
                self.m.release_emergency_stop()
            return
        if QMessageBox.warning(self, APP_NAME, "EMERGENCY STOP\n\nNew positions will be refused on this PC and the RR AI "
                                               "Engine kill switch will be activated. Continue?",
                               QMessageBox.Yes | QMessageBox.Cancel) == QMessageBox.Yes:
            self.m.emergency_stop("Emergency stop pressed on the PC")

    def show_logs(self):
        LogsDialog(self.m, self).exec()

    def show_settings(self):
        SettingsDialog(self.m, self, on_relink=self.show_setup, on_update=self.check_updates).exec()

    def show_setup(self):
        SetupWizard(self.m, self).exec()

    def check_updates(self):
        from rr_connector import updater

        def done(info, err):
            if err:
                QMessageBox.warning(self, APP_NAME, f"Update check failed: {err}")
            elif not info.get("available"):
                QMessageBox.information(self, APP_NAME, f"You have the latest version ({__version__}).")
            elif QMessageBox.question(self, APP_NAME, f"Version {info['version']} is available. Download, verify and "
                                                      "install it now?") == QMessageBox.Yes:
                run_bg(lambda: updater.download_and_verify(info, self.m.store), self._install)
        run_bg(lambda: updater.check(self.m.store, self.m.vault, self.m.identity), done)

    def _install(self, path, err):
        from PySide6.QtWidgets import QApplication

        from rr_connector import updater
        if err:
            QMessageBox.warning(self, APP_NAME, f"Update refused: {err}")
            return
        updater.run_installer(path)
        QApplication.instance().quit()

    # ---- rendering -----------------------------------------------------------------------------------
    def render(self, s: dict):
        mt5, eng = s.get("mt5"), s.get("engine")
        acct, term = s.get("account") or {}, s.get("terminal") or {}
        cur = acct.get("currency") or ""
        self.pill_mt5.set("MT5", mt5, MT5_TEXT.get(mt5, mt5 or ""))
        self.pill_engine.set("RR AI ENGINE", eng, ENGINE_TEXT.get(eng, eng or ""))
        mode = s.get("mode")
        if mode == "DEMO":
            self.mode_badge.setText("DEMO ACCOUNT")
            self.mode_badge.setStyleSheet(f"background:#0b2a5c;border:1px solid {theme.BLUE};border-radius:8px;"
                                          f"padding:8px 14px;font-weight:800;letter-spacing:2px;color:{theme.CYAN}")
        elif mode:
            auth = "AUTHORISED" if s.get("live_authorized") else "TRADING LOCKED"
            self.mode_badge.setText(f"LIVE ACCOUNT · {auth}")
            self.mode_badge.setStyleSheet(f"background:#3a0d16;border:2px solid {theme.RED};border-radius:8px;"
                                          f"padding:8px 14px;font-weight:800;letter-spacing:2px;color:#ffd5da")
        else:
            self.mode_badge.setText("")
            self.mode_badge.setStyleSheet("")
        c = self.cards
        c["MT5 Connection Status"].set(MT5_TEXT.get(mt5, mt5 or "—"), theme.STATE_COLOR.get(mt5))
        c["Broker Name"].set(acct.get("company") or "—")
        c["Trading Account Number"].set(str(acct.get("login") or "—"))
        c["MT5 Server Name"].set(acct.get("server") or "—")
        c["Account Currency"].set(cur or "—")
        c["Balance"].set(money(acct.get("balance"), cur))
        c["Equity"].set(money(acct.get("equity"), cur))
        c["Free Margin"].set(money(acct.get("margin_free"), cur))
        c["Leverage"].set(f"1:{acct['leverage']}" if acct.get("leverage") else "—")
        c["Demo / Live Account"].set(mode or "—", theme.CYAN if mode == "DEMO" else theme.RED if mode else None)
        if term:
            ts = "Online" if term.get("connected") else "Offline (no broker connection)"
            if term.get("connected") and not term.get("trade_allowed"):
                ts = "Online · Algo Trading OFF"
            c["MT5 Terminal Status"].set(ts, theme.GREEN if term.get("connected") and term.get("trade_allowed") else theme.AMBER)
        else:
            c["MT5 Terminal Status"].set("—")
        c["RR AI Engine Status"].set(ENGINE_TEXT.get(eng, eng or "—"), theme.STATE_COLOR.get(eng))
        hb = s.get("last_heartbeat")
        c["Last Heartbeat"].set(f"{int(time.time() - hb)} s ago" if hb else "—",
                                theme.GREEN if hb and time.time() - hb < 20 else theme.AMBER if hb else None)
        ping = round(term["ping_last"] / 1000) if term.get("ping_last") else None
        rtt = round(s["engine_rtt_ms"]) if s.get("engine_rtt_ms") is not None else None
        c["Connection Latency"].set(f"Broker {ping if ping is not None else '—'} ms · Engine {rtt if rtt is not None else '—'} ms")
        self._render_activity()
        chk = s.get("server_check") or {}
        c["Server Validation"].set(chk.get("result") or "—", theme.GREEN if chk.get("result") in ("MATCH", "REACHABLE") else None)
        self.connect_btn.setText("DISCONNECT" if mt5 == "CONNECTED" else "CONNECT")
        self.live_btn.setVisible(bool(mode) and mode != "DEMO")
        self.live_btn.setText("Revoke live trading" if s.get("live_authorized") else "Enable live trading…")
        self.stop_btn.setText("Release emergency stop" if s.get("local_kill_switch") else "■  EMERGENCY STOP")
        self.warnings.setText("\n".join(f"⚠ {w}" for w in (s.get("warnings") or [])) if mt5 == "CONNECTED" else "")
        if mt5 not in ("CONNECTED", "CONNECTING") and s.get("mt5_detail") and self.connect_btn.isEnabled():
            self.message.setText(s["mt5_detail"])
        # notifications on transitions
        for key, name, texts in (("mt5", "MetaTrader 5", MT5_TEXT), ("engine", "RR AI Engine", ENGINE_TEXT)):
            prev, now = self._last[key], s.get(key)
            if prev is not None and prev != now and now in ("CONNECTED", "DISCONNECTED", "RECONNECTING", "REJECTED", "ERROR"):
                self.notify(f"{name}: {texts.get(now, now)}", s.get(f"{key}_detail") or "", now != "CONNECTED")
            self._last[key] = now

    def _render_activity(self):
        colors = {"error": theme.RED, "warning": theme.AMBER}
        rows = []
        for e in self.m.store.events(9):
            c = colors.get(e["level"], theme.DIM)
            ts = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
            msg = e["message"].replace("<", "&lt;")
            rows.append(f"<div style='margin:2px 0'><span style='color:{theme.DIM}'>{ts}</span>&nbsp;&nbsp;"
                        f"<span style='color:{c}'>{e['category'].upper()}</span>&nbsp;&nbsp;{msg}</div>")
        self.activity.setText("".join(rows) or "No activity yet")

    def closeEvent(self, event):                       # keep running in the tray
        event.ignore()
        self.hide()
        if not self._hide_hint:
            self._hide_hint = True
            self.notify(APP_NAME, "Still running in the system tray. Synchronisation continues.", False)
