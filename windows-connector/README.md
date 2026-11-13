# RR MT5 Connector (Windows desktop app)

Connects a logged-in MetaTrader 5 terminal to the RR AI Trading Engine over one outbound secure WebSocket. The
user enters the MT5 server address and port and clicks **CONNECT**.

* User guide, architecture, integration contract, security and the testing report: [`docs/WINDOWS_CONNECTOR.md`](../docs/WINDOWS_CONNECTOR.md)
* Build the EXE and installer on Windows: `powershell -ExecutionPolicy Bypass -File windows-connector\packaging\build.ps1`
* Run from source (Windows, Python 3.12+ 64-bit): `pip install -r requirements.txt` then `python -m rr_connector`
* Tests: `python -m pytest -q tests` (they also run headless on Linux with `QT_QPA_PLATFORM=offscreen`)

| Module | Purpose |
|---|---|
| `rr_connector/terminals.py` | MT5 auto-detection: registry, install folders, MetaQuotes data folders, running processes |
| `rr_connector/mt5link.py` | Attach to the logged-in terminal (no credentials), validate the server, read the account |
| `rr_connector/manager.py` | Background manager: watchdog, account changes, journal reconciliation, relay dispatch into the original connector |
| `rr_connector/guard.py` | PC backstop: LIVE lock, lot, position and daily-loss caps, emergency stop |
| `rr_connector/agent.py` | Device identity, signed registration, outbound WebSocket with heartbeat and reconnection |
| `rr_connector/store.py`, `vault.py` | Encrypted SQLite settings, event log, Windows Credential Manager |
| `rr_connector/ui/` | Dashboard, tray, setup wizard, live authorisation, settings, logs |
| `packaging/` | PyInstaller spec, Inno Setup installer, icon, build script |
