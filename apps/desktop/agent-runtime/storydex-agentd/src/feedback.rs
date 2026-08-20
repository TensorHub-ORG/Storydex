//! Feedback submission and tool-failure analysis routes.
//!
//! These routes intentionally keep the same boundary as the Python service:
//! feedback is redacted before it leaves the machine, while tool-failure
//! analysis uses the currently configured Coomi provider and never falls back
//! to a fabricated report when that provider is unavailable.

use axum::Json;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use base64::Engine;
use coomi_engine::{ChatMessage, ModelProvider, ModelRequest, ReasoningEffort};
use coomi_services::{HttpModelProvider, ProviderRegistry};
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::path::Path;
use std::sync::OnceLock;
use std::time::{Duration, Instant};
use uuid::Uuid;

use crate::{ApiEnvelope, AppState, error_response, error_response_with_details};

pub(crate) const FEEDBACK_DEFAULT_URL: &str = "https://updates.septemc.com/storydex/feedback/api";
const FEEDBACK_MAX_IMAGES: usize = 4;
const FEEDBACK_MAX_IMAGE_BYTES: usize = 5 * 1024 * 1024;
const TOOL_TRACE_MAX_ITEMS: usize = 40;
const TOOL_TRACE_MAX_BYTES: usize = 28 * 1024;
const TOOL_TRACE_MIN_FAILURES: usize = 3;
const TOOL_ANALYSIS_TIMEOUT: Duration = Duration::from_secs(180);
const TOOL_ANALYSIS_REDACTION_VERSION: &str = "storydex-tool-trace-v1";

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct FeedbackImageRequest {
    name: String,
    mime_type: String,
    data_url: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct FeedbackSubmitRequest {
    source: String,
    #[serde(default = "default_category")]
    category: String,
    description: String,
    #[serde(default)]
    contact: String,
    #[serde(default)]
    error_message: String,
    #[serde(default)]
    error_type: String,
    #[serde(default)]
    error_details: Value,
    #[serde(default)]
    diagnostics: Value,
    #[serde(default)]
    images: Vec<FeedbackImageRequest>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct ToolFailureTraceRequest {
    sequence: u64,
    tool: String,
    status: String,
    #[serde(default)]
    argument_shape: Value,
    #[serde(default)]
    elapsed_ms: Option<u64>,
    #[serde(default)]
    category: Option<String>,
    #[serde(default)]
    error_summary: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ToolFailureAnalysisRequest {
    #[serde(default)]
    provider_id: String,
    trace: Vec<ToolFailureTraceRequest>,
}

fn default_category() -> String {
    "bug".to_owned()
}

fn sensitive_key(value: &str) -> bool {
    let normalized = value
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .collect::<String>()
        .to_ascii_lowercase();
    [
        "apikey",
        "authorization",
        "token",
        "accessToken",
        "secret",
        "password",
        "credential",
        "prompt",
        "conversation",
        "messages",
        "manuscript",
        "content",
        "requestbody",
        "responsebody",
    ]
    .iter()
    .any(|marker| {
        normalized == marker.to_ascii_lowercase()
            || normalized.contains(&marker.to_ascii_lowercase())
    })
}

fn secret_regex() -> &'static Regex {
    static REGEX: OnceLock<Regex> = OnceLock::new();
    REGEX.get_or_init(|| {
        Regex::new(r"(?i)\b(?:sk-|bearer\s+)[0-9a-z._-]{8,}\b").expect("secret regex")
    })
}

fn labelled_secret_regex() -> &'static Regex {
    static REGEX: OnceLock<Regex> = OnceLock::new();
    REGEX.get_or_init(|| {
        Regex::new(r#"(?i)\b(api[_-]?key|authorization|access[_-]?token|token|secret|password|credential)(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"#)
            .expect("labelled secret regex")
    })
}

fn redaction_regexes() -> (
    &'static Regex,
    &'static Regex,
    &'static Regex,
    &'static Regex,
) {
    static URL: OnceLock<Regex> = OnceLock::new();
    static WINDOWS_PATH: OnceLock<Regex> = OnceLock::new();
    static UNIX_PATH: OnceLock<Regex> = OnceLock::new();
    static EMAIL: OnceLock<Regex> = OnceLock::new();
    (
        URL.get_or_init(|| Regex::new(r#"https?://[^\s\"']+"#).expect("url regex")),
        WINDOWS_PATH
            .get_or_init(|| Regex::new(r#"\b[A-Za-z]:\\[^\s\"']+"#).expect("windows path regex")),
        UNIX_PATH.get_or_init(|| {
            Regex::new(r#"/(?:home|Users|data|storage|sdcard|tmp|var|mnt)/[^\s\"']+"#)
                .expect("unix path regex")
        }),
        EMAIL.get_or_init(|| {
            Regex::new(r#"[0-9A-Za-z._%+-]+@[0-9A-Za-z.-]+\.[A-Za-z]{2,}"#).expect("email regex")
        }),
    )
}

fn sanitize_text(value: impl AsRef<str>, limit: usize) -> String {
    let mut text = value
        .as_ref()
        .chars()
        .take(limit.saturating_mul(2))
        .collect::<String>();
    text = secret_regex()
        .replace_all(&text, "[redacted_secret]")
        .into_owned();
    text = labelled_secret_regex()
        .replace_all(&text, "$1$2[redacted_secret]")
        .into_owned();
    let (url, windows_path, unix_path, email) = redaction_regexes();
    text = url.replace_all(&text, "[redacted_url]").into_owned();
    text = windows_path
        .replace_all(&text, "[redacted_path]")
        .into_owned();
    text = unix_path.replace_all(&text, "[redacted_path]").into_owned();
    text = email.replace_all(&text, "[redacted_email]").into_owned();
    text.chars().take(limit).collect()
}

fn sanitize_value(value: &Value, depth: usize) -> Value {
    if depth > 4 {
        return Value::String("[truncated]".to_owned());
    }
    match value {
        Value::Object(object) => Value::Object(
            object
                .iter()
                .filter(|(key, _)| !sensitive_key(key))
                .map(|(key, item)| {
                    (
                        key.chars().take(80).collect(),
                        sanitize_value(item, depth + 1),
                    )
                })
                .collect(),
        ),
        Value::Array(items) => Value::Array(
            items
                .iter()
                .take(50)
                .map(|item| sanitize_value(item, depth + 1))
                .collect(),
        ),
        Value::String(text) => Value::String(sanitize_text(text, 4000)),
        other => other.clone(),
    }
}

fn identifier(value: &str, limit: usize) -> String {
    let cleaned = value
        .chars()
        .filter(|character| character.is_ascii_alphanumeric() || "._:-".contains(*character))
        .take(limit)
        .collect::<String>();
    if cleaned.is_empty() {
        "unknown".to_owned()
    } else {
        cleaned
    }
}

fn sanitize_shape(value: &Value, key: &str, depth: usize) -> Value {
    if depth > 5 {
        return Value::String("[max_depth]".to_owned());
    }
    if sensitive_key(key) {
        return Value::String("[redacted_secret]".to_owned());
    }
    match value {
        Value::Object(object) => Value::Object(
            object
                .iter()
                .take(30)
                .map(|(child_key, child)| {
                    (
                        identifier(child_key, 80),
                        sanitize_shape(child, child_key, depth + 1),
                    )
                })
                .collect(),
        ),
        Value::Array(items) => Value::Array(
            items
                .iter()
                .take(12)
                .map(|item| sanitize_shape(item, key, depth + 1))
                .collect(),
        ),
        Value::Bool(value) => Value::Bool(*value),
        Value::Number(_) => Value::String("[number]".to_owned()),
        Value::Null => Value::String("[null]".to_owned()),
        Value::String(text) => {
            let text = text.trim();
            if text.len() <= 32
                && text.chars().all(|character| {
                    character.is_ascii_alphanumeric() || "._:-".contains(character)
                })
            {
                Value::String(text.to_owned())
            } else {
                Value::String(format!("[string length={}]", text.chars().count()))
            }
        }
    }
}

fn validate_feedback_source(source: &str) -> Result<(), String> {
    if matches!(source, "error" | "settings") {
        Ok(())
    } else {
        Err("Feedback source must be `error` or `settings`.".to_owned())
    }
}

fn validate_len(value: &str, max: usize, field: &str) -> Result<(), String> {
    if value.chars().count() <= max {
        Ok(())
    } else {
        Err(format!("{field} is too long."))
    }
}

fn validated_images(images: &[FeedbackImageRequest]) -> Result<Vec<Value>, String> {
    if images.len() > FEEDBACK_MAX_IMAGES {
        return Err("At most four feedback images are allowed.".to_owned());
    }
    let mut output = Vec::with_capacity(images.len());
    for image in images {
        validate_len(&image.name, 160, "image name")?;
        validate_len(&image.mime_type, 80, "image mime type")?;
        if image.data_url.len() > 7_500_000 {
            return Err("Feedback image data is too large.".to_owned());
        }
        let mime = image.mime_type.trim().to_ascii_lowercase();
        if !matches!(mime.as_str(), "image/png" | "image/jpeg" | "image/webp") {
            return Err("Only PNG, JPEG, or WebP feedback images are supported.".to_owned());
        }
        let prefix = format!("data:{mime};base64,");
        let Some(encoded) = image.data_url.strip_prefix(&prefix) else {
            return Err("Feedback image data URL is invalid.".to_owned());
        };
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(encoded)
            .map_err(|_| "Feedback image encoding is invalid.".to_owned())?;
        if decoded.is_empty() || decoded.len() > FEEDBACK_MAX_IMAGE_BYTES {
            return Err("Each feedback image must be smaller than 5 MB.".to_owned());
        }
        let name = Path::new(image.name.trim())
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("feedback-image")
            .chars()
            .take(160)
            .collect::<String>();
        output.push(json!({
            "name": name,
            "mimeType": mime,
            "dataBase64": base64::engine::general_purpose::STANDARD.encode(decoded),
        }));
    }
    Ok(output)
}

async fn forward_feedback(endpoint: &str, payload: &Value) -> Result<Value, Response> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(20))
        .build()
        .map_err(|_| {
            error_response(
                StatusCode::BAD_GATEWAY,
                "feedback_unavailable",
                "Feedback service is unavailable.",
            )
        })?;
    let response = client
        .post(endpoint.trim())
        .json(payload)
        .send()
        .await
        .map_err(|_| {
            error_response(
                StatusCode::BAD_GATEWAY,
                "feedback_unavailable",
                "Feedback service is unavailable.",
            )
        })?;
    let status = response.status();
    let body = response.json::<Value>().await.map_err(|_| {
        error_response(
            StatusCode::BAD_GATEWAY,
            "feedback_unavailable",
            "Feedback service returned an invalid response.",
        )
    })?;
    if !status.is_success() {
        return Err(error_response(
            StatusCode::BAD_GATEWAY,
            "feedback_unavailable",
            "Feedback service rejected the submission.",
        ));
    }
    Ok(body)
}

pub(crate) async fn submit_feedback(
    State(state): State<AppState>,
    Json(request): Json<FeedbackSubmitRequest>,
) -> Response {
    let started = Instant::now();
    if let Err(message) = validate_feedback_source(&request.source)
        .and_then(|_| validate_len(&request.category, 40, "category"))
        .and_then(|_| validate_len(&request.description, 5000, "description"))
        .and_then(|_| validate_len(&request.contact, 200, "contact"))
        .and_then(|_| validate_len(&request.error_message, 5000, "error message"))
        .and_then(|_| validate_len(&request.error_type, 160, "error type"))
    {
        return error_response(
            StatusCode::UNPROCESSABLE_ENTITY,
            "feedback_invalid",
            &message,
        );
    }
    if request.description.trim().chars().count() < 5 {
        return error_response(
            StatusCode::UNPROCESSABLE_ENTITY,
            "feedback_invalid",
            "Description must contain at least five characters.",
        );
    }
    let images = match validated_images(&request.images) {
        Ok(images) => images,
        Err(message) => {
            return error_response(
                StatusCode::UNPROCESSABLE_ENTITY,
                "feedback_invalid",
                &message,
            );
        }
    };
    let trace_id = Uuid::new_v4().to_string();
    let allowed_keys = [
        "storydexVersion",
        "coomiVersion",
        "platform",
        "provider",
        "model",
        "traceId",
        "sessionId",
        "updateState",
        "runtime",
        "analysisRequestId",
        "analysisElapsedMs",
        "analysisResponseCategory",
        "redactionVersion",
        "failureCount",
    ];
    let diagnostics = request
        .diagnostics
        .as_object()
        .map(|object| {
            allowed_keys
                .iter()
                .filter_map(|key| {
                    object
                        .get(*key)
                        .filter(|value| !value.is_null() && **value != Value::String(String::new()))
                        .map(|value| ((*key).to_owned(), sanitize_value(value, 0)))
                })
                .collect::<Map<String, Value>>()
        })
        .unwrap_or_default();
    let remote_payload = json!({
        "schemaVersion": 1,
        "platform": "windows",
        "submissionId": trace_id.clone(),
        "submittedAt": chrono::Utc::now().to_rfc3339(),
        "source": request.source.clone(),
        "category": request.category.clone(),
        "description": request.description.clone(),
        "contact": request.contact.clone(),
        "error": if request.source == "error" { json!({
            "message": sanitize_text(request.error_message, 5000),
            "type": request.error_type.clone(),
            "details": sanitize_value(&request.error_details, 0),
        }) } else { Value::Null },
        "diagnostics": Value::Object(diagnostics),
        "images": images,
        "privacy": {"conversationIncluded": false, "projectFilesIncluded": false},
    });
    let result = match forward_feedback(state.feedback_url(), &remote_payload).await {
        Ok(result) => result,
        Err(response) => return response,
    };
    let feedback_id = result
        .get("id")
        .or_else(|| result.get("feedbackId"))
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .unwrap_or(&trace_id)
        .to_owned();
    Json(
        ApiEnvelope::success(json!({"feedbackId": feedback_id}), started)
            .with_audit(vec![json!({"action": "submit_feedback"})]),
    )
    .into_response()
}

fn sanitize_trace(trace: &[ToolFailureTraceRequest]) -> Result<(Vec<Value>, usize), String> {
    if trace.is_empty() || trace.len() > TOOL_TRACE_MAX_ITEMS {
        return Err("tool trace must contain between 1 and 40 items".to_owned());
    }
    let mut failure_count = 0;
    let mut sanitized = Vec::with_capacity(trace.len());
    for item in trace {
        let status = match item.status.to_ascii_lowercase().as_str() {
            "success" | "error" | "unknown" => item.status.to_ascii_lowercase(),
            _ => "unknown".to_owned(),
        };
        if status == "error" {
            failure_count += 1;
        }
        if item.sequence == 0 || item.sequence > 10_000 || item.tool.trim().is_empty() {
            return Err("tool trace contains an invalid sequence or tool name".to_owned());
        }
        if item.tool.chars().count() > 80
            || item
                .category
                .as_deref()
                .is_some_and(|value| value.chars().count() > 80)
            || item
                .error_summary
                .as_deref()
                .is_some_and(|value| value.chars().count() > 1200)
            || item.elapsed_ms.is_some_and(|value| value > 3_600_000)
        {
            return Err("tool trace contains an overlong field".to_owned());
        }
        let mut item_value = json!({
            "sequence": item.sequence.max(1),
            "tool": identifier(&item.tool, 80),
            "status": status,
            "argumentShape": sanitize_shape(&item.argument_shape, "", 0),
        });
        if let Some(elapsed_ms) = item.elapsed_ms {
            item_value["elapsedMs"] = json!(elapsed_ms);
        }
        if let Some(category) = item.category.as_deref().filter(|value| !value.is_empty()) {
            item_value["category"] = json!(identifier(category, 80));
        }
        if item_value["status"] == "error"
            && let Some(summary) = item
                .error_summary
                .as_deref()
                .filter(|value| !value.is_empty())
        {
            item_value["errorSummary"] = json!(sanitize_text(summary, 600));
        }
        sanitized.push(item_value);
    }
    if failure_count < TOOL_TRACE_MIN_FAILURES {
        return Err(format!(
            "tool trace requires at least {TOOL_TRACE_MIN_FAILURES} failures"
        ));
    }
    let bytes = serde_json::to_vec(&sanitized)
        .map_err(|_| "tool trace could not be serialized".to_owned())?;
    if bytes.len() > TOOL_TRACE_MAX_BYTES {
        return Err("sanitized tool trace is too large".to_owned());
    }
    Ok((sanitized, failure_count))
}

fn program_evidence(items: &[Value]) -> String {
    let mut lines = vec![
        "【程序采集的脱敏证据】".to_owned(),
        "不含用户消息、小说正文、原始参数值、文件内容、真实路径、URL、密钥或模型隐藏思维。"
            .to_owned(),
    ];
    for item in items {
        let sequence = item
            .get("sequence")
            .and_then(Value::as_u64)
            .unwrap_or_default();
        let tool = item
            .get("tool")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        let status = item
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        let mut line = format!("#{sequence} {tool} | {status}");
        if let Some(category) = item.get("category").and_then(Value::as_str) {
            line.push_str(&format!(" | {category}"));
        }
        if let Some(elapsed) = item.get("elapsedMs").and_then(Value::as_u64) {
            line.push_str(&format!(" | {elapsed}ms"));
        }
        lines.push(line);
        lines.push(format!(
            "参数结构: {}",
            serde_json::to_string(item.get("argumentShape").unwrap_or(&Value::Null))
                .unwrap_or_default()
        ));
        if let Some(summary) = item.get("errorSummary").and_then(Value::as_str) {
            lines.push(format!("错误摘要: {summary}"));
        }
    }
    sanitize_text(lines.join("\n"), 3500)
}

fn provider_for_analysis(state: &AppState, provider_id: &str) -> anyhow::Result<HttpModelProvider> {
    let config_path = state.coomi_home().join("config").join("providers.json");
    let registry = ProviderRegistry::load(&config_path)?;
    let selector = (!provider_id.trim().is_empty()).then_some(provider_id.trim());
    let mut config = registry.resolve(selector)?;
    if let Some(fast_model) = config.fast_model.clone() {
        config.model = fast_model;
    }
    HttpModelProvider::new(config)
}

pub(crate) async fn analyze_tool_failures(
    State(state): State<AppState>,
    Json(request): Json<ToolFailureAnalysisRequest>,
) -> Response {
    let started = Instant::now();
    let request_bytes = match serde_json::to_vec(&request) {
        Ok(bytes) => bytes.len(),
        Err(_) => 0,
    };
    if request_bytes > 32 * 1024 {
        return error_response(
            StatusCode::PAYLOAD_TOO_LARGE,
            "tool_feedback_invalid",
            "Sanitized tool trace must not exceed 32 KB.",
        );
    }
    let (sanitized, failure_count) = match sanitize_trace(&request.trace) {
        Ok(value) => value,
        Err(message) => {
            return error_response(
                StatusCode::UNPROCESSABLE_ENTITY,
                "tool_feedback_invalid",
                &message,
            );
        }
    };
    let provider = match provider_for_analysis(&state, &request.provider_id) {
        Ok(provider) => provider,
        Err(error) => {
            tracing::warn!(error = %error, "tool feedback provider configuration unavailable");
            return error_response(
                StatusCode::BAD_GATEWAY,
                "tool_feedback_provider_unavailable",
                "The configured analysis provider is unavailable.",
            );
        }
    };
    let trace_json = serde_json::to_string_pretty(&sanitized).unwrap_or_else(|_| "[]".to_owned());
    let request = ModelRequest {
        model: provider.model().to_owned(),
        messages: vec![
            ChatMessage::system(
                "你是 Storydex 的本地故障分析器。输入只包含脱敏工具调用轨迹。请生成不超过 3000 字的中文工程报告，必须包含：失败与恢复链、已确认的程序证据、明确标注的推断、修复建议、测试与验收条件、缺失证据。不得复原或猜测用户对话、小说正文、文件内容、真实路径、URL、密钥或模型隐藏思维。",
            ),
            ChatMessage::user(format!("请分析以下本轮脱敏工具轨迹：\n\n{trace_json}")),
        ],
        tools: Vec::new(),
        max_output_tokens: Some(4000),
        required_tool: None,
        reasoning_effort: ReasoningEffort::Low,
    };
    let response =
        match tokio::time::timeout(TOOL_ANALYSIS_TIMEOUT, provider.complete(request)).await {
            Ok(Ok(response)) => response,
            Ok(Err(error)) => {
                tracing::warn!(error = %error, "tool feedback analysis provider failed");
                return error_response(
                    StatusCode::BAD_GATEWAY,
                    "tool_feedback_provider_error",
                    "Local tool-failure analysis failed.",
                );
            }
            Err(_) => {
                return error_response(
                    StatusCode::GATEWAY_TIMEOUT,
                    "tool_feedback_timeout",
                    "Local tool-failure analysis timed out; no feedback was uploaded.",
                );
            }
        };
    let analysis = sanitize_text(response.content, 4000).trim().to_owned();
    if analysis.is_empty() {
        return error_response_with_details(
            StatusCode::BAD_GATEWAY,
            "tool_feedback_empty",
            "Local tool-failure analysis returned an empty report.",
            Some(json!({"provider": provider.provider_id(), "model": provider.model()})),
        );
    }
    let request_id = Uuid::new_v4().to_string();
    let elapsed_ms = u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX);
    Json(
        ApiEnvelope::success(
            json!({
                "analysis": analysis,
                "programEvidence": program_evidence(&sanitized),
                "failureCount": failure_count,
                "requestId": request_id,
                "elapsedMs": elapsed_ms,
                "responseCategory": "success",
                "redactionVersion": TOOL_ANALYSIS_REDACTION_VERSION,
            }),
            started,
        )
        .with_audit(vec![json!({"action": "analyze_tool_failures"})]),
    )
    .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;
    use axum::routing::post;
    use serde_json::json;
    use std::sync::{Arc, Mutex};
    use tokio::sync::oneshot;
    use tower::ServiceExt;

    async fn response_json(response: Response) -> Value {
        let bytes = to_bytes(response.into_body(), 1024 * 1024)
            .await
            .expect("read response body");
        serde_json::from_slice(&bytes).expect("decode response JSON")
    }

    async fn mock_feedback(
        State(captured): State<Arc<Mutex<Option<Value>>>>,
        Json(payload): Json<Value>,
    ) -> (StatusCode, Json<Value>) {
        *captured.lock().expect("capture lock") = Some(payload);
        (StatusCode::OK, Json(json!({"id": "remote-feedback-1"})))
    }

    async fn mock_feedback_error(Json(_payload): Json<Value>) -> (StatusCode, Json<Value>) {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": "temporarily unavailable"})),
        )
    }

    async fn start_feedback_server(
        route: axum::Router,
    ) -> (String, oneshot::Sender<()>, tokio::task::JoinHandle<()>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("feedback mock listener");
        let address = listener.local_addr().expect("feedback mock address");
        let (shutdown_tx, shutdown_rx) = oneshot::channel();
        let task = tokio::spawn(async move {
            axum::serve(listener, route)
                .with_graceful_shutdown(async {
                    let _ = shutdown_rx.await;
                })
                .await
                .expect("feedback mock server");
        });
        (format!("http://{address}/feedback"), shutdown_tx, task)
    }

    #[test]
    fn diagnostic_redaction_removes_secrets_paths_urls_and_email() {
        let value = sanitize_text(
            "Bearer sk-1234567890 https://example.test/a C:\\Users\\alice\\story.md alice@example.test",
            600,
        );
        assert!(value.contains("[redacted_secret]"));
        assert!(value.contains("[redacted_url]"));
        assert!(value.contains("[redacted_path]"));
        assert!(value.contains("[redacted_email]"));
    }

    #[test]
    fn tool_trace_requires_three_failures_and_redacts_argument_values() {
        let trace = (1..=3)
            .map(|sequence| ToolFailureTraceRequest {
                sequence,
                tool: "write_file".to_owned(),
                status: "error".to_owned(),
                argument_shape: json!({"token": "secret", "path": "C:\\Users\\alice\\x.md", "count": 4}),
                elapsed_ms: Some(20),
                category: Some("io".to_owned()),
                error_summary: Some("failed at C:\\Users\\alice\\x.md".to_owned()),
            })
            .collect::<Vec<_>>();
        let (sanitized, failures) = sanitize_trace(&trace).expect("valid trace");
        assert_eq!(failures, 3);
        assert_eq!(sanitized[0]["argumentShape"]["token"], "[redacted_secret]");
        assert_eq!(sanitized[0]["argumentShape"]["count"], "[number]");
    }

    #[test]
    fn tool_trace_rejects_fewer_than_three_failures() {
        let trace = vec![ToolFailureTraceRequest {
            sequence: 1,
            tool: "read_file".to_owned(),
            status: "success".to_owned(),
            argument_shape: json!({}),
            elapsed_ms: None,
            category: None,
            error_summary: None,
        }];
        assert!(sanitize_trace(&trace).is_err());
    }

    #[tokio::test]
    async fn feedback_route_forwards_redacted_payload_and_remote_id() {
        let captured = Arc::new(Mutex::new(None));
        let route = axum::Router::new()
            .route("/feedback", post(mock_feedback))
            .with_state(captured.clone());
        let (endpoint, shutdown, server) = start_feedback_server(route).await;
        let root = tempfile::tempdir().expect("test root");
        let state = AppState::with_paths_and_feedback_url(
            "test-token",
            root.path().join("coomi"),
            root.path().join("bridge"),
            Some(root.path().to_path_buf()),
            None,
            &endpoint,
        )
        .expect("state");
        let app = crate::router(state);
        let response = app
            .oneshot(
                axum::http::Request::builder()
                    .method("POST")
                    .uri("/api/v1/sys/feedback")
                    .header(axum::http::header::AUTHORIZATION, "Bearer test-token")
                    .header(axum::http::header::CONTENT_TYPE, "application/json")
                    .body(axum::body::Body::from(
                        serde_json::to_vec(&json!({
                            "source": "error",
                            "category": "bug",
                            "description": "反馈描述足够长",
                            "contact": "writer@example.test",
                            "errorMessage": "Bearer sk-1234567890 at C:\\Users\\alice\\story.md",
                            "errorType": "RuntimeError",
                            "errorDetails": {
                                "token": "do-not-forward",
                                "url": "https://example.test/private",
                                "message": "safe diagnostic"
                            },
                            "diagnostics": {
                                "runtime": "storydex-agentd",
                                "prompt": "do-not-forward",
                                "unknown": "drop-me"
                            }
                        }))
                        .expect("request JSON"),
                    ))
                    .expect("feedback request"),
            )
            .await
            .expect("feedback response");
        assert_eq!(response.status(), StatusCode::OK);
        let body = response_json(response).await;
        assert_eq!(body["data"]["feedbackId"], "remote-feedback-1");

        let payload = captured
            .lock()
            .expect("capture lock")
            .clone()
            .expect("forwarded payload");
        assert_eq!(payload["privacy"]["conversationIncluded"], false);
        assert_eq!(
            payload["error"]["message"],
            "[redacted_secret] at [redacted_path]"
        );
        assert!(payload["error"]["details"].get("token").is_none());
        assert_eq!(payload["error"]["details"]["url"], "[redacted_url]");
        assert_eq!(payload["diagnostics"]["runtime"], "storydex-agentd");
        assert!(payload["diagnostics"].get("prompt").is_none());
        assert!(payload["diagnostics"].get("unknown").is_none());

        let _ = shutdown.send(());
        server.await.expect("feedback mock shutdown");
    }

    #[tokio::test]
    async fn feedback_route_returns_bad_gateway_when_remote_rejects() {
        let route = axum::Router::new().route("/feedback", post(mock_feedback_error));
        let (endpoint, shutdown, server) = start_feedback_server(route).await;
        let root = tempfile::tempdir().expect("test root");
        let state = AppState::with_paths_and_feedback_url(
            "test-token",
            root.path().join("coomi"),
            root.path().join("bridge"),
            Some(root.path().to_path_buf()),
            None,
            &endpoint,
        )
        .expect("state");
        let response = crate::router(state)
            .oneshot(
                axum::http::Request::builder()
                    .method("POST")
                    .uri("/api/v1/sys/feedback")
                    .header(axum::http::header::AUTHORIZATION, "Bearer test-token")
                    .header(axum::http::header::CONTENT_TYPE, "application/json")
                    .body(axum::body::Body::from(
                        serde_json::to_vec(&json!({
                            "source": "settings",
                            "description": "设置反馈描述"
                        }))
                        .expect("request JSON"),
                    ))
                    .expect("feedback request"),
            )
            .await
            .expect("feedback response");
        assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
        let body = response_json(response).await;
        assert_eq!(body["ok"], false);
        assert_eq!(body["error"]["code"], "feedback_unavailable");

        let _ = shutdown.send(());
        server.await.expect("feedback mock shutdown");
    }
}
