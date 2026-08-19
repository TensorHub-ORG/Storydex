//! Story project state routes for the Rust candidate.

#![allow(clippy::result_large_err)]

use crate::workspace::{
    atomic_write, current_workspace, normalize_relative, resolve_existing, resolve_target,
};
use crate::{AppState, error_response};
use axum::Json;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use chrono::Utc;
use serde::Deserialize;
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;
use uuid::Uuid;

const DEFAULT_CHAPTER_TEMPLATE_ID: &str = "default_chapter_directory";
const SINGLE_FILE_CHAPTER_TEMPLATE_ID: &str = "single_file_chapter_directory";
const SETTINGS_RELATIVE_PATH: &str = ".storydex/config/project-settings.json";
const CHAPTER_PROGRESS_RELATIVE_PATH: &str = ".storydex/memory/chapter-progress.json";
const CURRENT_STATE_RELATIVE_PATH: &str = ".storydex/memory/current-state/全部变量.json";
const LATEST_INDEX_RELATIVE_PATH: &str = ".storydex/memory/current-state/最新快照索引.json";
const SNAPSHOT_ROOT_RELATIVE_PATH: &str = ".storydex/memory/chapters";

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct StorySettingsInput {
    segment_extension: Option<String>,
    story_segment_format: Option<String>,
    max_segments_per_chapter: Option<i64>,
    story_fragment_count: Option<i64>,
    chapter_length_tier: Option<String>,
    chapter_word_count_target: Option<i64>,
    precise_word_count_enabled: Option<bool>,
    story_fragment_word_count: Option<i64>,
    story_fragment_word_count_min: Option<i64>,
    story_fragment_word_count_max: Option<i64>,
    story_chapter_template_id: Option<String>,
    auto_update_variables: Option<bool>,
    auto_update_wiki: Option<bool>,
    agent_commit_prompt_enabled: Option<bool>,
    auto_name_chapter_title: Option<bool>,
    context_concision_min_calls: Option<i64>,
    context_concision_max_calls: Option<i64>,
    context_concision_max_input_tokens: Option<i64>,
    #[serde(default)]
    chapter_completion: BTreeMap<String, bool>,
}

fn trace(started: Instant) -> Value {
    json!({
        "traceId": Uuid::new_v4().to_string(),
        "durationMs": started.elapsed().as_millis(),
        "toolCalls": 0,
        "llmCalls": 0,
    })
}

fn success(data: Value, started: Instant, action: &str) -> Response {
    Json(json!({
        "ok": true,
        "data": data,
        "error": null,
        "trace": trace(started),
        "audit": [{"action": action}],
    }))
    .into_response()
}

fn story_error(status: StatusCode, code: &str, message: impl AsRef<str>) -> Response {
    error_response(status, code, message.as_ref())
}

fn io_error(action: &str, error: impl std::fmt::Display) -> Response {
    story_error(
        StatusCode::UNPROCESSABLE_ENTITY,
        "story_io_error",
        format!("{action} failed: {error}"),
    )
}

fn read_json(path: &Path) -> Value {
    fs::read_to_string(path)
        .ok()
        .and_then(|value| serde_json::from_str::<Value>(&value).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}))
}

fn resolve_optional_path(workspace: &Path, relative: &str) -> Result<Option<PathBuf>, Response> {
    let (_, target) = resolve_target(workspace, relative)?;
    if !target.exists() {
        return Ok(None);
    }
    resolve_existing(workspace, relative).map(|(_, path)| Some(path))
}

fn read_json_at(workspace: &Path, relative: &str) -> Result<Value, Response> {
    Ok(resolve_optional_path(workspace, relative)?
        .as_deref()
        .map(read_json)
        .unwrap_or_else(|| json!({})))
}

fn write_json(path: &Path, value: &Value) -> std::io::Result<()> {
    let mut bytes = serde_json::to_vec_pretty(value).map_err(std::io::Error::other)?;
    bytes.push(b'\n');
    atomic_write(path, &bytes)
}

