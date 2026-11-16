//! Docker Compose invocations for the local stack (`infrastructure/compose.yml`).
//!
//! Every invocation is a fixed argument vector: project name and compose file are always explicit, service names
//! come only from [`Service::compose_name`], and there is no code path that produces `down`, `rm`, `-v` or
//! `--volumes`, so PostgreSQL and Redis data volumes can never be deleted by the supervisor.

use crate::actions::Service;
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

pub const PROJECT: &str = "rr-trading-one";
/// Seconds compose waits for health checks during `up --wait`.
pub const WAIT_TIMEOUT_SECS: u32 = 300;
/// Seconds containers get to stop gracefully (workers finish their cycle; journal stays consistent).
pub const STOP_TIMEOUT_SECS: u32 = 30;
/// Where a backup is placed inside the database container for `pg_restore` (Phase 17).
pub const RESTORE_FILE: &str = "/tmp/rr-restore.dump";
/// Phase 18: the backup converted to SQL inside the database container, applied by `psql` in one transaction.
pub const RESTORE_SQL: &str = "/tmp/rr-restore.sql";
/// Phase 18: the restore replaces the whole `public` schema, in the same transaction as the backup's SQL. `pg_restore
/// --clean` only drops the objects the *backup* knows about, so an older backup (e.g. the Ubuntu server's schema 0003)
/// could not be restored over a newer database: newer tables still referenced the tables being dropped.
pub const RESET_SCHEMA: &str = "DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION pg_database_owner; \
    GRANT USAGE ON SCHEMA public TO PUBLIC;";
/// Phase 18: scratch database an import is verified in before the real database is replaced.
pub const SCRATCH_DB: &str = "rr_import_check";
/// Phase 18: exact row count of every table (one statement, no input).
pub const COUNT_ALL_TABLES: &str = "SELECT table_name, (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I', \
    table_name), false, true, '')))[1]::text FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' \
    ORDER BY 1";
/// Phase 18: an imported database starts like a new installation (DEMO, auto trading OFF, LIVE approvals cleared,
/// reconciliation required), recorded in the audit trail. Phase 19: identities that belonged to the server are not
/// trusted here: its connectors (relay agents) are revoked, its unused link tokens expire, its sessions are revoked.
/// The PC's connector is linked afresh to this engine.
pub const RESET_FOR_NEW_INSTALLATION: &str = "BEGIN; UPDATE trading_state SET active_mode = 'DEMO', auto_trading = false, \
    startup_checks_passed = false; UPDATE trading_accounts SET live_approved_by = NULL, live_approved_at = NULL, is_active = false \
    WHERE mode = 'LIVE'; \
    UPDATE mt5_agents SET enabled = false, revoked_at = now() WHERE revoked_at IS NULL; \
    UPDATE mt5_agent_tokens SET expires_at = now() WHERE used_at IS NULL AND expires_at > now(); \
    UPDATE user_sessions SET revoked = true WHERE NOT revoked; \
    INSERT INTO audit_logs (action, target, detail, created_at) VALUES ('DATABASE_IMPORTED', 'desktop', \
    '{\"reset\": \"DEMO, auto trading off, LIVE approvals cleared, reconciliation required\", \"server_connectors\": \"revoked\", \
    \"server_link_tokens\": \"expired\", \"server_sessions\": \"revoked\"}', now()); COMMIT;";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Compose {
    pub docker: PathBuf,
    pub file: PathBuf,
}

fn names(services: &[Service]) -> Vec<String> {
    services.iter().filter_map(|s| s.compose_name()).map(String::from).collect()
}

impl Compose {
    fn base(&self) -> Vec<String> {
        vec!["compose".into(), "-p".into(), PROJECT.into(), "-f".into(), self.file.to_string_lossy().into_owned()]
    }

    fn with(&self, rest: &[&str], services: &[Service]) -> Vec<String> {
        let mut v = self.base();
        v.extend(rest.iter().map(|s| s.to_string()));
        v.extend(names(services));
        v
    }

    /// Start services (and their dependencies) and wait until healthy.
    pub fn up(&self, services: &[Service]) -> Vec<String> {
        let t = WAIT_TIMEOUT_SECS.to_string();
        self.with(&["up", "-d", "--wait", "--wait-timeout", &t], services)
    }

    /// Stop services; containers and volumes are kept.
    pub fn stop(&self, services: &[Service]) -> Vec<String> {
        let t = STOP_TIMEOUT_SECS.to_string();
        self.with(&["stop", "-t", &t], services)
    }

    /// Apply Alembic migrations and seed through the one-shot `migrate` service.
    pub fn migrate(&self) -> Vec<String> {
        self.with(&["run", "--rm", "--no-TTY", "migrate"], &[])
    }

