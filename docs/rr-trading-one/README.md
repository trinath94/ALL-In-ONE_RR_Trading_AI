# RR Trading One: documentation and deliverables

RR Trading One is the all-in-one Windows desktop application built on the RR Trading AI Engine (this repository).
The engine's original documents (`docs/ARCHITECTURE.md`, `docs/INSTALL.md`, `docs/RECOVERY.md`, ...) describe the
Ubuntu deployment and are kept unchanged. This folder documents the Windows application.

| Document | For |
|---|---|
| [Installation manual](INSTALLATION_MANUAL.md) | Installing, first run, upgrading, uninstalling |
| [User manual](USER_MANUAL.md) | Daily use: the desktop window, the Trading Console, DEMO trading |
| [Administrator manual](ADMINISTRATOR_MANUAL.md) | Users and roles, LIVE approval, AI providers, models, secrets, monitoring |
| [Technical architecture](ARCHITECTURE.md) | How the parts fit together, and why |
| [Security document](SECURITY.md) | Threat model, controls, keys, what is not protected |
| [Migration manual](../MIGRATION_FROM_UBUNTU.md) | Moving from the Ubuntu server |
| [Recovery manual](RECOVERY_MANUAL.md) | Backups, restores, failures and what to do |
| [Release notes](RELEASE_NOTES.md) | What this version contains, known limitations |
| [Test matrix](../TEST_MATRIX.md) | Every Part 22 area and safety condition, and the tests that prove it |
| Phase reports | `../RR_TRADING_ONE_PHASE1_REPORT.md` … `PHASE23_REPORT.md`: what each phase did, verified and could not verify |

## Part 25: required deliverables

| # | Deliverable | Status | Where |
|---|---|---|---|
| 1 | Complete Windows desktop source code | ✅ | `desktop/` (Tauri app, `rr-one-core` supervisor, local UI) |
| 2 | Tauri application | ✅ | `desktop/src-tauri` (Tauri 2, NSIS installer built in CI) |
| 3 | Existing Next.js dashboard integrated | ✅ | `frontend/`: the Trading Console, opened by the desktop app; 30 pages |
| 4 | Local FastAPI deployment | ✅ | `infrastructure/compose.yml` (`api`, roles), built from `backend/` |
| 5 | Local PostgreSQL | ✅ | PostgreSQL 16.6 container, named volume |
| 6 | Local Redis | ✅ | Redis 7.4 container, named volume |
| 7 | Native Windows MT5 connector | ✅ | `windows-connector/` (headless, supervised by the desktop app) and the original `mt5-connector/` code it runs |
| 8 | Process supervisor | ✅ | `desktop/core/src/supervisor.rs`: allowlisted actions, fixed plans |
| 9 | Multi-agent orchestration | ✅ | `backend/app/orchestration` (21 agents, run log, Control Room; 26 after Parts 27-30 Phase 4, 27 after Phase 5) |
| 10 | AI provider gateway | ✅ | `backend/app/ai` (Anthropic, OpenAI-compatible, Qwen, Ollama; routes, fallback, limits, ledger) |
| 11 | Existing and extended AI learning engine | ✅ | `backend/app/learning_engine`, `backend/app/intelligence` (signed models, walk-forward, isolated jobs) |
| 12 | Adaptive strategy intelligence | ✅ | `backend/app/learning_engine/adaptive.py` (shadow first, approval to apply) |
| 13 | Broker symbol discovery | ✅ | symbol catalogue (new symbols disabled until chosen) |
| 14 | Multi-symbol scanner | ✅ | `backend/app/scanner` (budgeted, once per closed bar, timeframe-aware) |
| 15 | Backtesting and optimisation | ✅ | `backend/app/backtesting`, `backend/app/optimisation` (original engine) |
| 16 | DEMO/LIVE account controls | ✅ | separate identities, approval, typed confirmations, revocation (Phase 16) |
| 17 | Risk Centre | ✅ | original Risk Engine and Risk Centre page |
| 18 | Windows installer | ✅ | NSIS, per-machine, built and installed/upgraded/uninstalled in CI (unsigned) |
| 19 | **Application updater** | ❌ **Not built: decision needed** | See below |
| 20 | Backup and restore tools | ✅ | Desktop Backup and Recovery: encrypted backups, restore, Ubuntu import, backup key |
| 21 | Automated test suite | ✅ | Engine 323, MT5 bridge 17, RR MT5 Connector 36, desktop 130 tests; matrix check in CI |
| 22 | Installation manual | ✅ | [INSTALLATION_MANUAL.md](INSTALLATION_MANUAL.md) |
| 23 | User manual | ✅ | [USER_MANUAL.md](USER_MANUAL.md) |
| 24 | Administrator manual | ✅ | [ADMINISTRATOR_MANUAL.md](ADMINISTRATOR_MANUAL.md) |
| 25 | Technical architecture document | ✅ | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 26 | Security document | ✅ | [SECURITY.md](SECURITY.md) |
| 27 | Migration manual | ✅ | [MIGRATION_FROM_UBUNTU.md](../MIGRATION_FROM_UBUNTU.md) |
| 28 | Recovery manual | ✅ | [RECOVERY_MANUAL.md](RECOVERY_MANUAL.md) |
| 29 | Release notes | ✅ | [RELEASE_NOTES.md](RELEASE_NOTES.md) |

