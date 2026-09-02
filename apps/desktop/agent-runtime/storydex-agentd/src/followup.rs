use fs2::FileExt;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::fs;
use std::fs::{File, OpenOptions};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use uuid::Uuid;

const MAILBOX_VERSION: u32 = 1;
const EVENT_VERSION: u32 = 1;
const MAX_EVENTS: usize = 500;

#[derive(Clone, Default)]
pub(crate) struct FollowupStore {
    lock: Arc<Mutex<()>>,
}

#[derive(Debug)]
pub(crate) struct FollowupError {
    pub(crate) code: &'static str,
    pub(crate) message: String,
}

impl FollowupError {
    fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct FollowupMessage {
    pub(crate) message_id: String,
    pub(crate) session_id: String,
    pub(crate) active_trace_id: String,
    pub(crate) expected_trace_id: String,
    pub(crate) content: String,
    pub(crate) mode: String,
    pub(crate) status: String,
    pub(crate) status_detail: String,
    pub(crate) created_at: String,
    pub(crate) updated_at: String,
    pub(crate) sequence: u64,
    pub(crate) dispatch_trace_id: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub(crate) segment_id: String,
    /// The Python runtime did not persist `previousTraceId` for pending messages.
    #[serde(default)]
    pub(crate) previous_trace_id: String,
    pub(crate) error: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct FollowupState {
    #[serde(rename = "_type")]
    pub(crate) kind: String,
    #[serde(rename = "_version")]
    pub(crate) version: u32,
    pub(crate) revision: u64,
    pub(crate) workspace_root: String,
    pub(crate) session_id: String,
    pub(crate) active_trace_id: String,
    #[serde(default)]
    pub(crate) last_trace_id: String,
    pub(crate) paused: bool,
    pub(crate) pause_reason: String,
    pub(crate) message_sequence: u64,
    pub(crate) event_sequence: u64,
    pub(crate) messages: Vec<FollowupMessage>,
    pub(crate) events: Vec<Value>,
    pub(crate) created_at: String,
    pub(crate) updated_at: String,
}

impl FollowupState {
    fn fresh(workspace: &Path, session_id: &str) -> Self {
        let now = now_iso();
        Self {
            kind: "FollowupMailbox".to_owned(),
            version: MAILBOX_VERSION,
            revision: 0,
            workspace_root: contract_path(workspace),
            session_id: normalize_session(session_id),
            active_trace_id: String::new(),
            last_trace_id: String::new(),
            paused: false,
            pause_reason: String::new(),
            message_sequence: 0,
            event_sequence: 0,
            messages: Vec::new(),
            events: Vec::new(),
            created_at: now.clone(),
            updated_at: now,
        }
    }

    fn append_event(
        &mut self,
        event_type: &str,
        message: Option<&FollowupMessage>,
        trace_id: &str,
        previous_trace_id: &str,
    ) {
        self.event_sequence = self.event_sequence.saturating_add(1);
        let event = json!({
            "_type": event_type,
            "_version": EVENT_VERSION,
            "eventId": Uuid::new_v4().to_string(),
            "sequence": self.event_sequence,
            "messageId": message.map(|value| value.message_id.clone()).unwrap_or_default(),
            "sessionId": self.session_id,
            "activeTraceId": message.map(|value| value.active_trace_id.clone()).unwrap_or_else(|| self.active_trace_id.clone()),
            "traceId": trace_id,
            "previousTraceId": previous_trace_id,
            "content": message.map(|value| value.content.clone()).unwrap_or_default(),
            "mode": message.map(|value| value.mode.clone()).unwrap_or_default(),
            "status": message.map(|value| value.status.clone()).unwrap_or_default(),
            "updatedAt": now_iso(),
        });
        self.events.push(event);
        if self.events.len() > MAX_EVENTS {
            let remove = self.events.len() - MAX_EVENTS;
            self.events.drain(..remove);
        }
    }
}

impl FollowupStore {
    pub(crate) fn storage_path(&self, workspace: &Path, session_id: &str) -> PathBuf {
        mailbox_path(workspace, session_id)
    }

    pub(crate) fn delete(&self, workspace: &Path, session_id: &str) -> Result<bool, FollowupError> {
        let _guard = self.lock.lock().unwrap_or_else(|error| error.into_inner());
        let file_guard = lock_mailbox(workspace, session_id)?;
        let path = mailbox_path(workspace, session_id);
        if !path.exists() {
            drop(file_guard);
            return Ok(false);
        }
        self.load(workspace, session_id)?;
        fs::remove_file(&path)
            .map_err(|error| FollowupError::new("followup_storage_error", error.to_string()))?;
        drop(file_guard);
        let lock_path = path.with_extension("lock");
        match fs::remove_file(lock_path) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(FollowupError::new(
                    "followup_storage_error",
                    error.to_string(),
                ));
            }
        }
        Ok(true)
    }

