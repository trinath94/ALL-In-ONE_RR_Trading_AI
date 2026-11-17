//! RR Trading One supervisor core.
//!
//! Shared by the desktop shell and its tests: the allowlisted action set and its plans, the Windows directory
//! layout, desktop settings, loopback health probes, stack secrets, and the supervisor that executes plans as
//! fixed Docker Compose argument vectors (never through a shell). It contains no UI.

pub mod actions;
pub mod backupcrypt;
pub mod compose;
pub mod connector;
pub mod engine;
pub mod health;
pub mod local;
pub mod paths;
pub mod runner;
pub mod secrets;
pub mod settings;
pub mod supervisor;
pub mod sys;

use serde::Serialize;

/// The state RR Trading One ships in (spec Part 15/16). These values are owned by the engine (seeded by
/// `app.cli seed`, enforced by RBAC and the Risk Engine) and by the MT5 connector's PC-side LIVE lock. The
/// desktop shell displays them; it has no action that can change them.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct InitialSafetyState {
    pub account_mode: &'static str,
    pub auto_trading: bool,
    pub trading_worker_running: bool,
    pub live_locked: bool,
}

pub const INITIAL_SAFETY_STATE: InitialSafetyState =
    InitialSafetyState { account_mode: "DEMO", auto_trading: false, trading_worker_running: false, live_locked: true };

pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Phase 21: with an app ACL manifest, Tauri rejects at runtime any command not declared in build.rs and granted in
/// the main window's capability. This keeps the handler list, the manifest and the capability identical.
#[cfg(test)]
mod tauri_acl_tests {
    fn quoted(text: &str) -> Vec<String> {
        text.split('"').skip(1).step_by(2).map(String::from).collect()
    }

    #[test]
    fn tauri_acl_lists_every_command() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../src-tauri");
        let lib = std::fs::read_to_string(root.join("src/lib.rs")).unwrap();
        let start = lib.find("generate_handler![").unwrap() + "generate_handler![".len();
        let mut handlers: Vec<String> = lib[start..start + lib[start..].find(']').unwrap()]
            .split(',').map(|c| c.trim().to_string()).filter(|c| !c.is_empty()).collect();
        let build = std::fs::read_to_string(root.join("build.rs")).unwrap();
        let s = build.find(".commands(&[").unwrap();
        let mut manifest = quoted(&build[s..s + build[s..].find("])").unwrap()]);
        let cap = std::fs::read_to_string(root.join("capabilities/main.json")).unwrap();
        let mut granted: Vec<String> = quoted(&cap).into_iter().filter_map(|p| p.strip_prefix("allow-").map(|c| c.replace('-', "_"))).collect();
        handlers.sort();
        manifest.sort();
        granted.sort();
        assert!(handlers.len() >= 23, "{handlers:?}");
        assert_eq!(manifest, handlers, "build.rs manifest must declare every command");
        assert_eq!(granted, handlers, "capabilities/main.json must grant every command to the main window");
        let v: serde_json::Value = serde_json::from_str(&cap).unwrap();
        assert_eq!(v["windows"], serde_json::json!(["main"]), "main window only (the engine console gets no IPC)");
    }
}
