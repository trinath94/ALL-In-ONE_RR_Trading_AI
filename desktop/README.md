# RR Trading One — desktop shell

The Windows desktop application (Tauri 2) that manages the RR Trading Engine locally. It is a **shell and
supervisor**. The trading, risk, learning and execution logic stays in `backend/`, unchanged and not duplicated.

```
desktop/
  core/        rr-one-core: allowlisted actions, Windows paths, settings, loopback health probes (pure Rust, tested)
  src-tauri/   the app: windows, tray, single instance, IPC commands, permissions
  ui/          the local Control Centre page (static HTML/CSS/JS, strict CSP, no inline script)
```

## Windows and trust boundaries

| Window | Loads | IPC |
|---|---|---|
| `main`: Control Centre | bundled `ui/` | Only the five commands granted in `capabilities/main.json` |
| `console`: Trading Console | the existing Next.js UI at `http://127.0.0.1:<gateway_port>` | **None.** It is a remote origin, and navigation is pinned to that origin. |

The console opens only after the engine passes its real health check. That check is `GET /api/health`, which also verifies PostgreSQL and Redis.

## Service manager

Actions are a closed allowlist (`core/src/actions.rs`): Start/Stop/Restart All, Start/Stop Backend, Start/Stop MT5 Connector, Start/Stop Learner, Start/Stop Trading Worker, System Health and Recovery.

Each action expands into a fixed plan of typed steps. No variant carries a command line, so nothing typed by a user, a web page or an AI provider can become a shell command.

* **Start All never starts the trading worker.** Starting it requires typing `START TRADING WORKER`, and the engine's own auto-trading switch, RBAC and Risk Engine still apply.
* **Every stop plan stops the trading worker first.**
* **No action can switch to LIVE, enable auto-trading or change permissions.** Those remain engine operations.

**Execution (Phase 3).** `core/src/supervisor.rs` runs each step as a fixed Docker Compose argument vector against `infrastructure/compose.yml`, with no shell involved.
* **Start plans** stop at the first failure, and the remaining steps are reported as skipped.
* **Stop plans** are best-effort.
* **Migrations** never run without a fresh `pg_dump`.
* **Health** is verified from Compose health states plus the engine's `/api/health`.
* **The native MT5 connector** is a user-session process (see MT5 Manager below). Its steps are skipped, not
  failed, while no profile is selected or the connector is not yet linked.

`cargo run -p rr-one-core --example supervise -- <action>` runs the same code from a terminal.

## MT5 Manager (Phase 4)

The existing `windows-connector` runs **headless** (`RR_MT5_Connector.exe --headless PROFILE.json`):
* as a normal process in the signed-in user's session, started by the desktop app;
* never as a service and never in Session 0;
* dialling out to the loopback gateway (`ws://127.0.0.1:47810`), so nothing listens on the PC.

It keeps all of the original connector's protections:
* the execution journal, idempotency, and HMAC and replay checks;
* the PC-side LIVE lock, lot/position/daily-loss caps and emergency stop.

**Profiles** (`%APPDATA%\RR Trading One\mt5_profiles.json`) name the `terminal64.exe`, the broker server and the
expected account login.
* Nothing is selected automatically.
* A terminal logged in to a different account is disconnected (`PROFILE_MISMATCH`), never traded.

**Linking**
* A one-time, 10-minute token is issued inside the local `api` container and passed to the connector on stdin.
* The engine issues the connector two credentials: a trading credential and a **read-only** one.

**Isolation**
* The connector's data lives in `%LOCALAPPDATA%\RR Trading One\connector`, with its own Credential Manager namespace.
* A standalone RR MT5 Connector linked to a remote engine is therefore never overwritten.

**Supervision files** in that folder:
* `status.json`: written every second, login masked, no secrets;
* `stop.request`: graceful stop;
* `instance.lock`: single instance.

**Force-terminate rule:** a process is terminated only if `status.json` proves the PID is our live connector.

## Engine integration (Phase 5)

The Control Centre reads the **live** safety state from the engine's local supervisor API. That state covers mode,
auto trading, kill switch, risk lock, startup checks, the active account (masked), LIVE approvals, connector and
roles. The API is HMAC-signed with the stack's supervisor key (`core/src/engine.rs`). While the engine is
unreachable, the panel says so and shows the shipped defaults.

* **First run:** while the engine has no users, a card creates the administrator. The password goes only to the
  local engine and is stored there as Argon2id. The engine refuses a second bootstrap.
* **DEMO account:** **Use as engine DEMO account** registers and activates the selected profile's account. The engine
  accepts it only if the online connector itself reports exactly that login and server as DEMO. Auto trading stays
  OFF, and the monitor re-runs its startup checks.

## Locations

| Purpose | Path |
|---|---|
| Install (read-only, no secrets) | `C:\Program Files\RR Trading One\` |
| Data: backups, logs, models, datasets, cache | `C:\ProgramData\RR Trading One\` |
| Per-user settings (`settings.json`, no secrets) | `%APPDATA%\RR Trading One\` |
| PostgreSQL and Redis | Docker named volumes, kept on upgrade and uninstall |
| Secrets | Windows Credential Manager / DPAPI |

`ProgramData\RR Trading One\secrets\stack.secrets` holds the local stack's generated secrets:
* DPAPI-encrypted (current user);
* the folder ACL is limited to SYSTEM, Administrators and the user;
* never regenerated over an unreadable file.

See `infrastructure/README.md`.

## Settings

`settings.json` rejects unknown fields, so secrets can't be stored in it by mistake.

**LOCAL mode is the default.** In LOCAL mode the engine address is derived as `http://127.0.0.1:<gateway_port>` (default `47810`) and cannot be set to another host. REMOTE mode is reserved for a future release and requires `https://`.

## Build and test

Linux needs the webkit2gtk-4.1 development packages. Windows needs WebView2, which ships with Windows 11.

```bash
cd desktop && cargo test -p rr-one-core
```

```bash
cd desktop && cargo build -p rr-trading-one
```

```bash
cd desktop/src-tauri && npx --yes @tauri-apps/cli@2.12.4 build
```

CI (`.github/workflows/desktop.yml`) runs the core tests on Linux and Windows, then builds the app and an **unsigned** NSIS package on `windows-latest`. Code signing and the full installer arrive in Phase 16.