    pub(crate) fn list(
        &self,
        workspace: &Path,
        session_id: &str,
    ) -> Result<FollowupState, FollowupError> {
        let _guard = self.lock.lock().unwrap_or_else(|error| error.into_inner());
        let _file_guard = lock_mailbox(workspace, session_id)?;
        self.load(workspace, session_id)
    }

    pub(crate) fn set_active(
        &self,
        workspace: &Path,
        session_id: &str,
        trace_id: &str,
    ) -> Result<FollowupState, FollowupError> {
        self.mutate_state(workspace, session_id, |state| {
            state.active_trace_id = trace_id.trim().to_owned();
            state.last_trace_id = trace_id.trim().to_owned();
            Ok(())
        })
    }

    pub(crate) fn clear_active(
        &self,
        workspace: &Path,
        session_id: &str,
        expected_trace_id: &str,
    ) -> Result<FollowupState, FollowupError> {
        self.mutate_state(workspace, session_id, |state| {
            if state.active_trace_id == expected_trace_id.trim() {
                state.active_trace_id.clear();
            }
            Ok(())
        })
    }

    pub(crate) fn enqueue(
        &self,
        workspace: &Path,
        session_id: &str,
        message_id: &str,
        content: &str,
        mode: &str,
        expected_trace_id: &str,
    ) -> Result<FollowupMessage, FollowupError> {
        let normalized_id = message_id.trim();
        let normalized_content = content.trim();
        let normalized_mode = mode.trim().to_ascii_lowercase();
        if normalized_id.is_empty() {
            return Err(FollowupError::new(
                "missing_message_id",
                "messageId is required.",
            ));
        }
        if normalized_content.is_empty() {
            return Err(FollowupError::new(
                "empty_followup",
                "Follow-up content cannot be empty.",
            ));
        }
        if !matches!(normalized_mode.as_str(), "queued" | "steer") {
            return Err(FollowupError::new(
                "invalid_followup_mode",
                "mode must be queued or steer.",
            ));
        }
        self.mutate_optional(workspace, session_id, |state| {
            if let Some(existing) = state
                .messages
                .iter()
                .find(|item| item.message_id == normalized_id)
            {
                if existing.content == normalized_content && existing.mode == normalized_mode {
                    return Ok((existing.clone(), false));
                }
                return Err(FollowupError::new(
                    "message_id_conflict",
                    "messageId already exists with different content.",
                ));
            }
            let active = state.active_trace_id.clone();
            let expected = if expected_trace_id.trim().is_empty() {
                active.clone()
            } else {
                expected_trace_id.trim().to_owned()
            };
            if normalized_mode == "steer" {
                if active.is_empty() {
                    return Err(FollowupError::new(
                        "no_active_execution",
                        "There is no active execution to steer.",
                    ));
                }
                if !expected.is_empty() && expected != active {
                    return Err(FollowupError::new(
                        "stale_trace",
                        "The active execution changed before the steer request was applied.",
                    ));
                }
            }
            state.message_sequence = state.message_sequence.saturating_add(1);
            let now = now_iso();
            let message = FollowupMessage {
                message_id: normalized_id.to_owned(),
                session_id: state.session_id.clone(),
                active_trace_id: active.clone(),
                expected_trace_id: expected,
                content: normalized_content.to_owned(),
                mode: normalized_mode.clone(),
                status: if normalized_mode == "steer" {
                    "steering"
                } else {
                    "pending"
                }
                .to_owned(),
                status_detail: if normalized_mode == "steer" {
                    "waiting for a safe interruption point".to_owned()
                } else {
                    "waiting for the current turn to finish".to_owned()
                },
                created_at: now.clone(),
                updated_at: now,
                sequence: state.message_sequence,
                dispatch_trace_id: String::new(),
                segment_id: String::new(),
                previous_trace_id: String::new(),
                error: String::new(),
            };
            state.messages.push(message.clone());
            state.append_event("FollowupQueued", Some(&message), &active, "");
            if normalized_mode == "steer" {
                state.append_event("SteerRequested", Some(&message), &active, "");
            }
            Ok((message, true))
        })
    }

