//! Embedded help guide and prompt repository for the Rust desktop candidate.

#![allow(clippy::result_large_err)]

use crate::system::global_root;
use crate::workspace::atomic_write;
use crate::{AppState, error_response};
use axum::Json;
use axum::extract::{Path as AxumPath, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use chrono::Utc;
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Instant;
use uuid::Uuid;

static CUSTOM_PROMPT_LOCK: Mutex<()> = Mutex::new(());

const GUIDE_ASSETS: &[(&str, &str)] = &[
    (
        "00-使用指南.md",
        include_str!("../../../../../docs/guide/00-使用指南.md"),
    ),
    (
        "01-LLM配置.md",
        include_str!("../../../../../docs/guide/01-LLM配置.md"),
    ),
    (
        "02-目录结构与资源浏览器.md",
        include_str!("../../../../../docs/guide/02-目录结构与资源浏览器.md"),
    ),
    (
        "03-版本控制.md",
        include_str!("../../../../../docs/guide/03-版本控制.md"),
    ),
    (
        "04-预设管理.md",
        include_str!("../../../../../docs/guide/04-预设管理.md"),
    ),
    (
        "05-知识图谱.md",
        include_str!("../../../../../docs/guide/05-知识图谱.md"),
    ),
    (
        "06-系统设置.md",
        include_str!("../../../../../docs/guide/06-系统设置.md"),
    ),
    (
        "07-指令仓库.md",
        include_str!("../../../../../docs/guide/07-指令仓库.md"),
    ),
];

const PROMPT_ASSETS: &[(&str, &str)] = &[
    (
        "编辑审校/01-项目一致性检查.md",
        include_str!("../../../../../docs/prompts/编辑审校/01-项目一致性检查.md"),
    ),
    (
        "编辑审校/02-润色与改写.md",
        include_str!("../../../../../docs/prompts/编辑审校/02-润色与改写.md"),
    ),
    (
        "角色创作/01-创建新角色.md",
        include_str!("../../../../../docs/prompts/角色创作/01-创建新角色.md"),
    ),
    (
        "角色创作/02-设计角色关系网.md",
        include_str!("../../../../../docs/prompts/角色创作/02-设计角色关系网.md"),
    ),
    (
        "剧情设计/01-制定卷纲与章节大纲.md",
        include_str!("../../../../../docs/prompts/剧情设计/01-制定卷纲与章节大纲.md"),
    ),
    (
        "剧情设计/02-续写当前章节.md",
        include_str!("../../../../../docs/prompts/剧情设计/02-续写当前章节.md"),
    ),
    (
        "世界观/01-制定主题世界观.md",
        include_str!("../../../../../docs/prompts/世界观/01-制定主题世界观.md"),
    ),
    (
        "项目包装/01-生成小说简介.md",
        include_str!("../../../../../docs/prompts/项目包装/01-生成小说简介.md"),
    ),
    (
        "项目包装/02-生成封面AI绘图提示词.md",
        include_str!("../../../../../docs/prompts/项目包装/02-生成封面AI绘图提示词.md"),
    ),
    (
        "项目包装/03-生成书名与宣传语.md",
        include_str!("../../../../../docs/prompts/项目包装/03-生成书名与宣传语.md"),
    ),
];

#[derive(Debug, Default, Deserialize)]
pub(crate) struct PromptQuery {
    #[serde(default)]
    q: String,
    #[serde(default)]
    category: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct CustomPromptCreateRequest {
    title: String,
    prompt_text: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct CustomPromptUpdateRequest {
    prompt_text: String,
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

fn help_error(status: StatusCode, code: &str, message: impl AsRef<str>) -> Response {
    error_response(status, code, message.as_ref())
}

fn title_from_markdown(content: &str, fallback: &str) -> String {
    content
        .lines()
        .map(str::trim)
        .find_map(|line| {
            line.strip_prefix("# ")
                .map(str::trim)
                .filter(|value| !value.is_empty())
        })
        .unwrap_or(fallback)
        .to_owned()
}

fn summary_from_markdown(content: &str) -> String {
    content
        .lines()
        .map(str::trim)
        .find_map(|line| {
            line.strip_prefix('>')
                .map(str::trim)
                .filter(|value| !value.is_empty())
        })
        .unwrap_or_default()
        .to_owned()
}

fn prompt_text_from_markdown(content: &str) -> String {
    let mut collecting = false;
    let mut lines = Vec::new();
    for line in content.lines() {
        let trimmed = line.trim();
        if !collecting
            && (trimmed.eq_ignore_ascii_case("```prompt")
                || trimmed.eq_ignore_ascii_case("```text"))
        {
            collecting = true;
            continue;
        }
        if collecting && trimmed == "```" {
            return lines.join("\n").trim().to_owned();
        }
        if collecting {
            lines.push(line);
        }
    }
    content.trim().to_owned()
}

fn placeholders(prompt_text: &str) -> Vec<String> {
    let mut result = Vec::new();
    let mut offset = 0;
    while let Some(start) = prompt_text[offset..].find('[') {
        let absolute_start = offset + start;
        let Some(end) = prompt_text[absolute_start + 1..].find(']') else {
            break;
        };
        let absolute_end = absolute_start + 1 + end;
        let candidate = &prompt_text[absolute_start..=absolute_end];
        if (3..=42).contains(&candidate.len())
            && !candidate.contains('\n')
            && !candidate.contains('\r')
            && !result.iter().any(|value| value == candidate)
        {
            result.push(candidate.to_owned());
        }
        offset = absolute_end + 1;
    }
    result
}

fn guide_items() -> Vec<Value> {
    GUIDE_ASSETS
        .iter()
        .map(|(relative, content)| {
            let id = relative.trim_end_matches(".md");
            json!({
                "id": id,
                "title": title_from_markdown(content, id),
                "relativePath": relative,
                "content": content,
                "updatedAt": "",
            })
        })
        .collect()
}

fn embedded_prompt_items() -> Vec<Value> {
    PROMPT_ASSETS
        .iter()
        .map(|(relative, content)| {
            let id = relative.trim_end_matches(".md");
            let category = relative.split('/').next().unwrap_or("通用");
            let fallback = Path::new(relative)
                .file_stem()
                .and_then(|value| value.to_str())
                .unwrap_or(id);
            let prompt_text = prompt_text_from_markdown(content);
            json!({
                "id": id,
                "title": title_from_markdown(content, fallback),
                "summary": summary_from_markdown(content),
                "category": category,
                "relativePath": relative,
                "content": content,
                "promptText": prompt_text,
                "placeholders": placeholders(&prompt_text),
                "updatedAt": "",
                "isCustom": false,
            })
        })
        .collect()
}

fn custom_prompts_path(state: &AppState) -> PathBuf {
    global_root(state).join("prompts").join("custom.json")
}

fn read_custom_payload(state: &AppState) -> Result<Value, Response> {
    let path = custom_prompts_path(state);
    if !path.exists() {
        return Ok(json!({"version": 1, "items": []}));
    }
    let content = fs::read_to_string(&path).map_err(|error| {
        help_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "custom_prompt_read_failed",
            format!("Unable to read the custom prompt repository: {error}"),
        )
    })?;
    let payload = serde_json::from_str::<Value>(&content).map_err(|error| {
        help_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "custom_prompt_repository_invalid",
            format!("The custom prompt repository is invalid JSON: {error}"),
        )
    })?;
    if !payload.is_object() {
        return Err(help_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "custom_prompt_repository_invalid",
            "The custom prompt repository must be a JSON object.",
        ));
    }
    Ok(payload)
}

fn write_custom_payload(state: &AppState, payload: &Value) -> Result<(), Response> {
    let mut bytes = serde_json::to_vec_pretty(payload).map_err(|error| {
        help_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "custom_prompt_encode_failed",
            format!("Unable to encode the custom prompt repository: {error}"),
        )
    })?;
    bytes.push(b'\n');
    atomic_write(&custom_prompts_path(state), &bytes).map_err(|error| {
        help_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "custom_prompt_write_failed",
            format!("Unable to persist the custom prompt repository: {error}"),
        )
    })
}

