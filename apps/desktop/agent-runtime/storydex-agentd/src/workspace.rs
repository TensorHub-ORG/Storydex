//! Candidate workspace and file routes used by both desktop shells.
//!
//! The selected workspace is explicit state. In isolated Rust Beta and Tauri
//! smoke runs, `STORYDEX_AGENTD_REFACTOR_ROOT` is both the initial workspace
//! and a hard path boundary. In an unbounded production candidate, callers
//! must first select a project through the open/create routes.

#![allow(clippy::result_large_err)]

use crate::system::record_recent_project;
use crate::{AppState, error_response};
use axum::Json;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use chrono::{DateTime, Utc};
use coomi_services::resolve_bounded_path;
use serde::Deserialize;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::cmp::Ordering as CompareOrdering;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Component, Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant, SystemTime};
use uuid::Uuid;

const STORYDEX_DIR: &str = ".storydex";
const MAX_TREE_NODES: usize = 20_000;
const MAX_SEARCH_FILE_BYTES: u64 = 2 * 1024 * 1024;
const MAX_FULL_READ_BYTES: u64 = 8 * 1024 * 1024;
const WINDOWS_FILE_OPERATION_ATTEMPTS: usize = 5;

const STANDARD_DIRECTORIES: &[&str] = &[
    "chapters",
    ".storydex",
    ".storydex/config",
    ".storydex/presets",
    ".storydex/presets/active",
    ".storydex/presets/library",
    ".storydex/presets/compiled",
    ".storydex/presets/blocked",
    ".storydex/characters",
    ".storydex/worldbook",
    ".storydex/scripts",
    ".storydex/templates",
    ".storydex/templates/characters",
    ".storydex/templates/chapters",
    ".storydex/memory",
    ".storydex/memory/current-state",
    ".storydex/memory/current",
    ".storydex/memory/chapters",
    ".storydex/wiki",
    ".storydex/temp",
    ".storydex/.agent",
    ".storydex/.agent/skills",
    ".storydex/.agent/sessions",
    ".storydex/.agent/plans",
    ".storydex/.agent/temp",
];

