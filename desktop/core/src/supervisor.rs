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
//! * The native MT5 connector (a user-session process, never a container) is driven through [`NativeConnector`].

use crate::actions::{import_plan, restore_plan, valid_backup_id, Action, ActionError, BackupPurpose, Service, Step};
use crate::compose::{COUNT_ALL_TABLES, RESET_FOR_NEW_INSTALLATION, SCRATCH_DB};
use std::collections::BTreeMap;
use crate::compose::{self, Compose, ServiceStatus};
use crate::health::{HealthState, ProbeResult};
use crate::runner::{CommandOutput, CommandSpec, Runner};
use crate::migration::{self, SourceRecord};
use crate::secrets::StackSecrets;
use serde::Serialize;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const T_UP: Duration = Duration::from_secs(1800); // first start may build images
const T_STOP: Duration = Duration::from_secs(180);
const T_MIGRATE: Duration = Duration::from_secs(600);
const T_DUMP: Duration = Duration::from_secs(900);
const T_QUICK: Duration = Duration::from_secs(30);
const MIN_FREE_BYTES: u64 = 2 * 1024 * 1024 * 1024;
const WARN_FREE_BYTES: u64 = 10 * 1024 * 1024 * 1024;
pub const KEEP_PRE_MIGRATION_BACKUPS: usize = 10;
pub const KEEP_MANUAL_BACKUPS: usize = 30;
/// Parts 27-30 Phase 7 (Part 30.9): scheduled maintenance.
pub const KEEP_AUTO_BACKUPS: usize = 14;
pub const AUTO_BACKUP_EVERY_SECS: u64 = 24 * 3600;
pub const RESTORE_TEST_EVERY_SECS: u64 = 7 * 24 * 3600;
const MAINTENANCE_FILE: &str = "maintenance.json";

/// What scheduled maintenance last did (kept beside the backups; shown in the window).
#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize, PartialEq)]
pub struct MaintenanceState {
    pub last_auto_backup: Option<u64>,
    pub last_auto_backup_result: Option<String>,
    pub last_restore_test: Option<u64>,
    pub last_restore_test_backup: Option<String>,
    pub last_restore_test_ok: Option<bool>,
    pub last_restore_test_result: Option<String>,
}
/// Phase 18: largest dump accepted for an import.
const MAX_IMPORT_BYTES: u64 = 50_000_000_000;

/// Phase 21: rotate the access secrets (database and Redis passwords, session signing key, bridge secret, local
/// supervisor API key). Everyone is signed out. Runs the first half of `rotate_plans` with the current secrets,
/// changes the database password and saves the new secrets (undone if saving fails), then runs the second half
/// with the new secrets. Returns one report covering all of it.
pub fn rotate_secrets(cfg: SupervisorConfig, runner: &dyn Runner, current: &StackSecrets, probe: fn(u16) -> ProbeResult,
                      connector: Option<&dyn NativeConnector>, save: &dyn Fn(&StackSecrets) -> Result<(), String>,
                      confirmation: Option<&str>) -> Result<PlanReport, ActionError> {
    let (before, after) = crate::actions::rotate_plans(confirmation)?;
    let old = Supervisor { cfg: cfg.clone(), runner, secrets: current, probe: Box::new(probe), connector };
    let mut rep = old.run_plan("rotate_secrets", before);
    if !rep.ok {
        return Ok(rep);
    }
    let t0 = Instant::now();
    let rotated = current.with_rotated_access().map_err(|e| ActionError::Unknown(e.to_string()))?;
    let core: Result<(), String> = (|| {
        old.set_db_password(rotated.db_password())?;
        if let Err(e) = save(&rotated) {
            // the new password is in the database but could not be saved: put the old one back
            let undo = old.set_db_password(current.db_password());
            return Err(format!("the new secrets could not be saved ({e}); {}", match undo {
                Ok(()) => "the database password was put back, nothing changed".to_string(),
                Err(u) => format!("putting the database password back also failed ({u})"),
            }));
        }
        Ok(())
    })();
    let ok = core.is_ok();
    rep.steps.push(StepReport { step: Step::RotateAccessSecrets, outcome: if ok { Outcome::Ok } else { Outcome::Failed },
        detail: core.err().unwrap_or_else(|| "new database and Redis passwords, session signing key, bridge secret and \
            local API key saved (DPAPI); keys that encrypt stored data unchanged; everyone is signed out".into()),
        duration_ms: t0.elapsed().as_millis() as u64 });
    if !ok {
        rep.ok = false;
        return Ok(rep);
    }
    let new = Supervisor { cfg, runner, secrets: &rotated, probe: Box::new(probe), connector };
    let second = new.run_plan("rotate_secrets", after);
    rep.ok = second.ok;
    rep.steps.extend(second.steps);
    rep.services = second.services;
    rep.engine = second.engine;
    Ok(rep)
}

/// One backup in the backups folder (Phase 17).
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct BackupInfo {
    pub id: String,
    /// manual | pre-migrate | pre-restore
    pub kind: String,
    /// Unix seconds, from the file name.
    pub created: u64,
    pub bytes: u64,
    /// Phase 21: encrypted with a backup key (`.rrbk`). Backups made before Phase 21 are plain dumps (`.dump`).
    pub encrypted: bool,
}
const NO_CONNECTOR: &str = "MT5 connector is not available in this installation";

/// The native MT5 connector, as seen by the supervisor (implemented by `connector::LocalConnector`).
pub trait NativeConnector: Send + Sync {
    fn start(&self) -> (Outcome, String);
    fn stop(&self) -> (Outcome, String);
    fn health(&self) -> (Outcome, String);
}

#[derive(Debug, Clone)]
pub struct SupervisorConfig {
    pub compose: Compose,
    pub gateway_port: u16,
    /// Volume that must have free space (application data).
    pub data_dir: PathBuf,
    pub backups_dir: PathBuf,
    /// Phase 21: owner-only folder where a backup is decrypted for the moment of a restore (deleted afterwards).
    pub private_dir: PathBuf,
    pub log_file: Option<PathBuf>,
    /// `Some(path to wsl.exe)` on Windows; WSL2 is then a prerequisite.
    pub wsl: Option<PathBuf>,
    /// Phase 19: data folder of the standalone RR MT5 Connector used with the Ubuntu server
    /// (`%LOCALAPPDATA%\RRMT5Connector`); its presence is reported as a warning.
    pub legacy_connector_dir: Option<PathBuf>,
    /// Tag of the locally built engine images (`RR_ONE_IMAGE_TAG`). Installed builds derive it from the installed
    /// sources, so a new version's images are built by the next Start All instead of reusing the old ones.
    pub image_tag: String,
}

pub type Probe = Box<dyn Fn(u16) -> ProbeResult + Send + Sync>;

pub struct Supervisor<'a> {
    pub cfg: SupervisorConfig,
    pub runner: &'a dyn Runner,
    pub secrets: &'a StackSecrets,
    pub probe: Probe,
    pub connector: Option<&'a dyn NativeConnector>,
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

/// Phase 23: the manifest `scripts/migrate_export.sh` writes next to a dump (`rr_migrate_X.dump` ->
/// `rr_migrate_X.manifest.json`). None when there is none; an error when it does not describe this exact file.
fn read_manifest(source: &Path, size: u64) -> Result<Option<serde_json::Value>, String> {
    let name = source.file_name().and_then(|n| n.to_str()).unwrap_or("");
    let Some(stem) = name.strip_suffix(".dump") else { return Ok(None) };
    let path = source.with_file_name(format!("{stem}.manifest.json"));
    let Ok(text) = std::fs::read(&path) else { return Ok(None) };
    let m: serde_json::Value = serde_json::from_slice(&text).map_err(|_| format!("{} is not a valid manifest", path.display()))?;
    if m["format"].as_str() != Some("rr-migrate-manifest/1") || !m["tables"].is_object() {
        return Err(format!("{} is not an RR Trading One migration manifest", path.display()));
    }
    use sha2::Digest;
    let mut h = sha2::Sha256::new();
    let mut f = std::fs::File::open(source).map_err(|e| format!("cannot read the file: {e}"))?;
    let mut buf = vec![0u8; 1 << 20];
    loop {
        use std::io::Read;
        let n = f.read(&mut buf).map_err(|e| format!("cannot read the file: {e}"))?;
        if n == 0 { break; }
        h.update(&buf[..n]);
    }
    let digest: String = h.finalize().iter().map(|b| format!("{b:02x}")).collect();
    if m["sha256"].as_str() != Some(digest.as_str()) || m["bytes"].as_u64() != Some(size) {
        return Err("the dump does not match its manifest (sha256 or size differ): it was damaged in transfer or is a different \
                    file. Nothing was added; copy it again from the server.".into());
    }
    Ok(Some(m))
}

fn now_secs() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

impl<'a> Supervisor<'a> {
    fn compose(&self, args: Vec<String>, timeout: Duration, stdout_file: Option<PathBuf>) -> Result<CommandOutput, String> {
        let mut env = self.secrets.compose_env();
        env.push(("RR_ONE_IMAGE_TAG".into(), self.cfg.image_tag.clone()));
        let spec = CommandSpec { program: self.cfg.compose.docker.clone(), args, env, timeout, stdout_file, stdin: None };
        self.runner.run(&spec).map_err(|e| format!("could not run docker: {e}"))
    }

    /// Phase 21: set the database role's password. The SQL goes through stdin, never argv; the password is
    /// alphanumeric (generated), so quoting it is safe.
    fn set_db_password(&self, password: &str) -> Result<(), String> {
        if password.len() < 16 || !password.chars().all(|c| c.is_ascii_alphanumeric()) {
            return Err("refusing an unexpected password format".into());
        }
        let mut env = self.secrets.compose_env();
        env.push(("RR_ONE_IMAGE_TAG".into(), self.cfg.image_tag.clone()));
        let spec = CommandSpec { program: self.cfg.compose.docker.clone(), args: self.cfg.compose.psql_stdin("postgres"),
            env, timeout: T_QUICK, stdout_file: None, stdin: Some(format!("ALTER ROLE rr WITH PASSWORD '{password}';\n")) };
        let out = self.runner.run(&spec).map_err(|e| format!("could not run docker: {e}"))?;
        if out.success() { Ok(()) } else { Err(self.secrets.redact(&out.failure_summary()).replace(password, "***")) }
    }

    fn run_ok(&self, args: Vec<String>, timeout: Duration) -> Result<CommandOutput, String> {
        let out = self.compose(args, timeout, None)?;
        if out.success() {
            Ok(out)
        } else {
            Err(self.secrets.redact(&out.failure_summary()))
        }
    }