    /// Consistent PostgreSQL dump to stdout (custom format).
    /// Phase 17: copy a backup (a file the supervisor resolved in its own backups folder) into the database container.
    pub fn copy_into_postgres(&self, file: &std::path::Path) -> Vec<String> {
        let mut v = self.base();
        v.extend(["cp".to_string(), file.to_string_lossy().into_owned(), format!("postgres:{RESTORE_FILE}")]);
        v
    }

    /// Phase 18: convert the copied backup to SQL. Reading the whole dump first means a damaged file fails here,
    /// before the database is touched.
    pub fn pg_restore_to_sql(&self) -> Vec<String> {
        self.with(&["exec", "-T", "postgres", "pg_restore", "--no-owner", "--exit-on-error", "-f", RESTORE_SQL, RESTORE_FILE], &[])
    }

    /// Restore in one transaction (all or nothing): the schema is replaced and the backup applied, or nothing changes.
    pub fn psql_restore(&self) -> Vec<String> {
        self.with(&["exec", "-T", "postgres", "psql", "-U", "rr", "-d", "rr_trading", "-q", "-o", "/dev/null",
            "--set=ON_ERROR_STOP=1", "--single-transaction", "-c", RESET_SCHEMA, "-f", RESTORE_SQL], &[])
    }

    pub fn remove_restore_file(&self) -> Vec<String> {
        self.with(&["exec", "-T", "postgres", "rm", "-f", RESTORE_FILE, RESTORE_SQL], &[])
    }

    /// Phase 18: run one fixed SQL statement (a constant from this crate, never input) in a database of the stack.
    pub fn psql(&self, database: &str, sql: &str) -> Vec<String> {
        self.with(&["exec", "-T", "postgres", "psql", "-U", "rr", "-d", database, "--set=ON_ERROR_STOP=1", "-At", "-c", sql], &[])
    }

    /// Phase 21: run SQL read from stdin (so a value such as a new password never appears in a process's arguments).
    pub fn psql_stdin(&self, database: &str) -> Vec<String> {
        self.with(&["exec", "-T", "postgres", "psql", "-U", "rr", "-d", database, "--set=ON_ERROR_STOP=1", "-q", "-f", "-"], &[])
    }

    /// Phase 18: restore the copied dump into the scratch database, stopping at the first error.
    pub fn pg_restore_scratch(&self) -> Vec<String> {
        self.with(&["exec", "-T", "postgres", "pg_restore", "-U", "rr", "-d", SCRATCH_DB, "--no-owner", "--exit-on-error",
            RESTORE_FILE], &[])
    }

    /// Phase 17: after a restore, auto trading OFF and reconciliation required. Fixed SQL, no input.
    pub fn reset_trading_state(&self) -> Vec<String> {
        self.with(&["exec", "-T", "postgres", "psql", "-U", "rr", "-d", "rr_trading", "--set=ON_ERROR_STOP=1", "-c",
            "UPDATE trading_state SET auto_trading = false, startup_checks_passed = false"], &[])
    }

    pub fn pg_dump(&self) -> Vec<String> {
        self.with(&["exec", "-T", "postgres", "pg_dump", "-U", "rr", "-d", "rr_trading", "--format=custom", "--no-owner"], &[])
    }

    /// One-time MT5 connector registration token, issued inside the API container (10-minute lifetime).
    /// `label` must already be validated (profile ids are `[a-z0-9-]{1,32}`).
    pub fn issue_agent_token(&self, label: &str) -> Vec<String> {
        self.with(&["exec", "-T", "api", "python", "-m", "app.services.agent_tokens", "--label", label, "--ttl-minutes", "10"], &[])
    }

    pub fn ps(&self) -> Vec<String> {
        self.with(&["ps", "--all", "--format", "json"], &[])
    }

    pub fn version(&self) -> Vec<String> {
        vec!["compose".into(), "version".into(), "--short".into()]
    }
}

/// One container as reported by `docker compose ps --format json`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ServiceStatus {
    #[serde(rename = "Service")]
    pub service: String,
    /// running | exited | restarting | created | paused | dead
    #[serde(rename = "State")]
    pub state: String,
    /// healthy | unhealthy | starting | "" (no health check)
    #[serde(rename = "Health", default)]
    pub health: String,
    #[serde(rename = "ExitCode", default)]
    pub exit_code: i32,
}

impl ServiceStatus {
    pub fn is_running(&self) -> bool {
        self.state == "running"
    }

    /// Running and not failing its health check (a service without a health check counts once running).
    pub fn is_healthy(&self) -> bool {
        self.is_running() && (self.health.is_empty() || self.health == "healthy")
    }

    /// Needs attention: failing health check, crash-looping, or exited with an error.
    pub fn is_failed(&self) -> bool {
        self.health == "unhealthy" || self.state == "restarting" || self.state == "dead"
            || (self.state == "exited" && self.exit_code != 0)
    }
}

