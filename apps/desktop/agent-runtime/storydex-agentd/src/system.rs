//! Candidate-global configuration routes.
//!
//! The configuration root is the parent of the isolated Coomi home. This is
//! `~/.storydex` for the normal layout and the temporary desktop profile for
//! Rust Beta/Tauri smoke runs.

use crate::workspace::atomic_write;
use crate::{AppState, error_response};
use axum::Json;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;
use uuid::Uuid;

const MAX_RECENT_PROJECTS: usize = 8;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct UiPreferences {
    theme: String,
    active_activity: String,
    workbench_mode: String,
    sidebar_width: i64,
    sidebar_collapsed: bool,
    agent_collapsed: bool,
    agent_width: i64,
    left_pane_font_scale: i64,
    center_pane_font_scale: i64,
    right_pane_font_scale: i64,
    font_family: String,
    file_font_size: i64,
    player_font_size: i64,
    updated_at: String,
}

impl Default for UiPreferences {
    fn default() -> Self {
        Self {
            theme: "default".to_owned(),
            active_activity: "resources".to_owned(),
            workbench_mode: "storydex".to_owned(),
            sidebar_width: 320,
            sidebar_collapsed: false,
            agent_collapsed: false,
            agent_width: 560,
            left_pane_font_scale: 100,
            center_pane_font_scale: 100,
            right_pane_font_scale: 100,
            font_family: "system".to_owned(),
            file_font_size: 16,
            player_font_size: 14,
            updated_at: Utc::now().to_rfc3339(),
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct UiPreferencesInput {
    theme: Option<String>,
    active_activity: Option<String>,
    workbench_mode: Option<String>,
    sidebar_width: Option<i64>,
    sidebar_collapsed: Option<bool>,
    agent_collapsed: Option<bool>,
    agent_width: Option<i64>,
    left_pane_font_scale: Option<i64>,
    center_pane_font_scale: Option<i64>,
    right_pane_font_scale: Option<i64>,
    font_family: Option<String>,
    file_font_size: Option<i64>,
    player_font_size: Option<i64>,
    updated_at: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct AgentSettings {
    coomi_memory_enabled: bool,
    wiki_context_enabled: bool,
    updated_at: String,
}

impl Default for AgentSettings {
    fn default() -> Self {
        Self {
            coomi_memory_enabled: true,
            wiki_context_enabled: true,
            updated_at: String::new(),
        }
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct AgentSettingsInput {
    coomi_memory_enabled: bool,
    wiki_context_enabled: bool,
}

fn trace(started: Instant) -> Value {
    json!({
        "traceId": Uuid::new_v4().to_string(),
        "durationMs": started.elapsed().as_millis(),
        "toolCalls": 0,
        "llmCalls": 0,
    })
}

fn success(data: Value, started: Instant, action: &str) -> Response {
    Json(json!({
        "ok": true,
        "data": data,
        "error": null,
        "trace": trace(started),
        "audit": [{"action": action}],
    }))
    .into_response()
}

fn config_error(action: &str, error: impl std::fmt::Display) -> Response {
    error_response(
        StatusCode::UNPROCESSABLE_ENTITY,
        "global_config_error",
        &format!("{action} failed: {error}"),
    )
}

pub(crate) fn global_root(state: &AppState) -> PathBuf {
    state
        .coomi_home()
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| state.coomi_home().to_path_buf())
}

fn ui_preferences_path(state: &AppState) -> PathBuf {
    global_root(state).join("ui").join("preferences.json")
}

fn workspace_state_path(state: &AppState) -> PathBuf {
    global_root(state).join("state").join("workspace.json")
}

fn agent_settings_path(state: &AppState) -> PathBuf {
    global_root(state).join("config").join("agent.json")
}

fn read_json(path: &Path) -> Value {
    fs::read_to_string(path)
        .ok()
        .and_then(|content| serde_json::from_str::<Value>(&content).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}))
}

fn write_json(path: &Path, payload: &impl Serialize) -> std::io::Result<()> {
    let mut bytes = serde_json::to_vec_pretty(payload).map_err(std::io::Error::other)?;
    bytes.push(b'\n');
    atomic_write(path, &bytes)
}

fn clean_string(value: Option<String>, fallback: &str) -> String {
    value
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| fallback.to_owned())
}

fn normalize_ui(
    input: UiPreferencesInput,
    current: UiPreferences,
    touch_updated_at: bool,
) -> UiPreferences {
    let file_font_size = input
        .file_font_size
        .unwrap_or(current.file_font_size)
        .clamp(12, 24);
    let legacy_center_scale = ((file_font_size * 5 + 2) / 4) * 5;
    UiPreferences {
        theme: clean_string(input.theme, &current.theme),
        active_activity: clean_string(input.active_activity, &current.active_activity),
        workbench_mode: if input
            .workbench_mode
            .as_deref()
            .is_some_and(|value| value.trim().eq_ignore_ascii_case("storydex"))
        {
            "storydex".to_owned()
        } else {
            current.workbench_mode
        },
        sidebar_width: input
            .sidebar_width
            .unwrap_or(current.sidebar_width)
            .clamp(220, 520),
        sidebar_collapsed: input.sidebar_collapsed.unwrap_or(current.sidebar_collapsed),
        agent_collapsed: input.agent_collapsed.unwrap_or(current.agent_collapsed),
        agent_width: input
            .agent_width
            .unwrap_or(current.agent_width)
            .clamp(320, 760),
        left_pane_font_scale: input
            .left_pane_font_scale
            .unwrap_or(current.left_pane_font_scale)
            .clamp(75, 150),
        center_pane_font_scale: input
            .center_pane_font_scale
            .unwrap_or(if current.center_pane_font_scale == 0 {
                legacy_center_scale
            } else {
                current.center_pane_font_scale
            })
            .clamp(75, 150),
        right_pane_font_scale: input
            .right_pane_font_scale
            .unwrap_or(current.right_pane_font_scale)
            .clamp(75, 150),
        font_family: clean_string(input.font_family, &current.font_family),
        file_font_size,
        player_font_size: input
            .player_font_size
            .unwrap_or(current.player_font_size)
            .clamp(12, 28),
        updated_at: if touch_updated_at {
            Utc::now().to_rfc3339()
        } else {
            clean_string(input.updated_at, &current.updated_at)
        },
    }
}

fn read_ui(state: &AppState) -> UiPreferences {
    let payload = read_json(&ui_preferences_path(state));
    let defaults = UiPreferences::default();
    normalize_ui(
        serde_json::from_value(payload).unwrap_or_default(),
        defaults,
        false,
    )
}

fn read_agent(state: &AppState) -> AgentSettings {
    serde_json::from_value(read_json(&agent_settings_path(state))).unwrap_or_default()
}

fn normalize_workspace_state(mut payload: Value) -> Value {
    let now = Utc::now().to_rfc3339();
    let mut recent = payload
        .get_mut("recentProjects")
        .and_then(Value::as_array_mut)
        .map(std::mem::take)
        .unwrap_or_default()
        .into_iter()
        .filter_map(|item| {
            let root = item
                .get("workspaceRoot")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())?;
            let project_name = item
                .get("projectName")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(str::to_owned)
                .or_else(|| {
                    Path::new(root)
                        .file_name()
                        .and_then(|value| value.to_str())
                        .map(str::to_owned)
                })
                .unwrap_or_else(|| root.to_owned());
            Some(json!({
                "projectName": project_name,
                "workspaceRoot": root,
                "openedAt": item.get("openedAt").and_then(Value::as_str).filter(|value| !value.is_empty()).unwrap_or(&now),
            }))
        })
        .take(MAX_RECENT_PROJECTS)
        .collect::<Vec<_>>();
    let last = payload
        .get("lastProjectPath")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .or_else(|| {
            recent
                .first()
                .and_then(|item| item.get("workspaceRoot"))
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .unwrap_or_default();
    recent.truncate(MAX_RECENT_PROJECTS);
    json!({
        "lastProjectPath": last,
        "recentProjects": recent,
        "updatedAt": payload.get("updatedAt").and_then(Value::as_str).filter(|value| !value.is_empty()).unwrap_or(&now),
    })
}

fn read_workspace_state(state: &AppState) -> Value {
    normalize_workspace_state(read_json(&workspace_state_path(state)))
}

pub(crate) fn record_recent_project(state: &AppState, workspace: &Path) -> std::io::Result<()> {
    let now = Utc::now().to_rfc3339();
    let root = workspace.display().to_string();
    let project_name = workspace
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or(&root)
        .to_owned();
    let current = read_workspace_state(state);
    let mut recent = vec![json!({
        "projectName": project_name,
        "workspaceRoot": root,
        "openedAt": now,
    })];
    if let Some(items) = current.get("recentProjects").and_then(Value::as_array) {
        for item in items {
            if item.get("workspaceRoot").and_then(Value::as_str) == Some(root.as_str()) {
                continue;
            }
            recent.push(item.clone());
            if recent.len() >= MAX_RECENT_PROJECTS {
                break;
            }
        }
    }
    write_json(
        &workspace_state_path(state),
        &json!({
            "lastProjectPath": root,
            "recentProjects": recent,
            "updatedAt": now,
        }),
    )
}

pub(crate) async fn bootstrap(State(state): State<AppState>) -> Response {
    let started = Instant::now();
    let ui = read_ui(&state);
    let workspace = read_workspace_state(&state);
    success(
        json!({
            "globalRoot": global_root(&state).display().to_string(),
            "uiPreferences": ui,
            "workspaceState": workspace,
        }),
        started,
        "read_system_bootstrap",
    )
}

pub(crate) async fn ui_preferences(State(state): State<AppState>) -> Response {
    let started = Instant::now();
    success(
        serde_json::to_value(read_ui(&state)).unwrap_or_else(|_| json!({})),
        started,
        "read_ui_preferences",
    )
}

pub(crate) async fn update_ui_preferences(
    State(state): State<AppState>,
    Json(input): Json<UiPreferencesInput>,
) -> Response {
    let started = Instant::now();
    let normalized = normalize_ui(input, read_ui(&state), true);
    if let Err(error) = write_json(&ui_preferences_path(&state), &normalized) {
        return config_error("Writing UI preferences atomically", error);
    }
    success(
        serde_json::to_value(normalized).unwrap_or_else(|_| json!({})),
        started,
        "update_ui_preferences",
    )
}

pub(crate) async fn agent_settings(State(state): State<AppState>) -> Response {
    let started = Instant::now();
    success(
        serde_json::to_value(read_agent(&state)).unwrap_or_else(|_| json!({})),
        started,
        "read_agent_settings",
    )
}

pub(crate) async fn update_agent_settings(
    State(state): State<AppState>,
    Json(input): Json<AgentSettingsInput>,
) -> Response {
    let started = Instant::now();
    let normalized = AgentSettings {
        coomi_memory_enabled: input.coomi_memory_enabled,
        wiki_context_enabled: input.wiki_context_enabled,
        updated_at: Utc::now().to_rfc3339(),
    };
    if let Err(error) = write_json(&agent_settings_path(&state), &normalized) {
        return config_error("Writing Agent settings atomically", error);
    }
    success(
        serde_json::to_value(normalized).unwrap_or_else(|_| json!({})),
        started,
        "update_agent_settings",
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn ui_preferences_are_clamped_and_persisted_atomically() {
        let root = tempdir().expect("root");
        let home = root.path().join("coomi-home");
        fs::create_dir_all(&home).expect("home");
        let state = AppState::with_paths(
            "token",
            home,
            root.path().join("bridge"),
            Some(root.path().to_path_buf()),
            None,
        )
        .expect("state");
        let normalized = normalize_ui(
            UiPreferencesInput {
                sidebar_width: Some(9999),
                agent_width: Some(1),
                ..UiPreferencesInput::default()
            },
            UiPreferences::default(),
            true,
        );
        write_json(&ui_preferences_path(&state), &normalized).expect("write");
        let persisted = read_ui(&state);
        assert_eq!(persisted.sidebar_width, 520);
        assert_eq!(persisted.agent_width, 320);
    }

    #[test]
    fn recent_projects_are_deduplicated() {
        let root = tempdir().expect("root");
        let home = root.path().join("coomi-home");
        let workspace = root.path().join("workspace");
        fs::create_dir_all(&home).expect("home");
        fs::create_dir_all(&workspace).expect("workspace");
        let state = AppState::with_paths(
            "token",
            home,
            root.path().join("bridge"),
            Some(root.path().to_path_buf()),
            None,
        )
        .expect("state");
        record_recent_project(&state, &workspace).expect("first");
        record_recent_project(&state, &workspace).expect("second");
        let recent = read_workspace_state(&state)["recentProjects"]
            .as_array()
            .expect("recent projects")
            .len();
        assert_eq!(recent, 1);
    }
}
