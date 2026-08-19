//! Non-streaming Agent/Coomi control-plane routes.
//!
//! The streaming route remains the single execution implementation.  This
//! module only adapts the existing stream and the on-disk contracts to the
//! small control endpoints consumed by the frontend.

#![allow(clippy::result_large_err)]

use crate::chat::{self, ChatStreamRequest, RuntimeSessionBinding};
use crate::replacement;
use crate::workspace;
use crate::{AppState, error_response};
use axum::Json;
use axum::body::to_bytes;
use axum::extract::{Path as AxumPath, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use chrono::Utc;
use coomi_engine::Session;
use coomi_services::{ProviderDocument, ProviderRegistry};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::cmp::Ordering;
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use uuid::Uuid;

const MAX_STREAM_BYTES: usize = 16 * 1024 * 1024;
const MAX_MODEL_RESPONSE_BYTES: usize = 2 * 1024 * 1024;
const MAX_CONFIG_BYTES: usize = 4 * 1024 * 1024;
const MAX_CONTROL_SNAPSHOT_BYTES: usize = 64 * 1024 * 1024;

static CONTROL_MUTATION_LOCK: Mutex<()> = Mutex::new(());

struct SnapshotFile {
    relative_path: PathBuf,
    bytes: Vec<u8>,
}

enum SnapshotValue {
    Missing,
    File(Vec<u8>),
    Directory {
        directories: Vec<PathBuf>,
        files: Vec<SnapshotFile>,
    },
}

struct PathSnapshot {
    path: PathBuf,
    value: SnapshotValue,
}

impl PathSnapshot {
    fn capture(path: PathBuf) -> anyhow::Result<Self> {
        let metadata = match fs::symlink_metadata(&path) {
            Ok(value) => value,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Ok(Self {
                    path,
                    value: SnapshotValue::Missing,
                });
            }
            Err(error) => return Err(error.into()),
        };
        anyhow::ensure!(
            !metadata.file_type().is_symlink(),
            "control transaction target cannot be a symbolic link: {}",
            path.display()
        );
        let value = if metadata.is_file() {
            let bytes = fs::read(&path)?;
            anyhow::ensure!(
                bytes.len() <= MAX_CONTROL_SNAPSHOT_BYTES,
                "control transaction snapshot is too large: {}",
                path.display()
            );
            SnapshotValue::File(bytes)
        } else if metadata.is_dir() {
            let mut directories = Vec::new();
            let mut files = Vec::new();
            let mut total_bytes = 0usize;
            capture_directory(&path, &path, &mut directories, &mut files, &mut total_bytes)?;
            SnapshotValue::Directory { directories, files }
        } else {
            anyhow::bail!(
                "control transaction target must be a regular file or directory: {}",
                path.display()
            );
        };
        Ok(Self { path, value })
    }

    fn restore(&self) -> anyhow::Result<()> {
        remove_snapshot_target(&self.path)?;
        match &self.value {
            SnapshotValue::Missing => Ok(()),
            SnapshotValue::File(bytes) => {
                let parent = self
                    .path
                    .parent()
                    .ok_or_else(|| anyhow::anyhow!("snapshot file has no parent"))?;
                fs::create_dir_all(parent)?;
                workspace::atomic_write(&self.path, bytes)?;
                Ok(())
            }
            SnapshotValue::Directory { directories, files } => {
                fs::create_dir_all(&self.path)?;
                for directory in directories {
                    fs::create_dir_all(self.path.join(directory))?;
                }
                for file in files {
                    let target = self.path.join(&file.relative_path);
                    let parent = target
                        .parent()
                        .ok_or_else(|| anyhow::anyhow!("snapshot entry has no parent"))?;
                    fs::create_dir_all(parent)?;
                    workspace::atomic_write(&target, &file.bytes)?;
                }
                Ok(())
            }
        }
    }
}

fn capture_directory(
    root: &Path,
    current: &Path,
    directories: &mut Vec<PathBuf>,
    files: &mut Vec<SnapshotFile>,
    total_bytes: &mut usize,
) -> anyhow::Result<()> {
    for entry in fs::read_dir(current)? {
        let entry = entry?;
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path)?;
        anyhow::ensure!(
            !metadata.file_type().is_symlink(),
            "control transaction directory contains a symbolic link: {}",
            path.display()
        );
        let relative = path
            .strip_prefix(root)
            .map_err(|_| anyhow::anyhow!("snapshot entry escaped its root"))?
            .to_path_buf();
        if metadata.is_dir() {
            directories.push(relative);
            capture_directory(root, &path, directories, files, total_bytes)?;
        } else if metadata.is_file() {
            let bytes = fs::read(&path)?;
            *total_bytes = total_bytes.saturating_add(bytes.len());
            anyhow::ensure!(
                *total_bytes <= MAX_CONTROL_SNAPSHOT_BYTES,
                "control transaction directory snapshot is too large: {}",
                root.display()
            );
            files.push(SnapshotFile {
                relative_path: relative,
                bytes,
            });
        } else {
            anyhow::bail!(
                "control transaction directory contains a non-regular entry: {}",
                path.display()
            );
        }
    }
    Ok(())
}

fn remove_snapshot_target(path: &Path) -> anyhow::Result<()> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error.into()),
    };
    anyhow::ensure!(
        path.parent().is_some() && path.components().count() > 2,
        "unsafe control transaction restore target: {}",
        path.display()
    );
    if metadata.is_dir() && !metadata.file_type().is_symlink() {
        fs::remove_dir_all(path)?;
    } else {
        fs::remove_file(path)?;
    }
    Ok(())
}

fn restore_snapshots(snapshots: &[PathSnapshot]) -> anyhow::Result<()> {
    let mut failures = Vec::new();
    for snapshot in snapshots.iter().rev() {
        if let Err(error) = snapshot.restore() {
            failures.push(format!("{}: {error:#}", snapshot.path.display()));
        }
    }
    anyhow::ensure!(
        failures.is_empty(),
        "control transaction rollback failed: {}",
        failures.join("; ")
    );
    Ok(())
}

fn mutation_failed(
    code: &'static str,
    message: impl Into<String>,
    snapshots: &[PathSnapshot],
) -> Response {
    let message = message.into();
    match restore_snapshots(snapshots) {
        Ok(()) => failed(StatusCode::UNPROCESSABLE_ENTITY, code, message),
        Err(rollback_error) => failed(
            StatusCode::INTERNAL_SERVER_ERROR,
            "control_transaction_rollback_failed",
            format!("{message}; rollback error: {rollback_error:#}"),
        ),
    }
}

fn capture_snapshots(
    paths: impl IntoIterator<Item = PathBuf>,
) -> anyhow::Result<Vec<PathSnapshot>> {
    let mut seen = std::collections::HashSet::new();
    let mut snapshots = Vec::new();
    for path in paths {
        if seen.insert(path.clone()) {
            snapshots.push(PathSnapshot::capture(path)?);
        }
    }
    Ok(snapshots)
}

#[derive(Debug, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub(crate) struct SessionQuery {
    #[serde(default)]
    session_id: String,
}

#[derive(Debug, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub(crate) struct WorkspaceQuery {
    #[serde(default)]
    workspace_root: String,
    #[serde(default)]
    session_id: String,
    #[serde(default)]
    limit: Option<usize>,
}

#[derive(Debug, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ConfigUpdate {
    content: String,
}

#[derive(Debug, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ModelRequest {
    base_url: String,
    api_key: String,
    #[serde(default)]
    provider_type: String,
}

#[derive(Debug, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub(crate) struct PermissionRequest {
    permission_mode: String,
    #[serde(default)]
    workspace_root: String,
}

#[derive(Debug, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub(crate) struct PlanModeRequest {
    #[serde(default = "default_session")]
    session_id: String,
    active: bool,
    #[serde(default)]
    workspace_root: String,
}

#[derive(Debug, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub(crate) struct RollbackRequest {
    #[serde(default = "default_session")]
    session_id: String,
    #[serde(default)]
    expected_trace_id: String,
    #[serde(default)]
    workspace_root: String,
}

#[derive(Debug, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub(crate) struct CommitRequest {
    mode: String,
    #[serde(default)]
    message: String,
    #[serde(default)]
    session_id: String,
}

#[derive(Debug, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DeleteSessionRequest {
    session_id: String,
    #[serde(default)]
    workspace_root: String,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeControlConfig {
    #[serde(default = "default_permission")]
    permission_mode: String,
    #[serde(default)]
    plan_modes: BTreeMap<String, bool>,
}

impl Default for RuntimeControlConfig {
    fn default() -> Self {
        Self {
            permission_mode: default_permission(),
            plan_modes: BTreeMap::new(),
        }
    }
}

fn default_session() -> String {
    "default".to_owned()
}

fn default_permission() -> String {
    "ask_approval".to_owned()
}

fn normalized_session(value: &str) -> String {
    let value = value.trim();
    if value.is_empty() {
        "default".to_owned()
    } else {
        value.to_owned()
    }
}

fn permission_label(mode: &str) -> &'static str {
    match mode {
        "approve_for_me" => "Approve for me",
        "full_access" => "Full access",
        "plan_mode" => "Plan mode",
        _ => "Ask approval",
    }
}

fn validate_permission(mode: &str) -> bool {
    matches!(mode, "ask_approval" | "approve_for_me" | "full_access")
}

fn has_explicit_write_policy(payload: &ChatStreamRequest) -> bool {
    payload.capability_mode != "read_only"
        || payload.writes_allowed
        || payload.core_writes_allowed.is_some()
        || !payload.allowed_write_roots.is_empty()
}

fn clause_has_broad_no_write(clause: &str) -> bool {
    let normalized = clause.to_ascii_lowercase();
    [
        "只读",
        "不要修改任何",
        "不得修改任何",
        "不要写入任何",
        "不得写入任何",
        "不要改动任何",
        "不得改动任何",
        "read-only",
        "read only",
        "do not modify any",
        "don't modify any",
        "do not write any",
        "don't write any",
        "never modify any",
        "never write any",
    ]
    .iter()
    .any(|signal| normalized.contains(signal))
}

const WRITE_INTENT_SIGNALS: &[&str] = &[
    "修改",
    "更新",
    "调整",
    "重写",
    "改写",
    "续写",
    "创建",
    "新增",
    "生成",
    "删除",
    "整理",
    "同步",
    "实现",
    "修复",
    "写入",
    "保存",
    "创作",
    "edit",
    "update",
    "adjust",
    "rewrite",
    "continue writing",
    "create",
    "add",
    "generate",
    "delete",
    "organize",
    "sync",
    "implement",
    "fix",
    "write",
    "save",
];

fn strip_directive_prefix(mut value: &str) -> &str {
    loop {
        let before = value;
        for prefix in [
            "请",
            "帮我",
            "替我",
            "直接",
            "立即",
            "现在",
            "please",
            "can you",
            "could you",
        ] {
            if let Some(rest) = value.strip_prefix(prefix) {
                value = rest.trim_start();
                break;
            }
        }
        if value.len() == before.len() {
            return value;
        }
    }
}

fn starts_with_write_intent(value: &str) -> bool {
    let value = strip_directive_prefix(value.trim_start());
    WRITE_INTENT_SIGNALS
        .iter()
        .any(|signal| value.starts_with(signal))
}

fn has_direct_write_directive(value: &str) -> bool {
    if starts_with_write_intent(value) {
        return true;
    }
    [
        "并", "然后", "再", "同时", "接着", "而是", " and ", " then ", " but ",
    ]
    .iter()
    .filter_map(|connector| value.rsplit_once(connector).map(|(_, suffix)| suffix))
    .any(starts_with_write_intent)
}

