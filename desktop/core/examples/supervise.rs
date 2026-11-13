//! Development/diagnostic CLI that drives the same supervisor code path as the desktop app.
//!
//! cargo run -p rr-one-core --example supervise -- prereqs
//! cargo run -p rr-one-core --example supervise -- status
//! cargo run -p rr-one-core --example supervise -- <action_id> ["START TRADING WORKER"]
use rr_one_core::actions::Action;
use rr_one_core::health;
use rr_one_core::local;
use rr_one_core::paths::AppPaths;
use rr_one_core::runner::SystemRunner;
use rr_one_core::settings::Settings;
use rr_one_core::connector::LocalConnector;
use rr_one_core::supervisor::{NativeConnector, Supervisor};
use std::time::Duration;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(cmd) = args.first() else {
        eprintln!("usage: supervise prereqs|status|<action_id> [confirmation]");
        std::process::exit(2);
    };
    let paths = AppPaths::for_current_process().unwrap_or_else(|e| fail(&e.to_string()));
    let settings = Settings::load(&paths.settings_file()).unwrap_or_else(|e| fail(&e.to_string()));
    let runner = SystemRunner;
    let setup = local::prepare(&paths, &settings, &runner).unwrap_or_else(|e| fail(&e));
    if setup.secrets_created {
        eprintln!("created stack secrets in {}", paths.secrets_dir().display());
    }
    let native = setup.connector.as_ref().map(|c| LocalConnector { ctl: c.ctl(&runner), profile: c.profile.clone() });
    let sup = Supervisor {
        cfg: setup.config,
        runner: &runner,
        secrets: &setup.secrets,
        probe: Box::new(|port| health::probe_engine(port, Duration::from_secs(3))),
        connector: native.as_ref().map(|n| n as &dyn NativeConnector),
    };
    let out = match cmd.as_str() {
        "prereqs" => serde_json::to_value(sup.prerequisites()).unwrap(),
        "status" => serde_json::to_value(sup.status().unwrap_or_else(|e| fail(&e))).unwrap(),
        id => {
            let action = Action::parse(id).unwrap_or_else(|e| fail(&e.to_string()));
            let rep = sup.execute(action, args.get(1).map(String::as_str)).unwrap_or_else(|e| fail(&e.to_string()));
            let ok = rep.ok;
            println!("{}", serde_json::to_string_pretty(&rep).unwrap());
            std::process::exit(if ok { 0 } else { 1 });
        }
    };
    println!("{}", serde_json::to_string_pretty(&out).unwrap());
}

fn fail(msg: &str) -> ! {
    eprintln!("error: {msg}");
    std::process::exit(1)
}
