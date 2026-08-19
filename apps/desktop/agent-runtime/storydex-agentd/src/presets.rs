//! Structured preset management for the Rust desktop candidate.
//!
//! The disk contract intentionally matches Stable: a human-readable Markdown
//! file is paired with a same-stem `.preset.json` sidecar, while
//! `.storydex/presets/active.json` selects the single active main preset.

#![allow(clippy::result_large_err)]

use crate::workspace::{atomic_write, current_workspace, resolve_existing, resolve_target};
use crate::{AppState, error_response};
use axum::Json;
use axum::body::to_bytes;
use axum::extract::{Path as AxumPath, Request, State};
use axum::http::{Method, StatusCode};
use axum::response::{IntoResponse, Response};
use base64::Engine;
use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::cmp::{Ordering, Reverse};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Instant;
use uuid::Uuid;

const MAX_IMPORT_FILE_BYTES: usize = 8 * 1024 * 1024;
const MAX_REQUEST_BODY_BYTES: usize = 16 * 1024 * 1024;
const UNORDERED_SOURCE_ORDER_BASE: i64 = 100_000;
const IMPORTED_SOURCE_FORMATS: &[&str] = &["sillytavern", "generic"];

static PRESET_WRITE_LOCK: Mutex<()> = Mutex::new(());

#[derive(Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct PresetImportRequest {
    #[serde(default)]
    files: Vec<PresetImportFile>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct PresetImportFile {
    #[serde(default)]
    name: String,
    #[serde(default)]
    content_base64: String,
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

fn preset_error(status: StatusCode, code: &str, message: impl AsRef<str>) -> Response {
    error_response(status, code, message.as_ref())
}

fn io_error(action: &str, error: impl std::fmt::Display) -> Response {
    preset_error(
        StatusCode::UNPROCESSABLE_ENTITY,
        "preset_io_error",
        format!("{action} failed: {error}"),
    )
}

fn default_document() -> Value {
    json!({
        "version": 1,
        "meta": {
            "name": "",
            "description": "",
            "compatibleProviders": [],
            "updatedAt": "",
            "sourceFormat": null,
            "displayRegexes": [],
            "chatSquashMeta": {},
            "importWarnings": [],
        },
        "sampling": {
            "default": {
                "temperature": null,
                "topP": null,
                "topK": null,
                "frequencyPenalty": null,
                "presencePenalty": null,
                "seed": null,
                "stop": null,
            },
            "perPurpose": {},
        },
        "lengthContract": {
            "bodyMinChars": 1200,
            "bodyTargetChars": 2400,
            "bodyMaxChars": 3600,
            "paragraphMin": 6,
            "paragraphMax": 24,
            "requiredTags": [],
            "forbiddenTags": [],
        },
        "thinking": {
            "enabled": false,
            "mode": "stage_list",
            "stages": [],
            "injectPosition": "system_suffix",
            "visibleInOutput": false,
        },
        "style": {
            "pov": "",
            "narrator": "",
            "forbiddenWords": [],
            "forbiddenPatterns": [],
            "styleRules": [],
            "maxConsecutiveRepeat": 2,
            "proseRegister": "",
            "authorReference": null,
            "freeTextSlotPre": "",
            "freeTextSlotPost": "",
        },
        "memory": {
            "summaryFormat": "scene_outline",
            "summaryMinChars": 240,
            "summaryMaxChars": 600,
            "bigSummaryTriggerChapters": 8,
        },
        "terms": {
            "nameAliasMap": {},
            "termReplaceMap": {},
            "enforceAtGeneration": true,
        },
        "characterVoices": {},
    })
}

fn move_alias(object: &mut Map<String, Value>, canonical: &str, aliases: &[&str]) {
    if object.contains_key(canonical) {
        for alias in aliases {
            object.remove(*alias);
        }
        return;
    }
    for alias in aliases {
        if let Some(value) = object.remove(*alias) {
            object.insert(canonical.to_owned(), value);
            break;
        }
    }
}

fn rename_key(object: &mut Map<String, Value>, source: &str, target: &str) {
    if source == target || object.contains_key(target) {
        return;
    }
    if let Some(value) = object.remove(source) {
        object.insert(target.to_owned(), value);
    }
}

fn canonicalize_known_aliases(document: &mut Value) {
    let Some(root) = document.as_object_mut() else {
        return;
    };
    move_alias(root, "lengthContract", &["length_contract"]);
    move_alias(root, "characterVoices", &["character_voices"]);
    if let Some(meta) = root.get_mut("meta").and_then(Value::as_object_mut) {
        move_alias(meta, "compatibleProviders", &["compatible_providers"]);
        move_alias(meta, "updatedAt", &["updated_at"]);
        move_alias(meta, "sourceFormat", &["source_format"]);
        move_alias(meta, "displayRegexes", &["display_regexes"]);
        move_alias(meta, "chatSquashMeta", &["chat_squash_meta"]);
        move_alias(meta, "importWarnings", &["import_warnings"]);
    }
    if let Some(sampling) = root.get_mut("sampling").and_then(Value::as_object_mut) {
        move_alias(sampling, "perPurpose", &["per_purpose"]);
        if let Some(default) = sampling.get_mut("default").and_then(Value::as_object_mut) {
            canonicalize_sampling(default);
        }
        if let Some(per_purpose) = sampling
            .get_mut("perPurpose")
            .and_then(Value::as_object_mut)
        {
            for value in per_purpose.values_mut() {
                if let Some(params) = value.as_object_mut() {
                    canonicalize_sampling(params);
                }
            }
        }
    }
    if let Some(length) = root
        .get_mut("lengthContract")
        .and_then(Value::as_object_mut)
    {
        move_alias(length, "bodyMinChars", &["body_min_chars"]);
        move_alias(length, "bodyTargetChars", &["body_target_chars"]);
        move_alias(length, "bodyMaxChars", &["body_max_chars"]);
        move_alias(length, "paragraphMin", &["paragraph_min"]);
        move_alias(length, "paragraphMax", &["paragraph_max"]);
        move_alias(length, "requiredTags", &["required_tags"]);
        move_alias(length, "forbiddenTags", &["forbidden_tags"]);
    }
    if let Some(thinking) = root.get_mut("thinking").and_then(Value::as_object_mut) {
        move_alias(thinking, "injectPosition", &["inject_position"]);
        move_alias(thinking, "visibleInOutput", &["visible_in_output"]);
    }
    if let Some(style) = root.get_mut("style").and_then(Value::as_object_mut) {
        move_alias(style, "forbiddenWords", &["forbidden_words"]);
        move_alias(style, "forbiddenPatterns", &["forbidden_patterns"]);
        move_alias(style, "styleRules", &["style_rules"]);
        move_alias(style, "maxConsecutiveRepeat", &["max_consecutive_repeat"]);
        move_alias(style, "proseRegister", &["prose_register"]);
        move_alias(style, "authorReference", &["author_reference"]);
        move_alias(style, "freeTextSlotPre", &["free_text_slot_pre"]);
        move_alias(style, "freeTextSlotPost", &["free_text_slot_post"]);
        if let Some(author) = style
            .get_mut("authorReference")
            .and_then(Value::as_object_mut)
        {
            move_alias(author, "doNotBorrow", &["do_not_borrow"]);
        }
    }
    if let Some(memory) = root.get_mut("memory").and_then(Value::as_object_mut) {
        move_alias(memory, "summaryFormat", &["summary_format"]);
        move_alias(memory, "summaryMinChars", &["summary_min_chars"]);
        move_alias(memory, "summaryMaxChars", &["summary_max_chars"]);
        move_alias(
            memory,
            "bigSummaryTriggerChapters",
            &["big_summary_trigger_chapters"],
        );
    }
    if let Some(terms) = root.get_mut("terms").and_then(Value::as_object_mut) {
        move_alias(terms, "nameAliasMap", &["name_alias_map"]);
        move_alias(terms, "termReplaceMap", &["term_replace_map"]);
        move_alias(terms, "enforceAtGeneration", &["enforce_at_generation"]);
    }
    if let Some(voices) = root
        .get_mut("characterVoices")
        .and_then(Value::as_object_mut)
    {
        for voice in voices.values_mut() {
            if let Some(voice) = voice.as_object_mut() {
                move_alias(voice, "signatureActions", &["signature_actions"]);
            }
        }
    }
}

fn canonicalize_sampling(object: &mut Map<String, Value>) {
    move_alias(object, "topP", &["top_p"]);
    move_alias(object, "topK", &["top_k"]);
    move_alias(object, "frequencyPenalty", &["frequency_penalty"]);
    move_alias(object, "presencePenalty", &["presence_penalty"]);
}

fn disk_document(mut document: Value) -> Value {
    canonicalize_known_aliases(&mut document);
    let Some(root) = document.as_object_mut() else {
        return document;
    };
    rename_key(root, "lengthContract", "length_contract");
    rename_key(root, "characterVoices", "character_voices");
    if let Some(meta) = root.get_mut("meta").and_then(Value::as_object_mut) {
        rename_key(meta, "compatibleProviders", "compatible_providers");
        rename_key(meta, "updatedAt", "updated_at");
        rename_key(meta, "sourceFormat", "source_format");
        rename_key(meta, "displayRegexes", "display_regexes");
        rename_key(meta, "chatSquashMeta", "chat_squash_meta");
        rename_key(meta, "importWarnings", "import_warnings");
    }
    if let Some(sampling) = root.get_mut("sampling").and_then(Value::as_object_mut) {
        rename_key(sampling, "perPurpose", "per_purpose");
        if let Some(default) = sampling.get_mut("default").and_then(Value::as_object_mut) {
            disk_sampling(default);
        }
        if let Some(per_purpose) = sampling
            .get_mut("per_purpose")
            .and_then(Value::as_object_mut)
        {
            for value in per_purpose.values_mut() {
                if let Some(params) = value.as_object_mut() {
                    disk_sampling(params);
                }
            }
        }
    }
    if let Some(length) = root
        .get_mut("length_contract")
        .and_then(Value::as_object_mut)
    {
        rename_key(length, "bodyMinChars", "body_min_chars");
        rename_key(length, "bodyTargetChars", "body_target_chars");
        rename_key(length, "bodyMaxChars", "body_max_chars");
        rename_key(length, "paragraphMin", "paragraph_min");
        rename_key(length, "paragraphMax", "paragraph_max");
        rename_key(length, "requiredTags", "required_tags");
        rename_key(length, "forbiddenTags", "forbidden_tags");
    }
    if let Some(thinking) = root.get_mut("thinking").and_then(Value::as_object_mut) {
        rename_key(thinking, "injectPosition", "inject_position");
        rename_key(thinking, "visibleInOutput", "visible_in_output");
    }
    if let Some(style) = root.get_mut("style").and_then(Value::as_object_mut) {
        rename_key(style, "forbiddenWords", "forbidden_words");
        rename_key(style, "forbiddenPatterns", "forbidden_patterns");
        rename_key(style, "styleRules", "style_rules");
        rename_key(style, "maxConsecutiveRepeat", "max_consecutive_repeat");
        rename_key(style, "proseRegister", "prose_register");
        rename_key(style, "authorReference", "author_reference");
        rename_key(style, "freeTextSlotPre", "free_text_slot_pre");
        rename_key(style, "freeTextSlotPost", "free_text_slot_post");
        if let Some(author) = style
            .get_mut("author_reference")
            .and_then(Value::as_object_mut)
        {
            rename_key(author, "doNotBorrow", "do_not_borrow");
        }
    }
    if let Some(memory) = root.get_mut("memory").and_then(Value::as_object_mut) {
        rename_key(memory, "summaryFormat", "summary_format");
        rename_key(memory, "summaryMinChars", "summary_min_chars");
        rename_key(memory, "summaryMaxChars", "summary_max_chars");
        rename_key(
            memory,
            "bigSummaryTriggerChapters",
            "big_summary_trigger_chapters",
        );
    }
    if let Some(terms) = root.get_mut("terms").and_then(Value::as_object_mut) {
        rename_key(terms, "nameAliasMap", "name_alias_map");
        rename_key(terms, "termReplaceMap", "term_replace_map");
        rename_key(terms, "enforceAtGeneration", "enforce_at_generation");
    }
    if let Some(voices) = root
        .get_mut("character_voices")
        .and_then(Value::as_object_mut)
    {
        for voice in voices.values_mut() {
            if let Some(voice) = voice.as_object_mut() {
                rename_key(voice, "signatureActions", "signature_actions");
            }
        }
    }
    document
}

fn disk_sampling(object: &mut Map<String, Value>) {
    rename_key(object, "topP", "top_p");
    rename_key(object, "topK", "top_k");
    rename_key(object, "frequencyPenalty", "frequency_penalty");
    rename_key(object, "presencePenalty", "presence_penalty");
}

fn deep_merge(target: &mut Value, source: Value) {
    match (target, source) {
        (Value::Object(target), Value::Object(source)) => {
            for (key, value) in source {
                if let Some(existing) = target.get_mut(&key) {
                    deep_merge(existing, value);
                } else {
                    target.insert(key, value);
                }
            }
        }
        (target, source) => *target = source,
    }
}

fn normalize_document(mut input: Value, strict: bool) -> Result<(Value, Vec<String>), String> {
    if !input.is_object() {
        return Err("preset document must be a JSON object".to_owned());
    }
    canonicalize_known_aliases(&mut input);
    let mut normalized = default_document();
    deep_merge(&mut normalized, input);
    validate_document(&mut normalized, strict)?;
    Ok((normalized, Vec::new()))
}

fn validate_document(document: &mut Value, strict: bool) -> Result<(), String> {
    let Some(root) = document.as_object_mut() else {
        return Err("preset document must be a JSON object".to_owned());
    };
    require_integer(root, "version")?;
    for key in [
        "meta",
        "sampling",
        "lengthContract",
        "thinking",
        "style",
        "memory",
        "terms",
        "characterVoices",
    ] {
        if !root.get(key).is_some_and(Value::is_object) {
            return Err(format!("preset field {key} must be an object"));
        }
    }
    let meta = root
        .get("meta")
        .and_then(Value::as_object)
        .ok_or_else(|| "preset meta must be an object".to_owned())?;
    require_string(meta, "name")?;
    require_string(meta, "description")?;
    require_string_array(meta, "compatibleProviders")?;
    require_string(meta, "updatedAt")?;

    let sampling = root
        .get_mut("sampling")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| "preset sampling must be an object".to_owned())?;
    if !sampling.get("default").is_some_and(Value::is_object)
        || !sampling.get("perPurpose").is_some_and(Value::is_object)
    {
        return Err("preset sampling.default and sampling.perPurpose must be objects".to_owned());
    }
    if let Some(default) = sampling.get_mut("default").and_then(Value::as_object_mut) {
        validate_sampling(default)?;
    }
    if let Some(per_purpose) = sampling
        .get_mut("perPurpose")
        .and_then(Value::as_object_mut)
    {
        for (purpose, params) in per_purpose {
            let Some(params) = params.as_object_mut() else {
                return Err(format!("sampling.perPurpose.{purpose} must be an object"));
            };
            validate_sampling(params)?;
        }
    }

    let length = root
        .get("lengthContract")
        .and_then(Value::as_object)
        .ok_or_else(|| "preset lengthContract must be an object".to_owned())?;
    for key in [
        "bodyMinChars",
        "bodyTargetChars",
        "bodyMaxChars",
        "paragraphMin",
        "paragraphMax",
    ] {
        require_integer(length, key)?;
    }
    require_string_array(length, "requiredTags")?;
    require_string_array(length, "forbiddenTags")?;

    let thinking = root
        .get_mut("thinking")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| "preset thinking must be an object".to_owned())?;
    require_bool(thinking, "enabled")?;
    require_string_array(thinking, "stages")?;
    if thinking.get("mode").and_then(Value::as_str) != Some("stage_list") {
        return Err("thinking.mode must be stage_list".to_owned());
    }
    if !matches!(
        thinking.get("injectPosition").and_then(Value::as_str),
        Some("system_suffix" | "user_suffix")
    ) {
        return Err("thinking.injectPosition must be system_suffix or user_suffix".to_owned());
    }
    require_bool(thinking, "visibleInOutput")?;
    thinking.insert("visibleInOutput".to_owned(), Value::Bool(false));

    if let Some(modules) = root.get("modules")
        && !modules.is_array()
        && strict
    {
        return Err("preset modules must be an array".to_owned());
    }
    Ok(())
}

