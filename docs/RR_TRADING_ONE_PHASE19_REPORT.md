# RR Trading One — Phase 19 Report

**Phase:** 19 — Local and remote architecture (specification Part 19)
**Date:** 2026-11-16
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

Part 19 asks for three things:
1. **LOCAL WINDOWS MODE** as the primary mode, with every service managed on the PC;
2. **REMOTE ENGINE MODE** as an optional future mode, in which a Windows MT5 terminal connects to an external engine through the existing secure relay;
3. both modes **separate and explicit**, and local mode **never accidentally pointed at the previous Ubuntu server**.

The two modes were already designed separately in Phase 3. Phase 19 audited every path by which the PC could end up
talking to the Ubuntu server and found one real gap:
* **A database imported from the server brought the server's trust with it:**
  * its connector registrations stayed enabled;
  * an unused link token stayed valid;
  * browser sessions stayed unrevoked.

This is fixed. A warning was also added for the standalone connector from the Ubuntu setup, which the PC may still run.

| How local mode could reach the Ubuntu server | Protection | Evidence |
|---|---|---|
| A REMOTE URL left in the settings | LOCAL mode always uses `http://127.0.0.1:<port>` and ignores `remote_engine_url` | unit test; **end-to-end**: with the server URL left in the settings, the connector was started for the loopback gateway |
| The local stack's configuration | Database and Redis are fixed compose service names. The bridge runs in relay mode: the engine never dials out to a terminal, so an imported `bridge_url` is never used. Only the gateway publishes a port, on 127.0.0.1. CORS allows the loopback origin only. | compose file; **end-to-end**: no container refers to the server |
| The connector bundled with RR Trading One | It has its own data folder (`%LOCALAPPDATA%\RR Trading One\connector`), separate from the standalone connector's. It refuses to run unless it is registered to exactly the engine URL of the profile, so a registration to another engine means "Link first". | Phase 4 tests; **end-to-end** |
| **Server identities imported with the database** | **Fixed:** the import revokes the server's connector registrations, expires its unused link tokens and revokes its sessions, in the same transaction that resets trading to DEMO. The PC's connector is linked afresh. | **end-to-end**: the server connector's key is refused (403 "agent revoked"); the new link is a new agent; reconciliation passes through it |
| **The standalone RR MT5 Connector from the Ubuntu setup**, still installed and possibly linked to the server | **New warning** in the prerequisites and in the first-run wizard: close it, disable its start-up, and stop trading on the server before trading locally | unit test; browser check of the wizard |
| A REMOTE engine over plain HTTP | REMOTE requires `https://` (no credentials, query or fragment in the URL) | unit tests; **end-to-end** |
| Mixing the modes | In REMOTE mode, local service control, local health checks and the local console are unavailable. The window shows "REMOTE ENGINE MODE". Nothing local is touched. | **end-to-end**: Start All refused in REMOTE mode; the local engine kept running |

| Result | Value |
|---|---|
| End-to-end on the running install | **13 / 13** (section 4) |
| Desktop core tests (Rust) | **106 passed** (104 + 2 new); clippy clean with warnings as errors; the desktop app crate compiles |
| Desktop window in a browser (mocked supervisor) | wizard check (with the new warning) and the earlier window check both passed |
| Backend | unchanged in this phase (last full run: 302 passed, Phase 18) |
| CI for Phase 18 (`b78955a`) | desktop 43951669672, backend 43951669764: passed |

## 2. The gap: imported server identities

**Evidence.** The server's dump was restored into a scratch database with the Phase 18 import's restore step, but
without its final reset. It contained:
* relay agent 7, **enabled and not revoked**, recorded as serving **the same account** (1001@Demo-Server) as the PC. The engine chooses the agent for broker calls by matching the active account. A server agent was therefore a candidate whenever it had been seen more recently than the PC's own link, or before the PC was linked at all. Its secret is encrypted with the server's key, so such calls would fail; the identity would still be trusted.
* **an unused link token valid for another day**: whoever held it could register a device with the local engine;
* **an unrevoked browser session** from the server. Its cookie cannot be replayed (it is signed with the server's JWT secret, which the PC does not have), but it was still listed as active.

**Fix.** The import's last database step ("start like a new installation") now also, in the same transaction:
* revokes every connector registration (`enabled = false`, `revoked_at = now`). They stay visible in the history;
* expires every unused link token;
* revokes every session.

The audit entry `DATABASE_IMPORTED` lists what was revoked. Linking the PC's connector after the import (already a
listed follow-up) creates a new agent with new credentials.