const PROTECTED_DIRECTORIES: &[&str] = &[
    ".storydex",
    ".storydex/.agent",
    ".storydex/.agent/logs",
    ".storydex/characters",
    ".storydex/file-history",
    ".storydex/logs",
    ".storydex/memory",
    ".storydex/presets",
    ".storydex/regexs",
    ".storydex/scripts",
    ".storydex/sessions",
    ".storydex/templates",
    ".storydex/worldbook",
];

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ProjectPathRequest {
    #[serde(default)]
    project_path: String,
    #[serde(default)]
    architecture: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct FileReadRequest {
    #[serde(default)]
    relative_path: String,
    offset: Option<usize>,
    limit: Option<usize>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct FileWindowRequest {
    #[serde(default)]
    relative_path: String,
    #[serde(default)]
    start_line: usize,
    #[serde(default = "default_window_lines")]
    line_count: usize,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct FileWriteRequest {
    #[serde(default)]
    relative_path: String,
    #[serde(default)]
    content: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct CreateFileRequest {
    #[serde(default)]
    relative_path: String,
    #[serde(default)]
    content: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct PathRequest {
    #[serde(default)]
    relative_path: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct TransferRequest {
    #[serde(default)]
    from_relative_path: String,
    #[serde(default)]
    to_relative_path: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ImportFilesRequest {
    #[serde(default)]
    target_directory: String,
    #[serde(default)]
    files: Vec<ImportFileItem>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ImportFileItem {
    #[serde(default)]
    name: String,
    #[serde(default)]
    content_base64: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct SearchRequest {
    #[serde(default)]
    query: String,
    #[serde(default = "default_search_limit")]
    limit: usize,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DiagnosticsRequest {
    #[serde(default)]
    relative_paths: Vec<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DiagnosticFixRequest {
    #[serde(default)]
    relative_path: String,
    #[serde(default)]
    fix_id: String,
}

fn default_window_lines() -> usize {
    400
}

fn default_search_limit() -> usize {
    20
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

fn workspace_error(status: StatusCode, code: &str, message: impl AsRef<str>) -> Response {
    error_response(status, code, message.as_ref())
}

fn io_error(action: &str, error: impl std::fmt::Display) -> Response {
    workspace_error(
        StatusCode::UNPROCESSABLE_ENTITY,
        "workspace_io_error",
        format!("{action} failed: {error}"),
    )
}

pub(crate) fn current_workspace(state: &AppState) -> Result<PathBuf, Response> {
    let selected = state.current_workspace().ok_or_else(|| {
        workspace_error(
            StatusCode::CONFLICT,
            "workspace_not_selected",
            "Select or create a Storydex project before using workspace routes.",
        )
    })?;
    let canonical = selected.canonicalize().map_err(|error| {
        workspace_error(
            StatusCode::NOT_FOUND,
            "workspace_not_found",
            format!("The selected workspace is unavailable: {error}"),
        )
    })?;
    if !canonical.is_dir() {
        return Err(workspace_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "workspace_not_directory",
            "The selected workspace is not a directory.",
        ));
    }
    ensure_boundary(state, &canonical)?;
    Ok(canonical)
}

pub(crate) fn resolve_workspace_for_request(
    state: &AppState,
    requested: &str,
) -> Result<PathBuf, Response> {
    let requested = requested.trim();
    if requested.is_empty() {
        return current_workspace(state);
    }
    let candidate = PathBuf::from(requested);
    let canonical = candidate.canonicalize().map_err(|_| {
        workspace_error(
            StatusCode::NOT_FOUND,
            "workspace_not_found",
            "The requested workspace does not exist.",
        )
    })?;
    if !canonical.is_dir() {
        return Err(workspace_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "workspace_not_directory",
            "The requested workspace is not a directory.",
        ));
    }
    if state.refactor_root().is_some() {
        ensure_boundary(state, &canonical)?;
        return Ok(canonical);
    }
    let selected = current_workspace(state)?;
    if canonical != selected {
        return Err(workspace_error(
            StatusCode::FORBIDDEN,
            "workspace_not_selected",
            "The requested workspace is not the project selected by the desktop shell.",
        ));
    }
    Ok(canonical)
}

fn ensure_boundary(state: &AppState, candidate: &Path) -> Result<(), Response> {
    let Some(boundary) = state.refactor_root() else {
        return Ok(());
    };
    resolve_bounded_path(boundary, candidate).map_err(|_| {
        workspace_error(
            StatusCode::FORBIDDEN,
            "workspace_outside_refactor_root",
            "The project path is outside the isolated candidate workspace boundary.",
        )
    })?;
    Ok(())
}

fn ensure_workspace_switch_allowed(state: &AppState, workspace: &Path) -> Result<(), Response> {
    if let Some(active) = state.execution_registry().active()
        && active.workspace_root != workspace
    {
        return Err(workspace_error(
            StatusCode::CONFLICT,
            "agent_busy",
            "Stop the active Storydex Agent execution before switching projects.",
        ));
    }
    Ok(())
}

fn ensure_workspace_mutation_allowed(state: &AppState) -> Result<(), Response> {
    if state.execution_registry().active().is_some() {
        return Err(workspace_error(
            StatusCode::CONFLICT,
            "agent_busy",
            "Stop the active Storydex Agent execution before creating or initializing a project.",
        ));
    }
    Ok(())
}

fn validate_project_path(
    state: &AppState,
    raw: &str,
    must_exist: bool,
) -> Result<PathBuf, Response> {
    let raw = raw.trim();
    if raw.is_empty() {
        return Err(workspace_error(
            StatusCode::BAD_REQUEST,
            "project_path_required",
            "projectPath is required.",
        ));
    }
    let candidate = PathBuf::from(raw);
    if state.refactor_root().is_none() && !candidate.is_absolute() {
        return Err(workspace_error(
            StatusCode::BAD_REQUEST,
            "project_path_must_be_absolute",
            "Project selection requires an absolute directory path.",
        ));
    }
    if must_exist {
        let canonical = candidate.canonicalize().map_err(|_| {
            workspace_error(
                StatusCode::NOT_FOUND,
                "project_path_not_found",
                "The project directory does not exist.",
            )
        })?;
        if !canonical.is_dir() {
            return Err(workspace_error(
                StatusCode::UNPROCESSABLE_ENTITY,
                "project_path_invalid",
                "The project path must be a directory.",
            ));
        }
        ensure_boundary(state, &canonical)?;
        return Ok(canonical);
    }

    if candidate.exists() {
        let canonical = candidate
            .canonicalize()
            .map_err(|error| io_error("Resolving the project directory", error))?;
        if !canonical.is_dir() {
            return Err(workspace_error(
                StatusCode::UNPROCESSABLE_ENTITY,
                "project_path_invalid",
                "The project path must be a directory.",
            ));
        }
        ensure_boundary(state, &canonical)?;
        return Ok(canonical);
    }

    let mut existing = candidate.as_path();
    while !existing.exists() {
        existing = existing.parent().ok_or_else(|| {
            workspace_error(
                StatusCode::UNPROCESSABLE_ENTITY,
                "project_path_invalid",
                "The project path has no existing parent directory.",
            )
        })?;
    }
    let canonical_parent = existing
        .canonicalize()
        .map_err(|error| io_error("Resolving the project parent directory", error))?;
    ensure_boundary(state, &canonical_parent)?;
    Ok(candidate)
}

pub(crate) fn normalize_relative(raw: &str) -> Result<String, Response> {
    let replaced = raw.trim().replace('\\', "/");
    if replaced.is_empty() {
        return Err(workspace_error(
            StatusCode::BAD_REQUEST,
            "relative_path_required",
            "relativePath is required.",
        ));
    }
    if replaced.starts_with('/')
        || replaced.starts_with("//")
        || (replaced.len() >= 2 && replaced.as_bytes()[1] == b':')
    {
        return Err(workspace_error(
            StatusCode::FORBIDDEN,
            "workspace_path_outside_root",
            "Absolute paths are not allowed in workspace file operations.",
        ));
    }
    let mut parts = Vec::new();
    for component in Path::new(&replaced).components() {
        match component {
            Component::Normal(part) => parts.push(part.to_string_lossy().into_owned()),
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                return Err(workspace_error(
                    StatusCode::FORBIDDEN,
                    "workspace_path_outside_root",
                    "Workspace paths cannot escape the selected project.",
                ));
            }
        }
    }
    if parts.is_empty() {
        return Err(workspace_error(
            StatusCode::BAD_REQUEST,
            "relative_path_required",
            "relativePath is required.",
        ));
    }
    Ok(parts.join("/"))
}

fn reject_symlink_components(workspace: &Path, normalized: &str) -> Result<(), Response> {
    let mut current = workspace.to_path_buf();
    for part in normalized.split('/') {
        current.push(part);
        match fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err(workspace_error(
                    StatusCode::FORBIDDEN,
                    "workspace_symlink_forbidden",
                    "Workspace file operations do not follow symbolic links.",
                ));
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => break,
            Err(error) => return Err(io_error("Inspecting the workspace path", error)),
        }
    }
    Ok(())
}

pub(crate) fn resolve_existing(workspace: &Path, raw: &str) -> Result<(String, PathBuf), Response> {
    let normalized = normalize_relative(raw)?;
    reject_symlink_components(workspace, &normalized)?;
    let candidate = workspace.join(Path::new(&normalized));
    let canonical = candidate.canonicalize().map_err(|_| {
        workspace_error(
            StatusCode::NOT_FOUND,
            "workspace_path_not_found",
            "The requested workspace path does not exist.",
        )
    })?;
    if !canonical.starts_with(workspace) {
        return Err(workspace_error(
            StatusCode::FORBIDDEN,
            "workspace_path_outside_root",
            "The requested path escapes the selected project.",
        ));
    }
    Ok((normalized, canonical))
}

pub(crate) fn resolve_target(workspace: &Path, raw: &str) -> Result<(String, PathBuf), Response> {
    let normalized = normalize_relative(raw)?;
    reject_symlink_components(workspace, &normalized)?;
    let candidate = workspace.join(Path::new(&normalized));
    let mut existing = candidate.as_path();
    while !existing.exists() {
        existing = existing.parent().ok_or_else(|| {
            workspace_error(
                StatusCode::FORBIDDEN,
                "workspace_path_outside_root",
                "The requested path escapes the selected project.",
            )
        })?;
    }
    let canonical_parent = existing
        .canonicalize()
        .map_err(|error| io_error("Resolving the workspace path", error))?;
    if !canonical_parent.starts_with(workspace) {
        return Err(workspace_error(
            StatusCode::FORBIDDEN,
            "workspace_path_outside_root",
            "The requested path escapes the selected project.",
        ));
    }
    Ok((normalized, candidate))
}

fn is_protected_directory(normalized: &str) -> bool {
    PROTECTED_DIRECTORIES.contains(&normalized)
}

fn is_forbidden_delete_target(normalized: &str) -> bool {
    PROTECTED_DIRECTORIES.iter().any(|protected| {
        normalized == *protected || protected.starts_with(&format!("{normalized}/"))
    })
}

pub(crate) fn atomic_write(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let parent = path.parent().ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::InvalidInput, "target has no parent")
    })?;
    fs::create_dir_all(parent)?;
    let file_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("storydex-file");
    let temporary = parent.join(format!(".{file_name}.{}.tmp", Uuid::new_v4()));
    let backup = parent.join(format!(".{file_name}.{}.backup", Uuid::new_v4()));
    let write_result = (|| -> std::io::Result<()> {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        drop(file);
        if path.exists() {
            rename_with_retry(path, &backup)?;
            match rename_with_retry(&temporary, path) {
                Ok(()) => {
                    let _ = remove_file_with_retry(&backup);
                    Ok(())
                }
                Err(error) => {
                    let _ = rename_with_retry(&backup, path);
                    Err(error)
                }
            }
        } else {
            rename_with_retry(&temporary, path)
        }
    })();
    let _ = remove_file_with_retry(&temporary);
    if write_result.is_err() && backup.exists() && !path.exists() {
        let _ = rename_with_retry(&backup, path);
    }
    write_result
}

pub(crate) fn rename_with_retry(source: &Path, target: &Path) -> std::io::Result<()> {
    retry_windows_file_operation(|| fs::rename(source, target))
}

fn remove_file_with_retry(path: &Path) -> std::io::Result<()> {
    retry_windows_file_operation(|| fs::remove_file(path))
}

fn retry_windows_file_operation<T>(
    mut operation: impl FnMut() -> std::io::Result<T>,
) -> std::io::Result<T> {
    let mut last_error = None;
    for attempt in 0..WINDOWS_FILE_OPERATION_ATTEMPTS {
        match operation() {
            Ok(value) => return Ok(value),
            Err(error) if is_transient_windows_file_lock(&error) => last_error = Some(error),
            Err(error) => return Err(error),
        }
        if attempt + 1 < WINDOWS_FILE_OPERATION_ATTEMPTS {
            thread::sleep(Duration::from_millis(20 * (1_u64 << attempt)));
        }
    }
    Err(last_error.unwrap_or_else(|| std::io::Error::other("file operation retry exhausted")))
}

fn is_transient_windows_file_lock(error: &std::io::Error) -> bool {
    cfg!(windows)
        && (matches!(
            error.kind(),
            std::io::ErrorKind::PermissionDenied | std::io::ErrorKind::WouldBlock
        ) || matches!(error.raw_os_error(), Some(5 | 32 | 33)))
}

fn create_bytes(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let parent = path.parent().ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::InvalidInput, "target has no parent")
    })?;
    fs::create_dir_all(parent)?;
    let mut file = OpenOptions::new().create_new(true).write(true).open(path)?;
    file.write_all(bytes)?;
    file.sync_all()
}

fn modified_iso(metadata: &fs::Metadata) -> String {
    metadata
        .modified()
        .ok()
        .map(DateTime::<Utc>::from)
        .map(|value| value.to_rfc3339())
        .unwrap_or_default()
}

fn modified_ms(metadata: &fs::Metadata) -> Option<u128> {
    metadata
        .modified()
        .ok()
        .and_then(|value| value.duration_since(SystemTime::UNIX_EPOCH).ok())
        .map(|duration| duration.as_millis())
}

fn sha256_file(path: &Path) -> std::io::Result<String> {
    if path.is_dir() {
        return Ok(String::new());
    }
    let bytes = fs::read(path)?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

fn path_info(relative_path: &str, path: &Path) -> std::io::Result<Value> {
    if !path.exists() {
        return Ok(json!({
            "relativePath": relative_path,
            "exists": false,
            "kind": "file",
            "size": 0,
            "mtimeMs": null,
            "sha256": "",
        }));
    }
    let metadata = fs::metadata(path)?;
    Ok(json!({
        "relativePath": relative_path,
        "exists": true,
        "kind": if metadata.is_dir() { "directory" } else { "file" },
        "size": if metadata.is_file() { metadata.len() } else { 0 },
        "mtimeMs": modified_ms(&metadata),
        "sha256": sha256_file(path)?,
    }))
}

fn describe_document(
    relative_path: &str,
    path: &Path,
    offset: Option<usize>,
    limit: Option<usize>,
) -> Result<Value, Response> {
    let metadata = fs::metadata(path).map_err(|error| io_error("Reading file metadata", error))?;
    if metadata.is_dir() {
        let mut children = fs::read_dir(path)
            .map_err(|error| io_error("Reading directory", error))?
            .filter_map(Result::ok)
            .collect::<Vec<_>>();
        children.sort_by_key(|entry| entry.file_name());
        let limit = limit.unwrap_or(200).max(1);
        let visible = children.iter().take(limit).collect::<Vec<_>>();
        let mut lines = visible
            .iter()
            .map(|entry| {
                let kind = entry.file_type().ok().is_some_and(|value| value.is_dir());
                format!(
                    "- {} {}",
                    if kind { "dir" } else { "file" },
                    entry.file_name().to_string_lossy()
                )
            })
            .collect::<Vec<_>>();
        if children.len() > visible.len() {
            lines.push(format!(
                "... {} more item(s)",
                children.len() - visible.len()
            ));
        }
        let mut content = lines.join("\n");
        if !content.is_empty() {
            content.push('\n');
        }
        return Ok(json!({
            "relativePath": relative_path,
            "content": content,
            "fullContentSha256": "",
            "mtimeMs": modified_ms(&metadata),
            "size": 0,
            "wordCount": 0,
            "updatedAt": modified_iso(&metadata),
            "extension": "",
            "kind": "directory",
            "lineCount": lines.len(),
            "lineCountExact": true,
            "offset": null,
            "limit": if children.len() > visible.len() { Some(limit) } else { None },
            "isPartialView": children.len() > visible.len(),
            "childCount": children.len(),
        }));
    }
    if metadata.len() > MAX_FULL_READ_BYTES && limit.is_none() {
        return Err(workspace_error(
            StatusCode::PAYLOAD_TOO_LARGE,
            "file_too_large",
            "Use the bounded file window endpoint to read this large file.",
        ));
    }
    let bytes = fs::read(path).map_err(|error| io_error("Reading the workspace file", error))?;
    let decoded = String::from_utf8(bytes.clone()).map_err(|_| {
        workspace_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "file_not_utf8",
            "The workspace file is not valid UTF-8 text.",
        )
    })?;
    let content = decoded.strip_prefix('\u{feff}').unwrap_or(&decoded);
    let lines = content.lines().collect::<Vec<_>>();
    let normalized_offset = offset.unwrap_or(0).min(lines.len());
    let end = limit
        .map(|value| {
            normalized_offset
                .saturating_add(value.max(1))
                .min(lines.len())
        })
        .unwrap_or(lines.len());
    let partial = normalized_offset > 0 || end < lines.len();
    let selected = if partial {
        let mut value = lines[normalized_offset..end].join("\n");
        if content.ends_with('\n') && end < lines.len() {
            value.push('\n');
        }
        value
    } else {
        content.to_owned()
    };
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .map(|value| format!(".{value}"))
        .unwrap_or_default()
        .to_ascii_lowercase();
    Ok(json!({
        "relativePath": relative_path,
        "content": selected,
        "fullContentSha256": format!("{:x}", Sha256::digest(content.as_bytes())),
        "mtimeMs": modified_ms(&metadata),
        "size": metadata.len(),
        "wordCount": content.chars().filter(|value| !value.is_whitespace()).count(),
        "updatedAt": modified_iso(&metadata),
        "extension": extension,
        "kind": "file",
        "title": path.file_stem().and_then(|value| value.to_str()).unwrap_or_default(),
        "displayPath": relative_path,
        "readOnly": false,
        "lineCount": lines.len(),
        "lineCountExact": true,
        "offset": if partial { Some(normalized_offset) } else { None },
        "limit": if partial { limit } else { None },
        "isPartialView": partial,
    }))
}

fn describe_project(state: &AppState, workspace: &Path) -> Value {
    let missing_directories = STANDARD_DIRECTORIES
        .iter()
        .filter(|relative| !workspace.join(relative).is_dir())
        .map(|relative| (*relative).to_owned())
        .collect::<Vec<_>>();
    let free_ready =
        workspace.join(".storydex/.agent").is_dir() && workspace.join(".storydex/.cache").is_dir();
    let architecture = if missing_directories.is_empty() {
        "standard"
    } else if free_ready {
        "free"
    } else {
        "unconfigured"
    };
    let has_config = architecture != "unconfigured";
    let effective_missing = if architecture == "free" {
        Vec::new()
    } else {
        missing_directories
    };
    let project_name = workspace
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or_else(|| workspace.to_str().unwrap_or("Storydex Project"));
    json!({
        "projectName": project_name,
        "workspaceRoot": workspace.to_string_lossy(),
        "storydexRoot": workspace.join(STORYDEX_DIR).to_string_lossy(),
        "storydexDirName": STORYDEX_DIR,
        "hasStorydexConfig": has_config,
        "requiresInitialization": !has_config,
        "missingDirectories": effective_missing,
        "projectState": if has_config { "ready" } else { "needs_init" },
        "openedAt": state.current_workspace_opened_at().unwrap_or_else(|| Utc::now().to_rfc3339()),
        "architecture": architecture,
    })
}

fn ensure_readme(path: &Path, title: &str) -> std::io::Result<()> {
    let readme = path.join("README.md");
    if !readme.exists() {
        atomic_write(
            &readme,
            format!("# {title}\n\nStorydex 项目资产目录。\n").as_bytes(),
        )?;
    }
    Ok(())
}

fn reject_initialization_symlinks(workspace: &Path, relative: &str) -> std::io::Result<()> {
    let mut current = workspace.to_path_buf();
    for part in relative.split('/') {
        current.push(part);
        match fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::PermissionDenied,
                    format!(
                        "project initialization refuses symbolic-link component: {}",
                        current.display()
                    ),
                ));
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => break,
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

fn initialize_structure(workspace: &Path, architecture: &str) -> std::io::Result<()> {
    if architecture == "free" {
        for relative in [".storydex/.agent", ".storydex/.cache"] {
            reject_initialization_symlinks(workspace, relative)?;
            fs::create_dir_all(workspace.join(relative))?;
        }
    } else {
        for relative in STANDARD_DIRECTORIES {
            reject_initialization_symlinks(workspace, relative)?;
            fs::create_dir_all(workspace.join(relative))?;
        }
        reject_initialization_symlinks(workspace, ".storydex/.cache")?;
        fs::create_dir_all(workspace.join(".storydex/.cache"))?;
        ensure_readme(&workspace.join("chapters"), "正文章节")?;
        ensure_readme(&workspace.join(".storydex/characters"), "角色档案")?;
        ensure_readme(&workspace.join(".storydex/worldbook"), "世界书")?;
        ensure_readme(
            &workspace.join(".storydex/memory"),
            "Storydex 长期记忆与变量",
        )?;
    }
    reject_initialization_symlinks(workspace, ".storydex/project.json")?;
    let project_path = workspace.join(".storydex/project.json");
    let mut project = if project_path.exists() {
        fs::read_to_string(&project_path)
            .ok()
            .and_then(|value| serde_json::from_str::<Value>(&value).ok())
            .and_then(|value| value.as_object().cloned())
            .unwrap_or_default()
    } else {
        Map::new()
    };
    project.entry("name").or_insert_with(|| {
        json!(
            workspace
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("Storydex Project")
        )
    });
    project
        .entry("created_at")
        .or_insert_with(|| json!(Utc::now().to_rfc3339()));
    project.insert("storydex_version".to_owned(), json!("2026.04"));
    let story_settings = project.entry("storySettings").or_insert_with(|| json!({}));
    if !story_settings.is_object() {
        *story_settings = json!({});
    }
    if let Some(settings) = story_settings.as_object_mut() {
        settings
            .entry("fragmentFormat")
            .or_insert_with(|| json!("md"));
        settings
            .entry("updatedAt")
            .or_insert_with(|| json!(Utc::now().to_rfc3339()));
    }
    let bytes =
        serde_json::to_vec_pretty(&Value::Object(project)).map_err(std::io::Error::other)?;
    let mut terminated = bytes;
    terminated.push(b'\n');
    atomic_write(&project_path, &terminated)
}

pub(crate) async fn current_project(State(state): State<AppState>) -> Response {
    let started = Instant::now();
    match current_workspace(&state) {
        Ok(workspace) => success(
            describe_project(&state, &workspace),
            started,
            "read_current_project",
        ),
        Err(response) => response,
    }
}

pub(crate) async fn open_project(
    State(state): State<AppState>,
    Json(request): Json<ProjectPathRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match validate_project_path(&state, &request.project_path, true) {
        Ok(workspace) => workspace,
        Err(response) => return response,
    };
    if let Err(response) = ensure_workspace_switch_allowed(&state, &workspace) {
        return response;
    }
    if let Err(error) = record_recent_project(&state, &workspace) {
        return io_error("Persisting the recent project", error);
    }
    if let Err(error) = state.select_workspace(workspace.clone()) {
        return io_error("Selecting the project", error);
    }
    success(
        describe_project(&state, &workspace),
        started,
        "open_project",
    )
}

pub(crate) async fn create_project(
    State(state): State<AppState>,
    Json(request): Json<ProjectPathRequest>,
) -> Response {
    let started = Instant::now();
    let target = match validate_project_path(&state, &request.project_path, false) {
        Ok(target) => target,
        Err(response) => return response,
    };
    if let Err(response) = ensure_workspace_mutation_allowed(&state) {
        return response;
    }
    if let Err(error) = fs::create_dir_all(&target) {
        return io_error("Creating the project directory", error);
    }
    let workspace = match target.canonicalize() {
        Ok(workspace) => workspace,
        Err(error) => return io_error("Resolving the created project", error),
    };
    if let Err(response) = ensure_boundary(&state, &workspace) {
        return response;
    }
    let architecture = if request.architecture.trim() == "free" {
        "free"
    } else {
        "standard"
    };
    if let Err(error) = initialize_structure(&workspace, architecture) {
        return io_error("Initializing the project", error);
    }
    if let Err(error) = record_recent_project(&state, &workspace) {
        return io_error("Persisting the recent project", error);
    }
    if let Err(error) = state.select_workspace(workspace.clone()) {
        return io_error("Selecting the created project", error);
    }
    success(
        describe_project(&state, &workspace),
        started,
        "create_project",
    )
}

pub(crate) async fn initialize_project(
    State(state): State<AppState>,
    Json(request): Json<Option<ProjectPathRequest>>,
) -> Response {
    let started = Instant::now();
    let request = request.unwrap_or(ProjectPathRequest {
        project_path: String::new(),
        architecture: String::new(),
    });
    let workspace = if request.project_path.trim().is_empty() {
        match current_workspace(&state) {
            Ok(workspace) => workspace,
            Err(response) => return response,
        }
    } else {
        match validate_project_path(&state, &request.project_path, true) {
            Ok(workspace) => workspace,
            Err(response) => return response,
        }
    };
    if let Err(response) = ensure_workspace_mutation_allowed(&state) {
        return response;
    }
    let architecture = if request.architecture.trim() == "free" {
        "free"
    } else {
        "standard"
    };
    if let Err(error) = initialize_structure(&workspace, architecture) {
        return io_error("Initializing the project", error);
    }
    if let Err(error) = record_recent_project(&state, &workspace) {
        return io_error("Persisting the recent project", error);
    }
    if let Err(error) = state.select_workspace(workspace.clone()) {
        return io_error("Selecting the initialized project", error);
    }
    success(
        describe_project(&state, &workspace),
        started,
        "initialize_project",
    )
}

fn include_tree_path(relative: &str) -> bool {
    relative != ".git"
        && !relative.starts_with(".git/")
        && relative != ".storydex/.agent/temp"
        && !relative.starts_with(".storydex/.agent/temp/")
}

fn tree_node(workspace: &Path, path: &Path, count: &mut usize) -> Result<Option<Value>, Response> {
    if *count >= MAX_TREE_NODES {
        return Ok(None);
    }
    let relative = path
        .strip_prefix(workspace)
        .map_err(|_| {
            workspace_error(
                StatusCode::FORBIDDEN,
                "workspace_path_outside_root",
                "Tree path escaped the workspace.",
            )
        })?
        .to_string_lossy()
        .replace('\\', "/");
    if !include_tree_path(&relative) {
        return Ok(None);
    }
    let symlink_metadata = fs::symlink_metadata(path)
        .map_err(|error| io_error("Reading the workspace tree", error))?;
    if symlink_metadata.file_type().is_symlink() {
        return Ok(None);
    }
    *count += 1;
    if symlink_metadata.is_dir() {
        let mut entries = fs::read_dir(path)
            .map_err(|error| io_error("Reading the workspace tree", error))?
            .filter_map(Result::ok)
            .collect::<Vec<_>>();
        entries.sort_by(|left, right| {
            let left_dir = left.file_type().ok().is_some_and(|value| value.is_dir());
            let right_dir = right.file_type().ok().is_some_and(|value| value.is_dir());
            match (left_dir, right_dir) {
                (true, false) => CompareOrdering::Less,
                (false, true) => CompareOrdering::Greater,
                _ => left
                    .file_name()
                    .to_string_lossy()
                    .to_lowercase()
                    .cmp(&right.file_name().to_string_lossy().to_lowercase()),
            }
        });
        let mut children = Vec::new();
        for entry in entries {
            if let Some(child) = tree_node(workspace, &entry.path(), count)? {
                children.push(child);
            }
        }
        return Ok(Some(json!({
            "name": path.file_name().and_then(|value| value.to_str()).unwrap_or_default(),
            "relativePath": relative,
            "kind": "directory",
            "children": children,
        })));
    }
    Ok(Some(json!({
        "name": path.file_name().and_then(|value| value.to_str()).unwrap_or_default(),
        "relativePath": relative,
        "kind": "file",
        "children": [],
        "extension": path.extension().and_then(|value| value.to_str()).map(|value| format!(".{value}")).unwrap_or_default().to_ascii_lowercase(),
        "size": symlink_metadata.len(),
        "updatedAt": modified_iso(&symlink_metadata),
    })))
}

fn find_default_file(workspace: &Path) -> Option<String> {
    let mut stack = vec![workspace.to_path_buf()];
    while let Some(directory) = stack.pop() {
        let mut entries = fs::read_dir(&directory)
            .ok()?
            .filter_map(Result::ok)
            .collect::<Vec<_>>();
        entries.sort_by_key(|entry| entry.file_name());
        for entry in entries {
            let path = entry.path();
            let relative = path
                .strip_prefix(workspace)
                .ok()?
                .to_string_lossy()
                .replace('\\', "/");
            if !include_tree_path(&relative) || entry.file_type().ok()?.is_symlink() {
                continue;
            }
            if path.is_dir() {
                stack.push(path);
            } else if matches!(
                path.extension().and_then(|value| value.to_str()),
                Some("md" | "txt")
            ) && !path
                .file_name()
                .and_then(|value| value.to_str())
                .is_some_and(|value| value.eq_ignore_ascii_case("README.md"))
            {
                return Some(relative);
            }
        }
    }
    None
}

pub(crate) async fn workspace_tree(State(state): State<AppState>) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(workspace) => workspace,
        Err(response) => return response,
    };
    let mut entries = match fs::read_dir(&workspace) {
        Ok(entries) => entries.filter_map(Result::ok).collect::<Vec<_>>(),
        Err(error) => return io_error("Reading the workspace tree", error),
    };
    entries.sort_by_key(|entry| entry.file_name());
    let mut count = 0;
    let mut roots = Vec::new();
    for entry in entries {
        match tree_node(&workspace, &entry.path(), &mut count) {
            Ok(Some(node)) => roots.push(node),
            Ok(None) => {}
            Err(response) => return response,
        }
    }
    let project = describe_project(&state, &workspace);
    success(
        json!({
            "workspaceRoot": workspace.to_string_lossy(),
            "storydexRoot": workspace.join(STORYDEX_DIR).to_string_lossy(),
            "projectName": project["projectName"],
            "hasStorydexConfig": project["hasStorydexConfig"],
            "requiresInitialization": project["requiresInitialization"],
            "missingDirectories": project["missingDirectories"],
            "openedAt": project["openedAt"],
            "architecture": project["architecture"],
            "defaultFile": find_default_file(&workspace),
            "roots": roots,
            "truncated": count >= MAX_TREE_NODES,
        }),
        started,
        "read_workspace_tree",
    )
}

