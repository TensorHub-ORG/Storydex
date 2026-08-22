use crate::ProviderConfig;
use crate::ProviderKind;
use crate::ReasoningCapability;
use crate::ReasoningCapabilitySource;
use crate::ReasoningControlMode;
use crate::ReasoningLevelCapability;
use crate::ReasoningRequestPlan;
use crate::ReasoningSupport;
use crate::ReasoningWireField;
use crate::RemoteCompactionMode;
use anyhow::Context;
use anyhow::Result;
use async_trait::async_trait;
use coomi_engine::ChatMessage;
use coomi_engine::CompactionRequest;
use coomi_engine::CompactionResponse;
use coomi_engine::ModelCapabilities;
use coomi_engine::ModelProvider;
use coomi_engine::ModelRequest;
use coomi_engine::ModelResponse;
use coomi_engine::ModelResponseMetadata;
use coomi_engine::ModelStreamObserver;
use coomi_engine::ProviderStreamEvent;
use coomi_engine::ProviderStreamPhase;
use coomi_engine::ReasoningEffort;
use coomi_engine::Role;
use coomi_engine::TokenUsage;
use coomi_engine::ToolCall;
use coomi_engine::retained_user_history;
use futures_util::StreamExt;
use reqwest::Client;
use reqwest::RequestBuilder;
use reqwest::Response;
use reqwest::StatusCode;
use reqwest::header::HeaderMap;
use serde_json::Map;
use serde_json::Value;
use serde_json::json;
use std::collections::BTreeMap;
use std::time::Duration;
use std::time::Instant;

const PROVIDER_REQUEST_ATTEMPTS: usize = 3;
const PROVIDER_RETRY_DELAYS: [Duration; 2] =
    [Duration::from_millis(250), Duration::from_millis(750)];
const PROVIDER_CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
const PROVIDER_READ_TIMEOUT: Duration = Duration::from_secs(180);
const PROVIDER_COMPLETION_GRACE_TIMEOUT: Duration = Duration::from_secs(2);
/// Liveness budget for the HTTP response head. Gateways that accept a request
/// but never send response headers (stalled upstream, overloaded proxy) would
/// otherwise make the user wait out the whole 180s read timeout with no
/// feedback. Failing here routes into the existing stream-retry path (with
/// the ProviderRetry notice) instead.
const PROVIDER_RESPONSE_HEAD_TIMEOUT: Duration = Duration::from_secs(45);
/// Liveness budget for the first byte of a streaming response. Gateways that
/// stall before emitting anything are retried instead of making the user wait
/// for `PROVIDER_READ_TIMEOUT`.
const PROVIDER_FIRST_BYTE_TIMEOUT: Duration = Duration::from_secs(45);
/// Liveness budget between consecutive body chunks after the first byte. A
/// stream that stalls mid-body for this long is treated as dead and (if no
/// output was produced yet) retried, instead of waiting out the 180s
/// `PROVIDER_READ_TIMEOUT`.
const PROVIDER_STREAM_STALL_TIMEOUT: Duration = Duration::from_secs(60);
/// Total attempts for the response-body streaming phase. The HTTP request
/// itself already retries via `send_with_retry`; this covers truncated/stalled
/// streams that fail *after* the response headers arrive.
const PROVIDER_STREAM_ATTEMPTS: usize = 3;
const PROVIDER_STREAM_RETRY_DELAYS: [Duration; 2] =
    [Duration::from_millis(500), Duration::from_millis(1500)];
/// Default output-token budget sent when the caller does not specify one.
/// Gateways use their own (often smaller) default when max_tokens is omitted,
/// which truncates long tool-argument streams (e.g. a 3000-character chapter
/// write) mid-JSON. 8192 matches the anthropic path and engine defaults.
const PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS: u64 = 16_384;
const PROVIDER_MAX_RETRY_OUTPUT_TOKENS: u64 = 65_536;

fn provider_retry_delay(attempt: usize) -> Duration {
    PROVIDER_RETRY_DELAYS
        .get(attempt)
        .copied()
        .unwrap_or(Duration::ZERO)
}

fn provider_response_retry_delay(headers: &HeaderMap, attempt: usize) -> Duration {
    let server_delay = headers
        .get("retry-after-ms")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.trim().parse::<u64>().ok())
        .or_else(|| {
            headers
                .get("retry-after")
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.trim().parse::<u64>().ok())
                .map(|seconds| seconds.saturating_mul(1000))
        });
    server_delay.map_or_else(
        || provider_retry_delay(attempt),
        |milliseconds| Duration::from_millis(milliseconds.clamp(250, 30_000)),
    )
}

fn provider_stream_retry_delay(attempt: usize) -> Duration {
    PROVIDER_STREAM_RETRY_DELAYS
        .get(attempt)
        .copied()
        .unwrap_or(Duration::ZERO)
}

fn serialized_bytes(value: &Value) -> u64 {
    serde_json::to_vec(value)
        .map(|bytes| u64::try_from(bytes.len()).unwrap_or(u64::MAX))
        .unwrap_or(0)
}

fn elapsed_ms(started: Instant) -> u64 {
    u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX)
}

fn apply_parallel_tool_calls(body: &mut Value, enabled: bool, has_tools: bool) {
    if enabled && has_tools {
        body["parallel_tool_calls"] = Value::Bool(true);
    }
}

pub struct HttpModelProvider {
    config: ProviderConfig,
    client: Client,
}

impl HttpModelProvider {
    pub fn new(config: ProviderConfig) -> Result<Self> {
        let client = Client::builder()
            .connect_timeout(PROVIDER_CONNECT_TIMEOUT)
            .read_timeout(PROVIDER_READ_TIMEOUT)
            .build()
            .context("failed to build provider HTTP client")?;
        Ok(Self { config, client })
    }

    async fn send_with_retry(&self, builder: RequestBuilder) -> Result<Response> {
        let Some(template) = builder.try_clone() else {
            return Ok(builder.send().await?);
        };
        let mut current = builder;
        for attempt in 0..PROVIDER_REQUEST_ATTEMPTS {
            match current.send().await {
                Ok(response)
                    if retryable_provider_status(response.status())
                        && attempt + 1 < PROVIDER_REQUEST_ATTEMPTS =>
                {
                    let delay = provider_response_retry_delay(response.headers(), attempt);
                    tokio::time::sleep(delay).await;
                    current = template
                        .try_clone()
                        .context("failed to clone provider request for retry")?;
                }
                Ok(response) => return Ok(response),
                Err(error)
                    if (error.is_connect() || error.is_timeout())
                        && attempt + 1 < PROVIDER_REQUEST_ATTEMPTS =>
                {
                    tokio::time::sleep(provider_retry_delay(attempt)).await;
                    current = template
                        .try_clone()
                        .context("failed to clone provider request for retry")?;
                }
                Err(error) => return Err(error.into()),
            }
        }
        unreachable!("provider request retry loop always returns")
    }

    async fn send_openai_chat(
        &self,
        endpoint: &str,
        body: &Value,
        required_tool: Option<&str>,
    ) -> Result<(Response, Value)> {
        let mut request_body = body.clone();
        let mut can_relax_tool_choice = required_tool.is_some();
        let mut can_remove_reasoning = true;
        let mut can_add_message_ids = true;

        loop {
            let response = self
                .send_with_retry(
                    self.authenticated(self.client.post(endpoint))
                        .json(&request_body),
                )
                .await?;
            if !matches!(
                response.status(),
                StatusCode::BAD_REQUEST | StatusCode::UNPROCESSABLE_ENTITY
            ) {
                return Ok((response, request_body));
            }

            let status = response.status();
            let response_body = response
                .text()
                .await
                .context("failed to read provider response")?;

            if can_remove_reasoning && has_reasoning_rejection_error(&response_body) {
                can_remove_reasoning = false;
                let fallback =
                    without_reasoning_fields(&request_body, ReasoningWireProtocol::OpenAiChat);
                if fallback != request_body {
                    request_body = fallback;
                    continue;
                }
            }

            if can_add_message_ids && has_missing_openai_message_id_error(&response_body) {
                can_add_message_ids = false;
                if let Some(fallback) = openai_message_id_fallback(&request_body) {
                    request_body = fallback;
                    continue;
                }
            }

            if can_relax_tool_choice && required_tool_choice_unsupported(status) {
                can_relax_tool_choice = false;
                // Several OpenAI-compatible gateways support tools but reject named or
                // `required` tool_choice values. Retrying with `auto` preserves tool use
                // instead of failing the entire Storydex structured-output turn.
                request_body["tool_choice"] = Value::String("auto".into());
                continue;
            }

            return Err(provider_http_error(status, &response_body));
        }
    }

    async fn retry_reasoning_rejection(
        &self,
        response: Response,
        fallback_request: RequestBuilder,
    ) -> Result<(Response, bool)> {
        if !matches!(
            response.status(),
            StatusCode::BAD_REQUEST | StatusCode::UNPROCESSABLE_ENTITY
        ) {
            return Ok((response, false));
        }
        let status = response.status();
        let response_body = response
            .text()
            .await
            .context("failed to read provider response")?;
        if !has_reasoning_rejection_error(&response_body) {
            return Err(provider_http_error(status, &response_body));
        }
        Ok((self.send_with_retry(fallback_request).await?, true))
    }

    async fn openai_compatible(&self, request: ModelRequest) -> Result<ModelResponse> {
        let endpoint = endpoint(&self.config.base_url, "chat/completions");
        let required_tool = request.required_tool.clone();
        let mut body = json!({
            "model": request.model,
            "messages": openai_messages(&request.messages)?,
            "stream": false
        });
        if !request.tools.is_empty() {
            body["tools"] = Value::Array(
                request
                    .tools
                    .iter()
                    .map(|tool| {
                        json!({
                            "type": "function",
                            "function": {
                                "name": tool.name,
                                "description": tool.description,
                                "parameters": tool.parameters
                            }
                        })
                    })
                    .collect(),
            );
            body["tool_choice"] = openai_chat_tool_choice(request.required_tool.as_deref());
        }
        apply_parallel_tool_calls(
            &mut body,
            self.config.capabilities.supports_parallel_tool_calls,
            !request.tools.is_empty(),
        );
        body["max_tokens"] = Value::from(
            request
                .max_output_tokens
                .unwrap_or(PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS),
        );
        apply_reasoning_effort_best_effort(
            &mut body,
            &self.config,
            &request.model,
            request.reasoning_effort,
            request.max_output_tokens,
        )?;
        let (response, _) = self
            .send_openai_chat(&endpoint, &body, required_tool.as_deref())
            .await?;
        let value = checked_json(response).await?;
        let message = value
            .pointer("/choices/0/message")
            .context("provider response has no choices[0].message")?;
        let content = text_content(message.get("content"));
        let tool_calls = parse_openai_tool_calls(message.get("tool_calls"))?;
        let usage = openai_usage(value.get("usage"));
        Ok(ModelResponse {
            content,
            tool_calls,
            metadata: ModelResponseMetadata {
                response_model: optional_string(value.get("model")),
                finish_reason: optional_string(value.pointer("/choices/0/finish_reason")),
                response_status: None,
                native_reasoning: openai_message_has_native_reasoning(message),
            },
            usage,
            streamed: false,
            provider_items: vec![message.clone()],
        })
    }

    async fn openai_compatible_stream(
        &self,
        request: ModelRequest,
        observer: &dyn ModelStreamObserver,
    ) -> Result<ModelResponse> {
        let endpoint = endpoint(&self.config.base_url, "chat/completions");
        let required_tool = request.required_tool.clone();
        let mut body = json!({
            "model": request.model,
            "messages": openai_messages(&request.messages)?,
            "stream": true,
            "stream_options": {"include_usage": true}
        });
        if !request.tools.is_empty() {
            body["tools"] = Value::Array(
                request
                    .tools
                    .iter()
                    .map(|tool| {
                        json!({
                            "type": "function",
                            "function": {
                                "name": tool.name,
                                "description": tool.description,
                                "parameters": tool.parameters
                            }
                        })
                    })
                    .collect(),
            );
            body["tool_choice"] = openai_chat_tool_choice(request.required_tool.as_deref());
        }
        apply_parallel_tool_calls(
            &mut body,
            self.config.capabilities.supports_parallel_tool_calls,
            !request.tools.is_empty(),
        );
        body["max_tokens"] = Value::from(
            request
                .max_output_tokens
                .unwrap_or(PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS),
        );
        apply_reasoning_effort_best_effort(
            &mut body,
            &self.config,
            &request.model,
            request.reasoning_effort,
            request.max_output_tokens,
        )?;
        for attempt in 0..PROVIDER_STREAM_ATTEMPTS {
            let attempt_number = attempt + 1;
            let attempt_started = Instant::now();
            let request_bytes = serialized_bytes(&body);
            let max_output_tokens = body["max_tokens"]
                .as_u64()
                .unwrap_or(PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS);
            let parallel_tool_calls = body.get("parallel_tool_calls").and_then(Value::as_bool);
            ProviderStreamObservation {
                observer,
                attempt: attempt_number,
                started: attempt_started,
                request_bytes,
                max_output_tokens,
                parallel_tool_calls,
                http_status: 0,
            }
            .emit(ProviderStreamPhase::RequestStarted, 0);
            let (response, effective_body) = match tokio::time::timeout(
                PROVIDER_RESPONSE_HEAD_TIMEOUT,
                self.send_openai_chat(&endpoint, &body, required_tool.as_deref()),
            )
            .await
            {
                Ok(Ok(response)) => response,
                Ok(Err(error))
                    if attempt + 1 < PROVIDER_STREAM_ATTEMPTS
                        && is_retryable_send_error(&error) =>
                {
                    observer.on_provider_retry(attempt + 1, PROVIDER_STREAM_ATTEMPTS, 0);
                    tokio::time::sleep(provider_stream_retry_delay(attempt)).await;
                    continue;
                }
                Ok(Err(error)) => return Err(error),
                Err(_) if attempt + 1 < PROVIDER_STREAM_ATTEMPTS => {
                    observer.on_provider_retry(attempt + 1, PROVIDER_STREAM_ATTEMPTS, 0);
                    tokio::time::sleep(provider_stream_retry_delay(attempt)).await;
                    continue;
                }
                Err(_) => {
                    anyhow::bail!(
                        "provider stream failed: no response head within {}s",
                        PROVIDER_RESPONSE_HEAD_TIMEOUT.as_secs()
                    );
                }
            };
            // Reuse the compatibility-adjusted request on stream retries. This
            // avoids renegotiating rejected reasoning fields or message IDs after
            // a later transport-level retry.
            body = effective_body;
            let status = response.status();
            let request_bytes = serialized_bytes(&body);
            let max_output_tokens = body["max_tokens"]
                .as_u64()
                .unwrap_or(PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS);
            let parallel_tool_calls = body.get("parallel_tool_calls").and_then(Value::as_bool);
            let observation = ProviderStreamObservation {
                observer,
                attempt: attempt_number,
                started: attempt_started,
                request_bytes,
                max_output_tokens,
                parallel_tool_calls,
                http_status: status.as_u16(),
            };
            observation.emit(ProviderStreamPhase::ResponseHead, 0);
            if !status.is_success() {
                return checked_json(response)
                    .await
                    .map(|_| ModelResponse::default());
            }
            let mut state = ChatStreamState::default();
            match read_sse_observed(response, observation, |value| {
                state.consume(&value, observer)
            })
            .await
            {
                Ok(())
                    if is_truncated_tool_call_stream(&state)
                        && attempt + 1 < PROVIDER_STREAM_ATTEMPTS =>
                {
                    observer.on_provider_retry(
                        attempt + 1,
                        PROVIDER_STREAM_ATTEMPTS,
                        state.emitted_text_characters(),
                    );
                    grow_output_token_budget(&mut body, "max_tokens");
                    tokio::time::sleep(provider_stream_retry_delay(attempt)).await;
                }
                Ok(())
                    if is_empty_truncated_response(&state)
                        && attempt + 1 < PROVIDER_STREAM_ATTEMPTS =>
                {
                    observer.on_provider_retry(attempt + 1, PROVIDER_STREAM_ATTEMPTS, 0);
                    grow_output_token_budget(&mut body, "max_tokens");
                    tokio::time::sleep(provider_stream_retry_delay(attempt)).await;
                }
                Ok(()) if is_empty_truncated_response(&state) => {
                    anyhow::bail!(
                        "provider output was truncated after exhausting the output-token budget without text or tool calls"
                    );
                }
                Ok(()) => {
                    let reset_text_characters = state.emitted_text_characters();
                    match state.finish() {
                        Ok(response) => return Ok(response),
                        Err(error)
                            if is_retryable_finish_error(&error)
                                && attempt + 1 < PROVIDER_STREAM_ATTEMPTS =>
                        {
                            observer.on_provider_retry(
                                attempt + 1,
                                PROVIDER_STREAM_ATTEMPTS,
                                reset_text_characters,
                            );
                            grow_output_token_budget(&mut body, "max_tokens");
                            tokio::time::sleep(provider_stream_retry_delay(attempt)).await;
                        }
                        Err(error) => return Err(error),
                    }
                }
                Err(error)
                    if is_retryable_stream_error(&error)
                        && attempt + 1 < PROVIDER_STREAM_ATTEMPTS =>
                {
                    observer.on_provider_retry(
                        attempt + 1,
                        PROVIDER_STREAM_ATTEMPTS,
                        state.emitted_text_characters(),
                    );
                    tokio::time::sleep(provider_stream_retry_delay(attempt)).await;
                }
                Err(error) => return Err(error),
            }
        }
        unreachable!("provider stream retry loop always returns")
    }

    async fn openai_remote_compaction(
        &self,
        request: CompactionRequest,
    ) -> Result<CompactionResponse> {
        let endpoint = endpoint(&self.config.base_url, "responses/compact");
        let body = json!({
            "model": request.model,
            "input": responses_input(&request.messages)?,
            "instructions": request.system_prompt
        });
        let value = checked_json(
            self.authenticated(self.client.post(endpoint))
                .json(&body)
                .send()
                .await?,
        )
        .await?;
        let mut messages = Vec::new();
        for item in value
            .get("output")
            .and_then(Value::as_array)
            .context("compact response has no output array")?
        {
            if item.get("type").and_then(Value::as_str) == Some("message") {
                let role = match item.get("role").and_then(Value::as_str) {
                    Some("assistant") => Role::Assistant,
                    Some("system" | "developer") => Role::System,
                    _ => Role::User,
                };
                let content = item
                    .get("content")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(|part| part.get("text").and_then(Value::as_str))
                    .collect::<Vec<_>>()
                    .join("\n");
                if !content.is_empty() {
                    let mut message = match role {
                        Role::Assistant => ChatMessage::assistant(content, Vec::new()),
                        Role::System => ChatMessage::system(content),
                        Role::User | Role::Tool => ChatMessage::user(content),
                    };
                    message.provider_items.push(item.clone());
                    messages.push(message);
                }
            } else if matches!(
                item.get("type").and_then(Value::as_str),
                Some("compaction" | "context_compaction")
            ) {
                if item
                    .get("encrypted_content")
                    .and_then(Value::as_str)
                    .is_some()
                {
                    messages.push(ChatMessage::provider_item(item.clone()));
                } else if let Some(summary) = item
                    .get("summary")
                    .or_else(|| item.get("content"))
                    .and_then(Value::as_str)
                {
                    messages.push(ChatMessage::summary(summary));
                }
            }
        }
        if messages.is_empty() {
            anyhow::bail!("compact response contained no reusable history")
        }
        Ok(CompactionResponse {
            messages,
            usage: responses_usage(value.get("usage")),
        })
    }

    async fn openai_remote_compaction_v2(
        &self,
        request: CompactionRequest,
    ) -> Result<CompactionResponse> {
        let endpoint = endpoint(&self.config.base_url, "responses");
        let body = remote_compaction_v2_body(
            &request,
            self.config.capabilities.supports_web_search,
            self.config.capabilities.supports_parallel_tool_calls,
        )?;
        for attempt in 0..PROVIDER_STREAM_ATTEMPTS {
            let response = match tokio::time::timeout(
                PROVIDER_RESPONSE_HEAD_TIMEOUT,
                self.authenticated(self.client.post(&endpoint))
                    .json(&body)
                    .send(),
            )
            .await
            {
                Ok(Ok(response)) => response,
                Ok(Err(error))
                    if attempt + 1 < PROVIDER_STREAM_ATTEMPTS
                        && is_retryable_send_error(&error) =>
                {
                    tokio::time::sleep(provider_stream_retry_delay(attempt)).await;
                    continue;
                }
                Ok(Err(error)) => return Err(error.into()),
                Err(_) if attempt + 1 < PROVIDER_STREAM_ATTEMPTS => {
                    tokio::time::sleep(provider_stream_retry_delay(attempt)).await;
                    continue;
                }
                Err(_) => {
                    anyhow::bail!(
                        "provider stream failed: no response head within {}s",
                        PROVIDER_RESPONSE_HEAD_TIMEOUT.as_secs()
                    );
                }
            };
            let status = response.status();
            if !status.is_success() {
                return checked_json(response)
                    .await
                    .and_then(|_| anyhow::bail!("remote compaction returned no stream"));
            }
            let mut state = CompactionStreamState::default();
            match read_sse(response, |value| state.consume(&value)).await {
                Ok(()) => {
                    let (item, usage) = state.finish()?;
                    let mut messages = retained_user_history(&request.messages);
                    messages.push(ChatMessage::provider_item(item));
                    return Ok(CompactionResponse { messages, usage });
                }
                Err(error)
                    if is_retryable_stream_error(&error)
                        && attempt + 1 < PROVIDER_STREAM_ATTEMPTS =>
                {
                    tokio::time::sleep(provider_stream_retry_delay(attempt)).await;
                }
                Err(error) => return Err(error),
            }
        }
        unreachable!("remote compaction stream retry loop always returns")
    }

    async fn openai_responses(&self, request: ModelRequest) -> Result<ModelResponse> {
        let endpoint = endpoint(&self.config.base_url, "responses");
        let mut body = json!({
            "model": request.model,
            "input": responses_input(&request.messages)?,
            "stream": false
        });
        let provider_tools =
            openai_responses_tools(&request.tools, self.config.capabilities.supports_web_search);
        if !provider_tools.is_empty() {
            body["tools"] = Value::Array(provider_tools);
            body["tool_choice"] = openai_responses_tool_choice(request.required_tool.as_deref());
        }
        apply_parallel_tool_calls(
            &mut body,
            self.config.capabilities.supports_parallel_tool_calls,
            !request.tools.is_empty(),
        );
        body["max_output_tokens"] = Value::from(
            request
                .max_output_tokens
                .unwrap_or(PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS),
        );
        apply_reasoning_effort_best_effort(
            &mut body,
            &self.config,
            &request.model,
            request.reasoning_effort,
            request.max_output_tokens,
        )?;
        let fallback = without_reasoning_fields(&body, ReasoningWireProtocol::OpenAiResponses);
        let response = self
            .send_with_retry(self.authenticated(self.client.post(&endpoint)).json(&body))
            .await?;
        let response = if fallback == body {
            response
        } else {
            self.retry_reasoning_rejection(
                response,
                self.authenticated(self.client.post(&endpoint))
                    .json(&fallback),
            )
            .await?
            .0
        };
        let value = checked_json(response).await?;
        let mut content = String::new();
        let mut tool_calls = Vec::new();
        let output = value
            .get("output")
            .and_then(Value::as_array)
            .context("responses payload has no output array")?;
        for item in output {
            match item.get("type").and_then(Value::as_str) {
                Some("message") => {
                    for part in item
                        .get("content")
                        .and_then(Value::as_array)
                        .into_iter()
                        .flatten()
                    {
                        if matches!(
                            part.get("type").and_then(Value::as_str),
                            Some("output_text" | "text")
                        ) && let Some(text) = part.get("text").and_then(Value::as_str)
                        {
                            content.push_str(text);
                        }
                    }
                }
                Some("function_call") => {
                    tool_calls.push(parse_function_call_item(item)?);
                }
                _ => {}
            }
        }
        let usage = responses_usage(value.get("usage"));
        Ok(ModelResponse {
            content,
            tool_calls,
            metadata: ModelResponseMetadata {
                response_model: optional_string(value.get("model")),
                finish_reason: optional_string(value.pointer("/incomplete_details/reason")),
                response_status: optional_string(value.get("status")),
                native_reasoning: output_has_native_reasoning(output),
            },
            usage,
            streamed: false,
            provider_items: output.clone(),
        })
    }