fn default_settings() -> Value {
    json!({
        "version": 1,
        "storySegmentFormat": "md",
        "segmentExtension": ".md",
        "defaultDialogueQuote": "cn_double",
        "segmentNamingMode": "auto",
        "maxSegmentsPerChapter": 3,
        "storyFragmentCount": 1,
        "chapterLengthTier": "medium",
        "chapterWordCountTarget": 3000,
        "preciseWordCountEnabled": false,
        "storyFragmentWordCount": 3000,
        "storyFragmentWordCountMin": 3000,
        "storyFragmentWordCountMax": 3000,
        "storyChapterTemplateId": DEFAULT_CHAPTER_TEMPLATE_ID,
        "autoUpdateVariables": false,
        "autoUpdateWiki": false,
        "autoUpdateVariablesNote": "自动更新变量需要较多耗时，建议每次仅生成单条剧情片段。",
        "agentCommitPromptEnabled": true,
        "autoNameChapterTitle": false,
        "contextConcisionMinCalls": 1,
        "contextConcisionMaxCalls": 2,
        "contextConcisionMaxInputTokens": 32000,
        "updatedAt": Utc::now().to_rfc3339(),
    })
}

fn normalize_extension(input: &StorySettingsInput, current: &Value) -> String {
    let raw = input
        .segment_extension
        .as_deref()
        .or(input.story_segment_format.as_deref())
        .or_else(|| current.get("storySegmentFormat").and_then(Value::as_str))
        .unwrap_or("md")
        .trim()
        .trim_start_matches('.')
        .to_ascii_lowercase();
    if raw == "txt" {
        ".txt".to_owned()
    } else {
        ".md".to_owned()
    }
}

fn i64_value(
    input: Option<i64>,
    current: &Value,
    key: &str,
    fallback: i64,
    min: i64,
    max: i64,
) -> i64 {
    input
        .or_else(|| current.get(key).and_then(Value::as_i64))
        .unwrap_or(fallback)
        .clamp(min, max)
}

fn bool_value(input: Option<bool>, current: &Value, key: &str, fallback: bool) -> bool {
    input
        .or_else(|| current.get(key).and_then(Value::as_bool))
        .unwrap_or(fallback)
}

fn read_settings(workspace: &Path) -> Result<Value, Response> {
    let current = read_json_at(workspace, SETTINGS_RELATIVE_PATH)?;
    let mut defaults = default_settings();
    if let (Some(defaults), Some(current)) = (defaults.as_object_mut(), current.as_object()) {
        for (key, value) in current {
            defaults.insert(key.clone(), value.clone());
        }
    }
    Ok(defaults)
}

fn chapter_completion(workspace: &Path) -> Result<Value, Response> {
    let progress = read_json_at(workspace, CHAPTER_PROGRESS_RELATIVE_PATH)?;
    let mut completion = Map::new();
    if let Some(chapters) = progress.get("chapters").and_then(Value::as_object) {
        for (relative, item) in chapters {
            completion.insert(
                relative.clone(),
                json!(
                    item.get("completed")
                        .and_then(Value::as_bool)
                        .unwrap_or(false)
                ),
            );
        }
    }
    Ok(Value::Object(completion))
}

fn replace_chapter_completion(
    workspace: &Path,
    completion: &BTreeMap<String, bool>,
) -> Result<(), Response> {
    let now = Utc::now().to_rfc3339();
    let mut chapters = Map::new();
    for (raw, completed) in completion {
        let relative = normalize_relative(raw)?;
        let display_name = Path::new(&relative)
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or(&relative)
            .to_owned();
        chapters.insert(
            relative,
            json!({
                "completed": completed,
                "updatedAt": now,
                "displayName": display_name,
            }),
        );
    }
    let (_, path) = resolve_target(workspace, CHAPTER_PROGRESS_RELATIVE_PATH)?;
    write_json(
        &path,
        &json!({
            "version": 1,
            "chapters": chapters,
            "updatedAt": now,
        }),
    )
    .map_err(|error| io_error("Writing chapter completion atomically", error))
}