pub(crate) async fn read_file(
    State(state): State<AppState>,
    Json(request): Json<FileReadRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(workspace) => workspace,
        Err(response) => return response,
    };
    let (relative, path) = match resolve_existing(&workspace, &request.relative_path) {
        Ok(value) => value,
        Err(response) => return response,
    };
    match describe_document(&relative, &path, request.offset, request.limit) {
        Ok(document) => success(document, started, "read_file"),
        Err(response) => response,
    }
}

pub(crate) async fn read_file_window(
    State(state): State<AppState>,
    Json(request): Json<FileWindowRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(workspace) => workspace,
        Err(response) => return response,
    };
    let (relative, path) = match resolve_existing(&workspace, &request.relative_path) {
        Ok(value) => value,
        Err(response) => return response,
    };
    if !path.is_file() {
        return workspace_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "file_window_requires_file",
            "The bounded window endpoint only accepts files.",
        );
    }
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) => return io_error("Reading the workspace file", error),
    };
    let decoded = match String::from_utf8(bytes) {
        Ok(value) => value,
        Err(_) => {
            return workspace_error(
                StatusCode::UNPROCESSABLE_ENTITY,
                "file_not_utf8",
                "The workspace file is not valid UTF-8 text.",
            );
        }
    };
    let content = decoded.strip_prefix('\u{feff}').unwrap_or(&decoded);
    let lines = content.lines().collect::<Vec<_>>();
    let start = request.start_line.min(lines.len());
    let count = request.line_count.clamp(1, 2000);
    let end = start.saturating_add(count).min(lines.len());
    let selected = lines[start..end].join("\n");
    let metadata = match fs::metadata(&path) {
        Ok(metadata) => metadata,
        Err(error) => return io_error("Reading file metadata", error),
    };
    success(
        json!({
            "relativePath": relative,
            "content": selected,
            "size": metadata.len(),
            "mtimeMs": modified_ms(&metadata),
            "startLine": start,
            "loadedLines": end.saturating_sub(start),
            "lineCount": lines.len(),
            "lineCountExact": true,
            "hasPrevious": start > 0,
            "hasNext": end < lines.len(),
            "mode": if metadata.len() > MAX_FULL_READ_BYTES { "large-readonly" } else { "progressive" },
            "readOnly": metadata.len() > MAX_FULL_READ_BYTES,
            "initialChunkBytes": selected.len(),
        }),
        started,
        "read_file_window",
    )
}