fn last_signal_position(value: &str, signals: &[&str]) -> Option<usize> {
    signals
        .iter()
        .filter_map(|signal| value.rfind(signal))
        .max()
}

fn clause_has_positive_write(clause: &str) -> bool {
    let normalized = clause.to_ascii_lowercase();
    if clause_has_broad_no_write(&normalized) {
        return false;
    }
    if !WRITE_INTENT_SIGNALS
        .iter()
        .any(|signal| normalized.contains(signal))
    {
        return false;
    }
    let last_negation = last_signal_position(
        &normalized,
        &[
            "不要", "不得", "禁止", "请勿", "无需", "无须", "不用", "不必", "避免", "切勿", "别",
            "do not", "don't", "must not", "never",
        ],
    );
    let last_reset = last_signal_position(
        &normalized,
        &["但", "但是", "不过", "而是", " instead ", " but "],
    );
    if last_negation.is_some_and(|negation| last_reset.is_none_or(|reset| reset < negation)) {
        return false;
    }
    let advisory = [
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
    .any(|signal| normalized.contains(signal));
    if advisory && !has_direct_write_directive(&normalized) {
        return false;
    }
    true
}

fn extract_model_ids(payload: &Value) -> Vec<String> {
    let values = match payload {
        Value::Array(values) => values.as_slice(),
        Value::Object(object) => object
            .get("data")
            .or_else(|| object.get("models"))
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or_default(),
        _ => &[],
    };
    let mut models = values
        .iter()
        .filter_map(|item| match item {
            Value::String(value) => Some(value.as_str()),
            Value::Object(object) => object
                .get("id")
                .or_else(|| object.get("name"))
                .and_then(Value::as_str),
            _ => None,
        })
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(|value| value.strip_prefix("models/").unwrap_or(value).to_owned())
        .filter(|value| !value.is_empty())
        .collect::<Vec<_>>();
    models.sort_by(|left, right| {
        let folded = left.to_lowercase().cmp(&right.to_lowercase());
        if folded == Ordering::Equal {
            left.cmp(right)
        } else {
            folded
        }
    });
    models.dedup();
    models
}

pub(crate) fn inferred_write_intent(prompt: &str) -> bool {
    prompt
        .split(['，', ',', '。', '！', '!', '？', '?', '；', ';', '\n'])
        .any(clause_has_positive_write)
}

fn apply_inferred_capability(payload: &mut ChatStreamRequest) {
    if has_explicit_write_policy(payload) || !inferred_write_intent(&payload.prompt) {
        return;
    }
    payload.capability_mode = "workspace_write".to_owned();
    payload.writes_allowed = true;
    payload.core_writes_allowed = Some(true);
    payload.allowed_write_roots.clear();
}

fn control_config_path(state: &AppState) -> PathBuf {
    crate::system::global_root(state)
        .join("config")
        .join("coomi-runtime.json")
}

fn control_key(workspace: &Path, session_id: &str) -> String {
    format!(
        "{}::{}",
        workspace.display(),
        normalized_session(session_id)
    )
}

#[derive(Clone, Debug, Default)]
struct RuntimeUsageLedger {
    cumulative_tokens: u64,
    runtime_session_id: String,
    runtime_total_tokens: u64,
}

struct RuntimeUsageUpdate {
    path: PathBuf,
    bytes: Option<Vec<u8>>,
    cumulative_tokens: u64,
}

fn usage_ledger_path(workspace: &Path, session_id: &str) -> PathBuf {
    let normalized = normalized_session(session_id);
    let digest = format!("{:x}", Sha256::digest(normalized.as_bytes()));
    workspace
        .join(".storydex")
        .join(".agent")
        .join("runtime")
        .join("coomi-usage")
        .join(format!("{}.json", &digest[..24]))
}

fn nonnegative_u64(value: Option<&Value>) -> u64 {
    match value {
        Some(Value::Number(value)) => value.as_u64().unwrap_or_default(),
        Some(Value::String(value)) => value.trim().parse::<u64>().unwrap_or_default(),
        _ => 0,
    }
}

fn load_usage_ledger(workspace: &Path, session_id: &str) -> anyhow::Result<RuntimeUsageLedger> {
    let path = usage_ledger_path(workspace, session_id);
    let metadata = match fs::symlink_metadata(&path) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(RuntimeUsageLedger::default());
        }
        Err(error) => return Err(error.into()),
    };
    anyhow::ensure!(
        !metadata.file_type().is_symlink() && metadata.is_file(),
        "runtime usage ledger must be a regular file"
    );
    let bytes = fs::read(&path)?;
    anyhow::ensure!(
        bytes.len() <= MAX_CONFIG_BYTES,
        "runtime usage ledger is too large"
    );
    let Ok(Value::Object(value)) = serde_json::from_slice::<Value>(&bytes) else {
        return Ok(RuntimeUsageLedger::default());
    };
    if let Some(raw_workspace) = value
        .get("workspaceRoot")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        let recorded = PathBuf::from(raw_workspace).canonicalize()?;
        anyhow::ensure!(
            recorded == workspace,
            "runtime usage ledger workspace mismatch"
        );
    }
    if let Some(recorded_session) = value
        .get("storydexSessionId")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        anyhow::ensure!(
            recorded_session == normalized_session(session_id),
            "runtime usage ledger session mismatch"
        );
    }
    Ok(RuntimeUsageLedger {
        cumulative_tokens: nonnegative_u64(value.get("cumulativeTokens")),
        runtime_session_id: value
            .get("runtimeSessionId")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .trim()
            .to_owned(),
        runtime_total_tokens: nonnegative_u64(value.get("runtimeTotalTokens")),
    })
}

fn prepare_usage_update(
    workspace: &Path,
    session_id: &str,
    runtime: Option<(Uuid, u64)>,
) -> anyhow::Result<RuntimeUsageUpdate> {
    let path = usage_ledger_path(workspace, session_id);
    let ledger = load_usage_ledger(workspace, session_id)?;
    let Some((runtime_id, runtime_total)) = runtime else {
        return Ok(RuntimeUsageUpdate {
            path,
            bytes: None,
            cumulative_tokens: ledger.cumulative_tokens,
        });
    };
    let runtime_id = runtime_id.to_string();
    let cumulative_tokens = if ledger.runtime_session_id == runtime_id {
        ledger
            .cumulative_tokens
            .saturating_add(runtime_total.saturating_sub(ledger.runtime_total_tokens))
    } else {
        ledger.cumulative_tokens.saturating_add(runtime_total)
    };
    let changed = ledger.cumulative_tokens != cumulative_tokens
        || ledger.runtime_session_id != runtime_id
        || ledger.runtime_total_tokens != runtime_total;
    let bytes = if changed {
        let mut bytes = serde_json::to_vec_pretty(&json!({
            "version": 1,
            "workspaceRoot": workspace,
            "storydexSessionId": normalized_session(session_id),
            "cumulativeTokens": cumulative_tokens,
            "runtimeSessionId": runtime_id,
            "runtimeTotalTokens": runtime_total,
            "updatedAt": Utc::now().to_rfc3339(),
        }))?;
        bytes.push(b'\n');
        Some(bytes)
    } else {
        None
    };
    Ok(RuntimeUsageUpdate {
        path,
        bytes,
        cumulative_tokens,
    })
}

fn apply_usage_update(update: &RuntimeUsageUpdate) -> anyhow::Result<()> {
    if let Some(bytes) = &update.bytes {
        workspace::atomic_write(&update.path, bytes)?;
    }
    Ok(())
}

fn load_control_config(state: &AppState) -> anyhow::Result<RuntimeControlConfig> {
    let path = control_config_path(state);
    if !path.exists() {
        return Ok(RuntimeControlConfig::default());
    }
    let metadata = fs::symlink_metadata(&path)?;
    anyhow::ensure!(!metadata.file_type().is_symlink() && metadata.is_file());
    let bytes = fs::read(&path)?;
    anyhow::ensure!(
        bytes.len() <= MAX_CONFIG_BYTES,
        "runtime control config is too large"
    );
    let value: RuntimeControlConfig = serde_json::from_slice(&bytes)
        .map_err(|error| anyhow::anyhow!("invalid runtime control config: {error}"))?;
    anyhow::ensure!(validate_permission(&value.permission_mode));
    Ok(value)
}

fn save_control_config(state: &AppState, config: &RuntimeControlConfig) -> anyhow::Result<()> {
    anyhow::ensure!(validate_permission(&config.permission_mode));
    let path = control_config_path(state);
    let bytes = serde_json::to_vec_pretty(config)?;
    let parent = path
        .parent()
        .ok_or_else(|| anyhow::anyhow!("runtime control config has no parent"))?;
    fs::create_dir_all(parent)?;
    workspace::atomic_write(&path, &bytes)?;
    Ok(())
}

fn resolve_workspace(state: &AppState, requested: &str) -> Result<PathBuf, Response> {
    workspace::resolve_workspace_for_request(state, requested)
}

fn success(data: Value, audit: Vec<Value>, started: Instant) -> Response {
    let trace_id = Uuid::new_v4().to_string();
    Json(json!({
        "ok": true,
        "data": data,
        "error": Value::Null,
        "trace": {
            "traceId": trace_id,
            "durationMs": started.elapsed().as_millis(),
            "toolCalls": 0,
            "llmCalls": 0,
        },
        "audit": audit,
    }))
    .into_response()
}

fn failed(status: StatusCode, code: &str, message: impl Into<String>) -> Response {
    error_response(status, code, &message.into())
}

/// Apply the persisted permission/plan overlay before the streaming handler
/// validates and executes a turn.
pub(crate) fn apply_chat_policy(
    state: &AppState,
    workspace: &Path,
    session_id: &str,
    payload: &mut ChatStreamRequest,
) -> Result<(), ControlError> {
    let config =
        load_control_config(state).map_err(|error| ControlError::config(error.to_string()))?;
    let key = control_key(workspace, session_id);
    if config.plan_modes.get(&key).copied().unwrap_or(false) {
        payload.permission_mode = "plan_mode".to_owned();
        payload.capability_mode = "read_only".to_owned();
        payload.writes_allowed = false;
        payload.core_writes_allowed = Some(false);
        payload.allowed_write_roots.clear();
    } else {
        payload.permission_mode = config.permission_mode;
        apply_inferred_capability(payload);
    }
    Ok(())
}

#[derive(Debug)]
pub(crate) struct ControlError {
    pub(crate) status: StatusCode,
    pub(crate) code: &'static str,
    pub(crate) message: String,
}

impl ControlError {
    fn config(message: String) -> Self {
        Self {
            status: StatusCode::UNPROCESSABLE_ENTITY,
            code: "coomi_runtime_config_invalid",
            message,
        }
    }
}

