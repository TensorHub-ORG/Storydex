use crate::AgentEvent;
use crate::AgentObserver;
use crate::ApprovalHandler;
use crate::ChatMessage;
use crate::CompactionCheckpoint;
use crate::CompactionRequest;
use crate::InputQueue;
use crate::ModelProvider;
use crate::ModelRequest;
use crate::ModelStreamObserver;
use crate::ReasoningEffort;
use crate::SUMMARIZATION_PROMPT;
use crate::Session;
use crate::ToolRuntime;
use crate::compacted_history;
use crate::normalize_history;
use crate::trim_history_to_fit;
use std::fmt;
use std::sync::Arc;
use std::time::Duration;
use std::time::Instant;

#[derive(Debug)]
pub enum AgentError {
    Provider(anyhow::Error),
    Compaction(anyhow::Error),
    Checkpoint(anyhow::Error),
    ToolRoundLimit { limit: usize },
    Hook(String),
}

impl fmt::Display for AgentError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Provider(error) => write!(formatter, "provider request failed: {error:#}"),
            Self::Compaction(error) => write!(formatter, "context compaction failed: {error:#}"),
            Self::Checkpoint(error) => write!(formatter, "session checkpoint failed: {error:#}"),
            Self::ToolRoundLimit { limit } => {
                write!(formatter, "tool round limit reached ({limit})")
            }
            Self::Hook(error) => write!(formatter, "hook failed: {error}"),
        }
    }
}

impl std::error::Error for AgentError {}

pub struct Agent {
    system_prompt: String,
    max_tool_rounds: usize,
    force_compaction: bool,
    input_queue: Option<Arc<InputQueue>>,
    reasoning_effort: ReasoningEffort,
    compaction_checkpoint_writer: Option<Arc<dyn Fn(&Session) -> anyhow::Result<()> + Send + Sync>>,
}

impl Agent {
    pub fn new(system_prompt: impl Into<String>) -> Self {
        Self {
            system_prompt: system_prompt.into(),
            // Match the mature Python runtime: substantial coding turns commonly
            // need more than 24 model/tool exchanges, while 100 remains a guard
            // against an unbounded provider loop.
            max_tool_rounds: 100,
            force_compaction: false,
            input_queue: None,
            reasoning_effort: ReasoningEffort::Auto,
            compaction_checkpoint_writer: None,
        }
    }

    pub fn with_forced_compaction(mut self, force_compaction: bool) -> Self {
        self.force_compaction = force_compaction;
        self
    }

    pub fn with_max_tool_rounds(mut self, max_tool_rounds: usize) -> Self {
        self.max_tool_rounds = max_tool_rounds.max(1);
        self
    }

    pub fn with_input_queue(mut self, input_queue: Arc<InputQueue>) -> Self {
        self.input_queue = Some(input_queue);
        self
    }

    pub fn with_reasoning_effort(mut self, reasoning_effort: ReasoningEffort) -> Self {
        self.reasoning_effort = reasoning_effort;
        self
    }

    pub fn with_compaction_checkpoint_writer<F>(mut self, writer: F) -> Self
    where
        F: Fn(&Session) -> anyhow::Result<()> + Send + Sync + 'static,
    {
        self.compaction_checkpoint_writer = Some(Arc::new(writer));
        self
    }

    pub async fn run_turn(
        &self,
        session: &mut Session,
        prompt: impl Into<String>,
        provider: &dyn ModelProvider,
        tools: &dyn ToolRuntime,
        approval: &dyn ApprovalHandler,
        observer: &dyn AgentObserver,
    ) -> Result<String, AgentError> {
        self.run_accounted_turn(
            session,
            ChatMessage::user(prompt),
            provider,
            tools,
            approval,
            observer,
        )
        .await
    }

    pub async fn continue_interrupted_turn(
        &self,
        session: &mut Session,
        provider: &dyn ModelProvider,
        tools: &dyn ToolRuntime,
        approval: &dyn ApprovalHandler,
        observer: &dyn AgentObserver,
    ) -> Result<String, AgentError> {
        self.run_accounted_turn(
            session,
            ChatMessage::internal_user(
                "<recovery_context>The previous turn was interrupted. Continue from the current session checkpoint, complete only unfinished work, and do not repeat completed tool operations.</recovery_context>",
            ),
            provider,
            tools,
            approval,
            observer,
        )
        .await
    }