### The updater (deliverable 19): why it is not built

**What is missing.** A safe automatic updater must verify that an update really comes from you before running it,
and it must download updates from somewhere. That needs three things only the owner can provide:
1. **Where releases are published.** A private GitHub repository cannot serve downloads to the app without a token, so the choice is a public release location (a public repository or a website).
2. **An update-signing key** (Tauri's updater signature). It is generated once, stored as a CI secret, and its public half is built into the app.
3. **Ideally, a Windows code-signing certificate** in your name, so Windows SmartScreen trusts the installer. This is optional but strongly recommended.

**Until then.** Updating means installing the newer installer over the existing one. Windows CI verifies that this
replaces the program files and keeps every setting, backup and database (Phase 22).

## Part 24: development phases

The specification's 17 development phases were delivered as 23 numbered phases (some split, some combined). Each
has a report in `docs/`.

| Specification phase | Delivered in |
|---|---|
| 1 Repository audit and baseline verification | Phase 1 |
| 2 Windows desktop shell and application structure | Phase 2 |
| 3 Local infrastructure and process manager | Phase 3 |
| 4 Native Windows MT5 connector integration | Phase 4 |
| 5 Local backend and frontend integration | Phase 5 |
| 6 Dynamic broker symbol discovery | Phase 6 |
| 7 Original strategy library integration | Phase 7 |
| 8 Multi-symbol and multi-timeframe scanning | Phase 8 |
| 9 Multi-agent orchestration | Phase 9 (+ Phase 14 AI Studio) |
| 10 Historical data and trade intelligence | Phase 10 |
| 11 AI learning and model validation | Phase 11 |
| 12 Adaptive strategy intelligence | Phase 12 |
| 13 Signal fusion and predictive intelligence | Phase 13 |
| 14 Risk and controlled execution integration | Phases 15–16 (trading worker; DEMO/LIVE separation) |
| 15 Complete Windows dashboard | Phase 17 |
| 16 Installer and updater | Installer: Phase 8 and Phase 18 (structure), Phase 22 (upgrade test). **Updater: not built (see above).** |
| 17 Migration, recovery and acceptance testing | Phases 18, 19, 21, 22, 23 |

The specification's Parts 19–23 (local/remote architecture, performance, security, testing, migration) are Phases
19–23.

## Part 26: implementation reporting

Every phase report states:
* the files created and modified, the original files preserved, and database and migration changes;
* the tests and commands run, with their real results, including runs that failed and why;
* known limitations, security implications, rollback, and the next phase.

Where a report said something worked and a later phase found otherwise, the earlier report carries a dated
correction. Examples: Phase 17's restore of older backups; the desktop permission list for the Phase 17/18 commands.
