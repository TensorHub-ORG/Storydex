use crate::AppState;
use crate::chat::ChatStreamRequest;
use crate::chat::ProviderIdentity;
use crate::chat::StoryGenerationOptions;
use crate::chat::with_event_identity;
use crate::length_tier_calibration::{
    CalibrationSampleInput, ShortCalibrationSummary, read_tier_summary, record_tier_sample,
};
use anyhow::{Context, Result, bail, ensure};
use serde_json::{Value, json};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Instant;
use tokio::io::AsyncBufReadExt;
use tokio::io::AsyncWriteExt;
use tokio::io::BufReader;
use tokio::process::Command;
use tokio::sync::mpsc;
use uuid::Uuid;

const SHORT_HARD_MINIMUM: usize = 700;
const SHORT_RUNTIME_MAXIMUM: usize = 4_000;
const MEDIUM_HARD_MINIMUM: usize = 1_800;
const MEDIUM_RUNTIME_MAXIMUM: usize = 7_200;
const LONG_HARD_MINIMUM: usize = 2_500;
const LONG_RUNTIME_MAXIMUM: usize = 9_000;
const STORY_COUNTING_RULE: &str = "count every non-whitespace Unicode character in the prose itself; summary/details/thinking wrapper blocks are excluded";

pub(crate) struct StoryGenerationOutcome {
    pub(crate) terminal_event: String,
    pub(crate) reply: String,
}

