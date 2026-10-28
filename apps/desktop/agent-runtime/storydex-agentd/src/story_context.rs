//! Bounded file context for the direct chapter completion path. No model calls.
use crate::chat::ChatStreamRequest;
use anyhow::{Context, Result};
use coomi_services::resolve_bounded_path;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

const MAX_CONTEXT_CHARS: usize = 40_000;
const MAX_SOURCE_BYTES: u64 = 512 * 1024;

#[derive(Clone, Debug, Default)]
pub(crate) struct StoryContext {
    pub(crate) preset: String,
    pub(crate) references: String,
    blocks: Vec<Value>,
    notes: Vec<String>,
    total_chars: usize,
}

impl StoryContext {
    pub(crate) fn has_source(&self, kind: &str) -> bool {
        self.blocks.iter().any(|block| block["kind"] == kind)
    }

    pub(crate) fn trace(&self) -> Value {
        let sources = [
            "runtime_presets",
            "recent_segments",
            "active_document",
            "active_characters",
            "worldbook",
            "story_scripts",
        ]
        .into_iter()
        .filter_map(|kind| {
            let count = self
                .blocks
                .iter()
                .filter(|block| block["kind"] == kind)
                .count();
            (count > 0).then(|| json!({"kind": kind, "count": count}))
        })
        .collect::<Vec<_>>();
        json!({
            "budget": {"maxTotalChars": MAX_CONTEXT_CHARS, "totalChars": self.total_chars, "blockCount": self.blocks.len()},
            "promptBlocks": self.blocks,
            "contextTrace": {"sources": self.blocks},
            "sources": sources,
            "notes": self.notes,
        })
    }

    fn add(&mut self, kind: &str, path: &str, text: &str, limit: usize, keep_ending: bool) {
        let count = text.chars().count();
        if count == 0 {
            return;
        }
        let limit = limit.min(MAX_CONTEXT_CHARS.saturating_sub(self.total_chars));
        if limit == 0 {
            self.notes.push(format!("context_budget_exhausted:{path}"));
            return;
        }
        let truncated = count > limit;
        let included = if truncated && keep_ending {
            let marker = "\n[中间内容因上下文预算省略]\n";
            let usable = limit.saturating_sub(marker.chars().count());
            if usable == 0 {
                text.chars().take(limit).collect::<String>()
            } else {
                let head = usable / 3;
                format!(
                    "{}{marker}{}",
                    text.chars().take(head).collect::<String>(),
                    text.chars()
                        .skip(count - (usable - head))
                        .collect::<String>()
                )
            }
        } else {
            text.chars().take(limit).collect::<String>()
        };
        self.total_chars += included.chars().count();
        self.blocks.push(json!({
            "id": kind, "kind": kind, "sourcePaths": [path], "included": true,
            "truncated": truncated, "omitted": false, "chars": included.chars().count(),
            "sha256": format!("{:x}", Sha256::digest(included.as_bytes())),
        }));
        if kind == "runtime_presets" {
            self.preset = included;
        } else {
            self.references
                .push_str(&format!("\n来源：{path}\n{included}\n"));
        }
    }
}

fn text_files(root: &Path, directory: &Path, depth: usize) -> Result<Vec<PathBuf>> {
    if !directory.exists() {
        return Ok(Vec::new());
    }
    let directory = resolve_bounded_path(root, directory)?;
    let mut result = Vec::new();
    for entry in fs::read_dir(&directory)
        .with_context(|| format!("无法读取章节上下文目录 {}", directory.display()))?
    {
        let entry = entry?;
        let path = resolve_bounded_path(root, entry.path())?;
        if path.is_dir() && depth > 0 {
            result.extend(text_files(root, &path, depth - 1)?);
        } else if path.is_file()
            && path
                .extension()
                .and_then(|s| s.to_str())
                .is_some_and(|s| matches!(s.to_ascii_lowercase().as_str(), "md" | "txt" | "json"))
        {
            result.push(path);
        }
    }
    result.sort_by_key(|p| natural_key(p.file_name().and_then(|s| s.to_str()).unwrap_or_default()));
    Ok(result)
}

fn natural_key(name: &str) -> (u64, String) {
    let digits: String = name
        .chars()
        .skip_while(|c| !c.is_ascii_digit())
        .take_while(char::is_ascii_digit)
        .collect();
    (digits.parse().unwrap_or(0), name.to_owned())
}