    fn run_checkpoint(&self, session: &Session) -> Result<(), AgentError> {
        if let Some(writer) = &self.compaction_checkpoint_writer {
            writer(session).map_err(AgentError::Checkpoint)?;
        }
        Ok(())
    }

    pub async fn continue_loop(
        &self,
        session: &mut Session,
        provider: &dyn ModelProvider,
        tools: &dyn ToolRuntime,
        approval: &dyn ApprovalHandler,
        observer: &dyn AgentObserver,
    ) -> Result<String, AgentError> {
        let objective = session
            .loop_state
            .as_ref()
            .filter(|state| state.status == crate::LoopStatus::Active)
            .map(|state| state.objective.clone())
            .unwrap_or_default();
        let prompt = format!(
            "<loop_context>\nContinue working autonomously toward the active Loop objective: {objective}\nMake concrete progress, use tools when needed, and only mark the Loop complete when the objective is fully achieved.\n</loop_context>"
        );
        self.run_accounted_turn(
            session,
            ChatMessage::internal_user(prompt),
            provider,
            tools,
            approval,
            observer,
        )
        .await
    }

    pub async fn compact_session(
        &self,
        session: &mut Session,
        provider: &dyn ModelProvider,
        tools: &dyn ToolRuntime,
        observer: &dyn AgentObserver,
    ) -> Result<(), AgentError> {
        if session.messages.is_empty() {
            return Err(AgentError::Compaction(anyhow::anyhow!(
                "the current session has no context to compact"
            )));
        }
        let tool_specs = if provider.capabilities().supports_native_tools {
            tools.specs()
        } else {
            Vec::new()
        };
        let normalized = normalize_history(&session.messages);
        session
            .context
            .recompute(&self.system_prompt, &normalized, &tool_specs);
        observer.on_event(&AgentEvent::ContextUpdated(
            session.context.status(&provider.capabilities()),
        ));
        self.compact(session, provider, &tool_specs, observer, false)
            .await?;
        session.touch();
        self.run_checkpoint(session)?;
        Ok(())
    }

    async fn run_accounted_turn(
        &self,
        session: &mut Session,
        prompt: ChatMessage,
        provider: &dyn ModelProvider,
        tools: &dyn ToolRuntime,
        approval: &dyn ApprovalHandler,
        observer: &dyn AgentObserver,
    ) -> Result<String, AgentError> {
        let usage_before = session.usage.total_tokens();
        let started = Instant::now();
        let mut result = self
            .run_turn_message(session, prompt, provider, tools, approval, observer)
            .await;
        let lifecycle = tools
            .lifecycle(
                "turn_end",
                serde_json::json!({
                    "session_id": session.id,
                    "success": result.is_ok(),
                    "error": result.as_ref().err().map(ToString::to_string),
                }),
            )
            .await;
        match lifecycle {
            Ok(Some(context)) if !context.trim().is_empty() => {
                session.messages.push(ChatMessage::internal_user(context));
            }
            Err(error) if result.is_ok() => result = Err(AgentError::Hook(error)),
            _ => {}
        }
        update_loop_accounting(
            session,
            usage_before,
            started.elapsed(),
            result.as_ref().err(),
            observer,
        );
        result
    }

