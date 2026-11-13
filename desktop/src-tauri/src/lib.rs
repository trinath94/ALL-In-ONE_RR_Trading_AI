//! RR Trading One desktop shell (Tauri 2).
//!
//! Windows:
//! * `main`    - the local Control Centre (bundled `ui/`). The only window with IPC access.
//! * `console` - the existing RR Trading Engine web interface, loaded from the engine endpoint
//!   (`http://127.0.0.1:<port>` in LOCAL mode). It is a remote origin to Tauri and has **no** IPC access;
//!   its navigation is pinned to the engine origin.
//!
//! Every command goes through `rr_one_core`: actions are parsed against the allowlist and expanded into typed
//! plans. Nothing here runs a shell command.

use rr_one_core::actions::{Action, Service, START_WORKER_PHRASE};
use rr_one_core::compose::ServiceStatus;
use rr_one_core::health::{self, HealthState, ProbeResult};
use rr_one_core::local;
use rr_one_core::paths::AppPaths;
use rr_one_core::runner::SystemRunner;
use rr_one_core::settings::{OperatingMode, Settings};
use rr_one_core::supervisor::{PlanReport, Supervisor};
use rr_one_core::{INITIAL_SAFETY_STATE, VERSION};
use serde::Serialize;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, State, Url, WebviewUrl, WebviewWindowBuilder, WindowEvent};

const PROBE_TIMEOUT: Duration = Duration::from_secs(3);

struct AppState {
    paths: Result<AppPaths, String>,
    settings: Mutex<Settings>,
    /// Set when the settings file exists but is invalid. Defaults are used in memory; the file is untouched.
    settings_error: Option<String>,
    /// One supervisor action at a time.
    busy: AtomicBool,
}

#[derive(Serialize)]
struct ActionInfo {
    id: &'static str,
    requires_confirmation: bool,
}

#[derive(Serialize)]
struct ServiceInfo {
    id: Service,
    label: &'static str,
    runtime: rr_one_core::actions::Runtime,
}

#[derive(Serialize)]
struct Overview {
    version: &'static str,
    paths: Option<AppPaths>,
    paths_error: Option<String>,
    settings: Settings,
    settings_error: Option<String>,
    endpoint: Option<rr_one_core::settings::EngineEndpoint>,
    initial_safety_state: rr_one_core::InitialSafetyState,
    actions: Vec<ActionInfo>,
    services: Vec<ServiceInfo>,
    worker_confirmation_phrase: &'static str,
    /// Why service control is unavailable (no compose file / not LOCAL mode), if it is.
    supervisor_unavailable: Option<String>,
}

#[tauri::command]
fn get_overview(state: State<'_, AppState>) -> Overview {
    let settings = state.settings.lock().expect("settings lock").clone();
    Overview {
        version: VERSION,
        paths: state.paths.as_ref().ok().cloned(),
        paths_error: state.paths.as_ref().err().cloned(),
        endpoint: settings.endpoint().ok(),
        settings,
        settings_error: state.settings_error.clone(),
        initial_safety_state: INITIAL_SAFETY_STATE,
        actions: Action::ALL.iter().map(|a| ActionInfo { id: a.id(), requires_confirmation: a.requires_confirmation() }).collect(),
        services: Service::ALL.iter().map(|s| ServiceInfo { id: *s, label: s.label(), runtime: s.runtime() }).collect(),
        worker_confirmation_phrase: START_WORKER_PHRASE,
        supervisor_unavailable: supervisor_unavailable(&state),
    }
}

fn local_port(state: &AppState) -> Result<u16, String> {
    let s = state.settings.lock().map_err(|_| "settings lock poisoned")?.clone();
    match s.operating_mode {
        OperatingMode::Local => Ok(s.gateway_port),
        OperatingMode::Remote => Err("REMOTE ENGINE MODE is reserved for a future release; health checks run in LOCAL mode".into()),
    }
}

#[tauri::command]
async fn check_health(state: State<'_, AppState>) -> Result<Vec<ProbeResult>, String> {
    let port = local_port(&state)?;
    tauri::async_runtime::spawn_blocking(move || vec![health::probe_engine(port, PROBE_TIMEOUT), health::probe_web(port, PROBE_TIMEOUT)])
        .await
        .map_err(|e| e.to_string())
}

#[derive(Serialize)]
struct PlanPreview {
    action: &'static str,
    steps: Vec<rr_one_core::actions::Step>,
}

#[tauri::command]
fn plan_action(action: String, confirmation: Option<String>) -> Result<PlanPreview, String> {
    let a = Action::parse(&action).map_err(|e| e.to_string())?;
    let steps = a.plan(confirmation.as_deref()).map_err(|e| e.to_string())?;
    Ok(PlanPreview { action: a.id(), steps })
}

fn supervisor_unavailable(state: &AppState) -> Option<String> {
    let settings = state.settings.lock().ok()?.clone();
    if settings.operating_mode != OperatingMode::Local {
        return Some("service control applies to LOCAL WINDOWS MODE only".into());
    }
    let paths = match &state.paths {
        Ok(p) => p,
        Err(e) => return Some(e.clone()),
    };
    if local::find_compose_file(&paths.install_dir, std::env::var_os("RR_ONE_INFRA_DIR").map(Into::into)).is_none() {
        return Some("infrastructure/compose.yml not found next to the application".into());
    }
    None
}

