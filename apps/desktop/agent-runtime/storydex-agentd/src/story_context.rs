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
    pub(crate) file_reads: Vec<Value>,
}

impl StoryContext {
    fn record_read(&mut self, path: &str, source: &str, chars: Option<usize>) {
        if self.file_reads.iter().any(|value| {
            value["arguments"]["path"] == path && value["arguments"]["source"] == source
        }) {
            return;
        }
        self.file_reads.push(json!({
            "operation": "read_file", "origin": "runtime", "status": "success",
            "arguments": {"path": path, "source": source},
            "result": chars.map(|count| format!("已读取 {count} 字符")).unwrap_or_else(|| "已读取并核对记录来源".into()),
        }));
    }
    pub(crate) fn included_source_count(&self) -> usize {
        self.blocks
            .iter()
            .filter(|b| b["included"] == true)
            .flat_map(|b| b["sourcePaths"].as_array().into_iter().flatten())
            .filter_map(Value::as_str)
            .collect::<HashSet<_>>()
            .len()
    }
    pub(crate) fn has_source(&self, kind: &str) -> bool {
        self.blocks
            .iter()
            .any(|block| block["kind"] == kind && block["included"] == true)
    }

    pub(crate) fn trace(&self) -> Value {
        let sources = [
            "runtime_presets",
            "story_bible",
            "story_outline",
            "confirmed_relations",
            "recent_segments",
            "active_document",
            "active_characters",
            "worldbook",
            "story_scripts",
            "story_style",
            "story_continuity",
            "story_ledger",
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
            "budget": {"maxTotalChars": MAX_CONTEXT_CHARS, "totalChars": self.total_chars, "blockCount": self.blocks.len(), "includedSourceCount": self.included_source_count()},
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
            self.blocks
                .push(json!({"id": kind, "kind": kind, "sourcePaths": [path],
                "included": false, "truncated": false, "omitted": true, "chars": 0}));
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
    result.sort_by(|a, b| {
        coomi_services::compare_chapter_paths(
            &a.to_string_lossy().replace('\\', "/"),
            &b.to_string_lossy().replace('\\', "/"),
        )
    });
    Ok(result)
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

    // Canon and plans are persistent references, independent of a filename
    // appearing in the author's prompt or the previous chapter.
    let mut used = HashSet::new();
    let mut canon = Vec::new();
    for path in text_files(&root, &root, 0)? {
        let stem = path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or_default()
            .to_lowercase();
        if matches!(
            stem.as_str(),
            "章纲"
                | "大纲"
                | "卷纲"
                | "outline"
                | "summary"
                | "卷摘要"
                | "故事圣经"
                | "故事设定"
                | "写作规则"
                | "story-bible"
                | "文风"
                | "文风样例"
                | "style"
        ) {
            let kind = if matches!(stem.as_str(), "文风" | "文风样例" | "style") {
                "story_style"
            } else {
                "story_outline"
            };
            canon.push((path, kind));
        }
    }
    for (directory, kind) in [
        (".storydex/memory", "story_bible"),
        (".storydex/scripts", "story_scripts"),
        (".storydex/worldbook", "worldbook"),
    ] {
        canon.extend(
            text_files(&root, &root.join(directory), 2)?
                .into_iter()
                .filter(|p| {
                    kind != "story_bible"
                        || (is_prose(p) && !p.starts_with(root.join(".storydex/memory/backups")))
                })
                .map(|p| (p, kind)),
        );
    }
    let bible = root.join(".storydex/story-bible.md");
    if bible.is_file() {
        canon.insert(0, (resolve_bounded_path(&root, bible)?, "story_bible"));
    }
    for relative in [".storydex/style.md", ".storydex/style-examples.md"] {
        let path = root.join(relative);
        if path.is_file() {
            canon.insert(0, (resolve_bounded_path(&root, path)?, "story_style"));
        }
    }
    let mut remaining = 12_000usize;
    let count = canon.len();
    for (index, (path, kind)) in canon.into_iter().enumerate() {
        let text = source_text(&root, &path, payload, &mut context)?;
        let limit = (remaining / (count - index)).min(4_000);
        remaining = remaining.saturating_sub(text.chars().count().min(limit));
        let text = if kind == "story_style" {
            format!(
                "本书表达参考：仅采用适用的叙述、对白和节奏特征；样例中的人物、物件、事件不属于本书事实，不复制样例原句。\n{text}"
            )
        } else {
            text
        };
        add_file(&root, &path, &text, kind, limit, false, &mut context);
        used.insert(path);
    }

    // Reserve space for long-distance continuity before recent prose consumes it.
    let unsaved_paths = payload
        .editor_documents
        .iter()
        .filter(|d| d["dirty"] == true)
        .filter_map(|d| d["relativePath"].as_str().map(|p| p.replace('\\', "/")))
        .collect::<Vec<_>>();
    let view =
        crate::story_continuity::read_with_exclusions(&root, &payload.prompt, &unsaved_paths)?;
    for path in &view.paths {
        context.record_read(path, "disk", None);
    }
    context.notes.extend(view.notes);
    let path = view.paths.join(", ");
    context.add("story_continuity", &path, &view.text, 6_000, false);
    if !view.text.is_empty() {
        context.blocks.last_mut().expect("continuity block")["sourcePaths"] = json!(view.paths);
    }
    // Existing author/Agent-maintained ledgers remain references, not inferred facts.
    let mut ledger_budget = 4_000usize;
    for relative in [
        ".storydex/memory/current/foreshadow_ledger.json",
        ".storydex/memory/current/chapter_outline.json",
        ".storydex/memory/current/character_conflicts.json",
        ".storydex/memory/current/story_state.json",
    ] {
        let path = root.join(relative);
        if path.is_file() {
            let path = resolve_bounded_path(&root, path)?;
            let text = source_text(&root, &path, payload, &mut context)?;
            if let Ok(value) = serde_json::from_str::<Value>(&text) {
                let text = format!(
                    "项目已有台账，可能含未来计划和人物猜测；不要当作已发生的事实。核对线索本次进展与回收条件。\n{value}"
                );
                let limit = ledger_budget.min(1_500);
                ledger_budget = ledger_budget.saturating_sub(text.chars().count().min(limit));
                context.add("story_ledger", relative, &text, limit, false);
            } else {
                context
                    .notes
                    .push(format!("invalid_story_ledger:{relative}"));
            }
        }
    }

    // Read the latest three chapters, with directory and flat chapters sharing
    // the index's Chinese/Arabic chapter numbering and fragment ordering.
    let chapters = root.join("chapters");
    let mut recent = Vec::new();
    if chapters.is_dir() {
        let chapters = resolve_bounded_path(&root, &chapters)?;
        let mut numbered = Vec::new();
        for entry in fs::read_dir(&chapters)? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            if coomi_services::is_chapter_entry(&name, entry.path().is_dir()) {
                numbered.push((name, resolve_bounded_path(&root, entry.path())?));
            }
        }
        numbered.sort_by(|a, b| coomi_services::compare_chapter_paths(&b.0, &a.0));
        let mut groups = Vec::new();
        for (_, path) in numbered {
            let files = if path.is_dir() {
                text_files(&root, &path, 0)?
            } else {
                vec![path]
            };
            let files: Vec<_> = files
                .into_iter()
                .filter(|p| {
                    is_prose(p) && coomi_services::is_chapter_entry(&p.to_string_lossy(), false)
                })
                .collect();
            if !files.is_empty() {
                groups.push(files);
            }
            if groups.len() == 3 {
                break;
            }
        }
        recent = groups.into_iter().rev().flatten().collect();
    }
    let mut recent_remaining = 16_000usize.min(
        MAX_CONTEXT_CHARS
            .saturating_sub(context.total_chars)
            .saturating_sub(8_000),
    );
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
    for (directory, kind) in [(".storydex/characters", "active_characters")] {
        let mut candidates = Vec::new();
        let mut relevant_ids = HashSet::new();
        for path in text_files(&root, &root.join(directory), 1)? {
            let text = source_text(&root, &path, payload, &mut context)?;
            let data = serde_json::from_str::<Value>(&text).unwrap_or(Value::Null);
            if data["name"]
                .as_str()
                .is_some_and(|name| relevant_text.contains(name))
            {
                if let Some(id) = data["id"].as_str() {
                    relevant_ids.insert(id.to_owned());
                }
                if let Some(id) = path.file_stem().and_then(|s| s.to_str()) {
                    relevant_ids.insert(id.to_owned());
                }
            }
            candidates.push((path, text, data));
        }
        for (path, text, data) in candidates {
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
            if !is_readme(&path)
                && !relevant_text.contains(name)
                && !relevant_ids.contains(name)
                && !data["id"]
                    .as_str()
                    .is_some_and(|id| relevant_ids.contains(id))
                && !data["name"]
                    .as_str()
                    .is_some_and(|name| relevant_text.contains(name))
            {
                continue;
            }
            if !used.insert(path.clone()) {
                continue;
            }
            add_file(&root, &path, &text, kind, 4_000, false, &mut context);
        }
    }
    add_confirmed_relations(&root, payload, &mut context)?;
    Ok(context)
}

fn add_confirmed_relations(
    root: &Path,
    payload: &ChatStreamRequest,
    context: &mut StoryContext,
) -> Result<()> {
    let graph_path = root.join(".storydex/wiki/knowledge_graph.json");
    if !graph_path.is_file() && !root.join(".storydex/knowledge/relations.json").is_file() {
        return Ok(());
    }
    // Refresh the existing projection so edited/deleted candidates cannot keep
    // borrowing an old confirmation. This performs no model calls.
    let graph = match coomi_services::StorydexKnowledge::new(root)?.sync(false) {
        Ok(graph) => graph,
        Err(_) => {
            context
                .notes
                .push("knowledge_unavailable:知识图谱未能刷新，本轮未引用关系".into());
            return Ok(());
        }
    };
    let mut facts = String::new();
    for relation in graph["relations"].as_array().into_iter().flatten() {
        if relation["reviewStatus"] != "confirmed"
            || relation["candidateRemoved"] == true
            || relation["evidenceValid"] != true
        {
            continue;
        }
        let refs = relation["sourceRefs"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        if refs.is_empty() {
            continue;
        }
        let mut quotes = Vec::new();
        for reference in &refs {
            let path = reference["path"].as_str().unwrap_or_default();
            let quote = reference["quote"].as_str().unwrap_or_default();
            let source = root.join(path);
            if quote.is_empty() || !source.is_file() {
                break;
            }
            let source = resolve_bounded_path(root, source)?;
            if !source_text(root, &source, payload, context)?.contains(quote) {
                break;
            }
            quotes.push(format!("{path}：{quote}"));
        }
        if quotes.len() != refs.len() {
            continue;
        }
        facts.push_str(&format!(
            "{} — {} — {}\n{}\n",
            relation["subject"].as_str().unwrap_or_default(),
            relation["predicate"].as_str().unwrap_or_default(),
            relation["object"].as_str().unwrap_or_default(),
            quotes.join("\n")
        ));
    }
    if !facts.is_empty() {
        context.add(
            "confirmed_relations",
            ".storydex/wiki/knowledge_graph.json",
            &format!(
                "以下关系经作者确认；引文用于核对原文，称谓和推断仍应结合上下文理解。\n{facts}"
            ),
            4_000,
            false,
        );
    }
    Ok(())
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
        context.record_read(&relative, "editor_draft", Some(draft.chars().count()));
        return Ok(draft.to_owned());
    }
    if fs::metadata(path)?.len() > MAX_SOURCE_BYTES {
        context.notes.push(format!("source_too_large:{relative}"));
        return Ok(String::new());
    }
    let text =
        fs::read_to_string(path).with_context(|| format!("无法读取章节上下文 {relative}"))?;
    context.record_read(&relative, "disk", Some(text.chars().count()));
    Ok(text)
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
    fn includes_style_legacy_threads_and_id_named_character_state() {
        let directory = tempdir().expect("temp");
        let root = directory.path();
        write(root, "文风.md", "本书叙述允许心理直写。");
        write(
            root,
            ".storydex/style-examples.md",
            "样例人物示范，仅参考表达。",
        );
        write(
            root,
            ".storydex/memory/current/foreshadow_ledger.json",
            r#"{"threads":{"铜牌":{"status":"active","resolution_condition":"林岚向旧约当事人坦白"}}}"#,
        );
        write(
            root,
            ".storydex/characters/cards/char_17.json",
            r#"{"id":"char_17","name":"林岚","personality":"谨慎"}"#,
        );
        write(
            root,
            ".storydex/characters/states/char_17.json",
            r#"{"id":"char_17","emotional_state":"仍因昨日争执不愿求助"}"#,
        );
        let payload =
            serde_json::from_value(json!({"prompt":"林岚重新谈起铜牌"})).expect("payload");
        let context = prepare(root, &payload).expect("context");
        assert!(context.has_source("story_style"));
        assert!(context.has_source("story_ledger"));
        for text in ["仅参考表达", "不属于本书事实", "旧约当事人", "昨日争执"]
        {
            assert!(context.references.contains(text), "{text}");
        }
    }

    #[test]
    fn recalls_distant_evidenced_threads_but_excludes_changed_editor_sources() {
        let directory = tempdir().expect("temp");
        let root = directory.path();
        let source = "chapters/第1章/001.md";
        let quote = "小雨把铜牌藏进信封，答应交给旧友。";
        write(root, source, quote);
        for chapter in 2..=8 {
            write(
                root,
                &format!("chapters/第{chapter}章/001.md"),
                "最近的故事没有提及旧日物件。",
            );
        }
        let (path, record, _) =
            crate::story_continuity::draft(root, &[source.into()]).expect("draft");
        let extracted = json!({"entries": [{"kind":"thread","key":"铜牌旧约","summary":"铜牌仍待交付，尚未兑现承诺。","status":"active","characters":["小雨"],"sourcePath":source,"quote":quote}]}).to_string();
        crate::story_continuity::save_completion(root, &path, record, &extracted).expect("save");
        let mut payload =
            serde_json::from_value::<ChatStreamRequest>(json!({"prompt":"继续第九章"}))
                .expect("payload");
        let context = prepare(root, &payload).expect("context");
        assert!(context.has_source("story_continuity"));
        assert!(
            context
                .file_reads
                .iter()
                .any(|read| read["arguments"]["path"] == path)
        );
        assert!(context.references.contains(quote));
        assert!(
            !context
                .blocks
                .iter()
                .any(|b| b["kind"] == "recent_segments" && b["sourcePaths"][0] == source)
        );
        payload.editor_documents =
            vec![json!({"relativePath":source,"dirty":true,"content":"作者改写了旧约。"})];
        let context = prepare(root, &payload).expect("draft context");
        assert!(!context.references.contains(quote));
        assert!(context.notes.iter().any(|n| n.contains("continuity_stale")));
    }

    #[test]
    fn story_context_uses_latest_chapter_natural_order_relevant_facts_and_editor_drafts() {
        let directory = tempdir().expect("tempdir");
        let root = directory.path();
        write(root, "chapters/第9章 旧/正文.md", "上上章的伏笔");
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
        let draft_reads = context
            .file_reads
            .iter()
            .filter(|read| read["arguments"]["path"] == "chapters/第10章 新/10.md")
            .collect::<Vec<_>>();
        assert_eq!(
            draft_reads.len(),
            1,
            "reusing the active draft should not duplicate a read"
        );
        assert_eq!(draft_reads[0]["arguments"]["source"], "editor_draft");
        assert!(context.file_reads.iter().any(|read| {
            read["arguments"]["path"] == "chapters/第10章 新/2.md"
                && read["arguments"]["source"] == "disk"
        }));
        assert!(
            context.file_reads.iter().all(|read| {
                read["origin"] == "runtime"
                    && root
                        .join(read["arguments"]["path"].as_str().expect("path"))
                        .is_file()
            }),
            "compiled presets must not be reported as file reads"
        );
        for expected in [
            "母亲已故",
            "父亲四个月前失踪",
            "林岚二十八岁",
            "守钟人的孙女",
            "雾港每月大潮",
            "沿航线寻父",
            "上上章的伏笔",
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
            !context
                .file_reads
                .iter()
                .any(|read| { read["arguments"]["path"] == ".storydex/worldbook/README.md" }),
            "skipped oversized files must not be reported as read"
        );
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

    #[test]
    fn omitted_sources_are_not_reported_as_loaded() {
        let mut context = StoryContext::default();
        context.add(
            "runtime_presets",
            "preset",
            &"文".repeat(MAX_CONTEXT_CHARS),
            MAX_CONTEXT_CHARS,
            false,
        );
        context.add("confirmed_relations", "graph", "已确认关系", 100, false);
        assert!(!context.has_source("confirmed_relations"));
        assert_eq!(context.trace()["promptBlocks"][1]["omitted"], true);
    }

    #[test]
    fn story_context_keeps_canon_without_name_matches_and_mixes_chapter_layouts() {
        let directory = tempdir().expect("fixture");
        let root = directory.path();
        for (path, text) in [
            ("chapters/第一章.md", "很久以前的正文"),
            ("chapters/第九章/001.md", "九章正文"),
            ("chapters/第十章.md", "十章正文"),
            ("chapters/第十一章/001.md", "十一章正文"),
            ("chapters/README.md", "模板说明不能当正文"),
            ("章纲.md", "按既有线索收束"),
            (".storydex/story-bible.md", "朱笔写、墨笔划"),
            (".storydex/worldbook/临水镇.md", "石阶三十七级"),
            (".storydex/memory/卷摘要.md", "新钥匙尚未使用"),
            (
                ".storydex/memory/chapter-progress.json",
                "{\"bookkeeping\":true}",
            ),
        ] {
            write(root, path, text);
        }
        let payload = serde_json::from_value(json!({"prompt": "接着写"})).expect("request");
        let context = prepare(root, &payload).expect("context");
        for text in [
            "九章正文",
            "十章正文",
            "十一章正文",
            "按既有线索收束",
            "朱笔写、墨笔划",
            "石阶三十七级",
            "新钥匙尚未使用",
        ] {
            assert!(context.references.contains(text), "{text}");
        }
        for text in ["很久以前的正文", "模板说明不能当正文", "bookkeeping"] {
            assert!(!context.references.contains(text));
        }
        assert!(context.references.find("九章正文") < context.references.find("十章正文"));
        assert!(context.references.find("十章正文") < context.references.find("十一章正文"));
    }

    #[test]
    fn story_context_includes_only_confirmed_relations_with_current_quotes() {
        let directory = tempdir().expect("fixture");
        let root = directory.path();
        write(root, "chapters/第1章.md", "甲和乙是旧识。");
        write(root, ".storydex/characters/甲.md", "甲的人物卡");
        write(root, ".storydex/characters/乙.md", "乙的人物卡");
        let knowledge = coomi_services::StorydexKnowledge::new(root).expect("knowledge");
        let initial = knowledge.sync(false).expect("initial projection");
        let nodes = initial["graph"]["nodes"].as_array().expect("nodes");
        let node = |label| {
            nodes
                .iter()
                .find(|node| node["label"] == label)
                .expect("node")["id"]
                .clone()
        };
        let mut relation = json!({"id": "old-friends", "subjectId": node("甲"), "objectId": node("乙"), "predicate": "旧识",
            "sourceRefs": [{"path": "chapters/第1章.md", "quote": "甲和乙是旧识。"}]});
        let mut pending = relation.clone();
        pending["id"] = json!("enemies");
        pending["predicate"] = json!("仇敌");
        write(
            root,
            ".storydex/knowledge/relations.json",
            &json!({"relations": [relation, pending]}).to_string(),
        );
        let graph = knowledge.sync(false).expect("candidates");
        let fingerprint = graph["relations"]
            .as_array()
            .expect("relations")
            .iter()
            .find(|r| r["id"] == "old-friends")
            .expect("candidate")["fingerprint"]
            .as_str()
            .expect("fingerprint");
        knowledge
            .review_relation("old-friends", fingerprint, "confirmed", &json!({}))
            .expect("confirm");
        let mut payload: ChatStreamRequest =
            serde_json::from_value(json!({"prompt": "继续"})).expect("request");
        let context = prepare(root, &payload).expect("context");
        assert!(context.has_source("confirmed_relations"));
        assert!(context.references.contains("甲 — 旧识 — 乙"));
        assert!(!context.references.contains("仇敌"));
        payload.editor_documents = vec![
            json!({"relativePath": "chapters/第1章.md", "dirty": true, "content": "甲和乙素未谋面。"}),
        ];
        assert!(
            !prepare(root, &payload)
                .expect("context")
                .has_source("confirmed_relations")
        );
        payload.editor_documents.clear();
        relation["predicate"] = json!("陌生人");
        write(
            root,
            ".storydex/knowledge/relations.json",
            &json!({"relations": [relation]}).to_string(),
        );
        assert!(
            !prepare(root, &payload)
                .expect("updated candidate")
                .has_source("confirmed_relations")
        );
    }
}
