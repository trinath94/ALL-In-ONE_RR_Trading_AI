# RR Trading One — Phase 2 Report

**Phase:** 2 — Windows desktop shell and application structure
**Date:** 2026-11-13
**Branch:** `claude/focused-cori-b8em6u`
**Decisions applied (from Phase 1):**

| Decision | Applied |
|---|---|
| D1 | Split the worker into roles: monitor, analysis and trading worker. The trading worker is stopped by default. |
| D2 | Docker Desktop + WSL2 |
| D3 | The Tauri window loads the existing UI from a loopback gateway |
| D4 | The MT5 connector uses the existing relay over loopback |
| D5 | Anything not verified is marked as not verified |

## 1. What was delivered

| Item | Where | Status |
|---|---|---|
| Pinned backend dependencies (fixes gap 7.10) | `backend/requirements.lock` | ✅ Verified: fresh Python **3.12.3** venv from the lock → backend **155 passed**, including preservation |
| Undeclared test dependency (fixes gap 7.9) | `backend/requirements-dev.txt` | ✅ Now declares the `windows-connector` requirements the suite imports |
| Desktop workspace | `desktop/` (Tauri 2.12, Rust 1.97) | ✅ Builds on Linux; Windows build via CI (see 5) |
| Supervisor core: allowlisted actions, paths, settings, health probes | `desktop/core` (`rr-one-core`) | ✅ 31 unit tests |
| App shell: main window, tray, single instance, close-to-tray, IPC | `desktop/src-tauri` | ✅ Runs; verified under a virtual display |
| Control Centre UI | `desktop/ui` | ✅ Verified in the real app and under the production CSP |
| Trading Console window (existing Next.js UI) | `open_console` | 🟡 Opens only after a healthy engine check. Not yet exercised against the real engine stack (Phase 3/5). |
| CI | `.github/workflows/desktop.yml`, `.github/workflows/backend.yml` | 🟡 Added; results depend on GitHub Actions runs (see 5) |
| Service start/stop | — | ⛔ **Not in Phase 2.** Actions validate and display their plan, and the UI states "Not executed". Execution is Phase 3. |

## 2. Design (security-relevant points)

### Closed action allowlist
`core/src/actions.rs` defines the 13 specification actions as an enum. Parsing is exact-match only: no trimming, no case folding, no prefixes. Each action expands into a fixed plan of typed `Step` values. No variant carries a command line or free text, so a user, a web page or an AI provider cannot inject a shell command.

Tested invariants:
* **Start All, Restart All, Recovery, Start Backend, Start Learner and Start MT5 Connector never start the trading worker.**
* **Only Start Trading Worker starts it.** It requires the exact phrase `START TRADING WORKER` and verifies health first. The engine's `auto_trading` switch, RBAC and Risk Engine still apply on top.
* **Every stop plan stops the trading worker first.** Stop All stops PostgreSQL and Redis last.
* **No action concerns LIVE, auto-trading or permissions.** AI cannot enable LIVE through the desktop, because no such action exists.
* **The MT5 connector always runs natively in the user session.** It has no Docker service name, so it can never be scheduled in a container or in Session 0.

### Two windows, one trust boundary

| Window | Content | IPC |
|---|---|---|
| `main` | Bundled Control Centre | Five commands, granted explicitly in `capabilities/main.json` via a `tauri-build` app manifest |
| `console` | Existing engine UI at `http://127.0.0.1:<port>` | **None.** It is a remote origin, and navigation is pinned to that exact origin. |

The engine UI keeps its existing same-origin cookie and CSRF model (decision D3). No frontend change was needed.

### Paths

| Purpose | Path |
|---|---|
| Install | `C:\Program Files\RR Trading One` |
| Data | `C:\ProgramData\RR Trading One\{backups,logs,models,datasets,cache}` |
| Settings | `%APPDATA%\RR Trading One\settings.json` |

* Missing environment variables are errors, not guesses.
* Data or settings inside the install directory are rejected; the comparison is case-insensitive and separator-insensitive.
* `secrets\` is deliberately **not** created until Phase 3 can set a restricted ACL.

### Settings
* **No secrets possible:** unknown fields are rejected.
* **LOCAL is the default.** The local engine address is derived as `http://127.0.0.1:<port>` and cannot be configured, so local mode can never reach the old Ubuntu server, even if a remote URL is left in the file.
* **REMOTE requires `https://`** and is reserved for a later release.
* **Writes are atomic, and a corrupt file is reported and never overwritten.**

### Health
`/api/health` returns 200 `{"status":"ok"}` only after `select 1` on PostgreSQL and a Redis `PING`, so one probe covers the gateway, API, PostgreSQL and Redis.

The probe is limited to `127.0.0.1`, validates paths (no header injection) and caps responses at 64 KB. It distinguishes three states:
* **Healthy**
* **Unhealthy:** reachable, but a dependency failed or the response was wrong
* **Unreachable**

### UI
* Strict CSP: `script-src 'self'`, no inline script or style, `object-src 'none'`, `frame-ancestors 'none'`. `freezePrototype` is on.
* All dynamic text uses `textContent`.

## 3. Files

