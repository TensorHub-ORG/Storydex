use anyhow::{Context, Result};
use chrono::Utc;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use uuid::Uuid;

#[derive(Debug)]
pub struct ReplacementError {
    pub code: &'static str,
    pub message: String,
}

impl ReplacementError {
    fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}

impl std::fmt::Display for ReplacementError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for ReplacementError {}

pub struct ReplacementTransaction {
    workspace: PathBuf,
    session_id: String,
    expected_trace_id: String,
    replacement_trace_id: String,
    replacement_prompt: String,
    original_record: Value,
    session_path: Option<PathBuf>,
    session_snapshot: Option<Vec<u8>>,
    accepted: bool,
    restored: bool,
}

impl ReplacementTransaction {
    pub fn prepare(
        workspace: &Path,
        coomi_home: &Path,
        session_id: &str,
        expected_trace_id: &str,
        replacement_trace_id: &str,
        replacement_prompt: &str,
        runtime_session_id: Option<Uuid>,
    ) -> std::result::Result<Self, ReplacementError> {
        let workspace = workspace.canonicalize().map_err(|error| {
            ReplacementError::new("replacement_workspace_mismatch", error.to_string())
        })?;
        let session_id = normalized_session_id(session_id);
        let expected_trace_id = expected_trace_id.trim().to_owned();
        let replacement_trace_id = replacement_trace_id.trim().to_owned();
        let latest = latest_record(&workspace, &session_id)
            .map_err(|error| {
                ReplacementError::new(
                    "replacement_context_unavailable",
                    format!("Unable to read the latest execution: {error:#}"),
                )
            })?
            .ok_or_else(|| {
                ReplacementError::new(
                    "replacement_target_missing",
                    "There is no completed execution to replace.",
                )
            })?;
        let latest_trace_id = latest
            .get("traceId")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .trim()
            .to_owned();
        if latest_trace_id.is_empty() {
            return Err(ReplacementError::new(
                "replacement_target_missing",
                "There is no completed execution to replace.",
            ));
        }
        if !expected_trace_id.is_empty() && latest_trace_id != expected_trace_id {
            return Err(ReplacementError::new(
                "stale_trace",
                format!(
                    "The latest execution changed before replacement was confirmed. expectedTraceId={expected_trace_id}; latestTraceId={latest_trace_id}"
                ),
            ));
        }
        if latest
            .get("status")
            .and_then(Value::as_str)
            .is_some_and(|status| status.eq_ignore_ascii_case("running"))
        {
            return Err(ReplacementError::new(
                "replacement_target_running",
                "A running execution cannot be edited.",
            ));
        }
        if let Some(record_workspace) = latest.get("workspaceRoot").and_then(Value::as_str)
            && !record_workspace.trim().is_empty()
        {
            let record_workspace =
                PathBuf::from(record_workspace)
                    .canonicalize()
                    .map_err(|_| {
                        ReplacementError::new(
                            "replacement_workspace_mismatch",
                            "The replacement target workspace is unavailable.",
                        )
                    })?;
            if record_workspace != workspace {
                return Err(ReplacementError::new(
                    "replacement_workspace_mismatch",
                    "The replacement target belongs to another workspace.",
                ));
            }
        }

        let (session_path, session_snapshot) = if let Some(runtime_id) = runtime_session_id {
            let path = coomi_home
                .join("sessions")
                .join(format!("{runtime_id}.json"));
            let snapshot = fs::read(&path).map_err(|error| {
                ReplacementError::new(
                    "replacement_context_unavailable",
                    format!("Unable to snapshot the latest Coomi turn: {error}"),
                )
            })?;
            (Some(path), Some(snapshot))
        } else {
            (None, None)
        };

        let mut transaction = Self {
            workspace,
            session_id,
            expected_trace_id: latest_trace_id.clone(),
            replacement_trace_id: replacement_trace_id.to_owned(),
            replacement_prompt: replacement_prompt.to_owned(),
            original_record: latest,
            session_path,
            session_snapshot,
            accepted: false,
            restored: false,
        };
        if let Err(error) = transaction.mark_pending_and_rollback() {
            let _ = transaction.restore("prepare_failed");
            return Err(error);
        }
        Ok(transaction)
    }

