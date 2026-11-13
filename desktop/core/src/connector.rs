//! Native MT5 connector supervision (LOCAL WINDOWS MODE, spec Part 6).
//!
//! The existing `windows-connector` runs headless (`--headless PROFILE.json`) as a normal process in the signed-in
//! user's interactive session: started by this desktop app, never as a service, never in Session 0. It dials out
//! to the local gateway; nothing listens on the PC.
//!
//! * **Profiles** name the terminal executable, the broker server and the expected login. The connector refuses to
//!   use a terminal logged in to another account; nothing ever picks "the first terminal".
//! * **Supervision** is file based, in the connector's own folder: `status.json` (written every second; no
//!   secrets), `stop.request` (graceful stop), `instance.lock` (single instance, held by the OS).
//! * A process is only ever force-terminated when `status.json` proves that this PID is our live connector, so a
//!   recycled PID can never be killed by mistake.
//! * Linking uses a one-time token issued inside the local `api` container and handed to the connector on stdin.

use crate::runner::{CommandSpec, Runner};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const PROFILES_FILE: &str = "mt5_profiles.json";
pub const STATUS_FILE: &str = "status.json";
pub const STOP_FILE: &str = "stop.request";
pub const RUNTIME_PROFILE: &str = "profile.json";
const FRESH_SECS: f64 = 10.0;
const START_WAIT: Duration = Duration::from_secs(25);
const STOP_WAIT: Duration = Duration::from_secs(25);
/// Exit code of `--headless` when the connector is not linked to this engine.
pub const EXIT_NOT_REGISTERED: i32 = 3;

// ---- profiles -------------------------------------------------------------------------------------------------
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Mt5Profile {
    pub profile_id: String,
    pub name: String,
    /// Full path of the selected `terminal64.exe`.
    pub terminal_exe: String,
    /// Broker server name as shown in MetaTrader 5 (e.g. `ICMarkets-Demo`).
    pub server: String,
    #[serde(default = "default_port")]
    pub port: u16,
    /// The account this profile is for. A terminal logged in to any other account is never used.
    pub expected_login: u64,
}

fn default_port() -> u16 {
    443
}

fn host_ok(s: &str) -> bool {
    let b = s.as_bytes();
    !s.is_empty() && s.len() <= 128 && b[0].is_ascii_alphanumeric() && b[b.len() - 1].is_ascii_alphanumeric()
        && s.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_' | ' '))
}

impl Mt5Profile {
    pub fn validate(&self) -> Result<(), String> {
        if self.profile_id.is_empty() || self.profile_id.len() > 32
            || !self.profile_id.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-') {
            return Err("profile id: 1-32 characters a-z, 0-9 or '-'".into());
        }
        if self.name.trim().is_empty() || self.name.len() > 64 || self.name.chars().any(char::is_control) {
            return Err("profile name: 1-64 printable characters".into());
        }
        let exe = self.terminal_exe.trim();
        if !exe.to_ascii_lowercase().replace('/', "\\").ends_with("\\terminal64.exe")
            || exe.chars().any(|c| c.is_control() || c == '"') || exe.len() > 512 {
            return Err("terminal: select an installed MetaTrader 5 terminal64.exe".into());
        }
        if !host_ok(&self.server) {
            return Err("server: enter the broker server name shown in MetaTrader 5".into());
        }
        if self.port == 0 {
            return Err("port: 1-65535".into());
        }
        if self.expected_login == 0 {
            return Err("account login: enter the MT5 account number this profile is for".into());
        }
        Ok(())
    }

    /// Login masked for display (last three digits).
    pub fn masked_login(&self) -> String {
        let s = self.expected_login.to_string();
        let keep = s.len().min(3);
        "•".repeat(s.len() - keep) + &s[s.len() - keep..]
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Profiles {
    pub profiles: Vec<Mt5Profile>,
    /// Explicitly selected profile. None until the user picks one.
    pub active: Option<String>,
}

impl Profiles {
    pub fn load(path: &Path) -> Result<Self, String> {
        match std::fs::read_to_string(path) {
            Ok(t) => {
                let p: Profiles = serde_json::from_str(&t).map_err(|e| format!("MT5 profiles file is invalid: {e}"))?;
                for x in &p.profiles {
                    x.validate()?;
                }
                Ok(p)
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Profiles::default()),
            Err(e) => Err(e.to_string()),
        }
    }

    pub fn save(&self, path: &Path) -> Result<(), String> {
        for x in &self.profiles {
            x.validate()?;
        }
        if let Some(a) = &self.active {
            if !self.profiles.iter().any(|p| &p.profile_id == a) {
                return Err("active profile does not exist".into());
            }
        }
        write_atomic(path, serde_json::to_string_pretty(self).expect("serialise").as_bytes()).map_err(|e| e.to_string())
    }

    pub fn upsert(&mut self, p: Mt5Profile) -> Result<(), String> {
        p.validate()?;
        match self.profiles.iter_mut().find(|x| x.profile_id == p.profile_id) {
            Some(x) => *x = p,
            None => {
                if self.profiles.len() >= 20 {
                    return Err("at most 20 MT5 profiles".into());
                }
                self.profiles.push(p)
            }
        }
        Ok(())
    }

    pub fn remove(&mut self, id: &str) {
        self.profiles.retain(|p| p.profile_id != id);
        if self.active.as_deref() == Some(id) {
            self.active = None;
        }
    }

    pub fn select(&mut self, id: &str) -> Result<(), String> {
        if !self.profiles.iter().any(|p| p.profile_id == id) {
            return Err("unknown profile".into());
        }
        self.active = Some(id.to_string());
        Ok(())
    }

    pub fn active_profile(&self) -> Option<&Mt5Profile> {
        let a = self.active.as_ref()?;
        self.profiles.iter().find(|p| &p.profile_id == a)
    }
}

fn write_atomic(path: &Path, data: &[u8]) -> std::io::Result<()> {
    if let Some(d) = path.parent() {
        std::fs::create_dir_all(d)?;
    }
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, data)?;
    std::fs::rename(&tmp, path)
}

