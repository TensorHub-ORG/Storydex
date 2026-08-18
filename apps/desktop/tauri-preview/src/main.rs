#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Serialize;
use std::env;

#[derive(Debug, Serialize)]
struct RuntimeInfo {
    runtime: &'static str,
    backend_base_url: String,
    sidecar: &'static str,
}

#[tauri::command]
fn runtime_info() -> RuntimeInfo {
    let backend_base_url = env::var("STORYDEX_TAURI_BACKEND_URL")
        .unwrap_or_else(|_| "http://127.0.0.1:18081/api/v1".to_owned())
        .trim_end_matches('/')
        .to_owned();
    RuntimeInfo {
        runtime: "storydex-tauri-preview",
        backend_base_url,
        sidecar: "storydex-agentd",
    }
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![runtime_info])
        .run(tauri::generate_context!())
        .expect("error while running Storydex Tauri preview");
}