pub(crate) async fn write_file(
    State(state): State<AppState>,
    Json(request): Json<FileWriteRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(workspace) => workspace,
        Err(response) => return response,
    };
    let (relative, path) = match resolve_target(&workspace, &request.relative_path) {
        Ok(value) => value,
        Err(response) => return response,
    };
    if is_protected_directory(&relative) || path.is_dir() {
        return workspace_error(
            StatusCode::FORBIDDEN,
            "workspace_protected_path",
            "Writing directly to a protected Storydex directory is forbidden.",
        );
    }
    if let Err(error) = atomic_write(&path, request.content.as_bytes()) {
        return io_error("Writing the workspace file atomically", error);
    }
    match describe_document(&relative, &path, None, None) {
        Ok(document) => success(document, started, "write_file"),
        Err(response) => response,
    }
}

pub(crate) async fn create_file(
    State(state): State<AppState>,
    Json(request): Json<CreateFileRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(workspace) => workspace,
        Err(response) => return response,
    };
    let (relative, path) = match resolve_target(&workspace, &request.relative_path) {
        Ok(value) => value,
        Err(response) => return response,
    };
    if path.exists() {
        return workspace_error(
            StatusCode::CONFLICT,
            "workspace_target_exists",
            "The workspace file already exists.",
        );
    }
    if let Err(error) = create_bytes(&path, request.content.as_bytes()) {
        return io_error("Creating the workspace file", error);
    }
    match describe_document(&relative, &path, None, None) {
        Ok(document) => success(document, started, "create_file"),
        Err(response) => response,
    }
}

