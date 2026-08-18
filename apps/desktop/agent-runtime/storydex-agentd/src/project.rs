//! Candidate Rust project-boundary routes.
//!
//! These routes are deliberately scoped to the isolated refactor fixture root
//! configured on `AppState`.  They provide the first real HTTP consumer of the
//! shared Git and WIKI projection primitives; Stable FastAPI routes remain
//! untouched until the complete candidate contract is closed.

use crate::AppState;
use crate::error_response;
use axum::Json;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::Response;
use coomi_services::{
    ProjectionBundle, ProjectionBundleWriter, StorydexGit, graph_checksum, resolve_bounded_path,
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct WorkspaceQuery {
    #[serde(default)]
    pub workspace_root: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct GitCommitRequest {
    #[serde(default)]
    pub workspace_root: String,
    #[serde(default)]
    pub message: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct GitRestoreRequest {
    #[serde(default)]
    pub workspace_root: String,
    pub commit_id: String,
    #[serde(default = "default_true")]
    pub create_backup: bool,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ProjectionWriteRequest {
    pub workspace_root: String,
    pub payload: Value,
    #[serde(default)]
    pub markdown: String,
    #[serde(default)]
    pub index: Value,
    #[serde(default)]
    pub status: Value,
    #[serde(default)]
    pub source_snapshot: Option<Value>,
}

fn default_true() -> bool {
    true
}

fn trace(started: Instant) -> Value {
    json!({
        "traceId": uuid::Uuid::new_v4().to_string(),
        "durationMs": started.elapsed().as_millis(),
        "toolCalls": 0,
        "llmCalls": 0,
    })
}

fn success(data: Value, started: Instant, action: &str) -> Json<Value> {
    Json(json!({
        "ok": true,
        "data": data,
        "error": null,
        "trace": trace(started),
        "audit": [{"action": action}],
    }))
}

#[allow(clippy::result_large_err)]
fn resolve_workspace(state: &AppState, raw: &str) -> Result<PathBuf, Response> {
    let Some(bound) = state.refactor_root() else {
        return Err(error_response(
            StatusCode::SERVICE_UNAVAILABLE,
            "refactor_root_unconfigured",
            "Rust project routes require an isolated refactor fixture root.",
        ));
    };
    let raw = raw.trim();
    if raw.is_empty() {
        return Err(error_response(
            StatusCode::BAD_REQUEST,
            "workspace_root_required",
            "workspaceRoot is required for candidate project routes.",
        ));
    }
    let candidate = PathBuf::from(raw);
    let canonical = resolve_bounded_path(bound, &candidate).map_err(|_| {
        error_response(
            StatusCode::FORBIDDEN,
            "workspace_outside_refactor_root",
            "Candidate project routes only accept existing workspaces inside the isolated fixture root.",
        )
    })?;
    if !canonical.is_dir() {
        return Err(error_response(
            StatusCode::UNPROCESSABLE_ENTITY,
            "workspace_not_directory",
            "Candidate workspace root must be a directory.",
        ));
    }
    Ok(canonical)
}

fn map_git_summary(summary: coomi_services::GitSummary) -> Value {
    json!({
        "available": summary.available,
        "gitInstalled": summary.available,
        "initialized": summary.initialized,
        "branch": summary.branch,
        "clean": summary.clean,
        "changedFiles": summary.changed_files,
        "head": summary.head,
        "defaultBranch": "develop",
        "message": "",
    })
}

pub(crate) async fn git_summary(
    State(state): State<AppState>,
    Query(query): Query<WorkspaceQuery>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &query.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    match StorydexGit.summary(&workspace) {
        Ok(summary) => success(
            map_git_summary(summary),
            started,
            "read_rust_workspace_git_summary",
        )
        .into_response(),
        Err(error) => error_response(
            StatusCode::UNPROCESSABLE_ENTITY,
            "git_summary_failed",
            &format!("Rust Git summary failed: {error:#}"),
        ),
    }
}

pub(crate) async fn git_init(
    State(state): State<AppState>,
    Query(query): Query<WorkspaceQuery>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &query.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    match StorydexGit.initialize(&workspace) {
        Ok(summary) => success(
            map_git_summary(summary),
            started,
            "initialize_rust_workspace_git",
        )
        .into_response(),
        Err(error) => error_response(
            StatusCode::UNPROCESSABLE_ENTITY,
            "git_init_failed",
            &format!("Rust Git initialization failed: {error:#}"),
        ),
    }
}

pub(crate) async fn git_commit(
    State(state): State<AppState>,
    Json(request): Json<GitCommitRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &request.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    match StorydexGit.commit_all(&workspace, &request.message) {
        Ok(result) => success(
            json!({
                "created": result.created,
                "commit": result.commit,
                "summary": map_git_summary(result.summary),
                "event": if result.created { "GitAutoCommit" } else { "GitCommitPrompt" },
            }),
            started,
            "commit_rust_workspace_git",
        )
        .into_response(),
        Err(error) => error_response(
            StatusCode::UNPROCESSABLE_ENTITY,
            "git_commit_failed",
            &format!("Rust Git commit failed: {error:#}"),
        ),
    }
}

pub(crate) async fn git_restore(
    State(state): State<AppState>,
    Json(request): Json<GitRestoreRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &request.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    match StorydexGit.restore_to_commit(&workspace, &request.commit_id, request.create_backup) {
        Ok(summary) => success(
            json!({"restored": true, "summary": map_git_summary(summary)}),
            started,
            "restore_rust_workspace_git",
        )
        .into_response(),
        Err(error) => error_response(
            StatusCode::UNPROCESSABLE_ENTITY,
            "git_restore_failed",
            &format!("Rust Git restore failed: {error:#}"),
        ),
    }
}

pub(crate) async fn wiki_read(
    State(state): State<AppState>,
    Query(query): Query<WorkspaceQuery>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &query.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    let writer = match ProjectionBundleWriter::new(&workspace) {
        Ok(writer) => writer,
        Err(error) => {
            return error_response(
                StatusCode::UNPROCESSABLE_ENTITY,
                "wiki_root_invalid",
                &error.to_string(),
            );
        }
    };
    let wiki_root = writer.wiki_root();
    let payload = match read_json(&wiki_root.join("knowledge_graph.json")) {
        Ok(Some(value)) => value,
        Ok(None) => {
            return error_response(
                StatusCode::NOT_FOUND,
                "wiki_projection_missing",
                "Rust WIKI projection is not available.",
            );
        }
        Err(error) => {
            return error_response(
                StatusCode::UNPROCESSABLE_ENTITY,
                "wiki_projection_invalid",
                &error.to_string(),
            );
        }
    };
    let index = read_json(&wiki_root.join("index.json")).ok().flatten();
    let status = writer.read_status().ok().flatten();
    let markdown = fs::read_to_string(wiki_root.join("WIKI.md")).unwrap_or_default();
    success(
        json!({"wiki": payload, "index": index, "status": status, "markdown": markdown}),
        started,
        "read_rust_story_wiki",
    )
    .into_response()
}

pub(crate) async fn wiki_write(
    State(state): State<AppState>,
    Json(request): Json<ProjectionWriteRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &request.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    let mut status = request.status;
    let checksum = match graph_checksum(&request.payload) {
        Ok(value) => value,
        Err(error) => {
            return error_response(
                StatusCode::UNPROCESSABLE_ENTITY,
                "wiki_checksum_failed",
                &error.to_string(),
            );
        }
    };
    if let Some(object) = status.as_object_mut() {
        object.insert("graphChecksum".to_owned(), Value::String(checksum.clone()));
        object
            .entry("status".to_owned())
            .or_insert_with(|| Value::String("ready".to_owned()));
    }
    let writer = match ProjectionBundleWriter::new(&workspace) {
        Ok(writer) => writer,
        Err(error) => {
            return error_response(
                StatusCode::UNPROCESSABLE_ENTITY,
                "wiki_root_invalid",
                &error.to_string(),
            );
        }
    };
    match writer.write(&ProjectionBundle {
        payload: request.payload,
        markdown: request.markdown,
        index: request.index,
        status,
        source_snapshot: request.source_snapshot,
    }) {
        Ok(result) => success(
            json!({
                "ok": true,
                "changedPaths": result.changed_paths,
                "graphChecksum": checksum,
                "event": "KnowledgeProjectionUpdated",
            }),
            started,
            "write_rust_story_wiki_projection",
        )
        .into_response(),
        Err(error) => error_response(
            StatusCode::UNPROCESSABLE_ENTITY,
            "wiki_projection_write_failed",
            &error.to_string(),
        ),
    }
}

fn read_json(path: &Path) -> anyhow::Result<Option<Value>> {
    match fs::read(path) {
        Ok(bytes) => Ok(Some(serde_json::from_slice(&bytes)?)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

use axum::response::IntoResponse;
