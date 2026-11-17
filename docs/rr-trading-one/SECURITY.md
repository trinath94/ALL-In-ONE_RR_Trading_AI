# RR Trading One: security

## 1. What is protected, and against what

| Asset | Threats considered |
|---|---|
| The trading account (money) | Unintended orders; LIVE enabled by mistake or by an AI; duplicate or lost orders; wrong account; stale data |
| Credentials: broker passwords, AI keys, internal secrets | Disclosure through files, logs, backups, the network or an AI provider |
| The trading history and models | Loss, silent corruption, tampering, an unsafe migration |
| The PC | Commands injected through the UI, a web page or AI output |

**Out of scope.** The design does not try to protect against malware already running as the same Windows user, nor
against an administrator of the PC. Either can read the user's DPAPI-protected data.

## 2. Controls

**Network**
* The engine listens on **127.0.0.1:47810** only; PostgreSQL and Redis are on an internal Docker network.
* The MT5 connector dials out to the local gateway; the terminal is never exposed.
* Local traffic is HTTP on the loopback interface. It is protected by sessions and HMAC, not TLS (see §4).

**Authentication and access**
* Engine users: argon2 passwords, lockout, HttpOnly session cookies, CSRF tokens, revocable sessions.
* Role-based permissions on every endpoint (`admin`, `trader`, `readonly`).
* **Desktop ↔ engine:** HMAC-signed requests, ±60 s window, single-use nonce.
* **Engine ↔ connector:**
  * device registration with a one-time token and a device key pair;
  * every request signed (HMAC over method, path, time, nonce and body); replayed nonces refused;
  * a read-only credential for the roles that must not trade.
* **Desktop IPC:** only the local Control Centre window may call the app's commands. Each command is declared and granted explicitly, and a test keeps the lists equal. The engine's web UI window has no IPC.

**Execution safety**
* Closed list of supervisor actions; fixed Docker Compose argv; no shell.
* Typed confirmations for anything that trades or replaces data.
* Initial state: DEMO, trading worker stopped, LIVE locked. LIVE needs verification, approval and typed confirmations, and also the PC's own LIVE lock, which this version cannot release.
* The Risk Engine on every order; idempotent journal; reconciliation after restarts and mode switches.
* Wrong account identity, stale quotes and critical anomalies block entries.
* The emergency stop blocks entries without blindly closing positions.

**AI**
* AI output is schema-validated and advisory. It cannot place orders, enable LIVE, change permissions or increase confidence.
* No code path leads from the learner, the AI layer or the agents to order submission, LIVE or permissions (a static test checks this).
* Broker passwords never reach a provider; AI keys are encrypted and write-only.
* No AI output is ever executed. AI-generated strategy code runs only in a resource-limited sandbox, never in the production registry.

**Secrets**
* Stack secrets are DPAPI-protected, in an owner-only folder, and never in the program folder or settings.
* Each container receives only the keys it needs (verified in CI).
* Access secrets can be rotated (§3).
* The engine's logs redact its secrets and credential-shaped text (URL passwords, Authorization and API-key headers, provider keys), including web-server tracebacks. Verified on the install: no secret in any log.

**Data**
* **Backups:** AES-256-GCM, in authenticated chunks. Modification, truncation and reordering are detected. The plain dump exists only briefly in the owner-only folder.
* **Backup key:** exportable for disaster recovery.
* **Restores and imports** run in one transaction, after a safety backup.
* **Imports** are verified in a scratch database. Content is checked after migration, and imports never replace local trading records without the explicit policy.
* **Models** are HMAC-signed and verified on every use.

**Supply chain**
* Dependencies are pinned (Python lock, npm lock, Cargo.lock).
* A security workflow (pip-audit, npm audit, cargo-audit) runs on every dependency change and weekly; a known vulnerability fails it.

## 3. Key management

| Key | Protects | Rotation |
|---|---|---|
| Database and Redis passwords, session signing key, bridge secret, local API key | Access between components | **Security → Rotate secrets** (backup first; everyone signed out; engine restarted) |
| Broker-password key, AI-key key, read-only bridge key, model signing key | Stored data | Not rotatable yet (needs re-encryption or re-signing of what they protect) |
| Backup key | Backups | Not rotated. **Export and store offline**; another installation can add it to restore these backups. |
| Connector device key, API key | Connector identity | Relinking the connector issues new credentials. Importing a database revokes the server's connectors. |

## 4. Known limitations

* **The installer is not code-signed** and there is **no automatic updater** (both need the owner's certificate, signing key and release hosting).
* Local traffic is not encrypted (loopback only).
* Data-encryption keys are not rotatable.
* Protection against same-user malware is limited to what Windows DPAPI and folder permissions give.

## 5. Reporting a problem

Security issues should be reported privately to the repository owner, not in a public issue.
