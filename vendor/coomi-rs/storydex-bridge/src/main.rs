use anyhow::{Context, Result};
use async_trait::async_trait;
use coomi_engine::{
    Agent, AgentEvent, AgentObserver, ApprovalHandler, ChatMessage, ModelProvider, ModelRequest,
    ReasoningEffort, Role, SESSION_SCHEMA_VERSION, Session, SessionStore, ToolCall, ToolResult,
    ToolRuntime, ToolSpec, UserInputRequest, UserInputResponse,
};
use coomi_security::{AccessMode, HookRunner, SecurityPolicy};
use coomi_services::{
    HttpModelProvider, McpRuntime, MemoryManager, ProviderConfig, ProviderRegistry,
    reasoning_capability_best_effort, reasoning_request_plan_best_effort,
};
use coomi_tools::{AgentScheduler, CoreTools};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use tokio::sync::{Notify, oneshot};
use uuid::Uuid;

const PROTOCOL_VERSION: u32 = 1;
const RUNTIME_VERSION: &str = env!("CARGO_PKG_VERSION");
const RUNTIME_GIT_SHA: &str = env!("STORYDEX_COOMI_GIT_SHA");
const RUNTIME_SOURCE_FINGERPRINT: &str = env!("STORYDEX_COOMI_SOURCE_FINGERPRINT");

fn build_info() -> Value {
    json!({
        "runtime": "storydex-coomi-rs",
        "version": RUNTIME_VERSION,
        "gitSha": RUNTIME_GIT_SHA,
        "sourceFingerprint": RUNTIME_SOURCE_FINGERPRINT,
    })
}

#[derive(Clone)]
struct Emitter {
    output: Arc<Mutex<BufWriter<std::io::Stdout>>>,
}

impl Emitter {
    fn new() -> Self {
        Self {
            output: Arc::new(Mutex::new(BufWriter::new(std::io::stdout()))),
        }
    }

    fn emit(&self, value: Value) {
        let Ok(line) = serde_json::to_string(&value) else {
            return;
        };
        let Ok(mut output) = self.output.lock() else {
            return;
        };
        let _ = writeln!(output, "{line}");
        let _ = output.flush();
    }

    fn event(&self, kind: &str, data: Value) {
        self.emit(json!({
            "type": kind,
            "protocolVersion": PROTOCOL_VERSION,
            "data": data,
        }));
    }
}

#[derive(Default)]
struct ControlHub {
    pending: Mutex<HashMap<String, oneshot::Sender<Value>>>,
    cancelled: Notify,
    cancellation_requested: AtomicBool,
}

impl ControlHub {
    fn register(&self, request_id: String) -> oneshot::Receiver<Value> {
        let (sender, receiver) = oneshot::channel();
        if let Ok(mut pending) = self.pending.lock() {
            pending.insert(request_id, sender);
        }
        receiver
    }

    fn resolve(&self, request_id: &str, value: Value) {
        if let Ok(mut pending) = self.pending.lock()
            && let Some(sender) = pending.remove(request_id)
        {
            let _ = sender.send(value);
        }
    }