#[allow(clippy::too_many_arguments)]
pub(crate) async fn run_create_new(
    state: &AppState,
    payload: &ChatStreamRequest,
    options: &StoryGenerationOptions,
    workspace: &Path,
    trace_id: &str,
    session_id: &str,
    identity: &ProviderIdentity,
    cancellation: &crate::execution::ExecutionCancellation,
    sender: &mpsc::Sender<String>,
    trace_events: &mut Vec<(String, Value)>,
) -> Result<StoryGenerationOutcome> {
    ensure!(
        options.fragment_count == 1,
        "Rust story slice requires one fragment"
    );
    let tier = options.chapter_length_tier.as_str();
    let (hard_minimum, runtime_safety_maximum) = tier_bounds(tier)?;
    ensure!(
        payload.writes_allowed
            && payload
                .core_writes_allowed
                .unwrap_or(payload.writes_allowed)
            && payload.capability_mode != "read_only",
        "Rust story generation requires an explicit write capability"
    );

    let target = plan_create_new_target(workspace, &options.chapter_template_id)?;
    let calibration = read_tier_summary(workspace, &identity.id, &identity.model, tier)?;
    let mut provider_request = json!({
        "action": "complete",
        "home": state.coomi_home(),
        "provider": identity.id,
        "messages": [
            {
                "role": "system",
                "content": format!(
                    "Generate only the publishable Storydex chapter prose. Do not emit Markdown fences, XML/content wrappers, metadata, summaries, or commentary.\n\n{}",
                    tier_prompt(tier)
                )
            },
            {"role": "user", "content": payload.prompt}
        ],
        "maxOutputTokens": runtime_safety_maximum as u64,
        "reasoningEffort": payload.reasoning_effort,
        "tools": []
    });
    if let Some(path) = state.replay_fixture() {
        provider_request["providerReplayFixture"] = Value::String(path.display().to_string());
    }

    emit(
        sender,
        trace_events,
        "AgentStarted",
        json!({
            "mode": "coomi-rust-story-generation",
            "query": payload.prompt,
            "llmModel": identity.model,
            "llmProvider": identity.id,
            "providerMode": if state.replay_fixture().is_some() { "replay" } else { "live" },
            "coomiStatus": {
                "runtime": "storydex-coomi-rs",
                "installed": true,
                "providerId": identity.id,
                "model": identity.model,
                "display": identity.display,
            }
        }),
        trace_id,
        session_id,
    )
    .await?;

    let provider_started = Instant::now();
    let completion = match complete_once(state, provider_request, cancellation).await {
        Ok(value) => {
            emit_story_provider_attempt(sender, trace_events, trace_id, session_id, "success", "")
                .await?;
            value
        }
        Err(_error) if cancellation.is_cancelled() => {
            let reason = cancellation.reason();
            emit(
                sender,
                trace_events,
                "AgentCancelled",
                json!({"reason": reason}),
                trace_id,
                session_id,
            )
            .await?;
            return Ok(StoryGenerationOutcome {
                terminal_event: "AgentCancelled".to_owned(),
                reply: String::new(),
            });
        }
        Err(error) => {
            emit_story_provider_attempt(
                sender,
                trace_events,
                trace_id,
                session_id,
                "error",
                "story_generation_provider_error",
            )
            .await?;
            emit_story_accounting(
                sender,
                trace_events,
                trace_id,
                session_id,
                tier,
                provider_mode(state),
                1,
            )
            .await?;
            emit(
                sender,
                trace_events,
                "AgentError",
                json!({
                    "error_type": "story_generation_provider_error",
                    "code": "story_generation_provider_error",
                    "message": format!("Story generation Provider completion failed: {error:#}"),
                    "providerMode": provider_mode(state),
                }),
                trace_id,
                session_id,
            )
            .await?;
            return Ok(StoryGenerationOutcome {
                terminal_event: "AgentError".to_owned(),
                reply: String::new(),
            });
        }
    };
    let draft_duration_ms = provider_started.elapsed().as_millis().min(u64::MAX as u128) as u64;
    let content = completion
        .get("content")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    let provider_mode = completion
        .get("providerMode")
        .and_then(Value::as_str)
        .unwrap_or_else(|| provider_mode(state));
    let completion_tokens = completion
        .get("usage")
        .and_then(Value::as_object)
        .and_then(|usage| usage.get("output_tokens"))
        .and_then(Value::as_u64);
    let mut validation = validate_candidate(
        &content,
        &target,
        provider_mode,
        tier,
        hard_minimum,
        runtime_safety_maximum,
        &calibration,
    );
    if !validation
        .get("passed")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        emit_story_draft_measured(
            sender,
            trace_events,
            trace_id,
            session_id,
            visible_character_count(&content),
            completion_tokens,
            tier,
            &calibration,
        )
        .await?;
        emit(
            sender,
            trace_events,
            "StoryGenerationValidation",
            validation.clone(),
            trace_id,
            session_id,
        )
        .await?;
        emit_story_accounting(
            sender,
            trace_events,
            trace_id,
            session_id,
            tier,
            provider_mode,
            1,
        )
        .await?;
        emit(
            sender,
            trace_events,
            "AgentError",
            json!({
                "error_type": "story_generation_validation_failed",
                "code": "story_generation_validation_failed",
                "message": validation.get("message").cloned().unwrap_or_else(|| json!("Story generation candidate failed validation.")),
                "providerMode": provider_mode,
                "writtenPaths": [],
            }),
            trace_id,
            session_id,
        )
        .await?;
        return Ok(StoryGenerationOutcome {
            terminal_event: "AgentError".to_owned(),
            reply: String::new(),
        });
    }

    emit(
        sender,
        trace_events,
        "StoryCommitStarted",
        json!({}),
        trace_id,
        session_id,
    )
    .await?;
    let write_result = atomic_create(&target, serialized_story_content(&content));
    emit(
        sender,
        trace_events,
        "StoryCommitFinished",
        json!({}),
        trace_id,
        session_id,
    )
    .await?;
    if let Err(error) = write_result {
        if let Some(object) = validation.as_object_mut() {
            object.insert("passed".into(), Value::Bool(false));
            object.insert("status".into(), json!("error"));
            object.insert("writeToolApplied".into(), Value::Bool(false));
            object.insert(
                "message".into(),
                json!(format!("原子写入失败，未完成章节生成：{error:#}")),
            );
        }
        emit(
            sender,
            trace_events,
            "StoryGenerationValidation",
            validation,
            trace_id,
            session_id,
        )
        .await?;
        emit_story_accounting(
            sender,
            trace_events,
            trace_id,
            session_id,
            tier,
            provider_mode,
            1,
        )
        .await?;
        emit(
            sender,
            trace_events,
            "AgentError",
            json!({
                "error_type": "story_generation_write_failed",
                "code": "story_generation_write_failed",
                "message": format!("Story generation atomic write failed: {error:#}"),
                "providerMode": provider_mode,
                "writtenPaths": [],
            }),
            trace_id,
            session_id,
        )
        .await?;
        return Ok(StoryGenerationOutcome {
            terminal_event: "AgentError".to_owned(),
            reply: String::new(),
        });
    }
    let relative_target = target
        .strip_prefix(workspace)
        .unwrap_or(&target)
        .to_string_lossy()
        .replace('\\', "/");
    if let Some(object) = validation.as_object_mut() {
        object.insert("writeToolApplied".into(), Value::Bool(true));
        object.insert("writtenPaths".into(), json!([relative_target.clone()]));
        object.insert(
            "finalWordCount".into(),
            json!(visible_character_count(&content)),
        );
    }
    let actual_word_count = visible_character_count(&content);
    let tier_hit = (calibration.preferred_minimum..=calibration.preferred_maximum)
        .contains(&actual_word_count);
    emit_story_draft_measured(
        sender,
        trace_events,
        trace_id,
        session_id,
        actual_word_count,
        completion_tokens,
        tier,
        &calibration,
    )
    .await?;
    record_tier_sample(
        workspace,
        tier,
        CalibrationSampleInput {
            provider: &identity.id,
            model: &identity.model,
            actual_word_count,
            tier_hit,
            structure_passed: true,
            machine_quality_passed: true,
            logical_prose_calls: 1,
            completion_tokens,
            duration_ms: Some(draft_duration_ms),
            trace_id,
        },
    )?;
    emit(
        sender,
        trace_events,
        "StoryGenerationValidation",
        validation,
        trace_id,
        session_id,
    )
    .await?;
    emit_story_accounting(
        sender,
        trace_events,
        trace_id,
        session_id,
        tier,
        provider_mode,
        1,
    )
    .await?;
    emit(
        sender,
        trace_events,
        "TextChunk",
        json!({
            "content": completion_reply(&relative_target, actual_word_count, tier, tier_hit),
            "path": relative_target.clone(),
            "providerMode": provider_mode,
        }),
        trace_id,
        session_id,
    )
    .await?;
    emit(
        sender,
        trace_events,
        "AgentCompleted",
        json!({
            "providerMode": provider_mode,
            "writtenPaths": [relative_target.clone()],
            "fragmentCount": 1,
            "chapterLengthTier": tier,
            "wordCountScope": "candidate",
            "actualWordCount": actual_word_count,
            "generatedWordCount": actual_word_count,
            "retainedWordCount": 0,
            "resultingWordCount": actual_word_count,
            "finalWordCount": actual_word_count,
            "tierHit": tier_hit,
        }),
        trace_id,
        session_id,
    )
    .await?;
    Ok(StoryGenerationOutcome {
        terminal_event: "AgentCompleted".to_owned(),
        reply: completion_reply(&relative_target, actual_word_count, tier, tier_hit),
    })
}