    /// Issue a one-time MT5 connector registration token inside the API container. The token is returned to the
    /// caller only; it is never logged or placed in a report.
    pub fn issue_agent_token(&self, label: &str) -> Result<String, String> {
        if label.is_empty() || label.len() > 32 || !label.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-') {
            return Err("invalid label".into());
        }
        let out = self.run_ok(self.cfg.compose.issue_agent_token(label), T_QUICK)?;
        let v: serde_json::Value = out.stdout.lines().rev().find_map(|l| serde_json::from_str(l.trim()).ok())
            .ok_or("the engine did not return a token")?;
        v.get("token").and_then(|t| t.as_str()).filter(|t| t.starts_with("rrmt5_")).map(String::from)
            .ok_or_else(|| "the engine did not return a token".into())
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
            let spec = CommandSpec { program: wsl.clone(), args: vec!["--status".into()], env: vec![], timeout: T_QUICK, stdout_file: None, stdin: None };
            let ok = self.runner.run(&spec).map(|o| o.success()).unwrap_or(false);
            checks.push(Check { name: "wsl2", ok, detail: if ok { "WSL available".into() } else { "WSL2 is not installed or not enabled".into() } });
        }
        if let Some(dir) = self.cfg.legacy_connector_dir.as_ref().filter(|d| d.join("config.db").is_file()) {
            // A warning, not a failure: the standalone connector is a different application, linked (if at all) to
            // another engine. The desktop cannot read or stop it; it can say what it means.
            checks.push(Check { name: "previous_connector", ok: true, detail: format!(
                "warning: the standalone RR MT5 Connector from the Ubuntu setup is installed for this user ({}). If it still \
                 runs and is linked to the server, that server can trade the same MT5 terminal. Close it (and disable its \
                 start-up), and stop trading on the server, before trading in LOCAL mode.", dir.display()) });
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
        self.backup("pre-migrate", KEEP_PRE_MIGRATION_BACKUPS)
            .map_err(|e| format!("database backup failed, migrations not applied: {e}"))
    }

    /// Dump the database and encrypt it to `<prefix>-<unix seconds>.rrbk` (Phase 21); keep the newest `keep` of that
    /// kind. The plain dump exists only in the owner-only private folder, until it is encrypted.
    fn backup(&self, prefix: &str, keep: usize) -> Result<String, String> {
        std::fs::create_dir_all(&self.cfg.backups_dir).map_err(|e| format!("backup directory: {e}"))?;
        let key = self.secrets.own_backup_key()?;
        let mut ts = now_secs();
        while self.backup_path(&format!("{prefix}-{ts}")).is_some() {
            ts += 1; // two backups of one kind within a second never overwrite each other
        }
        let id = format!("{prefix}-{ts}");
        let plain = self.private_file(&format!("{id}.dump"))?;
        let res = (|| {
            let out = self.compose(self.cfg.compose.pg_dump(), T_DUMP, Some(plain.clone()))?;
            let size = std::fs::metadata(&plain).map(|m| m.len()).unwrap_or(0);
            if !out.success() || size == 0 {
                return Err(self.secrets.redact(&out.failure_summary()));
            }
            let file = self.cfg.backups_dir.join(format!("{id}.rrbk"));
            let bytes = crate::backupcrypt::encrypt_file(&plain, &file, &key)?;
            Ok(format!("backup {} ({} bytes, encrypted)", file.display(), bytes))
        })();
        let _ = std::fs::remove_file(&plain);
        if res.is_ok() {
            self.prune_backups(prefix, keep);
        }
        res
    }

    /// A file in the private folder (created owner-only). Leftovers of an interrupted run are removed first.
    fn private_file(&self, name: &str) -> Result<PathBuf, String> {
        std::fs::create_dir_all(&self.cfg.private_dir).map_err(|e| format!("private folder: {e}"))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&self.cfg.private_dir, std::fs::Permissions::from_mode(0o700));
        }
        let p = self.cfg.private_dir.join(name);
        let _ = std::fs::remove_file(&p);
        Ok(p)
    }

    /// The file of a backup id: encrypted (`.rrbk`) or, from before Phase 21, a plain dump (`.dump`).
    fn backup_path(&self, id: &str) -> Option<PathBuf> {
        ["rrbk", "dump"].iter().map(|ext| self.cfg.backups_dir.join(format!("{id}.{ext}"))).find(|p| p.is_file())
    }

    /// A plain PostgreSQL dump of a backup, for a restore: an encrypted backup is decrypted into the private folder
    /// (the caller removes it: second value true). A plain dump must start with the PostgreSQL header.
    fn plain_dump(&self, id: &str) -> Result<(PathBuf, bool), String> {
        let file = self.backup_path(id).ok_or_else(|| format!("{id} is not in the backups folder"))?;
        if file.extension().and_then(|e| e.to_str()) == Some("rrbk") {
            let out = self.private_file(&format!("{id}.restore.dump"))?;
            crate::backupcrypt::decrypt_file(&file, &out, &self.secrets.backup_keys()).map_err(|e| e.to_string())?;
            return Ok((out, true));
        }
        let mut head = [0u8; 5];
        let valid = std::fs::File::open(&file).and_then(|mut f| { use std::io::Read; f.read_exact(&mut head) }).is_ok()
            && &head == b"PGDMP";
        if !valid {
            return Err(format!("{id} is missing or is not a PostgreSQL custom-format dump"));
        }
        Ok((file, false))
    }

    /// Phase 21: refuse, before any service is stopped, a backup encrypted with a key this installation does not hold.
    fn check_backup_key(&self, id: &str) -> Result<(), ActionError> {
        if let Some(kid) = self.backup_path(id).as_deref().and_then(crate::backupcrypt::key_id_of) {
            if !self.secrets.backup_keys().iter().any(|k| k.id() == kid) {
                let hex: String = kid.iter().map(|b| format!("{b:02x}")).collect();
                return Err(ActionError::Backup(crate::backupcrypt::DecryptError::UnknownKey(hex).to_string()));
            }
        }
        Ok(())
    }

    /// Phase 21: this installation's backup key as text to keep offline (typed phrase required).
    pub fn export_backup_key(&self, confirmation: Option<&str>) -> Result<serde_json::Value, String> {
        if confirmation != Some(crate::actions::SHOW_KEY_PHRASE) {
            return Err(format!("showing the backup key requires typing exactly: {}", crate::actions::SHOW_KEY_PHRASE));
        }
        let k = self.secrets.own_backup_key()?;
        Ok(serde_json::json!({"key": k.export(), "id": k.id_hex()}))
    }

    /// Keep only the newest dumps of one kind. Touches nothing but files this supervisor created.
    fn prune_backups(&self, prefix: &str, keep: usize) {
        let mut dumps: Vec<(u64, PathBuf)> = self.backup_files().into_iter().filter(|b| b.kind == prefix)
            .filter_map(|b| self.backup_path(&b.id).map(|p| (b.created, p))).collect();
        dumps.sort_by_key(|d| std::cmp::Reverse(d.0));
        for (_, p) in dumps.into_iter().skip(keep) {
            let _ = std::fs::remove_file(p);
        }
    }

    fn backup_files(&self) -> Vec<BackupInfo> {
        let Ok(rd) = std::fs::read_dir(&self.cfg.backups_dir) else { return Vec::new() };
        let mut out: Vec<BackupInfo> = rd
            .filter_map(|e| e.ok())
            .filter(|e| e.file_type().map(|t| t.is_file()).unwrap_or(false))
            .filter_map(|e| {
                let name = e.file_name().to_string_lossy().into_owned();
                let (id, encrypted) = match name.strip_suffix(".rrbk") {
                    Some(id) => (id.to_string(), true),
                    None => (name.strip_suffix(".dump")?.to_string(), false),
                };
                if !valid_backup_id(&id) {
                    return None;
                }
                let (kind, ts) = id.rsplit_once('-')?;
                Some(BackupInfo { id: id.clone(), kind: kind.to_string(), created: ts.parse().ok()?,
                                  bytes: e.metadata().map(|m| m.len()).unwrap_or(0), encrypted })
            })
            .collect();
        out.sort_by_key(|b| std::cmp::Reverse(b.created));
        out
    }

    pub fn maintenance_state(&self) -> MaintenanceState {
        std::fs::read(self.cfg.backups_dir.join(MAINTENANCE_FILE)).ok()
            .and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
    }

    fn save_maintenance(&self, st: &MaintenanceState) {
        let _ = std::fs::create_dir_all(&self.cfg.backups_dir);
        let _ = std::fs::write(self.cfg.backups_dir.join(MAINTENANCE_FILE), serde_json::to_vec_pretty(st).unwrap_or_default());
    }

    /// Restore a backup into a scratch database and check it against the live database: it restores cleanly, it has
    /// every table the live database has, and the same schema version. The live database is never touched; the scratch
    /// database and the copied file are removed afterwards.
    pub fn restore_test(&self, backup: &str) -> Result<String, String> {
        if !valid_backup_id(backup) {
            return Err("invalid backup id".into());
        }
        let (file, temp) = self.plain_dump(backup)?;
        let copied = self.run_ok(self.cfg.compose.copy_into_postgres(&file), T_DUMP);
        if temp {
            let _ = std::fs::remove_file(&file);
        }
        copied.map_err(|e| format!("copy into the database container failed: {e}"))?;
        let drop_scratch = format!("DROP DATABASE IF EXISTS {SCRATCH_DB}");
        let res = (|| {
            self.run_ok(self.cfg.compose.psql("postgres", &drop_scratch), T_QUICK)?;
            self.run_ok(self.cfg.compose.psql("postgres", &format!("CREATE DATABASE {SCRATCH_DB}")), T_QUICK)?;
            self.run_ok(self.cfg.compose.pg_restore_scratch(), T_DUMP).map_err(|e| format!("the backup does not restore: {e}"))?;
            let restored = self.table_counts(SCRATCH_DB)?;
            let live = self.table_counts("rr_trading")?;
            let missing: Vec<&String> = live.keys().filter(|t| !restored.contains_key(*t)).collect();
            let ver = |db: &str| self.pairs(db, "SELECT 'v|' || version_num FROM alembic_version").ok().and_then(|p| p.get("v").cloned());
            let (rv, lv) = (ver(SCRATCH_DB), ver("rr_trading"));
            if restored.is_empty() {
                return Err("the restored backup has no tables".into());
            }
            if !missing.is_empty() {
                return Err(format!("tables missing from the backup: {missing:?}"));
            }
            if rv != lv {
                return Err(format!("schema version differs: backup {rv:?}, live {lv:?}"));
            }
            Ok(format!("{backup} restored into a scratch database: {} tables, {} rows, schema {}; the live database was not touched",
                       restored.len(), restored.values().sum::<u64>(), rv.unwrap_or_default()))
        })();
        let _ = self.run_ok(self.cfg.compose.psql("postgres", &drop_scratch), T_QUICK);
        let _ = self.run_ok(self.cfg.compose.remove_restore_file(), T_QUICK);
        res
    }

    /// Run the scheduled tasks that are due: a daily automatic backup and a weekly restore test of the newest backup.
    /// Only while PostgreSQL is already running: maintenance never starts or stops a service. Returns what it did.
    pub fn scheduled_maintenance(&self, now: u64) -> Vec<String> {
        let running = self.status().map(|s| s.iter().any(|x| x.service == "postgres" && x.state == "running")).unwrap_or(false);
        if !running {
            return vec![];
        }
        let mut st = self.maintenance_state();
        let mut done = Vec::new();
        let last_backup = st.last_auto_backup.or_else(|| self.backup_files().iter().find(|b| b.kind == "auto").map(|b| b.created));
        let due = |last: Option<u64>, every: u64| match last { None => true, Some(t) => now.saturating_sub(t) >= every };
        if due(last_backup, AUTO_BACKUP_EVERY_SECS) {
            let r = self.backup("auto", KEEP_AUTO_BACKUPS);
            st.last_auto_backup = Some(now);
            st.last_auto_backup_result = Some(match &r { Ok(m) => m.clone(), Err(e) => format!("FAILED: {e}") });
            done.push(format!("automatic backup: {}", st.last_auto_backup_result.clone().unwrap_or_default()));
        }
        if due(st.last_restore_test, RESTORE_TEST_EVERY_SECS) {
            if let Some(newest) = self.backup_files().into_iter().next() {
                let r = self.restore_test(&newest.id);
                st.last_restore_test = Some(now);
                st.last_restore_test_backup = Some(newest.id.clone());
                st.last_restore_test_ok = Some(r.is_ok());
                st.last_restore_test_result = Some(match r { Ok(m) => m, Err(e) => format!("FAILED: {e}") });
                done.push(format!("restore test: {}", st.last_restore_test_result.clone().unwrap_or_default()));
            }
        }
        if !done.is_empty() {
            self.save_maintenance(&st);
        }
        done
    }

    /// Phase 17: the backups in the backups folder, newest first.
    pub fn list_backups(&self) -> Vec<BackupInfo> {
        self.backup_files()
    }

    /// Phase 17: restore the database from a backup id (typed phrase required). Runs the fixed `restore_plan`.
    pub fn restore(&self, backup: &str, confirmation: Option<&str>) -> Result<PlanReport, ActionError> {
        let plan = restore_plan(backup, confirmation)?;
        if !self.backup_files().iter().any(|b| b.id == backup) {
            return Err(ActionError::Unknown(format!("backup {backup} (not in the backups folder)")));
        }
        self.check_backup_key(backup)?;
        Ok(self.run_plan("restore_backup", plan))
    }

    /// Phase 18: bring a dump from the previous Ubuntu server (its `scripts/backup.sh` output, PostgreSQL custom format)
    /// into this installation's backups folder as `import-<time>.dump`. The file is only read and copied by this
    /// process (never passed to a command); everything after this uses the backup id. Returns the id.
    pub fn import_dump(&self, source: &Path) -> Result<String, String> {
        let meta = std::fs::metadata(source).map_err(|e| format!("cannot read the file: {e}"))?;
        if !meta.is_file() || meta.len() < 5 {
            return Err("not a file, or empty".into());
        }
        if meta.len() > MAX_IMPORT_BYTES {
            return Err(format!("larger than {} GB", MAX_IMPORT_BYTES / 1_000_000_000));
        }
        let mut head = [0u8; 5];
        std::fs::File::open(source).and_then(|mut f| { use std::io::Read; f.read_exact(&mut head) })
            .map_err(|e| format!("cannot read the file: {e}"))?;
        if &head != b"PGDMP" {
            return Err("not a PostgreSQL custom-format dump (make it with pg_dump -Fc, as the server's backup.sh does)".into());
        }
        // Phase 23: the server's export (scripts/migrate_export.sh) writes a manifest next to the dump. When present,
        // the file must be exactly the one it describes (sha256 and size): a dump damaged in transfer is refused here.
        let manifest = read_manifest(source, meta.len())?;
        std::fs::create_dir_all(&self.cfg.backups_dir).map_err(|e| format!("backup directory: {e}"))?;
        let mut ts = now_secs();
        while self.backup_path(&format!("import-{ts}")).is_some() {
            ts += 1;
        }
        let id = format!("import-{ts}");
        // Phase 21: kept encrypted like every backup; no plain copy of the server's database is made.
        let key = self.secrets.own_backup_key()?;
        crate::backupcrypt::encrypt_file(source, &self.cfg.backups_dir.join(format!("{id}.rrbk")), &key)
            .map_err(|e| format!("copy failed: {e}"))?;
        if let Some(m) = manifest {
            std::fs::write(self.side_file(&id, "manifest.json"), serde_json::to_vec_pretty(&m).unwrap_or_default())
                .map_err(|e| format!("cannot keep the manifest: {e}"))?;
        }
        Ok(id)
    }

    /// Phase 18: import a dump brought in with `import_dump` (typed phrase required). Runs the fixed `import_plan`.
    pub fn import(&self, backup: &str, confirmation: Option<&str>) -> Result<PlanReport, ActionError> {
        let plan = import_plan(backup, confirmation)?;
        if !self.backup_files().iter().any(|b| b.id == backup) {
            return Err(ActionError::Unknown(format!("import {backup} (not in the backups folder)")));
        }
        self.check_backup_key(backup)?;
        let started = now_secs();
        for f in ["source.json", "result.json"] {
            let _ = std::fs::remove_file(self.side_file(backup, f));      // results of an earlier attempt
        }
        let mut rep = self.run_plan("import_database", plan);
        let t0 = Instant::now();
        let (outcome, detail) = match self.write_migration_report(backup, &rep, started) {
            Ok(p) => (Outcome::Ok, format!("migration report: {}", p.display())),
            Err(e) => (Outcome::Failed, format!("the migration report could not be written: {e}")),
        };
        rep.steps.push(StepReport { step: Step::WriteMigrationReport { backup: backup.to_string() }, outcome, detail,
                                    duration_ms: t0.elapsed().as_millis() as u64 });
        Ok(rep)
    }

    /// Phase 23: the migration report of an import, if one was written.
    pub fn migration_report(&self, backup: &str) -> Result<String, String> {
        if !valid_backup_id(backup) {
            return Err("invalid backup id".into());
        }
        std::fs::read_to_string(self.side_file(backup, "migration-report.md")).map_err(|_| format!("no migration report for {backup}"))
    }

    /// A file kept next to a backup (`<id>.<suffix>`): manifest, source record, result, report.
    fn side_file(&self, backup: &str, suffix: &str) -> PathBuf {
        self.cfg.backups_dir.join(format!("{backup}.{suffix}"))
    }

    /// Phase 23: run a SQL script through psql's stdin (generated SQL; identifiers validated by `migration`).
    fn psql_script(&self, database: &str, sql: &str) -> Result<CommandOutput, String> {
        let mut env = self.secrets.compose_env();
        env.push(("RR_ONE_IMAGE_TAG".into(), self.cfg.image_tag.clone()));
        let spec = CommandSpec { program: self.cfg.compose.docker.clone(), args: self.cfg.compose.psql_stdin(database), env,
            timeout: T_DUMP, stdout_file: None, stdin: Some(sql.to_string()) };
        let out = self.runner.run(&spec).map_err(|e| format!("could not run docker: {e}"))?;
        if out.success() { Ok(out) } else { Err(self.secrets.redact(&out.failure_summary())) }
    }

    fn pairs(&self, database: &str, sql: &str) -> Result<BTreeMap<String, String>, String> {
        let out = self.run_ok(self.cfg.compose.psql(database, sql), T_DUMP)?;
        Ok(out.stdout.lines().filter_map(|l| l.trim().split_once('|').map(|(a, b)| (a.to_string(), b.to_string()))).collect())
    }

    /// Records the import could replace, or None when they cannot be read (then only the explicit policy proceeds).
    fn local_records(&self) -> Option<BTreeMap<String, u64>> {
        let m = self.pairs("rr_trading", migration::LOCAL_RECORDS_SQL).ok()?;
        let counts: BTreeMap<String, u64> = m.iter().filter_map(|(k, v)| Some((k.clone(), v.parse().ok()?))).collect();
        (counts.len() == 7).then_some(counts)
    }

    fn source_record(&self, backup: &str) -> Option<SourceRecord> {
        std::fs::read(self.side_file(backup, "source.json")).ok().and_then(|b| serde_json::from_slice(&b).ok())
    }

    fn table_counts(&self, database: &str) -> Result<BTreeMap<String, u64>, String> {
        let out = self.run_ok(self.cfg.compose.psql(database, COUNT_ALL_TABLES), T_DUMP)?;
        Ok(out.stdout.lines().filter_map(|l| {
            let (t, n) = l.split_once('|')?;
            Some((t.trim().to_string(), n.trim().parse().ok()?))
        }).collect())
    }

    /// Restore into a scratch database and record every table's row count and content fingerprint; the real database
    /// is not touched. Phase 23: refuses first when this PC holds trading records and the REPLACE policy was not given.
    fn verify_dump(&self, backup: &str, replace_local: bool) -> (Outcome, String) {
        if !valid_backup_id(backup) {
            return (Outcome::Failed, "invalid backup id".into());
        }
        let local = self.local_records();
        let protected: Vec<String> = local.iter().flat_map(|m| migration::PROTECTED_LOCAL.iter()
            .filter_map(|t| m.get(*t).filter(|n| **n > 0).map(|n| format!("{t} {n}")))).collect();
        if !replace_local {
            if local.is_none() {
                return (Outcome::Failed, format!("this PC's database could not be checked for trading records, so nothing was \
                    replaced. To replace whatever it holds, import with the explicit policy: {}", crate::actions::REPLACE_PHRASE));
            }
            if !protected.is_empty() {
                return (Outcome::Failed, format!("this PC already holds trading records ({}); nothing was replaced. To replace \
                    them with the server's database, import with the explicit policy: {} (a safety backup is taken first)",
                    protected.join(", "), crate::actions::REPLACE_PHRASE));
            }
        }
        let (file, temp) = match self.plain_dump(backup) {
            Ok(x) => x,
            Err(e) => return (Outcome::Failed, e),
        };
        let copied = self.run_ok(self.cfg.compose.copy_into_postgres(&file), T_DUMP);
        if temp {
            let _ = std::fs::remove_file(&file);
        }
        if let Err(e) = copied {
            return (Outcome::Failed, format!("copy into the database container failed: {e}"));
        }
        let drop_scratch = format!("DROP DATABASE IF EXISTS {SCRATCH_DB}");
        let result = (|| {
            self.run_ok(self.cfg.compose.psql("postgres", &drop_scratch), T_QUICK)?;
            self.run_ok(self.cfg.compose.psql("postgres", &format!("CREATE DATABASE {SCRATCH_DB}")), T_QUICK)?;
            self.run_ok(self.cfg.compose.pg_restore_scratch(), T_DUMP)
                .map_err(|e| format!("the dump does not restore cleanly; nothing was replaced: {e}"))?;
            let counts = self.table_counts(SCRATCH_DB)?;
            if counts.is_empty() {
                return Err("the dump contains no tables".into());
            }
            let shapes = migration::parse_catalog(&self.run_ok(self.cfg.compose.psql(SCRATCH_DB, migration::CATALOG_SQL), T_DUMP)?.stdout);
            let fingerprints = migration::fingerprints_for(&shapes, &self.psql_script(SCRATCH_DB, &migration::fingerprint_sql(&shapes, None))?.stdout);
            if fingerprints.len() < shapes.len() || shapes.is_empty() {
                return Err("the dump's content could not be fingerprinted".into());
            }
            let manifest: Option<serde_json::Value> = std::fs::read(self.side_file(backup, "manifest.json")).ok()
                .and_then(|b| serde_json::from_slice(&b).ok());
            if let Some(m) = &manifest {
                // Same snapshot on the server: the restored dump must hold exactly the rows the manifest lists.
                let listed: BTreeMap<String, u64> = m["tables"].as_object().map(|o| o.iter()
                    .filter_map(|(k, v)| Some((k.clone(), v.as_u64()?))).collect()).unwrap_or_default();
                let version = self.pairs(SCRATCH_DB, "SELECT 'v|' || version_num FROM alembic_version").ok()
                    .and_then(|p| p.get("v").cloned());
                let diffs: Vec<String> = listed.keys().chain(counts.keys()).collect::<std::collections::BTreeSet<_>>().into_iter()
                    .filter(|t| listed.get(*t) != counts.get(*t))
                    .map(|t| format!("{t}: manifest {}, dump {}", listed.get(t).map_or("-".into(), |n| n.to_string()),
                                     counts.get(t).map_or("-".into(), |n| n.to_string()))).collect();
                if !diffs.is_empty() || version.as_deref() != m["alembic_version"].as_str() {
                    return Err(format!("the restored dump does not match its manifest; nothing was replaced: {}{}",
                        diffs.join("; "), if version.as_deref() != m["alembic_version"].as_str() { " (schema version differs)" } else { "" }));
                }
            }
            let rec = SourceRecord { counts: counts.clone(), shapes, fingerprints, manifest: manifest.clone(),
                local_before: local.clone().unwrap_or_default(),
                policy: if replace_local { "REPLACE: the PC's database is replaced (explicit policy)".into() }
                        else { "IMPORT: the PC held no trading records".into() } };
            std::fs::write(self.side_file(backup, "source.json"), serde_json::to_vec(&rec).unwrap_or_default())
                .map_err(|e| format!("cannot record the dump's content: {e}"))?;
            Ok((counts, manifest.is_some()))
        })();
        let _ = self.run_ok(self.cfg.compose.psql("postgres", &drop_scratch), T_QUICK);
        let _ = self.run_ok(self.cfg.compose.remove_restore_file(), T_QUICK);
        match result {
            Ok((c, m)) => (Outcome::Ok, format!("restored into a scratch database: {} tables, {} rows, every table's content \
                fingerprinted; {}; nothing replaced yet", c.len(), c.values().sum::<u64>(),
                if m { "matches the server's manifest (sha256, row counts, schema version)" }
                else { "no server manifest (integrity checked by the restore alone)" })),
            Err(e) => (Outcome::Failed, e),
        }
    }

    /// After the real restore and the migrations: every row the server had is present and unchanged (content
    /// fingerprints over the server's columns and rows), grouped into Part 23's domains. Also records what the reset
    /// to a new installation is about to change, for the report.
    fn verify_restored(&self, backup: &str) -> (Outcome, String) {
        let Some(src) = self.source_record(backup) else {
            return (Outcome::Failed, "no record of the verified dump for this import".into());
        };
        let res = (|| -> Result<(Vec<migration::TableResult>, usize), String> {
            let actual = self.table_counts("rr_trading")?;
            let now_shapes = migration::parse_catalog(&self.run_ok(self.cfg.compose.psql("rr_trading", migration::CATALOG_SQL), T_DUMP)?.stdout);
            let shapes: BTreeMap<String, migration::Shape> = src.shapes.iter()
                .filter(|(t, sh)| now_shapes.get(*t).is_some_and(|n| n.has_id == sh.has_id && sh.columns.iter().all(|c| n.columns.contains(c))))
                .map(|(t, sh)| (t.clone(), sh.clone())).collect();
            let now = migration::fingerprints_for(&shapes, &self.psql_script("rr_trading", &migration::fingerprint_sql(&shapes, Some(&src.fingerprints)))?.stdout);
            Ok((migration::compare(&src, &actual, &now), actual.len().saturating_sub(src.counts.len())))
        })();
        let (results, new_tables) = match res {
            Ok(r) => r,
            Err(e) => return (Outcome::Failed, e),
        };
        let observations = self.pairs("rr_trading", migration::POLICY_OBSERVATIONS_SQL).unwrap_or_default();
        let _ = std::fs::write(self.side_file(backup, "result.json"), serde_json::to_vec(&serde_json::json!({
            "tables": results, "new_tables": new_tables, "before_reset": observations })).unwrap_or_default());
        let bad = migration::failed(&results);
        let count = |v: migration::Verdict| results.iter().filter(|r| r.verdict == v).count();
        if bad.is_empty() {
            let grown: Vec<String> = results.iter().filter(|r| r.verdict == migration::Verdict::Grew)
                .map(|r| format!("{} +{}", r.table, r.rows_now.unwrap_or(0).saturating_sub(r.source_rows))).collect();
            (Outcome::Ok, format!("content verified: all {} tables of the server present; {} unchanged, {} with every server \
                row unchanged plus rows this version adds ({}), {} changed by policy; {} new tables from migrations",
                results.len(), count(migration::Verdict::Unchanged), grown.len(), grown.join(", "),
                count(migration::Verdict::Policy), new_tables))
        } else {
            (Outcome::Failed, format!("the migrated database differs from the server's: {}",
                bad.iter().map(|r| format!("{}: {}", r.table, r.detail)).collect::<Vec<_>>().join("; ")))
        }
    }

    /// Phase 23 (Part 23 item 15): the migration report, from the plan's steps and the records kept next to the backup.
    fn write_migration_report(&self, backup: &str, rep: &PlanReport, started: u64) -> Result<PathBuf, String> {
        let src = self.source_record(backup);
        let result: Option<serde_json::Value> = std::fs::read(self.side_file(backup, "result.json")).ok()
            .and_then(|b| serde_json::from_slice(&b).ok());
        let tables: Vec<migration::TableResult> = result.as_ref().and_then(|r| serde_json::from_value(r["tables"].clone()).ok())
            .unwrap_or_default();
        let step_ok = |f: &dyn Fn(&Step) -> bool| rep.steps.iter().find(|s| f(&s.step)).map(|s| s.outcome);
        let mark = |o: Option<Outcome>| match o { Some(Outcome::Ok) => "PASS", Some(Outcome::Failed) => "FAIL", _ => "not reached" };
        let safety = self.list_backups().into_iter().find(|b| b.kind == "pre-restore" && b.created >= started).map(|b| b.id);
        let version_now = self.pairs("rr_trading", "SELECT 'v|' || version_num FROM alembic_version").ok().and_then(|p| p.get("v").cloned());
        let manifest = src.as_ref().and_then(|s| s.manifest.clone());
        let mut md = vec![format!("# Migration report: {backup}"), String::new(),
            format!("* **Outcome:** {}", if rep.ok { "completed and verified" } else { "FAILED (see the steps below)" }),
            format!("* **Written:** {} (Unix time) by RR Trading One {}", now_secs(), env!("CARGO_PKG_VERSION")),
            format!("* **Policy:** {}", src.as_ref().map_or("not reached".to_string(), |s| s.policy.clone())),
            format!("* **Source:** {}", manifest.as_ref().map_or("a dump without a manifest".to_string(), |m| format!(
                "`{}` exported {} from database `{}` (PostgreSQL {}), schema {}, sha256 `{}`, {} bytes",
                m["dump"].as_str().unwrap_or("?"), m["created_utc"].as_str().unwrap_or("?"), m["database"].as_str().unwrap_or("?"),
                m["postgres_version"].as_str().unwrap_or("?"), m["alembic_version"].as_str().unwrap_or("?"),
                m["sha256"].as_str().unwrap_or("?"), m["bytes"]))),
            format!("* **Schema now:** {}", version_now.unwrap_or_else(|| "unknown".into())),
            format!("* **This PC's previous database:** {}", safety.as_ref().map_or("no safety backup was taken (nothing was replaced)".to_string(),
                |id| format!("kept in the safety backup `{id}` (restore it with RESTORE DATABASE to undo this import)"))),
            "* **The Ubuntu server:** not changed by the export or the import; keep it until this installation is accepted (rollback).".into(),
            String::new(), "## Part 23 checklist".into(), String::new(), "| # | Item | Result |".into(), "|---|---|---|".into()];
        let row = |n: usize, item: &str, res: String| format!("| {n} | {item} | {res} |");
        md.push(row(1, "Back up PostgreSQL", format!("{} {}", if manifest.is_some() { "server export with manifest;" } else { "server dump (no manifest);" },
            match step_ok(&|s| matches!(s, Step::BackupDatabase { .. })) { Some(Outcome::Ok) => "this PC's database backed up first: PASS",
                o => if mark(o) == "FAIL" { "this PC's backup: FAIL" } else { "this PC's backup: not reached" } })));
        md.push(row(2, "Verify backup integrity", format!("{}{}", mark(step_ok(&|s| matches!(s, Step::VerifyDump { .. }))),
            if manifest.is_some() { " (sha256 on arrival; restored row counts and schema equal the manifest)" } else { " (scratch restore)" })));
        md.push(row(3, "Restore to local PostgreSQL", mark(step_ok(&|s| matches!(s, Step::RestoreDatabase { .. }))).into()));
        md.push(row(4, "Apply required Alembic migrations", mark(step_ok(&|s| matches!(s, Step::RunMigrations))).into()));
        let doms = migration::domains(&tables);
        for (i, (name, text, ok)) in doms.iter().enumerate() {
            let res = if tables.is_empty() { "not reached".to_string() } else { format!("{}: {text}", if *ok { "PASS" } else { "FAIL" }) };
            md.push(row(5 + i, &format!("Verify {}", name.to_lowercase()), res));
        }
        md.push(row(14, "Verify database row counts", if tables.is_empty() { "not reached".into() } else {
            let lost = tables.iter().filter(|t| t.verdict == migration::Verdict::Lost).count();
            format!("{}: {} tables, no row lost{}", if lost == 0 { "PASS" } else { "FAIL" }, tables.len(),
                if lost == 0 { String::new() } else { format!(" except in {lost} tables") }) }));
        md.push(row(15, "Produce a migration report", "this document".into()));
        if let Some(obs) = result.as_ref().map(|r| r["before_reset"].clone()).filter(|o| o.is_object()) {
            md.extend([String::new(), "## Changed on purpose after verification (start like a new installation)".into(), String::new(),
                format!("* Mode {} → DEMO; auto trading {} → off; reconciliation with the broker required before any entry.",
                        obs["active_mode"].as_str().unwrap_or("?"), obs["auto_trading"].as_str().unwrap_or("?")),
                format!("* LIVE approvals cleared: {} (active LIVE accounts deactivated: {}).", obs["live_approved"].as_str().unwrap_or("?"),
                        obs["live_active"].as_str().unwrap_or("?")),
                format!("* Server identities no longer trusted: {} connector registrations revoked, {} unused link tokens expired, {} sessions revoked.",
                        obs["connectors"].as_str().unwrap_or("?"), obs["link_tokens"].as_str().unwrap_or("?"), obs["sessions"].as_str().unwrap_or("?"))]);
        }
        if let Some(s) = &src {
            md.extend([String::new(), "## This PC before the import".into(), String::new(),
                s.local_before.iter().map(|(k, v)| format!("{k} {v}")).collect::<Vec<_>>().join(", ")]);
        }
        if !tables.is_empty() {
            md.extend([String::new(), "## Tables".into(), String::new(), "| Table | Server rows | Rows now | Result |".into(), "|---|---|---|---|".into()]);
            md.extend(tables.iter().map(|t| format!("| {} | {} | {} | {:?}: {} |", t.table, t.source_rows,
                t.rows_now.map_or("-".into(), |n| n.to_string()), t.verdict, t.detail)));
        }
        md.extend([String::new(), "## Steps".into(), String::new()]);
        md.extend(rep.steps.iter().enumerate().map(|(i, s)| format!("{}. `{}` {:?}: {}", i + 1,
            serde_json::to_value(&s.step).ok().and_then(|v| v["step"].as_str().map(String::from)).unwrap_or_default(), s.outcome,
            s.detail.replace('\n', " "))));
        md.extend([String::new(), "## After the import".into(), String::new(),
            "* Sign in with the server's user accounts; link the MT5 connector on this PC; re-enter AI provider keys and broker passwords (encrypted with the server's keys); retrain models (the server's are unsigned here).".into(),
            "* Before switching, switch auto trading OFF on the server. Keep the server until this installation is accepted.".into()]);
        let path = self.side_file(backup, "migration-report.md");
        std::fs::write(&path, md.join("\n") + "\n").map_err(|e| e.to_string())?;
        Ok(path)
    }

    fn restore_database(&self, backup: &str) -> (Outcome, String) {
        if !valid_backup_id(backup) {
            return (Outcome::Failed, "invalid backup id".into());
        }
        let (file, temp) = match self.plain_dump(backup) {
            Ok(x) => x,
            Err(e) => return (Outcome::Failed, format!("{e}; the database is unchanged. Use Start All to start the engine again.")),
        };
        let copied = self.run_ok(self.cfg.compose.copy_into_postgres(&file), T_DUMP);
        if temp {
            let _ = std::fs::remove_file(&file);
        }
        if let Err(e) = copied {
            return (Outcome::Failed, format!("copy into the database container failed: {e}"));
        }
        let res = self.run_ok(self.cfg.compose.pg_restore_to_sql(), T_DUMP)
            .and_then(|_| self.run_ok(self.cfg.compose.psql_restore(), T_DUMP));
        let _ = self.run_ok(self.cfg.compose.remove_restore_file(), T_QUICK);
        match res {
            Ok(_) => (Outcome::Ok, format!("restored {backup} (single transaction)")),
            Err(e) => (Outcome::Failed, format!("restore failed and was rolled back; the database is unchanged. Use Start All \
                                                 to start the engine again. {e}")),
        }
    }

    fn native(&self, f: impl FnOnce(&dyn NativeConnector) -> (Outcome, String)) -> (Outcome, String) {
        match self.connector {
            Some(c) => f(c),
            None => (Outcome::Skipped, NO_CONNECTOR.into()),
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
        // The connector's state is always reported; it is a failure only when this plan started it.
        let mut connector_note = String::new();
        if self.connector.is_some() {
            let (o, d) = self.native(|c| c.health());
            if o == Outcome::Failed && expected.contains(&Service::Mt5Connector) {
                problems.push(d);
            } else {
                connector_note = format!("; {d}");
            }
        }
        let running = services.iter().filter(|x| x.is_running()).count();
        if problems.is_empty() {
            let eng = engine.as_ref().map(|p| format!("; engine: {}", p.detail)).unwrap_or_else(|| "; engine API not running".into());
            (Outcome::Ok, format!("{running} service(s) running and healthy{eng}{connector_note}"), engine)
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
                let mut detail = String::new();
                if !cs.is_empty() {
                    if let Err(e) = self.run_ok(self.cfg.compose.up(&cs), T_UP) {
                        return (Outcome::Failed, e);
                    }
                    expected.extend(cs.iter().copied());
                    detail = format!("started {}", cs.iter().filter_map(|s| s.compose_name()).collect::<Vec<_>>().join(", "));
                }
                if !native {
                    return (Outcome::Ok, detail);
                }
                let (o, d) = self.native(|c| c.start());
                if o == Outcome::Ok {
                    expected.push(Service::Mt5Connector);
                }
                let joined = if detail.is_empty() { format!("MT5 connector: {d}") } else { format!("{detail}; MT5 connector: {d}") };
                match (o, cs.is_empty()) {
                    (Outcome::Failed, _) => (Outcome::Failed, joined),
                    (Outcome::Skipped, true) => (Outcome::Skipped, joined),
                    _ => (Outcome::Ok, joined),
                }
            }
            Step::StopServices { services } => {
                let (cs, native) = split(services);
                expected.retain(|s| !services.contains(s));
                let mut parts = Vec::new();
                let mut failed = false;
                // Never call `compose stop` without names: that would stop everything.
                if !cs.is_empty() {
                    match self.run_ok(self.cfg.compose.stop(&cs), T_STOP) {
                        Ok(_) => parts.push(format!("stopped {}", cs.iter().filter_map(|s| s.compose_name()).collect::<Vec<_>>().join(", "))),
                        Err(e) => {
                            failed = true;
                            parts.push(e);
                        }
                    }
                }
                let mut skipped_only = cs.is_empty();
                if native {
                    let (o, d) = self.native(|c| c.stop());
                    failed |= o == Outcome::Failed;
                    skipped_only &= o == Outcome::Skipped;
                    parts.push(format!("MT5 connector: {d}"));
                }
                let o = if failed { Outcome::Failed } else if skipped_only { Outcome::Skipped } else { Outcome::Ok };
                (o, parts.join("; "))
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
            Step::BackupDatabase { purpose } => {
                let keep = match purpose { BackupPurpose::Manual => KEEP_MANUAL_BACKUPS, BackupPurpose::PreRestore => KEEP_PRE_MIGRATION_BACKUPS };
                match self.backup(purpose.prefix(), keep) {
                    Ok(d) => (Outcome::Ok, d),
                    Err(e) => (Outcome::Failed, format!("database backup failed: {e}")),
                }
            }
            Step::RestoreDatabase { backup } => self.restore_database(backup),
            Step::VerifyDump { backup, replace_local } => self.verify_dump(backup, *replace_local),
            Step::VerifyRestored { backup } => self.verify_restored(backup),
            Step::RotateAccessSecrets => (Outcome::Failed, "a rotation runs only through rotate_secrets".into()),
            Step::WriteMigrationReport { .. } => (Outcome::Failed, "the report is written by import, after its plan".into()),
            Step::ResetForNewInstallation => match self.run_ok(self.cfg.compose.psql("rr_trading", RESET_FOR_NEW_INSTALLATION), T_QUICK) {
                Ok(_) => (Outcome::Ok, "DEMO, auto trading OFF, LIVE approvals cleared, reconciliation required; the server's connectors, link tokens \
                           and sessions revoked (audited)".into()),
                Err(e) => (Outcome::Failed, e),
            },
            Step::ResetTradingState => match self.run_ok(self.cfg.compose.reset_trading_state(), T_QUICK) {
                Ok(_) => (Outcome::Ok, "auto trading OFF; reconciliation required before any entry".into()),
                Err(e) => (Outcome::Failed, e),
            },
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
        Ok(self.run_plan(action.id(), plan))
    }

    fn run_plan(&self, label: &'static str, plan: Vec<Step>) -> PlanReport {
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
            self.log(label, &rep);
            steps.push(rep);
        }
        let services = self.status().unwrap_or_default();
        let engine = services.iter().any(|x| x.service == "api" && x.is_running()).then(|| (self.probe)(self.cfg.gateway_port));
        PlanReport { action: label, ok: !failed, steps, services, engine }
    }

    fn log(&self, action: &str, rep: &StepReport) {
        let Some(path) = &self.cfg.log_file else { return };
        let line = serde_json::json!({"ts": now_secs(), "action": action, "step": rep.step, "outcome": rep.outcome,
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
{"Service":"claude-code","State":"running","Health":"","ExitCode":0}
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
            private_dir: t.path().join("private"),
            log_file: Some(t.path().join("supervisor.log")),
            wsl: None,
            legacy_connector_dir: None,
            image_tag: "local".into(),
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
        Supervisor { cfg: e.cfg.clone(), runner: r, secrets: &e.secrets, probe, connector: None }
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
            assert_eq!(c.env.len(), 10, "9 stack secrets + the image tag");
            assert!(c.env.contains(&("RR_ONE_IMAGE_TAG".to_string(), "local".to_string())));
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

    struct FakeNative {
        log: Mutex<Vec<String>>,
        start: Outcome,
        healthy: bool,
    }

    impl NativeConnector for FakeNative {
        fn start(&self) -> (Outcome, String) {
            self.log.lock().unwrap().push("native-start".into());
            (self.start, format!("{:?}", self.start))
        }
        fn stop(&self) -> (Outcome, String) {
            self.log.lock().unwrap().push("native-stop".into());
            (Outcome::Ok, "stopped".into())
        }
        fn health(&self) -> (Outcome, String) {
            if self.healthy { (Outcome::Ok, "MT5 connector running (CONNECTED)".into()) } else { (Outcome::Failed, "MT5 connector not running".into()) }
        }
    }

    fn native(start: Outcome, healthy: bool) -> FakeNative {
        FakeNative { log: Mutex::new(vec![]), start, healthy }
    }

    #[test]
    fn start_all_starts_the_native_connector_and_verifies_it() {
        let e = env();
        let r = fake(default_script(ALL_HEALTHY));
        let n = native(Outcome::Ok, true);
        let mut s = sup(&e, &r, healthy_probe());
        s.connector = Some(&n);
        let rep = s.execute(Action::StartAll, None).unwrap();
        assert!(rep.ok, "{rep:#?}");
        assert!(rep.steps.last().unwrap().detail.contains("MT5 connector running"));
        let n2 = native(Outcome::Ok, false);
        s.connector = Some(&n2);
        let rep = s.execute(Action::StartAll, None).unwrap();
        assert!(!rep.ok, "a connector that is not running fails verification");
    }

    #[test]
    fn system_health_reports_the_connector_without_requiring_it() {
        let e = env();
        let r = fake(default_script(ALL_HEALTHY));
        let up = native(Outcome::Ok, true);
        let mut s = sup(&e, &r, healthy_probe());
        s.connector = Some(&up);
        let rep = s.execute(Action::SystemHealth, None).unwrap();
        assert!(rep.ok && rep.steps[0].detail.contains("MT5 connector running"), "{rep:#?}");
        let down = native(Outcome::Ok, false);
        s.connector = Some(&down);
        let rep = s.execute(Action::SystemHealth, None).unwrap();
        assert!(rep.ok, "not started by this plan: reported, not a failure");
        assert!(rep.steps[0].detail.contains("MT5 connector not running"));
    }

    #[test]
    fn connector_start_failure_fails_the_step_and_no_profile_is_skipped() {
        let e = env();
        let r = fake(default_script(ALL_HEALTHY));
        let failing = native(Outcome::Failed, false);
        let mut s = sup(&e, &r, healthy_probe());
        s.connector = Some(&failing);
        let rep = s.execute(Action::StartMt5Connector, None).unwrap();
        assert!(!rep.ok && rep.steps[0].outcome == Outcome::Failed);
        let none = native(Outcome::Skipped, false);
        s.connector = Some(&none);
        let rep = s.execute(Action::StartMt5Connector, None).unwrap();
        assert_eq!(rep.steps[0].outcome, Outcome::Skipped);
    }

    #[test]
    fn stopping_the_connector_stops_the_trading_worker_first() {
        let e = env();
        let r = fake(default_script(ALL_HEALTHY));
        let n = native(Outcome::Ok, true);
        let mut s = sup(&e, &r, healthy_probe());
        s.connector = Some(&n);
        let rep = s.execute(Action::StopMt5Connector, None).unwrap();
        assert!(rep.ok, "{rep:#?}");
        let stops: Vec<String> = compose_cmds(&r).into_iter().filter(|c| c[0] == "stop").map(|c| c.join(" ")).collect();
        assert_eq!(stops, vec!["stop -t 30 trading-worker"]);
        assert_eq!(*n.log.lock().unwrap(), vec!["native-stop"]);
        assert!(rep.steps[0].detail.find("trading-worker").unwrap() < rep.steps[0].detail.find("MT5 connector").unwrap());
    }

    #[test]
    fn agent_token_is_parsed_and_label_validated() {
        let e = env();
        let r = fake(Box::new(|s: &CommandSpec| if has(s, "app.services.agent_tokens") { ok("{\"token\": \"rrmt5_abc\", \"expires_at\": \"x\"}") } else { ok("") }));
        let s = sup(&e, &r, healthy_probe());
        assert_eq!(s.issue_agent_token("demo-1").unwrap(), "rrmt5_abc");
        assert!(s.issue_agent_token("demo 1; rm").is_err());
        let call = r.calls.lock().unwrap().iter().find(|c| has(c, "app.services.agent_tokens")).unwrap().args.clone();
        assert_eq!(&call[5..], &["exec", "-T", "api", "python", "-m", "app.services.agent_tokens", "--label", "demo-1", "--ttl-minutes", "10"]);
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
    fn the_standalone_ubuntu_era_connector_is_reported_as_a_warning() {
        let mut e = env();
        let legacy = e.cfg.data_dir.join("RRMT5Connector");
        e.cfg.legacy_connector_dir = Some(legacy.clone());
        let r = fake(default_script(""));
        let checks = sup(&e, &r, unreachable_probe()).prerequisites();
        assert!(!checks.iter().any(|c| c.name == "previous_connector"), "nothing installed, nothing reported");
        std::fs::create_dir_all(&legacy).unwrap();
        std::fs::write(legacy.join("config.db"), b"x").unwrap();
        let checks = sup(&e, &r, unreachable_probe()).prerequisites();
        let c = checks.iter().find(|c| c.name == "previous_connector").unwrap();
        assert!(c.ok && c.detail.starts_with("warning:") && c.detail.contains("server can trade the same MT5 terminal"), "{c:?}");
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
        sup(&e, &r, healthy_probe()).prune_backups("pre-migrate", KEEP_PRE_MIGRATION_BACKUPS);
        let mut left: Vec<String> = std::fs::read_dir(&e.cfg.backups_dir).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().into_owned()).collect();
        left.sort();
        assert_eq!(left.len(), KEEP_PRE_MIGRATION_BACKUPS + 1);
        assert!(left.contains(&"manual-keep.dump".to_string()) && left.contains(&"pre-migrate-15.dump".to_string()));
        assert!(!left.contains(&"pre-migrate-5.dump".to_string()));
    }

    // ---- Phase 17: backup and restore --------------------------------------------------------------------------
    use crate::actions::RESTORE_PHRASE;

    #[test]
    fn backup_now_writes_a_listed_manual_backup_and_keeps_the_newest() {
        let e = env();
        let r = fake(default_script(ALL_HEALTHY));
        let s = sup(&e, &r, healthy_probe());
        let rep = s.execute(Action::BackupNow, None).unwrap();
        assert!(rep.ok, "{rep:?}");
        let list = s.list_backups();
        assert_eq!(list.len(), 1);
        assert!(list[0].kind == "manual" && list[0].bytes > 0 && valid_backup_id(&list[0].id));
        assert!(compose_cmds(&r).iter().any(|c| c.contains(&"pg_dump".to_string())));
        for i in 0..(KEEP_MANUAL_BACKUPS as u64 + 3) {
            std::fs::write(e.cfg.backups_dir.join(format!("manual-{}.dump", 1000 + i)), b"PGDMP").unwrap();
        }
        std::fs::write(e.cfg.backups_dir.join("pre-migrate-5.dump"), b"PGDMP").unwrap();
        std::fs::write(e.cfg.backups_dir.join("notes.txt"), b"x").unwrap();
        s.execute(Action::BackupNow, None).unwrap();
        let list = s.list_backups();
        assert_eq!(list.iter().filter(|b| b.kind == "manual").count(), KEEP_MANUAL_BACKUPS);
        assert!(list.iter().any(|b| b.id == "pre-migrate-5"), "other kinds are not pruned");
        assert!(!list.iter().any(|b| b.id.contains("notes")), "foreign files are not listed");
        assert!(e.cfg.backups_dir.join("notes.txt").exists(), "foreign files are never touched");
    }

    #[test]
    fn restore_refuses_without_phrase_bad_ids_and_missing_backups() {
        let e = env();
        let r = fake(default_script(ALL_HEALTHY));
        let s = sup(&e, &r, healthy_probe());
        assert_eq!(s.restore("manual-1", None).unwrap_err(), ActionError::RestoreConfirmationRequired);
        assert!(matches!(s.restore("../../etc/passwd", Some(RESTORE_PHRASE)), Err(ActionError::Unknown(_))));
        assert!(matches!(s.restore("manual-1", Some(RESTORE_PHRASE)), Err(ActionError::Unknown(_))), "not on disk");
        assert!(r.calls.lock().unwrap().is_empty(), "nothing was run");
    }

    #[test]
    fn restore_runs_the_fixed_sequence_and_never_starts_the_trading_worker() {
        let e = env();
        std::fs::create_dir_all(&e.cfg.backups_dir).unwrap();
        std::fs::write(e.cfg.backups_dir.join("manual-1700000000.dump"), b"PGDMP-old").unwrap();
        let r = fake(default_script(ALL_HEALTHY));
        let s = sup(&e, &r, healthy_probe());
        let rep = s.restore("manual-1700000000", Some(RESTORE_PHRASE)).unwrap();
        assert!(rep.ok && rep.action == "restore_backup", "{rep:?}");
        let cmds = compose_cmds(&r);
        let at = |pred: &dyn Fn(&Vec<String>) -> bool| cmds.iter().position(pred).unwrap_or_else(|| panic!("{cmds:?}"));
        let safety = at(&|c| c.contains(&"pg_dump".to_string()));
        let stop = at(&|c| c[0] == "stop");
        let copy = at(&|c| c[0] == "cp");
        let to_sql = at(&|c| c.contains(&"pg_restore".to_string()));
        let restore = at(&|c| c.contains(&"--single-transaction".to_string()));
        assert!(to_sql < restore, "the whole dump is read before the database is touched");
        assert!(cmds[restore].iter().any(|a| a.starts_with("DROP SCHEMA public CASCADE")), "{:?}", cmds[restore]);
        let reset = at(&|c| c.iter().any(|a| a.starts_with("UPDATE trading_state SET auto_trading = false")));
        let up_api = at(&|c| c[0] == "up" && c.contains(&"api".to_string()));
        assert!(safety < stop && stop < copy && copy < restore && restore < reset && reset < up_api, "{cmds:?}");
        assert_eq!(cmds[stop][cmds[stop].len() - 8..].first().map(String::as_str), Some("trading-worker"), "{:?}", cmds[stop]);
        assert!(cmds[restore].contains(&"--single-transaction".to_string()));
        assert!(cmds[copy][1].ends_with("manual-1700000000.dump"), "the file is resolved in the backups folder");
        assert!(!cmds.iter().any(|c| c[0] == "up" && c.contains(&"trading-worker".to_string())));
        assert!(s.list_backups().iter().any(|b| b.kind == "pre-restore"), "a safety backup was taken first");
    }

    #[test]
    fn a_failed_restore_stops_the_plan_and_says_the_database_is_unchanged() {
        let e = env();
        std::fs::create_dir_all(&e.cfg.backups_dir).unwrap();
        std::fs::write(e.cfg.backups_dir.join("manual-1700000000.dump"), b"PGDMP-old").unwrap();
        let r = fake(Box::new(|s: &CommandSpec| if has(s, "pg_restore") { fail("pg_restore: error") }
                              else if has(s, "ps") { ok(ALL_HEALTHY) } else { ok("29.0") }));
        let s = sup(&e, &r, healthy_probe());
        let rep = s.restore("manual-1700000000", Some(RESTORE_PHRASE)).unwrap();
        assert!(!rep.ok);
        let st = rep.steps.iter().find(|x| matches!(x.step, Step::RestoreDatabase { .. })).unwrap();
        assert!(st.outcome == Outcome::Failed && st.detail.contains("database is unchanged"), "{st:?}");
        assert!(!compose_cmds(&r).iter().any(|c| c.iter().any(|a| a.starts_with("UPDATE trading_state"))), "nothing after the failure");
    }

    #[test]
    fn a_file_that_is_not_a_dump_is_never_restored() {
        let e = env();
        std::fs::create_dir_all(&e.cfg.backups_dir).unwrap();
        std::fs::write(e.cfg.backups_dir.join("manual-1700000000.dump"), b"not a dump").unwrap();
        let r = fake(default_script(ALL_HEALTHY));
        let s = sup(&e, &r, healthy_probe());
        let rep = s.restore("manual-1700000000", Some(RESTORE_PHRASE)).unwrap();
        assert!(!rep.ok);
        assert!(!compose_cmds(&r).iter().any(|c| c.contains(&"pg_restore".to_string())));
    }

    // ---- Phase 18: import of the previous Ubuntu server's database -------------------------------------------------
    use crate::actions::IMPORT_PHRASE;

    fn counts_script(scratch: &'static str, real: &'static str) -> Script {
        db_script(scratch, real, NO_LOCAL, &[])
    }

    const NO_LOCAL: &str = "closed_positions|0\ntrade_orders|0\ntrading_signals|0\nmt5_deals|0\nstrategy_experiments|0\nmodel_registry|0\nusers|1";

    /// A fake PostgreSQL for imports (Phase 23): row counts of the scratch and the real database, this PC's records, and
    /// the tables whose content differs in the real database after the "migration".
    fn db_script(scratch: &'static str, real: &'static str, local: &'static str, changed: &'static [&'static str]) -> Script {
        Box::new(move |s: &CommandSpec| {
            let scratch_db = has(s, SCRATCH_DB);
            let counts = if scratch_db { scratch } else { real };
            if has(s, "ps") { ok(ALL_HEALTHY) }
            else if has(s, crate::compose::COUNT_ALL_TABLES) { ok(counts) }
            else if has(s, migration::CATALOG_SQL) {
                ok(&counts.lines().map(|l| format!("{}|id,v|true", l.split('|').next().unwrap())).collect::<Vec<_>>().join("\n"))
            }
            else if has(s, migration::LOCAL_RECORDS_SQL) { ok(local) }
            else if has(s, migration::POLICY_OBSERVATIONS_SQL) {
                ok("active_mode|LIVE\nauto_trading|true\nlive_approved|1\nlive_active|1\nconnectors|1\nlink_tokens|0\nsessions|2")
            }
            else if s.args.iter().any(|a| a.contains("alembic_version")) { ok("v|0003") }
            else if let Some(sql) = s.stdin.as_deref().filter(|x| x.contains("SELECT 'f|")) {
                let out: Vec<String> = sql.lines().filter_map(|l| l.split("SELECT 'f|").nth(1)?.split('|').next().map(String::from))
                    .map(|t| {
                        let n = counts.lines().find(|l| l.starts_with(&format!("{t}|"))).and_then(|l| l.split('|').nth(1)).unwrap_or("0");
                        let h = if !scratch_db && changed.contains(&t.as_str()) { "x" } else { "h" };
                        format!("f|{t}|{n}|{h}_{t}")
                    }).collect();
                ok(&out.join("\n"))
            }
            else { ok("29.0") }
        })
    }

    #[test]
    fn import_dump_copies_only_postgres_dumps_into_the_backups_folder() {
        let e = env();
        let r = fake(default_script(ALL_HEALTHY));
        let s = sup(&e, &r, healthy_probe());
        let src = e.cfg.data_dir.join("rr_20261101.dump");
        std::fs::write(&src, b"PGDMP-ubuntu").unwrap();
        let id = s.import_dump(&src).unwrap();
        assert!(id.starts_with("import-") && valid_backup_id(&id));
        assert!(s.list_backups().iter().any(|b| b.id == id && b.kind == "import"));
        let txt = e.cfg.data_dir.join("notes.sql");
        std::fs::write(&txt, b"DROP TABLE users;").unwrap();
        assert!(s.import_dump(&txt).unwrap_err().contains("not a PostgreSQL custom-format dump"));
        assert!(s.import_dump(&e.cfg.data_dir).is_err(), "a directory is refused");
        assert!(r.calls.lock().unwrap().is_empty(), "bringing a file in runs no command");
    }

    // ---- Parts 27-30 Phase 7 (Part 30.9): scheduled backups and restore tests --------------------------------------
    #[test]
    fn scheduled_maintenance_backs_up_daily_and_restore_tests_weekly() {
        let e = env();
        let r = fake(counts_script("users|2\ntrade_orders|7\nalembic_version|1", "users|2\ntrade_orders|7\nalembic_version|1"));
        let s = sup(&e, &r, healthy_probe());
        let t0 = 2_000_000_000u64;
        let done = s.scheduled_maintenance(t0);
        assert_eq!(done.len(), 2, "{done:?}");
        let st = s.maintenance_state();
        assert_eq!(st.last_auto_backup, Some(t0));
        assert_eq!(st.last_restore_test_ok, Some(true), "{st:?}");
        assert!(st.last_restore_test_result.as_deref().unwrap().contains("the live database was not touched"));
        let list = s.list_backups();
        assert!(list.iter().any(|b| b.kind == "auto" && valid_backup_id(&b.id)));
        let cmds = compose_cmds(&r);
        assert!(cmds.iter().any(|c| c.contains(&"pg_restore".to_string()) && c.contains(&SCRATCH_DB.to_string())));
        assert!(!cmds.iter().any(|c| c.contains(&"--single-transaction".to_string())), "the live database is never restored");
        assert!(s.scheduled_maintenance(t0 + 3600).is_empty(), "nothing due an hour later");
        let day = s.scheduled_maintenance(t0 + AUTO_BACKUP_EVERY_SECS);
        assert!(day.len() == 1 && day[0].starts_with("automatic backup"), "{day:?}");
        let week = s.scheduled_maintenance(t0 + RESTORE_TEST_EVERY_SECS);
        assert!(week.iter().any(|d| d.starts_with("restore test")), "{week:?}");
        for i in 0..(KEEP_AUTO_BACKUPS as u64 + 4) {
            std::fs::write(e.cfg.backups_dir.join(format!("auto-{}.dump", 1000 + i)), b"PGDMP").unwrap();
        }
        s.scheduled_maintenance(t0 + 2 * RESTORE_TEST_EVERY_SECS);
        assert_eq!(s.list_backups().iter().filter(|b| b.kind == "auto").count(), KEEP_AUTO_BACKUPS);
    }

    #[test]
    fn scheduled_maintenance_never_starts_the_database() {
        let e = env();
        let stopped = r#"{"Service":"postgres","State":"exited","Health":"","ExitCode":0}"#;
        let r = fake(default_script(stopped));
        let s = sup(&e, &r, healthy_probe());
        assert!(s.scheduled_maintenance(2_000_000_000).is_empty());
        assert!(compose_cmds(&r).iter().all(|c| c[0] == "ps"), "only the status was read");
        assert_eq!(s.maintenance_state(), MaintenanceState::default());
    }

    #[test]
    fn a_restore_test_fails_when_the_backup_lacks_a_live_table() {
        let e = env();
        let r = fake(counts_script("users|2\nalembic_version|1", "users|2\ntrade_orders|7\nalembic_version|1"));
        let s = sup(&e, &r, healthy_probe());
        s.scheduled_maintenance(2_000_000_000);
        let st = s.maintenance_state();
        assert_eq!(st.last_restore_test_ok, Some(false));
        assert!(st.last_restore_test_result.unwrap().contains("trade_orders"));
        assert!(s.restore_test("../etc/passwd").is_err(), "only backup ids");
    }

    #[test]
    fn import_verifies_in_a_scratch_database_and_compares_row_counts() {
        let e = env();
        let r = fake(counts_script("users|2\ntrade_orders|7\nalembic_version|1", "users|2\ntrade_orders|7\nalembic_version|1\nai_usage|0"));
        let s = sup(&e, &r, healthy_probe());
        let src = e.cfg.data_dir.join("rr.dump");
        std::fs::write(&src, b"PGDMP-ubuntu").unwrap();
        let id = s.import_dump(&src).unwrap();
        assert_eq!(s.import(&id, Some(RESTORE_PHRASE)).unwrap_err(), ActionError::ImportConfirmationRequired);
        let rep = s.import(&id, Some(IMPORT_PHRASE)).unwrap();
        assert!(rep.ok && rep.action == "import_database", "{rep:?}");
        let vr = rep.steps.iter().find(|x| matches!(x.step, Step::VerifyRestored { .. })).unwrap();
        assert!(vr.detail.contains("content verified: all 3 tables of the server present") && vr.detail.contains("1 new tables"), "{}", vr.detail);
        let cmds = compose_cmds(&r);
        let scratch_restore = cmds.iter().position(|c| c.contains(&"pg_restore".to_string()) && c.contains(&SCRATCH_DB.to_string())).unwrap();
        let real_restore = cmds.iter().position(|c| c.contains(&"--single-transaction".to_string())).unwrap();
        let reset = cmds.iter().position(|c| c.iter().any(|a| a.contains("active_mode = 'DEMO'"))).unwrap();
        assert!(scratch_restore < real_restore && real_restore < reset);
        assert!(!cmds.iter().any(|c| c[0] == "up" && c.contains(&"trading-worker".to_string())));
    }

    #[test]
    fn an_unrestorable_dump_never_touches_the_real_database() {
        let e = env();
        let r = fake(Box::new(|s: &CommandSpec| if has(s, "pg_restore") { fail("pg_restore: error: invalid TOC") }
                              else if has(s, "ps") { ok(ALL_HEALTHY) } else { ok("29.0") }));
        let s = sup(&e, &r, healthy_probe());
        let src = e.cfg.data_dir.join("rr.dump");
        std::fs::write(&src, b"PGDMP-broken").unwrap();
        let id = s.import_dump(&src).unwrap();
        let rep = s.import(&id, Some(IMPORT_PHRASE)).unwrap();
        assert!(!rep.ok);
        let cmds = compose_cmds(&r);
        assert!(!cmds.iter().any(|c| c[0] == "stop"), "nothing was stopped");
        assert!(!cmds.iter().any(|c| c.contains(&"--single-transaction".to_string())), "the real database was never restored");
        assert!(!s.list_backups().iter().any(|b| b.kind == "pre-restore"), "it failed before the safety backup");
    }

    #[test]
    fn a_lost_row_fails_the_import_but_added_reference_data_does_not() {
        let e = env();
        let r = fake(counts_script("users|2\ntrade_orders|7\npermissions|20", "users|2\ntrade_orders|6\npermissions|25"));
        let s = sup(&e, &r, healthy_probe());
        let src = e.cfg.data_dir.join("rr.dump");
        std::fs::write(&src, b"PGDMP-ubuntu").unwrap();
        let id = s.import_dump(&src).unwrap();
        let rep = s.import(&id, Some(IMPORT_PHRASE)).unwrap();
        assert!(!rep.ok);
        let vr = rep.steps.iter().find(|x| matches!(x.step, Step::VerifyRestored { .. })).unwrap();
        assert!(vr.outcome == Outcome::Failed && vr.detail.contains("trade_orders: 1 of the server's rows are missing"), "{}", vr.detail);
        assert!(!vr.detail.contains("permissions"), "growth of reference data is not a loss");
    }

    // ---- Phase 21: encrypted backups -------------------------------------------------------------------------
    use std::sync::Arc;

    /// Records the content of every file copied into the database container (what a restore actually restores).
    fn capturing(copied: Arc<Mutex<Vec<Vec<u8>>>>) -> Script {
        Box::new(move |spec: &CommandSpec| {
            if let Some(i) = spec.args.iter().position(|a| a == "cp") {
                copied.lock().unwrap().push(std::fs::read(&spec.args[i + 1]).unwrap_or_default());
            }
            if has(spec, "ps") { ok(ALL_HEALTHY) } else { ok("29.0") }
        })
    }

    fn secrets_in(dir: &Path) -> StackSecrets {
        crate::secrets::load_or_create(dir, &crate::secrets::PlainFileProtector).unwrap().0
    }

    #[test]
    fn backups_are_encrypted_and_no_plain_dump_is_left_anywhere() {
        let e = env();
        let r = fake(default_script(ALL_HEALTHY));
        let s = sup(&e, &r, healthy_probe());
        assert!(s.execute(Action::BackupNow, None).unwrap().ok);
        let b = s.list_backups();
        assert!(b.len() == 1 && b[0].kind == "manual" && b[0].encrypted, "{b:?}");
        let raw = std::fs::read(e.cfg.backups_dir.join(format!("{}.rrbk", b[0].id))).unwrap();
        assert!(raw.starts_with(crate::backupcrypt::MAGIC) && !raw.windows(10).any(|w| w == b"PGDMP-fake"));
        let names: Vec<String> = std::fs::read_dir(&e.cfg.backups_dir).unwrap().map(|x| x.unwrap().file_name().to_string_lossy().into()).collect();
        assert_eq!(names, vec![format!("{}.rrbk", b[0].id)], "only the encrypted file");
        assert_eq!(std::fs::read_dir(&e.cfg.private_dir).unwrap().count(), 0, "the plain dump was removed");
    }

    #[test]
    fn an_encrypted_backup_restores_its_exact_dump_and_the_decrypted_copy_is_removed() {
        let e = env();
        let copied = Arc::new(Mutex::new(vec![]));
        let r = fake(capturing(copied.clone()));
        let s = sup(&e, &r, healthy_probe());
        assert!(s.execute(Action::BackupNow, None).unwrap().ok);
        let id = s.list_backups()[0].id.clone();
        let rep = s.restore(&id, Some(RESTORE_PHRASE)).unwrap();
        assert!(rep.ok, "{rep:?}");
        assert_eq!(copied.lock().unwrap().last().unwrap(), b"PGDMP-fake", "the decrypted dump went into the container");
        assert_eq!(std::fs::read_dir(&e.cfg.private_dir).unwrap().count(), 0, "decrypted copy removed");
    }

    #[test]
    fn a_new_pc_restores_another_installations_backup_once_given_its_key() {
        let t = tempfile::tempdir().unwrap();
        let old = env();
        let mut new = env();
        new.cfg.backups_dir = old.cfg.backups_dir.clone();         // the backups folder copied to the new PC
        let r = fake(default_script(ALL_HEALTHY));
        let a = sup(&old, &r, healthy_probe());
        assert!(a.execute(Action::BackupNow, None).unwrap().ok);
        let id = a.list_backups()[0].id.clone();
        assert!(a.export_backup_key(Some("yes")).is_err(), "the key is shown only with the typed phrase");
        let exported = a.export_backup_key(Some(crate::actions::SHOW_KEY_PHRASE)).unwrap()["key"].as_str().unwrap().to_string();

        // the new installation does not hold the key: refused before anything is touched
        let r2 = fake(default_script(ALL_HEALTHY));
        let b = sup(&new, &r2, healthy_probe());
        let err = b.restore(&id, Some(RESTORE_PHRASE)).unwrap_err().to_string();
        assert!(err.contains("encrypted by another installation") && err.contains(&a.secrets.own_backup_key().unwrap().id_hex()), "{err}");
        assert!(r2.calls.lock().unwrap().is_empty(), "refused before anything ran: no service stopped");

        // given the exported key (stored with the new installation's secrets), it restores
        let dir = t.path().join("secrets");
        let _ = secrets_in(&dir);
        crate::secrets::add_backup_key(&dir, &crate::secrets::PlainFileProtector, &exported).unwrap();
        new.secrets = secrets_in(&dir);
        let copied = Arc::new(Mutex::new(vec![]));
        let r3 = fake(capturing(copied.clone()));
        let c = sup(&new, &r3, healthy_probe());
        assert!(c.restore(&id, Some(RESTORE_PHRASE)).unwrap().ok);
        assert_eq!(copied.lock().unwrap().last().unwrap(), b"PGDMP-fake");
    }

    #[test]
    fn a_modified_backup_is_refused_and_a_pre_phase_21_plain_backup_still_restores() {
        let e = env();
        let r = fake(default_script(ALL_HEALTHY));
        let s = sup(&e, &r, healthy_probe());
        assert!(s.execute(Action::BackupNow, None).unwrap().ok);
        let id = s.list_backups()[0].id.clone();
        let p = e.cfg.backups_dir.join(format!("{id}.rrbk"));
        let mut raw = std::fs::read(&p).unwrap();
        let n = raw.len();
        raw[n - 3] ^= 0x40;
        std::fs::write(&p, raw).unwrap();
        let rep = s.restore(&id, Some(RESTORE_PHRASE)).unwrap();
        let st = rep.steps.iter().find(|x| matches!(x.step, Step::RestoreDatabase { .. })).unwrap();
        assert!(!rep.ok && st.detail.contains("integrity check"), "{st:?}");
        std::fs::write(e.cfg.backups_dir.join("manual-1600000000.dump"), b"PGDMP-legacy").unwrap();
        let legacy = s.list_backups().into_iter().find(|b| b.id == "manual-1600000000").unwrap();
        assert!(!legacy.encrypted);
        assert!(s.restore("manual-1600000000", Some(RESTORE_PHRASE)).unwrap().ok);
    }

    #[test]
    fn an_ubuntu_import_is_stored_encrypted_without_a_plain_copy() {
        let e = env();
        let r = fake(default_script(ALL_HEALTHY));
        let s = sup(&e, &r, healthy_probe());
        let src = e.cfg.data_dir.join("rr_server.dump");
        std::fs::write(&src, b"PGDMP-ubuntu-server").unwrap();
        let id = s.import_dump(&src).unwrap();
        let names: Vec<String> = std::fs::read_dir(&e.cfg.backups_dir).unwrap().map(|x| x.unwrap().file_name().to_string_lossy().into()).collect();
        assert_eq!(names, vec![format!("{id}.rrbk")]);
        assert!(s.list_backups()[0].encrypted);
        assert_eq!(std::fs::read(&src).unwrap(), b"PGDMP-ubuntu-server", "the source file is untouched");
    }

    // ---- Phase 21: secret rotation ---------------------------------------------------------------------------
    fn env_of(spec: &CommandSpec, k: &str) -> Option<String> {
        spec.env.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone())
    }

    fn probe_ok(_: u16) -> ProbeResult {
        ProbeResult { target: "engine", state: HealthState::Healthy, detail: "ok".into(), latency_ms: Some(1) }
    }

    #[test]
    fn rotation_backs_up_stops_changes_the_password_saves_and_restarts_with_new_secrets() {
        let e = env();
        let r = fake(default_script(ALL_HEALTHY));
        let saved: Mutex<Option<StackSecrets>> = Mutex::new(None);
        let save = |s: &StackSecrets| { *saved.lock().unwrap() = Some(s.clone()); Ok(()) };
        assert_eq!(rotate_secrets(e.cfg.clone(), &r, &e.secrets, probe_ok, None, &save, Some("yes")).unwrap_err(),
                   ActionError::RotateConfirmationRequired);
        assert!(r.calls.lock().unwrap().is_empty(), "nothing ran without the phrase");
        let rep = rotate_secrets(e.cfg.clone(), &r, &e.secrets, probe_ok, None, &save, Some(crate::actions::ROTATE_PHRASE)).unwrap();
        assert!(rep.ok, "{rep:?}");
        let new = saved.lock().unwrap().clone().expect("saved");
        let old_pw = e.secrets.db_password().to_string();
        let new_pw = new.db_password().to_string();
        assert_ne!(old_pw, new_pw);
        let calls = r.calls.lock().unwrap().clone();
        let pos = |f: &dyn Fn(&CommandSpec) -> bool| calls.iter().position(f).unwrap_or_else(|| panic!("missing call"));
        let dump = pos(&|c| has(c, "pg_dump"));
        let stop = pos(&|c| c.args.get(5).map(String::as_str) == Some("stop"));
        let alter = pos(&|c| c.stdin.as_deref().is_some_and(|i| i.contains("ALTER ROLE rr")));
        let up_new = pos(&|c| has(c, "up") && env_of(c, "RR_ONE_DB_PASSWORD").as_deref() == Some(new_pw.as_str()));
        assert!(dump < stop && stop < alter && alter < up_new, "backup, stop, password, restart");
        assert!(calls[alter].stdin.as_deref().unwrap().contains(&new_pw));
        assert!(calls.iter().all(|c| !c.args.iter().any(|a| a.contains(&new_pw) || a.contains(&old_pw))), "never in argv");
        assert!(!calls[up_new..].iter().any(|c| env_of(c, "RR_ONE_DB_PASSWORD").as_deref() == Some(old_pw.as_str())));
        assert!(!calls.iter().any(|c| has(c, "up") && c.args.contains(&"trading-worker".to_string())), "worker not restarted");
        assert!(rep.steps.iter().any(|s| s.step == Step::RotateAccessSecrets && s.outcome == Outcome::Ok));
    }

    #[test]
    fn a_rotation_whose_secrets_cannot_be_saved_puts_the_old_password_back_and_restarts_nothing() {
        let e = env();
        let r = fake(default_script(ALL_HEALTHY));
        let save = |_: &StackSecrets| Err("disk full".to_string());
        let rep = rotate_secrets(e.cfg.clone(), &r, &e.secrets, probe_ok, None, &save, Some(crate::actions::ROTATE_PHRASE)).unwrap();
        assert!(!rep.ok);
        let st = rep.steps.last().unwrap();
        assert!(st.step == Step::RotateAccessSecrets && st.detail.contains("put back, nothing changed"), "{st:?}");
        let calls = r.calls.lock().unwrap().clone();
        let alters: Vec<&CommandSpec> = calls.iter().filter(|c| c.stdin.is_some()).collect();
        assert_eq!(alters.len(), 2);
        assert!(alters[1].stdin.as_deref().unwrap().contains(e.secrets.db_password()), "the old password restored");
        let first_alter = calls.iter().position(|c| c.stdin.is_some()).unwrap();
        assert!(!calls[first_alter..].iter().any(|c| has(c, "up")), "nothing was started with unsaved secrets");
    }

    // ---- Phase 23: migration utility ---------------------------------------------------------------------------
    fn import_with(e: &Env, r: &FakeRunner, phrase: &str) -> (String, PlanReport) {
        let s = sup(e, r, healthy_probe());
        let src = e.cfg.data_dir.join("rr.dump");
        std::fs::write(&src, b"PGDMP-ubuntu").unwrap();
        let id = s.import_dump(&src).unwrap();
        let rep = s.import(&id, Some(phrase)).unwrap();
        (id, rep)
    }

    const LOCAL_TRADES: &str = "closed_positions|12\ntrade_orders|30\ntrading_signals|0\nmt5_deals|0\nstrategy_experiments|0\nmodel_registry|0\nusers|2";

    #[test]
    fn local_trading_records_are_never_replaced_without_the_explicit_policy() {
        let e = env();
        let r = fake(db_script("users|2\nclosed_positions|240", "users|2\nclosed_positions|240", LOCAL_TRADES, &[]));
        let (id, rep) = import_with(&e, &r, IMPORT_PHRASE);
        let st = rep.steps.iter().find(|x| matches!(x.step, Step::VerifyDump { .. })).unwrap();
        assert!(!rep.ok && st.detail.contains("already holds trading records (closed_positions 12, trade_orders 30)")
                && st.detail.contains(crate::actions::REPLACE_PHRASE), "{st:?}");
        let cmds = compose_cmds(&r);
        assert!(!cmds.iter().any(|c| c[0] == "stop" || c.contains(&"--single-transaction".to_string())), "nothing stopped or replaced");
        let report = sup(&e, &r, healthy_probe()).migration_report(&id).unwrap();
        assert!(report.contains("FAILED") && report.contains("| 3 | Restore to local PostgreSQL | not reached |"), "{report}");

        let r2 = fake(db_script("users|2\nclosed_positions|240", "users|2\nclosed_positions|240", LOCAL_TRADES, &[]));
        let (_, rep) = import_with(&e, &r2, crate::actions::REPLACE_PHRASE);
        assert!(rep.ok, "{rep:?}");
        let rp = rep.steps.last().unwrap();
        assert!(matches!(rp.step, Step::WriteMigrationReport { .. }) && rp.outcome == Outcome::Ok);
    }

    #[test]
    fn an_unreadable_local_database_needs_the_explicit_policy() {
        let e = env();
        let r = fake(db_script("users|2", "users|2", "garbage", &[]));
        let (_, rep) = import_with(&e, &r, IMPORT_PHRASE);
        assert!(!rep.ok && rep.steps.iter().any(|x| x.detail.contains("could not be checked for trading records")));
    }

    #[test]
    fn a_changed_row_fails_its_domain_and_the_report_lists_all_fifteen_items() {
        let e = env();
        let r = fake(db_script("users|2\nclosed_positions|240\nstrategy_experiments|3", "users|2\nclosed_positions|240\nstrategy_experiments|3",
                               NO_LOCAL, &["closed_positions"]));
        let (id, rep) = import_with(&e, &r, IMPORT_PHRASE);
        assert!(!rep.ok);
        let vr = rep.steps.iter().find(|x| matches!(x.step, Step::VerifyRestored { .. })).unwrap();
        assert!(vr.detail.contains("closed_positions: some of the server's rows differ"), "{}", vr.detail);
        let md = sup(&e, &r, healthy_probe()).migration_report(&id).unwrap();
        for n in 1..=15 {
            assert!(md.contains(&format!("| {n} | ")), "item {n} missing:\n{md}");
        }
        assert!(md.contains("| 8 | Verify closed positions | FAIL: closed_positions: some of the server's rows differ"), "{md}");
        assert!(md.contains("| 10 | Verify experiments | PASS: verified: 1 tables, 3 server rows, content unchanged"), "{md}");
        assert!(md.contains("| 6 | Verify account records | PASS: not in the server's schema"), "{md}");
    }

    #[test]
    fn a_complete_import_reports_policy_changes_and_the_safety_backup() {
        let e = env();
        let r = fake(db_script("users|2\ntrading_accounts|2", "users|2\ntrading_accounts|2\nai_usage|0", NO_LOCAL, &[]));
        let (id, rep) = import_with(&e, &r, IMPORT_PHRASE);
        assert!(rep.ok, "{rep:?}");
        let md = sup(&e, &r, healthy_probe()).migration_report(&id).unwrap();
        assert!(md.contains("completed and verified") && md.contains("kept in the safety backup `pre-restore-"), "{md}");
        assert!(md.contains("Mode LIVE → DEMO; auto trading true → off") && md.contains("LIVE approvals cleared: 1"), "{md}");
        assert!(md.contains("| 6 | Verify account records | PASS: verified: 1 tables, 2 server rows, content unchanged |"), "{md}");
        assert!(md.contains("not changed by the export or the import"));
    }

    #[test]
    fn a_manifest_must_describe_the_exact_file_and_its_row_counts() {
        use sha2::Digest;
        let e = env();
        let r = fake(db_script("users|2\nclosed_positions|240", "users|2\nclosed_positions|240", NO_LOCAL, &[]));
        let s = sup(&e, &r, healthy_probe());
        let src = e.cfg.data_dir.join("rr_migrate_1.dump");
        std::fs::write(&src, b"PGDMP-server").unwrap();
        let digest: String = sha2::Sha256::digest(b"PGDMP-server").iter().map(|b| format!("{b:02x}")).collect();
        let man = |sha: &str, users: u64| serde_json::json!({"format": "rr-migrate-manifest/1", "dump": "rr_migrate_1.dump",
            "sha256": sha, "bytes": 12, "alembic_version": "0003", "tables": {"users": users, "closed_positions": 240}});
        let mpath = e.cfg.data_dir.join("rr_migrate_1.manifest.json");
        std::fs::write(&mpath, man("0000", 2).to_string()).unwrap();
        assert!(s.import_dump(&src).unwrap_err().contains("does not match its manifest"), "a damaged transfer is refused");
        assert!(s.list_backups().is_empty());
        std::fs::write(&mpath, man(&digest, 3).to_string()).unwrap();
        let id = s.import_dump(&src).unwrap();
        let rep = s.import(&id, Some(IMPORT_PHRASE)).unwrap();
        let st = rep.steps.iter().find(|x| matches!(x.step, Step::VerifyDump { .. })).unwrap();
        assert!(!rep.ok && st.detail.contains("does not match its manifest") && st.detail.contains("users: manifest 3, dump 2"), "{st:?}");
        std::fs::write(&mpath, man(&digest, 2).to_string()).unwrap();
        let id = s.import_dump(&src).unwrap();
        let rep = s.import(&id, Some(IMPORT_PHRASE)).unwrap();
        assert!(rep.ok, "{rep:?}");
        assert!(rep.steps.iter().any(|x| x.detail.contains("matches the server's manifest")));
        let md = s.migration_report(&id).unwrap();
        assert!(md.contains("sha256 `") && md.contains(&digest) && md.contains("restored row counts and schema equal the manifest"), "{md}");
    }
}