    pub(crate) fn update(
        &self,
        workspace: &Path,
        session_id: &str,
        message_id: &str,
        content: Option<&str>,
        mode: Option<&str>,
        expected_trace_id: &str,
    ) -> Result<FollowupMessage, FollowupError> {
        let normalized_content = content.map(str::trim);
        if normalized_content.is_some_and(str::is_empty) {
            return Err(FollowupError::new(
                "empty_followup",
                "Follow-up content cannot be empty.",
            ));
        }
        let normalized_mode = match mode {
            Some(value) => {
                let value = value.trim().to_ascii_lowercase();
                if !matches!(value.as_str(), "queued" | "steer") {
                    return Err(FollowupError::new(
                        "invalid_followup_mode",
                        "mode must be queued or steer.",
                    ));
                }
                Some(value)
            }
            None => None,
        };
        self.mutate_optional(workspace, session_id, |state| {
            let active_trace_id = state.active_trace_id.clone();
            let Some(index) = state
                .messages
                .iter()
                .position(|item| item.message_id == message_id.trim())
            else {
                return Err(FollowupError::new(
                    "followup_not_found",
                    "Follow-up message was not found.",
                ));
            };
            if !matches!(
                state.messages[index].status.as_str(),
                "pending" | "steering"
            ) {
                return Err(FollowupError::new(
                    "followup_not_editable",
                    "Only pending or steering messages can be edited.",
                ));
            }
            let content_changed =
                normalized_content.is_some_and(|value| value != state.messages[index].content);
            let mode_changed = normalized_mode
                .as_deref()
                .is_some_and(|value| value != state.messages[index].mode);
            let effective_mode = normalized_mode
                .as_deref()
                .unwrap_or(state.messages[index].mode.as_str());
            if effective_mode == "steer" {
                if active_trace_id.is_empty() {
                    return Err(FollowupError::new(
                        "no_active_execution",
                        "There is no active execution to steer.",
                    ));
                }
                let expected = if expected_trace_id.trim().is_empty() {
                    state.messages[index].expected_trace_id.trim()
                } else {
                    expected_trace_id.trim()
                };
                if !expected.is_empty() && expected != active_trace_id {
                    return Err(FollowupError::new(
                        "stale_trace",
                        "The active execution changed before the steer request was applied.",
                    ));
                }
            }
            if !content_changed && !mode_changed {
                return Ok((state.messages[index].clone(), false));
            }
            if let Some(content) = normalized_content {
                state.messages[index].content = content.to_owned();
            }
            if let Some(mode) = normalized_mode.as_deref() {
                state.messages[index].mode = mode.to_owned();
                if mode == "steer" {
                    state.messages[index].status = "steering".to_owned();
                    state.messages[index].status_detail =
                        "waiting for a safe interruption point".to_owned();
                    state.messages[index].active_trace_id = active_trace_id.clone();
                    state.messages[index].expected_trace_id = if expected_trace_id.trim().is_empty()
                    {
                        active_trace_id.clone()
                    } else {
                        expected_trace_id.trim().to_owned()
                    };
                } else {
                    state.messages[index].status = "pending".to_owned();
                    state.messages[index].status_detail =
                        "waiting for the current turn to finish".to_owned();
                }
            }
            state.messages[index].updated_at = now_iso();
            let output = state.messages[index].clone();
            if mode_changed && output.mode == "steer" {
                state.append_event("SteerRequested", Some(&output), &active_trace_id, "");
            }
            state.append_event("FollowupUpdated", Some(&output), &active_trace_id, "");
            Ok((output, true))
        })
    }

    pub(crate) fn cancel_message(
        &self,
        workspace: &Path,
        session_id: &str,
        message_id: &str,
    ) -> Result<FollowupMessage, FollowupError> {
        self.mutate_optional(workspace, session_id, |state| {
            let Some(index) = state
                .messages
                .iter()
                .position(|item| item.message_id == message_id.trim())
            else {
                return Err(FollowupError::new(
                    "followup_not_found",
                    "Follow-up message was not found.",
                ));
            };
            if state.messages[index].status == "cancelled" {
                return Ok((state.messages[index].clone(), false));
            }
            if !matches!(
                state.messages[index].status.as_str(),
                "pending" | "steering"
            ) {
                return Err(FollowupError::new(
                    "followup_not_editable",
                    "A dispatching or sent follow-up cannot be deleted.",
                ));
            }
            state.messages[index].status = "cancelled".to_owned();
            state.messages[index].status_detail = "deleted".to_owned();
            state.messages[index].updated_at = now_iso();
            let output = state.messages[index].clone();
            state.append_event(
                "FollowupUpdated",
                Some(&output),
                &state.active_trace_id.clone(),
                "",
            );
            Ok((output, true))
        })
    }

    pub(crate) fn claim(
        &self,
        workspace: &Path,
        session_id: &str,
        message_id: &str,
        previous_trace_id: &str,
        next_trace_id: &str,
        expected_trace_id: &str,
    ) -> Result<FollowupMessage, FollowupError> {
        self.mutate(workspace, session_id, |state| {
            if state.paused {
                return Err(FollowupError::new(
                    "followup_mailbox_paused",
                    "The follow-up mailbox is paused.",
                ));
            }
            let previous = previous_trace_id.trim();
            let Some(message) = state
                .messages
                .iter_mut()
                .find(|item| item.message_id == message_id.trim())
            else {
                return Err(FollowupError::new(
                    "followup_not_found",
                    "Follow-up message was not found.",
                ));
            };
            let expected = if expected_trace_id.trim().is_empty() {
                message.expected_trace_id.as_str()
            } else {
                expected_trace_id.trim()
            };
            if !expected.is_empty() && expected != previous {
                return Err(FollowupError::new(
                    "stale_trace",
                    "The latest execution changed before the queued follow-up was resumed.",
                ));
            }
            if message.mode != "queued" || message.status != "pending" {
                return Err(FollowupError::new(
                    "invalid_followup_transition",
                    "The follow-up is no longer pending dispatch.",
                ));
            }
            message.status = "dispatching".to_owned();
            message.status_detail = "starting the resumed turn".to_owned();
            message.dispatch_trace_id = next_trace_id.trim().to_owned();
            message.previous_trace_id = previous.to_owned();
            message.updated_at = now_iso();
            let output = message.clone();
            state.append_event(
                "ContinuationStarted",
                Some(&output),
                next_trace_id,
                previous,
            );
            Ok(output)
        })
    }

