# RR MT5 Connector — Windows desktop application

A Windows 10/11 (64-bit) desktop app that connects the user's MetaTrader 5 terminal to the RR AI Trading Engine on
Ubuntu.

* **Outbound only.** The PC opens one secure WebSocket to the engine. No inbound firewall port is needed on Windows.
* **Decisions stay in the engine.** The app never generates signals and cannot bypass the Risk Engine.

Source: `windows-connector/`. It reuses the original connector package `mt5-connector/connector` (journal,
idempotency, signature/replay checks, order building, retcode handling). No execution logic is duplicated.

## 1. What the user does

1. **Run `RR_MT5_Connector_Setup.exe`.**
   * The installer puts the app in `C:\Program Files\RR MT5 Connector`.
   * If MetaTrader 5 is missing, it explains where to get it.
   * One page asks for the **RR AI Engine address and a registration token**. This page is optional; the app also
     asks on first start.
2. **Log in to the trading account in MetaTrader 5**, as usual.
3. **In RR MT5 Connector, enter the MT5 server address and port, then click CONNECT.**

After that, everything is automatic:
* it starts at Windows sign-in;
* it runs in the system tray;
* it reconnects after network or terminal interruptions.

### What the two fields mean (honest scope)
The MetaTrader5 Python integration controls the **local terminal**, and the terminal holds the broker session. A
server address and port cannot log in to an account, so the connector **never asks for, stores or guesses a
trading password**.

On CONNECT, the app:
1. Finds the installed terminals. It starts the terminal if it is installed but not running.
2. Attaches to the terminal that is **already logged in**. If none is, it tells the user to log in in
   MetaTrader 5.
3. Validates the entered server:

   | Result | Meaning |
   |---|---|
   | **MATCH** | The address equals the server name the terminal is logged in to (e.g. `ICMarkets-Demo`). |
   | **REACHABLE** | `address:port` accepts a TCP connection. The latency is shown, along with the terminal's actual server. |
   | **UNREACHABLE** | CONNECT is refused with a clear message. |

4. Reads the account: broker, login, server, currency, balance, equity, free margin, leverage and DEMO/LIVE. It
   also checks trading permissions, and warns if Algo Trading is off.

If several terminals are logged in, a selector appears; otherwise none is shown.

## 2. Architecture

```
 Windows PC                                                    Ubuntu 24.04
 ┌──────────────────────────── RR MT5 Connector ─────────────┐          ┌──────────── RR AI Engine ────────────┐
 │ PySide6 UI + tray ── ConnectorManager                     │  WSS     │ nginx ─ /api/v1/mt5/agent/ws (hub)    │
 │   MT5Link (attach, validate)   watchdog (crash/acct/day)  │ ───────► │        │ Redis queue per agent        │
 │   EngineLink (hello, heartbeat 5 s, backoff reconnect) ◄──┼──────────┼─ BridgeClient (unchanged) + Relay-   │
 │   original connector app (journal, HMAC verify, execute)  │ requests │   Transport  ◄─ worker / API / learner│
 │   local guard (live auth, caps, emergency stop)           │          │ Risk Engine, kill switch, strategies  │
 │   MetaTrader5 package ──► terminal64.exe (logged in)      │          └───────────────────────────────────────┘
 └───────────────────────────────────────────────────────────┘
```

* **Engine side.** `RR_BRIDGE_MODE=relay` swaps the bridge's TCP transport for a Redis-backed relay
  (`backend/app/execution/relay.py`). Every existing caller works unchanged: worker, scanner, trade sync, risk
  checks and manual orders. `RR_BRIDGE_MODE=direct` (the default) keeps the original HTTPS connector.
* **Each request** keeps the original HMAC signature (timestamp, nonce, body hash). It is verified **on the PC** by
  the original connector code, so a compromised relay cannot forge orders.
* **Deadlines.** Every relayed request carries a deadline. Expired requests are dropped and never executed late.
* **Timeouts** behave exactly like a lost HTTPS connection: the order is UNKNOWN and is reconciled through the
  execution journal (`/executions/{id}`).

## 3. Integration contract (`/api/v1/mt5`, additive)