fn settings_response(workspace: &Path) -> Result<Value, Response> {
    let mut value = read_settings(workspace)?;
    let completion = chapter_completion(workspace)?;
    if let Some(object) = value.as_object_mut() {
        let extension = object
            .get("storySegmentFormat")
            .and_then(Value::as_str)
            .map(|value| format!(".{value}"))
            .unwrap_or_else(|| ".md".to_owned());
        object.insert("segmentExtension".to_owned(), json!(extension));
        object.insert(
            "settingsPath".to_owned(),
            json!(".storydex/config/project-settings.json"),
        );
        object.insert(
            "chapterProgressPath".to_owned(),
            json!(".storydex/memory/chapter-progress.json"),
        );
        object.insert(
            "snapshotRoot".to_owned(),
            json!(".storydex/memory/chapters"),
        );
        object.insert(
            "currentStateRoot".to_owned(),
            json!(".storydex/memory/current-state"),
        );
        object.insert("memoryRoot".to_owned(), json!(".storydex/memory"));
        object.insert("storyLengthTierEnabled".to_owned(), json!(false));
        object.insert("chapterCompletion".to_owned(), completion);
        object.insert("source".to_owned(), json!("project_file"));
    }
    Ok(value)
}

pub(crate) async fn story_settings(State(state): State<AppState>) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    match settings_response(&workspace) {
        Ok(settings) => success(settings, started, "read_story_settings"),
        Err(response) => response,
    }
}

pub(crate) async fn update_story_settings(
    State(state): State<AppState>,
    Json(input): Json<StorySettingsInput>,
) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let current = match read_settings(&workspace) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let extension = normalize_extension(&input, &current);
    let format = extension.trim_start_matches('.');
    let mut updated = current;
    let baseline = updated.clone();
    let _retired_precision_request = input.precise_word_count_enabled;
    if let Some(object) = updated.as_object_mut() {
        object.insert("storySegmentFormat".to_owned(), json!(format));
        object.insert("segmentExtension".to_owned(), json!(extension));
        object.insert(
            "maxSegmentsPerChapter".to_owned(),
            json!(i64_value(
                input.max_segments_per_chapter,
                &baseline,
                "maxSegmentsPerChapter",
                3,
                1,
                99
            )),
        );
        object.insert(
            "storyFragmentCount".to_owned(),
            json!(i64_value(
                input.story_fragment_count,
                &baseline,
                "storyFragmentCount",
                1,
                1,
                20
            )),
        );
        let tier = input
            .chapter_length_tier
            .as_deref()
            .or_else(|| baseline.get("chapterLengthTier").and_then(Value::as_str))
            .filter(|value| matches!(*value, "short" | "medium" | "long"))
            .unwrap_or("medium")
            .to_owned();
        object.insert("chapterLengthTier".to_owned(), json!(tier));
        let target = i64_value(
            input
                .chapter_word_count_target
                .or(input.story_fragment_word_count),
            &baseline,
            "chapterWordCountTarget",
            3000,
            100,
            20_000,
        );
        object.insert("chapterWordCountTarget".to_owned(), json!(target));
        object.insert("storyFragmentWordCount".to_owned(), json!(target));
        object.insert(
            "storyFragmentWordCountMin".to_owned(),
            json!(i64_value(
                input.story_fragment_word_count_min,
                &baseline,
                "storyFragmentWordCountMin",
                target,
                100,
                20_000
            )),
        );
        object.insert(
            "storyFragmentWordCountMax".to_owned(),
            json!(i64_value(
                input.story_fragment_word_count_max,
                &baseline,
                "storyFragmentWordCountMax",
                target,
                100,
                20_000
            )),
        );
        object.insert("preciseWordCountEnabled".to_owned(), json!(false));
        if let Some(value) = input.story_chapter_template_id {
            object.insert(
                "storyChapterTemplateId".to_owned(),
                json!(if value.trim().is_empty() {
                    DEFAULT_CHAPTER_TEMPLATE_ID
                } else {
                    value.trim()
                }),
            );
        }
        object.insert(
            "autoUpdateVariables".to_owned(),
            json!(bool_value(
                input.auto_update_variables,
                &baseline,
                "autoUpdateVariables",
                false
            )),
        );
        object.insert(
            "autoUpdateWiki".to_owned(),
            json!(bool_value(
                input.auto_update_wiki,
                &baseline,
                "autoUpdateWiki",
                false
            )),
        );
        object.insert(
            "agentCommitPromptEnabled".to_owned(),
            json!(bool_value(
                input.agent_commit_prompt_enabled,
                &baseline,
                "agentCommitPromptEnabled",
                true
            )),
        );
        object.insert(
            "autoNameChapterTitle".to_owned(),
            json!(bool_value(
                input.auto_name_chapter_title,
                &baseline,
                "autoNameChapterTitle",
                false
            )),
        );
        let min_calls = i64_value(
            input.context_concision_min_calls,
            &baseline,
            "contextConcisionMinCalls",
            1,
            1,
            20,
        );
        let max_calls = i64_value(
            input.context_concision_max_calls,
            &baseline,
            "contextConcisionMaxCalls",
            2,
            min_calls,
            20,
        );
        object.insert("contextConcisionMinCalls".to_owned(), json!(min_calls));
        object.insert("contextConcisionMaxCalls".to_owned(), json!(max_calls));
        object.insert(
            "contextConcisionMaxInputTokens".to_owned(),
            json!(i64_value(
                input.context_concision_max_input_tokens,
                &baseline,
                "contextConcisionMaxInputTokens",
                32000,
                1000,
                500_000
            )),
        );
        object.insert("updatedAt".to_owned(), json!(Utc::now().to_rfc3339()));
    }
    let (_, settings_file) = match resolve_target(&workspace, SETTINGS_RELATIVE_PATH) {
        Ok(value) => value,
        Err(response) => return response,
    };
    if let Err(error) = write_json(&settings_file, &updated) {
        return io_error("Writing story settings atomically", error);
    }
    if let Err(response) = replace_chapter_completion(&workspace, &input.chapter_completion) {
        return response;
    }
    match settings_response(&workspace) {
        Ok(settings) => success(settings, started, "update_story_settings"),
        Err(response) => response,
    }
}

