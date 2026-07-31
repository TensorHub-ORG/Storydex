mod agents;
mod patch;
mod processes;

use async_trait::async_trait;
use base64::Engine;
use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use coomi_catalogs::CatalogInstaller;
use coomi_engine::ApprovalHandler;
use coomi_engine::LoopState;
use coomi_engine::LoopStatus;
use coomi_engine::PlanState;
use coomi_engine::ToolCall;
use coomi_engine::ToolResult;
use coomi_engine::ToolRuntime;
use coomi_engine::ToolSpec;
use coomi_security::Decision;
use coomi_security::HookEvent;
use coomi_security::HookRunner;
use coomi_security::SecurityPolicy;
use coomi_services::AutoConfigIntent;
use coomi_services::McpRuntime;
use coomi_services::MemoryManager;
use coomi_services::MemoryScope;
use coomi_services::MemoryType;
use coomi_services::apply_auto_config;
use ignore::WalkBuilder;
use regex::Regex;
use serde::Deserialize;
use serde_json::Value;
use serde_json::json;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;
use tokio::process::Command;

pub use crate::agents::AgentScheduler;
use crate::agents::snapshots_json;
use crate::processes::ProcessManager;

const DEFAULT_MAX_OUTPUT: usize = 48_000;
const DEFAULT_TIMEOUT_MS: u64 = 30_000;

pub struct CoreTools {
    cwd: PathBuf,
    policy: SecurityPolicy,
    skills_directory: Option<PathBuf>,
    config_home: Option<PathBuf>,
    max_output: usize,
    processes: ProcessManager,
    plan: Arc<Mutex<Option<PlanState>>>,
    loop_state: Arc<Mutex<Option<LoopState>>>,
    agent_scheduler: Option<Arc<AgentScheduler>>,
    mcp_runtime: Option<Arc<McpRuntime>>,
    memory: Option<Arc<MemoryManager>>,
    hooks: Option<Arc<HookRunner>>,
    parent_history: Vec<coomi_engine::ChatMessage>,
}

impl CoreTools {
    pub fn new(cwd: PathBuf, policy: SecurityPolicy) -> Self {
        Self {
            cwd,
            policy,
            skills_directory: None,
            config_home: None,
            max_output: DEFAULT_MAX_OUTPUT,
            processes: ProcessManager::default(),
            plan: Arc::new(Mutex::new(None)),
            loop_state: Arc::new(Mutex::new(None)),
            agent_scheduler: None,
            mcp_runtime: None,
            memory: None,
            hooks: None,
            parent_history: Vec::new(),
        }
    }

    pub fn with_agent_scheduler(
        mut self,
        scheduler: Arc<AgentScheduler>,
        parent_history: Vec<coomi_engine::ChatMessage>,
    ) -> Self {
        self.agent_scheduler = Some(scheduler);
        self.parent_history = parent_history;
        self
    }

    pub fn with_session_state(
        mut self,
        plan: Option<PlanState>,
        loop_state: Option<LoopState>,
    ) -> Self {
        self.plan = Arc::new(Mutex::new(plan));
        self.loop_state = Arc::new(Mutex::new(loop_state));
        self
    }

    pub fn with_skills_directory(mut self, directory: PathBuf) -> Self {
        self.skills_directory = Some(directory);
        self
    }

    pub fn with_config_home(mut self, home: PathBuf) -> Self {
        self.config_home = Some(home);
        self
    }

    pub fn with_mcp_runtime(mut self, runtime: Arc<McpRuntime>) -> Self {
        self.mcp_runtime = Some(runtime);
        self
    }

    pub fn with_memory(mut self, memory: Arc<MemoryManager>) -> Self {
        self.memory = Some(memory);
        self
    }

    pub fn with_hooks(mut self, hooks: Arc<HookRunner>) -> Self {
        self.hooks = Some(hooks);
        self
    }

    pub fn policy(&self) -> &SecurityPolicy {
        &self.policy
    }

    async fn dispatch(&self, call: &ToolCall, approval: &dyn ApprovalHandler) -> ToolResult {
        match call.name.as_str() {
            "read_file" => self.read_file(&call.arguments).await,
            "write_file" => self.write_file(&call.arguments).await,
            "edit_file" => self.edit_file(&call.arguments).await,
            "list_dir" => self.list_dir(&call.arguments),
            "grep_files" | "search" => self.search(&call.arguments).await,
            "local_shell" => self.local_shell(call, approval).await,
            "shell" => self.shell(call, approval).await,
            "apply_patch" => self.apply_patch(call, approval).await,
            "web_search" => self.web_search(&call.arguments).await,
            "view_image" => self.view_image(&call.arguments).await,
            "request_user_input" => self.request_user_input(&call.arguments, approval).await,
            "update_plan" => self.update_plan(&call.arguments),
            "create_loop" => self.create_loop(&call.arguments),
            "get_loop" => self.get_loop(),
            "update_loop" => self.update_loop(&call.arguments),
            "spawn_agent" => self.spawn_agent(&call.arguments).await,
            "wait_agent" => self.wait_agent(&call.arguments).await,
            "close_agent" => self.close_agent(&call.arguments).await,
            "list_skills" => self.list_skills(),
            "read_skill" => self.read_skill(&call.arguments).await,
            "memory_list" => self.memory_list(),
            "memory_read" => self.memory_read(&call.arguments),
            "memory_search" => self.memory_search(&call.arguments),
            "memory_write" => self.memory_write(call, approval).await,
            "memory_delete" => self.memory_delete(call, approval).await,
            "configure_mcp" => self.configure_mcp(call, approval).await,
            "install_skill" => self.install_skill(call, approval).await,
            _ => {
                if let Some(runtime) = &self.mcp_runtime
                    && let Some(result) = runtime.call(&call.name, call.arguments.clone()).await
                {
                    return result;
                }
                ToolResult::error(format!("unknown tool: {}", call.name))
            }
        }
    }

    async fn configure_mcp(&self, call: &ToolCall, approval: &dyn ApprovalHandler) -> ToolResult {
        let Some(home) = &self.config_home else {
            return ToolResult::error("Coomi configuration directory is not available");
        };
        if !approval
            .approve(
                call,
                "configure_mcp will modify the Coomi MCP configuration",
            )
            .await
        {
            return ToolResult::error("MCP configuration was not approved");
        }
        if let Some(catalog_id) = string_arg(&call.arguments, "catalog_id") {
            let values = call
                .arguments
                .get("values")
                .and_then(Value::as_object)
                .map(|values| {
                    values
                        .iter()
                        .filter_map(|(key, value)| {
                            value.as_str().map(|value| (key.clone(), value.to_owned()))
                        })
                        .collect::<std::collections::BTreeMap<_, _>>()
                })
                .unwrap_or_default();
            return match CatalogInstaller::new(home).install_mcp(catalog_id, &values) {
                Ok(path) => ToolResult::success(format!(
                    "Configured catalog MCP `{catalog_id}` at {}",
                    path.display()
                )),
                Err(error) => ToolResult::error(format!("{error:#}")),
            };
        }

        let Some(name) = string_arg(&call.arguments, "name") else {
            return ToolResult::error("missing string argument: name or catalog_id");
        };
        let Some(config) = call.arguments.get("config").cloned() else {
            return ToolResult::error("missing object argument: config");
        };
        if !config.is_object() {
            return ToolResult::error("config must be an MCP server object");
        }
        match apply_auto_config(
            home,
            AutoConfigIntent::Mcp(json!({"servers": {name: config}})),
        )
        .await
        {
            Ok(result) => ToolResult::success(result.message),
            Err(error) => ToolResult::error(format!("{error:#}")),
        }
    }