    async fn run_turn_message(
        &self,
        session: &mut Session,
        prompt: ChatMessage,
        provider: &dyn ModelProvider,
        tools: &dyn ToolRuntime,
        approval: &dyn ApprovalHandler,
        observer: &dyn AgentObserver,
    ) -> Result<String, AgentError> {
        if !session.hooks_started {
            if let Some(context) = tools
                .lifecycle(
                    "session_start",
                    serde_json::json!({"session_id": session.id, "cwd": session.cwd}),
                )
                .await
                .map_err(AgentError::Hook)?
                .filter(|context| !context.trim().is_empty())
            {
                session.messages.push(ChatMessage::internal_user(context));
            }
            session.hooks_started = true;
        }
        if let Some(context) = tools
            .lifecycle(
                "turn_start",
                serde_json::json!({
                    "session_id": session.id,
                    "prompt": prompt.content,
                    "internal": prompt.internal,
                }),
            )
            .await
            .map_err(AgentError::Hook)?
            .filter(|context| !context.trim().is_empty())
        {
            session.messages.push(ChatMessage::internal_user(context));
        }
        session.messages.push(prompt);
        self.run_checkpoint(session)?;
        let capabilities = provider.capabilities();
        let tool_specs = if capabilities.supports_native_tools {
            tools.specs()
        } else {
            Vec::new()
        };
        let mut compacted_for_provider_error = false;

        for round in 1..=self.max_tool_rounds {
            let normalized = normalize_history(&session.messages);
            session
                .context
                .recompute(&self.system_prompt, &normalized, &tool_specs);
            observer.on_event(&AgentEvent::ContextUpdated(
                session.context.status(&capabilities),
            ));
            let should_compact = (self.force_compaction && round == 1)
                || session.context.should_compact(&capabilities);
            if should_compact {
                self.compact(
                    session,
                    provider,
                    &tool_specs,
                    observer,
                    !(self.force_compaction && round == 1),
                )
                .await?;
                self.run_checkpoint(session)?;
            } else {
                session.messages = normalized;
            }

            observer.on_event(&AgentEvent::ModelStarted {
                provider: provider.provider_id().to_string(),
                model: provider.model().to_string(),
                round,
            });

            let mut messages = Vec::with_capacity(session.messages.len() + 1);
            messages.push(ChatMessage::system(self.system_prompt.clone()));
            messages.extend(session.messages.iter().cloned());
            let request = ModelRequest {
                model: provider.model().to_string(),
                messages,
                tools: tool_specs.clone(),
                max_output_tokens: Some(capabilities.max_output_tokens),
                required_tool: None,
                reasoning_effort: self.reasoning_effort,
            };
            let stream_observer = ObserverStream { observer };
            let response = match provider.complete_stream(request, &stream_observer).await {
                Ok(response) => response,
                Err(error) if !compacted_for_provider_error && is_context_window_error(&error) => {
                    compacted_for_provider_error = true;
                    self.compact(session, provider, &tool_specs, observer, true)
                        .await?;
                    continue;
                }
                Err(error) => return Err(AgentError::Provider(error)),
            };

            session.usage.add(&response.usage);
            observer.on_event(&AgentEvent::ModelCompleted {
                round,
                metadata: response.metadata.clone(),
                usage: response.usage.clone(),
            });
            if !response.streamed && !response.content.is_empty() {
                observer.on_event(&AgentEvent::Text(response.content.clone()));
            }
            let mut assistant_message =
                ChatMessage::assistant(response.content.clone(), response.tool_calls.clone());
            assistant_message.provider_items = response.provider_items.clone();
            session.messages.push(assistant_message);
            session.context.observe_usage(
                &response.usage,
                &self.system_prompt,
                &session.messages,
                &tool_specs,
                &capabilities,
            );
            observer.on_event(&AgentEvent::ContextUpdated(
                session.context.status(&capabilities),
            ));
            self.run_checkpoint(session)?;

            if response.tool_calls.is_empty() {
                if self.accept_queued_input(session, observer) {
                    continue;
                }
                session.touch();
                self.run_checkpoint(session)?;
                observer.on_event(&AgentEvent::TurnCompleted(session.usage.clone()));
                return Ok(response.content);
            }

            for call in response.tool_calls {
                observer.on_event(&AgentEvent::ToolStarted(call.clone()));
                let result = tools.call(&call, approval).await;
                if let Some(plan) = result.plan.clone() {
                    session.plan = Some(plan.clone());
                    observer.on_event(&AgentEvent::PlanUpdated(plan));
                }
                if let Some(loop_state) = result.loop_state.clone() {
                    session.loop_state = Some(loop_state.clone());
                    observer.on_event(&AgentEvent::LoopUpdated(loop_state));
                }
                observer.on_event(&AgentEvent::ToolFinished {
                    call: call.clone(),
                    result: result.clone(),
                });
                let status = if result.success { "success" } else { "error" };
                let mut tool_message =
                    ChatMessage::tool(call.id, format!("{status}: {}", result.output));
                tool_message.images = result.images.clone();
                session.messages.push(tool_message);
                if let Some(context) = result.additional_context
                    && !context.trim().is_empty()
                {
                    session.messages.push(ChatMessage::internal_user(context));
                }
                self.run_checkpoint(session)?;
            }
            if self.accept_queued_input(session, observer) {
                self.run_checkpoint(session)?;
            }
        }

        session.touch();
        let message = format!(
            "Tool loop stopped after {} rounds to prevent an infinite cycle. The session is preserved; continue with a different approach.",
            self.max_tool_rounds
        );
        session
            .messages
            .push(ChatMessage::assistant(message.clone(), Vec::new()));
        self.run_checkpoint(session)?;
        observer.on_event(&AgentEvent::Text(message.clone()));
        observer.on_event(&AgentEvent::TurnCompleted(session.usage.clone()));
        Ok(message)
    }