fn validate_sampling(params: &mut Map<String, Value>) -> Result<(), String> {
    normalize_bounded_number(params, "temperature", 0.0, 2.0)?;
    normalize_bounded_number(params, "topP", 0.0, 1.0)?;
    normalize_bounded_integer(params, "topK", 1, 1024)?;
    normalize_bounded_number(params, "frequencyPenalty", -2.0, 2.0)?;
    normalize_bounded_number(params, "presencePenalty", -2.0, 2.0)?;
    if !params
        .get("seed")
        .is_none_or(|value| value.is_null() || value.is_i64())
    {
        return Err("sampling seed must be an integer or null".to_owned());
    }
    if !params.get("stop").is_none_or(|value| {
        value.is_null()
            || value
                .as_array()
                .is_some_and(|items| items.iter().all(Value::is_string))
    }) {
        return Err("sampling stop must be a string array or null".to_owned());
    }
    Ok(())
}

fn normalize_bounded_number(
    object: &mut Map<String, Value>,
    key: &str,
    minimum: f64,
    maximum: f64,
) -> Result<(), String> {
    let Some(value) = object.get(key) else {
        return Ok(());
    };
    if value.is_null() {
        return Ok(());
    }
    let Some(number) = value.as_f64() else {
        return Err(format!("sampling {key} must be numeric or null"));
    };
    if !(minimum..=maximum).contains(&number) {
        object.insert(key.to_owned(), Value::Null);
    }
    Ok(())
}

fn normalize_bounded_integer(
    object: &mut Map<String, Value>,
    key: &str,
    minimum: i64,
    maximum: i64,
) -> Result<(), String> {
    let Some(value) = object.get(key) else {
        return Ok(());
    };
    if value.is_null() {
        return Ok(());
    }
    let Some(number) = value.as_i64() else {
        return Err(format!("sampling {key} must be an integer or null"));
    };
    if !(minimum..=maximum).contains(&number) {
        object.insert(key.to_owned(), Value::Null);
    }
    Ok(())
}

fn require_string(object: &Map<String, Value>, key: &str) -> Result<(), String> {
    if object.get(key).is_some_and(Value::is_string) {
        Ok(())
    } else {
        Err(format!("preset field {key} must be a string"))
    }
}

fn require_integer(object: &Map<String, Value>, key: &str) -> Result<(), String> {
    if object.get(key).is_some_and(Value::is_i64) {
        Ok(())
    } else {
        Err(format!("preset field {key} must be an integer"))
    }
}

fn require_bool(object: &Map<String, Value>, key: &str) -> Result<(), String> {
    if object.get(key).is_some_and(Value::is_boolean) {
        Ok(())
    } else {
        Err(format!("preset field {key} must be a boolean"))
    }
}

fn require_string_array(object: &Map<String, Value>, key: &str) -> Result<(), String> {
    if object.get(key).is_some_and(|value| {
        value
            .as_array()
            .is_some_and(|items| items.iter().all(Value::is_string))
    }) {
        Ok(())
    } else {
        Err(format!("preset field {key} must be a string array"))
    }
}

fn preset_root(workspace: &Path) -> PathBuf {
    workspace.join(".storydex").join("presets")
}

fn sidecar_path(markdown: &Path) -> PathBuf {
    let stem = markdown
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or("preset");
    markdown.with_file_name(format!("{stem}.preset.json"))
}

fn relative_path(workspace: &Path, path: &Path) -> Result<String, Response> {
    path.strip_prefix(workspace)
        .map(|relative| relative.to_string_lossy().replace('\\', "/"))
        .map_err(|_| {
            preset_error(
                StatusCode::FORBIDDEN,
                "preset_path_outside_workspace",
                "Preset path escapes the selected project.",
            )
        })
}

fn resolve_preset_markdown(
    workspace: &Path,
    raw_name: &str,
    must_exist: bool,
) -> Result<PathBuf, Response> {
    let raw = raw_name.trim().trim_start_matches('/').replace('\\', "/");
    if raw.is_empty() {
        return Err(preset_error(
            StatusCode::BAD_REQUEST,
            "preset_path_required",
            "Preset path is required.",
        ));
    }
    if !raw.contains('/') && !raw.to_ascii_lowercase().ends_with(".md") {
        for section in ["active", "library"] {
            let relative = format!(".storydex/presets/{section}/{raw}.md");
            if let Ok((_, path)) = resolve_existing(workspace, &relative)
                && path.is_file()
            {
                return Ok(path);
            }
        }
        return Err(preset_error(
            StatusCode::NOT_FOUND,
            "preset_not_found",
            format!("Preset does not exist: {raw}"),
        ));
    }
    let normalized = crate::workspace::normalize_relative(&raw)?;
    if !normalized.starts_with(".storydex/presets/")
        || !normalized.to_ascii_lowercase().ends_with(".md")
    {
        return Err(preset_error(
            StatusCode::BAD_REQUEST,
            "preset_path_invalid",
            "Preset paths must be Markdown files under .storydex/presets/.",
        ));
    }
    let (_, path) = if must_exist {
        resolve_existing(workspace, &normalized)?
    } else {
        resolve_target(workspace, &normalized)?
    };
    if must_exist && !path.is_file() {
        return Err(preset_error(
            StatusCode::NOT_FOUND,
            "preset_not_found",
            "Preset Markdown file does not exist.",
        ));
    }
    Ok(path)
}

fn default_pointer() -> Value {
    json!({
        "version": 1,
        "runtimePolicy": {
            "mainPresetLimit": 1,
            "rawJsonRuntime": false,
            "presetSchemaVersion": 1,
            "description": "Only files under presets/active or compiled presets may affect generation.",
        },
        "directories": {
            "active": ".storydex/presets/active",
            "library": ".storydex/presets/library",
            "compiled": ".storydex/presets/compiled",
            "blocked": ".storydex/presets/blocked",
        },
        "activeMainPreset": "",
        "activePatches": [],
    })
}

fn pointer_path(workspace: &Path) -> PathBuf {
    preset_root(workspace).join("active.json")
}

fn read_pointer(workspace: &Path) -> Result<Value, Response> {
    let path = pointer_path(workspace);
    if !path.exists() {
        return Ok(default_pointer());
    }
    let (_, path) = resolve_existing(workspace, ".storydex/presets/active.json")?;
    let content = fs::read_to_string(path)
        .map_err(|error| io_error("Reading the active preset pointer", error))?;
    let payload = serde_json::from_str::<Value>(&content).map_err(|error| {
        preset_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "preset_pointer_invalid",
            format!("Active preset pointer is invalid JSON: {error}"),
        )
    })?;
    if !payload.is_object() {
        return Err(preset_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "preset_pointer_invalid",
            "Active preset pointer must be a JSON object.",
        ));
    }
    Ok(payload)
}

fn write_json(path: &Path, payload: &Value) -> Result<(), Response> {
    let mut bytes = serde_json::to_vec_pretty(payload).map_err(|error| {
        preset_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "preset_encode_failed",
            format!("Unable to encode preset JSON: {error}"),
        )
    })?;
    bytes.push(b'\n');
    atomic_write(path, &bytes).map_err(|error| io_error("Writing preset JSON", error))
}

fn write_pointer(workspace: &Path, payload: &Value) -> Result<(), Response> {
    let (_, target) = resolve_target(workspace, ".storydex/presets/active.json")?;
    write_json(&target, payload)
}

fn read_document(markdown: &Path) -> Result<(Value, Vec<String>), Response> {
    let sidecar = sidecar_path(markdown);
    if !sidecar.exists() {
        return Ok((
            default_document(),
            vec!["no sidecar JSON; returning empty document".to_owned()],
        ));
    }
    let metadata = fs::symlink_metadata(&sidecar)
        .map_err(|error| io_error("Inspecting the preset sidecar", error))?;
    if metadata.file_type().is_symlink() {
        return Err(preset_error(
            StatusCode::FORBIDDEN,
            "preset_symlink_forbidden",
            "Preset sidecars cannot be symbolic links.",
        ));
    }
    let content = fs::read_to_string(&sidecar)
        .map_err(|error| io_error("Reading the preset sidecar", error))?;
    let parsed = match serde_json::from_str::<Value>(&content) {
        Ok(value) => value,
        Err(error) => {
            return Ok((
                default_document(),
                vec![format!("preset sidecar JSON parse error: {error}")],
            ));
        }
    };
    match normalize_document(parsed, false) {
        Ok(value) => Ok(value),
        Err(error) => Ok((
            default_document(),
            vec![format!("preset sidecar validation error: {error}")],
        )),
    }
}

