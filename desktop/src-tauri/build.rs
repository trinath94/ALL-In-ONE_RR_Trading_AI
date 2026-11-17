// Declaring the app's commands in a manifest makes each one a permission that must be granted explicitly by a
// capability. Only the local `main` window is granted them (capabilities/main.json); the engine console window,
// a remote origin, gets none.
//
// Every command in `generate_handler!` (src/lib.rs) must be listed here AND granted in capabilities/main.json: with a
// manifest, Tauri rejects an unlisted command at runtime. (Phase 21 found the Phase 17 and 18 commands missing; a test
// in rr-one-core, `tauri_acl_lists_every_command`, now keeps the three lists equal.)
fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "get_overview", "check_health", "plan_action", "run_action", "service_status", "open_console",
            "mt5_overview", "mt5_detect", "mt5_save_profile", "mt5_select_profile", "mt5_delete_profile", "mt5_link",
            "engine_status", "engine_bootstrap_admin", "engine_use_demo_account",
            "backup_list", "backup_restore", "backup_import_file", "backup_import", "prerequisites",
            "backup_key_export", "backup_key_add", "rotate_secrets", "backup_import_report", "maintenance_status",
        ]),
    ))
    .expect("failed to run tauri-build");
}
