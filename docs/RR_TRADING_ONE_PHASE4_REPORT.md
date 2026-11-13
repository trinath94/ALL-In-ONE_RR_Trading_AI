# RR Trading One — Phase 4 Report

**Phase:** 4 — native Windows MT5 connector integration
**Date:** 2026-11-13
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

RR Trading One now links, starts, supervises and stops the existing MT5 connector as a native user-session process.
The connector reaches the local engine over the loopback relay, attaches only to an explicitly selected terminal and
account, and the app shows its live state in a new MT5 Manager.

Phase 1 **gap 7.4 is closed.** The learner and the scanner can no longer trade, and this is enforced
cryptographically by the original connector on the PC.

| Result | Value |
|---|---|
| Phase 4 end-to-end suite: real Docker stack + real headless connector (FakeMT5 test launcher) over the loopback relay, driven by the app's supervisor code | **41 / 41 passed** (final clean run) |
| Backend suite (Python 3.12, lock), including the 215-strategy preservation test | **183 passed** (172 + 11 new) |
| Original MT5 connector suite | **17 passed** (10 original + 7 new) |
| Windows connector suite | **36 passed** (24 original + 12 new) |
| Desktop core (Rust) | **83 passed**. Clippy `-D warnings` clean (all targets). Windows type-check clean. |
| Control Centre UI under the production CSP | Passed: MT5 Manager renders live status; login masked everywhere; injected HTML rendered as text |
| Real desktop app (virtual display) | MT5 Manager showed a linked, running connector with every spec Part 6 field (section 3) |
| Phase 3 CI (`c55dcce`) | Passed: desktop run 43412803479 (Windows build + NSIS), backend run 43412803742 |

## 2. What was built

### 2.1 Role-scoped bridge credentials (gap 7.4)

| Layer | Change |
|---|---|
| Original connector (`mt5-connector/connector/app.py`) | `ConnectorConfig.readonly_secret` (optional). `_verify` now returns which credential signed the request. A read-only signature is accepted only for `GET` and `POST /calc/margin`; `POST /executions` gets **403** before anything reaches MT5. **Without a read-only secret, behaviour is byte-for-byte the original** (tested). |
| Desktop connector (`windows-connector`) | `register` stores the read-only secret, or removes a stale one. `ConnectorManager` passes it to the original connector app. |
| Engine | Registration issues a second secret, encrypted with a separate key `RR_BRIDGE_READ_KEY` (new nullable column, **migration 0004**, additive). `RR_BRIDGE_SCOPE=read` makes `BridgeClient` sign with it, refuse `submit()`, and **fail closed** in direct mode or for an agent without a read credential. |
| Local stack | `analysis` and `learner` run read-scoped and **without `RR_ENCRYPTION_KEY`**, so they cannot decrypt the trading secret. `api`, `monitor` and `trading-worker` keep full scope. CI checks the resolved compose environment. |
| Stack secrets | Format 2 adds the read key. An existing format-1 file is **upgraded in place, keeping every existing value**. This was verified on the real Phase 3 secrets file: the DB password was unchanged. |

### 2.2 Headless connector (`windows-connector/rr_connector/headless.py`, new)

It reuses `ConnectorManager` unchanged, and through it the original connector code and the PC-side guard. Command line:
* `--headless PROFILE`
* `--register-stdin URL`: the token is read from stdin and never appears in a process list
* `--detect-json`

**Explicit profile.** The profile names the terminal exe, broker server and **expected login**. Another logged-in
account gives `PROFILE_MISMATCH`: the connector disconnects and never trades on it. Logins are masked everywhere,
including in messages.

**Isolated instance.**
* Data lives in `%LOCALAPPDATA%\RR Trading One\connector`.
* Credentials live under a separate Credential Manager service, so a standalone connector linked to a remote engine is never overwritten (tested).

**Supervision files**
* `status.json`: atomic, every second, no secrets;
* `stop.request`: graceful stop;
* `instance.lock`: an OS-held single-instance lock.

**No auto-pick.** The manager's own auto-connect is disabled, because it could otherwise choose a terminal by itself.

### 2.3 Token issuing (`backend/app/services/agent_tokens.py`, new)

* One `issue()` function is shared by the existing API route (refactored to call it) and a CLI.
* The local supervisor runs that CLI inside the `api` container, with fixed argv.
* Tokens: 10-minute lifetime (maximum 60), stored as hashes only, audited, single use (tested).

### 2.4 Desktop: supervision and MT5 Manager

**Connector control (`desktop/core/src/connector.rs`)**
* **Launch discovery:** the packaged `mt5-connector\RR_MT5_Connector.exe`, or an absolute dev override.
* **Profiles:** `mt5_profiles.json` in `%APPDATA%`. They are validated, unknown fields (such as a password) are rejected, there are at most 20, and **nothing is selected implicitly**.
* **Start:** a detached spawn in the user session (never a service, never Session 0). It waits for its own PID to report status.
* **Stop:** graceful, through `stop.request`. Force-termination happens **only** when `status.json` proves the PID is our live connector, so a recycled PID is never killed.
* **Linking:** refused while the connector runs (see 6, defect 2).