    async fn compact(
        &self,
        session: &mut Session,
        provider: &dyn ModelProvider,
        tool_specs: &[crate::ToolSpec],
        observer: &dyn AgentObserver,
        automatic: bool,
    ) -> Result<(), AgentError> {
        let before_tokens = session.context.estimated_active_tokens;
        let checkpoint = CompactionCheckpoint::from_session(session)
            .map_err(|error| AgentError::Compaction(anyhow::anyhow!(error)))?;
        checkpoint
            .validate()
            .map_err(|error| AgentError::Compaction(anyhow::anyhow!(error)))?;
        let checkpoint_hash = checkpoint.checkpoint_hash.clone();
        let tool_call_count = checkpoint.tool_calls.len();
        let evidence_revision_count = checkpoint.evidence_revisions.len();
        session.compaction_checkpoint = Some(checkpoint);
        if let Some(writer) = &self.compaction_checkpoint_writer {
            writer(session).map_err(AgentError::Compaction)?;
        }
        observer.on_event(&AgentEvent::CompactionStarted {
            automatic,
            checkpoint_valid: true,
            checkpoint_hash,
            tool_call_count,
            evidence_revision_count,
        });
        let capabilities = provider.capabilities();
        let mut normalized = normalize_history(&session.messages);
        let compaction_limit = capabilities
            .context_window
            .saturating_sub(capabilities.max_output_tokens)
            .max(1);
        trim_history_to_fit(&self.system_prompt, &mut normalized, &[], compaction_limit);
        let remote = provider
            .compact(CompactionRequest {
                model: provider.model().to_string(),
                messages: normalized.clone(),
                system_prompt: self.system_prompt.clone(),
                tools: tool_specs.to_vec(),
            })
            .await
            .map_err(AgentError::Compaction)?;

        let (messages, compact_usage) = if let Some(response) = remote {
            (normalize_history(&response.messages), response.usage)
        } else {
            let prompt_overhead = crate::estimate_request_tokens(
                &self.system_prompt,
                &[ChatMessage::user(SUMMARIZATION_PROMPT)],
                &[],
            );
            trim_history_to_fit(
                &self.system_prompt,
                &mut normalized,
                &[],
                compaction_limit.saturating_sub(prompt_overhead).max(1),
            );
            let mut compact_input = Vec::with_capacity(normalized.len() + 2);
            compact_input.push(ChatMessage::system(self.system_prompt.clone()));
            compact_input.extend(normalized.clone());
            compact_input.push(ChatMessage::user(SUMMARIZATION_PROMPT));
            let response = provider
                .complete(ModelRequest {
                    model: provider.model().to_string(),
                    messages: compact_input,
                    tools: Vec::new(),
                    max_output_tokens: Some(capabilities.max_output_tokens),
                    required_tool: None,
                    reasoning_effort: self.reasoning_effort,
                })
                .await
                .map_err(AgentError::Compaction)?;
            (
                compacted_history(&normalized, response.content.trim()),
                response.usage,
            )
        };
        session.messages = messages;
        session.context.reset_after_compaction(
            &self.system_prompt,
            &session.messages,
            tool_specs,
            &capabilities,
        );
        session.usage.add(&compact_usage);
        let status = session.context.status(&provider.capabilities());
        observer.on_event(&AgentEvent::CompactionCompleted {
            automatic,
            before_tokens,
            after_tokens: status.used_tokens,
        });
        observer.on_event(&AgentEvent::ContextUpdated(status));
        self.run_checkpoint(session)?;
        Ok(())
    }