fn template_payload(id: &str) -> Value {
    if id == SINGLE_FILE_CHAPTER_TEMPLATE_ID {
        json!({
            "version": 1,
            "id": SINGLE_FILE_CHAPTER_TEMPLATE_ID,
            "name": "章节目录（单正文文件）",
            "relativePath": ".storydex/templates/chapters/single-file-chapter-directory-template.json",
            "description": "一个章节目录下只保留一个正文文件。",
            "chapterMode": "directory",
            "contentMode": "single_file",
            "chapterNamePattern": "第X章 标题",
            "segmentNaming": "正文.md",
            "initialChapters": [{"number": 1, "title": "未命名", "directory": "第1章 未命名", "firstSegment": "正文.md"}],
            "rules": ["每个章节目录只允许一个正文文件。"],
        })
    } else {
        json!({
            "version": 1,
            "id": DEFAULT_CHAPTER_TEMPLATE_ID,
            "name": "章节目录（多片段文件）",
            "relativePath": ".storydex/templates/chapters/default-chapter-directory-template.json",
            "description": "一个章节目录下按片段数量创建正文文件。",
            "chapterMode": "directory",
            "contentMode": "multi_fragment",
            "chapterNamePattern": "第X章 标题",
            "segmentNaming": "001.md",
            "initialChapters": [{"number": 1, "title": "未命名", "directory": "第1章 未命名", "firstSegment": "001.md"}],
            "rules": ["正文片段只写入 chapters/ 下的章节目录。"],
        })
    }
}