    pub(crate) fn request_steer(
        &self,
        workspace: &Path,
        session_id: &str,
        message_id: &str,
        expected_trace_id: &str,
    ) -> Result<FollowupMessage, FollowupError> {
        self.mutate(workspace, session_id, |state| {
            if state.active_trace_id.is_empty() {
                return Err(FollowupError::new(
                    "no_active_execution",
                    "There is no active execution to steer.",
                ));
            }
            let expected = expected_trace_id.trim();
            if !expected.is_empty() && expected != state.active_trace_id {
                return Err(FollowupError::new(
                    "stale_trace",
                    "The active execution changed before the steer request was applied.",
                ));
            }
            let Some(message) = state
                .messages
                .iter_mut()
                .find(|item| item.message_id == message_id.trim())
            else {
                return Err(FollowupError::new(
                    "followup_not_found",
                    "Follow-up message was not found.",
                ));
            };
            if !matches!(message.status.as_str(), "pending" | "steering") {
                return Err(FollowupError::new(
                    "invalid_followup_transition",
                    "Only a pending follow-up can steer the active execution.",
                ));
            }
            message.mode = "steer".to_owned();
            message.status = "steering".to_owned();
            message.status_detail = "waiting for a safe interruption point".to_owned();
            message.active_trace_id = state.active_trace_id.clone();
            message.expected_trace_id = if expected.is_empty() {
                state.active_trace_id.clone()
            } else {
                expected.to_owned()
            };
            message.updated_at = now_iso();
            let output = message.clone();
            state.append_event(
                "SteerRequested",
                Some(&output),
                &state.active_trace_id.clone(),
                "",
            );
            Ok(output)
        })
    }

    pub(crate) fn mark_sent(
        &self,
        workspace: &Path,
        session_id: &str,
        message_id: &str,
        trace_id: &str,
    ) -> Result<FollowupState, FollowupError> {
        self.mutate_state(workspace, session_id, |state| {
            let Some(message) = state
                .messages
                .iter_mut()
                .find(|item| item.message_id == message_id.trim())
            else {
                return Err(FollowupError::new(
                    "followup_not_found",
                    "Follow-up message was not found.",
                ));
            };
            if message.status == "sent" {
                return Ok(());
            }
            if message.status != "dispatching" {
                return Err(FollowupError::new(
                    "invalid_followup_transition",
                    "Follow-up is not dispatching.",
                ));
            }
            message.status = "sent".to_owned();
            message.status_detail = "sent".to_owned();
            message.updated_at = now_iso();
            let snapshot = message.clone();
            state.append_event("FollowupUpdated", Some(&snapshot), trace_id, "");
            Ok(())
        })
    }

    pub(crate) fn pause(
        &self,
        workspace: &Path,
        session_id: &str,
        reason: &str,
    ) -> Result<FollowupState, FollowupError> {
        self.mutate_state(workspace, session_id, |state| {
            state.paused = true;
            state.pause_reason = reason.trim().to_owned();
            Ok(())
        })
    }

    /// The current bridge implements steer as cancellation. Preserve that
    /// message as a resumable queued follow-up instead of leaving it stuck in
    /// the non-claimable `steering` state.
    pub(crate) fn requeue_steering(
        &self,
        workspace: &Path,
        session_id: &str,
        trace_id: &str,
    ) -> Result<Vec<FollowupMessage>, FollowupError> {
        self.mutate(workspace, session_id, |state| {
            let normalized_trace = trace_id.trim();
            let mut changed_ids = Vec::new();
            for message in &mut state.messages {
                let expected_trace = if message.expected_trace_id.trim().is_empty() {
                    message.active_trace_id.trim()
                } else {
                    message.expected_trace_id.trim()
                };
                if message.mode == "steer"
                    && message.status == "steering"
                    && (normalized_trace.is_empty()
                        || expected_trace.is_empty()
                        || expected_trace == normalized_trace)
                {
                    message.mode = "queued".to_owned();
                    message.status = "pending".to_owned();
                    message.status_detail =
                        "steer was interrupted; resume the mailbox to send it".to_owned();
                    message.active_trace_id.clear();
                    message.expected_trace_id = normalized_trace.to_owned();
                    message.dispatch_trace_id.clear();
                    message.previous_trace_id.clear();
                    message.error = "steer_requires_resume".to_owned();
                    message.updated_at = now_iso();
                    changed_ids.push(message.message_id.clone());
                }
            }
            let mut output = Vec::with_capacity(changed_ids.len());
            for message_id in changed_ids {
                if let Some(message) = state
                    .messages
                    .iter()
                    .find(|item| item.message_id == message_id)
                    .cloned()
                {
                    state.append_event(
                        "FollowupUpdated",
                        Some(&message),
                        normalized_trace,
                        normalized_trace,
                    );
                    output.push(message);
                }
            }
            Ok(output)
        })
    }