#[derive(Clone, Debug)]
struct PresetModule {
    id: String,
    title: String,
    slot: String,
    enabled_by_default: bool,
    priority: i64,
    scope: String,
    placement: String,
    purpose: Vec<String>,
    content: String,
    virtual_module: bool,
    source_format: String,
    source_order: Option<i64>,
    source_role: String,
    injection_position: Option<i64>,
    injection_depth: Option<i64>,
    injection_order: Option<i64>,
    injection_trigger: Vec<String>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct CompiledSection {
    id: String,
    title: String,
    slot: String,
    source_module_id: String,
    priority: i64,
    enabled: bool,
    scope: String,
    placement: String,
    purpose: Vec<String>,
    text: String,
    virtual_module: bool,
    source_order: Option<i64>,
    source_role: String,
    injection_position: Option<i64>,
    injection_depth: Option<i64>,
    injection_order: Option<i64>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct CompiledInjection {
    depth: i64,
    order: i64,
    role: String,
    text: String,
    source_module_ids: Vec<String>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct PresetRisk {
    level: String,
    code: String,
    message: String,
    source_module_id: String,
    line: Option<usize>,
}

fn string_value(object: &Map<String, Value>, key: &str, fallback: &str) -> String {
    object
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or(fallback)
        .to_owned()
}

fn bool_value(object: &Map<String, Value>, key: &str, fallback: bool) -> bool {
    object.get(key).and_then(Value::as_bool).unwrap_or(fallback)
}

fn integer_value(object: &Map<String, Value>, key: &str, fallback: i64) -> i64 {
    object.get(key).and_then(Value::as_i64).unwrap_or(fallback)
}

fn optional_integer(object: &Map<String, Value>, key: &str) -> Option<i64> {
    object.get(key).and_then(Value::as_i64)
}

fn string_list(object: &Map<String, Value>, key: &str) -> Vec<String> {
    object
        .get(key)
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_owned)
        .collect()
}

fn explicit_modules(document: &Value) -> Vec<PresetModule> {
    let Some(items) = document.get("modules").and_then(Value::as_array) else {
        return Vec::new();
    };
    if items.is_empty() {
        return Vec::new();
    }
    let mut modules = Vec::new();
    for (index, item) in items.iter().enumerate() {
        let Some(object) = item.as_object() else {
            continue;
        };
        let mut id = string_value(object, "id", "");
        if id.trim().is_empty() {
            id = format!("module_{}", index + 1);
        }
        modules.push(PresetModule {
            id,
            title: string_value(object, "title", ""),
            slot: string_value(object, "slot", "advanced"),
            enabled_by_default: bool_value(object, "enabledByDefault", true),
            priority: integer_value(object, "priority", 50),
            scope: string_value(object, "scope", "global"),
            placement: string_value(object, "placement", "turn_plan"),
            purpose: string_list(object, "purpose"),
            content: string_value(object, "content", ""),
            virtual_module: bool_value(object, "virtual", false),
            source_format: string_value(object, "sourceFormat", ""),
            source_order: optional_integer(object, "sourceOrder"),
            source_role: string_value(object, "sourceRole", ""),
            injection_position: optional_integer(object, "injectionPosition"),
            injection_depth: optional_integer(object, "injectionDepth"),
            injection_order: optional_integer(object, "injectionOrder"),
            injection_trigger: string_list(object, "injectionTrigger"),
        });
    }
    modules
}

fn get_str(document: &Value, path: &[&str]) -> String {
    let mut cursor = document;
    for key in path {
        let Some(next) = cursor.get(*key) else {
            return String::new();
        };
        cursor = next;
    }
    cursor.as_str().unwrap_or_default().to_owned()
}

fn get_bool(document: &Value, path: &[&str]) -> bool {
    let mut cursor = document;
    for key in path {
        let Some(next) = cursor.get(*key) else {
            return false;
        };
        cursor = next;
    }
    cursor.as_bool().unwrap_or(false)
}

fn get_i64(document: &Value, path: &[&str], fallback: i64) -> i64 {
    let mut cursor = document;
    for key in path {
        let Some(next) = cursor.get(*key) else {
            return fallback;
        };
        cursor = next;
    }
    cursor.as_i64().unwrap_or(fallback)
}

fn get_strings(document: &Value, path: &[&str]) -> Vec<String> {
    let mut cursor = document;
    for key in path {
        let Some(next) = cursor.get(*key) else {
            return Vec::new();
        };
        cursor = next;
    }
    cursor
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_owned)
        .collect()
}

fn virtual_module(
    id: &str,
    title: &str,
    slot: &str,
    priority: i64,
    content: String,
) -> PresetModule {
    PresetModule {
        id: id.to_owned(),
        title: title.to_owned(),
        slot: slot.to_owned(),
        enabled_by_default: true,
        priority,
        scope: "global".to_owned(),
        placement: "turn_plan".to_owned(),
        purpose: Vec::new(),
        content,
        virtual_module: true,
        source_format: String::new(),
        source_order: None,
        source_role: String::new(),
        injection_position: None,
        injection_depth: None,
        injection_order: None,
        injection_trigger: Vec::new(),
    }
}

fn virtual_modules(document: &Value) -> Vec<PresetModule> {
    let mut modules = Vec::new();
    let pre = get_str(document, &["style", "freeTextSlotPre"]);
    if !pre.trim().is_empty() {
        modules.push(virtual_module(
            "v1_free_text_slot_pre",
            "硬边界",
            "boundary",
            100,
            pre,
        ));
    }
    if let Some(author) = document
        .get("style")
        .and_then(|value| value.get("authorReference"))
        .and_then(Value::as_object)
    {
        let mut lines = Vec::new();
        let primary = string_value(author, "primary", "");
        if !primary.is_empty() {
            lines.push(format!("主参考: {primary}"));
        }
        let borrow = string_list(author, "borrow");
        if !borrow.is_empty() {
            lines.push(format!("借鉴: {}", borrow.join(" / ")));
        }
        let not_borrow = string_list(author, "doNotBorrow");
        if !not_borrow.is_empty() {
            lines.push(format!("剔除: {}", not_borrow.join(" / ")));
        }
        let secondary = string_list(author, "secondary");
        if !secondary.is_empty() {
            lines.push(format!("辅参考: {}", secondary.join(" / ")));
        }
        let notes = string_value(author, "notes", "");
        if !notes.is_empty() {
            lines.push(format!("备注: {notes}"));
        }
        if !lines.is_empty() {
            modules.push(virtual_module(
                "v1_author_reference",
                "参考作家",
                "author_reference",
                90,
                lines.join("\n"),
            ));
        }
    }
    let mut mechanics = Vec::new();
    let register = get_str(document, &["style", "proseRegister"]);
    if !register.trim().is_empty() {
        mechanics.push(format!("整体定位: {}", register.trim()));
    }
    mechanics.extend(
        get_strings(document, &["style", "styleRules"])
            .into_iter()
            .filter(|value| !value.trim().is_empty()),
    );
    if !mechanics.is_empty() {
        modules.push(virtual_module(
            "v1_language_mechanics",
            "语言机制",
            "language_mechanics",
            80,
            mechanics.join("\n"),
        ));
    }
    let mut negative = Vec::new();
    let forbidden_words = get_strings(document, &["style", "forbiddenWords"]);
    if !forbidden_words.is_empty() {
        negative.push(format!("禁词: {}", forbidden_words.join(" / ")));
    }
    let forbidden_patterns = get_strings(document, &["style", "forbiddenPatterns"]);
    if !forbidden_patterns.is_empty() {
        negative.push(format!("禁式: {}", forbidden_patterns.join(" / ")));
    }
    if !negative.is_empty() {
        modules.push(virtual_module(
            "v1_negative_rules",
            "禁用规则",
            "negative_rules",
            80,
            negative.join("\n"),
        ));
    }
    let pov = get_str(document, &["style", "pov"]);
    let narrator = get_str(document, &["style", "narrator"]);
    if !pov.is_empty() {
        let suffix = if narrator.is_empty() {
            String::new()
        } else {
            format!("（视角主角 {narrator}）")
        };
        modules.push(virtual_module(
            "v1_pov",
            "视角约束",
            "language_mechanics",
            75,
            format!("视角: {pov} {suffix}").trim().to_owned(),
        ));
    }
    if !narrator.is_empty()
        && let Some(voice) = document
            .get("characterVoices")
            .and_then(|value| value.get(&narrator))
            .and_then(Value::as_object)
    {
        let mut lines = Vec::new();
        let tone = string_value(voice, "tone", "");
        if !tone.is_empty() {
            lines.push(format!("视角主角口吻: {tone}"));
        }
        let actions = string_list(voice, "signatureActions");
        if !actions.is_empty() {
            lines.push(format!("标志动作: {}", actions.join(" / ")));
        }
        let taboo = string_list(voice, "taboo");
        if !taboo.is_empty() {
            lines.push(format!("禁忌: {}", taboo.join(" / ")));
        }
        if !lines.is_empty() {
            modules.push(virtual_module(
                "v1_character_voice",
                "视角主角口吻",
                "language_mechanics",
                70,
                lines.join("\n"),
            ));
        }
    }
    let minimum = get_i64(document, &["lengthContract", "bodyMinChars"], 1200);
    let target = get_i64(document, &["lengthContract", "bodyTargetChars"], 2400);
    let maximum = get_i64(document, &["lengthContract", "bodyMaxChars"], 3600);
    if minimum != 0 && maximum != 0 {
        let paragraph_min = get_i64(document, &["lengthContract", "paragraphMin"], 6);
        let paragraph_max = get_i64(document, &["lengthContract", "paragraphMax"], 24);
        modules.push(virtual_module(
            "v1_length_contract",
            "长度合同",
            "boundary",
            60,
            format!(
                "正文不少于 {minimum} 字，目标 {target} 字，上限 {maximum} 字；段落 {paragraph_min}-{paragraph_max} 段。不足下限视为未完成。"
            ),
        ));
    }
    if let Some(replacements) = document
        .get("terms")
        .and_then(|value| value.get("termReplaceMap"))
        .and_then(Value::as_object)
    {
        let entries = replacements
            .iter()
            .filter_map(|(key, value)| value.as_str().map(|value| format!("{key}→{value}")))
            .collect::<Vec<_>>();
        if !entries.is_empty() {
            modules.push(virtual_module(
                "v1_terms",
                "术语硬替换",
                "negative_rules",
                65,
                format!("术语硬替换: {}", entries.join(" / ")),
            ));
        }
    }
    let stages = get_strings(document, &["thinking", "stages"]);
    if get_bool(document, &["thinking", "enabled"]) && !stages.is_empty() {
        let content = stages
            .iter()
            .enumerate()
            .map(|(index, stage)| format!("{}. {stage}", index + 1))
            .collect::<Vec<_>>()
            .join("\n");
        modules.push(virtual_module(
            "v1_self_check",
            "落笔前检查",
            "self_check",
            70,
            content,
        ));
    }
    let post = get_str(document, &["style", "freeTextSlotPost"]);
    if !post.trim().is_empty() {
        modules.push(virtual_module(
            "v1_free_text_slot_post",
            "底置自由规则",
            "advanced",
            10,
            post,
        ));
    }
    modules
}

fn modules_from_document(document: &Value) -> Vec<PresetModule> {
    let explicit = explicit_modules(document);
    if explicit.is_empty() {
        virtual_modules(document)
    } else {
        explicit
    }
}

fn document_source_format(document: &Value) -> String {
    let source = get_str(document, &["meta", "sourceFormat"]);
    if !source.trim().is_empty() {
        return source.trim().to_ascii_lowercase();
    }
    get_str(document, &["runtimeDefaults", "sourceFormat"])
        .trim()
        .to_ascii_lowercase()
}

fn slot_rank(slot: &str) -> i64 {
    match slot {
        "boundary" => 0,
        "author_reference" => 10,
        "language_mechanics" => 20,
        "scene_module" => 30,
        "negative_rules" => 40,
        "self_check" => 50,
        "advanced" => 60,
        _ => 999,
    }
}

fn compare_modules(left: &PresetModule, right: &PresetModule, imported: bool) -> Ordering {
    if imported
        || IMPORTED_SOURCE_FORMATS.contains(&left.source_format.as_str())
        || IMPORTED_SOURCE_FORMATS.contains(&right.source_format.as_str())
    {
        return (left.source_order.unwrap_or(1_000_000), left.id.as_str())
            .cmp(&(right.source_order.unwrap_or(1_000_000), right.id.as_str()));
    }
    (
        slot_rank(&left.slot),
        Reverse(left.priority),
        left.id.as_str(),
    )
        .cmp(&(
            slot_rank(&right.slot),
            Reverse(right.priority),
            right.id.as_str(),
        ))
}

fn compare_sections(left: &CompiledSection, right: &CompiledSection, imported: bool) -> Ordering {
    if imported {
        return (
            left.source_order.unwrap_or(1_000_000),
            left.source_module_id.as_str(),
        )
            .cmp(&(
                right.source_order.unwrap_or(1_000_000),
                right.source_module_id.as_str(),
            ));
    }
    (
        slot_rank(&left.slot),
        Reverse(left.priority),
        left.source_module_id.as_str(),
    )
        .cmp(&(
            slot_rank(&right.slot),
            Reverse(right.priority),
            right.source_module_id.as_str(),
        ))
}

fn runtime_overrides(payload: &Value) -> (BTreeSet<String>, BTreeSet<String>, Vec<String>) {
    let Some(overrides) = payload.get("presetOverrides").and_then(Value::as_object) else {
        return (BTreeSet::new(), BTreeSet::new(), Vec::new());
    };
    (
        string_list(overrides, "enabledModuleIds")
            .into_iter()
            .collect(),
        string_list(overrides, "disabledModuleIds")
            .into_iter()
            .collect(),
        string_list(overrides, "temporaryRules"),
    )
}

fn strip_import_macros(text: &str) -> String {
    let mut output = String::new();
    let mut offset = 0;
    while let Some(start) = text[offset..].find("{{//") {
        let absolute = offset + start;
        output.push_str(&text[offset..absolute]);
        let Some(end) = text[absolute + 4..].find("}}") else {
            output.push_str(&text[absolute..]);
            offset = text.len();
            break;
        };
        offset = absolute + 4 + end + 2;
    }
    if offset < text.len() {
        output.push_str(&text[offset..]);
    }
    output
        .replace("{{trim}}", "")
        .replace("{{ trim }}", "")
        .trim()
        .to_owned()
}

fn role_name(raw: &str) -> String {
    match raw.trim().to_ascii_lowercase().as_str() {
        "model" | "bot" | "ai" => "assistant".to_owned(),
        "human" => "user".to_owned(),
        "system" | "user" | "assistant" => raw.trim().to_ascii_lowercase(),
        _ => "system".to_owned(),
    }
}

fn compile_document(document: &Value, payload: &Value) -> Value {
    let source_format = document_source_format(document);
    let imported = IMPORTED_SOURCE_FORMATS.contains(&source_format.as_str());
    let mut modules = modules_from_document(document);
    let mut warnings = Vec::new();
    let mut seen = BTreeSet::new();
    for module in &modules {
        if !seen.insert(module.id.clone()) {
            warnings.push(format!(
                "duplicate module id ignored by UI risk: {}",
                module.id
            ));
        }
        if slot_rank(&module.slot) == 999 {
            warnings.push(format!(
                "unknown module slot '{}' on {}; compiled after fixed slots",
                module.slot, module.id
            ));
        }
    }
    modules.sort_by(|left, right| compare_modules(left, right, imported));
    let (enabled_extra, disabled, temporary_rules) = runtime_overrides(payload);
    let mut sections = Vec::new();
    for module in &modules {
        let enabled = (module.enabled_by_default || enabled_extra.contains(&module.id))
            && !disabled.contains(&module.id);
        if !enabled {
            continue;
        }
        if imported
            && !module.injection_trigger.is_empty()
            && !module
                .injection_trigger
                .iter()
                .any(|value| value.eq_ignore_ascii_case("normal"))
        {
            continue;
        }
        let text = if imported {
            strip_import_macros(&module.content)
        } else {
            module.content.trim().to_owned()
        };
        if text.is_empty() {
            continue;
        }
        sections.push(CompiledSection {
            id: format!("{}:{}", module.slot, module.id),
            title: if module.title.is_empty() {
                module.id.clone()
            } else {
                module.title.clone()
            },
            slot: module.slot.clone(),
            source_module_id: module.id.clone(),
            priority: module.priority,
            enabled: true,
            scope: module.scope.clone(),
            placement: module.placement.clone(),
            purpose: module.purpose.clone(),
            text,
            virtual_module: module.virtual_module,
            source_order: module.source_order,
            source_role: module.source_role.clone(),
            injection_position: module.injection_position,
            injection_depth: module.injection_depth,
            injection_order: module.injection_order,
        });
    }
    let temporary = temporary_rules
        .into_iter()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
        .collect::<Vec<_>>()
        .join("\n");
    if !temporary.is_empty() {
        sections.push(CompiledSection {
            id: "runtime:temporary_rules".to_owned(),
            title: "本轮临时规则".to_owned(),
            slot: "boundary".to_owned(),
            source_module_id: "runtime_temporary_rules".to_owned(),
            priority: 110,
            enabled: true,
            scope: "turn".to_owned(),
            placement: "turn_plan".to_owned(),
            purpose: Vec::new(),
            text: temporary,
            virtual_module: true,
            source_order: None,
            source_role: String::new(),
            injection_position: None,
            injection_depth: None,
            injection_order: None,
        });
    }
    sections.sort_by(|left, right| compare_sections(left, right, imported));
    let compiled_text = if imported {
        sections
            .iter()
            .filter(|section| section.injection_position != Some(1))
            .map(|section| section.text.trim())
            .filter(|text| !text.is_empty())
            .collect::<Vec<_>>()
            .join("\n\n")
    } else {
        sections
            .iter()
            .map(|section| {
                format!(
                    "[{}/{} | priority {} | {}]\n{}",
                    section.slot,
                    section.source_module_id,
                    section.priority,
                    section.scope,
                    section.text
                )
            })
            .collect::<Vec<_>>()
            .join("\n\n")
    };
    let injections = if imported {
        build_injections(&sections)
    } else {
        Vec::new()
    };
    let risks = check_risks(&modules, &sections);
    json!({
        "compiledText": compiled_text,
        "sections": sections,
        "injections": injections,
        "risks": risks,
        "warnings": warnings,
    })
}

fn build_injections(sections: &[CompiledSection]) -> Vec<CompiledInjection> {
    let mut grouped =
        BTreeMap::<(i64, Reverse<i64>, i64, String), (Vec<String>, Vec<String>)>::new();
    for section in sections {
        if section.injection_position != Some(1) {
            continue;
        }
        let role = role_name(&section.source_role);
        let role_rank = match role.as_str() {
            "system" => 0,
            "user" => 1,
            "assistant" => 2,
            _ => 3,
        };
        let depth = section.injection_depth.unwrap_or(4);
        let order = section.injection_order.unwrap_or(100);
        let entry = grouped
            .entry((depth, Reverse(order), role_rank, role))
            .or_default();
        entry.0.push(section.text.trim().to_owned());
        entry.1.push(section.source_module_id.clone());
    }
    grouped
        .into_iter()
        .filter_map(|((depth, Reverse(order), _, role), (texts, ids))| {
            let text = texts
                .into_iter()
                .filter(|value| !value.is_empty())
                .collect::<Vec<_>>()
                .join("\n");
            (!text.is_empty()).then_some(CompiledInjection {
                depth,
                order,
                role,
                text,
                source_module_ids: ids,
            })
        })
        .collect()
}

fn contains_any(text: &str, terms: &[&str]) -> bool {
    terms.iter().any(|term| text.contains(term))
}

fn risk_line(text: &str, terms: &[&str]) -> Option<usize> {
    let lowered = text.to_lowercase();
    terms
        .iter()
        .filter_map(|term| lowered.find(term))
        .min()
        .map(|offset| {
            text[..offset.min(text.len())]
                .bytes()
                .filter(|byte| *byte == b'\n')
                .count()
                + 1
        })
}

fn check_risks(modules: &[PresetModule], sections: &[CompiledSection]) -> Vec<PresetRisk> {
    let injected = sections
        .iter()
        .map(|section| section.source_module_id.as_str())
        .collect::<BTreeSet<_>>();
    let mut risks = Vec::new();
    for module in modules {
        let text = module.content.to_lowercase();
        let patterns = [
            (
                "visible_cot",
                "error",
                &[
                    "<thinking>",
                    "</thinking>",
                    "思维链",
                    "chain-of-thought",
                    "chain of thought",
                    "展示推理",
                    "输出推理过程",
                ][..],
                "包含显式思维链或要求展示推理的文本。",
            ),
            (
                "forced_hook",
                "warning",
                &["末句留", "未结清的悬念", "强行", "cliffhanger", "悬念必须"][..],
                "包含强制 hook 或强制悬念规则。",
            ),
            (
                "auto_darkline",
                "warning",
                &[
                    "自动暗线",
                    "推进暗线",
                    "异象升级",
                    "超自然铺垫",
                    "地底水纹",
                    "伞里有东西",
                    "浮上来",
                ][..],
                "包含自动暗线、异象升级或谜题化物件风险。",
            ),
        ];
        for (code, level, terms, message) in patterns {
            if contains_any(&text, terms) {
                risks.push(PresetRisk {
                    level: level.to_owned(),
                    code: code.to_owned(),
                    message: message.to_owned(),
                    source_module_id: module.id.clone(),
                    line: risk_line(&module.content, terms),
                });
            }
        }
        if module.enabled_by_default
            && !module.content.trim().is_empty()
            && !injected.contains(module.id.as_str())
        {
            risks.push(PresetRisk {
                level: "warning".to_owned(),
                code: "not_injected".to_owned(),
                message: "模块默认启用但未进入最终注入文本。".to_owned(),
                source_module_id: module.id.clone(),
                line: None,
            });
        }
        if module.content.chars().count() > 2400 {
            risks.push(PresetRisk {
                level: "info".to_owned(),
                code: "overlong_module".to_owned(),
                message: "模块内容超过 2400 字，建议拆分为更小的模块。".to_owned(),
                source_module_id: module.id.clone(),
                line: None,
            });
        }
        let low_psychology = contains_any(&text, &["日常场景零心理", "少写心理", "不要心理"]);
        let high_psychology = contains_any(
            &text,
            &[
                "每两句心理",
                "每2句心理",
                "每三句心理",
                "每3句心理",
                "大量心理",
                "高频心理",
            ],
        );
        let no_darkline = contains_any(&text, &["不要暗线", "禁止暗线"]);
        let force_darkline = contains_any(&text, &["必须暗线", "每章暗线", "总要暗线"]);
        if (low_psychology && high_psychology) || (no_darkline && force_darkline) {
            risks.push(PresetRisk {
                level: "warning".to_owned(),
                code: "conflict_rules".to_owned(),
                message: "同一模块包含互相冲突的写作规则。".to_owned(),
                source_module_id: module.id.clone(),
                line: None,
            });
        }
    }
    risks
}

fn sync_markdown_modules(markdown: &Path, document: &Value) -> Result<(), Response> {
    if !markdown.exists() {
        return Ok(());
    }
    let text = fs::read_to_string(markdown)
        .map_err(|error| io_error("Reading preset Markdown for module synchronization", error))?;
    let mut lines = text.lines().map(str::to_owned).collect::<Vec<_>>();
    let Some(start) = lines.iter().position(|line| line.trim() == "## Modules") else {
        return Ok(());
    };
    let end = lines
        .iter()
        .enumerate()
        .skip(start + 1)
        .find_map(|(index, line)| line.starts_with("## ").then_some(index))
        .unwrap_or(lines.len());
    let mut rendered = vec!["## Modules".to_owned()];
    for module in modules_from_document(document) {
        rendered.push(format!(
            "- [{}] {} ({})",
            if module.enabled_by_default {
                "on"
            } else {
                "off"
            },
            if module.title.is_empty() {
                module.id.as_str()
            } else {
                module.title.as_str()
            },
            if module.slot.is_empty() {
                "advanced"
            } else {
                module.slot.as_str()
            }
        ));
    }
    lines.splice(start..end, rendered);
    let mut next = lines.join("\n");
    if text.ends_with('\n') {
        next.push('\n');
    }
    if next != text {
        atomic_write(markdown, next.as_bytes())
            .map_err(|error| io_error("Synchronizing preset Markdown modules", error))?;
    }
    Ok(())
}

fn write_document(markdown: &Path, document: Value) -> Result<(), Response> {
    let (normalized, _) = normalize_document(document, true)
        .map_err(|error| preset_error(StatusCode::BAD_REQUEST, "preset_document_invalid", error))?;
    let sidecar = sidecar_path(markdown);
    let disk = disk_document(normalized.clone());
    write_json(&sidecar, &disk)?;
    sync_markdown_modules(markdown, &normalized)
}

fn collect_markdown_files(directory: &Path) -> Result<Vec<PathBuf>, Response> {
    if !directory.exists() {
        return Ok(Vec::new());
    }
    let mut stack = vec![directory.to_path_buf()];
    let mut files = Vec::new();
    while let Some(current) = stack.pop() {
        for entry in
            fs::read_dir(&current).map_err(|error| io_error("Listing preset directories", error))?
        {
            let entry =
                entry.map_err(|error| io_error("Reading a preset directory entry", error))?;
            let path = entry.path();
            let metadata = fs::symlink_metadata(&path)
                .map_err(|error| io_error("Inspecting a preset directory entry", error))?;
            if metadata.file_type().is_symlink() {
                return Err(preset_error(
                    StatusCode::FORBIDDEN,
                    "preset_symlink_forbidden",
                    "Preset directories cannot contain symbolic links.",
                ));
            }
            if metadata.is_dir() {
                stack.push(path);
            } else if metadata.is_file()
                && path
                    .extension()
                    .and_then(|value| value.to_str())
                    .is_some_and(|value| value.eq_ignore_ascii_case("md"))
                && !path
                    .file_name()
                    .and_then(|value| value.to_str())
                    .is_some_and(|value| value.eq_ignore_ascii_case("README.md"))
            {
                files.push(path);
            }
        }
    }
    files.sort_by_key(|path| path.to_string_lossy().to_lowercase());
    Ok(files)
}

fn list_section(workspace: &Path, section: &str) -> Result<Vec<Value>, Response> {
    let relative = format!(".storydex/presets/{section}");
    let (_, directory) = resolve_target(workspace, &relative)?;
    let mut items = collect_markdown_files(&directory)?
        .into_iter()
        .map(|path| {
            let name = path
                .file_stem()
                .and_then(|value| value.to_str())
                .unwrap_or_default()
                .to_owned();
            let relative = relative_path(workspace, &path)?;
            Ok(json!({
                "name": name,
                "path": relative,
                "hasSidecar": sidecar_path(&path).exists(),
            }))
        })
        .collect::<Result<Vec<_>, Response>>()?;
    items.sort_by_key(|item| {
        item.get("name")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_lowercase()
    });
    Ok(items)
}

#[derive(Debug)]
struct MoveReceipt {
    source_markdown: PathBuf,
    destination_markdown: PathBuf,
    source_sidecar: PathBuf,
    destination_sidecar: PathBuf,
    moved_sidecar: bool,
}

fn move_preset_pair(markdown: &Path, destination: &Path) -> Result<MoveReceipt, Response> {
    if destination.exists() || sidecar_path(destination).exists() {
        return Err(preset_error(
            StatusCode::CONFLICT,
            "preset_destination_exists",
            "A preset with the same name already exists in the destination directory.",
        ));
    }
    let parent = destination.parent().ok_or_else(|| {
        preset_error(
            StatusCode::BAD_REQUEST,
            "preset_path_invalid",
            "Preset destination has no parent directory.",
        )
    })?;
    fs::create_dir_all(parent)
        .map_err(|error| io_error("Creating the preset destination directory", error))?;
    let source_sidecar = sidecar_path(markdown);
    let destination_sidecar = sidecar_path(destination);
    fs::rename(markdown, destination).map_err(|error| io_error("Moving preset Markdown", error))?;
    let moved_sidecar = if source_sidecar.exists() {
        if let Err(error) = fs::rename(&source_sidecar, &destination_sidecar) {
            let rollback = fs::rename(destination, markdown);
            let message = if let Err(rollback_error) = rollback {
                format!(
                    "Moving preset sidecar failed: {error}; Markdown rollback failed: {rollback_error}"
                )
            } else {
                format!("Moving preset sidecar failed: {error}; Markdown move was rolled back")
            };
            return Err(preset_error(
                StatusCode::UNPROCESSABLE_ENTITY,
                "preset_move_failed",
                message,
            ));
        }
        true
    } else {
        false
    };
    Ok(MoveReceipt {
        source_markdown: markdown.to_path_buf(),
        destination_markdown: destination.to_path_buf(),
        source_sidecar,
        destination_sidecar,
        moved_sidecar,
    })
}

fn rollback_move(receipt: &MoveReceipt) -> Result<(), String> {
    let mut failures = Vec::new();
    if receipt.moved_sidecar
        && let Err(error) = fs::rename(&receipt.destination_sidecar, &receipt.source_sidecar)
    {
        failures.push(format!("sidecar: {error}"));
    }
    if let Err(error) = fs::rename(&receipt.destination_markdown, &receipt.source_markdown) {
        failures.push(format!("Markdown: {error}"));
    }
    if failures.is_empty() {
        Ok(())
    } else {
        Err(failures.join("; "))
    }
}

#[derive(Clone, Debug)]
struct ImportModule {
    id: String,
    title: String,
    slot: String,
    enabled: bool,
    priority: i64,
    content: String,
    source_format: String,
    source_identifier: String,
    source_name: String,
    source_order: i64,
    source_role: String,
    source_system_prompt: Option<bool>,
    forbid_overrides: Option<bool>,
    injection_position: Option<i64>,
    injection_depth: Option<i64>,
    injection_order: Option<i64>,
    injection_trigger: Vec<String>,
    source: String,
}

#[derive(Debug)]
struct ImportResult {
    title: String,
    document: Value,
    markdown: String,
    warnings: Vec<String>,
    import_warnings: Vec<String>,
    display_regexes: Vec<Value>,
    chat_squash_meta: Value,
    modules: Vec<ImportModule>,
}

#[derive(Clone, Debug)]
struct Candidate {
    identifier: String,
    name: String,
    content: String,
    enabled: bool,
    role: String,
    index: usize,
    source: String,
    order_index: Option<i64>,
    order_enabled: Option<bool>,
    marker: bool,
    injection_position: Option<i64>,
    injection_depth: Option<i64>,
    injection_order: Option<i64>,
    injection_trigger: Vec<String>,
    source_system_prompt: Option<bool>,
    forbid_overrides: Option<bool>,
}

fn first_text(object: &Map<String, Value>, keys: &[&str]) -> String {
    keys.iter()
        .find_map(|key| object.get(*key).and_then(Value::as_str))
        .unwrap_or_default()
        .trim()
        .to_owned()
}

fn decode_import_bytes(content_base64: &str) -> Result<Vec<u8>, Response> {
    let mut encoded = content_base64.trim();
    if encoded.to_ascii_lowercase().starts_with("data:")
        && let Some((_, payload)) = encoded.split_once(',')
    {
        encoded = payload;
    }
    let bytes = BASE64_STANDARD.decode(encoded).map_err(|error| {
        preset_error(
            StatusCode::BAD_REQUEST,
            "preset_import_base64_invalid",
            format!("Invalid preset import base64 content: {error}"),
        )
    })?;
    if bytes.len() > MAX_IMPORT_FILE_BYTES {
        return Err(preset_error(
            StatusCode::PAYLOAD_TOO_LARGE,
            "preset_import_file_too_large",
            format!("Preset import files cannot exceed {MAX_IMPORT_FILE_BYTES} bytes."),
        ));
    }
    Ok(bytes)
}

fn decode_import_text(bytes: &[u8]) -> String {
    let utf8 = String::from_utf8_lossy(bytes);
    if !utf8.contains('\u{fffd}') {
        return utf8.trim_start_matches('\u{feff}').to_owned();
    }
    let (gb, _, had_errors) = encoding_rs::GB18030.decode(bytes);
    if !had_errors {
        return gb.into_owned();
    }
    utf8.into_owned()
}

fn title_from_payload(payload: &Map<String, Value>, fallback: &str) -> String {
    let direct = first_text(payload, &["name", "title", "display_name", "identifier"]);
    if !direct.is_empty() {
        return direct;
    }
    for key in ["meta", "root"] {
        if let Some(object) = payload.get(key).and_then(Value::as_object) {
            let nested = first_text(object, &["name", "title", "identifier"]);
            if !nested.is_empty() {
                return nested;
            }
        }
    }
    fallback.to_owned()
}

fn selected_prompt_order(raw: Option<&Value>) -> Vec<Value> {
    let Some(entries) = raw.and_then(Value::as_array) else {
        return Vec::new();
    };
    let direct = entries
        .iter()
        .filter(|entry| {
            entry
                .as_object()
                .and_then(|object| object.get("identifier"))
                .is_some_and(Value::is_string)
        })
        .cloned()
        .collect::<Vec<_>>();
    if !direct.is_empty() {
        return direct;
    }
    let mut by_character = BTreeMap::<i64, Vec<Value>>::new();
    for entry in entries {
        let Some(object) = entry.as_object() else {
            continue;
        };
        let Some(order) = object.get("order").and_then(Value::as_array) else {
            continue;
        };
        let character = object
            .get("character_id")
            .and_then(Value::as_i64)
            .unwrap_or_default();
        by_character.insert(order_key(character), order.clone());
    }
    for character in [100001, 100000] {
        if let Some(order) = by_character.get(&order_key(character))
            && !order.is_empty()
        {
            return order.clone();
        }
    }
    by_character
        .into_values()
        .max_by_key(Vec::len)
        .unwrap_or_default()
}

fn order_key(character: i64) -> i64 {
    if character == 100001 {
        0
    } else if character == 100000 {
        1
    } else {
        character.saturating_add(2)
    }
}

fn prompt_order_map(raw: Option<&Value>) -> BTreeMap<String, (i64, bool)> {
    selected_prompt_order(raw)
        .into_iter()
        .enumerate()
        .filter_map(|(index, entry)| {
            let object = entry.as_object()?;
            let identifier = object.get("identifier")?.as_str()?.to_owned();
            Some((
                identifier,
                (
                    i64::try_from(index).unwrap_or(i64::MAX),
                    object.get("enabled").and_then(Value::as_bool) != Some(false),
                ),
            ))
        })
        .collect()
}

fn list_candidates(items: &[Value]) -> Vec<Candidate> {
    let mut candidates = Vec::new();
    for (index, item) in items.iter().enumerate() {
        if let Some(content) = item.as_str() {
            if content.trim().is_empty() {
                continue;
            }
            candidates.push(Candidate {
                identifier: format!("item_{}", index + 1),
                name: format!("item_{}", index + 1),
                content: content.to_owned(),
                enabled: true,
                role: "system".to_owned(),
                index,
                source: "list".to_owned(),
                order_index: None,
                order_enabled: None,
                marker: false,
                injection_position: None,
                injection_depth: None,
                injection_order: None,
                injection_trigger: Vec::new(),
                source_system_prompt: None,
                forbid_overrides: None,
            });
            continue;
        }
        let Some(object) = item.as_object() else {
            continue;
        };
        let content = first_text(object, &["content", "text", "prompt", "value"]);
        if content.is_empty() {
            continue;
        }
        let mut identifier = first_text(object, &["identifier", "id", "name", "title"]);
        if identifier.is_empty() {
            identifier = format!("item_{}", index + 1);
        }
        let title = first_text(object, &["name", "title", "label", "identifier", "id"]);
        candidates.push(Candidate {
            identifier: identifier.clone(),
            name: if title.is_empty() { identifier } else { title },
            content,
            enabled: object.get("enabled").and_then(Value::as_bool) != Some(false),
            role: role_name(&first_text(object, &["role"])),
            index,
            source: "list".to_owned(),
            order_index: None,
            order_enabled: None,
            marker: false,
            injection_position: None,
            injection_depth: None,
            injection_order: None,
            injection_trigger: Vec::new(),
            source_system_prompt: None,
            forbid_overrides: None,
        });
    }
    candidates
}

fn silly_tavern_candidates(payload: &Map<String, Value>) -> Vec<Candidate> {
    let Some(prompts) = payload.get("prompts").and_then(Value::as_array) else {
        return Vec::new();
    };
    let order = prompt_order_map(payload.get("prompt_order"));
    let mut candidates = Vec::new();
    for (index, prompt) in prompts.iter().enumerate() {
        let Some(object) = prompt.as_object() else {
            continue;
        };
        let mut identifier = first_text(object, &["identifier", "id", "name"]);
        if identifier.is_empty() {
            identifier = format!("prompt_{}", index + 1);
        }
        let title = first_text(object, &["name", "title", "identifier"]);
        let order_info = if order.is_empty() {
            None
        } else {
            Some(order.get(&identifier).copied().unwrap_or((
                UNORDERED_SOURCE_ORDER_BASE + i64::try_from(index).unwrap_or_default(),
                false,
            )))
        };
        candidates.push(Candidate {
            identifier: identifier.clone(),
            name: if title.is_empty() { identifier } else { title },
            content: first_text(object, &["content", "text", "prompt", "value"]),
            enabled: object.get("enabled").and_then(Value::as_bool) != Some(false),
            role: role_name(&first_text(object, &["role"])),
            index,
            source: "prompts".to_owned(),
            order_index: order_info.map(|value| value.0),
            order_enabled: order_info.map(|value| value.1),
            marker: object.get("marker").and_then(Value::as_bool) == Some(true),
            injection_position: object.get("injection_position").and_then(Value::as_i64),
            injection_depth: object.get("injection_depth").and_then(Value::as_i64),
            injection_order: object.get("injection_order").and_then(Value::as_i64),
            injection_trigger: object
                .get("injection_trigger")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(|value| value.trim().to_ascii_lowercase())
                .filter(|value| !value.is_empty())
                .collect(),
            source_system_prompt: object.get("system_prompt").and_then(Value::as_bool),
            forbid_overrides: object.get("forbid_overrides").and_then(Value::as_bool),
        });
    }
    candidates.sort_by_key(|candidate| {
        (
            candidate.order_index.is_none(),
            candidate
                .order_index
                .unwrap_or(i64::try_from(candidate.index).unwrap_or_default()),
            candidate.index,
        )
    });
    candidates
}

const GENERIC_META_KEYS: &[&str] = &[
    "name",
    "title",
    "label",
    "identifier",
    "id",
    "version",
    "char_version",
    "creator",
    "author",
    "create_date",
    "created",
    "updated",
    "updatedat",
    "tags",
    "spec",
    "spec_version",
    "avatar",
    "extensions",
    "type",
    "kind",
];

fn generic_candidates(payload: &Map<String, Value>, prefix: &str, depth: usize) -> Vec<Candidate> {
    let mut candidates = Vec::new();
    for (key, value) in payload {
        if GENERIC_META_KEYS.contains(&key.to_ascii_lowercase().as_str()) {
            continue;
        }
        let identifier = if prefix.is_empty() {
            key.clone()
        } else {
            format!("{prefix}{key}")
        };
        if let Some(content) = value.as_str() {
            if content.trim().chars().count() < 8 {
                continue;
            }
            let index = candidates.len();
            candidates.push(Candidate {
                identifier: identifier.clone(),
                name: identifier,
                content: content.to_owned(),
                enabled: true,
                role: "system".to_owned(),
                index,
                source: "generic".to_owned(),
                order_index: None,
                order_enabled: None,
                marker: false,
                injection_position: None,
                injection_depth: None,
                injection_order: None,
                injection_trigger: Vec::new(),
                source_system_prompt: None,
                forbid_overrides: None,
            });
        } else if let Some(items) = value.as_array() {
            let base = candidates.len();
            let mut nested = list_candidates(items);
            for (offset, candidate) in nested.iter_mut().enumerate() {
                candidate.index = base + offset;
                candidate.identifier = format!("{identifier}_{}", candidate.identifier);
                if candidate.name.is_empty() {
                    candidate.name.clone_from(&candidate.identifier);
                }
            }
            candidates.extend(nested);
        } else if depth == 0
            && matches!(key.as_str(), "data" | "config" | "preset" | "settings")
            && let Some(object) = value.as_object()
        {
            let base = candidates.len();
            let mut nested = generic_candidates(object, &format!("{identifier}."), depth + 1);
            for (offset, candidate) in nested.iter_mut().enumerate() {
                candidate.index = base + offset;
            }
            candidates.extend(nested);
        }
    }
    candidates
}

fn candidates_from_payload(payload: &Value) -> Vec<Candidate> {
    if let Some(object) = payload.as_object() {
        let direct = silly_tavern_candidates(object);
        if !direct.is_empty() {
            return direct;
        }
        if let Some(root) = object.get("root").and_then(Value::as_object) {
            let tree = tree_candidates(root);
            if !tree.is_empty() {
                return tree;
            }
        }
        if object.get("children").is_some_and(Value::is_array) {
            let tree = tree_candidates(object);
            if !tree.is_empty() {
                return tree;
            }
        }
        return generic_candidates(object, "", 0);
    }
    payload
        .as_array()
        .map_or_else(Vec::new, |items| list_candidates(items))
}

fn tree_candidates(root: &Map<String, Value>) -> Vec<Candidate> {
    fn walk(node: &Map<String, Value>, inherited: bool, output: &mut Vec<Candidate>) {
        let enabled = inherited && node.get("enabled").and_then(Value::as_bool) != Some(false);
        let content = first_text(node, &["content", "text", "prompt", "value"]);
        if !content.is_empty() {
            let index = output.len();
            let mut identifier = first_text(node, &["identifier", "id", "name", "title"]);
            if identifier.is_empty() {
                identifier = format!("node_{}", index + 1);
            }
            let title = first_text(node, &["name", "title", "label", "identifier", "id"]);
            output.push(Candidate {
                identifier: identifier.clone(),
                name: if title.is_empty() { identifier } else { title },
                content,
                enabled,
                role: role_name(&first_text(node, &["role"])),
                index,
                source: "tree".to_owned(),
                order_index: None,
                order_enabled: None,
                marker: node.get("marker").and_then(Value::as_bool) == Some(true)
                    || node.get("kind").and_then(Value::as_str) == Some("marker"),
                injection_position: None,
                injection_depth: None,
                injection_order: None,
                injection_trigger: Vec::new(),
                source_system_prompt: None,
                forbid_overrides: None,
            });
        }
        if let Some(children) = node.get("children").and_then(Value::as_array) {
            for child in children {
                if let Some(child) = child.as_object() {
                    walk(child, enabled, output);
                }
            }
        }
    }
    let mut output = Vec::new();
    walk(root, true, &mut output);
    output
}

fn sampling_from_payload(payload: &Map<String, Value>) -> Value {
    let aliases = [
        ("temperature", "temperature"),
        ("top_p", "topP"),
        ("topP", "topP"),
        ("top_k", "topK"),
        ("topK", "topK"),
        ("frequency_penalty", "frequencyPenalty"),
        ("frequencyPenalty", "frequencyPenalty"),
        ("presence_penalty", "presencePenalty"),
        ("presencePenalty", "presencePenalty"),
        ("seed", "seed"),
    ];
    let mut sampling = Map::new();
    for (source, target) in aliases {
        if let Some(value) = payload.get(source)
            && value.is_number()
        {
            sampling.insert(target.to_owned(), value.clone());
        }
    }
    Value::Object(sampling)
}

fn extract_display_regexes(payload: &Map<String, Value>) -> Vec<Value> {
    let Some(extensions) = payload.get("extensions").and_then(Value::as_object) else {
        return Vec::new();
    };
    let mut sources = Vec::new();
    if let Some(regexes) = extensions
        .get("SPreset")
        .and_then(Value::as_object)
        .and_then(|spreset| spreset.get("RegexBinding"))
        .and_then(Value::as_object)
        .and_then(|binding| binding.get("regexes"))
        .and_then(Value::as_array)
    {
        sources.push(("SPreset.RegexBinding", regexes));
    }
    if let Some(regexes) = extensions.get("regex_scripts").and_then(Value::as_array) {
        sources.push(("extensions.regex_scripts", regexes));
    }
    let mut seen = BTreeSet::new();
    let mut result = Vec::new();
    for (source, entries) in sources {
        for (index, entry) in entries.iter().enumerate() {
            let Some(object) = entry.as_object() else {
                continue;
            };
            let script_name = object
                .get("scriptName")
                .and_then(Value::as_str)
                .map(str::to_owned)
                .unwrap_or_else(|| format!("regex_{}", index + 1));
            let find_regex = object
                .get("findRegex")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let key = object
                .get("id")
                .and_then(Value::as_str)
                .map(str::to_owned)
                .unwrap_or_else(|| format!("{script_name}\n{find_regex}"));
            if !seen.insert(key) {
                continue;
            }
            result.push(json!({
                "source": source,
                "scriptName": script_name,
                "findRegex": find_regex,
                "replaceString": object.get("replaceString").and_then(Value::as_str).unwrap_or_default(),
                "promptOnly": object.get("promptOnly").and_then(Value::as_bool) == Some(true),
                "markdownOnly": object.get("markdownOnly").and_then(Value::as_bool) == Some(true),
                "disabled": object.get("disabled").and_then(Value::as_bool) == Some(true),
                "runOnEdit": object.get("runOnEdit").and_then(Value::as_bool) == Some(true),
                "substituteRegex": object.get("substituteRegex").cloned().unwrap_or(Value::Null),
                "id": object.get("id").cloned().unwrap_or(Value::Null),
            }));
        }
    }
    result
}

fn extract_chat_squash(payload: &Map<String, Value>) -> Value {
    let Some(chat_squash) = payload
        .get("extensions")
        .and_then(Value::as_object)
        .and_then(|extensions| extensions.get("SPreset"))
        .and_then(Value::as_object)
        .and_then(|spreset| spreset.get("ChatSquash"))
        .and_then(Value::as_object)
    else {
        return json!({});
    };
    let mut result = Map::new();
    for key in [
        "user_prefix",
        "char_prefix",
        "prefix_system",
        "suffix_system",
        "user_role_system",
        "stop_string",
        "squashed_post_script",
    ] {
        if let Some(value) = chat_squash.get(key).and_then(Value::as_str)
            && !value.trim().is_empty()
        {
            result.insert(key.to_owned(), Value::String(value.to_owned()));
        }
    }
    result.insert(
        "enabled".to_owned(),
        chat_squash.get("enabled").cloned().unwrap_or(Value::Null),
    );
    Value::Object(result)
}

fn clean_module_id(raw: &str, index: usize, used: &mut BTreeSet<String>) -> String {
    let mut slug = String::new();
    let mut previous_underscore = false;
    for character in raw.chars() {
        if character.is_ascii_alphanumeric() || character == '_' {
            slug.push(character.to_ascii_lowercase());
            previous_underscore = false;
        } else if !previous_underscore {
            slug.push('_');
            previous_underscore = true;
        }
    }
    let slug = slug.trim_matches('_');
    let slug = if slug.is_empty() {
        format!("module_{}", index + 1)
    } else {
        slug.chars().take(54).collect()
    };
    let base = format!("st_{slug}");
    let mut candidate = base.clone();
    let mut suffix = 2;
    while used.contains(&candidate) {
        candidate = format!("{base}_{suffix}");
        suffix += 1;
    }
    used.insert(candidate.clone());
    candidate
}

fn infer_slot(candidate: &Candidate) -> String {
    if candidate.injection_position == Some(1) {
        return "author_reference".to_owned();
    }
    let label = format!("{} {}", candidate.name, candidate.identifier).to_lowercase();
    let boundary = [
        "main",
        "主提示",
        "主任务",
        "初始化",
        "职责",
        "边界",
        "安全",
        "长度",
        "字数",
        "输出",
        "格式",
        "content",
        "task",
        "reset",
    ];
    if (candidate.role == "system" && contains_any(&label, &boundary))
        || contains_any(&label, &boundary)
    {
        return "boundary".to_owned();
    }
    if contains_any(
        &label,
        &["author", "reference", "作家", "参考", "借鉴", "作者注释"],
    ) {
        return "author_reference".to_owned();
    }
    if contains_any(
        &label,
        &[
            "style",
            "prose",
            "writing",
            "dialogue",
            "文风",
            "语言",
            "写法",
            "对白",
            "反模板",
            "节奏",
            "漫改",
            "吐槽",
        ],
    ) {
        return "language_mechanics".to_owned();
    }
    if contains_any(
        &label,
        &[
            "world",
            "scenario",
            "scene",
            "character",
            "persona",
            "npc",
            "角色",
            "场景",
            "世界",
            "设定",
            "剧情",
            "性格",
            "锚定",
        ],
    ) {
        return "scene_module".to_owned();
    }
    if contains_any(
        &label,
        &[
            "forbid",
            "avoid",
            "negative",
            "ban",
            "禁",
            "不要",
            "防",
            "杀",
            "去八股",
            "润色",
        ],
    ) {
        return "negative_rules".to_owned();
    }
    if contains_any(
        &label,
        &["check", "summary", "summar", "总结", "检查", "自检", "摘要"],
    ) {
        return "self_check".to_owned();
    }
    "advanced".to_owned()
}

fn import_module_value(module: &ImportModule) -> Value {
    json!({
        "id": module.id,
        "title": module.title,
        "slot": module.slot,
        "enabledByDefault": module.enabled,
        "priority": module.priority,
        "scope": "global",
        "content": module.content,
        "tags": [module.source_format, module.source_role, module.source],
        "sourceFormat": module.source_format,
        "sourceIdentifier": module.source_identifier,
        "sourceName": module.source_name,
        "sourceOrder": module.source_order,
        "sourceRole": module.source_role,
        "sourceSystemPrompt": module.source_system_prompt,
        "forbidOverrides": module.forbid_overrides,
        "injectionPosition": module.injection_position,
        "injectionDepth": module.injection_depth,
        "injectionOrder": module.injection_order,
        "injectionTrigger": module.injection_trigger,
    })
}

fn safe_filename_stem(raw: &str, fallback: &str) -> String {
    let mut value = raw
        .trim()
        .chars()
        .map(|character| {
            if character.is_control() || "<>:\"/\\|?*".contains(character) {
                '_'
            } else {
                character
            }
        })
        .collect::<String>();
    value = value
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .trim_matches([' ', '.'])
        .chars()
        .take(80)
        .collect();
    if value.is_empty() {
        fallback.to_owned()
    } else {
        value
    }
}

fn filename_stem(filename: &str) -> String {
    Path::new(filename)
        .file_stem()
        .and_then(|value| value.to_str())
        .filter(|value| !value.trim().is_empty())
        .unwrap_or("imported-preset")
        .to_owned()
}

fn macro_warning_names(content: &str, counts: &mut BTreeMap<String, usize>) {
    let mut offset = 0;
    while let Some(start) = content[offset..].find("{{") {
        let absolute = offset + start;
        let Some(end) = content[absolute + 2..].find("}}") else {
            break;
        };
        let body = content[absolute + 2..absolute + 2 + end].trim();
        let name = body
            .split(|character: char| character.is_whitespace() || character == ':')
            .next()
            .unwrap_or(body);
        if !matches!(name.to_ascii_lowercase().as_str(), "user" | "char") {
            *counts.entry(name.to_owned()).or_default() += 1;
        }
        offset = absolute + 2 + end + 2;
    }
}

fn convert_import(bytes: &[u8], filename: &str) -> ImportResult {
    let text = decode_import_text(bytes);
    let fallback_title = filename_stem(filename);
    let looks_json = filename.to_ascii_lowercase().ends_with(".json")
        || text.trim_start().starts_with('{')
        || text.trim_start().starts_with('[');
    let mut warnings = Vec::new();
    let parsed = if looks_json {
        match serde_json::from_str::<Value>(&text) {
            Ok(value) => Some(value),
            Err(error) => {
                warnings.push(format!(
                    "JSON parse failed; imported as plain text: {error}"
                ));
                None
            }
        }
    } else {
        None
    };
    let title = parsed
        .as_ref()
        .and_then(Value::as_object)
        .map(|object| title_from_payload(object, &fallback_title))
        .unwrap_or_else(|| fallback_title.clone());
    let source_format = if parsed
        .as_ref()
        .and_then(Value::as_object)
        .is_some_and(|object| {
            object.get("prompts").is_some_and(Value::is_array)
                || object.get("prompt_order").is_some_and(Value::is_array)
        }) {
        "sillytavern"
    } else {
        "generic"
    };
    let mut candidates = parsed
        .as_ref()
        .map(candidates_from_payload)
        .unwrap_or_else(|| {
            vec![Candidate {
                identifier: "plain_text".to_owned(),
                name: title.clone(),
                content: text.clone(),
                enabled: true,
                role: "system".to_owned(),
                index: 0,
                source: "text".to_owned(),
                order_index: None,
                order_enabled: None,
                marker: false,
                injection_position: None,
                injection_depth: None,
                injection_order: None,
                injection_trigger: Vec::new(),
                source_system_prompt: None,
                forbid_overrides: None,
            }]
        });
    let mut seen_candidates = BTreeSet::new();
    candidates.retain(|candidate| {
        seen_candidates.insert((
            candidate.identifier.clone(),
            candidate.content.trim().to_owned(),
        ))
    });
    let mut modules = Vec::new();
    let mut used_ids = BTreeSet::new();
    let mut macro_counts = BTreeMap::new();
    for candidate in &candidates {
        if candidate.marker || candidate.content.trim().is_empty() {
            continue;
        }
        macro_warning_names(&candidate.content, &mut macro_counts);
        let id = clean_module_id(
            if candidate.identifier.is_empty() {
                candidate.name.as_str()
            } else {
                candidate.identifier.as_str()
            },
            candidate.index,
            &mut used_ids,
        );
        let order = candidate
            .order_index
            .unwrap_or_else(|| i64::try_from(candidate.index).unwrap_or_default());
        modules.push(ImportModule {
            id,
            title: if candidate.name.is_empty() {
                candidate.identifier.clone()
            } else {
                candidate.name.clone()
            },
            slot: infer_slot(candidate),
            enabled: candidate.order_enabled.unwrap_or(candidate.enabled),
            priority: (1000 - order).max(1),
            content: candidate.content.clone(),
            source_format: source_format.to_owned(),
            source_identifier: candidate.identifier.clone(),
            source_name: candidate.name.clone(),
            source_order: order,
            source_role: candidate.role.clone(),
            source_system_prompt: candidate.source_system_prompt,
            forbid_overrides: candidate.forbid_overrides,
            injection_position: candidate.injection_position,
            injection_depth: candidate.injection_depth,
            injection_order: candidate.injection_order,
            injection_trigger: candidate.injection_trigger.clone(),
            source: candidate.source.clone(),
        });
    }
    let mut import_warnings = macro_counts
        .into_iter()
        .take(40)
        .map(|(name, count)| {
            format!(
                "保留外部预设宏 {{{{{name}}}}} ×{count}（导入阶段不执行，运行时兼容层会尽量展开）"
            )
        })
        .collect::<Vec<_>>();
    if modules.is_empty() && !text.trim().is_empty() {
        modules.push(ImportModule {
            id: "imported_raw_content".to_owned(),
            title: title.clone(),
            slot: "advanced".to_owned(),
            enabled: true,
            priority: 500,
            content: text.clone(),
            source_format: source_format.to_owned(),
            source_identifier: "raw_content".to_owned(),
            source_name: title.clone(),
            source_order: 0,
            source_role: "system".to_owned(),
            source_system_prompt: None,
            forbid_overrides: None,
            injection_position: None,
            injection_depth: None,
            injection_order: None,
            injection_trigger: Vec::new(),
            source: "raw".to_owned(),
        });
        import_warnings.push("未识别出结构化模块，已将文件原文导入为单个模块。".to_owned());
    }
    let payload_object = parsed.as_ref().and_then(Value::as_object);
    let display_regexes = payload_object
        .map(extract_display_regexes)
        .unwrap_or_default();
    let chat_squash_meta = payload_object
        .map(extract_chat_squash)
        .unwrap_or_else(|| json!({}));
    if chat_squash_meta
        .as_object()
        .is_some_and(|object| !object.is_empty())
    {
        import_warnings.push("SPreset ChatSquash 元数据已提取（JavaScript 不执行）".to_owned());
    }
    let sampling = payload_object
        .map(sampling_from_payload)
        .unwrap_or_else(|| json!({}));
    let format_label = if source_format == "sillytavern" {
        "external"
    } else {
        "imported"
    };
    let selected_order = payload_object
        .map(|object| selected_prompt_order(object.get("prompt_order")))
        .unwrap_or_default();
    let mut document = default_document();
    deep_merge(
        &mut document,
        json!({
            "meta": {
                "name": title,
                "description": format!("Imported from {format_label} preset {filename}. Review modules before activation."),
                "compatibleProviders": [],
                "updatedAt": Utc::now().to_rfc3339(),
                "sourceFormat": source_format,
                "displayRegexes": display_regexes,
                "chatSquashMeta": chat_squash_meta,
                "importWarnings": import_warnings,
            },
            "sampling": {"default": sampling, "perPurpose": {}},
            "modules": modules.iter().map(import_module_value).collect::<Vec<_>>(),
            "moduleProfiles": [{"id": source_format, "label": format_label}],
            "riskPolicy": {"filteredOnImport": false},
            "runtimeDefaults": {"sourceFormat": source_format},
            "sillyTavern": {
                "sourceFilename": filename,
                "sourcePreset": parsed,
                "selectedPromptOrder": selected_order,
            },
        }),
    );
    let (document, _) = normalize_document(document, false)
        .expect("internally generated imported preset document must be valid");
    let mut markdown = vec![
        format!("# {title}"),
        String::new(),
        format!("Source: {format_label} preset `{filename}`"),
        format!("Imported modules: {}", modules.len()),
        String::new(),
        "Review the sidecar module switches before activation.".to_owned(),
    ];
    if !modules.is_empty() {
        markdown.extend([String::new(), "## Modules".to_owned()]);
        markdown.extend(modules.iter().map(|module| {
            format!(
                "- [{}] {} ({})",
                if module.enabled { "on" } else { "off" },
                module.title,
                module.slot
            )
        }));
    }
    ImportResult {
        title,
        document,
        markdown: markdown.join("\n") + "\n",
        warnings,
        import_warnings,
        display_regexes,
        chat_squash_meta,
        modules,
    }
}

fn import_response_item(
    original_name: &str,
    converted: &ImportResult,
    paths: Option<(String, String)>,
) -> Value {
    let (relative_path, sidecar_path) = paths.unwrap_or_default();
    json!({
        "name": original_name,
        "title": converted.title,
        "relativePath": relative_path,
        "sidecarPath": sidecar_path,
        "moduleCount": converted.modules.len(),
        "filteredCount": 0,
        "filteredBlocks": [],
        "warnings": converted.warnings,
        "importWarnings": converted.import_warnings,
        "displayRegexes": converted.display_regexes,
        "chatSquashMeta": converted.chat_squash_meta,
        "modules": converted.modules.iter().map(|module| json!({
            "id": module.id,
            "title": module.title,
            "slot": module.slot,
            "priority": module.priority,
            "enabledByDefault": module.enabled,
        })).collect::<Vec<_>>(),
        "sampling": converted.document.get("sampling").cloned().unwrap_or_else(|| json!({})),
    })
}

fn unique_library_stem(library: &Path, raw: &str, fallback: &str) -> String {
    let base = safe_filename_stem(raw, fallback);
    let mut candidate = base.clone();
    let mut index = 1;
    while library.join(format!("{candidate}.md")).exists()
        || library.join(format!("{candidate}.preset.json")).exists()
    {
        candidate = format!("{base}-{index}");
        index += 1;
    }
    candidate
}

fn write_import_pair(markdown: &Path, converted: &ImportResult) -> Result<(), Response> {
    let sidecar = sidecar_path(markdown);
    if markdown.exists() || sidecar.exists() {
        return Err(preset_error(
            StatusCode::CONFLICT,
            "preset_import_destination_exists",
            "Preset import destination already exists.",
        ));
    }
    atomic_write(markdown, converted.markdown.as_bytes())
        .map_err(|error| io_error("Writing imported preset Markdown", error))?;
    if let Err(response) = write_document(markdown, converted.document.clone()) {
        let cleanup = fs::remove_file(markdown);
        if let Err(cleanup_error) = cleanup {
            return Err(preset_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "preset_import_rollback_failed",
                format!(
                    "Preset sidecar write failed and Markdown cleanup also failed: {cleanup_error}"
                ),
            ));
        }
        return Err(response);
    }
    Ok(())
}

