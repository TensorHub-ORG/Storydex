mod auto_config;
mod catalog_state;
mod chapters;
mod config;
mod mcp;
mod memory;
mod project_write;
mod provider;
mod replay;
mod storydex_project;
pub use chapters::{chapter_number, compare_chapter_paths, is_chapter_entry};
pub use project_write::ProjectWriteLock;
mod update;

/// Shared by the workbench and the direct bridge entrypoint.
pub const STORYDEX_TASK_GUIDANCE: &str = r#"Storydex is a file-based writing workspace. When the author asks you to write, continue, revise, or organize project content, carry out the work in project files using the available tools. A prose-only chat reply does not fulfill a request to write a chapter. Discussion, critique, examples, and explicit requests to reply only in chat should remain replies without file changes. Interpret the full request and conversation, including negations. Discover the existing project layout and choose the appropriate file; ask only when a material choice cannot be resolved from context.

Before substantial work, briefly state the intended action and target in the author's language, then proceed. Use native tool calls, not text pretending to be tool calls. Read relevant prose and character facts before writing. Inspect each tool result. On a version or exact-text mismatch, re-read the affected file and adapt the edit. Respect denied or cancelled approval; do not work around it with another tool or repeat unchanged failing calls.

For a multi-paragraph rewrite, read the whole affected passage, compose its complete replacement, and apply it in one edit_file call using start_line/end_line and the read revision. Preserve prose outside the requested range. Do not add a new ending first and remove the old ending through a chain of small edits. For an isolated word or sentence change, use old_string/new_string.

Before ending, check that every requested change was actually applied. Report changed paths and unfinished work based on successful tool results. If no file was written, say so; do not present proposed prose or failed operations as saved work. Keep the final reply concise instead of repeating the saved chapter unless requested."#;

pub use auto_config::AutoConfigIntent;
pub use auto_config::AutoConfigResult;
pub use auto_config::apply_auto_config;
pub use auto_config::detect_auto_config;
pub use auto_config::update_installed_skill;
pub use catalog_state::ConfiguredMcp;
pub use catalog_state::InstalledSkill;
pub use catalog_state::list_configured_mcp;
pub use catalog_state::list_installed_skills;
pub use catalog_state::remove_configured_mcp;
pub use catalog_state::remove_installed_skill;
pub use catalog_state::set_mcp_enabled;
pub use catalog_state::set_skill_enabled;
pub use config::ModelChoice;
pub use config::ProviderConfig;
pub use config::ProviderDocument;
pub use config::ProviderKind;
pub use config::ProviderRegistry;
pub use config::ProviderSettings;
pub use config::ReasoningCapability;
pub use config::ReasoningCapabilitySource;
pub use config::ReasoningControlMode;
pub use config::ReasoningLevelCapability;
pub use config::ReasoningProfileSettings;
pub use config::ReasoningRequestPlan;
pub use config::ReasoningSupport;
pub use config::ReasoningWireField;
pub use config::RemoteCompactionMode;
pub use mcp::McpRuntime;
pub use mcp::McpServerStatus;
pub use memory::Memory;
pub use memory::MemoryManager;
pub use memory::MemoryScope;
pub use memory::MemoryType;
pub use provider::HttpModelProvider;
pub use provider::reasoning_capability;
pub use provider::reasoning_capability_best_effort;
pub use provider::reasoning_request_plan;
pub use provider::reasoning_request_plan_best_effort;
pub use replay::ReplayModelProvider;
pub use storydex_project::GitBranch;
pub use storydex_project::GitBranches;
pub use storydex_project::GitChangedFile;
pub use storydex_project::GitCommit;
pub use storydex_project::GitCommitResult;
pub use storydex_project::GitDiff;
pub use storydex_project::GitDiffFile;
pub use storydex_project::GitDiffHunk;
pub use storydex_project::GitDiffLine;
pub use storydex_project::GitDiffTotals;
pub use storydex_project::GitJumpResult;
pub use storydex_project::GitRestoreResult;
pub use storydex_project::GitSummary;
pub use storydex_project::GitTimeline;
pub use storydex_project::GitTimelineBranch;
pub use storydex_project::GitTimelineEdge;
pub use storydex_project::GitTimelineNode;
pub use storydex_project::GitWorldlineResult;
pub use storydex_project::ProjectionBundle;
pub use storydex_project::ProjectionBundleWriter;
pub use storydex_project::ProjectionSource;
pub use storydex_project::ProjectionWriteResult;
pub use storydex_project::StorydexGit;
pub use storydex_project::StorydexKnowledge;
pub use storydex_project::canonical_json_bytes;
pub use storydex_project::graph_checksum;
pub use storydex_project::resolve_bounded_path;
pub use storydex_project::source_set_checksum;
pub use update::UpdateCheckResult;
pub use update::check_for_update;
