# RR Trading One: release notes

## In development: Parts 27-30 (autonomous research and AI orchestration)

Incremental additions to 0.2.0; nothing existing is replaced.

* **Phase 4 (Part 27):**
  * **Opportunity Lifecycle:** each researched setup moves through states (monitoring, waiting, qualified, rejected, no trade, expired, executed), with a stage-by-stage decision trace;
  * **five new agents** in the same registry: fundamental (economic calendar), entry validation, exit plan, failure-risk factors, AI consensus;
  * **OpenAI preset**, and an optional **consensus mode** in which two AI providers must agree;
  * **regime tags**: volatility expansion/contraction, accumulation/distribution, uncertain, news-driven;
  * **data-source registry** with observed health;
  * **event-driven re-analysis** on news releases, anomalies, and price reaching a waiting entry.
  * Database migration 0012 only adds tables and one column.

## Version 0.2.0 (development release, 2026-11-17)

The first complete development release of RR Trading One: the RR Trading AI Engine as one Windows desktop
application. **It is for DEMO use and acceptance testing**, not yet for LIVE trading (see Known limitations).

### What it contains

* **One Windows application** (Tauri 2) that installs, starts and supervises the whole engine locally:
  * PostgreSQL, Redis, the FastAPI engine and the Next.js console in Docker Desktop;
  * the MT5 connector natively in your Windows session.
* **The original engine, preserved:**
  * all 215 strategies, verified by ID, configuration and code hashes;
  * risk, execution, learning, intelligence and backtesting unchanged in behaviour;
  * all 112 original API routes kept.
* **Broker symbol discovery** with your choice of what is scanned. **A scanner** that analyses each closed bar once, within a time budget, with only the strategies valid for each timeframe.
* **21 specialist agents** with a Control Room, and an **AI Studio** supporting several providers (Anthropic, OpenAI-compatible, Qwen, local Ollama). These have routes, fallbacks, limits and a cost ledger, and are always advisory.
* **Historical trade intelligence and learning:** broker-sourced trade history, attribution, signed models with walk-forward validation and administrator approval, adaptive strategy weighting in shadow until approved, and calibrated probability and signal fusion that never makes a decision more aggressive.
* **DEMO and LIVE separation:**
  * initial state DEMO, worker stopped, LIVE locked;
  * LIVE needs verification, approval and typed confirmations, and can be revoked.
* **A 30-page console** following the specification's navigation, and a desktop Control Centre with a first-run wizard.
* **Backup and recovery:**
  * encrypted backups, with an exportable backup key;
  * one-transaction restores that refuse damaged or modified backups.
* **Migration from the Ubuntu deployment:** a read-only server export, plus a verified, content-checked import with an explicit replace policy and a migration report.
* **Security:**
  * loopback-only engine; signed and replay-protected local and connector traffic;
  * secrets DPAPI-protected and redacted from logs; secret rotation;
  * dependency scanning in CI.
* **Resource management:** CPU priority for position protection and execution over learning, learning jobs that are really stopped on timeout or cancel, hot-path database indexes, and per-role CPU, memory and disk monitoring.
* **Tests:** engine 323, MT5 bridge 17, RR MT5 Connector 36, desktop 130. Every area and safety condition of the specification's Part 22 is mapped to passing tests, checked in CI. The Windows installer is installed, upgraded and uninstalled in CI.

### Known limitations

* **LIVE trading cannot be completed from this version.** The PC's MT5 connector keeps its own LIVE lock, and RR Trading One has no control yet to release it. This is deliberate: the specification forbids enabling LIVE during development. Every other LIVE step works and is tested in-process against a simulated REAL account; none was performed on a real LIVE account.
* **No automatic updater, and the installer is not code-signed.** Both need your decisions: release hosting, an update-signing key and, ideally, a code-signing certificate. Updating means installing the newer installer (data is kept). Windows SmartScreen may warn.
* **Not run on a real Windows PC with a real broker during development.**
  * The engine was tested on Linux Docker, the connector against a simulated MT5 terminal.
  * The installer was tested on GitHub's Windows runner, without Docker Desktop or MT5.
  * **Acceptance testing on your PC, with a DEMO account, is the next step.**
* **Remote engine mode** is only a setting; local mode is the supported mode.
* **Data-encryption keys** (broker passwords, AI keys, model signatures, backups) cannot be rotated yet.
* **History is not pruned.** The database grows with use (about 1.2 GB per 40 days for 50 symbols in our measurement).
* **Upgrade testing** used a reinstall of the same version over modified files, because no earlier release exists.

### Upgrading from the Ubuntu deployment

See the [migration manual](../MIGRATION_FROM_UBUNTU.md). Keep the server until you accept the PC.

### Documentation

[Installation](INSTALLATION_MANUAL.md) · [User](USER_MANUAL.md) · [Administrator](ADMINISTRATOR_MANUAL.md) ·
[Architecture](ARCHITECTURE.md) · [Security](SECURITY.md) · [Migration](../MIGRATION_FROM_UBUNTU.md) ·
[Recovery](RECOVERY_MANUAL.md) · Phase reports `docs/RR_TRADING_ONE_PHASE*_REPORT.md`.
