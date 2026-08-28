use crate::ApiEnvelope;
use crate::AppState;
use crate::error_response;
use crate::execution::{ExecutionCancellation, ExecutionControl};
use crate::replacement::{
    ExecutionRecordInput, ReplacementError, ReplacementTransaction, change_ledger_from_events,
    persist_execution_record_with_events,
};
use crate::workspace;
use anyhow::Context;
use axum::Json;
use axum::body::Body;
use axum::extract::rejection::JsonRejection;
use axum::extract::{Path as AxumPath, Query, State};
use axum::http::HeaderMap;
use axum::http::Response;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use chrono::Utc;
use coomi_services::ProviderRegistry;
use futures_util::stream::unfold;
use serde::Deserialize;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::convert::Infallible;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;
use std::time::Instant;
use tokio::io::AsyncBufReadExt;
use tokio::io::AsyncWriteExt;
use tokio::io::BufReader;
use tokio::process::Command;
use tokio::sync::mpsc;
use uuid::Uuid;

const MAX_PROMPT_CHARS: usize = 12_000;
const MAX_EXECUTION_TIMEOUT_MS: u64 = 600_000;
const BRIDGE_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(3);
const CONTROL_PLANE_PREFLIGHT_GRACE: Duration = Duration::from_millis(50);

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ChatStreamRequest {
    #[serde(default)]
    pub prompt: String,
    #[serde(default)]
    pub active_file: String,
    #[serde(default)]
    pub workspace_root: String,
    #[serde(default = "default_reasoning_effort")]
    pub reasoning_effort: String,
    #[serde(default = "default_story_generation")]
    pub story_generation: Value,
    #[serde(default)]
    pub confirm_no_snapshot: bool,
    #[serde(default)]
    pub replace_latest_trace_id: String,
    #[serde(default)]
    pub source_followup_message_id: String,
    #[serde(default)]
    pub source_followup_expected_trace_id: String,
    #[serde(default)]
    pub timeout_ms: u64,
    #[serde(default = "default_permission_mode")]
    pub permission_mode: String,
    #[serde(default = "default_capability_mode")]
    pub capability_mode: String,
    #[serde(default)]
    pub writes_allowed: bool,
    #[serde(default)]
    pub core_writes_allowed: Option<bool>,
    #[serde(default)]
    pub allowed_write_roots: Vec<String>,
    #[serde(skip)]
    pub compiled_preset: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ExecutionStopRequest {
    #[serde(default = "default_session_id")]
    session_id: String,
    #[serde(default)]
    expected_trace_id: String,
    #[serde(default)]
    workspace_root: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ApprovalRequest {
    #[serde(default)]
    approval_id: String,
    #[serde(default)]
    decision: String,
    #[serde(default)]
    response: Value,
    #[serde(default = "default_session_id")]
    session_id: String,
    #[serde(default)]
    expected_trace_id: String,
    #[serde(default)]
    workspace_root: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FollowupRequest {
    #[serde(default)]
    message_id: String,
    #[serde(default = "default_session_id")]
    session_id: String,
    #[serde(default)]
    active_trace_id: String,
    #[serde(default)]
    expected_trace_id: String,
    #[serde(default)]
    workspace_root: String,
    #[serde(default)]
    content: String,
    #[serde(default = "default_followup_mode")]
    mode: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FollowupActionRequest {
    #[serde(default = "default_session_id")]
    session_id: String,
    #[serde(default)]
    expected_trace_id: String,
    #[serde(default)]
    workspace_root: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FollowupUpdateRequest {
    #[serde(default = "default_session_id")]
    session_id: String,
    #[serde(default)]
    expected_trace_id: String,
    #[serde(default)]
    workspace_root: String,
    #[serde(default)]
    content: Option<String>,
    #[serde(default)]
    mode: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FollowupQuery {
    #[serde(default = "default_session_id")]
    session_id: String,
    #[serde(default)]
    workspace_root: String,
}

#[derive(Debug, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ChatQuery {
    #[serde(default)]
    session_id: String,
}

fn default_reasoning_effort() -> String {
    "high".to_owned()
}

fn default_story_generation() -> Value {
    json!({})
}

fn default_session_id() -> String {
    "default".to_owned()
}

fn default_permission_mode() -> String {
    "ask_approval".to_owned()
}

fn default_capability_mode() -> String {
    String::new()
}

fn default_followup_mode() -> String {
    "queued".to_owned()
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct StoryGenerationOptions {
    pub(crate) fragment_count: u64,
    pub(crate) chapter_length_tier: String,
    pub(crate) chapter_template_id: String,
}

#[derive(Clone, Debug)]
pub(crate) struct ProviderIdentity {
    pub(crate) id: String,
    pub(crate) model: String,
    pub(crate) display: String,
}

struct ChatExecution {
    state: AppState,
    payload: ChatStreamRequest,
    workspace: PathBuf,
    trace_id: String,
    session_id: String,
    sender: mpsc::Sender<String>,
    cancellation: ExecutionCancellation,
    control_receiver: mpsc::Receiver<ExecutionControl>,
    replacement: Option<ReplacementTransaction>,
}

pub async fn chat_stream(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<ChatQuery>,
    payload: Result<Json<ChatStreamRequest>, JsonRejection>,
) -> Response<Body> {
    let Json(mut payload) = match payload {
        Ok(payload) => payload,
        Err(_) => {
            return error_response(
                StatusCode::BAD_REQUEST,
                "invalid_request",
                "Agent request body must be valid JSON.",
            )
            .into_response();
        }
    };
    let trace_id = header_value(&headers, "x-trace-id")
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    let session_id = resolve_chat_session_id(&headers, &query);
    if payload.prompt.trim().is_empty() {
        return error_response(
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid_request",
            "Agent prompt must not be empty.",
        )
        .into_response();
    }
    if payload.prompt.chars().count() > MAX_PROMPT_CHARS {
        return error_response(
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid_request",
            "Agent prompt exceeds the 12000 character limit.",
        )
        .into_response();
    }
    let workspace = match resolve_workspace(&state, &payload.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    if let Err(error) =
        crate::agent_control::apply_chat_policy(&state, &workspace, &session_id, &mut payload)
    {
        return error_response(error.status, error.code, &error.message).into_response();
    }
    payload.compiled_preset = match crate::presets::compile_active_for_agent(&workspace) {
        Ok(value) => value,
        Err(error) => {
            return error_response(StatusCode::UNPROCESSABLE_ENTITY, error.code, &error.message)
                .into_response();
        }
    };
    if let Err((code, message)) = validate_refactor_request(&payload, &workspace) {
        return error_response(StatusCode::UNPROCESSABLE_ENTITY, code, message).into_response();
    }
    if !payload.replace_latest_trace_id.trim().is_empty()
        && !payload.source_followup_message_id.trim().is_empty()
    {
        return error_response(
            StatusCode::CONFLICT,
            "invalid_followup_transition",
            "A replacement request cannot also dispatch a queued follow-up.",
        )
        .into_response();
    }

    let (sender, receiver) = mpsc::channel::<String>(32);
    let (control_sender, control_receiver) = mpsc::channel::<ExecutionControl>(32);
    let cancellation = ExecutionCancellation::default();
    let execution_guard = match state.execution_registry().register(
        trace_id.clone(),
        session_id.clone(),
        workspace.clone(),
        cancellation.clone(),
        control_sender,
    ) {
        Ok(guard) => guard,
        Err(active) => {
            tracing::warn!(
                active_trace_id = %active.active_trace_id,
                active_session_id = %active.active_session_id,
                "Refactor Agent rejected a concurrent execution"
            );
            return error_response(
                StatusCode::CONFLICT,
                "agent_busy",
                "Another Storydex Agent execution is already active.",
            )
            .into_response();
        }
    };
    let replacement = if payload.replace_latest_trace_id.trim().is_empty() {
        None
    } else {
        let runtime_session_id = match load_runtime_session_id(&state, &workspace, &session_id) {
            Ok(runtime_session_id) => runtime_session_id,
            Err(error) => {
                return error_response(
                    StatusCode::CONFLICT,
                    "replacement_context_unavailable",
                    &format!("Unable to load the replacement session: {error:#}"),
                )
                .into_response();
            }
        };
        match ReplacementTransaction::prepare(
            &workspace,
            state.coomi_home(),
            &session_id,
            &payload.replace_latest_trace_id,
            &trace_id,
            &payload.prompt,
            runtime_session_id,
        ) {
            Ok(transaction) => Some(transaction),
            Err(error) => return replacement_error_response(error),
        }
    };
    if !payload.source_followup_message_id.trim().is_empty() {
        let mailbox = match state.followup_store().list(&workspace, &session_id) {
            Ok(mailbox) => mailbox,
            Err(error) => {
                if let Some(mut replacement) = replacement {
                    let _ = replacement.restore("followup_claim_failed");
                }
                return followup_error_response(error);
            }
        };
        let claimed = match state.followup_store().claim(
            &workspace,
            &session_id,
            &payload.source_followup_message_id,
            &mailbox.last_trace_id,
            &trace_id,
            &payload.source_followup_expected_trace_id,
        ) {
            Ok(message) => message,
            Err(error) => {
                if let Some(mut replacement) = replacement {
                    let _ = replacement.restore("followup_claim_failed");
                }
                return followup_error_response(error);
            }
        };
        payload.prompt = claimed.content;
    }
    if let Err(error) = state
        .followup_store()
        .set_active(&workspace, &session_id, &trace_id)
    {
        if let Some(mut replacement) = replacement {
            let _ = replacement.restore("followup_activation_failed");
        }
        return followup_error_response(error);
    }
    let task_guard = state.task_registry().begin();
    let worker_state = state.clone();
    let worker_trace = trace_id.clone();
    let worker_session = session_id.clone();
    let worker_cancellation = cancellation.clone();
    let worker_control_receiver = control_receiver;
    let worker_replacement = replacement;
    tokio::spawn(async move {
        let _task_guard = task_guard;
        let _execution_guard = execution_guard;
        run_chat_with_timeout(ChatExecution {
            state: worker_state,
            payload,
            workspace,
            trace_id: worker_trace,
            session_id: worker_session,
            sender,
            cancellation: worker_cancellation,
            control_receiver: worker_control_receiver,
            replacement: worker_replacement,
        })
        .await;
    });

    let stream = unfold(receiver, |mut receiver| async move {
        receiver
            .recv()
            .await
            .map(|frame| (Ok::<String, Infallible>(frame), receiver))
    });
    match Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "text/event-stream; charset=utf-8")
        .header("cache-control", "no-cache")
        .header("connection", "keep-alive")
        .header("x-accel-buffering", "no")
        .body(Body::from_stream(stream))
    {
        Ok(response) => response,
        Err(_) => error_response(
            StatusCode::INTERNAL_SERVER_ERROR,
            "internal_error",
            "Unable to construct Agent SSE response.",
        )
        .into_response(),
    }
}

fn resolve_chat_session_id(headers: &HeaderMap, query: &ChatQuery) -> String {
    header_value(headers, "x-session-id")
        .filter(|value| !value.is_empty())
        .or_else(|| {
            let value = query.session_id.trim();
            (!value.is_empty()).then_some(value.to_owned())
        })
        .unwrap_or_else(|| "default".to_owned())
}

pub async fn stop_execution(
    State(state): State<AppState>,
    payload: Result<Json<ExecutionStopRequest>, JsonRejection>,
) -> Response<Body> {
    let started_at = Instant::now();
    let Json(payload) = match payload {
        Ok(payload) => payload,
        Err(_) => {
            return error_response(
                StatusCode::BAD_REQUEST,
                "invalid_request",
                "Execution stop request body must be valid JSON.",
            )
            .into_response();
        }
    };
    let workspace = match resolve_workspace(&state, &payload.workspace_root) {
        Ok(path) => Some(path),
        Err(response) => return response,
    };
    let result = state.execution_registry().cancel(
        &payload.session_id,
        &payload.expected_trace_id,
        workspace.as_deref(),
        "manual_stop",
    );
    if result.reason == "stale_trace" {
        return error_response(
            StatusCode::CONFLICT,
            "stale_trace",
            "The active execution changed before the stop request was applied.",
        )
        .into_response();
    }
    let mut data = serde_json::to_value(&result).unwrap_or_else(|_| json!({}));
    if let Some(data) = data.as_object_mut() {
        data.insert("mailboxPaused".into(), Value::Bool(true));
        data.insert("pauseReason".into(), Value::String("manual_stop".into()));
    }
    Json(
        ApiEnvelope::success(data, started_at).with_audit(vec![json!({
            "action": "stop_agent_execution",
            "sessionId": result.session_id,
            "activeTraceId": result.active_trace_id,
        })]),
    )
    .into_response()
}

pub async fn resolve_approval(
    State(state): State<AppState>,
    payload: Result<Json<ApprovalRequest>, JsonRejection>,
) -> Response<Body> {
    let started_at = Instant::now();
    let Json(payload) = match payload {
        Ok(payload) => payload,
        Err(_) => {
            return error_response(
                StatusCode::BAD_REQUEST,
                "invalid_request",
                "Approval request body must be valid JSON.",
            )
            .into_response();
        }
    };
    let workspace = match resolve_workspace(&state, &payload.workspace_root) {
        Ok(path) => Some(path),
        Err(response) => return response,
    };
    let value = approval_control_value(&payload.decision, &payload.response);
    let result = state.execution_registry().resolve(
        &payload.approval_id,
        &payload.session_id,
        &payload.expected_trace_id,
        workspace.as_deref(),
        value,
    );
    if result.reason == "stale_trace" {
        return error_response(
            StatusCode::CONFLICT,
            "stale_trace",
            "The active execution changed before the approval was applied.",
        )
        .into_response();
    }
    if result.reason == "invalid_request_id" {
        return error_response(
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid_request",
            "approvalId is required.",
        )
        .into_response();
    }
    let data = serde_json::to_value(&result).unwrap_or_else(|_| json!({}));
    Json(
        ApiEnvelope::success(data, started_at).with_audit(vec![json!({
            "action": "resolve_coomi_approval",
            "approvalId": result.request_id,
            "decision": payload.decision,
            "accepted": result.accepted,
        })]),
    )
    .into_response()
}

fn approval_control_value(decision: &str, response: &Value) -> Value {
    if let Some(object) = response.as_object() {
        if let Some(answers) = object.get("answers") {
            return json!({"answers": answers});
        }
        if let Some(approved) = object.get("approved").and_then(Value::as_bool) {
            return json!({"approved": approved});
        }
    }
    let normalized = decision.trim().to_ascii_lowercase();
    match normalized.as_str() {
        "allow" | "approve" | "approved" | "yes" | "y" => json!({"approved": true}),
        "deny" | "denied" | "no" | "n" | "cancel" => json!({"approved": false}),
        _ => json!({"approved": false}),
    }
}

pub async fn list_followups(
    State(state): State<AppState>,
    Query(query): Query<FollowupQuery>,
) -> Response<Body> {
    let started_at = Instant::now();
    let workspace = match resolve_workspace(&state, &query.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    match state.followup_store().list(&workspace, &query.session_id) {
        Ok(mailbox) => Json(
            ApiEnvelope::success(mailbox, started_at).with_audit(vec![json!({
                "action": "read_agent_followups",
                "sessionId": query.session_id,
            })]),
        )
        .into_response(),
        Err(error) => followup_error_response(error),
    }
}

pub async fn enqueue_followup(
    State(state): State<AppState>,
    payload: Result<Json<FollowupRequest>, JsonRejection>,
) -> Response<Body> {
    let started_at = Instant::now();
    let Json(payload) = match payload {
        Ok(payload) => payload,
        Err(_) => {
            return error_response(
                StatusCode::BAD_REQUEST,
                "invalid_request",
                "Follow-up request body must be valid JSON.",
            )
            .into_response();
        }
    };
    let workspace = match resolve_workspace(&state, &payload.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    let expected_trace = if payload.expected_trace_id.trim().is_empty() {
        payload.active_trace_id.as_str()
    } else {
        payload.expected_trace_id.as_str()
    };
    let message = match state.followup_store().enqueue(
        &workspace,
        &payload.session_id,
        &payload.message_id,
        &payload.content,
        &payload.mode,
        expected_trace,
    ) {
        Ok(message) => message,
        Err(error) => return followup_error_response(error),
    };
    let mut steer_requested = false;
    if message.mode == "steer" {
        steer_requested = state
            .execution_registry()
            .cancel(
                &payload.session_id,
                expected_trace,
                Some(&workspace),
                "steer",
            )
            .accepted;
    }
    Json(
        ApiEnvelope::success(
            json!({"message": message, "steerRequested": steer_requested}),
            started_at,
        )
        .with_audit(vec![json!({
            "action": "enqueue_agent_followup",
            "messageId": payload.message_id,
            "sessionId": payload.session_id,
            "steerRequested": steer_requested,
        })]),
    )
    .into_response()
}

pub async fn update_followup(
    State(state): State<AppState>,
    AxumPath(message_id): AxumPath<String>,
    payload: Result<Json<FollowupUpdateRequest>, JsonRejection>,
) -> Response<Body> {
    let started_at = Instant::now();
    let Json(payload) = match payload {
        Ok(payload) => payload,
        Err(_) => {
            return error_response(
                StatusCode::BAD_REQUEST,
                "invalid_request",
                "Follow-up update body must be valid JSON.",
            )
            .into_response();
        }
    };
    let workspace = match resolve_workspace(&state, &payload.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    let message = match state.followup_store().update(
        &workspace,
        &payload.session_id,
        &message_id,
        payload.content.as_deref(),
        payload.mode.as_deref(),
        &payload.expected_trace_id,
    ) {
        Ok(message) => message,
        Err(error) => return followup_error_response(error),
    };
    let mut steer_requested = false;
    if message.mode == "steer" {
        steer_requested = state
            .execution_registry()
            .cancel(
                &payload.session_id,
                &payload.expected_trace_id,
                Some(&workspace),
                "steer",
            )
            .accepted;
    }
    Json(
        ApiEnvelope::success(
            json!({"message": message, "steerRequested": steer_requested}),
            started_at,
        )
        .with_audit(vec![json!({
            "action": "update_agent_followup",
            "messageId": message_id,
            "status": message.status,
        })]),
    )
    .into_response()
}

pub async fn delete_followup(
    State(state): State<AppState>,
    AxumPath(message_id): AxumPath<String>,
    Query(query): Query<FollowupQuery>,
) -> Response<Body> {
    let started_at = Instant::now();
    let workspace = match resolve_workspace(&state, &query.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    let message =
        match state
            .followup_store()
            .cancel_message(&workspace, &query.session_id, &message_id)
        {
            Ok(message) => message,
            Err(error) => return followup_error_response(error),
        };
    Json(
        ApiEnvelope::success(json!({"message": message}), started_at).with_audit(vec![json!({
            "action": "delete_agent_followup",
            "messageId": message_id,
            "status": message.status,
        })]),
    )
    .into_response()
}

pub async fn steer_followup(
    State(state): State<AppState>,
    AxumPath(message_id): AxumPath<String>,
    payload: Result<Json<FollowupActionRequest>, JsonRejection>,
) -> Response<Body> {
    let started_at = Instant::now();
    let Json(payload) = match payload {
        Ok(payload) => payload,
        Err(_) => {
            return error_response(
                StatusCode::BAD_REQUEST,
                "invalid_request",
                "Follow-up action body must be valid JSON.",
            )
            .into_response();
        }
    };
    let workspace = match resolve_workspace(&state, &payload.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    let message = match state.followup_store().request_steer(
        &workspace,
        &payload.session_id,
        &message_id,
        &payload.expected_trace_id,
    ) {
        Ok(message) => message,
        Err(error) => return followup_error_response(error),
    };
    let result = state.execution_registry().cancel(
        &payload.session_id,
        &payload.expected_trace_id,
        Some(&workspace),
        "steer",
    );
    Json(
        ApiEnvelope::success(
            json!({"message": message, "steerRequested": result.accepted}),
            started_at,
        )
        .with_audit(vec![json!({
            "action": "steer_agent_followup",
            "messageId": message_id,
            "activeTraceId": result.active_trace_id,
        })]),
    )
    .into_response()
}

pub async fn resume_followups(
    State(state): State<AppState>,
    payload: Result<Json<FollowupActionRequest>, JsonRejection>,
) -> Response<Body> {
    let started_at = Instant::now();
    let Json(payload) = match payload {
        Ok(payload) => payload,
        Err(_) => {
            return error_response(
                StatusCode::BAD_REQUEST,
                "invalid_request",
                "Follow-up action body must be valid JSON.",
            )
            .into_response();
        }
    };
    let workspace = match resolve_workspace(&state, &payload.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    match state
        .followup_store()
        .resume(&workspace, &payload.session_id)
    {
        Ok(mailbox) => Json(
            ApiEnvelope::success(mailbox, started_at).with_audit(vec![json!({
                "action": "resume_agent_followups",
                "sessionId": payload.session_id,
            })]),
        )
        .into_response(),
        Err(error) => followup_error_response(error),
    }
}

fn followup_error_response(error: crate::followup::FollowupError) -> Response<Body> {
    let status = match error.code {
        "followup_not_found" => StatusCode::NOT_FOUND,
        "stale_trace"
        | "no_active_execution"
        | "message_id_conflict"
        | "invalid_followup_transition"
        | "followup_not_editable"
        | "followup_mailbox_paused" => StatusCode::CONFLICT,
        "corrupt_followup_mailbox" | "followup_storage_error" => StatusCode::INTERNAL_SERVER_ERROR,
        _ => StatusCode::UNPROCESSABLE_ENTITY,
    };
    error_response(status, error.code, &error.message).into_response()
}

fn replacement_error_response(error: ReplacementError) -> Response<Body> {
    let status = match error.code {
        "replacement_target_missing"
        | "stale_trace"
        | "replacement_target_running"
        | "replacement_workspace_mismatch"
        | "replacement_context_unavailable" => StatusCode::CONFLICT,
        _ => StatusCode::UNPROCESSABLE_ENTITY,
    };
    error_response(status, error.code, &error.message).into_response()
}

async fn run_chat_with_timeout(execution: ChatExecution) {
    let timeout_ms = execution.payload.timeout_ms;
    let followup_message_id = execution
        .payload
        .source_followup_message_id
        .trim()
        .to_owned();
    let lifecycle_state = execution.state.clone();
    let lifecycle_workspace = execution.workspace.clone();
    let lifecycle_trace = execution.trace_id.clone();
    let lifecycle_session = execution.session_id.clone();
    let cancellation = execution.cancellation.clone();
    let run = run_chat(execution);
    if timeout_ms == 0 {
        run.await;
    } else {
        tokio::pin!(run);
        tokio::select! {
            () = &mut run => {}
            () = tokio::time::sleep(Duration::from_millis(timeout_ms)) => {
                cancellation.cancel("timeout");
                run.await;
            }
        }
    }
    if !followup_message_id.is_empty()
        && let Err(error) = lifecycle_state.followup_store().mark_sent(
            &lifecycle_workspace,
            &lifecycle_session,
            &followup_message_id,
            &lifecycle_trace,
        )
    {
        tracing::warn!(code = error.code, message = %error.message, "unable to mark follow-up dispatched");
    }
    if cancellation.is_cancelled() {
        let cancellation_reason = cancellation.reason();
        if cancellation_reason == "steer" {
            if let Err(error) = lifecycle_state.followup_store().requeue_steering(
                &lifecycle_workspace,
                &lifecycle_session,
                &lifecycle_trace,
            ) {
                tracing::warn!(code = error.code, message = %error.message, "unable to requeue interrupted steer");
            }
            if let Err(error) = lifecycle_state.followup_store().pause(
                &lifecycle_workspace,
                &lifecycle_session,
                "steer_requires_resume",
            ) {
                tracing::warn!(code = error.code, message = %error.message, "unable to pause follow-up mailbox after steer");
            }
        } else if let Err(error) = lifecycle_state.followup_store().pause(
            &lifecycle_workspace,
            &lifecycle_session,
            &cancellation_reason,
        ) {
            tracing::warn!(code = error.code, message = %error.message, "unable to pause follow-up mailbox");
        }
    }
    if let Err(error) = lifecycle_state.followup_store().clear_active(
        &lifecycle_workspace,
        &lifecycle_session,
        &lifecycle_trace,
    ) {
        tracing::warn!(code = error.code, message = %error.message, "unable to clear follow-up active trace");
    }
}

fn validate_refactor_request(
    payload: &ChatStreamRequest,
    workspace: &Path,
) -> Result<(), (&'static str, &'static str)> {
    if payload.timeout_ms > MAX_EXECUTION_TIMEOUT_MS {
        return Err((
            "invalid_request",
            "Agent timeoutMs exceeds the 600000 millisecond limit.",
        ));
    }
    if !matches!(
        payload.reasoning_effort.as_str(),
        "auto" | "low" | "medium" | "high" | "xhigh" | "max"
    ) {
        return Err((
            "invalid_request",
            "Agent reasoningEffort is not supported by the v1 contract.",
        ));
    }
    parse_story_generation(&payload.story_generation)?;
    if payload.source_followup_message_id.trim().is_empty()
        && !payload.source_followup_expected_trace_id.trim().is_empty()
    {
        return Err((
            "invalid_request",
            "sourceFollowupExpectedTraceId requires sourceFollowupMessageId.",
        ));
    }
    // The streaming/control entry points compile an omitted capability before
    // validation. Treat an empty value as the safe read-only shape as well so
    // direct callers and older integrations remain valid while the bridge
    // still receives an explicit compiled capability on the real path.
    let capability = payload.capability_mode.trim().to_ascii_lowercase();
    let capability = if capability.is_empty() {
        "read_only"
    } else {
        capability.as_str()
    };
    if !matches!(capability, "read_only" | "scoped_write" | "workspace_write") {
        return Err((
            "invalid_request",
            "Agent capabilityMode must be read_only, scoped_write, or workspace_write.",
        ));
    }
    let permission = payload.permission_mode.trim().to_ascii_lowercase();
    if !matches!(
        permission.as_str(),
        "ask_approval" | "approve_for_me" | "full_access" | "plan_mode"
    ) {
        return Err((
            "invalid_request",
            "Agent permissionMode is not supported by the Refactor contract.",
        ));
    }
    if permission == "plan_mode"
        && (capability != "read_only"
            || payload.writes_allowed
            || payload.core_writes_allowed == Some(true)
            || !payload.allowed_write_roots.is_empty())
    {
        return Err((
            "invalid_request",
            "Plan-mode Refactor turns must remain read-only and cannot authorize writes.",
        ));
    }
    if capability == "read_only"
        && (payload.writes_allowed || payload.core_writes_allowed == Some(true))
    {
        return Err((
            "invalid_request",
            "Read-only Refactor turns cannot enable writes.",
        ));
    }
    if capability == "scoped_write" && payload.allowed_write_roots.is_empty() {
        return Err((
            "invalid_request",
            "Scoped-write Refactor turns require allowedWriteRoots.",
        ));
    }
    for raw_root in &payload.allowed_write_roots {
        let root = PathBuf::from(raw_root.trim());
        let candidate = if root.is_absolute() {
            root
        } else {
            workspace.join(root)
        };
        let Ok(root) = candidate.canonicalize() else {
            return Err((
                "invalid_request",
                "allowedWriteRoots must be existing directories.",
            ));
        };
        if !root.starts_with(workspace) {
            return Err((
                "workspace_outside_refactor_root",
                "allowedWriteRoots must remain inside the Refactor workspace.",
            ));
        }
    }
    Ok(())
}

fn parse_story_generation(
    value: &Value,
) -> Result<Option<StoryGenerationOptions>, (&'static str, &'static str)> {
    let Some(story_generation) = value.as_object() else {
        return Err((
            "invalid_request",
            "Agent storyGeneration must be a JSON object.",
        ));
    };
    if story_generation.is_empty() {
        return Ok(None);
    }
    if story_generation.keys().any(|key| {
        !matches!(
            key.as_str(),
            "fragmentCount" | "chapterLengthTier" | "chapterTemplateId"
        )
    }) {
        return Err((
            "invalid_request",
            "Agent storyGeneration contains unsupported fields.",
        ));
    }
    let fragment_count = story_generation
        .get("fragmentCount")
        .map(Value::as_u64)
        .unwrap_or(Some(1))
        .filter(|value| (1..=20).contains(value))
        .ok_or((
            "invalid_request",
            "Agent storyGeneration.fragmentCount must be an integer from 1 to 20.",
        ))?;
    let chapter_length_tier = story_generation
        .get("chapterLengthTier")
        .map(Value::as_str)
        .unwrap_or(Some("medium"))
        .filter(|value| matches!(*value, "short" | "medium" | "long"))
        .ok_or((
            "invalid_request",
            "Agent storyGeneration.chapterLengthTier must be short, medium, or long.",
        ))?
        .to_owned();
    let chapter_template_id = story_generation
        .get("chapterTemplateId")
        .map(Value::as_str)
        .unwrap_or(Some("default_chapter_directory"))
        .filter(|value| {
            !value.is_empty()
                && value.len() <= 128
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
        })
        .ok_or((
            "invalid_request",
            "Agent storyGeneration.chapterTemplateId must be a safe template identifier.",
        ))?
        .to_owned();
    Ok(Some(StoryGenerationOptions {
        fragment_count,
        chapter_length_tier,
        chapter_template_id,
    }))
}

fn story_operation_type(
    payload: &ChatStreamRequest,
    active_file_exists: bool,
) -> Option<&'static str> {
    if !payload.writes_allowed || payload.capability_mode == "read_only" {
        return None;
    }
    let prompt = payload.prompt.to_ascii_lowercase();
    let advisory_signal = [
        "如何",
        "怎样",
        "为什么",
        "建议",
        "意见",
        "评价",
        "点评",
        "评估",
        "分析",
        "怎么看",
        "怎么样",
        "怎么理解",
        "说明",
        "讲解",
        "how to",
        "why ",
        "explain",
        "advice",
        "suggest",
        "review",
        "evaluate",
        "assessment",
        "opinion",
        "what do you think",
    ]
    .iter()
    .any(|signal| prompt.contains(signal));
    if advisory_signal && !crate::agent_control::inferred_write_intent(&payload.prompt) {
        return None;
    }
    let active_story_file = active_file_exists
        && payload
            .active_file
            .trim()
            .replace('\\', "/")
            .starts_with("chapters/");
    let non_prose_signal = [
        "目录",
        "文件结构",
        "项目结构",
        "大纲",
        "剧本",
        "分镜",
        "角色卡",
        "人物设定",
        "世界观",
        "世界书",
        "知识图谱",
        "wiki",
        "directory",
        "folder",
        "outline",
        "screenplay",
        "character card",
        "worldbook",
    ]
    .iter()
    .any(|signal| prompt.contains(signal));
    let story_signal = active_story_file
        || [
            "续写",
            "扩写",
            "正文",
            "剧情",
            "故事",
            "章节",
            "下一章",
            "片段",
            "场景",
            "小说",
            "story",
            "chapter",
            "scene",
            "prose",
            "novel",
        ]
        .iter()
        .any(|signal| prompt.contains(signal));
    let modify_signal = [
        "重写",
        "改写",
        "修改",
        "整理",
        "重构",
        "rewrite",
        "edit existing",
        "modify existing",
    ]
    .iter()
    .any(|signal| prompt.contains(signal));
    let prose_signal = active_story_file
        || [
            "正文", "剧情", "故事", "片段", "场景", "小说", "story", "scene", "prose", "novel",
        ]
        .iter()
        .any(|signal| prompt.contains(signal))
        || (["重写", "改写", "rewrite"]
            .iter()
            .any(|signal| prompt.contains(signal))
            && ["章节", "chapter"]
                .iter()
                .any(|signal| prompt.contains(signal)));
    if modify_signal && prose_signal && !non_prose_signal {
        return Some("modify_existing");
    }
    let create_signal = [
        "续写",
        "扩写",
        "新写",
        "新增",
        "新建",
        "创建",
        "生成",
        "创作",
        "写第",
        "写一段",
        "写下一章",
        "continue writing",
        "write a new",
        "write new",
        "create the next",
        "add a new",
        "generate a new",
        "create a new",
        "create one",
        "create a ",
    ]
    .iter()
    .any(|signal| prompt.contains(signal));
    (create_signal && story_signal && !non_prose_signal).then_some("create_new")
}

fn should_use_rust_modify_existing(payload: &ChatStreamRequest) -> bool {
    let prompt = payload.prompt.to_ascii_lowercase();
    ![
        "write_file",
        "read_file",
        "tool call",
        "tool_call",
        "只调用",
        "不要调用其他工具",
    ]
    .iter()
    .any(|signal| prompt.contains(signal))
}

fn header_value(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get(name)
        .and_then(|value| value.to_str().ok())
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

#[allow(clippy::result_large_err)]
fn resolve_workspace(state: &AppState, raw: &str) -> Result<PathBuf, Response<Body>> {
    workspace::resolve_workspace_for_request(state, raw)
}

async fn run_chat(execution: ChatExecution) {
    let ChatExecution {
        state,
        payload,
        workspace,
        trace_id,
        session_id,
        sender,
        cancellation,
        control_receiver,
        mut replacement,
    } = execution;
    let mut terminal_sent = false;
    let mut terminal_event = String::new();
    let mut trace_events: Vec<(String, Value)> = Vec::new();
    if !send_event(
        &sender,
        "RunAccepted",
        json!({
            "phase": "accepted",
            "label": "请求已接收",
            "detail": "Refactor Agent 正在准备 Rust 执行环境",
            "status": "running",
            "elapsedMs": 0,
            "noRestorePoint": true,
        }),
        &trace_id,
        &session_id,
    )
    .await
    {
        cancellation.cancel("client_disconnected");
        return;
    }
    tokio::task::yield_now().await;
    for (phase, label, status) in [
        ("intent_classification", "执行意图识别完成", "success"),
        ("context_assembly", "项目上下文组装完成", "success"),
        ("workspace_snapshot", "将在无恢复点状态下继续", "warning"),
        ("task_planning", "无需生成执行步骤", "success"),
    ] {
        if !send_event(
            &sender,
            "TurnPhase",
            json!({
                "phase": phase,
                "label": label,
                "detail": label,
                "status": status,
                "elapsedMs": 0,
                "heartbeat": false,
                "noRestorePoint": phase == "workspace_snapshot",
            }),
            &trace_id,
            &session_id,
        )
        .await
        {
            cancellation.cancel("client_disconnected");
            return;
        }
    }
    if !send_event(
        &sender,
        "TaskPlanCreated",
        json!({"tasks": [], "status": "completed"}),
        &trace_id,
        &session_id,
    )
    .await
    {
        cancellation.cancel("client_disconnected");
        return;
    }
    tokio::select! {
        () = cancellation.cancelled() => {}
        () = tokio::time::sleep(CONTROL_PLANE_PREFLIGHT_GRACE) => {}
    }

    let identity = provider_identity(&state);
    let mut turn_contract = match build_turn_contract(&payload, &workspace) {
        Ok(contract) => contract,
        Err(error) => {
            let contract = json!({
                "status": "error",
                "reasoningEffort": payload.reasoning_effort,
                "requiredQuestions": [],
            });
            if !send_event(&sender, "TurnContract", contract, &trace_id, &session_id).await {
                cancellation.cancel("client_disconnected");
                return;
            }
            send_terminal_error(
                &sender,
                &mut terminal_sent,
                &trace_id,
                &session_id,
                "story_generation_plan_error",
                &format!("Rust story target planning failed: {error:#}"),
            )
            .await;
            send_done(&sender).await;
            return;
        }
    };
    if let (Some(contract), Ok(identity)) = (turn_contract.as_object_mut(), identity.as_ref()) {
        contract.insert("providerId".into(), Value::String(identity.id.clone()));
        contract.insert("model".into(), Value::String(identity.model.clone()));
        if !payload.replace_latest_trace_id.trim().is_empty() {
            contract.insert(
                "replacement".into(),
                json!({
                    "replacesTraceId": payload.replace_latest_trace_id,
                    "replacementTraceId": trace_id,
                    "dialogueOnly": true,
                    "fileChangesReverted": false,
                }),
            );
        }
    }
    if !send_event(
        &sender,
        "TurnContract",
        turn_contract.clone(),
        &trace_id,
        &session_id,
    )
    .await
    {
        cancellation.cancel("client_disconnected");
        return;
    }
    let published_plan_failed = turn_contract
        .get("turnPlan")
        .and_then(|plan| plan.get("chapterPlanValidation"))
        .and_then(Value::as_object)
        .and_then(|validation| validation.get("passed"))
        .and_then(Value::as_bool)
        .is_some_and(|passed| !passed);
    if published_plan_failed {
        let issues = turn_contract
            .get("turnPlan")
            .and_then(|plan| plan.get("chapterPlanValidation"))
            .and_then(|validation| validation.get("issues"))
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join("; ");
        let plan_error_message = format!(
            "Rust story target planning failed{}",
            if issues.is_empty() {
                ".".to_owned()
            } else {
                format!(": {issues}")
            }
        );
        send_terminal_error(
            &sender,
            &mut terminal_sent,
            &trace_id,
            &session_id,
            "story_generation_plan_error",
            &plan_error_message,
        )
        .await;
        send_done(&sender).await;
        return;
    }
    if cancellation.is_cancelled() {
        terminal_event = "AgentCancelled".to_owned();
        trace_events.push((
            terminal_event.clone(),
            with_event_identity(
                "AgentCancelled",
                json!({"reason": cancellation.reason()}),
                &trace_id,
                &session_id,
            ),
        ));
        send_terminal_cancelled(
            &sender,
            &mut terminal_sent,
            &trace_id,
            &session_id,
            &cancellation.reason(),
        )
        .await;
        send_done(&sender).await;
        return;
    }
    let runtime_session_id = match load_runtime_session_id(&state, &workspace, &session_id) {
        Ok(runtime_session_id) => runtime_session_id,
        Err(error) => {
            send_terminal_error(
                &sender,
                &mut terminal_sent,
                &trace_id,
                &session_id,
                "session_restore_failed",
                &format!("Refactor Agent session restore failed: {error:#}"),
            )
            .await;
            send_done(&sender).await;
            return;
        }
    };
    let identity = match identity {
        Ok(identity) => identity,
        Err(error) => {
            send_terminal_error(
                &sender,
                &mut terminal_sent,
                &trace_id,
                &session_id,
                "provider_config_unavailable",
                &format!("Unable to resolve Refactor Agent provider: {error:#}"),
            )
            .await;
            send_done(&sender).await;
            return;
        }
    };
    let provider_id = identity.id.clone();
    let model = identity.model.clone();
    if !send_event(
        &sender,
        "TurnPhase",
        json!({
            "phase": "model_execution",
            "label": "正在启动模型执行",
            "detail": "正在启动模型执行",
            "status": "running",
            "elapsedMs": 0,
            "heartbeat": false,
        }),
        &trace_id,
        &session_id,
    )
    .await
    {
        cancellation.cancel("client_disconnected");
        return;
    }

    let story_options = parse_story_generation(&payload.story_generation)
        .expect("validated storyGeneration request");
    let story_operation = story_options.as_ref().and_then(|_| {
        story_operation_type(&payload, workspace.join(&payload.active_file).is_file())
    });
    let story_create_new = story_operation == Some("create_new");
    let story_modify_existing =
        story_operation == Some("modify_existing") && should_use_rust_modify_existing(&payload);
    if story_create_new {
        let outcome = match crate::story_generation::run_create_new(
            &state,
            &payload,
            story_options.as_ref().expect("story generation options"),
            &workspace,
            &trace_id,
            &session_id,
            &identity,
            &cancellation,
            &sender,
            &mut trace_events,
        )
        .await
        {
            Ok(outcome) => outcome,
            Err(error) => {
                terminal_event = "AgentError".to_owned();
                trace_events.push((
                    terminal_event.clone(),
                    with_event_identity(
                        "AgentError",
                        json!({
                            "error_type": "story_generation_runtime_error",
                            "code": "story_generation_runtime_error",
                            "message": format!("Rust story generation failed: {error:#}"),
                        }),
                        &trace_id,
                        &session_id,
                    ),
                ));
                send_terminal_error(
                    &sender,
                    &mut terminal_sent,
                    &trace_id,
                    &session_id,
                    "story_generation_runtime_error",
                    &format!("Rust story generation failed: {error:#}"),
                )
                .await;
                send_done(&sender).await;
                return;
            }
        };
        terminal_event = outcome.terminal_event;
        let status = match terminal_event.as_str() {
            "AgentCompleted" => "completed",
            "AgentCancelled" => "cancelled",
            _ => "failed",
        };
        if let Err(error) = persist_execution_record_with_events(ExecutionRecordInput {
            workspace: &workspace,
            session_id: &session_id,
            trace_id: &trace_id,
            prompt: &payload.prompt,
            status,
            events: &trace_events,
            reply: &outcome.reply,
            provider_id: &provider_id,
            model: &model,
        }) {
            tracing::warn!(error = %error, trace_id = %trace_id, "unable to persist Rust story generation trace");
        }
        send_done(&sender).await;
        return;
    }

    if story_modify_existing {
        let outcome = match crate::story_generation::run_modify_existing(
            &state,
            &payload,
            story_options.as_ref().expect("story generation options"),
            &workspace,
            &trace_id,
            &session_id,
            &identity,
            &cancellation,
            &sender,
            &mut trace_events,
        )
        .await
        {
            Ok(outcome) => outcome,
            Err(error) => {
                terminal_event = "AgentError".to_owned();
                trace_events.push((
                    terminal_event.clone(),
                    with_event_identity(
                        "AgentError",
                        json!({
                            "error_type": "story_generation_runtime_error",
                            "code": "story_generation_runtime_error",
                            "message": format!("Rust existing-story generation failed: {error:#}"),
                        }),
                        &trace_id,
                        &session_id,
                    ),
                ));
                send_terminal_error(
                    &sender,
                    &mut terminal_sent,
                    &trace_id,
                    &session_id,
                    "story_generation_runtime_error",
                    &format!("Rust existing-story generation failed: {error:#}"),
                )
                .await;
                send_done(&sender).await;
                return;
            }
        };
        terminal_event = outcome.terminal_event;
        let status = match terminal_event.as_str() {
            "AgentCompleted" => "completed",
            "AgentCancelled" => "cancelled",
            _ => "failed",
        };
        if let Err(error) = persist_execution_record_with_events(ExecutionRecordInput {
            workspace: &workspace,
            session_id: &session_id,
            trace_id: &trace_id,
            prompt: &payload.prompt,
            status,
            events: &trace_events,
            reply: &outcome.reply,
            provider_id: &provider_id,
            model: &model,
        }) {
            tracing::warn!(error = %error, trace_id = %trace_id, "unable to persist Rust existing-story trace");
        }
        send_done(&sender).await;
        return;
    }

    if let Err(error) = run_bridge(BridgeRunContext {
        state: &state,
        payload: &payload,
        workspace: &workspace,
        trace_id: &trace_id,
        session_id: &session_id,
        sender: &sender,
        cancellation: &cancellation,
        control_receiver,
        terminal_sent: &mut terminal_sent,
        terminal_event: &mut terminal_event,
        trace_events: &mut trace_events,
        replacement: replacement.as_mut(),
        runtime_session_id,
        identity: &identity,
    })
    .await
    {
        terminal_event = "AgentError".to_owned();
        trace_events.push((
            terminal_event.clone(),
            with_event_identity(
                "AgentError",
                json!({
                    "error_type": "refactor_bridge_error",
                    "code": "refactor_bridge_error",
                    "message": format!("Refactor Agent bridge failed: {error:#}"),
                }),
                &trace_id,
                &session_id,
            ),
        ));
        send_terminal_error(
            &sender,
            &mut terminal_sent,
            &trace_id,
            &session_id,
            "refactor_bridge_error",
            &format!("Refactor Agent bridge failed: {error:#}"),
        )
        .await;
    }
    if !terminal_sent && !cancellation.is_cancelled() {
        terminal_event = "AgentError".to_owned();
        trace_events.push((
            terminal_event.clone(),
            with_event_identity(
                "AgentError",
                json!({
                    "error_type": "refactor_bridge_incomplete",
                    "code": "refactor_bridge_incomplete",
                    "message": "Refactor Agent bridge ended without a terminal event.",
                }),
                &trace_id,
                &session_id,
            ),
        ));
        send_terminal_error(
            &sender,
            &mut terminal_sent,
            &trace_id,
            &session_id,
            "refactor_bridge_incomplete",
            "Refactor Agent bridge ended without a terminal event.",
        )
        .await;
    }
    if terminal_event.is_empty() && cancellation.is_cancelled() {
        terminal_event = "AgentCancelled".to_owned();
    }
    if terminal_event != "AgentCompleted"
        && let Some(transaction) = replacement.as_mut()
        && !transaction.is_accepted()
        && !transaction.is_restored()
        && let Err(error) = transaction.restore("replacement_execution_failed")
    {
        tracing::warn!(error = %error, "unable to restore failed replacement execution");
    }
    let status = match terminal_event.as_str() {
        "AgentCompleted" => "completed",
        "AgentCancelled" => "cancelled",
        _ => "failed",
    };
    let reply = trace_events
        .iter()
        .filter(|(name, _)| name == "TextChunk")
        .filter_map(|(_, data)| data.get("content").and_then(Value::as_str))
        .collect::<String>();
    if let Err(error) = persist_execution_record_with_events(ExecutionRecordInput {
        workspace: &workspace,
        session_id: &session_id,
        trace_id: &trace_id,
        prompt: &payload.prompt,
        status,
        events: &trace_events,
        reply: &reply,
        provider_id: &provider_id,
        model: &model,
    }) {
        tracing::warn!(error = %error, trace_id = %trace_id, "unable to persist Refactor Agent trace");
    }
    send_done(&sender).await;
}

fn build_turn_contract(payload: &ChatStreamRequest, workspace: &Path) -> anyhow::Result<Value> {
    let preset_chars = payload
        .compiled_preset
        .as_deref()
        .map(str::trim)
        .map(str::len)
        .unwrap_or_default();
    let preset_included = preset_chars > 0;
    let story_generation = parse_story_generation(&payload.story_generation)
        .expect("validated storyGeneration request");
    let active_file_exists = workspace.join(&payload.active_file).is_file();
    let story_operation = story_generation
        .as_ref()
        .and_then(|_| story_operation_type(payload, active_file_exists));
    let Some(story_operation) = story_operation else {
        return Ok(json!({
            "status": "ready",
            "reasoningEffort": payload.reasoning_effort,
            "intentFrame": {
                "primary": "general",
                "operationType": if payload.writes_allowed { "modify_existing" } else { "inquiry" },
                "canWrite": payload.writes_allowed,
                "method": "refactor_deterministic",
            },
            "executionPolicy": {
                "capabilityMode": payload.capability_mode,
                "allowedWriteRoots": payload.allowed_write_roots,
                "directFileWrites": payload.core_writes_allowed.unwrap_or(payload.writes_allowed),
                "noRestorePointConfirmed": payload.confirm_no_snapshot,
            },
            "routeHints": {
                "operationSignals": if payload.writes_allowed {
                    vec!["read", "write"]
                } else {
                    vec!["read", "no_write"]
                }
            },
            "contextAssembly": {
                "budget": {"maxTotalChars": 10000, "totalChars": preset_chars, "blockCount": if preset_included { 1 } else { 0 }},
                "contextTrace": {"sources": [{
                    "kind": "runtime_presets",
                    "policy": "active_or_compiled_safe_only",
                    "included": preset_included,
                    "truncated": false,
                    "dropReason": if preset_included { "" } else { "empty" },
                }], "totals": {"assembleMs": 0}},
                "promptBlocks": [],
                "activeFile": payload.active_file,
            },
        }));
    };
    let story_generation = story_generation.expect("story operation requires generation options");

    let chapter_count = count_story_chapters(workspace);
    let chapter_word_count_target = story_chapter_word_count_target(workspace);
    let story_create_new = story_operation == "create_new";
    let bounded_modify_existing = !story_create_new && should_use_rust_modify_existing(payload);
    let chapter_content_mode = if story_generation.chapter_template_id.contains("single_file") {
        "single_file"
    } else {
        "multi_fragment"
    };
    let (
        target_chapter_number,
        authoritative_chapter_path,
        authoritative_fragment_paths,
        fragment_targets,
        chapter_action,
        chapter_action_reason,
        chapter_plan_validation,
    ) = if story_create_new {
        let targets = crate::story_generation::plan_create_new_targets(
            workspace,
            &story_generation.chapter_template_id,
            story_generation.fragment_count,
        )?;
        let relative_paths = targets
            .iter()
            .map(|target| {
                target
                    .strip_prefix(workspace)
                    .map_err(|_| anyhow::anyhow!("planned story target is outside workspace"))
                    .map(|relative| relative.to_string_lossy().replace('\\', "/"))
            })
            .collect::<anyhow::Result<Vec<_>>>()?;
        let chapter_path = Path::new(
            relative_paths
                .first()
                .ok_or_else(|| anyhow::anyhow!("planned story target list is empty"))?,
        )
        .parent()
        .ok_or_else(|| anyhow::anyhow!("planned story target has no chapter directory"))?
        .to_string_lossy()
        .replace('\\', "/");
        let chapter_name = Path::new(&chapter_path)
            .file_name()
            .and_then(|value| value.to_str())
            .ok_or_else(|| anyhow::anyhow!("planned story chapter name is invalid"))?;
        let chapter_number = chapter_name
            .strip_prefix('第')
            .and_then(|value| value.split('章').next())
            .and_then(|value| value.parse::<u64>().ok())
            .ok_or_else(|| anyhow::anyhow!("planned story chapter number is invalid"))?;
        let planned_targets = relative_paths
            .iter()
            .enumerate()
            .map(|(index, path)| {
                json!({
                    "order": index + 1,
                    "path": path,
                    "writeMode": "replace",
                    "baselineWordCount": 0,
                    "contentMode": chapter_content_mode,
                })
            })
            .collect::<Vec<_>>();
        (
            chapter_number,
            chapter_path.clone(),
            relative_paths.clone(),
            planned_targets,
            "create_next_chapter".to_owned(),
            if chapter_count == 0 {
                "new_story".to_owned()
            } else {
                "next_chapter_requested".to_owned()
            },
            json!({
                "_type": "ChapterPlanValidation",
                "_version": 1,
                "passed": true,
                "action": "create_next_chapter",
                "targetChapterNumber": chapter_number,
                "authoritativeChapterPath": chapter_path,
                "authoritativeFragmentPaths": relative_paths,
                "issues": [],
            }),
        )
    } else if bounded_modify_existing {
        match crate::story_generation::plan_modify_existing_targets(
            workspace,
            &payload.active_file,
            story_generation.fragment_count,
        ) {
            Ok(targets) => {
                let relative_paths = targets
                    .iter()
                    .map(|target| {
                        target
                            .strip_prefix(workspace)
                            .map_err(|_| {
                                anyhow::anyhow!(
                                    "planned existing-story target is outside workspace"
                                )
                            })
                            .map(|relative| relative.to_string_lossy().replace('\\', "/"))
                    })
                    .collect::<anyhow::Result<Vec<_>>>()?;
                let chapter_path = Path::new(relative_paths.first().ok_or_else(|| {
                    anyhow::anyhow!("planned existing-story target list is empty")
                })?)
                .parent()
                .ok_or_else(|| {
                    anyhow::anyhow!("planned existing-story target has no chapter directory")
                })?
                .to_string_lossy()
                .replace('\\', "/");
                let chapter_number = chapter_number_from_story_path(&payload.active_file);
                let planned_targets = targets
                    .iter()
                    .zip(relative_paths.iter())
                    .enumerate()
                    .map(|(index, (target, relative))| {
                        let bytes = fs::read(target).with_context(|| {
                            format!(
                                "unable to read existing-story baseline {}",
                                target.display()
                            )
                        })?;
                        let text = String::from_utf8(bytes.clone()).with_context(|| {
                            format!("existing-story baseline is not UTF-8: {}", target.display())
                        })?;
                        Ok(json!({
                            "order": index + 1,
                            "path": relative,
                            "writeMode": "replace",
                            "baselineWordCount": text
                                .trim_start_matches('\u{feff}')
                                .chars()
                                .filter(|character| !character.is_whitespace())
                                .count(),
                            "baselineSha256": format!("{:x}", Sha256::digest(&bytes)),
                            "contentMode": "multi_fragment",
                        }))
                    })
                    .collect::<anyhow::Result<Vec<_>>>()?;
                (
                    chapter_number,
                    chapter_path.clone(),
                    relative_paths.clone(),
                    planned_targets,
                    "modify_existing".to_owned(),
                    "active_existing_file".to_owned(),
                    json!({
                        "_type": "ModifyExistingPlanValidation",
                        "_version": 1,
                        "passed": true,
                        "action": "modify_existing",
                        "targetChapterNumber": chapter_number,
                        "authoritativeChapterPath": chapter_path,
                        "authoritativeFragmentPaths": relative_paths,
                        "issues": [],
                    }),
                )
            }
            Err(error) => (
                chapter_number_from_story_path(&payload.active_file),
                String::new(),
                Vec::new(),
                Vec::new(),
                String::new(),
                String::new(),
                json!({
                    "_type": "ModifyExistingPlanValidation",
                    "_version": 1,
                    "passed": false,
                    "action": "modify_existing",
                    "targetChapterNumber": chapter_number_from_story_path(&payload.active_file),
                    "authoritativeChapterPath": "",
                    "authoritativeFragmentPaths": [],
                    "issues": [format!("{error:#}")],
                }),
            ),
        }
    } else {
        (
            0,
            String::new(),
            Vec::new(),
            Vec::new(),
            String::new(),
            String::new(),
            json!({}),
        )
    };
    let effective_fragment_count = authoritative_fragment_paths.len() as u64;
    let next_segment_path = authoritative_fragment_paths
        .first()
        .cloned()
        .unwrap_or_default();
    let script_path = workspace.join(".storydex/scripts/README.md");
    let story_script_exists = script_path.is_file();
    let mut prompt_blocks = Vec::new();
    if active_file_exists {
        prompt_blocks.push(json!({
            "id": "recent_segments",
            "title": "Recent story segments",
            "sourcePaths": [payload.active_file],
            "truncated": false,
            "omitted": false,
            "dropReason": "",
        }));
    }
    if story_script_exists {
        prompt_blocks.push(json!({
            "id": "story_scripts",
            "title": "Relevant story scripts",
            "sourcePaths": [".storydex/scripts/README.md"],
            "truncated": false,
            "omitted": false,
            "dropReason": "",
        }));
    }
    if preset_included {
        prompt_blocks.push(json!({
            "id": "runtime_presets",
            "title": "Active Storydex preset",
            "sourcePaths": [".storydex/presets/active.json"],
            "truncated": false,
            "omitted": false,
            "dropReason": "",
        }));
    }
    let context_source = |kind: &str, policy: &str, included: bool| {
        json!({
            "kind": kind,
            "policy": policy,
            "included": included,
            "truncated": false,
            "dropReason": if included { "" } else { "empty" },
        })
    };
    let asset_roots = json!([
        "chapters/",
        ".storydex/memory/chapters/",
        ".storydex/characters/",
        ".storydex/memory/",
        ".storydex/wiki/"
    ]);

    let mut contract = json!({
        "status": "ready",
        "reasoningEffort": payload.reasoning_effort,
        "intentFrame": {
            "primary": "story_generation",
            "confidence": "medium",
            "signals": ["story_keywords"],
            "method": "deterministic_hybrid",
            "operationType": if story_create_new { "create_new" } else { "modify_existing" },
            "decision": "decided",
            "effect": if story_create_new { "create" } else { "modify" },
            "artifact": "chapter_prose",
            "targetScope": "none",
            "targetValue": "",
            "explicitConstraints": [],
            "ambiguities": [],
            "evidence": [],
            "canWrite": payload.writes_allowed,
            "complexity": if story_create_new { "simple" } else { "complex" },
            "existingChapterCount": chapter_count,
            "assetTargets": asset_roots,
            "matchedSkills": ["变量思考", "故事生成后更新"],
        },
        "executionPolicy": {
            "coomiRole": "general_agent_runtime",
            "storydexRole": "fiction_orchestration",
            "capabilityMode": payload.capability_mode,
            "directFileWrites": payload.core_writes_allowed.unwrap_or(payload.writes_allowed),
            "pendingWriteApproval": false,
            "localGitAutoCommit": true,
            "allowedWriteRoots": asset_roots,
            "remotePush": false,
            "highRiskChangeRequiresNotice": true,
        },
        "turnPlan": {
            "operationType": if story_create_new { "create_new" } else { "modify_existing" },
            "requestedFragmentCount": story_generation.fragment_count,
            "fragmentCount": if story_create_new { effective_fragment_count } else { story_generation.fragment_count },
            "selectedChapterTemplate": story_generation.chapter_template_id,
            "chapterContentMode": chapter_content_mode,
            "chapterWordCountTarget": chapter_word_count_target,
            "fragmentWordCount": chapter_word_count_target,
            "fragmentWordCountMin": chapter_word_count_target,
            "fragmentWordCountMax": chapter_word_count_target,
            "chapterAction": chapter_action,
            "chapterActionReason": chapter_action_reason,
            "targetChapterNumber": target_chapter_number,
            "authoritativeChapterPath": authoritative_chapter_path,
            "authoritativeFragmentPaths": authoritative_fragment_paths,
            "fragmentTargets": fragment_targets,
            "boundedStoryGeneration": story_create_new || bounded_modify_existing,
            "chapterPlanValidation": chapter_plan_validation,
            "isNewStory": story_create_new && chapter_count == 0,
            "requiresChapterTemplateSelection": false,
            "nextSegmentPath": next_segment_path,
            "chapterCount": chapter_count,
            "activeFile": payload.active_file,
            "storyFormatSource": if story_create_new && chapter_count == 0 { "selected_chapter_template" } else { "existing_project" },
            "wordCountPolicy": {
                "version": 5,
                "mode": "target",
                "scope": "chapter",
                "target": chapter_word_count_target,
                "minimum": chapter_word_count_target,
                "maximum": chapter_word_count_target,
            },
        },
        "contextAssembly": {
            "budget": {"maxTotalChars": 10000, "blockCount": prompt_blocks.len()},
            "contextTrace": {"sources": [
                context_source("runtime_presets", "active_or_compiled_safe_only", preset_included),
                context_source("recent_segments", "compact_recent_only", active_file_exists),
                context_source("rolling_summaries", "latest_chapters_only", false),
                context_source("active_characters", "structure_map_matched_spans_jit_read", false),
                context_source("worldbook", "structure_map_matched_spans_jit_read", false),
                context_source("facts", "relevant_only", false),
                context_source("relationships", "neighborhood_only", false),
                context_source("items", "compact_relevant_only", false),
                context_source("related_passages", "fts5_v3_chunk_bm25", false),
                context_source("wiki_reference", "entity_matched_reference_only", false),
                context_source("story_scripts", "relevant_only", story_script_exists),
                context_source("variable_snapshot", "compact_preview_only", false),
            ]},
            "promptBlocks": prompt_blocks,
        },
        "knowledgeWritePolicy": {
            "mode": "standard",
            "confirmationRequired": false,
            "confirmed": true,
        },
        "assetTargets": {
            "chapterRoot": "chapters/",
            "characterRoot": ".storydex/characters/",
            "variableThoughtRoot": ".storydex/memory/chapters/",
            "factMemoryPath": ".storydex/memory/current/facts.json",
            "relationshipGraphPath": ".storydex/memory/current/relationship_graph.json",
            "wikiRoot": ".storydex/wiki/",
        },
        "contextPolicy": {
            "activePresetsOnly": true,
            "compiledSafePresetsAllowed": true,
            "recentActiveCharactersOnly": true,
            "avoidFullMemoryDump": true,
            "variableThinkingFormat": "markdown_first",
            "machineVariableOperations": "optional",
            "sources": {
                "base_story_context": true,
                "story_structured_memory": true,
                "passive_fts": true,
                "wiki_context": true,
                "coomi_memory": true,
                "active_retrieval_tools": true,
            },
        },
        "updatePolicy": {
            "autoUpdateVariables": false,
            "autoUpdateWiki": false,
            "autoUpdateVariablesNote": "自动更新变量需要较多耗时，建议每次仅生成单条剧情片段。",
        },
        "requiredQuestions": [],
        "routeHints": {"operationSignals": ["read", "write"]},
        "storyGeneration": {
            "fragmentCount": story_generation.fragment_count,
            "chapterLengthTier": story_generation.chapter_length_tier,
            "chapterTemplateId": story_generation.chapter_template_id,
        },
    });
    if story_create_new
        && let Some(turn_plan) = contract.get_mut("turnPlan").and_then(Value::as_object_mut)
    {
        for legacy_key in [
            "chapterWordCountTarget",
            "fragmentWordCount",
            "fragmentWordCountMin",
            "fragmentWordCountMax",
        ] {
            turn_plan.remove(legacy_key);
        }
        turn_plan.insert(
            "chapterLengthTier".into(),
            Value::String(story_generation.chapter_length_tier.clone()),
        );
        turn_plan.insert(
            "wordCountPolicy".into(),
            json!({
                "version": 5,
                "mode": "tier",
                "scope": "candidate",
                "tier": story_generation.chapter_length_tier,
            }),
        );
    }
    Ok(contract)
}

fn count_story_chapters(workspace: &Path) -> usize {
    fs::read_dir(workspace.join("chapters"))
        .map(|entries| {
            entries
                .filter_map(Result::ok)
                .filter(|entry| {
                    let path = entry.path();
                    path.is_dir()
                        || (path.is_file()
                            && !matches!(
                                path.file_name().and_then(|value| value.to_str()),
                                Some("README.md")
                            ))
                })
                .count()
        })
        .unwrap_or(0)
}

fn chapter_number_from_story_path(path: &str) -> u64 {
    Path::new(path)
        .components()
        .filter_map(|component| component.as_os_str().to_str())
        .find_map(|component| {
            component
                .strip_prefix('第')
                .and_then(|value| value.split('章').next())
                .and_then(|value| value.parse::<u64>().ok())
        })
        .unwrap_or(0)
}

fn story_chapter_word_count_target(workspace: &Path) -> u64 {
    let settings_path = workspace.join(".storydex/config/project-settings.json");
    fs::read_to_string(settings_path)
        .ok()
        .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
        .and_then(|settings| {
            settings
                .get("chapterWordCountTarget")
                .and_then(Value::as_u64)
        })
        .filter(|target| (100..=20_000).contains(target))
        .unwrap_or(3000)
}

fn provider_identity(state: &AppState) -> anyhow::Result<ProviderIdentity> {
    let config = state.coomi_home().join("config").join("providers.json");
    let registry = ProviderRegistry::load(&config)
        .map_err(|error| anyhow::anyhow!("unable to load provider configuration: {error:#}"))?;
    let provider = registry.resolve(None)?;
    Ok(ProviderIdentity {
        id: provider.id,
        model: provider.model,
        display: provider.display,
    })
}

async fn run_bridge(context: BridgeRunContext<'_>) -> anyhow::Result<()> {
    let BridgeRunContext {
        state,
        payload,
        workspace,
        trace_id,
        session_id,
        sender,
        cancellation,
        mut control_receiver,
        terminal_sent,
        terminal_event,
        trace_events,
        mut replacement,
        runtime_session_id,
        identity,
    } = context;
    if cancellation.is_cancelled() {
        *terminal_event = "AgentCancelled".to_owned();
        trace_events.push((
            terminal_event.clone(),
            with_event_identity(
                "AgentCancelled",
                json!({"reason": cancellation.reason()}),
                trace_id,
                session_id,
            ),
        ));
        send_terminal_cancelled(
            sender,
            terminal_sent,
            trace_id,
            session_id,
            &cancellation.reason(),
        )
        .await;
        return Ok(());
    }
    let mut command = Command::new(state.bridge_path());
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let mut child = command.spawn().map_err(|error| {
        anyhow::anyhow!(
            "unable to start storydex-coomi-bridge {}: {error}",
            state.bridge_path().display()
        )
    })?;
    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| anyhow::anyhow!("storydex-coomi-bridge stdin is unavailable"))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| anyhow::anyhow!("storydex-coomi-bridge stdout is unavailable"))?;
    let request = bridge_request(state, payload, workspace, session_id, runtime_session_id)?;
    let mut line = serde_json::to_vec(&request)?;
    line.push(b'\n');
    stdin.write_all(&line).await?;
    stdin.flush().await?;
    tokio::task::yield_now().await;
    if cancellation.is_cancelled() {
        send_bridge_control(&mut stdin, "cancel", Some(&cancellation.reason())).await;
        let _ = tokio::time::timeout(BRIDGE_SHUTDOWN_TIMEOUT, child.wait()).await;
        *terminal_event = "AgentCancelled".to_owned();
        trace_events.push((
            terminal_event.clone(),
            with_event_identity(
                "AgentCancelled",
                json!({"reason": cancellation.reason()}),
                trace_id,
                session_id,
            ),
        ));
        send_terminal_cancelled(
            sender,
            terminal_sent,
            trace_id,
            session_id,
            &cancellation.reason(),
        )
        .await;
        return Ok(());
    }
    if !send_event(
        sender,
        "AgentStarted",
        json!({
            "mode": "coomi-rust-refactor",
            "query": payload.prompt,
            "llmModel": identity.model,
            "llmProvider": identity.id,
            "coomiStatus": {
                "runtime": "storydex-coomi-rs",
                "installed": true,
                "providerId": identity.id,
                "model": identity.model,
                "display": identity.display,
            },
        }),
        trace_id,
        session_id,
    )
    .await
    {
        cancellation.cancel("client_disconnected");
        return Ok(());
    }
    let mut lines = BufReader::new(stdout).lines();
    let mut child_terminal = false;
    let mut control_closed = false;
    loop {
        tokio::select! {
            _ = cancellation.cancelled() => {
                send_bridge_control(&mut stdin, "cancel", Some(&cancellation.reason())).await;
                let _ = tokio::time::timeout(BRIDGE_SHUTDOWN_TIMEOUT, child.wait()).await;
                if !child_terminal {
                    *terminal_event = "AgentCancelled".to_owned();
                    trace_events.push((
                        terminal_event.clone(),
                        with_event_identity(
                            "AgentCancelled",
                            json!({"reason": cancellation.reason()}),
                            trace_id,
                            session_id,
                        ),
                    ));
                    send_terminal_cancelled(
                        sender,
                        terminal_sent,
                        trace_id,
                        session_id,
                        &cancellation.reason(),
                    )
                    .await;
                }
                break;
            }
            control = control_receiver.recv(), if !control_closed => {
                match control {
                    Some(ExecutionControl::Resolve { request_id, value }) => {
                        let _ = send_bridge_resolve_value(&mut stdin, &request_id, value).await;
                    }
                    None => control_closed = true,
                }
            }
            result = lines.next_line() => {
                let Some(raw) = result? else { break; };
                let packet: Value = match serde_json::from_str(&raw) {
                    Ok(value) => value,
                    Err(error) => {
                        *terminal_event = "AgentError".to_owned();
                        trace_events.push((
                            terminal_event.clone(),
                            with_event_identity(
                                "AgentError",
                                json!({
                                    "error_type": "bridge_protocol_error",
                                    "code": "bridge_protocol_error",
                                    "message": format!("Invalid bridge event: {error}"),
                                }),
                                trace_id,
                                session_id,
                            ),
                        ));
                        send_terminal_error(sender, terminal_sent, trace_id, session_id, "bridge_protocol_error", &format!("Invalid bridge event: {error}")).await;
                        send_bridge_control(&mut stdin, "cancel", Some("bridge_protocol_error")).await;
                        break;
                    }
                };
                let kind = packet.get("type").and_then(Value::as_str).unwrap_or_default();
                if kind == "session_bound" {
                    persist_session_binding(
                        state,
                        workspace,
                        session_id,
                        packet.get("data").unwrap_or(&Value::Null),
                    )?;
                    continue;
                }
                if matches!(kind, "approval_request" | "user_input_request") {
                    let request_id = packet
                        .get("data")
                        .and_then(|data| data.get("requestId"))
                        .and_then(Value::as_str)
                        .unwrap_or_default();
                    state
                        .execution_registry()
                        .register_request(trace_id, request_id);
                }
                if kind == "tool_request" {
                    let request_id = packet.get("data").and_then(|data| data.get("requestId")).and_then(Value::as_str).unwrap_or_default();
                    if !request_id.is_empty() {
                        let call = packet
                            .get("data")
                            .and_then(|data| data.get("call"))
                            .cloned()
                            .unwrap_or_else(|| json!({}));
                        let value = dispatch_storydex_tool(state, workspace, &call).await;
                        if !send_bridge_resolve_value(&mut stdin, request_id, value).await {
                            cancellation.cancel("bridge_tool_resolution_failed");
                            break;
                        }
                    }
                    continue;
                }
                if let Some((event_name, data)) = translate_bridge_event(&packet, trace_id, session_id) {
                    if replacement_accepts_event(&event_name)
                        && let Some(transaction) = replacement.as_deref_mut()
                        && !transaction.is_accepted()
                    {
                        transaction.accept()?;
                    }
                    if event_name == "AgentCompleted" {
                        emit_git_commit_prompt(
                            state,
                            workspace,
                            sender,
                            trace_events,
                            trace_id,
                            session_id,
                            Some((&event_name, &data)),
                        )
                        .await?;
                    }
                    if event_name == "AgentCompleted" || event_name == "AgentError" || event_name == "AgentCancelled" {
                        child_terminal = true;
                        *terminal_sent = true;
                        *terminal_event = event_name.clone();
                    }
                    trace_events.push((event_name.clone(), data.clone()));
                    if !send_event_value(sender, &event_name, data).await {
                        cancellation.cancel("client_disconnected");
                        continue;
                    }
                }
            }
        }
    }
    if child_terminal {
        let _ = tokio::time::timeout(BRIDGE_SHUTDOWN_TIMEOUT, child.wait()).await;
    } else if !cancellation.is_cancelled() {
        let status = child.wait().await?;
        if !status.success() {
            anyhow::bail!("storydex-coomi-bridge exited with {status}");
        }
    }
    Ok(())
}

struct BridgeRunContext<'a> {
    state: &'a AppState,
    payload: &'a ChatStreamRequest,
    workspace: &'a Path,
    trace_id: &'a str,
    session_id: &'a str,
    sender: &'a mpsc::Sender<String>,
    cancellation: &'a ExecutionCancellation,
    control_receiver: mpsc::Receiver<ExecutionControl>,
    terminal_sent: &'a mut bool,
    terminal_event: &'a mut String,
    trace_events: &'a mut Vec<(String, Value)>,
    replacement: Option<&'a mut ReplacementTransaction>,
    runtime_session_id: Option<Uuid>,
    identity: &'a ProviderIdentity,
}

fn replacement_accepts_event(event_name: &str) -> bool {
    matches!(
        event_name,
        "ProviderStream" | "ModelCompleted" | "ToolStart" | "ToolDone" | "TextChunk"
    )
}

#[derive(Clone, Debug)]
pub(crate) struct RuntimeSessionBinding {
    pub(crate) runtime_id: Uuid,
    pub(crate) session_path: PathBuf,
    pub(crate) binding_path: PathBuf,
}

pub(crate) fn session_binding_path(workspace: &Path, session_id: &str) -> PathBuf {
    let normalized = if session_id.trim().is_empty() {
        "default"
    } else {
        session_id.trim()
    };
    let digest = format!("{:x}", Sha256::digest(normalized.as_bytes()));
    workspace
        .join(".storydex")
        .join(".agent")
        .join("runtime")
        .join("coomi-sessions")
        .join(format!("{}.json", &digest[..24]))
}

fn expected_runtime_session_path(state: &AppState, runtime_id: Uuid) -> PathBuf {
    state
        .coomi_home()
        .join("sessions")
        .join(format!("{runtime_id}.json"))
}

fn validate_runtime_session_path(
    state: &AppState,
    runtime_id: Uuid,
    raw_path: &str,
    must_exist: bool,
) -> anyhow::Result<PathBuf> {
    let expected = expected_runtime_session_path(state, runtime_id);
    let candidate = PathBuf::from(raw_path);
    let expected_contract = contract_path(&expected);
    let candidate_contract = contract_path(&candidate);
    let same_contract = if cfg!(windows) {
        candidate_contract.eq_ignore_ascii_case(&expected_contract)
    } else {
        candidate_contract == expected_contract
    };
    anyhow::ensure!(
        same_contract,
        "Refactor Agent session binding points outside the configured runtime home"
    );
    if expected.exists() {
        let expected_canonical = expected.canonicalize().map_err(|error| {
            anyhow::anyhow!("bound Refactor Agent session {runtime_id} is unavailable: {error}")
        })?;
        let candidate_canonical = candidate
            .canonicalize()
            .map_err(|error| anyhow::anyhow!("invalid Refactor Agent session path: {error}"))?;
        anyhow::ensure!(
            candidate_canonical == expected_canonical,
            "Refactor Agent session binding points outside the configured runtime home"
        );
        return Ok(expected_canonical);
    }
    anyhow::ensure!(
        !must_exist,
        "bound Refactor Agent session {runtime_id} is unavailable"
    );
    Ok(expected)
}

fn contract_path(path: &Path) -> String {
    let value = path.to_string_lossy();
    value
        .strip_prefix("\\\\?\\UNC\\")
        .map(|rest| format!("\\\\{rest}"))
        .or_else(|| value.strip_prefix("\\\\?\\").map(ToOwned::to_owned))
        .unwrap_or_else(|| value.into_owned())
}

pub(crate) fn load_runtime_session_binding(
    state: &AppState,
    workspace: &Path,
    session_id: &str,
) -> anyhow::Result<Option<RuntimeSessionBinding>> {
    let path = session_binding_path(workspace, session_id);
    let metadata = match fs::symlink_metadata(&path) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(anyhow::anyhow!(
                "unable to inspect Refactor Agent session binding: {error}"
            ));
        }
    };
    anyhow::ensure!(
        !metadata.file_type().is_symlink() && metadata.is_file(),
        "Refactor Agent session binding must be a regular file"
    );
    let raw = fs::read_to_string(&path).map_err(|error| {
        anyhow::anyhow!("unable to read Refactor Agent session binding: {error}")
    })?;
    let value: Value = serde_json::from_str(&raw)
        .map_err(|error| anyhow::anyhow!("invalid Refactor Agent session binding: {error}"))?;
    let binding = value
        .as_object()
        .ok_or_else(|| anyhow::anyhow!("Refactor Agent session binding must be an object"))?;
    anyhow::ensure!(
        binding.get("workspaceRoot").and_then(Value::as_str)
            == Some(contract_path(workspace).as_str()),
        "Refactor Agent session binding workspace mismatch"
    );
    anyhow::ensure!(
        binding.get("storydexSessionId").and_then(Value::as_str) == Some(session_id),
        "Refactor Agent session binding session mismatch"
    );
    let runtime_id = binding
        .get("runtimeSessionId")
        .or_else(|| binding.get("coomiSessionId"))
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow::anyhow!("Refactor Agent session binding has no runtime session id"))?
        .parse::<Uuid>()
        .map_err(|error| anyhow::anyhow!("invalid Refactor Agent runtime session id: {error}"))?;
    let raw_path = binding
        .get("sessionPath")
        .or_else(|| binding.get("historyPath"))
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow::anyhow!("Refactor Agent session binding has no session path"))?;
    let session_path = validate_runtime_session_path(state, runtime_id, raw_path, false)?;
    Ok(Some(RuntimeSessionBinding {
        runtime_id,
        session_path,
        binding_path: path,
    }))
}

pub(crate) fn load_runtime_session_id(
    state: &AppState,
    workspace: &Path,
    session_id: &str,
) -> anyhow::Result<Option<Uuid>> {
    let Some(binding) = load_runtime_session_binding(state, workspace, session_id)? else {
        return Ok(None);
    };
    validate_runtime_session_path(
        state,
        binding.runtime_id,
        &contract_path(&binding.session_path),
        true,
    )?;
    Ok(Some(binding.runtime_id))
}

fn persist_session_binding(
    state: &AppState,
    workspace: &Path,
    session_id: &str,
    data: &Value,
) -> anyhow::Result<()> {
    let data = data
        .as_object()
        .ok_or_else(|| anyhow::anyhow!("session_bound payload must be an object"))?;
    anyhow::ensure!(
        data.get("persisted").and_then(Value::as_bool) == Some(true),
        "Refactor Agent runtime session was not persisted"
    );
    anyhow::ensure!(
        data.get("sessionSchemaVersion").and_then(Value::as_u64) == Some(1),
        "Refactor Agent runtime session schema is unsupported"
    );
    let runtime_id = data
        .get("runtimeSessionId")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow::anyhow!("session_bound has no runtime session id"))?
        .parse::<Uuid>()
        .map_err(|error| anyhow::anyhow!("invalid session_bound runtime id: {error}"))?;
    let raw_path = data
        .get("sessionPath")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow::anyhow!("session_bound has no session path"))?;
    let session_path = validate_runtime_session_path(state, runtime_id, raw_path, true)?;
    let binding = json!({
        "version": 2,
        "runtime": "storydex-coomi-rs",
        "workspaceRoot": contract_path(workspace),
        "storydexSessionId": session_id,
        "coomiSessionId": runtime_id,
        "runtimeSessionId": runtime_id,
        "historyPath": contract_path(&session_path),
        "sessionPath": contract_path(&session_path),
        "updatedAt": Utc::now().to_rfc3339(),
    });
    let path = session_binding_path(workspace, session_id);
    let parent = path
        .parent()
        .ok_or_else(|| anyhow::anyhow!("Refactor Agent session binding has no parent"))?;
    fs::create_dir_all(parent)?;
    let temporary = path.with_file_name(format!(
        ".{}.{}.tmp",
        path.file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("session-binding"),
        Uuid::new_v4()
    ));
    let write_result = (|| -> anyhow::Result<()> {
        let mut file = fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)?;
        let mut bytes = serde_json::to_vec_pretty(&binding)?;
        bytes.push(b'\n');
        file.write_all(&bytes)?;
        file.sync_all()?;
        drop(file);
        if path.exists() {
            fs::remove_file(&path)?;
        }
        fs::rename(&temporary, &path)?;
        Ok(())
    })();
    if write_result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    write_result
}

fn bridge_request(
    state: &AppState,
    payload: &ChatStreamRequest,
    workspace: &Path,
    session_id: &str,
    runtime_session_id: Option<Uuid>,
) -> anyhow::Result<Value> {
    let allowed_write_roots = payload
        .allowed_write_roots
        .iter()
        .map(|value| {
            let root = PathBuf::from(value);
            let candidate = if root.is_absolute() {
                root
            } else {
                workspace.join(root)
            };
            candidate.canonicalize().map_err(|error| {
                anyhow::anyhow!("invalid Refactor allowed write root {value}: {error}")
            })
        })
        .collect::<anyhow::Result<Vec<_>>>()?;
    let writes_authorized = payload.writes_allowed
        && matches!(
            payload.capability_mode.trim(),
            "scoped_write" | "workspace_write"
        );
    const COOMI_IDENTITY_PROMPT: &str = "You are Coomi, the Storydex Agent. Your identity remains Coomi regardless of the permission mode or turn capability.";
    let capability_boundary = if writes_authorized {
        "This turn may modify only paths authorized by the compiled turn capability. Preserve unrelated work and report only verified results."
    } else {
        "This turn has no project-write authority. Use only tools allowed by the compiled turn capability and do not modify project files. Describe this as the current tool boundary, never as a different agent role."
    };
    let base_system_prompt = format!("{COOMI_IDENTITY_PROMPT}\n\n{capability_boundary}");
    let system_prompt = payload
        .compiled_preset
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(|preset| {
            format!(
                "{base_system_prompt}\n\n<storydex_active_preset>\n{preset}\n</storydex_active_preset>"
            )
        })
        .unwrap_or_else(|| base_system_prompt.to_owned());
    let mut request = json!({
        "action": "run",
        "cwd": workspace,
        "home": state.coomi_home(),
        "prompt": payload.prompt,
        "systemPrompt": system_prompt,
        "storydexSessionId": session_id,
        "permissionMode": payload.permission_mode,
        "basePermissionMode": payload.permission_mode,
        "capabilityMode": payload.capability_mode,
        "reasoningEffort": payload.reasoning_effort,
        "storyGeneration": payload.story_generation,
        "writesAllowed": payload.writes_allowed,
        "coreWritesAllowed": payload.core_writes_allowed.unwrap_or(payload.writes_allowed),
        "allowedWriteRoots": allowed_write_roots,
        "toolSpecs": storydex_tool_specs(),
        "mutatingToolNames": [],
    });
    if let Some(path) = state.replay_fixture() {
        request["providerReplayFixture"] = Value::String(path.display().to_string());
    }
    if let Some(runtime_id) = runtime_session_id {
        request["runtimeSessionId"] = Value::String(runtime_id.to_string());
    }
    Ok(request)
}

/// Return the Storydex-owned tools that the Rust agentd can dispatch locally.
///
/// The bridge deliberately knows nothing about the HTTP/project boundary: it
/// only forwards a tool request over its control pipe.  Keeping the registry
/// here makes the advertised surface match the dispatcher and avoids exposing
/// tools that would otherwise be acknowledged with a fake failure.
fn storydex_tool_specs() -> Value {
    json!([
        {
            "name": "StorydexWikiQuery",
            "description": "Query the project WIKI knowledge graph by keyword, entry, or node and return evidence-grounded entries and relationship neighbors.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword search over WIKI entries, nodes, and edges."
                    },
                    "nodeId": {
                        "type": "string",
                        "description": "Expand this graph node's relationship neighborhood."
                    },
                    "entryId": {
                        "type": "string",
                        "description": "Fetch this WIKI entry and its linked nodes."
                    },
                    "depth": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2,
                        "description": "Neighborhood expansion depth (default 1)."
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 30,
                        "description": "Maximum number of graph nodes to return (default 12)."
                    },
                    "workspaceRoot": {
                        "type": "string",
                        "description": "Optional active workspace root; external paths are rejected."
                    }
                },
                "additionalProperties": false
            }
        }
    ])
}