/// Run `f` with a supervisor for this machine, on a blocking thread, one action at a time.
async fn with_supervisor<T: Send + 'static>(
    state: &AppState,
    f: impl FnOnce(&Supervisor<'_>) -> Result<T, String> + Send + 'static,
) -> Result<T, String> {
    let paths = state.paths.clone()?;
    let settings = state.settings.lock().map_err(|_| "settings lock poisoned")?.clone();
    if state.busy.swap(true, Ordering::SeqCst) {
        return Err("another service action is still running".into());
    }
    let res = tauri::async_runtime::spawn_blocking(move || {
        let runner = SystemRunner;
        let setup = local::prepare(&paths, &settings, &runner)?;
        let sup = Supervisor {
            cfg: setup.config,
            runner: &runner,
            secrets: &setup.secrets,
            probe: Box::new(|port| health::probe_engine(port, PROBE_TIMEOUT)),
        };
        f(&sup)
    })
    .await
    .map_err(|e| e.to_string());
    state.busy.store(false, Ordering::SeqCst);
    res?
}

/// Execute an allowlisted action. The confirmation phrase is checked again inside `Action::plan`.
#[tauri::command]
async fn run_action(state: State<'_, AppState>, action: String, confirmation: Option<String>) -> Result<PlanReport, String> {
    let a = Action::parse(&action).map_err(|e| e.to_string())?;
    a.plan(confirmation.as_deref()).map_err(|e| e.to_string())?;
    with_supervisor(&state, move |sup| sup.execute(a, confirmation.as_deref()).map_err(|e| e.to_string())).await
}

/// Live container states for the Services table.
#[tauri::command]
async fn service_status(state: State<'_, AppState>) -> Result<Vec<ServiceStatus>, String> {
    with_supervisor(&state, |sup| sup.status()).await
}

/// Open the existing engine web interface. Only when the engine passes its real health check.
#[tauri::command]
async fn open_console(app: AppHandle, state: State<'_, AppState>) -> Result<(), String> {
    let port = local_port(&state)?;
    let probe = tauri::async_runtime::spawn_blocking(move || health::probe_engine(port, PROBE_TIMEOUT))
        .await
        .map_err(|e| e.to_string())?;
    if probe.state != HealthState::Healthy {
        return Err(format!("Engine is not healthy: {}", probe.detail));
    }
    show_console(&app, port)
}

fn show_console(app: &AppHandle, port: u16) -> Result<(), String> {
    if let Some(w) = app.get_webview_window("console") {
        w.show().map_err(|e| e.to_string())?;
        return w.set_focus().map_err(|e| e.to_string());
    }
    let origin = format!("http://127.0.0.1:{port}");
    let url = Url::parse(&format!("{origin}/")).map_err(|e| e.to_string())?;
    let allowed = origin.clone();
    WebviewWindowBuilder::new(app, "console", WebviewUrl::External(url))
        .title("RR Trading One")
        .inner_size(1440.0, 900.0)
        .min_inner_size(1024.0, 680.0)
        // Pin navigation to the engine origin; anything else (external links, redirects elsewhere) is refused.
        .on_navigation(move |u| u.origin().ascii_serialization() == allowed)
        .build()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open Control Centre", true, None::<&str>)?;
    let console = MenuItem::with_id(app, "console", "Open Trading Console", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit RR Trading One", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &console, &PredefinedMenuItem::separator(app)?, &quit])?;
    let mut tray = TrayIconBuilder::with_id("main-tray")
        .tooltip("RR Trading One")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, ev| match ev.id().as_ref() {
            "open" => show_main(app),
            "console" => {
                let port = app.state::<AppState>().settings.lock().map(|s| s.gateway_port).unwrap_or_default();
                let local = app.state::<AppState>().settings.lock().map(|s| s.operating_mode == OperatingMode::Local).unwrap_or(false);
                // Same rule as the command: only open the console for a healthy local engine.
                if local && health::probe_engine(port, PROBE_TIMEOUT).state == HealthState::Healthy {
                    let _ = show_console(app, port);
                } else {
                    show_main(app);
                }
            }
            // Quitting closes the desktop shell only. Engine services keep running (the monitor keeps protecting
            // positions); stopping them is the explicit "Stop All" action.
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, ev| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = ev {
                show_main(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

fn load_state() -> AppState {
    let paths = AppPaths::for_current_process().map_err(|e| e.to_string());
    let (settings, settings_error) = match &paths {
        Ok(p) => {
            if let Err(e) = p.ensure() {
                eprintln!("could not create application directories: {e}");
            }
            match Settings::load(&p.settings_file()) {
                Ok(s) => (s, None),
                Err(e) => (Settings::default(), Some(e.to_string())),
            }
        }
        Err(_) => (Settings::default(), None),
    };
    AppState { paths, settings: Mutex::new(settings), settings_error, busy: AtomicBool::new(false) }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let state = load_state();
    let start_minimized = state.settings.lock().map(|s| s.start_minimized).unwrap_or(false);
    tauri::Builder::default()
        // One supervisor per user session; a second launch focuses the existing window.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_main(app)))
        .manage(state)
        .invoke_handler(tauri::generate_handler![get_overview, check_health, plan_action, run_action, service_status, open_console])
        .setup(move |app| {
            build_tray(app.handle())?;
            if !start_minimized {
                show_main(app.handle());
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    let to_tray = window.state::<AppState>().settings.lock().map(|s| s.close_to_tray).unwrap_or(true);
                    if to_tray {
                        api.prevent_close();
                        let _ = window.hide();
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running RR Trading One");
}
