use anyhow::Result;
use chrono::Utc;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
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
        let latest = latest_record(&workspace, &session_id).ok_or_else(|| {
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
        "tasks": [],
        "audit": [],
        "createdAt": now,
        "updatedAt": Utc::now().to_rfc3339(),
    });
    persist_trace_record(workspace, session_id, &record)
}

fn normalized_session_id(value: &str) -> String {
    let value = value.trim();
    if value.is_empty() {
        "default".to_owned()
    } else {
        value.to_owned()
    }
}

fn session_directory(workspace: &Path, session_id: &str) -> PathBuf {
    let normalized = normalized_session_id(session_id);
    let safe = normalized
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-'));
    let directory = if safe {
        normalized
    } else {
        format!("sha256-{:x}", Sha256::digest(normalized.as_bytes()))
    };
    workspace
        .join(".storydex")
        .join(".agent")
        .join("sessions")
        .join(directory)
}

fn trace_path(workspace: &Path, session_id: &str, trace_id: &str) -> PathBuf {
    session_directory(workspace, session_id).join(format!("{trace_id}.json"))
}

fn latest_record(workspace: &Path, session_id: &str) -> Option<Value> {
    let directory = session_directory(workspace, session_id);
    let entries = fs::read_dir(directory).ok()?;
    let mut records = entries
        .filter_map(|entry| entry.ok())
        .filter(|entry| entry.path().extension().and_then(|value| value.to_str()) == Some("json"))
        .filter_map(|entry| {
            let value: Value = serde_json::from_slice(&fs::read(entry.path()).ok()?).ok()?;
            if value.get("sessionId").and_then(Value::as_str)
                == Some(normalized_session_id(session_id).as_str())
            {
                Some(value)
            } else {
                None
            }
        })
        .collect::<Vec<_>>();
    records.sort_by(|left, right| {
        let left_key = (
            left.get("updatedAt")
                .and_then(Value::as_str)
                .unwrap_or_default(),
            left.get("createdAt")
                .and_then(Value::as_str)
                .unwrap_or_default(),
        );
        let right_key = (
            right
                .get("updatedAt")
                .and_then(Value::as_str)
                .unwrap_or_default(),
            right
                .get("createdAt")
                .and_then(Value::as_str)
                .unwrap_or_default(),
        );
        left_key.cmp(&right_key)
    });
    records.pop()
}

fn persist_trace_record(workspace: &Path, session_id: &str, record: &Value) -> Result<()> {
    let trace_id = record
        .get("traceId")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| anyhow::anyhow!("trace record has no traceId"))?;
    let path = existing_trace_path(workspace, session_id, trace_id)
        .unwrap_or_else(|| trace_path(workspace, session_id, trace_id));
    let bytes = serde_json::to_vec_pretty(record)?;
    atomic_write(&path, &bytes)
}

fn existing_trace_path(workspace: &Path, session_id: &str, trace_id: &str) -> Option<PathBuf> {
    let directory = session_directory(workspace, session_id);
    let entries = fs::read_dir(directory).ok()?;
    entries
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| path.extension().and_then(|value| value.to_str()) == Some("json"))
        .find(|path| {
            fs::read(path)
                .ok()
                .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
                .and_then(|value| {
                    value
                        .get("traceId")
                        .and_then(Value::as_str)
                        .map(|value| value == trace_id)
                })
                .unwrap_or(false)
        })
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
}