pub(crate) async fn create_directory(
    State(state): State<AppState>,
    Json(request): Json<PathRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(workspace) => workspace,
        Err(response) => return response,
    };
    let (relative, path) = match resolve_target(&workspace, &request.relative_path) {
        Ok(value) => value,
        Err(response) => return response,
    };
    if path.exists() {
        return workspace_error(
            StatusCode::CONFLICT,
            "workspace_target_exists",
            "The workspace directory already exists.",
        );
    }
    if let Err(error) = fs::create_dir_all(&path) {
        return io_error("Creating the workspace directory", error);
    }
    match path_info(&relative, &path) {
        Ok(info) => success(info, started, "create_directory"),
        Err(error) => io_error("Reading the created directory", error),
    }
}

fn sanitize_file_name(raw: &str) -> String {
    let normalized = raw.replace('\\', "/");
    normalized
        .split('/')
        .rfind(|part| !part.is_empty())
        .unwrap_or_default()
        .chars()
        .filter(|value| {
            !matches!(value, '<' | '>' | ':' | '"' | '|' | '?' | '*') && !value.is_control()
        })
        .collect::<String>()
        .trim()
        .to_owned()
}

fn unique_import_target(
    workspace: &Path,
    directory: &str,
    file_name: &str,
) -> Result<(String, PathBuf), Response> {
    let sanitized = sanitize_file_name(file_name);
    if sanitized.is_empty() {
        return Err(workspace_error(
            StatusCode::BAD_REQUEST,
            "import_file_name_invalid",
            "Imported files require a valid file name.",
        ));
    }
    let leaf = Path::new(&sanitized);
    let stem = leaf
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or(&sanitized);
    let suffix = leaf
        .extension()
        .and_then(|value| value.to_str())
        .map(|value| format!(".{value}"))
        .unwrap_or_default();
    for attempt in 0..500 {
        let name = if attempt == 0 {
            sanitized.clone()
        } else {
            format!("{stem}-{attempt}{suffix}")
        };
        let relative = if directory.is_empty() {
            name
        } else {
            format!("{directory}/{name}")
        };
        let (_, target) = resolve_target(workspace, &relative)?;
        if !target.exists() {
            return Ok((relative, target));
        }
    }
    Err(workspace_error(
        StatusCode::CONFLICT,
        "import_target_exhausted",
        "Unable to choose a unique name for the imported file.",
    ))
}