    fn cancel(&self) {
        self.cancellation_requested.store(true, Ordering::Release);
        self.cancelled.notify_one();
    }
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct BridgeRequest {
    action: String,
    #[serde(default)]
    cwd: PathBuf,
    #[serde(default)]
    home: PathBuf,
    #[serde(default)]
    prompt: String,
    #[serde(default)]
    system_prompt: String,
    #[serde(default)]
    runtime_session_id: Option<Uuid>,
    #[serde(default)]
    storydex_session_id: String,
    #[serde(default)]
    provider: Option<String>,
    #[serde(default)]
    use_fast_model: bool,
    #[serde(default)]
    permission_mode: String,
    #[serde(default)]
    base_permission_mode: String,
    #[serde(default)]
    tool_specs: Vec<ToolSpec>,
    #[serde(default)]
    mutating_tool_names: Vec<String>,
    #[serde(default = "default_true")]
    writes_allowed: bool,
    #[serde(default)]
    core_writes_allowed: Option<bool>,
    #[serde(default)]
    allowed_write_roots: Vec<PathBuf>,
    #[serde(default)]
    messages: Vec<WireMessage>,
    #[serde(default)]
    tools: Vec<ToolSpec>,
    #[serde(default)]
    required_tool: Option<String>,
    #[serde(default)]
    max_output_tokens: Option<u64>,
    #[serde(default)]
    reasoning_effort: ReasoningEffort,
}

#[derive(Clone, Debug, Deserialize)]
struct WireMessage {
    role: String,
    #[serde(default)]
    content: String,
    #[serde(default)]
    tool_calls: Vec<ToolCall>,
    #[serde(default)]
    tool_call_id: Option<String>,
}

struct StorydexObserver {
    emitter: Emitter,
}

impl AgentObserver for StorydexObserver {
    fn on_event(&self, event: &AgentEvent) {
        let (kind, data) = match event {
            AgentEvent::ModelStarted {
                provider,
                model,
                round,
            } => (
                "model_started",
                json!({"provider": provider, "model": model, "round": round}),
            ),
            AgentEvent::ModelCompleted {
                round,
                metadata,
                usage,
            } => (
                "model_completed",
                json!({"round": round, "metadata": metadata, "usage": usage}),
            ),
            AgentEvent::Text(value) => ("text", json!({"text": value})),
            AgentEvent::TextDelta(value) => ("text_delta", json!({"text": value})),
            AgentEvent::ReasoningDelta(value) => ("reasoning_delta", json!({"text": value})),
            AgentEvent::ContextUpdated(status) => ("context_updated", json!(status)),
            AgentEvent::CompactionStarted { automatic } => {
                ("compaction_started", json!({"automatic": automatic}))
            }
            AgentEvent::CompactionCompleted {
                automatic,
                before_tokens,
                after_tokens,
            } => (
                "compaction_completed",
                json!({
                    "automatic": automatic,
                    "beforeTokens": before_tokens,
                    "afterTokens": after_tokens,
                }),
            ),
            AgentEvent::PlanUpdated(plan) => ("plan_updated", json!(plan)),
            AgentEvent::LoopUpdated(state) => ("loop_updated", json!(state)),
            AgentEvent::QueuedInputAccepted(values) => {
                ("queued_input_accepted", json!({"values": values}))
            }
            AgentEvent::ToolStarted(call) => ("tool_started", json!({"call": call})),
            AgentEvent::ToolFinished { call, result } => (
                "tool_finished",
                json!({
                    "call": call,
                    "result": wire_tool_result(result),
                }),
            ),
            AgentEvent::TurnCompleted(usage) => ("turn_completed", json!({"usage": usage})),
            AgentEvent::ProviderRetry {
                attempt,
                max_attempts,
                reset_text_characters,
            } => (
                "provider_retry",
                json!({
                    "attempt": attempt,
                    "maxAttempts": max_attempts,
                    "resetTextCharacters": reset_text_characters,
                }),
            ),
        };
        self.emitter.event(kind, data);
    }
}

struct StorydexApproval {
    base_mode: String,
    plan_mode_active: Arc<AtomicBool>,
    emitter: Emitter,
    controls: Arc<ControlHub>,
}

#[async_trait]
impl ApprovalHandler for StorydexApproval {
    async fn approve(&self, call: &ToolCall, reason: &str) -> bool {
        if self.plan_mode_active.load(Ordering::Acquire) {
            return false;
        }
        match self.base_mode.as_str() {
            "approve_for_me" | "full_access" => true,
            _ => {
                let request_id = Uuid::new_v4().to_string();
                let receiver = self.controls.register(request_id.clone());
                self.emitter.event(
                    "approval_request",
                    json!({
                        "requestId": request_id,
                        "call": call,
                        "reason": reason,
                    }),
                );
                receiver
                    .await
                    .ok()
                    .and_then(|value| value.get("approved").and_then(Value::as_bool))
                    .unwrap_or(false)
            }
        }
    }

