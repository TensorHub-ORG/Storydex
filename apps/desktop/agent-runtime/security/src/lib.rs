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
    allowed_write_roots: Option<Vec<PathBuf>>,
    blocked: Vec<PathBuf>,
    blocked_aliases: Vec<String>,
}

impl SecurityPolicy {
    pub fn new(workspace: impl AsRef<Path>, mode: AccessMode) -> Result<Self> {
        let workspace = workspace
            .as_ref()
            .canonicalize()
            .with_context(|| format!("invalid workspace {}", workspace.as_ref().display()))?;
        Ok(Self {
            workspace,
            mode,
            allowed_write_roots: None,
            blocked: Vec::new(),
            blocked_aliases: Vec::new(),
        })
    }

    pub fn with_allowed_write_roots(
        mut self,
        roots: impl IntoIterator<Item = PathBuf>,
    ) -> Result<Self> {
        let mut allowed = Vec::new();
        for root in roots {
            anyhow::ensure!(!root.as_os_str().is_empty(), "allowed write root is empty");
            let joined = if root.is_absolute() {
                root
            } else {
                self.workspace.join(root)
            };
            let resolved = resolve_symlinks(&normalize_path(&joined)?)?;
            anyhow::ensure!(
                resolved.starts_with(&self.workspace),
                "allowed write root {} is outside workspace {}",
                resolved.display(),
                self.workspace.display()
            );
            allowed.push(resolved);
        }
        allowed.sort();
        allowed.dedup();
        self.allowed_write_roots = Some(allowed);
        Ok(self)
    }