    pub fn accept(&mut self) -> Result<()> {
        if !self.prepared() || self.accepted || self.restored {
            return Ok(());
        }
        let mut record = self.original_record.clone();
        let object = record
            .as_object_mut()
            .ok_or_else(|| anyhow::anyhow!("replacement trace record is not an object"))?;
        object.insert("status".into(), Value::String("superseded".into()));
        object.insert("superseded".into(), Value::Bool(true));
        object.insert(
            "supersededByTraceId".into(),
            Value::String(self.replacement_trace_id.clone()),
        );
        object.insert(
            "replacement".into(),
            json!({
                "status": "accepted",
                "replacementTraceId": self.replacement_trace_id,
                "expectedTraceId": self.expected_trace_id,
                "acceptedAt": Utc::now().to_rfc3339(),
                "dialogueOnly": true,
                "fileChangesReverted": false,
            }),
        );
        object.insert("updatedAt".into(), Value::String(Utc::now().to_rfc3339()));
        persist_trace_record(&self.workspace, &self.session_id, &record)?;
        self.accepted = true;
        Ok(())
    }

    pub fn restore(&mut self, reason: &str) -> Result<()> {
        if self.restored || self.accepted {
            return Ok(());
        }
        if let (Some(path), Some(snapshot)) = (&self.session_path, &self.session_snapshot) {
            atomic_write(path, snapshot)?;
        }
        let mut record = self.original_record.clone();
        let object = record
            .as_object_mut()
            .ok_or_else(|| anyhow::anyhow!("replacement trace record is not an object"))?;
        object.insert("superseded".into(), Value::Bool(false));
        object.insert("supersededByTraceId".into(), Value::String(String::new()));
        object.insert(
            "replacement".into(),
            json!({
                "status": "restored",
                "replacementTraceId": self.replacement_trace_id,
                "restoredAt": Utc::now().to_rfc3339(),
                "reason": reason,
                "dialogueOnly": true,
                "fileChangesReverted": false,
            }),
        );
        object.insert("updatedAt".into(), Value::String(Utc::now().to_rfc3339()));
        persist_trace_record(&self.workspace, &self.session_id, &record)?;
        self.restored = true;
        Ok(())
    }

    pub fn is_accepted(&self) -> bool {
        self.accepted
    }

    pub fn is_restored(&self) -> bool {
        self.restored
    }

    fn prepared(&self) -> bool {
        !self.expected_trace_id.is_empty() && !self.replacement_trace_id.is_empty()
    }

    fn mark_pending_and_rollback(&mut self) -> std::result::Result<(), ReplacementError> {
        let mut pending = self.original_record.clone();
        let object = pending.as_object_mut().ok_or_else(|| {
            ReplacementError::new(
                "replacement_context_unavailable",
                "The replacement target trace is malformed.",
            )
        })?;
        object.insert("status".into(), Value::String("superseded".into()));
        object.insert("superseded".into(), Value::Bool(true));
        object.insert(
            "supersededByTraceId".into(),
            Value::String(self.replacement_trace_id.clone()),
        );
        object.insert(
            "replacement".into(),
            json!({
                "status": "pending",
                "replacementTraceId": self.replacement_trace_id,
                "expectedTraceId": self.expected_trace_id,
                "replacementPrompt": self.replacement_prompt,
                "preparedAt": Utc::now().to_rfc3339(),
                "dialogueOnly": true,
                "fileChangesReverted": false,
            }),
        );
        object.insert("updatedAt".into(), Value::String(Utc::now().to_rfc3339()));
        persist_trace_record(&self.workspace, &self.session_id, &pending).map_err(|error| {
            ReplacementError::new(
                "replacement_context_unavailable",
                format!("Unable to mark the replacement target: {error}"),
            )
        })?;
        if let Some(path) = &self.session_path {
            let Some(snapshot) = &self.session_snapshot else {
                return Ok(());
            };
            let mut value: Value = serde_json::from_slice(snapshot).map_err(|error| {
                ReplacementError::new(
                    "replacement_context_unavailable",
                    format!("The latest Coomi session is invalid: {error}"),
                )
            })?;
            let messages = value
                .get_mut("messages")
                .and_then(Value::as_array_mut)
                .ok_or_else(|| {
                    ReplacementError::new(
                        "replacement_context_unavailable",
                        "The latest Coomi session has no message history.",
                    )
                })?;
            let last_user = messages.iter().rposition(|message| {
                message
                    .get("role")
                    .and_then(Value::as_str)
                    .is_some_and(|role| role == "user")
                    && message.get("internal").and_then(Value::as_bool) != Some(true)
            });
            let Some(last_user) = last_user else {
                return Ok(());
            };
            messages.truncate(last_user);
            if let Some(object) = value.as_object_mut() {
                object.insert("updated_at".into(), Value::String(Utc::now().to_rfc3339()));
            }
            let bytes = serde_json::to_vec_pretty(&value).map_err(|error| {
                ReplacementError::new(
                    "replacement_context_unavailable",
                    format!("Unable to encode the rolled-back session: {error}"),
                )
            })?;
            atomic_write(path, &bytes).map_err(|error| {
                ReplacementError::new(
                    "replacement_context_unavailable",
                    format!("Unable to withdraw the latest Coomi turn: {error}"),
                )
            })?;
        }
        Ok(())
    }
}