pub(crate) async fn import_files(
    State(state): State<AppState>,
    Json(request): Json<ImportFilesRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(workspace) => workspace,
        Err(response) => return response,
    };
    let directory = if request.target_directory.trim().is_empty() {
        String::new()
    } else {
        match normalize_relative(&request.target_directory) {
            Ok(value) => value,
            Err(response) => return response,
        }
    };
    if !directory.is_empty() {
        let (_, target) = match resolve_target(&workspace, &directory) {
            Ok(value) => value,
            Err(response) => return response,
        };
        if target.exists() && !target.is_dir() {
            return workspace_error(
                StatusCode::UNPROCESSABLE_ENTITY,
                "import_target_not_directory",
                "The import target must be a directory.",
            );
        }
        if let Err(error) = fs::create_dir_all(target) {
            return io_error("Creating the import directory", error);
        }
    }
    let mut items = Vec::new();
    for item in request.files {
        let encoded = item
            .content_base64
            .split_once(',')
            .map(|(_, value)| value)
            .unwrap_or(item.content_base64.as_str());
        let bytes = match BASE64_STANDARD.decode(encoded.trim()) {
            Ok(bytes) => bytes,
            Err(_) => {
                return workspace_error(
                    StatusCode::BAD_REQUEST,
                    "import_content_invalid",
                    "Imported file content is not valid base64.",
                );
            }
        };
        let (relative, target) = match unique_import_target(&workspace, &directory, &item.name) {
            Ok(value) => value,
            Err(response) => return response,
        };
        if let Err(error) = create_bytes(&target, &bytes) {
            return io_error("Importing the workspace file", error);
        }
        match path_info(&relative, &target) {
            Ok(info) => items.push(info),
            Err(error) => return io_error("Reading the imported file", error),
        }
    }
    success(json!({"items": items}), started, "import_files")
}

