# RR Trading One — Phase 21 Report

**Phase:** 21 — Security (specification Part 21)
**Date:** 2026-11-16
**Branch:** `claude/focused-cori-b8em6u`

## 1. Summary

Part 21 lists fourteen security controls and three rules. Phase 21 audited each against the code. It built the five
that were missing or partial and fixed a real defect found during the audit:
* **encrypted backups**, with a recovery key;
* **secret rotation**;
* **dependency scanning**, which found one high-severity vulnerability, now fixed;
* **log redaction**;
* **secure IPC**: the desktop window's permission list was missing eight commands, so Backup and Recovery, the Ubuntu import and the wizard's first step would have been refused in the real Windows app.

| Part 21 item | Status | Where / evidence |
|---|---|---|
| Windows-protected credentials | ✅ Phases 3, 4, 9 | Stack secrets sealed with **DPAPI** in an owner-only folder. The connector's credentials in **Windows Credential Manager**. Broker passwords and AI keys **encrypted (Fernet)** in the database, with keys only in the containers that need them. |
| Secure local communication | ✅ Phase 3 | Only the gateway publishes a port, on **127.0.0.1**. PostgreSQL and Redis are on an internal-only network. Only roles that need it have outbound access. |
| HMAC and replay protection | ✅ Phases 1, 4, 5 | Engine → connector: HMAC over method, path, time, nonce and body; the connector's journal rejects a replayed nonce. Connector → engine: device-key signature, ±time window, single-use nonce (Redis). Desktop → engine: HMAC, ±60 s, single-use nonce. |
| Device authentication | ✅ Phase 4 | The connector registers with a one-time link token and a device key pair; each request is signed by the device. A read-only credential for non-trading roles. Phase 19: an imported server's devices are revoked. |
| Strict RBAC | ✅ | Permission-checked endpoints (`require(...)`); LIVE needs `trade.live`. Tests: Phase 9 boundaries, Phase 16 LIVE lifecycle, Phase 20 cancel endpoint. |
| Audit logs | ✅ | Every sensitive change is audited (users, accounts, LIVE, mode switches, kill switch, AI providers, models, learning jobs, imports). |
| Local API authentication | ✅ Phase 5 | HMAC-signed local supervisor API; the key is held only by the desktop app and the API container. **Rotatable now.** |
| **Secure IPC** | ⚠️ → ✅ **Fixed** | Section 4 |
| Input validation | ✅ | Pydantic request models; backup ids validated (never paths); import headers checked; typed phrases checked again in the supervisor; settings reject unknown fields. |
| Process supervision | ✅ Phases 3, 20 | Fixed action plans; health checks; role locks; the trading worker is never started or restarted automatically; learner jobs terminated on timeout (Phase 20). |
| **Encrypted backups** | ❌ → ✅ **New** | Section 2 |
| **Signed application updates** | ❌ **Not built** | Needs your decisions (section 7). The connector's own update check already requires HTTPS and verifies a SHA-256. |
| **Secret rotation** | ❌ → ✅ **New** | Section 3 |
| **Dependency scanning** | ❌ → ✅ **New** | Section 5 |
| Rule: never expose passwords, API keys or bridge secrets in logs | ⚠️ → ✅ **New redaction** | Section 6 |
| Rule: never execute AI-generated shell commands | ✅ | No AI output is ever executed as a command. AI-generated *strategy code* is statically validated and run only in a resource-limited sandbox process (Phase 11). |
| Rule: experimental code never in the production registry | ✅ | Experimental strategies are never registered (`test_preservation.py`, `test_discovery_sandbox.py`). |

| Result | Value |
|---|---|
| End-to-end on the running install | **20 / 20** (section 8) |
| Desktop core tests (Rust) | **120 passed** (106 + 14 new); clippy clean with warnings as errors |
| Backend suite, including the 215-strategy preservation test | **316 passed** (314 + 2 new) in one full run |
| Frontend `next build` | Passed (PostCSS 8.5.32) |
| CI (`e09168a`) | backend 43969624710, frontend 43969624767, security 43969624756 passed. Desktop 43969624797 **failed on Windows** in the new ACL test: it compared the capability file's text, which Windows checks out with CRLF line endings. The test now parses the JSON (verified with both line endings); fixed in `784f336`, whose desktop run 44012842457 **passed** on Linux and Windows. |
| Dependency scans | Engine, both connectors, frontend and desktop: **no known vulnerability** after the fix |
| Desktop window in a browser (mocked supervisor) | Security section, wizard and earlier checks passed |

## 2. Encrypted backups

**Before:** backups were plain PostgreSQL dumps. Anyone who could read the backups folder could read the database
(users, password hashes, accounts, trades), and an Ubuntu import kept a plain copy of the server's database.