pub(crate) async fn chat(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<SessionQuery>,
    Json(mut payload): Json<ChatStreamRequest>,
) -> Response {
    let started = Instant::now();
    let session_id = normalized_session(&query.session_id);
    let workspace = match resolve_workspace(&state, &payload.workspace_root) {
        Ok(path) => path,
        Err(response) => return response,
    };
    if let Err(error) = apply_chat_policy(&state, &workspace, &session_id, &mut payload) {
        return failed(error.status, error.code, error.message);
    }
    let mut forwarded = headers;
    if let Ok(value) = session_id.parse() {
        forwarded.insert("x-session-id", value);
    }
    let response = chat::chat_stream(State(state.clone()), forwarded, Ok(Json(payload))).await;
    if !response.status().is_success() {
        return response;
    }
    let body = match to_bytes(response.into_body(), MAX_STREAM_BYTES).await {
        Ok(body) => body,
        Err(error) => {
            return failed(
                StatusCode::BAD_GATEWAY,
                "stream_read_failed",
                error.to_string(),
            );
        }
    };
    match aggregate_sse(&body) {
        Ok((reply, events, terminal)) => {
            let (provider, model) = match provider_identity(&state) {
                Ok(value) => value,
                Err(error) => {
                    return failed(
                        StatusCode::SERVICE_UNAVAILABLE,
                        "provider_config_unavailable",
                        error.to_string(),
                    );
                }
            };
            success(
                json!({
                    "route": "coomi",
                    "reply": reply,
                    "llmModel": model,
                    "llmProvider": provider,
                    "events": events,
                    "assistant": terminal,
                }),
                vec![json!({"action": "agent_chat", "runtime": "coomi", "sessionId": session_id})],
                started,
            )
        }
        Err(error) => failed(error.status, error.code, error.message),
    }
}

#[derive(Debug)]
struct AggregationError {
    status: StatusCode,
    code: &'static str,
    message: String,
}

fn aggregate_sse(bytes: &[u8]) -> Result<(String, Vec<Value>, Value), AggregationError> {
    let text = String::from_utf8(bytes.to_vec()).map_err(|error| AggregationError {
        status: StatusCode::BAD_GATEWAY,
        code: "stream_invalid_utf8",
        message: error.to_string(),
    })?;
    let text = text.replace("\r\n", "\n").replace('\r', "\n");
    let mut events = Vec::new();
    let mut reply = String::new();
    let mut terminal: Option<Value> = None;
    let mut done_seen = false;
    for frame in text.split("\n\n").filter(|frame| !frame.trim().is_empty()) {
        if done_seen {
            return Err(AggregationError {
                status: StatusCode::BAD_GATEWAY,
                code: "stream_event_after_done",
                message: "Agent stream contained an event after the done marker.".to_owned(),
            });
        }
        let mut event_name: Option<String> = None;
        let mut data_lines = Vec::new();
        for line in frame.lines() {
            if line.starts_with(':') || line.is_empty() {
                continue;
            }
            if let Some(value) = line.strip_prefix("event:") {
                if event_name.is_some() {
                    return Err(AggregationError {
                        status: StatusCode::BAD_GATEWAY,
                        code: "stream_invalid_frame",
                        message: "Agent stream frame contained multiple event fields.".to_owned(),
                    });
                }
                event_name = Some(value.trim().to_owned());
            } else if let Some(value) = line.strip_prefix("data:") {
                data_lines.push(value.trim_start());
            }
        }
        let event_name = event_name
            .filter(|value| !value.is_empty())
            .ok_or_else(|| AggregationError {
                status: StatusCode::BAD_GATEWAY,
                code: "stream_invalid_frame",
                message: "Agent stream frame has no event name.".to_owned(),
            })?;
        if data_lines.is_empty() {
            return Err(AggregationError {
                status: StatusCode::BAD_GATEWAY,
                code: "stream_invalid_frame",
                message: format!("Agent stream event {event_name} has no data field."),
            });
        }
        let data = data_lines.join("\n");
        let value: Value = serde_json::from_str(&data).map_err(|error| AggregationError {
            status: StatusCode::BAD_GATEWAY,
            code: "stream_invalid_event",
            message: format!("invalid SSE event {event_name}: {error}"),
        })?;
        if !value.is_object() {
            return Err(AggregationError {
                status: StatusCode::BAD_GATEWAY,
                code: "stream_invalid_event",
                message: format!("Agent stream event {event_name} must contain a JSON object."),
            });
        }
        if event_name == "done" {
            if terminal.is_none() {
                return Err(AggregationError {
                    status: StatusCode::BAD_GATEWAY,
                    code: "stream_done_before_terminal",
                    message: "Agent stream emitted done before its terminal event.".to_owned(),
                });
            }
            done_seen = true;
            continue;
        }
        if terminal.is_some() {
            return Err(AggregationError {
                status: StatusCode::BAD_GATEWAY,
                code: "stream_event_after_terminal",
                message: format!("Agent stream emitted {event_name} after its terminal event."),
            });
        }
        if event_name == "TextChunk"
            && let Some(content) = value.get("content").and_then(Value::as_str)
        {
            reply.push_str(content);
        }
        if matches!(
            event_name.as_str(),
            "AgentCompleted" | "AgentCancelled" | "AgentError"
        ) {
            if terminal.is_some() {
                return Err(AggregationError {
                    status: StatusCode::BAD_GATEWAY,
                    code: "stream_multiple_terminals",
                    message: "Agent stream contained more than one terminal event.".to_owned(),
                });
            }
            terminal = Some(value.clone());
        }
        events.push(trace_event(&event_name, value, events.len() + 1));
    }
    if !done_seen || terminal.is_none() {
        return Err(AggregationError {
            status: StatusCode::BAD_GATEWAY,
            code: "stream_incomplete",
            message: "Agent stream did not contain exactly one terminal event and done marker."
                .to_owned(),
        });
    }
    Ok((reply, events, terminal.unwrap_or_else(|| json!({}))))
}

fn trace_event(event_name: &str, data: Value, index: usize) -> Value {
    json!({
        "index": index,
        "event": event_name,
        "phase": trace_phase(event_name),
        "status": trace_status(event_name, &data),
        "detail": trace_detail(event_name, &data),
        "timestamp": Utc::now().to_rfc3339(),
        "data": data,
    })
}

fn trace_phase(event_name: &str) -> &'static str {
    if event_name.starts_with("Tool") {
        "tool"
    } else if matches!(
        event_name,
        "TextChunk" | "ReasoningChunk" | "ConnectionRetry" | "ModelCompleted"
    ) {
        "model"
    } else if matches!(
        event_name,
        "GitAutoCommit" | "GitCommitPrompt" | "GitCommitResult" | "GitCommitSkipped"
    ) {
        "version_control"
    } else if event_name.starts_with("Task") {
        "planning"
    } else if matches!(event_name, "TurnContract" | "StoryGenerationValidation") {
        "orchestration"
    } else if event_name.starts_with("Agent") {
        "agent"
    } else {
        "runtime"
    }
}

fn trace_status(event_name: &str, data: &Value) -> String {
    if event_name == "AgentError" || data.get("is_error").and_then(Value::as_bool) == Some(true) {
        return "error".to_owned();
    }
    match event_name {
        "RunAccepted" | "TaskStarted" => "running",
        "TaskCompleted" | "TaskPlanCreated" | "TaskPlanUpdated" | "ModelCompleted"
        | "AgentCompleted" | "ToolDone" => "success",
        "TaskFailed" => "error",
        "TaskSkipped" | "AgentCancelled" | "ConnectionRetry" => "warning",
        "TurnContract"
            if data.get("status").and_then(Value::as_str) == Some("needs_user_input") =>
        {
            "warning"
        }
        "StoryGenerationValidation" => {
            if data.get("passed").and_then(Value::as_bool) == Some(true) {
                data.get("status")
                    .and_then(Value::as_str)
                    .filter(|value| *value == "warning")
                    .unwrap_or("success")
            } else {
                "error"
            }
        }
        _ => data.get("status").and_then(Value::as_str).unwrap_or("info"),
    }
    .to_owned()
}

fn trace_detail(event_name: &str, data: &Value) -> String {
    if event_name.starts_with("Task") {
        return data
            .get("title")
            .or_else(|| data.get("detail"))
            .and_then(Value::as_str)
            .unwrap_or(event_name)
            .to_owned();
    }
    if event_name.starts_with("Tool") {
        return data
            .get("tool_name")
            .and_then(Value::as_str)
            .unwrap_or(event_name)
            .to_owned();
    }
    match event_name {
        "TextChunk" | "ReasoningChunk" => data
            .get("content")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .chars()
            .take(240)
            .collect(),
        "AgentError" => data
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("Coomi Agent error")
            .to_owned(),
        "RunAccepted" | "TurnPhase" => data
            .get("detail")
            .or_else(|| data.get("label"))
            .and_then(Value::as_str)
            .unwrap_or(event_name)
            .to_owned(),
        "TurnContract" => data
            .get("intentFrame")
            .and_then(|value| value.get("primary"))
            .and_then(Value::as_str)
            .unwrap_or("Storydex turn contract")
            .to_owned(),
        "StoryGenerationValidation" => data
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("Storydex 正文客观验收")
            .to_owned(),
        _ => data
            .get("message")
            .or_else(|| data.get("detail"))
            .and_then(Value::as_str)
            .unwrap_or(event_name)
            .to_owned(),
    }
}

fn provider_identity(state: &AppState) -> anyhow::Result<(String, String)> {
    let path = state.coomi_home().join("config").join("providers.json");
    let registry = ProviderRegistry::load(&path)?;
    let provider = registry.resolve(None)?;
    Ok((provider.id, provider.model))
}

pub(crate) async fn read_config(State(state): State<AppState>) -> Response {
    let started = Instant::now();
    let path = state.coomi_home().join("config").join("providers.json");
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let default = b"{\n  \"version\": 1,\n  \"active\": \"\",\n  \"providers\": {}\n}\n";
            if let Some(parent) = path.parent()
                && let Err(error) =
                    fs::create_dir_all(parent).and_then(|_| fs::write(&path, default))
            {
                return failed(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "coomi_config_unavailable",
                    error.to_string(),
                );
            }
            default.to_vec()
        }
        Err(error) => {
            return failed(
                StatusCode::SERVICE_UNAVAILABLE,
                "coomi_config_unavailable",
                error.to_string(),
            );
        }
    };
    if bytes.len() > MAX_CONFIG_BYTES {
        return failed(
            StatusCode::UNPROCESSABLE_ENTITY,
            "coomi_config_invalid",
            "Coomi providers config is too large.",
        );
    }
    let content = String::from_utf8_lossy(&bytes).to_string();
    let parsed: Value = match serde_json::from_slice::<Value>(&bytes) {
        Ok(value) if value.is_object() => value,
        Ok(_) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "coomi_config_invalid",
                "Coomi providers config must be a JSON object.",
            );
        }
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "coomi_config_invalid",
                error.to_string(),
            );
        }
    };
    let updated_at = fs::metadata(&path)
        .and_then(|metadata| metadata.modified())
        .map(|time| chrono::DateTime::<Utc>::from(time).to_rfc3339())
        .unwrap_or_else(|_| Utc::now().to_rfc3339());
    success(
        json!({"configPath": path, "content": content, "parsed": parsed, "updatedAt": updated_at}),
        vec![json!({"action":"read_coomi_config"})],
        started,
    )
}

