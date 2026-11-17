# RR Trading One: administrator manual

For the person responsible for the installation: the first administrator, and anyone holding the `admin` role.

## 1. Users and roles

| Role | Can |
|---|---|
| `admin` | everything: users, accounts and connectors, LIVE approval, risk, strategies, backtests, trading control |
| `trader` | view, manual orders, trading control (auto trading on/off, pause, kill switch), backtests |
| `readonly` | view |

* **Managing users:** Trading Console → User Management (`users.manage`). Changes are audited. An administrator cannot demote themselves.
* **Sign-in protection:** repeated failed sign-ins lock the account temporarily. Sessions are HttpOnly cookies with CSRF protection, and signing out revokes the session.
* **The first administrator** is created from the desktop window while no user exists, and never again after that.

## 2. Accounts: DEMO and LIVE

* **DEMO:** registered from the desktop window (MT5 Manager → Use as engine DEMO account), only when the connector is logged in to exactly that account and the account is DEMO.
* **LIVE:** added in Settings with its own identity (login@server), broker connection and encrypted password. Then, in order:
  1. **verify** it against the connected terminal (exact login, exact server, REAL account);
  2. **approve** it (`trade.live`, typed `APPROVE <login>`);
  3. **activate** it and **switch** to LIVE (typed `ENABLE LIVE TRADING`).
  * Before the switch, new entries are blocked until verification and reconciliation; a switch is refused while executions are unresolved.
  * **Revoke live** re-locks an approved account immediately.
* **The PC's own LIVE lock:** the MT5 connector also refuses LIVE orders until LIVE is authorised on the PC for that account. RR Trading One does not offer this authorisation yet (see the Release notes), so LIVE trading cannot be completed from this version.
* **The same identity cannot be both DEMO and LIVE** (refused with a clear message).

## 3. Risk

The Risk Centre edits the risk configuration **per mode**; LIVE has stricter defaults (0.25 % per trade). It covers:
* daily loss and drawdown limits, consecutive losses;
* maximum positions, correlated exposure, margin, spread, news;
* the existing-position policy for an emergency stop: PROTECT (default), REDUCE or CLOSE_ALL.

Every order passes the Risk Engine; nothing (including AI) can bypass it.

## 4. Strategies, models and learning

* **The 215 original strategies** are listed in the Strategy Library. Risky strategies are disabled by default and can never be enabled for LIVE. The baseline is verified by hashes (preservation tests).
* **Experimental strategies** from the research agent run only in a sandbox and never enter the production registry.
* **Models** (probability, consensus weights, adaptive profiles):
  * trained → validated (walk-forward, compared with production) → **approved by an administrator** → production;
  * artefacts are HMAC-signed and verified on every use; rollback is available;
  * adaptive profiles run in shadow until approved.
* **Learning jobs** run in the learner, each as its own process with a timeout. A running job can be cancelled from the AI Learning Centre.

## 5. AI providers

**AI Studio** configures providers: Anthropic, OpenAI-compatible, Qwen, local Ollama. For each:
* the API key is encrypted and write-only;
* one route per task, with fallbacks;
* a per-minute limit; usage and cost are recorded in the ledger.

AI output is schema-checked and advisory. AI cannot place orders, enable LIVE, change permissions or raise
confidence. Broker passwords are never sent to a provider.

## 6. The desktop supervisor

**Allowed actions.** The desktop window runs a closed list of actions:

| Action | Notes |
|---|---|
| Start All, Stop All, Restart All | Stop plans always stop the trading worker first |
| Start / Stop Backend | |
| Start / Stop MT5 Connector | |
| Start / Stop Learner | |
| Start / Stop Trading Worker | Starting needs the typed phrase `START TRADING WORKER` |
| System Health, Recovery | Recovery never restarts the trading worker |
| Back up now | |

**How they run.** Each action is a fixed plan of Docker Compose commands, with no shell. Nothing typed into a page
becomes a command.

**Resources.**
* Each role has a CPU cap and a memory limit.
* The monitor (position protection) and the trading worker outrank analysis and the learner when the PC is busy.
* PostgreSQL has 256 MB of shared memory.
* The System Monitor page shows each role's usage. A role near its memory limit, or a disk below 2 GB free, raises a critical alert.

## 7. Secrets and keys

| Secret | Held by | Rotatable |
|---|---|---|
| Database and Redis passwords, session signing key, bridge secret, local API key | Stack secrets (DPAPI); the containers that need them | **Yes:** Security → Rotate secrets (`ROTATE SECRETS`). A backup is taken first, everyone is signed out, and the engine restarts. |
| Broker-password encryption key, AI-key encryption key, read-only bridge key, model signing key | Stack secrets; only the containers that need each | No: rotating them would make stored passwords, AI keys and models unreadable |
| **Backup key** | Stack secrets; the desktop app only | No. **Export it** (Security → Show backup key, `SHOW BACKUP KEY`) and keep it offline: without it, backups cannot be restored on another PC. |

Secrets are never in the program folder, settings or logs; the engine's logs redact them.

## 8. Monitoring and audit

* **Command Centre:** overall health, services, roles, active critical alerts, and past critical events.
* **System Monitor:** services, role resources, events, and the audit trail (who changed what).
* **Multi-Agent Control Room:** every agent run, its errors and health.
* **Logs:**
  * the desktop supervisor's log is in `C:\ProgramData\RR Trading One\logs`;
  * the engine's logs are the containers' logs (`docker logs rr-trading-one-api-1`, ...), as JSON;
  * the connector's logs are in `%LOCALAPPDATA%\RR Trading One\connector\logs`.

## 9. Maintenance

* **Backups:**
  * one is taken automatically before every migration and before every restore or import (the newest are kept);
  * take one by hand before any change you might want to undo (Back up now);
  * copy the backups folder off the PC regularly, and keep the backup key separately.
* **Dependency updates:** the repository's security workflow scans the dependencies weekly; a known vulnerability fails it.
* **Upgrades:** install the newer installer over the old one (Installation manual §5).
