//! Allowlisted supervisor actions (spec Part 5).
//!
//! The desktop shell can only do what is listed here. An action arrives from the UI as a string, is parsed
//! against this closed list, and expands into a fixed plan of typed steps. There is no variant that carries a
//! command line, path or arbitrary argument, so nothing supplied by a user, a web page or an AI provider can
//! be turned into a shell command.
//!
//! Safety invariants (tested below):
//! * `StartAll`, `RestartAll` and `Recovery` never start the trading worker.
//! * Starting the trading worker requires the exact typed confirmation phrase.
//! * Every stop plan stops the trading worker first, before anything it depends on.
//! * No action exists that switches to LIVE, enables auto-trading or changes execution permissions: those
//!   remain engine-side operations behind RBAC, typed confirmation and the Risk Engine.

use serde::{Deserialize, Serialize};
use std::fmt;

/// Phrase the operator must type to start the trading worker.
pub const START_WORKER_PHRASE: &str = "START TRADING WORKER";

/// Services the supervisor manages. Roles follow decision D1 (Phase 1 report): the original monolithic worker
/// loop is split into `Monitor` (heartbeat, sync, reconciliation, position protection), `Analysis` (scanner and
/// signals) and `TradingWorker` (auto-execution only), all reusing the existing engine functions.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Service {
    Postgres,
    Redis,
    Migrate,
    Api,
    Frontend,
    Gateway,
    Monitor,
    Analysis,
    Learner,
    TradingWorker,
    Mt5Connector,
}

impl Service {
    pub const ALL: [Service; 11] = [
        Service::Postgres,
        Service::Redis,
        Service::Migrate,
        Service::Api,
        Service::Frontend,
        Service::Gateway,
        Service::Monitor,
        Service::Analysis,
        Service::Learner,
        Service::TradingWorker,
        Service::Mt5Connector,
    ];

    /// Where the service runs. The MT5 connector must run natively in the interactive Windows session
    /// (never in Docker, never in Session 0).
    pub fn runtime(self) -> Runtime {
        match self {
            Service::Mt5Connector => Runtime::NativeUserSession,
            _ => Runtime::Compose,
        }
    }

