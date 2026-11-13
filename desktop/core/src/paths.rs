//! Windows directory layout (spec Part 18).
//!
//! ```text
//! C:\Program Files\RR Trading One\      install dir: binaries, compose files (read-only, no secrets)
//! C:\ProgramData\RR Trading One\         machine data: backups, logs, models, datasets, cache, secrets
//! %APPDATA%\RR Trading One\              per-user settings (no secrets)
//! ```
//!
//! PostgreSQL and Redis data live in Docker named volumes (decision D2) and are never removed by an upgrade or
//! uninstall. Secrets are kept in Windows Credential Manager / DPAPI. The `secrets` directory is reserved and
//! is only created by the Phase 3 code that also restricts its ACL; it is never created world-readable here.

use serde::Serialize;
use std::path::{Path, PathBuf};

pub const APP_DIR_NAME: &str = "RR Trading One";
/// Lower-case name used on non-Windows development machines.
const DEV_DIR_NAME: &str = "rr-trading-one";

/// Data sub-directories created on first run. `secrets` is deliberately absent (see module docs).
pub const DATA_SUBDIRS: [&str; 5] = ["backups", "logs", "models", "datasets", "cache"];

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AppPaths {
    pub install_dir: PathBuf,
    pub data_dir: PathBuf,
    pub settings_dir: PathBuf,
    /// True when resolved with Windows conventions; false on a non-Windows development machine.
    pub windows_layout: bool,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum PathError {
    #[error("environment variable {0} is not set")]
    MissingEnv(&'static str),
    #[error("{0} must be an absolute path")]
    NotAbsolute(String),
    #[error("{what} ({path}) must not be inside the installation directory")]
    InsideInstallDir { what: &'static str, path: String },
}

impl AppPaths {
    /// Resolve paths from environment variables. `env` is injected so this is testable on any OS.
    pub fn resolve(install_dir: &Path, windows: bool, env: impl Fn(&str) -> Option<String>) -> Result<Self, PathError> {
        let get = |k: &'static str| env(k).filter(|v| !v.is_empty()).ok_or(PathError::MissingEnv(k));
        let (data_dir, settings_dir) = if windows {
            (PathBuf::from(get("ProgramData")?).join(APP_DIR_NAME), PathBuf::from(get("APPDATA")?).join(APP_DIR_NAME))
        } else {
            let home = PathBuf::from(get("HOME")?);
            let data = env("XDG_DATA_HOME").filter(|v| !v.is_empty()).map(PathBuf::from)
                .unwrap_or_else(|| home.join(".local/share"));
            let cfg = env("XDG_CONFIG_HOME").filter(|v| !v.is_empty()).map(PathBuf::from)
                .unwrap_or_else(|| home.join(".config"));
            (data.join(DEV_DIR_NAME), cfg.join(DEV_DIR_NAME))
        };
        let p = AppPaths { install_dir: install_dir.to_path_buf(), data_dir, settings_dir, windows_layout: windows };
        p.validate()?;
        Ok(p)
    }

    /// Resolve for the running process.
    pub fn for_current_process() -> Result<Self, PathError> {
        let exe = std::env::current_exe().map_err(|_| PathError::MissingEnv("current_exe"))?;
        let install = exe.parent().map(Path::to_path_buf).unwrap_or_default();
        Self::resolve(&install, cfg!(windows), |k| std::env::var(k).ok())
    }

    pub fn validate(&self) -> Result<(), PathError> {
        for (what, p) in [("data directory", &self.data_dir), ("settings directory", &self.settings_dir),
            ("install directory", &self.install_dir)] {
            if !is_absolute(p, self.windows_layout) {
                return Err(PathError::NotAbsolute(format!("{what}: {}", p.display())));
            }
        }
        for (what, p) in [("data directory", &self.data_dir), ("settings directory", &self.settings_dir)] {
            if starts_with_ci(p, &self.install_dir, self.windows_layout) {
                return Err(PathError::InsideInstallDir { what, path: p.display().to_string() });
            }
        }
        Ok(())
    }

    pub fn settings_file(&self) -> PathBuf {
        self.settings_dir.join("settings.json")
    }

    pub fn logs_dir(&self) -> PathBuf {
        self.data_dir.join("logs")
    }

    pub fn backups_dir(&self) -> PathBuf {
        self.data_dir.join("backups")
    }

    /// Reserved; created with a restricted ACL in Phase 3.
    pub fn secrets_dir(&self) -> PathBuf {
        self.data_dir.join("secrets")
    }

    /// Create the settings directory and the non-secret data directories.
    pub fn ensure(&self) -> std::io::Result<()> {
        std::fs::create_dir_all(&self.settings_dir)?;
        for d in DATA_SUBDIRS {
            std::fs::create_dir_all(self.data_dir.join(d))?;
        }
        Ok(())
    }
}

/// `Path::is_absolute` follows the host OS; this follows the *target* layout so tests behave the same
/// everywhere.
fn is_absolute(p: &Path, windows: bool) -> bool {
    if !windows {
        return p.is_absolute();
    }
    let s = p.to_string_lossy();
    let b = s.as_bytes();
    (b.len() >= 3 && b[0].is_ascii_alphabetic() && b[1] == b':' && (b[2] == b'\\' || b[2] == b'/'))
        || s.starts_with("\\\\")
}

/// Windows paths are case-insensitive and accept both separators.
fn starts_with_ci(p: &Path, base: &Path, windows: bool) -> bool {
    if !windows {
        return p.starts_with(base);
    }
    let norm = |x: &Path| {
        let mut s = x.to_string_lossy().replace('/', "\\").to_lowercase();
        while s.ends_with('\\') {
            s.pop();
        }
        s
    };
    let (ps, bs) = (norm(p), norm(base));
    ps == bs || ps.starts_with(&(bs + "\\"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn env(map: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let m: HashMap<String, String> = map.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
        move |k| m.get(k).cloned()
    }

    #[test]
    fn windows_layout_matches_spec() {
        let p = AppPaths::resolve(
            Path::new(r"C:\Program Files\RR Trading One\desktop"),
            true,
            env(&[("ProgramData", r"C:\ProgramData"), ("APPDATA", r"C:\Users\trader\AppData\Roaming")]),
        )
        .unwrap();
        assert_eq!(p.data_dir, PathBuf::from(r"C:\ProgramData").join("RR Trading One"));
        assert_eq!(p.settings_dir, PathBuf::from(r"C:\Users\trader\AppData\Roaming").join("RR Trading One"));
        assert!(p.windows_layout);
    }

    #[test]
    fn missing_windows_env_is_an_error_not_a_guess() {
        let r = AppPaths::resolve(Path::new(r"C:\Program Files\RR Trading One"), true, env(&[("APPDATA", r"C:\x")]));
        assert_eq!(r, Err(PathError::MissingEnv("ProgramData")));
        let r = AppPaths::resolve(Path::new(r"C:\Program Files\RR Trading One"), true,
            env(&[("ProgramData", ""), ("APPDATA", r"C:\x")]));
        assert_eq!(r, Err(PathError::MissingEnv("ProgramData")));
    }

    #[test]
    fn data_inside_install_dir_is_rejected_case_insensitively() {
        let r = AppPaths::resolve(
            Path::new(r"C:\Apps"),
            true,
            env(&[("ProgramData", r"c:\apps\data"), ("APPDATA", r"C:\Users\t\AppData\Roaming")]),
        );
        assert!(matches!(r, Err(PathError::InsideInstallDir { what: "data directory", .. })), "{r:?}");
    }

    #[test]
    fn sibling_with_common_prefix_is_not_inside() {
        assert!(!starts_with_ci(Path::new(r"C:\Apps2\x"), Path::new(r"C:\Apps"), true));
        assert!(starts_with_ci(Path::new(r"C:\APPS\x"), Path::new(r"c:/apps/"), true));
    }

    #[test]
    fn relative_windows_paths_rejected() {
        let r = AppPaths::resolve(Path::new(r"C:\P"), true, env(&[("ProgramData", r"data"), ("APPDATA", r"C:\a")]));
        assert!(matches!(r, Err(PathError::NotAbsolute(_))));
    }

    #[test]
    fn dev_layout_uses_xdg() {
        let p = AppPaths::resolve(Path::new("/opt/rr"), false,
            env(&[("HOME", "/home/u"), ("XDG_CONFIG_HOME", "/cfg")])).unwrap();
        assert_eq!(p.data_dir, PathBuf::from("/home/u/.local/share/rr-trading-one"));
        assert_eq!(p.settings_dir, PathBuf::from("/cfg/rr-trading-one"));
    }

    #[test]
    fn ensure_creates_data_dirs_but_not_secrets() {
        let t = tempfile::tempdir().unwrap();
        let root = t.path().to_str().unwrap().to_string();
        let p = AppPaths::resolve(Path::new("/opt/rr"), false,
            env(&[("HOME", &root), ("XDG_DATA_HOME", &format!("{root}/d")), ("XDG_CONFIG_HOME", &format!("{root}/c"))]))
            .unwrap();
        p.ensure().unwrap();
        for d in DATA_SUBDIRS {
            assert!(p.data_dir.join(d).is_dir(), "{d}");
        }
        assert!(p.settings_dir.is_dir());
        assert!(!p.secrets_dir().exists());
    }
}