**Now (`desktop/core/src/backupcrypt.rs`):**
* **Format.** Every backup is **AES-256-GCM, in 1 MiB chunks** (STREAM construction), saved as `<id>.rrbk`. Each chunk's nonce encodes its position and whether it is the last, and the header (format, key id, nonce prefix) is authenticated with every chunk. A modified, reordered or truncated file fails to decrypt; nothing is restored.
* **Key.** A new `backup_key` (stack secrets format 6, DPAPI-protected, desktop app only; never given to a container). Upgrading adds only this key; every existing secret is kept.
* **Plain data on disk.** The plain dump exists only in an owner-only private folder, for the moment of encrypting or restoring, and is deleted afterwards (also on failure).
* **Ubuntu imports** are encrypted directly from the source file: no plain copy is made.
* **Older backups** (plain `.dump`, made before Phase 21) are still listed, marked "not encrypted", and still restorable.
* **Recovery key (disaster recovery).** DPAPI ties the key to this Windows account, so without an export a backup could not be restored on a new PC after a disk failure. In the window's **Security** section:
  * **Show backup key** (typed phrase `SHOW BACKUP KEY`) displays `RRBK1-…` to keep offline. It clears from the screen after 2 minutes.
  * **Add backup key** accepts another installation's key (password field), stored with the secrets.
* **Clear refusals.** A backup encrypted with a key this installation does not hold is refused **before anything is stopped**, naming the key id it needs.

## 3. Secret rotation

