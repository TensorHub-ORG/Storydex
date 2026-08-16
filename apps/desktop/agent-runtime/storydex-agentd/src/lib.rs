use axum::Json;
use axum::Router;
use axum::body::Body;
use axum::extract::State;
use axum::http::Request;
use axum::http::StatusCode;
use axum::http::header;
use axum::middleware;
use axum::middleware::Next;
use axum::response::IntoResponse;
use axum::response::Response;
use axum::routing::get;
use axum::routing::post;
use chrono::Utc;
use coomi_engine::ReasoningEffort;
use coomi_services::ProviderKind;
use coomi_services::ProviderRegistry;
use coomi_services::ReasoningCapability;
use coomi_services::ReasoningCapabilitySource;
use coomi_services::ReasoningControlMode;
use coomi_services::ReasoningLevelCapability;
use coomi_services::ReasoningRequestPlan;
use coomi_services::ReasoningSupport;
use coomi_services::ReasoningWireField;
use coomi_services::reasoning_capability_best_effort;
use coomi_services::reasoning_request_plan_best_effort;
use serde::Serialize;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::Ordering;
use std::time::Duration;
use std::time::Instant;
use tokio::net::TcpListener;
use tokio_util::sync::CancellationToken;
use tower_http::catch_panic::CatchPanicLayer;
use tower_http::trace::TraceLayer;
use uuid::Uuid;

pub const API_PROTOCOL_VERSION: u32 = 1;
pub const SERVICE_NAME: &str = "storydex-agentd";
pub const API_SERVICE_NAME: &str = "Storydex Backend";
pub const COOMI_RUNTIME_NAME: &str = "storydex-coomi-rs";

pub fn default_storydex_home() -> PathBuf {
    if let Ok(configured) = std::env::var("STORYDEX_COOMI_HOME") {
        let configured = configured.trim();
        if !configured.is_empty() {
            return PathBuf::from(configured);
        }
    }
    dirs::home_dir()
        .map(|home| home.join(".storydex").join(".coomi"))
        .unwrap_or_else(|| PathBuf::from(".storydex").join(".coomi"))
}

#[derive(Clone, Default)]
pub struct TaskRegistry {
    active: Arc<AtomicUsize>,
}

impl TaskRegistry {
    pub fn begin(&self) -> TaskGuard {
        self.active.fetch_add(1, Ordering::AcqRel);
        TaskGuard {
            active: Arc::clone(&self.active),
        }
    }

    pub fn active_count(&self) -> usize {
        self.active.load(Ordering::Acquire)
    }