async fn body_json(request: Request) -> Result<Value, Response> {
    let bytes = to_bytes(request.into_body(), MAX_REQUEST_BODY_BYTES)
        .await
        .map_err(|error| {
            preset_error(
                StatusCode::BAD_REQUEST,
                "preset_request_body_invalid",
                format!("Unable to read preset request body: {error}"),
            )
        })?;
    if bytes.is_empty() {
        return Ok(json!({}));
    }
    serde_json::from_slice(&bytes).map_err(|error| {
        preset_error(
            StatusCode::BAD_REQUEST,
            "preset_request_json_invalid",
            format!("Preset request body is invalid JSON: {error}"),
        )
    })
}

pub(crate) async fn list(State(state): State<AppState>) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let active = match list_section(&workspace, "active") {
        Ok(value) => value,
        Err(response) => return response,
    };
    let library = match list_section(&workspace, "library") {
        Ok(value) => value,
        Err(response) => return response,
    };
    let pointer = match read_pointer(&workspace) {
        Ok(value) => value,
        Err(response) => return response,
    };
    success(
        json!({
            "active": active,
            "library": library,
            "activeMainPreset": pointer.get("activeMainPreset").and_then(Value::as_str).unwrap_or_default(),
        }),
        started,
        "list_presets",
    )
}

