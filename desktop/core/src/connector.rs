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
/// Parts 27-30 Phase 10: the PC LIVE lock (see `ConnectorCtl::live_lock`).
pub const LIVE_REQUEST: &str = "live.request";
pub const LIVE_RESULT: &str = "live.result";
pub const LIVE_PHRASE: &str = "ENABLE LIVE TRADING";
const LIVE_WAIT: Duration = Duration::from_secs(15);
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
/// Why a start did not produce a running connector.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StartError {
    /// Setup is incomplete: the connector has not been linked to this engine yet.
    NotLinked(String),
    Failed(String),
}

impl std::fmt::Display for StartError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StartError::NotLinked(m) | StartError::Failed(m) => f.write_str(m),
        }
    }
}

pub struct ConnectorCtl<'a> {
    pub launch: Launch,
    pub home: PathBuf,
    /// Multi-account: the connector instance of an additional MT5 profile (`RRMT5_INSTANCE`); None = the primary.
    pub instance: Option<String>,
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
        if let Some(i) = &self.instance {
            env.push(("RRMT5_INSTANCE".into(), i.clone()));
        }
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
    pub fn start(&self, profile: &Mt5Profile) -> Result<String, StartError> {
        use StartError::Failed;
        profile.validate().map_err(Failed)?;
        std::fs::create_dir_all(&self.home).map_err(|e| Failed(e.to_string()))?;
        let st = self.status();
        if st.running {
            let same = st.doc.as_ref().and_then(|d| d.pointer("/profile/id")).and_then(|v| v.as_str()) == Some(&profile.profile_id);
            return if same { Ok(format!("already running ({})", st.state)) } else {
                Err(Failed("the connector is running with another profile; stop it first".into()))
            };
        }
        let mut doc = serde_json::to_value(profile).expect("profile json");
        doc["engine_url"] = serde_json::Value::String(self.engine_url.clone());
        let ppath = self.home.join(RUNTIME_PROFILE);
        write_atomic(&ppath, doc.to_string().as_bytes()).map_err(|e| Failed(e.to_string()))?;
        let _ = std::fs::remove_file(self.home.join(STOP_FILE));
        let pid = self.spawn_detached(&ppath).map_err(Failed)?;
        let t0 = Instant::now();
        while t0.elapsed() < START_WAIT {
            std::thread::sleep(Duration::from_millis(250));
            let st = self.status();
            let ours = st.pid == Some(pid);
            if ours && st.running {
                return Ok(format!("running ({}{})", st.state, if st.detail.is_empty() { String::new() } else { format!(": {}", st.detail) }));
            }
            if ours && st.state == "NOT_REGISTERED" {
                return Err(StartError::NotLinked("not linked to this engine yet: use Link in MT5 Manager".into()));
            }
            if ours && st.state == "INVALID_PROFILE" {
                return Err(Failed(format!("invalid MT5 profile: {}", st.detail)));
            }
            if !pid_alive(pid) && !ours {
                return Err(Failed(format!("the connector exited during start-up (see {})", self.home.join("logs").display())));
            }
        }
        Err(Failed("the connector did not report status within 25 s".into()))
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

    /// Parts 27-30 Phase 10: authorise or revoke LIVE trading **on this PC** (the connector's own lock).
    ///
    /// The request is checked here first (exact phrase, a login of digits), then by the running connector itself,
    /// which accepts only if the connected account is a LIVE account whose login is exactly the one typed. The
    /// request carries a random nonce and a timestamp; the connector uses it once and refuses it after 60 s. This
    /// grants the PC's consent only: the engine's LIVE switch, its administrator approval and its readiness checklist
    /// still apply. Revoking needs no phrase.
    pub fn live_lock(&self, action: &str, login: &str, phrase: &str) -> Result<serde_json::Value, String> {
        match action {
            "authorize" => {
                if phrase != LIVE_PHRASE {
                    return Err(format!("type {LIVE_PHRASE} exactly to confirm"));
                }
                if login.is_empty() || login.len() > 20 || !login.chars().all(|c| c.is_ascii_digit()) {
                    return Err("type the LIVE account number (digits only)".into());
                }
            }
            "revoke" => {}
            _ => return Err("unknown action".into()),
        }
        if !self.status().running {
            return Err("the MT5 connector is not running; start it first".into());
        }
        let mut b = [0u8; 8];
        getrandom::fill(&mut b).map_err(|e| e.to_string())?;
        let nonce: String = b.iter().map(|x| format!("{x:02x}")).collect();
        let req = serde_json::json!({"action": action, "login": login, "phrase": phrase, "nonce": nonce, "created": now()});
        let _ = std::fs::remove_file(self.home.join(LIVE_RESULT));
        let tmp = self.home.join("live.request.tmp");
        std::fs::write(&tmp, serde_json::to_vec(&req).unwrap_or_default()).map_err(|e| e.to_string())?;
        std::fs::rename(&tmp, self.home.join(LIVE_REQUEST)).map_err(|e| e.to_string())?;
        let t0 = Instant::now();
        while t0.elapsed() < LIVE_WAIT {
            std::thread::sleep(Duration::from_millis(200));
            let res: Option<serde_json::Value> = std::fs::read_to_string(self.home.join(LIVE_RESULT)).ok()
                .and_then(|t| serde_json::from_str(&t).ok());
            if let Some(v) = res.filter(|v| v.get("nonce").and_then(|n| n.as_str()) == Some(nonce.as_str())) {
                return if v.get("ok").and_then(|x| x.as_bool()) == Some(true) { Ok(v) } else {
                    Err(v.get("error").and_then(|e| e.as_str()).unwrap_or("refused by the connector").to_string())
                };
            }
        }
        let _ = std::fs::remove_file(self.home.join(LIVE_REQUEST));
        Err("the connector did not answer within 15 s; nothing was changed".into())
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
    /// The selected profile's instance folder.
    pub home: PathBuf,
    pub engine_url: String,
    /// The explicitly selected profile, if any.
    pub profile: Option<Mt5Profile>,
    /// The selected profile's instance name (None: the primary instance).
    pub instance: Option<String>,
    /// The primary instance folder; additional instances live in `<base>/instances/<profile id>`.
    pub base: PathBuf,
    /// Every saved profile, to see and stop every instance.
    pub all_profiles: Vec<Mt5Profile>,
}

/// Multi-account: one connector instance per MT5 profile (one terminal, one account). The first profile keeps the
/// original folder and vault entry (existing installations are unchanged); every other profile has its own.
pub fn instance_for(profiles: &[Mt5Profile], profile_id: &str) -> Option<String> {
    match profiles.first() {
        Some(first) if first.profile_id == profile_id => None,
        _ => Some(profile_id.to_string()),
    }
}

pub fn instance_home(base: &Path, instance: Option<&str>) -> PathBuf {
    match instance {
        None => base.to_path_buf(),
        Some(i) => base.join("instances").join(i),
    }
}

impl ConnectorSetup {
    pub fn ctl<'a>(&self, runner: &'a dyn Runner) -> ConnectorCtl<'a> {
        ConnectorCtl { launch: self.launch.clone(), home: self.home.clone(), instance: self.instance.clone(),
                       engine_url: self.engine_url.clone(), runner }
    }

    /// The connector of every saved profile other than the selected one.
    pub fn other_ctls<'a>(&self, runner: &'a dyn Runner) -> Vec<ConnectorCtl<'a>> {
        let sel = self.profile.as_ref().map(|p| p.profile_id.clone());
        self.all_profiles.iter().filter(|p| Some(&p.profile_id) != sel.as_ref()).map(|p| {
            let instance = instance_for(&self.all_profiles, &p.profile_id);
            ConnectorCtl { launch: self.launch.clone(), home: instance_home(&self.base, instance.as_deref()), instance,
                           engine_url: self.engine_url.clone(), runner }
        }).collect()
    }

    /// Status of each saved profile's connector instance.
    pub fn statuses(&self) -> Vec<(String, ConnectorStatus)> {
        self.all_profiles.iter().map(|p| {
            let inst = instance_for(&self.all_profiles, &p.profile_id);
            (p.profile_id.clone(), read_status(&instance_home(&self.base, inst.as_deref())))
        }).collect()
    }
}

