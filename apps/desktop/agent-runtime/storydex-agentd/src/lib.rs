use axum::Json;
use axum::Router;
use axum::body::Body;
use axum::extract::State;
use axum::http::Request;
use axum::http::StatusCode;
use axum::http::header;
use axum::http::{HeaderName, Method};
use axum::middleware;
use axum::middleware::Next;
use axum::response::IntoResponse;
use axum::response::Response;
use axum::routing::get;
use axum::routing::patch;
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
use tower_http::cors::{Any, CorsLayer};
use tower_http::trace::TraceLayer;
use uuid::Uuid;

pub(crate) mod chat;
mod execution;
mod followup;
mod length_tier_calibration;
mod project;
mod replacement;
mod story_generation;

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
    bridge_path: Arc<PathBuf>,
    refactor_root: Option<Arc<PathBuf>>,
    replay_fixture: Option<Arc<PathBuf>>,
    started_at: Instant,
    shutdown: CancellationToken,
    tasks: TaskRegistry,
    executions: execution::ExecutionRegistry,
    followups: followup::FollowupStore,
}

impl AppState {
    pub fn new(token: impl Into<String>) -> anyhow::Result<Self> {
        Self::with_home(token, default_storydex_home())
    }

    pub fn with_home(
        token: impl Into<String>,
        coomi_home: impl Into<PathBuf>,
    ) -> anyhow::Result<Self> {
        Self::with_paths(
            token,
            coomi_home,
            default_bridge_path(),
            configured_refactor_root(),
            configured_replay_fixture(),
        )
    }

    pub fn with_paths(
        token: impl Into<String>,
        coomi_home: impl Into<PathBuf>,
        bridge_path: impl Into<PathBuf>,
        refactor_root: Option<PathBuf>,
        replay_fixture: Option<PathBuf>,
    ) -> anyhow::Result<Self> {
        let token = token.into();
        anyhow::ensure!(!token.trim().is_empty(), "agentd token must not be empty");
        let coomi_home = coomi_home.into();
        anyhow::ensure!(
            !coomi_home.as_os_str().is_empty(),
            "Storydex Coomi home must not be empty"
        );
        let bridge_path = bridge_path.into();
        anyhow::ensure!(
            !bridge_path.as_os_str().is_empty(),
            "Storydex Coomi bridge path must not be empty"
        );
        let refactor_root = refactor_root
            .map(|path| path.canonicalize().unwrap_or(path))
            .map(Arc::new);
        let replay_fixture = replay_fixture.map(Arc::new);
        Ok(Self {
            token: Arc::from(token),
            coomi_home: Arc::new(coomi_home),
            bridge_path: Arc::new(bridge_path),
            refactor_root,
            replay_fixture,
            started_at: Instant::now(),
            shutdown: CancellationToken::new(),
            tasks: TaskRegistry::default(),
            executions: execution::ExecutionRegistry::default(),
            followups: followup::FollowupStore::default(),
        })
    }

    pub fn shutdown_token(&self) -> CancellationToken {
        self.shutdown.clone()
    }

    pub fn task_registry(&self) -> TaskRegistry {
        self.tasks.clone()
    }

    pub(crate) fn execution_registry(&self) -> execution::ExecutionRegistry {
        self.executions.clone()
    }

    pub(crate) fn followup_store(&self) -> followup::FollowupStore {
        self.followups.clone()
    }

    pub fn coomi_home(&self) -> &Path {
        self.coomi_home.as_path()
    }

    pub fn bridge_path(&self) -> &Path {
        self.bridge_path.as_path()
    }

    pub fn refactor_root(&self) -> Option<&Path> {
        self.refactor_root.as_deref().map(|path| path.as_path())
    }

    pub fn replay_fixture(&self) -> Option<&Path> {
        self.replay_fixture.as_deref().map(|path| path.as_path())
    }
}