pub(crate) async fn schema() -> Response {
    let started = Instant::now();
    let schema = json!({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "PresetDocument",
        "type": "object",
        "required": ["version", "meta", "sampling", "lengthContract", "thinking", "style", "memory", "terms", "characterVoices"],
        "additionalProperties": true,
        "properties": {
            "version": {"type": "integer", "default": 1},
            "meta": {"type": "object", "additionalProperties": true},
            "sampling": {"type": "object", "additionalProperties": true},
            "lengthContract": {"type": "object", "additionalProperties": true},
            "thinking": {"type": "object", "additionalProperties": true},
            "style": {"type": "object", "additionalProperties": true},
            "memory": {"type": "object", "additionalProperties": true},
            "terms": {"type": "object", "additionalProperties": true},
            "characterVoices": {"type": "object", "additionalProperties": true},
            "modules": {"type": "array", "items": {"type": "object", "additionalProperties": true}},
        },
    });
    success(
        json!({"schema": schema, "version": 1}),
        started,
        "read_preset_schema",
    )
}

async fn import_presets(state: AppState, payload: PresetImportRequest, preview: bool) -> Response {
    let started = Instant::now();
    if payload.files.is_empty() {
        return preset_error(
            StatusCode::BAD_REQUEST,
            "preset_import_files_required",
            "At least one preset import file is required.",
        );
    }
    let workspace = match current_workspace(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let mut converted_files = Vec::new();
    for file in &payload.files {
        let name = file.name.trim();
        if name.is_empty() || name.chars().count() > 255 || name.contains(['/', '\\', '\0']) {
            return preset_error(
                StatusCode::BAD_REQUEST,
                "preset_import_filename_invalid",
                "Preset import file names must be simple file names of at most 255 characters.",
            );
        }
        let bytes = match decode_import_bytes(&file.content_base64) {
            Ok(value) => value,
            Err(response) => return response,
        };
        converted_files.push((name.to_owned(), convert_import(&bytes, name)));
    }
    if preview {
        return success(
            json!({
                "items": converted_files
                    .iter()
                    .map(|(name, converted)| import_response_item(name, converted, None))
                    .collect::<Vec<_>>()
            }),
            started,
            "preview_preset_import",
        );
    }
    let Ok(_guard) = PRESET_WRITE_LOCK.lock() else {
        return preset_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "preset_write_lock_failed",
            "Preset write lock is unavailable.",
        );
    };
    let (_, library) = match resolve_target(&workspace, ".storydex/presets/library") {
        Ok(value) => value,
        Err(response) => return response,
    };
    if let Err(error) = fs::create_dir_all(&library) {
        return io_error("Creating the preset library", error);
    }
    let mut written: Vec<(PathBuf, PathBuf)> = Vec::new();
    let mut items = Vec::new();
    for (name, converted) in &converted_files {
        let stem = unique_library_stem(&library, &converted.title, &filename_stem(name));
        let markdown = library.join(format!("{stem}.md"));
        if let Err(response) = write_import_pair(&markdown, converted) {
            let mut rollback_failures = Vec::new();
            for (md, sidecar) in written.iter().rev() {
                if sidecar.exists()
                    && let Err(error) = fs::remove_file(sidecar)
                {
                    rollback_failures.push(format!("{}: {error}", sidecar.display()));
                }
                if md.exists()
                    && let Err(error) = fs::remove_file(md)
                {
                    rollback_failures.push(format!("{}: {error}", md.display()));
                }
            }
            if !rollback_failures.is_empty() {
                return preset_error(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "preset_import_rollback_failed",
                    format!(
                        "Preset import failed and rollback was incomplete: {}",
                        rollback_failures.join("; ")
                    ),
                );
            }
            return response;
        }
        let sidecar = sidecar_path(&markdown);
        let paths = match (
            relative_path(&workspace, &markdown),
            relative_path(&workspace, &sidecar),
        ) {
            (Ok(markdown), Ok(sidecar)) => (markdown, sidecar),
            (Err(response), _) | (_, Err(response)) => return response,
        };
        items.push(import_response_item(name, converted, Some(paths)));
        written.push((markdown, sidecar));
    }
    success(json!({"items": items}), started, "import_presets")
}