    pub(crate) fn resume(
        &self,
        workspace: &Path,
        session_id: &str,
    ) -> Result<FollowupState, FollowupError> {
        self.mutate_state(workspace, session_id, |state| {
            state.paused = false;
            state.pause_reason.clear();
            Ok(())
        })
    }

    fn mutate<T>(
        &self,
        workspace: &Path,
        session_id: &str,
        operation: impl FnOnce(&mut FollowupState) -> Result<T, FollowupError>,
    ) -> Result<T, FollowupError> {
        let _guard = self.lock.lock().unwrap_or_else(|error| error.into_inner());
        let _file_guard = lock_mailbox(workspace, session_id)?;
        let mut state = self.load(workspace, session_id)?;
        let value = operation(&mut state)?;
        state.revision = state.revision.saturating_add(1);
        state.updated_at = now_iso();
        self.write(workspace, session_id, &state)?;
        Ok(value)
    }

    fn mutate_state(
        &self,
        workspace: &Path,
        session_id: &str,
        operation: impl FnOnce(&mut FollowupState) -> Result<(), FollowupError>,
    ) -> Result<FollowupState, FollowupError> {
        let _guard = self.lock.lock().unwrap_or_else(|error| error.into_inner());
        let _file_guard = lock_mailbox(workspace, session_id)?;
        let mut state = self.load(workspace, session_id)?;
        operation(&mut state)?;
        state.revision = state.revision.saturating_add(1);
        state.updated_at = now_iso();
        self.write(workspace, session_id, &state)?;
        Ok(state)
    }

    fn mutate_optional<T>(
        &self,
        workspace: &Path,
        session_id: &str,
        operation: impl FnOnce(&mut FollowupState) -> Result<(T, bool), FollowupError>,
    ) -> Result<T, FollowupError> {
        let _guard = self.lock.lock().unwrap_or_else(|error| error.into_inner());
        let _file_guard = lock_mailbox(workspace, session_id)?;
        let mut state = self.load(workspace, session_id)?;
        let (value, changed) = operation(&mut state)?;
        if changed {
            state.revision = state.revision.saturating_add(1);
            state.updated_at = now_iso();
            self.write(workspace, session_id, &state)?;
        }
        Ok(value)
    }

    fn load(&self, workspace: &Path, session_id: &str) -> Result<FollowupState, FollowupError> {
        let workspace = workspace
            .canonicalize()
            .map_err(|error| FollowupError::new("invalid_workspace", error.to_string()))?;
        let path = mailbox_path(&workspace, session_id);
        if !path.is_file() {
            return Ok(FollowupState::fresh(&workspace, session_id));
        }
        let raw = fs::read_to_string(&path)
            .map_err(|error| FollowupError::new("followup_storage_error", error.to_string()))?;
        let mut state: FollowupState = serde_json::from_str(&raw).map_err(|error| {
            FollowupError::new(
                "corrupt_followup_mailbox",
                format!("invalid follow-up mailbox: {error}"),
            )
        })?;
        let expected_workspace_root = contract_path(&workspace);
        if state.version != MAILBOX_VERSION
            || state.session_id != normalize_session(session_id)
            || !workspace_roots_match(&state.workspace_root, &expected_workspace_root)
        {
            return Err(FollowupError::new(
                "corrupt_followup_mailbox",
                "follow-up mailbox identity does not match the requested workspace/session",
            ));
        }
        // Keep the response and the next write on the current canonical
        // spelling, while accepting the slash/extended-prefix spellings that
        // the Python runtime persisted on Windows.
        state.workspace_root = expected_workspace_root;
        if state.last_trace_id.trim().is_empty() {
            // `lastTraceId` was introduced by the Rust runtime. Recover it
            // from the legacy mailbox's active/pending trace metadata so a
            // queued message can still pass the stale-trace guard on resume.
            state.last_trace_id = recover_last_trace_id(&state);
        }
        Ok(state)
    }