    async fn openai_responses_stream(
        &self,
        request: ModelRequest,
        observer: &dyn ModelStreamObserver,
    ) -> Result<ModelResponse> {
        let endpoint = endpoint(&self.config.base_url, "responses");
        let mut body = json!({
            "model": request.model,
            "input": responses_input(&request.messages)?,
            "stream": true
        });
        let provider_tools =
            openai_responses_tools(&request.tools, self.config.capabilities.supports_web_search);
        if !provider_tools.is_empty() {
            body["tools"] = Value::Array(provider_tools);
            body["tool_choice"] = openai_responses_tool_choice(request.required_tool.as_deref());
        }
        apply_parallel_tool_calls(
            &mut body,
            self.config.capabilities.supports_parallel_tool_calls,
            !request.tools.is_empty(),
        );
        body["max_output_tokens"] = Value::from(
            request
                .max_output_tokens
                .unwrap_or(PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS),
        );
        apply_reasoning_effort_best_effort(
            &mut body,
            &self.config,
            &request.model,
            request.reasoning_effort,
            request.max_output_tokens,
        )?;
        for attempt in 0..PROVIDER_STREAM_ATTEMPTS {
            let attempt_number = attempt + 1;
            let attempt_started = Instant::now();
            let request_bytes = serialized_bytes(&body);
            let max_output_tokens = body["max_output_tokens"]
                .as_u64()
                .unwrap_or(PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS);
            let parallel_tool_calls = body.get("parallel_tool_calls").and_then(Value::as_bool);
            ProviderStreamObservation {
                observer,
                attempt: attempt_number,
                started: attempt_started,
                request_bytes,
                max_output_tokens,
                parallel_tool_calls,
                http_status: 0,
            }
            .emit(ProviderStreamPhase::RequestStarted, 0);
            let response = match tokio::time::timeout(
                PROVIDER_RESPONSE_HEAD_TIMEOUT,
                self.authenticated(self.client.post(&endpoint))
                    .json(&body)
                    .send(),
            )
            .await
            {
                Ok(Ok(response)) => response,
                Ok(Err(error))
                    if attempt + 1 < PROVIDER_STREAM_ATTEMPTS
                        && is_retryable_send_error(&error) =>
                {
                    observer.on_provider_retry(attempt + 1, PROVIDER_STREAM_ATTEMPTS, 0);
                    tokio::time::sleep(provider_stream_retry_delay(attempt)).await;
                    continue;
                }
                Ok(Err(error)) => return Err(error.into()),
                Err(_) if attempt + 1 < PROVIDER_STREAM_ATTEMPTS => {
                    observer.on_provider_retry(attempt + 1, PROVIDER_STREAM_ATTEMPTS, 0);
                    tokio::time::sleep(provider_stream_retry_delay(attempt)).await;
                    continue;
                }
                Err(_) => {
                    anyhow::bail!(
                        "provider stream failed: no response head within {}s",
                        PROVIDER_RESPONSE_HEAD_TIMEOUT.as_secs()
                    );
                }
            };
            let fallback = without_reasoning_fields(&body, ReasoningWireProtocol::OpenAiResponses);
            let (response, used_fallback) = if fallback == body {
                (response, false)
            } else {
                self.retry_reasoning_rejection(
                    response,
                    self.authenticated(self.client.post(&endpoint))
                        .json(&fallback),
                )
                .await?
            };
            if used_fallback {
                body = fallback;
            }
            let status = response.status();
            let request_bytes = serialized_bytes(&body);
            let max_output_tokens = body["max_output_tokens"]
                .as_u64()
                .unwrap_or(PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS);
            let parallel_tool_calls = body.get("parallel_tool_calls").and_then(Value::as_bool);
            let observation = ProviderStreamObservation {
                observer,
                attempt: attempt_number,
                started: attempt_started,
                request_bytes,
                max_output_tokens,
                parallel_tool_calls,
                http_status: status.as_u16(),
            };
            observation.emit(ProviderStreamPhase::ResponseHead, 0);
            if !status.is_success() {
                return checked_json(response)
                    .await
                    .map(|_| ModelResponse::default());
            }
            let mut state = ResponsesStreamState::default();
            match read_sse_observed(response, observation, |value| {
                state.consume(&value, observer)
            })
            .await
            {
                Ok(())
                    if is_truncated_tool_call_stream(&state)
                        && attempt + 1 < PROVIDER_STREAM_ATTEMPTS =>
                {
                    observer.on_provider_retry(
                        attempt + 1,
                        PROVIDER_STREAM_ATTEMPTS,
                        state.emitted_text_characters(),
                    );
                    grow_output_token_budget(&mut body, "max_output_tokens");
                    tokio::time::sleep(provider_stream_retry_delay(attempt)).await;
                }
                Ok(())
                    if is_empty_truncated_response(&state)
                        && attempt + 1 < PROVIDER_STREAM_ATTEMPTS =>
                {
                    observer.on_provider_retry(attempt + 1, PROVIDER_STREAM_ATTEMPTS, 0);
                    grow_output_token_budget(&mut body, "max_output_tokens");
                    tokio::time::sleep(provider_stream_retry_delay(attempt)).await;
                }
                Ok(()) if is_empty_truncated_response(&state) => {
                    anyhow::bail!(
                        "provider output was truncated after exhausting the output-token budget without text or tool calls"
                    );
                }
                Ok(()) => {
                    let reset_text_characters = state.emitted_text_characters();
                    match state.finish() {
                        Ok(response) => return Ok(response),
                        Err(error)
                            if is_retryable_finish_error(&error)
                                && attempt + 1 < PROVIDER_STREAM_ATTEMPTS =>
                        {
                            observer.on_provider_retry(
                                attempt + 1,
                                PROVIDER_STREAM_ATTEMPTS,
                                reset_text_characters,
                            );
                            grow_output_token_budget(&mut body, "max_output_tokens");
                            tokio::time::sleep(provider_stream_retry_delay(attempt)).await;
                        }
                        Err(error) => return Err(error),
                    }
                }
                Err(error)
                    if is_retryable_stream_error(&error)
                        && attempt + 1 < PROVIDER_STREAM_ATTEMPTS =>
                {
                    observer.on_provider_retry(
                        attempt + 1,
                        PROVIDER_STREAM_ATTEMPTS,
                        state.emitted_text_characters(),
                    );
                    tokio::time::sleep(provider_stream_retry_delay(attempt)).await;
                }
                Err(error) => return Err(error),
            }
        }
        unreachable!("provider stream retry loop always returns")
    }

    async fn anthropic_messages(&self, request: ModelRequest) -> Result<ModelResponse> {
        let endpoint = endpoint(&self.config.base_url, "messages");
        let (system, messages) = anthropic_messages(&request.messages)?;
        let mut body = json!({
            "model": request.model,
            "max_tokens": request.max_output_tokens.unwrap_or(8192),
            "messages": messages,
            "stream": false
        });
        if !system.is_empty() {
            body["system"] = Value::String(system);
        }
        let mut provider_tools = request
            .tools
            .iter()
            .filter(|tool| {
                !(self.config.capabilities.supports_web_search && tool.name == "web_search")
            })
            .map(|tool| {
                json!({
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters
                })
            })
            .collect::<Vec<_>>();
        if self.config.capabilities.supports_web_search {
            provider_tools.push(json!({
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 5
            }));
        }
        if !provider_tools.is_empty() {
            body["tools"] = Value::Array(provider_tools);
            if let Some(name) = request.required_tool.as_deref() {
                body["tool_choice"] = json!({"type": "tool", "name": name});
            }
        }
        apply_reasoning_effort_best_effort(
            &mut body,
            &self.config,
            &request.model,
            request.reasoning_effort,
            request.max_output_tokens,
        )?;
        let uses_interleaved_thinking =
            anthropic_interleaved_thinking_beta_enabled(&body, !request.tools.is_empty());
        let mut builder = self
            .client
            .post(&endpoint)
            .header("anthropic-version", "2023-06-01")
            .header("content-type", "application/json");
        if uses_interleaved_thinking {
            builder = builder.header("anthropic-beta", "interleaved-thinking-2025-05-14");
        }
        if !self.config.api_key.is_empty() {
            builder = builder.header("x-api-key", &self.config.api_key);
        }
        let fallback = without_reasoning_fields(&body, ReasoningWireProtocol::Anthropic);
        let mut fallback_builder = self
            .client
            .post(&endpoint)
            .header("anthropic-version", "2023-06-01")
            .header("content-type", "application/json");
        if !self.config.api_key.is_empty() {
            fallback_builder = fallback_builder.header("x-api-key", &self.config.api_key);
        }
        let response = self.send_with_retry(builder.json(&body)).await?;
        let response = if fallback == body {
            response
        } else {
            self.retry_reasoning_rejection(response, fallback_builder.json(&fallback))
                .await?
                .0
        };
        let value = checked_json(response).await?;
        let mut content = String::new();
        let mut tool_calls = Vec::new();
        let blocks = value
            .get("content")
            .and_then(Value::as_array)
            .context("anthropic response has no content array")?;
        for block in blocks {
            match block.get("type").and_then(Value::as_str) {
                Some("text") => {
                    if let Some(text) = block.get("text").and_then(Value::as_str) {
                        content.push_str(text);
                    }
                }
                Some("tool_use") => tool_calls.push(ToolCall {
                    id: required_string(block, "id")?.to_string(),
                    name: required_string(block, "name")?.to_string(),
                    arguments: block.get("input").cloned().unwrap_or_else(|| json!({})),
                }),
                _ => {}
            }
        }
        let usage = value.get("usage");
        Ok(ModelResponse {
            content,
            tool_calls,
            usage: TokenUsage {
                input_tokens: nested_u64(usage, "input_tokens"),
                cached_input_tokens: nested_u64(usage, "cache_read_input_tokens"),
                output_tokens: nested_u64(usage, "output_tokens"),
                reasoning_tokens: None,
            },
            metadata: ModelResponseMetadata {
                response_model: optional_string(value.get("model")),
                finish_reason: optional_string(value.get("stop_reason")),
                response_status: None,
                native_reasoning: blocks.iter().any(anthropic_block_is_native_reasoning),
            },
            streamed: false,
            provider_items: blocks.clone(),
        })
    }

    async fn gemini_native(&self, request: ModelRequest) -> Result<ModelResponse> {
        let base = self.config.base_url.trim_end_matches('/');
        let endpoint = if base.ends_with(":generateContent") {
            base.to_string()
        } else {
            format!("{base}/models/{}:generateContent", request.model)
        };
        let (system, contents) = gemini_messages(&request.messages)?;
        let mut body = json!({"contents": contents});
        if !system.is_empty() {
            body["systemInstruction"] = json!({"parts": [{"text": system}]});
        }
        let function_declarations = request
            .tools
            .iter()
            .filter(|tool| {
                !(self.config.capabilities.supports_web_search && tool.name == "web_search")
            })
            .map(|tool| {
                json!({
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters
                })
            })
            .collect::<Vec<_>>();
        let mut provider_tools = Vec::new();
        if !function_declarations.is_empty() {
            provider_tools.push(json!({"functionDeclarations": function_declarations}));
        }
        if self.config.capabilities.supports_web_search {
            provider_tools.push(json!({"google_search": {}}));
        }
        if !provider_tools.is_empty() {
            body["tools"] = Value::Array(provider_tools);
            if let Some(name) = request.required_tool.as_deref() {
                body["toolConfig"] = json!({
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": [name]
                    }
                });
            }
        }
        body["generationConfig"] = json!({
            "maxOutputTokens": request
                .max_output_tokens
                .unwrap_or(PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS)
        });
        apply_reasoning_effort_best_effort(
            &mut body,
            &self.config,
            &request.model,
            request.reasoning_effort,
            request.max_output_tokens,
        )?;
        let mut builder = self
            .client
            .post(&endpoint)
            .header("content-type", "application/json");
        if !self.config.api_key.is_empty() {
            builder = builder.header("x-goog-api-key", &self.config.api_key);
        }
        let fallback = without_reasoning_fields(&body, ReasoningWireProtocol::Gemini);
        let mut fallback_builder = self
            .client
            .post(&endpoint)
            .header("content-type", "application/json");
        if !self.config.api_key.is_empty() {
            fallback_builder = fallback_builder.header("x-goog-api-key", &self.config.api_key);
        }
        let response = self.send_with_retry(builder.json(&body)).await?;
        let response = if fallback == body {
            response
        } else {
            self.retry_reasoning_rejection(response, fallback_builder.json(&fallback))
                .await?
                .0
        };
        let value = checked_json(response).await?;
        let parts = value
            .pointer("/candidates/0/content/parts")
            .and_then(Value::as_array)
            .context("gemini response has no candidate content")?;
        let mut content = String::new();
        let mut tool_calls = Vec::new();
        for (index, part) in parts.iter().enumerate() {
            if let Some(text) = part.get("text").and_then(Value::as_str) {
                content.push_str(text);
            }
            if let Some(call) = part.get("functionCall") {
                tool_calls.push(ToolCall {
                    id: format!("gemini-call-{index}"),
                    name: required_string(call, "name")?.to_string(),
                    arguments: call.get("args").cloned().unwrap_or_else(|| json!({})),
                });
            }
        }
        let usage = value.get("usageMetadata");
        Ok(ModelResponse {
            content,
            tool_calls,
            usage: TokenUsage {
                input_tokens: nested_u64(usage, "promptTokenCount"),
                cached_input_tokens: nested_u64(usage, "cachedContentTokenCount"),
                output_tokens: nested_u64(usage, "candidatesTokenCount"),
                reasoning_tokens: usage
                    .and_then(|usage| usage.get("thoughtsTokenCount"))
                    .and_then(Value::as_u64),
            },
            metadata: ModelResponseMetadata {
                response_model: optional_string(
                    value
                        .get("modelVersion")
                        .or_else(|| value.get("responseModelVersion")),
                ),
                finish_reason: optional_string(value.pointer("/candidates/0/finishReason")),
                response_status: None,
                native_reasoning: parts.iter().any(gemini_part_is_native_reasoning),
            },
            streamed: false,
            provider_items: parts.clone(),
        })
    }

    fn prepare_reasoning_request(&self, mut request: ModelRequest) -> Result<ModelRequest> {
        let plan = reasoning_request_plan_best_effort(
            &self.config,
            &request.model,
            request.reasoning_effort,
            request.max_output_tokens,
        );
        if plan.control == ReasoningControlMode::Prompt {
            inject_reasoning_prompt(&mut request.messages, request.reasoning_effort);
        }
        Ok(request)
    }

    async fn complete_prepared(&self, request: ModelRequest) -> Result<ModelResponse> {
        match self.config.kind {
            ProviderKind::OpenAiCompatible => self.openai_compatible(request).await,
            ProviderKind::OpenAiResponses => self.openai_responses(request).await,
            ProviderKind::AnthropicMessages => self.anthropic_messages(request).await,
            ProviderKind::GeminiNative => self.gemini_native(request).await,
        }
    }

    fn authenticated(&self, builder: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        if self.config.api_key.is_empty() {
            builder
        } else {
            builder.bearer_auth(&self.config.api_key)
        }
    }
}

#[cfg_attr(not(test), allow(dead_code))]
fn apply_reasoning_effort(
    body: &mut Value,
    config: &ProviderConfig,
    model: &str,
    effort: ReasoningEffort,
    max_output_tokens: Option<u64>,
) -> Result<()> {
    let plan = reasoning_request_plan(config, model, effort, max_output_tokens)?;
    if !plan.sent {
        return Ok(());
    }

    for field in &plan.wire_fields {
        set_json_path(body, &field.path, field.value.clone())?;
    }
    Ok(())
}

/// Runtime request preparation must never fail solely because a reasoning
/// level is stale, misconfigured, or unsupported by the selected route. The
/// strict helper above remains available for validation and diagnostics.
fn apply_reasoning_effort_best_effort(
    body: &mut Value,
    config: &ProviderConfig,
    model: &str,
    effort: ReasoningEffort,
    max_output_tokens: Option<u64>,
) -> Result<()> {
    let plan = reasoning_request_plan_best_effort(config, model, effort, max_output_tokens);
    if !plan.sent {
        return Ok(());
    }

    // Apply atomically so a future malformed path cannot leave a partially
    // mutated provider request. A path failure degrades to provider defaults.
    let mut candidate = body.clone();
    for field in &plan.wire_fields {
        if set_json_path(&mut candidate, &field.path, field.value.clone()).is_err() {
            return Ok(());
        }
    }
    *body = candidate;
    Ok(())
}

const REASONING_PROMPT_MARKER: &str = "<storydex-reasoning-guidance>";

/// Inject a bounded, non-chain-of-thought instruction when the selected model
/// has no native effort field. The marker makes retries/idempotent preparation
/// safe and keeps this control distinguishable from provider-native fields.
fn inject_reasoning_prompt(messages: &mut Vec<ChatMessage>, effort: ReasoningEffort) {
    if messages.iter().any(|message| {
        message.role == Role::System && message.content.contains(REASONING_PROMPT_MARKER)
    }) {
        return;
    }
    let guidance = match effort {
        ReasoningEffort::Low => {
            "Use a direct, efficient approach. Spend only the reasoning needed to avoid obvious mistakes, then answer concisely."
        }
        ReasoningEffort::Medium => {
            "Balance speed and depth. Check the important constraints and give a concise, well-supported result."
        }
        ReasoningEffort::High => {
            "Work carefully through the constraints, verify important intermediate conclusions, and check the final result before answering."
        }
        ReasoningEffort::XHigh => {
            "Use the most thorough approach available: examine edge cases, cross-check the result, and self-correct before answering."
        }
        ReasoningEffort::Max => {
            "Use the deepest supported approach: exhaustively check constraints, cross-check the result, and self-correct before answering."
        }
        ReasoningEffort::Auto => return,
    };
    messages.insert(
        0,
        ChatMessage::system(format!(
            "{REASONING_PROMPT_MARKER}\n{guidance}\nDo not reveal private chain-of-thought or hidden reasoning text; provide conclusions and concise rationale only.\n</storydex-reasoning-guidance>"
        )),
    );
}

/// Resolve the exact fields that will be sent for one user-selected level.
/// The UI, bridge diagnostics and request builder all consume this plan so a
/// displayed wire value cannot drift from the value put on the wire.
pub fn reasoning_request_plan(
    config: &ProviderConfig,
    model: &str,
    effort: ReasoningEffort,
    max_output_tokens: Option<u64>,
) -> Result<ReasoningRequestPlan> {
    let metadata = resolve_reasoning_metadata(config, model);
    let capability =
        reasoning_capability_from_metadata(config, model, &metadata, max_output_tokens)?;
    if effort == ReasoningEffort::Auto {
        return Ok(ReasoningRequestPlan {
            requested: effort,
            control: ReasoningControlMode::Auto,
            sent: false,
            prompt_applied: false,
            wire_fields: Vec::new(),
            support: capability.support,
            source: capability.source,
            route_sensitive: false,
            fallback_reason: None,
        });
    }
    let Some(level) = capability
        .levels
        .iter()
        .find(|level| level.effort == effort)
    else {
        if capability.support != ReasoningSupport::Supported {
            anyhow::bail!(
                "reasoning effort `{}` cannot be sent for model `{model}`: native reasoning support is {:?} and prompt fallback is disabled",
                effort.as_str(),
                capability.support,
            );
        }
        anyhow::bail!(
            "reasoning effort `{}` is not declared for model `{model}`",
            effort.as_str()
        );
    };
    let control = level.control;
    Ok(ReasoningRequestPlan {
        requested: effort,
        control,
        sent: control == ReasoningControlMode::Native,
        prompt_applied: control == ReasoningControlMode::Prompt,
        wire_fields: level.wire_fields.clone(),
        support: capability.support,
        source: capability.source,
        route_sensitive: level.route_sensitive,
        fallback_reason: None,
    })
}

/// Resolve the selected level for a live request without letting capability
/// metadata failures interrupt the model call. Invalid native controls degrade
/// to an explicitly configured prompt fallback or to provider-managed `auto`.
pub fn reasoning_request_plan_best_effort(
    config: &ProviderConfig,
    model: &str,
    effort: ReasoningEffort,
    max_output_tokens: Option<u64>,
) -> ReasoningRequestPlan {
    let metadata = resolve_reasoning_metadata(config, model);
    if effort == ReasoningEffort::Auto {
        return reasoning_fallback_plan(&metadata, effort, None);
    }

    let native_declared =
        metadata.support == ReasoningSupport::Supported && metadata.levels.contains(&effort);
    if native_declared {
        match reasoning_wire_fields(
            config,
            model,
            effort,
            max_output_tokens,
            &metadata.effort_map,
        ) {
            Ok(wire_fields) if !wire_fields.is_empty() => {
                return ReasoningRequestPlan {
                    requested: effort,
                    control: ReasoningControlMode::Native,
                    sent: true,
                    prompt_applied: false,
                    wire_fields,
                    support: metadata.support,
                    source: metadata.source,
                    route_sensitive: metadata
                        .route_sensitive
                        .unwrap_or_else(|| inferred_route_sensitive(config, model, effort)),
                    fallback_reason: None,
                };
            }
            Ok(_) => {
                return reasoning_request_fallback(
                    config,
                    model,
                    effort,
                    &metadata,
                    "native reasoning control produced no request fields".into(),
                );
            }
            Err(error) => {
                return reasoning_request_fallback(
                    config,
                    model,
                    effort,
                    &metadata,
                    error.to_string(),
                );
            }
        }
    }

    let reason = if metadata.support != ReasoningSupport::Supported {
        format!("native reasoning support is {:?}", metadata.support)
    } else {
        "the selected level is not declared for this model and route".into()
    };
    reasoning_request_fallback(config, model, effort, &metadata, reason)
}

fn reasoning_request_fallback(
    config: &ProviderConfig,
    model: &str,
    effort: ReasoningEffort,
    metadata: &ReasoningMetadata,
    reason: String,
) -> ReasoningRequestPlan {
    let fallback_reason = Some(format!(
        "reasoning effort `{}` was not applied natively for model `{model}`: {reason}",
        effort.as_str()
    ));
    let prompt_declared = metadata.prompt_fallback
        && default_reasoning_levels_for(config, model, &metadata.effort_map).contains(&effort);
    if prompt_declared {
        return ReasoningRequestPlan {
            requested: effort,
            control: ReasoningControlMode::Prompt,
            sent: false,
            prompt_applied: true,
            wire_fields: Vec::new(),
            support: metadata.support,
            source: metadata.source,
            route_sensitive: false,
            fallback_reason,
        };
    }
    reasoning_fallback_plan(metadata, effort, fallback_reason)
}

fn reasoning_fallback_plan(
    metadata: &ReasoningMetadata,
    requested: ReasoningEffort,
    fallback_reason: Option<String>,
) -> ReasoningRequestPlan {
    ReasoningRequestPlan {
        requested,
        control: ReasoningControlMode::Auto,
        sent: false,
        prompt_applied: false,
        wire_fields: Vec::new(),
        support: metadata.support,
        source: metadata.source,
        route_sensitive: false,
        fallback_reason,
    }
}

/// Describe all explicitly selectable levels for a concrete provider/model.
pub fn reasoning_capability(config: &ProviderConfig, model: &str) -> Result<ReasoningCapability> {
    let metadata = resolve_reasoning_metadata(config, model);
    reasoning_capability_from_metadata(
        config,
        model,
        &metadata,
        Some(config.capabilities.max_output_tokens),
    )
}

/// Build a capability snapshot for UI/status surfaces while isolating malformed
/// levels. Valid levels remain selectable; invalid levels are omitted and the
/// diagnostic is retained in `fallbackReason` instead of failing all models.
pub fn reasoning_capability_best_effort(
    config: &ProviderConfig,
    model: &str,
) -> ReasoningCapability {
    let metadata = resolve_reasoning_metadata(config, model);
    let mut levels = Vec::new();
    let mut failures = Vec::new();
    if metadata.support == ReasoningSupport::Supported {
        for effort in metadata
            .levels
            .iter()
            .copied()
            .filter(|effort| *effort != ReasoningEffort::Auto)
        {
            match reasoning_wire_fields(
                config,
                model,
                effort,
                Some(config.capabilities.max_output_tokens),
                &metadata.effort_map,
            ) {
                Ok(wire_fields) if !wire_fields.is_empty() => {
                    let route_sensitive = metadata
                        .route_sensitive
                        .unwrap_or_else(|| inferred_route_sensitive(config, model, effort));
                    levels.push(ReasoningLevelCapability {
                        effort,
                        wire_fields,
                        control: ReasoningControlMode::Native,
                        route_sensitive,
                    });
                }
                Ok(_) => failures.push(format!(
                    "{}: native reasoning control produced no request fields",
                    effort.as_str()
                )),
                Err(error) => failures.push(format!("{}: {error}", effort.as_str())),
            }
        }
    }
    append_prompt_fallback_levels(config, model, &metadata, &mut levels);
    ReasoningCapability {
        support: metadata.support,
        prompt_fallback: metadata.prompt_fallback,
        route_sensitive: levels.iter().any(|level| level.route_sensitive),
        levels,
        source: metadata.source,
        fallback_reason: (!failures.is_empty()).then(|| failures.join("; ")),
    }
}

#[derive(Clone, Debug)]
struct ReasoningMetadata {
    support: ReasoningSupport,
    source: ReasoningCapabilitySource,
    levels: Vec<ReasoningEffort>,
    effort_map: BTreeMap<String, String>,
    route_sensitive: Option<bool>,
    prompt_fallback: bool,
}