const STORYDEX_TOOL_RESPONSE_BYTES: usize = 8 * 1024 * 1024;

async fn dispatch_storydex_tool(state: &AppState, workspace: &Path, call: &Value) -> Value {
    let name = call
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();
    let arguments = call.get("arguments").cloned().unwrap_or_else(|| json!({}));
    if name != "StorydexWikiQuery" {
        return json!({
            "success": false,
            "output": format!("Unknown Storydex tool: {name}"),
        });
    }

    match dispatch_storydex_wiki_query(state, workspace, &arguments).await {
        Ok(output) => json!({"success": true, "output": output}),
        Err(error) => json!({"success": false, "output": format!("{error:#}")}),
    }
}

async fn dispatch_storydex_wiki_query(
    state: &AppState,
    workspace: &Path,
    arguments: &Value,
) -> anyhow::Result<String> {
    let arguments = arguments
        .as_object()
        .ok_or_else(|| anyhow::anyhow!("StorydexWikiQuery arguments must be an object"))?;
    let value_string = |key: &str| {
        arguments
            .get(key)
            .and_then(Value::as_str)
            .unwrap_or_default()
            .trim()
            .to_owned()
    };
    let query = value_string("query");
    let node_id = value_string("nodeId");
    let entry_id = value_string("entryId");
    anyhow::ensure!(
        !query.is_empty() || !node_id.is_empty() || !entry_id.is_empty(),
        "one of query/nodeId/entryId is required"
    );
    if let Some(requested_root) = arguments
        .get("workspaceRoot")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        let requested_root = PathBuf::from(requested_root)
            .canonicalize()
            .with_context(|| format!("workspaceRoot does not exist: {requested_root}"))?;
        anyhow::ensure!(
            requested_root == workspace,
            "StorydexWikiQuery workspaceRoot does not match the active workspace"
        );
    }
    let depth = arguments
        .get("depth")
        .and_then(Value::as_u64)
        .unwrap_or(1)
        .clamp(1, 2) as usize;
    let limit = arguments
        .get("limit")
        .and_then(Value::as_u64)
        .unwrap_or(12)
        .clamp(1, 30) as usize;
    let query = crate::project::WikiGraphQuery {
        workspace_root: workspace.to_string_lossy().into_owned(),
        q: query,
        category: String::new(),
        entry_id,
        node_id,
        depth,
        limit,
        offset: 0,
        include_review: false,
    };
    let response = crate::project::wiki_graph(State(state.clone()), Query(query)).await;
    let status = response.status();
    let body = axum::body::to_bytes(response.into_body(), STORYDEX_TOOL_RESPONSE_BYTES)
        .await
        .context("failed to read StorydexWikiQuery response")?;
    let envelope: Value = serde_json::from_slice(&body)
        .context("StorydexWikiQuery returned invalid project response JSON")?;
    if !status.is_success() || envelope.get("ok").and_then(Value::as_bool) != Some(true) {
        let message = envelope
            .pointer("/error/message")
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .unwrap_or("Storydex WIKI query failed");
        anyhow::bail!(message.to_owned());
    }
    let mut data = envelope.get("data").cloned().unwrap_or_else(|| json!({}));
    if let Some(object) = data.as_object_mut() {
        object.insert("ok".to_owned(), Value::Bool(true));
        object.insert(
            "workspaceRoot".to_owned(),
            Value::String(workspace.to_string_lossy().into_owned()),
        );
    }
    serde_json::to_string_pretty(&data).context("failed to encode StorydexWikiQuery result")
}