pub(crate) async fn import(
    State(state): State<AppState>,
    Json(payload): Json<PresetImportRequest>,
) -> Response {
    import_presets(state, payload, false).await
}

pub(crate) async fn preview_import(
    State(state): State<AppState>,
    Json(payload): Json<PresetImportRequest>,
) -> Response {
    import_presets(state, payload, true).await
}

pub(crate) async fn active(State(state): State<AppState>) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let pointer = match read_pointer(&workspace) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let relative = pointer
        .get("activeMainPreset")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let (document, warnings) = if relative.is_empty() {
        (default_document(), Vec::new())
    } else {
        match resolve_preset_markdown(&workspace, relative, true) {
            Ok(markdown) => match read_document(&markdown) {
                Ok(value) => value,
                Err(response) => return response,
            },
            Err(response) => return response,
        }
    };
    success(
        json!({
            "activeMainPreset": relative,
            "document": document,
            "warnings": warnings,
        }),
        started,
        "read_active_preset",
    )
}

pub(crate) async fn document(
    State(state): State<AppState>,
    AxumPath(name): AxumPath<String>,
) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let markdown = match resolve_preset_markdown(&workspace, &name, true) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let (document, warnings) = match read_document(&markdown) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let relative = match relative_path(&workspace, &markdown) {
        Ok(value) => value,
        Err(response) => return response,
    };
    success(
        json!({
            "relativePath": relative,
            "document": document,
            "warnings": warnings,
        }),
        started,
        "read_preset_document",
    )
}