## 3. Remote engine mode

* **Today:** REMOTE mode is a setting, not a feature. It is explicit (`operating_mode: "remote"` with an `https://` engine URL in the user's settings file) and disables everything local.
* **Not built:** a REMOTE workflow in the window (linking the PC's connector to an external engine, opening its console). The specification calls it an optional future mode.
* **When it is built,** the pieces are already there:
  * the bundled connector dials the configured engine URL and requires `https://` for anything that is not the local loopback gateway;
  * the relay protocol is the one the Ubuntu deployment used.

## 4. End-to-end verification

Run on the running install through the desktop supervisor (the same calls as the window), with the connector.

**1. LOCAL mode with a REMOTE URL left over** (`https://rr.ubuntu-server.example` in the settings, mode `local`):
* Start All succeeded.
* The connector's runtime profile was written for `http://127.0.0.1:47810`, and it reached CONNECTED.
* No running container refers to the server.

**2. REMOTE mode:**
* An `http://` URL was refused.
* With an `https://` URL, Start All was refused ("LOCAL WINDOWS MODE only") and the local engine kept running.
* Back in LOCAL mode, the supervisor worked again.

**3. Import of a server database containing the server's connector registration, a spare link token and a session:**
* The import completed.
* The server's agent was disabled and revoked; its API key got **403 "agent revoked"** from the local engine.
* The token was expired and the session revoked; the audit entry lists all three.
* Linking the PC's connector created **a new agent**, which the DEMO account registration used.
* The engine reconciled with the broker through it, in DEMO; it was the only enabled agent.

**4. Undo:** the safety backup restored the installation's own database, and reconciliation passed again after relinking.

All 13 checks passed on the first run.

## 5. Files

**Created**
* this report

**Modified**
* `desktop/core/src/compose.rs`: the post-import reset revokes the server's identities; test.
* `desktop/core/src/supervisor.rs`: `legacy_connector_dir` and the `previous_connector` warning; test; step detail.
* `desktop/core/src/local.rs`: `%LOCALAPPDATA%\RRMT5Connector` on Windows.
* `desktop/core/src/connector.rs`: test configuration.
* `desktop/ui/wizard.js`, `desktop/ui/style.css`: prerequisite warnings shown under step 1.
* `docs/RR_TRADING_ONE_PHASE18_REPORT.md`: CI results.

**Preserved:** the engine, the relay, the Risk Engine, permissions, all 215 strategies and every original test. No
migration, no backend change.

## 6. Known limitations

* **The Ubuntu server itself is outside this installation's control.** If it keeps running with auto trading on, it can still trade the account through any connector still linked to it. The warning and the import follow-ups say to stop trading on the server first; the PC cannot do it.
* **The standalone connector is detected by its data folder, not its process.** The desktop cannot read its configuration (encrypted with that application's own key) or tell whether it is running. The warning appears whenever it is installed for the user.
* **REMOTE mode has no workflow yet** (section 3).

## 7. Security implications

* Stricter only. After an import, the engine trusts no connector, link token or session from the server.
* The new prerequisite is a read-only file check and a warning; it changes nothing.

## 8. Rollback

* Revert the Phase 19 commit. No migration.
* Agents revoked by an import stay revoked after a revert, which is the safe direction. Relinking the PC's connector is unaffected.

## 9. Next phase

**Phase 20 — Performance and resource management (Part 20)**
* Audit what exists (scanner scheduling and timeframe filter, role processes, AI per-minute limit, job queue) against Part 20's list:
  * parallel scanning; worker and learner limits; queues, priorities, cancellation, timeouts and retries;
  * caching and indexes; CPU, memory and disk monitoring; scanner backpressure.
* Verify by measurement that learning and AI research cannot delay the trading worker or position protection.
