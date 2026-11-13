//! Assemble the supervisor for LOCAL WINDOWS MODE on this machine.

use crate::compose::Compose;
use crate::connector::{self, ConnectorSetup, Profiles};
use crate::paths::AppPaths;
use crate::runner::{which, CommandSpec, Runner};
use crate::secrets::{self, StackSecrets};
use crate::settings::{OperatingMode, Settings};
use crate::supervisor::SupervisorConfig;
use std::path::{Path, PathBuf};
use std::time::Duration;

/// Where `infrastructure/compose.yml` lives: `RR_ONE_INFRA_DIR` (development override), otherwise the install
/// directory or one of its parents (installed layout: `<install>\infrastructure`; development builds run from
/// `desktop/target/<profile>`).
pub fn find_compose_file(install_dir: &Path, override_dir: Option<PathBuf>) -> Option<PathBuf> {
    if let Some(d) = override_dir.filter(|d| d.is_absolute()) {
        let f = d.join("compose.yml");
        return f.is_file().then_some(f);
    }
    install_dir.ancestors().take(5).map(|a| a.join("infrastructure").join("compose.yml")).find(|f| f.is_file())
}

/// Tag for the locally built engine images. An installed copy carries `BUNDLE_MANIFEST.txt` (SHA-256 of every
/// engine file, written by `desktop/scripts/stage_bundle.py`) in the folder that holds `infrastructure/`; the tag is
/// derived from it, so different engine sources never share an image tag. A source checkout has no manifest: `local`.
pub fn image_tag(compose_file: &Path) -> String {
    use sha2::{Digest, Sha256};
    let manifest = compose_file.parent().and_then(Path::parent).map(|root| root.join("BUNDLE_MANIFEST.txt"));
    match manifest.and_then(|m| std::fs::read(m).ok()) {
        Some(bytes) => {
            let d = Sha256::digest(bytes);
            format!("b{}", d.iter().take(6).map(|b| format!("{b:02x}")).collect::<String>())
        }
        None => "local".into(),
    }
}

pub fn find_docker() -> Option<PathBuf> {
    let extra = if cfg!(windows) {
        vec![PathBuf::from(r"C:\Program Files\Docker\Docker\resources\bin")]
    } else {
        vec![PathBuf::from("/usr/bin"), PathBuf::from("/usr/local/bin")]
    };
    which("docker", &extra)
}

fn system32(exe: &str) -> PathBuf {
    let root = std::env::var_os("SystemRoot").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(r"C:\Windows"));
    root.join("System32").join(exe)
}

/// `icacls` arguments that remove inherited permissions and grant full control only to SYSTEM, the local
/// Administrators group and the given account.
pub fn icacls_restrict_args(dir: &Path, account: &str) -> Vec<String> {
    vec![
        dir.to_string_lossy().into_owned(),
        "/inheritance:r".into(),
        "/grant:r".into(),
        "*S-1-5-18:(OI)(CI)F".into(),
        "*S-1-5-32-544:(OI)(CI)F".into(),
        format!("{account}:(OI)(CI)F"),
    ]
}

/// Restrict the secrets directory ACL on Windows (defence in depth; the file is also DPAPI-encrypted).
pub fn restrict_secrets_dir(runner: &dyn Runner, dir: &Path) -> Result<(), String> {
    if !cfg!(windows) {
        return Ok(()); // unix: 0700 is applied when the directory is created
    }
    let user = std::env::var("USERNAME").map_err(|_| "USERNAME not set")?;
    let account = match std::env::var("USERDOMAIN") {
        Ok(d) if !d.is_empty() => format!("{d}\\{user}"),
        _ => user,
    };
    if account.contains(':') || account.contains('"') {
        return Err("unexpected account name".into());
    }
    let spec = CommandSpec { program: system32("icacls.exe"), args: icacls_restrict_args(dir, &account), env: vec![],
        timeout: Duration::from_secs(30), stdout_file: None, stdin: None };
    let out = runner.run(&spec).map_err(|e| e.to_string())?;
    if out.success() { Ok(()) } else { Err(format!("icacls failed: {}", out.failure_summary())) }
}

/// Everything needed to construct a [`crate::supervisor::Supervisor`].
pub struct LocalSetup {
    pub config: SupervisorConfig,
    pub secrets: StackSecrets,
    pub secrets_created: bool,
    /// None when no connector build is found next to the application.
    pub connector: Option<ConnectorSetup>,
}

/// The engine's local supervisor API key, from the stack secrets. Never creates secrets: before the first
/// Start All there is no engine to talk to.
pub fn supervisor_key(paths: &AppPaths) -> Result<String, String> {
    let dir = paths.secrets_dir();
    if !secrets::secrets_file(&dir).is_file() {
        return Err("the engine has not been set up yet: run Start All first".into());
    }
    let (s, _) = secrets::load_or_create(&dir, secrets::platform_protector().as_ref()).map_err(|e| e.to_string())?;
    Ok(s.supervisor_key().to_string())
}

/// MT5 profiles file in the per-user settings folder.
pub fn profiles_file(paths: &AppPaths) -> PathBuf {
    paths.settings_dir.join(connector::PROFILES_FILE)
}