    async fn install_skill(&self, call: &ToolCall, approval: &dyn ApprovalHandler) -> ToolResult {
        let Some(home) = &self.config_home else {
            return ToolResult::error("Coomi configuration directory is not available");
        };
        if !approval
            .approve(
                call,
                "install_skill will download or copy files into the Coomi Skill directory",
            )
            .await
        {
            return ToolResult::error("Skill installation was not approved");
        }
        if let Some(catalog_id) = string_arg(&call.arguments, "catalog_id") {
            let catalog_id = catalog_id.to_owned();
            let home = home.clone();
            return match tokio::task::spawn_blocking(move || {
                CatalogInstaller::new(home).install_skill(&catalog_id)
            })
            .await
            {
                Ok(Ok(path)) => {
                    ToolResult::success(format!("Installed catalog Skill at {}", path.display()))
                }
                Ok(Err(error)) => ToolResult::error(format!("{error:#}")),
                Err(error) => ToolResult::error(format!("Skill install task failed: {error}")),
            };
        }
        let Some(source) = string_arg(&call.arguments, "source") else {
            return ToolResult::error("missing string argument: source or catalog_id");
        };
        match apply_auto_config(home, AutoConfigIntent::Skill(source.to_owned())).await {
            Ok(result) => ToolResult::success(result.message),
            Err(error) => ToolResult::error(format!("{error:#}")),
        }
    }

    async fn spawn_agent(&self, arguments: &Value) -> ToolResult {
        let Some(scheduler) = &self.agent_scheduler else {
            return ToolResult::error("agent scheduler is not configured");
        };
        let Some(task) = string_arg(arguments, "task") else {
            return ToolResult::error("missing string argument: task");
        };
        let fork_turns = string_arg(arguments, "fork_turns");
        match scheduler
            .spawn(task.to_owned(), &self.parent_history, fork_turns)
            .await
        {
            Ok(id) => ToolResult::success(format!("agent_id: {id}")),
            Err(error) => ToolResult::error(error),
        }
    }

