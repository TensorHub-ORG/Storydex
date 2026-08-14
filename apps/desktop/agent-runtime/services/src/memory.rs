use anyhow::Context;
use anyhow::Result;
use chrono::DateTime;
use chrono::Duration;
use chrono::Utc;
use serde::Deserialize;
use serde::Serialize;
use std::collections::BTreeSet;
use std::fs;
use std::path::Path;
use std::path::PathBuf;

const STALE_AFTER_DAYS: i64 = 7;
const MAX_PROMPT_CHARS: usize = 32_000;

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MemoryType {
    #[default]
    User,
    Feedback,
    Project,
    Reference,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MemoryScope {
    Local,
    Project,
    Global,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Memory {
    pub name: String,
    pub description: String,
    #[serde(rename = "type", default)]
    pub memory_type: MemoryType,
    pub created: DateTime<Utc>,
    pub updated: DateTime<Utc>,
    #[serde(skip)]
    pub content: String,
    #[serde(skip)]
    pub stale: bool,
    #[serde(skip)]
    pub scope: Option<MemoryScope>,
}

#[derive(Clone)]
pub struct MemoryManager {
    local_dir: PathBuf,
    project_dir: PathBuf,
    global_dir: PathBuf,
}

impl MemoryManager {
    pub fn new(home: &Path, project_path: &Path) -> Self {
        let project_key = format!(
            "{:x}",
            md5::compute(project_path.to_string_lossy().as_bytes())
        );
        Self {
            local_dir: project_path.join(".coomi").join("memory"),
            project_dir: home
                .join("projects")
                .join(&project_key[..12.min(project_key.len())])
                .join("memory"),
            global_dir: home.join("memory"),
        }
    }

    pub fn list(&self) -> Vec<Memory> {
        let mut seen = BTreeSet::new();
        let mut memories = Vec::new();
        for (scope, directory) in self.directories() {
            let Ok(entries) = fs::read_dir(directory) else {
                continue;
            };
            let mut paths = entries
                .flatten()
                .map(|entry| entry.path())
                .filter(|path| {
                    path.extension().and_then(|value| value.to_str()) == Some("md")
                        && path.file_name().and_then(|value| value.to_str()) != Some("MEMORY.md")
                })
                .collect::<Vec<_>>();
            paths.sort();
            for path in paths {
                let Ok(mut memory) = read_memory(&path) else {
                    continue;
                };
                if !seen.insert(memory.name.clone()) {
                    continue;
                }
                memory.scope = Some(scope);
                memory.stale = matches!(
                    memory.memory_type,
                    MemoryType::Project | MemoryType::Reference
                ) && Utc::now().signed_duration_since(memory.updated)
                    > Duration::days(STALE_AFTER_DAYS);
                memories.push(memory);
            }
        }
        memories
    }

    pub fn get(&self, name: &str) -> Option<Memory> {
        self.list().into_iter().find(|memory| memory.name == name)
    }

    pub fn search(&self, query: &str, limit: usize) -> Vec<Memory> {
        let terms = query
            .split(|character: char| {
                !character.is_alphanumeric() && character != '_' && character != '-'
            })
            .filter(|term| term.chars().count() >= 2)
            .map(str::to_lowercase)
            .collect::<Vec<_>>();
        let mut scored = self
            .list()
            .into_iter()
            .filter_map(|memory| {
                let name = memory.name.to_lowercase();
                let description = memory.description.to_lowercase();
                let content = memory.content.to_lowercase();
                let score = terms.iter().fold(0usize, |score, term| {
                    score
                        + usize::from(name.contains(term)) * 5
                        + usize::from(description.contains(term)) * 3
                        + usize::from(content.contains(term))
                });
                (score > 0).then_some((score, memory))
            })
            .collect::<Vec<_>>();
        scored.sort_by_key(|item| std::cmp::Reverse(item.0));
        scored
            .into_iter()
            .take(limit.max(1))
            .map(|(_, memory)| memory)
            .collect()
    }

    pub fn save(
        &self,
        scope: MemoryScope,
        name: &str,
        description: &str,
        memory_type: MemoryType,
        content: &str,
    ) -> Result<PathBuf> {
        validate_name(name)?;
        let directory = self.directory(scope);
        fs::create_dir_all(directory)?;
        let path = directory.join(format!("{name}.md"));
        let existing = read_memory(&path).ok();
        let now = Utc::now();
        let memory = Memory {
            name: name.to_owned(),
            description: description.to_owned(),
            memory_type,
            created: existing.as_ref().map_or(now, |memory| memory.created),
            updated: now,
            content: content.to_owned(),
            stale: false,
            scope: Some(scope),
        };
        fs::write(&path, render_memory(&memory))
            .with_context(|| format!("failed to save memory {}", path.display()))?;
        self.refresh_index()?;
        Ok(path)
    }

    pub fn delete(&self, name: &str) -> Result<bool> {
        validate_name(name)?;
        for (_, directory) in self.directories() {
            let path = directory.join(format!("{name}.md"));
            if path.is_file() {
                fs::remove_file(&path)?;
                self.refresh_index()?;
                return Ok(true);
            }
        }
        Ok(false)
    }

    pub fn prompt_context(&self) -> String {
        let mut output = String::new();
        for memory in self.list().into_iter().filter(|memory| !memory.stale) {
            let entry = format!(
                "### {}\n_{}_\n\n{}\n\n",
                memory.name, memory.description, memory.content
            );
            if output.len().saturating_add(entry.len()) > MAX_PROMPT_CHARS {
                break;
            }
            output.push_str(&entry);
        }
        output
    }

    pub fn refresh_index(&self) -> Result<()> {
        let directory = if self.local_dir.is_dir() {
            &self.local_dir
        } else {
            &self.project_dir
        };
        fs::create_dir_all(directory)?;
        let mut lines = vec![
            "# Memory Index".to_owned(),
            "> Auto-generated. Local entries override project and global entries.".to_owned(),
            String::new(),
        ];
        for memory in self.list() {
            lines.push(format!(
                "- [{}](./{}.md) - {}{}",
                memory.name,
                memory.name,
                memory.description,
                if memory.stale { " [stale]" } else { "" }
            ));
        }
        fs::write(directory.join("MEMORY.md"), lines.join("\n"))?;
        Ok(())
    }

    fn directories(&self) -> [(MemoryScope, &Path); 3] {
        [
            (MemoryScope::Local, &self.local_dir),
            (MemoryScope::Project, &self.project_dir),
            (MemoryScope::Global, &self.global_dir),
        ]
    }

    fn directory(&self, scope: MemoryScope) -> &Path {
        match scope {
            MemoryScope::Local => &self.local_dir,
            MemoryScope::Project => &self.project_dir,
            MemoryScope::Global => &self.global_dir,
        }
    }
}

fn validate_name(name: &str) -> Result<()> {
    anyhow::ensure!(
        !name.is_empty()
            && name.len() <= 80
            && name.chars().all(
                |character| character.is_ascii_alphanumeric() || matches!(character, '-' | '_')
            ),
        "memory name must use 1-80 ASCII letters, numbers, hyphens, or underscores"
    );
    Ok(())
}

fn read_memory(path: &Path) -> Result<Memory> {
    let text = fs::read_to_string(path)?;
    let rest = text
        .strip_prefix("---\n")
        .context("memory has no frontmatter")?;
    let (frontmatter, content) = rest
        .split_once("\n---\n")
        .context("memory frontmatter is not closed")?;
    let mut memory: Memory = serde_yaml::from_str(frontmatter)?;
    memory.content = content.trim().to_owned();
    Ok(memory)
}

fn render_memory(memory: &Memory) -> String {
    let frontmatter = serde_yaml::to_string(memory).unwrap_or_default();
    format!("---\n{}---\n\n{}\n", frontmatter, memory.content)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_memory_overrides_project_and_global() {
        let home = tempfile::tempdir().expect("home");
        let project = tempfile::tempdir().expect("project");
        let manager = MemoryManager::new(home.path(), project.path());
        manager
            .save(
                MemoryScope::Global,
                "preference",
                "global",
                MemoryType::User,
                "dark",
            )
            .expect("global memory");
        manager
            .save(
                MemoryScope::Local,
                "preference",
                "local",
                MemoryType::User,
                "light",
            )
            .expect("local memory");
        let memories = manager.list();
        assert_eq!(memories.len(), 1);
        assert_eq!(memories[0].content, "light");
        assert_eq!(memories[0].scope, Some(MemoryScope::Local));
    }
}