fn validate_title(raw: &str) -> Result<String, Response> {
    let value = raw.trim();
    if value.is_empty()
        || value.chars().count() > 120
        || value.contains('\n')
        || value.contains('\r')
    {
        return Err(help_error(
            StatusCode::BAD_REQUEST,
            "custom_prompt_title_invalid",
            "Custom prompt title must contain 1 to 120 characters on one line.",
        ));
    }
    Ok(value.to_owned())
}

fn validate_prompt_text(raw: &str) -> Result<String, Response> {
    let value = raw.trim();
    if value.is_empty() || value.chars().count() > 12_000 {
        return Err(help_error(
            StatusCode::BAD_REQUEST,
            "custom_prompt_body_invalid",
            "Custom prompt body must contain 1 to 12000 characters.",
        ));
    }
    Ok(value.to_owned())
}

fn valid_custom_prompt_id(value: &str) -> bool {
    value.len() == 32
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn custom_item(record: &Value) -> Option<Value> {
    let id = record.get("id")?.as_str()?.trim();
    let title = record.get("title")?.as_str()?.trim();
    let prompt_text = record.get("promptText")?.as_str()?.trim();
    if !valid_custom_prompt_id(id) || title.is_empty() || prompt_text.is_empty() {
        return None;
    }
    Some(json!({
        "id": format!("custom/{id}"),
        "title": title,
        "summary": "用户自定义的可复用指令。",
        "category": "自定义",
        "relativePath": "",
        "content": prompt_text,
        "promptText": prompt_text,
        "placeholders": placeholders(prompt_text),
        "updatedAt": record.get("updatedAt").and_then(Value::as_str).unwrap_or_default(),
        "isCustom": true,
    }))
}

fn custom_items(state: &AppState) -> Result<Vec<Value>, Response> {
    Ok(read_custom_payload(state)?
        .get("items")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(custom_item)
        .collect())
}

pub(crate) async fn guide() -> Response {
    let started = Instant::now();
    let items = guide_items();
    let mut combined = String::from("# 使用指南\n");
    for item in &items {
        let title = item.get("title").and_then(Value::as_str).unwrap_or("指南");
        let content = item
            .get("content")
            .and_then(Value::as_str)
            .unwrap_or_default();
        combined.push_str(&format!("\n\n## {title}\n\n{}", content.trim()));
    }
    combined.push('\n');
    success(
        json!({
            "root": "embedded://storydex/guide",
            "items": items,
            "content": combined,
        }),
        started,
        "read_help_guide",
    )
}

pub(crate) async fn prompts(
    State(state): State<AppState>,
    Query(query): Query<PromptQuery>,
) -> Response {
    let started = Instant::now();
    let mut all_items = embedded_prompt_items();
    match custom_items(&state) {
        Ok(items) => all_items.extend(items),
        Err(response) => return response,
    }
    let normalized_query = query.q.trim().to_lowercase();
    let normalized_category = query.category.trim();
    let mut counts = BTreeMap::<String, usize>::new();
    for item in &all_items {
        let category = item
            .get("category")
            .and_then(Value::as_str)
            .unwrap_or("通用")
            .to_owned();
        *counts.entry(category).or_default() += 1;
    }
    counts.entry("自定义".to_owned()).or_default();
    let categories = counts
        .into_iter()
        .map(|(name, count)| json!({"id": name, "label": name, "count": count}))
        .collect::<Vec<_>>();
    let items = all_items
        .into_iter()
        .filter(|item| {
            let category_matches = normalized_category.is_empty()
                || item.get("category").and_then(Value::as_str) == Some(normalized_category);
            if !category_matches || normalized_query.is_empty() {
                return category_matches;
            }
            ["title", "summary", "category", "content"]
                .iter()
                .filter_map(|key| item.get(key).and_then(Value::as_str))
                .any(|value| value.to_lowercase().contains(&normalized_query))
        })
        .collect::<Vec<_>>();
    success(
        json!({
            "root": "embedded://storydex/prompts",
            "query": query.q.trim(),
            "category": normalized_category,
            "categories": categories,
            "items": items,
        }),
        started,
        "read_prompt_repository",
    )
}

pub(crate) async fn create_custom_prompt(
    State(state): State<AppState>,
    Json(request): Json<CustomPromptCreateRequest>,
) -> Response {
    let started = Instant::now();
    let title = match validate_title(&request.title) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let prompt_text = match validate_prompt_text(&request.prompt_text) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let Ok(_guard) = CUSTOM_PROMPT_LOCK.lock() else {
        return help_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "custom_prompt_lock_failed",
            "The custom prompt repository lock is unavailable.",
        );
    };
    let mut payload = match read_custom_payload(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let now = Utc::now().to_rfc3339();
    let record = json!({
        "id": Uuid::new_v4().simple().to_string(),
        "title": title,
        "promptText": prompt_text,
        "createdAt": now,
        "updatedAt": now,
    });
    let object = payload.as_object_mut().expect("payload object validated");
    let items = object.entry("items").or_insert_with(|| json!([]));
    if !items.is_array() {
        return help_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "custom_prompt_repository_invalid",
            "The custom prompt repository items field must be an array.",
        );
    }
    items
        .as_array_mut()
        .expect("items array")
        .push(record.clone());
    object.insert("version".to_owned(), json!(1));
    if let Err(response) = write_custom_payload(&state, &payload) {
        return response;
    }
    success(
        json!({"item": custom_item(&record).expect("new record is valid")}),
        started,
        "create_custom_prompt",
    )
}