    async fn wait_agent(&self, arguments: &Value) -> ToolResult {
        let Some(scheduler) = &self.agent_scheduler else {
            return ToolResult::error("agent scheduler is not configured");
        };
        let ids = arguments
            .get("ids")
            .and_then(Value::as_array)
            .map(|values| {
                values
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        let timeout_ms = u64_arg(arguments, "timeout_ms").unwrap_or(30_000);
        let snapshots = scheduler.wait(&ids, timeout_ms).await;
        ToolResult::success(
            serde_json::to_string_pretty(&snapshots_json(&snapshots))
                .unwrap_or_else(|_| "[]".into()),
        )
    }

    async fn close_agent(&self, arguments: &Value) -> ToolResult {
        let Some(scheduler) = &self.agent_scheduler else {
            return ToolResult::error("agent scheduler is not configured");
        };
        let Some(id) = string_arg(arguments, "id") else {
            return ToolResult::error("missing string argument: id");
        };
        match scheduler.close(id).await {
            Ok(snapshot) => ToolResult::success(
                serde_json::to_string_pretty(&snapshots_json(&[snapshot]))
                    .unwrap_or_else(|_| "[]".into()),
            ),
            Err(error) => ToolResult::error(error),
        }
    }

    fn list_dir(&self, arguments: &Value) -> ToolResult {
        let relative = string_arg(arguments, "path").unwrap_or(".");
        let path = match self.checked_path(relative, false) {
            Ok(path) => path,
            Err(error) => return ToolResult::error(error),
        };
        let depth = usize_arg(arguments, "depth").unwrap_or(1).clamp(1, 8);
        let max_entries = usize_arg(arguments, "max_entries")
            .unwrap_or(500)
            .clamp(1, 2_000);
        let mut entries = WalkBuilder::new(&path)
            .max_depth(Some(depth))
            .hidden(false)
            .build()
            .flatten()
            .skip(1)
            .take(max_entries)
            .map(|entry| {
                let display = entry.path().strip_prefix(&self.cwd).unwrap_or(entry.path());
                let suffix = if entry.file_type().is_some_and(|kind| kind.is_dir()) {
                    "/"
                } else {
                    ""
                };
                format!("{}{suffix}", display.display())
            })
            .collect::<Vec<_>>();
        entries.sort();
        if entries.is_empty() {
            ToolResult::success("directory is empty")
        } else {
            ToolResult::success(self.truncate(entries.join("\n")))
        }
    }

    async fn local_shell(&self, call: &ToolCall, approval: &dyn ApprovalHandler) -> ToolResult {
        let action = string_arg(&call.arguments, "action").unwrap_or("exec");
        if action == "exec" {
            let Some(command) = string_arg(&call.arguments, "command") else {
                return ToolResult::error("missing string argument: command");
            };
            match self.policy.assess_shell(command) {
                Decision::Allow => {}
                Decision::Deny(reason) => return ToolResult::error(reason),
                Decision::Ask(reason) => {
                    if !approval.approve(call, &reason).await {
                        return ToolResult::error("shell command was not approved");
                    }
                }
            }
        }
        self.processes.execute(&self.cwd, &call.arguments).await
    }

    async fn apply_patch(&self, call: &ToolCall, approval: &dyn ApprovalHandler) -> ToolResult {
        let Some(patch_text) = string_arg(&call.arguments, "patch") else {
            return ToolResult::error("missing string argument: patch");
        };
        if self.policy.mode() == coomi_security::AccessMode::FullAccess
            && !approval
                .approve(call, "apply_patch will modify files")
                .await
        {
            return ToolResult::error("patch was not approved");
        }
        match patch::apply_patch(&self.policy, patch_text) {
            Ok(output) => ToolResult::success(output),
            Err(error) => ToolResult::error(error),
        }
    }

    async fn web_search(&self, arguments: &Value) -> ToolResult {
        let Some(query) = string_arg(arguments, "query") else {
            return ToolResult::error("missing string argument: query");
        };
        let limit = usize_arg(arguments, "limit").unwrap_or(5).clamp(1, 10);
        let response = match reqwest::Client::new()
            .get("https://html.duckduckgo.com/html/")
            .query(&[("q", query)])
            .header("user-agent", "Coomi/2.0")
            .send()
            .await
        {
            Ok(response) => response,
            Err(error) => return ToolResult::error(format!("web search failed: {error}")),
        };
        let html = match response.error_for_status() {
            Ok(response) => match response.text().await {
                Ok(text) => text,
                Err(error) => {
                    return ToolResult::error(format!("failed to read search response: {error}"));
                }
            },
            Err(error) => return ToolResult::error(format!("web search failed: {error}")),
        };
        let result_re = Regex::new(
            r#"(?s)<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>"#,
        )
        .expect("valid search result regex");
        let tag_re = Regex::new(r"<[^>]+>").expect("valid HTML tag regex");
        let mut results = Vec::new();
        for captures in result_re.captures_iter(&html).take(limit) {
            let url = captures.get(1).map_or("", |value| value.as_str());
            let title = captures.get(2).map_or("", |value| value.as_str());
            let title = decode_html(&tag_re.replace_all(title, ""));
            results.push(format!("- {title}\n  {url}"));
        }
        if results.is_empty() {
            ToolResult::error("web search returned no parseable results")
        } else {
            ToolResult::success(results.join("\n"))
        }
    }

    async fn view_image(&self, arguments: &Value) -> ToolResult {
        let Some(path) = string_arg(arguments, "path") else {
            return ToolResult::error("missing string argument: path");
        };
        let path = match self.checked_path(path, false) {
            Ok(path) => path,
            Err(error) => return ToolResult::error(error),
        };
        let bytes = match tokio::fs::read(&path).await {
            Ok(bytes) => bytes,
            Err(error) => return ToolResult::error(format!("failed to read image: {error}")),
        };
        let media_type = match path
            .extension()
            .and_then(|value| value.to_str())
            .map(str::to_ascii_lowercase)
            .as_deref()
        {
            Some("png") => "image/png",
            Some("jpg" | "jpeg") => "image/jpeg",
            Some("gif") => "image/gif",
            Some("webp") => "image/webp",
            _ => return ToolResult::error("supported image formats: png, jpg, gif, webp"),
        };
        if bytes.len() > 10 * 1024 * 1024 {
            return ToolResult::error("image exceeds the 10 MiB tool limit");
        }
        ToolResult::success(format!(
            "path: {}\nmedia_type: {media_type}\nbytes: {}",
            path.display(),
            bytes.len()
        ))
        .with_image(media_type, BASE64_STANDARD.encode(bytes))
    }

    async fn request_user_input(
        &self,
        arguments: &Value,
        approval: &dyn ApprovalHandler,
    ) -> ToolResult {
        let request =
            match serde_json::from_value::<coomi_engine::UserInputRequest>(arguments.clone()) {
                Ok(request) => request,
                Err(error) => {
                    return ToolResult::error(format!("invalid user input request: {error}"));
                }
            };
        if let Err(error) = validate_user_input_request(&request) {
            return ToolResult::error(error);
        }
        match approval.request_user_input(&request).await {
            Some(response) => ToolResult::success(
                serde_json::to_string(&response).unwrap_or_else(|_| "{}".into()),
            ),
            None => ToolResult::error("user input request was cancelled"),
        }
    }

    fn update_plan(&self, arguments: &Value) -> ToolResult {
        let plan = match serde_json::from_value::<PlanState>(arguments.clone()) {
            Ok(plan) => plan,
            Err(error) => return ToolResult::error(format!("invalid plan: {error}")),
        };
        if let Err(error) = plan.validate() {
            return ToolResult::error(error);
        }
        *self.plan.lock().expect("plan lock") = Some(plan.clone());
        ToolResult::success("plan updated").with_plan(plan)
    }

    fn create_loop(&self, arguments: &Value) -> ToolResult {
        #[derive(Deserialize)]
        struct Args {
            objective: String,
            #[serde(default)]
            token_budget: Option<u64>,
        }
        let args = match serde_json::from_value::<Args>(arguments.clone()) {
            Ok(args) => args,
            Err(error) => return ToolResult::error(format!("invalid loop: {error}")),
        };
        if args.objective.trim().is_empty() {
            return ToolResult::error("loop objective must not be empty");
        }
        let mut current = self.loop_state.lock().expect("loop lock");
        if current
            .as_ref()
            .is_some_and(|loop_state| loop_state.status == LoopStatus::Active)
        {
            return ToolResult::error("an active loop already exists");
        }
        let loop_state = LoopState {
            objective: args.objective,
            status: LoopStatus::Active,
            token_budget: args.token_budget,
            tokens_used: 0,
            time_used_seconds: 0,
            blocked_streak: 0,
            turns_completed: 0,
        };
        *current = Some(loop_state.clone());
        ToolResult::success("loop created").with_loop(loop_state)
    }

    fn get_loop(&self) -> ToolResult {
        let current = self.loop_state.lock().expect("loop lock");
        match current.as_ref() {
            Some(loop_state) => ToolResult::success(
                serde_json::to_string_pretty(loop_state).unwrap_or_else(|_| "{}".into()),
            )
            .with_loop(loop_state.clone()),
            None => ToolResult::success("no loop is active"),
        }
    }

    fn update_loop(&self, arguments: &Value) -> ToolResult {
        #[derive(Deserialize)]
        struct Args {
            status: LoopStatus,
            #[serde(default)]
            objective: Option<String>,
        }
        let args = match serde_json::from_value::<Args>(arguments.clone()) {
            Ok(args) => args,
            Err(error) => return ToolResult::error(format!("invalid loop update: {error}")),
        };
        let mut current = self.loop_state.lock().expect("loop lock");
        let Some(loop_state) = current.as_mut() else {
            return ToolResult::error("no loop exists");
        };
        if let Some(objective) = args.objective {
            if objective.trim().is_empty() {
                return ToolResult::error("loop objective must not be empty");
            }
            loop_state.objective = objective;
        }
        if args.status == LoopStatus::Blocked {
            loop_state.blocked_streak = loop_state.blocked_streak.saturating_add(1);
            if loop_state.blocked_streak < 3 {
                loop_state.status = LoopStatus::Active;
                let copy = loop_state.clone();
                return ToolResult::success(format!(
                    "blocking condition recorded ({}/3); loop remains active",
                    loop_state.blocked_streak
                ))
                .with_loop(copy);
            }
        } else {
            loop_state.blocked_streak = 0;
        }
        loop_state.status = args.status;
        let copy = loop_state.clone();
        ToolResult::success("loop updated").with_loop(copy)
    }

    fn list_skills(&self) -> ToolResult {
        let Some(directory) = &self.skills_directory else {
            return ToolResult::error("Skill directory is not configured");
        };
        let Ok(entries) = std::fs::read_dir(directory) else {
            return ToolResult::success("no installed skills");
        };
        let mut names = entries
            .flatten()
            .filter(|entry| {
                entry.path().join("SKILL.md").is_file()
                    && self.skill_is_enabled(&entry.file_name().to_string_lossy())
            })
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .collect::<Vec<_>>();
        names.sort();
        if names.is_empty() {
            ToolResult::success("no installed skills")
        } else {
            ToolResult::success(names.join("\n"))
        }
    }

    async fn read_skill(&self, arguments: &Value) -> ToolResult {
        let Some(name) = string_arg(arguments, "name") else {
            return ToolResult::error("missing string argument: name");
        };
        if name.is_empty()
            || PathBuf::from(name).components().count() != 1
            || name == "."
            || name == ".."
        {
            return ToolResult::error("Skill name must be one directory name");
        }
        let Some(directory) = &self.skills_directory else {
            return ToolResult::error("Skill directory is not configured");
        };
        if !self.skill_is_enabled(name) {
            return ToolResult::error(format!("Skill `{name}` is disabled"));
        }
        let root = match directory.canonicalize() {
            Ok(root) => root,
            Err(_) => return ToolResult::error("no installed skills"),
        };
        let path = directory.join(name).join("SKILL.md");
        let canonical = match path.canonicalize() {
            Ok(path) if path.starts_with(&root) => path,
            Ok(_) => return ToolResult::error("Skill path escapes the installed Skill directory"),
            Err(error) => {
                return ToolResult::error(format!("failed to open Skill `{name}`: {error}"));
            }
        };
        match tokio::fs::read_to_string(&canonical).await {
            Ok(content) => ToolResult::success(self.truncate(content)),
            Err(error) => ToolResult::error(format!("failed to read Skill `{name}`: {error}")),
        }
    }

    fn skill_is_enabled(&self, name: &str) -> bool {
        let Some(directory) = &self.skills_directory else {
            return false;
        };
        let Some(home) = directory.parent() else {
            return true;
        };
        let path = home.join("config").join("skills.json");
        let Ok(bytes) = std::fs::read(path) else {
            return true;
        };
        serde_json::from_slice::<Value>(&bytes)
            .ok()
            .and_then(|document| {
                document
                    .pointer(&format!("/skills/{name}/enabled"))
                    .and_then(Value::as_bool)
            })
            .unwrap_or(true)
    }

    fn memory_list(&self) -> ToolResult {
        let Some(memory) = &self.memory else {
            return ToolResult::error("Memory is not configured");
        };
        let entries = memory
            .list()
            .into_iter()
            .map(|entry| {
                format!(
                    "- {} [{:?}/{:?}]{}: {}",
                    entry.name,
                    entry.scope.unwrap_or(MemoryScope::Project),
                    entry.memory_type,
                    if entry.stale { " stale" } else { "" },
                    entry.description
                )
            })
            .collect::<Vec<_>>();
        ToolResult::success(if entries.is_empty() {
            "no memories".into()
        } else {
            entries.join("\n")
        })
    }

    fn memory_read(&self, arguments: &Value) -> ToolResult {
        let Some(memory) = &self.memory else {
            return ToolResult::error("Memory is not configured");
        };
        let Some(name) = string_arg(arguments, "name") else {
            return ToolResult::error("missing string argument: name");
        };
        match memory.get(name) {
            Some(entry) => ToolResult::success(format!(
                "# {}\n\n{}\n\n{}",
                entry.name, entry.description, entry.content
            )),
            None => ToolResult::error(format!("memory `{name}` was not found")),
        }
    }

    fn memory_search(&self, arguments: &Value) -> ToolResult {
        let Some(memory) = &self.memory else {
            return ToolResult::error("Memory is not configured");
        };
        let Some(query) = string_arg(arguments, "query") else {
            return ToolResult::error("missing string argument: query");
        };
        let entries = memory.search(query, usize_arg(arguments, "limit").unwrap_or(5));
        ToolResult::success(
            entries
                .into_iter()
                .map(|entry| {
                    format!(
                        "## {}\n{}\n\n{}",
                        entry.name, entry.description, entry.content
                    )
                })
                .collect::<Vec<_>>()
                .join("\n\n"),
        )
    }

    async fn memory_write(&self, call: &ToolCall, approval: &dyn ApprovalHandler) -> ToolResult {
        #[derive(Deserialize)]
        struct Args {
            name: String,
            description: String,
            #[serde(rename = "type")]
            memory_type: MemoryType,
            content: String,
            #[serde(default = "default_memory_scope")]
            scope: MemoryScope,
        }
        let Some(memory) = &self.memory else {
            return ToolResult::error("Memory is not configured");
        };
        let args = match serde_json::from_value::<Args>(call.arguments.clone()) {
            Ok(args) => args,
            Err(error) => return ToolResult::error(format!("invalid memory: {error}")),
        };
        if args.scope != MemoryScope::Local
            && !approval
                .approve(call, "memory_write will update persistent user data")
                .await
        {
            return ToolResult::error("memory write was not approved");
        }
        match memory.save(
            args.scope,
            &args.name,
            &args.description,
            args.memory_type,
            &args.content,
        ) {
            Ok(path) => ToolResult::success(format!("saved {}", path.display())),
            Err(error) => ToolResult::error(error.to_string()),
        }
    }

    async fn memory_delete(&self, call: &ToolCall, approval: &dyn ApprovalHandler) -> ToolResult {
        let Some(memory) = &self.memory else {
            return ToolResult::error("Memory is not configured");
        };
        let Some(name) = string_arg(&call.arguments, "name") else {
            return ToolResult::error("missing string argument: name");
        };
        if !approval
            .approve(call, "memory_delete will remove persistent user data")
            .await
        {
            return ToolResult::error("memory deletion was not approved");
        }
        match memory.delete(name) {
            Ok(true) => ToolResult::success(format!("deleted memory `{name}`")),
            Ok(false) => ToolResult::error(format!("memory `{name}` was not found")),
            Err(error) => ToolResult::error(error.to_string()),
        }
    }

    async fn read_file(&self, arguments: &Value) -> ToolResult {
        let Some(path) = string_arg(arguments, "path") else {
            return ToolResult::error("missing string argument: path");
        };
        let path = match self.checked_path(path, false) {
            Ok(path) => path,
            Err(error) => return ToolResult::error(error),
        };
        let content = match tokio::fs::read_to_string(&path).await {
            Ok(content) => content,
            Err(error) => {
                return ToolResult::error(format!("failed to read {}: {error}", path.display()));
            }
        };
        let offset = usize_arg(arguments, "offset").unwrap_or(1).max(1);
        let limit = usize_arg(arguments, "limit").unwrap_or(500).clamp(1, 2_000);
        let lines = content
            .lines()
            .enumerate()
            .skip(offset - 1)
            .take(limit)
            .map(|(index, line)| format!("{:>6}  {line}", index + 1))
            .collect::<Vec<_>>()
            .join("\n");
        ToolResult::success(self.truncate(lines))
    }

    async fn write_file(&self, arguments: &Value) -> ToolResult {
        let Some(path) = string_arg(arguments, "path") else {
            return ToolResult::error("missing string argument: path");
        };
        let Some(content) = string_arg(arguments, "content") else {
            return ToolResult::error("missing string argument: content");
        };
        let path = match self.checked_path(path, true) {
            Ok(path) => path,
            Err(error) => return ToolResult::error(error),
        };
        if let Some(parent) = path.parent()
            && let Err(error) = tokio::fs::create_dir_all(parent).await
        {
            return ToolResult::error(format!(
                "failed to create directory {}: {error}",
                parent.display()
            ));
        }
        match tokio::fs::write(&path, content).await {
            Ok(()) => ToolResult::success(format!(
                "wrote {} bytes to {}",
                content.len(),
                path.display()
            )),
            Err(error) => ToolResult::error(format!("failed to write {}: {error}", path.display())),
        }
    }

    async fn edit_file(&self, arguments: &Value) -> ToolResult {
        let Some(path) = string_arg(arguments, "path") else {
            return ToolResult::error("missing string argument: path");
        };
        let Some(old_string) = string_arg(arguments, "old_string") else {
            return ToolResult::error("missing string argument: old_string");
        };
        let Some(new_string) = string_arg(arguments, "new_string") else {
            return ToolResult::error("missing string argument: new_string");
        };
        if old_string.is_empty() {
            return ToolResult::error("old_string must not be empty");
        }
        let path = match self.checked_path(path, true) {
            Ok(path) => path,
            Err(error) => return ToolResult::error(error),
        };
        let content = match tokio::fs::read_to_string(&path).await {
            Ok(content) => content,
            Err(error) => {
                return ToolResult::error(format!("failed to read {}: {error}", path.display()));
            }
        };
        let matches = content.matches(old_string).count();
        if matches == 0 {
            return ToolResult::error("old_string was not found");
        }
        let replace_all = arguments
            .get("replace_all")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if matches > 1 && !replace_all {
            return ToolResult::error(format!(
                "old_string matched {matches} locations; set replace_all=true or provide more context"
            ));
        }
        let updated = if replace_all {
            content.replace(old_string, new_string)
        } else {
            content.replacen(old_string, new_string, 1)
        };
        match tokio::fs::write(&path, updated).await {
            Ok(()) => ToolResult::success(format!("edited {}", path.display())),
            Err(error) => ToolResult::error(format!("failed to edit {}: {error}", path.display())),
        }
    }

    async fn search(&self, arguments: &Value) -> ToolResult {
        let Some(query) = string_arg(arguments, "query") else {
            return ToolResult::error("missing string argument: query");
        };
        let regex = match Regex::new(query) {
            Ok(regex) => regex,
            Err(error) => return ToolResult::error(format!("invalid regex: {error}")),
        };
        let relative = string_arg(arguments, "path").unwrap_or(".");
        let root = match self.checked_path(relative, false) {
            Ok(path) => path,
            Err(error) => return ToolResult::error(error),
        };
        let max_results = usize_arg(arguments, "max_results")
            .unwrap_or(200)
            .clamp(1, 1_000);
        let mut output = Vec::new();

        for entry in WalkBuilder::new(&root).hidden(false).build().flatten() {
            if output.len() >= max_results {
                break;
            }
            if !entry.file_type().is_some_and(|kind| kind.is_file()) {
                continue;
            }
            let Ok(content) = std::fs::read_to_string(entry.path()) else {
                continue;
            };
            for (line_index, line) in content.lines().enumerate() {
                if regex.is_match(line) {
                    let display = entry.path().strip_prefix(&self.cwd).unwrap_or(entry.path());
                    output.push(format!("{}:{}:{line}", display.display(), line_index + 1));
                    if output.len() >= max_results {
                        break;
                    }
                }
            }
        }

        if output.is_empty() {
            ToolResult::success("no matches")
        } else {
            ToolResult::success(self.truncate(output.join("\n")))
        }
    }

    async fn shell(&self, call: &ToolCall, approval: &dyn ApprovalHandler) -> ToolResult {
        let Some(command) = string_arg(&call.arguments, "command") else {
            return ToolResult::error("missing string argument: command");
        };
        match self.policy.assess_shell(command) {
            Decision::Allow => {}
            Decision::Deny(reason) => return ToolResult::error(reason),
            Decision::Ask(reason) => {
                if !approval.approve(call, &reason).await {
                    return ToolResult::error("shell command was not approved");
                }
            }
        }

        let timeout_ms = u64_arg(&call.arguments, "timeout_ms")
            .unwrap_or(DEFAULT_TIMEOUT_MS)
            .clamp(1_000, 300_000);
        let mut process = platform_shell(command);
        process
            .current_dir(&self.cwd)
            .kill_on_drop(true)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let output = match tokio::time::timeout(Duration::from_millis(timeout_ms), process.output())
            .await
        {
            Ok(Ok(output)) => output,
            Ok(Err(error)) => return ToolResult::error(format!("failed to start shell: {error}")),
            Err(_) => return ToolResult::error(format!("shell timed out after {timeout_ms} ms")),
        };
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        let rendered = match (stdout.trim().is_empty(), stderr.trim().is_empty()) {
            (true, true) => format!("exit code: {}", output.status),
            (false, true) => stdout.into_owned(),
            (true, false) => stderr.into_owned(),
            (false, false) => format!("{stdout}\n[stderr]\n{stderr}"),
        };
        if output.status.success() {
            ToolResult::success(self.truncate(rendered))
        } else {
            ToolResult::error(self.truncate(format!("exit code: {}\n{rendered}", output.status)))
        }
    }

    fn checked_path(&self, value: &str, write: bool) -> Result<PathBuf, String> {
        let path = self
            .policy
            .resolve_path(value)
            .map_err(|error| error.to_string())?;
        let decision = if write {
            self.policy.assess_write(&path)
        } else {
            self.policy.assess_read(&path)
        };
        match decision {
            Decision::Allow => Ok(path),
            Decision::Ask(reason) | Decision::Deny(reason) => Err(reason),
        }
    }

    fn truncate(&self, mut output: String) -> String {
        if output.len() <= self.max_output {
            return output;
        }
        output.truncate(self.max_output);
        output.push_str("\n[output truncated]");
        output
    }
}

#[async_trait]
impl ToolRuntime for CoreTools {
    fn specs(&self) -> Vec<ToolSpec> {
        let mut specs = vec![
            ToolSpec {
                name: "read_file".into(),
                description: "Read a UTF-8 text file with stable line numbers.".into(),
                parameters: json!({
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "offset": {"type": "integer", "minimum": 1},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 2000}
                    },
                    "required": ["path"],
                    "additionalProperties": false
                }),
            },
            ToolSpec {
                name: "write_file".into(),
                description: "Create or replace a UTF-8 text file inside the workspace.".into(),
                parameters: json!({
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"}
                    },
                    "required": ["path", "content"],
                    "additionalProperties": false
                }),
            },
            ToolSpec {
                name: "edit_file".into(),
                description: "Replace an exact text fragment in a workspace file.".into(),
                parameters: json!({
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"}
                    },
                    "required": ["path", "old_string", "new_string"],
                    "additionalProperties": false
                }),
            },
            ToolSpec {
                name: "search".into(),
                description: "Search workspace text files with a regular expression.".into(),
                parameters: json!({
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 1000}
                    },
                    "required": ["query"],
                    "additionalProperties": false
                }),
            },
            ToolSpec {
                name: "shell".into(),
                description: "Run one shell command in the workspace under the active policy."
                    .into(),
                parameters: json!({
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout_ms": {"type": "integer", "minimum": 1000, "maximum": 300000}
                    },
                    "required": ["command"],
                    "additionalProperties": false
                }),
            },
            ToolSpec {
                name: "list_dir".into(),
                description: "List files and directories under a workspace path.".into(),
                parameters: json!({
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "depth": {"type": "integer", "minimum": 1, "maximum": 8},
                        "max_entries": {"type": "integer", "minimum": 1, "maximum": 2000}
                    },
                    "additionalProperties": false
                }),
            },
            ToolSpec {
                name: "grep_files".into(),
                description: "Search workspace text files with a regular expression.".into(),
                parameters: json!({
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 1000}
                    },
                    "required": ["query"],
                    "additionalProperties": false
                }),
            },
            ToolSpec {
                name: "local_shell".into(),
                description: "Run and manage a persistent local shell process. Use exec to start, write for stdin, wait for incremental output, and terminate to stop it.".into(),
                parameters: json!({
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["exec", "write", "wait", "terminate"]},
                        "command": {"type": "string"},
                        "session_id": {"type": "string"},
                        "input": {"type": "string"},
                        "close_stdin": {"type": "boolean"},
                        "yield_time_ms": {"type": "integer", "minimum": 0, "maximum": 60000}
                    },
                    "required": ["action"],
                    "additionalProperties": false
                }),
            },
            ToolSpec {
                name: "apply_patch".into(),
                description: "Atomically apply a Coomi patch containing add, update, move, and delete file operations.".into(),
                parameters: json!({
                    "type": "object",
                    "properties": {"patch": {"type": "string"}},
                    "required": ["patch"],
                    "additionalProperties": false
                }),
            },
            ToolSpec {
                name: "web_search".into(),
                description: "Search the web. Provider-native search is used when available; this client search is the fallback.".into(),
                parameters: json!({
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10}
                    },
                    "required": ["query"],
                    "additionalProperties": false
                }),
            },
            ToolSpec {
                name: "view_image".into(),
                description: "Load a local PNG, JPEG, GIF, or WebP image for visual inspection.".into(),
                parameters: json!({
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": false
                }),
            },
            ToolSpec {
                name: "request_user_input".into(),
                description: "Ask the user one to three short questions and wait for answers.".into(),
                parameters: json!({
                    "type": "object",
                    "properties": {
                        "questions": {
                            "type": "array", "minItems": 1, "maxItems": 3,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "header": {"type": "string"},
                                    "question": {"type": "string"},
                                    "options": {
                                        "type": "array", "minItems": 2, "maxItems": 3,
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "label": {"type": "string"},
                                                "description": {"type": "string"}
                                            },
                                            "required": ["label", "description"],
                                            "additionalProperties": false
                                        }
                                    }
                                },
                                "required": ["id", "header", "question", "options"],
                                "additionalProperties": false
                            }
                        },
                        "auto_resolution_ms": {"type": "integer", "minimum": 60000, "maximum": 240000}
                    },
                    "required": ["questions"],
                    "additionalProperties": false
                }),
            },
            ToolSpec {
                name: "update_plan".into(),
                description: "Create or update the current task plan. At most one step may be in progress.".into(),
                parameters: json!({
                    "type": "object",
                    "properties": {
                        "explanation": {"type": "string"},
                        "steps": {
                            "type": "array", "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "step": {"type": "string"},
                                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}
                                },
                                "required": ["step", "status"],
                                "additionalProperties": false
                            }
                        }
                    },
                    "required": ["steps"],
                    "additionalProperties": false
                }),
            },
            ToolSpec {
                name: "create_loop".into(),
                description: "Create a persistent autonomous Loop objective when no active Loop exists.".into(),
                parameters: json!({
                    "type": "object",
                    "properties": {
                        "objective": {"type": "string"},
                        "token_budget": {"type": "integer", "minimum": 1}
                    },
                    "required": ["objective"],
                    "additionalProperties": false
                }),
            },
            ToolSpec {
                name: "get_loop".into(),
                description: "Read the current Loop objective, status, budget, and usage.".into(),
                parameters: json!({"type": "object", "properties": {}, "additionalProperties": false}),
            },
            ToolSpec {
                name: "update_loop".into(),
                description: "Update the persistent Loop objective or status. Blocking requires the same condition across three turns.".into(),
                parameters: json!({
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["active", "paused", "blocked", "usage_limited", "budget_limited", "complete"]},
                        "objective": {"type": "string"}
                    },
                    "required": ["status"],
                    "additionalProperties": false
                }),
            },
        ];
        if self.skills_directory.is_some() {
            specs.extend([
                ToolSpec {
                    name: "list_skills".into(),
                    description: "List installed Skills that can be loaded on demand.".into(),
                    parameters: json!({
                        "type": "object",
                        "properties": {},
                        "additionalProperties": false
                    }),
                },
                ToolSpec {
                    name: "read_skill".into(),
                    description: "Load the full instructions for one installed Skill.".into(),
                    parameters: json!({
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                        "additionalProperties": false
                    }),
                },
            ]);
        }
        if self.config_home.is_some() {
            specs.extend([
                ToolSpec {
                    name: "configure_mcp".into(),
                    description: "Install a curated MCP server or create/repair one Coomi MCP server configuration. Use catalog_id for curated entries; otherwise provide name and config.".into(),
                    parameters: json!({
                        "type": "object",
                        "properties": {
                            "catalog_id": {"type": "string", "enum": ["filesystem", "git", "fetch", "memory", "playwright", "github"]},
                            "values": {
                                "type": "object",
                                "additionalProperties": {"type": "string"}
                            },
                            "name": {"type": "string"},
                            "config": {
                                "type": "object",
                                "description": "MCP server object containing transport and command/args or url",
                                "additionalProperties": true
                            }
                        },
                        "additionalProperties": false
                    }),
                },
                ToolSpec {
                    name: "install_skill".into(),
                    description: "Install a curated Coomi Skill by catalog_id, or install from a local directory or GitHub repository URL using source.".into(),
                    parameters: json!({
                        "type": "object",
                        "properties": {
                            "catalog_id": {"type": "string", "enum": ["frontend-design", "webapp-testing", "code-review", "security-review", "react-nextjs", "api-design", "git-workflow", "technical-writing"]},
                            "source": {"type": "string"}
                        },
                        "additionalProperties": false
                    }),
                },
            ]);
        }
        if self.agent_scheduler.is_some() {
            specs.extend([
                ToolSpec {
                    name: "spawn_agent".into(),
                    description: "Spawn a background Coomi sub-agent with an optional fork of parent history.".into(),
                    parameters: json!({
                        "type": "object",
                        "properties": {
                            "task": {"type": "string"},
                            "fork_turns": {"type": "string", "description": "none, all, or a positive integer"}
                        },
                        "required": ["task"],
                        "additionalProperties": false
                    }),
                },
                ToolSpec {
                    name: "wait_agent".into(),
                    description: "Wait for selected background agents and return their latest status and output.".into(),
                    parameters: json!({
                        "type": "object",
                        "properties": {
                            "ids": {"type": "array", "items": {"type": "string"}},
                            "timeout_ms": {"type": "integer", "minimum": 10, "maximum": 3600000}
                        },
                        "required": ["ids"],
                        "additionalProperties": false
                    }),
                },
                ToolSpec {
                    name: "close_agent".into(),
                    description: "Close a background agent, cancelling it if still running.".into(),
                    parameters: json!({
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                        "required": ["id"],
                        "additionalProperties": false
                    }),
                },
            ]);
        }
        if let Some(runtime) = &self.mcp_runtime {
            specs.extend(runtime.specs());
        }
        if self.memory.is_some() {
            specs.extend(memory_specs());
        }
        specs
    }

    async fn call(&self, call: &ToolCall, approval: &dyn ApprovalHandler) -> ToolResult {
        let mut effective_call = call.clone();
        let mut additional_context = String::new();
        if let Some(hooks) = &self.hooks {
            let outcome = match hooks
                .run(
                    HookEvent::PreToolUse,
                    Some(&call.name),
                    json!({"tool_name": call.name, "arguments": call.arguments, "cwd": self.cwd}),
                )
                .await
            {
                Ok(outcome) => outcome,
                Err(error) => {
                    return ToolResult::error(format!("PreToolUse hook failed: {error:#}"));
                }
            };
            if !outcome.allow {
                return ToolResult::error(if outcome.reason.is_empty() {
                    "PreToolUse hook denied the call".into()
                } else {
                    outcome.reason
                });
            }
            if let Some(arguments) = outcome.arguments {
                if !arguments.is_object() {
                    return ToolResult::error("PreToolUse hook arguments must be a JSON object");
                }
                effective_call.arguments = arguments;
            }
            additional_context = outcome.additional_context;
        }

        let mut result = self.dispatch(&effective_call, approval).await;
        if let Some(hooks) = &self.hooks {
            let outcome = match hooks
                .run(
                    HookEvent::PostToolUse,
                    Some(&effective_call.name),
                    json!({
                        "tool_name": effective_call.name,
                        "arguments": effective_call.arguments,
                        "result": {"success": result.success, "output": result.output}
                    }),
                )
                .await
            {
                Ok(outcome) => outcome,
                Err(error) => {
                    return ToolResult::error(format!("PostToolUse hook failed: {error:#}"));
                }
            };
            if let Some(value) = outcome.result {
                if let Some(output) = value.as_str() {
                    result.output = output.to_owned();
                } else if let Some(output) = value.get("output").and_then(Value::as_str) {
                    result.output = output.to_owned();
                    if let Some(success) = value.get("success").and_then(Value::as_bool) {
                        result.success = success;
                    }
                }
            }
            if !outcome.additional_context.trim().is_empty() {
                if !additional_context.is_empty() {
                    additional_context.push_str("\n\n");
                }
                additional_context.push_str(&outcome.additional_context);
            }
        }
        if !additional_context.trim().is_empty() {
            result.additional_context = Some(additional_context);
        }
        result
    }

    async fn lifecycle(&self, event: &str, payload: Value) -> Result<Option<String>, String> {
        let Some(hooks) = &self.hooks else {
            return Ok(None);
        };
        let event = match event {
            "session_start" => HookEvent::SessionStart,
            "turn_start" => HookEvent::TurnStart,
            "turn_end" => HookEvent::TurnEnd,
            other => return Err(format!("unknown hook lifecycle event: {other}")),
        };
        let outcome = hooks
            .run(event, None, payload)
            .await
            .map_err(|error| format!("{error:#}"))?;
        if !outcome.allow {
            return Err(if outcome.reason.is_empty() {
                format!("{event:?} hook denied execution")
            } else {
                outcome.reason
            });
        }
        Ok((!outcome.additional_context.trim().is_empty()).then_some(outcome.additional_context))
    }
}