impl Drop for ReplacementTransaction {
    fn drop(&mut self) {
        if self.accepted || self.restored {
            return;
        }
        if let Err(error) = self.restore("worker_dropped") {
            tracing::warn!(error = %error, "unable to restore dropped replacement transaction");
        }
    }
}

pub struct ExecutionRecordInput<'a> {
    pub workspace: &'a Path,
    pub session_id: &'a str,
    pub trace_id: &'a str,
    pub prompt: &'a str,
    pub status: &'a str,
    pub events: &'a [(String, Value)],
    pub reply: &'a str,
    pub provider_id: &'a str,
    pub model: &'a str,
}

pub fn persist_execution_record_with_events(input: ExecutionRecordInput<'_>) -> Result<()> {
    let ExecutionRecordInput {
        workspace,
        session_id,
        trace_id,
        prompt,
        status,
        events,
        reply,
        provider_id,
        model,
    } = input;
    let now = Utc::now().to_rfc3339();
    let trace_events = events
        .iter()
        .enumerate()
        .map(|(index, (name, data))| {
            json!({
                "index": index + 1,
                "event": name,
                "phase": data.get("phase").and_then(Value::as_str).unwrap_or_default(),
                "status": data.get("status").and_then(Value::as_str).unwrap_or("info"),
                "detail": data.get("message").and_then(Value::as_str).unwrap_or_default(),
                "timestamp": Utc::now().to_rfc3339(),
                "data": data,
            })
        })
        .collect::<Vec<_>>();
    let change_ledger = change_ledger_from_events(workspace, session_id, trace_id, events);
    let changed_files = change_ledger
        .get("changedFiles")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let checkpoint_status = if changed_files.is_empty() {
        "not_needed"
    } else if status.eq_ignore_ascii_case("completed") {
        "saved"
    } else {
        "captured"
    };
    let record = json!({
        "traceId": trace_id,
        "sessionId": normalized_session_id(session_id),
        "workspaceRoot": workspace.to_string_lossy(),
        "route": "coomi",
        "agentMode": "coomi",
        "status": status,
        "prompt": prompt,
        "reply": reply,
        "llmProvider": provider_id,
        "llmModel": model,
        "events": trace_events,
        "changeLedger": change_ledger,
        "checkpoint": {
            "kind": "hidden",
            "status": checkpoint_status,
            "source": "execution_record",
            "changedFiles": changed_files,
            "changedFileCount": changed_files.len(),
            "updatedAt": now,
        },
        "tasks": [],
        "audit": [],
        "createdAt": now,
        "updatedAt": Utc::now().to_rfc3339(),
    });
    persist_trace_record(workspace, session_id, &record)
}

