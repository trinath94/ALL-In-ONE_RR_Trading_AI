//! Executes allowlisted plans against the local stack.
//!
//! The supervisor only runs the typed [`Step`]s produced by [`Action::plan`]. Each step maps to fixed Docker
//! Compose argument vectors ([`crate::compose`]) run through a [`Runner`] (no shell). Stack secrets reach Compose
//! only through the child environment, and any text that is logged or returned is redacted.
//!
//! Semantics:
//! * Start plans stop at the first failed step; later steps are reported as skipped.
//! * Stop plans are best-effort: every stop is attempted.
//! * Migrations never run without a fresh, non-empty database backup.
//! * Health is verified with real checks: Compose health status of every service started in the plan, plus the
//!   engine's `/api/health` through the loopback gateway whenever the API is running.
//! * The native MT5 connector is supervised from Phase 4; until then its steps are reported as skipped.

use crate::actions::{Action, ActionError, Service, Step};
use crate::compose::{self, Compose, ServiceStatus};
use crate::health::{HealthState, ProbeResult};
use crate::runner::{CommandOutput, CommandSpec, Runner};
use crate::secrets::StackSecrets;
use serde::Serialize;
use std::io::Write;
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const T_UP: Duration = Duration::from_secs(1800); // first start may build images
const T_STOP: Duration = Duration::from_secs(180);
const T_MIGRATE: Duration = Duration::from_secs(600);
const T_DUMP: Duration = Duration::from_secs(900);
const T_QUICK: Duration = Duration::from_secs(30);
const MIN_FREE_BYTES: u64 = 2 * 1024 * 1024 * 1024;
const WARN_FREE_BYTES: u64 = 10 * 1024 * 1024 * 1024;
pub const KEEP_PRE_MIGRATION_BACKUPS: usize = 10;
const NATIVE_CONNECTOR_NOTE: &str = "native MT5 connector supervision is delivered in Phase 4";

#[derive(Debug, Clone)]
pub struct SupervisorConfig {
    pub compose: Compose,
    pub gateway_port: u16,
    /// Volume that must have free space (application data).
    pub data_dir: PathBuf,
    pub backups_dir: PathBuf,
    pub log_file: Option<PathBuf>,
    /// `Some(path to wsl.exe)` on Windows; WSL2 is then a prerequisite.
    pub wsl: Option<PathBuf>,
}

pub type Probe = Box<dyn Fn(u16) -> ProbeResult + Send + Sync>;

pub struct Supervisor<'a> {
    pub cfg: SupervisorConfig,
    pub runner: &'a dyn Runner,
    pub secrets: &'a StackSecrets,
    pub probe: Probe,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Ok,
    Failed,
    Skipped,
}

