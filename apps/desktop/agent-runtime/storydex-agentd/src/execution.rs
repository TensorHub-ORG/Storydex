use serde::Serialize;
use serde_json::Value;
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;

#[derive(Clone, Default)]
pub(crate) struct ExecutionRegistry {
    active: Arc<Mutex<Option<ExecutionEntry>>>,
}

#[derive(Clone)]
struct ExecutionEntry {
    trace_id: String,
    session_id: String,
    workspace_root: PathBuf,
    cancellation: ExecutionCancellation,
    control_sender: mpsc::Sender<ExecutionControl>,
    pending_requests: HashSet<String>,
}

#[derive(Debug)]
pub(crate) enum ExecutionControl {
    Resolve { request_id: String, value: Value },
}

#[derive(Clone, Default)]
pub(crate) struct ExecutionCancellation {
    token: CancellationToken,
    reason: Arc<Mutex<String>>,
}

impl ExecutionCancellation {
    pub(crate) fn cancel(&self, reason: &str) -> bool {
        let mut stored = self
            .reason
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if self.token.is_cancelled() {
            return false;
        }
        *stored = normalize_reason(reason);
        self.token.cancel();
        true
    }

    pub(crate) fn is_cancelled(&self) -> bool {
        self.token.is_cancelled()
    }

    pub(crate) async fn cancelled(&self) {
        self.token.cancelled().await;
    }

    pub(crate) fn reason(&self) -> String {
        let reason = self
            .reason
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone();
        normalize_reason(&reason)
    }
}

#[derive(Debug)]
pub(crate) struct ExecutionBusy {
    pub(crate) active_trace_id: String,
    pub(crate) active_session_id: String,
}

#[derive(Clone, Debug)]
pub(crate) struct ActiveExecution {
    pub(crate) trace_id: String,
    pub(crate) session_id: String,
    pub(crate) workspace_root: PathBuf,
}

pub(crate) struct ExecutionGuard {
    registry: ExecutionRegistry,
    trace_id: String,
}

