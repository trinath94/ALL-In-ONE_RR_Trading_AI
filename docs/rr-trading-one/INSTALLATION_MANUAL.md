# RR Trading One: installation manual

## 1. What you need

| | Requirement |
|---|---|
| Windows | Windows 10 or 11, 64-bit |
| Docker | **Docker Desktop with WSL2.** The installer offers to install Docker Desktop (with winget) if it is missing; it never does so silently. |
| Disk | At least 2 GB free to start (checked); 10 GB or more recommended. The database grows with history: about 1.2 GB per 40 days for 50 symbols in our measurement. |
| Memory | The engine's roles are capped at about 6.3 GB together (monitor 768 MB, analysis 2 GB, learner 3 GB, trading worker 512 MB), plus PostgreSQL, Redis, the API and the web interface. **A PC with 16 GB is recommended.** |
| MetaTrader 5 | The MT5 terminal of your broker, installed and logged in to the account you will trade (start with a DEMO account) |
| Network | Only the PC itself: the engine listens on `127.0.0.1:47810` and is not reachable from the network. AI providers, if you add any, are reached over HTTPS. |

## 2. Install

1. Run `RR Trading One_<version>_x64-setup.exe` (from the build's `rr-trading-one-windows-unsigned` artifact). The installer is not code-signed yet, so Windows SmartScreen may warn; check the SHA-256 against `SHA256SUMS.txt` from the same build.
2. It installs for all users into `C:\Program Files\RR Trading One`.
3. If Docker Desktop is missing, the installer **asks** whether to install it. Afterwards restart Windows if asked, and open Docker Desktop once to finish its setup.

Where things go:

| Folder | Contents |
|---|---|
| `C:\Program Files\RR Trading One` | The application, the engine's sources (built locally on first start), the MT5 connector. Nothing secret. |
| `C:\ProgramData\RR Trading One` | Backups (encrypted), logs, secrets (DPAPI-protected, owner-only folder) |
| `%APPDATA%\RR Trading One` | Your settings and MT5 profiles (no secrets) |
| `%LOCALAPPDATA%\RR Trading One\connector` | The connector's own data (its credentials are in Windows Credential Manager) |
| Docker volumes `rr-trading-one_pgdata`, `rr-trading-one_redisdata` | The database and cache. Never removed by the application, an upgrade or an uninstall. |

## 3. First run: the setup wizard

Open **RR Trading One**. A first-run card lists nine steps. Each is checked against the running system (never just
ticked) and names the button to use:

1. **Check Windows, Docker Desktop, WSL2, disk space and the local port.** Warnings appear here too, for example when the standalone RR MT5 Connector from an old Ubuntu setup is still installed.
2. **Start the engine:** Service manager → Start All. The first start builds the engine images (several minutes).
3. **Verify health:** System health → Check now.
4. **Create the administrator account:** First-time setup. This works only while no user exists.
5. **Detect the MT5 terminal and save its profile:** MT5 Manager → Detect terminals.
6. **Link and start the MT5 connector:** MT5 Manager → Link connector, then Start connector.
7. **Register the DEMO account with the engine:** MT5 Manager → Use as engine DEMO account. It must be a DEMO account the connector is logged in to.
8. **Set up an AI provider (optional):** Trading Console → AI Studio.
9. **Take a first backup:** Backup and Recovery → Back up now. Then, under Security, **Show backup key** and store it offline (see the Recovery manual).

The application starts in **DEMO, with the trading worker stopped and LIVE locked**.

## 4. Moving from the Ubuntu server

See the [migration manual](../MIGRATION_FROM_UBUNTU.md): an export on the server, then a verified import on the PC.

## 5. Upgrading

Run the newer installer over the existing installation. It:
* stops what the old version started (never deleting data);
* replaces the program files and keeps every setting, backup, secret and database volume (verified in CI).

The next **Start All**:
* rebuilds the engine images for the new version;
* takes a database backup;
* applies any new database migrations.

## 6. Uninstalling

Windows Settings → Apps → RR Trading One → Uninstall. This removes the program and the connector. **It keeps**:
* the database volumes;
* `C:\ProgramData\RR Trading One` (backups, secrets);
* your settings.

To remove those as well, after making sure you have a backup and its key: delete the two Docker volumes in Docker Desktop, and the two folders.

## 7. Checking an installation

* The desktop window's **System health** shows the engine API, PostgreSQL and Redis, and the web interface.
* The Trading Console's **System Monitor** shows each role's CPU and memory against its limit, the disk and the database size.
* If something does not start: **Service manager → Recovery** restarts unhealthy services. It never restarts the trading worker.
