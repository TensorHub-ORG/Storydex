mod hooks;

use anyhow::Context;
use anyhow::Result;
use clap::ValueEnum;
use regex::Regex;
use std::path::Component;
use std::path::Path;
use std::path::PathBuf;

pub use hooks::HookEvent;
pub use hooks::HookOutcome;
pub use hooks::HookRunner;

#[derive(Clone, Copy, Debug, Eq, PartialEq, ValueEnum)]
pub enum AccessMode {
    ReadOnly,
    WorkspaceWrite,
    FullAccess,
}

impl AccessMode {
    pub fn label(self) -> &'static str {
        match self {
            Self::ReadOnly => "read-only",
            Self::WorkspaceWrite => "workspace-write",
            Self::FullAccess => "full-access",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Decision {
    Allow,
    Ask(String),
    Deny(String),
}

#[derive(Clone, Debug)]
pub struct SecurityPolicy {
    workspace: PathBuf,
    mode: AccessMode,
}

impl SecurityPolicy {
    pub fn new(workspace: impl AsRef<Path>, mode: AccessMode) -> Result<Self> {
        let workspace = workspace
            .as_ref()
            .canonicalize()
            .with_context(|| format!("invalid workspace {}", workspace.as_ref().display()))?;
        Ok(Self { workspace, mode })
    }

    pub fn workspace(&self) -> &Path {
        &self.workspace
    }

    pub fn mode(&self) -> AccessMode {
        self.mode
    }

    pub fn resolve_path(&self, value: impl AsRef<Path>) -> Result<PathBuf> {
        let value = value.as_ref();
        let joined = if value.is_absolute() {
            value.to_path_buf()
        } else {
            self.workspace.join(value)
        };
        resolve_symlinks(&normalize_path(&joined)?)
    }

    pub fn assess_read(&self, path: &Path) -> Decision {
        self.assess_path(path, false)
    }

    pub fn assess_write(&self, path: &Path) -> Decision {
        self.assess_path(path, true)
    }

    pub fn assess_shell(&self, command: &str) -> Decision {
        let trimmed = command.trim();
        if trimmed.is_empty() {
            return Decision::Deny("empty shell command".into());
        }

        if destructive_command().is_match(trimmed) {
            return match self.mode {
                AccessMode::FullAccess => {
                    Decision::Ask("command may delete or overwrite data".into())
                }
                _ => Decision::Deny("destructive command is blocked by the active policy".into()),
            };
        }

        match self.mode {
            AccessMode::FullAccess => Decision::Allow,
            AccessMode::WorkspaceWrite => {
                Decision::Ask("shell commands can change files or start processes".into())
            }
            AccessMode::ReadOnly if read_only_command().is_match(trimmed) => Decision::Allow,
            AccessMode::ReadOnly => Decision::Deny("command is not recognized as read-only".into()),
        }
    }

    fn assess_path(&self, path: &Path, write: bool) -> Decision {
        let Ok(path) = normalize_path(path) else {
            return Decision::Deny("path could not be normalized".into());
        };
        if self.mode == AccessMode::FullAccess {
            return Decision::Allow;
        }
        if !path.starts_with(&self.workspace) {
            return Decision::Deny(format!(
                "path is outside workspace {}",
                self.workspace.display()
            ));
        }
        if write && self.mode == AccessMode::ReadOnly {
            return Decision::Deny("write blocked by read-only policy".into());
        }
        Decision::Allow
    }
}

fn normalize_path(path: &Path) -> Result<PathBuf> {
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                if !normalized.pop() {
                    anyhow::bail!("path escapes its root")
                }
            }
            other => normalized.push(other.as_os_str()),
        }
    }
    Ok(normalized)
}

fn resolve_symlinks(path: &Path) -> Result<PathBuf> {
    if path.exists() {
        return path
            .canonicalize()
            .with_context(|| format!("failed to resolve path {}", path.display()));
    }

    let mut existing = path.to_path_buf();
    let mut missing = Vec::new();
    while !existing.exists() {
        let name = existing
            .file_name()
            .context("path has no existing ancestor")?
            .to_os_string();
        missing.push(name);
        anyhow::ensure!(existing.pop(), "path has no existing ancestor");
    }
    let mut resolved = existing
        .canonicalize()
        .with_context(|| format!("failed to resolve path {}", existing.display()))?;
    for name in missing.into_iter().rev() {
        resolved.push(name);
    }
    normalize_path(&resolved)
}

fn destructive_command() -> Regex {
    Regex::new(
        r"(?i)(^|[;&|]\s*)(rm\s+-[^\r\n]*r|remove-item\b[^\r\n]*-recurse|rmdir\s+/s|del\s+/[a-z]*[sq]|git\s+reset\s+--hard|git\s+clean\s+-[^\r\n]*f|format\s+[a-z]:)",
    )
    .expect("valid destructive command regex")
}

fn read_only_command() -> Regex {
    Regex::new(
        r"(?i)^(pwd|ls\b|dir\b|get-childitem\b|get-content\b|type\b|cat\b|rg\b|grep\b|findstr\b|git\s+(status|diff|log|show|branch\s+--show-current)\b)",
    )
    .expect("valid read-only command regex")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn workspace_policy_blocks_escape_and_allows_local_write() {
        let workspace = tempfile::tempdir().expect("temporary workspace");
        let policy = SecurityPolicy::new(workspace.path(), AccessMode::WorkspaceWrite)
            .expect("security policy");
        let local = policy.resolve_path("src/main.rs").expect("local path");
        assert_eq!(policy.assess_write(&local), Decision::Allow);

        let outside = workspace
            .path()
            .parent()
            .expect("parent")
            .join("outside.txt");
        assert!(matches!(policy.assess_write(&outside), Decision::Deny(_)));
    }

    #[test]
    fn read_only_shell_policy_fails_closed() {
        let workspace = tempfile::tempdir().expect("temporary workspace");
        let policy =
            SecurityPolicy::new(workspace.path(), AccessMode::ReadOnly).expect("security policy");
        assert_eq!(policy.assess_shell("git status"), Decision::Allow);
        assert!(matches!(
            policy.assess_shell("cargo fmt"),
            Decision::Deny(_)
        ));
        assert!(matches!(
            policy.assess_shell("Remove-Item -Recurse src"),
            Decision::Deny(_)
        ));
    }
}