pub(crate) async fn update_config(
    State(state): State<AppState>,
    Json(payload): Json<ConfigUpdate>,
) -> Response {
    let value: Value = match serde_json::from_str::<Value>(&payload.content) {
        Ok(value) if value.is_object() => value,
        Ok(_) => {
            return failed(
                StatusCode::BAD_REQUEST,
                "coomi_config_invalid",
                "Coomi providers config must be a JSON object.",
            );
        }
        Err(error) => {
            return failed(
                StatusCode::BAD_REQUEST,
                "coomi_config_invalid",
                error.to_string(),
            );
        }
    };
    let document: ProviderDocument = match serde_json::from_value(value.clone()) {
        Ok(document) => document,
        Err(error) => {
            return failed(
                StatusCode::BAD_REQUEST,
                "coomi_config_invalid",
                error.to_string(),
            );
        }
    };
    if let Err(error) = document.validate() {
        return failed(
            StatusCode::BAD_REQUEST,
            "coomi_config_invalid",
            error.to_string(),
        );
    }
    let path = state.coomi_home().join("config").join("providers.json");
    let bytes = match serde_json::to_vec_pretty(&value) {
        Ok(mut bytes) => {
            bytes.push(b'\n');
            bytes
        }
        Err(error) => {
            return failed(
                StatusCode::BAD_REQUEST,
                "coomi_config_invalid",
                error.to_string(),
            );
        }
    };
    if let Some(parent) = path.parent()
        && let Err(error) =
            fs::create_dir_all(parent).and_then(|_| workspace::atomic_write(&path, &bytes))
    {
        return failed(
            StatusCode::SERVICE_UNAVAILABLE,
            "coomi_config_unavailable",
            error.to_string(),
        );
    }
    read_config(State(state)).await
}

fn model_endpoint(base_url: &str, provider_type: &str) -> Result<String, String> {
    let parsed = reqwest::Url::parse(base_url.trim()).map_err(|error| error.to_string())?;
    if !matches!(parsed.scheme(), "http" | "https") || parsed.host_str().is_none() {
        return Err("baseUrl must be an HTTP(S) URL".to_owned());
    }
    let mut path = parsed.path().trim_end_matches('/').to_owned();
    let lower = path.to_ascii_lowercase();
    for suffix in [
        "/chat/completions",
        "/completions",
        "/responses",
        "/messages",
    ] {
        if lower.ends_with(suffix) {
            path.truncate(path.len() - suffix.len());
            path.push_str("/models");
            return Ok(parsed
                .join(&path)
                .map_err(|error| error.to_string())?
                .to_string());
        }
    }
    if !lower.ends_with("/models") {
        if (provider_type.eq_ignore_ascii_case("anthropic")
            || provider_type.eq_ignore_ascii_case("anthropic_messages"))
            && !lower.ends_with("/v1")
        {
            path.push_str("/v1");
        }
        path.push_str("/models");
    }
    let mut endpoint = parsed;
    endpoint.set_path(&path);
    endpoint.set_query(None);
    Ok(endpoint.to_string())
}

pub(crate) async fn list_models(Json(payload): Json<ModelRequest>) -> Response {
    let started = Instant::now();
    let endpoint = match model_endpoint(&payload.base_url, &payload.provider_type) {
        Ok(endpoint) => endpoint,
        Err(error) => return failed(StatusCode::BAD_REQUEST, "coomi_models_unavailable", error),
    };
    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(20))
        .build()
    {
        Ok(client) => client,
        Err(error) => {
            return failed(
                StatusCode::SERVICE_UNAVAILABLE,
                "coomi_models_unavailable",
                error.to_string(),
            );
        }
    };
    let normalized = payload.provider_type.to_ascii_lowercase().replace('-', "_");
    let mut request = client.get(&endpoint).header("Accept", "application/json");
    if normalized == "gemini" || normalized == "gemini_native" {
        if !payload.api_key.is_empty() {
            request = request.header("x-goog-api-key", &payload.api_key);
        }
    } else if normalized == "anthropic" || normalized == "anthropic_messages" {
        if !payload.api_key.is_empty() {
            request = request.header("x-api-key", &payload.api_key);
        }
        request = request.header("anthropic-version", "2023-06-01");
    } else if !payload.api_key.is_empty() {
        request = request.bearer_auth(&payload.api_key);
    }
    let response = match request.send().await {
        Ok(response) => response,
        Err(error) => {
            return failed(
                StatusCode::BAD_GATEWAY,
                "coomi_models_unavailable",
                redact_secret(&error.to_string(), &payload.api_key),
            );
        }
    };
    let status = response.status();
    let bytes = match response.bytes().await {
        Ok(bytes) if bytes.len() <= MAX_MODEL_RESPONSE_BYTES => bytes,
        Ok(_) => {
            return failed(
                StatusCode::BAD_GATEWAY,
                "coomi_models_unavailable",
                "Model list response is too large.",
            );
        }
        Err(error) => {
            return failed(
                StatusCode::BAD_GATEWAY,
                "coomi_models_unavailable",
                redact_secret(&error.to_string(), &payload.api_key),
            );
        }
    };
    if !status.is_success() {
        return failed(
            StatusCode::BAD_GATEWAY,
            "coomi_models_unavailable",
            redact_secret(&String::from_utf8_lossy(&bytes), &payload.api_key),
        );
    }
    let value: Value = match serde_json::from_slice(&bytes) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::BAD_GATEWAY,
                "coomi_models_unavailable",
                error.to_string(),
            );
        }
    };
    let models = extract_model_ids(&value);
    success(
        json!({"endpoint": endpoint, "models": models}),
        vec![json!({"action":"fetch_coomi_models", "modelCount": models.len()})],
        started,
    )
}

fn redact_secret(value: &str, secret: &str) -> String {
    if secret.is_empty() {
        value.to_owned()
    } else {
        value.replace(secret, "***")
    }
}

fn workspace_session_from(
    state: &AppState,
    workspace_root: &str,
    session_id: &str,
) -> Result<(PathBuf, String), Response> {
    let workspace = resolve_workspace(state, workspace_root)?;
    Ok((workspace, normalized_session(session_id)))
}

fn reject_active_execution(
    state: &AppState,
    workspace: Option<&Path>,
    session_id: Option<&str>,
) -> Option<Response> {
    let active = state.execution_registry().active()?;
    let scope = match (workspace, session_id) {
        (Some(workspace), Some(session_id))
            if active.workspace_root == workspace
                && active.session_id == normalized_session(session_id) =>
        {
            "the requested session"
        }
        _ => "Storydex Agent",
    };
    Some(failed(
        StatusCode::CONFLICT,
        "execution_running",
        format!("Execution {} is still active for {scope}.", active.trace_id),
    ))
}

pub(crate) async fn cycle_permission(State(state): State<AppState>) -> Response {
    set_permission_inner(&state, "", "", true).await
}

pub(crate) async fn set_permission(
    State(state): State<AppState>,
    Json(payload): Json<PermissionRequest>,
) -> Response {
    set_permission_inner(
        &state,
        &payload.permission_mode,
        &payload.workspace_root,
        false,
    )
    .await
}

async fn set_permission_inner(
    state: &AppState,
    requested: &str,
    workspace_root: &str,
    cycle: bool,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(state, workspace_root) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let _mutation_guard = CONTROL_MUTATION_LOCK
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    let mut config = match load_control_config(state) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "coomi_runtime_config_invalid",
                error.to_string(),
            );
        }
    };
    let mode = if cycle {
        match config.permission_mode.as_str() {
            "ask_approval" => "approve_for_me",
            "approve_for_me" => "full_access",
            "full_access" => "ask_approval",
            _ => {
                return failed(
                    StatusCode::UNPROCESSABLE_ENTITY,
                    "coomi_runtime_config_invalid",
                    "Unsupported permission mode.",
                );
            }
        }
        .to_owned()
    } else {
        requested.trim().to_owned()
    };
    if !validate_permission(&mode) {
        return failed(
            StatusCode::BAD_REQUEST,
            "invalid_permission_mode",
            "permissionMode must be ask_approval, approve_for_me, or full_access.",
        );
    }
    config.permission_mode = mode.clone();
    if let Err(error) = save_control_config(state, &config) {
        return failed(
            StatusCode::UNPROCESSABLE_ENTITY,
            "coomi_runtime_config_invalid",
            error.to_string(),
        );
    }
    success(
        json!({"permissionMode": mode, "permissionLabel": permission_label(&mode), "workspaceRoot": workspace}),
        vec![json!({"action": if cycle {"cycle_coomi_permission"} else {"set_coomi_permission"}})],
        started,
    )
}

pub(crate) async fn set_plan_mode(
    State(state): State<AppState>,
    Json(payload): Json<PlanModeRequest>,
) -> Response {
    let started = Instant::now();
    let (workspace, session_id) =
        match workspace_session_from(&state, &payload.workspace_root, &payload.session_id) {
            Ok(value) => value,
            Err(response) => return response,
        };
    let _mutation_guard = CONTROL_MUTATION_LOCK
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    let mut config = match load_control_config(&state) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "coomi_runtime_config_invalid",
                error.to_string(),
            );
        }
    };
    config
        .plan_modes
        .insert(control_key(&workspace, &session_id), payload.active);
    if let Err(error) = save_control_config(&state, &config) {
        return failed(
            StatusCode::UNPROCESSABLE_ENTITY,
            "coomi_runtime_config_invalid",
            error.to_string(),
        );
    }
    let mode = if payload.active {
        "plan_mode"
    } else {
        config.permission_mode.as_str()
    };
    success(
        json!({"sessionId": session_id, "planMode": payload.active, "permissionMode": mode, "permissionLabel": permission_label(mode)}),
        vec![json!({"action":"set_coomi_plan_mode"})],
        started,
    )
}

pub(crate) async fn coomi_status(
    State(state): State<AppState>,
    Query(query): Query<SessionQuery>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, "") {
        Ok(value) => value,
        Err(response) => return response,
    };
    let session_id = normalized_session(&query.session_id);
    let _mutation_guard = CONTROL_MUTATION_LOCK
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    let config = match load_control_config(&state) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "coomi_runtime_config_invalid",
                error.to_string(),
            );
        }
    };
    let base = match super::read_coomi_status(state.coomi_home()) {
        Ok(value) => value,
        Err(error) => {
            tracing::error!(error = %error, "Storydex Coomi status could not be read");
            return failed(
                StatusCode::SERVICE_UNAVAILABLE,
                "provider_config_unavailable",
                "Storydex Coomi provider configuration is unavailable.",
            );
        }
    };
    let binding = match chat::load_runtime_session_binding(&state, &workspace, &session_id) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "session_unavailable",
                error.to_string(),
            );
        }
    };
    let runtime_session = if let Some(binding) = &binding {
        match read_bound_runtime_session(binding, &workspace) {
            Ok(Some(value)) => Some(value),
            Ok(None) => {
                return failed(
                    StatusCode::UNPROCESSABLE_ENTITY,
                    "session_unavailable",
                    "The bound Coomi runtime session is unavailable.",
                );
            }
            Err(error) => {
                return failed(
                    StatusCode::UNPROCESSABLE_ENTITY,
                    "session_unavailable",
                    error.to_string(),
                );
            }
        }
    } else {
        None
    };
    let runtime_usage = runtime_session
        .as_ref()
        .map(|session| (session.id, session.usage.total_tokens()));
    let usage_update = match prepare_usage_update(&workspace, &session_id, runtime_usage) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "session_unavailable",
                error.to_string(),
            );
        }
    };
    if let Err(error) = apply_usage_update(&usage_update) {
        return failed(
            StatusCode::UNPROCESSABLE_ENTITY,
            "session_unavailable",
            error.to_string(),
        );
    }
    let context = runtime_session
        .map(|session| session.context)
        .unwrap_or_default();
    let cumulative_tokens = usage_update.cumulative_tokens;
    let context_status = context.status(&base.provider_capabilities);
    let plan_mode = config
        .plan_modes
        .get(&control_key(&workspace, &session_id))
        .copied()
        .unwrap_or(false);
    let permission_mode = if plan_mode {
        "plan_mode"
    } else {
        config.permission_mode.as_str()
    };
    let mut data = match serde_json::to_value(base) {
        Ok(Value::Object(value)) => value,
        Ok(_) => {
            return failed(
                StatusCode::INTERNAL_SERVER_ERROR,
                "coomi_status_invalid",
                "Coomi status serialization produced an invalid payload.",
            );
        }
        Err(error) => {
            return failed(
                StatusCode::INTERNAL_SERVER_ERROR,
                "coomi_status_invalid",
                error.to_string(),
            );
        }
    };
    data.insert("permissionMode".into(), json!(permission_mode));
    data.insert(
        "permissionLabel".into(),
        json!(permission_label(permission_mode)),
    );
    data.insert("planMode".into(), json!(plan_mode));
    data.insert("toolCount".into(), json!(0));
    data.insert("contextWindow".into(), json!(context_status.context_window));
    data.insert("usedTokens".into(), json!(context_status.used_tokens));
    data.insert(
        "usageRatio".into(),
        json!(context_status.used_tokens as f64 / context_status.context_window.max(1) as f64),
    );
    data.insert("cumulativeTokens".into(), json!(cumulative_tokens));
    data.insert(
        "compactThreshold".into(),
        json!(context_status.auto_compact_token_limit),
    );
    data.insert(
        "warningThreshold".into(),
        json!(context_status.context_window.saturating_mul(3) / 5),
    );
    data.insert("compressionStatus".into(), json!("idle"));
    success(
        Value::Object(data),
        vec![json!({
            "action": "read_coomi_status",
            "sessionId": session_id,
            "toolCount": 0,
        })],
        started,
    )
}