fn chapter_templates_list(workspace: &Path) -> Result<Vec<Value>, Response> {
    let mut templates = Vec::new();
    if let Some(root) = resolve_optional_path(workspace, ".storydex/templates/chapters")? {
        if !root.is_dir() {
            return Err(story_error(
                StatusCode::UNPROCESSABLE_ENTITY,
                "chapter_template_root_invalid",
                "The chapter template root is not a directory.",
            ));
        }
        let mut entries = fs::read_dir(&root)
            .map_err(|error| io_error("Reading chapter templates", error))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| io_error("Reading chapter templates", error))?;
        entries.sort_by_key(|entry| entry.file_name());
        for entry in entries {
            let file_type = entry
                .file_type()
                .map_err(|error| io_error("Inspecting a chapter template", error))?;
            if file_type.is_symlink() {
                return Err(story_error(
                    StatusCode::FORBIDDEN,
                    "workspace_symlink_forbidden",
                    "Chapter templates cannot be loaded through symbolic links.",
                ));
            }
            let relative = entry
                .path()
                .strip_prefix(workspace)
                .ok()
                .map(|value| value.to_string_lossy().replace('\\', "/"))
                .unwrap_or_default();
            let (_, path) = resolve_existing(workspace, &relative)?;
            if path.extension().and_then(|value| value.to_str()) != Some("json") {
                continue;
            }
            let payload = read_json(&path);
            if payload.as_object().is_some_and(|object| !object.is_empty()) {
                let id = payload
                    .get("id")
                    .and_then(Value::as_str)
                    .unwrap_or_else(|| {
                        path.file_stem()
                            .and_then(|value| value.to_str())
                            .unwrap_or_default()
                    })
                    .to_owned();
                let mut item = payload;
                if let Some(object) = item.as_object_mut() {
                    object.insert("id".to_owned(), json!(id));
                    object.insert("relativePath".to_owned(), json!(relative));
                }
                templates.push(item);
            }
        }
    }
    if templates.is_empty() {
        templates.push(template_payload(DEFAULT_CHAPTER_TEMPLATE_ID));
        templates.push(template_payload(SINGLE_FILE_CHAPTER_TEMPLATE_ID));
    }
    Ok(templates)
}

pub(crate) async fn chapter_templates(State(state): State<AppState>) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    match chapter_templates_list(&workspace) {
        Ok(items) => success(
            json!({"items": items}),
            started,
            "read_story_chapter_templates",
        ),
        Err(response) => response,
    }
}

fn chapter_state(workspace: &Path) -> Result<Vec<Value>, Response> {
    let progress = read_json_at(workspace, CHAPTER_PROGRESS_RELATIVE_PATH)?;
    let progress_map = progress.get("chapters").and_then(Value::as_object);
    let mut paths = Vec::new();
    let Some(chapters) = resolve_optional_path(workspace, "chapters")? else {
        return Ok(paths);
    };
    if !chapters.is_dir() {
        return Err(story_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "chapter_root_invalid",
            "The chapters path is not a directory.",
        ));
    }
    let entries = fs::read_dir(chapters)
        .map_err(|error| io_error("Reading story chapters", error))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| io_error("Reading story chapters", error))?;
    for entry in entries {
        let file_type = entry
            .file_type()
            .map_err(|error| io_error("Inspecting a story chapter", error))?;
        if file_type.is_symlink() {
            return Err(story_error(
                StatusCode::FORBIDDEN,
                "workspace_symlink_forbidden",
                "Story chapter routes do not follow symbolic links.",
            ));
        }
        let relative = entry
            .path()
            .strip_prefix(workspace)
            .ok()
            .map(|value| value.to_string_lossy().replace('\\', "/"))
            .unwrap_or_default();
        let (_, path) = resolve_existing(workspace, &relative)?;
        let is_chapter = file_type.is_dir()
            || path
                .extension()
                .and_then(|value| value.to_str())
                .is_some_and(|value| {
                    value.eq_ignore_ascii_case("md") || value.eq_ignore_ascii_case("txt")
                });
        if !is_chapter {
            continue;
        }
        let name = path
            .file_stem()
            .or_else(|| path.file_name())
            .and_then(|value| value.to_str())
            .unwrap_or_default();
        let progress_item = progress_map.and_then(|items| items.get(&relative));
        let completed = progress_item
            .and_then(|item| item.get("completed"))
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let updated_at = progress_item
            .and_then(|item| item.get("updatedAt"))
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_owned)
            .unwrap_or_else(|| Utc::now().to_rfc3339());
        paths.push(json!({
            "relativePath": relative,
            "name": path.file_name().and_then(|value| value.to_str()).unwrap_or_default(),
            "displayName": name,
            "chapterNumber": extract_chapter_number(name).unwrap_or(paths.len() as i64 + 1),
            "completed": completed,
            "updatedAt": updated_at,
        }));
    }
    paths.sort_by(|left, right| {
        left["relativePath"]
            .as_str()
            .cmp(&right["relativePath"].as_str())
    });
    Ok(paths)
}

