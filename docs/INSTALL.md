# Installation guide

## A. Ubuntu 24.04 application server

```bash
git clone <your repo> /opt/rr-trading-engine
```

```bash
cd /opt/rr-trading-engine
```

```bash
sudo ./scripts/install.sh --domain trading.example.com --email ops@example.com
```

The installer:
* installs Docker Engine and Compose;
* enables UFW for 22, 80 and 443 only;
* generates `.env` with fresh secrets;
* obtains a Let's Encrypt certificate (or a self-signed one with `--self-signed`);
* builds and starts the stack;
* schedules a daily backup.

Then:
1. Edit `.env`: set `RR_BRIDGE_URL=https://<windows-ip>:8443` and `RR_ANTHROPIC_API_KEY`, and
   optionally the news, Telegram and SMTP settings.
2. Copy the Windows connector certificate to `secrets/connector-ca.pem`.
3. Run `docker compose up -d`.
4. Create the first administrator:

```bash
read -rs RR_ADMIN_PASSWORD; export RR_ADMIN_PASSWORD
```

```bash
docker compose run --rm -e RR_ADMIN_PASSWORD api python -m app.cli create-admin --username admin
```

5. In the UI, go to **Settings → Add account** (DEMO), then **Verify**, **Activate** and **Discover from MT5**.
6. The worker runs startup reconciliation. **System Monitoring** and **MT5 Terminal** show the result.
7. Run DEMO with auto trading for a validation period before creating a LIVE account. A LIVE account
   requires an administrator to **Approve live** and then explicitly switch modes.

Verification:

```bash
./scripts/healthcheck.sh
```

```bash
docker compose logs -f worker
```

## B. Windows MT5 execution server

Prerequisites:
* MetaTrader 5 installed and logged in to the broker account, with **Algo Trading** enabled.
* 64-bit Python 3.10 or newer.
* A dedicated local user (e.g. `rrsvc`) with the "Log on as a service" right, under whose profile the
  terminal is installed.

Run in an elevated PowerShell:

```powershell
cd C:\path\to\rr-trading-engine\mt5-connector\windows
.\install.ps1 -LinuxServerIp 10.0.0.10 -ConnectorHost 10.0.0.20 -Mt5Login 12345678 -Mt5Server "Broker-Demo" -Mt5TerminalPath "C:\Program Files\MetaTrader 5\terminal64.exe" -ServiceUser ".\rrsvc"
```

The script prompts for the MT5 password and the bridge secret, and stores them in Credential Manager.
It also:
* creates the venv and verifies the `MetaTrader5` import;
* generates the TLS certificate (`C:\RRConnector\tls\connector.crt`);
* restricts the firewall to the Linux IP;
* installs an auto-start service that restarts on failure.

Check it with `.\health.ps1`. Upgrade with `.\upgrade.ps1`; the journal is preserved.

> Validate on your server that the terminal is reachable from the service account's session. Some
> brokers' terminals need an interactive session. In that case, run `run.py` as an "At log on"
> scheduled task for the auto-logon user instead of a service. The connector's code is the same either way.
