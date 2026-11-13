//! RR Trading One supervisor core.
//!
//! Pure logic shared by the desktop shell and its tests: the allowlisted action set, the Windows directory
//! layout, desktop settings and loopback health probes. It contains no UI, spawns no processes and runs no
//! shell commands. Process execution (Docker Compose, the native MT5 connector) arrives in Phase 3 and may
//! only execute the typed [`actions::Step`] values produced here.

pub mod actions;
pub mod health;
pub mod paths;
pub mod settings;

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
