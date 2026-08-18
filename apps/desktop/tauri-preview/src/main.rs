#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod desktop;
mod sidecar;

use std::sync::Arc;
use std::time::Duration;
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

#[tauri::command]
async fn pick_directory(options: Option<desktop::PickDirectoryOptions>) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        desktop::pick_directory(options.unwrap_or_default())
    })
    .await
    .map_err(|error| format!("directory picker task failed: {error}"))?
    .map_err(|error| error.to_string())
}

#[tauri::command]
fn reveal_path(absolute_path: String) -> Result<bool, String> {
    desktop::reveal_path(&absolute_path).map_err(|error| error.to_string())
}

#[tauri::command]
fn open_with_dialog(absolute_path: String) -> Result<bool, String> {
    desktop::open_with_dialog(&absolute_path).map_err(|error| error.to_string())
}

#[tauri::command]
fn runtime_info(runtime: tauri::State<'_, Arc<sidecar::SidecarRuntime>>) -> sidecar::RuntimeInfo {
    runtime.runtime_info()
}

fn main() {
    let app = tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            runtime_info,
            pick_directory,
            reveal_path,
            open_with_dialog
        ])
        .setup(|app| {
            let runtime = sidecar::SidecarRuntime::start(app)?;
            let initialization_script = runtime.initialization_script()?;
            let webview_data_dir = runtime.webview_data_directory();
            app.manage(runtime.clone());

            let mut window =
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title("Storydex Rust/Tauri Preview")
                    .inner_size(1440.0, 900.0)
                    .min_inner_size(1024.0, 700.0)
                    .resizable(true)
                    .initialization_script(initialization_script);
            if let Some(webview_data_dir) = webview_data_dir {
                window = window.data_directory(webview_data_dir);
            }
            window.build()?;

            runtime.monitor(app.handle().clone());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Storydex Tauri preview");

    app.run(|app_handle, event| {
        if matches!(event, tauri::RunEvent::ExitRequested { .. }) {
            if let Some(runtime) = app_handle.try_state::<Arc<sidecar::SidecarRuntime>>() {
                runtime.shutdown(Duration::from_secs(6));
            }
        }
    });
}