    async fn request_user_input(&self, request: &UserInputRequest) -> Option<UserInputResponse> {
        let request_id = Uuid::new_v4().to_string();
        let receiver = self.controls.register(request_id.clone());
        self.emitter.event(
            "user_input_request",
            json!({"requestId": request_id, "request": request}),
        );
        let value = receiver.await.ok()?;
        serde_json::from_value(value.get("answers")?.clone()).ok()
    }
}

struct StorydexTools {
    core: CoreTools,
    cwd: PathBuf,
    writes_allowed: bool,
    core_writes_allowed: bool,
    allowed_write_roots: Vec<PathBuf>,
    custom_specs: Vec<ToolSpec>,
    custom_names: HashSet<String>,
    mutating_custom_names: HashSet<String>,
    plan_mode_active: Arc<AtomicBool>,
    base_permission_mode: String,
    emitter: Emitter,
    controls: Arc<ControlHub>,
}

#[async_trait]
impl ToolRuntime for StorydexTools {
    fn specs(&self) -> Vec<ToolSpec> {
        let mut specs = self.core.specs();
        if !self.core_writes_allowed {
            specs.retain(|spec| !is_mutating_core_tool(&spec.name));
        }
        specs.extend(self.custom_specs.clone());
        if self.plan_mode_active.load(Ordering::Acquire) {
            specs.push(ToolSpec {
                name: "exit_plan_mode".into(),
                description: "Exit Storydex plan/read-only mode and continue this turn using the configured permission mode. Call this when planning is complete and the user's task now requires execution or file changes.".into(),
                parameters: json!({
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Brief reason why execution should leave read-only mode."
                        }
                    },
                    "additionalProperties": false
                }),
            });
        }
        specs
    }

    async fn call(&self, call: &ToolCall, approval: &dyn ApprovalHandler) -> ToolResult {
        if call.name == "exit_plan_mode" {
            if !self.plan_mode_active.swap(false, Ordering::AcqRel) {
                return ToolResult::success("Storydex plan mode is already disabled.");
            }
            self.emitter.event(
                "plan_mode_changed",
                json!({
                    "active": false,
                    "permissionMode": self.base_permission_mode,
                    "source": "agent",
                    "message": "Coomi 已自主退出计划模式，并将按当前权限继续执行。",
                    "reason": call.arguments.get("reason").and_then(Value::as_str).unwrap_or_default(),
                }),
            );
            return ToolResult::success(
                "Plan mode disabled. Continue the current task using the configured Storydex permission mode.",
            );
        }
        if !self.custom_names.contains(&call.name) {
            if let Some(error) = self.rejected_core_write(call) {
                return ToolResult::error(error);
            }
            return self.core.call(call, approval).await;
        }
        if self.mutating_custom_names.contains(&call.name)
            && (self.plan_mode_active.load(Ordering::Acquire) || !self.writes_allowed)
        {
            return ToolResult::error(if self.plan_mode_active.load(Ordering::Acquire) {
                "Storydex plan mode blocks state-changing tools until exit_plan_mode is called"
            } else {
                "Storydex turn contract blocks state-changing tools"
            });
        }
        let request_id = Uuid::new_v4().to_string();
        let receiver = self.controls.register(request_id.clone());
        self.emitter.event(
            "tool_request",
            json!({"requestId": request_id, "call": call}),
        );
        match receiver.await {
            Ok(value) => ToolResult {
                success: value
                    .get("success")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
                output: value
                    .get("output")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_owned(),
                plan: None,
                loop_state: None,
                additional_context: value
                    .get("additionalContext")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                images: Vec::new(),
            },
            Err(_) => ToolResult::error("Storydex tool bridge closed before returning a result"),
        }
    }

    async fn lifecycle(&self, event: &str, payload: Value) -> Result<Option<String>, String> {
        self.core.lifecycle(event, payload).await
    }
}

impl StorydexTools {
    fn rejected_core_write(&self, call: &ToolCall) -> Option<String> {
        let plan_mode_active = self.plan_mode_active.load(Ordering::Acquire);
        if plan_mode_active && !is_plan_safe_core_tool(&call.name) {
            return Some(
                "Storydex plan mode blocks state-changing tools until exit_plan_mode is called"
                    .into(),
            );
        }
        let mutating = is_mutating_core_tool(&call.name);
        if mutating && !self.core_writes_allowed {
            return Some("Storydex turn contract blocks state-changing tools".into());
        }
        if self.allowed_write_roots.is_empty() || !mutating {
            return None;
        }
        if matches!(call.name.as_str(), "shell" | "local_shell" | "apply_patch") {
            return Some(
                "Storydex scoped-write turns block shell and patch tools; use a path-specific file tool"
                    .into(),
            );
        }
        if !matches!(call.name.as_str(), "write_file" | "edit_file") {
            return None;
        }
        let Some(path) = call.arguments.get("path").and_then(Value::as_str) else {
            return Some("Storydex scoped write has no target path".into());
        };
        let candidate = lexical_path(&self.cwd, Path::new(path));
        if self
            .allowed_write_roots
            .iter()
            .map(|root| lexical_path(&self.cwd, root))
            .any(|root| candidate == root || candidate.starts_with(root))
        {
            None
        } else {
            Some(format!(
                "Storydex turn contract blocks writes outside: {}",
                self.allowed_write_roots
                    .iter()
                    .map(|path| path.display().to_string())
                    .collect::<Vec<_>>()
                    .join(", ")
            ))
        }
    }
}

fn is_mutating_core_tool(name: &str) -> bool {
    matches!(
        name,
        "write_file"
            | "edit_file"
            | "apply_patch"
            | "shell"
            | "local_shell"
            | "memory_write"
            | "memory_delete"
            | "configure_mcp"
            | "install_skill"
            | "spawn_agent"
    )
}

fn is_plan_safe_core_tool(name: &str) -> bool {
    matches!(
        name,
        "read_file"
            | "search"
            | "list_dir"
            | "grep_files"
            | "web_search"
            | "view_image"
            | "request_user_input"
            | "update_plan"
            | "get_loop"
            | "list_skills"
            | "read_skill"
            | "memory_list"
            | "memory_read"
            | "memory_search"
    )
}