    /// Fixed Docker Compose service name. Never derived from input.
    pub fn compose_name(self) -> Option<&'static str> {
        Some(match self {
            Service::Postgres => "postgres",
            Service::Redis => "redis",
            Service::Migrate => "migrate",
            Service::Api => "api",
            Service::Frontend => "frontend",
            Service::Gateway => "gateway",
            Service::Monitor => "monitor",
            Service::Analysis => "analysis",
            Service::Learner => "learner",
            Service::TradingWorker => "trading-worker",
            Service::Mt5Connector => return None,
        })
    }

    pub fn label(self) -> &'static str {
        match self {
            Service::Postgres => "PostgreSQL",
            Service::Redis => "Redis",
            Service::Migrate => "Database migrations",
            Service::Api => "Engine API",
            Service::Frontend => "Web interface",
            Service::Gateway => "Local gateway",
            Service::Monitor => "Account monitor & position protection",
            Service::Analysis => "Market analysis (scanner & signals)",
            Service::Learner => "AI Learner",
            Service::TradingWorker => "Trading worker (auto-execution)",
            Service::Mt5Connector => "MT5 connector",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Runtime {
    Compose,
    NativeUserSession,
}

/// Engine services started by "Start Backend" (D1: monitor and analysis are part of the backend; the trading
/// worker is not).
pub const BACKEND: &[Service] = &[Service::Api, Service::Frontend, Service::Gateway, Service::Monitor, Service::Analysis];
const INFRA: &[Service] = &[Service::Postgres, Service::Redis];

/// One step of a plan. Closed set: the Phase 3 executor maps each variant to a fixed argv / API call.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "step", rename_all = "snake_case")]
pub enum Step {
    /// Windows version, Docker Desktop, WSL2, disk space, ports.
    CheckPrerequisites,
    StartServices { services: Vec<Service> },
    StopServices { services: Vec<Service> },
    /// Back up, then apply pending Alembic migrations through the one-shot `migrate` service.
    RunMigrations,
    /// Real health checks (HTTP / protocol level), never just "process exists".
    VerifyHealth,
    /// Restart only services whose health check failed. Never includes the trading worker.
    RestartUnhealthy,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Action {
    StartAll,
    StopAll,
    RestartAll,
    StartBackend,
    StopBackend,
    StartMt5Connector,
    StopMt5Connector,
    StartLearner,
    StopLearner,
    StartTradingWorker,
    StopTradingWorker,
    SystemHealth,
    Recovery,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum ActionError {
    #[error("unknown action '{0}': only allowlisted actions can be run")]
    Unknown(String),
    #[error("starting the trading worker requires typing exactly: {START_WORKER_PHRASE}")]
    ConfirmationRequired,
}

impl Action {
    pub const ALL: [Action; 13] = [
        Action::StartAll,
        Action::StopAll,
        Action::RestartAll,
        Action::StartBackend,
        Action::StopBackend,
        Action::StartMt5Connector,
        Action::StopMt5Connector,
        Action::StartLearner,
        Action::StopLearner,
        Action::StartTradingWorker,
        Action::StopTradingWorker,
        Action::SystemHealth,
        Action::Recovery,
    ];

    pub fn id(self) -> &'static str {
        match self {
            Action::StartAll => "start_all",
            Action::StopAll => "stop_all",
            Action::RestartAll => "restart_all",
            Action::StartBackend => "start_backend",
            Action::StopBackend => "stop_backend",
            Action::StartMt5Connector => "start_mt5_connector",
            Action::StopMt5Connector => "stop_mt5_connector",
            Action::StartLearner => "start_learner",
            Action::StopLearner => "stop_learner",
            Action::StartTradingWorker => "start_trading_worker",
            Action::StopTradingWorker => "stop_trading_worker",
            Action::SystemHealth => "system_health",
            Action::Recovery => "recovery",
        }
    }

    /// Exact-match parse against the allowlist. No trimming, case folding or prefix matching.
    pub fn parse(s: &str) -> Result<Action, ActionError> {
        Action::ALL.into_iter().find(|a| a.id() == s).ok_or_else(|| ActionError::Unknown(s.chars().take(64).collect()))
    }

    pub fn requires_confirmation(self) -> bool {
        matches!(self, Action::StartTradingWorker)
    }

    /// Build the fixed plan. `confirmation` is only consulted for actions that require it.
    pub fn plan(self, confirmation: Option<&str>) -> Result<Vec<Step>, ActionError> {
        use Step::*;
        if self.requires_confirmation() && confirmation != Some(START_WORKER_PHRASE) {
            return Err(ActionError::ConfirmationRequired);
        }
        Ok(match self {
            Action::StartAll => vec![
                CheckPrerequisites,
                StartServices { services: INFRA.to_vec() },
                RunMigrations,
                StartServices { services: BACKEND.to_vec() },
                StartServices { services: vec![Service::Mt5Connector] },
                StartServices { services: vec![Service::Learner] },
                VerifyHealth,
            ],
            Action::StopAll => vec![StopServices { services: stop_order(&Service::ALL) }],
            Action::RestartAll => {
                let mut p = Action::StopAll.plan(None)?;
                p.extend(Action::StartAll.plan(None)?);
                p
            }
            Action::StartBackend => vec![
                CheckPrerequisites,
                StartServices { services: INFRA.to_vec() },
                RunMigrations,
                StartServices { services: BACKEND.to_vec() },
                VerifyHealth,
            ],
            // Stopping the backend stops everything that depends on it, trading worker first.
            Action::StopBackend => vec![StopServices {
                services: stop_order(&[Service::TradingWorker, Service::Learner, Service::Analysis, Service::Monitor,
                    Service::Gateway, Service::Frontend, Service::Api]),
            }],
            Action::StartMt5Connector => vec![StartServices { services: vec![Service::Mt5Connector] }, VerifyHealth],
            Action::StopMt5Connector => vec![StopServices {
                services: stop_order(&[Service::TradingWorker, Service::Mt5Connector]),
            }],
            Action::StartLearner => vec![StartServices { services: vec![Service::Learner] }, VerifyHealth],
            Action::StopLearner => vec![StopServices { services: vec![Service::Learner] }],
            Action::StartTradingWorker => vec![
                VerifyHealth,
                StartServices { services: vec![Service::TradingWorker] },
                VerifyHealth,
            ],
            Action::StopTradingWorker => vec![StopServices { services: vec![Service::TradingWorker] }],
            Action::SystemHealth => vec![VerifyHealth],
            // RestartUnhealthy inspects state itself; a leading VerifyHealth would abort exactly when recovery is
            // needed.
            Action::Recovery => vec![RestartUnhealthy, VerifyHealth],
        })
    }
}

impl fmt::Display for Action {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.id())
    }
}