**Security → Rotate secrets** (typed phrase `ROTATE SECRETS`) replaces every **access** secret:
* the database password (changed with `ALTER ROLE`, sent through stdin, never in a process's arguments);
* the Redis password;
* the session signing key (everyone is signed out);
* the bridge secret;
* the desktop ↔ engine local API key.

It runs in three steps:
1. With the current secrets: a backup, then everything that uses the database or Redis is stopped, trading worker first.
2. The database password is changed and the new secrets are saved atomically. **If saving fails, the old password is put back** and nothing is restarted with unsaved secrets.
3. With the new secrets: the database, Redis, engine and learner start again and are health-checked. **The trading worker is not restarted.**

**Not rotated:** the keys that encrypt or sign stored data (broker passwords, AI keys, model signatures, backups).
Rotating them means re-encrypting or re-signing what they protect (section 7).

## 4. Secure IPC: a defect from Phases 17 and 18, fixed

The desktop app declares an ACL manifest (`src-tauri/build.rs`). With a manifest, Tauri **rejects at runtime** any
command that is not declared there and granted to the window (verified in Tauri 2.12's source).

The manifest still listed only the 15 commands of Phase 5. **Missing:**
* `backup_list`, `backup_restore` (Phase 17);
* `backup_import_file`, `backup_import`, `prerequisites` (Phase 18).

In the real Windows window, Backup and Recovery, the Ubuntu import and the wizard's first step would have been
refused. My earlier verification used the supervisor, the `supervise` CLI and a mocked browser, none of which go
through Tauri's ACL.

**Fix:**
* All 23 commands (including Phase 21's three) are declared and granted to the local `main` window only. The engine console window still gets none.
* A new test (`tauri_acl_lists_every_command`) requires the handler list, the manifest and the capability to be identical. It fails on the previous files and passes now.

The Phase 17 and 18 reports carry this correction.

## 5. Dependency scanning

New workflow `.github/workflows/security.yml`:
* runs on every change to a dependency file, and **weekly** (advisories appear against unchanged dependencies);
* a known vulnerability fails it.

| Scan | Tool | Result |
|---|---|---|
| Engine (exact pinned lock) | pip-audit 2.10.1 | No known vulnerabilities |
| RR MT5 Connector, MT5 bridge | pip-audit | No known vulnerabilities |
| Frontend (production) | npm audit | **1 high** (fixed below) |
| Desktop (462 crates) | cargo-audit 0.23.0 | No vulnerabilities. Five advisories that are not vulnerabilities: four unmaintained and one unsound crate, in Linux desktop libraries (glib, zbus) that the Windows build does not use. Reported, not failing. |

**The fix.** Next.js 15.5.28 bundled PostCSS 8.4.31, which has four advisories (cross-site scripting in CSS output;
file disclosure through source maps). npm's own remedy was a major upgrade to Next 16. Instead, PostCSS is pinned
through an npm override to **8.5.32**, the same major version and above every affected range. The audit now reports
0 vulnerabilities, and the frontend builds and runs unchanged. (8.5.33 was skipped because it was only three days old.)

## 6. Log redaction

The engine's logs now pass through a redaction step (`app/core/logging.py`) before they are written, for both
messages and tracebacks:
* **Uvicorn too.** Its own handlers (request errors with tracebacks, access lines) are covered.
* **What is masked (`***`):**
  * every secret in the engine's environment (session signing key, encryption keys, bridge secret, supervisor key, AI keys key, model signing key, Anthropic key, and the database and Redis passwords, also URL-encoded);
  * credential-shaped text: passwords in URLs, Authorization and API-key headers (any quoting), provider API keys (`sk-…`), and `password=` / `"token": …` pairs.
* **What is kept:** the surrounding context, e.g. `postgresql+psycopg://rr:***@postgres:5432/rr_trading`.

On the install, after two backups, two restores, a refused restore and a rotation, **no secret value (16 values: the
old and new secrets and the backup key) appeared** in any container log, the supervisor's log or the connector's logs
(1.8 MB scanned).

## 7. Not built, and why

* **Signed application updates.** Signing needs a code-signing certificate, which is bought in the owner's name (an EV certificate also avoids SmartScreen warnings). Automatic updates also need somewhere to host releases. Both are your decisions. Until then, the installer is unsigned and updating means running a newer installer, which keeps all data.
* **Rotation of data keys.** The encryption, AI keys, model signing and backup keys are not rotated. Rotation would re-encrypt broker passwords and AI keys, re-sign models, and keep the old backup key for older backups. This is feasible but needs a migration; it is noted for a later phase if you want it.
* **Local traffic is HTTP on 127.0.0.1,** not TLS. It never leaves the PC. A program already running on the PC could reach the port, but it still needs a signed-in session or the HMAC key.

## 8. Verification

**End-to-end on the running install (20 / 20), through the desktop supervisor:**
1. **Upgrade:** the secrets file became format 6 with a backup key; every existing value was unchanged.
2. **Back up now:**
   * wrote an encrypted `.rrbk`, with no plain dump anywhere;
   * a known row's text and the PostgreSQL header are not in the file;
   * listed as encrypted.
3. **Restore of the encrypted backup:** the backup's row was back and a later row gone; no decrypted copy was left; the engine reconciled again; the trading worker was not running.
4. **Backup key:**
   * shown only with the phrase, and it is the stored key;
   * an invalid key was refused; adding a key already held changed nothing;
   * a backup from "another installation" (different key id) was **refused before anything was stopped**, naming the key.
5. **A modified backup** (one byte changed) failed its integrity check. Nothing was restored and no decrypted copy was left; Start All brought the engine back.
6. **Rotation:**
   * refused without the phrase; nothing changed;
   * with the phrase: backup, stop (worker first), password change and save, start, health: all OK;
   * access secrets replaced; data keys unchanged;
   * the **old database password was rejected and the new one accepted** (over the stack's network), and the same for **Redis**;
   * the earlier browser session was **signed out**, and signing in again worked;
   * the desktop reached the engine with the new local API key;
   * data intact; reconciled with the broker; the trading worker not running.
7. **No secret in any log** (section 6).

**How the runs went:**
* **First run, 19 / 20.** The failing check tested the old database password from inside the database container, where PostgreSQL trusts `127.0.0.1`. Tested over the stack's network, as the engine connects, the old password was rejected and the new one accepted.
* **Second run, 17 / 20.** The script did not remove the first run's marker rows; it now does.
* **Third run, 20 / 20.**

**Tests (Rust 14 new, backend 2 new):**
* encryption: round trips at every chunk boundary; tampering, truncation, header edits and wrong keys refused; key export;
* backups: encrypted, with no plain file left; an encrypted restore delivers the exact dump; a new PC restores once given the key and is refused before anything runs without it; a modified backup is refused while an older plain one still restores; imports stored encrypted;
* secrets: format 5 → 6 upgrade; rotation keeps data keys; adding a key;
* rotation: order, the password only in stdin, new secrets used afterwards, no worker restart; a failed save puts the old password back and restarts nothing;
* the Tauri ACL list;
* log redaction (JSON and uvicorn handlers).

## 9. Files

**Created**
* `desktop/core/src/backupcrypt.rs`
* `desktop/ui/security.js`
* `.github/workflows/security.yml`
* `backend/tests/test_security.py`
* this report

**Modified**
* `desktop/core/src/secrets.rs`: format 6, backup keys, rotation, save.
* `desktop/core/src/supervisor.rs`: encrypted backups and imports, private decryption, key check, export, `rotate_secrets`.
* `desktop/core/src/actions.rs`: phrases, `rotate_plans`, `RotateAccessSecrets`.
* `desktop/core/src/compose.rs`: `psql_stdin`.
* `desktop/core/src/local.rs`: private folder, rotation, add key.
* `desktop/core/src/lib.rs`: module, ACL test.
* `desktop/core/Cargo.toml`: `aes-gcm`.
* `desktop/core/examples/supervise.rs`: `backup-key`, `backup-key-add`, `rotate-secrets`.
* `desktop/src-tauri/{build.rs,capabilities/main.json,src/lib.rs}`: every command declared and granted; three new commands.
* `desktop/ui/{index.html,app.js,backup.js}`: Security section; encrypted column.
* `backend/app/core/logging.py`: redaction.
* `backend/tests/test_resources.py`: the timeout test no longer depends on start-up time.
* `frontend/package.json`, `frontend/package-lock.json`: PostCSS 8.5.32.
* `docs/RR_TRADING_ONE_PHASE17/18/20_REPORT.md`: corrections and CI results.

**Preserved:** all 215 strategies, the engine, the Risk Engine, permissions, every original test. No database migration.

## 10. Rollback

* Revert the Phase 21 commit.
* **Before reverting, restore or export what you need.** Encrypted `.rrbk` backups cannot be read by the earlier version.
* A format-6 secrets file is not readable by the earlier version, which expects format 5. Put back the secrets file from before the upgrade, or keep this version.
* Secrets rotated with this version stay valid: they live in the same file.

## 11. Next phase

**Phase 22 — Testing (Part 22).**