/// Link the selected profile's connector to the local engine. Refused while the connector runs: re-linking rotates
/// its credentials, and a running connector would keep the old ones in memory.
pub fn link(sup: &crate::supervisor::Supervisor<'_>, n: &LocalConnector<'_>) -> Result<serde_json::Value, String> {
    let p = n.profile.as_ref().ok_or("select an MT5 profile first")?;
    if n.ctl.status().running {
        return Err("stop the MT5 connector before linking it again".into());
    }
    let token = sup.issue_agent_token(&p.profile_id)?;
    n.ctl.register(&token)
}

/// The supervisor's view of the connector: starting needs an explicitly selected profile.
pub struct LocalConnector<'a> {
    pub ctl: ConnectorCtl<'a>,
    pub profile: Option<Mt5Profile>,
    /// Multi-account: the other profiles' connectors. Stop (Stop All) stops them too; start stays per profile.
    pub others: Vec<ConnectorCtl<'a>>,
}

impl crate::supervisor::NativeConnector for LocalConnector<'_> {
    fn start(&self) -> (crate::supervisor::Outcome, String) {
        use crate::supervisor::Outcome;
        match &self.profile {
            None => (Outcome::Skipped, "no MT5 profile selected (choose one in MT5 Manager)".into()),
            Some(p) => match self.ctl.start(p) {
                Ok(m) => (Outcome::Ok, format!("{} [{}]", m, p.name)),
                Err(StartError::NotLinked(m)) => (Outcome::Skipped, m),
                Err(StartError::Failed(m)) => (Outcome::Failed, m),
            },
        }
    }

    fn stop(&self) -> (crate::supervisor::Outcome, String) {
        use crate::supervisor::Outcome;
        let mut failed = false;
        let mut msgs = vec![];
        for (i, c) in std::iter::once(&self.ctl).chain(self.others.iter()).enumerate() {
            if i > 0 && !c.status().running {
                continue;
            }
            match c.stop() {
                Ok(m) => msgs.push(if i == 0 { m } else { format!("{}: {m}", c.instance.as_deref().unwrap_or("primary")) }),
                Err(e) => {
                    failed = true;
                    msgs.push(e)
                }
            }
        }
        (if failed { Outcome::Failed } else { Outcome::Ok }, msgs.join("; "))
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
    fn each_profile_has_its_own_connector_instance() {
        let a = profile();
        let b = Mt5Profile { profile_id: "demo-2".into(), expected_login: 51234568, ..profile() };
        let c = Mt5Profile { profile_id: "live-1".into(), expected_login: 7001, ..profile() };
        let all = vec![a.clone(), b.clone(), c.clone()];
        assert_eq!(instance_for(&all, "demo-1"), None, "the first profile keeps the original instance");
        assert_eq!(instance_for(&all, "demo-2").as_deref(), Some("demo-2"));
        let base = PathBuf::from("/x/connector");
        assert_eq!(instance_home(&base, None), base);
        assert_eq!(instance_home(&base, Some("live-1")), base.join("instances").join("live-1"));
        let setup = ConnectorSetup { launch: Launch { program: "py".into(), prefix: vec![], env: vec![] },
            home: instance_home(&base, Some("demo-2")), engine_url: "http://127.0.0.1:47810".into(), profile: Some(b),
            instance: Some("demo-2".into()), base: base.clone(), all_profiles: all };
        let runner = crate::runner::SystemRunner;
        let ctl = setup.ctl(&runner);
        let spec = ctl.spec(&["--status"], Duration::ZERO, None);
        assert!(spec.env.contains(&("RRMT5_INSTANCE".into(), "demo-2".into())));
        assert!(spec.env.contains(&("RRMT5_HOME".into(), base.join("instances").join("demo-2").to_string_lossy().into_owned())));
        let others = setup.other_ctls(&runner);
        assert_eq!(others.len(), 2);
        assert_eq!(others[0].home, base, "demo-1 (primary) is stopped by Stop All too");
        assert_eq!(others[0].instance, None);
        assert_eq!(others[1].home, base.join("instances").join("live-1"));
        let primary = ConnectorSetup { profile: Some(a), instance: None, home: base.clone(), ..setup };
        let spec = primary.ctl(&runner).spec(&["--status"], Duration::ZERO, None);
        assert!(!spec.env.iter().any(|(k, _)| k == "RRMT5_INSTANCE"), "existing installations are unchanged");
        assert_eq!(primary.statuses().len(), 3);
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
    req = os.path.join(home, "live.request")
    if os.path.exists(req):
        r = json.load(open(req)); os.remove(req)
        ok = r["action"] == "revoke" or (r["login"] == "2002" and r["phrase"] == "ENABLE LIVE TRADING")
        out = {"nonce": r["nonce"], "ok": ok, "live_authorized": ok and r["action"] == "authorize"}
        if not ok: out["error"] = "The account number you typed does not match the connected account."
        json.dump(out, open(os.path.join(home, "live.result.tmp"), "w")); os.replace(os.path.join(home, "live.result.tmp"), os.path.join(home, "live.result"))
os.remove(os.path.join(home, "stop.request")); st("STOPPED", "", {"id": p["profile_id"]})
"#;

    #[cfg(unix)]
    fn fake_ctl(home: &Path, script: &Path, extra_env: Vec<(String, String)>) -> ConnectorCtl<'static> {
        std::fs::write(script, FAKE).unwrap();
        let py = crate::runner::which("python3", &[]).expect("python3");
        ConnectorCtl { launch: Launch { program: py, prefix: vec![script.to_string_lossy().into_owned()], env: extra_env },
            home: home.to_path_buf(), instance: None, engine_url: "http://127.0.0.1:47810".into(), runner: &crate::runner::SystemRunner }
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
        assert!(ctl.start(&other).unwrap_err().to_string().contains("another profile"));
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
    fn linking_is_refused_while_the_connector_runs() {
        let t = tempfile::tempdir().unwrap();
        let ctl = fake_ctl(&t.path().join("home"), &t.path().join("fake.py"), vec![]);
        ctl.start(&profile()).unwrap();
        let secrets = crate::secrets::StackSecrets::generate().unwrap();
        let cfg = crate::supervisor::SupervisorConfig {
            compose: crate::compose::Compose { docker: "/nonexistent/docker".into(), file: "/nonexistent/compose.yml".into() },
            gateway_port: 47810, data_dir: t.path().into(), backups_dir: t.path().join("b"), private_dir: t.path().join("p"), log_file: None, wsl: None, legacy_connector_dir: None,
            image_tag: "local".into(),
        };
        let sup = crate::supervisor::Supervisor { cfg, runner: &crate::runner::SystemRunner, secrets: &secrets,
            probe: Box::new(|_| unreachable!()), connector: None };
        let lc = LocalConnector { ctl, profile: Some(profile()), others: vec![] };
        let err = link(&sup, &lc).unwrap_err();
        assert!(err.contains("stop the MT5 connector"), "{err}");
        lc.ctl.stop().unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn unregistered_connector_is_reported_not_retried() {
        let t = tempfile::tempdir().unwrap();
        let ctl = fake_ctl(&t.path().join("home"), &t.path().join("fake.py"), vec![("FAKE_NOT_REGISTERED".into(), "1".into())]);
        assert!(matches!(ctl.start(&profile()).unwrap_err(), StartError::NotLinked(_)));
        use crate::supervisor::{NativeConnector, Outcome};
        let lc = LocalConnector { ctl, profile: Some(profile()), others: vec![] };
        assert_eq!(lc.start().0, Outcome::Skipped, "an unlinked connector is incomplete setup, not a failure");
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

    #[cfg(unix)]
    #[test]
    fn pc_live_lock_request_round_trip_and_local_checks() {
        let t = tempfile::tempdir().unwrap();
        let home = t.path().join("home");
        let ctl = fake_ctl(&home, &t.path().join("fake.py"), vec![]);
        assert!(ctl.live_lock("authorize", "2002", LIVE_PHRASE).unwrap_err().contains("not running"));
        let pf = t.path().join("p.json");
        std::fs::write(&pf, serde_json::to_vec(&profile()).unwrap()).unwrap();
        ctl.start(&profile()).unwrap();
        for _ in 0..50 { if ctl.status().running { break } std::thread::sleep(Duration::from_millis(100)); }
        assert!(ctl.live_lock("authorize", "2002", "enable live trading").unwrap_err().contains("exactly"));
        assert!(ctl.live_lock("authorize", "20 02", LIVE_PHRASE).unwrap_err().contains("digits"));
        assert!(ctl.live_lock("format", "", "").is_err());
        assert!(!home.join(LIVE_REQUEST).exists(), "local refusals never reach the connector");
        assert!(ctl.live_lock("authorize", "2003", LIVE_PHRASE).unwrap_err().contains("does not match"));
        let ok = ctl.live_lock("authorize", "2002", LIVE_PHRASE).unwrap();
        assert_eq!(ok["live_authorized"], true);
        assert_eq!(ctl.live_lock("revoke", "", "").unwrap()["ok"], true);
        ctl.stop().unwrap();
    }
}