**Supervisor integration**
* Start All, Stop All, Start/Stop MT5 Connector and Recovery now drive the real connector.
* An **unlinked connector or no selected profile is reported as *skipped*** (setup incomplete), not as a failure.
* Stop plans stop the trading worker before the connector.
* System Health always reports the connector's state, and treats it as a failure only when the plan started it.

**App**
* Six new IPC commands: `mt5_overview`, `mt5_detect`, `mt5_save_profile`, `mt5_select_profile`, `mt5_delete_profile`, `mt5_link`.
* Like all others, they are granted only to the local Control Centre window.
* The MT5 Manager panel shows every spec Part 6 field, with the login masked.

### 2.5 Worker roles

The roles reconnect when the registered agent **or its credentials** change. They compare a fingerprint of the stored
ciphertext, never decrypting anything (see 6, defect 2).

## 3. End-to-end verification (real Docker stack, this environment)

**How it ran:** everything went through the app's supervisor code (`examples/supervise`), with the same calls the
MT5 Manager makes. The connector was the **real** headless code; only the MetaTrader5 package was replaced by FakeMT5,
via a test-only launcher (`windows-connector/tests/fake_connector_entry.py`, never shipped).

**Start-up and linking**
* **Upgrade path:** Start All on the existing Phase 3 data.
  * The secrets upgraded from v1 to v2 with the DB password unchanged.
  * A `pg_dump` ran, then migration **0004** applied.
  * The unlinked connector was reported as *skipped* with guidance.
* **Isolation:** `analysis` and `learner` hold no `RR_ENCRYPTION_KEY` and run read-scoped (checked inside the containers).
* **Linking:** the token was issued in `api`, passed on stdin and consumed (single use). The agent was stored with an encrypted read-only secret.
* **Native start:** the connector ran as a host process and reported **CONNECTED** with masked login, DEMO and balance. The engine link was CONNECTED over the loopback relay.
* **Re-linking while running** was refused.

**Monitor (full scope)**
* Heartbeat connected; **startup checks passed** (identity, positions, journal).
* An account snapshot synced from MT5.
* **Symbols were discovered dynamically** through the connector.

**Read scope, in the real containers**
* `analysis` and `learner` read health and rates through the read credential.
* `submit()` was refused by the client.
* A hand-forged `POST /executions` was refused **by the connector on the PC with 403**.
* No position was opened.
* `analysis` completed scans through the read credential.

**Stopping and safety**
* System Health included "MT5 connector running".
* Stop MT5 Connector was graceful: the process exited and the monitor reported MT5 disconnected.
* **Wrong account in the terminal:** `PROFILE_MISMATCH`, disconnected, both logins masked in the message.
* Start All with a linked connector started it. Stop All stopped the connector and every container, and the volumes were kept.

**No leaks**
* No registration token in the supervisor log or reports.
* No stack secret in the supervisor log.
* No connector credential in its status file.

**Real app:** the MT5 Manager showed:
* the connector running and CONNECTED, with the profile ("expects •001");
* the terminal path, broker connection ON and Algo Trading ON;
* broker, masked account, server and DEMO mode;
* balance and equity, free margin and leverage;
* the engine link at 5.7 ms and the last heartbeat;
* the PC LIVE lock "locked" and the emergency stop "off".

## 4. Files

**Created**
* `backend/app/services/agent_tokens.py`
* `backend/migrations/versions/0004_bridge_readonly_secret.py`
* `backend/tests/{test_bridge_scope,test_agent_tokens}.py`
* `mt5-connector/tests/test_readonly_scope.py`
* `windows-connector/rr_connector/headless.py`
* `windows-connector/tests/{test_headless,fake_connector_entry}.py`
* `desktop/core/src/connector.rs`
* `desktop/ui/mt5.js`
* this report

**Modified original files.** All changes are additive or behaviour-preserving, and each is covered by tests:

| File | Change |
|---|---|
| `mt5-connector/connector/app.py` | Optional read-only secret, scope check |
| `windows-connector/rr_connector/{agent,manager,app,vault}.py` | Store and pass the read secret; headless dispatch; vault service name parameter (default unchanged) |
| `backend/app/core/{config,security}.py` | `bridge_read_key`, `bridge_scope`; read-key encryption helpers |
| `backend/app/db/models_agent.py` | Nullable column |
| `backend/app/api/routes/mt5_agent.py` | Issue the read secret when a read key is configured; token route calls the shared `issue()` |
| `backend/app/execution/{relay,bridge}.py` | Scope-aware agent resolution; read-only client |

**Modified Phase 2–3 files**
* `backend/app/workers/roles.py`
* `backend/tests/test_roles.py`
* `infrastructure/{compose.yml,README.md}`
* `desktop/core/src/{actions,compose,local,runner,secrets,supervisor,lib}.rs`
* `desktop/core/examples/supervise.rs`
* `desktop/src-tauri/{src/lib.rs,build.rs,capabilities/main.json}`
* `desktop/ui/{index.html,app.js,style.css}`
* `desktop/README.md`
* `.github/workflows/backend.yml`
* `docs/RR_TRADING_ONE_PHASE3_REPORT.md`: CI results