const fn default_memory_scope() -> MemoryScope {
    MemoryScope::Project
}

fn memory_specs() -> Vec<ToolSpec> {
    vec![
        ToolSpec {
            name: "memory_list".into(),
            description: "List persistent memories using local, project, then global precedence.".into(),
            parameters: json!({"type":"object","properties":{},"additionalProperties":false}),
        },
        ToolSpec {
            name: "memory_read".into(),
            description: "Read one persistent memory by name.".into(),
            parameters: json!({"type":"object","properties":{"name":{"type":"string"}},"required":["name"],"additionalProperties":false}),
        },
        ToolSpec {
            name: "memory_search".into(),
            description: "Search persistent memories for relevant project or user context.".into(),
            parameters: json!({"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":20}},"required":["query"],"additionalProperties":false}),
        },
        ToolSpec {
            name: "memory_write".into(),
            description: "Create or update a durable memory. Prefer project scope unless the fact belongs in the repository or applies globally.".into(),
            parameters: json!({"type":"object","properties":{"name":{"type":"string"},"description":{"type":"string"},"type":{"type":"string","enum":["user","feedback","project","reference"]},"content":{"type":"string"},"scope":{"type":"string","enum":["local","project","global"]}},"required":["name","description","type","content"],"additionalProperties":false}),
        },
        ToolSpec {
            name: "memory_delete".into(),
            description: "Delete the highest-precedence persistent memory with this name.".into(),
            parameters: json!({"type":"object","properties":{"name":{"type":"string"}},"required":["name"],"additionalProperties":false}),
        },
    ]
}