#[derive(Debug, Clone, Serialize)]
pub struct StepReport {
    pub step: Step,
    pub outcome: Outcome,
    pub detail: String,
    pub duration_ms: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct PlanReport {
    pub action: &'static str,
    pub ok: bool,
    pub steps: Vec<StepReport>,
    pub services: Vec<ServiceStatus>,
    pub engine: Option<ProbeResult>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Check {
    pub name: &'static str,
    pub ok: bool,
    pub detail: String,
}

fn service_by_name(name: &str) -> Option<Service> {
    Service::ALL.into_iter().find(|s| s.compose_name() == Some(name))
}

fn split(services: &[Service]) -> (Vec<Service>, bool) {
    let compose: Vec<Service> = services.iter().copied().filter(|s| s.compose_name().is_some()).collect();
    (compose, services.contains(&Service::Mt5Connector))
}

fn now_secs() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

impl<'a> Supervisor<'a> {
    fn compose(&self, args: Vec<String>, timeout: Duration, stdout_file: Option<PathBuf>) -> Result<CommandOutput, String> {
        let spec = CommandSpec { program: self.cfg.compose.docker.clone(), args, env: self.secrets.compose_env(), timeout, stdout_file };
        self.runner.run(&spec).map_err(|e| format!("could not run docker: {e}"))
    }

    fn run_ok(&self, args: Vec<String>, timeout: Duration) -> Result<CommandOutput, String> {
        let out = self.compose(args, timeout, None)?;
        if out.success() {
            Ok(out)
        } else {
            Err(self.secrets.redact(&out.failure_summary()))
        }
    }

    /// Current container states.
    pub fn status(&self) -> Result<Vec<ServiceStatus>, String> {
        let out = self.run_ok(self.cfg.compose.ps(), T_QUICK)?;
        compose::parse_ps(&out.stdout)
    }

    pub fn prerequisites(&self) -> Vec<Check> {
        let mut checks = Vec::new();
        let docker = &self.cfg.compose.docker;
        let docker_ok = docker.is_file();
        checks.push(Check {
            name: "docker_installed",
            ok: docker_ok,
            detail: if docker_ok { docker.display().to_string() } else { "Docker Desktop is not installed (docker executable not found)".into() },
        });
        if docker_ok {
            let v = self.compose(vec!["version".into(), "--format".into(), "{{.Server.Version}}".into()], T_QUICK, None);
            checks.push(match v {
                Ok(o) if o.success() => Check { name: "docker_running", ok: true, detail: format!("Docker Engine {}", o.stdout.trim()) },
                Ok(o) => Check { name: "docker_running", ok: false, detail: format!("Docker Desktop is not running ({})", self.secrets.redact(&o.failure_summary())) },
                Err(e) => Check { name: "docker_running", ok: false, detail: e },
            });
            let c = self.compose(self.cfg.compose.version(), T_QUICK, None);
            checks.push(match c {
                Ok(o) if o.success() => Check { name: "docker_compose", ok: true, detail: format!("Compose {}", o.stdout.trim()) },
                Ok(o) => Check { name: "docker_compose", ok: false, detail: o.failure_summary() },
                Err(e) => Check { name: "docker_compose", ok: false, detail: e },
            });
        }
        if let Some(wsl) = &self.cfg.wsl {
            let spec = CommandSpec { program: wsl.clone(), args: vec!["--status".into()], env: vec![], timeout: T_QUICK, stdout_file: None };
            let ok = self.runner.run(&spec).map(|o| o.success()).unwrap_or(false);
            checks.push(Check { name: "wsl2", ok, detail: if ok { "WSL available".into() } else { "WSL2 is not installed or not enabled".into() } });
        }
        let cf = &self.cfg.compose.file;
        checks.push(Check { name: "compose_file", ok: cf.is_file(), detail: cf.display().to_string() });
        checks.push(match crate::sys::available_space(&self.cfg.data_dir) {
            Ok(b) if b < MIN_FREE_BYTES => Check { name: "disk_space", ok: false, detail: format!("only {:.1} GiB free; at least 2 GiB required", b as f64 / 1073741824.0) },
            Ok(b) if b < WARN_FREE_BYTES => Check { name: "disk_space", ok: true, detail: format!("warning: {:.1} GiB free (10 GiB recommended)", b as f64 / 1073741824.0) },
            Ok(b) => Check { name: "disk_space", ok: true, detail: format!("{:.1} GiB free", b as f64 / 1073741824.0) },
            Err(e) => Check { name: "disk_space", ok: false, detail: format!("cannot determine free space: {e}") },
        });
        let port = self.cfg.gateway_port;
        let probe = (self.probe)(port);
        checks.push(if probe.state == HealthState::Healthy {
            Check { name: "gateway_port", ok: true, detail: format!("127.0.0.1:{port} already served by RR Trading One") }
        } else {
            match std::net::TcpListener::bind(("127.0.0.1", port)) {
                Ok(_) => Check { name: "gateway_port", ok: true, detail: format!("127.0.0.1:{port} is free") },
                Err(_) if probe.state == HealthState::Unhealthy => Check { name: "gateway_port", ok: true, detail: format!("127.0.0.1:{port} held by the RR gateway (engine not healthy yet)") },
                Err(_) => Check { name: "gateway_port", ok: false, detail: format!("127.0.0.1:{port} is used by another program; choose another gateway port in Settings") },
            }
        });
        checks
    }

    fn backup_before_migration(&self) -> Result<String, String> {
        std::fs::create_dir_all(&self.cfg.backups_dir).map_err(|e| format!("backup directory: {e}"))?;
        let file = self.cfg.backups_dir.join(format!("pre-migrate-{}.dump", now_secs()));
        let out = self.compose(self.cfg.compose.pg_dump(), T_DUMP, Some(file.clone()))?;
        let size = std::fs::metadata(&file).map(|m| m.len()).unwrap_or(0);
        if !out.success() || size == 0 {
            let _ = std::fs::remove_file(&file);
            return Err(format!("database backup failed, migrations not applied: {}", self.secrets.redact(&out.failure_summary())));
        }
        self.prune_pre_migration_backups();
        Ok(format!("backup {} ({} bytes)", file.display(), size))
    }

    /// Keep only the newest pre-migration dumps. Touches nothing but files this supervisor created.
    fn prune_pre_migration_backups(&self) {
        let Ok(rd) = std::fs::read_dir(&self.cfg.backups_dir) else { return };
        let mut dumps: Vec<(u64, PathBuf)> = rd
            .filter_map(|e| e.ok())
            .filter_map(|e| {
                let name = e.file_name().to_string_lossy().into_owned();
                let ts = name.strip_prefix("pre-migrate-")?.strip_suffix(".dump")?.parse::<u64>().ok()?;
                e.file_type().ok().filter(|t| t.is_file())?;
                Some((ts, e.path()))
            })
            .collect();
        dumps.sort_by(|a, b| b.0.cmp(&a.0));
        for (_, p) in dumps.into_iter().skip(KEEP_PRE_MIGRATION_BACKUPS) {
            let _ = std::fs::remove_file(p);
        }
    }

    fn verify(&self, expected: &[Service]) -> (Outcome, String, Option<ProbeResult>) {
        let services = match self.status() {
            Ok(s) => s,
            Err(e) => return (Outcome::Failed, format!("cannot read service status: {e}"), None),
        };
        let mut problems = Vec::new();
        for s in expected.iter().filter_map(|s| s.compose_name()) {
            match services.iter().find(|x| x.service == s) {
                None => problems.push(format!("{s} not created")),
                Some(x) if s == "migrate" => {
                    if x.state == "exited" && x.exit_code != 0 {
                        problems.push(format!("migrate failed (exit {})", x.exit_code));
                    }
                }
                Some(x) if !x.is_healthy() => problems.push(format!("{s} {}{}", x.state, if x.health.is_empty() { String::new() } else { format!("/{}", x.health) })),
                _ => {}
            }
        }
        for x in services.iter().filter(|x| x.is_failed() && !expected.iter().any(|e| e.compose_name() == Some(&x.service))) {
            problems.push(format!("{} {} (exit {})", x.service, if x.health == "unhealthy" { "unhealthy" } else { &x.state }, x.exit_code));
        }
        let api_running = services.iter().any(|x| x.service == "api" && x.is_running());
        let engine = if api_running { Some((self.probe)(self.cfg.gateway_port)) } else { None };
        if let Some(p) = &engine {
            if p.state != HealthState::Healthy {
                problems.push(format!("engine health check: {}", p.detail));
            }
        }
        let running = services.iter().filter(|x| x.is_running()).count();
        if problems.is_empty() {
            let eng = engine.as_ref().map(|p| format!("; engine: {}", p.detail)).unwrap_or_else(|| "; engine API not running".into());
            (Outcome::Ok, format!("{running} service(s) running and healthy{eng}"), engine)
        } else {
            (Outcome::Failed, problems.join("; "), engine)
        }
    }

    fn run_step(&self, step: &Step, expected: &mut Vec<Service>) -> (Outcome, String) {
        match step {
            Step::CheckPrerequisites => {
                let checks = self.prerequisites();
                let failed: Vec<String> = checks.iter().filter(|c| !c.ok).map(|c| format!("{}: {}", c.name, c.detail)).collect();
                if failed.is_empty() {
                    (Outcome::Ok, checks.iter().map(|c| c.detail.clone()).collect::<Vec<_>>().join("; "))
                } else {
                    (Outcome::Failed, failed.join("; "))
                }
            }
            Step::StartServices { services } => {
                let (cs, native) = split(services);
                if cs.is_empty() {
                    return (Outcome::Skipped, NATIVE_CONNECTOR_NOTE.into());
                }
                match self.run_ok(self.cfg.compose.up(&cs), T_UP) {
                    Ok(_) => {
                        expected.extend(cs.iter().copied());
                        let names: Vec<_> = cs.iter().filter_map(|s| s.compose_name()).collect();
                        (Outcome::Ok, format!("started {}{}", names.join(", "), if native { format!("; {NATIVE_CONNECTOR_NOTE}") } else { String::new() }))
                    }
                    Err(e) => (Outcome::Failed, e),
                }
            }
            Step::StopServices { services } => {
                let (cs, native) = split(services);
                if cs.is_empty() {
                    // Never call `compose stop` without names: that would stop everything.
                    return (Outcome::Skipped, NATIVE_CONNECTOR_NOTE.into());
                }
                expected.retain(|s| !cs.contains(s));
                match self.run_ok(self.cfg.compose.stop(&cs), T_STOP) {
                    Ok(_) => (Outcome::Ok, format!("stopped {}{}", cs.iter().filter_map(|s| s.compose_name()).collect::<Vec<_>>().join(", "),
                        if native { format!("; {NATIVE_CONNECTOR_NOTE}") } else { String::new() })),
                    Err(e) => (Outcome::Failed, e),
                }
            }
            Step::RunMigrations => {
                let backup = match self.backup_before_migration() {
                    Ok(b) => b,
                    Err(e) => return (Outcome::Failed, e),
                };
                match self.run_ok(self.cfg.compose.migrate(), T_MIGRATE) {
                    Ok(_) => (Outcome::Ok, format!("{backup}; migrations applied")),
                    Err(e) => (Outcome::Failed, format!("{backup}; migration failed: {e}")),
                }
            }
            Step::VerifyHealth => {
                let (o, d, _) = self.verify(expected);
                (o, d)
            }
            Step::RestartUnhealthy => {
                let services = match self.status() {
                    Ok(s) => s,
                    Err(e) => return (Outcome::Failed, e),
                };
                // Never the trading worker (it must only be started explicitly) and never the one-shot migrator.
                let targets: Vec<Service> = services
                    .iter()
                    .filter(|x| x.is_failed())
                    .filter_map(|x| service_by_name(&x.service))
                    .filter(|s| !matches!(s, Service::TradingWorker | Service::Migrate))
                    .collect();
                if targets.is_empty() {
                    return (Outcome::Ok, "no failed services".into());
                }
                match self.run_ok(self.cfg.compose.up(&targets), T_UP) {
                    Ok(_) => {
                        expected.extend(targets.iter().copied());
                        (Outcome::Ok, format!("restarted {}", targets.iter().filter_map(|s| s.compose_name()).collect::<Vec<_>>().join(", ")))
                    }
                    Err(e) => (Outcome::Failed, e),
                }
            }
        }
    }

    pub fn execute(&self, action: Action, confirmation: Option<&str>) -> Result<PlanReport, ActionError> {
        let plan = action.plan(confirmation)?;
        let best_effort = plan.iter().all(|s| matches!(s, Step::StopServices { .. }));
        let mut expected = Vec::new();
        let mut steps = Vec::with_capacity(plan.len());
        let mut failed = false;
        for step in plan {
            let t0 = Instant::now();
            let (outcome, detail) = if failed && !best_effort {
                (Outcome::Skipped, "not run: an earlier step failed".to_string())
            } else {
                self.run_step(&step, &mut expected)
            };
            if outcome == Outcome::Failed {
                failed = true;
            }
            let rep = StepReport { step, outcome, detail: self.secrets.redact(&detail), duration_ms: t0.elapsed().as_millis() as u64 };
            self.log(action, &rep);
            steps.push(rep);
        }
        let services = self.status().unwrap_or_default();
        let engine = services.iter().any(|x| x.service == "api" && x.is_running()).then(|| (self.probe)(self.cfg.gateway_port));
        Ok(PlanReport { action: action.id(), ok: !failed, steps, services, engine })
    }

    fn log(&self, action: Action, rep: &StepReport) {
        let Some(path) = &self.cfg.log_file else { return };
        let line = serde_json::json!({"ts": now_secs(), "action": action.id(), "step": rep.step, "outcome": rep.outcome,
            "detail": rep.detail, "duration_ms": rep.duration_ms});
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
            let _ = writeln!(f, "{line}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::actions::START_WORKER_PHRASE;
    use std::sync::Mutex;

    type Script = Box<dyn Fn(&CommandSpec) -> CommandOutput + Send + Sync>;

    struct FakeRunner {
        calls: Mutex<Vec<CommandSpec>>,
        script: Script,
    }

    impl Runner for FakeRunner {
        fn run(&self, spec: &CommandSpec) -> std::io::Result<CommandOutput> {
            self.calls.lock().unwrap().push(spec.clone());
            if let Some(p) = &spec.stdout_file {
                std::fs::write(p, b"PGDMP-fake")?;
            }
            Ok((self.script)(spec))
        }
    }

    fn ok(stdout: &str) -> CommandOutput {
        CommandOutput { code: Some(0), stdout: stdout.into(), ..Default::default() }
    }

    fn fail(stderr: &str) -> CommandOutput {
        CommandOutput { code: Some(1), stderr: stderr.into(), ..Default::default() }
    }

    const ALL_HEALTHY: &str = r#"{"Service":"postgres","State":"running","Health":"healthy","ExitCode":0}
{"Service":"redis","State":"running","Health":"healthy","ExitCode":0}
{"Service":"migrate","State":"exited","Health":"","ExitCode":0}
{"Service":"api","State":"running","Health":"healthy","ExitCode":0}
{"Service":"frontend","State":"running","Health":"healthy","ExitCode":0}
{"Service":"gateway","State":"running","Health":"healthy","ExitCode":0}
{"Service":"monitor","State":"running","Health":"healthy","ExitCode":0}
{"Service":"analysis","State":"running","Health":"healthy","ExitCode":0}
{"Service":"learner","State":"running","Health":"healthy","ExitCode":0}
{"Service":"trading-worker","State":"running","Health":"healthy","ExitCode":0}"#;

    fn has(spec: &CommandSpec, word: &str) -> bool {
        spec.args.iter().any(|a| a == word)
    }

    fn default_script(ps: &'static str) -> Script {
        Box::new(move |s: &CommandSpec| if has(s, "ps") { ok(ps) } else { ok("29.0") })
    }

    struct Env {
        _t: tempfile::TempDir,
        cfg: SupervisorConfig,
        secrets: StackSecrets,
    }

    fn env() -> Env {
        let t = tempfile::tempdir().unwrap();
        let docker = t.path().join("docker");
        std::fs::write(&docker, b"").unwrap();
        let file = t.path().join("compose.yml");
        std::fs::write(&file, b"").unwrap();
        let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        let cfg = SupervisorConfig {
            compose: Compose { docker, file },
            gateway_port: port,
            data_dir: t.path().to_path_buf(),
            backups_dir: t.path().join("backups"),
            log_file: Some(t.path().join("supervisor.log")),
            wsl: None,
        };
        Env { _t: t, cfg, secrets: StackSecrets::generate().unwrap() }
    }

    fn healthy_probe() -> Probe {
        Box::new(|_| ProbeResult { target: "engine", state: HealthState::Healthy, detail: "ok".into(), latency_ms: Some(1) })
    }

    fn unreachable_probe() -> Probe {
        Box::new(|_| ProbeResult { target: "engine", state: HealthState::Unreachable, detail: "not reachable".into(), latency_ms: None })
    }

    fn sup<'a>(e: &'a Env, r: &'a FakeRunner, probe: Probe) -> Supervisor<'a> {
        Supervisor { cfg: e.cfg.clone(), runner: r, secrets: &e.secrets, probe }
    }

    fn fake(script: Script) -> FakeRunner {
        FakeRunner { calls: Mutex::new(vec![]), script }
    }

    fn compose_cmds(r: &FakeRunner) -> Vec<Vec<String>> {
        // Project-scoped compose commands only (excludes `compose version`), without the 5-arg prefix.
        r.calls.lock().unwrap().iter().filter(|c| c.args.len() > 5 && c.args[0] == "compose" && c.args[1] == "-p")
            .map(|c| c.args[5..].to_vec()).collect()
    }

    #[test]
    fn start_all_runs_in_order_and_never_starts_trading_worker() {
        let e = env();
        let r = fake(default_script(ALL_HEALTHY));
        let rep = sup(&e, &r, healthy_probe()).execute(Action::StartAll, None).unwrap();
        assert!(rep.ok, "{rep:#?}");
        let cmds: Vec<String> = compose_cmds(&r).iter().map(|a| a.join(" ")).collect();
        let pos = |p: &str| cmds.iter().position(|c| c.starts_with(p)).unwrap_or_else(|| panic!("{p} missing in {cmds:?}"));
        assert!(pos("up -d --wait --wait-timeout 300 postgres redis") < pos("exec -T postgres pg_dump"));
        assert!(pos("exec -T postgres pg_dump") < pos("run --rm --no-TTY migrate"));
        assert!(pos("run --rm --no-TTY migrate") < pos("up -d --wait --wait-timeout 300 api frontend gateway monitor analysis"));
        assert!(!cmds.iter().any(|c| c.contains("trading-worker")), "{cmds:?}");
        let connector = rep.steps.iter().find(|s| matches!(&s.step, Step::StartServices { services } if services == &vec![Service::Mt5Connector])).unwrap();
        assert_eq!(connector.outcome, Outcome::Skipped);
        assert!(std::fs::read_dir(&e.cfg.backups_dir).unwrap().count() == 1, "pre-migration backup written");
    }

    #[test]
    fn secrets_only_in_env_never_in_argv_and_redacted_in_log() {
        let e = env();
        let pw = e.secrets.values()[0].to_string();
        let leaky = pw.clone();
        let r = fake(Box::new(move |s: &CommandSpec| if has(s, "migrate") { fail(&format!("auth failed for rr:{leaky}")) } else if has(s, "ps") { ok(ALL_HEALTHY) } else { ok("") }));
        let rep = sup(&e, &r, healthy_probe()).execute(Action::StartAll, None).unwrap();
        assert!(!rep.ok);
        for c in r.calls.lock().unwrap().iter().filter(|c| c.args.first().map(|a| a == "compose").unwrap_or(false)) {
            assert!(c.args.iter().all(|a| !e.secrets.values().iter().any(|v| a.contains(v))), "secret in argv");
            assert_eq!(c.env.len(), 5);
        }
        let json = serde_json::to_string(&rep).unwrap();
        assert!(!json.contains(&pw) && json.contains("rr:***"));
        let log = std::fs::read_to_string(e.cfg.log_file.as_ref().unwrap()).unwrap();
        assert!(!log.contains(&pw) && log.contains("rr:***"));
    }

    #[test]
    fn failure_skips_remaining_start_steps() {
        let e = env();
        let r = fake(Box::new(|s: &CommandSpec| if has(s, "up") && has(s, "postgres") { fail("pull failed") } else if has(s, "ps") { ok("") } else { ok("29") }));
        let rep = sup(&e, &r, unreachable_probe()).execute(Action::StartAll, None).unwrap();
        assert!(!rep.ok);
        let outcomes: Vec<Outcome> = rep.steps.iter().map(|s| s.outcome).collect();
        assert_eq!(outcomes, vec![Outcome::Ok, Outcome::Failed, Outcome::Skipped, Outcome::Skipped, Outcome::Skipped, Outcome::Skipped, Outcome::Skipped]);
        assert!(!compose_cmds(&r).iter().any(|c| c.contains(&"migrate".to_string())));
    }

    #[test]
    fn migration_refused_without_backup() {
        let e = env();
        let r = fake(Box::new(|s: &CommandSpec| if has(s, "pg_dump") { fail("server closed") } else if has(s, "ps") { ok(ALL_HEALTHY) } else { ok("") }));
        let rep = sup(&e, &r, healthy_probe()).execute(Action::StartAll, None).unwrap();
        let mig = rep.steps.iter().find(|s| s.step == Step::RunMigrations).unwrap();
        assert_eq!(mig.outcome, Outcome::Failed);
        assert!(mig.detail.contains("migrations not applied"));
        assert!(!compose_cmds(&r).iter().any(|c| c.first().map(|x| x == "run").unwrap_or(false)));
        assert_eq!(std::fs::read_dir(&e.cfg.backups_dir).unwrap().count(), 0, "failed dump removed");
    }

    #[test]
    fn trading_worker_requires_phrase_and_healthy_system() {
        let e = env();
        let r = fake(default_script(ALL_HEALTHY));
        assert!(sup(&e, &r, healthy_probe()).execute(Action::StartTradingWorker, None).is_err());
        assert!(r.calls.lock().unwrap().is_empty(), "nothing runs without the phrase");
        let rep = sup(&e, &r, unreachable_probe()).execute(Action::StartTradingWorker, Some(START_WORKER_PHRASE)).unwrap();
        assert!(!rep.ok);
        assert!(!compose_cmds(&r).iter().any(|c| c.first().map(|x| x == "up").unwrap_or(false)), "not started on unhealthy engine");
        let rep = sup(&e, &r, healthy_probe()).execute(Action::StartTradingWorker, Some(START_WORKER_PHRASE)).unwrap();
        assert!(rep.ok, "{rep:#?}");
        assert!(compose_cmds(&r).iter().any(|c| c.join(" ") == "up -d --wait --wait-timeout 300 trading-worker"));
    }

    #[test]
    fn stop_all_is_best_effort_single_ordered_stop() {
        let e = env();
        let r = fake(Box::new(|s: &CommandSpec| if has(s, "stop") { fail("one container refused") } else { ok("") }));
        let rep = sup(&e, &r, unreachable_probe()).execute(Action::StopAll, None).unwrap();
        assert!(!rep.ok);
        let stops: Vec<Vec<String>> = compose_cmds(&r).into_iter().filter(|c| c[0] == "stop").collect();
        assert_eq!(stops.len(), 1);
        assert_eq!(stops[0][3], "trading-worker");
        assert_eq!(stops[0].last().unwrap(), "postgres");
    }

    #[test]
    fn stopping_only_the_connector_never_stops_everything() {
        let e = env();
        let r = fake(default_script(ALL_HEALTHY));
        // StopMt5Connector = stop [trading-worker, mt5]: compose stop must name trading-worker only.
        sup(&e, &r, healthy_probe()).execute(Action::StopMt5Connector, None).unwrap();
        let stops: Vec<Vec<String>> = compose_cmds(&r).into_iter().filter(|c| c[0] == "stop").collect();
        assert_eq!(stops, vec![vec!["stop".to_string(), "-t".into(), "30".into(), "trading-worker".into()]]);
    }

    #[test]
    fn recovery_restarts_failed_services_but_never_trading_worker() {
        let e = env();
        let ps = r#"{"Service":"api","State":"running","Health":"unhealthy","ExitCode":0}
{"Service":"learner","State":"exited","Health":"","ExitCode":137}
{"Service":"trading-worker","State":"exited","Health":"","ExitCode":1}
{"Service":"migrate","State":"exited","Health":"","ExitCode":0}"#;
        let r = fake(default_script(ps));
        let _ = sup(&e, &r, healthy_probe()).execute(Action::Recovery, None).unwrap();
        let ups: Vec<String> = compose_cmds(&r).into_iter().filter(|c| c[0] == "up").map(|c| c.join(" ")).collect();
        assert_eq!(ups, vec!["up -d --wait --wait-timeout 300 api learner"]);
    }

    #[test]
    fn verify_fails_on_unhealthy_engine_even_if_containers_report_healthy() {
        let e = env();
        let r = fake(default_script(ALL_HEALTHY));
        let rep = sup(&e, &r, unreachable_probe()).execute(Action::SystemHealth, None).unwrap();
        assert!(!rep.ok);
        assert!(rep.steps[0].detail.contains("engine health check"));
    }

    #[test]
    fn prerequisites_report_missing_docker_and_busy_port() {
        let mut e = env();
        e.cfg.compose.docker = PathBuf::from("/nonexistent/docker");
        let holder = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        e.cfg.gateway_port = holder.local_addr().unwrap().port();
        let r = fake(default_script(""));
        let checks = sup(&e, &r, unreachable_probe()).prerequisites();
        let get = |n: &str| checks.iter().find(|c| c.name == n).unwrap().clone();
        assert!(!get("docker_installed").ok);
        assert!(!get("gateway_port").ok && get("gateway_port").detail.contains("another program"));
        assert!(get("disk_space").ok);
    }

    #[test]
    fn prerequisites_report_docker_not_running() {
        let e = env();
        let r = fake(Box::new(|s: &CommandSpec| if has(s, "{{.Server.Version}}") { fail("Cannot connect to the Docker daemon") } else { ok("2.30") }));
        let checks = sup(&e, &r, unreachable_probe()).prerequisites();
        let c = checks.iter().find(|c| c.name == "docker_running").unwrap();
        assert!(!c.ok && c.detail.contains("not running"));
    }

    #[test]
    fn pre_migration_backups_are_pruned_to_newest() {
        let e = env();
        std::fs::create_dir_all(&e.cfg.backups_dir).unwrap();
        for ts in 1..=15u64 {
            std::fs::write(e.cfg.backups_dir.join(format!("pre-migrate-{ts}.dump")), b"x").unwrap();
        }
        std::fs::write(e.cfg.backups_dir.join("manual-keep.dump"), b"x").unwrap();
        let r = fake(default_script(ALL_HEALTHY));
        sup(&e, &r, healthy_probe()).prune_pre_migration_backups();
        let mut left: Vec<String> = std::fs::read_dir(&e.cfg.backups_dir).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().into_owned()).collect();
        left.sort();
        assert_eq!(left.len(), KEEP_PRE_MIGRATION_BACKUPS + 1);
        assert!(left.contains(&"manual-keep.dump".to_string()) && left.contains(&"pre-migrate-15.dump".to_string()));
        assert!(!left.contains(&"pre-migrate-5.dump".to_string()));
    }
}
