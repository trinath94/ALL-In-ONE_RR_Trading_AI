# RR Trading One: user manual

RR Trading One has two windows:
* **the desktop window** (Control Centre), which runs the engine on your PC;
* **the Trading Console**, the engine's web interface. Open it with **Open Trading Console**, or from the tray icon.

Closing the desktop window keeps it in the tray. Quitting it does **not** stop the engine: the monitor keeps
protecting open positions. Stopping the engine is **Stop All**.

## 1. The desktop window

| Section | What it does |
|---|---|
| **Badges** (top) | Local mode, account mode (DEMO/LIVE), auto trading, trading worker, LIVE lock. Read live from the engine. |
| **First-run setup** | The nine setup steps and which one is next (Installation manual §3) |
| **Safety state** | Mode, auto trading, worker, kill switch, risk lock, startup checks, active account, connector, engine roles |
| **System health** | Real checks of the engine API (with PostgreSQL and Redis) and the web interface |
| **MT5 Manager** | Detect terminals, save and select a profile, link/start/stop the connector, register the DEMO account. It shows the connected account (login masked), balance, Algo Trading and warnings. |
| **Services / Service manager** | Start All, Stop All, Restart All; start/stop the backend, connector, learner and trading worker; System Health; Recovery. Each action shows its plan and every step's result. |
| **Backup and Recovery** | Back up now, the backups list, restore, import from the Ubuntu server, migration report |
| **Security** | Show the backup key; add another installation's key; rotate secrets |
| **Locations** | The folders the application uses |

**Typed confirmations.** Actions that can trade or replace data ask you to type an exact phrase:

| Action | Phrase |
|---|---|
| Start the trading worker | `START TRADING WORKER` |
| Restore a backup | `RESTORE DATABASE` |
| Import the Ubuntu database | `IMPORT DATABASE` (or `IMPORT AND REPLACE LOCAL DATA` when the PC already has trading records) |
| Show the backup key | `SHOW BACKUP KEY` |
| Rotate secrets | `ROTATE SECRETS` |

## 2. The Trading Console

Sign in with your user. The menu follows the specification:

| Area | Pages |
|---|---|
| **Overview** | Command Centre (health, services, roles, account, signals, executions, critical alerts); AI Trading Engine |
| **Agents** | Multi-Agent Control Room (27 agents: status, task, market, provider/model, output, errors, health) |
| **Broker connection** | MT5 Manager; Broker Symbol Discovery (choose which symbols are scanned; new ones start disabled) |
| **Scanning** | Market Scanner (bid/ask, spread, regime, consensus, calibrated probability or "insufficient data", data quality); Market Analysis; Signal Centre; Opportunity Lifecycle (every researched setup, its state and its decision trace, stage by stage, plus the observed health of each data source); Pattern Intelligence (harmonic and Elliott detections with their lifecycle, the strategies that support or contradict them, and outcomes measured on price bars; advisory only); LIVE Readiness (the checklist the LIVE switch requires; read-only); Simulation Laboratory (scenario stress tests and counterfactual alternatives, research only) |
| **Trading** | Trading Worker; Open Positions; Trade History |
| **Learning** | AI Learning Centre (trades, verdicts, models, drift, jobs, with a cancel button for running jobs); Adaptive Intelligence; Strategy Performance; Strategy Correlation; Predictive Intelligence |
| **Research** | Backtesting Lab; Optimisation Lab; Strategy Library (the 215 original strategies and their data needs); Strategy Research Lab |
| **Risk** | Risk Centre; Portfolio Intelligence |
| **AI** | AI Studio (providers, models, routes, usage) |
| **Market context** | News and Economic Calendar |
| **System** | System Monitor (services, each role's CPU and memory, disk, database size, API latency and errors, error logs per service, database connections, job backlog, model governance, market data quality, events, audit); Backup and Recovery; Settings |

Also: Trade Intelligence, Notifications, User Management.

Every number comes from recorded data or live state. Where there is not enough data (for example a calibrated
probability), the console says so instead of showing a guess.

## 3. Trading in DEMO

Automatic trading on a DEMO account needs **all** of the following:
1. The connector is running and logged in to the DEMO account registered with the engine; the startup reconciliation has passed.
2. **Auto trading ON** (Trading Console, by a user allowed to control trading).
3. **The trading worker started** (desktop window, `START TRADING WORKER`).

The worker executes only signals created after auto trading was switched on, whose direction the review confirmed.
Every order still goes through the Risk Engine. Manual orders are placed from the console.

**To stop:**
* **Auto trading OFF** stops new entries.
* **Stop Trading Worker** stops execution.
* The **kill switch** (Risk Centre) blocks all new entries. By default it tightens stops on open positions (PROTECT) and does not close them; closing everything is a separate, typed `CLOSE ALL`.

## 4. LIVE trading

LIVE is locked until an administrator:
* adds the LIVE account;
* verifies it against the connected terminal (exact login, server and a REAL account);
* approves it (typed `APPROVE <login>`);
* switches to LIVE (typed `ENABLE LIVE TRADING`).

**In this version, LIVE cannot be completed from RR Trading One.** The MT5 connector on the PC has its own LIVE lock,
bound to the account. The connector that RR Trading One runs shows it as "LIVE on this PC: locked", and the desktop
window has no control yet to authorise it. The connector therefore refuses LIVE orders whatever the engine allows.
This follows the development rule that LIVE is never enabled during development; see the Release notes.

## 5. When something looks wrong

* **Critical alerts** on the Command Centre show active problems: kill switch, risk lock, critical anomaly, a service down, pending reconciliation, a role about to run out of memory, low disk. Past events are listed separately.
* See the [Recovery manual](RECOVERY_MANUAL.md).