pub(crate) fn change_ledger_from_events(
    workspace: &Path,
    session_id: &str,
    trace_id: &str,
    events: &[(String, Value)],
) -> Value {
    let mut changed_files = BTreeSet::new();
    let mut commit_hash = String::new();
    for (event_name, data) in events {
        collect_string_array_paths(workspace, data.get("writtenPaths"), &mut changed_files);
        collect_string_array_paths(
            workspace,
            data.get("authoritativeFragmentPaths"),
            &mut changed_files,
        );
        if event_name == "ToolDone"
            && data.get("is_error").and_then(Value::as_bool) != Some(true)
            && is_write_tool(
                data.get("tool_name")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            )
            && let Some(arguments) = data.get("arguments")
        {
            collect_argument_paths(workspace, arguments, "", 0, &mut changed_files);
        }
        if matches!(
            event_name.as_str(),
            "GitAutoCommit" | "GitCommitPrompt" | "GitCommitResult"
        ) {
            collect_string_array_paths(workspace, data.get("changedFiles"), &mut changed_files);
            if let Some(value) = data
                .get("commitHash")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
            {
                commit_hash = value.to_owned();
            }
        }
    }
    let changed_files = changed_files.into_iter().collect::<Vec<_>>();
    json!({
        "traceId": trace_id,
        "sessionId": normalized_session_id(session_id),
        "workspaceRoot": workspace.to_string_lossy(),
        "changedFiles": changed_files,
        "changedFileCount": changed_files.len(),
        "added": 0,
        "removed": 0,
        "diffSource": if commit_hash.is_empty() {
            if changed_files.is_empty() { "" } else { "working_tree" }
        } else {
            "commit"
        },
        "commitHash": commit_hash,
        "shortHash": commit_hash.chars().take(12).collect::<String>(),
        "updatedAt": Utc::now().to_rfc3339(),
    })
}

fn collect_string_array_paths(
    workspace: &Path,
    value: Option<&Value>,
    output: &mut BTreeSet<String>,
) {
    for raw in value.and_then(Value::as_array).into_iter().flatten() {
        if let Some(path) = raw
            .as_str()
            .and_then(|value| normalize_ledger_path(workspace, value))
        {
            output.insert(path);
        }
    }
}

fn collect_argument_paths(
    workspace: &Path,
    value: &Value,
    key: &str,
    depth: usize,
    output: &mut BTreeSet<String>,
) {
    if depth > 6 {
        return;
    }
    match value {
        Value::String(raw) if is_path_key(key) => {
            if let Some(path) = normalize_ledger_path(workspace, raw) {
                output.insert(path);
            }
        }
        Value::Array(items) => {
            for item in items {
                collect_argument_paths(workspace, item, key, depth + 1, output);
            }
        }
        Value::Object(object) => {
            for (child_key, child_value) in object {
                collect_argument_paths(workspace, child_value, child_key, depth + 1, output);
            }
        }
        _ => {}
    }
}

fn is_path_key(key: &str) -> bool {
    let normalized = key
        .chars()
        .filter(|character| !matches!(character, '_' | '-' | ' '))
        .collect::<String>()
        .to_ascii_lowercase();
    matches!(
        normalized.as_str(),
        "path"
            | "paths"
            | "file"
            | "files"
            | "filename"
            | "filepath"
            | "relativepath"
            | "target"
            | "targetfile"
            | "segmentpath"
            | "sourcepath"
            | "outputpath"
    ) || normalized.ends_with("path")
        || normalized.ends_with("paths")
}

fn is_write_tool(name: &str) -> bool {
    let normalized = name
        .chars()
        .filter(|character| !matches!(character, '_' | '-' | ' '))
        .collect::<String>()
        .to_ascii_lowercase();
    if normalized.contains("versionstatus") || normalized.contains("runtimepresetstatus") {
        return false;
    }
    [
        "write",
        "edit",
        "patch",
        "save",
        "create",
        "delete",
        "move",
        "rename",
        "mkdir",
        "applystoryincrement",
        "syncwiki",
    ]
    .iter()
    .any(|signal| normalized.contains(signal))
}

pub(crate) fn normalize_ledger_path(workspace: &Path, raw: &str) -> Option<String> {
    let raw = raw.trim().trim_matches(['\'', '"', '`']);
    if raw.is_empty() || raw.contains(['\r', '\n', '\0']) {
        return None;
    }
    let mut candidate = PathBuf::from(raw);
    if candidate.is_absolute() {
        candidate = candidate.strip_prefix(workspace).ok()?.to_path_buf();
    }
    let mut parts = Vec::new();
    for component in candidate.components() {
        match component {
            std::path::Component::Normal(part) => {
                let part = part.to_str()?;
                if part.is_empty() {
                    return None;
                }
                parts.push(part.to_owned());
            }
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir
            | std::path::Component::RootDir
            | std::path::Component::Prefix(_) => return None,
        }
    }
    (!parts.is_empty()).then(|| parts.join("/"))
}