fn provider_mode(state: &AppState) -> &'static str {
    if state.replay_fixture().is_some() {
        "replay"
    } else {
        "live"
    }
}

async fn complete_once(
    state: &AppState,
    request: Value,
    cancellation: &crate::execution::ExecutionCancellation,
) -> Result<Value> {
    let mut child = Command::new(state.bridge_path())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .with_context(|| {
            format!(
                "unable to start storydex-coomi-bridge {}",
                state.bridge_path().display()
            )
        })?;
    let mut stdin = child
        .stdin
        .take()
        .context("storydex bridge stdin unavailable")?;
    let stdout = child
        .stdout
        .take()
        .context("storydex bridge stdout unavailable")?;
    let mut line = serde_json::to_vec(&request)?;
    line.push(b'\n');
    stdin.write_all(&line).await?;
    stdin.flush().await?;
    drop(stdin);
    let mut lines = BufReader::new(stdout).lines();
    let mut completion = None;
    while let Some(raw) = tokio::select! {
        value = lines.next_line() => value?,
        _ = cancellation.cancelled() => {
            let _ = child.kill().await;
            return bail_cancelled();
        }
    } {
        let packet: Value = serde_json::from_str(&raw).context("invalid completion bridge JSON")?;
        match packet
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default()
        {
            "reasoning_plan" => {}
            "completion" => {
                let data = packet.get("data").cloned().unwrap_or_else(|| json!({}));
                completion = Some(data);
            }
            "error" => {
                let data = packet.get("data").cloned().unwrap_or_else(|| json!({}));
                bail!(
                    "{}",
                    data.get("message")
                        .and_then(Value::as_str)
                        .unwrap_or("completion bridge error")
                );
            }
            _ => {}
        }
    }
    let status = child.wait().await?;
    ensure!(status.success(), "completion bridge exited with {status}");
    completion.context("completion bridge ended without a completion packet")
}