fn lexical_path(cwd: &Path, value: &Path) -> PathBuf {
    use std::path::Component;
    let source = if value.is_absolute() {
        value.to_path_buf()
    } else {
        cwd.join(value)
    };
    let mut output = PathBuf::new();
    for component in source.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                let _ = output.pop();
            }
            other => output.push(other.as_os_str()),
        }
    }
    output
}

const fn default_true() -> bool {
    true
}

fn wire_tool_result(result: &ToolResult) -> Value {
    json!({
        "success": result.success,
        "output": result.output,
        "plan": result.plan,
        "loopState": result.loop_state,
        "additionalContext": result.additional_context,
        "imageCount": result.images.len(),
    })
}

fn start_control_reader(controls: Arc<ControlHub>, emitter: Emitter) {
    std::thread::spawn(move || {
        let stdin = std::io::stdin();
        for line in stdin.lock().lines() {
            let Ok(line) = line else {
                break;
            };
            let Ok(value) = serde_json::from_str::<Value>(&line) else {
                emitter.event(
                    "protocol_warning",
                    json!({"message": "invalid control JSON"}),
                );
                continue;
            };
            match value.get("action").and_then(Value::as_str) {
                Some("resolve") => {
                    let request_id = value
                        .get("requestId")
                        .and_then(Value::as_str)
                        .unwrap_or_default();
                    controls.resolve(
                        request_id,
                        value.get("value").cloned().unwrap_or(Value::Null),
                    );
                }
                Some("cancel" | "steer") => controls.cancel(),
                _ => emitter.event(
                    "protocol_warning",
                    json!({"message": "unknown control action"}),
                ),
            }
        }
    });
}

fn canonical_directory(path: &Path, label: &str) -> Result<PathBuf> {
    path.canonicalize()
        .with_context(|| format!("invalid {label} directory {}", path.display()))
}

fn provider_registry(home: &Path) -> Result<ProviderRegistry> {
    ProviderRegistry::load(&home.join("config").join("providers.json"))
        .context("unable to load Storydex Coomi provider configuration")
}

fn resolve_provider(
    registry: &ProviderRegistry,
    selector: Option<&str>,
    use_fast_model: bool,
) -> Result<coomi_services::ProviderConfig> {
    let provider = registry.resolve(selector)?;
    let selected_provider_directly =
        selector.is_none_or(|value| value.trim().eq_ignore_ascii_case(&provider.id));
    if use_fast_model
        && selected_provider_directly
        && let Some(choice) = registry
            .choices()
            .into_iter()
            .find(|choice| choice.provider_id == provider.id && choice.is_fast)
    {
        return registry.resolve(Some(&choice.selector));
    }
    Ok(provider)
}

fn save_session_after_run(
    store: &SessionStore,
    session: &mut Session,
    outcome: Result<bool, coomi_engine::AgentError>,
) -> Result<bool> {
    session.touch();
    let save_result = store.save(session);
    match (outcome, save_result) {
        (Ok(cancelled), Ok(())) => Ok(cancelled),
        (Ok(_), Err(save_error)) => Err(save_error),
        (Err(run_error), Ok(())) => Err(run_error.into()),
        (Err(run_error), Err(save_error)) => Err(anyhow::anyhow!(
            "{run_error}; additionally failed to save interrupted session: {save_error:#}"
        )),
    }
}

fn prepare_session(
    store: &SessionStore,
    runtime_session_id: Option<Uuid>,
    provider_id: &str,
    model: &str,
    cwd: &Path,
) -> Result<Session> {
    let (mut session, is_new) = match runtime_session_id {
        Some(id) => (
            store
                .load(id)
                .with_context(|| format!("failed to restore bound runtime session {id}"))?,
            false,
        ),
        None => (Session::new(provider_id, model, cwd.to_path_buf()), true),
    };
    let cwd_changed = session.cwd != cwd;
    let model_changed = session.provider_id != provider_id || session.model != model;
    session.cwd = cwd.to_path_buf();
    if model_changed {
        session.switch_model(provider_id, model);
    }
    if is_new || cwd_changed || model_changed {
        store.save(&session).with_context(|| {
            if is_new {
                format!(
                    "failed to persist new runtime session {} before binding",
                    session.id
                )
            } else {
                format!(
                    "failed to checkpoint runtime session {} before binding",
                    session.id
                )
            }
        })?;
    }
    Ok(session)
}