pub(crate) async fn history(
    State(state): State<AppState>,
    Query(query): Query<WorkspaceQuery>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &query.workspace_root) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let session_id = normalized_session(&query.session_id);
    let records = match replacement::list_records(&workspace, &session_id) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "history_unavailable",
                error.to_string(),
            );
        }
    };
    let limit = query.limit.unwrap_or(40).clamp(1, 200);
    let items = records.into_iter().take(limit).collect::<Vec<_>>();
    success(
        json!({"items": items}),
        vec![json!({"action":"read_agent_history", "count": items.len()})],
        started,
    )
}

pub(crate) async fn sessions(State(state): State<AppState>) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, "") {
        Ok(value) => value,
        Err(response) => return response,
    };
    let items = match replacement::list_session_summaries(&workspace) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "history_unavailable",
                error.to_string(),
            );
        }
    };
    success(
        json!({"items": items}),
        vec![json!({"action":"read_agent_sessions", "count": items.len()})],
        started,
    )
}

fn preflight_runtime_binding(
    state: &AppState,
    workspace: &Path,
    session_id: &str,
) -> anyhow::Result<Option<RuntimeSessionBinding>> {
    let binding = chat::load_runtime_session_binding(state, workspace, session_id)?;
    if let Some(binding) = &binding {
        read_bound_runtime_session(binding, workspace)?;
    }
    Ok(binding)
}

fn read_bound_runtime_session(
    binding: &RuntimeSessionBinding,
    workspace: &Path,
) -> anyhow::Result<Option<Session>> {
    let metadata = match fs::symlink_metadata(&binding.session_path) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    anyhow::ensure!(
        !metadata.file_type().is_symlink() && metadata.is_file(),
        "bound Coomi runtime session must be a regular file"
    );
    let bytes = fs::read(&binding.session_path)?;
    let session: Session = serde_json::from_slice(&bytes)
        .map_err(|error| anyhow::anyhow!("invalid Coomi runtime session: {error}"))?;
    anyhow::ensure!(
        session.id == binding.runtime_id,
        "Coomi runtime session identity does not match its binding"
    );
    let session_workspace = session.cwd.canonicalize()?;
    anyhow::ensure!(
        session_workspace == workspace,
        "Coomi runtime session belongs to another workspace"
    );
    Ok(Some(session))
}

fn remove_regular_file_if_exists(path: &Path) -> anyhow::Result<()> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error.into()),
    };
    anyhow::ensure!(
        !metadata.file_type().is_symlink() && metadata.is_file(),
        "control transaction file must be a regular file: {}",
        path.display()
    );
    fs::remove_file(path)?;
    Ok(())
}

fn remove_runtime_binding(binding: Option<RuntimeSessionBinding>) -> anyhow::Result<()> {
    let Some(binding) = binding else {
        return Ok(());
    };
    remove_regular_file_if_exists(&binding.session_path)?;
    remove_regular_file_if_exists(&binding.binding_path)?;
    Ok(())
}

pub(crate) async fn clear_conversation(
    State(state): State<AppState>,
    Query(query): Query<SessionQuery>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, "") {
        Ok(value) => value,
        Err(response) => return response,
    };
    let session_id = normalized_session(&query.session_id);
    if let Some(response) = reject_active_execution(&state, Some(&workspace), Some(&session_id)) {
        return response;
    }
    let _mutation_guard = CONTROL_MUTATION_LOCK
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    if let Some(response) = reject_active_execution(&state, Some(&workspace), Some(&session_id)) {
        return response;
    }
    let binding = match preflight_runtime_binding(&state, &workspace, &session_id) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "session_unavailable",
                error.to_string(),
            );
        }
    };
    let runtime_session = match binding
        .as_ref()
        .map(|binding| read_bound_runtime_session(binding, &workspace))
        .transpose()
    {
        Ok(value) => value.flatten(),
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "session_unavailable",
                error.to_string(),
            );
        }
    };
    let usage_update = match prepare_usage_update(
        &workspace,
        &session_id,
        runtime_session
            .as_ref()
            .map(|session| (session.id, session.usage.total_tokens())),
    ) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "session_unavailable",
                error.to_string(),
            );
        }
    };
    let record_count = match replacement::list_records(&workspace, &session_id) {
        Ok(value) => value.len(),
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "history_unavailable",
                error.to_string(),
            );
        }
    };
    if let Err(error) = state.followup_store().list(&workspace, &session_id) {
        return failed(StatusCode::UNPROCESSABLE_ENTITY, error.code, error.message);
    }
    let mut config = match load_control_config(&state) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "coomi_runtime_config_invalid",
                error.to_string(),
            );
        }
    };
    config
        .plan_modes
        .remove(&control_key(&workspace, &session_id));
    let mut snapshot_paths = vec![
        replacement::session_directory(&workspace, &session_id),
        state.followup_store().storage_path(&workspace, &session_id),
        control_config_path(&state),
        usage_update.path.clone(),
    ];
    if let Some(binding) = &binding {
        snapshot_paths.push(binding.session_path.clone());
        snapshot_paths.push(binding.binding_path.clone());
    }
    let snapshots = match capture_snapshots(snapshot_paths) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "control_transaction_unavailable",
                error.to_string(),
            );
        }
    };
    if let Err(error) = apply_usage_update(&usage_update) {
        return mutation_failed("session_unavailable", error.to_string(), &snapshots);
    }
    let removed = match replacement::clear_records(&workspace, &session_id) {
        Ok(value) => value,
        Err(error) => {
            return mutation_failed("history_unavailable", error.to_string(), &snapshots);
        }
    };
    if removed != record_count {
        return mutation_failed(
            "history_changed",
            "Agent history changed while the conversation was being cleared.",
            &snapshots,
        );
    }
    if let Err(error) = state.followup_store().delete(&workspace, &session_id) {
        return mutation_failed(error.code, error.message, &snapshots);
    }
    if let Err(error) = remove_runtime_binding(binding) {
        return mutation_failed("session_unavailable", error.to_string(), &snapshots);
    }
    if let Err(error) = save_control_config(&state, &config) {
        return mutation_failed(
            "coomi_runtime_config_invalid",
            error.to_string(),
            &snapshots,
        );
    }
    success(
        json!({"cleared": true, "sessionId": session_id, "historyClearedCount": removed, "runtime": "coomi"}),
        vec![json!({"action":"clear_conversation"})],
        started,
    )
}

pub(crate) async fn delete_session(
    State(state): State<AppState>,
    Json(payload): Json<DeleteSessionRequest>,
) -> Response {
    let started = Instant::now();
    let workspace = match resolve_workspace(&state, &payload.workspace_root) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let session_id = normalized_session(&payload.session_id);
    if let Some(response) = reject_active_execution(&state, Some(&workspace), Some(&session_id)) {
        return response;
    }
    let _mutation_guard = CONTROL_MUTATION_LOCK
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    if let Some(response) = reject_active_execution(&state, Some(&workspace), Some(&session_id)) {
        return response;
    }
    let binding = match preflight_runtime_binding(&state, &workspace, &session_id) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "session_unavailable",
                error.to_string(),
            );
        }
    };
    let record_count = match replacement::list_records(&workspace, &session_id) {
        Ok(value) => value.len(),
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "history_unavailable",
                error.to_string(),
            );
        }
    };
    if let Err(error) = state.followup_store().list(&workspace, &session_id) {
        return failed(StatusCode::UNPROCESSABLE_ENTITY, error.code, error.message);
    }
    let mut config = match load_control_config(&state) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "coomi_runtime_config_invalid",
                error.to_string(),
            );
        }
    };
    config
        .plan_modes
        .remove(&control_key(&workspace, &session_id));
    let mut snapshot_paths = vec![
        replacement::session_directory(&workspace, &session_id),
        state.followup_store().storage_path(&workspace, &session_id),
        control_config_path(&state),
        usage_ledger_path(&workspace, &session_id),
    ];
    if let Some(binding) = &binding {
        snapshot_paths.push(binding.session_path.clone());
        snapshot_paths.push(binding.binding_path.clone());
    }
    let snapshots = match capture_snapshots(snapshot_paths) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "control_transaction_unavailable",
                error.to_string(),
            );
        }
    };
    if let Err(error) = replacement::delete_session(&workspace, &session_id) {
        return mutation_failed("history_unavailable", error.to_string(), &snapshots);
    }
    if let Err(error) = state.followup_store().delete(&workspace, &session_id) {
        return mutation_failed(error.code, error.message, &snapshots);
    }
    if let Err(error) = remove_runtime_binding(binding) {
        return mutation_failed("session_unavailable", error.to_string(), &snapshots);
    }
    if let Err(error) = remove_regular_file_if_exists(&usage_ledger_path(&workspace, &session_id)) {
        return mutation_failed("session_unavailable", error.to_string(), &snapshots);
    }
    if let Err(error) = save_control_config(&state, &config) {
        return mutation_failed(
            "coomi_runtime_config_invalid",
            error.to_string(),
            &snapshots,
        );
    }
    success(
        json!({"deleted": true, "sessionId": session_id, "removedCount": record_count, "runtime":"coomi"}),
        vec![json!({"action":"delete_agent_session"})],
        started,
    )
}

