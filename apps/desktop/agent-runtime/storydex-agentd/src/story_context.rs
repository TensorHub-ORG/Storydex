//! Bounded file context for the direct chapter completion path. No model calls.
use crate::chat::ChatStreamRequest;
use anyhow::{Context, Result};
use coomi_services::resolve_bounded_path;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

const MAX_CONTEXT_CHARS: usize = 40_000;
const MAX_SOURCE_BYTES: u64 = 512 * 1024;
const MAX_RELATION_DOCUMENT_BYTES: u64 = 8 * 1024 * 1024;

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
            "recent_structure",
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
        let prefix = if kind == "runtime_presets" {
            String::new()
        } else {
            format!("\n来源：{path}\n")
        };
        let overhead = prefix.chars().count() + usize::from(kind != "runtime_presets");
        let limit = limit.min(
            MAX_CONTEXT_CHARS
                .saturating_sub(self.total_chars)
                .saturating_sub(overhead),
        );
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
        self.total_chars += included.chars().count() + overhead;
        self.blocks.push(json!({
            "id": kind, "kind": kind, "sourcePaths": [path], "included": true,
            "truncated": truncated, "omitted": false, "chars": included.chars().count(),
            "overheadChars": overhead,
            "sha256": format!("{:x}", Sha256::digest(included.as_bytes())),
        }));
        if kind == "runtime_presets" {
            self.preset = included;
        } else {
            self.references.push_str(&format!("{prefix}{included}\n"));
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

struct RetrievalQuery {
    author: String,
    plan: String,
    recent: String,
    terms: Vec<(String, usize)>,
    chapter: Option<u32>,
}

impl RetrievalQuery {
    fn new(author: &str, plan: &str, recent: &[(PathBuf, String)], chapter: Option<u32>) -> Self {
        let plan = plan.chars().take(2_400).collect::<String>();
        let recent = recent
            .iter()
            .rev()
            .take(6)
            .map(|(_, text)| {
                let chars = text.chars().collect::<Vec<_>>();
                chars[chars.len().saturating_sub(1_000)..]
                    .iter()
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n");
        let mut terms = Vec::new();
        let mut seen = HashSet::new();
        for (text, weight) in [(author, 12), (plan.as_str(), 5), (recent.as_str(), 1)] {
            for term in retrieval_terms(text) {
                if seen.insert(term.clone()) {
                    terms.push((term, weight));
                }
            }
        }
        Self {
            author: author.into(),
            plan,
            recent,
            terms,
            chapter,
        }
    }

    fn text(&self) -> String {
        format!("{}\n{}\n{}", self.author, self.plan, self.recent)
    }

    fn score(&self, text: &str) -> usize {
        let text = text.to_lowercase();
        self.terms
            .iter()
            .filter(|(term, _)| text.contains(term))
            .take(80)
            .map(|(_, weight)| *weight)
            .sum()
    }

    fn named_score(&self, path: &Path) -> usize {
        let name = path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or_default();
        if name.chars().count() < 2 || name.eq_ignore_ascii_case("readme") {
            return 0;
        }
        if self.author.contains(name) {
            1_000
        } else if self.plan.contains(name) {
            500
        } else if self.recent.contains(name) {
            100
        } else {
            0
        }
    }
}

fn retrieval_terms(text: &str) -> Vec<String> {
    let mut terms = Vec::new();
    let mut seen = HashSet::new();
    for word in text.split(|c: char| !c.is_alphanumeric()) {
        let chars = word.chars().collect::<Vec<_>>();
        if word.is_ascii() {
            if chars.len() >= 3 {
                let term = word.to_ascii_lowercase();
                if seen.insert(term.clone()) {
                    terms.push(term);
                }
            }
        } else {
            for length in [3, 2] {
                for window in chars.windows(length) {
                    let term = window.iter().collect::<String>();
                    if !matches!(
                        term.as_str(),
                        "继续"
                            | "下一"
                            | "一章"
                            | "本章"
                            | "人物"
                            | "故事"
                            | "正文"
                            | "然后"
                            | "这个"
                            | "自己"
                            | "一个"
                            | "没有"
                            | "什么"
                            | "可以"
                            | "就是"
                            | "需要"
                            | "作者"
                    ) && seen.insert(term.clone())
                    {
                        terms.push(term);
                    }
                }
            }
        }
        if terms.len() >= 768 {
            break;
        }
    }
    terms.truncate(768);
    terms
}

fn requested_chapter(prompt: &str) -> Option<u32> {
    let chapter = r"(?:第[0-9零〇一二两三四五六七八九十百千万]+章|(?i:chapter)\s+[0-9]+)";
    let directed = regex::Regex::new(&format!(
        r"(?:续写|继续|生成|创作|写|(?i:write|continue))\s*({chapter})"
    ))
    .ok()?;
    if let Some(capture) = directed.captures_iter(prompt).last() {
        return coomi_services::chapter_number(capture.get(1)?.as_str());
    }
    // A chapter cited as a style/reference example is not a new target when
    // the author explicitly asks to move forward from the current manuscript.
    let forward = regex::Regex::new(
        r"(?:下一章|接着写|往下写|继续写|续写下一|继续下一|(?i:next\s+chapter|continue\s+writing))",
    )
    .ok()?;
    if forward.is_match(prompt) {
        return None;
    }
    let any = regex::Regex::new(chapter).ok()?;
    any.find(prompt)
        .and_then(|m| coomi_services::chapter_number(m.as_str()))
}

fn heading_chapter(line: &str) -> Option<u32> {
    let line = line.trim_start_matches(|c: char| c.is_whitespace() || c == '#');
    static HEADING: OnceLock<regex::Regex> = OnceLock::new();
    let heading = HEADING.get_or_init(|| {
        regex::Regex::new(
        r"^(?:第[0-9零〇一二两三四五六七八九十百千万]+章|(?i:chapter)\s+[0-9]+)(?:[\s:：、.．-]|$)"
    ).expect("chapter heading pattern")
    });
    if heading.is_match(line) {
        coomi_services::chapter_number(line)
    } else {
        None
    }
}

fn recent_chapter_files(root: &Path) -> Result<Vec<Vec<PathBuf>>> {
    let chapters = root.join("chapters");
    if !chapters.is_dir() {
        return Ok(Vec::new());
    }
    let chapters = resolve_bounded_path(root, chapters)?;
    let mut numbered = Vec::new();
    for entry in fs::read_dir(chapters)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if coomi_services::is_chapter_entry(&name, entry.path().is_dir()) {
            numbered.push((name, resolve_bounded_path(root, entry.path())?));
        }
    }
    numbered.sort_by(|a, b| coomi_services::compare_chapter_paths(&b.0, &a.0));
    let mut groups = Vec::new();
    for (_, path) in numbered {
        let files = if path.is_dir() {
            text_files(root, &path, 0)?
        } else {
            vec![path]
        };
        let files = files
            .into_iter()
            .filter(|p| {
                is_prose(p) && coomi_services::is_chapter_entry(&p.to_string_lossy(), false)
            })
            .collect::<Vec<_>>();
        if !files.is_empty() {
            groups.push(files);
        }
        if groups.len() == 3 {
            break;
        }
    }
    groups.reverse();
    Ok(groups)
}

fn canon_priority(
    root: &Path,
    path: &Path,
    kind: &str,
    text: &str,
    query: &RetrievalQuery,
) -> (u8, usize) {
    let core = path == root.join(".storydex/story-bible.md")
        || (path.parent() == Some(root) && kind == "story_bible");
    let current_outline = kind == "story_outline"
        && query.chapter.is_some()
        && text
            .lines()
            .any(|line| heading_chapter(line) == query.chapter);
    let class = if core {
        5
    } else if current_outline {
        4
    } else if kind == "story_outline" {
        3
    } else if kind == "story_style" {
        2
    } else {
        1
    };
    (class, query.named_score(path) + query.score(text))
}

struct SourcePassage {
    text: String,
    first_line: usize,
    last_line: usize,
    chapter: Option<u32>,
}

fn source_passages(text: &str) -> Vec<SourcePassage> {
    let mut result = Vec::new();
    let mut chapter = None;
    for (index, line) in text.lines().enumerate() {
        if let Some(number) = heading_chapter(line) {
            chapter = Some(number);
        }
        let chars = line.chars().collect::<Vec<_>>();
        for chunk in chars.chunks(900) {
            let text = chunk.iter().collect::<String>();
            if let Some(last) = result.last_mut().filter(|last: &&mut SourcePassage| {
                last.chapter == chapter
                    && last.last_line + 1 == index + 1
                    && last.text.chars().count() + chunk.len() < 1_000
                    && !line.starts_with('#')
            }) {
                last.text.push('\n');
                last.text.push_str(&text);
                last.last_line = index + 1;
            } else {
                result.push(SourcePassage {
                    text,
                    first_line: index + 1,
                    last_line: index + 1,
                    chapter,
                });
            }
        }
    }
    result
}

fn add_selected_file(
    root: &Path,
    path: &Path,
    source: &str,
    kind: &str,
    limit: usize,
    query: &RetrievalQuery,
    context: &mut StoryContext,
) {
    let source_chars = source.chars().count();
    if source_chars == 0 {
        return;
    }
    let guidance = if kind == "story_style" {
        "本书表达参考：仅采用适用的叙述、对白和节奏特征；样例中的人物、物件、事件不属于本书事实，不复制样例原句。\n"
    } else {
        ""
    };
    let available = limit.saturating_sub(guidance.chars().count());
    if available < 40 && source_chars > available {
        add_file(root, path, source, kind, 0, false, context);
        return;
    }
    let mut ranges = Vec::new();
    let selected = if source_chars <= available {
        ranges.push([1, source.lines().count().max(1)]);
        source.to_owned()
    } else {
        let passages = source_passages(source);
        let mut ranked = passages
            .iter()
            .enumerate()
            .map(|(index, passage)| {
                let chapter_score = usize::from(
                    kind == "story_outline"
                        && query.chapter.is_some()
                        && passage.chapter == query.chapter,
                ) * 100_000;
                let opening_score = usize::from(index == 0) * 1_000;
                (
                    index,
                    chapter_score + opening_score + query.score(&passage.text),
                )
            })
            .collect::<Vec<_>>();
        ranked.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
        let marker = "[按当前章节及相关内容选取；其余原文省略]\n";
        let mut remaining = available.saturating_sub(marker.chars().count());
        let mut chosen = Vec::new();
        for (index, _) in ranked {
            if remaining < 40 {
                break;
            }
            let passage = &passages[index];
            let label = format!("[原文第 {}—{} 行]\n", passage.first_line, passage.last_line);
            let room = remaining.saturating_sub(label.chars().count() + 1);
            let excerpt = passage.text.chars().take(room).collect::<String>();
            if excerpt.is_empty() {
                continue;
            }
            remaining =
                remaining.saturating_sub(label.chars().count() + excerpt.chars().count() + 1);
            chosen.push((index, format!("{label}{excerpt}\n")));
        }
        chosen.sort_by_key(|(index, _)| *index);
        let mut selected = String::from(marker);
        for (index, text) in chosen {
            let passage = &passages[index];
            ranges.push([passage.first_line, passage.last_line]);
            selected.push_str(&text);
        }
        selected
    };
    let before = context.blocks.len();
    add_file(
        root,
        path,
        &format!("{guidance}{selected}"),
        kind,
        limit,
        false,
        context,
    );
    if context.blocks.len() > before {
        let block = context.blocks.last_mut().expect("selected context block");
        block["selection"] = json!({"method": "chapter_and_lexical", "targetChapter": query.chapter,
            "sourceChars": source_chars, "sourceSha256": format!("{:x}", Sha256::digest(source.as_bytes())), "sourceLineRanges": ranges});
        if source_chars > available {
            block["truncated"] = json!(true);
        }
    }
}

fn add_recent_structure(
    root: &Path,
    groups: &[Vec<PathBuf>],
    sources: &[(PathBuf, String)],
    context: &mut StoryContext,
) {
    let mut text = String::from(
        "近期章节边界原文，仅用于观察开场、收尾和信息推进是否无意重复；有意呼应、缓冲章和延迟回收可以保留，不要求每章反转或悬念。\n",
    );
    let mut paths = Vec::new();
    for group in groups {
        let find = |path: &PathBuf| {
            sources
                .iter()
                .find(|(source, _)| source == path)
                .map(|(_, text)| text.as_str())
                .unwrap_or_default()
        };
        let Some(first) = group.first() else {
            continue;
        };
        let last = group.last().expect("nonempty chapter");
        if group
            .iter()
            .map(|path| find(path).chars().count())
            .sum::<usize>()
            <= 1_200
        {
            continue;
        }
        let start = find(first).chars().take(180).collect::<String>();
        let ending_chars = find(last).chars().collect::<Vec<_>>();
        let end = ending_chars[ending_chars.len().saturating_sub(180)..]
            .iter()
            .collect::<String>();
        for (path, label, excerpt) in [(first, "开头", start), (last, "结尾", end)] {
            let relative = path
                .strip_prefix(root)
                .expect("bounded chapter")
                .to_string_lossy()
                .replace('\\', "/");
            text.push_str(&format!("{relative} {label}：{excerpt}\n"));
            if !paths.contains(&relative) {
                paths.push(relative);
            }
        }
    }
    if paths.is_empty() {
        return;
    }
    context.add("recent_structure", &paths.join(", "), &text, 2_000, false);
    context.blocks.last_mut().expect("structure context")["sourcePaths"] = json!(paths);
}

pub(crate) fn prepare(workspace: &Path, payload: &ChatStreamRequest) -> Result<StoryContext> {
    prepare_with_plan(workspace, payload, "")
}

/// A proposal can improve retrieval, but never replaces the author's request or
/// becomes a source of established facts. Rebuild instead of appending beyond
/// the shared context budget.
pub(crate) fn refine_for_plan(
    workspace: &Path,
    payload: &ChatStreamRequest,
    initial: &StoryContext,
    plan_text: &str,
) -> Result<StoryContext> {
    if plan_text.trim().is_empty() {
        return Ok(initial.clone());
    }
    let mut context = prepare_with_plan(workspace, payload, plan_text)?;
    context.notes.push(
        "retrieval_refined_from_proposal:构思仅用于补充资料检索，未作为已发生事实加入正文参考"
            .into(),
    );
    Ok(context)
}

fn prepare_with_plan(
    workspace: &Path,
    payload: &ChatStreamRequest,
    plan_text: &str,
) -> Result<StoryContext> {
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

    // Read first so a short request such as “continue” still recalls characters
    // and threads from the actual ending, including unsaved editor drafts.
    let recent_groups = recent_chapter_files(&root)?;
    let mut recent_sources = Vec::new();
    for group in &recent_groups {
        for path in group {
            let text = source_text(&root, path, payload, &mut context)?;
            recent_sources.push((path.clone(), text));
        }
    }
    let latest_chapter = recent_groups
        .last()
        .and_then(|group| group.first())
        .and_then(|path| {
            let relative = path.strip_prefix(root.join("chapters")).ok()?;
            coomi_services::chapter_number(relative.components().next()?.as_os_str().to_str()?)
        });
    let query = RetrievalQuery::new(
        &payload.prompt,
        plan_text,
        &recent_sources,
        requested_chapter(&payload.prompt)
            .or_else(|| latest_chapter.and_then(|n| n.checked_add(1))),
    );

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
            } else if matches!(
                stem.as_str(),
                "故事圣经" | "故事设定" | "写作规则" | "story-bible"
            ) {
                "story_bible"
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
                    (kind != "worldbook" || !is_readme(p))
                        && (kind != "story_bible"
                            || (is_prose(p)
                                && !p.starts_with(root.join(".storydex/memory/backups"))))
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
    let mut candidates = Vec::new();
    for (path, kind) in canon {
        if candidates
            .iter()
            .any(|(existing, _, _, _)| existing == &path)
        {
            continue;
        }
        let text = source_text(&root, &path, payload, &mut context)?;
        let priority = canon_priority(&root, &path, kind, &text, &query);
        candidates.push((path, kind, text, priority));
    }
    candidates.sort_by(|a, b| b.3.cmp(&a.3).then_with(|| a.0.cmp(&b.0)));
    let mut remaining = 12_000usize;
    for (path, kind, text, _) in candidates {
        let preferred = match kind {
            "story_bible" => 3_000,
            "story_outline" => 4_000,
            "story_style" => 2_000,
            _ if query.score(&text) > 0 || query.named_score(&path) > 0 => 1_800,
            _ => 700,
        };
        let before = context.total_chars;
        add_selected_file(
            &root,
            &path,
            &text,
            kind,
            remaining.min(preferred),
            &query,
            &mut context,
        );
        remaining = remaining.saturating_sub(context.total_chars - before);
        used.insert(path);
    }

    // Reserve space for long-distance continuity before recent prose consumes it.
    let unsaved_paths = payload
        .editor_documents
        .iter()
        .filter(|d| d["dirty"] == true)
        .filter_map(|d| d["relativePath"].as_str().map(|p| p.replace('\\', "/")))
        .collect::<Vec<_>>();
    let view = crate::story_continuity::read_with_exclusions(&root, &query.text(), &unsaved_paths)?;
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

    // Recent prose still runs in manuscript order; selection above only changes
    // which reference passages receive space, not the story chronology.
    let mut recent_remaining = 16_000usize.min(
        MAX_CONTEXT_CHARS
            .saturating_sub(context.total_chars)
            .saturating_sub(8_000),
    );
    let recent_count = recent_sources.len();
    for (index, (path, text)) in recent_sources.iter().enumerate() {
        let limit = recent_remaining / (recent_count - index);
        recent_remaining = recent_remaining.saturating_sub(text.chars().count().min(limit));
        add_file(
            &root,
            path,
            text,
            "recent_segments",
            limit,
            true,
            &mut context,
        );
        used.insert(path.clone());
    }
    add_recent_structure(&root, &recent_groups, &recent_sources, &mut context);
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
    let relevant_text = format!("{}\n{}", query.text(), context.references);
    for (directory, kind) in [(".storydex/characters", "active_characters")] {
        let mut candidates = Vec::new();
        let mut relevant_ids = HashSet::new();
        for path in text_files(&root, &root.join(directory), 1)? {
            if is_readme(&path) {
                continue;
            }
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
        candidates.sort_by(|a, b| {
            let score = |(path, text, _): &(PathBuf, String, Value)| {
                query.named_score(path).saturating_add(query.score(text))
            };
            score(b).cmp(&score(a)).then_with(|| a.0.cmp(&b.0))
        });
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
            if !relevant_text.contains(name)
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
    // Context tools are read-only. Check the saved projection against the live
    // candidate and independent review ledger instead of refreshing WIKI here.
    let loaded = (|| -> Result<(Value, Value, Value, Value)> {
        Ok((
            read_relation_document(root, ".storydex/wiki/knowledge_graph.json", None, context)?,
            read_relation_document(
                root,
                ".storydex/knowledge/relations.json",
                Some(payload),
                context,
            )?,
            read_relation_document(
                root,
                ".storydex/knowledge/relation-reviews.json",
                None,
                context,
            )?,
            read_relation_document(
                root,
                ".storydex/knowledge/source-identities.json",
                None,
                context,
            )?,
        ))
    })();
    let (graph, candidates, reviews, identities) = match loaded {
        Ok(documents) => documents,
        Err(error) => {
            context
                .notes
                .push(format!("knowledge_unavailable:关系投影或独立确认资料无法读取，本轮未引用关系；只读上下文不会自动重建 WIKI：{error}"));
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
        let id = relation["id"].as_str().unwrap_or_default();
        let matching = candidates["relations"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|candidate| candidate["id"] == id)
            .collect::<Vec<_>>();
        let review = &reviews[id];
        if id.is_empty()
            || matching.len() != 1
            || matching[0] != &relation["candidate"]
            || review["decision"] != "confirmed"
            || !relation["baseFingerprint"].is_string()
            || review["baseFingerprint"] != relation["baseFingerprint"]
        {
            context.notes.push(format!(
                "knowledge_stale:{id}：候选或作者确认已改变，未沿用旧投影"
            ));
            continue;
        }
        let fingerprint = format!(
            "{:x}",
            Sha256::digest(coomi_services::canonical_json_bytes(&json!([
                relation["baseFingerprint"],
                review
            ]))?)
        );
        if relation["fingerprint"].as_str() != Some(fingerprint.as_str()) {
            context
                .notes
                .push(format!("knowledge_stale:{id}：作者确认与投影版本不一致"));
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
        let mut reviewed_refs = Vec::new();
        for reference in &refs {
            let path = reference["path"].as_str().unwrap_or_default();
            let quote = reference["quote"].as_str().unwrap_or_default();
            let source = root.join(path);
            let source_id = reference["sourceId"].as_str().unwrap_or_default();
            let identity = &identities["sources"][source_id];
            if quote.is_empty()
                || !source.is_file()
                || source_id.is_empty()
                || identity["missing"] == true
                || identity["path"] != path
                || path.starts_with(".storydex/knowledge/")
            {
                break;
            }
            let source = resolve_bounded_path(root, source)?;
            let text =
                normalized_knowledge_text(&source, &source_text(root, &source, payload, context)?);
            if !text.contains(quote) {
                break;
            }
            quotes.push(format!("{path}：{quote}"));
            reviewed_refs.push(
                json!({"sourceId":source_id,"quote":quote,"matched":true,"unresolvedPath":null}),
            );
        }
        if quotes.len() != refs.len() {
            context
                .notes
                .push(format!("knowledge_stale:{id}：来源身份或引文已改变"));
            continue;
        }
        let mut candidate = matching[0].clone();
        candidate["sourceRefs"] = json!(reviewed_refs);
        let base = format!(
            "{:x}",
            Sha256::digest(coomi_services::canonical_json_bytes(
                &json!({"candidate":candidate,"removed":false})
            )?)
        );
        if relation["baseFingerprint"].as_str() != Some(base.as_str()) {
            continue;
        }
        let expected = |key: &str| {
            review["changes"]
                .get(key)
                .filter(|value| value.as_str().is_some_and(|s| !s.trim().is_empty()))
                .unwrap_or(&matching[0][key])
        };
        if ["subjectId", "objectId", "predicate"]
            .iter()
            .any(|key| &relation[*key] != expected(key))
        {
            continue;
        }
        let subject = current_relation_label(root, expected("subjectId"), &graph, &identities)?;
        let object = current_relation_label(root, expected("objectId"), &graph, &identities)?;
        let (Some(subject), Some(object)) = (subject, object) else {
            context
                .notes
                .push(format!("knowledge_stale:{id}：关系端点已改变"));
            continue;
        };
        facts.push_str(&format!(
            "{} — {} — {}\n{}\n",
            subject,
            relation["predicate"].as_str().unwrap_or_default(),
            object,
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

fn read_relation_document(
    root: &Path,
    relative: &str,
    payload: Option<&ChatStreamRequest>,
    context: &mut StoryContext,
) -> Result<Value> {
    let path = resolve_bounded_path(root, root.join(relative))?;
    let draft = payload
        .and_then(|payload| {
            payload.editor_documents.iter().find(|document| {
                document["dirty"] == true
                    && document["relativePath"]
                        .as_str()
                        .is_some_and(|p| p.replace('\\', "/") == relative)
            })
        })
        .and_then(|document| document["content"].as_str());
    let text = if let Some(draft) = draft {
        anyhow::ensure!(
            draft.len() as u64 <= MAX_RELATION_DOCUMENT_BYTES,
            "关系资料 {relative} 超过 8 MiB，本轮省略关系"
        );
        context.notes.push(format!("unsaved_draft:{relative}"));
        context.record_read(relative, "editor_draft", Some(draft.chars().count()));
        draft.to_owned()
    } else {
        let file = fs::File::open(&path)?;
        anyhow::ensure!(
            file.metadata()?.len() <= MAX_RELATION_DOCUMENT_BYTES,
            "关系资料 {relative} 超过 8 MiB，本轮省略关系"
        );
        let mut text = String::new();
        file.take(MAX_RELATION_DOCUMENT_BYTES + 1)
            .read_to_string(&mut text)?;
        anyhow::ensure!(
            text.len() as u64 <= MAX_RELATION_DOCUMENT_BYTES,
            "关系资料 {relative} 读取期间超过 8 MiB，本轮省略关系"
        );
        context.record_read(relative, "disk", Some(text.chars().count()));
        text
    };
    serde_json::from_str(&text).context("关系资料不是有效 JSON")
}

fn normalized_knowledge_text(path: &Path, text: &str) -> String {
    if path
        .extension()
        .and_then(|extension| extension.to_str())
        .is_some_and(|extension| extension.eq_ignore_ascii_case("json"))
        && let Ok(value) = serde_json::from_str::<Value>(text)
        && let Ok(pretty) = serde_json::to_string_pretty(&value)
    {
        return pretty;
    }
    text.replace("\r\n", "\n").replace('\r', "\n")
}

fn current_relation_label(
    root: &Path,
    id: &Value,
    graph: &Value,
    identities: &Value,
) -> Result<Option<String>> {
    let Some(id) = id.as_str().filter(|s| !s.is_empty()) else {
        return Ok(None);
    };
    if !graph["graph"]["nodes"]
        .as_array()
        .into_iter()
        .flatten()
        .any(|node| node["id"] == id)
    {
        return Ok(None);
    }
    if id == "project:root" {
        return Ok(root
            .file_name()
            .map(|name| name.to_string_lossy().into_owned()));
    }
    let identity = &identities["sources"][id];
    let Some(relative) = identity["path"]
        .as_str()
        .filter(|path| !path.starts_with(".storydex/knowledge/"))
    else {
        return Ok(None);
    };
    if identity["missing"] == true || !root.join(relative).is_file() {
        return Ok(None);
    }
    let path = resolve_bounded_path(root, root.join(relative))?;
    let Some(stem) = path.file_stem().and_then(|name| name.to_str()) else {
        return Ok(None);
    };
    let mut title = stem.to_owned();
    if relative.starts_with("chapters/")
        && coomi_services::is_chapter_entry(relative, false)
        && let Some(parent) = path
            .parent()
            .filter(|parent| *parent != root.join("chapters"))
        && let Some(name) = parent.file_name()
    {
        title = format!("{} / {title}", name.to_string_lossy());
    }
    Ok(Some(title))
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
    fn selects_the_requested_late_outline_and_keeps_author_rules_under_pressure() {
        let directory = tempdir().expect("temp");
        let root = directory.path();
        let outline = (1..=24)
            .map(|chapter| {
                format!(
                    "## 第{chapter}章\n{}\n\n",
                    if chapter == 17 {
                        "潮汐证词改变了卫迟的选择，找出旧案见证人。".repeat(50)
                    } else {
                        format!("第{chapter}章旧计划，无关的过渡事件。").repeat(80)
                    }
                )
            })
            .collect::<String>();
        write(root, "章纲.md", &outline);
        write(root, "写作规则.md", "作者约定：此卷不得揭露姐姐身份。");
        write(root, "chapters/第16章.md", "前章结束，主角仍在码头等待。");
        for index in 0..24 {
            write(
                root,
                &format!(".storydex/worldbook/地区{index}.md"),
                &"其他地区的旧闻。".repeat(700),
            );
        }
        let payload = serde_json::from_value(json!({"prompt":"参考第三章的叙述，继续第十七章"}))
            .expect("payload");
        let context = prepare(root, &payload).expect("context");
        assert!(context.references.contains("潮汐证词改变了卫迟的选择"));
        assert!(context.references.contains("此卷不得揭露姐姐身份"));
        let outline_block = context
            .blocks
            .iter()
            .find(|b| b["sourcePaths"][0] == "章纲.md")
            .expect("outline block");
        assert_eq!(outline_block["selection"]["targetChapter"], 17);
        assert_eq!(outline_block["truncated"], true);
        assert!(
            outline_block["selection"]["sourceLineRanges"]
                .as_array()
                .expect("ranges")
                .iter()
                .any(|range| range[0].as_u64().unwrap_or(0) > 40)
        );
        assert_eq!(
            context.total_chars,
            context.references.chars().count() + context.preset.chars().count()
        );
        assert!(context.total_chars <= MAX_CONTEXT_CHARS);
        assert!(context.blocks.iter().any(|b| b["omitted"] == true));
    }

    #[test]
    fn continuation_infers_next_chapter_instead_of_reading_only_outline_opening() {
        let directory = tempdir().expect("temp");
        let root = directory.path();
        write(root, "chapters/第十五章/001.md", "灯塔的火熄灭了。");
        write(
            root,
            "章纲.md",
            &format!(
                "# 第一章\n{}\n# 第十六章\n下章应核对守塔人的口供。\n",
                "遥远的起点。".repeat(1_000)
            ),
        );
        let payload = serde_json::from_value(json!({"prompt":"接着写"})).expect("payload");
        let context = prepare(root, &payload).expect("context");
        assert!(context.references.contains("下章应核对守塔人的口供"));
        assert_eq!(requested_chapter("Write chapter 21"), Some(21));
        assert_eq!(requested_chapter("续写第一百零二章"), Some(102));
    }

    #[test]
    fn forward_request_ignores_reference_chapter_and_scaffold_readmes() {
        let directory = tempdir().expect("temp");
        let root = directory.path();
        write(root, "chapters/第十五章.md", "上章的河水已经退去。");
        write(
            root,
            "章纲.md",
            &format!(
                "# 第三章\n{}\n# 第十六章\n卫迟应当核对新的潮汐证词。\n",
                "只用于早期风格参考。".repeat(800)
            ),
        );
        write(
            root,
            ".storydex/worldbook/README.md",
            "世界书模板说明，不是小说设定。",
        );
        write(
            root,
            ".storydex/characters/README.md",
            "角色模板说明，不是人物资料。",
        );
        write(
            root,
            ".storydex/scripts/README.md",
            "作者在此保留的安排：暂不揭晓渡口真相。",
        );
        let payload = serde_json::from_value(json!({"prompt":"参考第三章的风格，继续下一章"}))
            .expect("payload");
        let context = prepare(root, &payload).expect("context");
        let outline = context
            .blocks
            .iter()
            .find(|block| block["sourcePaths"][0] == "章纲.md")
            .expect("outline");
        assert_eq!(outline["selection"]["targetChapter"], 16);
        assert!(context.references.contains("卫迟应当核对新的潮汐证词"));
        assert!(!context.references.contains("世界书模板说明"));
        assert!(!context.references.contains("角色模板说明"));
        assert!(context.references.contains("暂不揭晓渡口真相"));
        assert_eq!(requested_chapter("参考第三章，接着写"), None);
        assert_eq!(requested_chapter("参考第三章，继续写第十七章"), Some(17));
    }

    fn write_crowded_continuity(root: &Path) -> String {
        // Use a substantial evidence span: the reader deliberately fills small
        // gaps with short entries even after larger entries exceed its budget.
        let old_quote = format!(
            "卫迟保留着锈锁，等待当年见证人归来。{}",
            "他仍然记得那天码头上的每一句证词。".repeat(40)
        )
        .chars()
        .take(600)
        .collect::<String>();
        for chapter in 1..=16 {
            let path = format!("chapters/第{chapter}章/001.md");
            let (key, character, quote) = if chapter == 1 {
                ("锈锁旧案".to_owned(), "卫迟".to_owned(), old_quote.clone())
            } else {
                (
                    format!("支线{chapter}"),
                    format!("访客{chapter}"),
                    format!(
                        "访客{chapter}取走一份报纸。{}",
                        "这件事尚待后续核对。".repeat(40)
                    ),
                )
            };
            write(root, &path, &quote);
            let (record_path, record, _) =
                crate::story_continuity::draft(root, std::slice::from_ref(&path)).expect("draft");
            let extracted = json!({"entries":[{"kind":"thread","key":key,"summary":"线索仍待核对。","status":"active","characters":[character],"sourcePath":path,"quote":quote}]}).to_string();
            crate::story_continuity::save_completion(root, &record_path, record, &extracted)
                .expect("save continuity");
        }
        write(
            root,
            ".storydex/characters/卫迟.md",
            "卫迟曾因证词错误而自责，拒绝轻率指认。",
        );
        old_quote
    }

    #[test]
    fn plan_refinement_recalls_distant_evidence_without_promoting_the_plan_to_fact() {
        let directory = tempdir().expect("temp");
        let root = directory.path();
        let old_quote = write_crowded_continuity(root);
        write(root, "写作规则.md", "作者要求：旧案真凶仍不揭晓。");
        let payload =
            serde_json::from_value(json!({"prompt":"接着写，保持旧案悬而未决"})).expect("payload");
        let initial = prepare(root, &payload).expect("initial");
        assert!(!initial.references.contains(&old_quote));
        assert!(
            initial
                .notes
                .iter()
                .any(|note| note == "continuity_budget_omitted:锈锁旧案")
        );
        let refined = refine_for_plan(
            root,
            &payload,
            &initial,
            "本章考虑让卫迟核对锈锁旧案；构思新增事件XYZ尚未发生。",
        )
        .expect("refined");
        assert!(refined.references.contains(&old_quote));
        assert!(refined.references.contains("拒绝轻率指认"));
        assert!(refined.references.contains("旧案真凶仍不揭晓"));
        assert!(!refined.references.contains("构思新增事件XYZ"));
        assert!(
            refined
                .notes
                .iter()
                .any(|note| note.starts_with("retrieval_refined_from_proposal:"))
        );
        assert!(refined.total_chars <= MAX_CONTEXT_CHARS);
        assert_eq!(payload.prompt, "接着写，保持旧案悬而未决");
        assert_eq!(
            refine_for_plan(root, &payload, &initial, "")
                .expect("unchanged")
                .references,
            initial.references
        );
    }

    #[test]
    fn recent_editor_draft_guides_distant_recall_and_boundary_observation() {
        let directory = tempdir().expect("temp");
        let root = directory.path();
        let old_quote = write_crowded_continuity(root);
        let draft = format!(
            "卫迟带着锈锁回到渡口。{}卫迟仍未打开那把锁。",
            "潮水缓慢升高。".repeat(250)
        );
        let payload = serde_json::from_value(json!({"prompt":"继续", "editorDocuments":[{"relativePath":"chapters/第16章/001.md","dirty":true,"content":draft}]})).expect("payload");
        let context = prepare(root, &payload).expect("context");
        assert!(context.references.contains(&old_quote));
        assert!(context.has_source("recent_structure"));
        assert!(
            context
                .references
                .contains("有意呼应、缓冲章和延迟回收可以保留")
        );
        assert!(context.references.contains("卫迟仍未打开那把锁"));
        let reads = context
            .file_reads
            .iter()
            .filter(|read| read["arguments"]["path"] == "chapters/第16章/001.md")
            .collect::<Vec<_>>();
        assert_eq!(reads.len(), 1);
        assert_eq!(reads[0]["arguments"]["source"], "editor_draft");
        assert_eq!(
            context.total_chars,
            context.references.chars().count() + context.preset.chars().count()
        );
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
            ".storydex/worldbook/巨型设定.md",
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
                .any(|read| { read["arguments"]["path"] == ".storydex/worldbook/巨型设定.md" }),
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
        let graph_path = root.join(".storydex/wiki/knowledge_graph.json");
        let projection_before = fs::read(&graph_path).expect("projection bytes");
        let reviews_path = root.join(".storydex/knowledge/relation-reviews.json");
        let reviews_before = fs::read_to_string(&reviews_path).expect("review ledger");
        let mut payload: ChatStreamRequest =
            serde_json::from_value(json!({"prompt": "继续"})).expect("request");
        let context = prepare(root, &payload).expect("context");
        assert!(context.has_source("confirmed_relations"));
        assert!(context.references.contains("甲 — 旧识 — 乙"));
        assert!(!context.references.contains("仇敌"));
        let mut rejected: Value = serde_json::from_str(&reviews_before).expect("reviews");
        rejected["old-friends"]["decision"] = json!("rejected");
        fs::write(&reviews_path, rejected.to_string()).expect("change review");
        assert!(
            !prepare(root, &payload)
                .expect("rejected review")
                .has_source("confirmed_relations")
        );
        fs::write(&reviews_path, &reviews_before).expect("restore review");
        rejected["old-friends"]["decision"] = json!("confirmed");
        rejected["old-friends"]["changes"]["predicate"] = json!("同门");
        fs::write(&reviews_path, rejected.to_string()).expect("change review details");
        assert!(
            !prepare(root, &payload)
                .expect("changed review")
                .has_source("confirmed_relations")
        );
        fs::write(&reviews_path, &reviews_before).expect("restore review");
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
        write(
            root,
            ".storydex/knowledge/relations.json",
            r#"{"relations":[]}"#,
        );
        assert!(
            !prepare(root, &payload)
                .expect("removed candidate")
                .has_source("confirmed_relations")
        );
        assert_eq!(
            fs::read(&graph_path).expect("unchanged projection"),
            projection_before,
            "context preparation must not refresh or rewrite the projection"
        );
    }
}