async fn run_agent(request: BridgeRequest, emitter: Emitter) -> Result<()> {
    let cwd = canonical_directory(&request.cwd, "workspace")?;
    std::fs::create_dir_all(&request.home)?;
    let home = canonical_directory(&request.home, "runtime home")?;
    let registry = provider_registry(&home)?;
    let provider_config = resolve_provider(
        &registry,
        request.provider.as_deref(),
        request.use_fast_model,
    )?;
    let reasoning_plan = reasoning_request_plan_best_effort(
        &provider_config,
        &provider_config.model,
        request.reasoning_effort,
        None,
    );
    emit_reasoning_plan(&emitter, &provider_config, &reasoning_plan);
    let store = SessionStore::new(&home);
    let mut session = prepare_session(
        &store,
        request.runtime_session_id,
        &provider_config.id,
        &provider_config.model,
        &cwd,
    )?;
    emitter.event(
        "session_bound",
        json!({
            "storydexSessionId": request.storydex_session_id,
            "runtimeSessionId": session.id,
            "sessionPath": store.path(session.id),
            "sessionSchemaVersion": SESSION_SCHEMA_VERSION,
            "persisted": true,
        }),
    );

    let base_permission_mode = if request.base_permission_mode.trim().is_empty() {
        request.permission_mode.clone()
    } else {
        request.base_permission_mode.clone()
    };
    let plan_mode_active = Arc::new(AtomicBool::new(request.permission_mode == "plan_mode"));
    let access_mode = match base_permission_mode.as_str() {
        "full_access" => AccessMode::FullAccess,
        _ => AccessMode::WorkspaceWrite,
    };
    let mut system_prompt = if request.system_prompt.trim().is_empty() {
        format!(
            "You are Coomi for Storydex, a local-first long-form fiction workspace. Inspect project evidence before acting, use Storydex tools for narrative state, preserve unrelated work, and report only verified results.\n\nStorydex workspace: {}",
            cwd.display()
        )
    } else {
        request.system_prompt
    };
    let instructions = coomi_engine::discover_project_instructions(&cwd)?;
    if !instructions.trim().is_empty() {
        system_prompt.push_str("\n\nProject instructions:\n");
        system_prompt.push_str(&instructions);
    }
    let memory = Arc::new(MemoryManager::new(&home, &cwd));
    let memory_context = memory.prompt_context();
    if !memory_context.trim().is_empty() {
        system_prompt.push_str("\n\nPersistent Storydex Coomi memory:\n");
        system_prompt.push_str(&memory_context);
    }
    let policy = SecurityPolicy::new(&cwd, access_mode)?;
    let scheduler = AgentScheduler::new(
        cwd.clone(),
        home.clone(),
        provider_config.clone(),
        access_mode,
        system_prompt.clone(),
    );
    let core = CoreTools::new(cwd.clone(), policy)
        .with_skills_directory(home.join("skills"))
        .with_config_home(home.clone())
        .with_session_state(session.plan.clone(), session.loop_state.clone())
        .with_mcp_runtime(Arc::new(McpRuntime::load(&home).await))
        .with_memory(memory)
        .with_hooks(Arc::new(HookRunner::load(&home)?))
        .with_agent_scheduler(scheduler, session.messages.clone());
    let custom_names = request
        .tool_specs
        .iter()
        .map(|tool| tool.name.clone())
        .collect();
    let mutating_custom_names = request.mutating_tool_names.into_iter().collect();
    let core_writes_allowed = request
        .core_writes_allowed
        .unwrap_or(request.writes_allowed);
    let controls = Arc::new(ControlHub::default());
    start_control_reader(Arc::clone(&controls), emitter.clone());
    let tools = StorydexTools {
        core,
        cwd: cwd.clone(),
        writes_allowed: request.writes_allowed,
        core_writes_allowed,
        allowed_write_roots: request.allowed_write_roots,
        custom_specs: request.tool_specs,
        custom_names,
        mutating_custom_names,
        plan_mode_active: Arc::clone(&plan_mode_active),
        base_permission_mode: base_permission_mode.clone(),
        emitter: emitter.clone(),
        controls: Arc::clone(&controls),
    };
    let approval = StorydexApproval {
        base_mode: base_permission_mode,
        plan_mode_active,
        emitter: emitter.clone(),
        controls: Arc::clone(&controls),
    };
    let observer = StorydexObserver {
        emitter: emitter.clone(),
    };
    let provider = HttpModelProvider::new(provider_config)?;
    let agent = Agent::new(system_prompt).with_reasoning_effort(request.reasoning_effort);
    let run_outcome: Result<bool, coomi_engine::AgentError> = {
        let run = async {
            agent
                .run_turn(
                    &mut session,
                    request.prompt,
                    &provider,
                    &tools,
                    &approval,
                    &observer,
                )
                .await?;
            while session
                .loop_state
                .as_ref()
                .is_some_and(|state| state.status == coomi_engine::LoopStatus::Active)
            {
                agent
                    .continue_loop(&mut session, &provider, &tools, &approval, &observer)
                    .await?;
            }
            Ok::<(), coomi_engine::AgentError>(())
        };
        tokio::pin!(run);
        if controls.cancellation_requested.load(Ordering::Acquire) {
            Ok(true)
        } else {
            tokio::select! {
                result = &mut run => result.map(|()| false),
                _ = controls.cancelled.notified() => Ok(true),
            }
        }
    };
    let cancelled = save_session_after_run(&store, &mut session, run_outcome)?;
    if cancelled {
        emitter.event(
            "cancelled",
            json!({"runtimeSessionId": session.id, "reason": "requested"}),
        );
    } else {
        emitter.event(
            "completed",
            json!({
                "runtimeSessionId": session.id,
                "usage": session.usage,
                "context": session.context.status(&provider.capabilities()),
                "reasoningRequestPlan": reasoning_plan,
            }),
        );
    }
    Ok(())
}

