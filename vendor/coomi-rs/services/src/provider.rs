use crate::ProviderConfig;
use crate::ProviderKind;
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
use coomi_engine::ModelStreamObserver;
use coomi_engine::Role;
use coomi_engine::TokenUsage;
use coomi_engine::ToolCall;
use coomi_engine::retained_user_history;
use futures_util::StreamExt;
use reqwest::Client;
use reqwest::RequestBuilder;
use reqwest::Response;
use reqwest::StatusCode;
use serde_json::Map;
use serde_json::Value;
use serde_json::json;
use std::collections::BTreeMap;
use std::time::Duration;

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
                    tokio::time::sleep(PROVIDER_RETRY_DELAYS[attempt]).await;
                    current = template
                        .try_clone()
                        .context("failed to clone provider request for retry")?;
                }
                Ok(response) => return Ok(response),
                Err(error)
                    if (error.is_connect() || error.is_timeout())
                        && attempt + 1 < PROVIDER_REQUEST_ATTEMPTS =>
                {
                    tokio::time::sleep(PROVIDER_RETRY_DELAYS[attempt]).await;
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
    ) -> Result<Response> {
        let mut request_body = body.clone();
        let mut response = self
            .send_with_retry(self.authenticated(self.client.post(endpoint)).json(body))
            .await?;
        if required_tool.is_some() && required_tool_choice_unsupported(response.status()) {
            // Several OpenAI-compatible gateways support tools but reject named or
            // `required` tool_choice values. Retrying with `auto` preserves tool use
            // instead of failing the entire Storydex structured-output turn.
            request_body["tool_choice"] = Value::String("auto".into());
            response = self
                .send_with_retry(
                    self.authenticated(self.client.post(endpoint))
                        .json(&request_body),
                )
                .await?;
        }

        self.retry_missing_openai_message_id(endpoint, &request_body, response)
            .await
    }

    async fn retry_missing_openai_message_id(
        &self,
        endpoint: &str,
        body: &Value,
        response: Response,
    ) -> Result<Response> {
        if response.status() != StatusCode::BAD_REQUEST {
            return Ok(response);
        }

        let status = response.status();
        let response_body = response
            .text()
            .await
            .context("failed to read provider response")?;
        if !has_missing_openai_message_id_error(&response_body) {
            return Err(provider_http_error(status, &response_body));
        }
        let fallback = openai_message_id_fallback(body)
            .context("provider requested message IDs but request has no compatible messages")?;

        self.send_with_retry(
            self.authenticated(self.client.post(endpoint))
                .json(&fallback),
        )
        .await
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
        body["max_tokens"] = Value::from(
            request
                .max_output_tokens
                .unwrap_or(PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS),
        );
        let response = self
            .send_openai_chat(&endpoint, &body, required_tool.as_deref())
            .await?;
        let value = checked_json(response).await?;
        let message = value
            .pointer("/choices/0/message")
            .context("provider response has no choices[0].message")?;
        let content = text_content(message.get("content"));
        let tool_calls = parse_openai_tool_calls(message.get("tool_calls"))?;
        Ok(ModelResponse {
            content,
            tool_calls,
            usage: openai_usage(value.get("usage")),
            streamed: false,
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
        body["max_tokens"] = Value::from(
            request
                .max_output_tokens
                .unwrap_or(PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS),
        );
        for attempt in 0..PROVIDER_STREAM_ATTEMPTS {
            let response = match tokio::time::timeout(
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
                    tokio::time::sleep(PROVIDER_STREAM_RETRY_DELAYS[attempt]).await;
                    continue;
                }
                Ok(Err(error)) => return Err(error),
                Err(_) if attempt + 1 < PROVIDER_STREAM_ATTEMPTS => {
                    observer.on_provider_retry(attempt + 1, PROVIDER_STREAM_ATTEMPTS, 0);
                    tokio::time::sleep(PROVIDER_STREAM_RETRY_DELAYS[attempt]).await;
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
                    .map(|_| ModelResponse::default());
            }
            let mut state = ChatStreamState::default();
            match read_sse(response, |value| state.consume(&value, observer)).await {
                Ok(())
                    if is_truncated_tool_call_stream(&state)
                        && attempt + 1 < PROVIDER_STREAM_ATTEMPTS =>
                {
                    observer.on_provider_retry(
                        attempt + 1,
                        PROVIDER_STREAM_ATTEMPTS,
                        state.emitted_text_characters(),
                    );
                    tokio::time::sleep(PROVIDER_STREAM_RETRY_DELAYS[attempt]).await;
                }
                Ok(())
                    if is_empty_truncated_response(&state)
                        && attempt + 1 < PROVIDER_STREAM_ATTEMPTS =>
                {
                    observer.on_provider_retry(attempt + 1, PROVIDER_STREAM_ATTEMPTS, 0);
                    grow_output_token_budget(&mut body, "max_tokens");
                    tokio::time::sleep(PROVIDER_STREAM_RETRY_DELAYS[attempt]).await;
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
                            tokio::time::sleep(PROVIDER_STREAM_RETRY_DELAYS[attempt]).await;
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
                    tokio::time::sleep(PROVIDER_STREAM_RETRY_DELAYS[attempt]).await;
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
                    tokio::time::sleep(PROVIDER_STREAM_RETRY_DELAYS[attempt]).await;
                    continue;
                }
                Ok(Err(error)) => return Err(error.into()),
                Err(_) if attempt + 1 < PROVIDER_STREAM_ATTEMPTS => {
                    tokio::time::sleep(PROVIDER_STREAM_RETRY_DELAYS[attempt]).await;
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
                    tokio::time::sleep(PROVIDER_STREAM_RETRY_DELAYS[attempt]).await;
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
        body["max_output_tokens"] = Value::from(
            request
                .max_output_tokens
                .unwrap_or(PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS),
        );
        let response = self
            .authenticated(self.client.post(endpoint))
            .json(&body)
            .send()
            .await?;
        let value = checked_json(response).await?;
        let mut content = String::new();
        let mut tool_calls = Vec::new();
        for item in value
            .get("output")
            .and_then(Value::as_array)
            .context("responses payload has no output array")?
        {
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
        Ok(ModelResponse {
            content,
            tool_calls,
            usage: responses_usage(value.get("usage")),
            streamed: false,
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
        body["max_output_tokens"] = Value::from(
            request
                .max_output_tokens
                .unwrap_or(PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS),
        );
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
                    observer.on_provider_retry(attempt + 1, PROVIDER_STREAM_ATTEMPTS, 0);
                    tokio::time::sleep(PROVIDER_STREAM_RETRY_DELAYS[attempt]).await;
                    continue;
                }
                Ok(Err(error)) => return Err(error.into()),
                Err(_) if attempt + 1 < PROVIDER_STREAM_ATTEMPTS => {
                    observer.on_provider_retry(attempt + 1, PROVIDER_STREAM_ATTEMPTS, 0);
                    tokio::time::sleep(PROVIDER_STREAM_RETRY_DELAYS[attempt]).await;
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
                    .map(|_| ModelResponse::default());
            }
            let mut state = ResponsesStreamState::default();
            match read_sse(response, |value| state.consume(&value, observer)).await {
                Ok(())
                    if is_truncated_tool_call_stream(&state)
                        && attempt + 1 < PROVIDER_STREAM_ATTEMPTS =>
                {
                    observer.on_provider_retry(
                        attempt + 1,
                        PROVIDER_STREAM_ATTEMPTS,
                        state.emitted_text_characters(),
                    );
                    tokio::time::sleep(PROVIDER_STREAM_RETRY_DELAYS[attempt]).await;
                }
                Ok(())
                    if is_empty_truncated_response(&state)
                        && attempt + 1 < PROVIDER_STREAM_ATTEMPTS =>
                {
                    observer.on_provider_retry(attempt + 1, PROVIDER_STREAM_ATTEMPTS, 0);
                    grow_output_token_budget(&mut body, "max_output_tokens");
                    tokio::time::sleep(PROVIDER_STREAM_RETRY_DELAYS[attempt]).await;
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
                            tokio::time::sleep(PROVIDER_STREAM_RETRY_DELAYS[attempt]).await;
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
                    tokio::time::sleep(PROVIDER_STREAM_RETRY_DELAYS[attempt]).await;
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
        let mut builder = self
            .client
            .post(endpoint)
            .header("anthropic-version", "2023-06-01")
            .header("content-type", "application/json");
        if !self.config.api_key.is_empty() {
            builder = builder.header("x-api-key", &self.config.api_key);
        }
        let value = checked_json(builder.json(&body).send().await?).await?;
        let mut content = String::new();
        let mut tool_calls = Vec::new();
        for block in value
            .get("content")
            .and_then(Value::as_array)
            .context("anthropic response has no content array")?
        {
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
            },
            streamed: false,
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
        let mut builder = self
            .client
            .post(endpoint)
            .header("content-type", "application/json");
        if !self.config.api_key.is_empty() {
            builder = builder.header("x-goog-api-key", &self.config.api_key);
        }
        let value = checked_json(builder.json(&body).send().await?).await?;
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
            },
            streamed: false,
        })
    }

    fn authenticated(&self, builder: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        if self.config.api_key.is_empty() {
            builder
        } else {
            builder.bearer_auth(&self.config.api_key)
        }
    }
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
        match self.config.kind {
            ProviderKind::OpenAiCompatible => self.openai_compatible(request).await,
            ProviderKind::OpenAiResponses => self.openai_responses(request).await,
            ProviderKind::AnthropicMessages => self.anthropic_messages(request).await,
            ProviderKind::GeminiNative => self.gemini_native(request).await,
        }
    }

    async fn complete_stream(
        &self,
        request: ModelRequest,
        observer: &dyn ModelStreamObserver,
    ) -> Result<ModelResponse> {
        match self.config.kind {
            ProviderKind::OpenAiCompatible => {
                self.openai_compatible_stream(request, observer).await
            }
            ProviderKind::OpenAiResponses => self.openai_responses_stream(request, observer).await,
            ProviderKind::AnthropicMessages | ProviderKind::GeminiNative => {
                self.complete(request).await
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

async fn read_sse(response: Response, mut consume: impl FnMut(Value) -> Result<()>) -> Result<()> {
    let mut stream = response.bytes_stream();
    let mut buffer = Vec::new();
    let mut saw_completion_event = false;
    let mut first_byte_received = false;
    loop {
        let next_chunk = if saw_completion_event {
            match tokio::time::timeout(PROVIDER_COMPLETION_GRACE_TIMEOUT, stream.next()).await {
                Ok(chunk) => chunk,
                Err(_) => return Ok(()),
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
                ProviderSseLine::Done => return Ok(()),
                ProviderSseLine::Value(value) => {
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
            ProviderSseLine::Done => return Ok(()),
            ProviderSseLine::Value(value) => {
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
        if let Some(reasoning) = delta
            .get("reasoning_content")
            .or_else(|| delta.get("reasoning"))
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
        {
            self.pushed_any = true;
            observer.on_reasoning_delta(reasoning);
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
        Ok(ModelResponse {
            content: self.content,
            tool_calls,
            usage: self.usage,
            streamed: true,
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
    usage: TokenUsage,
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
                if value.pointer("/response/status").and_then(Value::as_str) == Some("incomplete") {
                    self.truncated = true;
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
    messages
        .iter()
        .filter(|message| message.provider_items.is_empty())
        .map(|message| {
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
                                    json!({
                                        "id": call.id,
                                        "type": "function",
                                        "function": {
                                            "name": call.name,
                                            "arguments": serde_json::to_string(&call.arguments).unwrap_or_else(|_| "{}".into())
                                        }
                                    })
                                })
                                .collect(),
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
            Ok(value)
        })
        .collect()
}

fn responses_input(messages: &[ChatMessage]) -> Result<Vec<Value>> {
    let mut input = Vec::new();
    for message in messages {
        if !message.provider_items.is_empty() {
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
            continue;
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
            continue;
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
    }

    impl ModelStreamObserver for CountingRetryObserver {
        fn on_text_delta(&self, _delta: &str) {}

        fn on_reasoning_delta(&self, _delta: &str) {}

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
}