fn transfer_paths(
    state: &AppState,
    request: &TransferRequest,
) -> Result<(PathBuf, String, PathBuf, String, PathBuf), Response> {
    let workspace = current_workspace(state)?;
    let (from_relative, source) = resolve_existing(&workspace, &request.from_relative_path)?;
    let (to_relative, target) = resolve_target(&workspace, &request.to_relative_path)?;
    if is_protected_directory(&from_relative) || is_protected_directory(&to_relative) {
        return Err(workspace_error(
            StatusCode::FORBIDDEN,
            "workspace_protected_path",
            "Protected Storydex directories cannot be renamed or moved.",
        ));
    }
    if target.exists() {
        return Err(workspace_error(
            StatusCode::CONFLICT,
            "workspace_target_exists",
            "The workspace target already exists.",
        ));
    }
    if target.starts_with(&source) {
        return Err(workspace_error(
            StatusCode::BAD_REQUEST,
            "workspace_recursive_target",
            "A directory cannot be copied or moved inside itself.",
        ));
    }
    Ok((workspace, from_relative, source, to_relative, target))
}

pub(crate) async fn rename_path(
    State(state): State<AppState>,
    Json(request): Json<TransferRequest>,
) -> Response {
    move_path(State(state), Json(request)).await
}

pub(crate) async fn move_path(
    State(state): State<AppState>,
    Json(request): Json<TransferRequest>,
) -> Response {
    let started = Instant::now();
    let (_workspace, _from_relative, source, to_relative, target) =
        match transfer_paths(&state, &request) {
            Ok(value) => value,
            Err(response) => return response,
        };
    if let Some(parent) = target.parent()
        && let Err(error) = fs::create_dir_all(parent)
    {
        return io_error("Creating the move target directory", error);
    }
    if let Err(error) = fs::rename(&source, &target) {
        return io_error("Moving the workspace path", error);
    }
    match path_info(&to_relative, &target) {
        Ok(info) => success(info, started, "move_path"),
        Err(error) => io_error("Reading the moved workspace path", error),
    }
}

fn copy_entry(source: &Path, target: &Path) -> std::io::Result<()> {
    let metadata = fs::symlink_metadata(source)?;
    if metadata.file_type().is_symlink() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "symbolic links are not copied by Storydex",
        ));
    }
    if metadata.is_file() {
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::copy(source, target)?;
        return Ok(());
    }
    fs::create_dir_all(target)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        copy_entry(&entry.path(), &target.join(entry.file_name()))?;
    }
    Ok(())
}

pub(crate) async fn copy_path(
    State(state): State<AppState>,
    Json(request): Json<TransferRequest>,
) -> Response {
    let started = Instant::now();
    let (_workspace, _from_relative, source, to_relative, target) =
        match transfer_paths(&state, &request) {
            Ok(value) => value,
            Err(response) => return response,
        };
    if let Err(error) = copy_entry(&source, &target) {
        let _ = if target.is_dir() {
            fs::remove_dir_all(&target)
        } else {
            fs::remove_file(&target)
        };
        return io_error("Copying the workspace path", error);
    }
    match path_info(&to_relative, &target) {
        Ok(info) => success(info, started, "copy_path"),
        Err(error) => io_error("Reading the copied workspace path", error),
    }
}

pub(crate) async fn delete_path(
    State(state): State<AppState>,
    Json(request): Json<PathRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(workspace) => workspace,
        Err(response) => return response,
    };
    let (relative, path) = match resolve_existing(&workspace, &request.relative_path) {
        Ok(value) => value,
        Err(response) => return response,
    };
    if is_forbidden_delete_target(&relative) {
        return workspace_error(
            StatusCode::FORBIDDEN,
            "workspace_protected_path",
            "Deleting protected Storydex directories is forbidden.",
        );
    }
    let info = match path_info(&relative, &path) {
        Ok(info) => info,
        Err(error) => return io_error("Reading the workspace path before deletion", error),
    };
    let result = if path.is_dir() {
        fs::remove_dir_all(&path)
    } else {
        fs::remove_file(&path)
    };
    if let Err(error) = result {
        return io_error("Deleting the workspace path", error);
    }
    success(info, started, "delete_path")
}

fn is_searchable(path: &Path) -> bool {
    matches!(
        path.extension()
            .and_then(|value| value.to_str())
            .map(str::to_ascii_lowercase)
            .as_deref(),
        Some(
            "md" | "txt"
                | "json"
                | "jsonl"
                | "yaml"
                | "yml"
                | "toml"
                | "js"
                | "ts"
                | "vue"
                | "rs"
                | "py"
        )
    )
}

fn collect_files(
    workspace: &Path,
    directory: &Path,
    output: &mut Vec<PathBuf>,
) -> std::io::Result<()> {
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let path = entry.path();
        let relative = path
            .strip_prefix(workspace)
            .unwrap_or(path.as_path())
            .to_string_lossy()
            .replace('\\', "/");
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() || !include_tree_path(&relative) {
            continue;
        }
        if metadata.is_dir() {
            collect_files(workspace, &path, output)?;
        } else if metadata.len() <= MAX_SEARCH_FILE_BYTES && is_searchable(&path) {
            output.push(path);
        }
    }
    Ok(())
}

