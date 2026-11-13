// Release builds use the GUI subsystem (no console window on Windows).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    rr_trading_one_lib::run()
}