**Preserved:** all 215 strategies (preservation test passes), the risk engine, the execution manager and the
original worker. Direct-mode (Ubuntu) behaviour is unchanged by default.

**Database:** migration `0004` adds the nullable `mt5_agents.bridge_readonly_secret_enc`. The downgrade drops only that column.

## 5. Commands executed (main ones)

```
pytest -q (backend, Python 3.12.3 lock)            → 183 passed
pytest -q (mt5-connector)                           → 17 passed
pytest -q (windows-connector, Qt offscreen)         → 36 passed
cargo test -p rr-one-core --locked                  → 83 passed
cargo clippy --workspace --all-targets -D warnings  → clean
cargo check -p rr-one-core --target x86_64-pc-windows-msvc --all-targets → clean
scratchpad e2e4.sh (41 checks, real stack)          → 41 passed
Playwright UI check (production CSP)                → passed
real app under Xvfb                                 → MT5 Manager screenshot verified
```

## 6. Defects found by verification (all fixed, with regression tests)

| # | Defect | Found by | Fix |
|---|---|---|---|
| 1 | The first status after a connect reported the **stale** pre-connect state (`DISCONNECTED`) | Real stack | Snapshot taken after the connect attempt. The regression test **fails without the fix** (checked by reverting). |
| 2 | **Re-linking while the connector runs rotated its credentials but the running process kept the old ones**, so new engine bridges failed with "bad signature" (the discovery API returned 500). The roles only reconnected on a changed agent **id**, so after a re-link they would keep a stale secret. | Real stack (second run) | Linking refused while running. Roles reconnect on a credential fingerprint (SHA-256 of the stored ciphertext; nothing decrypted). |
| 3 | An unlinked connector made Start All **fail** and skip the learner on first run | Design review before the e2e | Typed `StartError::NotLinked`, reported as skipped with guidance |
| 4 | System Health did not report the connector unless the plan had started it | Real stack | Always reported; it is a failure only when expected |
| 5 | "Messages" showed the same text twice | Real-app screenshot | De-duplicated |

Test-harness errors that were mine, not product defects, are listed for completeness:
* the probe's import path;
* a too-strict 403 match;
* a re-run that assumed an unlinked connector.

## 7. Not verified here / known limitations

* **Not tested with a real MetaTrader 5 terminal or real Windows.** The MetaTrader5 package was replaced by FakeMT5 in every connector test. Not yet exercised:
  * terminal detection on a real PC;
  * the packaged `RR_MT5_Connector.exe --headless` under PyInstaller (Phase 16);
  * `OpenProcess`/`TerminateProcess` and the Windows file lock at runtime. They are type-checked only.
* **LIVE authorisation is not exposed in RR Trading One yet.** The connector's PC-side LIVE lock therefore stays **locked**, and a LIVE account's trading commands are refused on the PC. This is the safe default until the DEMO/LIVE phase. The standalone connector UI can still authorise LIVE for its own instance.
* **The connector does not start at Windows sign-in**; the desktop app starts it. Auto-start belongs with the installer (Phase 16).
* **Pre-existing:** `POST /api/symbols/discover` returns a generic 500 when the bridge rejects a request. It is out of scope here and noted for Phase 6 (symbol discovery).
* **Sandbox build workaround.** Docker Hub rate-limited base-image lookups (HTTP 429). The final images were built in this environment by layering the changed backend source onto the locally built Phase 3 image (the dependency lock is unchanged). The committed Dockerfile is unaffected, and CI builds normally.

## 8. Security implications

* **The learner and the scanner cannot place orders.** This holds three independent ways: the client refuses, the PC connector refuses (403), and they cannot decrypt the trading secret. It closes spec safety condition 1 structurally, not by convention.
* **Relay queue access.** Read-scoped containers can still see signed write requests in the Redis relay queue. They cannot alter one (HMAC over method, path and body) or replay one (single-use nonce in the connector journal, 30 s skew). The worst they can do is delay or drop requests (denial of service). Noted for the security document.
* **Linking tokens** never appear in argv, logs or reports (tested). They are single-use and short-lived.
* **Logins** are masked in every status, message and UI table. Profiles reject any extra field.
* **Force-termination** is limited to a PID proven live by our own status file.

## 9. Rollback

* **Revert the Phase 4 commits.** Migration `0004` downgrade (`alembic downgrade 0003`) drops only the new column.
* **Without reverting:** remove `RR_BRIDGE_SCOPE`/`RR_BRIDGE_READ_KEY` from the compose environment and every service returns to full scope. Agents keep working with their trading secret.
* **Connector data** is isolated in `%LOCALAPPDATA%\RR Trading One\connector`. Deleting it plus its Credential Manager entries (service "RR Trading One Connector") un-links the PC.

## 10. Next phase

**Phase 5 — local backend and frontend integration**
* live safety state in the Control Centre (replacing shipped defaults);
* first-run admin creation;
* engine account setup from the app;
* the MT5 profile tied to the engine account.