pub(crate) async fn save_document(
    State(state): State<AppState>,
    AxumPath(name): AxumPath<String>,
    request: Request,
) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let markdown = match resolve_preset_markdown(&workspace, &name, true) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let payload = match body_json(request).await {
        Ok(value) => value,
        Err(response) => return response,
    };
    let Ok(_guard) = PRESET_WRITE_LOCK.lock() else {
        return preset_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "preset_write_lock_failed",
            "Preset write lock is unavailable.",
        );
    };
    if let Err(response) = write_document(&markdown, payload) {
        return response;
    }
    let relative = match relative_path(&workspace, &markdown) {
        Ok(value) => value,
        Err(response) => return response,
    };
    success(
        json!({"relativePath": relative, "ok": true}),
        started,
        "save_preset_document",
    )
}

pub(crate) async fn patch_params(
    State(state): State<AppState>,
    AxumPath(name): AxumPath<String>,
    request: Request,
) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let markdown = match resolve_preset_markdown(&workspace, &name, true) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let patch = match body_json(request).await {
        Ok(value) if value.is_object() => value,
        Ok(_) => {
            return preset_error(
                StatusCode::BAD_REQUEST,
                "preset_patch_invalid",
                "Preset patch must be a JSON object.",
            );
        }
        Err(response) => return response,
    };
    let Ok(_guard) = PRESET_WRITE_LOCK.lock() else {
        return preset_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "preset_write_lock_failed",
            "Preset write lock is unavailable.",
        );
    };
    let (mut document, _) = match read_document(&markdown) {
        Ok(value) => value,
        Err(response) => return response,
    };
    deep_merge(&mut document, patch);
    if let Err(response) = write_document(&markdown, document) {
        return response;
    }
    let relative = match relative_path(&workspace, &markdown) {
        Ok(value) => value,
        Err(response) => return response,
    };
    success(
        json!({"relativePath": relative, "ok": true}),
        started,
        "patch_preset_params",
    )
}