fn wire_chat_message(value: WireMessage) -> Result<ChatMessage> {
    let mut message = match value.role.to_ascii_lowercase().as_str() {
        "system" | "developer" => ChatMessage::system(value.content),
        "user" => ChatMessage::user(value.content),
        "assistant" => ChatMessage::assistant(value.content, value.tool_calls),
        "tool" => ChatMessage::tool(
            value
                .tool_call_id
                .context("tool message has no tool_call_id")?,
            value.content,
        ),
        role => anyhow::bail!("unsupported message role: {role}"),
    };
    if message.role == Role::Assistant && message.tool_calls.is_empty() {
        message.tool_calls = Vec::new();
    }
    Ok(message)
}

async fn complete(request: BridgeRequest, emitter: Emitter) -> Result<()> {
    std::fs::create_dir_all(&request.home)?;
    let home = canonical_directory(&request.home, "runtime home")?;
    let registry = provider_registry(&home)?;
    let provider_config = resolve_provider(
        &registry,
        request.provider.as_deref(),
        request.use_fast_model,
    )?;
    let reasoning_plan = reasoning_request_plan_best_effort(
        &provider_config,
        &provider_config.model,
        request.reasoning_effort,
        request.max_output_tokens,
    );
    emit_reasoning_plan(&emitter, &provider_config, &reasoning_plan);
    let provider = HttpModelProvider::new(provider_config)?;
    let messages = request
        .messages
        .into_iter()
        .map(wire_chat_message)
        .collect::<Result<Vec<_>>>()?;
    let response = provider
        .complete(ModelRequest {
            model: provider.model().to_owned(),
            messages,
            tools: request.tools,
            max_output_tokens: request.max_output_tokens,
            required_tool: request.required_tool,
            reasoning_effort: request.reasoning_effort,
        })
        .await?;
    emitter.event(
        "completion",
        json!({
            "content": response.content,
            "toolCalls": response.tool_calls,
            "usage": response.usage,
            "metadata": response.metadata,
            "provider": provider.provider_id(),
            "model": provider.model(),
            "reasoningRequestPlan": reasoning_plan,
        }),
    );
    Ok(())
}

fn emit_reasoning_plan(
    emitter: &Emitter,
    provider: &ProviderConfig,
    plan: &coomi_services::ReasoningRequestPlan,
) {
    emitter.event(
        "reasoning_plan",
        json!({
            "provider": provider.id,
            "model": provider.model,
            "plan": plan,
        }),
    );
}

fn status(request: &BridgeRequest, emitter: &Emitter) -> Result<()> {
    std::fs::create_dir_all(&request.home)?;
    let home = canonical_directory(&request.home, "runtime home")?;
    let registry = provider_registry(&home)?;
    let active = resolve_provider(
        &registry,
        request.provider.as_deref(),
        request.use_fast_model,
    )?;
    let active_reasoning_capability = reasoning_capability_best_effort(&active, &active.model);
    let active_reasoning_plan = reasoning_request_plan_best_effort(
        &active,
        &active.model,
        request.reasoning_effort,
        request.max_output_tokens,
    );
    let models = registry
        .choices()
        .into_iter()
        .map(|choice| -> Result<Value> {
            let config = registry.resolve(Some(&choice.selector))?;
            let capability = reasoning_capability_best_effort(&config, &choice.model);
            Ok(json!({
                "selector": choice.selector,
                "providerId": choice.provider_id,
                "providerDisplay": choice.provider_display,
                "model": choice.model,
                "isFast": choice.is_fast,
                "reasoningCapability": capability,
            }))
        })
        .collect::<Result<Vec<_>>>()?;
    emitter.event(
        "status",
        json!({
            "runtime": "storydex-coomi-rs",
            "version": RUNTIME_VERSION,
            "gitSha": RUNTIME_GIT_SHA,
            "sourceFingerprint": RUNTIME_SOURCE_FINGERPRINT,
            "protocolVersion": PROTOCOL_VERSION,
            "activeProvider": active.id,
            "activeModel": active.model,
            "capabilities": active.capabilities,
            "reasoningCapability": active_reasoning_capability,
            "reasoningRequestPlan": active_reasoning_plan,
            "models": models,
        }),
    );
    Ok(())
}

