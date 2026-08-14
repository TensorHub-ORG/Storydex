use crate::ChatMessage;
use crate::ContextState;
use crate::LoopState;
use crate::PlanState;
use crate::TokenUsage;
use crate::ToolCall;
use anyhow::Context;
use anyhow::Result;
use chrono::DateTime;
use chrono::Utc;
use serde::Deserialize;
use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::cmp::Reverse;
use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::path::Path;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::Mutex;
use std::thread;
use std::time::Duration;
use std::time::Instant;
use uuid::Uuid;

pub const SESSION_SCHEMA_VERSION: u32 = 1;
pub const COMPACTION_CHECKPOINT_SCHEMA_VERSION: u32 = 1;
const SESSION_PUBLISH_ATTEMPTS: usize = 5;

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ToolCallCheckpoint {
    pub id: String,
    pub name: String,
    pub arguments: Value,
    #[serde(default)]
    pub result: Option<String>,
    #[serde(default)]
    pub complete: bool,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct CompactionCheckpoint {
    pub schema_version: u32,
    #[serde(default)]
    pub plan: Option<PlanState>,
    #[serde(default)]
    pub loop_state: Option<LoopState>,
    #[serde(default)]
    pub permission_mode: String,
    #[serde(default)]
    pub target: String,
    #[serde(default)]
    pub evidence_revisions: Vec<Value>,
    #[serde(default)]
    pub tool_calls: Vec<ToolCallCheckpoint>,
    pub source_message_count: usize,
    pub source_message_hash: String,
    pub checkpoint_hash: String,
    pub created_at: DateTime<Utc>,
}

impl CompactionCheckpoint {
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != COMPACTION_CHECKPOINT_SCHEMA_VERSION {
            return Err(format!(
                "unsupported compaction checkpoint schema {}; expected {}",
                self.schema_version, COMPACTION_CHECKPOINT_SCHEMA_VERSION
            ));
        }
        if let Some(plan) = &self.plan {
            plan.validate()?;
        }
        let mut ids = std::collections::BTreeSet::new();
        for call in &self.tool_calls {
            if call.id.trim().is_empty() || !ids.insert(call.id.clone()) {
                return Err(
                    "compaction checkpoint contains duplicate or empty tool call ids".into(),
                );
            }
            if call.name.trim().is_empty() {
                return Err(format!(
                    "compaction checkpoint tool call {} has no name",
                    call.id
                ));
            }
            if call.complete && call.result.is_none() {
                return Err(format!("completed tool call {} has no result", call.id));
            }
        }
        if self.source_message_count == 0 {
            return Err("compaction checkpoint has no source messages".into());
        }
        if self.source_message_hash.trim().is_empty() || self.checkpoint_hash.trim().is_empty() {
            return Err("compaction checkpoint is missing integrity hashes".into());
        }
        Ok(())
    }

    pub fn from_session(session: &Session) -> Result<Self, String> {
        if session.messages.is_empty() {
            return Err("compaction checkpoint requires at least one message".into());
        }
        let mut calls: BTreeMap<String, (ToolCall, Option<String>)> = BTreeMap::new();
        for message in &session.messages {
            for call in &message.tool_calls {
                if calls.contains_key(&call.id) {
                    return Err(format!("duplicate tool call id {}", call.id));
                }
                calls.insert(call.id.clone(), (call.clone(), None));
            }
            if let Some(call_id) = &message.tool_call_id {
                let Some((_call, result)) = calls.get_mut(call_id) else {
                    return Err(format!("orphan tool result {}", call_id));
                };
                if result.is_some() {
                    return Err(format!("duplicate tool result {}", call_id));
                }
                *result = Some(message.content.clone());
            }
        }
        let tool_calls = calls
            .into_values()
            .map(|(call, result)| ToolCallCheckpoint {
                id: call.id,
                name: call.name,
                arguments: call.arguments,
                complete: result.is_some(),
                result,
            })
            .collect::<Vec<_>>();
        let source_bytes =
            serde_json::to_vec(&session.messages).map_err(|error| error.to_string())?;
        let source_message_hash = format!("sha256:{:x}", Sha256::digest(source_bytes));
        let context = session
            .checkpoint_context
            .as_ref()
            .and_then(Value::as_object);
        let permission_mode = context
            .and_then(|value| {
                value
                    .get("permissionMode")
                    .or_else(|| value.get("permission_mode"))
            })
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let target = context
            .and_then(|value| value.get("target").or_else(|| value.get("targetPath")))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let evidence_revisions = context
            .and_then(|value| value.get("evidenceRevisions"))
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let mut checkpoint = Self {
            schema_version: COMPACTION_CHECKPOINT_SCHEMA_VERSION,
            plan: session.plan.clone(),
            loop_state: session.loop_state.clone(),
            permission_mode,
            target,
            evidence_revisions,
            tool_calls,
            source_message_count: session.messages.len(),
            source_message_hash,
            checkpoint_hash: String::new(),
            created_at: Utc::now(),
        };
        let bytes = serde_json::to_vec(&checkpoint).map_err(|error| error.to_string())?;
        checkpoint.checkpoint_hash = format!("sha256:{:x}", Sha256::digest(bytes));
        checkpoint.validate()?;
        Ok(checkpoint)
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct Session {
    pub id: Uuid,
    pub provider_id: String,
    pub model: String,
    pub cwd: PathBuf,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub messages: Vec<ChatMessage>,
    pub usage: TokenUsage,
    #[serde(default)]
    pub context: ContextState,
    #[serde(default)]
    pub plan: Option<PlanState>,
    #[serde(default)]
    pub loop_state: Option<LoopState>,
    #[serde(default)]
    pub hooks_started: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub checkpoint_context: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub compaction_checkpoint: Option<CompactionCheckpoint>,
}

impl Session {
    pub fn new(provider_id: impl Into<String>, model: impl Into<String>, cwd: PathBuf) -> Self {
        let now = Utc::now();
        Self {
            id: Uuid::new_v4(),
            provider_id: provider_id.into(),
            model: model.into(),
            cwd,
            created_at: now,
            updated_at: now,
            messages: Vec::new(),
            usage: TokenUsage::default(),
            context: ContextState::default(),
            plan: None,
            loop_state: None,
            hooks_started: false,
            checkpoint_context: None,
            compaction_checkpoint: None,
        }
    }

    pub fn switch_model(&mut self, provider_id: impl Into<String>, model: impl Into<String>) {
        self.provider_id = provider_id.into();
        self.model = model.into();
        self.touch();
    }

    pub fn touch(&mut self) {
        self.updated_at = Utc::now();
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionSummary {
    pub id: Uuid,
    pub provider_id: String,
    pub model: String,
    pub cwd: PathBuf,
    pub updated_at: DateTime<Utc>,
    pub preview: String,
}

#[derive(Clone)]
pub struct SessionStore {
    directory: PathBuf,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CheckpointPriority {
    Buffered,
    Immediate,
}

pub trait CheckpointSink: Send + Sync {
    fn checkpoint(&self, session: &Session, priority: CheckpointPriority) -> Result<()>;
    fn flush(&self) -> Result<()>;
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct SessionCheckpointStats {
    pub requested_checkpoints: u64,
    pub deduplicated_checkpoints: u64,
    pub coalesced_checkpoints: u64,
    pub persisted_writes: u64,
    pub write_attempts: u64,
    pub flushes: u64,
    pub write_elapsed: Duration,
}

#[derive(Default)]
struct SessionCheckpointState {
    last_persisted: Option<Session>,
    pending: Option<Session>,
    last_write_at: Option<Instant>,
    stats: SessionCheckpointStats,
}

#[derive(Clone)]
pub struct SessionCheckpointBuffer {
    store: SessionStore,
    min_write_interval: Duration,
    state: Arc<Mutex<SessionCheckpointState>>,
}

impl SessionCheckpointBuffer {
    pub fn new(store: SessionStore, min_write_interval: Duration) -> Self {
        Self {
            store,
            min_write_interval,
            state: Arc::new(Mutex::new(SessionCheckpointState::default())),
        }
    }

    pub fn checkpoint(&self, session: &Session) -> Result<()> {
        self.checkpoint_with_priority(session, CheckpointPriority::Buffered)
    }

    pub fn checkpoint_immediate(&self, session: &Session) -> Result<()> {
        self.checkpoint_with_priority(session, CheckpointPriority::Immediate)
    }

    pub fn flush(&self) -> Result<()> {
        let mut state = self.lock_state()?;
        state.stats.flushes = state.stats.flushes.saturating_add(1);
        let Some(pending) = state.pending.take() else {
            return Ok(());
        };
        self.persist_locked(&mut state, pending)
    }

    pub fn stats(&self) -> Result<SessionCheckpointStats> {
        Ok(self.lock_state()?.stats)
    }

    fn checkpoint_with_priority(
        &self,
        session: &Session,
        priority: CheckpointPriority,
    ) -> Result<()> {
        let mut state = self.lock_state()?;
        state.stats.requested_checkpoints = state.stats.requested_checkpoints.saturating_add(1);
        if state.last_persisted.as_ref() == Some(session) {
            state.stats.deduplicated_checkpoints =
                state.stats.deduplicated_checkpoints.saturating_add(1);
            return Ok(());
        }

        let write_is_due = state
            .last_write_at
            .is_none_or(|last_write| last_write.elapsed() >= self.min_write_interval);
        if state.pending.as_ref() == Some(session) {
            if (priority == CheckpointPriority::Immediate || write_is_due)
                && let Some(pending) = state.pending.take()
            {
                return self.persist_locked(&mut state, pending);
            }
            state.stats.deduplicated_checkpoints =
                state.stats.deduplicated_checkpoints.saturating_add(1);
            return Ok(());
        }
        if priority == CheckpointPriority::Immediate || write_is_due {
            if state.pending.take().is_some() {
                state.stats.coalesced_checkpoints =
                    state.stats.coalesced_checkpoints.saturating_add(1);
            }
            return self.persist_locked(&mut state, session.clone());
        }

        if state.pending.replace(session.clone()).is_some() {
            state.stats.coalesced_checkpoints = state.stats.coalesced_checkpoints.saturating_add(1);
        }
        Ok(())
    }

    fn persist_locked(&self, state: &mut SessionCheckpointState, session: Session) -> Result<()> {
        if state.last_persisted.as_ref() == Some(&session) {
            state.stats.deduplicated_checkpoints =
                state.stats.deduplicated_checkpoints.saturating_add(1);
            return Ok(());
        }
        state.stats.write_attempts = state.stats.write_attempts.saturating_add(1);
        let started = Instant::now();
        let result = self.store.save(&session);
        state.stats.write_elapsed = state.stats.write_elapsed.saturating_add(started.elapsed());
        match result {
            Ok(()) => {
                state.stats.persisted_writes = state.stats.persisted_writes.saturating_add(1);
                state.last_write_at = Some(Instant::now());
                state.last_persisted = Some(session);
                Ok(())
            }
            Err(error) => {
                state.pending = Some(session);
                Err(error)
            }
        }
    }

    fn lock_state(&self) -> Result<std::sync::MutexGuard<'_, SessionCheckpointState>> {
        self.state
            .lock()
            .map_err(|_| anyhow::anyhow!("session checkpoint buffer lock was poisoned"))
    }
}

impl CheckpointSink for SessionCheckpointBuffer {
    fn checkpoint(&self, session: &Session, priority: CheckpointPriority) -> Result<()> {
        self.checkpoint_with_priority(session, priority)
    }

    fn flush(&self) -> Result<()> {
        SessionCheckpointBuffer::flush(self)
    }
}

impl SessionStore {
    pub fn new(coomi_home: impl AsRef<Path>) -> Self {
        Self {
            directory: coomi_home.as_ref().join("sessions"),
        }
    }

    pub fn save(&self, session: &Session) -> Result<()> {
        fs::create_dir_all(&self.directory).with_context(|| {
            format!(
                "failed to create session directory {}",
                self.directory.display()
            )
        })?;
        let path = self.path(session.id);
        let temporary_path = self
            .directory
            .join(format!(".{}.{}.tmp", session.id, Uuid::new_v4()));
        let mut payload = serde_json::to_value(session)?;
        payload
            .as_object_mut()
            .context("serialized session must be a JSON object")?
            .insert(
                "schema_version".into(),
                serde_json::json!(SESSION_SCHEMA_VERSION),
            );
        let bytes = serde_json::to_vec_pretty(&payload)?;
        let save_result = (|| -> Result<()> {
            let mut file = fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&temporary_path)
                .with_context(|| {
                    format!(
                        "failed to create temporary session {}",
                        temporary_path.display()
                    )
                })?;
            file.write_all(&bytes).with_context(|| {
                format!(
                    "failed to write temporary session {}",
                    temporary_path.display()
                )
            })?;
            file.sync_all().with_context(|| {
                format!(
                    "failed to flush temporary session {}",
                    temporary_path.display()
                )
            })?;
            drop(file);
            publish_session_file(&temporary_path, &path)
                .with_context(|| format!("failed to publish session {}", path.display()))?;
            Ok(())
        })();
        if save_result.is_err() {
            let _ = fs::remove_file(&temporary_path);
        }
        save_result
    }

    pub fn load(&self, id: Uuid) -> Result<Session> {
        let path = self.path(id);
        let bytes = fs::read(&path)
            .with_context(|| format!("failed to read session {}", path.display()))?;
        let payload: serde_json::Value = serde_json::from_slice(&bytes)
            .with_context(|| format!("invalid session file {}", path.display()))?;
        let schema_version = match payload.get("schema_version") {
            None => u64::from(SESSION_SCHEMA_VERSION),
            Some(value) => value.as_u64().unwrap_or(0),
        };
        anyhow::ensure!(
            schema_version == u64::from(SESSION_SCHEMA_VERSION),
            "session {} has unsupported schema version {}; expected {}",
            path.display(),
            schema_version,
            SESSION_SCHEMA_VERSION
        );
        let session: Session = serde_json::from_value(payload)
            .with_context(|| format!("invalid session file {}", path.display()))?;
        anyhow::ensure!(
            session.id == id,
            "session id {} in {} does not match requested id {}",
            session.id,
            path.display(),
            id
        );
        Ok(session)
    }

    pub fn delete(&self, id: Uuid) -> Result<bool> {
        let path = self.path(id);
        if !path.exists() {
            return Ok(false);
        }
        fs::remove_file(&path)
            .with_context(|| format!("failed to delete session {}", path.display()))?;
        Ok(true)
    }

    pub fn latest(&self, cwd: Option<&Path>) -> Result<Option<Session>> {
        let summaries = self.list(cwd)?;
        summaries
            .first()
            .map(|summary| self.load(summary.id))
            .transpose()
    }

    pub fn list(&self, cwd: Option<&Path>) -> Result<Vec<SessionSummary>> {
        if !self.directory.exists() {
            return Ok(Vec::new());
        }

        let canonical_filter = cwd.and_then(|path| path.canonicalize().ok());
        let mut summaries = Vec::new();
        for entry in fs::read_dir(&self.directory)? {
            let entry = entry?;
            if entry.path().extension().and_then(|value| value.to_str()) != Some("json") {
                continue;
            }
            let Ok(bytes) = fs::read(entry.path()) else {
                continue;
            };
            let Ok(session) = serde_json::from_slice::<Session>(&bytes) else {
                continue;
            };
            if let Some(filter) = &canonical_filter
                && session.cwd.canonicalize().ok().as_ref() != Some(filter)
            {
                continue;
            }
            let preview = session
                .messages
                .iter()
                .find(|message| message.role == crate::Role::User)
                .map(|message| compact_preview(&message.content))
                .unwrap_or_default();
            summaries.push(SessionSummary {
                id: session.id,
                provider_id: session.provider_id,
                model: session.model,
                cwd: session.cwd,
                updated_at: session.updated_at,
                preview,
            });
        }
        summaries.sort_by_key(|summary| Reverse(summary.updated_at));
        Ok(summaries)
    }

    pub fn path(&self, id: Uuid) -> PathBuf {
        self.directory.join(format!("{id}.json"))
    }
}

fn publish_session_file(temporary_path: &Path, path: &Path) -> Result<()> {
    let mut last_error = None;
    for attempt in 0..SESSION_PUBLISH_ATTEMPTS {
        match fs::rename(temporary_path, path) {
            Ok(()) => return Ok(()),
            Err(error) => {
                last_error = Some(error);
            }
        }

        #[cfg(windows)]
        if path.exists() {
            let backup_path = path.with_extension(format!("json.{}.bak", Uuid::new_v4()));
            if fs::rename(path, &backup_path).is_ok() {
                match fs::rename(temporary_path, path) {
                    Ok(()) => {
                        let _ = fs::remove_file(backup_path);
                        return Ok(());
                    }
                    Err(error) => {
                        let restore_result = fs::rename(&backup_path, path);
                        if let Err(restore_error) = restore_result {
                            return Err(anyhow::anyhow!(
                                "failed to publish replacement ({error}); failed to restore previous session ({restore_error})"
                            ));
                        }
                        last_error = Some(error);
                    }
                }
            }
        }

        if attempt + 1 < SESSION_PUBLISH_ATTEMPTS {
            thread::sleep(Duration::from_millis(25 * (attempt as u64 + 1)));
        }
    }
    Err(last_error
        .map(anyhow::Error::from)
        .unwrap_or_else(|| anyhow::anyhow!("session publish failed without an I/O error")))
}

fn compact_preview(value: &str) -> String {
    let single_line = value.split_whitespace().collect::<Vec<_>>().join(" ");
    single_line.chars().take(72).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn saves_lists_and_loads_sessions() {
        let home = tempfile::tempdir().expect("temporary home");
        let store = SessionStore::new(home.path());
        let mut session = Session::new("provider", "model", home.path().to_path_buf());
        session
            .messages
            .push(ChatMessage::user("inspect this project"));
        store.save(&session).expect("save session");

        let listed = store.list(Some(home.path())).expect("list sessions");
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].preview, "inspect this project");
        assert_eq!(store.load(session.id).expect("load session").model, "model");
        assert!(store.delete(session.id).expect("delete session"));
        assert!(!store.delete(session.id).expect("delete missing session"));
    }

    #[test]
    fn load_rejects_session_whose_embedded_id_does_not_match_requested_id() {
        let home = tempfile::tempdir().expect("temporary home");
        let store = SessionStore::new(home.path());
        let session = Session::new("provider", "model", home.path().to_path_buf());
        let requested_id = Uuid::new_v4();
        std::fs::create_dir_all(home.path().join("sessions")).expect("create sessions");
        std::fs::write(
            store.path(requested_id),
            serde_json::to_vec_pretty(&session).expect("serialize mismatched session"),
        )
        .expect("write mismatched session");

        let error = store
            .load(requested_id)
            .expect_err("mismatched embedded session id must fail");
        assert!(error.to_string().contains("does not match"));
    }

    #[test]
    fn load_rejects_unsupported_session_schema_version() {
        let home = tempfile::tempdir().expect("temporary home");
        let store = SessionStore::new(home.path());
        let session = Session::new("provider", "model", home.path().to_path_buf());
        let mut payload = serde_json::to_value(&session).expect("serialize session");
        payload["schema_version"] = serde_json::json!(999);
        std::fs::create_dir_all(home.path().join("sessions")).expect("create sessions");
        std::fs::write(
            store.path(session.id),
            serde_json::to_vec_pretty(&payload).expect("serialize future session"),
        )
        .expect("write future session");

        let error = store
            .load(session.id)
            .expect_err("future session schema must fail closed");
        assert!(error.to_string().contains("schema version"));
    }

    #[test]
    fn save_atomically_replaces_a_previous_session() {
        let home = tempfile::tempdir().expect("temporary home");
        let store = SessionStore::new(home.path());
        let mut session = Session::new("provider", "model", home.path().to_path_buf());
        session.messages.push(ChatMessage::user("first"));
        store.save(&session).expect("save first revision");
        session
            .messages
            .push(ChatMessage::assistant("second", Vec::new()));
        store.save(&session).expect("replace session revision");

        let restored = store.load(session.id).expect("load replaced session");
        assert_eq!(restored.messages.len(), 2);
        let temporary_files = std::fs::read_dir(home.path().join("sessions"))
            .expect("list sessions")
            .filter_map(Result::ok)
            .filter(|entry| entry.file_name().to_string_lossy().ends_with(".tmp"))
            .count();
        assert_eq!(temporary_files, 0);
    }

    #[test]
    fn checkpoint_buffer_deduplicates_and_coalesces_latest_snapshot() {
        let home = tempfile::tempdir().expect("temporary home");
        let store = SessionStore::new(home.path());
        let buffer = SessionCheckpointBuffer::new(store.clone(), Duration::from_secs(60));
        let mut session = Session::new("provider", "model", home.path().to_path_buf());
        session.messages.push(ChatMessage::user("first revision"));

        buffer
            .checkpoint(&session)
            .expect("persist first checkpoint");
        for _ in 0..8 {
            buffer
                .checkpoint(&session)
                .expect("deduplicate identical checkpoint");
        }

        session.messages[0].content = "second revision".into();
        buffer.checkpoint(&session).expect("buffer second revision");
        session.messages[0].content = "latest revision".into();
        buffer
            .checkpoint(&session)
            .expect("coalesce latest revision");

        assert_eq!(
            store
                .load(session.id)
                .expect("load first checkpoint")
                .messages[0]
                .content,
            "first revision"
        );
        buffer.flush().expect("flush latest checkpoint");

        let restored = store.load(session.id).expect("load latest checkpoint");
        assert_eq!(restored.messages[0].content, "latest revision");
        let stats = buffer.stats().expect("checkpoint stats");
        assert_eq!(stats.persisted_writes, 2);
        assert_eq!(stats.write_attempts, 2);
        assert_eq!(stats.deduplicated_checkpoints, 8);
        assert_eq!(stats.coalesced_checkpoints, 1);
    }

    #[test]
    fn immediate_checkpoint_persists_an_identical_pending_snapshot() {
        let home = tempfile::tempdir().expect("temporary home");
        let store = SessionStore::new(home.path());
        let buffer = SessionCheckpointBuffer::new(store.clone(), Duration::from_secs(60));
        let mut session = Session::new("provider", "model", home.path().to_path_buf());
        session.messages.push(ChatMessage::user("persisted"));
        buffer
            .checkpoint(&session)
            .expect("persist first checkpoint");

        session.messages[0].content = "pending".into();
        buffer.checkpoint(&session).expect("buffer checkpoint");
        buffer
            .checkpoint_immediate(&session)
            .expect("force pending checkpoint to disk");

        assert_eq!(
            store
                .load(session.id)
                .expect("load forced checkpoint")
                .messages[0]
                .content,
            "pending"
        );
        assert_eq!(
            buffer.stats().expect("checkpoint stats").persisted_writes,
            2
        );
    }

    #[cfg(windows)]
    #[test]
    fn windows_checkpoint_write_count_and_elapsed_time_regression() {
        let home = tempfile::tempdir().expect("temporary home");
        let store = SessionStore::new(home.path());
        let buffer = SessionCheckpointBuffer::new(store.clone(), Duration::from_secs(60));
        let mut session = Session::new("provider", "model", home.path().to_path_buf());
        session.messages.push(ChatMessage::user("revision 0"));
        let started = Instant::now();

        buffer
            .checkpoint(&session)
            .expect("persist initial checkpoint");
        for revision in 1..=128 {
            session.messages[0].content = format!("revision {revision}");
            buffer
                .checkpoint(&session)
                .expect("coalesce checkpoint revision");
        }
        buffer.flush().expect("flush checkpoint batch");
        let elapsed = started.elapsed();

        let stats = buffer.stats().expect("checkpoint stats");
        assert_eq!(stats.requested_checkpoints, 129);
        assert_eq!(stats.persisted_writes, 2);
        assert_eq!(stats.write_attempts, 2);
        assert_eq!(stats.coalesced_checkpoints, 127);
        assert!(stats.write_elapsed <= elapsed);
        assert!(
            stats.write_elapsed < Duration::from_secs(3),
            "two Windows checkpoint writes took {:?}",
            stats.write_elapsed
        );
        assert!(
            elapsed < Duration::from_secs(5),
            "batched Windows checkpoint regression took {elapsed:?}"
        );
        assert_eq!(
            store
                .load(session.id)
                .expect("load final revision")
                .messages[0]
                .content,
            "revision 128"
        );
    }

    #[test]
    fn compaction_checkpoint_preserves_structured_state_and_tool_pairs() {
        let home = tempfile::tempdir().expect("temporary home");
        let mut session = Session::new("provider", "model", home.path().to_path_buf());
        session.plan = Some(PlanState {
            explanation: Some("inspect before edit".into()),
            steps: vec![crate::PlanStep {
                step: "Inspect chapter".into(),
                status: crate::PlanStepStatus::InProgress,
            }],
        });
        session.loop_state = Some(LoopState {
            objective: "Finish chapter audit".into(),
            status: crate::LoopStatus::Active,
            token_budget: Some(10_000),
            tokens_used: 120,
            time_used_seconds: 2,
            blocked_streak: 0,
            turns_completed: 1,
        });
        session.checkpoint_context = Some(serde_json::json!({
            "permissionMode": "plan_mode",
            "target": "chapters/001.md",
            "evidenceRevisions": [{"path": "chapters/001.md", "revision": "sha256:test"}],
        }));
        session.messages.push(ChatMessage::assistant(
            "",
            vec![ToolCall {
                id: "call-1".into(),
                name: "read_file".into(),
                arguments: serde_json::json!({"path": "chapters/001.md"}),
            }],
        ));
        session
            .messages
            .push(ChatMessage::tool("call-1", "success: chapter evidence"));

        let checkpoint = CompactionCheckpoint::from_session(&session).expect("build checkpoint");

        checkpoint.validate().expect("valid checkpoint");
        assert_eq!(checkpoint.permission_mode, "plan_mode");
        assert_eq!(checkpoint.target, "chapters/001.md");
        assert_eq!(checkpoint.evidence_revisions.len(), 1);
        assert_eq!(checkpoint.tool_calls.len(), 1);
        assert!(checkpoint.tool_calls[0].complete);
        assert_eq!(
            checkpoint.tool_calls[0].result.as_deref(),
            Some("success: chapter evidence")
        );
    }

    #[test]
    fn corrupt_tool_pair_checkpoint_fails_without_mutating_messages() {
        let home = tempfile::tempdir().expect("temporary home");
        let mut session = Session::new("provider", "model", home.path().to_path_buf());
        session
            .messages
            .push(ChatMessage::tool("orphan", "unexpected"));
        let before = session.messages.clone();

        let error = CompactionCheckpoint::from_session(&session)
            .expect_err("orphan result must fail checkpoint validation");

        assert!(error.contains("orphan tool result"));
        assert_eq!(session.messages, before);
        assert!(session.compaction_checkpoint.is_none());
    }
}