pub(crate) async fn rollback_latest(
    State(state): State<AppState>,
    Json(payload): Json<RollbackRequest>,
) -> Response {
    let started = Instant::now();
    let session_id = normalized_session(&payload.session_id);
    let workspace_hint = resolve_workspace(&state, &payload.workspace_root);
    let workspace = match workspace_hint {
        Ok(value) => value,
        Err(response) => return response,
    };
    if let Some(response) = reject_active_execution(&state, Some(&workspace), Some(&session_id)) {
        return response;
    }
    let _mutation_guard = CONTROL_MUTATION_LOCK
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    if let Some(response) = reject_active_execution(&state, Some(&workspace), Some(&session_id)) {
        return response;
    }
    let latest = match replacement::latest_record(&workspace, &session_id) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "history_unavailable",
                error.to_string(),
            );
        }
    };
    let Some(latest) = latest else {
        return success(
            json!({"rolledBack":false,"sessionId":session_id,"removedTraceId":"","prompt":""}),
            Vec::new(),
            started,
        );
    };
    let trace_id = latest
        .get("traceId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    if !payload.expected_trace_id.trim().is_empty() && payload.expected_trace_id.trim() != trace_id
    {
        return failed(
            StatusCode::CONFLICT,
            "stale_trace",
            "The latest execution changed before rollback was confirmed.",
        );
    }
    if latest
        .get("status")
        .and_then(Value::as_str)
        .is_some_and(|value| value.eq_ignore_ascii_case("running"))
    {
        return failed(
            StatusCode::CONFLICT,
            "execution_running",
            "A running execution cannot be rolled back.",
        );
    }
    let binding = match preflight_runtime_binding(&state, &workspace, &session_id) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "session_unavailable",
                error.to_string(),
            );
        }
    };
    let Some(binding) = binding else {
        return success(
            json!({"rolledBack":false,"sessionId":session_id,"removedTraceId":"","prompt":latest.get("prompt").and_then(Value::as_str).unwrap_or_default()}),
            Vec::new(),
            started,
        );
    };
    if !binding.session_path.exists() {
        return success(
            json!({"rolledBack":false,"sessionId":session_id,"removedTraceId":"","prompt":latest.get("prompt").and_then(Value::as_str).unwrap_or_default()}),
            Vec::new(),
            started,
        );
    }
    let original = match fs::read(&binding.session_path) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "session_unavailable",
                error.to_string(),
            );
        }
    };
    let mut value: Value = match serde_json::from_slice(&original) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "session_unavailable",
                error.to_string(),
            );
        }
    };
    let messages = match value.get_mut("messages").and_then(Value::as_array_mut) {
        Some(value) => value,
        None => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "session_unavailable",
                "Runtime session has no message history.",
            );
        }
    };
    let Some(index) = messages.iter().rposition(|message| {
        message.get("role").and_then(Value::as_str) == Some("user")
            && message.get("internal").and_then(Value::as_bool) != Some(true)
    }) else {
        return success(
            json!({"rolledBack":false,"sessionId":session_id,"removedTraceId":"","prompt":latest.get("prompt").and_then(Value::as_str).unwrap_or_default()}),
            Vec::new(),
            started,
        );
    };
    messages.truncate(index);
    if let Some(object) = value.as_object_mut() {
        object.insert("updated_at".into(), Value::String(Utc::now().to_rfc3339()));
    }
    let updated = match serde_json::to_vec_pretty(&value) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "session_unavailable",
                error.to_string(),
            );
        }
    };
    let snapshots = match capture_snapshots([
        binding.session_path.clone(),
        replacement::session_directory(&workspace, &session_id),
    ]) {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "control_transaction_unavailable",
                error.to_string(),
            );
        }
    };
    if let Err(error) = workspace::atomic_write(&binding.session_path, &updated) {
        return mutation_failed("session_unavailable", error.to_string(), &snapshots);
    }
    if let Err(error) = replacement::delete_record(&workspace, &session_id, &trace_id) {
        return mutation_failed("history_unavailable", error.to_string(), &snapshots);
    }
    success(
        json!({"rolledBack":true,"sessionId":session_id,"removedTraceId":trace_id,"prompt":latest.get("prompt").and_then(Value::as_str).unwrap_or_default()}),
        vec![json!({"action":"rollback_latest_execution", "rolledBack":true})],
        started,
    )
}

fn diff_payload(
    diff: coomi_services::GitDiff,
    trace_id: &str,
    session_id: &str,
    changed: &[String],
    commit_hash: &str,
) -> Value {
    let mut value = serde_json::to_value(diff).unwrap_or_else(|_| json!({}));
    let files = value
        .get("files")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let filtered: Vec<Value> = files
        .into_iter()
        .filter(|file| {
            changed
                .iter()
                .any(|path| file.get("relativePath").and_then(Value::as_str) == Some(path))
        })
        .collect();
    let added = filtered
        .iter()
        .map(|file| file.get("added").and_then(Value::as_u64).unwrap_or(0))
        .sum::<u64>();
    let removed = filtered
        .iter()
        .map(|file| file.get("removed").and_then(Value::as_u64).unwrap_or(0))
        .sum::<u64>();
    if let Some(object) = value.as_object_mut() {
        object.insert("files".into(), Value::Array(filtered));
        object.insert("traceId".into(), json!(trace_id));
        object.insert("sessionId".into(), json!(session_id));
        object.insert("changedFiles".into(), json!(changed));
        object.insert("changedFileCount".into(), json!(changed.len()));
        object.insert("added".into(), json!(added));
        object.insert("removed".into(), json!(removed));
        object.insert(
            "diffSource".into(),
            json!(if commit_hash.is_empty() {
                "working_tree"
            } else {
                "commit"
            }),
        );
        object.insert("commitHash".into(), json!(commit_hash));
        object.insert(
            "shortHash".into(),
            json!(commit_hash.chars().take(12).collect::<String>()),
        );
    }
    value
}

pub(crate) async fn diff_run(
    State(state): State<AppState>,
    AxumPath(trace_id): AxumPath<String>,
    Query(query): Query<WorkspaceQuery>,
) -> Response {
    let started = Instant::now();
    let session_id = normalized_session(&query.session_id);
    let (workspace, record) =
        match strict_run_record(&state, &query.workspace_root, &session_id, &trace_id) {
            Ok(value) => value,
            Err(response) => return response,
        };
    let ledger = match strict_record_ledger(&record, &workspace, &session_id, &trace_id) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let git = coomi_services::StorydexGit;
    let diff = if !ledger.commit_hash.is_empty() {
        git.commit_diff(&workspace, &ledger.commit_hash)
    } else {
        git.diff(&workspace)
    };
    let diff = match diff {
        Ok(value) => value,
        Err(error) => {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "git_diff_failed",
                error.to_string(),
            );
        }
    };
    let payload = diff_payload(
        diff,
        &trace_id,
        &session_id,
        &ledger.changed_files,
        &ledger.commit_hash,
    );
    success(
        payload,
        vec![json!({"action":"read_agent_run_diff", "traceId":trace_id})],
        started,
    )
}

struct RunLedger {
    changed_files: Vec<String>,
    commit_hash: String,
}

fn strict_run_record(
    state: &AppState,
    requested_workspace: &str,
    session_id: &str,
    trace_id: &str,
) -> Result<(PathBuf, Value), Response> {
    let lookup_workspace = resolve_workspace(state, requested_workspace)?;
    let record =
        replacement::read_record(&lookup_workspace, session_id, trace_id).map_err(|error| {
            failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "history_unavailable",
                error.to_string(),
            )
        })?;
    let record = record.ok_or_else(|| {
        failed(
            StatusCode::NOT_FOUND,
            "run_not_found",
            "Agent run was not found.",
        )
    })?;
    let raw_workspace = record
        .get("workspaceRoot")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "history_invalid",
                "Agent run has no workspace identity.",
            )
        })?;
    let record_workspace = resolve_workspace(state, raw_workspace)?;
    if record_workspace != lookup_workspace {
        return Err(failed(
            StatusCode::CONFLICT,
            "run_workspace_mismatch",
            "Agent run workspace does not match the selected project.",
        ));
    }
    Ok((record_workspace, record))
}

fn strict_record_ledger(
    record: &Value,
    workspace: &Path,
    session_id: &str,
    trace_id: &str,
) -> Result<RunLedger, Response> {
    let ledger = record
        .get("changeLedger")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "change_ledger_invalid",
                "Agent run has no trusted change ledger.",
            )
        })?;
    if ledger.get("traceId").and_then(Value::as_str) != Some(trace_id)
        || ledger.get("sessionId").and_then(Value::as_str) != Some(session_id)
    {
        return Err(failed(
            StatusCode::UNPROCESSABLE_ENTITY,
            "change_ledger_invalid",
            "Agent run change ledger identity does not match the requested run.",
        ));
    }
    let raw_workspace = ledger
        .get("workspaceRoot")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "change_ledger_invalid",
                "Agent run change ledger has no workspace identity.",
            )
        })?;
    let ledger_workspace = resolve_workspace_for_ledger(workspace, raw_workspace)?;
    if ledger_workspace != workspace {
        return Err(failed(
            StatusCode::UNPROCESSABLE_ENTITY,
            "change_ledger_invalid",
            "Agent run change ledger belongs to another workspace.",
        ));
    }
    let items = ledger
        .get("changedFiles")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "change_ledger_invalid",
                "Agent run change ledger has no changedFiles array.",
            )
        })?;
    let mut changed_files = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for item in items {
        let raw = item.as_str().ok_or_else(|| {
            failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "change_ledger_invalid",
                "Agent run changedFiles must contain only strings.",
            )
        })?;
        let normalized = workspace::normalize_relative(raw)?;
        let _ = workspace::resolve_target(workspace, &normalized)?;
        if !seen.insert(normalized.clone()) {
            return Err(failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "change_ledger_invalid",
                "Agent run changedFiles contains duplicate paths.",
            ));
        }
        changed_files.push(normalized);
    }
    if !changed_files.windows(2).all(|items| items[0] < items[1]) {
        return Err(failed(
            StatusCode::UNPROCESSABLE_ENTITY,
            "change_ledger_invalid",
            "Agent run changedFiles must use canonical sorted order.",
        ));
    }
    if ledger.get("changedFileCount").and_then(Value::as_u64)
        != u64::try_from(changed_files.len()).ok()
    {
        return Err(failed(
            StatusCode::UNPROCESSABLE_ENTITY,
            "change_ledger_invalid",
            "Agent run changedFileCount does not match changedFiles.",
        ));
    }
    for field in ["added", "removed"] {
        if ledger.get(field).and_then(Value::as_u64).is_none() {
            return Err(failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "change_ledger_invalid",
                format!("Agent run change ledger field {field} must be a non-negative integer."),
            ));
        }
    }
    let commit_hash = ledger
        .get("commitHash")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_owned();
    if !commit_hash.is_empty()
        && (commit_hash.len() < 7
            || commit_hash.len() > 64
            || !commit_hash.bytes().all(|byte| byte.is_ascii_hexdigit()))
    {
        return Err(failed(
            StatusCode::UNPROCESSABLE_ENTITY,
            "change_ledger_invalid",
            "Agent run commitHash is invalid.",
        ));
    }
    Ok(RunLedger {
        changed_files,
        commit_hash,
    })
}

fn resolve_workspace_for_ledger(workspace: &Path, raw: &str) -> Result<PathBuf, Response> {
    let path = PathBuf::from(raw.trim());
    let canonical = path.canonicalize().map_err(|_| {
        failed(
            StatusCode::UNPROCESSABLE_ENTITY,
            "change_ledger_invalid",
            "Agent run change ledger workspace is unavailable.",
        )
    })?;
    if canonical != workspace {
        return Err(failed(
            StatusCode::UNPROCESSABLE_ENTITY,
            "change_ledger_invalid",
            "Agent run change ledger workspace does not match its record.",
        ));
    }
    Ok(canonical)
}

