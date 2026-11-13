"""Setup wizard (engine link), live-trading authorisation, settings and log viewer."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from rr_connector import APP_NAME, __version__, paths, startup
from rr_connector.manager import LIVE_PHRASE, ConnectorManager
from rr_connector.ui import theme


class _Result(QObject):
    done = Signal(object, object)       # (result, error)


def run_bg(fn, on_done) -> None:
    """Run ``fn`` on a worker thread and deliver (result, error) on the GUI thread."""
    sig = _Result()
    sig.done.connect(on_done, Qt.QueuedConnection)
    sig._keep = sig                                       # keep alive until delivered

    def work():
        try:
            sig.done.emit(fn(), None)
        except Exception as exc:  # noqa: BLE001
            sig.done.emit(None, exc)
    threading.Thread(target=work, daemon=True).start()


def heading(text: str, sub: str = "") -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 8)
    t = QLabel(text)
    t.setObjectName("title")
    lay.addWidget(t)
    if sub:
        s = QLabel(sub)
        s.setObjectName("subtitle")
        s.setWordWrap(True)
        lay.addWidget(s)
    return w


class SetupWizard(QDialog):
    """One-time link of this PC to the RR AI Engine (URL + registration token)."""

    def __init__(self, manager: ConnectorManager, parent=None):
        super().__init__(parent)
        self.m = manager
        self.setWindowTitle(f"{APP_NAME} — Link to RR AI Engine")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.addWidget(heading("LINK TO RR AI ENGINE", "One-time setup. In the RR AI Engine open MT5 Terminal > "
                                                      "RR MT5 Connector agents > Create registration token, then paste it here."))
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.url = QLineEdit(manager.store.get("engine_url") or "")
        self.url.setPlaceholderText("https://your-rr-engine.example.com")
        self.token = QLineEdit()
        self.token.setPlaceholderText("rrmt5_…")
        self.token.setEchoMode(QLineEdit.Password)
        self.ca = QLineEdit(manager.store.get("ca_file") or "")
        self.ca.setPlaceholderText("Only for a private/self-signed certificate (optional)")
        pick = QPushButton("Browse…")
        pick.clicked.connect(self._pick_ca)
        row = QHBoxLayout()
        row.addWidget(self.ca)
        row.addWidget(pick)
        form.addRow("RR AI Engine address", self.url)
        form.addRow("Registration token", self.token)
        form.addRow("CA certificate", row)
        lay.addLayout(form)
        self.msg = QLabel("")
        self.msg.setObjectName("message")
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)
        self.btn = QPushButton("LINK THIS PC")
        self.btn.setObjectName("primary")
        self.btn.clicked.connect(self._register)
        lay.addWidget(self.btn)

    def _pick_ca(self):
        f, _ = QFileDialog.getOpenFileName(self, "CA certificate", "", "Certificates (*.pem *.crt *.cer)")
        if f:
            self.ca.setText(f)

    def _register(self):
        self.m.store.set("ca_file", self.ca.text().strip())
        self.btn.setEnabled(False)
        self.msg.setText("Contacting the RR AI Engine…")
        run_bg(lambda: self.m.register(self.url.text(), self.token.text()), self._done)

    def _done(self, res, err):
        self.btn.setEnabled(True)
        if err:
            self.msg.setText(f"<span style='color:{theme.RED}'>{err}</span>")
            return
        self.token.clear()
        QMessageBox.information(self, APP_NAME, f"This PC is linked to the RR AI Engine as “{res.get('name')}”.\n"
                                                "Settings were saved securely; you will not be asked again.")
        self.accept()


class LiveDialog(QDialog):
    """Explicit, typed authorisation before a LIVE account can trade from this PC."""

    def __init__(self, manager: ConnectorManager, parent=None):
        super().__init__(parent)
        self.m = manager
        acct = manager.snapshot.get("account") or {}
        self.setWindowTitle("Enable LIVE trading")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 22)
        warn = QLabel(f"<div style='font-size:18px;font-weight:700;color:{theme.RED}'>LIVE ACCOUNT — REAL MONEY</div>"
                      f"<div style='color:{theme.TEXT};margin-top:8px'>Account <b>{acct.get('login')}</b> on "
                      f"<b>{acct.get('server')}</b> ({acct.get('company') or ''}).<br><br>"
                      "Authorising lets the RR AI Engine send orders to this live account from this PC. "
                      "The RR AI Engine's risk limits and live approval still apply, and this PC enforces its own "
                      "maximum lot size, maximum open positions and maximum daily loss. You can revoke at any time, "
                      "and changing account in MetaTrader 5 revokes it automatically.</div>")
        warn.setWordWrap(True)
        lay.addWidget(warn)
        form = QFormLayout()
        self.login = QLineEdit()
        self.login.setPlaceholderText("type the account number")
        self.phrase = QLineEdit()
        self.phrase.setPlaceholderText(LIVE_PHRASE)
        self.ack = QCheckBox("I understand that real money is at risk.")
        form.addRow("Account number", self.login)
        form.addRow(f"Type {LIVE_PHRASE}", self.phrase)
        lay.addLayout(form)
        lay.addWidget(self.ack)
        self.msg = QLabel("")
        self.msg.setObjectName("warn")
        lay.addWidget(self.msg)
        row = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.ok = QPushButton("AUTHORISE LIVE TRADING")
        self.ok.setObjectName("danger")
        self.ok.setEnabled(False)
        self.ok.clicked.connect(self._authorise)
        row.addWidget(cancel)
        row.addStretch()
        row.addWidget(self.ok)
        lay.addLayout(row)
        for w in (self.login, self.phrase):
            w.textChanged.connect(self._validate)
        self.ack.toggled.connect(self._validate)

    def _validate(self):
        self.ok.setEnabled(bool(self.login.text().strip()) and self.phrase.text().strip() == LIVE_PHRASE
                           and self.ack.isChecked())

    def _authorise(self):
        try:
            self.m.authorize_live(self.login.text(), self.phrase.text())
        except ValueError as exc:
            self.msg.setText(str(exc))
            return
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, manager: ConnectorManager, parent=None, on_relink=None, on_update=None):
        super().__init__(parent)
        self.m = manager
        self.setWindowTitle(f"{APP_NAME} — Settings")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.addWidget(heading("SETTINGS", f"Version {__version__} · data folder {paths.data_dir()}"))
        self.autostart = QCheckBox("Start automatically when I sign in to Windows")
        self.autostart.setChecked(startup.is_enabled() if sys.platform == "win32" else bool(manager.store.get("autostart")))
        self.minimized = QCheckBox("Start minimised to the system tray")
        self.minimized.setChecked(bool(manager.store.get("start_minimized")))
        lay.addWidget(self.autostart)
        lay.addWidget(self.minimized)
        sec = QLabel("PC SAFETY LIMITS (backstop to the RR AI Engine risk engine)")
        sec.setObjectName("section")
        lay.addWidget(sec)
        g = manager.store.get("guard") or {}
        form = QFormLayout()
        self.max_lot = QDoubleSpinBox(decimals=2, minimum=0.01, maximum=100, singleStep=0.1)
        self.max_lot.setValue(float(g.get("max_lot", 1.0)))
        self.max_pos = QSpinBox(minimum=1, maximum=500)
        self.max_pos.setValue(int(g.get("max_open_positions", 10)))
        self.max_loss = QDoubleSpinBox(decimals=1, minimum=0.1, maximum=100, singleStep=0.5, suffix=" %")
        self.max_loss.setValue(float(g.get("max_daily_loss_pct", 5.0)))
        form.addRow("Maximum lot per order", self.max_lot)
        form.addRow("Maximum open positions", self.max_pos)
        form.addRow("Maximum daily loss", self.max_loss)
        lay.addLayout(form)
        row = QHBoxLayout()
        relink = QPushButton("Change RR AI Engine link…")
        relink.clicked.connect(lambda: (self.accept(), on_relink and on_relink()))
        upd = QPushButton("Check for updates")
        upd.clicked.connect(lambda: on_update and on_update())
        row.addWidget(relink)
        row.addWidget(upd)
        lay.addLayout(row)
        save = QPushButton("SAVE")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        lay.addWidget(save)

    def _save(self):
        try:
            self.m.set_limits(self.max_lot.value(), self.max_pos.value(), self.max_loss.value())
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self.m.store.update(autostart=self.autostart.isChecked(), start_minimized=self.minimized.isChecked())
        startup.set_enabled(self.autostart.isChecked())
        self.accept()


class LogsDialog(QDialog):
    def __init__(self, manager: ConnectorManager, parent=None):
        super().__init__(parent)
        self.m = manager
        self.setWindowTitle(f"{APP_NAME} — Logs")
        self.resize(900, 520)
        lay = QVBoxLayout(self)
        lay.addWidget(heading("CONNECTION & AUDIT LOG", f"Full log files: {paths.logs_dir()}"))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Time", "Level", "Category", "Message"])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        ref = QPushButton("Refresh")
        ref.clicked.connect(self.refresh)
        folder = QPushButton("Open log folder")
        folder.clicked.connect(self._open_folder)
        row.addWidget(ref)
        row.addWidget(folder)
        row.addStretch()
        lay.addLayout(row)
        self.refresh()

    def refresh(self):
        rows = self.m.store.events(500)
        self.table.setRowCount(len(rows))
        colors = {"error": theme.RED, "warning": theme.AMBER}
        for i, e in enumerate(rows):
            vals = [datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d %H:%M:%S"), e["level"].upper(), e["category"],
                    e["message"]]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if e["level"] in colors and j == 1:
                    it.setForeground(Qt.GlobalColor.white)
                    it.setBackground(QColor(colors[e["level"]]))
                self.table.setItem(i, j, it)
        self.table.resizeColumnsToContents()

    def _open_folder(self):
        d = str(paths.logs_dir())
        if sys.platform == "win32":
            os.startfile(d)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", d])