// ---- launching ------------------------------------------------------------------------------------------------
/// How to run the connector: the packaged executable, or (development) a Python interpreter with the source tree.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Launch {
    pub program: PathBuf,
    pub prefix: Vec<String>,
    pub env: Vec<(String, String)>,
}

/// Packaged: `<install or ancestor>\mt5-connector\RR_MT5_Connector.exe`. Development: `RR_ONE_CONNECTOR_PYTHON`
/// (absolute interpreter) + `RR_ONE_CONNECTOR_SRC` (absolute `windows-connector` folder), optionally
/// `RR_ONE_CONNECTOR_ENTRY` (absolute script used instead of `-m rr_connector`, e.g. a test harness).
pub fn find_launch(install_dir: &Path, env: impl Fn(&str) -> Option<String>) -> Option<Launch> {
    let abs = |k: &str| env(k).map(PathBuf::from).filter(|p| p.is_absolute());
    if let (Some(py), Some(src)) = (abs("RR_ONE_CONNECTOR_PYTHON"), abs("RR_ONE_CONNECTOR_SRC")) {
        if py.is_file() && src.is_dir() {
            let mtsrc = src.parent().map(|p| p.join("mt5-connector")).unwrap_or_default();
            let sep = if cfg!(windows) { ";" } else { ":" };
            let pythonpath = format!("{}{sep}{}", src.display(), mtsrc.display());
            let prefix = match abs("RR_ONE_CONNECTOR_ENTRY").filter(|p| p.is_file()) {
                Some(entry) => vec![entry.to_string_lossy().into_owned()],
                None => vec!["-m".into(), "rr_connector".into()],
            };
            return Some(Launch { program: py, prefix, env: vec![("PYTHONPATH".into(), pythonpath)] });
        }
    }
    install_dir.ancestors().take(5).map(|a| a.join("mt5-connector").join("RR_MT5_Connector.exe")).find(|p| p.is_file())
        .map(|program| Launch { program, prefix: vec![], env: vec![] })
}

/// Must match `rr_connector.headless.home()`; also passed explicitly as `RRMT5_HOME`, so the two never disagree.
pub fn connector_home(windows: bool, env: impl Fn(&str) -> Option<String>) -> Option<PathBuf> {
    if windows {
        env("LOCALAPPDATA").filter(|v| !v.is_empty()).map(|d| PathBuf::from(d).join("RR Trading One").join("connector"))
    } else {
        env("HOME").filter(|v| !v.is_empty()).map(|h| PathBuf::from(h).join(".rr-trading-one-connector"))
    }
}

