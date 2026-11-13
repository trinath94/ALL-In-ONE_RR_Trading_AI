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

**Phase 2 status:** plans are validated and displayed, and System Health runs real probes. Starting and stopping services is implemented in **Phase 3**. Until then the UI says plainly that nothing was executed.

## Locations

| Purpose | Path |
|---|---|
| Install (read-only, no secrets) | `C:\Program Files\RR Trading One\` |
| Data: backups, logs, models, datasets, cache | `C:\ProgramData\RR Trading One\` |
| Per-user settings (`settings.json`, no secrets) | `%APPDATA%\RR Trading One\` |
| PostgreSQL and Redis | Docker named volumes, kept on upgrade and uninstall |
| Secrets | Windows Credential Manager / DPAPI |

`ProgramData\RR Trading One\secrets` is reserved. It is created in Phase 3 together with its restricted ACL, never world-readable.

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