pub(crate) async fn update_custom_prompt(
    State(state): State<AppState>,
    AxumPath(prompt_id): AxumPath<String>,
    Json(request): Json<CustomPromptUpdateRequest>,
) -> Response {
    let started = Instant::now();
    let id = prompt_id.trim();
    if !valid_custom_prompt_id(id) {
        return help_error(
            StatusCode::BAD_REQUEST,
            "custom_prompt_id_invalid",
            "Invalid custom prompt id.",
        );
    }
    let prompt_text = match validate_prompt_text(&request.prompt_text) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let Ok(_guard) = CUSTOM_PROMPT_LOCK.lock() else {
        return help_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "custom_prompt_lock_failed",
            "The custom prompt repository lock is unavailable.",
        );
    };
    let mut payload = match read_custom_payload(&state) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let Some(items) = payload.get_mut("items").and_then(Value::as_array_mut) else {
        return help_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "custom_prompt_repository_invalid",
            "The custom prompt repository items field must be an array.",
        );
    };
    let mut updated = None;
    for item in items {
        if item.get("id").and_then(Value::as_str) != Some(id) {
            continue;
        }
        let Some(object) = item.as_object_mut() else {
            continue;
        };
        object.insert("promptText".to_owned(), json!(prompt_text));
        object.insert("updatedAt".to_owned(), json!(Utc::now().to_rfc3339()));
        updated = Some(item.clone());
        break;
    }
    let Some(updated) = updated else {
        return help_error(
            StatusCode::NOT_FOUND,
            "custom_prompt_not_found",
            "Custom prompt does not exist.",
        );
    };
    if let Err(response) = write_custom_payload(&state, &payload) {
        return response;
    }
    success(
        json!({"item": custom_item(&updated).expect("updated record is valid")}),
        started,
        "update_custom_prompt",
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    #[test]
    fn embedded_prompt_assets_keep_categories_and_placeholders() {
        let items = embedded_prompt_items();
        assert!(items.len() >= 10);
        assert!(items.iter().any(|item| item["category"] == "项目包装"));
        let categories = items
            .iter()
            .filter_map(|item| item.get("category").and_then(Value::as_str))
            .collect::<BTreeSet<_>>();
        assert!(categories.len() >= 5);
    }

    #[test]
    fn custom_prompt_validation_rejects_empty_and_multiline_titles() {
        assert!(validate_title("").is_err());
        assert!(validate_title("bad\ntitle").is_err());
        assert!(validate_prompt_text(" ").is_err());
    }

    #[test]
    fn custom_prompt_ids_match_the_stable_disk_format() {
        let id = Uuid::new_v4().simple().to_string();
        assert!(valid_custom_prompt_id(&id));
        assert!(!valid_custom_prompt_id(&Uuid::new_v4().to_string()));
        assert!(!valid_custom_prompt_id(&id.to_uppercase()));
    }
}