pub fn default_bridge_path() -> PathBuf {
    if let Ok(configured) = std::env::var("STORYDEX_COOMI_BRIDGE") {
        let configured = configured.trim();
        if !configured.is_empty() {
            return PathBuf::from(configured);
        }
    }
    let filename = if cfg!(windows) {
        "storydex-coomi-bridge.exe"
    } else {
        "storydex-coomi-bridge"
    };
    std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(|parent| parent.join(filename)))
        .unwrap_or_else(|| PathBuf::from(filename))
}

fn configured_refactor_root() -> Option<PathBuf> {
    std::env::var("STORYDEX_AGENTD_REFACTOR_ROOT")
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

fn configured_replay_fixture() -> Option<PathBuf> {
    std::env::var("STORYDEX_AGENT_PROVIDER_REPLAY_FIXTURE")
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
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

pub(crate) fn error_response(status: StatusCode, code: &str, message: &str) -> Response {
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
    if request.uri().path() == "/api/v1/sys/health" || request.method() == Method::OPTIONS {
        return next.run(request).await;
    }
    let bearer_authorized = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .is_some_and(|value| value == state.token.as_ref());
    let runtime_authorized = request
        .headers()
        .get("x-storydex-runtime-token")
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value == state.token.as_ref());
    if bearer_authorized || runtime_authorized {
        next.run(request).await
    } else {
        error_response(
            StatusCode::UNAUTHORIZED,
            "unauthorized",
            "Missing or invalid Storydex Agent access token.",
        )
    }
}

fn cors_layer() -> CorsLayer {
    CorsLayer::new()
        .allow_origin(Any)
        .allow_methods([
            Method::GET,
            Method::POST,
            Method::PUT,
            Method::PATCH,
            Method::DELETE,
            Method::OPTIONS,
        ])
        .allow_headers([
            header::ACCEPT,
            header::AUTHORIZATION,
            header::CONTENT_TYPE,
            HeaderName::from_static("x-storydex-runtime-token"),
            HeaderName::from_static("x-trace-id"),
        ])
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
        .route("/api/v1/workspace/git/summary", get(project::git_summary))
        .route("/api/v1/workspace/git/diff", get(project::git_diff))
        .route("/api/v1/workspace/git/init", post(project::git_init))
        .route(
            "/api/v1/workspace/git/branches",
            get(project::git_branches).post(project::git_create_branch),
        )
        .route(
            "/api/v1/workspace/git/checkout",
            post(project::git_checkout),
        )
        .route("/api/v1/workspace/git/commit", post(project::git_commit))
        .route("/api/v1/workspace/git/restore", post(project::git_restore))
        .route("/api/v1/workspace/git/timeline", get(project::git_timeline))
        .route("/api/v1/workspace/git/jump", post(project::git_jump))
        .route(
            "/api/v1/workspace/git/commit-diff",
            get(project::git_commit_diff),
        )
        .route(
            "/api/v1/workspace/git/worldlines",
            post(project::git_worldline_create),
        )
        .route(
            "/api/v1/workspace/git/worldlines/rename",
            post(project::git_worldline_rename),
        )
        .route(
            "/api/v1/workspace/git/worldlines/delete",
            post(project::git_worldline_delete),
        )
        .route("/api/v1/story/wiki", get(project::wiki_read))
        .route("/api/v1/story/wiki/projection", post(project::wiki_write))
        .route(
            "/api/v1/agent/followups",
            get(chat::list_followups).post(chat::enqueue_followup),
        )
        .route(
            "/api/v1/agent/followups/resume",
            post(chat::resume_followups),
        )
        .route(
            "/api/v1/agent/followups/{message_id}/steer",
            post(chat::steer_followup),
        )
        .route(
            "/api/v1/agent/followups/{message_id}",
            patch(chat::update_followup).delete(chat::delete_followup),
        )
        .route("/api/v1/agent/chat/stream", post(chat::chat_stream))
        .route("/api/v1/agent/executions/stop", post(chat::stop_execution))
        .route("/api/v1/agent/coomi/approval", post(chat::resolve_approval))
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
        .layer(cors_layer())
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

    fn protected_json_request_with_method(
        method: &str,
        uri: &str,
        payload: Value,
    ) -> Request<Body> {
        Request::builder()
            .method(method)
            .uri(uri)
            .header(header::AUTHORIZATION, "Bearer test-token")
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(
                serde_json::to_vec(&payload).expect("serialize request body"),
            ))
            .expect("request")
    }

    fn protected_json_request(uri: &str, payload: Value) -> Request<Body> {
        protected_json_request_with_method("POST", uri, payload)
    }

    fn protected_get_request(uri: &str) -> Request<Body> {
        Request::builder()
            .uri(uri)
            .header(header::AUTHORIZATION, "Bearer test-token")
            .body(Body::empty())
            .expect("request")
    }

    fn encode_query_value(value: &Path) -> String {
        value
            .to_string_lossy()
            .replace('%', "%25")
            .replace(':', "%3A")
            .replace('\\', "%5C")
            .replace('/', "%2F")
            .replace(' ', "%20")
    }

    fn followup_test_state(root: &Path) -> (AppState, PathBuf) {
        let workspace = root.join("workspace");
        let home = root.join("coomi-home");
        std::fs::create_dir_all(&workspace).expect("workspace");
        std::fs::create_dir_all(&home).expect("coomi home");
        let state = AppState::with_paths(
            "test-token",
            home,
            root.join("unused-bridge"),
            Some(root.to_path_buf()),
            None,
        )
        .expect("state");
        (state, workspace)
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
    async fn protected_version_accepts_desktop_runtime_token() {
        let app = router(AppState::new("test-token").expect("state"));
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/v1/sys/version")
                    .header("x-storydex-runtime-token", "test-token")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("version response");
        assert_eq!(response.status(), StatusCode::OK);
        let body = response_json(response).await;
        assert_eq!(body["data"]["runtime"], SERVICE_NAME);
    }

    #[tokio::test]
    async fn cors_preflight_allows_the_narrow_desktop_runtime_header() {
        let app = router(AppState::new("test-token").expect("state"));
        let response = app
            .oneshot(
                Request::builder()
                    .method(Method::OPTIONS)
                    .uri("/api/v1/sys/version")
                    .header(header::ORIGIN, "http://tauri.localhost")
                    .header(header::ACCESS_CONTROL_REQUEST_METHOD, "GET")
                    .header(
                        header::ACCESS_CONTROL_REQUEST_HEADERS,
                        "x-storydex-runtime-token",
                    )
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("preflight response");
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers().get(header::ACCESS_CONTROL_ALLOW_ORIGIN),
            Some(&header::HeaderValue::from_static("*"))
        );
        let allowed_methods = response
            .headers()
            .get(header::ACCESS_CONTROL_ALLOW_METHODS)
            .and_then(|value| value.to_str().ok())
            .expect("allowed methods");
        assert!(
            allowed_methods
                .split(',')
                .any(|method| method.trim() == "GET")
        );
        let allowed_headers = response
            .headers()
            .get(header::ACCESS_CONTROL_ALLOW_HEADERS)
            .and_then(|value| value.to_str().ok())
            .expect("allowed headers");
        assert!(
            allowed_headers
                .split(',')
                .any(|name| { name.trim().eq_ignore_ascii_case("x-storydex-runtime-token") })
        );
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
    async fn chat_stream_rejects_malformed_json_with_storydex_envelope() {
        let app = router(AppState::new("test-token").expect("state"));
        let response = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/agent/chat/stream")
                    .header(header::AUTHORIZATION, "Bearer test-token")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from("{"))
                    .expect("request"),
            )
            .await
            .expect("chat response");
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        let body = response_json(response).await;
        assert_eq!(body["ok"], false);
        assert_eq!(body["error"]["code"], "invalid_request");
    }

    #[tokio::test]
    async fn followup_routes_enforce_idempotency_stale_trace_and_corruption() {
        let root = tempdir().expect("root");
        let (state, workspace) = followup_test_state(root.path());
        let workspace_root = workspace.to_string_lossy().into_owned();
        let payload = json!({
            "messageId": "followup-1",
            "sessionId": "session-1",
            "workspaceRoot": workspace_root,
            "content": "continue from the durable queue",
            "mode": "queued"
        });

        for _ in 0..2 {
            let response = router(state.clone())
                .oneshot(protected_json_request(
                    "/api/v1/agent/followups",
                    payload.clone(),
                ))
                .await
                .expect("enqueue response");
            assert_eq!(response.status(), StatusCode::OK);
            let body = response_json(response).await;
            assert_eq!(body["data"]["message"]["status"], "pending");
        }

        let response = router(state.clone())
            .oneshot(protected_json_request(
                "/api/v1/agent/followups",
                json!({
                    "messageId": "followup-1",
                    "sessionId": "session-1",
                    "workspaceRoot": workspace.to_string_lossy(),
                    "content": "conflicting content",
                    "mode": "queued"
                }),
            ))
            .await
            .expect("conflict response");
        assert_eq!(response.status(), StatusCode::CONFLICT);
        assert_eq!(
            response_json(response).await["error"]["code"],
            "message_id_conflict"
        );

        state
            .followup_store()
            .set_active(&workspace, "session-2", "trace-current")
            .expect("set active trace");
        let response = router(state.clone())
            .oneshot(protected_json_request(
                "/api/v1/agent/followups",
                json!({
                    "messageId": "steer-stale",
                    "sessionId": "session-2",
                    "workspaceRoot": workspace.to_string_lossy(),
                    "expectedTraceId": "trace-old",
                    "content": "steer this turn",
                    "mode": "steer"
                }),
            ))
            .await
            .expect("stale trace response");
        assert_eq!(response.status(), StatusCode::CONFLICT);
        assert_eq!(
            response_json(response).await["error"]["code"],
            "stale_trace"
        );

        let mailbox_root = workspace.join(".storydex").join(".agent").join("followups");
        let mailbox_path = std::fs::read_dir(&mailbox_root)
            .expect("mailbox directory")
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .find(|path| {
                std::fs::read_to_string(path).is_ok_and(|value| value.contains("followup-1"))
            })
            .expect("session-1 mailbox path");
        std::fs::write(mailbox_path, b"not-json").expect("corrupt mailbox");
        let response = router(state)
            .oneshot(protected_json_request(
                "/api/v1/agent/followups",
                json!({
                    "messageId": "followup-2",
                    "sessionId": "session-1",
                    "workspaceRoot": workspace.to_string_lossy(),
                    "content": "must fail closed",
                    "mode": "queued"
                }),
            ))
            .await
            .expect("corrupt mailbox response");
        assert_eq!(response.status(), StatusCode::INTERNAL_SERVER_ERROR);
        assert_eq!(
            response_json(response).await["error"]["code"],
            "corrupt_followup_mailbox"
        );
    }

    #[tokio::test]
    async fn followup_patch_and_delete_routes_are_idempotent() {
        let root = tempdir().expect("root");
        let (state, workspace) = followup_test_state(root.path());
        let workspace_root = workspace.to_string_lossy().into_owned();
        let response = router(state.clone())
            .oneshot(protected_json_request(
                "/api/v1/agent/followups",
                json!({
                    "messageId": "followup-mutation-1",
                    "sessionId": "session-mutation",
                    "workspaceRoot": workspace_root,
                    "content": "first content",
                    "mode": "queued"
                }),
            ))
            .await
            .expect("enqueue response");
        assert_eq!(response.status(), StatusCode::OK);

        let patch_payload = json!({
            "sessionId": "session-mutation",
            "workspaceRoot": workspace.to_string_lossy(),
            "content": "updated content"
        });
        for _ in 0..2 {
            let response = router(state.clone())
                .oneshot(protected_json_request_with_method(
                    "PATCH",
                    "/api/v1/agent/followups/followup-mutation-1",
                    patch_payload.clone(),
                ))
                .await
                .expect("patch response");
            assert_eq!(response.status(), StatusCode::OK);
            let body = response_json(response).await;
            assert_eq!(body["data"]["message"]["content"], "updated content");
            assert_eq!(body["data"]["message"]["status"], "pending");
        }

        let encoded_workspace = workspace
            .to_string_lossy()
            .replace('%', "%25")
            .replace(':', "%3A")
            .replace('\\', "%5C")
            .replace('/', "%2F");
        let delete_uri = format!(
            "/api/v1/agent/followups/followup-mutation-1?sessionId=session-mutation&workspaceRoot={encoded_workspace}"
        );
        for _ in 0..2 {
            let response = router(state.clone())
                .oneshot(
                    Request::builder()
                        .method("DELETE")
                        .uri(&delete_uri)
                        .header(header::AUTHORIZATION, "Bearer test-token")
                        .body(Body::empty())
                        .expect("delete request"),
                )
                .await
                .expect("delete response");
            assert_eq!(response.status(), StatusCode::OK);
            assert_eq!(
                response_json(response).await["data"]["message"]["status"],
                "cancelled"
            );
        }

        let mailbox = state
            .followup_store()
            .list(&workspace, "session-mutation")
            .expect("mailbox");
        assert_eq!(mailbox.revision, 3);
        assert_eq!(mailbox.messages.len(), 1);
        assert_eq!(mailbox.messages[0].content, "updated content");
        assert_eq!(mailbox.messages[0].status, "cancelled");
        assert_eq!(
            mailbox
                .events
                .iter()
                .filter(|event| event["_type"] == "FollowupUpdated")
                .count(),
            2
        );
    }

    #[tokio::test]
    async fn followup_route_rejects_workspace_outside_refactor_root() {
        let root = tempdir().expect("root");
        let outside = tempdir().expect("outside");
        let (state, _) = followup_test_state(root.path());
        let response = router(state)
            .oneshot(protected_json_request(
                "/api/v1/agent/followups",
                json!({
                    "messageId": "outside-1",
                    "sessionId": "session-1",
                    "workspaceRoot": outside.path().to_string_lossy(),
                    "content": "must stay inside fixture root",
                    "mode": "queued"
                }),
            ))
            .await
            .expect("outside workspace response");
        assert_eq!(response.status(), StatusCode::FORBIDDEN);
        assert_eq!(
            response_json(response).await["error"]["code"],
            "workspace_outside_refactor_root"
        );
    }

    #[tokio::test]
    async fn rust_project_routes_write_projection_and_finish_local_git() {
        let root = tempdir().expect("root");
        let (state, workspace) = followup_test_state(root.path());
        let encoded = encode_query_value(&workspace);

        let response = router(state.clone())
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri(format!(
                        "/api/v1/workspace/git/init?workspaceRoot={encoded}"
                    ))
                    .header(header::AUTHORIZATION, "Bearer test-token")
                    .body(Body::empty())
                    .expect("git init request"),
            )
            .await
            .expect("git init response");
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response_json(response).await["data"]["initialized"], true);

        let projection = json!({
            "workspaceRoot": workspace.to_string_lossy(),
            "payload": {
                "schemaVersion": 3,
                "entries": [{"id": "character:lin", "title": "林澈"}],
                "graph": {"nodes": [{"id": "character:lin", "label": "林澈"}], "edges": []}
            },
            "markdown": "# WIKI\n\n- 林澈\n",
            "index": {"schemaVersion": 3, "entries": [{"id": "character:lin"}]},
            "status": {"schemaVersion": 3, "status": "ready", "knowledgeRevision": 1},
            "sourceSnapshot": {"sources": []}
        });
        let response = router(state.clone())
            .oneshot(protected_json_request(
                "/api/v1/story/wiki/projection",
                projection,
            ))
            .await
            .expect("projection response");
        assert_eq!(response.status(), StatusCode::OK);
        let body = response_json(response).await;
        assert_eq!(body["data"]["event"], "KnowledgeProjectionUpdated");
        assert_eq!(
            body["data"]["changedPaths"].as_array().map(Vec::len),
            Some(5)
        );
        assert!(
            body["data"]["graphChecksum"]
                .as_str()
                .is_some_and(|value| value.starts_with("sha256:"))
        );

        let response = router(state.clone())
            .oneshot(
                Request::builder()
                    .uri(format!("/api/v1/story/wiki?workspaceRoot={encoded}"))
                    .header(header::AUTHORIZATION, "Bearer test-token")
                    .body(Body::empty())
                    .expect("wiki read request"),
            )
            .await
            .expect("wiki read response");
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response_json(response).await["data"]["wiki"]["entries"][0]["title"],
            "林澈"
        );

        let response = router(state.clone())
            .oneshot(protected_json_request(
                "/api/v1/workspace/git/commit",
                json!({
                    "workspaceRoot": workspace.to_string_lossy(),
                    "message": "故事：提交 Rust WIKI 投影"
                }),
            ))
            .await
            .expect("git commit response");
        assert_eq!(response.status(), StatusCode::OK);
        let body = response_json(response).await;
        assert_eq!(body["data"]["created"], true);
        assert_eq!(body["data"]["event"], "GitAutoCommit");
        assert_eq!(body["data"]["summary"]["clean"], true);

        let response = router(state)
            .oneshot(
                Request::builder()
                    .uri(format!(
                        "/api/v1/workspace/git/summary?workspaceRoot={encoded}"
                    ))
                    .header(header::AUTHORIZATION, "Bearer test-token")
                    .body(Body::empty())
                    .expect("git summary request"),
            )
            .await
            .expect("git summary response");
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response_json(response).await["data"]["clean"], true);
    }

    #[tokio::test]
    async fn rust_project_routes_cover_git_diff_timeline_and_worldlines() {
        let root = tempdir().expect("root");
        let (state, workspace) = followup_test_state(root.path());
        let encoded = encode_query_value(&workspace);

        let response = router(state.clone())
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri(format!(
                        "/api/v1/workspace/git/init?workspaceRoot={encoded}"
                    ))
                    .header(header::AUTHORIZATION, "Bearer test-token")
                    .body(Body::empty())
                    .expect("git init request"),
            )
            .await
            .expect("git init response");
        assert_eq!(response.status(), StatusCode::OK);

        std::fs::write(workspace.join("story.md"), "共同前史\n").expect("baseline story");
        let response = router(state.clone())
            .oneshot(protected_json_request(
                "/api/v1/workspace/git/commit",
                json!({
                    "workspaceRoot": workspace.to_string_lossy(),
                    "message": "故事：共同前史"
                }),
            ))
            .await
            .expect("baseline commit response");
        assert_eq!(response.status(), StatusCode::OK);
        let baseline = response_json(response).await;
        let baseline_id = baseline["data"]["commit"]["id"]
            .as_str()
            .expect("baseline commit id")
            .to_owned();

        std::fs::write(workspace.join("story.md"), "改写主线\n").expect("changed story");
        std::fs::write(workspace.join("新章.md"), "新内容\n").expect("unicode story");
        let response = router(state.clone())
            .oneshot(protected_get_request(&format!(
                "/api/v1/workspace/git/diff?workspaceRoot={encoded}"
            )))
            .await
            .expect("diff response");
        assert_eq!(response.status(), StatusCode::OK);
        let diff = response_json(response).await;
        assert_eq!(diff["data"]["totals"]["files"], 2);
        assert!(diff["data"]["files"].as_array().is_some_and(|files| {
            files
                .iter()
                .any(|file| file["relativePath"] == "新章.md" && file["added"] == 1)
        }));

        let response = router(state.clone())
            .oneshot(protected_json_request(
                "/api/v1/workspace/git/commit",
                json!({
                    "workspaceRoot": workspace.to_string_lossy(),
                    "message": "故事：改写主线"
                }),
            ))
            .await
            .expect("second commit response");
        assert_eq!(response.status(), StatusCode::OK);
        let second = response_json(response).await;
        let second_id = second["data"]["commit"]["id"]
            .as_str()
            .expect("second commit id")
            .to_owned();

        let response = router(state.clone())
            .oneshot(protected_get_request(&format!(
                "/api/v1/workspace/git/commit-diff?workspaceRoot={encoded}&commitId={second_id}"
            )))
            .await
            .expect("commit diff response");
        assert_eq!(response.status(), StatusCode::OK);
        let commit_diff = response_json(response).await;
        assert_eq!(commit_diff["data"]["totals"]["files"], 2);

        let response = router(state.clone())
            .oneshot(protected_json_request(
                "/api/v1/workspace/git/branches",
                json!({
                    "workspaceRoot": workspace.to_string_lossy(),
                    "name": "alternate",
                    "checkout": true
                }),
            ))
            .await
            .expect("create branch response");
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response_json(response).await["data"]["current"],
            "alternate"
        );

        let response = router(state.clone())
            .oneshot(protected_get_request(&format!(
                "/api/v1/workspace/git/branches?workspaceRoot={encoded}"
            )))
            .await
            .expect("branches response");
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response_json(response).await["data"]["branches"]
                .as_array()
                .map(Vec::len),
            Some(2)
        );

        let response = router(state.clone())
            .oneshot(protected_get_request(&format!(
                "/api/v1/workspace/git/timeline?workspaceRoot={encoded}"
            )))
            .await
            .expect("timeline response");
        assert_eq!(response.status(), StatusCode::OK);
        let timeline = response_json(response).await;
        assert_eq!(timeline["data"]["currentBranch"], "alternate");
        assert_eq!(timeline["data"]["branches"][0]["lane"], 0);
        assert_eq!(timeline["data"]["nodes"].as_array().map(Vec::len), Some(2));

        let response = router(state.clone())
            .oneshot(protected_json_request(
                "/api/v1/workspace/git/jump",
                json!({
                    "workspaceRoot": workspace.to_string_lossy(),
                    "commitId": baseline_id
                }),
            ))
            .await
            .expect("jump response");
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response_json(response).await["data"]["detached"], true);

        let response = router(state.clone())
            .oneshot(protected_json_request(
                "/api/v1/workspace/git/worldlines",
                json!({
                    "workspaceRoot": workspace.to_string_lossy(),
                    "fromCommit": baseline_id,
                    "name": "rewrite"
                }),
            ))
            .await
            .expect("create worldline response");
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response_json(response).await["data"]["worldline"],
            "rewrite"
        );

        let response = router(state.clone())
            .oneshot(protected_json_request(
                "/api/v1/workspace/git/worldlines/rename",
                json!({
                    "workspaceRoot": workspace.to_string_lossy(),
                    "name": "rewrite",
                    "newName": "rewrite-v2"
                }),
            ))
            .await
            .expect("rename worldline response");
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response_json(response).await["data"]["current"],
            "rewrite-v2"
        );

        let response = router(state.clone())
            .oneshot(protected_json_request(
                "/api/v1/workspace/git/checkout",
                json!({
                    "workspaceRoot": workspace.to_string_lossy(),
                    "name": "develop"
                }),
            ))
            .await
            .expect("checkout response");
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response_json(response).await["data"]["current"], "develop");

        let response = router(state)
            .oneshot(protected_json_request(
                "/api/v1/workspace/git/worldlines/delete",
                json!({
                    "workspaceRoot": workspace.to_string_lossy(),
                    "name": "rewrite-v2"
                }),
            ))
            .await
            .expect("delete worldline response");
        assert_eq!(response.status(), StatusCode::OK);
        let deleted = response_json(response).await;
        assert_eq!(deleted["data"]["deleted"], "rewrite-v2");
        assert_eq!(deleted["data"]["exclusiveCommits"], 0);
    }

    #[tokio::test]
    async fn rust_project_routes_reject_workspace_outside_fixture_boundary() {
        let root = tempdir().expect("root");
        let outside = tempdir().expect("outside");
        let (state, _) = followup_test_state(root.path());
        let encoded = encode_query_value(outside.path());
        let response = router(state)
            .oneshot(
                Request::builder()
                    .uri(format!(
                        "/api/v1/workspace/git/summary?workspaceRoot={encoded}"
                    ))
                    .header(header::AUTHORIZATION, "Bearer test-token")
                    .body(Body::empty())
                    .expect("outside request"),
            )
            .await
            .expect("outside response");
        assert_eq!(response.status(), StatusCode::FORBIDDEN);
        assert_eq!(
            response_json(response).await["error"]["code"],
            "workspace_outside_refactor_root"
        );
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
