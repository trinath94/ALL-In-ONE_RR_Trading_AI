// Declaring the app's commands in a manifest makes each one a permission that must be granted explicitly by a
// capability. Only the local `main` window is granted them (capabilities/main.json); the engine console window,
// a remote origin, gets none.
fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&["get_overview", "check_health", "plan_action", "run_action", "service_status", "open_console",
            "mt5_overview", "mt5_detect", "mt5_save_profile", "mt5_select_profile", "mt5_delete_profile", "mt5_link"]),
    ))
    .expect("failed to run tauri-build");
}