fn bail_cancelled<T>() -> Result<T> {
    bail!("story generation cancelled")
}

async fn emit_story_provider_attempt(
    sender: &mpsc::Sender<String>,
    trace_events: &mut Vec<(String, Value)>,
    trace_id: &str,
    session_id: &str,
    outcome: &str,
    error_type: &str,
) -> Result<()> {
    emit(
        sender,
        trace_events,
        "StoryProviderAttempt",
        json!({
            "purpose": "story_initial_generation",
            "attempt": 1,
            "outcome": outcome,
            "statusCode": null,
            "errorType": error_type,
            "retryScheduled": false,
            "retryDelaySeconds": 0,
        }),
        trace_id,
        session_id,
    )
    .await
}

#[allow(clippy::too_many_arguments)]
async fn emit_story_draft_measured(
    sender: &mpsc::Sender<String>,
    trace_events: &mut Vec<(String, Value)>,
    trace_id: &str,
    session_id: &str,
    word_count: usize,
    completion_tokens: Option<u64>,
    tier: &str,
    calibration: &ShortCalibrationSummary,
) -> Result<()> {
    let tier_hit =
        (calibration.preferred_minimum..=calibration.preferred_maximum).contains(&word_count);
    emit(
        sender,
        trace_events,
        "StoryDraftMeasured",
        json!({
            "initialWordCount": word_count,
            "retainedWordCount": 0,
            "generatedWordCount": word_count,
            "completionTokens": completion_tokens,
            "capApplied": false,
            "wordCountScope": "candidate",
            "actualWordCount": word_count,
            "resultingWordCount": word_count,
            "chapterLengthTier": tier,
            "tierHit": tier_hit,
            "tierDeviation": tier_deviation(word_count, calibration),
            "machineQualityPassed": true,
            "calibrationStatus": calibration.status,
        }),
        trace_id,
        session_id,
    )
    .await
}

async fn emit_story_accounting(
    sender: &mpsc::Sender<String>,
    trace_events: &mut Vec<(String, Value)>,
    trace_id: &str,
    session_id: &str,
    tier: &str,
    provider_mode: &str,
    provider_attempts: u64,
) -> Result<()> {
    emit(
        sender,
        trace_events,
        "StoryCallAccounting",
        json!({
            "_type": "StoryCallAccounting",
            "_version": 1,
            "chapterLengthTier": tier,
            "preciseWordCountEnabled": false,
            "asymmetricLengthEnabled": false,
            "logicalStoryCalls": 1,
            "providerAttempts": provider_attempts,
            "transportRetries": 0,
            "initialGenerationCalls": 1,
            "lengthRevisionCalls": 0,
            "secondDraftCalls": 0,
            "nonProseCalls": {},
            "contractViolations": [],
            "providerMode": provider_mode,
        }),
        trace_id,
        session_id,
    )
    .await
}

async fn emit(
    sender: &mpsc::Sender<String>,
    trace_events: &mut Vec<(String, Value)>,
    name: &str,
    data: Value,
    trace_id: &str,
    session_id: &str,
) -> Result<()> {
    let payload = with_event_identity(name, data, trace_id, session_id);
    ensure!(
        crate::chat::send_event_value(sender, name, payload.clone()).await,
        "story generation SSE sender closed"
    );
    trace_events.push((name.to_owned(), payload));
    Ok(())
}