| Prompt endpoint | Implemented as | Auth |
|---|---|---|
| `POST /api/v1/mt5/agent/register` | Same path. One-time token plus an Ed25519 device signature. Returns `agent_id`, API key and per-agent bridge secret. | Registration token |
| `POST /api/v1/mt5/heartbeat` | Same path (REST fallback). Normally sent over the WebSocket every 5 s. | Agent signature |
| `GET /api/v1/mt5/status` | Same path: agents, online state, terminal, account snapshot, latency, selected agent. | User session |
| `GET /api/v1/mt5/accounts` | **Existing** `GET /api/accounts`. Not duplicated. | User session |
| `POST /api/v1/mt5/account/sync` | Existing `POST /api/positions/sync` and the worker's continuous account sync, now running **through the relay**. | User session |
| `GET …/account/{id}/positions`, `…/orders` | **Existing** `GET /api/positions`, `GET /api/orders`. | User session |
| `POST …/account/{id}/execute` / `close` / `modify` | **Existing** `POST /api/orders`, `POST /api/positions/{ticket}/action`. All go through the Risk Engine. | User session + CSRF |
| (new) `GET /api/v1/mt5/agent/ws` | Outbound agent WebSocket: hello → welcome, request/response, heartbeat, `kill_switch`, `event`. | API key + Ed25519 + nonce |
| (new) `GET /api/v1/mt5/agent/update` | Latest installer version, HTTPS URL and SHA-256. | Agent signature |
| (new) `POST /api/v1/mt5/agent/tokens`, `GET /api/v1/mt5/agents`, `POST /api/v1/mt5/agents/{id}/revoke` | Admin management (UI: **MT5 Terminal → RR MT5 Connector agents**). | `system.manage` |

**Signed messages (Ed25519, base64):**
* Register: `RRMT5-REGISTER\n{device_id}\n{public_key}\n{ts}\n{sha256(token)}`
* Hello: `RRMT5-HELLO\n{agent_id}\n{ts}\n{nonce}`
* REST: `RRMT5-REQ\n{METHOD}\n{path}\n{ts}\n{nonce}\n{sha256(body)}`. Headers: `X-RR-Agent-Id`, `-Key`, `-Ts`,
  `-Nonce`, `-Sig`.

Timestamps must be within ±60 s, and nonces are single-use (stored in Redis).

**Relay frames:**
* Engine → PC: `{"type":"request","id","method","path","headers"(x-rr-*),"body"(b64),"deadline"}`
* PC → engine: `{"type":"response","id","status","body"(b64)}`

A response is accepted only once, and only for an id issued to that agent.

## 4. Security

| Requirement | Implementation |
|---|---|
| Windows Credential Manager | API key, per-agent bridge secret, Ed25519 private key and config-encryption key are stored there via `keyring` (WinVault). There are no secret files. |
| Encrypted local configuration | `%LOCALAPPDATA%\RRMT5Connector\config.db`: every value is Fernet-encrypted; the key is in Credential Manager. A test checks that no plaintext is on disk. |
| HTTPS / secure WebSocket | HTTPS is required (http only for localhost). System trust store, with an optional private CA file. |
| API key, device id, session auth, signed registration | Device id = SHA-256(MachineGuid + install salt). The Ed25519 key pair is generated on the PC. The engine stores only hashes (API key, token) and the Fernet-encrypted bridge secret. Each WebSocket session is authenticated with a challenge signature. |
| Request validation, replay protection | Engine: timestamp skew plus single-use nonces. PC: the original connector's HMAC verification and nonce journal on every relayed request; requests past their deadline are dropped. |
| Audit logging | Engine: `audit_logs` (token created, registered, denied, revoked), system events and notifications from the PC. PC: local event log (connection, trade and audit categories) plus rotating file logs with secrets redacted. |
| Trading credentials | Never requested, stored or sent; the terminal's own session is used. |
| Revocation | **Revoke** in the engine UI disconnects the PC immediately; it must be registered again. |

## 5. DEMO / LIVE safety

* **The account mode comes from MT5** (`trade_mode`). Anything that is not DEMO is treated as **LIVE**. The UI shows
  a large **DEMO ACCOUNT** or **LIVE ACCOUNT · TRADING LOCKED/AUTHORISED** badge.
* **Live is locked by default.** On a LIVE account the PC refuses **every** trading command until the user enables
  it, which requires:
  * typing the account number;
  * typing `ENABLE LIVE TRADING`;
  * ticking the risk acknowledgement.

  The authorisation is bound to that login and server; changing account revokes it. The engine's own live approval
  and the Risk Engine still apply.
* **PC backstops** apply to new exposure only; closing and reducing positions are always allowed:
  * maximum lot per order;
  * maximum open positions;
  * maximum daily loss (% of the equity recorded at the start of the UTC day).
* **Emergency stop** (button or tray):
  * refuses new positions on the PC;
  * sends a `kill_switch` that activates the engine's existing kill switch, with an audit entry.

  Releasing the local stop does **not** reset the engine kill switch; that is done in the engine.
* The PC never generates signals and never sends an order on its own.

## 6. Ubuntu 24.04 — engine configuration

Additive migration `0003` (two new tables) plus the relay mode.

Back up first:
```bash
cd /opt/rr-trading-engine && ./scripts/backup.sh
```

Switch to the branch:
```bash
git fetch origin && git checkout feature/rr-mt5-connector-desktop
```

Enable relay mode in the engine's environment file:
```bash
sed -i 's/^RR_BRIDGE_MODE=.*/RR_BRIDGE_MODE=relay/' .env && grep -q '^RR_BRIDGE_MODE=' .env || echo 'RR_BRIDGE_MODE=relay' >> .env
```