pub(crate) async fn search_workspace(
    State(state): State<AppState>,
    Json(request): Json<SearchRequest>,
) -> Response {
    let started = Instant::now();
    let query = request.query.trim();
    if query.is_empty() {
        return workspace_error(
            StatusCode::BAD_REQUEST,
            "search_query_required",
            "A non-empty search query is required.",
        );
    }
    let workspace = match current_workspace(&state) {
        Ok(workspace) => workspace,
        Err(response) => return response,
    };
    let mut files = Vec::new();
    if let Err(error) = collect_files(&workspace, &workspace, &mut files) {
        return io_error("Scanning workspace files for search", error);
    }
    files.sort();
    let query_folded = query.to_lowercase();
    let limit = request.limit.clamp(1, 50);
    let mut items = Vec::new();
    for path in files {
        let Ok(content) = fs::read_to_string(&path) else {
            continue;
        };
        let mut first_line = None;
        let mut first_snippet = String::new();
        let mut matches = 0usize;
        for (index, line) in content.lines().enumerate() {
            let folded = line.to_lowercase();
            let count = folded.match_indices(&query_folded).count();
            if count == 0 {
                continue;
            }
            matches += count;
            if first_line.is_none() {
                first_line = Some(index + 1);
                first_snippet = line.chars().take(240).collect();
            }
        }
        if matches == 0 {
            continue;
        }
        let relative = path
            .strip_prefix(&workspace)
            .map(|value| value.to_string_lossy().replace('\\', "/"))
            .unwrap_or_default();
        items.push(json!({
            "relativePath": relative,
            "snippet": first_snippet,
            "lineNumber": first_line,
            "matchCount": matches,
            "score": matches,
            "engine": "rust-literal",
        }));
        if items.len() >= limit {
            break;
        }
    }
    success(
        json!({"query": query, "items": items}),
        started,
        "search_workspace",
    )
}

pub(crate) async fn workspace_diagnostics(
    State(state): State<AppState>,
    Json(request): Json<DiagnosticsRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(workspace) => workspace,
        Err(response) => return response,
    };
    let mut items = Vec::new();
    for raw in request.relative_paths {
        let normalized = match normalize_relative(&raw) {
            Ok(normalized) => normalized,
            Err(_) => {
                items.push(json!({
                    "code": "workspace.path_invalid",
                    "source": "workspace",
                    "severity": "error",
                    "relativePath": raw,
                    "line": 1,
                    "column": 1,
                    "message": "The path escapes the selected workspace.",
                }));
                continue;
            }
        };
        let (_, path) = match resolve_existing(&workspace, &normalized) {
            Ok(value) => value,
            Err(_) => {
                items.push(json!({
                    "code": "workspace.file_missing",
                    "source": "workspace",
                    "severity": "warning",
                    "relativePath": normalized,
                    "line": 1,
                    "column": 1,
                    "message": "The workspace file does not exist.",
                }));
                continue;
            }
        };
        if !path.is_file() {
            continue;
        }
        let bytes = match fs::read(&path) {
            Ok(bytes) => bytes,
            Err(error) => {
                items.push(json!({
                    "code": "workspace.file_unreadable",
                    "source": "workspace",
                    "severity": "error",
                    "relativePath": normalized,
                    "line": 1,
                    "column": 1,
                    "message": format!("Unable to read the file: {error}"),
                }));
                continue;
            }
        };
        if bytes.starts_with(&[0xef, 0xbb, 0xbf]) {
            items.push(json!({
                "code": "text.utf8_bom",
                "source": "text",
                "severity": "info",
                "relativePath": normalized,
                "line": 1,
                "column": 1,
                "message": "The UTF-8 BOM can be removed safely.",
                "fixes": [{"id": "remove_utf8_bom", "label": "Remove UTF-8 BOM"}],
            }));
        }
        let content = match std::str::from_utf8(&bytes) {
            Ok(content) => content.trim_start_matches('\u{feff}'),
            Err(_) => {
                items.push(json!({
                    "code": "text.invalid_utf8",
                    "source": "text",
                    "severity": "error",
                    "relativePath": normalized,
                    "line": 1,
                    "column": 1,
                    "message": "The file is not valid UTF-8 text.",
                }));
                continue;
            }
        };
        if path
            .extension()
            .and_then(|value| value.to_str())
            .is_some_and(|value| value.eq_ignore_ascii_case("json"))
            && let Err(error) = serde_json::from_str::<Value>(content)
        {
            items.push(json!({
                "code": "json.syntax",
                "source": "json",
                "severity": "error",
                "relativePath": normalized,
                "line": error.line(),
                "column": error.column(),
                "message": error.to_string(),
            }));
        }
    }
    success(
        json!({"items": items}),
        started,
        "read_workspace_diagnostics",
    )
}

pub(crate) async fn apply_diagnostic_fix(
    State(state): State<AppState>,
    Json(request): Json<DiagnosticFixRequest>,
) -> Response {
    let started = Instant::now();
    if request.fix_id != "remove_utf8_bom" {
        return workspace_error(
            StatusCode::BAD_REQUEST,
            "diagnostic_fix_unknown",
            "The requested diagnostic fix is not supported.",
        );
    }
    let workspace = match current_workspace(&state) {
        Ok(workspace) => workspace,
        Err(response) => return response,
    };
    let (relative, path) = match resolve_existing(&workspace, &request.relative_path) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) => return io_error("Reading the diagnostic target", error),
    };
    let changed = bytes.starts_with(&[0xef, 0xbb, 0xbf]);
    if changed && let Err(error) = atomic_write(&path, &bytes[3..]) {
        return io_error("Applying the diagnostic fix atomically", error);
    }
    success(
        json!({
            "relativePath": relative,
            "fixId": request.fix_id,
            "changed": changed,
        }),
        started,
        "apply_workspace_diagnostic_fix",
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn relative_paths_reject_absolute_parent_and_drive_paths() {
        assert!(normalize_relative("chapters/001.md").is_ok());
        for invalid in [
            "../escape.md",
            "..\\escape.md",
            "/absolute.md",
            "C:\\absolute.md",
        ] {
            assert!(normalize_relative(invalid).is_err(), "{invalid}");
        }
    }

    #[test]
    fn atomic_write_replaces_content_and_leaves_no_temporary_files() {
        let root = tempdir().expect("workspace");
        let target = root.path().join("notes.md");
        fs::write(&target, "before").expect("seed");
        atomic_write(&target, b"after").expect("replace");
        assert_eq!(fs::read_to_string(&target).expect("read"), "after");
        assert_eq!(fs::read_dir(root.path()).expect("entries").count(), 1);
    }

    #[cfg(windows)]
    #[test]
    fn transient_windows_file_lock_is_retried() {
        let mut attempts = 0;
        let result = retry_windows_file_operation(|| {
            attempts += 1;
            if attempts < 3 {
                return Err(std::io::Error::from_raw_os_error(5));
            }
            Ok("published")
        });

        assert_eq!(result.expect("retry succeeds"), "published");
        assert_eq!(attempts, 3);
    }

    #[test]
    fn symlink_components_are_rejected() {
        let root = tempdir().expect("workspace");
        let outside = tempdir().expect("outside");
        #[cfg(windows)]
        if std::os::windows::fs::symlink_dir(outside.path(), root.path().join("linked")).is_err() {
            // Windows developer-mode/symlink privileges are not guaranteed in
            // the CI worker. The production resolver remains fail-closed; a
            // privileged worker exercises the assertion below.
            return;
        }
        #[cfg(unix)]
        std::os::unix::fs::symlink(outside.path(), root.path().join("linked"))
            .expect("directory symlink");
        assert!(resolve_target(root.path(), "linked/escape.md").is_err());
    }
}