pub(crate) fn plan_create_new_target(workspace: &Path, template_id: &str) -> Result<PathBuf> {
    let root = workspace
        .canonicalize()
        .context("story workspace is unavailable")?;
    let chapters_path = root.join("chapters");
    fs::create_dir_all(&chapters_path)?;
    let chapters = chapters_path
        .canonicalize()
        .context("story chapters directory is unavailable")?;
    ensure!(
        chapters.starts_with(&root),
        "story chapters directory escaped workspace"
    );
    let mut next_number = 1u64;
    for entry in fs::read_dir(&chapters)? {
        let entry = entry?;
        if !entry.path().is_dir() {
            continue;
        }
        let name = entry.file_name().to_string_lossy().to_string();
        if let Some(number) = name
            .strip_prefix('第')
            .and_then(|value| value.split('章').next())
            && let Ok(number) = number.parse::<u64>()
        {
            next_number = next_number.max(number.saturating_add(1));
        }
    }
    let segment = if template_id.contains("single_file") {
        "正文.md"
    } else {
        "001.md"
    };
    let target = chapters
        .join(format!("第{next_number}章 未命名"))
        .join(segment);
    ensure!(
        target.starts_with(&root),
        "planned story path escaped workspace"
    );
    Ok(target)
}

fn validate_candidate(
    content: &str,
    target: &Path,
    provider_mode: &str,
    tier: &str,
    hard_minimum: usize,
    runtime_safety_maximum: usize,
    calibration: &ShortCalibrationSummary,
) -> Value {
    let count = visible_character_count(content);
    let quality_issues = [
        (content.trim().is_empty(), "empty_prose"),
        (content.contains('\0'), "nul_character"),
        (
            content.contains("```") || content.contains("<content>"),
            "wrapper_block",
        ),
    ]
    .into_iter()
    .filter_map(|(failed, issue)| failed.then_some(issue))
    .collect::<Vec<_>>();
    let passed = target.extension().is_some()
        && (hard_minimum..=runtime_safety_maximum).contains(&count)
        && quality_issues.is_empty();
    let tier_hit = (calibration.preferred_minimum..=calibration.preferred_maximum).contains(&count);
    json!({
        "_type": "StoryGenerationValidation",
        "_version": 1,
        "applicable": true,
        "passed": passed,
        "status": if passed && tier_hit { "success" } else if passed { "warning" } else { "error" },
        "algorithm": "storydex_visible_characters_v1",
        "countingRule": STORY_COUNTING_RULE,
        "exact": false,
        "fragmentCount": 1,
        "actualWordCount": count,
        "generatedWordCount": count,
        "retainedWordCount": 0,
        "resultingWordCount": count,
        "chapterLengthTier": tier,
        "tierHit": tier_hit,
        "tierDeviation": tier_deviation(count, calibration),
        "wordCountScope": "candidate",
        "preferredMinimum": calibration.preferred_minimum,
        "preferredMaximum": calibration.preferred_maximum,
        "hardMinimum": hard_minimum,
        "hardMinimumPassed": count >= hard_minimum,
        "runtimeSafetyMaximum": runtime_safety_maximum,
        "runtimeSafetyExceeded": count > runtime_safety_maximum,
        "structurePassed": true,
        "qualityPassed": quality_issues.is_empty(),
        "machineQualityPassed": quality_issues.is_empty(),
        "qualityIssues": quality_issues,
        "providerCalls": 1,
        "contractViolations": [],
        "initialWordCount": count,
        "finalWordCount": 0,
        "normalBandPassed": tier_hit,
        "precisionAchieved": null,
        "providerMode": provider_mode,
        "targetPath": target.to_string_lossy(),
        "writeToolApplied": false,
        "writtenPaths": [],
        "message": if passed { "正文数量、章节结构和质量门禁均已通过。" } else { "正文未通过章节结构、质量或字数门禁，未写入项目文件。" },
    })
}

fn tier_bounds(tier: &str) -> Result<(usize, usize)> {
    match tier {
        "short" => Ok((SHORT_HARD_MINIMUM, SHORT_RUNTIME_MAXIMUM)),
        "medium" => Ok((MEDIUM_HARD_MINIMUM, MEDIUM_RUNTIME_MAXIMUM)),
        "long" => Ok((LONG_HARD_MINIMUM, LONG_RUNTIME_MAXIMUM)),
        _ => bail!("unsupported story length tier {tier}"),
    }
}

fn visible_character_count(content: &str) -> usize {
    content
        .chars()
        .filter(|character| !character.is_whitespace())
        .count()
}

fn tier_deviation(count: usize, calibration: &ShortCalibrationSummary) -> &'static str {
    if count < calibration.preferred_minimum {
        "below_preferred"
    } else if count > calibration.preferred_maximum {
        "above_preferred"
    } else {
        "in_preferred"
    }
}