async fn send_bridge_control(
    stdin: &mut tokio::process::ChildStdin,
    action: &str,
    reason: Option<&str>,
) {
    let mut value = json!({"action": action});
    if let Some(reason) = reason.filter(|value| !value.trim().is_empty()) {
        value["reason"] = Value::String(reason.to_owned());
    }
    let line = match serde_json::to_string(&value) {
        Ok(line) => format!("{line}\n"),
        Err(_) => return,
    };
    let _ = stdin.write_all(line.as_bytes()).await;
    let _ = stdin.flush().await;
}

async fn send_bridge_resolve_value(
    stdin: &mut tokio::process::ChildStdin,
    request_id: &str,
    value: Value,
) -> bool {
    if let Ok(mut line) = serde_json::to_vec(&json!({
        "action": "resolve",
        "requestId": request_id,
        "value": value,
    })) {
        line.push(b'\n');
        if stdin.write_all(&line).await.is_ok() && stdin.flush().await.is_ok() {
            return true;
        }
    }
    false
}

fn translate_bridge_event(
    packet: &Value,
    trace_id: &str,
    session_id: &str,
) -> Option<(String, Value)> {
    let kind = packet
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let data = packet.get("data").cloned().unwrap_or_else(|| json!({}));
    let (name, mut payload) = match kind {
        "text" | "text_delta" => (
            "TextChunk",
            json!({
                "content": data
                    .get("content")
                    .or_else(|| data.get("text"))
                    .cloned()
                    .unwrap_or_default(),
            }),
        ),
        "provider_stream" => ("ProviderStream", data),
        "model_started" => (
            "TurnPhase",
            json!({
                "phase": "model",
                "label": format!(
                    "{} / {}",
                    data.get("provider").and_then(Value::as_str).unwrap_or_default(),
                    data.get("model").and_then(Value::as_str).unwrap_or_default()
                ),
                "status": "running",
                "current": data.get("round").and_then(Value::as_u64).unwrap_or(1),
            }),
        ),
        "reasoning_plan" => ("ReasoningPlan", data),
        "model_completed" => ("ModelCompleted", data),
        "runtime_initialized" => {
            let mut metrics = Map::new();
            metrics.insert(
                "providerMode".into(),
                data.get("providerMode")
                    .cloned()
                    .unwrap_or_else(|| json!("live")),
            );
            if let Some(object) = data.as_object() {
                for (key, value) in object {
                    if key.ends_with("Ms") {
                        metrics.insert(key.clone(), value.clone());
                    }
                }
            }
            ("RuntimeMetrics", Value::Object(metrics))
        }
        "tool_started" => {
            let call = data.get("call").cloned().unwrap_or_else(|| json!({}));
            (
                "ToolStart",
                json!({
                    "tool_name": call.get("name").and_then(Value::as_str).unwrap_or_default(),
                    "tool_call_id": call.get("id").and_then(Value::as_str).unwrap_or_default(),
                    "arguments": call.get("arguments").cloned().unwrap_or_else(|| json!({})),
                }),
            )
        }
        "tool_finished" => {
            let call = data.get("call").cloned().unwrap_or_else(|| json!({}));
            let result = data.get("result").cloned().unwrap_or_else(|| json!({}));
            let output = result
                .get("output")
                .and_then(Value::as_str)
                .unwrap_or_default();
            (
                "ToolDone",
                json!({
                    "tool_name": call.get("name").and_then(Value::as_str).unwrap_or_default(),
                    "tool_call_id": call.get("id").and_then(Value::as_str).unwrap_or_default(),
                    "is_error": !result.get("success").and_then(Value::as_bool).unwrap_or(false),
                    "result_preview": output.chars().take(4000).collect::<String>(),
                    "arguments": call.get("arguments").cloned().unwrap_or_else(|| json!({})),
                }),
            )
        }
        "approval_request" => {
            let request_id = data
                .get("requestId")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let call = data.get("call").cloned().unwrap_or_else(|| json!({}));
            (
                "PermissionRequest",
                json!({
                    "kind": "permission",
                    "approvalId": request_id,
                    "approval_id": request_id,
                    "requestId": request_id,
                    "question": data.get("reason").cloned().unwrap_or_else(|| json!("Allow this tool call?")),
                    "tool": call,
                    "options": [
                        {"label": "Allow", "value": "allow", "isRecommended": true},
                        {"label": "Deny", "value": "deny", "isRecommended": false}
                    ]
                }),
            )
        }
        "user_input_request" => {
            let request_id = data
                .get("requestId")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let request = data.get("request").cloned().unwrap_or_else(|| json!({}));
            (
                "PermissionRequest",
                json!({
                    "kind": "question",
                    "approvalId": request_id,
                    "approval_id": request_id,
                    "requestId": request_id,
                    "request": request
                }),
            )
        }
        "context_updated" | "turn_completed" => ("UsageUpdate", data),
        "compaction_started" | "compaction_completed" => ("CompressionEvent", data),
        "plan_updated" => ("TaskPlanUpdated", data),
        "plan_mode_changed" => ("PlanModeChanged", data),
        "loop_updated" => ("TurnPhase", data),
        "provider_retry" => ("ConnectionRetry", data),
        "protocol_warning" => ("AgentWarning", data),
        "cancelled" => ("AgentCancelled", data),
        "completed" => ("AgentCompleted", data),
        "error" => (
            "AgentError",
            json!({
                "error_type": data.get("errorType").and_then(Value::as_str).unwrap_or("storydex_coomi_bridge_error"),
                "message": data.get("message").and_then(Value::as_str).unwrap_or("Refactor Agent bridge failed."),
                "details": {
                    "runtime": "storydex-coomi-rs",
                    "httpStatus": data.get("httpStatus").cloned().unwrap_or(Value::Null),
                },
            }),
        ),
        _ => return None,
    };
    if let Some(object) = payload.as_object_mut() {
        object.insert("_type".into(), Value::String(name.to_owned()));
        object.insert("_version".into(), json!(1));
        object.entry("traceId").or_insert_with(|| json!(trace_id));
        object
            .entry("sessionId")
            .or_insert_with(|| json!(session_id));
    }
    Some((name.to_owned(), payload))
}

