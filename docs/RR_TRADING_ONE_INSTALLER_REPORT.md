# RR Trading One — One-Click Installer Report

**Scope:** self-contained Windows installer (part of Phase 16, brought forward at the user's request)
**Date:** 2026-11-13
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

Before this change, the installer contained only the desktop app. Using it needed:
* a source checkout;
* an `RR_ONE_INFRA_DIR` variable;
* Python plus two connector variables.

**Now one installer, `RR Trading One_0.2.0_x64-setup.exe`, carries everything the app runs.**

| Carried by the installer | Used for |
|---|---|
| Desktop app (Tauri) | Control Centre, service manager, MT5 Manager |
| Engine sources: `backend\`, `frontend\`, `infrastructure\`, `docker\` | **Start All** builds the local engine images from them |
| Packaged MT5 connector `mt5-connector\RR_MT5_Connector.exe` | Built with the standalone connector's own PyInstaller spec; no Python needed |
| `BUNDLE_MANIFEST.txt` | SHA-256 of every installed engine and connector file, plus the source commit |

**Docker Desktop is the one prerequisite the installer cannot carry.** If it is missing, the installer **asks**
whether to install it with `winget`, under Docker's licence terms. It never installs it silently.

**After installing, the user's steps are:**
1. Open the app.
2. Start All.
3. Create the administrator.
4. Add the MetaTrader 5 profile.
5. Link and start the connector.

These are the same in-app steps as before; no terminal or environment variables are needed.

| Verification | Result |
|---|---|
| Installed-layout check here: the app's supervisor run from a folder laid out exactly like the installer's, with no source checkout, using real Docker | **17 / 17 passed** |
| Windows CI: build the installer, **silent install**, every file checked against the manifest, packaged connector stdin/stdout, the app's launcher from the install folder, **silent uninstall** | **Passed** (run 43503261385) |
| Desktop core tests | **89 passed** (88 + 1 new); clippy clean with `-D warnings` |
| Windows connector tests | 36 passed |
| Tauri 2.12 accepts the release config; resource layout inspected in a built package | Passed |

## 2. What changed

### 2.1 Installer contents (`desktop/scripts/stage_bundle.py`, `desktop/src-tauri/tauri.bundle.conf.json`)

**Staging.** `stage_bundle.py` copies only **git-tracked** files. It never copies `node_modules`, build output,
caches or local secrets, and it refuses to stage them even if they were tracked by mistake. It then:
* adds the packaged connector;
* checks that the required files are present;
* writes `BUNDLE_MANIFEST.txt`.

**Release-only config.** The resources are declared in `tauri.bundle.conf.json`, which is used only for release builds
(`tauri build --config tauri.bundle.conf.json`). Ordinary `cargo build`, clippy and tests need no staging.

**Build context.** `infrastructure/install.dockerignore` becomes `<install>\.dockerignore`. The install folder is the
Docker build context, and only `backend/` and `frontend/` are sent; this was verified. The app and connector binaries
never go to Docker.

### 2.2 Per-machine install (`tauri.conf.json`: `installMode: perMachine`, version 0.2.0)

The app installs to `C:\Program Files\RR Trading One\`, as the Phase 2 location table always said.

The previous build used Tauri's per-user default, `%LOCALAPPDATA%\RR Trading One`. That is **the same folder as the
connector's data**, so an uninstall could have removed connector data. Install and data are now separate:
* `ProgramData` for engine data;
* `%APPDATA%` for settings;
* `%LOCALAPPDATA%\RR Trading One\connector` for the connector;
* Docker volumes for the database.

### 2.3 Upgrades get the new engine (`desktop/core/src/local.rs`, `supervisor.rs`, `infrastructure/compose.yml`)

**Defect found.** Start All runs `docker compose up -d`, which builds an image only if it is missing. After installing
a newer version, the app would have **kept running the old engine images** with no warning.

**Fix**
* Images are tagged `${RR_ONE_IMAGE_TAG:-local}`.
* The supervisor derives the tag from the installed manifest (`b` + 12 hex digits of its SHA-256).
* A new version therefore has a new tag, and its images are built by the next Start All. Migrations still run with a backup first, and the data volumes are kept.
* A source checkout has no manifest, so development and every existing test keep `local`.

### 2.4 Installer hooks (`desktop/src-tauri/windows/hooks.nsh`, `infrastructure/windows/uninstall-stop.ps1`)

**Before an upgrade or uninstall**, the installer stops the running copy, in the app's own stop order:
1. The connector started from *this* install folder: a graceful `stop.request` first, then a forced stop after 20 s. A standalone RR MT5 Connector elsewhere is never touched.
2. The engine containers, **trading worker first**.

Nothing is removed: no containers, volumes, settings or data. A stop failure never blocks uninstalling.

**After install**, if Docker Desktop is missing, the installer asks Yes/No with an explanation:
* **Yes:** `winget install Docker.DockerDesktop`. If that fails, the Docker download page opens.
* **No, or a silent install:** a reminder to install it before Start All.

### 2.5 Packaged connector stdio (`windows-connector/rr_connector/headless.py`)

The connector is a windowed (GUI-subsystem) executable. The app passes it the one-time link token on stdin and reads
terminal detection and link results from stdout. A windowed Python build can start with these streams unset even when
the parent passes pipes, so `headless` now re-attaches any missing stream to the handle Windows passed. This is a
no-op elsewhere. CI verifies it on Windows with the real packaged executable (section 4).

## 3. Installed-layout check in this sandbox (17 / 17)

**Setup.** A folder was laid out exactly like the installer's, from the staged bundle, with the supervisor CLI (the
app's own service code) placed beside it. There was no source checkout, no `RR_ONE_INFRA_DIR`, no engine images and
no data.

**Results**
* The Docker build context, as filtered by the installed `.dockerignore`, held only `backend` and `frontend`.
* The prerequisite check found the compose file **inside the install folder**.
* The engine images built from the install folder alone, under the tag the app computed.
* **Start All succeeded.**
  * The services ran the install's images.
  * The engine was healthy through the gateway, and the login page was served.
  * The schema was at `0005`.
  * **215 strategies, 215 / 215 identical to the shipped baseline.**
  * Safe initial state: DEMO, auto-trading off, trading worker not running.
* **Upgrade simulation:**
  * the installed sources were changed, giving a new manifest and tag;
  * new images were built;
  * Start All ran them, with the new code present in the running API container;
  * **the administrator account (the data) was kept.**
* Stop All succeeded.

**Sandbox-only difference:** this sandbox intercepts TLS, so the two image builds used copies of the shipped
Dockerfiles that add its CA certificate. On a normal PC, Start All builds the images itself with the shipped
Dockerfiles.

## 4. Windows CI (`.github/workflows/desktop.yml`, `windows-latest`)

**Build**
1. Core tests and clippy.
2. Build the connector with PyInstaller.
3. Stage the bundle.
4. Build the NSIS installer.
5. Write checksums.

**Silent install and checks**
* Run the installer silently, then check the required files in `C:\Program Files\RR Trading One`.
* Confirm `node_modules` and `.next` are absent.
* Check **every manifest entry's SHA-256**.
* Packaged connector: `--detect-json` must answer valid JSON on stdout. `--register-stdin`, with the token on stdin and an unreachable engine, must report the error as JSON on stdout.
* The app's supervisor code, run from the install folder, must use the installed compose file. Its MT5 detection must work through the installed connector.

**Silent uninstall:** the app and connector must be removed.

The workflow now also runs when the engine, connector or infrastructure change, because the installer carries them.

**Result: passed.** Run 43503261385, commit `53299dc`. From its log:

```
installed to C:\Program Files\RR Trading One
detect exit 0: []                                     packaged connector answered on stdout (no MT5 on the runner)
register exit 1: {"ok": false, "error": "Cannot reach the RR AI Engine at http://127.0.0.1:9: [WinError 10061] ..."}
                                                      token read from stdin, error reported on stdout
created stack secrets in C:\ProgramData\RR Trading One\secrets
docker_installed True ... compose_file True C:\Program Files\RR Trading One\infrastructure\compose.yml
wsl2 True ... gateway_port True
mt5-detect via the app's launcher: []                 the app's own launcher code ran the installed connector
uninstalled
```

Every manifest entry matched its SHA-256 after installation. The installer artifact is 53.8 MB zipped (artifact
14075378596). The same files were rebuilt by run 43503488436 (documentation-only commit).

## 5. Not verified / limitations

* **No real PC install yet.** CI's runner has no Docker Desktop with Linux containers and no MetaTrader 5, so on Windows:
  * the image build and the connector's live trading path are not exercised;
  * the Docker Desktop prompt is not exercised (CI installs silently, which skips it).

  The Linux-side check covers the engine build and run. **Your first install is the first full real-world run.**
* **First Start All needs internet** and takes several minutes, to build the engine images (Python and Node packages). Publishing prebuilt images would remove this; that is planned with code signing in Phase 16.
* **Unsigned:** SmartScreen warns on first run. Code signing is Phase 16.
* **Old engine images are not pruned** after an upgrade (roughly 1–2 GB each). A cleanup action is planned.
* Docker Desktop's own setup (WSL 2, possible restart, accepting its terms on first open) is outside the installer's control.

## 6. Security implications

**No secrets in the installer.** Stack secrets are still generated on first run, DPAPI-encrypted, in
`ProgramData\...\secrets` with a restricted ACL. The staging script refuses to ship `.env` files.

**Install folder.** The install folder under Program Files can't be changed by a standard user, so the compose file and
engine sources Start All uses can't be swapped without administrator rights. Each install ships a SHA-256 manifest.

**Installer hooks**
* Their commands are fixed: `powershell -File` on the installed script, and `winget` with a fixed package ID.
* They take no input from the network or the user.
* `winget` runs only after an explicit Yes.

**Uninstall** never removes data, so it cannot destroy trading history or the journal.

**Safety defaults are unchanged:** DEMO, worker stopped, auto-trading off, LIVE locked. These were verified on the
installed layout.

## 7. Rollback

Revert the installer commits.
* The image-tag change is backward compatible: no manifest means `local`.
* Already-installed copies uninstall cleanly, and their data stays.

## 8. Files

**Created**
* `desktop/scripts/stage_bundle.py`
* `desktop/src-tauri/tauri.bundle.conf.json`
* `desktop/src-tauri/windows/hooks.nsh`
* `infrastructure/install.dockerignore`
* `infrastructure/windows/uninstall-stop.ps1`
* this report

**Modified**
* `desktop/src-tauri/tauri.conf.json`: per-machine install, version 0.2.0
* `desktop/core/src/{local,supervisor,connector}.rs`: image tag (and test)
* `infrastructure/compose.yml`: image tag variable
* `windows-connector/rr_connector/headless.py`: stdio re-attach
* `.github/workflows/desktop.yml`
* `desktop/README.md`, `infrastructure/README.md`