pub fn connector_setup(paths: &AppPaths, settings: &Settings) -> Result<Option<ConnectorSetup>, String> {
    let env = |k: &str| std::env::var(k).ok();
    let Some(launch) = connector::find_launch(&paths.install_dir, env) else { return Ok(None) };
    let home = connector::connector_home(paths.windows_layout, env).ok_or("cannot locate the user's local app-data folder")?;
    let profiles = Profiles::load(&profiles_file(paths))?;
    let engine_url = settings.endpoint().map_err(|e| e.to_string())?.base_url;
    Ok(Some(ConnectorSetup { launch, home, engine_url, profile: profiles.active_profile().cloned() }))
}

pub fn prepare(paths: &AppPaths, settings: &Settings, runner: &dyn Runner) -> Result<LocalSetup, String> {
    if settings.operating_mode != OperatingMode::Local {
        return Err("service control applies to LOCAL WINDOWS MODE only".into());
    }
    let file = find_compose_file(&paths.install_dir, std::env::var_os("RR_ONE_INFRA_DIR").map(PathBuf::from))
        .ok_or("infrastructure/compose.yml not found next to the application")?;
    let docker = find_docker().ok_or("Docker Desktop is not installed (docker executable not found)")?;
    paths.ensure().map_err(|e| format!("application directories: {e}"))?;
    let dir = paths.secrets_dir();
    let (secrets, created) = secrets::load_or_create(&dir, secrets::platform_protector().as_ref()).map_err(|e| e.to_string())?;
    if created {
        restrict_secrets_dir(runner, &dir)?;
    }
    let image_tag = image_tag(&file);
    Ok(LocalSetup {
        config: SupervisorConfig {
            compose: Compose { docker, file },
            gateway_port: settings.gateway_port,
            data_dir: paths.data_dir.clone(),
            backups_dir: paths.backups_dir(),
            log_file: Some(paths.logs_dir().join("supervisor.log")),
            wsl: cfg!(windows).then(|| system32("wsl.exe")),
            image_tag,
        },
        secrets,
        secrets_created: created,
        connector: connector_setup(paths, settings)?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn icacls_args_grant_only_system_admins_and_user() {
        let a = icacls_restrict_args(Path::new(r"C:\ProgramData\RR Trading One\secrets"), r"PC\trader");
        assert_eq!(a, vec![r"C:\ProgramData\RR Trading One\secrets", "/inheritance:r", "/grant:r",
            "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", r"PC\trader:(OI)(CI)F"]);
        assert!(!a.iter().any(|x| x.contains("S-1-1-0") || x.contains("S-1-5-32-545")), "no Everyone/Users");
    }

    #[test]
    fn compose_file_found_via_override_or_ancestors() {
        let t = tempfile::tempdir().unwrap();
        let infra = t.path().join("infrastructure");
        std::fs::create_dir_all(&infra).unwrap();
        std::fs::write(infra.join("compose.yml"), "x").unwrap();
        let deep = t.path().join("desktop/target/debug");
        std::fs::create_dir_all(&deep).unwrap();
        assert_eq!(find_compose_file(&deep, None), Some(infra.join("compose.yml")));
        assert_eq!(find_compose_file(Path::new("/nonexistent"), Some(infra.clone())), Some(infra.join("compose.yml")));
        // A relative override is ignored; discovery falls back to the install directory's ancestors.
        assert_eq!(find_compose_file(&deep, Some(PathBuf::from("relative/dir"))), Some(infra.join("compose.yml")));
        assert_eq!(find_compose_file(Path::new("/nonexistent/a"), Some(PathBuf::from("rel"))), None);
    }

    #[test]
    fn image_tag_follows_the_installed_sources() {
        let t = tempfile::tempdir().unwrap();
        let compose = t.path().join("infrastructure").join("compose.yml");
        std::fs::create_dir_all(compose.parent().unwrap()).unwrap();
        std::fs::write(&compose, "x").unwrap();
        assert_eq!(image_tag(&compose), "local", "source checkout: no manifest");
        std::fs::write(t.path().join("BUNDLE_MANIFEST.txt"), "aaaa  backend/app/main.py\n").unwrap();
        let v1 = image_tag(&compose);
        assert!(v1.len() == 13 && v1.starts_with('b') && v1[1..].chars().all(|c| c.is_ascii_hexdigit()), "{v1}");
        assert_eq!(image_tag(&compose), v1, "stable for the same install");
        std::fs::write(t.path().join("BUNDLE_MANIFEST.txt"), "bbbb  backend/app/main.py\n").unwrap();
        assert_ne!(image_tag(&compose), v1, "an upgrade with different sources gets new images");
    }

    #[test]
    fn remote_mode_has_no_local_supervisor() {
        let t = tempfile::tempdir().unwrap();
        let p = AppPaths { install_dir: t.path().into(), data_dir: t.path().join("d"), settings_dir: t.path().join("s"), windows_layout: false };
        let s = Settings { operating_mode: OperatingMode::Remote, remote_engine_url: Some("https://e.example.com".into()), ..Default::default() };
        assert!(prepare(&p, &s, &crate::runner::SystemRunner).is_err());
    }
}