    async fn wait_for_empty(&self, timeout: Duration) -> bool {
        tokio::time::timeout(timeout, async {
            while self.active_count() != 0 {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .is_ok()
    }
}

pub struct TaskGuard {
    active: Arc<AtomicUsize>,
}

impl Drop for TaskGuard {
    fn drop(&mut self) {
        self.active.fetch_sub(1, Ordering::AcqRel);
    }
}

#[derive(Clone)]
pub struct AppState {
    token: Arc<str>,
    coomi_home: Arc<PathBuf>,
    started_at: Instant,
    shutdown: CancellationToken,
    tasks: TaskRegistry,
}

impl AppState {
    pub fn new(token: impl Into<String>) -> anyhow::Result<Self> {
        Self::with_home(token, default_storydex_home())
    }

    pub fn with_home(
        token: impl Into<String>,
        coomi_home: impl Into<PathBuf>,
    ) -> anyhow::Result<Self> {
        let token = token.into();
        anyhow::ensure!(!token.trim().is_empty(), "agentd token must not be empty");
        let coomi_home = coomi_home.into();
        anyhow::ensure!(
            !coomi_home.as_os_str().is_empty(),
            "Storydex Coomi home must not be empty"
        );
        Ok(Self {
            token: Arc::from(token),
            coomi_home: Arc::new(coomi_home),
            started_at: Instant::now(),
            shutdown: CancellationToken::new(),
            tasks: TaskRegistry::default(),
        })
    }

    pub fn shutdown_token(&self) -> CancellationToken {
        self.shutdown.clone()
    }

    pub fn task_registry(&self) -> TaskRegistry {
        self.tasks.clone()
    }

    pub fn coomi_home(&self) -> &Path {
        self.coomi_home.as_path()
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ApiTrace {
    trace_id: String,
    duration_ms: u128,
    tool_calls: u32,
    llm_calls: u32,
}

impl ApiTrace {
    fn new(started_at: Instant) -> Self {
        Self {
            trace_id: Uuid::new_v4().to_string(),
            duration_ms: started_at.elapsed().as_millis(),
            tool_calls: 0,
            llm_calls: 0,
        }
    }
}

#[derive(Serialize)]
struct ApiError {
    code: String,
    message: String,
    details: Option<Value>,
}

#[derive(Serialize)]
struct ApiEnvelope<T>
where
    T: Serialize,
{
    ok: bool,
    data: Option<T>,
    error: Option<ApiError>,
    trace: ApiTrace,
    audit: Vec<Value>,
}

impl<T> ApiEnvelope<T>
where
    T: Serialize,
{
    fn success(data: T, started_at: Instant) -> Self {
        Self {
            ok: true,
            data: Some(data),
            error: None,
            trace: ApiTrace::new(started_at),
            audit: Vec::new(),
        }
    }

    fn with_audit(mut self, audit: Vec<Value>) -> Self {
        self.audit = audit;
        self
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct HealthData {
    status: &'static str,
    service: &'static str,
    time: String,
    runtime: &'static str,
    version: &'static str,
    protocol_version: u32,
    active_tasks: usize,
    uptime_ms: u128,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct VersionData {
    runtime: &'static str,
    version: &'static str,
    protocol_version: u32,
}

#[derive(Serialize)]
struct ShutdownData {
    status: &'static str,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ProviderChoiceData {
    selector: String,
    provider_id: String,
    provider_display: String,
    model: String,
    is_fast: bool,
    reasoning_capability: ReasoningCapabilityData,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ReasoningCapabilityData {
    support: ReasoningSupport,
    levels: Vec<ReasoningLevelCapability>,
    source: ReasoningCapabilitySource,
    prompt_fallback: bool,
    route_sensitive: bool,
    fallback_reason: String,
}

impl From<ReasoningCapability> for ReasoningCapabilityData {
    fn from(value: ReasoningCapability) -> Self {
        Self {
            support: value.support,
            levels: value.levels,
            source: value.source,
            prompt_fallback: value.prompt_fallback,
            route_sensitive: value.route_sensitive,
            fallback_reason: value.fallback_reason.unwrap_or_default(),
        }
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ReasoningRequestPlanData {
    requested: ReasoningEffort,
    control: ReasoningControlMode,
    sent: bool,
    prompt_applied: bool,
    wire_fields: Vec<ReasoningWireField>,
    support: ReasoningSupport,
    source: ReasoningCapabilitySource,
    route_sensitive: bool,
    fallback_reason: String,
}

impl From<ReasoningRequestPlan> for ReasoningRequestPlanData {
    fn from(value: ReasoningRequestPlan) -> Self {
        Self {
            requested: value.requested,
            control: value.control,
            sent: value.sent,
            prompt_applied: value.prompt_applied,
            wire_fields: value.wire_fields,
            support: value.support,
            source: value.source,
            route_sensitive: value.route_sensitive,
            fallback_reason: value.fallback_reason.unwrap_or_default(),
        }
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CoomiStatusData {
    runtime: &'static str,
    installed: bool,
    home: String,
    config_path: String,
    sessions_path: String,
    provider_id: String,
    provider_type: &'static str,
    model: String,
    display: String,
    reasoning_capability: ReasoningCapabilityData,
    reasoning_request_plan: ReasoningRequestPlanData,
    models: Vec<ProviderChoiceData>,
    provider_capabilities: coomi_engine::ModelCapabilities,
}

fn provider_kind_name(kind: ProviderKind) -> &'static str {
    match kind {
        ProviderKind::OpenAiCompatible => "openai_compatible",
        ProviderKind::OpenAiResponses => "openai_responses",
        ProviderKind::AnthropicMessages => "anthropic_messages",
        ProviderKind::GeminiNative => "gemini_native",
    }
}

fn read_coomi_status(home: &Path) -> anyhow::Result<CoomiStatusData> {
    let config_path = home.join("config").join("providers.json");
    let registry = ProviderRegistry::load(&config_path).map_err(|error| {
        anyhow::anyhow!("unable to load Storydex Coomi provider configuration: {error:#}")
    })?;
    let active = registry.resolve(None)?;
    let models = registry
        .choices()
        .into_iter()
        .map(|choice| -> anyhow::Result<ProviderChoiceData> {
            let config = registry.resolve(Some(&choice.selector))?;
            Ok(ProviderChoiceData {
                selector: choice.selector,
                provider_id: choice.provider_id,
                provider_display: choice.provider_display,
                model: choice.model.clone(),
                is_fast: choice.is_fast,
                reasoning_capability: reasoning_capability_best_effort(&config, &choice.model)
                    .into(),
            })
        })
        .collect::<anyhow::Result<Vec<_>>>()?;
    Ok(CoomiStatusData {
        runtime: COOMI_RUNTIME_NAME,
        installed: true,
        home: home.display().to_string(),
        config_path: config_path.display().to_string(),
        sessions_path: home.join("sessions").display().to_string(),
        provider_id: active.id.clone(),
        provider_type: provider_kind_name(active.kind),
        model: active.model.clone(),
        display: active.display.clone(),
        reasoning_capability: reasoning_capability_best_effort(&active, &active.model).into(),
        reasoning_request_plan: reasoning_request_plan_best_effort(
            &active,
            &active.model,
            ReasoningEffort::Auto,
            Some(active.capabilities.max_output_tokens),
        )
        .into(),
        models,
        provider_capabilities: active.capabilities.clone(),
    })
}

fn error_response(status: StatusCode, code: &str, message: &str) -> Response {
    let envelope = ApiEnvelope::<Value> {
        ok: false,
        data: None,
        error: Some(ApiError {
            code: code.to_owned(),
            message: message.to_owned(),
            details: None,
        }),
        trace: ApiTrace::new(Instant::now()),
        audit: Vec::new(),
    };
    (status, Json(envelope)).into_response()
}

async fn auth_layer(State(state): State<AppState>, request: Request<Body>, next: Next) -> Response {
    if request.uri().path() == "/api/v1/sys/health" {
        return next.run(request).await;
    }
    let authorized = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .is_some_and(|value| value == state.token.as_ref());
    if authorized {
        next.run(request).await
    } else {
        error_response(
            StatusCode::UNAUTHORIZED,
            "unauthorized",
            "Missing or invalid Storydex Agent access token.",
        )
    }
}

async fn health(State(state): State<AppState>) -> Json<ApiEnvelope<HealthData>> {
    let started_at = Instant::now();
    Json(ApiEnvelope::success(
        HealthData {
            status: "ok",
            service: API_SERVICE_NAME,
            time: Utc::now().to_rfc3339(),
            runtime: SERVICE_NAME,
            version: env!("CARGO_PKG_VERSION"),
            protocol_version: API_PROTOCOL_VERSION,
            active_tasks: state.tasks.active_count(),
            uptime_ms: state.started_at.elapsed().as_millis(),
        },
        started_at,
    ))
}

async fn version() -> Json<ApiEnvelope<VersionData>> {
    let started_at = Instant::now();
    Json(ApiEnvelope::success(
        VersionData {
            runtime: SERVICE_NAME,
            version: env!("CARGO_PKG_VERSION"),
            protocol_version: API_PROTOCOL_VERSION,
        },
        started_at,
    ))
}

async fn shutdown(State(state): State<AppState>) -> Json<ApiEnvelope<ShutdownData>> {
    let started_at = Instant::now();
    state.shutdown.cancel();
    Json(ApiEnvelope::success(
        ShutdownData { status: "stopping" },
        started_at,
    ))
}

async fn coomi_status(State(state): State<AppState>) -> Response {
    let started_at = Instant::now();
    let _task = state.tasks.begin();
    match read_coomi_status(state.coomi_home()) {
        Ok(data) => {
            Json(
                ApiEnvelope::success(data, started_at).with_audit(vec![serde_json::json!({
                    "action": "read_coomi_status",
                    "toolCount": 0,
                })]),
            )
            .into_response()
        }
        Err(error) => {
            tracing::error!(error = %error, "Storydex Coomi status could not be read");
            error_response(
                StatusCode::SERVICE_UNAVAILABLE,
                "provider_config_unavailable",
                "Storydex Coomi provider configuration is unavailable.",
            )
        }
    }
}

async fn not_found() -> Response {
    error_response(StatusCode::NOT_FOUND, "not_found", "Route not found.")
}

async fn method_not_allowed() -> Response {
    error_response(
        StatusCode::METHOD_NOT_ALLOWED,
        "method_not_allowed",
        "Method not allowed for this route.",
    )
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/api/v1/sys/health", get(health))
        .route("/api/v1/sys/version", get(version))
        .route("/api/v1/sys/shutdown", post(shutdown))
        .route("/api/v1/agent/coomi/status", get(coomi_status))
        .fallback(not_found)
        .method_not_allowed_fallback(method_not_allowed)
        .layer(CatchPanicLayer::custom(|_| {
            error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "internal_error",
                "Storydex Agent service encountered an internal error.",
            )
        }))
        .layer(TraceLayer::new_for_http())
        .layer(middleware::from_fn_with_state(state.clone(), auth_layer))
        .with_state(state)
}

pub async fn serve(
    listener: TcpListener,
    state: AppState,
    shutdown_timeout: Duration,
) -> anyhow::Result<()> {
    let shutdown = state.shutdown_token();
    axum::serve(listener, router(state.clone()))
        .with_graceful_shutdown(shutdown.cancelled_owned())
        .await?;
    anyhow::ensure!(
        state.tasks.wait_for_empty(shutdown_timeout).await,
        "agentd task shutdown exceeded {}ms",
        shutdown_timeout.as_millis()
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;
    use serde_json::json;
    use tempfile::tempdir;
    use tower::ServiceExt;

    async fn response_json(response: Response) -> Value {
        let bytes = to_bytes(response.into_body(), 1024 * 1024)
            .await
            .expect("read response body");
        serde_json::from_slice(&bytes).expect("decode response JSON")
    }

    #[tokio::test]
    async fn health_is_public_and_uses_storydex_envelope() {
        let app = router(AppState::new("test-token").expect("state"));
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/sys/health")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("health response");
        assert_eq!(response.status(), StatusCode::OK);
        let body = response_json(response).await;
        assert_eq!(body["ok"], true);
        assert_eq!(body["data"]["status"], "ok");
        assert_eq!(body["data"]["service"], API_SERVICE_NAME);
        assert!(
            body["trace"]["traceId"]
                .as_str()
                .is_some_and(|value| !value.is_empty())
        );
        assert_eq!(body["audit"], json!([]));
    }

    #[tokio::test]
    async fn protected_routes_reject_missing_token_with_envelope() {
        let app = router(AppState::new("test-token").expect("state"));
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/sys/version")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("version response");
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
        let body = response_json(response).await;
        assert_eq!(body["ok"], false);
        assert_eq!(body["error"]["code"], "unauthorized");
    }

    #[tokio::test]
    async fn protected_version_accepts_bearer_token() {
        let app = router(AppState::new("test-token").expect("state"));
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/sys/version")
                    .header(header::AUTHORIZATION, "Bearer test-token")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("version response");
        assert_eq!(response.status(), StatusCode::OK);
        let body = response_json(response).await;
        assert_eq!(body["data"]["runtime"], SERVICE_NAME);
        assert_eq!(body["data"]["protocolVersion"], API_PROTOCOL_VERSION);
    }

    #[tokio::test]
    async fn coomi_status_reads_storydex_provider_config_without_exposing_key() {
        let home = tempdir().expect("home");
        let config = home.path().join("config").join("providers.json");
        std::fs::create_dir_all(config.parent().expect("config parent")).expect("config dir");
        std::fs::write(
            &config,
            r#"{
                "version": 1,
                "active": "OPENCODE",
                "providers": {
                    "OPENCODE": {
                        "type": "openai_compatible",
                        "display": "OpenCode",
                        "api_key": "must-not-leak",
                        "base_url": "https://example.invalid/v1",
                        "model": "deepseek-v4-flash"
                    }
                }
            }"#,
        )
        .expect("provider config");
        let app = router(AppState::with_home("test-token", home.path()).expect("state"));
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/agent/coomi/status")
                    .header(header::AUTHORIZATION, "Bearer test-token")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("status response");
        assert_eq!(response.status(), StatusCode::OK);
        let body = response_json(response).await;
        assert_eq!(body["data"]["providerId"], "OPENCODE");
        assert_eq!(body["data"]["model"], "deepseek-v4-flash");
        assert_eq!(body["data"]["providerType"], "openai_compatible");
        assert_eq!(body["data"]["reasoningCapability"]["fallbackReason"], "");
        assert_eq!(body["data"]["reasoningRequestPlan"]["fallbackReason"], "");
        assert!(!body.to_string().contains("must-not-leak"));
        assert_eq!(body["audit"][0]["action"], "read_coomi_status");
    }

    #[tokio::test]
    async fn coomi_status_reports_missing_config_without_fallback() {
        let home = tempdir().expect("home");
        let app = router(AppState::with_home("test-token", home.path()).expect("state"));
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/agent/coomi/status")
                    .header(header::AUTHORIZATION, "Bearer test-token")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("status response");
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
        let body = response_json(response).await;
        assert_eq!(body["ok"], false);
        assert_eq!(body["error"]["code"], "provider_config_unavailable");
    }

    #[tokio::test]
    async fn shutdown_endpoint_requests_graceful_stop() {
        let state = AppState::new("test-token").expect("state");
        let shutdown = state.shutdown_token();
        let response = router(state)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/sys/shutdown")
                    .header(header::AUTHORIZATION, "Bearer test-token")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("shutdown response");
        assert_eq!(response.status(), StatusCode::OK);
        assert!(shutdown.is_cancelled());
    }

    #[tokio::test]
    async fn task_registry_tracks_in_flight_work() {
        let registry = TaskRegistry::default();
        let guard = registry.begin();
        assert_eq!(registry.active_count(), 1);
        drop(guard);
        assert_eq!(registry.active_count(), 0);
        assert!(registry.wait_for_empty(Duration::from_millis(20)).await);
    }
}