fn string_arg<'a>(value: &'a Value, key: &str) -> Option<&'a str> {
    value.get(key).and_then(Value::as_str)
}

fn usize_arg(value: &Value, key: &str) -> Option<usize> {
    value
        .get(key)
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
}

fn u64_arg(value: &Value, key: &str) -> Option<u64> {
    value.get(key).and_then(Value::as_u64)
}

fn validate_user_input_request(request: &coomi_engine::UserInputRequest) -> Result<(), String> {
    if !(1..=3).contains(&request.questions.len()) {
        return Err("request_user_input requires one to three questions".into());
    }
    if request
        .auto_resolution_ms
        .is_some_and(|value| !(60_000..=240_000).contains(&value))
    {
        return Err("auto_resolution_ms must be between 60000 and 240000".into());
    }
    let mut ids = std::collections::HashSet::new();
    for question in &request.questions {
        if question.id.trim().is_empty()
            || question.header.trim().is_empty()
            || question.question.trim().is_empty()
        {
            return Err("question id, header, and question must not be empty".into());
        }
        if !ids.insert(question.id.as_str()) {
            return Err(format!("duplicate question id: {}", question.id));
        }
        if !(2..=3).contains(&question.options.len()) {
            return Err(format!(
                "question `{}` requires two or three options",
                question.id
            ));
        }
        if question
            .options
            .iter()
            .any(|option| option.label.trim().is_empty())
        {
            return Err(format!(
                "question `{}` has an empty option label",
                question.id
            ));
        }
    }
    Ok(())
}