/// Order services for shutdown: trading worker first (no new orders can be in flight from it while its
/// dependencies go away), databases last.
pub fn stop_order(services: &[Service]) -> Vec<Service> {
    const ORDER: [Service; 11] = [
        Service::TradingWorker,
        Service::Learner,
        Service::Analysis,
        Service::Monitor,
        Service::Mt5Connector,
        Service::Gateway,
        Service::Frontend,
        Service::Api,
        Service::Migrate,
        Service::Redis,
        Service::Postgres,
    ];
    ORDER.into_iter().filter(|s| services.contains(s)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn starts(plan: &[Step]) -> Vec<Service> {
        plan.iter()
            .filter_map(|s| match s {
                Step::StartServices { services } => Some(services.clone()),
                _ => None,
            })
            .flatten()
            .collect()
    }

    #[test]
    fn parse_is_exact_allowlist() {
        for a in Action::ALL {
            assert_eq!(Action::parse(a.id()), Ok(a));
        }
        for bad in ["", "START_ALL", " start_all", "start_all ", "start_all;rm -rf /", "start", "enable_live",
            "switch_live", "auto_trading_on", "cmd.exe /c whoami", "powershell -c x"] {
            assert!(matches!(Action::parse(bad), Err(ActionError::Unknown(_))), "{bad:?} accepted");
        }
    }

    #[test]
    fn unknown_action_error_is_truncated() {
        let long = "x".repeat(10_000);
        match Action::parse(&long) {
            Err(ActionError::Unknown(s)) => assert_eq!(s.len(), 64),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn no_action_touches_live_or_auto_trading() {
        for a in Action::ALL {
            let id = a.id();
            assert!(!id.contains("live") && !id.contains("auto") && !id.contains("permission"), "{id}");
        }
    }

    #[test]
    fn start_all_restart_and_recovery_never_start_trading_worker() {
        for a in [Action::StartAll, Action::RestartAll, Action::Recovery, Action::StartBackend,
            Action::StartLearner, Action::StartMt5Connector, Action::SystemHealth] {
            let plan = a.plan(None).unwrap();
            assert!(!starts(&plan).contains(&Service::TradingWorker), "{a} starts the trading worker");
        }
    }

    #[test]
    fn only_start_trading_worker_starts_it_and_needs_exact_phrase() {
        let starters: Vec<Action> = Action::ALL
            .into_iter()
            .filter(|a| a.plan(Some(START_WORKER_PHRASE)).map(|p| starts(&p).contains(&Service::TradingWorker)).unwrap_or(false))
            .collect();
        assert_eq!(starters, vec![Action::StartTradingWorker]);
        assert_eq!(Action::StartTradingWorker.plan(None), Err(ActionError::ConfirmationRequired));
        for wrong in ["start trading worker", "START TRADING WORKER ", "yes", ""] {
            assert_eq!(Action::StartTradingWorker.plan(Some(wrong)), Err(ActionError::ConfirmationRequired));
        }
        let p = Action::StartTradingWorker.plan(Some(START_WORKER_PHRASE)).unwrap();
        assert_eq!(p.first(), Some(&Step::VerifyHealth), "must verify health before starting the worker");
    }

    #[test]
    fn stop_plans_stop_trading_worker_first() {
        for a in [Action::StopAll, Action::StopBackend, Action::StopMt5Connector, Action::RestartAll] {
            let plan = a.plan(None).unwrap();
            let first_stop = plan.iter().find_map(|s| match s {
                Step::StopServices { services } => Some(services.clone()),
                _ => None,
            });
            assert_eq!(first_stop.unwrap()[0], Service::TradingWorker, "{a}");
        }
    }

    #[test]
    fn stop_all_stops_every_service_with_databases_last() {
        let plan = Action::StopAll.plan(None).unwrap();
        let Step::StopServices { services } = &plan[0] else { panic!() };
        assert_eq!(services.len(), Service::ALL.len());
        assert_eq!(&services[services.len() - 2..], &[Service::Redis, Service::Postgres]);
    }

    #[test]
    fn start_all_orders_infra_then_migrations_then_backend() {
        let plan = Action::StartAll.plan(None).unwrap();
        assert_eq!(plan[0], Step::CheckPrerequisites);
        let infra = plan.iter().position(|s| matches!(s, Step::StartServices { services } if services.contains(&Service::Postgres))).unwrap();
        let mig = plan.iter().position(|s| *s == Step::RunMigrations).unwrap();
        let api = plan.iter().position(|s| matches!(s, Step::StartServices { services } if services.contains(&Service::Api))).unwrap();
        assert!(infra < mig && mig < api);
        assert_eq!(plan.last(), Some(&Step::VerifyHealth));
    }

    #[test]
    fn every_compose_name_exists_in_the_real_compose_file() {
        let f = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../infrastructure/compose.yml");
        let text = std::fs::read_to_string(&f).expect("infrastructure/compose.yml");
        for s in Service::ALL {
            if let Some(n) = s.compose_name() {
                assert!(text.contains(&format!("\n  {n}:\n")), "service {n} missing from {}", f.display());
            }
        }
        assert!(text.contains("profiles: [\"trading\"]"), "trading worker must sit behind a profile");
    }

    #[test]
    fn mt5_connector_is_native_never_compose() {
        assert_eq!(Service::Mt5Connector.runtime(), Runtime::NativeUserSession);
        assert_eq!(Service::Mt5Connector.compose_name(), None);
        for s in Service::ALL.into_iter().filter(|s| *s != Service::Mt5Connector) {
            let n = s.compose_name().unwrap();
            assert!(n.chars().all(|c| c.is_ascii_lowercase() || c == '-'), "{n}");
        }
    }
}