pub(crate) async fn commit_run(
    State(state): State<AppState>,
    AxumPath(trace_id): AxumPath<String>,
    Query(query): Query<SessionQuery>,
    Json(payload): Json<CommitRequest>,
) -> Response {
    let started = Instant::now();
    let session_id = normalized_session(if payload.session_id.is_empty() {
        &query.session_id
    } else {
        &payload.session_id
    });
    if let Some(response) = reject_active_execution(&state, None, None) {
        return response;
    }
    let _mutation_guard = CONTROL_MUTATION_LOCK
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    if let Some(response) = reject_active_execution(&state, None, None) {
        return response;
    }
    let (workspace, mut record) = match strict_run_record(&state, "", &session_id, &trace_id) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let ledger = match strict_record_ledger(&record, &workspace, &session_id, &trace_id) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let mode = payload.mode.trim().to_ascii_lowercase();
    if !matches!(mode.as_str(), "auto" | "manual" | "skip") {
        return failed(
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid_agent_commit_decision",
            "mode must be auto, manual, or skip.",
        );
    }
    let changed = ledger.changed_files;
    let message = if mode == "manual" {
        if payload.message.trim().is_empty() {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "commit_message_required",
                "Commit message is required.",
            );
        }
        payload.message.trim().to_owned()
    } else if mode == "auto" {
        format!(
            "agent: update {}",
            trace_id.chars().take(12).collect::<String>()
        )
    } else {
        String::new()
    };
    let git = coomi_services::StorydexGit;
    let history_snapshot = if mode != "skip" && !changed.is_empty() && ledger.commit_hash.is_empty()
    {
        let summary = match git.summary(&workspace) {
            Ok(value) => value,
            Err(error) => {
                return failed(
                    StatusCode::UNPROCESSABLE_ENTITY,
                    "git_commit_failed",
                    error.to_string(),
                );
            }
        };
        let allowed = changed
            .iter()
            .map(String::as_str)
            .collect::<std::collections::HashSet<_>>();
        let staged_outside = summary
            .changed_files
            .iter()
            .filter(|file| {
                file.staged
                    && file.relative_path != ".gitignore"
                    && !allowed.contains(file.relative_path.as_str())
            })
            .map(|file| file.relative_path.clone())
            .collect::<Vec<_>>();
        if !staged_outside.is_empty() {
            return failed(
                StatusCode::CONFLICT,
                "staged_changes_outside_run",
                format!(
                    "Git index contains staged files outside this Agent run: {}",
                    staged_outside.join(", ")
                ),
            );
        }
        let snapshot =
            match PathSnapshot::capture(replacement::session_directory(&workspace, &session_id)) {
                Ok(value) => value,
                Err(error) => {
                    return failed(
                        StatusCode::UNPROCESSABLE_ENTITY,
                        "control_transaction_unavailable",
                        error.to_string(),
                    );
                }
            };
        if let Err(error) = replacement::persist_trace_record(&workspace, &session_id, &record) {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "history_unavailable",
                error.to_string(),
            );
        }
        Some(snapshot)
    } else {
        None
    };
    let result = if mode == "skip" || changed.is_empty() || !ledger.commit_hash.is_empty() {
        json!({"_type":"GitCommitResult","created":false,"status":"info","reason":if mode == "skip" {"user_skipped"} else if !ledger.commit_hash.is_empty() {"already_committed"} else {"no_changes"},"changedFiles":changed,"changedFileCount":changed.len(),"commitHash":ledger.commit_hash,"shortHash":ledger.commit_hash.chars().take(12).collect::<String>(),"generatedMessage":false,"commitMessageStrategy":"deterministic"})
    } else {
        let commit = match git.commit_paths(&workspace, &changed, &message) {
            Ok(value) => value,
            Err(error) => {
                return failed(
                    StatusCode::UNPROCESSABLE_ENTITY,
                    "git_commit_failed",
                    error.to_string(),
                );
            }
        };
        let commit_value = serde_json::to_value(&commit).unwrap_or_else(|_| json!({}));
        let hash = commit_value
            .get("commit")
            .and_then(|value| value.get("id"))
            .and_then(Value::as_str)
            .unwrap_or_default();
        json!({"_type":"GitCommitResult","created":commit.created,"status":if commit.created {"success"} else {"info"},"reason":if commit.created {"committed"} else {"no_changes"},"changedFiles":changed,"changedFileCount":changed.len(),"commitHash":hash,"shortHash":hash.chars().take(12).collect::<String>(),"message":message,"generatedMessage":false,"commitMessageStrategy":"deterministic"})
    };
    let event_name = if mode == "skip" {
        "GitCommitSkipped"
    } else {
        "GitCommitResult"
    };
    let event = json!({"index": record.get("events").and_then(Value::as_array).map(|items| items.len() + 1).unwrap_or(1), "event": event_name, "phase":"version_control", "status":result.get("status").and_then(Value::as_str).unwrap_or("info"), "detail":result.get("message").and_then(Value::as_str).unwrap_or_default(), "timestamp":Utc::now().to_rfc3339(), "data":result});
    if let Some(events) = record.get_mut("events").and_then(Value::as_array_mut) {
        events.push(event);
    }
    if result.get("created").and_then(Value::as_bool) == Some(true) {
        record["status"] = json!("committed");
    }
    if let Some(ledger) = record
        .get_mut("changeLedger")
        .and_then(Value::as_object_mut)
    {
        ledger.insert(
            "changedFiles".into(),
            result
                .get("changedFiles")
                .cloned()
                .unwrap_or_else(|| json!([])),
        );
        ledger.insert(
            "changedFileCount".into(),
            result
                .get("changedFileCount")
                .cloned()
                .unwrap_or_else(|| json!(0)),
        );
        let commit_hash = result
            .get("commitHash")
            .and_then(Value::as_str)
            .unwrap_or_default();
        ledger.insert("commitHash".into(), json!(commit_hash));
        ledger.insert(
            "shortHash".into(),
            json!(commit_hash.chars().take(12).collect::<String>()),
        );
        ledger.insert(
            "diffSource".into(),
            json!(if commit_hash.is_empty() && !changed.is_empty() {
                "working_tree"
            } else if commit_hash.is_empty() {
                ""
            } else {
                "commit"
            }),
        );
        ledger.insert("updatedAt".into(), json!(Utc::now().to_rfc3339()));
    }
    record["updatedAt"] = json!(Utc::now().to_rfc3339());
    if let Err(error) = replacement::persist_trace_record(&workspace, &session_id, &record) {
        let mut rollback_failures = Vec::new();
        if result.get("created").and_then(Value::as_bool) == Some(true)
            && let Some(commit_hash) = result
                .get("commitHash")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
            && let Err(rollback_error) = git.rollback_commit_keep_changes(&workspace, commit_hash)
        {
            rollback_failures.push(format!("Git rollback: {rollback_error:#}"));
        }
        if let Some(snapshot) = &history_snapshot
            && let Err(rollback_error) = snapshot.restore()
        {
            rollback_failures.push(format!("history rollback: {rollback_error:#}"));
        }
        if rollback_failures.is_empty() {
            return failed(
                StatusCode::UNPROCESSABLE_ENTITY,
                "history_unavailable",
                format!("{error}; the Git commit was rolled back"),
            );
        }
        return failed(
            StatusCode::INTERNAL_SERVER_ERROR,
            "control_transaction_rollback_failed",
            format!(
                "{error}; commit transaction rollback failed: {}",
                rollback_failures.join("; ")
            ),
        );
    }
    success(
        result,
        vec![json!({"action":"agent_git_commit_decision", "mode":mode, "traceId":trace_id})],
        started,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;
    use coomi_engine::{ChatMessage, Session};
    use tempfile::tempdir;

    #[test]
    fn non_streaming_sse_aggregation_enforces_terminal_and_done_order() {
        let valid = concat!(
            "event: RunAccepted\r\n",
            "data: {\"status\":\"running\",\"detail\":\"accepted\"}\r\n\r\n",
            "event: TextChunk\r\n",
            "data: {\"content\":\"hello\"}\r\n\r\n",
            "event: AgentCompleted\r\n",
            "data: {\"status\":\"success\"}\r\n\r\n",
            "event: done\r\n",
            "data: {\"type\":\"done\"}\r\n\r\n"
        );
        let (reply, events, terminal) = aggregate_sse(valid.as_bytes()).expect("valid stream");
        assert_eq!(reply, "hello");
        assert_eq!(events.len(), 3);
        assert_eq!(events[1]["event"], "TextChunk");
        assert_eq!(events[1]["phase"], "model");
        assert_eq!(events[1]["detail"], "hello");
        assert_eq!(terminal["status"], "success");

        let cases = [
            (
                "event: done\ndata: {\"type\":\"done\"}\n\n",
                "stream_done_before_terminal",
            ),
            (
                concat!(
                    "event: AgentCompleted\ndata: {}\n\n",
                    "event: TextChunk\ndata: {\"content\":\"late\"}\n\n",
                    "event: done\ndata: {\"type\":\"done\"}\n\n"
                ),
                "stream_event_after_terminal",
            ),
            (
                concat!(
                    "event: AgentCompleted\ndata: {}\n\n",
                    "event: done\ndata: {\"type\":\"done\"}\n\n",
                    "event: done\ndata: {\"type\":\"done\"}\n\n"
                ),
                "stream_event_after_done",
            ),
            ("event: AgentCompleted\ndata: {}\n\n", "stream_incomplete"),
            (
                concat!(
                    "event: AgentCompleted\ndata: {}\n\n",
                    "event: AgentError\ndata: {}\n\n",
                    "event: done\ndata: {\"type\":\"done\"}\n\n"
                ),
                "stream_event_after_terminal",
            ),
            ("event: AgentCompleted\ndata: {\n\n", "stream_invalid_event"),
        ];
        for (stream, expected_code) in cases {
            let error = aggregate_sse(stream.as_bytes()).expect_err(expected_code);
            assert_eq!(error.code, expected_code);
        }
    }

    #[test]
    fn inferred_write_intent_keeps_advisory_prompts_read_only() {
        for prompt in [
            "请解释如何修改代码",
            "请说明怎样更新章节",
            "帮我分析如何重写这个文件",
            "please explain how to update this chapter",
            "不要修改代码",
            "请勿删除章节",
        ] {
            assert!(!inferred_write_intent(prompt), "{prompt}");
        }
        for prompt in [
            "请修改代码并保存",
            "请重写当前章节",
            "please update this chapter",
            "请分析问题并修改代码",
            "不要只解释，但请直接修改代码",
        ] {
            assert!(inferred_write_intent(prompt), "{prompt}");
        }
    }

    #[test]
    fn model_ids_match_stable_normalization_contract() {
        assert_eq!(
            extract_model_ids(&json!({
                "data": [
                    {"id": "models/Zeta"},
                    "beta",
                    {"name": "Alpha"},
                    {"id": "beta"},
                    {"id": "  gamma  "},
                    null
                ]
            })),
            vec!["Alpha", "beta", "gamma", "Zeta"]
        );
        assert_eq!(
            extract_model_ids(&json!(["models/gemini-2.5-flash", "deepseek-v4-flash"])),
            vec!["deepseek-v4-flash", "gemini-2.5-flash"]
        );
    }

    #[test]
    fn strict_change_ledger_rejects_missing_workspace_duplicates_and_unsorted_paths() {
        let root = tempdir().expect("workspace");
        let workspace = root.path().canonicalize().expect("canonical workspace");
        let base = json!({
            "traceId": "trace",
            "sessionId": "session",
            "workspaceRoot": contract_test_path(&workspace),
            "changedFiles": ["a.md", "b.md"],
            "changedFileCount": 2,
            "added": 0,
            "removed": 0,
            "commitHash": "",
        });

        assert!(
            strict_record_ledger(
                &json!({"changeLedger": base.clone()}),
                &workspace,
                "session",
                "trace"
            )
            .is_ok()
        );

        let mut missing_workspace = base.clone();
        missing_workspace
            .as_object_mut()
            .expect("ledger object")
            .remove("workspaceRoot");
        assert!(
            strict_record_ledger(
                &json!({"changeLedger": missing_workspace}),
                &workspace,
                "session",
                "trace"
            )
            .is_err()
        );

        for paths in [json!(["a.md", "a.md"]), json!(["b.md", "a.md"])] {
            let mut invalid = base.clone();
            invalid["changedFiles"] = paths;
            assert!(
                strict_record_ledger(
                    &json!({"changeLedger": invalid}),
                    &workspace,
                    "session",
                    "trace"
                )
                .is_err()
            );
        }
    }

    #[test]
    fn runtime_binding_rejects_symbolic_link_file() {
        let root = tempdir().expect("root");
        let workspace = root.path().join("workspace");
        let home = root.path().join("home");
        fs::create_dir_all(&workspace).expect("workspace");
        fs::create_dir_all(&home).expect("home");
        let workspace = workspace.canonicalize().expect("canonical workspace");
        let state = AppState::with_paths(
            "token",
            &home,
            root.path().join("bridge"),
            Some(workspace.clone()),
            None,
        )
        .expect("state");
        let target = root.path().join("binding-target.json");
        fs::write(&target, "{}").expect("target");
        let binding_path = chat::session_binding_path(&workspace, "session");
        fs::create_dir_all(binding_path.parent().expect("binding parent"))
            .expect("binding directory");
        #[cfg(windows)]
        if std::os::windows::fs::symlink_file(&target, &binding_path).is_err() {
            return;
        }
        #[cfg(unix)]
        std::os::unix::fs::symlink(&target, &binding_path).expect("binding symlink");
        let error = chat::load_runtime_session_binding(&state, &workspace, "session")
            .expect_err("symlink binding must fail closed");
        assert!(error.to_string().contains("regular file"));
    }

    #[tokio::test]
    async fn successful_write_emits_git_prompt_before_terminal_and_pauses_followups() {
        let root = tempdir().expect("root");
        let workspace = root.path().join("workspace");
        let home = root.path().join("home");
        fs::create_dir_all(&workspace).expect("workspace");
        fs::create_dir_all(&home).expect("home");
        let workspace = workspace.canonicalize().expect("canonical workspace");
        let state = AppState::with_paths(
            "token",
            home,
            root.path().join("bridge"),
            Some(workspace.clone()),
            None,
        )
        .expect("state");
        let (sender, mut receiver) = tokio::sync::mpsc::channel(2);
        let mut events = vec![(
            "ToolDone".to_owned(),
            chat::with_event_identity(
                "ToolDone",
                json!({
                    "tool_name": "write_file",
                    "is_error": false,
                    "arguments": {"path": "chapters/001.md"}
                }),
                "trace",
                "session",
            ),
        )];
        let terminal = chat::with_event_identity(
            "AgentCompleted",
            json!({"status": "success"}),
            "trace",
            "session",
        );
        assert!(
            chat::emit_git_commit_prompt(
                &state,
                &workspace,
                &sender,
                &mut events,
                "trace",
                "session",
                Some(("AgentCompleted", &terminal)),
            )
            .await
            .expect("commit prompt")
        );
        events.push(("AgentCompleted".to_owned(), terminal));
        assert_eq!(events[1].0, "GitCommitPrompt");
        assert_eq!(events[2].0, "AgentCompleted");
        assert_eq!(events[1].1["changedFiles"], json!(["chapters/001.md"]));
        assert!(
            receiver
                .recv()
                .await
                .expect("prompt frame")
                .starts_with("event: GitCommitPrompt\n")
        );
        let mailbox = state
            .followup_store()
            .list(&workspace, "session")
            .expect("mailbox");
        assert!(mailbox.paused);
        assert_eq!(mailbox.pause_reason, "git_commit_prompt");
    }

    #[tokio::test]
    async fn clear_conversation_rolls_back_every_store_when_config_save_fails() {
        let root = tempdir().expect("root");
        let workspace = root.path().join("workspace");
        let home = root.path().join("home");
        fs::create_dir_all(&workspace).expect("workspace");
        fs::create_dir_all(&home).expect("home");
        let workspace = workspace.canonicalize().expect("canonical workspace");
        let state = AppState::with_paths(
            "token",
            &home,
            root.path().join("bridge"),
            Some(workspace.clone()),
            None,
        )
        .expect("state");
        let session_id = "transaction-session";
        let trace_id = "transaction-trace";
        replacement::persist_execution_record_with_events(replacement::ExecutionRecordInput {
            workspace: &workspace,
            session_id,
            trace_id,
            prompt: "prompt",
            status: "completed",
            events: &[(
                "AgentCompleted".to_owned(),
                json!({"writtenPaths": ["chapters/001.md"]}),
            )],
            reply: "reply",
            provider_id: "provider",
            model: "model",
        })
        .expect("history");
        state
            .followup_store()
            .set_active(&workspace, session_id, trace_id)
            .expect("follow-up mailbox");

        let mut runtime_session = Session::new("provider", "model", workspace.clone());
        runtime_session.messages.push(ChatMessage::user("keep me"));
        let runtime_path = home
            .join("sessions")
            .join(format!("{}.json", runtime_session.id));
        fs::create_dir_all(runtime_path.parent().expect("runtime parent"))
            .expect("runtime directory");
        let runtime_bytes = serde_json::to_vec_pretty(&runtime_session).expect("runtime bytes");
        fs::write(&runtime_path, &runtime_bytes).expect("runtime session");
        let binding_path = chat::session_binding_path(&workspace, session_id);
        fs::create_dir_all(binding_path.parent().expect("binding parent"))
            .expect("binding directory");
        fs::write(
            &binding_path,
            serde_json::to_vec_pretty(&json!({
                "workspaceRoot": contract_test_path(&workspace),
                "storydexSessionId": session_id,
                "runtimeSessionId": runtime_session.id,
                "sessionPath": contract_test_path(&runtime_path),
            }))
            .expect("binding bytes"),
        )
        .expect("binding");

        let followup_path = state.followup_store().storage_path(&workspace, session_id);
        let followup_bytes = fs::read(&followup_path).expect("follow-up bytes");
        let history_path = replacement::trace_path(&workspace, session_id, trace_id);
        let history_bytes = fs::read(&history_path).expect("history bytes");
        fs::write(root.path().join("config"), "block config directory").expect("blocker");

        let response = clear_conversation(
            State(state.clone()),
            Query(SessionQuery {
                session_id: session_id.to_owned(),
            }),
        )
        .await;
        assert_eq!(response.status(), StatusCode::UNPROCESSABLE_ENTITY);
        let body = to_bytes(response.into_body(), 1024 * 1024)
            .await
            .expect("response body");
        let body: Value = serde_json::from_slice(&body).expect("response JSON");
        assert_eq!(body["error"]["code"], "coomi_runtime_config_invalid");
        assert_eq!(
            fs::read(&runtime_path).expect("restored runtime"),
            runtime_bytes
        );
        assert_eq!(
            fs::read(&history_path).expect("restored history"),
            history_bytes
        );
        assert_eq!(
            fs::read(&followup_path).expect("restored follow-up"),
            followup_bytes
        );
        assert_eq!(
            state
                .followup_store()
                .list(&workspace, session_id)
                .expect("restored mailbox")
                .active_trace_id,
            trace_id
        );
    }

    #[tokio::test]
    async fn delete_session_rolls_back_every_store_when_config_save_fails() {
        let root = tempdir().expect("root");
        let workspace = root.path().join("workspace");
        let home = root.path().join("home");
        fs::create_dir_all(&workspace).expect("workspace");
        fs::create_dir_all(&home).expect("home");
        let workspace = workspace.canonicalize().expect("canonical workspace");
        let state = AppState::with_paths(
            "token",
            &home,
            root.path().join("bridge"),
            Some(workspace.clone()),
            None,
        )
        .expect("state");
        let session_id = "delete-transaction-session";
        let trace_id = "delete-transaction-trace";
        replacement::persist_execution_record_with_events(replacement::ExecutionRecordInput {
            workspace: &workspace,
            session_id,
            trace_id,
            prompt: "prompt",
            status: "completed",
            events: &[(
                "AgentCompleted".to_owned(),
                json!({"writtenPaths": ["chapters/001.md"]}),
            )],
            reply: "reply",
            provider_id: "provider",
            model: "model",
        })
        .expect("history");
        state
            .followup_store()
            .set_active(&workspace, session_id, trace_id)
            .expect("follow-up mailbox");

        let mut runtime_session = Session::new("provider", "model", workspace.clone());
        runtime_session.messages.push(ChatMessage::user("keep me"));
        let runtime_path = home
            .join("sessions")
            .join(format!("{}.json", runtime_session.id));
        fs::create_dir_all(runtime_path.parent().expect("runtime parent"))
            .expect("runtime directory");
        let runtime_bytes = serde_json::to_vec_pretty(&runtime_session).expect("runtime bytes");
        fs::write(&runtime_path, &runtime_bytes).expect("runtime session");
        let binding_path = chat::session_binding_path(&workspace, session_id);
        fs::create_dir_all(binding_path.parent().expect("binding parent"))
            .expect("binding directory");
        let binding_bytes = serde_json::to_vec_pretty(&json!({
            "workspaceRoot": contract_test_path(&workspace),
            "storydexSessionId": session_id,
            "runtimeSessionId": runtime_session.id,
            "sessionPath": contract_test_path(&runtime_path),
        }))
        .expect("binding bytes");
        fs::write(&binding_path, &binding_bytes).expect("binding");

        let followup_path = state.followup_store().storage_path(&workspace, session_id);
        let followup_bytes = fs::read(&followup_path).expect("follow-up bytes");
        let history_path = replacement::trace_path(&workspace, session_id, trace_id);
        let history_bytes = fs::read(&history_path).expect("history bytes");

        // Make the final control-config write fail after all other stores have
        // been removed, so the transaction must restore the full delete set.
        fs::write(root.path().join("config"), "block config directory").expect("blocker");

        let response = delete_session(
            State(state.clone()),
            Json(DeleteSessionRequest {
                session_id: session_id.to_owned(),
                workspace_root: contract_test_path(&workspace),
            }),
        )
        .await;
        assert_eq!(response.status(), StatusCode::UNPROCESSABLE_ENTITY);
        let body = to_bytes(response.into_body(), 1024 * 1024)
            .await
            .expect("response body");
        let body: Value = serde_json::from_slice(&body).expect("response JSON");
        assert_eq!(body["error"]["code"], "coomi_runtime_config_invalid");
        assert_eq!(
            fs::read(&runtime_path).expect("restored runtime"),
            runtime_bytes
        );
        assert_eq!(
            fs::read(&binding_path).expect("restored binding"),
            binding_bytes
        );
        assert_eq!(
            fs::read(&history_path).expect("restored history"),
            history_bytes
        );
        assert_eq!(
            fs::read(&followup_path).expect("restored follow-up"),
            followup_bytes
        );
        assert_eq!(
            state
                .followup_store()
                .list(&workspace, session_id)
                .expect("restored mailbox")
                .active_trace_id,
            trace_id
        );
    }

    fn contract_test_path(path: &Path) -> String {
        let value = path.to_string_lossy();
        value
            .strip_prefix("\\\\?\\UNC\\")
            .map(|rest| format!("\\\\{rest}"))
            .or_else(|| value.strip_prefix("\\\\?\\").map(ToOwned::to_owned))
            .unwrap_or_else(|| value.into_owned())
    }
}