/// Parse `ps --format json`. Newer Compose prints one JSON object per line; older versions print an array.
pub fn parse_ps(stdout: &str) -> Result<Vec<ServiceStatus>, String> {
    let t = stdout.trim();
    if t.is_empty() {
        return Ok(vec![]);
    }
    if t.starts_with('[') {
        return serde_json::from_str(t).map_err(|e| e.to_string());
    }
    t.lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| serde_json::from_str::<ServiceStatus>(l).map_err(|e| e.to_string()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn c() -> Compose {
        Compose { docker: PathBuf::from("/usr/bin/docker"), file: PathBuf::from("/opt/rr/infrastructure/compose.yml") }
    }

    fn all_argvs() -> Vec<Vec<String>> {
        let svc = Service::ALL;
        vec![c().up(&svc), c().stop(&svc), c().migrate(), c().pg_dump(), c().ps(), c().version(), c().issue_agent_token("demo-1")]
    }

    #[test]
    fn project_and_file_are_always_explicit() {
        for a in all_argvs().into_iter().filter(|a| a[1] != "version") {
            assert_eq!(&a[..5], &["compose", "-p", PROJECT, "-f", "/opt/rr/infrastructure/compose.yml"], "{a:?}");
        }
    }

    #[test]
    fn database_commands_run_inside_the_postgres_container_only() {
        let p = std::path::Path::new("/b/manual-1.dump");
        for a in [c().copy_into_postgres(p), c().pg_restore_to_sql(), c().psql_restore(), c().psql(SCRATCH_DB, "SELECT 1"),
                  c().pg_restore_scratch(), c().remove_restore_file(), c().reset_trading_state()] {
            assert_eq!(&a[..5], &["compose", "-p", PROJECT, "-f", "/opt/rr/infrastructure/compose.yml"], "{a:?}");
            if a[5] == "cp" { continue; }
            assert_eq!(&a[5..8], &["exec", "-T", "postgres"], "{a:?}");
            for bad in ["down", "-v", "--volumes", "prune", "kill", "--remove-orphans"] {
                assert!(!a.iter().any(|x| x == bad), "{bad} in {a:?}");
            }
        }
        let r = c().psql_restore();
        assert!(r.contains(&"--single-transaction".to_string()) && r.contains(&"--set=ON_ERROR_STOP=1".to_string()), "{r:?}");
    }

    #[test]
    fn an_import_trusts_no_identity_of_the_server() {
        let r = RESET_FOR_NEW_INSTALLATION;
        assert!(r.starts_with("BEGIN;") && r.ends_with("COMMIT;"), "one transaction");
        for must in ["active_mode = 'DEMO'", "auto_trading = false", "startup_checks_passed = false", "live_approved_at = NULL",
                     "UPDATE mt5_agents SET enabled = false, revoked_at = now() WHERE revoked_at IS NULL",
                     "UPDATE mt5_agent_tokens SET expires_at = now() WHERE used_at IS NULL",
                     "UPDATE user_sessions SET revoked = true", "'DATABASE_IMPORTED'"] {
            assert!(r.contains(must), "{must}");
        }
    }

    #[test]
    fn nothing_can_remove_containers_or_volumes() {
        for a in all_argvs() {
            for bad in ["down", "rm", "-v", "--volumes", "prune", "kill", "--remove-orphans"] {
                assert!(!a.iter().any(|x| x == bad), "{bad} in {a:?}");
            }
        }
    }

    #[test]
    fn up_waits_for_health_and_maps_service_names() {
        let a = c().up(&[Service::Postgres, Service::Redis, Service::Mt5Connector]);
        assert_eq!(&a[5..], &["up", "-d", "--wait", "--wait-timeout", "300", "postgres", "redis"]);
    }

    #[test]
    fn stop_is_graceful_and_ordered() {
        let a = c().stop(&[Service::TradingWorker, Service::Postgres]);
        assert_eq!(&a[5..], &["stop", "-t", "30", "trading-worker", "postgres"]);
    }

    #[test]
    fn stop_with_no_compose_services_has_no_names() {
        // Stopping only the native connector must not turn into "stop everything".
        let a = c().stop(&[Service::Mt5Connector]);
        assert_eq!(&a[5..], &["stop", "-t", "30"]);
    }

    #[test]
    fn parse_ps_ndjson_and_array() {
        let nd = r#"{"Service":"api","State":"running","Health":"healthy","ExitCode":0,"Name":"x"}
{"Service":"migrate","State":"exited","Health":"","ExitCode":0}
{"Service":"learner","State":"running","Health":"unhealthy","ExitCode":0}"#;
        let v = parse_ps(nd).unwrap();
        assert_eq!(v.len(), 3);
        assert!(v[0].is_healthy() && !v[0].is_failed());
        assert!(!v[1].is_running() && !v[1].is_failed(), "migrate exited 0 is fine");
        assert!(v[2].is_failed());
        let arr = r#"[{"Service":"redis","State":"restarting","Health":"","ExitCode":1}]"#;
        assert!(parse_ps(arr).unwrap()[0].is_failed());
        assert!(parse_ps("").unwrap().is_empty());
        assert!(parse_ps("garbage").is_err());
    }
}