    fn write(
        &self,
        workspace: &Path,
        session_id: &str,
        state: &FollowupState,
    ) -> Result<(), FollowupError> {
        let path = mailbox_path(workspace, session_id);
        let parent = path
            .parent()
            .ok_or_else(|| FollowupError::new("followup_storage_error", "mailbox has no parent"))?;
        fs::create_dir_all(parent)
            .map_err(|error| FollowupError::new("followup_storage_error", error.to_string()))?;
        let temporary = path.with_file_name(format!(
            ".{}.{}.tmp",
            path.file_name().unwrap_or_default().to_string_lossy(),
            Uuid::new_v4()
        ));
        let mut bytes = serde_json::to_vec_pretty(state)
            .map_err(|error| FollowupError::new("followup_storage_error", error.to_string()))?;
        bytes.push(b'\n');
        fs::write(&temporary, bytes)
            .map_err(|error| FollowupError::new("followup_storage_error", error.to_string()))?;
        if path.exists() {
            let backup = path.with_file_name(format!(
                ".{}.{}.bak",
                path.file_name().unwrap_or_default().to_string_lossy(),
                Uuid::new_v4()
            ));
            fs::rename(&path, &backup).map_err(|error| {
                let _ = fs::remove_file(&temporary);
                FollowupError::new("followup_storage_error", error.to_string())
            })?;
            match fs::rename(&temporary, &path) {
                Ok(()) => {
                    let _ = fs::remove_file(backup);
                    Ok(())
                }
                Err(error) => {
                    let _ = fs::rename(backup, &path);
                    let _ = fs::remove_file(&temporary);
                    Err(FollowupError::new(
                        "followup_storage_error",
                        error.to_string(),
                    ))
                }
            }
        } else {
            fs::rename(&temporary, &path).map_err(|error| {
                let _ = fs::remove_file(&temporary);
                FollowupError::new("followup_storage_error", error.to_string())
            })
        }
    }
}

fn lock_mailbox(workspace: &Path, session_id: &str) -> Result<File, FollowupError> {
    let workspace = workspace
        .canonicalize()
        .map_err(|error| FollowupError::new("invalid_workspace", error.to_string()))?;
    let path = mailbox_path(&workspace, session_id);
    let parent = path
        .parent()
        .ok_or_else(|| FollowupError::new("followup_storage_error", "mailbox has no parent"))?;
    fs::create_dir_all(parent)
        .map_err(|error| FollowupError::new("followup_storage_error", error.to_string()))?;
    let lock_path = path.with_extension("lock");
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(lock_path)
        .map_err(|error| FollowupError::new("followup_storage_error", error.to_string()))?;
    file.lock_exclusive()
        .map_err(|error| FollowupError::new("followup_storage_error", error.to_string()))?;
    Ok(file)
}

fn mailbox_path(workspace: &Path, session_id: &str) -> PathBuf {
    let digest = format!(
        "{:x}",
        Sha256::digest(normalize_session(session_id).as_bytes())
    );
    workspace
        .join(".storydex")
        .join(".agent")
        .join("followups")
        .join(format!("{}.json", &digest[..24]))
}

fn normalize_session(value: &str) -> String {
    let value = value.trim();
    if value.is_empty() {
        "default".to_owned()
    } else {
        value.to_owned()
    }
}

fn contract_path(path: &Path) -> String {
    let value = path.to_string_lossy();
    value
        .strip_prefix("\\\\?\\UNC\\")
        .map(|rest| format!("\\\\{rest}"))
        .or_else(|| value.strip_prefix("\\\\?\\").map(ToOwned::to_owned))
        .unwrap_or_else(|| value.into_owned())
}

fn workspace_roots_match(stored: &str, expected: &str) -> bool {
    let stored = normalize_workspace_identity(stored);
    let expected = normalize_workspace_identity(expected);
    if cfg!(windows) {
        stored.eq_ignore_ascii_case(&expected)
    } else {
        stored == expected
    }
}

fn normalize_workspace_identity(value: &str) -> String {
    // The desktop release is Windows-only, but keeping the non-Windows path
    // spelling untouched avoids treating a literal backslash in a Unix file
    // name as a separator.
    if !cfg!(windows) {
        return value.trim().to_owned();
    }

    let mut normalized = value.trim().replace('/', "\\");
    const EXTENDED_UNC_PREFIX: &str = "\\\\?\\UNC\\";
    const EXTENDED_PREFIX: &str = "\\\\?\\";
    if normalized.len() >= EXTENDED_UNC_PREFIX.len()
        && normalized[..EXTENDED_UNC_PREFIX.len()].eq_ignore_ascii_case(EXTENDED_UNC_PREFIX)
    {
        normalized = format!("\\\\{}", &normalized[EXTENDED_UNC_PREFIX.len()..]);
    } else if normalized.len() >= EXTENDED_PREFIX.len()
        && normalized[..EXTENDED_PREFIX.len()].eq_ignore_ascii_case(EXTENDED_PREFIX)
    {
        normalized = normalized[EXTENDED_PREFIX.len()..].to_owned();
    }

    // Windows accepts repeated separators in a path. Collapse them while
    // retaining the two leading separators that identify a UNC path.
    let preserve_unc_prefix = normalized.starts_with("\\\\");
    let mut compact = String::with_capacity(normalized.len());
    let mut separator_run = 0usize;
    for character in normalized.chars() {
        if character == '\\' {
            separator_run += 1;
            if separator_run > 1 && !(preserve_unc_prefix && compact.len() < 2) {
                continue;
            }
        } else {
            separator_run = 0;
        }
        compact.push(character);
    }
    normalized = compact;

    // A trailing separator is not part of the workspace identity, except for
    // a drive root (for example, `C:\\`).
    let minimum_length = if normalized.starts_with("\\\\") {
        2
    } else if normalized.as_bytes().get(1) == Some(&b':') {
        3
    } else {
        1
    };
    while normalized.len() > minimum_length && normalized.ends_with('\\') {
        normalized.pop();
    }
    normalized
}