pub(crate) fn normalized_session_id(value: &str) -> String {
    let value = value.trim();
    if value.is_empty() {
        "default".to_owned()
    } else {
        value.to_owned()
    }
}

fn safe_component(value: &str) -> String {
    let safe = !matches!(value, "." | "..")
        && value.chars().all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-')
        });
    if safe {
        value.to_owned()
    } else {
        format!("sha256-{:x}", Sha256::digest(value.as_bytes()))
    }
}

fn sessions_root(workspace: &Path) -> PathBuf {
    workspace.join(".storydex").join(".agent").join("sessions")
}

pub(crate) fn session_directory(workspace: &Path, session_id: &str) -> PathBuf {
    let normalized = normalized_session_id(session_id);
    sessions_root(workspace).join(safe_component(&normalized))
}

pub(crate) fn trace_path(workspace: &Path, session_id: &str, trace_id: &str) -> PathBuf {
    session_directory(workspace, session_id).join(format!("{}.json", safe_component(trace_id)))
}

fn record_sort_key(record: &Value) -> (&str, &str, &str) {
    (
        record
            .get("updatedAt")
            .and_then(Value::as_str)
            .unwrap_or_default(),
        record
            .get("createdAt")
            .and_then(Value::as_str)
            .unwrap_or_default(),
        record
            .get("traceId")
            .and_then(Value::as_str)
            .unwrap_or_default(),
    )
}

fn read_record_file(path: &Path, expected_session_id: Option<&str>) -> Result<Value> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("unable to inspect trace record {}", path.display()))?;
    anyhow::ensure!(
        !metadata.file_type().is_symlink(),
        "trace record cannot be a symbolic link: {}",
        path.display()
    );
    anyhow::ensure!(
        metadata.is_file(),
        "trace record is not a file: {}",
        path.display()
    );
    let bytes = fs::read(path)
        .with_context(|| format!("unable to read trace record {}", path.display()))?;
    let value: Value = serde_json::from_slice(&bytes)
        .with_context(|| format!("invalid trace record JSON {}", path.display()))?;
    anyhow::ensure!(
        value.is_object(),
        "trace record must be a JSON object: {}",
        path.display()
    );
    let trace_id = value
        .get("traceId")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or_default();
    anyhow::ensure!(
        !trace_id.is_empty(),
        "trace record has no traceId: {}",
        path.display()
    );
    let session_id = value
        .get("sessionId")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or_default();
    anyhow::ensure!(
        !session_id.is_empty(),
        "trace record has no sessionId: {}",
        path.display()
    );
    if let Some(expected) = expected_session_id {
        anyhow::ensure!(
            session_id == normalized_session_id(expected),
            "trace record session mismatch in {}",
            path.display()
        );
    }
    Ok(value)
}

fn list_records_in_directory(
    directory: &Path,
    expected_session_id: Option<&str>,
) -> Result<Vec<Value>> {
    if !directory.exists() {
        return Ok(Vec::new());
    }
    let metadata = fs::symlink_metadata(directory).with_context(|| {
        format!(
            "unable to inspect session directory {}",
            directory.display()
        )
    })?;
    anyhow::ensure!(
        !metadata.file_type().is_symlink() && metadata.is_dir(),
        "session history path is not a regular directory: {}",
        directory.display()
    );
    let mut records = Vec::new();
    for entry in fs::read_dir(directory)
        .with_context(|| format!("unable to read session directory {}", directory.display()))?
    {
        let entry = entry.with_context(|| {
            format!(
                "unable to enumerate session directory {}",
                directory.display()
            )
        })?;
        let path = entry.path();
        if path.extension().and_then(|value| value.to_str()) != Some("json")
            || path.file_name().and_then(|value| value.to_str()) == Some("_session.json")
        {
            continue;
        }
        records.push(read_record_file(&path, expected_session_id)?);
    }
    records.sort_by(|left, right| record_sort_key(right).cmp(&record_sort_key(left)));
    Ok(records)
}

