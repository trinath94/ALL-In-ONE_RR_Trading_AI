# RR Trading One — Phase 5 Report

**Phase:** 5 — local backend and frontend integration
**Date:** 2026-11-13
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

The desktop app is now wired to the running engine:
* It shows the **live** safety state instead of shipped defaults.
* It sets up the first administrator.
* It registers the connector's **DEMO** account in the engine with one click, accepted only when the connector itself reports that account.

The existing web UI no longer contacts Google at runtime: its fonts are bundled.

| Result | Value |
|---|---|
| Phase 5 end-to-end on a **fresh install** (new volumes, new user profile, real stack, real headless connector with FakeMT5) | **32 / 32 passed** (final run) |
| Backend suite, including the 215-strategy preservation test | **194 passed** (183 + 11 new) |
| MT5 connector / Windows connector suites | **17 / 36 passed** |
| Desktop core (Rust) | **88 passed**. Clippy `-D warnings` clean. Windows type-check clean. |
| HMAC interoperability | Rust and Python produce the identical signature for a shared test vector (asserted in both suites) |
| Frontend `next build` (strict TS) | Passed, 24 pages, **0 references to Google Fonts**, 20 bundled `.woff2` files |
| Control Centre UI under the production CSP | Passed: first-run card, live badges (incl. kill switch), masked account; injected HTML rendered as text |
| Real desktop app | Screenshot verified: live safety state from the engine (section 3) |

## 2. What was built

### 2.1 Engine: local supervisor API (`backend/app/api/routes/local.py`, new)

**Authentication**
* Disabled (404) unless `RR_SUPERVISOR_KEY` is set. **Only the `api` container receives it** (CI-checked); monitor, analysis, learner and trading-worker do not.
* HMAC-SHA256 over `RRLOCAL\n{METHOD}\n{path}\n{ts}\n{nonce}\n{sha256(body)}`.
* ±60 s timestamp window and single-use nonces (Redis).

| Endpoint | Behaviour |
|---|---|
| `GET /status` | Mode, auto trading, kill switch and reason, risk lock, pause, startup checks, active account (login masked), approved LIVE accounts, connector (online, masked login, DEMO/LIVE, PC LIVE lock, PC emergency stop), symbol counts, role liveness. No secrets. |
| `POST /bootstrap-admin` | Creates the first admin **only while there are no users**. Serialised with an advisory lock. Existing password rules apply (10+ characters, Argon2id). Audited. |
| `POST /demo-account` | Registers (if needed) and activates the account **only if an online connector reports exactly that login and server as `DEMO`**. It refuses an offline connector, a different account, a LIVE account, and any existing LIVE registration. Activation re-arms the startup checks and leaves auto-trading OFF. Audited. |

**Nothing in this API** can trade, enable auto-trading, change risk settings, approve or switch to LIVE, or change
permissions.

**Refactor:** account creation and activation now live in `services/trading.py` (`create_account_record`,
`activate_account`). The existing API routes call them with identical behaviour; the existing tests pass.

### 2.2 Desktop

**Core**
* `core/src/engine.rs`: signed loopback client. `health.rs` gains a general loopback request with header validation (no CR/LF injection).
* Stack secrets **format 3** adds the supervisor key. Older files are upgraded in place, keeping every existing value (format 1 and format 2 upgrades both tested).

**New IPC commands** (`engine_status`, `engine_bootstrap_admin`, `engine_use_demo_account`), granted only to the
Control Centre window. The DEMO command uses the selected profile, and requires the connector to be running and
CONNECTED.

**UI** (`ui/engine.js`)
* **Live safety panel and badges**, refreshed every 5 seconds. If the engine is unreachable, the panel says so and shows the shipped defaults, labelled as not live.
* **First-run administrator card**, shown only while the engine has no users. Password fields are cleared after each attempt.
* **"Use as engine DEMO account"** in the MT5 Manager.

### 2.3 Frontend: bundled fonts

* `@fontsource/jetbrains-mono` and `@fontsource/orbitron` 5.3.0 (OFL-1.1, pinned) replace the runtime Google Fonts links in `app/layout.tsx`, with the same families and weights.
* The local gateway's CSP drops Google entirely (`font-src 'self'`). The Ubuntu nginx is unchanged.
* `package.json` changes are only the two additions; npm re-sorted the keys, and no existing version changed.

## 3. End-to-end verification (fresh install, this environment)

**Fresh install:** data volumes and the user profile were removed first. Then Start All:
* stack secrets were **created** as format 3;
* schema migrated from scratch to `0004`;
* the connector was skipped because no profile was selected.

**API protections**
* An unsigned request and a wrong key were rejected (401).
* The monitor, analysis and learner containers don't hold the supervisor key.

**Live state and first administrator**
* **Initial state:** no admin; DEMO; auto OFF; kill switch off; monitor, analysis and learner running; trading worker stopped.
* **Administrator:**
  * A weak password was refused.
  * The administrator was created (admin role, Argon2id hash) and signs in through the gateway.
  * **A second bootstrap was refused.**
  * The bootstrap was audited, and the password never appeared in the supervisor log.