fn is_readme(path: &Path) -> bool {
    path.file_stem()
        .and_then(|s| s.to_str())
        .is_some_and(|s| s.eq_ignore_ascii_case("README"))
}

fn is_prose(path: &Path) -> bool {
    !is_readme(path)
        && path
            .extension()
            .and_then(|s| s.to_str())
            .is_some_and(|s| matches!(s.to_ascii_lowercase().as_str(), "md" | "txt"))
}

pub(crate) fn prepare(workspace: &Path, payload: &ChatStreamRequest) -> Result<StoryContext> {
    let root = workspace.canonicalize()?;
    let mut context = StoryContext::default();
    if let Some(preset) = payload.compiled_preset.as_deref() {
        context.add(
            "runtime_presets",
            ".storydex/presets/active.json",
            preset,
            8_000,
            false,
        );
    }

    // Read the most recent chapter, not just the editor that happens to be open.
    let chapters = root.join("chapters");
    let mut recent = Vec::new();
    if chapters.is_dir() {
        let chapters = resolve_bounded_path(&root, &chapters)?;
        let mut numbered = Vec::new();
        for entry in fs::read_dir(&chapters)? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            if entry.path().is_dir() && name.starts_with('第') && name.contains('章') {
                numbered.push((
                    natural_key(&name),
                    resolve_bounded_path(&root, entry.path())?,
                ));
            }
        }
        numbered.sort_by(|a, b| b.0.cmp(&a.0));
        for (_, directory) in numbered {
            recent = text_files(&root, &directory, 0)?
                .into_iter()
                .filter(|p| is_prose(p))
                .collect();
            if !recent.is_empty() {
                break;
            }
        }
        if recent.is_empty() {
            recent = text_files(&root, &chapters, 0)?
                .into_iter()
                .filter(|p| is_prose(p))
                .collect();
            recent = recent.into_iter().rev().take(3).collect();
            recent.reverse();
        }
    }
    let mut used = HashSet::new();
    let mut recent_remaining = 20_000usize;
    let recent_count = recent.len();
    for (index, path) in recent.into_iter().enumerate() {
        let text = source_text(&root, &path, payload, &mut context)?;
        let limit = recent_remaining / (recent_count - index);
        recent_remaining = recent_remaining.saturating_sub(text.chars().count().min(limit));
        add_file(
            &root,
            &path,
            &text,
            "recent_segments",
            limit,
            true,
            &mut context,
        );
        used.insert(path);
    }
    if !payload.active_file.is_empty() {
        let path = root.join(payload.active_file.replace('\\', "/"));
        if path.is_file() {
            let path = resolve_bounded_path(&root, path)?;
            if used.insert(path.clone()) {
                let text = source_text(&root, &path, payload, &mut context)?;
                add_file(
                    &root,
                    &path,
                    &text,
                    "active_document",
                    4_000,
                    true,
                    &mut context,
                );
            }
        }
    }
    let relevant_text = format!("{}\n{}", payload.prompt, context.references);
    for (directory, kind) in [
        (".storydex/characters", "active_characters"),
        (".storydex/worldbook", "worldbook"),
        (".storydex/scripts", "story_scripts"),
    ] {
        for path in text_files(&root, &root.join(directory), 1)? {
            if context.total_chars >= MAX_CONTEXT_CHARS {
                context
                    .notes
                    .push(format!("context_budget_exhausted:{directory}"));
                break;
            }
            let name = path
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or_default();
            if !is_readme(&path) && !relevant_text.contains(name) {
                continue;
            }
            if !used.insert(path.clone()) {
                continue;
            }
            let text = source_text(&root, &path, payload, &mut context)?;
            add_file(&root, &path, &text, kind, 4_000, false, &mut context);
        }
    }
    Ok(context)
}

fn add_file(
    root: &Path,
    path: &Path,
    text: &str,
    kind: &str,
    limit: usize,
    tail: bool,
    context: &mut StoryContext,
) {
    let relative = path
        .strip_prefix(root)
        .expect("bounded context path")
        .to_string_lossy()
        .replace('\\', "/");
    context.add(kind, &relative, text, limit, tail);
}