async fn compile_or_check(
    state: AppState,
    name: String,
    request: Request,
    action: &str,
) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let markdown = match resolve_preset_markdown(&workspace, &name, true) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let payload = match body_json(request).await {
        Ok(value) if value.is_object() => value,
        Ok(_) => {
            return preset_error(
                StatusCode::BAD_REQUEST,
                "preset_compile_request_invalid",
                "Preset compile request must be a JSON object.",
            );
        }
        Err(response) => return response,
    };
    let (document, mut load_warnings) = if let Some(document) = payload.get("document") {
        match normalize_document(document.clone(), true) {
            Ok(value) => value,
            Err(error) => {
                return preset_error(StatusCode::BAD_REQUEST, "preset_document_invalid", error);
            }
        }
    } else {
        match read_document(&markdown) {
            Ok(value) => value,
            Err(response) => return response,
        }
    };
    let mut result = compile_document(&document, &payload);
    if !load_warnings.is_empty() {
        let existing = result
            .get_mut("warnings")
            .and_then(Value::as_array_mut)
            .expect("compile warnings are an array");
        load_warnings.extend(
            existing
                .drain(..)
                .filter_map(|value| value.as_str().map(str::to_owned)),
        );
        *existing = load_warnings.into_iter().map(Value::String).collect();
    }
    result["relativePath"] = match relative_path(&workspace, &markdown) {
        Ok(value) => Value::String(value),
        Err(response) => return response,
    };
    success(result, started, action)
}

pub(crate) async fn compile(
    State(state): State<AppState>,
    AxumPath(name): AxumPath<String>,
    request: Request,
) -> Response {
    compile_or_check(state, name, request, "compile_preset").await
}

pub(crate) async fn risk_check(
    State(state): State<AppState>,
    AxumPath(name): AxumPath<String>,
    request: Request,
) -> Response {
    compile_or_check(state, name, request, "risk_check_preset").await
}

pub(crate) async fn activate(
    State(state): State<AppState>,
    AxumPath(name): AxumPath<String>,
) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let markdown = match resolve_preset_markdown(&workspace, &name, true) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let Ok(_guard) = PRESET_WRITE_LOCK.lock() else {
        return preset_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "preset_write_lock_failed",
            "Preset write lock is unavailable.",
        );
    };
    let active_dir = preset_root(&workspace).join("active");
    let receipt = if markdown.parent() == Some(active_dir.as_path()) {
        None
    } else {
        let destination = active_dir.join(
            markdown
                .file_name()
                .unwrap_or_else(|| std::ffi::OsStr::new("preset.md")),
        );
        match move_preset_pair(&markdown, &destination) {
            Ok(receipt) => Some(receipt),
            Err(response) => return response,
        }
    };
    let active_markdown = receipt.as_ref().map_or(markdown.as_path(), |receipt| {
        receipt.destination_markdown.as_path()
    });
    let mut pointer = match read_pointer(&workspace) {
        Ok(value) => value,
        Err(response) => {
            if let Some(receipt) = &receipt {
                let _ = rollback_move(receipt);
            }
            return response;
        }
    };
    pointer["activeMainPreset"] = match relative_path(&workspace, active_markdown) {
        Ok(value) => Value::String(value),
        Err(response) => return response,
    };
    if let Err(response) = write_pointer(&workspace, &pointer) {
        if let Some(receipt) = &receipt
            && let Err(rollback_error) = rollback_move(receipt)
        {
            return preset_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "preset_activation_rollback_failed",
                format!(
                    "Active pointer write failed and preset move rollback was incomplete: {rollback_error}"
                ),
            );
        }
        return response;
    }
    success(pointer, started, "activate_preset")
}

pub(crate) async fn deactivate(
    State(state): State<AppState>,
    AxumPath(name): AxumPath<String>,
) -> Response {
    let started = Instant::now();
    let workspace = match current_workspace(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let markdown = match resolve_preset_markdown(&workspace, &name, true) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let old_relative = match relative_path(&workspace, &markdown) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let Ok(_guard) = PRESET_WRITE_LOCK.lock() else {
        return preset_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "preset_write_lock_failed",
            "Preset write lock is unavailable.",
        );
    };
    let active_dir = preset_root(&workspace).join("active");
    let receipt = if markdown.parent() == Some(active_dir.as_path()) {
        let library_dir = preset_root(&workspace).join("library");
        let destination = library_dir.join(
            markdown
                .file_name()
                .unwrap_or_else(|| std::ffi::OsStr::new("preset.md")),
        );
        match move_preset_pair(&markdown, &destination) {
            Ok(receipt) => Some(receipt),
            Err(response) => return response,
        }
    } else {
        None
    };
    let mut pointer = match read_pointer(&workspace) {
        Ok(value) => value,
        Err(response) => {
            if let Some(receipt) = &receipt {
                let _ = rollback_move(receipt);
            }
            return response;
        }
    };
    if pointer.get("activeMainPreset").and_then(Value::as_str) == Some(old_relative.as_str()) {
        pointer["activeMainPreset"] = Value::String(String::new());
        if let Err(response) = write_pointer(&workspace, &pointer) {
            if let Some(receipt) = &receipt
                && let Err(rollback_error) = rollback_move(receipt)
            {
                return preset_error(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "preset_deactivation_rollback_failed",
                    format!(
                        "Active pointer write failed and preset move rollback was incomplete: {rollback_error}"
                    ),
                );
            }
            return response;
        }
    }
    success(pointer, started, "deactivate_preset")
}

pub(crate) async fn dispatch(
    State(state): State<AppState>,
    AxumPath(path): AxumPath<String>,
    request: Request,
) -> Response {
    let method = request.method().clone();
    let actions = [
        ("/risk-check", "risk-check"),
        ("/deactivate", "deactivate"),
        ("/document", "document"),
        ("/activate", "activate"),
        ("/compile", "compile"),
        ("/params", "params"),
    ];
    let Some((name, action)) = actions.iter().find_map(|(suffix, action)| {
        path.strip_suffix(suffix)
            .filter(|name| !name.trim_matches('/').is_empty())
            .map(|name| (name.trim_matches('/').to_owned(), *action))
    }) else {
        return preset_error(
            StatusCode::NOT_FOUND,
            "preset_route_not_found",
            "Preset route does not exist.",
        );
    };
    match (method, action) {
        (Method::GET, "document") => document(State(state), AxumPath(name)).await,
        (Method::PUT, "document") => save_document(State(state), AxumPath(name), request).await,
        (Method::POST, "compile") => compile(State(state), AxumPath(name), request).await,
        (Method::POST, "risk-check") => risk_check(State(state), AxumPath(name), request).await,
        (Method::PATCH, "params") => patch_params(State(state), AxumPath(name), request).await,
        (Method::POST, "activate") => activate(State(state), AxumPath(name)).await,
        (Method::POST, "deactivate") => deactivate(State(state), AxumPath(name)).await,
        _ => preset_error(
            StatusCode::METHOD_NOT_ALLOWED,
            "method_not_allowed",
            "Method not allowed for this preset route.",
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stable_sidecar_keys_round_trip_through_the_frontend_shape() {
        let stable = json!({
            "version": 1,
            "meta": {"name": "测试", "compatible_providers": [], "updated_at": ""},
            "sampling": {"default": {"top_p": 0.8}, "per_purpose": {}},
            "length_contract": {"body_min_chars": 100, "body_target_chars": 200, "body_max_chars": 300, "paragraph_min": 1, "paragraph_max": 3, "required_tags": [], "forbidden_tags": []},
            "thinking": {"enabled": false, "mode": "stage_list", "stages": [], "inject_position": "system_suffix", "visible_in_output": true},
            "style": {"pov": "", "narrator": "", "forbidden_words": [], "forbidden_patterns": [], "style_rules": [], "max_consecutive_repeat": 2, "free_text_slot_pre": "规则"},
            "memory": {"summary_format": "scene_outline", "summary_min_chars": 1, "summary_max_chars": 2, "big_summary_trigger_chapters": 3},
            "terms": {"name_alias_map": {}, "term_replace_map": {}, "enforce_at_generation": true},
            "character_voices": {},
        });
        let (frontend, _) = normalize_document(stable, true).expect("normalize");
        assert_eq!(frontend["sampling"]["default"]["topP"], 0.8);
        assert_eq!(frontend["thinking"]["visibleInOutput"], false);
        let disk = disk_document(frontend);
        assert_eq!(disk["sampling"]["default"]["top_p"], 0.8);
        assert_eq!(disk["length_contract"]["body_min_chars"], 100);
    }

    #[test]
    fn generic_and_silly_tavern_imports_keep_real_modules_and_order() {
        let generic = convert_import(br#"["first rule", "second rule"]"#, "array.json");
        assert_eq!(generic.modules.len(), 2);
        assert_eq!(
            compile_document(&generic.document, &json!({}))["compiledText"],
            "first rule\n\nsecond rule"
        );

        let source = json!({
            "prompts": [
                {"identifier": "one", "name": "One", "content": "first"},
                {"identifier": "two", "name": "Two", "content": "second"}
            ],
            "prompt_order": [{"character_id": 100001, "order": [
                {"identifier": "two", "enabled": true},
                {"identifier": "one", "enabled": false}
            ]}]
        });
        let imported = convert_import(source.to_string().as_bytes(), "st.json");
        assert_eq!(imported.modules[0].source_identifier, "two");
        assert!(imported.modules[0].enabled);
        assert!(!imported.modules[1].enabled);
        assert_eq!(
            compile_document(&imported.document, &json!({}))["compiledText"],
            "second"
        );
    }

    #[test]
    fn unrecognized_json_never_produces_an_empty_preset() {
        let imported = convert_import(br#"{"alpha":1,"beta":[1,2,3]}"#, "opaque.json");
        assert_eq!(imported.modules.len(), 1);
        assert_eq!(imported.modules[0].source_identifier, "raw_content");
        assert!(
            compile_document(&imported.document, &json!({}))["compiledText"]
                .as_str()
                .is_some_and(|text| text.contains("alpha"))
        );
    }
}