fn extract_chapter_number(name: &str) -> Option<i64> {
    let digits = name
        .chars()
        .skip_while(|value| !value.is_ascii_digit())
        .take_while(|value| value.is_ascii_digit())
        .collect::<String>();
    (!digits.is_empty()).then(|| digits.parse().ok()).flatten()
}

pub(crate) async fn story_chapters(State(state): State<AppState>) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    match chapter_state(&workspace) {
        Ok(items) => success(json!({"items": items}), started, "read_story_chapters"),
        Err(response) => response,
    }
}

pub(crate) async fn update_chapter_completion(
    State(state): State<AppState>,
    Json(input): Json<Value>,
) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let raw_relative = input
        .get("chapterPath")
        .or_else(|| input.get("chapterRelativePath"))
        .and_then(Value::as_str)
        .unwrap_or_default();
    let relative = match normalize_relative(raw_relative) {
        Ok(value) => value,
        Err(_) => {
            return story_error(
                StatusCode::BAD_REQUEST,
                "chapter_path_invalid",
                "chapterPath is invalid.",
            );
        }
    };
    if !relative.starts_with("chapters/") {
        return story_error(
            StatusCode::BAD_REQUEST,
            "chapter_path_invalid",
            "chapterPath is invalid.",
        );
    }
    let (_, path) = match resolve_existing(&workspace, &relative) {
        Ok(value) => value,
        Err(response) => return response,
    };
    if !path.is_dir() {
        return story_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "chapter_not_directory",
            "The chapter path must be a directory.",
        );
    }
    let completed = input
        .get("completed")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let mut progress = match read_json_at(&workspace, CHAPTER_PROGRESS_RELATIVE_PATH) {
        Ok(value) => value,
        Err(response) => return response,
    };
    if !progress.is_object() {
        progress = json!({});
    }
    let object = progress.as_object_mut().expect("object initialized");
    let chapters = object.entry("chapters").or_insert_with(|| json!({}));
    if !chapters.is_object() {
        *chapters = json!({});
    }
    chapters.as_object_mut().expect("chapters object").insert(
        relative.clone(),
        json!({"completed": completed, "updatedAt": Utc::now().to_rfc3339(), "displayName": path.file_name().and_then(|value| value.to_str()).unwrap_or_default()}),
    );
    object.insert("version".to_owned(), json!(1));
    object.insert("updatedAt".to_owned(), json!(Utc::now().to_rfc3339()));
    let (_, progress_file) = match resolve_target(&workspace, CHAPTER_PROGRESS_RELATIVE_PATH) {
        Ok(value) => value,
        Err(response) => return response,
    };
    if let Err(error) = write_json(&progress_file, &progress) {
        return io_error("Writing chapter completion atomically", error);
    }
    match settings_response(&workspace) {
        Ok(settings) => success(settings, started, "update_story_chapter_completion"),
        Err(response) => response,
    }
}

pub(crate) async fn story_current_state(State(state): State<AppState>) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    match read_json_at(&workspace, CURRENT_STATE_RELATIVE_PATH) {
        Ok(data) => success(
            json!({
                "currentStatePath": CURRENT_STATE_RELATIVE_PATH,
                "latestSnapshotIndexPath": LATEST_INDEX_RELATIVE_PATH,
                "data": data,
            }),
            started,
            "read_story_current_state",
        ),
        Err(response) => response,
    }
}

fn snapshot_files(workspace: &Path) -> Result<Vec<PathBuf>, Response> {
    let Some(root) = resolve_optional_path(workspace, SNAPSHOT_ROOT_RELATIVE_PATH)? else {
        return Ok(Vec::new());
    };
    if !root.is_dir() {
        return Err(story_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "snapshot_root_invalid",
            "The story snapshot root is not a directory.",
        ));
    }
    let mut pending = vec![root];
    let mut files = Vec::new();
    while let Some(directory) = pending.pop() {
        let entries = fs::read_dir(&directory)
            .map_err(|error| io_error("Reading story snapshots", error))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| io_error("Reading story snapshots", error))?;
        for entry in entries {
            let file_type = entry
                .file_type()
                .map_err(|error| io_error("Inspecting a story snapshot", error))?;
            if file_type.is_symlink() {
                return Err(story_error(
                    StatusCode::FORBIDDEN,
                    "workspace_symlink_forbidden",
                    "Story snapshot routes do not follow symbolic links.",
                ));
            }
            let relative = entry
                .path()
                .strip_prefix(workspace)
                .ok()
                .map(|value| value.to_string_lossy().replace('\\', "/"))
                .unwrap_or_default();
            let (_, path) = resolve_existing(workspace, &relative)?;
            if file_type.is_dir() {
                pending.push(path);
            } else if path
                .extension()
                .and_then(|value| value.to_str())
                .is_some_and(|value| value.eq_ignore_ascii_case("json"))
            {
                files.push(path);
            }
        }
    }
    Ok(files)
}

