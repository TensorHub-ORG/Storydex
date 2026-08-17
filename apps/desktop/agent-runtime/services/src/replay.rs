//! Deterministic, provider-network-free model responses for Refactor fixtures.
//!
//! Replay is intentionally opt-in.  The caller must pass an explicit fixture
//! path; a missing file, an unexpected request, or an incomplete fixture is a
//! hard error.  This keeps replay useful for Python/Rust differential tests
//! without allowing a production request to silently turn into fake success.

use crate::config::ProviderConfig;
use anyhow::{Context, Result, bail, ensure};
use async_trait::async_trait;
use coomi_engine::{
    ChatMessage, ModelProvider, ModelRequest, ModelResponse, ModelResponseMetadata,
    ModelStreamObserver, ProviderStreamEvent, ProviderStreamPhase, TokenUsage, ToolCall,
};
use serde::Deserialize;
use serde_json::Value;
use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

const MAX_REPLAY_DELAY_MS: u64 = 60_000;

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ReplayFixture {
    schema_version: u32,
    contract_id: String,
    provider_id: String,
    model: String,
    steps: Vec<ReplayStep>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ReplayStep {
    #[serde(default)]
    expect: ReplayExpectation,
    response: ReplayResponse,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ReplayExpectation {
    #[serde(default)]
    message_contains: Option<String>,
    #[serde(default)]
    tool_names: Vec<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ReplayResponse {
    #[serde(default)]
    content: String,
    #[serde(default)]
    tool_calls: Vec<ReplayToolCall>,
    #[serde(default)]
    usage: ReplayUsage,
    #[serde(default)]
    response_model: String,
    #[serde(default)]
    finish_reason: String,
    #[serde(default)]
    response_status: String,
    #[serde(default)]
    native_reasoning: bool,
    #[serde(default)]
    delay_ms: u64,
}

#[derive(Clone, Debug, Default, Deserialize)]
struct ReplayUsage {
    #[serde(default)]
    input_tokens: u64,
    #[serde(default)]
    cached_input_tokens: u64,
    #[serde(default)]
    output_tokens: u64,
    #[serde(default)]
    reasoning_tokens: Option<u64>,
}

impl From<ReplayUsage> for TokenUsage {
    fn from(value: ReplayUsage) -> Self {
        Self {
            input_tokens: value.input_tokens,
            cached_input_tokens: value.cached_input_tokens,
            output_tokens: value.output_tokens,
            reasoning_tokens: value.reasoning_tokens,
        }
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ReplayToolCall {
    id: String,
    name: String,
    #[serde(default)]
    arguments: Value,
}

#[derive(Debug, Default)]
struct ReplayState {
    next_step: usize,
}

/// A [`ModelProvider`] backed by a checked-in deterministic fixture.
#[derive(Clone)]
pub struct ReplayModelProvider {
    config: ProviderConfig,
    fixture_path: PathBuf,
    fixture: ReplayFixture,
    state: Arc<Mutex<ReplayState>>,
}

impl ReplayModelProvider {
    pub fn load(config: ProviderConfig, path: impl AsRef<Path>) -> Result<Self> {
        let fixture_path = path.as_ref().to_path_buf();
        let raw = fs::read_to_string(&fixture_path).with_context(|| {
            format!(
                "unable to read provider replay fixture {}",
                fixture_path.display()
            )
        })?;
        let fixture: ReplayFixture = serde_json::from_str(&raw).with_context(|| {
            format!("invalid provider replay fixture {}", fixture_path.display())
        })?;
        ensure!(
            fixture.schema_version == 1,
            "unsupported provider replay schema version {}",
            fixture.schema_version
        );
        ensure!(
            !fixture.contract_id.trim().is_empty(),
            "provider replay contract id must not be empty"
        );
        ensure!(
            fixture.provider_id == config.id,
            "provider replay provider {} does not match configured provider {}",
            fixture.provider_id,
            config.id
        );
        ensure!(
            fixture.model == config.model,
            "provider replay model {} does not match configured model {}",
            fixture.model,
            config.model
        );
        ensure!(
            !fixture.steps.is_empty(),
            "provider replay fixture must contain at least one step"
        );
        for (index, step) in fixture.steps.iter().enumerate() {
            ensure!(
                !step.response.content.is_empty() || !step.response.tool_calls.is_empty(),
                "provider replay step {} must contain content or a tool call",
                index + 1
            );
            ensure!(
                step.response.delay_ms <= MAX_REPLAY_DELAY_MS,
                "provider replay step {} delay exceeds {}ms",
                index + 1,
                MAX_REPLAY_DELAY_MS
            );
            for call in &step.response.tool_calls {
                ensure!(
                    !call.id.trim().is_empty(),
                    "provider replay step {} has an empty tool call id",
                    index + 1
                );
                ensure!(
                    !call.name.trim().is_empty(),
                    "provider replay step {} has an empty tool name",
                    index + 1
                );
            }
        }
        Ok(Self {
            config,
            fixture_path,
            fixture,
            state: Arc::new(Mutex::new(ReplayState::default())),
        })
    }

    pub fn fixture_path(&self) -> &Path {
        &self.fixture_path
    }

    pub fn assert_complete(&self) -> Result<()> {
        let state = self
            .state
            .lock()
            .map_err(|_| anyhow::anyhow!("provider replay state lock poisoned"))?;
        ensure!(
            state.next_step == self.fixture.steps.len(),
            "provider replay fixture has {} unused step(s)",
            self.fixture.steps.len().saturating_sub(state.next_step)
        );
        Ok(())
    }

    fn next_response(&self, request: &ModelRequest) -> Result<(ModelResponse, u64)> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| anyhow::anyhow!("provider replay state lock poisoned"))?;
        let step_index = state.next_step;
        let Some(step) = self.fixture.steps.get(step_index) else {
            bail!(
                "provider replay fixture exhausted before model request {}",
                step_index + 1
            );
        };
        validate_request(step_index, &step.expect, request)?;
        state.next_step += 1;
        let response = &step.response;
        let tool_calls = response
            .tool_calls
            .iter()
            .map(|call| ToolCall {
                id: call.id.clone(),
                name: call.name.clone(),
                arguments: call.arguments.clone(),
            })
            .collect::<Vec<_>>();
        let response_model = if response.response_model.trim().is_empty() {
            self.config.model.clone()
        } else {
            response.response_model.clone()
        };
        let finish_reason = if response.finish_reason.trim().is_empty() {
            if tool_calls.is_empty() {
                "stop"
            } else {
                "tool_calls"
            }
            .to_owned()
        } else {
            response.finish_reason.clone()
        };
        let response_status = if response.response_status.trim().is_empty() {
            "200".to_owned()
        } else {
            response.response_status.clone()
        };
        Ok((
            ModelResponse {
                content: response.content.clone(),
                tool_calls,
                usage: response.usage.clone().into(),
                streamed: true,
                provider_items: Vec::new(),
                metadata: ModelResponseMetadata {
                    response_model: Some(response_model),
                    finish_reason: Some(finish_reason),
                    response_status: Some(response_status),
                    native_reasoning: response.native_reasoning,
                },
            },
            response.delay_ms,
        ))
    }
}

fn validate_request(
    step_index: usize,
    expected: &ReplayExpectation,
    request: &ModelRequest,
) -> Result<()> {
    if let Some(needle) = expected.message_contains.as_deref() {
        let found = request
            .messages
            .iter()
            .any(|message| message.content.contains(needle));
        ensure!(
            found,
            "provider replay step {} did not find expected message marker",
            step_index + 1
        );
    }
    let available = request
        .tools
        .iter()
        .map(|tool| tool.name.as_str())
        .collect::<HashSet<_>>();
    for name in &expected.tool_names {
        ensure!(
            available.contains(name.as_str()),
            "provider replay step {} is missing expected tool {}",
            step_index + 1,
            name
        );
    }
    Ok(())
}

fn request_bytes(request: &ModelRequest) -> u64 {
    request
        .messages
        .iter()
        .map(|message: &ChatMessage| message.content.len() as u64)
        .sum::<u64>()
        .saturating_add(
            request
                .tools
                .iter()
                .map(|tool| tool.name.len() as u64 + tool.description.len() as u64)
                .sum(),
        )
}

fn response_bytes(response: &ModelResponse) -> u64 {
    response.content.len() as u64
        + response
            .tool_calls
            .iter()
            .map(|call| call.name.len() as u64 + call.arguments.to_string().len() as u64)
            .sum::<u64>()
}

#[async_trait]
impl ModelProvider for ReplayModelProvider {
    fn provider_id(&self) -> &str {
        &self.config.id
    }

    fn model(&self) -> &str {
        &self.config.model
    }

    fn capabilities(&self) -> coomi_engine::ModelCapabilities {
        self.config.capabilities.clone()
    }

    async fn complete(&self, request: ModelRequest) -> Result<ModelResponse> {
        let (response, delay_ms) = self.next_response(&request)?;
        if delay_ms != 0 {
            tokio::time::sleep(Duration::from_millis(delay_ms)).await;
        }
        Ok(response)
    }

    async fn complete_stream(
        &self,
        request: ModelRequest,
        observer: &dyn ModelStreamObserver,
    ) -> Result<ModelResponse> {
        let request_size = request_bytes(&request);
        let (response, delay_ms) = self.next_response(&request)?;
        if delay_ms != 0 {
            tokio::time::sleep(Duration::from_millis(delay_ms)).await;
        }
        let response_size = response_bytes(&response);
        let phases = [
            (ProviderStreamPhase::RequestStarted, 0, 0),
            (ProviderStreamPhase::ResponseHead, 0, 0),
            (ProviderStreamPhase::FirstByte, 0, response_size),
            (ProviderStreamPhase::FirstEvent, 0, response_size),
            (ProviderStreamPhase::Completed, 0, response_size),
        ];
        for (phase, elapsed_ms, bytes) in phases {
            observer.on_provider_stream(&ProviderStreamEvent {
                attempt: 1,
                phase,
                elapsed_ms,
                request_bytes: request_size,
                response_bytes: bytes,
                max_output_tokens: request
                    .max_output_tokens
                    .unwrap_or(self.config.capabilities.max_output_tokens),
                parallel_tool_calls: Some(self.config.capabilities.supports_parallel_tool_calls),
                http_status: 200,
            });
        }
        if !response.content.is_empty() {
            observer.on_text_delta(&response.content);
        }
        Ok(response)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use coomi_engine::{ModelRequest, ReasoningEffort, Role, ToolSpec};
    use tempfile::tempdir;

    fn provider_config() -> ProviderConfig {
        ProviderConfig {
            id: "OPENCODE".into(),
            kind: crate::config::ProviderKind::OpenAiCompatible,
            display: "OpenCode".into(),
            api_key: "secret".into(),
            base_url: "https://example.invalid/v1".into(),
            model: "deepseek-v4-flash".into(),
            fast_model: None,
            capabilities: coomi_engine::ModelCapabilities::default(),
            supports_reasoning_effort: None,
            reasoning_prompt_fallback: None,
            reasoning_effort_map: Default::default(),
            reasoning_profiles: Default::default(),
            remote_compaction_mode: crate::config::RemoteCompactionMode::Legacy,
        }
    }

    fn fixture(dir: &Path) -> PathBuf {
        let path = dir.join("replay.json");
        fs::write(
            &path,
            r#"{
                "schemaVersion": 1,
                "contractId": "agent.chat.stream.v1",
                "providerId": "OPENCODE",
                "model": "deepseek-v4-flash",
                "steps": [
                    {"expect": {"messageContains": "prompt-marker", "toolNames": ["read_file"]}, "response": {"toolCalls": [{"id": "call-1", "name": "read_file", "arguments": {"path": "fixture.md"}}]}},
                    {"expect": {"messageContains": "file-marker"}, "response": {"content": "file-marker"}}
                ]
            }"#,
        )
        .expect("fixture");
        path
    }

    fn request(content: &str) -> ModelRequest {
        ModelRequest {
            model: "deepseek-v4-flash".into(),
            messages: vec![coomi_engine::ChatMessage {
                role: Role::User,
                content: content.into(),
                tool_calls: Vec::new(),
                tool_call_id: None,
                compaction_summary: false,
                internal: false,
                provider_items: Vec::new(),
                images: Vec::new(),
            }],
            tools: vec![ToolSpec {
                name: "read_file".into(),
                description: "read".into(),
                parameters: Value::Null,
            }],
            max_output_tokens: Some(128),
            required_tool: None,
            reasoning_effort: ReasoningEffort::Low,
        }
    }

    #[tokio::test]
    async fn replay_matches_requests_and_requires_complete_consumption() {
        let directory = tempdir().expect("tempdir");
        let provider = ReplayModelProvider::load(provider_config(), fixture(directory.path()))
            .expect("load replay");
        let first = provider
            .complete(request("prompt-marker"))
            .await
            .expect("first");
        assert_eq!(first.tool_calls[0].name, "read_file");
        let second = provider
            .complete(request("file-marker"))
            .await
            .expect("second");
        assert_eq!(second.content, "file-marker");
        provider.assert_complete().expect("complete fixture");
    }

    #[tokio::test]
    async fn replay_rejects_unexpected_marker_without_advancing() {
        let directory = tempdir().expect("tempdir");
        let provider = ReplayModelProvider::load(provider_config(), fixture(directory.path()))
            .expect("load replay");
        let error = provider
            .complete(request("wrong"))
            .await
            .expect_err("mismatch");
        assert!(error.to_string().contains("message marker"));
        assert!(provider.assert_complete().is_err());
    }
}