fn source_text(
    root: &Path,
    path: &Path,
    payload: &ChatStreamRequest,
    context: &mut StoryContext,
) -> Result<String> {
    let relative = path
        .strip_prefix(root)?
        .to_string_lossy()
        .replace('\\', "/");
    if let Some(draft) = payload
        .editor_documents
        .iter()
        .find(|d| {
            d["dirty"] == true
                && d["relativePath"]
                    .as_str()
                    .is_some_and(|p| p.replace('\\', "/") == relative)
        })
        .and_then(|d| d["content"].as_str())
    {
        context.notes.push(format!("unsaved_draft:{relative}"));
        return Ok(draft.to_owned());
    }
    if fs::metadata(path)?.len() > MAX_SOURCE_BYTES {
        context.notes.push(format!("source_too_large:{relative}"));
        return Ok(String::new());
    }
    fs::read_to_string(path).with_context(|| format!("无法读取章节上下文 {relative}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn write(root: &Path, path: &str, text: &str) {
        let path = root.join(path);
        fs::create_dir_all(path.parent().expect("parent")).expect("directories");
        fs::write(path, text).expect("fixture");
    }

    #[test]
    fn story_context_uses_latest_chapter_natural_order_relevant_facts_and_editor_drafts() {
        let directory = tempdir().expect("tempdir");
        let root = directory.path();
        write(root, "chapters/第9章 旧/正文.md", "不应选旧章");
        write(
            root,
            "chapters/第10章 新/2.md",
            "林岚在雾港见到苏晚。母亲已故。",
        );
        write(root, "chapters/第10章 新/10.md", "磁盘旧稿");
        write(root, ".storydex/characters/林岚.md", "林岚二十八岁。");
        write(root, ".storydex/characters/苏晚.md", "苏晚是守钟人的孙女。");
        write(root, ".storydex/characters/无关角色.md", "不应载入无关人物");
        write(root, ".storydex/worldbook/雾港.md", "雾港每月大潮。");
        write(root, ".storydex/scripts/README.md", "沿航线寻父。");
        let mut payload: ChatStreamRequest = serde_json::from_value(json!({
            "prompt": "林岚准备出海", "activeFile": "chapters/第10章 新/10.md",
            "editorDocuments": [{"relativePath": "chapters/第10章 新/10.md", "dirty": true, "content": "父亲四个月前失踪，尚未保存的修订。"}]
        })).expect("payload");
        payload.compiled_preset = Some("简洁中文叙述".into());
        let context = prepare(root, &payload).expect("context");
        assert_eq!(context.preset, "简洁中文叙述");
        for expected in [
            "母亲已故",
            "父亲四个月前失踪",
            "林岚二十八岁",
            "守钟人的孙女",
            "雾港每月大潮",
            "沿航线寻父",
        ] {
            assert!(context.references.contains(expected), "missing {expected}");
        }
        assert!(!context.references.contains("磁盘旧稿"));
        assert!(!context.references.contains("不应"));
        assert!(context.references.find("母亲已故") < context.references.find("父亲四个月前失踪"));
        assert_eq!(context.references.matches("父亲四个月前失踪").count(), 1);
        assert!(
            context.trace()["notes"]
                .to_string()
                .contains("unsaved_draft")
        );
        assert_eq!(
            fs::read_to_string(root.join("chapters/第10章 新/10.md"))
                .expect("context test fixture"),
            "磁盘旧稿"
        );
    }

    #[test]
    fn story_context_budget_keeps_chapter_start_and_ending_and_reports_omissions() {
        let directory = tempdir().expect("tempdir");
        let root = directory.path();
        write(
            root,
            "chapters/第1章/正文.md",
            &format!("开头{}结尾", "中".repeat(30_000)),
        );
        write(
            root,
            ".storydex/worldbook/README.md",
            &"设".repeat(MAX_SOURCE_BYTES as usize),
        );
        let payload =
            serde_json::from_value(json!({"prompt": "继续"})).expect("context test fixture");
        let context = prepare(root, &payload).expect("context test fixture");
        assert!(context.references.contains("开头"));
        assert!(context.references.contains("结尾"));
        assert!(context.references.contains("省略"));
        assert!(context.total_chars <= MAX_CONTEXT_CHARS);
        assert_eq!(context.blocks[0]["truncated"], true);
        assert!(
            context
                .notes
                .iter()
                .any(|n| n.starts_with("source_too_large:"))
        );
    }

    #[test]
    fn story_context_rejects_active_files_outside_the_project() {
        let directory = tempdir().expect("context test fixture");
        let root = directory.path().join("project");
        fs::create_dir(&root).expect("context test fixture");
        fs::write(directory.path().join("outside.md"), "private").expect("context test fixture");
        let payload =
            serde_json::from_value(json!({"prompt": "继续", "activeFile": "../outside.md"}))
                .expect("context test fixture");
        assert!(prepare(&root, &payload).is_err());
    }
}