**DEMO account from the profile**
* **Refused before the connector reported it.**
* Then: link, start the connector (CONNECTED), heartbeat reached the engine.
* **The DEMO account was registered and activated from the connector's report** (login masked).
* The monitor **passed startup checks** for the new account.
* Live state showed the active account and the online connector. Auto trading stayed OFF and no order was placed.

**Read-scoped learner after linking:** manually triggered `trade_sync`, `portfolio` and `failure_analysis` all completed
(`DONE`) through the read-only credential.

**Browser:** the engine UI made 11 requests, **all to `127.0.0.1:47810`**. JetBrains Mono and Orbitron loaded from
the bundle. The CSP sets `font-src 'self'`.

**Real app:** the safety panel showed "Live values from the engine":
* DEMO, auto OFF, worker STOPPED, kill switch and risk lock off, startup checks passed;
* active account `DEMO •001 @ Demo-Server (verified)`;
* connector online, LIVE locked on the PC;
* role liveness, and symbol counts (0, correctly, since discovery wasn't run).

## 4. Files

**Created**
* `backend/app/api/routes/local.py`
* `backend/tests/test_local_api.py`
* `desktop/core/src/engine.rs`
* `desktop/ui/engine.js`
* this report

**Modified**
* `backend/app/main.py`: include the router
* `backend/app/core/config.py`: `supervisor_key`
* `backend/app/services/trading.py`: shared account functions
* `backend/app/api/routes/trading.py`: calls them; one unused import removed
* `infrastructure/compose.yml`: key for `api` only
* `infrastructure/nginx.local.conf`: CSP
* `infrastructure/README.md`
* `.github/workflows/backend.yml`: key-holder check
* `desktop/core/src/{secrets,local,health,supervisor,lib}.rs`
* `desktop/core/examples/supervise.rs`
* `desktop/core/Cargo.toml`: `hmac`, `sha2`
* `desktop/src-tauri/{src/lib.rs,build.rs,capabilities/main.json}`
* `desktop/ui/index.html`
* `desktop/README.md`
* `frontend/app/layout.tsx`
* `frontend/package.json`, `frontend/package-lock.json`

**Database:** no schema change. **Strategies:** unchanged (preservation test passes).

## 5. Defects and lessons found by verification

| # | Finding | Fix |
|---|---|---|
| 1 | **My test reset was wrong.** `docker compose ps` needs the stack secrets, so it found no containers and the old volumes survived. The resulting state (old data, brand-new secrets) made PostgreSQL reject the password; the supervisor correctly refused to migrate without a backup. | Reset now selects containers by compose project label. **Product lesson:** losing `stack.secrets` while keeping the data locks the database. This must be covered by the backup and recovery tooling (Phase 17). |
| 2 | The first screenshot showed the **old** UI: Tauri embeds the UI at compile time, and the app had been built before the UI edits | Rebuilt and re-verified. Always rebuild the app after UI changes before visual checks. |
| 3 | The CLI prints JSON with sorted keys (`serde_json`); a check compared a key-ordered string | The check now compares dicts |
| 4 | Learner jobs that need the connector failed at start-up, before linking. They are scheduled every ≥15 min, so they had not retried yet. | Verified by triggering them after linking: all `DONE`. Added to the suite. |

## 6. Not verified here / known limitations

* **No real Windows or MetaTrader 5 yet:** WebView2, DPAPI at runtime outside CI, and real MT5 are unverified (unchanged from Phase 4).
* **LIVE setup** is intentionally not available from the desktop. LIVE accounts still go through the engine's existing administrator approval with typed confirmation. The PC LIVE lock stays locked.
* **The frontend has no CI job** (`next build` was verified here only). Adding one is planned for Phase 15 (dashboard work).
* **Sandbox image builds** layered the new backend source and the new frontend build onto the existing local images (Docker Hub rate limit). The frontend build was placed in a fresh directory inside the image so no previous file could be served. The committed Dockerfiles are unchanged.
* **Lost `stack.secrets` with kept data locks the database** (see 5.1). Recovery tooling is Phase 17.

## 7. Security implications

* **New authenticated surface:** three endpoints, reachable only through the loopback gateway. They are HMAC-signed with a key that only the desktop user (DPAPI) and the `api` container hold, protected by single-use nonces and a ±60 s window, and disabled entirely without the key.
* **Bootstrap** is one-time and serialised, and reuses the engine's own password rules and hashing.
* **DEMO account registration** trusts only the connector's own heartbeat report, never typed input, and can never create or modify a LIVE account.
* **Privacy:** the engine UI no longer contacts Google on every load (verified by recording network requests).

## 8. Rollback

* Revert the Phase 5 commits.
* **Disabling the local API without reverting:** remove `RR_SUPERVISOR_KEY` from the `api` environment. The API then answers 404, and the desktop shows the shipped defaults labelled "engine not reachable".
* The secrets format-3 field is ignored by older builds, because they do not read it.

## 9. Next phase

**Phase 6 — dynamic broker symbol discovery:**
* new symbols default to **scan disabled** (Phase 1 gap 7.5);
* full symbol specification capture, including honest "not provided" for trading sessions;
* user controls for scan scope and priority;
* fix the pre-existing 500 when discovery hits a bridge rejection.