// ---- status ---------------------------------------------------------------------------------------------------
#[derive(Debug, Clone, Serialize)]
pub struct ConnectorStatus {
    /// A live connector process is writing status (fresh within 10 s and not stopped).
    pub running: bool,
    pub state: String,
    pub detail: String,
    pub pid: Option<u32>,
    /// The full status document written by the connector (login masked, no secrets).
    pub doc: Option<serde_json::Value>,
}

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

pub fn read_status(home: &Path) -> ConnectorStatus {
    let doc: Option<serde_json::Value> =
        std::fs::read_to_string(home.join(STATUS_FILE)).ok().and_then(|t| serde_json::from_str(&t).ok());
    let Some(d) = doc else {
        return ConnectorStatus { running: false, state: "NOT_STARTED".into(), detail: String::new(), pid: None, doc: None };
    };
    let state = d.get("state").and_then(|v| v.as_str()).unwrap_or("UNKNOWN").to_string();
    let updated = d.get("updated_at").and_then(|v| v.as_f64()).unwrap_or(0.0);
    let pid = d.get("pid").and_then(|v| v.as_u64()).map(|p| p as u32);
    let terminal = matches!(state.as_str(), "STOPPED" | "NOT_REGISTERED" | "INVALID_PROFILE");
    let running = !terminal && (now() - updated) < FRESH_SECS && pid.map(pid_alive).unwrap_or(false);
    let detail = d.get("detail").and_then(|v| v.as_str()).unwrap_or("").to_string();
    ConnectorStatus { running, state: if running || terminal { state } else { "NOT_RUNNING".into() }, detail, pid, doc: Some(d) }
}

#[cfg(unix)]
pub fn pid_alive(pid: u32) -> bool {
    // SAFETY: signal 0 only checks existence/permission.
    pid > 0 && unsafe { libc::kill(pid as i32, 0) } == 0
}

#[cfg(windows)]
pub fn pid_alive(pid: u32) -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, STILL_ACTIVE};
    use windows_sys::Win32::System::Threading::{GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION};
    // SAFETY: handle checked for null and closed.
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if h.is_null() {
            return false;
        }
        let mut code = 0u32;
        let ok = GetExitCodeProcess(h, &mut code);
        CloseHandle(h);
        ok != 0 && code == STILL_ACTIVE as u32
    }
}

#[cfg(unix)]
fn terminate(pid: u32) -> bool {
    // SAFETY: plain kill(2) on a PID verified to be our live connector.
    unsafe { libc::kill(pid as i32, libc::SIGKILL) == 0 }
}

#[cfg(windows)]
fn terminate(pid: u32) -> bool {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Threading::{OpenProcess, TerminateProcess, PROCESS_TERMINATE};
    // SAFETY: handle checked for null and closed.
    unsafe {
        let h = OpenProcess(PROCESS_TERMINATE, 0, pid);
        if h.is_null() {
            return false;
        }
        let ok = TerminateProcess(h, 1);
        CloseHandle(h);
        ok != 0
    }
}

// ---- control --------------------------------------------------------------------------------------------------
pub struct ConnectorCtl<'a> {
    pub launch: Launch,
    pub home: PathBuf,
    /// Engine address the connector dials (LOCAL mode: the loopback gateway).
    pub engine_url: String,
    pub runner: &'a dyn Runner,
}

