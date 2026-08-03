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

pub struct HttpModelProvider {
    config: ProviderConfig,
    client: Client,
}

impl HttpModelProvider {
    pub fn new(config: ProviderConfig) -> Result<Self> {
        let client = Client::builder()
            .timeout(Duration::from_secs(180))
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
        let response = self
            .send_with_retry(self.authenticated(self.client.post(endpoint)).json(body))
            .await?;
        if required_tool.is_none() || !required_tool_choice_unsupported(response.status()) {
            return Ok(response);
        }

        // Several OpenAI-compatible gateways support tools but reject named or
        // `required` tool_choice values. Retrying with `auto` preserves tool use
        // instead of failing the entire Storydex structured-output turn.
        let mut fallback = body.clone();
        fallback["tool_choice"] = Value::String("auto".into());
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
        if let Some(limit) = request.max_output_tokens {
            body["max_tokens"] = Value::from(limit);
        }
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
        if let Some(limit) = request.max_output_tokens {
            body["max_tokens"] = Value::from(limit);
        }
        let response = self
            .send_openai_chat(&endpoint, &body, required_tool.as_deref())
            .await?;
        let status = response.status();
        if !status.is_success() {
            return checked_json(response)
                .await
                .map(|_| ModelResponse::default());
        }
        let mut state = ChatStreamState::default();
        read_sse(response, |value| state.consume(&value, observer)).await?;
        state.finish()
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
        let response = self
            .authenticated(self.client.post(endpoint))
            .json(&body)
            .send()
            .await?;
        let status = response.status();
        if !status.is_success() {
            return checked_json(response)
                .await
                .and_then(|_| anyhow::bail!("remote compaction returned no stream"));
        }
        let mut state = CompactionStreamState::default();
        read_sse(response, |value| state.consume(&value)).await?;
        let (item, usage) = state.finish()?;
        let mut messages = retained_user_history(&request.messages);
        messages.push(ChatMessage::provider_item(item));
        Ok(CompactionResponse { messages, usage })
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
        if let Some(limit) = request.max_output_tokens {
            body["max_output_tokens"] = Value::from(limit);
        }
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
        if let Some(limit) = request.max_output_tokens {
            body["max_output_tokens"] = Value::from(limit);
        }
        let response = self
            .authenticated(self.client.post(endpoint))
            .json(&body)
            .send()
            .await?;
        let status = response.status();
        if !status.is_success() {
            return checked_json(response)
                .await
                .map(|_| ModelResponse::default());
        }
        let mut state = ResponsesStreamState::default();
        read_sse(response, |value| state.consume(&value, observer)).await?;
        state.finish()
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
        if let Some(limit) = request.max_output_tokens {
            body["generationConfig"] = json!({"maxOutputTokens": limit});
        }
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
    while let Some(chunk) = stream.next().await {
        buffer.extend_from_slice(&chunk.context("provider stream failed")?);
        while let Some(newline) = buffer.iter().position(|byte| *byte == b'\n') {
            let mut line = buffer.drain(..=newline).collect::<Vec<_>>();
            while matches!(line.last(), Some(b'\n' | b'\r')) {
                line.pop();
            }
            let line = String::from_utf8(line).context("provider stream was not UTF-8")?;
            let Some(data) = line.strip_prefix("data:") else {
                continue;
            };
            let data = data.trim();
            if data.is_empty() || data == "[DONE]" {
                continue;
            }
            consume(serde_json::from_str(data).context("invalid provider SSE JSON")?)?;
        }
    }
    Ok(())
}

#[derive(Default)]
struct PartialToolCall {
    id: String,
    name: String,
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

fn merge_partial_tool_call(target: &mut PartialToolCall, source: PartialToolCall) {
    if target.id.is_empty() {
        target.id = source.id;
    }
    if target.name.is_empty() {
        target.name = source.name;
    }
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
}

impl ChatStreamState {
    fn consume(&mut self, value: &Value, observer: &dyn ModelStreamObserver) -> Result<()> {
        if value.get("usage").is_some_and(|usage| !usage.is_null()) {
            self.usage = openai_usage(value.get("usage"));
        }
        let Some(delta) = value.pointer("/choices/0/delta") else {
            return Ok(());
        };
        if let Some(reasoning) = delta
            .get("reasoning_content")
            .or_else(|| delta.get("reasoning"))
            .and_then(Value::as_str)
        {
            observer.on_reasoning_delta(reasoning);
        }
        if let Some(content) = delta.get("content").and_then(Value::as_str) {
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
            let target = self.tools.entry(index).or_default();
            if !item_id.is_empty() {
                merge_streamed_identifier(&mut target.id, item_id);
            }
            if let Some(function) = item.get("function") {
                if let Some(name) = function.get("name").and_then(Value::as_str) {
                    merge_streamed_identifier(&mut target.name, name);
                }
                if let Some(arguments) = function.get("arguments").and_then(Value::as_str) {
                    target.arguments.push_str(arguments);
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
                if let Some(delta) = value.get("delta").and_then(Value::as_str) {
                    self.content.push_str(delta);
                    observer.on_text_delta(delta);
                }
            }
            Some("response.reasoning_summary_text.delta" | "response.reasoning_text.delta") => {
                if let Some(delta) = value.get("delta").and_then(Value::as_str) {
                    observer.on_reasoning_delta(delta);
                }
            }
            Some("response.output_item.added" | "response.output_item.done") => {
                if let Some(item) = value.get("item")
                    && item.get("type").and_then(Value::as_str) == Some("function_call")
                {
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
        let detail = body.chars().take(800).collect::<String>();
        anyhow::bail!("provider returned HTTP {status}: {detail}")
    }
    serde_json::from_str(&body).context("provider returned invalid JSON")
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

    struct TestStreamObserver;

    impl ModelStreamObserver for TestStreamObserver {
        fn on_text_delta(&self, _delta: &str) {}

        fn on_reasoning_delta(&self, _delta: &str) {}
    }

    #[test]
    fn rejects_non_object_tool_arguments() {
        assert!(parse_arguments(&Value::String("[]".into())).is_err());
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
}