    pub fn with_blocked(mut self, blocked: impl IntoIterator<Item = PathBuf>) -> Self {
        for candidate in blocked {
            let Ok(normalized) = normalize_path(&candidate) else {
                continue;
            };
            let canonical = if normalized.exists() {
                normalized
                    .canonicalize()
                    .unwrap_or_else(|_| normalized.clone())
            } else {
                normalized.clone()
            };
            self.blocked.push(canonical.clone());
            for path in [&normalized, &canonical] {
                self.blocked_aliases.push(
                    path.to_string_lossy()
                        .replace('\\', "/")
                        .to_ascii_lowercase(),
                );
            }
            if let (Some(parent), Some(name)) = (normalized.parent(), normalized.file_name())
                && let Some(home_name) = parent.file_name()
            {
                let relative =
                    format!("{}/{}", home_name.to_string_lossy(), name.to_string_lossy())
                        .to_ascii_lowercase();
                self.blocked_aliases.extend([
                    relative.clone(),
                    format!("~/{relative}"),
                    format!("$home/{relative}"),
                    format!("${{home}}/{relative}"),
                ]);
            }
        }
        self.blocked.sort();
        self.blocked.dedup();
        self.blocked_aliases.sort();
        self.blocked_aliases.dedup();
        self
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
        let comparable = trimmed.replace('\\', "/").to_ascii_lowercase();
        if self
            .blocked_aliases
            .iter()
            .any(|alias| comparable.contains(alias))
        {
            return Decision::Deny("command references a private runtime directory".into());
        }
        if !self.blocked.is_empty()
            && (comparable.contains('~')
                || comparable.contains("$home")
                || comparable.contains("${home}"))
        {
            return Decision::Ask(
                "command uses a home-directory alias that may reach private runtime data".into(),
            );
        }
        if self.allowed_write_roots.is_some() {
            return Decision::Deny(
                "shell commands are blocked by the active scoped-write policy".into(),
            );
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
        let Ok(path) = self.resolve_path(path) else {
            return Decision::Deny("path could not be normalized".into());
        };
        if self.blocked.iter().any(|blocked| path.starts_with(blocked)) {
            return Decision::Deny("path belongs to a private runtime directory".into());
        }
        if self.mode != AccessMode::FullAccess && !path.starts_with(&self.workspace) {
            return Decision::Deny(format!(
                "path is outside workspace {}",
                self.workspace.display()
            ));
        }
        if write && self.mode == AccessMode::ReadOnly {
            return Decision::Deny("write blocked by read-only policy".into());
        }
        if write
            && let Some(roots) = &self.allowed_write_roots
            && !roots
                .iter()
                .any(|root| path == *root || path.starts_with(root))
        {
            return Decision::Deny(if roots.is_empty() {
                "write blocked because the scoped-write policy has no allowed roots".into()
            } else {
                format!(
                    "write is outside scoped roots: {}",
                    roots
                        .iter()
                        .map(|root| root.display().to_string())
                        .collect::<Vec<_>>()
                        .join(", ")
                )
            });
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

    #[test]
    fn blocked_paths_override_full_access_and_shell_aliases() {
        let workspace = tempfile::tempdir().expect("temporary workspace");
        let home = workspace.path().join(".coomi");
        let private = home.join("sessions");
        std::fs::create_dir_all(&private).expect("create private directory");
        let policy = SecurityPolicy::new(workspace.path(), AccessMode::FullAccess)
            .expect("security policy")
            .with_blocked([private.clone()]);
        assert!(matches!(policy.assess_read(&private), Decision::Deny(_)));
        assert!(matches!(
            policy.assess_shell("cat ~/.coomi/sessions/current.json"),
            Decision::Deny(_)
        ));
        assert!(matches!(
            policy.assess_shell("cat $HOME/.coomi/sessions/current.json"),
            Decision::Deny(_)
        ));
    }

    #[test]
    fn scoped_write_policy_fails_closed_for_empty_roots() {
        let workspace = tempfile::tempdir().expect("temporary workspace");
        let policy = SecurityPolicy::new(workspace.path(), AccessMode::WorkspaceWrite)
            .expect("security policy")
            .with_allowed_write_roots(Vec::new())
            .expect("scoped policy");
        let target = policy.resolve_path("chapter.md").expect("target path");
        assert!(matches!(policy.assess_write(&target), Decision::Deny(_)));
        assert!(matches!(
            policy.assess_shell("git status"),
            Decision::Deny(_)
        ));
    }

    #[test]
    fn scoped_write_policy_allows_children_and_denies_siblings() {
        let workspace = tempfile::tempdir().expect("temporary workspace");
        std::fs::create_dir_all(workspace.path().join("allowed")).expect("allowed directory");
        let policy = SecurityPolicy::new(workspace.path(), AccessMode::WorkspaceWrite)
            .expect("security policy")
            .with_allowed_write_roots([PathBuf::from("allowed")])
            .expect("scoped policy");
        let child = policy
            .resolve_path("allowed/child.md")
            .expect("allowed child");
        let sibling = policy.resolve_path("sibling.md").expect("sibling path");
        assert_eq!(policy.assess_write(&child), Decision::Allow);
        assert!(matches!(policy.assess_write(&sibling), Decision::Deny(_)));
    }

    #[cfg(unix)]
    #[test]
    fn scoped_write_policy_resolves_symlink_escape() {
        use std::os::unix::fs::symlink;

        let workspace = tempfile::tempdir().expect("temporary workspace");
        let outside = tempfile::tempdir().expect("outside directory");
        let allowed = workspace.path().join("allowed");
        std::fs::create_dir_all(&allowed).expect("allowed directory");
        symlink(outside.path(), allowed.join("escape")).expect("create symlink");
        let policy = SecurityPolicy::new(workspace.path(), AccessMode::WorkspaceWrite)
            .expect("security policy")
            .with_allowed_write_roots([PathBuf::from("allowed")])
            .expect("scoped policy");
        let escaped = policy
            .resolve_path("allowed/escape/file.md")
            .expect("resolved escaped path");
        assert!(matches!(policy.assess_write(&escaped), Decision::Deny(_)));
    }
}
