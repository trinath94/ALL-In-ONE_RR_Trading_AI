//! Per-user desktop settings, `%APPDATA%\RR Trading One\settings.json`.
//!
//! * No secrets: unknown fields are rejected, so an API key or password cannot be stored here by accident.
//! * LOCAL WINDOWS MODE is the default. In local mode the engine address is *derived* (`http://127.0.0.1:<port>`)
//!   and cannot be configured, so local mode can never point at a previous Ubuntu server (spec Part 19).
//! * REMOTE ENGINE MODE is explicit, separate and requires an `https://` URL.
//! * A corrupt file is reported, never silently overwritten.

use serde::{Deserialize, Serialize};
use std::io::Write;
use std::path::Path;

pub const SCHEMA_VERSION: u32 = 1;
/// Default loopback port for the local gateway (nginx container, bound to 127.0.0.1 only).
pub const DEFAULT_GATEWAY_PORT: u16 = 47810;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum OperatingMode {
    #[default]
    Local,
    Remote,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct Settings {
    pub schema_version: u32,
    pub operating_mode: OperatingMode,
    /// Loopback port of the local gateway (LOCAL mode).
    pub gateway_port: u16,
    /// REMOTE mode only. Ignored in LOCAL mode.
    pub remote_engine_url: Option<String>,
    pub start_minimized: bool,
    pub close_to_tray: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            schema_version: SCHEMA_VERSION,
            operating_mode: OperatingMode::Local,
            gateway_port: DEFAULT_GATEWAY_PORT,
            remote_engine_url: None,
            start_minimized: false,
            close_to_tray: true,
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum SettingsError {
    #[error("settings file is unreadable: {0}")]
    Io(#[from] std::io::Error),
    #[error("settings file is invalid and was left untouched: {0}")]
    Parse(String),
    #[error("invalid settings: {0}")]
    Invalid(String),
}

/// Where the desktop shell reaches the engine.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct EngineEndpoint {
    pub mode: OperatingMode,
    /// Base URL of the web interface (and `/api` behind it).
    pub base_url: String,
    /// True when the endpoint is on this machine's loopback interface.
    pub loopback: bool,
}

impl Settings {
    pub fn validate(&self) -> Result<(), SettingsError> {
        if self.schema_version != SCHEMA_VERSION {
            return Err(SettingsError::Invalid(format!("unsupported schema_version {}", self.schema_version)));
        }
        if self.gateway_port < 1024 {
            return Err(SettingsError::Invalid("gateway_port must be 1024-65535".into()));
        }
        if self.operating_mode == OperatingMode::Remote {
            let url = self.remote_engine_url.as_deref().unwrap_or("");
            validate_remote_url(url)?;
        }
        Ok(())
    }

    pub fn endpoint(&self) -> Result<EngineEndpoint, SettingsError> {
        self.validate()?;
        Ok(match self.operating_mode {
            OperatingMode::Local => EngineEndpoint {
                mode: OperatingMode::Local,
                base_url: format!("http://127.0.0.1:{}", self.gateway_port),
                loopback: true,
            },
            OperatingMode::Remote => EngineEndpoint {
                mode: OperatingMode::Remote,
                base_url: self.remote_engine_url.clone().unwrap_or_default().trim_end_matches('/').to_string(),
                loopback: false,
            },
        })
    }

    /// Load settings. A missing file yields defaults; an invalid file is an error (the caller decides how to
    /// proceed, and the file is not overwritten).
    pub fn load(path: &Path) -> Result<Settings, SettingsError> {
        let text = match std::fs::read_to_string(path) {
            Ok(t) => t,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Settings::default()),
            Err(e) => return Err(e.into()),
        };
        let s: Settings = serde_json::from_str(&text).map_err(|e| SettingsError::Parse(e.to_string()))?;
        s.validate()?;
        Ok(s)
    }

    /// Validate, then write atomically (temp file + rename) so a crash never leaves a half-written file.
    pub fn save(&self, path: &Path) -> Result<(), SettingsError> {
        self.validate()?;
        let dir = path.parent().ok_or_else(|| SettingsError::Invalid("settings path has no parent".into()))?;
        std::fs::create_dir_all(dir)?;
        let tmp = dir.join(".settings.json.tmp");
        {
            let mut f = std::fs::File::create(&tmp)?;
            f.write_all(serde_json::to_string_pretty(self).expect("settings serialise").as_bytes())?;
            f.sync_all()?;
        }
        std::fs::rename(&tmp, path)?;
        Ok(())
    }
}

fn validate_remote_url(url: &str) -> Result<(), SettingsError> {
    let rest = url
        .strip_prefix("https://")
        .ok_or_else(|| SettingsError::Invalid("REMOTE mode requires an https:// engine URL".into()))?;
    let host_port = rest.split('/').next().unwrap_or("");
    if host_port.is_empty() || host_port.contains('@') || url.chars().any(|c| c.is_whitespace() || c.is_control()) {
        return Err(SettingsError::Invalid("REMOTE engine URL is malformed".into()));
    }
    if url.contains('?') || url.contains('#') {
        return Err(SettingsError::Invalid("REMOTE engine URL must not contain a query or fragment".into()));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_is_local_loopback() {
        let s = Settings::default();
        assert_eq!(s.operating_mode, OperatingMode::Local);
        let e = s.endpoint().unwrap();
        assert_eq!(e.base_url, "http://127.0.0.1:47810");
        assert!(e.loopback);
    }

    #[test]
    fn local_mode_ignores_a_leftover_remote_url() {
        let s = Settings { remote_engine_url: Some("https://old-ubuntu.example.com".into()), ..Default::default() };
        let e = s.endpoint().unwrap();
        assert_eq!(e.base_url, "http://127.0.0.1:47810");
        assert!(!e.base_url.contains("ubuntu"));
    }

    #[test]
    fn remote_mode_requires_https() {
        for bad in [None, Some(""), Some("http://engine.example.com"), Some("https://"), Some("https://u:p@h"),
            Some("https://h/x?y=1"), Some("https://h bad")] {
            let s = Settings { operating_mode: OperatingMode::Remote, remote_engine_url: bad.map(String::from),
                ..Default::default() };
            assert!(s.validate().is_err(), "{bad:?} accepted");
        }
        let s = Settings { operating_mode: OperatingMode::Remote,
            remote_engine_url: Some("https://engine.example.com/".into()), ..Default::default() };
        let e = s.endpoint().unwrap();
        assert_eq!(e.base_url, "https://engine.example.com");
        assert!(!e.loopback);
    }

    #[test]
    fn unknown_fields_such_as_secrets_are_rejected() {
        let t = tempfile::tempdir().unwrap();
        let p = t.path().join("settings.json");
        std::fs::write(&p, r#"{"schema_version":1,"anthropic_api_key":"sk-ant-xxx"}"#).unwrap();
        assert!(matches!(Settings::load(&p), Err(SettingsError::Parse(_))));
    }

    #[test]
    fn missing_file_gives_defaults_and_corrupt_file_is_left_untouched() {
        let t = tempfile::tempdir().unwrap();
        let p = t.path().join("settings.json");
        assert_eq!(Settings::load(&p).unwrap(), Settings::default());
        std::fs::write(&p, "{not json").unwrap();
        assert!(Settings::load(&p).is_err());
        assert_eq!(std::fs::read_to_string(&p).unwrap(), "{not json");
    }

    #[test]
    fn save_roundtrip_is_atomic_and_validated() {
        let t = tempfile::tempdir().unwrap();
        let p = t.path().join("sub").join("settings.json");
        let s = Settings { gateway_port: 50000, start_minimized: true, ..Default::default() };
        s.save(&p).unwrap();
        assert_eq!(Settings::load(&p).unwrap(), s);
        assert!(!p.parent().unwrap().join(".settings.json.tmp").exists());
        let bad = Settings { gateway_port: 80, ..Default::default() };
        assert!(bad.save(&p).is_err());
        assert_eq!(Settings::load(&p).unwrap(), s, "invalid save must not replace the file");
    }

    #[test]
    fn partial_file_fills_defaults() {
        let t = tempfile::tempdir().unwrap();
        let p = t.path().join("settings.json");
        std::fs::write(&p, r#"{"gateway_port": 48000}"#).unwrap();
        let s = Settings::load(&p).unwrap();
        assert_eq!(s.gateway_port, 48000);
        assert_eq!(s.operating_mode, OperatingMode::Local);
    }
}
