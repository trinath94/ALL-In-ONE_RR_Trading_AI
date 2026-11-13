"""Headless UI tests (Qt offscreen): the dashboard renders real manager state; dialogs enforce their rules."""
import pytest
from PySide6.QtWidgets import QApplication

from connector.adapter import MT5Adapter
from fake_mt5 import FakeMT5
from rr_connector.manager import ConnectorManager
from rr_connector.terminals import Terminal


@pytest.fixture(scope="module")
def qapp():
    from rr_connector.ui import theme
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.QSS)
    return app


@pytest.fixture
def manager(store, vault, home):
    fake = FakeMT5()
    m = ConnectorManager(store, vault, MT5Adapter(fake), finder=lambda: [Terminal("C:/MT5", "C:/MT5/terminal64.exe", running=True)],
                         prober=lambda a, p: {"reachable": True, "latency_ms": 5}, journal_path=str(home / "j.db"))
    m.fake = fake
    return m


def test_dashboard_shows_two_fields_and_renders_connected_account(qapp, manager):
    from rr_connector.ui.main_window import MainWindow
    w = MainWindow(manager)
    assert w.address.placeholderText() == "Enter MT5 Server Address" and w.port.text() == "443"
    manager.on_change = w.bridge.changed.emit
    manager.connect_mt5("Demo-Server", 443)
    w.render(dict(manager.snapshot))
    assert w.cards["Trading Account Number"].value.text() == "1001"
    assert w.cards["Demo / Live Account"].value.text() == "DEMO" and w.mode_badge.text() == "DEMO ACCOUNT"
    assert w.cards["MT5 Connection Status"].value.text() == "Connected" and w.connect_btn.text() == "DISCONNECT"
    assert "USD" in w.cards["Balance"].value.text() and not w.live_btn.isVisible()
    w.close()                                           # hides to tray instead of quitting
    assert not w.isVisible()


def test_live_account_shows_locked_badge_and_dialog_requires_typed_confirmation(qapp, manager):
    from rr_connector.ui.dialogs import LiveDialog
    from rr_connector.ui.main_window import MainWindow
    manager.fake.trade_mode = 2
    manager.connect_mt5("Demo-Server", 443)
    w = MainWindow(manager)
    w.render(dict(manager.snapshot))
    assert "LIVE ACCOUNT" in w.mode_badge.text() and "LOCKED" in w.mode_badge.text()
    d = LiveDialog(manager)
    assert not d.ok.isEnabled()
    d.login.setText("1001")
    d.phrase.setText("ENABLE LIVE TRADING")
    assert not d.ok.isEnabled()                         # acknowledgement still missing
    d.ack.setChecked(True)
    assert d.ok.isEnabled()
    d._authorise()
    assert manager.snapshot["live_authorized"] is True


def test_setup_wizard_reports_engine_errors(qapp, manager):
    from rr_connector.ui.dialogs import SetupWizard
    d = SetupWizard(manager)
    d.url.setText("http://insecure.example.com")
    d.token.setText("rrmt5_" + "a" * 30)
    d._done(None, Exception("The RR AI Engine address must use HTTPS."))
    assert "HTTPS" in d.msg.text()


def test_tray_menu_has_required_entries(qapp, manager):
    from rr_connector.ui.main_window import MainWindow
    from rr_connector.ui.tray import Tray
    t = Tray(MainWindow(manager), lambda: None, lambda: None)
    labels = [a.text() for a in t.menu.actions() if a.text()]
    assert labels == ["Open Dashboard", "Connect MT5", "Disconnect MT5", "RR AI Engine Status", "Account Information",
                      "View Logs", "Restart Connector", "Exit"]


def test_settings_and_logs_dialogs_open(qapp, manager):
    from rr_connector.ui.dialogs import LogsDialog, SettingsDialog
    manager.store.event("warning", "mt5", "test event")
    LogsDialog(manager).refresh()
    s = SettingsDialog(manager)
    s.max_lot.setValue(0.5)
    s._save()
    assert manager.store.get("guard")["max_lot"] == 0.5