impl ConnectorCtl<'_> {
    fn spec(&self, args: &[&str], timeout: Duration, stdin: Option<String>) -> CommandSpec {
        let mut a = self.launch.prefix.clone();
        a.extend(args.iter().map(|s| s.to_string()));
        let mut env = self.launch.env.clone();
        env.push(("RRMT5_HOME".into(), self.home.to_string_lossy().into_owned()));
        CommandSpec { program: self.launch.program.clone(), args: a, env, timeout, stdout_file: None, stdin }
    }

    fn last_json_line(out: &str) -> Option<serde_json::Value> {
        out.lines().rev().find_map(|l| serde_json::from_str(l.trim()).ok())
    }

    /// Installed MetaTrader 5 terminals.
    pub fn detect(&self) -> Result<Vec<serde_json::Value>, String> {
        let out = self.runner.run(&self.spec(&["--detect-json"], Duration::from_secs(60), None)).map_err(|e| e.to_string())?;
        if !out.success() {
            return Err(format!("terminal detection failed: {}", out.failure_summary()));
        }
        match Self::last_json_line(&out.stdout) {
            Some(serde_json::Value::Array(v)) => Ok(v),
            _ => Err("terminal detection returned no data".into()),
        }
    }

    /// Link the connector to the engine with a one-time token (passed on stdin, never in argv).
    pub fn register(&self, token: &str) -> Result<serde_json::Value, String> {
        let out = self.runner
            .run(&self.spec(&["--register-stdin", &self.engine_url], Duration::from_secs(60), Some(format!("{token}\n"))))
            .map_err(|e| e.to_string())?;
        let v = Self::last_json_line(&out.stdout).unwrap_or_default();
        if out.success() && v.get("ok").and_then(|x| x.as_bool()) == Some(true) {
            Ok(v)
        } else {
            let msg = v.get("error").and_then(|x| x.as_str()).map(String::from).unwrap_or_else(|| out.failure_summary());
            Err(format!("linking failed: {}", msg.replace(token, "***")))
        }
    }

    pub fn status(&self) -> ConnectorStatus {
        read_status(&self.home)
    }

    /// Start (or confirm) the connector for `profile`. Returns a human-readable state.
    pub fn start(&self, profile: &Mt5Profile) -> Result<String, String> {
        profile.validate()?;
        std::fs::create_dir_all(&self.home).map_err(|e| e.to_string())?;
        let st = self.status();
        if st.running {
            let same = st.doc.as_ref().and_then(|d| d.pointer("/profile/id")).and_then(|v| v.as_str()) == Some(&profile.profile_id);
            return if same { Ok(format!("already running ({})", st.state)) } else {
                Err("the connector is running with another profile; stop it first".into())
            };
        }
        let mut doc = serde_json::to_value(profile).expect("profile json");
        doc["engine_url"] = serde_json::Value::String(self.engine_url.clone());
        let ppath = self.home.join(RUNTIME_PROFILE);
        write_atomic(&ppath, doc.to_string().as_bytes()).map_err(|e| e.to_string())?;
        let _ = std::fs::remove_file(self.home.join(STOP_FILE));
        let pid = self.spawn_detached(&ppath)?;
        let t0 = Instant::now();
        while t0.elapsed() < START_WAIT {
            std::thread::sleep(Duration::from_millis(250));
            let st = self.status();
            let ours = st.pid == Some(pid);
            if ours && st.running {
                return Ok(format!("running ({}{})", st.state, if st.detail.is_empty() { String::new() } else { format!(": {}", st.detail) }));
            }
            if ours && matches!(st.state.as_str(), "NOT_REGISTERED" | "INVALID_PROFILE") {
                return Err(match st.state.as_str() {
                    "NOT_REGISTERED" => "the connector is not linked to this engine yet: use Link in MT5 Manager".into(),
                    _ => format!("invalid MT5 profile: {}", st.detail),
                });
            }
            if !pid_alive(pid) && !ours {
                return Err(format!("the connector exited during start-up (see {})", self.home.join("logs").display()));
            }
        }
        Err("the connector did not report status within 25 s".into())
    }

    fn spawn_detached(&self, profile_path: &Path) -> Result<u32, String> {
        use std::process::{Command, Stdio};
        let logs = self.home.join("logs");
        std::fs::create_dir_all(&logs).map_err(|e| e.to_string())?;
        let out = std::fs::OpenOptions::new().create(true).append(true).open(logs.join("headless.out")).map_err(|e| e.to_string())?;
        let err = out.try_clone().map_err(|e| e.to_string())?;
        let spec = self.spec(&["--headless", &profile_path.to_string_lossy()], Duration::ZERO, None);
        let mut cmd = Command::new(&spec.program);
        cmd.args(&spec.args).stdin(Stdio::null()).stdout(Stdio::from(out)).stderr(Stdio::from(err));
        for (k, v) in &spec.env {
            cmd.env(k, v);
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW);
        }
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            cmd.process_group(0); // survives the desktop app; not killed with its process group
        }
        let mut child = cmd.spawn().map_err(|e| format!("could not start the MT5 connector: {e}"))?;
        let pid = child.id();
        std::thread::spawn(move || {
            let _ = child.wait(); // reap when it exits (no zombie while the desktop app runs)
        });
        Ok(pid)
    }

    /// Graceful stop through `stop.request`; force-terminate only a PID proven to be our live connector.
    pub fn stop(&self) -> Result<String, String> {
        let st = self.status();
        if !st.running {
            return Ok(format!("not running ({})", st.state));
        }
        let pid = st.pid;
        std::fs::write(self.home.join(STOP_FILE), b"stop").map_err(|e| e.to_string())?;
        let t0 = Instant::now();
        while t0.elapsed() < STOP_WAIT {
            std::thread::sleep(Duration::from_millis(250));
            let now = self.status();
            if !now.running && pid.map(|p| !pid_alive(p)).unwrap_or(true) {
                return Ok("stopped".into());
            }
            if now.state == "STOPPED" {
                return Ok("stopped".into());
            }
        }
        let fresh = self.status();
        match (pid, fresh.running && fresh.pid == pid) {
            (Some(p), true) if terminate(p) => Ok("did not stop within 25 s; terminated (the execution journal is crash-safe)".into()),
            _ => Err("the connector did not confirm the stop; check MT5 Manager".into()),
        }
    }
}