    fn accept_queued_input(&self, session: &mut Session, observer: &dyn AgentObserver) -> bool {
        let Some(input_queue) = &self.input_queue else {
            return false;
        };
        let messages = input_queue.drain();
        if messages.is_empty() {
            return false;
        }
        session
            .messages
            .extend(messages.iter().cloned().map(ChatMessage::user));
        observer.on_event(&AgentEvent::QueuedInputAccepted(messages));
        true
    }
}

fn update_loop_accounting(
    session: &mut Session,
    usage_before: u64,
    elapsed: Duration,
    error: Option<&AgentError>,
    observer: &dyn AgentObserver,
) {
    let Some(loop_state) = session.loop_state.as_mut() else {
        return;
    };
    loop_state.tokens_used = loop_state
        .tokens_used
        .saturating_add(session.usage.total_tokens().saturating_sub(usage_before));
    loop_state.time_used_seconds = loop_state
        .time_used_seconds
        .saturating_add(elapsed.as_secs());
    loop_state.turns_completed = loop_state.turns_completed.saturating_add(1);
    if loop_state.status == crate::LoopStatus::Active
        && loop_state
            .token_budget
            .is_some_and(|budget| loop_state.tokens_used >= budget)
    {
        loop_state.status = crate::LoopStatus::BudgetLimited;
    } else if loop_state.status == crate::LoopStatus::Active
        && error.is_some_and(is_usage_limit_error)
    {
        loop_state.status = crate::LoopStatus::UsageLimited;
    }
    observer.on_event(&AgentEvent::LoopUpdated(loop_state.clone()));
}

fn is_usage_limit_error(error: &AgentError) -> bool {
    let text = error.to_string().to_ascii_lowercase();
    text.contains("429")
        || text.contains("rate limit")
        || text.contains("usage limit")
        || text.contains("quota")
}

struct ObserverStream<'a> {
    observer: &'a dyn AgentObserver,
}

impl ModelStreamObserver for ObserverStream<'_> {
    fn on_text_delta(&self, delta: &str) {
        self.observer
            .on_event(&AgentEvent::TextDelta(delta.to_owned()));
    }

    fn on_reasoning_delta(&self, delta: &str) {
        self.observer
            .on_event(&AgentEvent::ReasoningDelta(delta.to_owned()));
    }

    fn on_provider_stream(&self, event: &crate::ProviderStreamEvent) {
        self.observer
            .on_event(&AgentEvent::ProviderStream(event.clone()));
    }

    fn on_provider_retry(&self, attempt: usize, max_attempts: usize, reset_text_characters: usize) {
        self.observer.on_event(&AgentEvent::ProviderRetry {
            attempt,
            max_attempts,
            reset_text_characters,
        });
    }
}

