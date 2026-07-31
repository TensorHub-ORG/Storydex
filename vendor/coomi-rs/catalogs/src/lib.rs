use anyhow::Context;
use anyhow::Result;
use serde::Deserialize;
use serde::Serialize;
use serde_json::Value;
use serde_json::json;
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;
use std::path::PathBuf;
use std::process::Command;

const MCP_CATALOG: &str = include_str!("../mcp.json");
const SKILL_CATALOG: &str = include_str!("../skills.json");

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Catalog<T> {
    pub version: u32,
    pub entries: Vec<T>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct RequiredParameter {
    pub key: String,
    pub label: String,
    #[serde(default)]
    pub secret: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct McpEntry {
    pub id: String,
    pub name: String,
    pub description: String,
    pub transport: String,
    pub command: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    #[serde(default)]
    pub required_parameters: Vec<RequiredParameter>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct SkillEntry {
    pub id: String,
    pub name: String,
    pub description: String,
    pub repository: String,
    #[serde(rename = "ref")]
    pub git_ref: String,
    pub subdir: String,
}

pub fn builtin_mcp() -> Result<Catalog<McpEntry>> {
    serde_json::from_str(MCP_CATALOG).context("built-in MCP catalog is invalid")
}

pub fn builtin_skills() -> Result<Catalog<SkillEntry>> {
    serde_json::from_str(SKILL_CATALOG).context("built-in Skill catalog is invalid")
}

pub struct CatalogInstaller {
    home: PathBuf,
}

impl CatalogInstaller {
    pub fn new(home: impl AsRef<Path>) -> Self {
        Self {
            home: home.as_ref().to_path_buf(),
        }
    }

    pub fn install_mcp(&self, id: &str, values: &BTreeMap<String, String>) -> Result<PathBuf> {
        let catalog = builtin_mcp()?;
        let entry = catalog
            .entries
            .iter()
            .find(|entry| entry.id.eq_ignore_ascii_case(id))
            .with_context(|| format!("MCP `{id}` is not in the built-in catalog"))?;
        for parameter in &entry.required_parameters {
            if values
                .get(&parameter.key)
                .is_none_or(|value| value.trim().is_empty())
            {
                anyhow::bail!(
                    "missing parameter `{}` ({})",
                    parameter.key,
                    parameter.label
                )
            }
        }

        let args = entry
            .args
            .iter()
            .map(|value| substitute(value, values))
            .collect::<Result<Vec<_>>>()?;
        let env = entry
            .env
            .iter()
            .map(|(key, value)| Ok((key.clone(), substitute(value, values)?)))
            .collect::<Result<BTreeMap<_, _>>>()?;
        let path = self.home.join("config").join("mcp_servers.json");
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut document = if path.exists() {
            serde_json::from_slice::<Value>(&fs::read(&path)?)
                .with_context(|| format!("invalid MCP config {}", path.display()))?
        } else {
            json!({"version": 1, "servers": {}})
        };
        let servers = document
            .get_mut("servers")
            .and_then(Value::as_object_mut)
            .context("MCP config must contain an object named `servers`")?;
        servers.insert(
            entry.id.clone(),
            json!({
                "transport": entry.transport,
                "command": entry.command,
                "args": args,
                "env": env,
                "enabled": true
            }),
        );
        fs::write(&path, serde_json::to_vec_pretty(&document)?)
            .with_context(|| format!("failed to write MCP config {}", path.display()))?;
        Ok(path)
    }

    pub fn install_skill(&self, id: &str) -> Result<PathBuf> {
        self.install_skill_inner(id, false)
    }

    pub fn update_skill(&self, id: &str) -> Result<PathBuf> {
        self.install_skill_inner(id, true)
    }

    fn install_skill_inner(&self, id: &str, replace: bool) -> Result<PathBuf> {
        let catalog = builtin_skills()?;
        let entry = catalog
            .entries
            .iter()
            .find(|entry| entry.id.eq_ignore_ascii_case(id))
            .with_context(|| format!("Skill `{id}` is not in the built-in catalog"))?;
        let destination = self.home.join("skills").join(&entry.id);
        if destination.exists() && !replace {
            anyhow::bail!("Skill `{}` is already installed", entry.id)
        }
        let cache = self
            .home
            .join("cache")
            .join(format!("skill-{}-partial", entry.id));
        if cache.exists() {
            fs::remove_dir_all(&cache)
                .with_context(|| format!("failed to clear cache {}", cache.display()))?;
        }
        if let Some(parent) = cache.parent() {
            fs::create_dir_all(parent)?;
        }
        let repository_url = format!("https://github.com/{}.git", entry.repository);
        run_git([
            "clone",
            "--depth",
            "1",
            "--filter=blob:none",
            "--sparse",
            "--branch",
            entry.git_ref.as_str(),
            repository_url.as_str(),
            cache.to_string_lossy().as_ref(),
        ])?;
        run_git_in(&cache, ["sparse-checkout", "set", entry.subdir.as_str()])?;
        let source = cache.join(&entry.subdir);
        if !source.is_dir() {
            anyhow::bail!("downloaded repository has no directory `{}`", entry.subdir)
        }
        let commit = run_git_in_capture(&cache, ["rev-parse", "HEAD"])?;
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent)?;
        }
        let backup = self
            .home
            .join("cache")
            .join(format!("skill-{}-backup", entry.id));
        if replace && destination.exists() {
            if backup.exists() {
                fs::remove_dir_all(&backup)?;
            }
            fs::rename(&destination, &backup)?;
        }
        if let Err(error) = copy_directory(&source, &destination) {
            let _ = fs::remove_dir_all(&destination);
            if backup.exists() {
                let _ = fs::rename(&backup, &destination);
            }
            return Err(error);
        }
        if backup.exists() {
            fs::remove_dir_all(&backup)?;
        }
        save_skill_metadata(&self.home, entry, &destination, &commit)?;
        fs::remove_dir_all(&cache)
            .with_context(|| format!("failed to clear cache {}", cache.display()))?;
        Ok(destination)
    }
}

fn save_skill_metadata(
    home: &Path,
    entry: &SkillEntry,
    destination: &Path,
    commit: &str,
) -> Result<()> {
    let path = home.join("config").join("skills.json");
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut document = if path.exists() {
        serde_json::from_slice::<Value>(&fs::read(&path)?)
            .with_context(|| format!("invalid Skill config {}", path.display()))?
    } else {
        json!({"version": 1, "skills": {}})
    };
    let skills = document
        .get_mut("skills")
        .and_then(Value::as_object_mut)
        .context("Skill config must contain an object named `skills`")?;
    skills.insert(
        entry.id.clone(),
        json!({
            "enabled": true,
            "path": destination,
            "source": entry.id,
            "source_type": "catalog",
            "repository": entry.repository,
            "git_ref": entry.git_ref,
            "subdir": entry.subdir,
            "commit": commit
        }),
    );
    fs::write(&path, serde_json::to_vec_pretty(&document)?)?;
    Ok(())
}

fn substitute(template: &str, values: &BTreeMap<String, String>) -> Result<String> {
    let mut output = template.to_string();
    while let Some(start) = output.find("{{") {
        let relative_end = output[start + 2..]
            .find("}}")
            .context("unclosed catalog placeholder")?;
        let end = start + 2 + relative_end;
        let key = &output[start + 2..end];
        let value = values
            .get(key)
            .with_context(|| format!("missing catalog parameter `{key}`"))?;
        output.replace_range(start..end + 2, value);
    }
    Ok(output)
}

fn run_git<'a>(args: impl IntoIterator<Item = &'a str>) -> Result<()> {
    let output = Command::new("git").args(args).output()?;
    if !output.status.success() {
        anyhow::bail!(
            "git clone failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        )
    }
    Ok(())
}

fn run_git_in<'a>(directory: &Path, args: impl IntoIterator<Item = &'a str>) -> Result<()> {
    let output = Command::new("git")
        .current_dir(directory)
        .args(args)
        .output()?;
    if !output.status.success() {
        anyhow::bail!(
            "git sparse checkout failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        )
    }
    Ok(())
}

fn run_git_in_capture<'a>(
    directory: &Path,
    args: impl IntoIterator<Item = &'a str>,
) -> Result<String> {
    let output = Command::new("git")
        .current_dir(directory)
        .args(args)
        .output()?;
    if !output.status.success() {
        anyhow::bail!(
            "git command failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        )
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

fn copy_directory(source: &Path, destination: &Path) -> Result<()> {
    fs::create_dir_all(destination)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let target = destination.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_directory(&entry.path(), &target)?;
        } else {
            fs::copy(entry.path(), target)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn built_in_catalogs_are_valid_and_non_empty() {
        let mcp = builtin_mcp().expect("MCP catalog");
        assert!(!mcp.entries.is_empty());
        assert!(!builtin_skills().expect("Skill catalog").entries.is_empty());
        let fetch = mcp
            .entries
            .iter()
            .find(|entry| entry.id == "fetch")
            .expect("fetch catalog entry");
        assert!(fetch.args.iter().any(|argument| argument == "mcp==1.16.0"));
    }

    #[test]
    fn installs_parameterized_mcp_config() {
        let home = tempfile::tempdir().expect("temporary home");
        let values = BTreeMap::from([(
            "allowed_path".to_string(),
            home.path().display().to_string(),
        )]);
        let path = CatalogInstaller::new(home.path())
            .install_mcp("filesystem", &values)
            .expect("install MCP");
        let document: Value = serde_json::from_slice(&fs::read(path).expect("read MCP config"))
            .expect("parse MCP config");
        assert_eq!(
            document.pointer("/servers/filesystem/enabled"),
            Some(&Value::Bool(true))
        );
    }
}