fn recover_last_trace_id(state: &FollowupState) -> String {
    if !state.active_trace_id.trim().is_empty() {
        return state.active_trace_id.trim().to_owned();
    }
    state
        .messages
        .iter()
        .rev()
        .filter(|message| message.mode == "queued" && message.status == "pending")
        .find_map(|message| {
            [
                message.expected_trace_id.as_str(),
                message.active_trace_id.as_str(),
                message.previous_trace_id.as_str(),
            ]
            .into_iter()
            .map(str::trim)
            .find(|value| !value.is_empty())
            .map(ToOwned::to_owned)
        })
        .or_else(|| {
            state.events.iter().rev().find_map(|event| {
                [
                    event.get("traceId").and_then(Value::as_str),
                    event.get("activeTraceId").and_then(Value::as_str),
                ]
                .into_iter()
                .flatten()
                .map(str::trim)
                .find(|value| !value.is_empty())
                .map(ToOwned::to_owned)
            })
        })
        .unwrap_or_default()
}

fn now_iso() -> String {
    chrono::Utc::now().to_rfc3339()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Barrier;
    use std::thread;
    use tempfile::tempdir;

    #[test]
    fn mailbox_enforces_idempotency_stale_trace_and_resume() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path();
        let store = FollowupStore::default();
        store
            .set_active(workspace, "session", "trace-1")
            .expect("active trace");
        let message = store
            .enqueue(workspace, "session", "m1", "continue", "queued", "trace-1")
            .expect("enqueue");
        assert_eq!(message.status, "pending");
        let revision = store.list(workspace, "session").expect("mailbox").revision;
        assert!(
            store
                .enqueue(workspace, "session", "m1", "continue", "queued", "trace-1")
                .is_ok()
        );
        assert_eq!(
            store.list(workspace, "session").expect("mailbox").revision,
            revision
        );
        assert_eq!(
            store
                .claim(workspace, "session", "m1", "old", "trace-2", "")
                .expect_err("stale claim")
                .code,
            "stale_trace"
        );
        let claimed = store
            .claim(workspace, "session", "m1", "trace-1", "trace-2", "trace-1")
            .expect("claim");
        assert_eq!(claimed.status, "dispatching");
        store
            .mark_sent(workspace, "session", "m1", "trace-2")
            .expect("sent");
        store
            .pause(workspace, "session", "manual_stop")
            .expect("pause");
        assert!(
            store
                .claim(workspace, "session", "m1", "trace-2", "trace-3", "")
                .is_err()
        );
        store.resume(workspace, "session").expect("resume");
        let state = store.list(workspace, "session").expect("list");
        assert!(!state.paused);
        assert_eq!(state.messages[0].status, "sent");
    }

    #[test]
    fn corrupt_mailbox_fails_closed() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path();
        let path = mailbox_path(workspace, "session");
        fs::create_dir_all(path.parent().expect("parent")).expect("parent");
        fs::write(path, b"not-json").expect("corrupt file");
        let error = FollowupStore::default()
            .list(workspace, "session")
            .expect_err("corrupt mailbox must fail");
        assert_eq!(error.code, "corrupt_followup_mailbox");
    }

    #[test]
    fn legacy_python_mailbox_is_accepted_and_preserved_on_write() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path().canonicalize().expect("workspace");
        let path = mailbox_path(&workspace, "legacy-session");
        fs::create_dir_all(path.parent().expect("parent")).expect("parent");

        let legacy_workspace = contract_path(&workspace).replace('\\', "/");
        let legacy = json!({
            "_type": "FollowupMailbox",
            "_version": MAILBOX_VERSION,
            "revision": 2,
            "workspaceRoot": legacy_workspace,
            "sessionId": "legacy-session",
            "activeTraceId": "",
            "paused": false,
            "pauseReason": "",
            "messageSequence": 1,
            "eventSequence": 1,
            "messages": [{
                "messageId": "legacy-message",
                "sessionId": "legacy-session",
                "activeTraceId": "",
                "expectedTraceId": "trace-old",
                "content": "continue from the old runtime",
                "mode": "queued",
                "status": "pending",
                "statusDetail": "等待当前轮完成",
                "createdAt": "2026-08-20T00:00:00Z",
                "updatedAt": "2026-08-20T00:00:00Z",
                "sequence": 1,
                "dispatchTraceId": "",
                "segmentId": "legacy-segment",
                "error": ""
            }],
            "events": [],
            "createdAt": "2026-08-20T00:00:00Z",
            "updatedAt": "2026-08-20T00:00:00Z"
        });
        fs::write(
            &path,
            serde_json::to_vec_pretty(&legacy).expect("legacy mailbox JSON"),
        )
        .expect("legacy mailbox");

        let store = FollowupStore::default();
        let state = store
            .list(&workspace, "legacy-session")
            .expect("legacy mailbox should load");
        assert_eq!(state.workspace_root, contract_path(&workspace));
        assert_eq!(state.last_trace_id, "trace-old");
        assert_eq!(state.messages[0].previous_trace_id, "");
        assert_eq!(state.messages[0].segment_id, "legacy-segment");

        // The first normal state mutation performs the safe schema rewrite;
        // the pending message itself must remain dispatchable.
        let claimed = store
            .claim(
                &workspace,
                "legacy-session",
                "legacy-message",
                "trace-old",
                "trace-next",
                "",
            )
            .expect("legacy pending message should be claimable");
        assert_eq!(claimed.status, "dispatching");

        let persisted: Value =
            serde_json::from_str(&fs::read_to_string(&path).expect("rewritten mailbox"))
                .expect("rewritten mailbox JSON");
        assert_eq!(
            persisted["workspaceRoot"],
            Value::String(contract_path(&workspace))
        );
        assert_eq!(
            persisted["messages"][0]["content"],
            legacy["messages"][0]["content"]
        );
        assert_eq!(
            persisted["messages"][0]["segmentId"],
            Value::String("legacy-segment".to_owned())
        );
        assert_eq!(
            persisted["messages"][0]["previousTraceId"],
            Value::String("trace-old".to_owned())
        );
    }

    #[test]
    fn windows_workspace_identity_accepts_legacy_spellings() {
        if !cfg!(windows) {
            return;
        }
        assert!(workspace_roots_match(
            "E:/Docs/Story/",
            r"\\?\e:\\docs\\story"
        ));
        assert!(workspace_roots_match(
            r"\\server\share\Story",
            "//SERVER/share/Story/"
        ));
    }

    #[test]
    fn interrupted_steer_is_requeued_for_explicit_resume() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path();
        let store = FollowupStore::default();
        store
            .set_active(workspace, "session", "trace-1")
            .expect("active trace");
        store
            .enqueue(
                workspace,
                "session",
                "steer-1",
                "continue safely",
                "steer",
                "trace-1",
            )
            .expect("steer enqueue");

        let changed = store
            .requeue_steering(workspace, "session", "trace-1")
            .expect("requeue steer");
        assert_eq!(changed.len(), 1);
        assert_eq!(changed[0].mode, "queued");
        assert_eq!(changed[0].status, "pending");
        assert_eq!(changed[0].expected_trace_id, "trace-1");

        store
            .pause(workspace, "session", "steer_requires_resume")
            .expect("pause after steer");
        store.resume(workspace, "session").expect("resume mailbox");
        let claimed = store
            .claim(workspace, "session", "steer-1", "trace-1", "trace-2", "")
            .expect("claim requeued steer");
        assert_eq!(claimed.status, "dispatching");
        assert_eq!(claimed.mode, "queued");
    }

    #[test]
    fn pending_followups_can_be_edited_and_cancelled_idempotently() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path();
        let store = FollowupStore::default();
        store
            .enqueue(workspace, "session", "m1", "first", "queued", "")
            .expect("enqueue");

        let edited = store
            .update(workspace, "session", "m1", Some("second"), None, "")
            .expect("edit");
        assert_eq!(edited.content, "second");
        assert_eq!(edited.status, "pending");

        let cancelled = store
            .cancel_message(workspace, "session", "m1")
            .expect("cancel");
        assert_eq!(cancelled.status, "cancelled");
        let repeated = store
            .cancel_message(workspace, "session", "m1")
            .expect("repeat cancel");
        assert_eq!(repeated.status, "cancelled");

        let state = store.list(workspace, "session").expect("mailbox");
        assert_eq!(state.messages.len(), 1);
        assert_eq!(
            state
                .events
                .iter()
                .filter(|event| event["_type"] == "FollowupUpdated")
                .count(),
            2
        );
    }

    #[test]
    fn independent_stores_serialize_mailbox_updates() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path().canonicalize().expect("workspace");
        let barrier = Arc::new(Barrier::new(3));
        let handles = (1..=2)
            .map(|index| {
                let workspace = workspace.clone();
                let barrier = Arc::clone(&barrier);
                thread::spawn(move || {
                    let store = FollowupStore::default();
                    barrier.wait();
                    store
                        .enqueue(
                            &workspace,
                            "shared-session",
                            &format!("message-{index}"),
                            &format!("content-{index}"),
                            "queued",
                            "",
                        )
                        .expect("enqueue")
                })
            })
            .collect::<Vec<_>>();
        barrier.wait();
        for handle in handles {
            handle.join().expect("join concurrent enqueue");
        }

        let state = FollowupStore::default()
            .list(&workspace, "shared-session")
            .expect("mailbox");
        assert_eq!(state.revision, 2);
        assert_eq!(state.message_sequence, 2);
        assert_eq!(state.messages.len(), 2);
        assert_eq!(state.events.len(), 2);
    }
}