pub(crate) fn list_records(workspace: &Path, session_id: &str) -> Result<Vec<Value>> {
    list_records_in_directory(&session_directory(workspace, session_id), Some(session_id))
}

pub(crate) fn latest_record(workspace: &Path, session_id: &str) -> Result<Option<Value>> {
    Ok(list_records(workspace, session_id)?.into_iter().next())
}

pub(crate) fn read_record(
    workspace: &Path,
    session_id: &str,
    trace_id: &str,
) -> Result<Option<Value>> {
    let trace_id = trace_id.trim();
    if trace_id.is_empty() {
        return Ok(None);
    }
    Ok(list_records(workspace, session_id)?
        .into_iter()
        .find(|record| record.get("traceId").and_then(Value::as_str) == Some(trace_id)))
}

pub(crate) fn delete_record(workspace: &Path, session_id: &str, trace_id: &str) -> Result<bool> {
    let Some(path) = existing_trace_path(workspace, session_id, trace_id)? else {
        return Ok(false);
    };
    fs::remove_file(&path)
        .with_context(|| format!("unable to remove trace record {}", path.display()))?;
    Ok(true)
}

pub(crate) fn clear_records(workspace: &Path, session_id: &str) -> Result<usize> {
    let directory = session_directory(workspace, session_id);
    let records = list_records(workspace, session_id)?;
    for record in &records {
        let trace_id = record
            .get("traceId")
            .and_then(Value::as_str)
            .context("validated trace record lost traceId")?;
        anyhow::ensure!(
            delete_record(workspace, session_id, trace_id)?,
            "trace record disappeared while clearing session"
        );
    }
    if directory.exists() {
        let marker = directory.join("_session.json");
        let now = Utc::now().to_rfc3339();
        atomic_write(
            &marker,
            &serde_json::to_vec_pretty(&json!({
                "sessionId": normalized_session_id(session_id),
                "firstPrompt": "",
                "createdAt": now,
                "updatedAt": now,
                "traceCount": 0,
                "clearedAt": now,
            }))?,
        )?;
    }
    Ok(records.len())
}

pub(crate) fn delete_session(workspace: &Path, session_id: &str) -> Result<usize> {
    let directory = session_directory(workspace, session_id);
    if !directory.exists() {
        return Ok(0);
    }
    let root = sessions_root(workspace);
    anyhow::ensure!(
        directory.starts_with(&root) && directory != root,
        "unsafe session delete target"
    );
    let record_count = list_records(workspace, session_id)?.len();
    fs::remove_dir_all(&directory)
        .with_context(|| format!("unable to remove session directory {}", directory.display()))?;
    Ok(record_count.max(1))
}

pub(crate) fn list_session_summaries(workspace: &Path) -> Result<Vec<Value>> {
    let root = sessions_root(workspace);
    if !root.exists() {
        return Ok(Vec::new());
    }
    let mut summaries = Vec::new();
    for entry in fs::read_dir(&root)
        .with_context(|| format!("unable to read sessions root {}", root.display()))?
    {
        let entry = entry
            .with_context(|| format!("unable to enumerate sessions root {}", root.display()))?;
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path)
            .with_context(|| format!("unable to inspect session entry {}", path.display()))?;
        anyhow::ensure!(
            !metadata.file_type().is_symlink(),
            "session entry cannot be a symbolic link: {}",
            path.display()
        );
        if !metadata.is_dir() {
            continue;
        }
        let records = list_records_in_directory(&path, None)?;
        if let Some(first) = records.first() {
            let session_id = first
                .get("sessionId")
                .and_then(Value::as_str)
                .context("validated session record lost sessionId")?;
            anyhow::ensure!(
                records
                    .iter()
                    .all(|record| record.get("sessionId").and_then(Value::as_str)
                        == Some(session_id)),
                "session directory contains records for multiple sessions: {}",
                path.display()
            );
            let oldest = records
                .last()
                .context("non-empty records have an oldest entry")?;
            summaries.push(json!({
                "sessionId": session_id,
                "firstPrompt": oldest.get("prompt").and_then(Value::as_str).unwrap_or_default(),
                "createdAt": oldest.get("createdAt").and_then(Value::as_str).unwrap_or_default(),
                "updatedAt": first.get("updatedAt").and_then(Value::as_str).unwrap_or_default(),
                "traceCount": records.len(),
            }));
            continue;
        }
        let marker = path.join("_session.json");
        if marker.exists() {
            let value: Value =
                serde_json::from_slice(&fs::read(&marker).with_context(|| {
                    format!("unable to read session marker {}", marker.display())
                })?)
                .with_context(|| format!("invalid session marker JSON {}", marker.display()))?;
            anyhow::ensure!(
                value.is_object(),
                "session marker must be an object: {}",
                marker.display()
            );
            summaries.push(value);
        }
    }
    summaries.sort_by(|left, right| record_sort_key(right).cmp(&record_sort_key(left)));
    Ok(summaries)
}