fn is_context_window_error(error: &anyhow::Error) -> bool {
    let value = format!("{error:#}").to_ascii_lowercase();
    value.contains("context_window_exceeded")
        || value.contains("context length")
        || value.contains("maximum context")
        || value.contains("too many tokens")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ModelCapabilities;
    use crate::ModelResponse;
    use crate::NoopObserver;
    use crate::ToolCall;
    use crate::ToolResult;
    use crate::ToolSpec;
    use anyhow::Result;
    use async_trait::async_trait;
    use serde_json::json;
    use std::path::PathBuf;
    use std::sync::Mutex;

    #[test]
    fn provider_errors_include_the_underlying_context_chain() {
        let error = anyhow::anyhow!("connection reset").context("provider stream failed");
        let message = AgentError::Provider(error).to_string();

        assert_eq!(
            message,
            "provider request failed: provider stream failed: connection reset"
        );
    }

    struct Approve;

    #[async_trait]
    impl ApprovalHandler for Approve {
        async fn approve(&self, _call: &ToolCall, _reason: &str) -> bool {
            true
        }
    }

    struct EchoTool;

    #[async_trait]
    impl ToolRuntime for EchoTool {
        fn specs(&self) -> Vec<ToolSpec> {
            vec![ToolSpec {
                name: "echo".into(),
                description: "echo".into(),
                parameters: json!({"type": "object"}),
            }]
        }

        async fn call(&self, call: &ToolCall, _approval: &dyn ApprovalHandler) -> ToolResult {
            ToolResult::success(call.arguments["value"].as_str().unwrap_or_default())
        }
    }

    struct MockProvider {
        calls: Mutex<usize>,
    }

    #[async_trait]
    impl ModelProvider for MockProvider {
        fn provider_id(&self) -> &str {
            "mock"
        }

        fn model(&self) -> &str {
            "mock-model"
        }

        async fn complete(&self, request: ModelRequest) -> Result<ModelResponse> {
            assert_eq!(
                request.max_output_tokens,
                Some(ModelCapabilities::default().max_output_tokens)
            );
            let mut calls = self.calls.lock().expect("lock mock call count");
            *calls += 1;
            if *calls == 1 {
                return Ok(ModelResponse {
                    tool_calls: vec![ToolCall {
                        id: "call-1".into(),
                        name: "echo".into(),
                        arguments: json!({"value": "ok"}),
                    }],
                    ..ModelResponse::default()
                });
            }
            assert!(request.messages.iter().any(|message| {
                message.role == crate::Role::Tool && message.content.contains("success: ok")
            }));
            Ok(ModelResponse {
                content: "done".into(),
                ..ModelResponse::default()
            })
        }
    }

    #[tokio::test]
    async fn completes_a_native_tool_loop() {
        let mut session = Session::new("mock", "mock-model", PathBuf::from("."));
        let provider = MockProvider {
            calls: Mutex::new(0),
        };
        let output = Agent::new("test")
            .run_turn(
                &mut session,
                "run",
                &provider,
                &EchoTool,
                &Approve,
                &NoopObserver,
            )
            .await
            .expect("agent turn");
        assert_eq!(output, "done");
        assert_eq!(session.messages.len(), 4);
    }

    struct QueuedInputProvider {
        calls: Mutex<usize>,
    }

    #[async_trait]
    impl ModelProvider for QueuedInputProvider {
        fn provider_id(&self) -> &str {
            "mock"
        }

        fn model(&self) -> &str {
            "queued"
        }

        async fn complete(&self, request: ModelRequest) -> Result<ModelResponse> {
            let mut calls = self.calls.lock().expect("lock calls");
            *calls += 1;
            if *calls == 1 {
                return Ok(ModelResponse {
                    content: "ready for follow-up".into(),
                    ..Default::default()
                });
            }
            assert!(request.messages.iter().any(|message| {
                message.role == crate::Role::User && message.content == "also check tests"
            }));
            Ok(ModelResponse {
                content: "done".into(),
                ..Default::default()
            })
        }
    }

    #[tokio::test]
    async fn queued_input_continues_the_active_model_loop() {
        let queue = Arc::new(InputQueue::default());
        queue.push("also check tests".into());
        let mut session = Session::new("mock", "queued", PathBuf::from("."));
        let output = Agent::new("test")
            .with_input_queue(queue)
            .run_turn(
                &mut session,
                "start",
                &QueuedInputProvider {
                    calls: Mutex::new(0),
                },
                &EchoTool,
                &Approve,
                &NoopObserver,
            )
            .await
            .expect("queued turn");
        assert_eq!(output, "done");
    }

    struct CompactingProvider {
        calls: Mutex<usize>,
    }

    #[async_trait]
    impl ModelProvider for CompactingProvider {
        fn provider_id(&self) -> &str {
            "mock"
        }

        fn model(&self) -> &str {
            "tiny"
        }

        fn capabilities(&self) -> ModelCapabilities {
            ModelCapabilities {
                context_window: 100,
                effective_context_window_percent: 100,
                auto_compact_token_limit: Some(90),
                ..ModelCapabilities::default()
            }
        }

        async fn complete(&self, request: ModelRequest) -> Result<ModelResponse> {
            let mut calls = self.calls.lock().expect("lock calls");
            *calls += 1;
            if request.tools.is_empty() {
                return Ok(ModelResponse {
                    content: "summary".into(),
                    usage: crate::TokenUsage {
                        input_tokens: 40,
                        output_tokens: 4,
                        ..Default::default()
                    },
                    ..Default::default()
                });
            }
            Ok(ModelResponse {
                content: "done".into(),
                usage: crate::TokenUsage {
                    input_tokens: 50,
                    output_tokens: 2,
                    ..Default::default()
                },
                ..Default::default()
            })
        }
    }

    #[tokio::test]
    async fn auto_compaction_replaces_history_with_summary() {
        let mut session = Session::new("mock", "tiny", PathBuf::from("."));
        session.messages.push(ChatMessage::user("x".repeat(500)));
        let provider = CompactingProvider {
            calls: Mutex::new(0),
        };
        Agent::new("test")
            .run_turn(
                &mut session,
                "continue",
                &provider,
                &EchoTool,
                &Approve,
                &NoopObserver,
            )
            .await
            .expect("compacted turn");
        assert_eq!(session.context.compaction_count, 1);
        assert!(
            session
                .messages
                .iter()
                .any(|message| message.compaction_summary)
        );
    }

    #[tokio::test]
    async fn manual_compaction_is_a_standalone_operation() {
        let mut session = Session::new("mock", "tiny", PathBuf::from("."));
        session
            .messages
            .push(ChatMessage::user("keep this context"));
        let provider = CompactingProvider {
            calls: Mutex::new(0),
        };
        Agent::new("test")
            .compact_session(&mut session, &provider, &EchoTool, &NoopObserver)
            .await
            .expect("manual compaction");
        assert_eq!(*provider.calls.lock().expect("lock calls"), 1);
        assert_eq!(session.context.compaction_count, 1);
        assert!(session.messages.last().is_some_and(|message| {
            message.compaction_summary && message.content.contains("summary")
        }));
        assert!(
            session
                .compaction_checkpoint
                .as_ref()
                .is_some_and(|checkpoint| {
                    checkpoint.validate().is_ok() && checkpoint.source_message_count == 1
                })
        );
    }

    #[tokio::test]
    async fn invalid_checkpoint_preserves_the_original_session() {
        let mut session = Session::new("mock", "tiny", PathBuf::from("."));
        session.messages.push(ChatMessage::user("keep this target"));
        session
            .messages
            .push(ChatMessage::tool("orphan", "unexpected result"));
        let before = session.messages.clone();
        let provider = CompactingProvider {
            calls: Mutex::new(0),
        };

        let error = Agent::new("test")
            .compact_session(&mut session, &provider, &EchoTool, &NoopObserver)
            .await
            .expect_err("invalid checkpoint must fail closed");

        assert!(error.to_string().contains("orphan tool result"));
        assert_eq!(session.messages, before);
        assert_eq!(*provider.calls.lock().expect("lock calls"), 0);
    }

    #[tokio::test]
    async fn compaction_persists_pre_compaction_checkpoint_before_provider_call() {
        let mut session = Session::new("mock", "tiny", PathBuf::from("."));
        session
            .messages
            .push(ChatMessage::user("retain original target"));
        let snapshots = Arc::new(Mutex::new(Vec::<Session>::new()));
        let writer_snapshots = Arc::clone(&snapshots);
        let provider = CompactingProvider {
            calls: Mutex::new(0),
        };

        Agent::new("test")
            .with_compaction_checkpoint_writer(move |value| {
                writer_snapshots
                    .lock()
                    .expect("lock checkpoint snapshots")
                    .push(value.clone());
                Ok(())
            })
            .compact_session(&mut session, &provider, &EchoTool, &NoopObserver)
            .await
            .expect("compact with checkpoint writer");

        let snapshots = snapshots.lock().expect("lock checkpoint snapshots");
        assert!(snapshots.len() >= 2);
        assert_eq!(snapshots[0].messages[0].content, "retain original target");
        assert!(snapshots[0].compaction_checkpoint.is_some());
        assert!(snapshots.last().is_some_and(|persisted| {
            persisted
                .messages
                .iter()
                .any(|message| message.compaction_summary)
                && persisted.compaction_checkpoint.is_some()
        }));
    }

    #[test]
    fn loop_accounting_stops_at_the_token_budget() {
        let mut session = Session::new("mock", "model", PathBuf::from("."));
        session.usage.input_tokens = 30;
        session.loop_state = Some(crate::LoopState {
            objective: "finish".into(),
            status: crate::LoopStatus::Active,
            token_budget: Some(20),
            tokens_used: 0,
            time_used_seconds: 0,
            blocked_streak: 0,
            turns_completed: 0,
        });
        update_loop_accounting(&mut session, 0, Duration::from_secs(2), None, &NoopObserver);
        let state = session.loop_state.expect("loop state");
        assert_eq!(state.status, crate::LoopStatus::BudgetLimited);
        assert_eq!(state.tokens_used, 30);
        assert_eq!(state.turns_completed, 1);
    }
}