async fn send_event(
    sender: &mpsc::Sender<String>,
    name: &str,
    data: Value,
    trace_id: &str,
    session_id: &str,
) -> bool {
    let payload = with_event_identity(name, data, trace_id, session_id);
    send_event_value(sender, name, payload).await
}

pub(crate) async fn send_event_value(
    sender: &mpsc::Sender<String>,
    name: &str,
    payload: Value,
) -> bool {
    let Ok(data) = serde_json::to_string(&payload) else {
        return false;
    };
    sender
        .send(format!("event: {name}\ndata: {data}\n\n"))
        .await
        .is_ok()
}

pub(crate) fn with_event_identity(
    name: &str,
    data: Value,
    trace_id: &str,
    session_id: &str,
) -> Value {
    if name == "done" {
        return json!({"type": "done"});
    }
    let mut object = data.as_object().cloned().unwrap_or_default();
    object.insert("_type".into(), Value::String(name.to_owned()));
    object.insert("_version".into(), json!(1));
    object.entry("traceId").or_insert_with(|| json!(trace_id));
    object
        .entry("sessionId")
        .or_insert_with(|| json!(session_id));
    Value::Object(object)
}

pub(crate) async fn emit_git_commit_prompt(
    state: &AppState,
    workspace: &Path,
    sender: &mpsc::Sender<String>,
    trace_events: &mut Vec<(String, Value)>,
    trace_id: &str,
    session_id: &str,
    pending_terminal: Option<(&str, &Value)>,
) -> anyhow::Result<bool> {
    if trace_events
        .iter()
        .any(|(name, _)| name == "GitCommitPrompt")
    {
        return Ok(false);
    }
    let mut observed = trace_events.clone();
    if let Some((name, data)) = pending_terminal {
        observed.push((name.to_owned(), data.clone()));
    }
    let ledger = change_ledger_from_events(workspace, session_id, trace_id, &observed);
    let changed_files = ledger
        .get("changedFiles")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let commit_hash = ledger
        .get("commitHash")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if changed_files.is_empty() || !commit_hash.is_empty() {
        return Ok(false);
    }
    state
        .followup_store()
        .pause(workspace, session_id, "git_commit_prompt")
        .map_err(|error| anyhow::anyhow!(error.message))?;
    let payload = with_event_identity(
        "GitCommitPrompt",
        json!({
            "created": false,
            "status": "pending",
            "reason": "pending_commit",
            "message": "检测到未提交修改，请选择创建本地版本或保留工作区修改。",
            "workspaceRoot": workspace,
            "changedFiles": changed_files,
            "changedFileCount": changed_files.len(),
            "added": 0,
            "removed": 0,
            "diffSource": "working_tree",
            "commitHash": "",
            "shortHash": "",
        }),
        trace_id,
        session_id,
    );
    anyhow::ensure!(
        send_event_value(sender, "GitCommitPrompt", payload.clone()).await,
        "Agent SSE sender closed before Git commit prompt"
    );
    trace_events.push(("GitCommitPrompt".to_owned(), payload));
    Ok(true)
}