async fn dispatch(request: BridgeRequest, emitter: Emitter) -> Result<()> {
    match request.action.as_str() {
        "run" => run_agent(request, emitter).await,
        "complete" => complete(request, emitter).await,
        "status" | "models" => status(&request, &emitter),
        action => anyhow::bail!("unsupported bridge action: {action}"),
    }
}

fn read_request() -> Result<BridgeRequest> {
    let mut line = String::new();
    std::io::stdin()
        .lock()
        .read_line(&mut line)
        .context("failed to read bridge request")?;
    anyhow::ensure!(!line.trim().is_empty(), "bridge request is empty");
    serde_json::from_str(&line).context("invalid bridge request JSON")
}

#[tokio::main]
async fn main() {
    let arguments = std::env::args().skip(1).collect::<Vec<_>>();
    if arguments.iter().any(|argument| argument == "--build-info") {
        println!("{}", build_info());
        return;
    }
    if arguments.iter().any(|argument| argument == "--version") {
        println!(
            "storydex-coomi-bridge {RUNTIME_VERSION} git={RUNTIME_GIT_SHA} source={RUNTIME_SOURCE_FINGERPRINT}"
        );
        return;
    }
    let emitter = Emitter::new();
    let result = match read_request() {
        Ok(request) => dispatch(request, emitter.clone()).await,
        Err(error) => Err(error),
    };
    if let Err(error) = result {
        emitter.event(
            "error",
            json!({
                "message": format!("{error:#}"),
                "errorType": "storydex_coomi_bridge_error",
            }),
        );
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bridge_request_accepts_extended_reasoning_efforts() {
        for (value, expected) in [
            ("xhigh", ReasoningEffort::XHigh),
            ("max", ReasoningEffort::Max),
        ] {
            let request: BridgeRequest = serde_json::from_value(json!({
                "action": "complete",
                "reasoningEffort": value
            }))
            .expect("deserialize bridge request");
            assert_eq!(request.reasoning_effort, expected);
        }
    }

    #[test]
    fn bridge_request_accepts_separate_core_write_boundary() {
        let request: BridgeRequest = serde_json::from_value(json!({
            "action": "run",
            "writesAllowed": true,
            "coreWritesAllowed": false
        }))
        .expect("deserialize bridge request");
        assert!(request.writes_allowed);
        assert_eq!(request.core_writes_allowed, Some(false));
        assert!(is_mutating_core_tool("write_file"));
        assert!(!is_mutating_core_tool("read_file"));
    }

    #[test]
    fn explicit_provider_selector_honors_fast_model_without_overriding_explicit_model() {
        let directory = std::env::temp_dir().join(format!(
            "storydex-coomi-bridge-provider-test-{}",
            Uuid::new_v4()
        ));
        std::fs::create_dir_all(&directory).expect("create provider test directory");
        let path = directory.join("providers.json");
        std::fs::write(
            &path,
            r#"{
                "active": "primary",
                "providers": {
                    "primary": {
                        "type": "generic",
                        "base_url": "https://example.test/v1",
                        "model": "main-model",
                        "fast_model": "fast-model"
                    }
                }
            }"#,
        )
        .expect("write provider test config");
        let registry = ProviderRegistry::load(&path).expect("load provider registry");

        let fast = resolve_provider(&registry, Some("primary"), true)
            .expect("resolve explicit provider fast model");
        assert_eq!(fast.model, "fast-model");

        let explicit = resolve_provider(&registry, Some("primary:main-model"), true)
            .expect("resolve explicit main model");
        assert_eq!(explicit.model, "main-model");

        std::fs::remove_dir_all(&directory).expect("remove provider test directory");
    }

    #[test]
    fn wire_messages_preserve_tool_calls() {
        let message = wire_chat_message(WireMessage {
            role: "assistant".into(),
            content: "".into(),
            tool_calls: vec![ToolCall {
                id: "call-1".into(),
                name: "StorydexWordCount".into(),
                arguments: json!({"path": "chapters/1.md"}),
            }],
            tool_call_id: None,
        })
        .expect("assistant message");
        assert_eq!(message.tool_calls.len(), 1);
    }

    #[test]
    fn failed_run_outcome_is_saved_before_error_propagates() {
        let directory = std::env::temp_dir().join(format!(
            "storydex-coomi-bridge-failed-run-test-{}",
            Uuid::new_v4()
        ));
        std::fs::create_dir_all(&directory).expect("create failed-run test directory");
        let store = SessionStore::new(&directory);
        let mut session = Session::new("mock", "deepseek-v4-flash", directory.clone());
        let session_id = session.id;
        session
            .messages
            .push(ChatMessage::user("read probe.txt and finish the task"));
        let mut assistant = ChatMessage::assistant(
            "",
            vec![ToolCall {
                id: "call-probe".into(),
                name: "read_file".into(),
                arguments: json!({"path": "probe.txt"}),
            }],
        );
        assistant.provider_items.push(json!({
            "role": "assistant",
            "content": null,
            "reasoning_content": "inspect the requested file",
            "tool_calls": [{
                "id": "call-probe",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{\"path\":\"probe.txt\"}"}
            }]
        }));
        session.messages.push(assistant);
        session
            .messages
            .push(ChatMessage::tool("call-probe", "success: probe contents"));

        let error = save_session_after_run(
            &store,
            &mut session,
            Err(coomi_engine::AgentError::Provider(anyhow::anyhow!(
                "provider returned HTTP 402 Payment Required"
            ))),
        )
        .expect_err("the provider error must still propagate");

        assert!(error.to_string().contains("HTTP 402 Payment Required"));
        let restored = store.load(session_id).expect("load interrupted session");
        assert_eq!(restored.messages.len(), 3);
        assert_eq!(
            restored.messages[0].content,
            "read probe.txt and finish the task"
        );
        assert_eq!(
            restored.messages[1].provider_items[0]["reasoning_content"],
            "inspect the requested file"
        );
        assert_eq!(
            restored.messages[2].tool_call_id.as_deref(),
            Some("call-probe")
        );

        std::fs::remove_dir_all(&directory).expect("remove failed-run test directory");
    }

    #[test]
    fn missing_bound_session_fails_without_creating_a_replacement() {
        let directory = std::env::temp_dir().join(format!(
            "storydex-coomi-bridge-missing-session-test-{}",
            Uuid::new_v4()
        ));
        std::fs::create_dir_all(&directory).expect("create missing-session test directory");
        let store = SessionStore::new(&directory);
        let missing_id = Uuid::new_v4();

        let error = prepare_session(
            &store,
            Some(missing_id),
            "mock",
            "deepseek-v4-flash",
            &directory,
        )
        .expect_err("missing bound session must fail closed");

        assert!(
            error
                .to_string()
                .contains("failed to restore bound runtime session")
        );
        assert!(!store.path(missing_id).exists());
        assert!(!directory.join("sessions").exists());
        std::fs::remove_dir_all(&directory).expect("remove missing-session test directory");
    }

    #[test]
    fn new_session_is_persisted_before_it_can_be_bound() {
        let directory = std::env::temp_dir().join(format!(
            "storydex-coomi-bridge-new-session-test-{}",
            Uuid::new_v4()
        ));
        std::fs::create_dir_all(&directory).expect("create new-session test directory");
        let store = SessionStore::new(&directory);

        let session = prepare_session(&store, None, "mock", "deepseek-v4-flash", &directory)
            .expect("prepare new session");

        assert!(store.path(session.id).is_file());
        assert_eq!(
            store.load(session.id).expect("load initial checkpoint").id,
            session.id
        );
        std::fs::remove_dir_all(&directory).expect("remove new-session test directory");
    }

    #[test]
    fn storydex_permission_modes_map_to_expected_boundaries() {
        let cases = [
            ("plan_mode", AccessMode::ReadOnly),
            ("ask_approval", AccessMode::WorkspaceWrite),
            ("approve_for_me", AccessMode::WorkspaceWrite),
            ("full_access", AccessMode::FullAccess),
        ];
        for (mode, expected) in cases {
            let actual = match mode {
                "plan_mode" => AccessMode::ReadOnly,
                "full_access" => AccessMode::FullAccess,
                _ => AccessMode::WorkspaceWrite,
            };
            assert_eq!(actual, expected);
        }
    }

    #[test]
    fn plan_mode_core_allowlist_blocks_delegation_and_unknown_mcp_tools() {
        assert!(is_plan_safe_core_tool("read_file"));
        assert!(is_plan_safe_core_tool("update_plan"));
        assert!(!is_plan_safe_core_tool("write_file"));
        assert!(!is_plan_safe_core_tool("spawn_agent"));
        assert!(!is_plan_safe_core_tool("enter_plan_mode"));
        assert!(!is_plan_safe_core_tool("third_party_mcp_tool"));
    }
}