pub(crate) fn persist_trace_record(
    workspace: &Path,
    session_id: &str,
    record: &Value,
) -> Result<()> {
    let trace_id = record
        .get("traceId")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| anyhow::anyhow!("trace record has no traceId"))?;
    let path = existing_trace_path(workspace, session_id, trace_id)?
        .unwrap_or_else(|| trace_path(workspace, session_id, trace_id));
    let bytes = serde_json::to_vec_pretty(record)?;
    atomic_write(&path, &bytes)
}

fn existing_trace_path(
    workspace: &Path,
    session_id: &str,
    trace_id: &str,
) -> Result<Option<PathBuf>> {
    let directory = session_directory(workspace, session_id);
    if !directory.exists() {
        return Ok(None);
    }
    for entry in fs::read_dir(&directory)
        .with_context(|| format!("unable to read session directory {}", directory.display()))?
    {
        let path = entry
            .with_context(|| {
                format!(
                    "unable to enumerate session directory {}",
                    directory.display()
                )
            })?
            .path();
        if path.extension().and_then(|value| value.to_str()) != Some("json")
            || path.file_name().and_then(|value| value.to_str()) == Some("_session.json")
        {
            continue;
        }
        let value = read_record_file(&path, Some(session_id))?;
        if value.get("traceId").and_then(Value::as_str) == Some(trace_id) {
            return Ok(Some(path));
        }
    }
    Ok(None)
}