fn decode_html(value: &str) -> String {
    value
        .replace("&amp;", "&")
        .replace("&quot;", "\"")
        .replace("&#x27;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
}

#[cfg(windows)]
fn platform_shell(command: &str) -> Command {
    let mut process = Command::new("powershell.exe");
    process.args(["-NoLogo", "-NoProfile", "-Command", command]);
    process
}

#[cfg(not(windows))]
fn platform_shell(command: &str) -> Command {
    let mut process = Command::new("/bin/bash");
    process.args(["-lc", command]);
    process
}

#[cfg(test)]
mod tests {
    use super::*;
    use coomi_security::AccessMode;

    struct Deny;

    struct Approve;

    #[async_trait]
    impl ApprovalHandler for Deny {
        async fn approve(&self, _call: &ToolCall, _reason: &str) -> bool {
            false
        }
    }

    #[async_trait]
    impl ApprovalHandler for Approve {
        async fn approve(&self, _call: &ToolCall, _reason: &str) -> bool {
            true
        }
    }

    #[tokio::test]
    async fn edits_files_inside_the_workspace() {
        let workspace = tempfile::tempdir().expect("temporary workspace");
        let file = workspace.path().join("sample.txt");
        std::fs::write(&file, "before").expect("write fixture");
        let policy = SecurityPolicy::new(workspace.path(), AccessMode::WorkspaceWrite)
            .expect("security policy");
        let tools = CoreTools::new(workspace.path().to_path_buf(), policy);
        let result = tools
            .call(
                &ToolCall {
                    id: "1".into(),
                    name: "edit_file".into(),
                    arguments: json!({
                        "path": "sample.txt",
                        "old_string": "before",
                        "new_string": "after"
                    }),
                },
                &Deny,
            )
            .await;
        assert!(result.success);
        assert_eq!(std::fs::read_to_string(file).expect("read result"), "after");
    }

    #[tokio::test]
    async fn rejects_unknown_tools() {
        let workspace = tempfile::tempdir().expect("temporary workspace");
        let policy =
            SecurityPolicy::new(workspace.path(), AccessMode::ReadOnly).expect("security policy");
        let tools = CoreTools::new(workspace.path().to_path_buf(), policy);
        let result = tools
            .call(
                &ToolCall {
                    id: "1".into(),
                    name: "missing".into(),
                    arguments: json!({}),
                },
                &Deny,
            )
            .await;
        assert!(!result.success);
    }

    #[tokio::test]
    async fn loads_installed_skills_on_demand() {
        let workspace = tempfile::tempdir().expect("temporary workspace");
        let skills = tempfile::tempdir().expect("temporary skills");
        let skill = skills.path().join("review");
        std::fs::create_dir(&skill).expect("create Skill directory");
        std::fs::write(skill.join("SKILL.md"), "Review carefully.").expect("write Skill");
        let policy =
            SecurityPolicy::new(workspace.path(), AccessMode::ReadOnly).expect("security policy");
        let tools = CoreTools::new(workspace.path().to_path_buf(), policy)
            .with_skills_directory(skills.path().to_path_buf());
        let result = tools
            .call(
                &ToolCall {
                    id: "1".into(),
                    name: "read_skill".into(),
                    arguments: json!({"name": "review"}),
                },
                &Deny,
            )
            .await;
        assert_eq!(result, ToolResult::success("Review carefully."));
    }

    #[tokio::test]
    async fn view_image_returns_structured_image_content() {
        let workspace = tempfile::tempdir().expect("temporary workspace");
        std::fs::write(workspace.path().join("pixel.png"), [1, 2, 3, 4]).expect("image fixture");
        let policy =
            SecurityPolicy::new(workspace.path(), AccessMode::ReadOnly).expect("security policy");
        let tools = CoreTools::new(workspace.path().to_path_buf(), policy);
        let result = tools
            .call(
                &ToolCall {
                    id: "1".into(),
                    name: "view_image".into(),
                    arguments: json!({"path": "pixel.png"}),
                },
                &Deny,
            )
            .await;
        assert!(result.success);
        assert_eq!(result.images.len(), 1);
        assert_eq!(result.images[0].media_type, "image/png");
        assert_eq!(result.images[0].data, "AQIDBA==");
        assert!(!result.output.contains("base64"));
    }

    #[tokio::test]
    async fn agent_can_configure_curated_mcp_in_coomi_home() {
        let workspace = tempfile::tempdir().expect("temporary workspace");
        let home = tempfile::tempdir().expect("temporary Coomi home");
        let policy =
            SecurityPolicy::new(workspace.path(), AccessMode::ReadOnly).expect("security policy");
        let tools = CoreTools::new(workspace.path().to_path_buf(), policy)
            .with_config_home(home.path().to_path_buf());
        let specs = tools.specs();
        assert!(specs.iter().any(|spec| spec.name == "configure_mcp"));
        assert!(specs.iter().any(|spec| spec.name == "install_skill"));

        let result = tools
            .call(
                &ToolCall {
                    id: "configure-fetch".into(),
                    name: "configure_mcp".into(),
                    arguments: json!({"catalog_id": "fetch"}),
                },
                &Approve,
            )
            .await;
        assert!(result.success, "{}", result.output);
        let config = std::fs::read_to_string(home.path().join("config/mcp_servers.json"))
            .expect("MCP configuration");
        assert!(config.contains("mcp==1.16.0"));
    }
}
