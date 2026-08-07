use crate::ChatMessage;
use crate::ContextState;
use crate::LoopState;
use crate::PlanState;
use crate::TokenUsage;
use anyhow::Context;
use anyhow::Result;
use chrono::DateTime;
use chrono::Utc;
use serde::Deserialize;
use serde::Serialize;
use std::cmp::Reverse;
use std::fs;
use std::io::Write;
use std::path::Path;
use std::path::PathBuf;
use uuid::Uuid;

pub const SESSION_SCHEMA_VERSION: u32 = 1;

#[derive(Clone, Debug, Deserialize, Serialize)]
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

pub struct SessionStore {
    directory: PathBuf,
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
            fs::rename(&temporary_path, &path)
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
}