fn serialized_story_content(content: &str) -> String {
    let normalized = content.replace("\r\n", "\n").replace('\r', "\n");
    let normalized = normalized.trim_end_matches('\n');
    if cfg!(windows) {
        normalized.replace('\n', "\r\n") + "\r\n"
    } else {
        normalized.to_owned() + "\n"
    }
}

fn completion_reply(
    relative_target: &str,
    word_count: usize,
    tier: &str,
    tier_hit: bool,
) -> String {
    let chapter_path = Path::new(relative_target)
        .parent()
        .map(|path| path.to_string_lossy().replace('\\', "/"))
        .unwrap_or_default();
    format!(
        "章节已写入 {chapter_path}，本次续写 {word_count} 字，{}档{}。",
        tier_label(tier),
        if tier_hit {
            "已命中"
        } else {
            "未命中，正文按原稿保留"
        }
    )
}

fn tier_label(tier: &str) -> &'static str {
    match tier {
        "short" => "短",
        "medium" => "中",
        "long" => "长",
        _ => "未知",
    }
}

fn tier_prompt(tier: &str) -> &'static str {
    match tier {
        "short" => {
            "篇幅档位为短。用短章规模完成本章核心推进，保持剧情完整并自然收束；既定事件、人物事实和叙事节奏优先。"
        }
        "medium" => {
            "篇幅档位为中。用常规章节规模完整展开本章主要推进，按剧情需要自然组织场景；既定事件、人物事实和叙事节奏优先。"
        }
        "long" => {
            "篇幅档位为长。用长章规模充分展开本章重要推进，按剧情需要容纳多个场景或线索；既定事件、人物事实和叙事节奏优先。"
        }
        _ => "",
    }
}