fn resolve_reasoning_metadata(config: &ProviderConfig, model: &str) -> ReasoningMetadata {
    let profile = config
        .reasoning_profiles
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case(model))
        .map(|(_, profile)| profile)
        .or_else(|| config.reasoning_profiles.get("*"));
    if let Some(profile) = profile {
        let support = profile.supported.map_or_else(
            || {
                if profile
                    .levels
                    .as_ref()
                    .is_some_and(|levels| !levels.is_empty())
                    || !profile.effort_map.is_empty()
                {
                    ReasoningSupport::Supported
                } else {
                    ReasoningSupport::Unknown
                }
            },
            |enabled| {
                if enabled {
                    ReasoningSupport::Supported
                } else {
                    ReasoningSupport::Unsupported
                }
            },
        );
        let mut effort_map = config.reasoning_effort_map.clone();
        effort_map.extend(profile.effort_map.clone());
        let levels = profile
            .levels
            .clone()
            .unwrap_or_else(|| default_reasoning_levels_for(config, model, &effort_map));
        let prompt_fallback = profile
            .prompt_fallback
            .or(config.reasoning_prompt_fallback)
            .unwrap_or(false);
        return ReasoningMetadata {
            support,
            source: ReasoningCapabilitySource::ModelConfig,
            levels,
            effort_map,
            route_sensitive: profile.route_sensitive,
            prompt_fallback,
        };
    }

    if let Some(enabled) = config.supports_reasoning_effort {
        return ReasoningMetadata {
            support: if enabled {
                ReasoningSupport::Supported
            } else {
                ReasoningSupport::Unsupported
            },
            source: ReasoningCapabilitySource::ProviderConfig,
            levels: default_reasoning_levels_for(config, model, &config.reasoning_effort_map),
            effort_map: config.reasoning_effort_map.clone(),
            route_sensitive: None,
            prompt_fallback: config.reasoning_prompt_fallback.unwrap_or(false),
        };
    }
    if !config.reasoning_effort_map.is_empty() {
        return ReasoningMetadata {
            support: ReasoningSupport::Supported,
            source: ReasoningCapabilitySource::ProviderConfig,
            levels: default_reasoning_levels_for(config, model, &config.reasoning_effort_map),
            effort_map: config.reasoning_effort_map.clone(),
            route_sensitive: None,
            prompt_fallback: config.reasoning_prompt_fallback.unwrap_or(false),
        };
    }

    let inferred = infer_reasoning_support(config, model);
    ReasoningMetadata {
        support: inferred,
        source: if inferred == ReasoningSupport::Supported {
            ReasoningCapabilitySource::ModelRule
        } else {
            ReasoningCapabilitySource::Unknown
        },
        levels: default_reasoning_levels_for(config, model, &BTreeMap::new()),
        effort_map: BTreeMap::new(),
        route_sensitive: None,
        prompt_fallback: config.reasoning_prompt_fallback.unwrap_or(false),
    }
}

fn default_reasoning_levels() -> Vec<ReasoningEffort> {
    vec![
        ReasoningEffort::Low,
        ReasoningEffort::Medium,
        ReasoningEffort::High,
    ]
}

fn default_reasoning_levels_for(
    config: &ProviderConfig,
    model: &str,
    effort_map: &BTreeMap<String, String>,
) -> Vec<ReasoningEffort> {
    let inferred = inferred_reasoning_levels(config, model);
    // Keep presentation order stable. An explicit map key also declares that
    // logical level, while model rules supply conservative built-in defaults.
    // Exact `reasoning_profiles[].levels` remain authoritative above this path.
    [
        ReasoningEffort::Low,
        ReasoningEffort::Medium,
        ReasoningEffort::High,
        ReasoningEffort::XHigh,
        ReasoningEffort::Max,
    ]
    .into_iter()
    .filter(|effort| inferred.contains(effort) || effort_map.contains_key(effort.as_str()))
    .collect()
}

/// Built-in model catalog for effort levels whose names are not portable
/// across providers. Unknown models intentionally get only the common
/// low/medium/high subset; xhigh and max require a known rule or explicit map.
fn inferred_reasoning_levels(config: &ProviderConfig, model: &str) -> Vec<ReasoningEffort> {
    let normalized = model.to_ascii_lowercase();
    let gpt_family = matches!(
        config.kind,
        ProviderKind::OpenAiCompatible | ProviderKind::OpenAiResponses
    ) && normalized.contains("gpt-5");
    if gpt_family {
        return vec![
            ReasoningEffort::Low,
            ReasoningEffort::Medium,
            ReasoningEffort::High,
            ReasoningEffort::XHigh,
            ReasoningEffort::Max,
        ];
    }

    // Claude effort support is model-version specific. In particular, 4.6
    // exposes max but not xhigh, while newer Opus/Sonnet/Fable models expose
    // both. This rule is also useful for Claude models routed by OpenRouter or
    // an explicitly enabled compatible gateway.
    if let Some(levels) = anthropic_effort_levels(&normalized) {
        return levels;
    }

    if matches!(
        config.kind,
        ProviderKind::OpenAiCompatible | ProviderKind::OpenAiResponses
    ) && is_deepseek_v4_model(&normalized)
    {
        return if is_openrouter(config) {
            // OpenRouter currently exposes xhigh for this route but not max.
            vec![ReasoningEffort::High, ReasoningEffort::XHigh]
        } else if is_opencode(config) {
            // Live OpenCode observations accept low/high/max. The max value is
            // route-sensitive and may select a different upstream tier.
            vec![
                ReasoningEffort::Low,
                ReasoningEffort::High,
                ReasoningEffort::Max,
            ]
        } else if is_official_deepseek(config) {
            vec![ReasoningEffort::High, ReasoningEffort::Max]
        } else {
            // Unknown relays have been observed exposing xhigh instead of max.
            // Do not claim max until that concrete route declares it.
            vec![
                ReasoningEffort::Low,
                ReasoningEffort::High,
                ReasoningEffort::XHigh,
            ]
        };
    }

    if is_kimi_k3_model(&normalized) {
        return match config.kind {
            ProviderKind::AnthropicMessages => vec![
                ReasoningEffort::Low,
                ReasoningEffort::High,
                ReasoningEffort::Max,
            ],
            ProviderKind::OpenAiCompatible | ProviderKind::OpenAiResponses
                if is_opencode(config) =>
            {
                vec![ReasoningEffort::Max]
            }
            ProviderKind::OpenAiCompatible | ProviderKind::OpenAiResponses
                if is_official_kimi(config) =>
            {
                vec![
                    ReasoningEffort::Low,
                    ReasoningEffort::High,
                    ReasoningEffort::Max,
                ]
            }
            ProviderKind::OpenAiCompatible | ProviderKind::OpenAiResponses => {
                default_reasoning_levels()
            }
            ProviderKind::GeminiNative => default_reasoning_levels(),
        };
    }

    default_reasoning_levels()
}

fn reasoning_capability_from_metadata(
    config: &ProviderConfig,
    model: &str,
    metadata: &ReasoningMetadata,
    max_output_tokens: Option<u64>,
) -> Result<ReasoningCapability> {
    let mut levels = Vec::new();
    if metadata.support == ReasoningSupport::Supported {
        for effort in metadata
            .levels
            .iter()
            .copied()
            .filter(|effort| *effort != ReasoningEffort::Auto)
        {
            let wire_fields = reasoning_wire_fields(
                config,
                model,
                effort,
                max_output_tokens,
                &metadata.effort_map,
            )?;
            let route_sensitive = metadata
                .route_sensitive
                .unwrap_or_else(|| inferred_route_sensitive(config, model, effort));
            levels.push(ReasoningLevelCapability {
                effort,
                wire_fields,
                control: ReasoningControlMode::Native,
                route_sensitive,
            });
        }
    }
    append_prompt_fallback_levels(config, model, metadata, &mut levels);
    Ok(ReasoningCapability {
        support: metadata.support,
        prompt_fallback: metadata.prompt_fallback,
        route_sensitive: levels.iter().any(|level| level.route_sensitive),
        levels,
        source: metadata.source,
        fallback_reason: None,
    })
}

fn append_prompt_fallback_levels(
    config: &ProviderConfig,
    model: &str,
    metadata: &ReasoningMetadata,
    levels: &mut Vec<ReasoningLevelCapability>,
) {
    if !metadata.prompt_fallback {
        return;
    }
    // Fill gaps with a soft prompt control. Native declarations always win for
    // the same logical level, so explicitly configured fallback remains visible
    // without being misreported as provider-native support.
    for effort in default_reasoning_levels_for(config, model, &metadata.effort_map) {
        if !levels.iter().any(|level| level.effort == effort) {
            levels.push(ReasoningLevelCapability {
                effort,
                wire_fields: Vec::new(),
                control: ReasoningControlMode::Prompt,
                route_sensitive: false,
            });
        }
    }
}

fn infer_reasoning_support(config: &ProviderConfig, model: &str) -> ReasoningSupport {
    let normalized = model.to_ascii_lowercase();
    match config.kind {
        ProviderKind::OpenAiCompatible | ProviderKind::OpenAiResponses => {
            let model_id = normalized.rsplit('/').next().unwrap_or(&normalized);
            let openai_family = normalized.contains("gpt-5")
                || normalized.contains("gpt-oss")
                || ["o1", "o3", "o4"].iter().any(|family| {
                    model_id == *family
                        || model_id.starts_with(&format!("{family}-"))
                        || model_id.starts_with(&format!("{family}."))
                });
            let native_compatible = is_deepseek_v4_model(&normalized)
                || is_kimi_k3_model(&normalized)
                || normalized.contains("glm-5.2")
                || normalized.contains("glm-5-2")
                || normalized.contains("grok-3-mini");
            let openrouter_family = is_openrouter(config)
                && (anthropic_supports_thinking(&normalized)
                    || model_version_after(&normalized, "gemini")
                        .is_some_and(|version| version >= (2, 5))
                    || normalized.contains("deepseek-r1")
                    || normalized.contains("deepseek-reasoner"));
            if openai_family || native_compatible || openrouter_family {
                ReasoningSupport::Supported
            } else {
                ReasoningSupport::Unknown
            }
        }
        ProviderKind::AnthropicMessages => {
            if anthropic_supports_thinking(&normalized) || is_kimi_k3_model(&normalized) {
                ReasoningSupport::Supported
            } else {
                ReasoningSupport::Unknown
            }
        }
        ProviderKind::GeminiNative => {
            if model_version_after(&normalized, "gemini").is_some_and(|version| version >= (2, 5)) {
                ReasoningSupport::Supported
            } else {
                ReasoningSupport::Unknown
            }
        }
    }
}

fn inferred_route_sensitive(config: &ProviderConfig, model: &str, effort: ReasoningEffort) -> bool {
    if !matches!(effort, ReasoningEffort::XHigh | ReasoningEffort::Max) {
        return false;
    }
    let normalized = model.to_ascii_lowercase();
    if is_opencode(config) {
        return is_deepseek_v4_model(&normalized) || is_kimi_k3_model(&normalized);
    }
    is_deepseek_v4_model(&normalized) && !is_official_deepseek(config) && !is_openrouter(config)
}

fn reasoning_wire_fields(
    config: &ProviderConfig,
    model: &str,
    effort: ReasoningEffort,
    max_output_tokens: Option<u64>,
    effort_map: &BTreeMap<String, String>,
) -> Result<Vec<ReasoningWireField>> {
    let value = reasoning_effort_value_with_map(config, model, effort, effort_map)?;
    let fields = match config.kind {
        ProviderKind::OpenAiCompatible => {
            if is_openrouter(config) {
                vec![("reasoning.effort", Value::String(value))]
            } else {
                vec![("reasoning_effort", Value::String(value))]
            }
        }
        ProviderKind::OpenAiResponses => vec![
            ("reasoning.effort", Value::String(value)),
            ("include", json!(["reasoning.encrypted_content"])),
        ],
        ProviderKind::AnthropicMessages => {
            if anthropic_uses_adaptive_thinking(model) {
                vec![
                    ("thinking.type", Value::String("adaptive".into())),
                    ("output_config.effort", Value::String(value)),
                ]
            } else {
                let max_tokens = max_output_tokens.unwrap_or(8_192);
                let mut fields = vec![
                    ("thinking.type", Value::String("enabled".into())),
                    (
                        "thinking.budget_tokens",
                        Value::from(anthropic_thinking_budget_with_map(
                            config, effort, max_tokens, effort_map,
                        )?),
                    ),
                ];
                if anthropic_uses_legacy_effort(model) {
                    fields.push((
                        "output_config.effort",
                        Value::String(
                            if matches!(effort, ReasoningEffort::XHigh | ReasoningEffort::Max) {
                                "high".into()
                            } else {
                                effort.as_str().into()
                            },
                        ),
                    ));
                }
                fields
            }
        }
        ProviderKind::GeminiNative => {
            let version = model_version_after(model, "gemini");
            if version == Some((2, 5)) {
                vec![(
                    "generationConfig.thinkingConfig.thinkingBudget",
                    Value::from(gemini_thinking_budget_with_map(
                        config, model, effort, effort_map,
                    )?),
                )]
            } else {
                vec![(
                    "generationConfig.thinkingConfig.thinkingLevel",
                    Value::String(value),
                )]
            }
        }
    };
    Ok(fields
        .into_iter()
        .map(|(path, value)| ReasoningWireField {
            path: path.into(),
            value,
        })
        .collect())
}

fn set_json_path(body: &mut Value, path: &str, value: Value) -> Result<()> {
    let mut cursor = body;
    let mut segments = path.split('.').peekable();
    while let Some(segment) = segments.next() {
        if segments.peek().is_none() {
            let object = cursor
                .as_object_mut()
                .context("provider request body must be an object")?;
            object.insert(segment.to_owned(), value);
            return Ok(());
        }
        let object = cursor
            .as_object_mut()
            .context("provider request body must be an object")?;
        cursor = object
            .entry(segment.to_owned())
            .or_insert_with(|| json!({}));
    }
    Ok(())
}

fn reasoning_effort_value_with_map(
    config: &ProviderConfig,
    model: &str,
    effort: ReasoningEffort,
    effort_map: &BTreeMap<String, String>,
) -> Result<String> {
    if let Some(value) = effort_map.get(effort.as_str()) {
        anyhow::ensure!(
            !value.trim().is_empty(),
            "reasoning_effort_map.{} must not be empty",
            effort.as_str()
        );
        return Ok(value.trim().to_owned());
    }

    let model = model.to_ascii_lowercase();
    let value = match config.kind {
        ProviderKind::OpenAiCompatible if is_openrouter(config) => effort.as_str(),
        ProviderKind::OpenAiCompatible | ProviderKind::OpenAiResponses => {
            openai_reasoning_effort(config, &model, effort)
        }
        ProviderKind::AnthropicMessages if effort == ReasoningEffort::Max => "max",
        ProviderKind::AnthropicMessages if effort == ReasoningEffort::XHigh => {
            if anthropic_supports_native_xhigh(&model) {
                "xhigh"
            } else {
                "max"
            }
        }
        ProviderKind::GeminiNative
            if matches!(effort, ReasoningEffort::XHigh | ReasoningEffort::Max) =>
        {
            "high"
        }
        _ => effort.as_str(),
    };
    Ok(value.to_owned())
}

fn anthropic_thinking_budget_with_map(
    _config: &ProviderConfig,
    effort: ReasoningEffort,
    max_tokens: u64,
    effort_map: &BTreeMap<String, String>,
) -> Result<u64> {
    anyhow::ensure!(
        max_tokens > 1_024,
        "Anthropic thinking requires max_tokens greater than 1024"
    );
    let maximum_budget = max_tokens.saturating_sub((max_tokens - 1_024).min(1_024));
    let requested = if let Some(value) = effort_map.get(effort.as_str()) {
        value.trim().parse::<u64>().with_context(|| {
            format!(
                "reasoning_effort_map.{} must be a token count for legacy Anthropic models",
                effort.as_str()
            )
        })?
    } else {
        match effort {
            ReasoningEffort::Auto => return Ok(0),
            ReasoningEffort::Low => 1_024,
            ReasoningEffort::Medium => 2_048,
            ReasoningEffort::High => 4_096,
            ReasoningEffort::XHigh | ReasoningEffort::Max => maximum_budget,
        }
    };
    Ok(requested.clamp(1_024, maximum_budget))
}

fn gemini_thinking_budget_with_map(
    _config: &ProviderConfig,
    model: &str,
    effort: ReasoningEffort,
    effort_map: &BTreeMap<String, String>,
) -> Result<u64> {
    if let Some(value) = effort_map.get(effort.as_str()) {
        return value.trim().parse::<u64>().with_context(|| {
            format!(
                "reasoning_effort_map.{} must be a token count for Gemini 2.5 models",
                effort.as_str()
            )
        });
    }
    let maximum = if model.to_ascii_lowercase().contains("pro")
        && !model.to_ascii_lowercase().contains("flash")
    {
        32_768
    } else {
        24_576
    };
    Ok(match effort {
        ReasoningEffort::Auto => 0,
        ReasoningEffort::Low => 2_048,
        ReasoningEffort::Medium => 8_192,
        ReasoningEffort::High => 16_000,
        ReasoningEffort::XHigh | ReasoningEffort::Max => maximum,
    })
}

fn is_openrouter(config: &ProviderConfig) -> bool {
    base_url_matches_domain(config, "openrouter.ai")
}

fn is_opencode(config: &ProviderConfig) -> bool {
    base_url_matches_domain(config, "opencode.ai")
}

fn is_official_deepseek(config: &ProviderConfig) -> bool {
    base_url_matches_domain(config, "api.deepseek.com")
}

fn is_official_kimi(config: &ProviderConfig) -> bool {
    base_url_matches_domain(config, "api.moonshot.cn")
        || base_url_matches_domain(config, "api.moonshot.ai")
}

fn base_url_matches_domain(config: &ProviderConfig, domain: &str) -> bool {
    reqwest::Url::parse(config.base_url.trim())
        .ok()
        .and_then(|url| url.host_str().map(str::to_ascii_lowercase))
        .is_some_and(|host| host == domain || host.ends_with(&format!(".{domain}")))
}

fn openai_reasoning_effort(
    config: &ProviderConfig,
    model: &str,
    effort: ReasoningEffort,
) -> &'static str {
    if model.contains("deep-research") {
        return "medium";
    }
    if model.contains("gpt-5") && model.contains("-chat") {
        return "medium";
    }
    if model.contains("gpt-5-pro") || model.contains("gpt-5.pro") {
        return "high";
    }
    if effort == ReasoningEffort::Max {
        return "max";
    }
    if effort != ReasoningEffort::XHigh {
        return effort.as_str();
    }
    if is_deepseek_v4_model(model) && is_opencode(config) {
        return "max";
    }
    if is_deepseek_v4_model(model) {
        return "xhigh";
    }
    if model.contains("codex-max")
        || model_version_after(model, "gpt-5").is_some_and(|(version, _)| version >= 2)
    {
        return "xhigh";
    }
    "high"
}

fn anthropic_supports_thinking(model: &str) -> bool {
    anthropic_model_version(model).is_some_and(|version| version >= (3, 7))
        || anthropic_is_latest_alias(model)
        || model.to_ascii_lowercase().contains("mythos-preview")
        || is_kimi_k3_model(model)
}

fn anthropic_uses_adaptive_thinking(model: &str) -> bool {
    anthropic_model_version(model).is_some_and(|version| version >= (4, 6))
        || anthropic_is_latest_alias(model)
        || model.to_ascii_lowercase().contains("mythos-preview")
        || is_kimi_k3_model(model)
}

fn anthropic_supports_native_xhigh(model: &str) -> bool {
    anthropic_effort_levels(model).is_some_and(|levels| levels.contains(&ReasoningEffort::XHigh))
}

fn anthropic_effort_levels(model: &str) -> Option<Vec<ReasoningEffort>> {
    let normalized = model.to_ascii_lowercase();
    let all_levels = || {
        vec![
            ReasoningEffort::Low,
            ReasoningEffort::Medium,
            ReasoningEffort::High,
            ReasoningEffort::XHigh,
            ReasoningEffort::Max,
        ]
    };
    if anthropic_is_latest_alias(&normalized)
        || normalized.contains("mythos-preview")
        || anthropic_model_version(&normalized).is_some_and(|version| version >= (4, 7))
        || model_version_after(&normalized, "opus").is_some_and(|version| version >= (4, 7))
        || model_version_after(&normalized, "sonnet").is_some_and(|version| version >= (5, 0))
        || model_version_after(&normalized, "fable").is_some_and(|version| version >= (5, 0))
        || model_version_after(&normalized, "mythos").is_some_and(|version| version >= (5, 0))
    {
        return Some(all_levels());
    }

    if anthropic_model_version(&normalized).is_some_and(|version| version >= (4, 6))
        || model_version_after(&normalized, "opus").is_some_and(|version| version >= (4, 6))
        || model_version_after(&normalized, "sonnet").is_some_and(|version| version >= (4, 6))
    {
        return Some(vec![
            ReasoningEffort::Low,
            ReasoningEffort::Medium,
            ReasoningEffort::High,
            ReasoningEffort::Max,
        ]);
    }

    None
}

fn is_deepseek_v4_model(model: &str) -> bool {
    let normalized = model.to_ascii_lowercase();
    let model_id = normalized.rsplit('/').next().unwrap_or(&normalized);
    normalized.contains("deepseek-v4")
        || normalized.contains("deepseek_v4")
        || normalized.contains("deepseekv4")
        || model_id.starts_with("v4flash")
        || model_id.starts_with("v4-flash")
}

fn is_kimi_k3_model(model: &str) -> bool {
    let normalized = model.to_ascii_lowercase();
    let model_id = normalized.rsplit('/').next().unwrap_or(&normalized);
    model_id == "k3"
        || model_id.starts_with("k3-")
        || model_id == "kimi-k3"
        || model_id.starts_with("kimi-k3-")
        || model_id == "kimi_k3"
        || model_id.starts_with("kimi_k3_")
}

fn anthropic_uses_legacy_effort(model: &str) -> bool {
    model.to_ascii_lowercase().contains("opus") && anthropic_model_version(model) == Some((4, 5))
}

fn anthropic_interleaved_thinking_beta_enabled(body: &Value, has_tools: bool) -> bool {
    has_tools && body.pointer("/thinking/type").and_then(Value::as_str) == Some("enabled")
}

fn anthropic_is_latest_alias(model: &str) -> bool {
    let normalized = model.to_ascii_lowercase();
    normalized.contains("claude") && normalized.contains("latest")
}

fn anthropic_model_version(model: &str) -> Option<(u64, u64)> {
    ["claude", "opus", "sonnet", "haiku", "fable", "mythos"]
        .into_iter()
        .find_map(|marker| model_version_after(model, marker))
}

fn model_version_after(value: &str, marker: &str) -> Option<(u64, u64)> {
    let normalized = value.to_ascii_lowercase();
    let mut remainder = normalized.split_once(marker)?.1;
    remainder = remainder.trim_start_matches(['-', '.', '_', '/']);
    let major_len = remainder.chars().take_while(char::is_ascii_digit).count();
    if major_len == 0 {
        return None;
    }
    let major = remainder[..major_len].parse().ok()?;
    remainder = &remainder[major_len..];
    remainder = remainder.trim_start_matches(['-', '.', '_']);
    let minor_len = remainder.chars().take_while(char::is_ascii_digit).count();
    let mut minor = if minor_len == 0 {
        0
    } else {
        remainder[..minor_len].parse().ok()?
    };
    // Unversioned model IDs often append an YYYYMMDD release date after the major.
    if minor >= 1_000 {
        minor = 0;
    }
    Some((major, minor))
}

fn openai_chat_tool_choice(required: Option<&str>) -> Value {
    required.map_or_else(
        || Value::String("auto".into()),
        |name| json!({"type": "function", "function": {"name": name}}),
    )
}

fn openai_responses_tool_choice(required: Option<&str>) -> Value {
    required.map_or_else(
        || Value::String("auto".into()),
        |name| json!({"type": "function", "name": name}),
    )
}

fn openai_responses_tools(tools: &[coomi_engine::ToolSpec], native_web_search: bool) -> Vec<Value> {
    let mut output = tools
        .iter()
        .filter(|tool| !(native_web_search && tool.name == "web_search"))
        .map(|tool| {
            json!({
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "strict": true
            })
        })
        .collect::<Vec<_>>();
    if native_web_search {
        output.push(json!({"type": "web_search"}));
    }
    output
}

#[async_trait]
impl ModelProvider for HttpModelProvider {
    fn provider_id(&self) -> &str {
        &self.config.id
    }

    fn model(&self) -> &str {
        &self.config.model
    }

    fn capabilities(&self) -> ModelCapabilities {
        self.config.capabilities.clone()
    }

    async fn complete(&self, request: ModelRequest) -> Result<ModelResponse> {
        let request = self.prepare_reasoning_request(request)?;
        self.complete_prepared(request).await
    }

    async fn complete_stream(
        &self,
        request: ModelRequest,
        observer: &dyn ModelStreamObserver,
    ) -> Result<ModelResponse> {
        let request = self.prepare_reasoning_request(request)?;
        match self.config.kind {
            ProviderKind::OpenAiCompatible => {
                self.openai_compatible_stream(request, observer).await
            }
            ProviderKind::OpenAiResponses => self.openai_responses_stream(request, observer).await,
            ProviderKind::AnthropicMessages | ProviderKind::GeminiNative => {
                self.complete_prepared(request).await
            }
        }
    }

    async fn compact(&self, request: CompactionRequest) -> Result<Option<CompactionResponse>> {
        if self.config.kind == ProviderKind::OpenAiResponses
            && self.config.capabilities.supports_remote_compaction
        {
            let response = match self.config.remote_compaction_mode {
                RemoteCompactionMode::Legacy => self.openai_remote_compaction(request).await,
                RemoteCompactionMode::V2 => self.openai_remote_compaction_v2(request).await,
            }?;
            return Ok(Some(response));
        }
        Ok(None)
    }
}

struct ProviderStreamObservation<'a> {
    observer: &'a dyn ModelStreamObserver,
    attempt: usize,
    started: Instant,
    request_bytes: u64,
    max_output_tokens: u64,
    parallel_tool_calls: Option<bool>,
    http_status: u16,
}

impl ProviderStreamObservation<'_> {
    fn emit(&self, phase: ProviderStreamPhase, response_bytes: u64) {
        self.observer.on_provider_stream(&ProviderStreamEvent {
            attempt: self.attempt,
            phase,
            elapsed_ms: elapsed_ms(self.started),
            request_bytes: self.request_bytes,
            response_bytes,
            max_output_tokens: self.max_output_tokens,
            parallel_tool_calls: self.parallel_tool_calls,
            http_status: self.http_status,
        });
    }
}

