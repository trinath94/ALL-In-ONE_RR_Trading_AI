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
| **Scanning** | Market Scanner (bid/ask, spread, regime, consensus, calibrated probability or "insufficient data", data quality); Market Analysis; Signal Centre (with each signal's decision trace); Opportunity Lifecycle (every researched setup, its state and its decision trace, stage by stage, plus the observed health of each data source); Pattern Intelligence (harmonic and Elliott detections with their lifecycle, the strategies that support or contradict them, and outcomes measured on price bars; advisory only); DEMO Validation (acceptance evidence from what the installation recorded; read-only); LIVE Readiness (the checklist the LIVE switch requires; read-only); Simulation Laboratory (scenario stress tests and counterfactual alternatives, research only) |
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

The worker executes only signals created after auto trading was switched on. Which ones depends on the **Engine**
switch in the Live Trading Control Center:

| Engine switch | What is auto-executed | AI provider needed |
|---|---|---|
| **With AI** (default) | only candidates whose direction the AI review confirmed | yes (AI Studio); without one, nothing is auto-executed |
| **Without AI** | the engine's own BUY/SELL signals, with no AI review and no AI cost | no |

* Every order still goes through the Risk Engine, with every risk limit, in both positions.
* The switch applies to signals produced after it. A candidate the AI rejected or left unreviewed is never executed by
  switching to Without AI, and a signal produced without AI is not executed after switching back to With AI.
* In LIVE, switching to Without AI needs the LIVE permission and the typed phrase `RUN WITHOUT AI`. Switching back is
  one click.
* Every switch is in the audit log.

Manual orders are placed from the console.

**Ask the engine.** The *Ask the engine* button (bottom right of every page) opens the Engine Assistant. Ask "Engine
status", "Why was the last signal not executed?", "Why was signal #42 not executed?" or "What should I do next?". In the
Signal Centre, **Ask why** on a signal does the same for that signal. The answer lists each check the trading worker
applies (✔ passed, ✖ stopped here), each account's result (eligible, reconciled, Risk Engine decision, order) and the
next step. The assistant only reads; it never changes a setting or places an order.

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

**LIVE on this PC (the connector's own lock).** The MT5 connector on the PC refuses LIVE orders until LIVE is authorised on the PC for that exact account. In the RR Trading One window, **MT5 Manager → LIVE on this PC…**:
* type the connected LIVE account number and `ENABLE LIVE TRADING`;
* the connector checks both against the account it is connected to, and refuses a DEMO account or any other number;
* the authorisation is bound to that login and server, and changing account revokes it;
* **Revoke** locks it again at any time.

This is the PC's consent only. LIVE trading also needs everything the engine requires (an administrator-approved LIVE account, the readiness checklist with a DEMO track record, the typed switch in the console). Leave it locked unless you have decided to trade LIVE.

## 5. When something looks wrong

* **Critical alerts** on the Command Centre show active problems: kill switch, risk lock, critical anomaly, a service down, pending reconciliation, a role about to run out of memory, low disk. Past events are listed separately.
* See the [Recovery manual](RECOVERY_MANUAL.md).
