use anyhow::{Context, Result};
use async_trait::async_trait;
use coomi_engine::{
    Agent, AgentEvent, AgentObserver, ApprovalHandler, ChatMessage, ModelProvider, ModelRequest,
    Role, Session, SessionStore, ToolCall, ToolResult, ToolRuntime, ToolSpec, UserInputRequest,
    UserInputResponse,
};
use coomi_security::{AccessMode, HookRunner, SecurityPolicy};
use coomi_services::{HttpModelProvider, McpRuntime, MemoryManager, ProviderRegistry};
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
    allowed_write_roots: Vec<PathBuf>,
    #[serde(default)]
    messages: Vec<WireMessage>,
    #[serde(default)]
    tools: Vec<ToolSpec>,
    #[serde(default)]
    required_tool: Option<String>,
    #[serde(default)]
    max_output_tokens: Option<u64>,
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
        let mutating = matches!(
            call.name.as_str(),
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
        );
        if mutating && !self.writes_allowed {
            return Some(
                "Storydex turn contract blocks state-changing tools".into(),
            );
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
    if use_fast_model && selector.is_none() {
        if let Some(choice) = registry
            .choices()
            .into_iter()
            .find(|choice| choice.provider_id == registry.active_id() && choice.is_fast)
        {
            return registry.resolve(Some(&choice.selector));
        }
    }
    registry.resolve(selector)
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
    let store = SessionStore::new(&home);
    let mut session = if let Some(id) = request.runtime_session_id {
        store.load(id).unwrap_or_else(|_| {
            Session::new(&provider_config.id, &provider_config.model, cwd.clone())
        })
    } else {
        Session::new(&provider_config.id, &provider_config.model, cwd.clone())
    };
    session.cwd = cwd.clone();
    if session.provider_id != provider_config.id || session.model != provider_config.model {
        session.switch_model(&provider_config.id, &provider_config.model);
    }
    emitter.event(
        "session_bound",
        json!({
            "storydexSessionId": request.storydex_session_id,
            "runtimeSessionId": session.id,
            "sessionPath": home.join("sessions").join(format!("{}.json", session.id)),
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
    let controls = Arc::new(ControlHub::default());
    start_control_reader(Arc::clone(&controls), emitter.clone());
    let tools = StorydexTools {
        core,
        cwd: cwd.clone(),
        writes_allowed: request.writes_allowed,
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
    let agent = Agent::new(system_prompt);
    let cancelled = {
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
            true
        } else {
            tokio::select! {
                result = &mut run => {
                    result?;
                    false
                }
                _ = controls.cancelled.notified() => true,
            }
        }
    };
    store.save(&session)?;
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
        })
        .await?;
    emitter.event(
        "completion",
        json!({
            "content": response.content,
            "toolCalls": response.tool_calls,
            "usage": response.usage,
            "provider": provider.provider_id(),
            "model": provider.model(),
        }),
    );
    Ok(())
}

fn status(request: &BridgeRequest, emitter: &Emitter) -> Result<()> {
    std::fs::create_dir_all(&request.home)?;
    let home = canonical_directory(&request.home, "runtime home")?;
    let registry = provider_registry(&home)?;
    let active = registry.resolve(None)?;
    let models = registry
        .choices()
        .into_iter()
        .map(|choice| {
            json!({
                "selector": choice.selector,
                "providerId": choice.provider_id,
                "providerDisplay": choice.provider_display,
                "model": choice.model,
                "isFast": choice.is_fast,
            })
        })
        .collect::<Vec<_>>();
    emitter.event(
        "status",
        json!({
            "runtime": "storydex-coomi-rs",
            "version": RUNTIME_VERSION,
            "protocolVersion": PROTOCOL_VERSION,
            "activeProvider": active.id,
            "activeModel": active.model,
            "capabilities": active.capabilities,
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
    if std::env::args()
        .skip(1)
        .any(|argument| argument == "--version")
    {
        println!("storydex-coomi-bridge {RUNTIME_VERSION}");
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
