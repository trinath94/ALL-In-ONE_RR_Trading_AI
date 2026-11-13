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
