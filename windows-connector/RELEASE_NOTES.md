Windows desktop app that connects your logged-in MetaTrader 5 terminal to the RR AI Trading Engine over one outbound secure connection. You don't need to open a port on Windows.

### Download
**`RR_MT5_Connector_Setup.exe`**: Windows 10/11, 64-bit. Check the download against `SHA256SUMS.txt`.

### Before installing
1. On the engine server, set `RR_BRIDGE_MODE=relay` and restart the engine (see `docs/WINDOWS_CONNECTOR.md`, section 6).
2. In the engine web UI, go to **MT5 Terminal → RR MT5 Connector agents → Create registration token**.

### Install and connect
1. Run the installer. Enter the engine's `https://` address and the token when asked.
2. Log in to your trading account in MetaTrader 5.
3. In RR MT5 Connector, enter the broker server and port, then click **CONNECT**.

The connector never asks for your trading password; it uses the session of the MT5 terminal you're logged in to.

### Safety
- **Live accounts:** locked until you authorise them on the PC, by typing the account number and `ENABLE LIVE TRADING`.
- **PC limits:** maximum lot size, maximum open positions and maximum daily loss.
- **Emergency stop:** also triggers the engine's kill switch.
- **Engine controls:** the engine's Risk Engine and live approval still apply.

### Tested
Built and tested by GitHub Actions on Windows:
- unit and screen tests;
- the original connector's tests;
- a self-check of the packaged app (`selftest.json`): MetaTrader5 package, Windows Credential Manager, encrypted settings, MT5 detection;
- a silent install and uninstall.

Still to be confirmed on a PC running MetaTrader 5:
- connecting to a real terminal;
- a demo trade;
- live market data;
- recovery after a network drop;
- auto-start after a Windows restart.

### Known limitation
The installer is not code-signed. Windows SmartScreen may warn on first run; choose **More info → Run anyway**.
