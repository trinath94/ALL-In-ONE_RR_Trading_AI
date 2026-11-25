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
* **The PC's own LIVE lock:** the MT5 connector also refuses LIVE orders until LIVE is authorised on the PC for that exact account. Since Parts 27–30 Phase 10, the RR Trading One window offers this (MT5 Manager → LIVE on this PC…, typed account number and phrase, checked by the connector against the connected account; Revoke locks it again). It is the last of the independent consents, never given automatically.
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

**AI Studio** configures providers: Anthropic, OpenAI, other OpenAI-compatible APIs, Qwen, local Ollama. For each:
* the API key is encrypted and write-only;
* one route per task, with fallbacks;
* a per-minute limit; usage and cost are recorded in the ledger.

API access is billed separately from chat subscriptions: a Claude.ai or ChatGPT subscription does not include API
credits.

**Configuration profiles and the automation policy.** Strategy Library → a strategy → Configuration profiles:
* **New profile** (parameters as JSON, optional regime) or **Ask AI** (the active AI proposes; bounded, untrusted).
  Both are queued for validation; the learner validates up to two per hour on broker bars, recording every gate.
* **Activate on DEMO** (VALIDATED only) · **Activate on LIVE…** (`trade.live`, typed `ACTIVATE LIVE PROFILE`, at
  least 20 closed DEMO trades with the profile and a positive result) · **Roll back** (re-activates the validated
  parent; otherwise the strategy's configuration applies) · **Retire**.
* Policy checkboxes (`system.manage`): activate validated profiles on DEMO automatically; start the DEMO shadow test of
  validated experiments automatically. Both off by default. Nothing is ever promoted to LIVE automatically.
* API: `GET/POST /api/strategies/{code}/profiles`, `POST /api/strategies/{code}/profiles/propose`,
  `POST /api/profiles/{id}/action`, `GET/PUT /api/automation/policy`, `GET /api/opportunities/{id}/thesis`,
  `GET /api/trades/{closed_id}/review`, `GET /api/strategies/{code}/loss-significance`.

**Active AI (one provider for everything).** AI Studio → Active AI: tick *Use one provider for every AI task*, choose
the provider and model (Discover lists the provider's models), and optionally a fallback provider and model. Then:
* only the active provider (and the fallback, if enabled) receives requests; the per-task routes and consensus are
  set aside until you switch the mode off;
* without fallback, if the active provider fails or is switched off, AI analysis stops: candidates stay unreviewed,
  nothing is auto-executed in With AI mode (Run without AI is a separate, explicit switch);
* each provider row shows its last successful request and last error.
Claude Pro is listed as not available: Anthropic offers no official way for applications to use a Claude.ai
subscription. Use the Anthropic API key from the Anthropic Console. API: `GET/PUT /api/ai/control` (`system.manage`,
audited as `AI_CONTROL_UPDATE`).

**Consensus mode (candidate review only).** Set the route's mode to *consensus* and put two different providers first
(for example Claude, then OpenAI). Each reviews the candidate independently; each answer is schema-checked and
boundary-checked on its own. The candidate is confirmed only if **both** confirm the engine's direction; any rejection
rejects it; any other combination waits. If either provider gives no valid answer, the candidate stays unreviewed and
is never auto-executed. Consensus costs two calls per candidate.

**Trading several accounts at once.** MetaTrader 5 logs one terminal in to one account, so each account needs its own
MT5 terminal installation (a separate folder, logged in to that account).
1. In the desktop window, MT5 Manager: add one profile per terminal. For each profile: **Use**, **Link**, **Start**.
   Each profile runs its own connector (the first profile keeps the original one).
2. In the console, Settings → Trading accounts: add and **Verify** each account; open **Settings** and **Enable
   trading**. A LIVE account also needs its LIVE approval and is traded only while LIVE is the active mode.
3. Settings → **Execution accounts** shows each account: traded now (or why not), reconciled, connector online,
   equity and open positions.

How it trades: every signal is evaluated separately for each execution account (its symbols and strategies, its own
risk limits and equity, its own idempotency key) and sent through that account's connector. An account is reconciled
at start through its own connector before its first entry, and again after a disconnection. The kill switch blocks new
entries on every account and applies the position policy on each. A tripped risk limit on any account locks new
entries on all accounts (conservative); its position policy is applied to that account. **Stop All** stops every
connector.

**Per-account settings (multi-account foundation).** Settings → Trading accounts → **Settings** on an account:
* **Name**, and **Enable trading / Make research-only**. Enabling needs a verified account; for a LIVE account also
  `trade.live` and the LIVE approval. Disabling is always allowed.
* **Symbols** and **strategies** the account may trade automatically (blank = all scanned symbols / all enabled
  strategies).
* **Risk limits** for this account (`risk.manage`). Blank fields use the mode template from the Risk Centre. The
  values are validated like the templates; the LIVE readiness checklist compares a LIVE account's own limits with the
  DEMO template.
* API: `PUT /api/accounts/{id}/settings`, `GET/PUT /api/accounts/{id}/risk`; audited as `ACCOUNT_SETTINGS` and
  `ACCOUNT_RISK_UPDATE`.

**Engine with or without AI.** The Live Trading Control Center has an **Engine** switch: *With AI* (default) or
*Without AI*. Without AI, candidate review is skipped (no provider call, no cost) and the engine's own BUY/SELL signals
are auto-executed when auto trading is on; the Risk Engine and every risk limit apply unchanged. The switch needs
`trade.control`; in LIVE, switching to Without AI also needs `trade.live` and the typed phrase `RUN WITHOUT AI`. It
affects only signals produced after the switch. API: `POST /api/control/ai-mode` (`AI` | `ENGINE_ONLY`), audited as
`AI_MODE_*`; `GET /api/control/state` reports `ai_mode` and `ai_configured`.

**Opportunity gate.** Each analysis with a setup is checked by the entry-validation and failure-prediction agents
(see the Opportunity Lifecycle page). A stop or target on the wrong side of the entry always makes the setup NO_TRADE.
The other checks are advisory by default; to let them downgrade BUY/SELL to WAIT, set in the engine's environment:

| Setting | Default | Meaning |
|---|---|---|
| `RR_OPPORTUNITY_GATE` | `advisory` | `enforce`: chased entries, wide spreads and HIGH failure risk downgrade BUY/SELL to WAIT (never upgrade) |
| `RR_ENTRY_MAX_CHASE_ATR` | `0.5` | maximum distance from entry to the current price, in ATR |
| `RR_ENTRY_MAX_SPREAD_SL` | `0.25` | maximum spread as a fraction of the stop distance |
| `RR_REANALYSIS_THROTTLE_SECONDS` | `300` | event-driven re-analysis at most once per symbol per period |
| `RR_OPPORTUNITY_RETENTION_DAYS` | `90` | finished opportunities older than this are pruned |
| `RR_LIVE_MIN_DEMO_TRADES` | `20` | closed DEMO trades the LIVE readiness checklist requires |

In enforce mode, two more checks downgrade BUY/SELL to WAIT:
* a strategy selection of REJECT (the supporting strategies with enough evidence all have a profit factor below 1);
* an unhealthy production model (a drift event in the last 24 hours).

**Optional exits and strategy concentration** (Risk Centre, per mode). All are off by default:

| Parameter | Meaning |
|---|---|
| `max_positions_per_strategy` | refuse a new order when a strategy already has this many open positions |
| `max_holding_minutes` | close a position held this long |
| `volatility_exit_atr_ratio` | close when the current ATR is this multiple of the ATR at entry (must be above 1) |
| `structure_exit` | close on a change of character against the position, after it opened |
| `invalidation_exit` | close when the engine's latest analysis on the position's timeframe says the opposite direction or NO_TRADE |

Exits are closes sent through the same journalled, idempotent execution manager. Missing data never causes an exit.

**Capital protection** (Risk Centre, per mode). All are off by default:

| Parameter | Meaning |
|---|---|
| `max_floating_loss_pct` | no new entry while open positions are this far under water (% of balance) |
| `max_strategy_risk_pct` | the open risk of one strategy, including the new order, as % of equity |
| `volatility_sizing` | at high volatility (ATR percentile above 0.5) the risk budget is reduced, to as little as a quarter; it is never increased |

**Emergency and reconciliation.**
* The kill switch now also cancels the engine's own pending orders at the broker. These are journalled CANCEL_PENDING commands; orders placed by anyone else are never touched.
* Startup reconciliation compares the broker's pending orders with the journal. Engine-tagged orders the journal does not know (for example after restoring an older database) block new entries until you resolve them.

**Model governance.**
* A production model validated longer ago than `RR_MODEL_VALIDATION_MAX_AGE_DAYS` (default 90) is shown as degraded. In enforce mode it downgrades BUY/SELL to WAIT.
* Every trained model records a `dataset_version`: a hash of its exact training data.
* Strategies with 30 or more paper/live trades and a profit factor below 0.8 are listed for suspension in System Monitor. Disabling one is your decision.

**Scheduled maintenance** (the desktop application, local mode).
* While the database is running, a backup is taken automatically once a day; the newest 14 automatic backups are kept.
* Once a week the newest backup is restored into a scratch database and checked against the live one: every table present, same schema version. The live database is never touched.
* The result is shown under Backup and Recovery.

**LIVE readiness.** The typed LIVE switch is refused until every required item of the checklist passes:
* an active, verified and approved LIVE account;
* a LIVE risk configuration that is valid and no looser than DEMO's;
* kill switch and risk lock clear;
* no unresolved executions;
* at least `RR_LIVE_MIN_DEMO_TRADES` closed DEMO trades.

The connection, the PC connector's LIVE lock and the AI review provider are shown for information. Trading Console → LIVE Readiness shows the checklist. It can only refuse the switch, never enable LIVE.

**Parameter variants.** Strategy Library → a strategy → *Generate parameter variants* records experiments that change one numeric parameter at a time. The original strategy and its configuration are never changed. The learner evaluates up to two variants per run:
* in-sample and out-of-sample against the base;
* plus a bias report: look-ahead/repaint, leakage, survivorship, multiple testing.

Promotion follows the experiment lifecycle and your approval.

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