impl Drop for ExecutionGuard {
    fn drop(&mut self) {
        self.registry.release(&self.trace_id);
    }
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct CancelResult {
    pub(crate) accepted: bool,
    pub(crate) session_id: String,
    pub(crate) active_trace_id: String,
    pub(crate) reason: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub(crate) expected_trace_id: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ResolveResult {
    pub(crate) accepted: bool,
    pub(crate) session_id: String,
    pub(crate) active_trace_id: String,
    pub(crate) reason: String,
    pub(crate) request_id: String,
}

impl ExecutionRegistry {
    pub(crate) fn active(&self) -> Option<ActiveExecution> {
        self.active
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .as_ref()
            .map(|entry| ActiveExecution {
                trace_id: entry.trace_id.clone(),
                session_id: entry.session_id.clone(),
                workspace_root: entry.workspace_root.clone(),
            })
    }

    pub(crate) fn register(
        &self,
        trace_id: String,
        session_id: String,
        workspace_root: PathBuf,
        cancellation: ExecutionCancellation,
        control_sender: mpsc::Sender<ExecutionControl>,
    ) -> Result<ExecutionGuard, ExecutionBusy> {
        let mut active = self
            .active
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if let Some(entry) = active.as_ref() {
            return Err(ExecutionBusy {
                active_trace_id: entry.trace_id.clone(),
                active_session_id: entry.session_id.clone(),
            });
        }
        *active = Some(ExecutionEntry {
            trace_id: trace_id.clone(),
            session_id,
            workspace_root,
            cancellation,
            control_sender,
            pending_requests: HashSet::new(),
        });
        Ok(ExecutionGuard {
            registry: self.clone(),
            trace_id,
        })
    }

    pub(crate) fn resolve(
        &self,
        request_id: &str,
        session_id: &str,
        expected_trace_id: &str,
        workspace_root: Option<&Path>,
        value: Value,
    ) -> ResolveResult {
        let normalized_session = normalize_session(session_id);
        let request_id = request_id.trim().to_owned();
        if request_id.is_empty() {
            return ResolveResult {
                accepted: false,
                session_id: normalized_session,
                active_trace_id: String::new(),
                reason: "invalid_request_id".to_owned(),
                request_id,
            };
        }
        let entry = {
            let mut active = self
                .active
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            let Some(entry) = active.as_mut().filter(|entry| {
                entry.session_id == normalized_session
                    && workspace_root.is_none_or(|root| entry.workspace_root == root)
            }) else {
                return ResolveResult {
                    accepted: false,
                    session_id: normalized_session,
                    active_trace_id: String::new(),
                    reason: "no_active_execution".to_owned(),
                    request_id,
                };
            };
            let expected_trace = expected_trace_id.trim();
            if !expected_trace.is_empty() && entry.trace_id != expected_trace {
                return ResolveResult {
                    accepted: false,
                    session_id: entry.session_id.clone(),
                    active_trace_id: entry.trace_id.clone(),
                    reason: "stale_trace".to_owned(),
                    request_id,
                };
            }
            if !entry.pending_requests.remove(&request_id) {
                return ResolveResult {
                    accepted: false,
                    session_id: entry.session_id.clone(),
                    active_trace_id: entry.trace_id.clone(),
                    reason: "request_not_pending".to_owned(),
                    request_id,
                };
            }
            entry.clone()
        };
        let send_result = entry.control_sender.try_send(ExecutionControl::Resolve {
            request_id: request_id.clone(),
            value,
        });
        let accepted = send_result.is_ok();
        if !accepted {
            let mut active = self
                .active
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            if let Some(active) = active
                .as_mut()
                .filter(|active| active.trace_id == entry.trace_id)
            {
                active.pending_requests.insert(request_id.clone());
            }
        }
        ResolveResult {
            accepted,
            session_id: entry.session_id,
            active_trace_id: entry.trace_id,
            reason: if accepted {
                "accepted".to_owned()
            } else {
                "control_unavailable".to_owned()
            },
            request_id,
        }
    }

    pub(crate) fn register_request(&self, trace_id: &str, request_id: &str) -> bool {
        let request_id = request_id.trim();
        if request_id.is_empty() {
            return false;
        }
        let mut active = self
            .active
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let Some(entry) = active
            .as_mut()
            .filter(|entry| entry.trace_id == trace_id.trim())
        else {
            return false;
        };
        entry.pending_requests.insert(request_id.to_owned())
    }

    pub(crate) fn cancel(
        &self,
        session_id: &str,
        expected_trace_id: &str,
        workspace_root: Option<&Path>,
        reason: &str,
    ) -> CancelResult {
        let normalized_session = normalize_session(session_id);
        let expected_trace = expected_trace_id.trim();
        let entry = {
            let active = self
                .active
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            active
                .as_ref()
                .filter(|entry| {
                    entry.session_id == normalized_session
                        && workspace_root.is_none_or(|root| entry.workspace_root == root)
                })
                .cloned()
        };
        let Some(entry) = entry else {
            return CancelResult {
                accepted: false,
                session_id: normalized_session,
                active_trace_id: String::new(),
                reason: "no_active_execution".to_owned(),
                expected_trace_id: String::new(),
            };
        };
        if !expected_trace.is_empty() && entry.trace_id != expected_trace {
            return CancelResult {
                accepted: false,
                session_id: entry.session_id,
                active_trace_id: entry.trace_id,
                reason: "stale_trace".to_owned(),
                expected_trace_id: expected_trace.to_owned(),
            };
        }
        let accepted = entry.cancellation.cancel(reason);
        CancelResult {
            accepted,
            session_id: entry.session_id,
            active_trace_id: entry.trace_id,
            reason: normalize_reason(reason),
            expected_trace_id: String::new(),
        }
    }

    fn release(&self, trace_id: &str) {
        let mut active = self
            .active
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if active
            .as_ref()
            .is_some_and(|entry| entry.trace_id == trace_id)
        {
            *active = None;
        }
    }
}

fn normalize_session(value: &str) -> String {
    let normalized = value.trim();
    if normalized.is_empty() {
        "default".to_owned()
    } else {
        normalized.to_owned()
    }
}

fn normalize_reason(value: &str) -> String {
    let normalized = value.trim();
    if normalized.is_empty() {
        "cancelled".to_owned()
    } else {
        normalized.to_owned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registry_enforces_one_active_execution_and_trace_scoped_cancel() {
        let registry = ExecutionRegistry::default();
        let cancellation = ExecutionCancellation::default();
        let guard = registry
            .register(
                "trace-1".into(),
                "session-1".into(),
                PathBuf::from("workspace-1"),
                cancellation.clone(),
                mpsc::channel(1).0,
            )
            .expect("register execution");
        assert!(
            registry
                .register(
                    "trace-2".into(),
                    "session-2".into(),
                    PathBuf::from("workspace-2"),
                    ExecutionCancellation::default(),
                    mpsc::channel(1).0,
                )
                .is_err()
        );

        let stale = registry.cancel(
            "session-1",
            "trace-old",
            Some(Path::new("workspace-1")),
            "manual_stop",
        );
        assert_eq!(stale.reason, "stale_trace");
        assert!(!cancellation.is_cancelled());

        let cancelled = registry.cancel(
            "session-1",
            "trace-1",
            Some(Path::new("workspace-1")),
            "manual_stop",
        );
        assert!(cancelled.accepted);
        assert!(cancellation.is_cancelled());
        assert_eq!(cancellation.reason(), "manual_stop");

        drop(guard);
        let _next_guard = registry
            .register(
                "trace-2".into(),
                "session-2".into(),
                PathBuf::from("workspace-2"),
                ExecutionCancellation::default(),
                mpsc::channel(1).0,
            )
            .expect("released execution slot");
    }

    #[test]
    fn approval_request_can_only_be_resolved_once() {
        let registry = ExecutionRegistry::default();
        let (sender, mut receiver) = mpsc::channel(1);
        let _guard = registry
            .register(
                "trace-approval".into(),
                "session-approval".into(),
                PathBuf::from("workspace-approval"),
                ExecutionCancellation::default(),
                sender,
            )
            .expect("register execution");
        assert!(registry.register_request("trace-approval", "approval-1"));

        let first = registry.resolve(
            "approval-1",
            "session-approval",
            "trace-approval",
            Some(Path::new("workspace-approval")),
            serde_json::json!({"approved": false}),
        );
        assert!(first.accepted);
        let repeated = registry.resolve(
            "approval-1",
            "session-approval",
            "trace-approval",
            Some(Path::new("workspace-approval")),
            serde_json::json!({"approved": false}),
        );
        assert!(!repeated.accepted);
        assert_eq!(repeated.reason, "request_not_pending");
        assert!(matches!(
            receiver.try_recv(),
            Ok(ExecutionControl::Resolve { request_id, .. }) if request_id == "approval-1"
        ));
        assert!(receiver.try_recv().is_err());
    }
}