async fn read_sse(response: Response, consume: impl FnMut(Value) -> Result<()>) -> Result<()> {
    read_sse_inner(response, None, consume).await
}

async fn read_sse_observed(
    response: Response,
    observation: ProviderStreamObservation<'_>,
    consume: impl FnMut(Value) -> Result<()>,
) -> Result<()> {
    read_sse_inner(response, Some(&observation), consume).await
}

async fn read_sse_inner(
    response: Response,
    observation: Option<&ProviderStreamObservation<'_>>,
    mut consume: impl FnMut(Value) -> Result<()>,
) -> Result<()> {
    let mut stream = response.bytes_stream();
    let mut buffer = Vec::new();
    let mut saw_completion_event = false;
    let mut first_byte_received = false;
    let mut first_event_received = false;
    let mut response_bytes = 0_u64;
    loop {
        let next_chunk = if saw_completion_event {
            match tokio::time::timeout(PROVIDER_COMPLETION_GRACE_TIMEOUT, stream.next()).await {
                Ok(chunk) => chunk,
                Err(_) => {
                    if let Some(observation) = observation {
                        observation.emit(ProviderStreamPhase::Completed, response_bytes);
                    }
                    return Ok(());
                }
            }
        } else if !first_byte_received {
            // Gateways that accept a streaming request but never send any body
            // byte (stalled upstream, overloaded proxy) must fail fast so the
            // caller can retry instead of making the user wait ~3 minutes.
            match tokio::time::timeout(PROVIDER_FIRST_BYTE_TIMEOUT, stream.next()).await {
                Ok(chunk) => chunk,
                Err(_) => anyhow::bail!(
                    "provider stream failed: no first byte within {}s",
                    PROVIDER_FIRST_BYTE_TIMEOUT.as_secs()
                ),
            }
        } else {
            // Mid-stream liveness: a long gap between chunks means the gateway
            // wedged. Fail fast so the caller can retry before the user waits
            // out the whole read timeout.
            match tokio::time::timeout(PROVIDER_STREAM_STALL_TIMEOUT, stream.next()).await {
                Ok(chunk) => chunk,
                Err(_) => anyhow::bail!(
                    "provider stream failed: stream stalled for {}s",
                    PROVIDER_STREAM_STALL_TIMEOUT.as_secs()
                ),
            }
        };
        let Some(chunk) = next_chunk else {
            break;
        };
        match chunk {
            Ok(chunk) => {
                if !chunk.is_empty() {
                    response_bytes = response_bytes
                        .saturating_add(u64::try_from(chunk.len()).unwrap_or(u64::MAX));
                    if !first_byte_received && let Some(observation) = observation {
                        observation.emit(ProviderStreamPhase::FirstByte, response_bytes);
                    }
                    first_byte_received = true;
                }
                buffer.extend_from_slice(&chunk)
            }
            Err(_) if saw_completion_event => return Ok(()),
            Err(error) => return Err(error).context("provider stream failed"),
        }
        while let Some(newline) = buffer.iter().position(|byte| *byte == b'\n') {
            let mut line = buffer.drain(..=newline).collect::<Vec<_>>();
            while matches!(line.last(), Some(b'\n' | b'\r')) {
                line.pop();
            }
            match parse_provider_sse_line(&line)? {
                ProviderSseLine::Ignore => {}
                ProviderSseLine::Done => {
                    if let Some(observation) = observation {
                        observation.emit(ProviderStreamPhase::Completed, response_bytes);
                    }
                    return Ok(());
                }
                ProviderSseLine::Value(value) => {
                    if !first_event_received && let Some(observation) = observation {
                        observation.emit(ProviderStreamPhase::FirstEvent, response_bytes);
                    }
                    first_event_received = true;
                    let completed = provider_sse_value_completed(&value);
                    consume(value)?;
                    if completed {
                        saw_completion_event = true;
                    }
                }
            }
        }
    }

    while matches!(buffer.last(), Some(b'\n' | b'\r')) {
        buffer.pop();
    }
    if !buffer.is_empty() {
        match parse_provider_sse_line(&buffer)? {
            ProviderSseLine::Ignore => {}
            ProviderSseLine::Done => {
                if let Some(observation) = observation {
                    observation.emit(ProviderStreamPhase::Completed, response_bytes);
                }
                return Ok(());
            }
            ProviderSseLine::Value(value) => {
                if !first_event_received && let Some(observation) = observation {
                    observation.emit(ProviderStreamPhase::FirstEvent, response_bytes);
                }
                let completed = provider_sse_value_completed(&value);
                consume(value)?;
                saw_completion_event |= completed;
            }
        }
    }

    anyhow::ensure!(
        saw_completion_event,
        "provider stream failed: stream ended before [DONE], finish_reason, or response.completed"
    );
    if let Some(observation) = observation {
        observation.emit(ProviderStreamPhase::Completed, response_bytes);
    }
    Ok(())
}

enum ProviderSseLine {
    Ignore,
    Done,
    Value(Value),
}

fn parse_provider_sse_line(line: &[u8]) -> Result<ProviderSseLine> {
    let line = std::str::from_utf8(line).context("provider stream was not UTF-8")?;
    let Some(data) = line.strip_prefix("data:") else {
        return Ok(ProviderSseLine::Ignore);
    };
    let data = data.trim();
    if data.is_empty() {
        return Ok(ProviderSseLine::Ignore);
    }
    if data == "[DONE]" {
        return Ok(ProviderSseLine::Done);
    }
    let value = serde_json::from_str::<Value>(data).context("invalid provider SSE JSON")?;
    Ok(ProviderSseLine::Value(value))
}

fn provider_sse_value_completed(value: &Value) -> bool {
    value.get("type").and_then(Value::as_str) == Some("response.completed")
        || value
            .pointer("/choices/0/finish_reason")
            .is_some_and(|reason| !reason.is_null())
}

/// Classifies streaming-phase failures that are worth retrying. Only
/// transport-level conditions are retryable (stalled first byte, truncated
/// stream, read timeout, connection reset). Content or protocol errors
/// (invalid JSON, 4xx bodies) are not — retrying them would just waste tokens.
/// The "provider stream failed" prefix requirement keeps observer/parser
/// errors (which lack it) out of the retry path.
fn is_retryable_stream_error(error: &anyhow::Error) -> bool {
    let text = format!("{error:#}").to_ascii_lowercase();
    if !text.contains("provider stream failed") {
        return false;
    }
    text.contains("stream ended before")
        || text.contains("no first byte")
        || text.contains("no response head")
        || text.contains("stream stalled")
        || text.contains("timed out")
        || text.contains("connection reset")
        || text.contains("broken pipe")
        || text.contains("unexpected eof")
        || text.contains("connection closed")
}

/// Classifies send()-phase failures (before any response headers) that a
/// retry may recover from: connection refused/reset, TLS, DNS, timeouts.
/// Content errors surface as responses (HTTP status), not as send errors, so
/// a send error here is overwhelmingly transport-level. Accepts either the
/// anyhow error from send_openai_chat or the raw reqwest error from send().
fn is_retryable_send_error(error: &impl std::fmt::Display) -> bool {
    let text = format!("{error:#}").to_ascii_lowercase();
    text.contains("connect")
        || text.contains("timed out")
        || text.contains("timeout")
        || text.contains("connection reset")
        || text.contains("broken pipe")
        || text.contains("tls")
        || text.contains("dns")
        || text.contains("lookup")
        || text.contains("no response head")
}

/// Minimal retry-state surface shared by the chat and responses stream states.
trait StreamRetryState {
    fn saw_tool_calls(&self) -> bool;
    fn pushed_any(&self) -> bool;
    fn truncated(&self) -> bool;
    fn emitted_text_characters(&self) -> usize;
}

impl StreamRetryState for ChatStreamState {
    fn saw_tool_calls(&self) -> bool {
        self.saw_tool_calls
    }

    fn pushed_any(&self) -> bool {
        self.pushed_any
    }

    fn truncated(&self) -> bool {
        self.finish_reason.as_deref() == Some("length")
    }

    fn emitted_text_characters(&self) -> usize {
        self.content.chars().count()
    }
}

impl StreamRetryState for ResponsesStreamState {
    fn saw_tool_calls(&self) -> bool {
        self.saw_tool_calls
    }

    fn pushed_any(&self) -> bool {
        self.pushed_any
    }

    fn truncated(&self) -> bool {
        self.truncated
    }

    fn emitted_text_characters(&self) -> usize {
        self.content.chars().count()
    }
}

/// A stream that ended truncated (finish_reason=length / response incomplete)
/// after emitting tool-call deltas but no user-visible text is retryable: the
/// tool was never delivered to the engine, so no side effects were produced.
/// Some gateways truncate a long tool-argument stream this way (e.g. the
/// opencode-go gateway ending deepseek-v4-flash output with finish_reason
/// = length), leaving an incomplete arguments JSON that would otherwise fail
/// in finish() with "tool arguments are not valid JSON".
fn is_truncated_tool_call_stream(state: &impl StreamRetryState) -> bool {
    state.truncated() && state.saw_tool_calls() && !state.pushed_any()
}

fn is_empty_truncated_response(state: &impl StreamRetryState) -> bool {
    state.truncated() && !state.saw_tool_calls() && state.emitted_text_characters() == 0
}

fn grow_output_token_budget(body: &mut Value, key: &str) {
    let current = body
        .get(key)
        .and_then(Value::as_u64)
        .unwrap_or(PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS);
    body[key] = Value::from(
        current
            .max(PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS)
            .saturating_mul(2)
            .min(PROVIDER_MAX_RETRY_OUTPUT_TOKENS),
    );
}

/// Tool arguments that fail to parse are retryable. The tool never ran (the
/// parse happens before delivery to the engine), so a retry cannot duplicate
/// tool execution; the only cost is re-streaming the short lead-in text the
/// model already emitted, which is preferable to failing the whole turn after
/// a multi-minute wait.
fn is_retryable_finish_error(error: &anyhow::Error) -> bool {
    format!("{error:#}").contains("tool arguments are not valid JSON")
}

#[derive(Default)]
struct PartialToolCall {
    id: String,
    name: String,
    argument_fragments: Vec<String>,
    arguments: String,
}

fn merge_streamed_identifier(target: &mut String, fragment: &str) {
    if target.is_empty() {
        target.push_str(fragment);
    } else if fragment.starts_with(target.as_str()) {
        *target = fragment.to_owned();
    } else if target != fragment && !target.ends_with(fragment) {
        target.push_str(fragment);
    }
}

fn merge_streamed_arguments(target: &mut String, fragment: &str) {
    if target.is_empty() || fragment.starts_with(target.as_str()) {
        *target = fragment.to_owned();
    } else if !fragment.is_empty() && fragment != target && !target.starts_with(fragment) {
        target.push_str(fragment);
    }
}

fn record_streamed_arguments(target: &mut PartialToolCall, fragment: &str) {
    target.argument_fragments.push(fragment.to_owned());
    merge_streamed_arguments(&mut target.arguments, fragment);
}

fn parse_streamed_arguments(call: &PartialToolCall) -> Result<Value> {
    let incremental = call.argument_fragments.concat();
    match parse_arguments(&Value::String(incremental.clone())) {
        Ok(arguments) => Ok(arguments),
        Err(incremental_error) if incremental != call.arguments => {
            parse_arguments(&Value::String(call.arguments.clone())).with_context(|| {
                format!(
                    "streamed tool arguments failed both incremental and cumulative decoding; incremental error: {incremental_error:#}"
                )
            })
        }
        Err(error) => Err(error),
    }
}

fn merge_partial_tool_call(target: &mut PartialToolCall, source: PartialToolCall) {
    if target.id.is_empty() {
        target.id = source.id;
    }
    if target.name.is_empty() {
        target.name = source.name;
    }
    target.argument_fragments.extend(source.argument_fragments);
    if target.arguments.is_empty() || source.arguments.starts_with(&target.arguments) {
        target.arguments = source.arguments;
    } else if !source.arguments.is_empty()
        && source.arguments != target.arguments
        && !target.arguments.starts_with(&source.arguments)
    {
        target.arguments.push_str(&source.arguments);
    }
}

fn response_tool_aliases(value: &Value, item: Option<&Value>) -> Vec<String> {
    let mut aliases = Vec::new();
    if let Some(index) = value.get("output_index").and_then(|index| {
        index
            .as_u64()
            .map(|value| value.to_string())
            .or_else(|| index.as_str().map(str::to_owned))
    }) {
        aliases.push(format!("output-index:{index}"));
    }
    for (prefix, id) in [
        ("item", value.get("item_id")),
        ("call", value.get("call_id")),
        ("item", item.and_then(|item| item.get("id"))),
        ("call", item.and_then(|item| item.get("call_id"))),
    ] {
        if let Some(id) = id.and_then(Value::as_str).filter(|id| !id.is_empty()) {
            let alias = format!("{prefix}:{id}");
            if !aliases.contains(&alias) {
                aliases.push(alias);
            }
        }
    }
    aliases
}

#[derive(Default)]
struct ChatStreamState {
    content: String,
    reasoning: String,
    reasoning_details: Vec<Value>,
    response_model: Option<String>,
    tools: BTreeMap<usize, PartialToolCall>,
    implicit_tools: BTreeMap<usize, usize>,
    usage: TokenUsage,
    /// True once user-visible text/reasoning deltas have been pushed to the
    /// observer. A retry must never re-stream already-displayed deltas, so
    /// streams that produced visible output are not retried. Tool-call deltas
    /// do not count: a tool that was never delivered to the engine has no
    /// side effects, so its stream may safely be retried.
    pushed_any: bool,
    /// True once any tool-call delta has been received. Used to decide whether
    /// a truncated stream (finish_reason=length) may be retried.
    saw_tool_calls: bool,
    /// finish_reason reported by the stream, if any ("stop", "length", ...).
    finish_reason: Option<String>,
}

impl ChatStreamState {
    fn consume(&mut self, value: &Value, observer: &dyn ModelStreamObserver) -> Result<()> {
        if let Some(model) = optional_string(value.get("model")) {
            self.response_model = Some(model);
        }
        if value.get("usage").is_some_and(|usage| !usage.is_null()) {
            self.usage = openai_usage(value.get("usage"));
        }
        if let Some(reason) = value
            .pointer("/choices/0/finish_reason")
            .and_then(Value::as_str)
        {
            self.finish_reason = Some(reason.to_owned());
        }
        let Some(delta) = value.pointer("/choices/0/delta") else {
            return Ok(());
        };
        let reasoning = delta
            .get("reasoning_content")
            .or_else(|| delta.get("reasoning"))
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty());
        if let Some(reasoning) = reasoning {
            self.pushed_any = true;
            self.reasoning.push_str(reasoning);
            observer.on_reasoning_delta(reasoning);
        }
        if let Some(details) = delta.get("reasoning_details").and_then(Value::as_array) {
            self.reasoning_details.extend(details.iter().cloned());
            if reasoning.is_none() {
                for detail in details {
                    if let Some(text) = detail
                        .get("text")
                        .or_else(|| detail.get("summary"))
                        .and_then(Value::as_str)
                        .filter(|value| !value.is_empty())
                    {
                        self.pushed_any = true;
                        self.reasoning.push_str(text);
                        observer.on_reasoning_delta(text);
                    }
                }
            }
        }
        if let Some(content) = delta
            .get("content")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
        {
            self.pushed_any = true;
            self.content.push_str(content);
            observer.on_text_delta(content);
        }
        for (position, item) in delta
            .get("tool_calls")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .enumerate()
        {
            let explicit_index = item
                .get("index")
                .and_then(Value::as_u64)
                .and_then(|value| usize::try_from(value).ok());
            let item_id = item.get("id").and_then(Value::as_str).unwrap_or_default();
            let item_name = item
                .pointer("/function/name")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let index = explicit_index.unwrap_or_else(|| {
                self.tools
                    .iter()
                    .find_map(|(index, call)| {
                        (!item_id.is_empty() && call.id == item_id).then_some(*index)
                    })
                    .or_else(|| {
                        self.implicit_tools.get(&position).copied().filter(|index| {
                            self.tools.get(index).is_some_and(|call| {
                                (item_id.is_empty() || call.id.is_empty() || call.id == item_id)
                                    && (item_name.is_empty()
                                        || call.name.is_empty()
                                        || call.name == item_name)
                            })
                        })
                    })
                    .unwrap_or_else(|| {
                        self.tools
                            .last_key_value()
                            .map_or(0, |(index, _)| index + 1)
                    })
            });
            self.implicit_tools.insert(position, index);
            self.saw_tool_calls = true;
            let target = self.tools.entry(index).or_default();
            if !item_id.is_empty() {
                merge_streamed_identifier(&mut target.id, item_id);
            }
            if let Some(function) = item.get("function") {
                if let Some(name) = function.get("name").and_then(Value::as_str) {
                    merge_streamed_identifier(&mut target.name, name);
                }
                if let Some(arguments) = function.get("arguments").and_then(Value::as_str) {
                    record_streamed_arguments(target, arguments);
                }
            }
        }
        Ok(())
    }

    fn finish(self) -> Result<ModelResponse> {
        let tool_calls = self
            .tools
            .into_values()
            .enumerate()
            .map(|(index, call)| {
                anyhow::ensure!(!call.name.is_empty(), "streamed tool call has no name");
                let arguments = parse_streamed_arguments(&call).with_context(|| {
                    format!(
                        "streamed tool call {} ({}) has invalid arguments",
                        index, call.name
                    )
                })?;
                Ok(ToolCall {
                    id: if call.id.is_empty() {
                        format!("call-{index}")
                    } else {
                        call.id
                    },
                    name: call.name,
                    arguments,
                })
            })
            .collect::<Result<Vec<_>>>()?;
        let metadata = ModelResponseMetadata {
            response_model: self.response_model.clone(),
            finish_reason: self.finish_reason.clone(),
            response_status: None,
            native_reasoning: !self.reasoning.is_empty() || !self.reasoning_details.is_empty(),
        };
        let mut provider_message = json!({
            "role": "assistant",
            "content": if self.content.is_empty() {
                Value::Null
            } else {
                Value::String(self.content.clone())
            }
        });
        if !self.reasoning_details.is_empty() {
            provider_message["reasoning_details"] = Value::Array(self.reasoning_details);
        } else if !self.reasoning.is_empty() {
            provider_message["reasoning_content"] = Value::String(self.reasoning);
        }
        if !tool_calls.is_empty() {
            provider_message["tool_calls"] = Value::Array(
                tool_calls
                    .iter()
                    .map(|call| {
                        Ok(json!({
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": serde_json::to_string(&call.arguments)?
                            }
                        }))
                    })
                    .collect::<Result<Vec<_>>>()?,
            );
        }
        Ok(ModelResponse {
            content: self.content,
            tool_calls,
            usage: self.usage,
            streamed: true,
            provider_items: vec![provider_message],
            metadata,
        })
    }
}

#[derive(Default)]
struct CompactionStreamState {
    item: Option<Value>,
    usage: TokenUsage,
}

impl CompactionStreamState {
    fn consume(&mut self, value: &Value) -> Result<()> {
        match value.get("type").and_then(Value::as_str) {
            Some("response.output_item.added" | "response.output_item.done") => {
                if let Some(item) = value.get("item")
                    && matches!(
                        item.get("type").and_then(Value::as_str),
                        Some("compaction" | "context_compaction")
                    )
                {
                    self.item = Some(item.clone());
                }
            }
            Some("response.completed") => {
                self.usage = responses_usage(value.pointer("/response/usage"));
            }
            Some("error" | "response.failed") => {
                anyhow::bail!(
                    "provider compaction stream failed: {}",
                    value
                        .pointer("/error/message")
                        .and_then(Value::as_str)
                        .unwrap_or("unknown error")
                );
            }
            _ => {}
        }
        Ok(())
    }

    fn finish(self) -> Result<(Value, TokenUsage)> {
        let item = self
            .item
            .context("compaction stream contained no compaction output item")?;
        anyhow::ensure!(
            item.get("encrypted_content")
                .and_then(Value::as_str)
                .is_some(),
            "compaction output has no encrypted_content"
        );
        Ok((item, self.usage))
    }
}

#[derive(Default)]
struct ResponsesStreamState {
    content: String,
    tools: BTreeMap<String, PartialToolCall>,
    tool_aliases: BTreeMap<String, String>,
    provider_items: BTreeMap<u64, Value>,
    usage: TokenUsage,
    response_model: Option<String>,
    response_status: Option<String>,
    finish_reason: Option<String>,
    native_reasoning: bool,
    /// True once user-visible text/reasoning deltas have been pushed to the
    /// observer. Streams that produced visible output are never retried (a
    /// retry would re-display already-streamed deltas). Function-call deltas
    /// do not count: an undelivered tool call has no side effects.
    pushed_any: bool,
    /// True once any function-call delta has been received.
    saw_tool_calls: bool,
    /// True when the stream ended with response.status == "incomplete" — the
    /// responses-API equivalent of finish_reason=length (truncated output).
    truncated: bool,
}

impl ResponsesStreamState {
    fn resolve_tool_key(&mut self, value: &Value, item: Option<&Value>) -> String {
        let aliases = response_tool_aliases(value, item);
        let mut existing_keys = aliases
            .iter()
            .filter_map(|alias| {
                self.tool_aliases
                    .get(alias)
                    .cloned()
                    .or_else(|| self.tools.contains_key(alias).then(|| alias.clone()))
            })
            .collect::<Vec<_>>();
        existing_keys.dedup();

        let key = existing_keys.first().cloned().unwrap_or_else(|| {
            aliases.first().cloned().unwrap_or_else(|| {
                if self.tools.len() == 1
                    && let Some((key, _)) = self.tools.first_key_value()
                {
                    return key.clone();
                }
                let mut index = self.tools.len();
                loop {
                    let candidate = format!("anonymous-tool-{index}");
                    if !self.tools.contains_key(&candidate) {
                        return candidate;
                    }
                    index += 1;
                }
            })
        });

        for old_key in existing_keys.into_iter().skip(1) {
            if old_key == key {
                continue;
            }
            if let Some(source) = self.tools.remove(&old_key) {
                merge_partial_tool_call(self.tools.entry(key.clone()).or_default(), source);
            }
            for target in self.tool_aliases.values_mut() {
                if *target == old_key {
                    *target = key.clone();
                }
            }
        }
        for alias in aliases {
            self.tool_aliases.insert(alias, key.clone());
        }
        key
    }

    fn consume(&mut self, value: &Value, observer: &dyn ModelStreamObserver) -> Result<()> {
        if let Some(model) = optional_string(value.get("model")) {
            self.response_model = Some(model);
        }
        match value.get("type").and_then(Value::as_str) {
            Some("response.output_text.delta") => {
                if let Some(delta) = value
                    .get("delta")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                {
                    self.pushed_any = true;
                    self.content.push_str(delta);
                    observer.on_text_delta(delta);
                }
            }
            Some("response.reasoning_summary_text.delta" | "response.reasoning_text.delta") => {
                self.native_reasoning = true;
                if let Some(delta) = value
                    .get("delta")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                {
                    self.pushed_any = true;
                    observer.on_reasoning_delta(delta);
                }
            }
            Some("response.output_item.added" | "response.output_item.done") => {
                if let Some(item) = value.get("item") {
                    self.native_reasoning |=
                        output_has_native_reasoning(std::slice::from_ref(item));
                    let output_index = value
                        .get("output_index")
                        .and_then(Value::as_u64)
                        .unwrap_or(self.provider_items.len() as u64);
                    self.provider_items.insert(output_index, item.clone());
                }
                if let Some(item) = value.get("item")
                    && item.get("type").and_then(Value::as_str) == Some("function_call")
                {
                    self.saw_tool_calls = true;
                    let key = self.resolve_tool_key(value, Some(item));
                    let id = item
                        .get("call_id")
                        .or_else(|| item.get("id"))
                        .and_then(Value::as_str)
                        .unwrap_or_default();
                    let target = self.tools.entry(key).or_default();
                    if !id.is_empty() {
                        target.id = id.to_owned();
                    }
                    if let Some(name) = item.get("name").and_then(Value::as_str) {
                        target.name = name.to_owned();
                    }
                    if let Some(arguments) = item.get("arguments").and_then(Value::as_str)
                        && (!arguments.is_empty() || target.arguments.is_empty())
                    {
                        target.arguments = arguments.to_owned();
                    }
                }
            }
            Some("response.function_call_arguments.delta") => {
                let key = self.resolve_tool_key(value, None);
                if let Some(delta) = value.get("delta").and_then(Value::as_str) {
                    self.saw_tool_calls = true;
                    let target = self.tools.entry(key).or_default();
                    if target.id.is_empty()
                        && let Some(call_id) = value.get("call_id").and_then(Value::as_str)
                    {
                        target.id = call_id.to_owned();
                    }
                    target.arguments.push_str(delta);
                }
            }
            Some("response.completed") => {
                self.usage = responses_usage(value.pointer("/response/usage"));
                self.response_model = optional_string(value.pointer("/response/model"))
                    .or_else(|| self.response_model.clone());
                self.response_status = optional_string(value.pointer("/response/status"));
                self.finish_reason =
                    optional_string(value.pointer("/response/incomplete_details/reason"));
                if value.pointer("/response/status").and_then(Value::as_str) == Some("incomplete") {
                    self.truncated = true;
                }
                if let Some(output) = value.pointer("/response/output").and_then(Value::as_array) {
                    self.native_reasoning |= output_has_native_reasoning(output);
                    self.provider_items = output
                        .iter()
                        .enumerate()
                        .map(|(index, item)| (index as u64, item.clone()))
                        .collect();
                }
            }
            Some("error" | "response.failed") => {
                anyhow::bail!(
                    "provider stream failed: {}",
                    value
                        .pointer("/error/message")
                        .and_then(Value::as_str)
                        .unwrap_or("unknown error")
                );
            }
            _ => {}
        }
        Ok(())
    }