**Created**
* Backend: `backend/requirements.lock`
* CI: `.github/workflows/desktop.yml`, `.github/workflows/backend.yml`
* Workspace: `desktop/.gitignore`, `desktop/Cargo.toml`, `desktop/Cargo.lock`, `desktop/README.md`
* Core crate: `desktop/core/Cargo.toml`, `desktop/core/src/{lib,actions,paths,settings,health}.rs`
* App crate: `desktop/src-tauri/{Cargo.toml,build.rs,tauri.conf.json}`, `desktop/src-tauri/src/{main,lib}.rs`, `desktop/src-tauri/capabilities/main.json`, `desktop/src-tauri/permissions/autogenerated/*.toml` (generated by `tauri-build`), `desktop/src-tauri/icons/*` (original artwork)
* UI: `desktop/ui/{index.html,app.js,style.css,logo.png}`
* This report

**Modified**
* `backend/requirements-dev.txt` (3 lines added)

**Preserved unchanged**
* Everything under `backend/app`, `backend/migrations`, `backend/tests`, `frontend`, `mt5-connector` and `windows-connector`. The `git diff` for those paths is empty.
* `backend/requirements.txt` and `docker-compose.yml`. The production image will switch to the lock in Phase 3.

**Database changes:** none. **Migrations:** none.

## 4. Commands and results (this environment, Linux)

| Command | Result |
|---|---|
| `uv pip compile requirements.txt --python-version 3.12 … -o requirements.lock` | 59 pins, constrained to the versions verified in Phase 1 |
| Fresh 3.12 venv: `uv pip install -r requirements-dev.txt` and `pytest -q` | **155 passed** (pandas 3.1.1, numpy 2.5.4, SQLAlchemy 2.1.8) |
| `pytest tests/test_preservation.py` | **3 passed**: 215 IDs, config and code hashes unchanged |
| `cargo test -p rr-one-core --locked` | **31 passed** |
| `cargo clippy --workspace --all-targets --locked -- -D warnings` | Clean |
| `cargo build -p rr-trading-one --locked` | Builds (Tauri 2.12.5) |
| Real app under Xvfb, with a stand-in engine on `127.0.0.1:47810` | Started with no panic. Real IPC returned the overview, and the real Rust probes reported both checks **healthy**. It created only the five data directories (no `secrets`). Screenshot inspected. |
| Playwright: `ui/` served with the production CSP and mocked IPC | 0 console errors or CSP violations. An `<img onerror>` health detail rendered as text (no XSS). A wrong worker phrase was refused. Start All shows its plan and "Not executed". The console was refused while the engine was unhealthy. No horizontal overflow at 900 px. |

Tauri's only runtime warnings were the expected missing session bus for the tray icon and missing DRI3 on the virtual display. Both are headless-environment artifacts.

## 5. Not verified here (⛔ / 🟡)
* **Windows build: ✅ verified in CI** (GitHub Actions run 43406772448, `windows-latest`).
  * Core tests passed on Windows, and clippy `-D warnings` was clean.
  * The Tauri app and its **unsigned** NSIS installer built successfully.
  * The first run (43405985724) failed: two core tests used a host-dependent path check. That was fixed in `2d70d6e`, without weakening any test.
  * Backend CI run 43405985759 passed.
  * **Windows runtime** (launching the app on a real desktop, tray, WebView2) is still unverified.
* **Console window isolation.** It relies on Tauri's capability model (remote origins get no IPC unless listed) and on the navigation pin. Both are configured, but no automated test injects an IPC call from the console origin yet. Adding one is planned for Phase 3, alongside the real gateway.
* **The tray menu on Windows** has not been seen on a real desktop.
* **WebView2 install path:** `downloadBootstrapper`, which needs internet on first install. The offline installer is decided in Phase 16.

## 6. Known limitations
* **Service control is not implemented.** Every action except System Health is plan-only and says so.
* **The safety-state panel shows shipped defaults, not live engine values.** The UI states this. Live values come with engine integration (Phase 5).
* **REMOTE mode** is validated in settings but intentionally not operational.
* **The installer is not code-signed.** Signing is in Phase 16.
* **pandas 3 deprecation warnings** remain in the frozen strategy code. Fixing them would change strategy hashes, so pandas is pinned instead.

## 7. Security implications
* **Positive:**
  * There is no path from the UI, web content or AI to a shell.
  * The engine UI has no access to the supervisor.
  * Secrets are structurally excluded from the settings file and the install directory.
  * The health probe cannot be used to reach other hosts.
* **New surface:** five IPC commands on a local, bundled page. Their inputs are an allowlisted action id and an optional confirmation phrase.

## 8. Rollback
* **Remove the desktop shell:** delete `desktop/` and `.github/workflows/desktop.yml`. Nothing else depends on them.
* **Revert the dependency changes:** restore `backend/requirements-dev.txt` and delete `backend/requirements.lock`.
* The backend runtime is unaffected either way.

## 9. Next phase
**Phase 3 — local infrastructure and process manager.** It covers:
* a Windows compose profile bound to `127.0.0.1` (gap 7.12), with the production image installed from `requirements.lock`;
* compose services for the D1 roles (`monitor`, `analysis`, `trading-worker`) reusing the existing worker functions;
* the step executor (fixed argv to Docker Compose; no shell);
* Docker Desktop and WSL2 prerequisite checks, plus a restricted ACL for the `secrets` directory;
* a test that the console origin cannot invoke IPC.