/// Everything needed to control the connector on this machine.
#[derive(Debug, Clone)]
pub struct ConnectorSetup {
    pub launch: Launch,
    pub home: PathBuf,
    pub engine_url: String,
    /// The explicitly selected profile, if any.
    pub profile: Option<Mt5Profile>,
}

impl ConnectorSetup {
    pub fn ctl<'a>(&self, runner: &'a dyn Runner) -> ConnectorCtl<'a> {
        ConnectorCtl { launch: self.launch.clone(), home: self.home.clone(), engine_url: self.engine_url.clone(), runner }
    }
}

/// The supervisor's view of the connector: starting needs an explicitly selected profile.
pub struct LocalConnector<'a> {
    pub ctl: ConnectorCtl<'a>,
    pub profile: Option<Mt5Profile>,
}

impl crate::supervisor::NativeConnector for LocalConnector<'_> {
    fn start(&self) -> (crate::supervisor::Outcome, String) {
        use crate::supervisor::Outcome;
        match &self.profile {
            None => (Outcome::Skipped, "no MT5 profile selected (choose one in MT5 Manager)".into()),
            Some(p) => match self.ctl.start(p) {
                Ok(m) => (Outcome::Ok, format!("{} [{}]", m, p.name)),
                Err(e) => (Outcome::Failed, e),
            },
        }
    }

    fn stop(&self) -> (crate::supervisor::Outcome, String) {
        use crate::supervisor::Outcome;
        match self.ctl.stop() {
            Ok(m) => (Outcome::Ok, m),
            Err(e) => (Outcome::Failed, e),
        }
    }

    fn health(&self) -> (crate::supervisor::Outcome, String) {
        use crate::supervisor::Outcome;
        let st = self.ctl.status();
        if st.running {
            (Outcome::Ok, format!("MT5 connector running ({}{})", st.state, if st.detail.is_empty() { String::new() } else { format!(": {}", st.detail) }))
        } else if self.profile.is_none() {
            (Outcome::Skipped, "MT5 connector not started (no profile selected)".into())
        } else {
            (Outcome::Failed, format!("MT5 connector not running ({})", st.state))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn profile() -> Mt5Profile {
        Mt5Profile { profile_id: "demo-1".into(), name: "Demo".into(), terminal_exe: r"C:\Program Files\MetaTrader 5\terminal64.exe".into(),
            server: "MetaQuotes-Demo".into(), port: 443, expected_login: 51234567 }
    }

    #[test]
    fn profile_validation() {
        assert!(profile().validate().is_ok());
        let bad = [
            Mt5Profile { profile_id: "Demo 1".into(), ..profile() },
            Mt5Profile { terminal_exe: r"C:\Windows\System32\cmd.exe".into(), ..profile() },
            Mt5Profile { terminal_exe: "C:\\x\\terminal64.exe\" --evil".into(), ..profile() },
            Mt5Profile { server: "srv;rm -rf".into(), ..profile() },
            Mt5Profile { server: "".into(), ..profile() },
            Mt5Profile { expected_login: 0, ..profile() },
            Mt5Profile { name: "a\nb".into(), ..profile() },
        ];
        for b in bad {
            assert!(b.validate().is_err(), "{b:?}");
        }
        assert_eq!(profile().masked_login(), "•••••567");
    }

    #[test]
    fn profiles_require_explicit_selection_and_roundtrip() {
        let t = tempfile::tempdir().unwrap();
        let path = t.path().join(PROFILES_FILE);
        let mut p = Profiles::load(&path).unwrap();
        assert!(p.active_profile().is_none());
        p.upsert(profile()).unwrap();
        p.upsert(Mt5Profile { profile_id: "live-1".into(), expected_login: 999, ..profile() }).unwrap();
        assert!(p.active_profile().is_none(), "never picks one implicitly");
        assert!(p.select("nope").is_err());
        p.select("live-1").unwrap();
        p.save(&path).unwrap();
        let q = Profiles::load(&path).unwrap();
        assert_eq!(q.active_profile().unwrap().expected_login, 999);
        let mut q = q;
        q.remove("live-1");
        assert!(q.active.is_none());
        std::fs::write(&path, r#"{"profiles":[],"active":null,"password":"x"}"#).unwrap();
        assert!(Profiles::load(&path).is_err(), "unknown fields (e.g. a password) rejected");
    }

    #[test]
    fn launch_discovery_requires_absolute_paths() {
        let t = tempfile::tempdir().unwrap();
        let py = t.path().join("python");
        std::fs::write(&py, "").unwrap();
        let src = t.path().join("windows-connector");
        std::fs::create_dir_all(&src).unwrap();
        let (pys, srcs) = (py.to_string_lossy().into_owned(), src.to_string_lossy().into_owned());
        let env = |k: &str| match k { "RR_ONE_CONNECTOR_PYTHON" => Some(pys.clone()), "RR_ONE_CONNECTOR_SRC" => Some(srcs.clone()), _ => None };
        let l = find_launch(Path::new("/nonexistent"), env).unwrap();
        assert_eq!(l.prefix, vec!["-m", "rr_connector"]);
        assert!(l.env[0].1.contains("windows-connector") && l.env[0].1.contains("mt5-connector"));
        let rel = |k: &str| match k { "RR_ONE_CONNECTOR_PYTHON" => Some("python".to_string()), "RR_ONE_CONNECTOR_SRC" => Some("src".into()), _ => None };
        assert!(find_launch(Path::new("/nonexistent"), rel).is_none(), "relative overrides ignored");
        let inst = t.path().join("install");
        std::fs::create_dir_all(inst.join("mt5-connector")).unwrap();
        std::fs::write(inst.join("mt5-connector").join("RR_MT5_Connector.exe"), "").unwrap();
        let packaged = find_launch(&inst.join("desktop"), |_| None).unwrap();
        assert!(packaged.prefix.is_empty() && packaged.program.ends_with("RR_MT5_Connector.exe"));
    }

    #[test]
    fn connector_home_matches_python_side() {
        let h = connector_home(true, |k| (k == "LOCALAPPDATA").then(|| r"C:\Users\t\AppData\Local".to_string())).unwrap();
        assert!(h.ends_with(Path::new("RR Trading One").join("connector")));
        assert!(connector_home(true, |_| None).is_none());
        assert_eq!(connector_home(false, |k| (k == "HOME").then(|| "/home/u".to_string())).unwrap(),
            PathBuf::from("/home/u/.rr-trading-one-connector"));
    }

    #[test]
    fn stale_or_foreign_status_is_not_running() {
        let t = tempfile::tempdir().unwrap();
        assert_eq!(read_status(t.path()).state, "NOT_STARTED");
        let me = std::process::id();
        std::fs::write(t.path().join(STATUS_FILE), format!(r#"{{"state":"CONNECTED","pid":{me},"updated_at":{}}}"#, now())).unwrap();
        assert!(read_status(t.path()).running);
        std::fs::write(t.path().join(STATUS_FILE), format!(r#"{{"state":"CONNECTED","pid":{me},"updated_at":{}}}"#, now() - 60.0)).unwrap();
        let s = read_status(t.path());
        assert!(!s.running && s.state == "NOT_RUNNING", "stale status must not count as running");
        std::fs::write(t.path().join(STATUS_FILE), format!(r#"{{"state":"CONNECTED","pid":4000000,"updated_at":{}}}"#, now())).unwrap();
        assert!(!read_status(t.path()).running, "dead pid");
    }

    /// A stand-in for the headless connector that speaks the same file protocol.
    #[cfg(unix)]
    const FAKE: &str = r#"
import json, os, sys, time
home = os.environ["RRMT5_HOME"]
def st(state, detail="", prof=None):
    tmp = os.path.join(home, "status.tmp")
    json.dump({"state": state, "detail": detail, "pid": os.getpid(), "updated_at": time.time(), "profile": prof}, open(tmp, "w"))
    os.replace(tmp, os.path.join(home, "status.json"))
if sys.argv[1] == "--detect-json":
    print(json.dumps([{"exe": "C:/MT5/terminal64.exe", "running": True}])); sys.exit(0)
if sys.argv[1] == "--register-stdin":
    tok = sys.stdin.readline().strip()
    ok = tok.startswith("rrmt5_") and tok not in " ".join(sys.argv)
    print(json.dumps({"ok": ok, "agent_id": 7} if ok else {"ok": False, "error": "bad token " + tok})); sys.exit(0 if ok else 1)
p = json.load(open(sys.argv[2]))
if os.environ.get("FAKE_NOT_REGISTERED"):
    st("NOT_REGISTERED", "link first", {"id": p["profile_id"]}); sys.exit(3)
while not os.path.exists(os.path.join(home, "stop.request")):
    st("CONNECTED", "", {"id": p["profile_id"]}); time.sleep(0.1)
os.remove(os.path.join(home, "stop.request")); st("STOPPED", "", {"id": p["profile_id"]})
"#;

    #[cfg(unix)]
    fn fake_ctl(home: &Path, script: &Path, extra_env: Vec<(String, String)>) -> ConnectorCtl<'static> {
        std::fs::write(script, FAKE).unwrap();
        let py = crate::runner::which("python3", &[]).expect("python3");
        ConnectorCtl { launch: Launch { program: py, prefix: vec![script.to_string_lossy().into_owned()], env: extra_env },
            home: home.to_path_buf(), engine_url: "http://127.0.0.1:47810".into(), runner: &crate::runner::SystemRunner }
    }

    #[cfg(unix)]
    #[test]
    fn start_status_stop_lifecycle_with_real_processes() {
        let t = tempfile::tempdir().unwrap();
        let ctl = fake_ctl(&t.path().join("home"), &t.path().join("fake.py"), vec![]);
        assert!(!ctl.status().running);
        let msg = ctl.start(&profile()).unwrap();
        assert!(msg.contains("CONNECTED"), "{msg}");
        let st = ctl.status();
        assert!(st.running && st.pid.is_some());
        assert!(ctl.start(&profile()).unwrap().contains("already running"), "idempotent start");
        let other = Mt5Profile { profile_id: "other".into(), ..profile() };
        assert!(ctl.start(&other).unwrap_err().contains("another profile"));
        let pid = st.pid.unwrap();
        assert_eq!(ctl.stop().unwrap(), "stopped");
        assert_eq!(ctl.status().state, "STOPPED");
        std::thread::sleep(Duration::from_millis(300));
        assert!(!pid_alive(pid), "process exited gracefully");
        assert!(ctl.stop().unwrap().starts_with("not running"));
        let written: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(t.path().join("home").join(RUNTIME_PROFILE)).unwrap()).unwrap();
        assert_eq!(written["engine_url"], "http://127.0.0.1:47810");
        assert_eq!(written["expected_login"], 51234567);
    }

    #[cfg(unix)]
    #[test]
    fn unregistered_connector_is_reported_not_retried() {
        let t = tempfile::tempdir().unwrap();
        let ctl = fake_ctl(&t.path().join("home"), &t.path().join("fake.py"), vec![("FAKE_NOT_REGISTERED".into(), "1".into())]);
        assert!(ctl.start(&profile()).unwrap_err().contains("not linked"));
    }

    #[cfg(unix)]
    #[test]
    fn detect_and_register_pass_token_on_stdin_only() {
        let t = tempfile::tempdir().unwrap();
        let ctl = fake_ctl(&t.path().join("home"), &t.path().join("fake.py"), vec![]);
        assert_eq!(ctl.detect().unwrap().len(), 1);
        assert_eq!(ctl.register("rrmt5_secret-token-value").unwrap()["agent_id"], 7);
        let err = ctl.register("bogus-token").unwrap_err();
        assert!(err.contains("***") && !err.contains("bogus-token"), "token redacted from errors: {err}");
    }
}
