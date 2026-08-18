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
    ProjectionBundle, ProjectionBundleWriter, StorydexGit, StorydexKnowledge, graph_checksum,
    resolve_bounded_path,
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
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
pub(crate) struct GitCommitDiffQuery {
    #[serde(default)]
    pub workspace_root: String,
    #[serde(default)]
    pub commit_id: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct GitBranchCreateRequest {
    #[serde(default)]
    pub workspace_root: String,
    #[serde(default)]
    pub name: String,
    #[serde(default = "default_true")]
    pub checkout: bool,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct GitBranchRequest {
    #[serde(default)]
    pub workspace_root: String,
    #[serde(default)]
    pub name: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct GitJumpRequest {
    #[serde(default)]
    pub workspace_root: String,
    #[serde(default)]
    pub commit_id: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct GitWorldlineCreateRequest {
    #[serde(default)]
    pub workspace_root: String,
    #[serde(default)]
    pub from_commit: String,
    #[serde(default)]
    pub name: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct GitWorldlineRenameRequest {
    #[serde(default)]
    pub workspace_root: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub new_name: String,
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
    #[serde(default)]
    pub change_set: Option<Value>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct WikiGraphQuery {
    #[serde(default)]
    pub workspace_root: String,
    #[serde(default)]
    pub q: String,
    #[serde(default)]
    pub category: String,
    #[serde(default)]
    pub entry_id: String,
    #[serde(default)]
    pub node_id: String,
    #[serde(default = "default_one")]
    pub depth: usize,
    #[serde(default = "default_wiki_limit")]
    pub limit: usize,
    #[serde(default)]
    pub offset: usize,
    #[serde(default)]
    pub include_review: bool,
}

fn default_one() -> usize {
    1
}

fn default_wiki_limit() -> usize {
    60
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
        "gitInstalled": summary.git_installed,
        "initialized": summary.initialized,
        "branch": summary.branch,
        "clean": summary.clean,
        "changedFiles": summary.changed_files,
        "recentCommits": summary.recent_commits,
        "graphLines": summary.graph_lines,
        "head": summary.head,
        "defaultBranch": summary.default_branch,
        "message": summary.message,
        "generatedAt": summary.generated_at,
    })
}

fn git_failure(code: &str, error: anyhow::Error) -> Response {
    let message = format!("{error:#}");
    let normalized = message.to_ascii_lowercase();
    let status = if normalized.contains("invalid branch name") || normalized.contains("is required")
    {
        StatusCode::BAD_REQUEST
    } else if normalized.contains("does not exist") {
        StatusCode::NOT_FOUND
    } else if normalized.contains("uncommitted changes")
        || normalized.contains("already exists")
        || normalized.contains("only worldline")
        || normalized.contains("currently on")
        || normalized.contains("no commits yet")
        || normalized.contains("before the first commit")
    {
        StatusCode::CONFLICT
    } else {
        StatusCode::UNPROCESSABLE_ENTITY
    };
    error_response(
        status,
        code,
        &format!("Rust Git operation failed: {message}"),
    )
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

pub(crate) async fn git_diff(
    State(state): State<AppState>,
    Query(query): Query<WorkspaceQuery>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &query.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    match StorydexGit.diff(&workspace) {
        Ok(result) => {
            success(json!(result), started, "read_rust_workspace_git_diff").into_response()
        }
        Err(error) => git_failure("git_diff_failed", error),
    }
}

pub(crate) async fn git_branches(
    State(state): State<AppState>,
    Query(query): Query<WorkspaceQuery>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &query.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    match StorydexGit.branches(&workspace) {
        Ok(result) => {
            success(json!(result), started, "read_rust_workspace_git_branches").into_response()
        }
        Err(error) => git_failure("git_branches_failed", error),
    }
}

pub(crate) async fn git_create_branch(
    State(state): State<AppState>,
    Json(request): Json<GitBranchCreateRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &request.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    match StorydexGit.create_branch(&workspace, &request.name, request.checkout) {
        Ok(result) => {
            success(json!(result), started, "create_rust_workspace_git_branch").into_response()
        }
        Err(error) => git_failure("git_branch_create_failed", error),
    }
}

pub(crate) async fn git_checkout(
    State(state): State<AppState>,
    Json(request): Json<GitBranchRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &request.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    match StorydexGit.switch_branch(&workspace, &request.name) {
        Ok(result) => {
            success(json!(result), started, "switch_rust_workspace_git_branch").into_response()
        }
        Err(error) => git_failure("git_checkout_failed", error),
    }
}

pub(crate) async fn git_timeline(
    State(state): State<AppState>,
    Query(query): Query<WorkspaceQuery>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &query.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    match StorydexGit.timeline(&workspace) {
        Ok(result) => {
            success(json!(result), started, "read_rust_workspace_git_timeline").into_response()
        }
        Err(error) => git_failure("git_timeline_failed", error),
    }
}

pub(crate) async fn git_commit_diff(
    State(state): State<AppState>,
    Query(query): Query<GitCommitDiffQuery>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &query.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    match StorydexGit.commit_diff(&workspace, &query.commit_id) {
        Ok(result) => success(
            json!(result),
            started,
            "read_rust_workspace_git_commit_diff",
        )
        .into_response(),
        Err(error) => git_failure("git_commit_diff_failed", error),
    }
}

pub(crate) async fn git_jump(
    State(state): State<AppState>,
    Json(request): Json<GitJumpRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &request.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    match StorydexGit.jump_to_commit(&workspace, &request.commit_id) {
        Ok(result) => {
            success(json!(result), started, "jump_rust_workspace_git_commit").into_response()
        }
        Err(error) => git_failure("git_jump_failed", error),
    }
}

pub(crate) async fn git_worldline_create(
    State(state): State<AppState>,
    Json(request): Json<GitWorldlineCreateRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &request.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    match StorydexGit.create_worldline(&workspace, &request.from_commit, &request.name) {
        Ok(result) => success(
            json!(result),
            started,
            "create_rust_workspace_git_worldline",
        )
        .into_response(),
        Err(error) => git_failure("git_worldline_create_failed", error),
    }
}

pub(crate) async fn git_worldline_rename(
    State(state): State<AppState>,
    Json(request): Json<GitWorldlineRenameRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &request.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    match StorydexGit.rename_worldline(&workspace, &request.name, &request.new_name) {
        Ok(result) => success(
            json!(result),
            started,
            "rename_rust_workspace_git_worldline",
        )
        .into_response(),
        Err(error) => git_failure("git_worldline_rename_failed", error),
    }
}

pub(crate) async fn git_worldline_delete(
    State(state): State<AppState>,
    Json(request): Json<GitBranchRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &request.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    match StorydexGit.delete_worldline(&workspace, &request.name) {
        Ok(result) => success(
            json!(result),
            started,
            "delete_rust_workspace_git_worldline",
        )
        .into_response(),
        Err(error) => git_failure("git_worldline_delete_failed", error),
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
                "worldlineBranch": result.worldline_branch,
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
        Ok(result) => success(
            json!({
                "restored": result.restored,
                "restoredCommit": result.restored_commit,
                "backupCommit": result.backup_commit,
                "backupRef": result.backup_ref,
                "summary": map_git_summary(result.summary),
            }),
            started,
            "restore_rust_workspace_git",
        )
        .into_response(),
        Err(error) => git_failure("git_restore_failed", error),
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
            match StorydexKnowledge::new(&workspace).and_then(|service| service.sync(false)) {
                Ok(value) => value,
                Err(error) => {
                    return error_response(
                        StatusCode::UNPROCESSABLE_ENTITY,
                        "wiki_projection_build_failed",
                        &error.to_string(),
                    );
                }
            }
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
    let mut data = payload.clone();
    merge_projection_status(&mut data, status.as_ref());
    let wiki = data.clone();
    if let Some(object) = data.as_object_mut() {
        object.insert("wiki".to_owned(), wiki);
        object.insert("index".to_owned(), index.clone().unwrap_or(Value::Null));
        object.insert(
            "statusSidecar".to_owned(),
            status.clone().unwrap_or(Value::Null),
        );
        object.insert("markdown".to_owned(), Value::String(markdown.clone()));
    }
    success(data, started, "read_rust_story_wiki").into_response()
}

pub(crate) async fn wiki_sync(
    State(state): State<AppState>,
    Query(query): Query<WorkspaceQuery>,
    body: Option<Json<WorkspaceQuery>>,
) -> Response {
    let request = body.map(|Json(value)| value).unwrap_or(query);
    wiki_sync_inner(state, request, false).await
}

pub(crate) async fn wiki_rebuild(
    State(state): State<AppState>,
    Query(query): Query<WorkspaceQuery>,
    body: Option<Json<WorkspaceQuery>>,
) -> Response {
    let request = body.map(|Json(value)| value).unwrap_or(query);
    wiki_sync_inner(state, request, true).await
}

async fn wiki_sync_inner(state: AppState, request: WorkspaceQuery, force: bool) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &request.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    match StorydexKnowledge::new(&workspace).and_then(|service| service.sync(force)) {
        Ok(payload) => success(
            payload,
            started,
            if force {
                "rebuild_rust_story_wiki"
            } else {
                "sync_rust_story_wiki"
            },
        )
        .into_response(),
        Err(error) => error_response(
            StatusCode::UNPROCESSABLE_ENTITY,
            "wiki_sync_failed",
            &error.to_string(),
        ),
    }
}

pub(crate) async fn wiki_graph(
    State(state): State<AppState>,
    Query(query): Query<WikiGraphQuery>,
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
    let mut payload = match read_json(&writer.wiki_root().join("knowledge_graph.json")) {
        Ok(Some(value)) => value,
        Ok(None) => {
            match StorydexKnowledge::new(&workspace).and_then(|service| service.sync(false)) {
                Ok(value) => value,
                Err(error) => {
                    return error_response(
                        StatusCode::UNPROCESSABLE_ENTITY,
                        "wiki_graph_build_failed",
                        &error.to_string(),
                    );
                }
            }
        }
        Err(error) => {
            return error_response(
                StatusCode::UNPROCESSABLE_ENTITY,
                "wiki_projection_invalid",
                &error.to_string(),
            );
        }
    };
    let status = match writer.read_status() {
        Ok(value) => value,
        Err(error) => {
            return error_response(
                StatusCode::UNPROCESSABLE_ENTITY,
                "wiki_status_invalid",
                &error.to_string(),
            );
        }
    };
    merge_projection_status(&mut payload, status.as_ref());
    let projection_status = payload
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or("ready");
    let projection_freshness = payload
        .get("projectionFreshness")
        .and_then(Value::as_str)
        .unwrap_or("fresh");
    if matches!(projection_status, "stale" | "error" | "rebuilding")
        || matches!(projection_freshness, "stale" | "error")
    {
        return success(json!({
            "mode": "unavailable",
            "queryStatus": "unavailable",
            "query": query.q,
            "category": normalize_wiki_category(&query.category),
            "entryId": query.entry_id,
            "nodeId": query.node_id,
            "depth": query.depth.clamp(1, 2),
            "limit": query.limit.clamp(1, 120),
            "offset": query.offset,
            "includeReview": query.include_review,
            "schemaVersion": payload.get("schemaVersion").cloned().unwrap_or(json!(3)),
            "knowledgeRevision": payload.get("knowledgeRevision").cloned().unwrap_or(json!(0)),
            "builtFromRevision": payload.get("builtFromRevision").cloned().unwrap_or(json!(0)),
            "lastSuccessfulRevision": payload.get("lastSuccessfulRevision").cloned().unwrap_or(json!(0)),
            "sourceSetChecksum": payload.get("sourceSetChecksum").cloned().unwrap_or(Value::Null),
            "graphChecksum": payload.get("graphChecksum").cloned().unwrap_or(Value::Null),
            "status": projection_status,
            "projectionFreshness": projection_freshness,
            "diagnostics": payload.get("diagnostics").cloned().unwrap_or_else(|| json!([])),
            "entries": [],
            "graph": {"nodes": [], "edges": []},
            "matchedEntryIds": [],
            "returnedNodeCount": 0,
            "hasMore": false,
            "nextOffset": Value::Null,
            "total": empty_graph_stats(),
            "graphStats": empty_graph_stats(),
            "pagination": {"offset": query.offset, "limit": query.limit.clamp(1, 120), "returnedNodeCount": 0, "hasMore": false, "nextOffset": Value::Null},
        }), started, "query_rust_story_wiki_graph_unavailable").into_response();
    }
    let raw_graph = payload
        .get("graph")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let raw_entries = payload
        .get("entries")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let raw_nodes = raw_graph
        .get("nodes")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .cloned()
        .collect::<Vec<_>>();
    let entry_by_id = raw_entries
        .iter()
        .filter_map(|entry| {
            entry
                .get("id")
                .and_then(Value::as_str)
                .filter(|id| !id.is_empty())
                .map(|id| (id.to_owned(), entry.clone()))
        })
        .collect::<HashMap<_, _>>();
    let node_by_id = raw_nodes
        .iter()
        .filter_map(|node| {
            node.get("id")
                .and_then(Value::as_str)
                .filter(|id| !id.is_empty())
                .map(|id| (id.to_owned(), node.clone()))
        })
        .collect::<HashMap<_, _>>();
    let content_edges = raw_graph
        .get("edges")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|edge| {
            let source = edge
                .get("source")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let target = edge
                .get("target")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let review_visible = query.include_review
                || (edge
                    .get("reviewStatus")
                    .and_then(Value::as_str)
                    .unwrap_or("confirmed")
                    != "review_required"
                    && !edge
                        .get("needsReview")
                        .and_then(Value::as_bool)
                        .unwrap_or(false));
            source != target
                && review_visible
                && node_by_id.contains_key(source)
                && node_by_id.contains_key(target)
                && !node_by_id.get(source).is_some_and(wiki_hub_node)
                && !node_by_id.get(target).is_some_and(wiki_hub_node)
        })
        .cloned()
        .collect::<Vec<_>>();

    let query_text = query.q.trim();
    let entry_id = query.entry_id.trim();
    let node_id = query.node_id.trim();
    let depth = query.depth.clamp(1, 2);
    let limit = query.limit.clamp(1, 120);
    let offset = query.offset;
    let mut category = normalize_wiki_category(&query.category);
    let mode;
    let mut matched_entry_ids = Vec::<String>::new();
    let mut selected_node_ids = HashSet::<String>::new();

    if !node_id.is_empty() {
        mode = "node";
        if let Some(node) = node_by_id.get(node_id).filter(|node| !wiki_hub_node(node)) {
            selected_node_ids.insert(node_id.to_owned());
            if let Some(entry_ref) = node.get("entryId").and_then(Value::as_str)
                && entry_by_id.contains_key(entry_ref)
            {
                matched_entry_ids.push(entry_ref.to_owned());
            }
        }
    } else if !entry_id.is_empty() {
        mode = "entry";
        if entry_by_id.contains_key(entry_id) {
            matched_entry_ids.push(entry_id.to_owned());
            selected_node_ids.extend(raw_nodes.iter().filter_map(|node| {
                (node.get("entryId").and_then(Value::as_str) == Some(entry_id)
                    && !wiki_hub_node(node))
                .then(|| node.get("id").and_then(Value::as_str).map(str::to_owned))
                .flatten()
            }));
        }
    } else if !query_text.is_empty() {
        mode = "search";
        let tokens = query_text
            .split_whitespace()
            .map(|token| token.to_lowercase())
            .collect::<Vec<_>>();
        matched_entry_ids.extend(raw_entries.iter().filter_map(|entry| {
            wiki_value_matches(
                entry,
                &[
                    "id",
                    "title",
                    "category",
                    "categoryLabel",
                    "summary",
                    "details",
                    "sourcePaths",
                ],
                &tokens,
            )
            .then(|| entry.get("id").and_then(Value::as_str).map(str::to_owned))
            .flatten()
        }));
        let matched_entry_set = matched_entry_ids
            .iter()
            .map(String::as_str)
            .collect::<HashSet<_>>();
        selected_node_ids.extend(raw_nodes.iter().filter_map(|node| {
            let entry_match = node
                .get("entryId")
                .and_then(Value::as_str)
                .is_some_and(|value| matched_entry_set.contains(value));
            (entry_match
                || wiki_value_matches(
                    node,
                    &["id", "label", "type", "category", "entryId", "summary"],
                    &tokens,
                ))
            .then(|| node.get("id").and_then(Value::as_str).map(str::to_owned))
            .flatten()
        }));
        for edge in &content_edges {
            if wiki_value_matches(
                edge,
                &["source", "target", "label", "type", "evidence"],
                &tokens,
            ) {
                if let Some(source) = edge.get("source").and_then(Value::as_str) {
                    selected_node_ids.insert(source.to_owned());
                }
                if let Some(target) = edge.get("target").and_then(Value::as_str) {
                    selected_node_ids.insert(target.to_owned());
                }
            }
        }
    } else {
        mode = "category";
        if category.is_empty() || category == "overview" {
            category = "characters".to_owned();
        }
        matched_entry_ids.extend(raw_entries.iter().filter_map(|entry| {
            (normalize_wiki_category(
                entry
                    .get("category")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            ) == category)
                .then(|| entry.get("id").and_then(Value::as_str).map(str::to_owned))
                .flatten()
        }));
        selected_node_ids.extend(raw_nodes.iter().filter_map(|node| {
            let entry_category = node
                .get("entryId")
                .and_then(Value::as_str)
                .and_then(|entry_ref| entry_by_id.get(entry_ref))
                .and_then(|entry| entry.get("category"))
                .and_then(Value::as_str)
                .map(normalize_wiki_category)
                .unwrap_or_default();
            let node_category = normalize_wiki_category(
                node.get("category")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            );
            ((node_category == category || entry_category == category) && !wiki_hub_node(node))
                .then(|| node.get("id").and_then(Value::as_str).map(str::to_owned))
                .flatten()
        }));
    }

    if matches!(mode, "search" | "entry" | "node") {
        expand_wiki_neighborhood(&mut selected_node_ids, &node_by_id, &content_edges, depth);
    }
    let all_nodes = raw_nodes
        .iter()
        .filter(|node| {
            node.get("id")
                .and_then(Value::as_str)
                .is_some_and(|id| selected_node_ids.contains(id))
        })
        .cloned()
        .collect::<Vec<_>>();
    let all_node_ids = all_nodes
        .iter()
        .filter_map(|node| node.get("id").and_then(Value::as_str).map(str::to_owned))
        .collect::<HashSet<_>>();
    let all_edges = content_edges
        .iter()
        .filter(|edge| {
            let source = edge
                .get("source")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let target = edge
                .get("target")
                .and_then(Value::as_str)
                .unwrap_or_default();
            all_node_ids.contains(source) && all_node_ids.contains(target)
        })
        .cloned()
        .collect::<Vec<_>>();
    let nodes = all_nodes
        .iter()
        .skip(offset)
        .take(limit)
        .cloned()
        .collect::<Vec<_>>();
    let visible_node_ids = nodes
        .iter()
        .filter_map(|node| node.get("id").and_then(Value::as_str).map(str::to_owned))
        .collect::<HashSet<_>>();
    let edges = all_edges
        .iter()
        .filter(|edge| {
            let source = edge
                .get("source")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let target = edge
                .get("target")
                .and_then(Value::as_str)
                .unwrap_or_default();
            visible_node_ids.contains(source) && visible_node_ids.contains(target)
        })
        .take(limit.saturating_mul(2))
        .cloned()
        .collect::<Vec<_>>();
    let mut relevant_entry_ids = matched_entry_ids.iter().cloned().collect::<HashSet<_>>();
    relevant_entry_ids.extend(all_nodes.iter().filter_map(|node| {
        node.get("entryId")
            .and_then(Value::as_str)
            .filter(|entry_ref| entry_by_id.contains_key(*entry_ref))
            .map(str::to_owned)
    }));
    let visible_matched_entry_ids = matched_entry_ids
        .iter()
        .skip(offset)
        .take(limit)
        .cloned()
        .collect::<Vec<_>>();
    let mut visible_entry_ids = visible_matched_entry_ids
        .iter()
        .cloned()
        .collect::<HashSet<_>>();
    visible_entry_ids.extend(nodes.iter().filter_map(|node| {
        node.get("entryId")
            .and_then(Value::as_str)
            .filter(|entry_ref| entry_by_id.contains_key(*entry_ref))
            .map(str::to_owned)
    }));
    let entries = raw_entries
        .iter()
        .filter(|entry| {
            entry
                .get("id")
                .and_then(Value::as_str)
                .is_some_and(|id| visible_entry_ids.contains(id))
        })
        .cloned()
        .collect::<Vec<_>>();
    let total = wiki_graph_stats(&all_nodes, &all_edges, relevant_entry_ids.len());
    let returned_node_count = nodes.len();
    let has_more = offset.saturating_add(returned_node_count) < all_nodes.len();
    let next_offset = has_more.then_some(offset.saturating_add(returned_node_count));
    success(json!({
        "mode": mode,
        "query": query.q,
        "category": category,
        "entryId": query.entry_id,
        "nodeId": query.node_id,
        "depth": depth,
        "limit": limit,
        "offset": offset,
        "includeReview": query.include_review,
        "schemaVersion": payload.get("schemaVersion").cloned().unwrap_or(json!(3)),
        "knowledgeRevision": payload.get("knowledgeRevision").cloned().unwrap_or(json!(0)),
        "builtFromRevision": payload.get("builtFromRevision").cloned().unwrap_or(json!(0)),
        "lastSuccessfulRevision": payload.get("lastSuccessfulRevision").cloned().unwrap_or(json!(0)),
        "sourceSetChecksum": payload.get("sourceSetChecksum").cloned().unwrap_or(Value::Null),
        "graphChecksum": payload.get("graphChecksum").cloned().unwrap_or(Value::Null),
        "status": payload.get("status").cloned().unwrap_or(json!("ready")),
        "projectionFreshness": payload.get("projectionFreshness").cloned().unwrap_or(json!("fresh")),
        "diagnostics": payload.get("diagnostics").cloned().unwrap_or_else(|| json!([])),
        "sourceStats": payload.get("sourceStats").cloned().unwrap_or_else(|| json!({})),
        "entries": entries,
        "graph": {"nodes": nodes, "edges": edges},
        "matchedEntryIds": visible_matched_entry_ids,
        "returnedNodeCount": returned_node_count,
        "hasMore": has_more,
        "nextOffset": next_offset,
        "total": total,
        "graphStats": total,
        "pagination": {"offset": offset, "limit": limit, "returnedNodeCount": returned_node_count, "hasMore": has_more, "nextOffset": next_offset},
    }), started, "query_rust_story_wiki_graph").into_response()
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
    let change_set = request.change_set;
    match writer.write(&ProjectionBundle {
        payload: request.payload,
        markdown: request.markdown,
        index: request.index,
        status,
        source_snapshot: request.source_snapshot,
        change_set: change_set.clone(),
    }) {
        Ok(result) => success(
            json!({
                "ok": true,
                "changedPaths": result.changed_paths,
                "noChanges": result.changed_paths.is_empty(),
                "graphChecksum": checksum,
                "changeSet": change_set,
                "event": if result.changed_paths.is_empty() { "KnowledgeProjectionSynchronized" } else { "KnowledgeProjectionUpdated" },
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

fn merge_projection_status(payload: &mut Value, status: Option<&Value>) {
    let Some(object) = payload.as_object_mut() else {
        return;
    };
    let Some(status) = status.and_then(Value::as_object) else {
        object
            .entry("projectionFreshness".to_owned())
            .or_insert_with(|| json!("fresh"));
        return;
    };
    for key in [
        "status",
        "diagnostics",
        "knowledgeRevision",
        "builtFromRevision",
        "lastSuccessfulRevision",
        "sourceSetChecksum",
        "graphChecksum",
        "projectionFreshness",
        "attemptedSourceSetChecksum",
        "catalogGeneration",
        "catalogRevision",
    ] {
        if let Some(value) = status.get(key) {
            object.insert(key.to_owned(), value.clone());
        }
    }
}

fn normalize_wiki_category(value: &str) -> String {
    match value.trim().to_ascii_lowercase().as_str() {
        "chapters" | "events" | "timeline" | "plot" => "plot".to_owned(),
        "world" | "locations" | "items" | "factions" | "foreshadow" | "setting" => {
            "setting".to_owned()
        }
        "relationships" | "characters" => "characters".to_owned(),
        "overview" | "index" => "overview".to_owned(),
        other => other.to_owned(),
    }
}

fn wiki_value_matches(value: &Value, keys: &[&str], tokens: &[String]) -> bool {
    if tokens.is_empty() {
        return false;
    }
    let Some(object) = value.as_object() else {
        return false;
    };
    let mut fields = Vec::<String>::new();
    for key in keys {
        match object.get(*key) {
            Some(Value::String(text)) => fields.push(text.clone()),
            Some(Value::Array(values)) => fields.extend(values.iter().filter_map(|item| {
                item.as_str()
                    .map(str::to_owned)
                    .or_else(|| item.is_object().then(|| item.to_string()))
            })),
            Some(Value::Number(number)) => fields.push(number.to_string()),
            Some(Value::Bool(boolean)) => fields.push(boolean.to_string()),
            _ => {}
        }
    }
    let haystack = fields.join(" ").to_lowercase();
    tokens.iter().all(|token| haystack.contains(token))
}

fn wiki_hub_node(node: &Value) -> bool {
    node.get("id").and_then(Value::as_str) == Some("project:root")
        || matches!(
            node.get("category").and_then(Value::as_str),
            Some("overview" | "index")
        )
        || node.get("type").and_then(Value::as_str) == Some("project")
        || matches!(
            node.get("role").and_then(Value::as_str),
            Some("projectHub" | "categoryHub")
        )
}

fn expand_wiki_neighborhood(
    selected: &mut HashSet<String>,
    node_by_id: &HashMap<String, Value>,
    edges: &[Value],
    depth: usize,
) {
    let mut frontier = selected.clone();
    for _ in 0..depth.clamp(1, 2) {
        let mut next = HashSet::<String>::new();
        for edge in edges {
            let source = edge
                .get("source")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let target = edge
                .get("target")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if frontier.contains(source)
                && node_by_id.contains_key(target)
                && !selected.contains(target)
            {
                next.insert(target.to_owned());
            }
            if frontier.contains(target)
                && node_by_id.contains_key(source)
                && !selected.contains(source)
            {
                next.insert(source.to_owned());
            }
        }
        if next.is_empty() {
            break;
        }
        selected.extend(next.iter().cloned());
        frontier = next;
    }
}

fn empty_graph_stats() -> Value {
    json!({
        "entryCount": 0,
        "nodeCount": 0,
        "edgeCount": 0,
        "confirmedEdgeCount": 0,
        "reviewRequiredEdgeCount": 0,
        "connectedNodeCount": 0,
        "isolatedNodeCount": 0,
    })
}

fn wiki_graph_stats(nodes: &[Value], edges: &[Value], entry_count: usize) -> Value {
    let node_ids = nodes
        .iter()
        .filter_map(|node| node.get("id").and_then(Value::as_str).map(str::to_owned))
        .collect::<HashSet<_>>();
    let semantic_edges = edges
        .iter()
        .filter(|edge| {
            matches!(
                edge.get("type")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_ascii_lowercase()
                    .as_str(),
                "relationship" | "fact"
            )
        })
        .collect::<Vec<_>>();
    let confirmed_edges = semantic_edges
        .iter()
        .filter(|edge| {
            edge.get("reviewStatus")
                .and_then(Value::as_str)
                .unwrap_or("confirmed")
                == "confirmed"
                && !edge
                    .get("needsReview")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
        })
        .collect::<Vec<_>>();
    let connected = confirmed_edges
        .iter()
        .flat_map(|edge| {
            [
                edge.get("source").and_then(Value::as_str),
                edge.get("target").and_then(Value::as_str),
            ]
        })
        .flatten()
        .filter(|id| node_ids.contains(*id))
        .map(str::to_owned)
        .collect::<HashSet<_>>();
    json!({
        "entryCount": entry_count,
        "nodeCount": node_ids.len(),
        "edgeCount": semantic_edges.len(),
        "confirmedEdgeCount": confirmed_edges.len(),
        "reviewRequiredEdgeCount": semantic_edges.iter().filter(|edge| {
            edge.get("reviewStatus").and_then(Value::as_str) == Some("review_required")
                || edge.get("needsReview").and_then(Value::as_bool).unwrap_or(false)
        }).count(),
        "connectedNodeCount": connected.len(),
        "isolatedNodeCount": node_ids.len().saturating_sub(connected.len()),
    })
}

fn read_json(path: &Path) -> anyhow::Result<Option<Value>> {
    match fs::read(path) {
        Ok(bytes) => Ok(Some(serde_json::from_slice(&bytes)?)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

use axum::response::IntoResponse;
