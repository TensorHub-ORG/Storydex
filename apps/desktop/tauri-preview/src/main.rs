#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod desktop;
mod sidecar;

use anyhow::{ensure, Result};
use serde::Serialize;
use std::env;
use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, State, WebviewUrl, WebviewWindowBuilder};

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct OpenTarget {
    id: u64,
    path: String,
    is_file: bool,
}

#[derive(Default)]
pub struct OpenTargetStore {
    next_id: AtomicU64,
    pending: std::sync::Mutex<Option<OpenTarget>>,
}

fn queue_open_target(app: &AppHandle, store: &OpenTargetStore, candidate: &str) {
    let Some(target) = build_open_target(store, candidate) else {
        return;
    };
    *store
        .pending
        .lock()
        .unwrap_or_else(|error| error.into_inner()) = Some(target.clone());
    let _ = app.emit("storydex:open-target", target);
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.set_focus();
    }
}

fn build_open_target(store: &OpenTargetStore, candidate: &str) -> Option<OpenTarget> {
    let value = candidate.trim().trim_matches('"');
    if value.is_empty() || value.starts_with('-') || value == "." {
        return None;
    }
    let path = PathBuf::from(value);
    let canonical = fs::canonicalize(&path).ok()?;
    let metadata = fs::metadata(&canonical).ok()?;
    Some(OpenTarget {
        id: store.next_id.fetch_add(1, Ordering::Relaxed) + 1,
        path: canonical.display().to_string(),
        is_file: metadata.is_file(),
    })
}

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

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct TitleBarResult {
    applied: bool,
    color: Option<String>,
    symbol_color: Option<String>,
    height: Option<u32>,
}

#[tauri::command]
fn set_titlebar_theme(_theme: serde_json::Value) -> TitleBarResult {
    TitleBarResult {
        applied: false,
        color: None,
        symbol_color: None,
        height: None,
    }
}

#[tauri::command]
fn get_pending_open_target(state: State<'_, Arc<OpenTargetStore>>) -> Option<OpenTarget> {
    state
        .pending
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .clone()
}

#[tauri::command]
fn ack_open_target(target_id: u64, state: State<'_, Arc<OpenTargetStore>>) -> bool {
    let mut pending = state
        .pending
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    if pending.as_ref().map(|target| target.id) != Some(target_id) {
        return false;
    }
    pending.take();
    true
}

#[tauri::command]
fn open_preview_window(
    app: AppHandle,
    runtime: State<'_, Arc<sidecar::SidecarRuntime>>,
    relative_path: String,
) -> Result<bool, String> {
    let relative_path =
        normalize_relative_path(&relative_path).map_err(|error| error.to_string())?;
    if let Some(window) = app.get_webview_window("preview") {
        app.emit_to("preview", "storydex:preview-open-file", relative_path)
            .map_err(|error| error.to_string())?;
        let _ = window.unminimize();
        window.set_focus().map_err(|error| error.to_string())?;
        return Ok(true);
    }

    let initialization_script = runtime
        .initialization_script(app.package_info().version.to_string())
        .map_err(|error| error.to_string())?;
    let encoded_path: String =
        url::form_urlencoded::byte_serialize(relative_path.as_bytes()).collect();
    let preview_url = format!("index.html#/preview?relativePath={encoded_path}");
    WebviewWindowBuilder::new(&app, "preview", WebviewUrl::App(preview_url.into()))
        .title("Storydex 预览")
        .inner_size(1180.0, 860.0)
        .min_inner_size(760.0, 560.0)
        .resizable(true)
        .initialization_script(initialization_script)
        .build()
        .map_err(|error| error.to_string())?;
    Ok(true)
}

#[tauri::command]
fn runtime_info(runtime: tauri::State<'_, Arc<sidecar::SidecarRuntime>>) -> sidecar::RuntimeInfo {
    runtime.runtime_info()
}

fn normalize_relative_path(value: &str) -> Result<String> {
    let normalized = value.trim().replace('\\', "/").trim_matches('/').to_owned();
    ensure!(!normalized.is_empty(), "relative preview path is required");
    ensure!(
        normalized != "." && !normalized.contains("../") && normalized != "..",
        "invalid relative preview path"
    );
    Ok(normalized)
}

fn main() {
    let open_targets = Arc::new(OpenTargetStore::default());
    let single_instance_targets = open_targets.clone();
    let app = tauri::Builder::default()
        .manage(open_targets.clone())
        .plugin(tauri_plugin_single_instance::init(
            move |app, argv, _cwd| {
                for argument in argv.iter().skip(1) {
                    queue_open_target(app, &single_instance_targets, argument);
                }
            },
        ))
        .invoke_handler(tauri::generate_handler![
            runtime_info,
            pick_directory,
            reveal_path,
            open_with_dialog,
            set_titlebar_theme,
            get_pending_open_target,
            ack_open_target,
            open_preview_window
        ])
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|app| {
            let targets = app.state::<Arc<OpenTargetStore>>().inner().clone();
            for argument in env::args().skip(1) {
                queue_open_target(app.handle(), &targets, &argument);
            }
            let runtime = sidecar::SidecarRuntime::start(app)?;
            let initialization_script =
                runtime.initialization_script(app.package_info().version.to_string())?;
            let webview_data_dir = runtime.webview_data_directory();
            app.manage(runtime.clone());

            let mut window =
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title("Storydex")
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
        .expect("error while building Storydex Tauri desktop shell");

    app.run(|app_handle, event| {
        if let tauri::RunEvent::WindowEvent {
            label,
            event: tauri::WindowEvent::CloseRequested { api, .. },
            ..
        } = &event
        {
            if label == "main" {
                api.prevent_close();
                let _ = app_handle.exit(0);
            }
        }

        if matches!(event, tauri::RunEvent::ExitRequested { .. }) {
            if let Some(runtime) = app_handle.try_state::<Arc<sidecar::SidecarRuntime>>() {
                runtime.shutdown(Duration::from_secs(6));
            }
        }
    });
}