Rebuild, migrate and restart:
```bash
docker compose build && docker compose run --rm migrate
```

```bash
docker compose up -d api worker learner frontend nginx
```

Verify the migration (expect `0003`):
```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select version_num from alembic_version"
```

Check that nginx accepts the new WebSocket location:
```bash
docker compose exec nginx nginx -t
```

Then, in the web UI, open **MT5 Terminal → RR MT5 Connector agents → Create registration token**.

**Rollback** (the PC app simply shows "reconnecting"):
```bash
sed -i 's/^RR_BRIDGE_MODE=.*/RR_BRIDGE_MODE=direct/' .env && docker compose up -d api worker learner
```

To also remove the schema (this drops the registered agents and tokens):
```bash
docker compose run --rm api alembic downgrade 0002
```

## 7. Building on Windows

See `windows-connector/packaging/`. With 64-bit Python 3.12+, from the repository root:
```powershell
powershell -ExecutionPolicy Bypass -File windows-connector\packaging\build.ps1
```
The build:
1. runs the tests;
2. builds `dist\RR_MT5_Connector\RR_MT5_Connector.exe` with PyInstaller;
3. runs the **packaged self-test** (MetaTrader5 package, Credential Manager round-trip, encrypted store, terminal
   detection);
4. compiles `dist\RR_MT5_Connector_Setup.exe` with Inno Setup 6 and writes `SHA256SUMS.txt`.

The GitHub Actions workflow `.github/workflows/windows-connector.yml` does the same on `windows-latest`, adds a
silent install/uninstall smoke test, and uploads the installer as an artifact.

**Installer options:**
* `/VERYSILENT /ENGINEURL=https://… /TOKEN=rrmt5_…` for unattended deployment.
* Tasks: start with Windows, desktop shortcut.
* Uninstall can remove credentials and data (`--forget`).

**Updates:** set `RR_AGENT_LATEST_VERSION`, `RR_AGENT_UPDATE_URL` (HTTPS) and `RR_AGENT_UPDATE_SHA256` on the
engine. **Settings → Check for updates** downloads the file, refuses any SHA-256 mismatch, and upgrades in place.

## 8. Testing report

| Suite | Where | Result |
|---|---|---|
| Desktop unit + UI tests (offscreen Qt) — detection, input validation, server MATCH/REACHABLE/UNREACHABLE, not-logged-in, auto-launch, multi-terminal choice, guard rules, encrypted store, device key, updater hash check, dashboard/live dialog/tray/settings | `windows-connector/tests` | 24 passed (Linux) |
| End-to-end over a real socket — registration (invalid/reused token, forged signature, HTTPS rule), WebSocket auth (bad key, replayed nonce, stale timestamp), relay health/account/symbols/ticks/rates, startup checks, **DEMO trade executed in the terminal**, heartbeat in engine status, RBAC, offline fail-fast, expired-request drop, restart recovery, LIVE lock / typed authorisation / PC lot cap, PC emergency stop → engine kill switch, revocation, terminal crash recovery | `backend/tests/test_mt5_agent.py` | 9 passed |
| Original connector tests | `mt5-connector/tests` | 10 passed |
| PyInstaller build + frozen self-test | Linux build (spec/imports verified); Windows build in CI | Linux OK except the Windows-only MetaTrader5 package; Windows: see the CI run |

The MT5 terminal in automated tests is the **FakeMT5 test fixture**. It is never shipped and never used by the
application.

### Final acceptance tests — status

| # | Test | Status |
|---|---|---|
| 1 | Install on Windows 10/11 | Installer and silent install/uninstall run in Windows CI; **needs a manual check on a user PC** |
| 2 | Auto-detect installed MT5 | Detection logic is tested; the packaged self-test runs detection on Windows. **Needs a PC with MT5.** |
| 3–4 | Enter server and port, click CONNECT | Tested (UI + manager, fake terminal) |
| 5 | Actual authenticated MT5 account | **Needs a real terminal.** The code attaches with `MetaTrader5.initialize(path)` without credentials. |
| 6 | Balance/equity synchronised with Ubuntu | Tested end to end (heartbeat → `/api/v1/mt5/status`; worker sync through the relay) |
| 7 | Engine receives live market data | Tested through the relay (ticks, rates, symbols) with the fake terminal; **real feed needs a terminal** |
| 8 | Demo trade from the engine executed in MT5 | Tested end to end with the fake terminal; **needs a real demo account** |
| 9 | Internet loss → automatic recovery | Reconnect with backoff and restart recovery are tested; **pulling the network cable needs a PC** |
| 10 | Windows restart → auto-start | HKCU Run key (`--minimized`) is written by the installer and app; **needs a PC** |
| 11 | Live cannot be enabled without explicit authorisation | Tested (PC lock + typed authorisation + account binding) |