pub(crate) async fn latest_snapshot(State(state): State<AppState>) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let index = match read_json_at(&workspace, LATEST_INDEX_RELATIVE_PATH) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let raw_relative = index
        .get("latestSnapshotPath")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();
    if !raw_relative.is_empty() {
        let relative = match normalize_relative(raw_relative) {
            Ok(value)
                if value.starts_with(&format!("{SNAPSHOT_ROOT_RELATIVE_PATH}/"))
                    && Path::new(&value)
                        .extension()
                        .and_then(|extension| extension.to_str())
                        .is_some_and(|extension| extension.eq_ignore_ascii_case("json")) =>
            {
                value
            }
            _ => {
                return story_error(
                    StatusCode::UNPROCESSABLE_ENTITY,
                    "snapshot_path_invalid",
                    "The latest snapshot index points outside the Storydex snapshot root.",
                );
            }
        };
        let (_, path) = match resolve_existing(&workspace, &relative) {
            Ok(value) => value,
            Err(response) => return response,
        };
        let snapshot = read_json(&path);
        if snapshot
            .as_object()
            .is_some_and(|object| !object.is_empty())
        {
            return success(
                json!({"relativePath": relative, "snapshot": snapshot}),
                started,
                "read_story_latest_snapshot",
            );
        }
    }
    let mut latest: Option<(PathBuf, std::time::SystemTime)> = None;
    let files = match snapshot_files(&workspace) {
        Ok(value) => value,
        Err(response) => return response,
    };
    for path in files {
        let modified = fs::metadata(&path)
            .and_then(|meta| meta.modified())
            .unwrap_or(std::time::UNIX_EPOCH);
        if latest
            .as_ref()
            .is_none_or(|(_, current)| modified > *current)
        {
            latest = Some((path, modified));
        }
    }
    if let Some((path, _)) = latest {
        let relative = path
            .strip_prefix(&workspace)
            .ok()
            .map(|value| value.to_string_lossy().replace('\\', "/"))
            .unwrap_or_default();
        return success(
            json!({"relativePath": relative, "snapshot": read_json(&path)}),
            started,
            "read_story_latest_snapshot",
        );
    }
    success(
        json!({"relativePath": "", "snapshot": {}}),
        started,
        "read_story_latest_snapshot",
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn default_settings_include_compatibility_fields() {
        let settings = default_settings();
        assert_eq!(settings["storySegmentFormat"], "md");
        assert_eq!(settings["preciseWordCountEnabled"], false);
        assert_eq!(
            settings["storyChapterTemplateId"],
            DEFAULT_CHAPTER_TEMPLATE_ID
        );
    }

    #[test]
    fn chapter_state_reads_completion_without_mutating_sources() {
        let root = tempdir().expect("root");
        fs::create_dir_all(root.path().join("chapters/第1章")).expect("chapter");
        fs::write(root.path().join("chapters/第1章/001.md"), "text").expect("segment");
        fs::create_dir_all(root.path().join(".storydex/memory")).expect("memory");
        write_json(
            &root.path().join(CHAPTER_PROGRESS_RELATIVE_PATH),
            &json!({"chapters": {"chapters/第1章": {"completed": true}}}),
        )
        .expect("progress");
        let workspace = root.path().canonicalize().expect("canonical workspace");
        assert_eq!(
            chapter_state(&workspace).expect("chapter state")[0]["completed"],
            true
        );
    }
}