fn atomic_write(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| anyhow::anyhow!("atomic write path has no parent"))?;
    fs::create_dir_all(parent)?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("record"),
        Uuid::new_v4()
    ));
    fs::write(&temporary, bytes)?;
    if path.exists() {
        let backup = parent.join(format!(
            ".{}.{}.bak",
            path.file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("record"),
            Uuid::new_v4()
        ));
        fs::rename(path, &backup)?;
        match fs::rename(&temporary, path) {
            Ok(()) => {
                let _ = fs::remove_file(backup);
                Ok(())
            }
            Err(error) => {
                let _ = fs::rename(backup, path);
                let _ = fs::remove_file(&temporary);
                Err(error.into())
            }
        }
    } else {
        fs::rename(&temporary, path).map_err(Into::into)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record(workspace: &Path, trace_id: &str) -> Value {
        json!({
            "traceId": trace_id,
            "sessionId": "session",
            "workspaceRoot": workspace.to_string_lossy(),
            "status": "completed",
            "createdAt": "2026-01-01T00:00:00+00:00",
            "updatedAt": "2026-01-01T00:00:00+00:00"
        })
    }

    #[test]
    fn replacement_accept_updates_latest_record_and_rollback_is_dialogue_only() {
        let root = tempfile::tempdir().expect("workspace");
        let workspace = root.path().join("workspace");
        let home = root.path().join("home");
        fs::create_dir_all(&workspace).expect("workspace dir");
        let session_id = Uuid::new_v4();
        let session_path = home.join("sessions").join(format!("{session_id}.json"));
        fs::create_dir_all(session_path.parent().expect("session parent")).expect("session dir");
        fs::write(
            &session_path,
            serde_json::to_vec(&json!({
                "messages": [
                    {"role": "user", "content": "old"},
                    {"role": "assistant", "content": "answer"}
                ]
            }))
            .expect("session bytes"),
        )
        .expect("session");
        let original = record(&workspace, "old-trace");
        persist_trace_record(&workspace, "session", &original).expect("trace");
        let mut replacement = ReplacementTransaction::prepare(
            &workspace,
            &home,
            "session",
            "old-trace",
            "new-trace",
            "new prompt",
            Some(session_id),
        )
        .expect("prepare");
        let rolled: Value = serde_json::from_slice(&fs::read(&session_path).expect("rolled bytes"))
            .expect("rolled session");
        assert_eq!(rolled["messages"].as_array().expect("messages").len(), 0);
        replacement.accept().expect("accept");
        let accepted: Value = serde_json::from_slice(
            &fs::read(trace_path(&workspace, "session", "old-trace")).expect("accepted trace"),
        )
        .expect("accepted record");
        assert_eq!(accepted["replacement"]["status"], "accepted");
        assert!(replacement.is_accepted());
    }

    #[test]
    fn replacement_restore_restores_session_bytes_and_original_status() {
        let root = tempfile::tempdir().expect("workspace");
        let workspace = root.path().join("workspace");
        let home = root.path().join("home");
        fs::create_dir_all(&workspace).expect("workspace dir");
        let session_id = Uuid::new_v4();
        let session_path = home.join("sessions").join(format!("{session_id}.json"));
        fs::create_dir_all(session_path.parent().expect("session parent")).expect("session dir");
        let original_session = br#"{"messages":[{"role":"user","content":"old"}]}"#;
        fs::write(&session_path, original_session).expect("session");
        persist_trace_record(&workspace, "session", &record(&workspace, "old-trace"))
            .expect("trace");
        let mut replacement = ReplacementTransaction::prepare(
            &workspace,
            &home,
            "session",
            "old-trace",
            "new-trace",
            "new prompt",
            Some(session_id),
        )
        .expect("prepare");
        replacement.restore("provider_error").expect("restore");
        assert_eq!(
            fs::read(&session_path).expect("restored session"),
            original_session
        );
        let restored: Value = serde_json::from_slice(
            &fs::read(trace_path(&workspace, "session", "old-trace")).expect("restored trace"),
        )
        .expect("restored record");
        assert_eq!(restored["status"], "completed");
        assert_eq!(restored["replacement"]["status"], "restored");
    }

    #[test]
    fn persisted_change_ledger_uses_only_server_observed_safe_write_paths() {
        let root = tempfile::tempdir().expect("workspace");
        let workspace = root.path().canonicalize().expect("canonical workspace");
        let absolute = workspace.join("chapters/002.md");
        let events = vec![
            (
                "ToolDone".to_owned(),
                json!({
                    "tool_name": "write_file",
                    "is_error": false,
                    "arguments": {
                        "path": "chapters/001.md",
                        "backupPath": "../escape.md",
                        "nested": {"output_path": absolute},
                    }
                }),
            ),
            (
                "ToolDone".to_owned(),
                json!({
                    "tool_name": "read_file",
                    "is_error": false,
                    "arguments": {"path": "private/read-only.md"}
                }),
            ),
            (
                "ToolDone".to_owned(),
                json!({
                    "tool_name": "edit_file",
                    "is_error": true,
                    "arguments": {"path": "failed.md"}
                }),
            ),
            (
                "AgentCompleted".to_owned(),
                json!({"writtenPaths": ["chapters/003.md", "../../outside.md"]}),
            ),
            (
                "GitCommitResult".to_owned(),
                json!({
                    "changedFiles": ["chapters/001.md", "chapters/003.md"],
                    "commitHash": "abcdef1234567890"
                }),
            ),
        ];
        persist_execution_record_with_events(ExecutionRecordInput {
            workspace: &workspace,
            session_id: "session",
            trace_id: "trace",
            prompt: "write",
            status: "completed",
            events: &events,
            reply: "done",
            provider_id: "provider",
            model: "model",
        })
        .expect("persist record");
        let record = read_record(&workspace, "session", "trace")
            .expect("read record")
            .expect("record");
        assert_eq!(
            record["changeLedger"]["changedFiles"],
            json!(["chapters/001.md", "chapters/002.md", "chapters/003.md"])
        );
        assert_eq!(record["changeLedger"]["changedFileCount"], 3);
        assert_eq!(record["changeLedger"]["commitHash"], "abcdef1234567890");
        assert_eq!(record["changeLedger"]["diffSource"], "commit");
    }
}