fn atomic_create(path: &Path, content: String) -> Result<()> {
    ensure!(!path.exists(), "create_new story target already exists");
    fs::create_dir_all(path.parent().context("story target has no parent")?)?;
    let temporary = path.with_file_name(format!(
        ".{}.storydex-tmp-{}",
        path.file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("chapter"),
        Uuid::new_v4()
    ));
    let result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        file.write_all(content.as_bytes())?;
        file.flush()?;
        file.sync_all()?;
        fs::rename(&temporary, path)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn cold_start_calibration() -> ShortCalibrationSummary {
        ShortCalibrationSummary {
            status: "cold_start".to_owned(),
            preferred_minimum: 1_000,
            preferred_maximum: 3_000,
        }
    }

    fn story_request(tier: &str) -> (ChatStreamRequest, StoryGenerationOptions) {
        let payload = serde_json::from_value(json!({
            "prompt": format!("generate a {tier} chapter"),
            "reasoningEffort": "low",
            "capabilityMode": "scoped_write",
            "writesAllowed": true,
            "coreWritesAllowed": true,
            "allowedWriteRoots": ["chapters/"],
        }))
        .expect("story request");
        let options = StoryGenerationOptions {
            fragment_count: 1,
            chapter_length_tier: tier.to_owned(),
            chapter_template_id: "default_chapter_directory".to_owned(),
        };
        (payload, options)
    }

    fn provider_identity() -> ProviderIdentity {
        ProviderIdentity {
            id: "OPENCODE".to_owned(),
            model: "deepseek-v4-flash".to_owned(),
            display: "OpenCode".to_owned(),
        }
    }

    #[test]
    fn short_validation_counts_non_whitespace_unicode_characters() {
        let directory = tempdir().expect("tempdir");
        let target = directory
            .path()
            .join("chapters")
            .join("第1章 未命名")
            .join("001.md");
        let prose = "界".repeat(SHORT_HARD_MINIMUM);
        let validation = validate_candidate(
            &prose,
            &target,
            "replay",
            "short",
            SHORT_HARD_MINIMUM,
            SHORT_RUNTIME_MAXIMUM,
            &cold_start_calibration(),
        );
        assert_eq!(validation["actualWordCount"], SHORT_HARD_MINIMUM);
        assert_eq!(validation["passed"], true);
        assert_eq!(validation["providerMode"], "replay");
    }

    #[test]
    fn short_validation_rejects_wrappers_and_runaway_output() {
        let directory = tempdir().expect("tempdir");
        let target = directory
            .path()
            .join("chapters")
            .join("第1章 未命名")
            .join("001.md");
        let wrapped = format!("```\n{}\n```", "a".repeat(SHORT_HARD_MINIMUM));
        assert_eq!(
            validate_candidate(
                &wrapped,
                &target,
                "replay",
                "short",
                SHORT_HARD_MINIMUM,
                SHORT_RUNTIME_MAXIMUM,
                &cold_start_calibration(),
            )["passed"],
            false
        );
        let runaway = "a".repeat(SHORT_RUNTIME_MAXIMUM + 1);
        assert_eq!(
            validate_candidate(
                &runaway,
                &target,
                "replay",
                "short",
                SHORT_HARD_MINIMUM,
                SHORT_RUNTIME_MAXIMUM,
                &cold_start_calibration(),
            )["passed"],
            false
        );
    }

    #[test]
    fn tier_bounds_match_python_story_length_policy() {
        assert_eq!(tier_bounds("short").expect("short"), (700, 4_000));
        assert_eq!(tier_bounds("medium").expect("medium"), (1_800, 7_200));
        assert_eq!(tier_bounds("long").expect("long"), (2_500, 9_000));
        assert!(tier_bounds("unknown").is_err());
    }

    #[test]
    fn tier_prompts_are_explicit_for_live_medium_and_long_generation() {
        assert!(tier_prompt("medium").contains("篇幅档位为中"));
        assert!(tier_prompt("long").contains("篇幅档位为长"));
        assert!(tier_prompt("short").contains("篇幅档位为短"));
    }

    #[test]
    fn medium_and_long_validation_use_their_own_write_gates() {
        let directory = tempdir().expect("tempdir");
        let target = directory
            .path()
            .join("chapters")
            .join("第1章 未命名")
            .join("001.md");
        let calibration = cold_start_calibration();

        let medium = "中".repeat(MEDIUM_HARD_MINIMUM);
        let medium_validation = validate_candidate(
            &medium,
            &target,
            "replay",
            "medium",
            MEDIUM_HARD_MINIMUM,
            MEDIUM_RUNTIME_MAXIMUM,
            &calibration,
        );
        assert_eq!(medium_validation["passed"], true);
        assert_eq!(medium_validation["chapterLengthTier"], "medium");
        assert_eq!(medium_validation["hardMinimum"], MEDIUM_HARD_MINIMUM);

        let long = "长".repeat(LONG_RUNTIME_MAXIMUM + 1);
        let long_validation = validate_candidate(
            &long,
            &target,
            "replay",
            "long",
            LONG_HARD_MINIMUM,
            LONG_RUNTIME_MAXIMUM,
            &calibration,
        );
        assert_eq!(long_validation["passed"], false);
        assert_eq!(long_validation["runtimeSafetyExceeded"], true);
    }

    #[tokio::test]
    async fn medium_story_events_keep_the_selected_tier() {
        let (sender, mut receiver) = mpsc::channel(4);
        let mut events = Vec::new();
        let calibration = ShortCalibrationSummary {
            status: "cold_start".to_owned(),
            preferred_minimum: 2_200,
            preferred_maximum: 5_000,
        };
        emit_story_draft_measured(
            &sender,
            &mut events,
            "trace-medium",
            "session-medium",
            2_800,
            Some(1_200),
            "medium",
            &calibration,
        )
        .await
        .expect("draft event");
        emit_story_accounting(
            &sender,
            &mut events,
            "trace-medium",
            "session-medium",
            "medium",
            "replay",
            1,
        )
        .await
        .expect("accounting event");

        assert_eq!(events[0].0, "StoryDraftMeasured");
        assert_eq!(events[0].1["chapterLengthTier"], "medium");
        assert_eq!(events[1].0, "StoryCallAccounting");
        assert_eq!(events[1].1["chapterLengthTier"], "medium");
        assert!(receiver.recv().await.expect("draft SSE").contains("medium"));
        assert!(
            receiver
                .recv()
                .await
                .expect("accounting SSE")
                .contains("medium")
        );
    }

    #[tokio::test]
    async fn provider_failure_emits_tier_accounting_and_no_story_file() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path().join("workspace");
        let home = directory.path().join("coomi-home");
        fs::create_dir_all(&workspace).expect("workspace");
        fs::create_dir_all(&home).expect("coomi home");
        let state = AppState::with_paths(
            "token",
            home,
            directory.path().join("missing-bridge.exe"),
            Some(directory.path().to_path_buf()),
            None,
        )
        .expect("state");
        let (payload, options) = story_request("medium");
        let cancellation = crate::execution::ExecutionCancellation::default();
        let (sender, _receiver) = mpsc::channel(16);
        let mut events = Vec::new();

        let outcome = run_create_new(
            &state,
            &payload,
            &options,
            &workspace,
            "trace-provider-error",
            "session-provider-error",
            &provider_identity(),
            &cancellation,
            &sender,
            &mut events,
        )
        .await
        .expect("provider error outcome");

        assert_eq!(outcome.terminal_event, "AgentError");
        assert_eq!(
            events
                .iter()
                .find(|(name, _)| name == "StoryProviderAttempt")
                .expect("provider attempt")
                .1["outcome"],
            "error"
        );
        assert_eq!(
            events
                .iter()
                .find(|(name, _)| name == "StoryCallAccounting")
                .expect("accounting")
                .1["chapterLengthTier"],
            "medium"
        );
        assert_eq!(
            events.last().expect("terminal event").1["code"],
            "story_generation_provider_error"
        );
        assert!(!workspace.join("chapters/第1章 未命名/001.md").exists());
    }

    #[tokio::test]
    async fn pre_cancelled_generation_emits_cancelled_terminal_without_writing() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path().join("workspace");
        let home = directory.path().join("coomi-home");
        fs::create_dir_all(&workspace).expect("workspace");
        fs::create_dir_all(&home).expect("coomi home");
        let state = AppState::with_paths(
            "token",
            home,
            directory.path().join("missing-bridge.exe"),
            Some(directory.path().to_path_buf()),
            None,
        )
        .expect("state");
        let (payload, options) = story_request("long");
        let cancellation = crate::execution::ExecutionCancellation::default();
        assert!(cancellation.cancel("fixture_cancelled"));
        let (sender, _receiver) = mpsc::channel(8);
        let mut events = Vec::new();

        let outcome = run_create_new(
            &state,
            &payload,
            &options,
            &workspace,
            "trace-cancelled",
            "session-cancelled",
            &provider_identity(),
            &cancellation,
            &sender,
            &mut events,
        )
        .await
        .expect("cancelled outcome");

        assert_eq!(outcome.terminal_event, "AgentCancelled");
        assert_eq!(events.last().expect("terminal event").0, "AgentCancelled");
        assert_eq!(
            events.last().expect("terminal event").1["reason"],
            "fixture_cancelled"
        );
        assert!(!workspace.join("chapters/第1章 未命名/001.md").exists());
    }

    #[test]
    fn create_new_target_is_programmatic_and_atomic() {
        let directory = tempdir().expect("tempdir");
        let chapters = directory.path().join("chapters");
        fs::create_dir_all(chapters.join("第2章 已有")).expect("existing chapter");
        let target = plan_create_new_target(directory.path(), "default_chapter_directory")
            .expect("planned target");
        let canonical_root = directory.path().canonicalize().expect("canonical root");
        assert_eq!(
            target
                .strip_prefix(canonical_root)
                .expect("relative target")
                .to_string_lossy()
                .replace('\\', "/"),
            "chapters/第3章 未命名/001.md"
        );
        atomic_create(&target, "正文\n".to_owned()).expect("atomic create");
        assert_eq!(
            fs::read_to_string(&target).expect("read created file"),
            "正文\n"
        );
        assert!(atomic_create(&target, "replacement\n".to_owned()).is_err());
        assert_eq!(
            fs::read_to_string(&target).expect("preserved file"),
            "正文\n"
        );
        assert!(
            fs::read_dir(target.parent().expect("target parent"))
                .expect("target directory")
                .filter_map(Result::ok)
                .all(|entry| !entry.file_name().to_string_lossy().contains("storydex-tmp"))
        );
    }

    #[test]
    fn serialized_story_content_matches_platform_newlines() {
        let serialized = serialized_story_content("first\r\n\r\nsecond\n");
        if cfg!(windows) {
            assert_eq!(serialized, "first\r\n\r\nsecond\r\n");
        } else {
            assert_eq!(serialized, "first\n\nsecond\n");
        }
    }
}