    fn finish(self) -> Result<ModelResponse> {
        let tool_calls = self
            .tools
            .into_values()
            .enumerate()
            .map(|(index, call)| {
                anyhow::ensure!(!call.name.is_empty(), "streamed tool call has no name");
                Ok(ToolCall {
                    id: if call.id.is_empty() {
                        format!("call-{index}")
                    } else {
                        call.id
                    },
                    name: call.name,
                    arguments: parse_arguments(&Value::String(call.arguments))?,
                })
            })
            .collect::<Result<Vec<_>>>()?;
        Ok(ModelResponse {
            content: self.content,
            tool_calls,
            usage: self.usage,
            streamed: true,
            provider_items: self.provider_items.into_values().collect(),
            metadata: ModelResponseMetadata {
                response_model: self.response_model,
                finish_reason: self.finish_reason,
                response_status: self.response_status,
                native_reasoning: self.native_reasoning,
            },
        })
    }
}

fn endpoint(base_url: &str, suffix: &str) -> String {
    let base_url = base_url.trim_end_matches('/');
    if base_url.ends_with(suffix) {
        base_url.to_string()
    } else {
        format!("{base_url}/{suffix}")
    }
}

fn retryable_provider_status(status: StatusCode) -> bool {
    matches!(
        status,
        StatusCode::REQUEST_TIMEOUT
            | StatusCode::TOO_MANY_REQUESTS
            | StatusCode::INTERNAL_SERVER_ERROR
            | StatusCode::BAD_GATEWAY
            | StatusCode::SERVICE_UNAVAILABLE
            | StatusCode::GATEWAY_TIMEOUT
    )
}

fn required_tool_choice_unsupported(status: StatusCode) -> bool {
    matches!(
        status,
        StatusCode::BAD_REQUEST | StatusCode::UNPROCESSABLE_ENTITY
    )
}

#[derive(Clone, Copy)]
enum ReasoningWireProtocol {
    OpenAiChat,
    OpenAiResponses,
    Anthropic,
    Gemini,
}

fn without_reasoning_fields(body: &Value, protocol: ReasoningWireProtocol) -> Value {
    let mut fallback = body.clone();
    match protocol {
        ReasoningWireProtocol::OpenAiChat => {
            if let Some(object) = fallback.as_object_mut() {
                object.remove("reasoning_effort");
                object.remove("reasoning");
            }
        }
        ReasoningWireProtocol::OpenAiResponses => {
            if let Some(object) = fallback.as_object_mut() {
                object.remove("reasoning");
                if let Some(include) = object.get_mut("include").and_then(Value::as_array_mut) {
                    include.retain(|value| {
                        !value
                            .as_str()
                            .is_some_and(|item| item.to_ascii_lowercase().contains("reasoning"))
                    });
                }
                if object
                    .get("include")
                    .and_then(Value::as_array)
                    .is_some_and(Vec::is_empty)
                {
                    object.remove("include");
                }
            }
        }
        ReasoningWireProtocol::Anthropic => {
            if let Some(object) = fallback.as_object_mut() {
                object.remove("thinking");
                if let Some(output_config) = object
                    .get_mut("output_config")
                    .and_then(Value::as_object_mut)
                {
                    output_config.remove("effort");
                }
                if object
                    .get("output_config")
                    .and_then(Value::as_object)
                    .is_some_and(Map::is_empty)
                {
                    object.remove("output_config");
                }
            }
        }
        ReasoningWireProtocol::Gemini => {
            if let Some(thinking_config) = fallback.pointer_mut("/generationConfig/thinkingConfig")
            {
                *thinking_config = Value::Null;
                if let Some(object) = fallback
                    .pointer_mut("/generationConfig")
                    .and_then(Value::as_object_mut)
                {
                    object.remove("thinkingConfig");
                }
            }
        }
    }
    fallback
}

fn has_reasoning_rejection_error(body: &str) -> bool {
    let normalized = body.to_ascii_lowercase();
    let mentions_field = [
        "reasoning_effort",
        "reasoning.effort",
        "output_config.effort",
        "thinkingbudget",
        "thinkinglevel",
        "thinking",
    ]
    .iter()
    .any(|field| normalized.contains(field));
    let describes_rejection = [
        "unknown",
        "unsupported",
        "unrecognized",
        "unexpected",
        "invalid",
        "not allowed",
        "not support",
        "additional propert",
        "extra inputs",
        "does not permit",
        "must be one of",
    ]
    .iter()
    .any(|phrase| normalized.contains(phrase));
    mentions_field && describes_rejection
}

async fn checked_json(response: Response) -> Result<Value> {
    let status = response.status();
    let body = response
        .text()
        .await
        .context("failed to read provider response")?;
    if !status.is_success() {
        return Err(provider_http_error(status, &body));
    }
    serde_json::from_str(&body).context("provider returned invalid JSON")
}

fn provider_http_error(status: StatusCode, body: &str) -> anyhow::Error {
    let detail = body.chars().take(800).collect::<String>();
    anyhow::anyhow!("provider returned HTTP {status}: {detail}")
}

fn has_missing_openai_message_id_error(body: &str) -> bool {
    let parsed = serde_json::from_str::<Value>(body).ok();
    if parsed
        .as_ref()
        .is_some_and(has_structured_missing_message_id)
    {
        return true;
    }
    parsed
        .as_ref()
        .and_then(|value| value.pointer("/error/message"))
        .and_then(Value::as_str)
        .map_or_else(
            || text_mentions_missing_message_id(body),
            text_mentions_missing_message_id,
        )
}

fn has_structured_missing_message_id(value: &Value) -> bool {
    match value {
        Value::Array(items) => items.iter().any(has_structured_missing_message_id),
        Value::Object(object) => {
            let location_matches =
                object
                    .get("loc")
                    .and_then(Value::as_array)
                    .is_some_and(|location| {
                        let message_position = location
                            .iter()
                            .position(|part| part.as_str() == Some("messages"));
                        message_position.is_some_and(|position| {
                            location.get(position + 1).and_then(Value::as_u64).is_some()
                                && location.get(position + 2).and_then(Value::as_str) == Some("id")
                                && position + 3 == location.len()
                        })
                    });
            let required = object
                .get("msg")
                .or_else(|| object.get("message"))
                .and_then(Value::as_str)
                .is_some_and(text_describes_missing_field);
            (location_matches && required)
                || ["error", "errors", "detail", "details"]
                    .iter()
                    .filter_map(|key| object.get(*key))
                    .any(has_structured_missing_message_id)
        }
        Value::String(text) => text_mentions_missing_message_id(text),
        _ => false,
    }
}

fn text_describes_missing_field(text: &str) -> bool {
    let normalized = text.to_ascii_lowercase();
    normalized.contains("missing") || normalized.contains("required")
}

fn text_mentions_missing_message_id(text: &str) -> bool {
    let normalized = text.to_ascii_lowercase();
    for prefix in ["messages[", "messages."] {
        let mut remaining = normalized.as_str();
        while let Some(start) = remaining.find(prefix) {
            let after_prefix = &remaining[start + prefix.len()..];
            let digit_count = after_prefix
                .chars()
                .take_while(char::is_ascii_digit)
                .count();
            if digit_count == 0 {
                break;
            }
            let after_index = &after_prefix[digit_count..];
            let after_path = if prefix.ends_with('[') {
                after_index.strip_prefix(']').unwrap_or(after_index)
            } else {
                after_index
            };
            let clause = after_path
                .split(';')
                .next()
                .unwrap_or(after_path)
                .trim_start();
            let direct_id_path = clause
                .strip_prefix(".id")
                .is_some_and(text_describes_missing_field);
            let cleaned = clause
                .chars()
                .filter(|character| !matches!(character, '`' | '\'' | '"'))
                .collect::<String>();
            let missing_named_id = cleaned.starts_with(':') && cleaned.contains("missing field id");
            if direct_id_path || missing_named_id {
                return true;
            }
            remaining = after_prefix;
        }
    }
    false
}

fn openai_message_id_fallback(body: &Value) -> Option<Value> {
    let mut fallback = body.clone();
    let messages = fallback.get_mut("messages")?.as_array_mut()?;
    let mut changed = false;

    for (index, value) in messages.iter_mut().enumerate() {
        let Some(message) = value.as_object_mut() else {
            continue;
        };
        if message.get("id").is_some_and(|value| !value.is_null()) {
            continue;
        }
        message.insert(
            "id".into(),
            Value::String(format!("storydex-message-{index}")),
        );
        changed = true;
    }

    changed.then_some(fallback)
}

fn openai_messages(messages: &[ChatMessage]) -> Result<Vec<Value>> {
    let mut output = Vec::new();
    for message in messages {
        if !message.provider_items.is_empty()
            && let Some(provider_message) = message
                .provider_items
                .iter()
                .find(|item| is_openai_chat_assistant_message(item))
        {
            output.push(provider_message.clone());
            continue;
        }
        if message.role == Role::Assistant
            && message.content.is_empty()
            && message.tool_calls.is_empty()
        {
            continue;
        }
        let value = match message.role {
            Role::System => json!({"role": "system", "content": message.content}),
            Role::User => json!({"role": "user", "content": message.content}),
            Role::Assistant => {
                let mut value = json!({
                    "role": "assistant",
                    "content": if message.content.is_empty() { Value::Null } else { Value::String(message.content.clone()) }
                });
                if !message.tool_calls.is_empty() {
                    value["tool_calls"] = Value::Array(
                        message
                            .tool_calls
                            .iter()
                            .map(|call| {
                                Ok(json!({
                                    "id": call.id,
                                    "type": "function",
                                    "function": {
                                        "name": call.name,
                                        "arguments": serde_json::to_string(&call.arguments)?
                                    }
                                }))
                            })
                            .collect::<Result<Vec<_>>>()?,
                    );
                }
                value
            }
            Role::Tool => {
                let content = if message.images.is_empty() {
                    Value::String(message.content.clone())
                } else {
                    Value::Array(tool_output_images(message, "text", "image_url"))
                };
                json!({
                    "role": "tool",
                    "tool_call_id": message.tool_call_id.as_deref().context("tool message has no call id")?,
                    "content": content
                })
            }
        };
        output.push(value);
    }
    Ok(output)
}

fn responses_input(messages: &[ChatMessage]) -> Result<Vec<Value>> {
    let mut input = Vec::new();
    for message in messages {
        if !message.provider_items.is_empty()
            && message.provider_items.iter().all(is_openai_responses_item)
        {
            input.extend(message.provider_items.iter().cloned());
            continue;
        }
        match message.role {
            Role::System | Role::User => input.push(json!({
                "role": role_name(message.role),
                "content": message.content
            })),
            Role::Assistant => {
                if !message.content.is_empty() {
                    input.push(json!({"role": "assistant", "content": message.content}));
                }
                for call in &message.tool_calls {
                    input.push(json!({
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": serde_json::to_string(&call.arguments)?
                    }));
                }
            }
            Role::Tool => {
                let output = if message.images.is_empty() {
                    Value::String(message.content.clone())
                } else {
                    let mut items = vec![json!({
                        "type": "input_text",
                        "text": message.content
                    })];
                    items.extend(message.images.iter().map(|image| {
                        json!({
                            "type": "input_image",
                            "image_url": image.data_url()
                        })
                    }));
                    Value::Array(items)
                };
                input.push(json!({
                    "type": "function_call_output",
                    "call_id": message.tool_call_id.as_deref().context("tool message has no call id")?,
                    "output": output
                }));
            }
        }
    }
    Ok(input)
}

fn remote_compaction_v2_body(
    request: &CompactionRequest,
    supports_web_search: bool,
    parallel_tool_calls: bool,
) -> Result<Value> {
    let mut input = responses_input(&request.messages)?;
    input.push(json!({"type": "compaction_trigger"}));
    let mut body = json!({
        "model": request.model,
        "input": input,
        "instructions": request.system_prompt,
        "stream": true,
        "parallel_tool_calls": parallel_tool_calls
    });
    let tools = openai_responses_tools(&request.tools, supports_web_search);
    if !tools.is_empty() {
        body["tools"] = Value::Array(tools);
    }
    Ok(body)
}

fn anthropic_messages(messages: &[ChatMessage]) -> Result<(String, Vec<Value>)> {
    let mut system = Vec::new();
    let mut output = Vec::new();
    for message in messages {
        if !message.provider_items.is_empty() {
            if message
                .provider_items
                .iter()
                .all(is_anthropic_content_block)
            {
                output.push(json!({
                    "role": "assistant",
                    "content": message.provider_items.clone()
                }));
                continue;
            }
            if message.role == Role::Assistant
                && message.content.is_empty()
                && message.tool_calls.is_empty()
            {
                continue;
            }
        }
        match message.role {
            Role::System => system.push(message.content.clone()),
            Role::User => output.push(json!({"role": "user", "content": message.content})),
            Role::Assistant => {
                let mut blocks = Vec::new();
                if !message.content.is_empty() {
                    blocks.push(json!({"type": "text", "text": message.content}));
                }
                for call in &message.tool_calls {
                    blocks.push(json!({
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments
                    }));
                }
                output.push(json!({"role": "assistant", "content": blocks}));
            }
            Role::Tool => {
                let content = if message.images.is_empty() {
                    Value::String(message.content.clone())
                } else {
                    let mut blocks = vec![json!({"type": "text", "text": message.content})];
                    blocks.extend(message.images.iter().map(|image| {
                        json!({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": image.media_type,
                                "data": image.data
                            }
                        })
                    }));
                    Value::Array(blocks)
                };
                output.push(json!({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id.as_deref().context("tool message has no call id")?,
                        "content": content
                    }]
                }));
            }
        }
    }
    Ok((system.join("\n\n"), output))
}

fn gemini_messages(messages: &[ChatMessage]) -> Result<(String, Vec<Value>)> {
    let mut system = Vec::new();
    let mut output = Vec::new();
    let mut call_names = Map::new();
    for message in messages {
        if !message.provider_items.is_empty() {
            if message.provider_items.iter().all(is_gemini_part) {
                for (index, part) in message.provider_items.iter().enumerate() {
                    if let Some(call) = part.get("functionCall")
                        && let Some(name) = call.get("name").and_then(Value::as_str)
                    {
                        call_names.insert(
                            format!("gemini-call-{index}"),
                            Value::String(name.to_owned()),
                        );
                    }
                }
                output.push(json!({
                    "role": "model",
                    "parts": message.provider_items.clone()
                }));
                continue;
            }
            if message.role == Role::Assistant
                && message.content.is_empty()
                && message.tool_calls.is_empty()
            {
                continue;
            }
        }
        match message.role {
            Role::System => system.push(message.content.clone()),
            Role::User => output.push(json!({
                "role": "user",
                "parts": [{"text": message.content}]
            })),
            Role::Assistant => {
                let mut parts = Vec::new();
                if !message.content.is_empty() {
                    parts.push(json!({"text": message.content}));
                }
                for call in &message.tool_calls {
                    call_names.insert(call.id.clone(), Value::String(call.name.clone()));
                    parts.push(json!({
                        "functionCall": {"name": call.name, "args": call.arguments}
                    }));
                }
                output.push(json!({"role": "model", "parts": parts}));
            }
            Role::Tool => {
                let call_id = message
                    .tool_call_id
                    .as_deref()
                    .context("tool message has no call id")?;
                let name = call_names
                    .get(call_id)
                    .and_then(Value::as_str)
                    .context("gemini tool result has no matching call")?;
                let mut parts = vec![json!({
                    "functionResponse": {
                        "name": name,
                        "response": {"output": message.content}
                    }
                })];
                parts.extend(message.images.iter().map(|image| {
                    json!({
                        "inlineData": {
                            "mimeType": image.media_type,
                            "data": image.data
                        }
                    })
                }));
                output.push(json!({"role": "user", "parts": parts}));
            }
        }
    }
    Ok((system.join("\n\n"), output))
}

fn is_openai_chat_assistant_message(value: &Value) -> bool {
    let has_content = value.get("content").is_some_and(|content| match content {
        Value::Null => false,
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        _ => true,
    });
    let has_tool_calls = value
        .get("tool_calls")
        .and_then(Value::as_array)
        .is_some_and(|items| !items.is_empty());
    value.get("type").is_none()
        && value.get("role").and_then(Value::as_str) == Some("assistant")
        && (has_content || has_tool_calls)
}

fn is_openai_responses_item(value: &Value) -> bool {
    matches!(
        value.get("type").and_then(Value::as_str),
        Some(
            "message"
                | "function_call"
                | "reasoning"
                | "compaction"
                | "context_compaction"
                | "computer_call"
                | "web_search_call"
        )
    )
}

fn is_anthropic_content_block(value: &Value) -> bool {
    matches!(
        value.get("type").and_then(Value::as_str),
        Some("text" | "thinking" | "redacted_thinking" | "tool_use")
    )
}

fn is_gemini_part(value: &Value) -> bool {
    value.get("type").is_none()
        && [
            "text",
            "functionCall",
            "thoughtSignature",
            "inlineData",
            "executableCode",
            "codeExecutionResult",
        ]
        .iter()
        .any(|key| value.get(*key).is_some())
}

fn parse_openai_tool_calls(value: Option<&Value>) -> Result<Vec<ToolCall>> {
    value
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(|call| {
            let function = call
                .get("function")
                .context("tool call has no function object")?;
            let arguments = function
                .get("arguments")
                .context("tool call has no arguments")?;
            Ok(ToolCall {
                id: required_string(call, "id")?.to_string(),
                name: required_string(function, "name")?.to_string(),
                arguments: parse_arguments(arguments)?,
            })
        })
        .collect()
}

fn tool_output_images(message: &ChatMessage, text_type: &str, image_type: &str) -> Vec<Value> {
    let mut parts = vec![json!({"type": text_type, "text": message.content})];
    parts.extend(message.images.iter().map(|image| {
        json!({
            "type": image_type,
            "image_url": {"url": image.data_url()}
        })
    }));
    parts
}

fn parse_function_call_item(item: &Value) -> Result<ToolCall> {
    Ok(ToolCall {
        id: item
            .get("call_id")
            .or_else(|| item.get("id"))
            .and_then(Value::as_str)
            .context("function call item has no call_id")?
            .to_string(),
        name: required_string(item, "name")?.to_string(),
        arguments: parse_arguments(
            item.get("arguments")
                .context("function call item has no arguments")?,
        )?,
    })
}

fn parse_arguments(value: &Value) -> Result<Value> {
    let parsed = match value {
        Value::String(value) => {
            serde_json::from_str(value).context("tool arguments are not valid JSON")?
        }
        value => value.clone(),
    };
    if !parsed.is_object() {
        anyhow::bail!("tool arguments must be a JSON object")
    }
    Ok(parsed)
}

fn required_string<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value
        .get(key)
        .and_then(Value::as_str)
        .with_context(|| format!("missing string field `{key}`"))
}

fn text_content(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(value)) => value.clone(),
        Some(Value::Array(parts)) => parts
            .iter()
            .filter_map(|part| part.get("text").and_then(Value::as_str))
            .collect::<Vec<_>>()
            .join(""),
        _ => String::new(),
    }
}

fn optional_string(value: Option<&Value>) -> Option<String> {
    value
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
}

fn openai_message_has_native_reasoning(message: &Value) -> bool {
    ["reasoning_content", "reasoning", "analysis"]
        .into_iter()
        .filter_map(|key| message.get(key))
        .any(|value| match value {
            Value::String(text) => !text.is_empty(),
            Value::Array(items) => !items.is_empty(),
            Value::Object(items) => !items.is_empty(),
            _ => false,
        })
        || message
            .get("reasoning_details")
            .and_then(Value::as_array)
            .is_some_and(|items| !items.is_empty())
}

fn output_has_native_reasoning(output: &[Value]) -> bool {
    output.iter().any(|item| {
        matches!(
            item.get("type").and_then(Value::as_str),
            Some("reasoning" | "analysis")
        )
    })
}

fn anthropic_block_is_native_reasoning(block: &Value) -> bool {
    matches!(
        block.get("type").and_then(Value::as_str),
        Some("thinking" | "redacted_thinking")
    )
}

fn gemini_part_is_native_reasoning(part: &Value) -> bool {
    part.get("thought").and_then(Value::as_bool) == Some(true)
}

fn role_name(role: Role) -> &'static str {
    match role {
        Role::System => "system",
        Role::User => "user",
        Role::Assistant => "assistant",
        Role::Tool => "tool",
    }
}

fn openai_usage(value: Option<&Value>) -> TokenUsage {
    TokenUsage {
        input_tokens: nested_u64(value, "prompt_tokens"),
        cached_input_tokens: value
            .and_then(|usage| usage.pointer("/prompt_tokens_details/cached_tokens"))
            .and_then(Value::as_u64)
            .unwrap_or(0),
        output_tokens: nested_u64(value, "completion_tokens"),
        reasoning_tokens: value
            .and_then(|usage| usage.pointer("/completion_tokens_details/reasoning_tokens"))
            .and_then(Value::as_u64),
    }
}

fn responses_usage(value: Option<&Value>) -> TokenUsage {
    TokenUsage {
        input_tokens: nested_u64(value, "input_tokens"),
        cached_input_tokens: value
            .and_then(|usage| usage.pointer("/input_tokens_details/cached_tokens"))
            .and_then(Value::as_u64)
            .unwrap_or(0),
        output_tokens: nested_u64(value, "output_tokens"),
        reasoning_tokens: value
            .and_then(|usage| usage.pointer("/output_tokens_details/reasoning_tokens"))
            .and_then(Value::as_u64),
    }
}

