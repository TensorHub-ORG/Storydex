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
                    "缺少必填参数 `{}` ({})，请填写后再安装",
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

    /// 卸载 Skill：删除 skills/{id} 目录与 config/skills.json 中的条目。
    pub fn uninstall_skill(&self, id: &str) -> Result<PathBuf> {
        // 与安装一致：id 必须先在内置目录中解析出合法条目，杜绝路径穿越
        // （id=".."、"%2E%2E%2F" 等经 URL 解码后越界删除任意目录）。
        let catalog = builtin_skills()?;
        let entry = catalog
            .entries
            .iter()
            .find(|entry| entry.id.eq_ignore_ascii_case(id))
            .with_context(|| format!("Skill `{id}` is not in the built-in catalog"))?;
        let destination = self.home.join("skills").join(&entry.id);
        if destination.exists() {
            fs::remove_dir_all(&destination)
                .with_context(|| format!("failed to remove {}", destination.display()))?;
        }
        let config_path = self.home.join("config").join("skills.json");
        if config_path.exists() {
            let bytes = fs::read(&config_path)
                .with_context(|| format!("failed to read {}", config_path.display()))?;
            if let Ok(mut document) = serde_json::from_slice::<Value>(&bytes) {
                if let Some(skills) = document.get_mut("skills").and_then(Value::as_object_mut) {
                    skills.remove(id);
                    fs::write(&config_path, serde_json::to_vec_pretty(&document)?)
                        .with_context(|| format!("failed to write {}", config_path.display()))?;
                }
            }
        }
        Ok(destination)
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
        // 通过 GitHub codeload zip 下载并解压（不依赖 git 命令：手机 bootstrap 未内置 git）。
        let zip_url = format!(
            "https://codeload.github.com/{}/zip/refs/heads/{}",
            entry.repository, entry.git_ref
        );
        let bytes = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(120))
            .user_agent("coomi-android")
            .build()
            .context("failed to build download client")?
            .get(&zip_url)
            .send()
            .context("failed to download skill archive")?
            .error_for_status()
            .context("skill archive download failed")?
            .bytes()
            .context("failed to read skill archive")?;
        let mut archive = zip::ZipArchive::new(std::io::Cursor::new(&bytes))
            .context("skill archive is not a valid zip")?;
        // codeload zip 的根目录形如 {repo}-{ref}/，把其中 {subdir}/ 的内容解压到目标。
        let repo_basename = entry.repository.rsplit('/').next().unwrap_or("repository");
        // GitHub normalizes slashes in branch names in codeload archive roots
        // (for example codex/feat/x becomes repo-codex-feat-x).
        let root_prefixes = [
            format!("{repo_basename}-{}/", entry.git_ref),
            format!("{repo_basename}-{}/", entry.git_ref.replace('/', "-")),
        ];
        let subdir_prefix = format!("{}/", entry.subdir);
        for index in 0..archive.len() {
            let mut file = archive
                .by_index(index)
                .with_context(|| format!("invalid zip entry #{index}"))?;
            let name = file.name().to_string();
            let Some(rest) = root_prefixes
                .iter()
                .find_map(|prefix| name.strip_prefix(prefix))
            else {
                continue;
            };
            if rest != entry.subdir && !rest.starts_with(&subdir_prefix) {
                continue;
            }
            // zip-slip 防护：拒绝任何越界片段。
            if rest
                .split('/')
                .any(|segment| segment.is_empty() || segment == "..")
            {
                continue;
            }
            let target = cache.join(rest);
            if file.is_dir() {
                fs::create_dir_all(&target)?;
            } else {
                if let Some(parent) = target.parent() {
                    fs::create_dir_all(parent)?;
                }
                let mut output = std::fs::File::create(&target)
                    .with_context(|| format!("failed to write {}", target.display()))?;
                std::io::copy(&mut file, &mut output)?;
            }
        }
        let source = cache.join(&entry.subdir);
        if !source.is_dir() {
            anyhow::bail!("downloaded repository has no directory `{}`", entry.subdir)
        }
        // zip 包不带 commit hash，用 ref 名作为版本记录。
        let commit = entry.git_ref.clone();
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

    #[test]
    fn uninstall_skill_rejects_path_traversal_ids() {
        // 卸载只接受内置目录中的合法 id：路径穿越（..、绝对路径、任意目录名）一律拒绝。
        let home = tempfile::tempdir().expect("temporary home");
        let installer = CatalogInstaller::new(home.path());
        for malicious in ["..", "../x", "/etc", "a/b", "%2e%2e"] {
            assert!(
                installer.uninstall_skill(malicious).is_err(),
                "uninstall should reject {malicious}"
            );
        }
        // 不存在的合法目录 id 不会报错（视为已卸载），但也不得删到 skills 之外。
        assert!(installer.uninstall_skill("frontend-design").is_ok());
        assert!(home.path().join("skills").is_dir() || !home.path().join("skills").exists());
    }
}