async fn send_terminal_error(
    sender: &mpsc::Sender<String>,
    terminal_sent: &mut bool,
    trace_id: &str,
    session_id: &str,
    code: &str,
    message: &str,
) {
    if *terminal_sent {
        return;
    }
    *terminal_sent = true;
    let _ = send_event(
        sender,
        "AgentError",
        json!({"error_type": code, "code": code, "message": message}),
        trace_id,
        session_id,
    )
    .await;
}

async fn send_terminal_cancelled(
    sender: &mpsc::Sender<String>,
    terminal_sent: &mut bool,
    trace_id: &str,
    session_id: &str,
    reason: &str,
) {
    if *terminal_sent {
        return;
    }
    *terminal_sent = true;
    let _ = send_event(
        sender,
        "AgentCancelled",
        json!({"reason": reason}),
        trace_id,
        session_id,
    )
    .await;
}

async fn send_done(sender: &mpsc::Sender<String>) {
    let _ = send_event_value(sender, "done", json!({"type": "done"})).await;
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;
    use tempfile::tempdir;

    async fn response_error_code(response: Response<Body>) -> String {
        let bytes = to_bytes(response.into_body(), 1024 * 1024)
            .await
            .expect("read response body");
        serde_json::from_slice::<Value>(&bytes).expect("decode response JSON")["error"]["code"]
            .as_str()
            .unwrap_or_default()
            .to_owned()
    }

    #[test]
    fn bridge_translation_preserves_read_tool_and_provider_mode() {
        let translated = translate_bridge_event(
            &json!({
                "type": "runtime_initialized",
                "data": {"providerMode": "replay", "providerInitMs": 2.5, "secret": "drop"}
            }),
            "trace",
            "session",
        )
        .expect("runtime event");
        assert_eq!(translated.0, "RuntimeMetrics");
        assert_eq!(translated.1["providerMode"], "replay");
        assert_eq!(translated.1["providerInitMs"], 2.5);
        assert!(translated.1.get("secret").is_none());
    }

    #[test]
    fn bridge_text_translation_uses_contract_content_field() {
        let translated = translate_bridge_event(
            &json!({"type": "text", "data": {"text": "fixed reply"}}),
            "trace",
            "session",
        )
        .expect("text event");
        assert_eq!(translated.0, "TextChunk");
        assert_eq!(translated.1["content"], "fixed reply");
        assert!(translated.1.get("text").is_none());
    }

    #[tokio::test]
    async fn workspace_resolution_requires_configured_fixture_root() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path().join("workspace");
        let outside = tempdir().expect("outside");
        std::fs::create_dir_all(&workspace).expect("workspace");
        let state = AppState::with_paths(
            "token",
            directory.path().join("home"),
            directory.path().join("bridge"),
            Some(directory.path().to_path_buf()),
            None,
        )
        .expect("state");
        assert_eq!(
            resolve_workspace(&state, &workspace.to_string_lossy()).expect("root"),
            workspace.canonicalize().expect("canonical")
        );
        let response = resolve_workspace(&state, &outside.path().to_string_lossy())
            .expect_err("outside workspace must be rejected");
        assert_eq!(response.status(), StatusCode::FORBIDDEN);
        assert_eq!(
            response_error_code(response).await,
            "workspace_outside_refactor_root"
        );
    }

    #[tokio::test]
    async fn workspace_resolution_requires_desktop_selection_without_fixture_root() {
        let directory = tempdir().expect("tempdir");
        let state = AppState::with_paths(
            "token",
            directory.path().join("home"),
            directory.path().join("bridge"),
            None,
            None,
        )
        .expect("state");
        let response = resolve_workspace(&state, "").expect_err("selection is required");
        assert_eq!(response.status(), StatusCode::CONFLICT);
        assert_eq!(
            response_error_code(response).await,
            "workspace_not_selected"
        );
    }

    #[tokio::test]
    async fn workspace_resolution_uses_only_the_desktop_selected_project() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path().join("workspace");
        let other = directory.path().join("other");
        std::fs::create_dir_all(&workspace).expect("workspace");
        std::fs::create_dir_all(&other).expect("other workspace");
        let state = AppState::with_paths(
            "token",
            directory.path().join("home"),
            directory.path().join("bridge"),
            None,
            None,
        )
        .expect("state");
        state
            .select_workspace(workspace.canonicalize().expect("canonical workspace"))
            .expect("select workspace");

        assert_eq!(
            resolve_workspace(&state, "").expect("selected workspace"),
            workspace.canonicalize().expect("canonical workspace")
        );
        assert_eq!(
            resolve_workspace(&state, &workspace.to_string_lossy()).expect("matching workspace"),
            workspace.canonicalize().expect("canonical workspace")
        );

        let response = resolve_workspace(&state, &other.to_string_lossy())
            .expect_err("unselected workspace must be rejected");
        assert_eq!(response.status(), StatusCode::FORBIDDEN);
        assert_eq!(
            response_error_code(response).await,
            "workspace_not_selected"
        );
    }

    #[test]
    fn refactor_request_accepts_controlled_story_generation() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path();
        let mut payload: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "read only",
            "reasoningEffort": "low"
        }))
        .expect("request");
        assert!(validate_refactor_request(&payload, workspace).is_ok());

        payload.story_generation = json!({
            "fragmentCount": 1,
            "chapterLengthTier": "short",
            "chapterTemplateId": "default_chapter_directory"
        });
        assert!(validate_refactor_request(&payload, workspace).is_ok());

        payload.story_generation = json!({"enabled": true});
        assert_eq!(
            validate_refactor_request(&payload, workspace).map_err(|error| error.0),
            Err("invalid_request")
        );

        payload.story_generation = json!({});
        payload.replace_latest_trace_id = "latest-trace".to_owned();
        assert!(validate_refactor_request(&payload, workspace).is_ok());
    }

    #[test]
    fn refactor_request_validates_reasoning_and_story_generation_types() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path();
        let invalid_reasoning: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "read only",
            "reasoningEffort": "extreme"
        }))
        .expect("request");
        assert_eq!(
            validate_refactor_request(&invalid_reasoning, workspace).map_err(|error| error.0),
            Err("invalid_request")
        );

        let invalid_story: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "read only",
            "storyGeneration": []
        }))
        .expect("request");
        assert_eq!(
            validate_refactor_request(&invalid_story, workspace).map_err(|error| error.0),
            Err("invalid_request")
        );

        for invalid_story in [
            json!({"fragmentCount": 0}),
            json!({"fragmentCount": 1.5}),
            json!({"chapterLengthTier": "unbounded"}),
            json!({"chapterTemplateId": "../../escape"}),
        ] {
            let payload: ChatStreamRequest = serde_json::from_value(json!({
                "prompt": "read only",
                "storyGeneration": invalid_story
            }))
            .expect("request");
            assert_eq!(
                validate_refactor_request(&payload, workspace).map_err(|error| error.0),
                Err("invalid_request")
            );
        }
    }

    #[test]
    fn chat_stream_session_id_uses_query_when_header_is_absent() {
        let query = ChatQuery {
            session_id: "query-session".to_owned(),
        };
        let headers = HeaderMap::new();
        assert_eq!(resolve_chat_session_id(&headers, &query), "query-session");

        let mut headers = HeaderMap::new();
        headers.insert("x-session-id", "header-session".parse().expect("header"));
        assert_eq!(resolve_chat_session_id(&headers, &query), "header-session");

        let empty_query = ChatQuery::default();
        let empty_headers = HeaderMap::new();
        assert_eq!(
            resolve_chat_session_id(&empty_headers, &empty_query),
            "default"
        );
    }

    #[test]
    fn story_operation_routes_creation_continuation_and_existing_rewrites() {
        let create: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "生成故事的下一章",
            "activeFile": "chapters/第1章 既有/001.md",
            "storyGeneration": {"fragmentCount": 2},
            "capabilityMode": "workspace_write",
            "writesAllowed": true
        }))
        .expect("create request");
        assert_eq!(story_operation_type(&create, true), Some("create_new"));

        let rewrite: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "请重写当前章节，保留既有事实",
            "activeFile": "chapters/第1章 既有/001.md",
            "storyGeneration": {"fragmentCount": 2},
            "capabilityMode": "workspace_write",
            "writesAllowed": true
        }))
        .expect("rewrite request");
        assert_eq!(
            story_operation_type(&rewrite, true),
            Some("modify_existing")
        );
        assert!(should_use_rust_modify_existing(&rewrite));

        let tool_contract: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "请重写当前章节，只调用一次 write_file",
            "activeFile": "chapters/fixture-story.md",
            "storyGeneration": {"fragmentCount": 1},
            "capabilityMode": "workspace_write",
            "writesAllowed": true
        }))
        .expect("tool request");
        assert_eq!(
            story_operation_type(&tool_contract, true),
            Some("modify_existing")
        );
        assert!(!should_use_rust_modify_existing(&tool_contract));
    }

    #[test]
    fn story_preferences_do_not_reclassify_general_or_read_only_turns() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path();
        let ordinary: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "请解释这个错误的原因",
            "storyGeneration": {"fragmentCount": 1, "chapterLengthTier": "short"}
        }))
        .expect("ordinary request");
        assert_eq!(story_operation_type(&ordinary, false), None);
        let contract = build_turn_contract(&ordinary, workspace).expect("general contract");
        assert_eq!(contract["intentFrame"]["primary"], "general");
        assert_eq!(contract["intentFrame"]["operationType"], "inquiry");

        let generic_write: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "请修改 src/lib.rs 中的错误处理",
            "storyGeneration": {"fragmentCount": 1, "chapterLengthTier": "short"},
            "capabilityMode": "workspace_write",
            "writesAllowed": true
        }))
        .expect("generic write request");
        assert_eq!(story_operation_type(&generic_write, false), None);
        let contract =
            build_turn_contract(&generic_write, workspace).expect("general write contract");
        assert_eq!(contract["intentFrame"]["primary"], "general");
        assert_eq!(contract["intentFrame"]["operationType"], "modify_existing");

        let read_only_story: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "只读分析，不要修改任何文件：请重写当前章节",
            "activeFile": "chapters/fixture.md",
            "storyGeneration": {"fragmentCount": 1},
            "capabilityMode": "read_only",
            "writesAllowed": false
        }))
        .expect("read-only story request");
        assert_eq!(story_operation_type(&read_only_story, true), None);

        for prompt in ["请整理章节目录", "请生成故事大纲"] {
            let non_prose: ChatStreamRequest = serde_json::from_value(json!({
                "prompt": prompt,
                "activeFile": "chapters/fixture.md",
                "storyGeneration": {"fragmentCount": 1},
                "capabilityMode": "workspace_write",
                "writesAllowed": true
            }))
            .expect("non-prose story asset request");
            assert_eq!(story_operation_type(&non_prose, true), None, "{prompt}");
        }

        for prompt in [
            "请解释如何修改当前章节",
            "请说明怎样更新这段故事",
            "帮我分析如何重写这一幕",
        ] {
            let advisory: ChatStreamRequest = serde_json::from_value(json!({
                "prompt": prompt,
                "activeFile": "chapters/fixture.md",
                "storyGeneration": {"fragmentCount": 1},
                "capabilityMode": "workspace_write",
                "writesAllowed": true
            }))
            .expect("advisory story request");
            assert_eq!(story_operation_type(&advisory, true), None, "{prompt}");
        }
    }

    #[test]
    fn scoped_write_roots_resolve_relative_to_workspace() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path().join("workspace");
        let allowed = workspace.join(".storydex").join("characters");
        std::fs::create_dir_all(&allowed).expect("allowed root");
        let workspace = workspace.canonicalize().expect("canonical workspace");
        let payload: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "update the character fixture",
            "capabilityMode": "scoped_write",
            "writesAllowed": true,
            "coreWritesAllowed": true,
            "allowedWriteRoots": [".storydex/characters/"]
        }))
        .expect("request");

        assert!(validate_refactor_request(&payload, &workspace).is_ok());

        let state = AppState::with_paths(
            "token",
            directory.path().join("home"),
            directory.path().join("bridge"),
            Some(directory.path().to_path_buf()),
            None,
        )
        .expect("state");
        let request =
            bridge_request(&state, &payload, &workspace, "session", None).expect("bridge request");
        assert_eq!(
            request["allowedWriteRoots"],
            json!([allowed.canonicalize().expect("canonical allowed root")])
        );
    }

    #[test]
    fn bridge_prompt_keeps_coomi_identity_across_permissions_and_turn_capabilities() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path().join("workspace");
        std::fs::create_dir_all(&workspace).expect("workspace");
        let workspace = workspace.canonicalize().expect("canonical workspace");
        let state = AppState::with_paths(
            "token",
            directory.path().join("home"),
            directory.path().join("bridge"),
            Some(workspace.clone()),
            None,
        )
        .expect("state");

        for permission_mode in ["ask_approval", "approve_for_me", "full_access", "plan_mode"] {
            let read_only: ChatStreamRequest = serde_json::from_value(json!({
                "prompt": "你好，你是谁",
                "permissionMode": permission_mode,
                "capabilityMode": "read_only"
            }))
            .expect("read-only request");
            let read_only_request = bridge_request(
                &state,
                &read_only,
                &workspace,
                &format!("{permission_mode}-read-only"),
                None,
            )
            .expect("read-only bridge request");
            let read_only_prompt = read_only_request["systemPrompt"]
                .as_str()
                .expect("read-only system prompt");
            assert!(read_only_prompt.contains("You are Coomi, the Storydex Agent."));
            assert!(read_only_prompt.contains("identity remains Coomi"));
            assert!(read_only_prompt.contains("current tool boundary"));
            assert!(!read_only_prompt.contains("read-only Refactor Agent"));
            assert!(!read_only_prompt.contains("fixture-scoped Refactor Agent"));
            assert_eq!(read_only_request["permissionMode"], permission_mode);
        }

        for permission_mode in ["ask_approval", "approve_for_me", "full_access"] {
            let writable: ChatStreamRequest = serde_json::from_value(json!({
                "prompt": "请修改 README.md",
                "permissionMode": permission_mode,
                "capabilityMode": "workspace_write",
                "writesAllowed": true,
                "coreWritesAllowed": true
            }))
            .expect("writable request");
            let writable_request = bridge_request(
                &state,
                &writable,
                &workspace,
                &format!("{permission_mode}-writable"),
                None,
            )
            .expect("writable bridge request");
            let writable_prompt = writable_request["systemPrompt"]
                .as_str()
                .expect("writable system prompt");
            assert!(writable_prompt.contains("You are Coomi, the Storydex Agent."));
            assert!(writable_prompt.contains("identity remains Coomi"));
            assert!(writable_prompt.contains("This turn may modify only paths authorized"));
            assert!(!writable_prompt.contains("This turn has no project-write authority"));
            assert!(!writable_prompt.contains("read-only Refactor Agent"));
            assert!(!writable_prompt.contains("fixture-scoped Refactor Agent"));
            assert_eq!(writable_request["permissionMode"], permission_mode);
        }
    }

    #[test]
    fn bridge_request_advertises_only_storydex_tools_with_a_local_dispatcher() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path().join("workspace");
        std::fs::create_dir_all(&workspace).expect("workspace");
        let workspace = workspace.canonicalize().expect("canonical workspace");
        let state = AppState::with_paths(
            "token",
            directory.path().join("home"),
            directory.path().join("bridge"),
            Some(workspace.clone()),
            None,
        )
        .expect("state");
        let payload: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "query the project WIKI",
            "capabilityMode": "read_only",
            "writesAllowed": false
        }))
        .expect("request");

        let request =
            bridge_request(&state, &payload, &workspace, "session", None).expect("bridge request");
        assert_eq!(
            request["toolSpecs"]
                .as_array()
                .expect("tool specs")
                .iter()
                .map(|spec| spec["name"].as_str().unwrap_or_default())
                .collect::<Vec<_>>(),
            vec!["StorydexWikiQuery"]
        );
        assert_eq!(request["mutatingToolNames"], json!([]));
    }

    #[tokio::test]
    async fn storydex_wiki_query_dispatches_through_the_rust_project_route() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path().join("workspace");
        std::fs::create_dir_all(workspace.join("chapters")).expect("chapters");
        std::fs::write(
            workspace.join("chapters/001.md"),
            "# Dawn\n\nHero reaches the river. WIKI_DISPATCH_MARKER\n",
        )
        .expect("chapter");
        let workspace = workspace.canonicalize().expect("canonical workspace");
        let state = AppState::with_paths(
            "token",
            directory.path().join("home"),
            directory.path().join("bridge"),
            Some(directory.path().to_path_buf()),
            None,
        )
        .expect("state");

        let resolved = dispatch_storydex_tool(
            &state,
            &workspace,
            &json!({
                "name": "StorydexWikiQuery",
                "arguments": {"query": "Hero", "limit": 3}
            }),
        )
        .await;
        assert_eq!(resolved["success"], true);
        let output = resolved["output"].as_str().expect("structured output");
        let result: Value = serde_json::from_str(output).expect("decode WIKI output");
        assert_eq!(result["ok"], true);
        assert_eq!(result["mode"], "search");
        assert!(result["entries"].as_array().is_some_and(|entries| {
            entries.iter().any(|entry| {
                entry
                    .get("summary")
                    .and_then(Value::as_str)
                    .is_some_and(|summary| summary.contains("Hero"))
            })
        }));
    }

    #[test]
    fn story_turn_contract_freezes_external_semantics_without_expanding_bridge_roots() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path().join("workspace");
        std::fs::create_dir_all(workspace.join("chapters")).expect("chapters");
        std::fs::create_dir_all(workspace.join(".storydex/scripts")).expect("scripts");
        std::fs::write(workspace.join("chapters/fixture.md"), "fixture\n").expect("chapter");
        std::fs::write(workspace.join(".storydex/scripts/README.md"), "fixture\n").expect("script");
        let workspace = workspace.canonicalize().expect("canonical workspace");
        let payload: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "rewrite the story",
            "activeFile": "chapters/fixture.md",
            "reasoningEffort": "low",
            "storyGeneration": {"fragmentCount": 1, "chapterLengthTier": "short"},
            "capabilityMode": "scoped_write",
            "writesAllowed": true,
            "coreWritesAllowed": true,
            "allowedWriteRoots": ["chapters/"]
        }))
        .expect("request");

        let contract = build_turn_contract(&payload, &workspace).expect("turn contract");
        assert_eq!(contract["intentFrame"]["primary"], "story_generation");
        assert_eq!(contract["turnPlan"]["chapterCount"], 1);
        assert_eq!(contract["contextAssembly"]["budget"]["blockCount"], 2);
        assert_eq!(contract["updatePolicy"]["autoUpdateWiki"], false);

        let state = AppState::with_paths(
            "token",
            directory.path().join("home"),
            directory.path().join("bridge"),
            Some(directory.path().to_path_buf()),
            None,
        )
        .expect("state");
        let request =
            bridge_request(&state, &payload, &workspace, "session", None).expect("bridge request");
        assert_eq!(request["storyGeneration"]["fragmentCount"], 1);
        assert_eq!(
            request["allowedWriteRoots"].as_array().map(Vec::len),
            Some(1)
        );
    }

    #[test]
    fn modify_existing_turn_contract_publishes_authoritative_targets_and_baselines() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path().join("workspace");
        let chapter = workspace.join("chapters/第1章 既有");
        std::fs::create_dir_all(&chapter).expect("chapter");
        let second = chapter.join("002.md");
        let third = chapter.join("003.md");
        std::fs::write(&second, "原始二\n").expect("second");
        std::fs::write(&third, "原始三\n").expect("third");
        let workspace = workspace.canonicalize().expect("canonical workspace");
        let payload: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "请重写当前两个连续片段，保留既有事实",
            "activeFile": "chapters/第1章 既有/002.md",
            "reasoningEffort": "low",
            "storyGeneration": {"fragmentCount": 2, "chapterLengthTier": "short"},
            "capabilityMode": "scoped_write",
            "writesAllowed": true,
            "coreWritesAllowed": true,
            "allowedWriteRoots": ["chapters/"]
        }))
        .expect("request");

        let contract = build_turn_contract(&payload, &workspace).expect("turn contract");
        let plan = &contract["turnPlan"];
        assert_eq!(plan["chapterAction"], "modify_existing");
        assert_eq!(plan["chapterActionReason"], "active_existing_file");
        assert_eq!(plan["targetChapterNumber"], 1);
        assert_eq!(plan["authoritativeChapterPath"], "chapters/第1章 既有");
        assert_eq!(
            plan["authoritativeFragmentPaths"],
            json!(["chapters/第1章 既有/002.md", "chapters/第1章 既有/003.md"])
        );
        assert_eq!(plan["fragmentCount"], 2);
        assert_eq!(plan["fragmentTargets"][0]["writeMode"], "replace");
        assert_eq!(plan["fragmentTargets"][0]["baselineWordCount"], 3);
        assert_eq!(
            plan["fragmentTargets"][0]["baselineSha256"],
            format!("{:x}", Sha256::digest("原始二\n".as_bytes()))
        );
        assert_eq!(
            plan["chapterPlanValidation"]["_type"],
            "ModifyExistingPlanValidation"
        );
        assert_eq!(plan["chapterPlanValidation"]["passed"], true);
    }

    #[test]
    fn modify_existing_turn_contract_rejects_missing_contiguous_targets_before_provider() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path().join("workspace");
        let chapter = workspace.join("chapters/第1章 既有");
        std::fs::create_dir_all(&chapter).expect("chapter");
        std::fs::write(chapter.join("002.md"), "原始二\n").expect("second");
        let workspace = workspace.canonicalize().expect("canonical workspace");
        let payload: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "请重写当前两个连续片段",
            "activeFile": "chapters/第1章 既有/002.md",
            "storyGeneration": {"fragmentCount": 2, "chapterLengthTier": "short"},
            "capabilityMode": "scoped_write",
            "writesAllowed": true,
            "coreWritesAllowed": true,
            "allowedWriteRoots": ["chapters/"]
        }))
        .expect("request");

        let contract = build_turn_contract(&payload, &workspace).expect("turn contract");
        let plan = &contract["turnPlan"];
        assert_eq!(plan["fragmentTargets"], json!([]));
        assert_eq!(plan["authoritativeFragmentPaths"], json!([]));
        assert_eq!(plan["chapterPlanValidation"]["passed"], false);
        assert!(
            plan["chapterPlanValidation"]["issues"][0]
                .as_str()
                .is_some_and(|issue| issue.contains("enough contiguous fragments"))
        );
    }

    #[test]
    fn create_new_short_turn_contract_uses_tier_paths_without_target_legacy_fields() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path().join("workspace");
        std::fs::create_dir_all(workspace.join("chapters")).expect("chapters");
        std::fs::create_dir_all(workspace.join(".storydex/scripts")).expect("scripts");
        std::fs::write(workspace.join(".storydex/scripts/README.md"), "fixture\n").expect("script");
        let workspace = workspace.canonicalize().expect("canonical workspace");
        let payload: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "create the next chapter",
            "activeFile": "",
            "reasoningEffort": "low",
            "storyGeneration": {
                "fragmentCount": 1,
                "chapterLengthTier": "short",
                "chapterTemplateId": "default_chapter_directory"
            },
            "capabilityMode": "scoped_write",
            "writesAllowed": true,
            "coreWritesAllowed": true,
            "allowedWriteRoots": ["chapters/"]
        }))
        .expect("request");

        let contract = build_turn_contract(&payload, &workspace).expect("turn contract");
        assert_eq!(contract["intentFrame"]["operationType"], "create_new");
        assert_eq!(contract["intentFrame"]["effect"], "create");
        assert_eq!(contract["intentFrame"]["complexity"], "simple");
        assert_eq!(contract["turnPlan"]["operationType"], "create_new");
        assert_eq!(contract["turnPlan"]["chapterLengthTier"], "short");
        assert_eq!(contract["turnPlan"]["chapterAction"], "create_next_chapter");
        assert_eq!(contract["turnPlan"]["targetChapterNumber"], 1);
        assert_eq!(
            contract["turnPlan"]["authoritativeFragmentPaths"],
            json!(["chapters/第1章 未命名/001.md"])
        );
        assert_eq!(contract["turnPlan"]["wordCountPolicy"]["mode"], "tier");
        assert_eq!(
            contract["turnPlan"]["wordCountPolicy"]["scope"],
            "candidate"
        );
        for legacy_key in [
            "chapterWordCountTarget",
            "fragmentWordCount",
            "fragmentWordCountMin",
            "fragmentWordCountMax",
        ] {
            assert!(contract["turnPlan"].get(legacy_key).is_none());
        }
    }

    #[test]
    fn create_new_medium_turn_contract_keeps_medium_tier_for_rust_slice() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path().join("workspace");
        std::fs::create_dir_all(workspace.join("chapters")).expect("chapters");
        let workspace = workspace.canonicalize().expect("canonical workspace");
        let payload: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "create a medium chapter",
            "activeFile": "",
            "reasoningEffort": "low",
            "storyGeneration": {
                "fragmentCount": 1,
                "chapterLengthTier": "medium",
                "chapterTemplateId": "default_chapter_directory"
            },
            "capabilityMode": "scoped_write",
            "writesAllowed": true,
            "coreWritesAllowed": true,
            "allowedWriteRoots": ["chapters/"]
        }))
        .expect("request");

        let contract = build_turn_contract(&payload, &workspace).expect("turn contract");
        assert_eq!(contract["intentFrame"]["operationType"], "create_new");
        assert_eq!(contract["turnPlan"]["chapterLengthTier"], "medium");
        assert_eq!(contract["turnPlan"]["wordCountPolicy"]["mode"], "tier");
        assert_eq!(contract["storyGeneration"]["chapterLengthTier"], "medium");
    }

    #[test]
    fn create_new_multi_fragment_turn_contract_publishes_authoritative_targets() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path().join("workspace");
        std::fs::create_dir_all(workspace.join("chapters")).expect("chapters");
        let workspace = workspace.canonicalize().expect("canonical workspace");
        let payload: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "create one short chapter across three fragment files",
            "activeFile": "",
            "reasoningEffort": "low",
            "storyGeneration": {
                "fragmentCount": 3,
                "chapterLengthTier": "short",
                "chapterTemplateId": "default_chapter_directory"
            },
            "capabilityMode": "scoped_write",
            "writesAllowed": true,
            "coreWritesAllowed": true,
            "allowedWriteRoots": ["chapters/"]
        }))
        .expect("request");

        let contract = build_turn_contract(&payload, &workspace).expect("turn contract");
        assert_eq!(contract["intentFrame"]["operationType"], "create_new");
        assert_eq!(contract["turnPlan"]["requestedFragmentCount"], 3);
        assert_eq!(contract["turnPlan"]["fragmentCount"], 3);
        assert_eq!(
            contract["turnPlan"]["authoritativeFragmentPaths"],
            json!([
                "chapters/第1章 未命名/001.md",
                "chapters/第1章 未命名/002.md",
                "chapters/第1章 未命名/003.md",
            ])
        );
        assert_eq!(
            contract["turnPlan"]["fragmentTargets"]
                .as_array()
                .map(Vec::len),
            Some(3)
        );
        assert_eq!(contract["turnPlan"]["chapterContentMode"], "multi_fragment");
        assert_eq!(
            contract["turnPlan"]["chapterPlanValidation"]["passed"],
            true
        );
        assert_eq!(
            contract["turnPlan"]["wordCountPolicy"]["scope"],
            "candidate"
        );
    }
}