fn nested_u64(value: Option<&Value>, key: &str) -> u64 {
    value
        .and_then(|value| value.get(key))
        .and_then(Value::as_u64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ReasoningProfileSettings;
    use std::io::Read;
    use std::io::Write;
    use std::net::TcpListener;
    use std::net::TcpStream;
    use std::thread::JoinHandle;

    struct TestStreamObserver;

    impl ModelStreamObserver for TestStreamObserver {
        fn on_text_delta(&self, _delta: &str) {}

        fn on_reasoning_delta(&self, _delta: &str) {}
    }

    fn reasoning_provider(kind: ProviderKind, model: &str) -> ProviderConfig {
        ProviderConfig {
            id: "test".into(),
            kind,
            display: "Test".into(),
            api_key: String::new(),
            base_url: "https://example.test/v1".into(),
            model: model.into(),
            fast_model: None,
            capabilities: ModelCapabilities::default(),
            supports_reasoning_effort: Some(true),
            reasoning_prompt_fallback: None,
            reasoning_effort_map: BTreeMap::new(),
            reasoning_profiles: BTreeMap::new(),
            remote_compaction_mode: RemoteCompactionMode::default(),
        }
    }

    #[test]
    fn auto_omits_fields_and_disabled_reasoning_is_rejected() {
        let mut config = reasoning_provider(ProviderKind::OpenAiCompatible, "gpt-5.6");
        let mut body = json!({});
        apply_reasoning_effort(
            &mut body,
            &config,
            &config.model,
            ReasoningEffort::Auto,
            None,
        )
        .expect("auto reasoning");
        assert_eq!(body, json!({}));

        config.supports_reasoning_effort = Some(false);
        let error = apply_reasoning_effort(
            &mut body,
            &config,
            &config.model,
            ReasoningEffort::High,
            None,
        )
        .expect_err("disabled reasoning must be rejected");
        assert!(error.to_string().contains("cannot be sent"));
        assert_eq!(body, json!({}));
    }

    #[test]
    fn infers_reasoning_support_and_respects_explicit_overrides() {
        let mut ordinary = reasoning_provider(ProviderKind::OpenAiCompatible, "gpt-4.1");
        ordinary.supports_reasoning_effort = None;
        let mut ordinary_body = json!({});
        let error = apply_reasoning_effort(
            &mut ordinary_body,
            &ordinary,
            &ordinary.model,
            ReasoningEffort::High,
            None,
        )
        .expect_err("unknown model capability must be rejected");
        assert!(error.to_string().contains("Unknown"));
        assert_eq!(ordinary_body, json!({}));

        let mut inferred = reasoning_provider(ProviderKind::OpenAiCompatible, "deepseek-v4-flash");
        inferred.base_url = "https://api.deepseek.com/v1".into();
        inferred.supports_reasoning_effort = None;
        let mut inferred_body = json!({});
        apply_reasoning_effort(
            &mut inferred_body,
            &inferred,
            &inferred.model,
            ReasoningEffort::Max,
            None,
        )
        .expect("reasoning model inference");
        assert_eq!(inferred_body["reasoning_effort"], "max");

        ordinary.supports_reasoning_effort = Some(true);
        let mut forced_body = json!({});
        apply_reasoning_effort(
            &mut forced_body,
            &ordinary,
            &ordinary.model,
            ReasoningEffort::High,
            None,
        )
        .expect("forced reasoning support");
        assert_eq!(forced_body["reasoning_effort"], "high");

        inferred.supports_reasoning_effort = Some(false);
        let mut disabled_body = json!({});
        let error = apply_reasoning_effort(
            &mut disabled_body,
            &inferred,
            &inferred.model,
            ReasoningEffort::High,
            None,
        )
        .expect_err("disabled inferred reasoning support must be rejected");
        assert!(error.to_string().contains("Unsupported"));
        assert_eq!(disabled_body, json!({}));
    }

    #[test]
    fn runtime_reasoning_falls_back_without_blocking_the_provider_request() {
        let mut config = reasoning_provider(ProviderKind::GeminiNative, "gemini-2.5-pro");
        config
            .reasoning_effort_map
            .insert("high".into(), "not-a-token-budget".into());

        assert!(reasoning_capability(&config, &config.model).is_err());
        let capability = reasoning_capability_best_effort(&config, &config.model);
        assert!(capability.fallback_reason.is_some());
        assert!(
            capability
                .levels
                .iter()
                .any(|level| level.effort == ReasoningEffort::Low)
        );
        assert!(
            capability
                .levels
                .iter()
                .all(|level| level.effort != ReasoningEffort::High)
        );

        let mut low_body = json!({});
        apply_reasoning_effort_best_effort(
            &mut low_body,
            &config,
            &config.model,
            ReasoningEffort::Low,
            None,
        )
        .expect("valid level must survive an unrelated malformed level");
        assert_eq!(
            low_body["generationConfig"]["thinkingConfig"]["thinkingBudget"],
            2_048
        );

        let mut high_body = json!({});
        apply_reasoning_effort_best_effort(
            &mut high_body,
            &config,
            &config.model,
            ReasoningEffort::High,
            None,
        )
        .expect("invalid reasoning must not fail the main request");
        assert_eq!(high_body, json!({}));
        let plan =
            reasoning_request_plan_best_effort(&config, &config.model, ReasoningEffort::High, None);
        assert_eq!(plan.control, ReasoningControlMode::Auto);
        assert!(!plan.sent);
        assert!(plan.fallback_reason.is_some());

        config.reasoning_prompt_fallback = Some(true);
        let prompt_plan =
            reasoning_request_plan_best_effort(&config, &config.model, ReasoningEffort::High, None);
        assert_eq!(prompt_plan.control, ReasoningControlMode::Prompt);
        assert!(prompt_plan.prompt_applied);
        assert!(prompt_plan.fallback_reason.is_some());
    }

    #[test]
    fn gpt_5_capability_exposes_max_and_responses_plan_sends_it() {
        let config = reasoning_provider(ProviderKind::OpenAiResponses, "gpt-5.6-luna");
        let capability = reasoning_capability(&config, &config.model).expect("GPT capability");
        assert_eq!(
            capability
                .levels
                .iter()
                .map(|level| level.effort)
                .collect::<Vec<_>>(),
            vec![
                ReasoningEffort::Low,
                ReasoningEffort::Medium,
                ReasoningEffort::High,
                ReasoningEffort::XHigh,
                ReasoningEffort::Max,
            ]
        );

        let plan =
            reasoning_request_plan(&config, &config.model, ReasoningEffort::Max, Some(16_384))
                .expect("GPT max request plan");
        assert_eq!(plan.control, ReasoningControlMode::Native);
        assert!(plan.sent);
        assert_eq!(
            plan.wire_fields,
            vec![
                ReasoningWireField {
                    path: "reasoning.effort".into(),
                    value: Value::String("max".into()),
                },
                ReasoningWireField {
                    path: "include".into(),
                    value: json!(["reasoning.encrypted_content"]),
                },
            ]
        );
    }

    #[test]
    fn claude_effort_levels_follow_model_version() {
        let mut opus_46 = reasoning_provider(ProviderKind::AnthropicMessages, "claude-opus-4-6");
        opus_46.supports_reasoning_effort = None;
        let capability = reasoning_capability(&opus_46, &opus_46.model).expect("Opus 4.6");
        assert_eq!(
            capability
                .levels
                .iter()
                .map(|level| level.effort)
                .collect::<Vec<_>>(),
            vec![
                ReasoningEffort::Low,
                ReasoningEffort::Medium,
                ReasoningEffort::High,
                ReasoningEffort::Max,
            ]
        );
        let max_plan =
            reasoning_request_plan(&opus_46, &opus_46.model, ReasoningEffort::Max, Some(16_384))
                .expect("Opus 4.6 max");
        assert_eq!(
            max_plan.wire_fields,
            vec![
                ReasoningWireField {
                    path: "thinking.type".into(),
                    value: Value::String("adaptive".into()),
                },
                ReasoningWireField {
                    path: "output_config.effort".into(),
                    value: Value::String("max".into()),
                },
            ]
        );
        assert!(
            reasoning_request_plan(
                &opus_46,
                &opus_46.model,
                ReasoningEffort::XHigh,
                Some(16_384)
            )
            .is_err()
        );

        let mut opus_47 = reasoning_provider(ProviderKind::AnthropicMessages, "claude-opus-4-7");
        opus_47.supports_reasoning_effort = None;
        let capability = reasoning_capability(&opus_47, &opus_47.model).expect("Opus 4.7");
        assert_eq!(
            capability
                .levels
                .iter()
                .map(|level| level.effort)
                .collect::<Vec<_>>(),
            vec![
                ReasoningEffort::Low,
                ReasoningEffort::Medium,
                ReasoningEffort::High,
                ReasoningEffort::XHigh,
                ReasoningEffort::Max,
            ]
        );
        let xhigh_plan = reasoning_request_plan(
            &opus_47,
            &opus_47.model,
            ReasoningEffort::XHigh,
            Some(16_384),
        )
        .expect("Opus 4.7 xhigh");
        assert_eq!(
            xhigh_plan
                .wire_fields
                .iter()
                .find(|field| field.path == "output_config.effort")
                .map(|field| &field.value),
            Some(&Value::String("xhigh".into()))
        );

        for (model, expected) in [
            (
                "claude-4-6",
                vec![
                    ReasoningEffort::Low,
                    ReasoningEffort::Medium,
                    ReasoningEffort::High,
                    ReasoningEffort::Max,
                ],
            ),
            (
                "claude-4-7",
                vec![
                    ReasoningEffort::Low,
                    ReasoningEffort::Medium,
                    ReasoningEffort::High,
                    ReasoningEffort::XHigh,
                    ReasoningEffort::Max,
                ],
            ),
        ] {
            let mut generic = reasoning_provider(ProviderKind::AnthropicMessages, model);
            generic.supports_reasoning_effort = None;
            assert_eq!(
                reasoning_capability(&generic, &generic.model)
                    .expect("generic Claude version")
                    .levels
                    .iter()
                    .map(|level| level.effort)
                    .collect::<Vec<_>>(),
                expected
            );
        }
    }

    #[test]
    fn domestic_model_rules_expose_route_specific_max_levels() {
        let mut deepseek = reasoning_provider(ProviderKind::OpenAiCompatible, "deepseek-v4-flash");
        deepseek.base_url = "https://api.deepseek.com/v1".into();
        deepseek.supports_reasoning_effort = None;
        let capability = reasoning_capability(&deepseek, &deepseek.model).expect("DeepSeek V4");
        assert_eq!(
            capability
                .levels
                .iter()
                .map(|level| level.effort)
                .collect::<Vec<_>>(),
            vec![ReasoningEffort::High, ReasoningEffort::Max]
        );
        let max_plan =
            reasoning_request_plan(&deepseek, &deepseek.model, ReasoningEffort::Max, None)
                .expect("DeepSeek V4 max");
        assert_eq!(
            max_plan.wire_fields[0],
            ReasoningWireField {
                path: "reasoning_effort".into(),
                value: Value::String("max".into()),
            }
        );

        let mut opencode = reasoning_provider(ProviderKind::OpenAiCompatible, "v4flash0731");
        opencode.base_url = "https://opencode.ai/zen/go/v1".into();
        opencode.supports_reasoning_effort = None;
        let capability = reasoning_capability(&opencode, &opencode.model).expect("OpenCode V4");
        assert_eq!(
            capability
                .levels
                .iter()
                .map(|level| level.effort)
                .collect::<Vec<_>>(),
            vec![
                ReasoningEffort::Low,
                ReasoningEffort::High,
                ReasoningEffort::Max,
            ]
        );
        let max_plan =
            reasoning_request_plan(&opencode, &opencode.model, ReasoningEffort::Max, None)
                .expect("OpenCode V4 max");
        assert_eq!(max_plan.wire_fields[0].value, Value::String("max".into()));
        assert!(max_plan.route_sensitive);

        let mut openrouter =
            reasoning_provider(ProviderKind::OpenAiCompatible, "deepseek/deepseek-v4-flash");
        openrouter.base_url = "https://openrouter.ai/api/v1".into();
        openrouter.supports_reasoning_effort = None;
        let capability =
            reasoning_capability(&openrouter, &openrouter.model).expect("OpenRouter DeepSeek V4");
        assert_eq!(
            capability
                .levels
                .iter()
                .map(|level| level.effort)
                .collect::<Vec<_>>(),
            vec![ReasoningEffort::High, ReasoningEffort::XHigh]
        );
        assert!(
            reasoning_request_plan(&openrouter, &openrouter.model, ReasoningEffort::Max, None)
                .is_err()
        );

        let mut kimi = reasoning_provider(ProviderKind::OpenAiCompatible, "kimi-k3");
        kimi.base_url = "https://api.moonshot.cn/v1".into();
        kimi.supports_reasoning_effort = None;
        let capability = reasoning_capability(&kimi, &kimi.model).expect("Kimi K3");
        assert_eq!(
            capability
                .levels
                .iter()
                .map(|level| level.effort)
                .collect::<Vec<_>>(),
            vec![
                ReasoningEffort::Low,
                ReasoningEffort::High,
                ReasoningEffort::Max,
            ]
        );
        let max_plan = reasoning_request_plan(&kimi, &kimi.model, ReasoningEffort::Max, None)
            .expect("Kimi K3 max");
        assert_eq!(max_plan.wire_fields[0].value, Value::String("max".into()));

        let mut opencode_kimi = reasoning_provider(ProviderKind::OpenAiCompatible, "k3");
        opencode_kimi.base_url = "https://opencode.ai/zen/go/v1".into();
        opencode_kimi.supports_reasoning_effort = None;
        let capability =
            reasoning_capability(&opencode_kimi, &opencode_kimi.model).expect("OpenCode Kimi K3");
        assert_eq!(
            capability
                .levels
                .iter()
                .map(|level| level.effort)
                .collect::<Vec<_>>(),
            vec![ReasoningEffort::Max]
        );
        let max_plan = reasoning_request_plan(
            &opencode_kimi,
            &opencode_kimi.model,
            ReasoningEffort::Max,
            None,
        )
        .expect("OpenCode Kimi K3 max");
        assert_eq!(max_plan.wire_fields[0].value, Value::String("max".into()));
        assert!(max_plan.route_sensitive);

        let mut kimi_messages = reasoning_provider(ProviderKind::AnthropicMessages, "k3");
        kimi_messages.supports_reasoning_effort = None;
        let max_plan = reasoning_request_plan(
            &kimi_messages,
            &kimi_messages.model,
            ReasoningEffort::Max,
            Some(16_384),
        )
        .expect("Anthropic-compatible Kimi K3 max");
        assert_eq!(
            max_plan.wire_fields,
            vec![
                ReasoningWireField {
                    path: "thinking.type".into(),
                    value: Value::String("adaptive".into()),
                },
                ReasoningWireField {
                    path: "output_config.effort".into(),
                    value: Value::String("max".into()),
                },
            ]
        );
    }

    #[test]
    fn profile_defaults_use_merged_provider_effort_map() {
        let mut config = reasoning_provider(ProviderKind::OpenAiCompatible, "custom-model");
        config
            .reasoning_effort_map
            .insert("max".into(), "maximum".into());
        config.reasoning_profiles.insert(
            "custom-model".into(),
            ReasoningProfileSettings {
                supported: Some(true),
                ..ReasoningProfileSettings::default()
            },
        );

        let capability = reasoning_capability(&config, &config.model).expect("profile capability");
        let maximum = capability
            .levels
            .iter()
            .find(|level| level.effort == ReasoningEffort::Max)
            .expect("inherited max level");
        assert_eq!(
            maximum.wire_fields[0].value,
            Value::String("maximum".into())
        );

        let ordinary = reasoning_provider(ProviderKind::OpenAiCompatible, "custom-model");
        let ordinary_capability =
            reasoning_capability(&ordinary, &ordinary.model).expect("ordinary capability");
        assert!(
            ordinary_capability
                .levels
                .iter()
                .all(|level| level.effort != ReasoningEffort::Max)
        );
    }

    #[test]
    fn prompt_fallback_is_explicit_and_not_reported_as_native_support() {
        let mut config = reasoning_provider(ProviderKind::OpenAiCompatible, "ordinary-model");
        config.supports_reasoning_effort = None;
        config.reasoning_prompt_fallback = Some(true);

        let capability = reasoning_capability(&config, &config.model).expect("capability");
        assert_eq!(capability.support, ReasoningSupport::Unknown);
        assert!(capability.prompt_fallback);
        let level = capability
            .levels
            .iter()
            .find(|level| level.effort == ReasoningEffort::High)
            .expect("prompt fallback level");
        assert_eq!(level.control, ReasoningControlMode::Prompt);
        assert!(level.wire_fields.is_empty());

        let plan = reasoning_request_plan(&config, &config.model, ReasoningEffort::High, None)
            .expect("prompt request plan");
        assert_eq!(plan.control, ReasoningControlMode::Prompt);
        assert!(!plan.sent);
        assert!(plan.prompt_applied);
        assert!(plan.wire_fields.is_empty());

        let mut messages = vec![ChatMessage::user("solve the task")];
        inject_reasoning_prompt(&mut messages, ReasoningEffort::High);
        inject_reasoning_prompt(&mut messages, ReasoningEffort::High);
        assert_eq!(messages.len(), 2);
        assert!(messages[0].content.contains(REASONING_PROMPT_MARKER));

        config.reasoning_prompt_fallback = None;
        let error = reasoning_request_plan(&config, &config.model, ReasoningEffort::High, None)
            .expect_err("unknown native support must be rejected without fallback");
        assert!(error.to_string().contains("Unknown"));
        assert!(error.to_string().contains("cannot be sent"));
    }

    #[test]
    fn exact_model_profile_overrides_wildcard_capability() {
        let mut config = reasoning_provider(ProviderKind::OpenAiCompatible, "deepseek-v4-flash");
        config.reasoning_profiles.insert(
            "*".into(),
            ReasoningProfileSettings {
                supported: Some(false),
                prompt_fallback: Some(true),
                ..ReasoningProfileSettings::default()
            },
        );
        config.reasoning_profiles.insert(
            "deepseek-v4-flash".into(),
            ReasoningProfileSettings {
                supported: Some(true),
                levels: Some(vec![ReasoningEffort::Low, ReasoningEffort::XHigh]),
                effort_map: BTreeMap::from([("xhigh".into(), "max".into())]),
                route_sensitive: Some(true),
                prompt_fallback: Some(false),
            },
        );

        let exact = reasoning_capability(&config, &config.model).expect("exact profile");
        assert_eq!(exact.support, ReasoningSupport::Supported);
        assert_eq!(exact.source, ReasoningCapabilitySource::ModelConfig);
        assert!(!exact.prompt_fallback);
        assert_eq!(exact.levels.len(), 2);
        let xhigh = exact
            .levels
            .iter()
            .find(|level| level.effort == ReasoningEffort::XHigh)
            .expect("xhigh level");
        assert_eq!(xhigh.control, ReasoningControlMode::Native);
        assert_eq!(xhigh.wire_fields[0].value, Value::String("max".into()));
        assert!(xhigh.route_sensitive);
        assert!(
            reasoning_request_plan(&config, &config.model, ReasoningEffort::Medium, None).is_err()
        );

        let wildcard = reasoning_capability(&config, "other-model").expect("wildcard profile");
        assert_eq!(wildcard.support, ReasoningSupport::Unsupported);
        assert!(wildcard.prompt_fallback);
        assert!(
            wildcard
                .levels
                .iter()
                .all(|level| level.control == ReasoningControlMode::Prompt)
        );
    }

    #[test]
    fn openrouter_uses_nested_reasoning_effort() {
        let mut config = reasoning_provider(ProviderKind::OpenAiCompatible, "openai/gpt-5.6");
        config.base_url = "https://openrouter.ai/api/v1".into();
        config.supports_reasoning_effort = None;
        let mut body = json!({});
        apply_reasoning_effort(
            &mut body,
            &config,
            &config.model,
            ReasoningEffort::High,
            None,
        )
        .expect("OpenRouter reasoning");
        assert_eq!(body["reasoning"], json!({"effort": "high"}));
        assert!(body.get("reasoning_effort").is_none());

        let mut xhigh_body = json!({});
        apply_reasoning_effort(
            &mut xhigh_body,
            &config,
            &config.model,
            ReasoningEffort::XHigh,
            None,
        )
        .expect("OpenRouter xhigh reasoning");
        assert_eq!(xhigh_body["reasoning"], json!({"effort": "xhigh"}));

        config.base_url = "https://openrouter.ai.evil.example/api/v1".into();
        let mut lookalike_body = json!({});
        apply_reasoning_effort(
            &mut lookalike_body,
            &config,
            &config.model,
            ReasoningEffort::High,
            None,
        )
        .expect("lookalike host uses generic compatible wire format");
        assert_eq!(lookalike_body["reasoning_effort"], "high");
        assert!(lookalike_body.get("reasoning").is_none());

        config.base_url = "https://api.openrouter.ai/v1".into();
        let mut subdomain_body = json!({});
        apply_reasoning_effort(
            &mut subdomain_body,
            &config,
            &config.model,
            ReasoningEffort::High,
            None,
        )
        .expect("OpenRouter subdomain");
        assert_eq!(subdomain_body["reasoning"], json!({"effort": "high"}));
    }

    #[test]
    fn maps_openai_protocols_and_deepseek_max() {
        let mut chat = reasoning_provider(ProviderKind::OpenAiCompatible, "deepseek-v4-flash");
        chat.base_url = "https://api.deepseek.com/v1".into();
        let mut chat_body = json!({});
        apply_reasoning_effort(
            &mut chat_body,
            &chat,
            &chat.model,
            ReasoningEffort::Max,
            None,
        )
        .expect("chat reasoning");
        assert_eq!(chat_body["reasoning_effort"], "max");

        let mut opencode = reasoning_provider(ProviderKind::OpenAiCompatible, "deepseek-v4-flash");
        opencode.base_url = "https://opencode.ai/zen/go/v1".into();
        let mut opencode_body = json!({});
        apply_reasoning_effort(
            &mut opencode_body,
            &opencode,
            &opencode.model,
            ReasoningEffort::Max,
            None,
        )
        .expect("OpenCode DeepSeek reasoning");
        assert_eq!(opencode_body["reasoning_effort"], "max");

        let responses = reasoning_provider(ProviderKind::OpenAiResponses, "gpt-5.6");
        let mut responses_body = json!({});
        apply_reasoning_effort(
            &mut responses_body,
            &responses,
            &responses.model,
            ReasoningEffort::High,
            None,
        )
        .expect("Responses reasoning");
        assert_eq!(responses_body["reasoning"], json!({"effort": "high"}));
    }

    #[test]
    fn maps_anthropic_adaptive_and_legacy_thinking() {
        let adaptive = reasoning_provider(ProviderKind::AnthropicMessages, "claude-sonnet-4-6");
        let mut adaptive_body = json!({});
        apply_reasoning_effort(
            &mut adaptive_body,
            &adaptive,
            &adaptive.model,
            ReasoningEffort::Max,
            Some(8_192),
        )
        .expect("adaptive thinking");
        assert_eq!(adaptive_body["thinking"], json!({"type": "adaptive"}));
        assert_eq!(adaptive_body["output_config"], json!({"effort": "max"}));

        let legacy = reasoning_provider(
            ProviderKind::AnthropicMessages,
            "claude-sonnet-4-5-20250929",
        );
        let mut legacy_body = json!({});
        apply_reasoning_effort(
            &mut legacy_body,
            &legacy,
            &legacy.model,
            ReasoningEffort::High,
            Some(8_192),
        )
        .expect("legacy thinking");
        assert_eq!(legacy_body["thinking"]["type"], "enabled");
        assert_eq!(legacy_body["thinking"]["budget_tokens"], 4_096);

        let mut old_style = reasoning_provider(
            ProviderKind::AnthropicMessages,
            "claude-3-7-sonnet-20250219",
        );
        old_style.supports_reasoning_effort = None;
        let mut old_style_body = json!({});
        apply_reasoning_effort(
            &mut old_style_body,
            &old_style,
            &old_style.model,
            ReasoningEffort::High,
            Some(8_192),
        )
        .expect("old-style Claude version");
        assert_eq!(old_style_body["thinking"]["type"], "enabled");
        assert!(old_style_body.get("output_config").is_none());

        let opus_45 = reasoning_provider(ProviderKind::AnthropicMessages, "claude-opus-4-5");
        let mut opus_45_body = json!({});
        apply_reasoning_effort(
            &mut opus_45_body,
            &opus_45,
            &opus_45.model,
            ReasoningEffort::High,
            Some(8_192),
        )
        .expect("Opus 4.5 thinking and effort");
        assert_eq!(opus_45_body["thinking"]["type"], "enabled");
        assert_eq!(opus_45_body["output_config"]["effort"], "high");

        let mut latest =
            reasoning_provider(ProviderKind::AnthropicMessages, "claude-sonnet-latest");
        latest.supports_reasoning_effort = None;
        let mut latest_body = json!({});
        apply_reasoning_effort(
            &mut latest_body,
            &latest,
            &latest.model,
            ReasoningEffort::XHigh,
            Some(8_192),
        )
        .expect("latest Claude alias");
        assert_eq!(latest_body["thinking"], json!({"type": "adaptive"}));
        assert_eq!(latest_body["output_config"]["effort"], "xhigh");
    }

    #[test]
    fn anthropic_beta_header_requires_actual_legacy_thinking_fields() {
        assert!(anthropic_interleaved_thinking_beta_enabled(
            &json!({"thinking": {"type": "enabled"}}),
            true,
        ));
        assert!(!anthropic_interleaved_thinking_beta_enabled(
            &json!({"thinking": {"type": "adaptive"}}),
            true,
        ));
        assert!(!anthropic_interleaved_thinking_beta_enabled(
            &json!({}),
            true,
        ));
        assert!(!anthropic_interleaved_thinking_beta_enabled(
            &json!({"thinking": {"type": "enabled"}}),
            false,
        ));
    }

    #[test]
    fn maps_gemini_levels_and_budgets_without_overwriting_output_limit() {
        let level = reasoning_provider(ProviderKind::GeminiNative, "gemini-3.6-flash");
        let mut level_body = json!({"generationConfig": {"maxOutputTokens": 4096}});
        apply_reasoning_effort(
            &mut level_body,
            &level,
            &level.model,
            ReasoningEffort::High,
            Some(4_096),
        )
        .expect("Gemini level");
        assert_eq!(level_body["generationConfig"]["maxOutputTokens"], 4_096);
        assert_eq!(
            level_body["generationConfig"]["thinkingConfig"]["thinkingLevel"],
            "high"
        );

        let budget = reasoning_provider(ProviderKind::GeminiNative, "gemini-2.5-pro");
        let mut budget_body = json!({});
        apply_reasoning_effort(
            &mut budget_body,
            &budget,
            &budget.model,
            ReasoningEffort::High,
            None,
        )
        .expect("Gemini budget");
        assert_eq!(
            budget_body["generationConfig"]["thinkingConfig"]["thinkingBudget"],
            16_000
        );
    }

    #[test]
    fn reasoning_rejection_retry_helpers_are_narrow_and_remove_only_reasoning_fields() {
        assert!(has_reasoning_rejection_error(
            r#"{"error":{"message":"Unknown field reasoning_effort"}}"#
        ));
        assert!(has_reasoning_rejection_error(
            r#"{"detail":"thinkingBudget is not supported"}"#
        ));
        assert!(!has_reasoning_rejection_error(
            r#"{"error":{"message":"invalid api key"}}"#
        ));

        let openai = without_reasoning_fields(
            &json!({"model":"m","reasoning_effort":"high","messages":[]}),
            ReasoningWireProtocol::OpenAiChat,
        );
        assert!(openai.get("reasoning_effort").is_none());
        assert!(openai.get("messages").is_some());

        let responses = without_reasoning_fields(
            &json!({
                "reasoning":{"effort":"high"},
                "include":["reasoning.encrypted_content", "message.output_text"]
            }),
            ReasoningWireProtocol::OpenAiResponses,
        );
        assert!(responses.get("reasoning").is_none());
        assert_eq!(responses["include"], json!(["message.output_text"]));

        let anthropic = without_reasoning_fields(
            &json!({"thinking":{"type":"adaptive"},"output_config":{"effort":"high"}}),
            ReasoningWireProtocol::Anthropic,
        );
        assert!(anthropic.get("thinking").is_none());
        assert!(anthropic.get("output_config").is_none());

        let gemini = without_reasoning_fields(
            &json!({"generationConfig":{"maxOutputTokens":1024,"thinkingConfig":{"thinkingLevel":"high"}}}),
            ReasoningWireProtocol::Gemini,
        );
        assert_eq!(gemini["generationConfig"]["maxOutputTokens"], 1024);
        assert!(gemini["generationConfig"].get("thinkingConfig").is_none());
    }

    fn spawn_truncated_sse(body: &str) -> (String, JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind SSE test server");
        let address = listener.local_addr().expect("read SSE test address");
        let body = body.as_bytes().to_vec();
        let handle = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().expect("accept SSE test request");
            let mut request = [0_u8; 2048];
            let _ = socket.read(&mut request).expect("read SSE test request");
            let declared_length = body.len() + 128;
            write!(
                socket,
                "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {declared_length}\r\nConnection: close\r\n\r\n"
            )
            .expect("write SSE test headers");
            socket.write_all(&body).expect("write SSE test body");
        });
        (format!("http://{address}"), handle)
    }

    fn spawn_clean_sse(body: &str) -> (String, JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind SSE test server");
        let address = listener.local_addr().expect("read SSE test address");
        let body = body.as_bytes().to_vec();
        let handle = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().expect("accept SSE test request");
            let mut request = [0_u8; 2048];
            let _ = socket.read(&mut request).expect("read SSE test request");
            write!(
                socket,
                "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            )
            .expect("write SSE test headers");
            socket.write_all(&body).expect("write SSE test body");
        });
        (format!("http://{address}"), handle)
    }

    fn read_http_json(socket: &mut TcpStream) -> Value {
        socket
            .set_read_timeout(Some(Duration::from_secs(5)))
            .expect("set request read timeout");
        let mut received = Vec::new();
        let mut buffer = [0_u8; 4096];
        let (header_end, content_length) = loop {
            let read = socket.read(&mut buffer).expect("read HTTP request");
            assert!(read > 0, "HTTP request ended before its body arrived");
            received.extend_from_slice(&buffer[..read]);
            let Some(header_end) = received.windows(4).position(|part| part == b"\r\n\r\n") else {
                continue;
            };
            let headers = std::str::from_utf8(&received[..header_end]).expect("UTF-8 headers");
            let content_length = headers
                .lines()
                .filter_map(|line| line.split_once(':'))
                .find(|(name, _)| name.eq_ignore_ascii_case("content-length"))
                .and_then(|(_, value)| value.trim().parse::<usize>().ok())
                .expect("request Content-Length");
            break (header_end + 4, content_length);
        };
        while received.len() < header_end + content_length {
            let read = socket.read(&mut buffer).expect("read HTTP request body");
            assert!(read > 0, "HTTP request body was truncated");
            received.extend_from_slice(&buffer[..read]);
        }
        serde_json::from_slice(&received[header_end..header_end + content_length])
            .expect("request JSON")
    }

    fn write_json_response(socket: &mut TcpStream, status: &str, body: &str) {
        write!(
            socket,
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .expect("write HTTP response");
    }

    fn spawn_missing_message_id_server() -> (String, JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind compatibility server");
        let address = listener.local_addr().expect("read compatibility address");
        let handle = std::thread::spawn(move || {
            let (mut first, _) = listener.accept().expect("accept initial request");
            let initial = read_http_json(&mut first);
            assert!(initial.pointer("/messages/2/id").is_none());
            let error = json!({
                "error": {
                    "type": "invalid_request_error",
                    "message": "Upstream request failed: messages[2]: missing field `id` at line 1 column 99"
                }
            })
            .to_string();
            write_json_response(&mut first, "400 Bad Request", &error);

            let (mut retry, _) = listener.accept().expect("accept compatibility retry");
            let fallback = read_http_json(&mut retry);
            for index in 0..4 {
                assert_eq!(
                    fallback.pointer(&format!("/messages/{index}/id")),
                    Some(&json!(format!("storydex-message-{index}")))
                );
            }
            let success = json!({
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1}
            })
            .to_string();
            write_json_response(&mut retry, "200 OK", &success);
        });
        (format!("http://{address}"), handle)
    }

    fn spawn_reasoning_rejection_server() -> (String, JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind reasoning server");
        let address = listener.local_addr().expect("read reasoning address");
        let handle = std::thread::spawn(move || {
            let (mut first, _) = listener.accept().expect("accept initial reasoning request");
            let initial = read_http_json(&mut first);
            assert_eq!(initial["reasoning_effort"], "high");
            let error = json!({
                "error": {"message": "Unknown field reasoning_effort"}
            })
            .to_string();
            write_json_response(&mut first, "400 Bad Request", &error);

            let (mut retry, _) = listener.accept().expect("accept reasoning fallback");
            let fallback = read_http_json(&mut retry);
            assert!(fallback.get("reasoning_effort").is_none());
            let success = json!({
                "choices": [{"message": {"role": "assistant", "content": "fallback-ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1}
            })
            .to_string();
            write_json_response(&mut retry, "200 OK", &success);
        });
        (format!("http://{address}"), handle)
    }

    fn spawn_chained_openai_compatibility_server() -> (String, JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind compatibility server");
        let address = listener.local_addr().expect("read compatibility address");
        let handle = std::thread::spawn(move || {
            let (mut first, _) = listener.accept().expect("accept initial request");
            let initial = read_http_json(&mut first);
            assert_eq!(initial["reasoning_effort"], "high");
            assert!(initial.pointer("/messages/0/id").is_none());
            let reasoning_error = json!({
                "error": {"message": "Unknown field reasoning_effort"}
            })
            .to_string();
            write_json_response(&mut first, "400 Bad Request", &reasoning_error);

            let (mut second, _) = listener.accept().expect("accept reasoning fallback");
            let without_reasoning = read_http_json(&mut second);
            assert!(without_reasoning.get("reasoning_effort").is_none());
            assert!(without_reasoning.pointer("/messages/0/id").is_none());
            let message_id_error = json!({
                "error": {
                    "message": "Upstream request failed: messages[0]: missing field `id`"
                }
            })
            .to_string();
            write_json_response(&mut second, "422 Unprocessable Entity", &message_id_error);

            let (mut third, _) = listener.accept().expect("accept message id fallback");
            let compatible = read_http_json(&mut third);
            assert!(compatible.get("reasoning_effort").is_none());
            assert_eq!(
                compatible.pointer("/messages/0/id"),
                Some(&json!("storydex-message-0"))
            );
            let success = json!({
                "choices": [{"message": {"role": "assistant", "content": "combined-ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1}
            })
            .to_string();
            write_json_response(&mut third, "200 OK", &success);
        });
        (format!("http://{address}"), handle)
    }

    #[tokio::test]
    async fn retries_once_without_reasoning_when_gateway_rejects_the_field() {
        let (url, server) = spawn_reasoning_rejection_server();
        let mut config = reasoning_provider(ProviderKind::OpenAiCompatible, "gpt-5.6");
        config.base_url = url;
        let provider = HttpModelProvider::new(config.clone()).expect("reasoning provider");
        let response = provider
            .complete(ModelRequest {
                model: config.model,
                messages: vec![ChatMessage::user("test")],
                tools: Vec::new(),
                max_output_tokens: None,
                required_tool: None,
                reasoning_effort: ReasoningEffort::High,
            })
            .await
            .expect("reasoning rejection fallback");
        server.join().expect("join reasoning server");
        assert_eq!(response.content, "fallback-ok");
    }

    #[tokio::test]
    async fn composes_reasoning_and_message_id_compatibility_fallbacks() {
        let (url, server) = spawn_chained_openai_compatibility_server();
        let mut config = reasoning_provider(ProviderKind::OpenAiCompatible, "gpt-5.6");
        config.base_url = url;
        let provider = HttpModelProvider::new(config.clone()).expect("compatibility provider");
        let response = provider
            .complete(ModelRequest {
                model: config.model,
                messages: vec![ChatMessage::user("test")],
                tools: Vec::new(),
                max_output_tokens: None,
                required_tool: None,
                reasoning_effort: ReasoningEffort::High,
            })
            .await
            .expect("combined compatibility fallback");
        server.join().expect("join compatibility server");
        assert_eq!(response.content, "combined-ok");
    }

    #[tokio::test]
    async fn done_marker_finishes_before_a_truncated_transport_close() {
        let (url, server) = spawn_truncated_sse(
            "data: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}\n\ndata: [DONE]\n\n",
        );
        let response = Client::new()
            .get(url)
            .send()
            .await
            .expect("request SSE stream");
        let mut values = Vec::new();

        read_sse(response, |value| {
            values.push(value);
            Ok(())
        })
        .await
        .expect("terminal marker should finish the stream");
        server.join().expect("join SSE test server");

        assert_eq!(values.len(), 1);
        assert_eq!(
            values[0].pointer("/choices/0/delta/content"),
            Some(&json!("ok"))
        );
    }

    #[tokio::test]
    async fn responses_completed_finishes_before_a_truncated_transport_close() {
        let (url, server) = spawn_truncated_sse(
            "data: {\"type\":\"response.output_text.delta\",\"delta\":\"ok\"}\n\ndata: {\"type\":\"response.completed\",\"response\":{\"usage\":{}}}\n\n",
        );
        let response = Client::new()
            .get(url)
            .send()
            .await
            .expect("request SSE stream");
        let mut values = Vec::new();

        read_sse(response, |value| {
            values.push(value);
            Ok(())
        })
        .await
        .expect("completed event should finish the stream");
        server.join().expect("join SSE test server");

        assert_eq!(values.len(), 2);
        assert_eq!(values[1].get("type"), Some(&json!("response.completed")));
    }

    #[tokio::test]
    async fn truncated_stream_without_a_terminal_marker_remains_an_error() {
        let (url, server) =
            spawn_truncated_sse("data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n");
        let response = Client::new()
            .get(url)
            .send()
            .await
            .expect("request SSE stream");

        let error = read_sse(response, |_| Ok(()))
            .await
            .expect_err("incomplete stream must not be accepted");
        server.join().expect("join SSE test server");

        let detail = format!("{error:#}");
        assert!(detail.starts_with("provider stream failed: "));
        assert!(detail.split(": ").count() >= 3);
    }

    #[test]
    fn message_id_compatibility_matches_known_error_shapes_only() {
        let body = json!({
            "error": {
                "message": "messages[2]: missing field `id`; messages[4].tool_calls[0]: missing field `id`"
            }
        })
        .to_string();
        assert!(has_missing_openai_message_id_error(&body));
        assert!(has_missing_openai_message_id_error(
            &json!({
                "detail": [{
                    "loc": ["body", "messages", 2, "id"],
                    "msg": "Field required",
                    "type": "missing"
                }]
            })
            .to_string()
        ));
        assert!(has_missing_openai_message_id_error(
            "validation failed at messages.2.id: Field required"
        ));
        assert!(!has_missing_openai_message_id_error(
            "messages[4].tool_calls[0]: missing field `id`"
        ));
        assert!(!has_missing_openai_message_id_error(
            "unrelated bad request"
        ));
    }

    #[tokio::test]
    async fn clean_stream_requires_a_semantic_completion_event() {
        let (url, server) =
            spawn_clean_sse("data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n");
        let response = Client::new()
            .get(url)
            .send()
            .await
            .expect("request SSE stream");
        let error = read_sse(response, |_| Ok(()))
            .await
            .expect_err("clean EOF without a completion event must fail");
        server.join().expect("join SSE test server");
        assert!(format!("{error:#}").contains("stream ended before"));
    }

    #[tokio::test]
    async fn finish_reason_allows_gateways_that_omit_done_marker() {
        let (url, server) = spawn_clean_sse(
            "data: {\"choices\":[{\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\n",
        );
        let response = Client::new()
            .get(url)
            .send()
            .await
            .expect("request SSE stream");
        let mut values = Vec::new();
        read_sse(response, |value| {
            values.push(value);
            Ok(())
        })
        .await
        .expect("finish_reason is a semantic completion event");
        server.join().expect("join SSE test server");
        assert_eq!(values.len(), 1);
    }

    #[tokio::test]
    async fn retries_an_explicit_missing_openai_message_id_once() {
        let (base_url, server) = spawn_missing_message_id_server();
        let provider = HttpModelProvider::new(ProviderConfig {
            id: "compat".into(),
            kind: ProviderKind::OpenAiCompatible,
            display: "Compat".into(),
            api_key: "test-key".into(),
            base_url,
            model: "test-model".into(),
            fast_model: None,
            capabilities: ModelCapabilities::default(),
            supports_reasoning_effort: Some(true),
            reasoning_prompt_fallback: None,
            reasoning_effort_map: BTreeMap::new(),
            reasoning_profiles: BTreeMap::new(),
            remote_compaction_mode: RemoteCompactionMode::default(),
        })
        .expect("compatibility provider");
        let response = provider
            .openai_compatible(ModelRequest {
                model: "test-model".into(),
                messages: vec![
                    ChatMessage::system("system"),
                    ChatMessage::user("question"),
                    ChatMessage::assistant("prior answer", Vec::new()),
                    ChatMessage::user("follow-up"),
                ],
                tools: Vec::new(),
                max_output_tokens: None,
                required_tool: None,
                reasoning_effort: ReasoningEffort::Auto,
            })
            .await
            .expect("message ID compatibility retry");
        server.join().expect("join compatibility server");
        assert_eq!(response.content, "ok");
    }

    #[derive(Default)]
    struct CountingRetryObserver {
        retries: std::sync::Arc<std::sync::Mutex<usize>>,
        reset_text_characters: std::sync::Arc<std::sync::Mutex<Vec<usize>>>,
        stream_events: std::sync::Arc<std::sync::Mutex<Vec<ProviderStreamEvent>>>,
    }

    impl ModelStreamObserver for CountingRetryObserver {
        fn on_text_delta(&self, _delta: &str) {}

        fn on_reasoning_delta(&self, _delta: &str) {}

        fn on_provider_stream(&self, event: &ProviderStreamEvent) {
            self.stream_events
                .lock()
                .expect("provider stream event lock")
                .push(event.clone());
        }

        fn on_provider_retry(
            &self,
            _attempt: usize,
            _max_attempts: usize,
            reset_text_characters: usize,
        ) {
            *self.retries.lock().expect("retry counter lock") += 1;
            self.reset_text_characters
                .lock()
                .expect("reset character lock")
                .push(reset_text_characters);
        }
    }

    fn spawn_retry_then_success() -> (String, JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind retry test server");
        let address = listener.local_addr().expect("read retry test address");
        let handle = std::thread::spawn(move || {
            // First connection: clean EOF with no terminal marker and no
            // output — the exact "stream ended before [DONE]..." failure a
            // stalled gateway produces before emitting anything.
            {
                let (mut socket, _) = listener.accept().expect("accept first request");
                let mut request = [0_u8; 2048];
                let _ = socket.read(&mut request).expect("read first request");
                write!(
                    socket,
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                .expect("write first headers");
            }
            // Second connection: a well-formed stream with finish_reason + [DONE].
            {
                let (mut socket, _) = listener.accept().expect("accept second request");
                let mut request = [0_u8; 2048];
                let _ = socket.read(&mut request).expect("read second request");
                let body = concat!(
                    "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"ok\"},\"finish_reason\":null}]}\n\n",
                    "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
                    "data: [DONE]\n\n",
                );
                write!(
                    socket,
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                )
                .expect("write second headers");
                socket
                    .write_all(body.as_bytes())
                    .expect("write second body");
            }
        });
        (format!("http://{address}"), handle)
    }

    #[tokio::test]
    async fn retries_truncated_stream_and_surfaces_retry_notice() {
        let (base_url, server) = spawn_retry_then_success();
        let provider = HttpModelProvider::new(ProviderConfig {
            id: "retry".into(),
            kind: ProviderKind::OpenAiCompatible,
            display: "Retry".into(),
            api_key: "test-key".into(),
            base_url,
            model: "test-model".into(),
            fast_model: None,
            capabilities: ModelCapabilities::default(),
            supports_reasoning_effort: None,
            reasoning_prompt_fallback: None,
            reasoning_effort_map: BTreeMap::new(),
            reasoning_profiles: BTreeMap::new(),
            remote_compaction_mode: RemoteCompactionMode::default(),
        })
        .expect("retry provider");
        let observer = CountingRetryObserver::default();
        let response = provider
            .openai_compatible_stream(
                ModelRequest {
                    model: "test-model".into(),
                    messages: vec![ChatMessage::user("hi")],
                    tools: Vec::new(),
                    max_output_tokens: None,
                    required_tool: None,
                    reasoning_effort: ReasoningEffort::Auto,
                },
                &observer,
            )
            .await
            .expect("truncated stream should be retried and succeed");
        server.join().expect("join retry test server");
        assert_eq!(response.content, "ok");
        assert_eq!(
            *observer.retries.lock().expect("retry counter lock"),
            1,
            "observer must be notified exactly once for the retry"
        );
        let stream_events = observer
            .stream_events
            .lock()
            .expect("provider stream event lock");
        let second_attempt = stream_events
            .iter()
            .filter(|event| event.attempt == 2)
            .collect::<Vec<_>>();
        assert_eq!(
            second_attempt
                .iter()
                .map(|event| event.phase)
                .collect::<Vec<_>>(),
            vec![
                ProviderStreamPhase::RequestStarted,
                ProviderStreamPhase::ResponseHead,
                ProviderStreamPhase::FirstByte,
                ProviderStreamPhase::FirstEvent,
                ProviderStreamPhase::Completed,
            ]
        );
        let completed = second_attempt.last().expect("completed stream event");
        assert_eq!(completed.http_status, 200);
        assert_eq!(
            completed.max_output_tokens,
            PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS
        );
        assert!(completed.request_bytes > 0);
        assert!(completed.response_bytes > 0);
    }

    fn spawn_content_then_truncate_then_success() -> (String, JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind reset-retry test server");
        let address = listener
            .local_addr()
            .expect("read reset-retry test address");
        let handle = std::thread::spawn(move || {
            {
                let (mut socket, _) = listener.accept().expect("accept first request");
                let mut request = [0_u8; 2048];
                let _ = socket.read(&mut request).expect("read first request");
                let body = b"data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"partial-answer\"},\"finish_reason\":null}]}\n\n";
                write!(
                    socket,
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                )
                .expect("write first headers");
                socket.write_all(body).expect("write first body");
            }
            {
                let (mut socket, _) = listener.accept().expect("accept replacement request");
                let mut request = [0_u8; 2048];
                let _ = socket.read(&mut request).expect("read replacement request");
                let body = concat!(
                    "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"replacement\"},\"finish_reason\":null}]}\n\n",
                    "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
                    "data: [DONE]\n\n",
                );
                write!(
                    socket,
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                )
                .expect("write replacement headers");
                socket
                    .write_all(body.as_bytes())
                    .expect("write replacement body");
            }
        });
        (format!("http://{address}"), handle)
    }

    #[tokio::test]
    async fn retries_and_resets_stream_that_already_produced_output() {
        let (base_url, server) = spawn_content_then_truncate_then_success();
        let provider = HttpModelProvider::new(ProviderConfig {
            id: "no-retry".into(),
            kind: ProviderKind::OpenAiCompatible,
            display: "NoRetry".into(),
            api_key: "test-key".into(),
            base_url,
            model: "test-model".into(),
            fast_model: None,
            capabilities: ModelCapabilities::default(),
            supports_reasoning_effort: None,
            reasoning_prompt_fallback: None,
            reasoning_effort_map: BTreeMap::new(),
            reasoning_profiles: BTreeMap::new(),
            remote_compaction_mode: RemoteCompactionMode::default(),
        })
        .expect("no-retry provider");
        let observer = CountingRetryObserver::default();
        let response = provider
            .openai_compatible_stream(
                ModelRequest {
                    model: "test-model".into(),
                    messages: vec![ChatMessage::user("hi")],
                    tools: Vec::new(),
                    max_output_tokens: None,
                    required_tool: None,
                    reasoning_effort: ReasoningEffort::Auto,
                },
                &observer,
            )
            .await
            .expect("partial output should be reset before the replacement attempt");
        server.join().expect("join reset-retry test server");
        assert_eq!(response.content, "replacement");
        assert_eq!(
            *observer.retries.lock().expect("retry counter lock"),
            1,
            "observer must be notified when partial output is replaced"
        );
        assert_eq!(
            *observer
                .reset_text_characters
                .lock()
                .expect("reset character lock"),
            vec!["partial-answer".chars().count()]
        );
    }

    #[test]
    fn rejects_non_object_tool_arguments() {
        assert!(parse_arguments(&Value::String("[]".into())).is_err());
    }

    fn spawn_invalid_args_then_success() -> (String, JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind invalid-args test server");
        let address = listener
            .local_addr()
            .expect("read invalid-args test address");
        let handle = std::thread::spawn(move || {
            // First connection: completes normally ([DONE]) but the tool
            // arguments are truncated mid-JSON — exactly what a gateway that
            // cuts a long argument stream but still emits a terminal marker
            // leaves behind ("tool arguments are not valid JSON").
            {
                let (mut socket, _) = listener.accept().expect("accept first request");
                let mut request = [0_u8; 2048];
                let _ = socket.read(&mut request).expect("read first request");
                let body = concat!(
                    "data: {\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call-1\",\"function\":{\"name\":\"write_file\",\"arguments\":\"{\\\"path\\\":\\\"x\\\"\"}}]},\"finish_reason\":null}]}\n\n",
                    "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
                    "data: [DONE]\n\n",
                );
                write!(
                    socket,
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                )
                .expect("write first headers");
                socket.write_all(body.as_bytes()).expect("write first body");
            }
            // Second connection: valid JSON arguments.
            {
                let (mut socket, _) = listener.accept().expect("accept second request");
                let mut request = [0_u8; 2048];
                let bytes = socket.read(&mut request).expect("read second request");
                let request_text = String::from_utf8_lossy(&request[..bytes]);
                assert!(request_text.contains("\"max_tokens\":32768"));
                let body = concat!(
                    "data: {\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call-1\",\"function\":{\"name\":\"write_file\",\"arguments\":\"{\\\"path\\\":\\\"x\\\",\\\"content\\\":\\\"ok\\\"}\"}}]},\"finish_reason\":null}]}\n\n",
                    "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
                    "data: [DONE]\n\n",
                );
                write!(
                    socket,
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                )
                .expect("write second headers");
                socket
                    .write_all(body.as_bytes())
                    .expect("write second body");
            }
        });
        (format!("http://{address}"), handle)
    }

    #[tokio::test]
    async fn retries_invalid_tool_arguments_when_no_text_streamed() {
        let (base_url, server) = spawn_invalid_args_then_success();
        let provider = HttpModelProvider::new(ProviderConfig {
            id: "invalid-args".into(),
            kind: ProviderKind::OpenAiCompatible,
            display: "InvalidArgs".into(),
            api_key: "test-key".into(),
            base_url,
            model: "test-model".into(),
            fast_model: None,
            capabilities: ModelCapabilities::default(),
            supports_reasoning_effort: None,
            reasoning_prompt_fallback: None,
            reasoning_effort_map: BTreeMap::new(),
            reasoning_profiles: BTreeMap::new(),
            remote_compaction_mode: RemoteCompactionMode::default(),
        })
        .expect("invalid-args provider");
        let observer = CountingRetryObserver::default();
        let response = provider
            .openai_compatible_stream(
                ModelRequest {
                    model: "test-model".into(),
                    messages: vec![ChatMessage::user("write a file")],
                    tools: Vec::new(),
                    max_output_tokens: None,
                    required_tool: None,
                    reasoning_effort: ReasoningEffort::Auto,
                },
                &observer,
            )
            .await
            .expect("invalid tool arguments should be retried and succeed");
        server.join().expect("join invalid-args test server");
        assert_eq!(response.tool_calls.len(), 1);
        assert_eq!(response.tool_calls[0].name, "write_file");
        assert_eq!(
            response.tool_calls[0].arguments,
            json!({"path": "x", "content": "ok"})
        );
        assert_eq!(
            *observer.retries.lock().expect("retry counter lock"),
            1,
            "observer must be notified exactly once for the retry"
        );
    }

    fn spawn_length_truncated_then_success() -> (String, JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind length-truncate test server");
        let address = listener
            .local_addr()
            .expect("read length-truncate test address");
        let handle = std::thread::spawn(move || {
            // First connection: ends with finish_reason=length after a tool-call
            // delta and no text — the gateway-side truncation of a long
            // argument stream. The arguments happen to parse, but the stream
            // was cut short, so it must still be retried.
            {
                let (mut socket, _) = listener.accept().expect("accept first request");
                let mut request = [0_u8; 2048];
                let _ = socket.read(&mut request).expect("read first request");
                let body = concat!(
                    "data: {\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call-1\",\"function\":{\"name\":\"write_file\",\"arguments\":\"{\\\"path\\\":\\\"x\\\"}\"}}]},\"finish_reason\":null}]}\n\n",
                    "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"length\"}]}\n\n",
                );
                write!(
                    socket,
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                )
                .expect("write first headers");
                socket.write_all(body.as_bytes()).expect("write first body");
            }
            // Second connection: normal completion.
            {
                let (mut socket, _) = listener.accept().expect("accept second request");
                let mut request = [0_u8; 2048];
                let _ = socket.read(&mut request).expect("read second request");
                let body = concat!(
                    "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"ok\"},\"finish_reason\":null}]}\n\n",
                    "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
                    "data: [DONE]\n\n",
                );
                write!(
                    socket,
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                )
                .expect("write second headers");
                socket
                    .write_all(body.as_bytes())
                    .expect("write second body");
            }
        });
        (format!("http://{address}"), handle)
    }

    #[tokio::test]
    async fn retries_truncated_tool_call_stream_on_length_finish() {
        let (base_url, server) = spawn_length_truncated_then_success();
        let provider = HttpModelProvider::new(ProviderConfig {
            id: "length-truncate".into(),
            kind: ProviderKind::OpenAiCompatible,
            display: "LengthTruncate".into(),
            api_key: "test-key".into(),
            base_url,
            model: "test-model".into(),
            fast_model: None,
            capabilities: ModelCapabilities::default(),
            supports_reasoning_effort: None,
            reasoning_prompt_fallback: None,
            reasoning_effort_map: BTreeMap::new(),
            reasoning_profiles: BTreeMap::new(),
            remote_compaction_mode: RemoteCompactionMode::default(),
        })
        .expect("length-truncate provider");
        let observer = CountingRetryObserver::default();
        let response = provider
            .openai_compatible_stream(
                ModelRequest {
                    model: "test-model".into(),
                    messages: vec![ChatMessage::user("write a file")],
                    tools: Vec::new(),
                    max_output_tokens: None,
                    required_tool: None,
                    reasoning_effort: ReasoningEffort::Auto,
                },
                &observer,
            )
            .await
            .expect("truncated tool-call stream should be retried and succeed");
        server.join().expect("join length-truncate test server");
        assert_eq!(response.content, "ok");
        assert_eq!(
            *observer.retries.lock().expect("retry counter lock"),
            1,
            "finish_reason=length tool-call stream must notify the observer once"
        );
    }

    fn spawn_reasoning_truncated_then_success() -> (String, JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind reasoning-truncate server");
        let address = listener
            .local_addr()
            .expect("read reasoning-truncate address");
        let handle = std::thread::spawn(move || {
            {
                let (mut socket, _) = listener
                    .accept()
                    .expect("accept truncated reasoning request");
                let mut request = [0_u8; 4096];
                let _ = socket
                    .read(&mut request)
                    .expect("read truncated reasoning request");
                let body = concat!(
                    "data: {\"choices\":[{\"index\":0,\"delta\":{\"reasoning_content\":\"private reasoning only\"},\"finish_reason\":null}]}\n\n",
                    "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"length\"}]}\n\n",
                );
                write!(
                    socket,
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                )
                .expect("write truncated reasoning headers");
                socket
                    .write_all(body.as_bytes())
                    .expect("write truncated reasoning body");
            }
            {
                let (mut socket, _) = listener.accept().expect("accept replacement request");
                let mut request = [0_u8; 4096];
                let bytes = socket.read(&mut request).expect("read replacement request");
                let request_text = String::from_utf8_lossy(&request[..bytes]);
                assert!(request_text.contains("\"max_tokens\":32768"));
                let body = concat!(
                    "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"visible answer\"},\"finish_reason\":null}]}\n\n",
                    "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
                    "data: [DONE]\n\n",
                );
                write!(
                    socket,
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                )
                .expect("write replacement headers");
                socket
                    .write_all(body.as_bytes())
                    .expect("write replacement body");
            }
        });
        (format!("http://{address}"), handle)
    }

    #[tokio::test]
    async fn retries_empty_reasoning_truncation_with_a_larger_budget() {
        let (base_url, server) = spawn_reasoning_truncated_then_success();
        let provider = HttpModelProvider::new(ProviderConfig {
            id: "reasoning-truncate".into(),
            kind: ProviderKind::OpenAiCompatible,
            display: "ReasoningTruncate".into(),
            api_key: "test-key".into(),
            base_url,
            model: "test-model".into(),
            fast_model: None,
            capabilities: ModelCapabilities::default(),
            supports_reasoning_effort: None,
            reasoning_prompt_fallback: None,
            reasoning_effort_map: BTreeMap::new(),
            reasoning_profiles: BTreeMap::new(),
            remote_compaction_mode: RemoteCompactionMode::default(),
        })
        .expect("reasoning-truncate provider");
        let observer = CountingRetryObserver::default();
        let response = provider
            .openai_compatible_stream(
                ModelRequest {
                    model: "test-model".into(),
                    messages: vec![ChatMessage::user("continue")],
                    tools: Vec::new(),
                    max_output_tokens: None,
                    required_tool: None,
                    reasoning_effort: ReasoningEffort::Auto,
                },
                &observer,
            )
            .await
            .expect("empty truncated reasoning should retry and return visible output");
        server.join().expect("join reasoning-truncate server");
        assert_eq!(response.content, "visible answer");
        assert_eq!(*observer.retries.lock().expect("retry counter lock"), 1);
        assert_eq!(
            *observer
                .reset_text_characters
                .lock()
                .expect("reset character lock"),
            vec![0]
        );
    }

    fn spawn_text_then_invalid_args_then_success() -> (String, JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind text-then-invalid server");
        let address = listener
            .local_addr()
            .expect("read text-then-invalid address");
        let handle = std::thread::spawn(move || {
            // First connection: streams visible text, then a tool call whose
            // arguments are invalid JSON. Even though text was streamed, the
            // arguments parse failure is retryable — the tool never ran.
            {
                let (mut socket, _) = listener.accept().expect("accept first request");
                let mut request = [0_u8; 2048];
                let _ = socket.read(&mut request).expect("read first request");
                let body = concat!(
                    "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"partial\"},\"finish_reason\":null}]}\n\n",
                    "data: {\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call-1\",\"function\":{\"name\":\"write_file\",\"arguments\":\"{\\\"path\\\":\\\"x\\\"\"}}]},\"finish_reason\":null}]}\n\n",
                    "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
                    "data: [DONE]\n\n",
                );
                write!(
                    socket,
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                )
                .expect("write first headers");
                socket.write_all(body.as_bytes()).expect("write first body");
            }
            // Second connection: valid JSON arguments.
            {
                let (mut socket, _) = listener.accept().expect("accept second request");
                let mut request = [0_u8; 2048];
                let _ = socket.read(&mut request).expect("read second request");
                let body = concat!(
                    "data: {\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call-1\",\"function\":{\"name\":\"write_file\",\"arguments\":\"{\\\"path\\\":\\\"x\\\",\\\"content\\\":\\\"ok\\\"}\"}}]},\"finish_reason\":null}]}\n\n",
                    "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
                    "data: [DONE]\n\n",
                );
                write!(
                    socket,
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                )
                .expect("write second headers");
                socket
                    .write_all(body.as_bytes())
                    .expect("write second body");
            }
        });
        (format!("http://{address}"), handle)
    }

    #[tokio::test]
    async fn retries_invalid_tool_arguments_even_after_text_was_streamed() {
        let (base_url, server) = spawn_text_then_invalid_args_then_success();
        let provider = HttpModelProvider::new(ProviderConfig {
            id: "text-then-invalid".into(),
            kind: ProviderKind::OpenAiCompatible,
            display: "TextThenInvalid".into(),
            api_key: "test-key".into(),
            base_url,
            model: "test-model".into(),
            fast_model: None,
            capabilities: ModelCapabilities::default(),
            supports_reasoning_effort: None,
            reasoning_prompt_fallback: None,
            reasoning_effort_map: BTreeMap::new(),
            reasoning_profiles: BTreeMap::new(),
            remote_compaction_mode: RemoteCompactionMode::default(),
        })
        .expect("text-then-invalid provider");
        let observer = CountingRetryObserver::default();
        let response = provider
            .openai_compatible_stream(
                ModelRequest {
                    model: "test-model".into(),
                    messages: vec![ChatMessage::user("write a file")],
                    tools: Vec::new(),
                    max_output_tokens: None,
                    required_tool: None,
                    reasoning_effort: ReasoningEffort::Auto,
                },
                &observer,
            )
            .await
            .expect("invalid arguments after visible text should be retried and succeed");
        server.join().expect("join text-then-invalid test server");
        assert_eq!(response.tool_calls.len(), 1);
        assert_eq!(
            response.tool_calls[0].arguments,
            json!({"path": "x", "content": "ok"})
        );
        assert_eq!(
            *observer.retries.lock().expect("retry counter lock"),
            1,
            "observer must be notified exactly once for the retry"
        );
    }

    #[test]
    fn preserves_openai_tool_history() {
        let messages = vec![ChatMessage::assistant(
            "",
            vec![ToolCall {
                id: "call-1".into(),
                name: "read_file".into(),
                arguments: json!({"path": "README.md"}),
            }],
        )];
        let rendered = openai_messages(&messages).expect("render messages");
        assert_eq!(
            rendered[0].pointer("/tool_calls/0/function/name"),
            Some(&Value::String("read_file".into()))
        );
    }

    #[test]
    fn skips_reasoning_only_openai_assistant_history() {
        let mut assistant = ChatMessage::assistant("", Vec::new());
        assistant.provider_items.push(json!({
            "role": "assistant",
            "content": null,
            "reasoning_content": "opaque reasoning without a deliverable message"
        }));

        let rendered = openai_messages(&[assistant]).expect("render messages");
        assert!(rendered.is_empty());
    }

    #[test]
    fn skips_empty_openai_assistant_history_without_provider_items() {
        let rendered =
            openai_messages(&[ChatMessage::assistant("", Vec::new())]).expect("render messages");
        assert!(rendered.is_empty());
    }

    #[test]
    fn replays_native_reasoning_items_across_tool_rounds() {
        let mut openai = ChatMessage::assistant(
            "",
            vec![ToolCall {
                id: "call-1".into(),
                name: "read_file".into(),
                arguments: json!({"path": "README.md"}),
            }],
        );
        openai.provider_items.push(json!({
            "role": "assistant",
            "content": null,
            "reasoning_content": "opaque reasoning",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{\"path\":\"README.md\"}"}
            }]
        }));
        let chat = openai_messages(&[openai]).expect("OpenAI chat history");
        assert_eq!(chat[0]["reasoning_content"], "opaque reasoning");
        assert_eq!(
            chat[0].pointer("/tool_calls/0/function/name"),
            Some(&Value::String("read_file".into()))
        );

        let mut anthropic = ChatMessage::assistant(
            "",
            vec![ToolCall {
                id: "tool-1".into(),
                name: "read_file".into(),
                arguments: json!({"path": "README.md"}),
            }],
        );
        anthropic.provider_items = vec![
            json!({"type": "thinking", "thinking": "summary", "signature": "signed"}),
            json!({"type": "tool_use", "id": "tool-1", "name": "read_file", "input": {"path": "README.md"}}),
        ];
        let (_, anthropic_history) = anthropic_messages(&[anthropic]).expect("Anthropic history");
        assert_eq!(anthropic_history[0]["content"][0]["signature"], "signed");

        let mut gemini = ChatMessage::assistant(
            "",
            vec![ToolCall {
                id: "gemini-call-0".into(),
                name: "read_file".into(),
                arguments: json!({"path": "README.md"}),
            }],
        );
        gemini.provider_items = vec![json!({
            "functionCall": {"name": "read_file", "args": {"path": "README.md"}},
            "thoughtSignature": "signed"
        })];
        let tool_result = ChatMessage::tool("gemini-call-0", "success: file");
        let (_, gemini_history) = gemini_messages(&[gemini, tool_result]).expect("Gemini history");
        assert_eq!(gemini_history[0]["parts"][0]["thoughtSignature"], "signed");
        assert_eq!(
            gemini_history[1]["parts"][0]["functionResponse"]["name"],
            "read_file"
        );
    }

    #[test]
    fn incompatible_provider_items_fall_back_to_generic_messages() {
        let mut responses_assistant = ChatMessage::assistant("answer", Vec::new());
        responses_assistant.provider_items = vec![json!({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "answer"}]
        })];
        let chat = openai_messages(&[responses_assistant]).expect("Chat history fallback");
        assert_eq!(
            chat,
            vec![json!({"role": "assistant", "content": "answer"})]
        );

        let mut chat_assistant = ChatMessage::assistant("answer", Vec::new());
        chat_assistant.provider_items = vec![json!({
            "role": "assistant",
            "content": "answer",
            "reasoning_content": "opaque reasoning"
        })];

        let responses =
            responses_input(&[chat_assistant.clone()]).expect("Responses history fallback");
        assert_eq!(
            responses,
            vec![json!({"role": "assistant", "content": "answer"})]
        );

        let (_, anthropic) =
            anthropic_messages(&[chat_assistant.clone()]).expect("Anthropic history fallback");
        assert_eq!(
            anthropic,
            vec![json!({
                "role": "assistant",
                "content": [{"type": "text", "text": "answer"}]
            })]
        );

        let (_, gemini) = gemini_messages(&[chat_assistant]).expect("Gemini history fallback");
        assert_eq!(
            gemini,
            vec![json!({"role": "model", "parts": [{"text": "answer"}]})]
        );
    }

    #[test]
    fn chat_stream_merges_tool_fragments_without_indexes() {
        let mut state = ChatStreamState::default();
        for value in [
            json!({
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "id": "call-1",
                            "function": {"name": "read_file", "arguments": ""}
                        }]
                    }
                }]
            }),
            json!({
                "choices": [{
                    "delta": {
                        "tool_calls": [{"function": {"arguments": "{\"path\":"}}]
                    }
                }]
            }),
            json!({
                "choices": [{
                    "delta": {
                        "tool_calls": [{"function": {"arguments": "\"README.md\"}"}}]
                    }
                }]
            }),
        ] {
            state
                .consume(&value, &TestStreamObserver)
                .expect("consume chat stream fragment");
        }

        let response = state.finish().expect("finish chat stream");
        assert_eq!(response.tool_calls.len(), 1);
        assert_eq!(response.tool_calls[0].id, "call-1");
        assert_eq!(response.tool_calls[0].name, "read_file");
        assert_eq!(
            response.tool_calls[0].arguments,
            json!({"path": "README.md"})
        );
    }

    #[test]
    fn chat_stream_preserves_openrouter_reasoning_details() {
        let mut state = ChatStreamState::default();
        for value in [
            json!({
                "choices": [{
                    "delta": {
                        "reasoning_details": [{
                            "type": "reasoning.text",
                            "text": "first",
                            "id": "reasoning-1",
                            "format": "anthropic-claude-v1",
                            "index": 0
                        }]
                    }
                }]
            }),
            json!({
                "choices": [{
                    "delta": {
                        "reasoning_details": [{
                            "type": "reasoning.encrypted",
                            "data": "opaque",
                            "id": "reasoning-2",
                            "format": "anthropic-claude-v1",
                            "index": 1
                        }],
                        "content": "answer"
                    }
                }]
            }),
        ] {
            state
                .consume(&value, &TestStreamObserver)
                .expect("consume OpenRouter reasoning fragment");
        }

        let response = state.finish().expect("finish OpenRouter stream");
        let details = response.provider_items[0]["reasoning_details"]
            .as_array()
            .expect("reasoning details");
        assert_eq!(details.len(), 2);
        assert_eq!(details[0]["text"], "first");
        assert_eq!(details[1]["data"], "opaque");
        assert_eq!(response.content, "answer");
    }

    #[test]
    fn chat_stream_accepts_cumulative_tool_arguments() {
        let mut state = ChatStreamState::default();
        for value in [
            json!({
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "id": "call-1",
                            "function": {"name": "read_file", "arguments": "{\"path\":"}
                        }]
                    }
                }]
            }),
            json!({
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "id": "call-1",
                            "function": {
                                "name": "read_file",
                                "arguments": "{\"path\":\"README.md\"}"
                            }
                        }]
                    }
                }]
            }),
        ] {
            state
                .consume(&value, &TestStreamObserver)
                .expect("consume cumulative chat stream fragment");
        }

        let response = state.finish().expect("finish cumulative chat stream");
        assert_eq!(response.tool_calls.len(), 1);
        assert_eq!(
            response.tool_calls[0].arguments,
            json!({"path": "README.md"})
        );
    }

    #[test]
    fn chat_stream_preserves_increment_that_matches_the_json_prefix() {
        let mut state = ChatStreamState::default();
        for value in [
            json!({
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call-1",
                            "function": {
                                "name": "write_file",
                                "arguments": "{\"content\":\"before "
                            }
                        }]
                    }
                }]
            }),
            json!({
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "function": {"arguments": "{"}
                        }]
                    }
                }]
            }),
            json!({
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "function": {"arguments": " after\",\"path\":\"chapter.md\"}"}
                        }]
                    }
                }]
            }),
        ] {
            state
                .consume(&value, &TestStreamObserver)
                .expect("consume chat stream fragment");
        }

        let response = state.finish().expect("finish incremental chat stream");
        assert_eq!(
            response.tool_calls[0].arguments,
            json!({"content": "before { after", "path": "chapter.md"})
        );
    }

    #[test]
    fn responses_stream_merges_item_and_call_id_fragments() {
        let mut state = ResponsesStreamState::default();
        for value in [
            json!({
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "fc-1",
                    "call_id": "call-1",
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": ""
                }
            }),
            json!({
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": "fc-1",
                "delta": "{\"path\":\"README.md\"}"
            }),
            json!({
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "fc-1",
                    "call_id": "call-1",
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": "{\"path\":\"README.md\"}"
                }
            }),
        ] {
            state
                .consume(&value, &TestStreamObserver)
                .expect("consume Responses stream event");
        }

        let response = state.finish().expect("finish Responses stream");
        assert_eq!(response.tool_calls.len(), 1);
        assert_eq!(response.tool_calls[0].id, "call-1");
        assert_eq!(response.tool_calls[0].name, "read_file");
        assert_eq!(
            response.tool_calls[0].arguments,
            json!({"path": "README.md"})
        );
    }

    #[test]
    fn joins_endpoint_without_duplicate_suffix() {
        assert_eq!(
            endpoint("https://example.test/v1", "responses"),
            "https://example.test/v1/responses"
        );
        assert_eq!(
            endpoint("https://example.test/v1/responses", "responses"),
            "https://example.test/v1/responses"
        );
    }

    #[test]
    fn retries_only_transient_provider_statuses() {
        for status in [
            StatusCode::REQUEST_TIMEOUT,
            StatusCode::TOO_MANY_REQUESTS,
            StatusCode::INTERNAL_SERVER_ERROR,
            StatusCode::BAD_GATEWAY,
            StatusCode::SERVICE_UNAVAILABLE,
            StatusCode::GATEWAY_TIMEOUT,
        ] {
            assert!(retryable_provider_status(status));
        }
        assert!(!retryable_provider_status(StatusCode::BAD_REQUEST));
        assert!(!retryable_provider_status(StatusCode::UNAUTHORIZED));
    }

    #[test]
    fn relaxes_required_tool_choice_only_for_compatibility_errors() {
        assert!(required_tool_choice_unsupported(StatusCode::BAD_REQUEST));
        assert!(required_tool_choice_unsupported(
            StatusCode::UNPROCESSABLE_ENTITY
        ));
        assert!(!required_tool_choice_unsupported(
            StatusCode::TOO_MANY_REQUESTS
        ));
    }

    #[test]
    fn native_web_search_replaces_the_local_function_for_responses() {
        let tools = vec![
            coomi_engine::ToolSpec {
                name: "web_search".into(),
                description: "fallback".into(),
                parameters: json!({"type": "object"}),
            },
            coomi_engine::ToolSpec {
                name: "read_file".into(),
                description: "read".into(),
                parameters: json!({"type": "object"}),
            },
        ];
        let output = openai_responses_tools(&tools, true);
        assert_eq!(
            output
                .iter()
                .filter(|tool| tool.get("type").and_then(Value::as_str) == Some("web_search"))
                .count(),
            1
        );
        assert!(!output.iter().any(|tool| {
            tool.get("type").and_then(Value::as_str) == Some("function")
                && tool.get("name").and_then(Value::as_str) == Some("web_search")
        }));
    }

    #[test]
    fn responses_history_replays_opaque_compaction_items() {
        let item = json!({
            "id": "cmp_1",
            "type": "compaction",
            "encrypted_content": "opaque"
        });
        let input =
            responses_input(&[ChatMessage::provider_item(item.clone())]).expect("responses input");
        assert_eq!(input, vec![item]);
        assert!(
            openai_messages(&[ChatMessage::provider_item(json!({
                "type": "compaction",
                "encrypted_content": "opaque"
            }))])
            .expect("chat messages")
            .is_empty()
        );
    }

    #[test]
    fn compaction_stream_preserves_encrypted_output_and_usage() {
        let mut state = CompactionStreamState::default();
        state
            .consume(&json!({
                "type": "response.output_item.done",
                "item": {
                    "id": "cmp_1",
                    "type": "compaction",
                    "encrypted_content": "opaque"
                }
            }))
            .expect("compaction item");
        state
            .consume(&json!({
                "type": "response.completed",
                "response": {"usage": {"input_tokens": 42, "output_tokens": 3}}
            }))
            .expect("usage");
        let (item, usage) = state.finish().expect("finished stream");
        assert_eq!(item["encrypted_content"], "opaque");
        assert_eq!(usage.input_tokens, 42);
        assert_eq!(usage.output_tokens, 3);
    }

    #[test]
    fn renders_structured_image_tool_outputs_for_each_provider() {
        let call = ToolCall {
            id: "call-1".into(),
            name: "view_image".into(),
            arguments: json!({"path": "image.png"}),
        };
        let mut output = ChatMessage::tool("call-1", "success: image loaded");
        output.images.push(coomi_engine::ImageContent {
            media_type: "image/png".into(),
            data: "BASE64".into(),
        });
        let history = vec![ChatMessage::assistant("", vec![call]), output];

        let responses = responses_input(&history).expect("Responses history");
        assert_eq!(responses[1]["output"][1]["type"], "input_image");
        assert_eq!(
            responses[1]["output"][1]["image_url"],
            "data:image/png;base64,BASE64"
        );

        let chat = openai_messages(&history).expect("Chat history");
        assert_eq!(chat[1]["content"][1]["type"], "image_url");

        let (_, anthropic) = anthropic_messages(&history).expect("Anthropic history");
        assert_eq!(
            anthropic[1]["content"][0]["content"][1]["source"]["media_type"],
            "image/png"
        );

        let (_, gemini) = gemini_messages(&history).expect("Gemini history");
        assert_eq!(gemini[1]["parts"][1]["inlineData"]["mimeType"], "image/png");
    }

    #[test]
    fn remote_compaction_v2_appends_one_trigger() {
        let body = remote_compaction_v2_body(
            &CompactionRequest {
                model: "test-model".into(),
                messages: vec![ChatMessage::user("checkpoint")],
                system_prompt: "instructions".into(),
                tools: vec![coomi_engine::ToolSpec {
                    name: "read_file".into(),
                    description: "Read a file".into(),
                    parameters: json!({"type": "object"}),
                }],
            },
            false,
            true,
        )
        .expect("compaction body");
        let input = body["input"].as_array().expect("input array");
        assert_eq!(
            input
                .iter()
                .filter(|item| item["type"] == "compaction_trigger")
                .count(),
            1
        );
        assert_eq!(input.last(), Some(&json!({"type": "compaction_trigger"})));
        assert_eq!(body["parallel_tool_calls"], true);
        assert_eq!(body["tools"][0]["name"], "read_file");
    }

    #[test]
    fn parallel_tool_calls_are_sent_only_when_enabled_with_tools() {
        let mut enabled = json!({"tools": [{"type": "function"}]});
        apply_parallel_tool_calls(&mut enabled, true, true);
        assert_eq!(enabled["parallel_tool_calls"], true);

        let mut disabled = json!({"tools": [{"type": "function"}]});
        apply_parallel_tool_calls(&mut disabled, false, true);
        assert!(disabled.get("parallel_tool_calls").is_none());

        let mut no_tools = json!({});
        apply_parallel_tool_calls(&mut no_tools, true, false);
        assert!(no_tools.get("parallel_tool_calls").is_none());
    }

    #[test]
    fn retryable_stream_error_matches_transport_failures_only() {
        let retryable = [
            "provider stream failed: stream ended before [DONE], finish_reason, or response.completed",
            "provider stream failed: no first byte within 45s",
            "provider stream failed: no response head within 45s",
            "provider stream failed: operation timed out",
            "provider stream failed: connection reset by peer",
            "provider stream failed: broken pipe",
        ];
        for message in retryable {
            assert!(
                is_retryable_stream_error(&anyhow::anyhow!("{message}")),
                "expected retryable: {message}"
            );
        }
        let not_retryable = [
            "invalid provider SSE JSON",
            "provider returned HTTP 400: bad request",
            "streamed tool call has no name",
            "provider response has no choices[0].message",
            "failed to build provider HTTP client",
        ];
        for message in not_retryable {
            assert!(
                !is_retryable_stream_error(&anyhow::anyhow!("{message}")),
                "expected non-retryable: {message}"
            );
        }
    }

    #[test]
    fn retryable_send_error_matches_transport_failures_only() {
        let retryable = [
            "error sending request for url (http://127.0.0.1/v1/chat/completions): connection refused",
            "operation timed out",
            "connection reset by peer",
            "broken pipe",
            "error sending request: tls handshake eof",
            "failed to lookup address information: name or service not known",
        ];
        for message in retryable {
            assert!(
                is_retryable_send_error(&anyhow::anyhow!("{message}")),
                "expected retryable send error: {message}"
            );
        }
        let not_retryable = [
            "invalid provider SSE JSON",
            "provider returned HTTP 400: bad request",
            "streamed tool call has no name",
        ];
        for message in not_retryable {
            assert!(
                !is_retryable_send_error(&anyhow::anyhow!("{message}")),
                "expected non-retryable send error: {message}"
            );
        }
    }

    #[test]
    fn retryable_stream_error_matches_context_chains() {
        let error = anyhow::anyhow!("connection reset")
            .context("provider stream failed")
            .context("provider request failed");
        assert!(is_retryable_stream_error(&error));
    }

    #[test]
    fn retry_after_headers_are_respected_and_bounded() {
        let mut headers = HeaderMap::new();
        headers.insert("retry-after", "2".parse().expect("retry-after header"));
        assert_eq!(
            provider_response_retry_delay(&headers, 0),
            Duration::from_secs(2)
        );
        headers.insert(
            "retry-after-ms",
            "90000".parse().expect("retry-after-ms header"),
        );
        assert_eq!(
            provider_response_retry_delay(&headers, 0),
            Duration::from_secs(30)
        );
    }
}
