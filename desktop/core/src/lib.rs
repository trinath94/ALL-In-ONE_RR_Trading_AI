//! RR Trading One supervisor core.
//!
//! Shared by the desktop shell and its tests: the allowlisted action set and its plans, the Windows directory
//! layout, desktop settings, loopback health probes, stack secrets, and the supervisor that executes plans as
//! fixed Docker Compose argument vectors (never through a shell). It contains no UI.

pub mod actions;
pub mod compose;
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
