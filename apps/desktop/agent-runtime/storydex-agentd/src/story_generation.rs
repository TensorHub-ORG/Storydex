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
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
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
pub(crate) async fn run_modify_existing(
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
    // Rewriting existing files is deliberately outside the create-new length-tier
    // contract.  Stable uses the chapter runtime ceiling only as a runaway guard;
    // it does not publish a selected tier, tier hit, or calibration sample for an
    // in-place replacement.
    let tier = "";
    let runtime_safety_maximum = existing_story_runtime_safety_maximum(workspace);
    ensure!(
        payload.writes_allowed
            && payload
                .core_writes_allowed
                .unwrap_or(payload.writes_allowed)
            && payload.capability_mode != "read_only",
        "Rust existing-story generation requires an explicit write capability"
    );
    let targets =
        plan_modify_existing_targets(workspace, &payload.active_file, options.fragment_count)?;
    ensure_story_targets_are_authorized(payload, workspace, &targets)?;
    let original_bytes = targets
        .iter()
        .map(|target| {
            fs::read(target).with_context(|| {
                format!("unable to read existing story target {}", target.display())
            })
        })
        .collect::<Result<Vec<_>>>()?;
    let originals = targets
        .iter()
        .zip(original_bytes.iter())
        .map(|(target, bytes)| {
            String::from_utf8(bytes.clone()).with_context(|| {
                format!("existing story target is not UTF-8: {}", target.display())
            })
        })
        .collect::<Result<Vec<_>>>()?;
    let original_sha256 = original_bytes
        .iter()
        .map(|bytes| format!("{:x}", Sha256::digest(bytes)))
        .collect::<Vec<_>>();
    let retained_word_count = originals
        .iter()
        .map(|content| visible_character_count(content))
        .sum::<usize>();
    let target_context = targets
        .iter()
        .zip(originals.iter())
        .map(|(target, content)| {
            let relative = target
                .strip_prefix(workspace)
                .unwrap_or(target)
                .to_string_lossy()
                .replace('\\', "/");
            format!("\n## Existing file: {relative}\n{content}")
        })
        .collect::<String>();
    let fragment_prompt = if targets.len() > 1 {
        "\n\nReturn continuous replacement prose with blank-line paragraph boundaries. Storydex will split it across the already-existing target files in order. Do not create or name new fragments."
    } else {
        "\n\nReturn only the replacement prose for the existing file. Do not create a new file."
    };
    let mut provider_request = json!({
        "action": "complete",
        "home": state.coomi_home(),
        "provider": identity.id,
        "operationType": "modify_existing",
        "messages": [
            {
                "role": "system",
                "content": format!(
                    "Edit existing Storydex chapter prose in place. Preserve the author's intent while applying the requested rewrite. Return only publishable prose; no Markdown fences, XML/content wrappers, metadata, summaries, commentary, or tool calls.{}",
                    fragment_prompt
                )
            },
            {
                "role": "user",
                "content": format!("Author request: {}\n\nExisting project files:{}", payload.prompt, target_context)
            }
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
            "mode": "coomi-rust-story-modify-existing",
            "query": payload.prompt,
            "llmModel": identity.model,
            "llmProvider": identity.id,
            "providerMode": provider_mode(state),
            "operationType": "modify_existing",
            "targetPaths": targets.iter().map(|target| target.to_string_lossy().into_owned()).collect::<Vec<_>>(),
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
            return emit_cancelled_outcome(
                sender,
                trace_events,
                trace_id,
                session_id,
                cancellation,
            )
            .await;
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
                    "message": format!("Story existing-story Provider completion failed: {error:#}"),
                    "providerMode": provider_mode(state),
                    "operationType": "modify_existing",
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
    if cancellation.is_cancelled() {
        return emit_cancelled_outcome(sender, trace_events, trace_id, session_id, cancellation)
            .await;
    }
    let fragments = if targets.len() == 1 {
        vec![content.clone()]
    } else {
        split_publishable_prose(&content, targets.len())
    };
    let mut validation = validate_existing_candidate(
        &targets,
        &fragments,
        provider_mode,
        tier,
        runtime_safety_maximum,
        retained_word_count,
    );
    if !validation
        .get("passed")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        emit_existing_story_draft_measured(
            sender,
            trace_events,
            trace_id,
            session_id,
            visible_character_count(&content),
            completion_tokens,
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
                "message": validation.get("message").cloned().unwrap_or_else(|| json!("Existing story candidate failed validation.")),
                "providerMode": provider_mode,
                "operationType": "modify_existing",
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

    let baseline_mismatches = existing_story_baseline_mismatches(&targets, &original_sha256);
    if !baseline_mismatches.is_empty() {
        if let Some(object) = validation.as_object_mut() {
            object.insert("passed".into(), Value::Bool(false));
            object.insert("status".into(), json!("error"));
            object.insert("baselineMatches".into(), Value::Bool(false));
            object.insert("writeToolApplied".into(), Value::Bool(false));
            object.insert("writtenPaths".into(), json!([]));
            object.insert("issues".into(), json!(baseline_mismatches.clone()));
            object.insert(
                "message".into(),
                json!("现有故事 baseline 在 Provider 执行期间发生变化，未写入项目文件。"),
            );
        }
        emit_existing_story_draft_measured(
            sender,
            trace_events,
            trace_id,
            session_id,
            visible_character_count(&content),
            completion_tokens,
        )
        .await?;
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
                "error_type": "story_generation_baseline_changed",
                "code": "story_generation_baseline_changed",
                "message": baseline_mismatches.join("; "),
                "providerMode": provider_mode,
                "operationType": "modify_existing",
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
        json!({"operationType": "modify_existing"}),
        trace_id,
        session_id,
    )
    .await?;
    let writes = targets
        .iter()
        .cloned()
        .zip(
            fragments
                .iter()
                .map(|fragment| serialized_story_content(fragment)),
        )
        .collect::<Vec<_>>();
    let write_result = atomic_replace_many(&writes);
    emit(
        sender,
        trace_events,
        "StoryCommitFinished",
        json!({"operationType": "modify_existing"}),
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
                json!(format!(
                    "Existing story atomic replacement failed: {error:#}"
                )),
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
                "message": format!("Story existing-story atomic replacement failed: {error:#}"),
                "providerMode": provider_mode,
                "operationType": "modify_existing",
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
    let relative_targets = targets
        .iter()
        .map(|target| {
            target
                .strip_prefix(workspace)
                .unwrap_or(target)
                .to_string_lossy()
                .replace('\\', "/")
        })
        .collect::<Vec<_>>();
    let actual_word_count = visible_character_count(&content);
    if let Some(object) = validation.as_object_mut() {
        object.insert("writeToolApplied".into(), Value::Bool(true));
        object.insert("writtenPaths".into(), json!(relative_targets.clone()));
        object.insert("finalWordCount".into(), json!(actual_word_count));
    }
    emit_existing_story_draft_measured(
        sender,
        trace_events,
        trace_id,
        session_id,
        actual_word_count,
        completion_tokens,
    )
    .await?;
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
    let reply = format!(
        "已重写现有故事文件 {}，本次生成 {} 字。",
        relative_targets.join(", "),
        actual_word_count
    );
    emit(
        sender,
        trace_events,
        "TextChunk",
        json!({
            "content": reply,
            "path": relative_targets.first().cloned().unwrap_or_default(),
            "writtenPaths": relative_targets.clone(),
            "providerMode": provider_mode,
            "operationType": "modify_existing",
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
            "operationType": "modify_existing",
            "writtenPaths": relative_targets,
            "fragmentCount": targets.len(),
            "chapterLengthTier": "",
            "wordCountScope": "edited_existing",
            "actualWordCount": actual_word_count,
            "generatedWordCount": actual_word_count,
            "retainedWordCount": retained_word_count,
            "resultingWordCount": actual_word_count,
            "finalWordCount": actual_word_count,
            "tierHit": null,
            "draftDurationMs": draft_duration_ms,
        }),
        trace_id,
        session_id,
    )
    .await?;
    Ok(StoryGenerationOutcome {
        terminal_event: "AgentCompleted".to_owned(),
        reply,
    })
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

    let targets = plan_create_new_targets(
        workspace,
        &options.chapter_template_id,
        options.fragment_count,
    )?;
    ensure_story_targets_are_authorized(payload, workspace, &targets)?;
    let effective_fragment_count = targets.len();
    let calibration = read_tier_summary(workspace, &identity.id, &identity.model, tier)?;
    let fragment_prompt = if effective_fragment_count > 1 {
        "\n\nWrite continuous prose with clear blank-line paragraph boundaries. Storydex will split the prose programmatically into the planned fragment files; do not emit fragment labels or separate metadata."
    } else {
        ""
    };
    let mut provider_request = json!({
        "action": "complete",
        "home": state.coomi_home(),
        "provider": identity.id,
        "messages": [
            {
                "role": "system",
                "content": format!(
                    "Generate only the publishable Storydex chapter prose. Do not emit Markdown fences, XML/content wrappers, metadata, summaries, or commentary.\n\n{}{}",
                    tier_prompt(tier),
                    fragment_prompt,
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
    if cancellation.is_cancelled() {
        return emit_cancelled_outcome(sender, trace_events, trace_id, session_id, cancellation)
            .await;
    }
    let fragments = if effective_fragment_count == 1 {
        vec![content.clone()]
    } else {
        split_publishable_prose(&content, effective_fragment_count)
    };
    let mut validation = validate_candidate(
        &content,
        &targets,
        &fragments,
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

    if cancellation.is_cancelled() {
        return emit_cancelled_outcome(sender, trace_events, trace_id, session_id, cancellation)
            .await;
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
    let writes = targets
        .iter()
        .cloned()
        .zip(
            fragments
                .iter()
                .map(|fragment| serialized_story_content(fragment)),
        )
        .collect::<Vec<_>>();
    let write_result = atomic_create_many(&writes);
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
    let relative_targets = targets
        .iter()
        .map(|target| {
            target
                .strip_prefix(workspace)
                .unwrap_or(target)
                .to_string_lossy()
                .replace('\\', "/")
        })
        .collect::<Vec<_>>();
    if let Some(object) = validation.as_object_mut() {
        object.insert("writeToolApplied".into(), Value::Bool(true));
        object.insert("writtenPaths".into(), json!(relative_targets.clone()));
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
            "content": completion_reply(&relative_targets[0], actual_word_count, tier, tier_hit),
            "path": relative_targets[0].clone(),
            "writtenPaths": relative_targets.clone(),
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
            "writtenPaths": relative_targets.clone(),
            "fragmentCount": effective_fragment_count,
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
        reply: completion_reply(&relative_targets[0], actual_word_count, tier, tier_hit),
    })
}

async fn emit_cancelled_outcome(
    sender: &mpsc::Sender<String>,
    trace_events: &mut Vec<(String, Value)>,
    trace_id: &str,
    session_id: &str,
    cancellation: &crate::execution::ExecutionCancellation,
) -> Result<StoryGenerationOutcome> {
    emit(
        sender,
        trace_events,
        "AgentCancelled",
        json!({"reason": cancellation.reason()}),
        trace_id,
        session_id,
    )
    .await?;
    Ok(StoryGenerationOutcome {
        terminal_event: "AgentCancelled".to_owned(),
        reply: String::new(),
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

/// Emit the Stable event shape for an in-place existing-story rewrite.
///
/// Existing rewrites are not tier samples: the Python Stable path leaves the
/// tier-specific fields absent and only records the generic draft measurement.
async fn emit_existing_story_draft_measured(
    sender: &mpsc::Sender<String>,
    trace_events: &mut Vec<(String, Value)>,
    trace_id: &str,
    session_id: &str,
    word_count: usize,
    completion_tokens: Option<u64>,
) -> Result<()> {
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
            "calibrationStatus": "",
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

pub(crate) fn plan_create_new_targets(
    workspace: &Path,
    template_id: &str,
    requested_fragment_count: u64,
) -> Result<Vec<PathBuf>> {
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
    let chapter = chapters.join(format!("第{next_number}章 未命名"));
    let fragment_count = if template_id.contains("single_file") {
        1usize
    } else {
        usize::try_from(requested_fragment_count.clamp(1, 20))
            .context("story fragment count is unsupported")?
    };
    let targets = (1..=fragment_count)
        .map(|index| {
            chapter.join(if template_id.contains("single_file") {
                "正文.md".to_owned()
            } else {
                format!("{index:03}.md")
            })
        })
        .collect::<Vec<_>>();
    ensure!(
        targets.iter().all(|target| target.starts_with(&root)),
        "planned story path escaped workspace"
    );
    Ok(targets)
}

pub(crate) fn plan_modify_existing_targets(
    workspace: &Path,
    active_file: &str,
    requested_fragment_count: u64,
) -> Result<Vec<PathBuf>> {
    let root = workspace
        .canonicalize()
        .context("story workspace is unavailable")?;
    let normalized = active_file.trim().replace('\\', "/");
    ensure!(
        !normalized.is_empty()
            && !Path::new(&normalized).is_absolute()
            && !Path::new(&normalized)
                .components()
                .any(|component| matches!(component, std::path::Component::ParentDir)),
        "existing story activeFile must be a safe workspace-relative path"
    );
    let active = root
        .join(&normalized)
        .canonicalize()
        .with_context(|| format!("existing story target is unavailable: {normalized}"))?;
    ensure!(
        active.starts_with(&root) && active.is_file(),
        "existing story target is invalid"
    );
    let count = usize::try_from(requested_fragment_count.clamp(1, 20))
        .context("existing story fragment count is unsupported")?;
    if count == 1 {
        return Ok(vec![active]);
    }
    let parent = active
        .parent()
        .context("existing story target has no parent directory")?;
    let mut siblings = fs::read_dir(parent)
        .with_context(|| format!("unable to list existing story chapter {}", parent.display()))?
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let path = entry.path();
            if !path.is_file() {
                return None;
            }
            let extension = path
                .extension()
                .and_then(|value| value.to_str())
                .unwrap_or_default();
            if !matches!(
                extension.to_ascii_lowercase().as_str(),
                "md" | "markdown" | "txt"
            ) || path
                .file_name()
                .and_then(|value| value.to_str())
                .is_some_and(|name| name.eq_ignore_ascii_case("readme.md"))
            {
                return None;
            }
            path.canonicalize().ok()
        })
        .filter(|path| path.starts_with(&root))
        .collect::<Vec<_>>();
    siblings.sort();
    let active_index = siblings
        .iter()
        .position(|path| path == &active)
        .context("existing story activeFile is not a supported chapter segment")?;
    ensure!(
        siblings.len().saturating_sub(active_index) >= count,
        "existing story does not contain enough contiguous fragments for the requested edit"
    );
    Ok(siblings
        .into_iter()
        .skip(active_index)
        .take(count)
        .collect())
}

fn validate_existing_candidate(
    targets: &[PathBuf],
    fragments: &[String],
    _provider_mode: &str,
    tier: &str,
    runtime_safety_maximum: usize,
    retained_word_count: usize,
) -> Value {
    let generated_word_count = fragments
        .iter()
        .map(|fragment| visible_character_count(fragment))
        .sum::<usize>();
    let quality_issues = [
        (
            fragments.iter().any(|fragment| fragment.trim().is_empty()),
            "empty_prose",
        ),
        (
            fragments.iter().any(|fragment| fragment.contains('\0')),
            "nul_character",
        ),
        (
            fragments
                .iter()
                .any(|fragment| fragment.contains("```") || fragment.contains("<content>")),
            "wrapper_block",
        ),
    ]
    .into_iter()
    .filter_map(|(failed, issue)| failed.then_some(issue))
    .collect::<Vec<_>>();
    let unique_targets = targets.iter().collect::<HashSet<_>>().len() == targets.len();
    let structure_passed = !targets.is_empty()
        && targets.len() == fragments.len()
        && unique_targets
        && targets.iter().all(|target| target.is_file());
    let passed = structure_passed
        && quality_issues.is_empty()
        && generated_word_count <= runtime_safety_maximum;
    let target_paths = targets
        .iter()
        .map(|target| target.to_string_lossy().into_owned())
        .collect::<Vec<_>>();
    json!({
        "_type": "StoryGenerationValidation",
        "_version": 1,
        "applicable": true,
        "passed": passed,
        "status": if passed { "success" } else { "error" },
        "algorithm": "storydex_visible_characters_v1",
        "countingRule": STORY_COUNTING_RULE,
        "exact": false,
        "operationType": "modify_existing",
        "fragmentCount": targets.len(),
        "expectedFragmentCount": targets.len(),
        "actualFragmentCount": fragments.len(),
        "actualWordCount": generated_word_count,
        "generatedWordCount": generated_word_count,
        "retainedWordCount": retained_word_count,
        "resultingWordCount": generated_word_count,
        "chapterLengthTier": tier,
        "wordCountScope": "edited_existing",
        "hardMinimum": 0,
        "hardMinimumPassed": true,
        "runtimeSafetyMaximum": runtime_safety_maximum,
        "runtimeSafetyExceeded": generated_word_count > runtime_safety_maximum,
        "structurePassed": structure_passed,
        "qualityPassed": quality_issues.is_empty(),
        "machineQualityPassed": quality_issues.is_empty(),
        "qualityIssues": quality_issues,
        "providerCalls": 1,
        "contractViolations": [],
        "initialWordCount": generated_word_count,
        "finalWordCount": 0,
        "normalBandPassed": false,
        "precisionAchieved": null,
        "targetPaths": target_paths,
        "writeToolApplied": false,
        "writtenPaths": [],
        "message": if passed { "现有故事正文已通过替换写入前的结构与质量门禁。" } else { "现有故事候选未通过结构、质量或运行时安全门禁，未写入项目文件。" },
    })
}

#[allow(clippy::too_many_arguments)]
fn validate_candidate(
    content: &str,
    targets: &[PathBuf],
    fragments: &[String],
    provider_mode: &str,
    tier: &str,
    hard_minimum: usize,
    runtime_safety_maximum: usize,
    calibration: &ShortCalibrationSummary,
) -> Value {
    let count = visible_character_count(content);
    let fragment_count = targets.len();
    let fragment_counts = fragments
        .iter()
        .map(|fragment| visible_character_count(fragment))
        .collect::<Vec<_>>();
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
    let parents = targets
        .iter()
        .filter_map(|target| target.parent())
        .collect::<HashSet<_>>();
    let unique_targets = targets.iter().collect::<HashSet<_>>().len() == targets.len();
    let structure_passed = !targets.is_empty()
        && targets.len() == fragments.len()
        && unique_targets
        && parents.len() == 1
        && targets.iter().all(|target| {
            target.extension().and_then(|value| value.to_str()) == Some("md")
                && target
                    .file_name()
                    .and_then(|value| value.to_str())
                    .is_some_and(|name| !name.eq_ignore_ascii_case("README.md"))
        })
        && fragments.iter().all(|fragment| !fragment.trim().is_empty());
    let passed = structure_passed
        && (hard_minimum..=runtime_safety_maximum).contains(&count)
        && quality_issues.is_empty();
    let tier_hit = (calibration.preferred_minimum..=calibration.preferred_maximum).contains(&count);
    let target_paths = targets
        .iter()
        .map(|target| target.to_string_lossy().into_owned())
        .collect::<Vec<_>>();
    let fragment_validation = fragment_counts
        .iter()
        .enumerate()
        .map(|(index, count)| {
            json!({
                "order": index + 1,
                "path": target_paths.get(index).cloned().unwrap_or_default(),
                "actualWordCount": count,
                "generatedWordCount": count,
                "status": if fragments.get(index).is_some_and(|fragment| !fragment.trim().is_empty()) { "passed" } else { "failed" },
            })
        })
        .collect::<Vec<_>>();
    json!({
        "_type": "StoryGenerationValidation",
        "_version": 1,
        "applicable": true,
        "passed": passed,
        "status": if passed && tier_hit { "success" } else if passed { "warning" } else { "error" },
        "algorithm": "storydex_visible_characters_v1",
        "countingRule": STORY_COUNTING_RULE,
        "exact": false,
        "fragmentCount": fragment_count,
        "expectedFragmentCount": fragment_count,
        "actualFragmentCount": fragments.len(),
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
        "structurePassed": structure_passed,
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
        "targetPath": target_paths.first().cloned().unwrap_or_default(),
        "targetPaths": target_paths,
        "fragments": fragment_validation,
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

fn existing_story_runtime_safety_maximum(workspace: &Path) -> usize {
    let target = fs::read_to_string(workspace.join(".storydex/config/project-settings.json"))
        .ok()
        .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
        .and_then(|settings| {
            settings
                .get("chapterWordCountTarget")
                .and_then(Value::as_u64)
        })
        .filter(|value| (100..=20_000).contains(value))
        .unwrap_or(3_000);
    usize::try_from(target.saturating_mul(2)).unwrap_or(40_000)
}

fn visible_character_count(content: &str) -> usize {
    content
        .chars()
        .filter(|character| !character.is_whitespace())
        .count()
}

fn existing_story_baseline_mismatches(
    targets: &[PathBuf],
    expected_sha256: &[String],
) -> Vec<String> {
    targets
        .iter()
        .zip(expected_sha256.iter())
        .filter_map(|(target, expected)| match fs::read(target) {
            Ok(bytes) if format!("{:x}", Sha256::digest(&bytes)) == expected.as_str() => None,
            Ok(_) => Some(format!(
                "existing story baseline changed while Provider was running: {}",
                target.display()
            )),
            Err(error) => Some(format!(
                "existing story baseline is unavailable before commit: {}: {error}",
                target.display()
            )),
        })
        .collect()
}

fn split_publishable_prose(content: &str, fragment_count: usize) -> Vec<String> {
    let count = fragment_count.max(1);
    let normalized = content.replace("\r\n", "\n").replace('\r', "\n");
    let paragraphs = normalized
        .split("\n\n")
        .map(str::trim)
        .filter(|paragraph| !paragraph.is_empty())
        .map(ToOwned::to_owned)
        .collect::<Vec<_>>();
    if paragraphs.is_empty() {
        return Vec::new();
    }
    if count == 1 {
        return vec![paragraphs.join("\n\n")];
    }

    let mut units = paragraphs;
    if units.len() < count {
        units = units
            .iter()
            .flat_map(|paragraph| split_story_sentences(paragraph))
            .collect();
    }
    if units.len() < count {
        return units;
    }

    let total = units.iter().map(|unit| unit.chars().count()).sum::<usize>();
    let per_fragment = total as f64 / count as f64;
    let mut groups = vec![Vec::<String>::new(); count];
    let mut index = 0usize;
    let mut accumulated = 0usize;
    for (position, unit) in units.iter().enumerate() {
        let remaining_units = units.len() - position;
        let remaining_slots = count - index;
        groups[index].push(unit.clone());
        accumulated += unit.chars().count();
        if index < count - 1
            && (accumulated as f64 >= per_fragment * (index + 1) as f64
                || remaining_units <= remaining_slots)
        {
            index += 1;
        }
    }
    groups
        .into_iter()
        .filter(|group| !group.is_empty())
        .map(|group| group.join("\n\n"))
        .collect()
}

fn split_story_sentences(paragraph: &str) -> Vec<String> {
    let mut sentences = Vec::new();
    let mut current = String::new();
    for character in paragraph.chars() {
        current.push(character);
        if matches!(character, '。' | '！' | '？' | '…' | '”' | '」' | '』') {
            let sentence = current.trim();
            if !sentence.is_empty() {
                sentences.push(sentence.to_owned());
            }
            current.clear();
        }
    }
    let tail = current.trim();
    if !tail.is_empty() {
        sentences.push(tail.to_owned());
    }
    if sentences.is_empty() {
        vec![paragraph.trim().to_owned()]
    } else {
        sentences
    }
}

fn ensure_story_targets_are_authorized(
    payload: &ChatStreamRequest,
    workspace: &Path,
    targets: &[PathBuf],
) -> Result<()> {
    let root = workspace
        .canonicalize()
        .context("story workspace is unavailable")?;
    ensure!(
        targets.iter().all(|target| target.starts_with(&root)),
        "story target escaped workspace"
    );
    if payload.capability_mode != "scoped_write" {
        return Ok(());
    }
    let allowed_roots = payload
        .allowed_write_roots
        .iter()
        .map(|raw| {
            let path = PathBuf::from(raw);
            let candidate = if path.is_absolute() {
                path
            } else {
                root.join(path)
            };
            candidate
                .canonicalize()
                .with_context(|| format!("story allowed write root is unavailable: {raw}"))
        })
        .collect::<Result<Vec<_>>>()?;
    ensure!(
        targets.iter().all(|target| allowed_roots
            .iter()
            .any(|allowed| target.starts_with(allowed))),
        "story target is outside allowedWriteRoots"
    );
    Ok(())
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

fn atomic_create_many(writes: &[(PathBuf, String)]) -> Result<()> {
    atomic_create_many_with(writes, |temporary, target| fs::rename(temporary, target))
}

fn atomic_create_many_with<F>(writes: &[(PathBuf, String)], mut publish: F) -> Result<()>
where
    F: FnMut(&Path, &Path) -> io::Result<()>,
{
    ensure!(!writes.is_empty(), "story commit has no target files");
    let mut unique_targets = HashSet::new();
    for (target, _) in writes {
        ensure!(
            unique_targets.insert(target.clone()),
            "story commit contains duplicate targets"
        );
        ensure!(!target.exists(), "create_new story target already exists");
    }

    let mut created_directories = HashSet::new();
    let mut staged = Vec::with_capacity(writes.len());
    let stage_result = (|| -> Result<()> {
        for (target, content) in writes {
            let parent = target.parent().context("story target has no parent")?;
            let mut missing = Vec::new();
            let mut cursor = parent;
            while !cursor.exists() {
                missing.push(cursor.to_path_buf());
                cursor = cursor
                    .parent()
                    .context("story target parent escaped filesystem root")?;
            }
            fs::create_dir_all(parent)?;
            created_directories.extend(missing);
            let temporary = target.with_file_name(format!(
                ".{}.storydex-tmp-{}",
                target
                    .file_name()
                    .and_then(|value| value.to_str())
                    .unwrap_or("chapter"),
                Uuid::new_v4()
            ));
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temporary)?;
            file.write_all(content.as_bytes())?;
            file.flush()?;
            file.sync_all()?;
            drop(file);
            staged.push((temporary, target.clone()));
        }
        Ok(())
    })();
    if let Err(error) = stage_result {
        cleanup_story_staging(&staged, &[], &created_directories);
        return Err(error);
    }

    let mut published = Vec::with_capacity(staged.len());
    for (temporary, target) in &staged {
        if target.exists() {
            cleanup_story_staging(&staged, &published, &created_directories);
            bail!(
                "create_new story target appeared before commit: {}",
                target.display()
            );
        }
        if let Err(error) = publish(temporary, target) {
            cleanup_story_staging(&staged, &published, &created_directories);
            return Err(error)
                .with_context(|| format!("unable to publish story target {}", target.display()));
        }
        published.push(target.clone());
    }
    for (temporary, _) in &staged {
        if let Err(error) = fs::remove_file(temporary) {
            tracing::warn!(error = %error, path = %temporary.display(), "unable to remove committed story staging file");
        }
    }
    Ok(())
}

fn cleanup_story_staging(
    staged: &[(PathBuf, PathBuf)],
    published: &[PathBuf],
    created_directories: &HashSet<PathBuf>,
) {
    for target in published.iter().rev() {
        let _ = fs::remove_file(target);
    }
    for (temporary, _) in staged {
        let _ = fs::remove_file(temporary);
    }
    let mut directories = created_directories.iter().collect::<Vec<_>>();
    directories.sort_by_key(|path| std::cmp::Reverse(path.components().count()));
    for directory in directories {
        let _ = fs::remove_dir(directory);
    }
}

fn atomic_replace_many(writes: &[(PathBuf, String)]) -> Result<()> {
    atomic_replace_many_with(writes, |temporary, target| fs::rename(temporary, target))
}

fn atomic_replace_many_with<F>(writes: &[(PathBuf, String)], mut publish: F) -> Result<()>
where
    F: FnMut(&Path, &Path) -> io::Result<()>,
{
    ensure!(
        !writes.is_empty(),
        "existing story replacement has no targets"
    );
    let mut unique_targets = HashSet::new();
    for (target, _) in writes {
        ensure!(
            unique_targets.insert(target.clone()),
            "existing story replacement contains duplicate targets"
        );
        ensure!(
            target.is_file(),
            "existing story replacement target is missing"
        );
    }
    let mut staged = Vec::with_capacity(writes.len());
    for (target, content) in writes {
        let temporary = target.with_file_name(format!(
            ".{}.storydex-replace-{}",
            target
                .file_name()
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
            Ok(())
        })();
        if let Err(error) = result {
            for (temporary, _, _) in &staged {
                let _ = fs::remove_file(temporary);
            }
            let _ = fs::remove_file(&temporary);
            return Err(error);
        }
        staged.push((temporary, target.clone(), None::<PathBuf>));
    }

    let mut moved_backups = Vec::new();
    for index in 0..staged.len() {
        let temporary = staged[index].0.clone();
        let target = staged[index].1.clone();
        let backup = target.with_file_name(format!(
            ".{}.storydex-backup-{}",
            target
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("chapter"),
            Uuid::new_v4()
        ));
        if let Err(error) = fs::rename(&target, &backup) {
            let rollback_result = rollback_replacement(&staged, &moved_backups);
            if let Err(rollback_error) = rollback_result {
                bail!(
                    "unable to stage existing story target {}: {error}; {rollback_error:#}",
                    target.display()
                );
            }
            return Err(error).with_context(|| {
                format!("unable to stage existing story target {}", target.display())
            });
        }
        staged[index].2 = Some(backup.clone());
        moved_backups.push(backup.clone());
        if let Err(error) = publish(&temporary, &target) {
            let rollback_result = rollback_replacement(&staged, &moved_backups);
            if let Err(rollback_error) = rollback_result {
                bail!(
                    "unable to publish existing story target {}: {error}; {rollback_error:#}",
                    target.display()
                );
            }
            return Err(error).with_context(|| {
                format!(
                    "unable to publish existing story target {}",
                    target.display()
                )
            });
        }
    }

    for (_, _, backup) in &staged {
        if let Some(backup) = backup {
            let _ = fs::remove_file(backup);
        }
    }
    for (temporary, _, _) in &staged {
        let _ = fs::remove_file(temporary);
    }
    Ok(())
}

fn rollback_replacement(
    staged: &[(PathBuf, PathBuf, Option<PathBuf>)],
    moved_backups: &[PathBuf],
) -> Result<()> {
    let mut failures = Vec::new();
    for backup in moved_backups.iter().rev() {
        if let Some(target) = staged
            .iter()
            .find(|(_, _, candidate)| candidate.as_ref() == Some(backup))
            .map(|(_, target, _)| target)
        {
            let target_cleared = if target.exists() {
                match fs::remove_file(target) {
                    Ok(()) => true,
                    Err(error) => {
                        failures.push(format!(
                            "unable to clear replacement target {}: {error}",
                            target.display()
                        ));
                        false
                    }
                }
            } else {
                true
            };
            if target_cleared && let Err(error) = fs::rename(backup, target) {
                failures.push(format!(
                    "unable to restore backup {} to {}: {error}",
                    backup.display(),
                    target.display()
                ));
            }
        }
    }
    for (temporary, _, _) in staged {
        if let Err(error) = fs::remove_file(temporary)
            && error.kind() != io::ErrorKind::NotFound
        {
            failures.push(format!(
                "unable to remove replacement staging file {}: {error}",
                temporary.display()
            ));
        }
    }
    if failures.is_empty() {
        Ok(())
    } else {
        bail!("rollback incomplete: {}", failures.join("; "))
    }
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

    fn validate_single_candidate(
        prose: &str,
        target: &Path,
        tier: &str,
        hard_minimum: usize,
        runtime_safety_maximum: usize,
        calibration: &ShortCalibrationSummary,
    ) -> Value {
        validate_candidate(
            prose,
            &[target.to_path_buf()],
            &[prose.to_owned()],
            "replay",
            tier,
            hard_minimum,
            runtime_safety_maximum,
            calibration,
        )
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
        let validation = validate_single_candidate(
            &prose,
            &target,
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
            validate_single_candidate(
                &wrapped,
                &target,
                "short",
                SHORT_HARD_MINIMUM,
                SHORT_RUNTIME_MAXIMUM,
                &cold_start_calibration(),
            )["passed"],
            false
        );
        let runaway = "a".repeat(SHORT_RUNTIME_MAXIMUM + 1);
        assert_eq!(
            validate_single_candidate(
                &runaway,
                &target,
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
        let medium_validation = validate_single_candidate(
            &medium,
            &target,
            "medium",
            MEDIUM_HARD_MINIMUM,
            MEDIUM_RUNTIME_MAXIMUM,
            &calibration,
        );
        assert_eq!(medium_validation["passed"], true);
        assert_eq!(medium_validation["chapterLengthTier"], "medium");
        assert_eq!(medium_validation["hardMinimum"], MEDIUM_HARD_MINIMUM);

        let long = "长".repeat(LONG_RUNTIME_MAXIMUM + 1);
        let long_validation = validate_single_candidate(
            &long,
            &target,
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
        let (payload, mut options) = story_request("medium");
        options.fragment_count = 3;
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
        assert!(!workspace.join("chapters/第1章 未命名/002.md").exists());
        assert!(!workspace.join("chapters/第1章 未命名/003.md").exists());
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
        let (payload, mut options) = story_request("long");
        options.fragment_count = 3;
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
        assert!(!workspace.join("chapters/第1章 未命名/002.md").exists());
        assert!(!workspace.join("chapters/第1章 未命名/003.md").exists());
    }

    #[tokio::test]
    async fn pre_cancelled_modify_existing_preserves_original_story_bytes() {
        let directory = tempdir().expect("tempdir");
        let workspace = directory.path().join("workspace");
        let home = directory.path().join("coomi-home");
        let target = workspace.join("chapters/fixture-story.md");
        fs::create_dir_all(target.parent().expect("target parent")).expect("chapter");
        fs::create_dir_all(&home).expect("coomi home");
        fs::write(&target, "before\n").expect("before");
        let state = AppState::with_paths(
            "token",
            home,
            directory.path().join("missing-bridge.exe"),
            Some(directory.path().to_path_buf()),
            None,
        )
        .expect("state");
        let (mut payload, options) = story_request("short");
        payload.prompt = "请重写现有章节".to_owned();
        payload.active_file = "chapters/fixture-story.md".to_owned();
        let cancellation = crate::execution::ExecutionCancellation::default();
        assert!(cancellation.cancel("fixture_cancelled"));
        let (sender, _receiver) = mpsc::channel(8);
        let mut events = Vec::new();

        let outcome = run_modify_existing(
            &state,
            &payload,
            &options,
            &workspace,
            "trace-modify-cancelled",
            "session-modify-cancelled",
            &provider_identity(),
            &cancellation,
            &sender,
            &mut events,
        )
        .await
        .expect("cancelled outcome");

        assert_eq!(outcome.terminal_event, "AgentCancelled");
        assert_eq!(events.last().expect("terminal event").0, "AgentCancelled");
        assert_eq!(fs::read(&target).expect("preserved bytes"), b"before\n");
    }

    #[test]
    fn multi_fragment_planner_keeps_all_targets_in_one_chapter() {
        let directory = tempdir().expect("tempdir");
        fs::create_dir_all(directory.path().join("chapters/第2章 已有")).expect("existing chapter");
        let root = directory.path().canonicalize().expect("canonical root");
        let targets = plan_create_new_targets(directory.path(), "default_chapter_directory", 4)
            .expect("planned targets");
        let relative = targets
            .iter()
            .map(|target| {
                target
                    .strip_prefix(&root)
                    .expect("relative target")
                    .to_string_lossy()
                    .replace('\\', "/")
            })
            .collect::<Vec<_>>();
        assert_eq!(
            relative,
            vec![
                "chapters/第3章 未命名/001.md",
                "chapters/第3章 未命名/002.md",
                "chapters/第3章 未命名/003.md",
                "chapters/第3章 未命名/004.md",
            ]
        );

        let single_file =
            plan_create_new_targets(directory.path(), "single_file_chapter_directory", 9)
                .expect("single-file target");
        assert_eq!(single_file.len(), 1);
        assert_eq!(
            single_file[0].file_name().and_then(|value| value.to_str()),
            Some("正文.md")
        );
    }

    #[test]
    fn multi_fragment_split_matches_python_paragraph_allocation() {
        let prose = [
            "甲".repeat(250),
            "乙".repeat(250),
            "丙".repeat(250),
            "丁".repeat(250),
            "戊".repeat(250),
            "己".repeat(250),
        ]
        .join("\n\n");
        let fragments = split_publishable_prose(&prose, 3);
        assert_eq!(fragments.len(), 3);
        assert_eq!(
            fragments
                .iter()
                .map(|fragment| visible_character_count(fragment))
                .collect::<Vec<_>>(),
            vec![500, 500, 500]
        );
        assert!(fragments[0].starts_with('甲'));
        assert!(fragments[1].starts_with('丙'));
        assert!(fragments[2].starts_with('戊'));
    }

    #[test]
    fn multi_fragment_validation_uses_one_candidate_length_gate() {
        let directory = tempdir().expect("tempdir");
        fs::create_dir_all(directory.path().join("chapters")).expect("chapters");
        let targets = plan_create_new_targets(directory.path(), "default_chapter_directory", 3)
            .expect("targets");
        let content = ["甲".repeat(500), "乙".repeat(500), "丙".repeat(500)].join("\n\n");
        let fragments = split_publishable_prose(&content, 3);
        let validation = validate_candidate(
            &content,
            &targets,
            &fragments,
            "replay",
            "short",
            SHORT_HARD_MINIMUM,
            SHORT_RUNTIME_MAXIMUM,
            &cold_start_calibration(),
        );
        assert_eq!(validation["passed"], true);
        assert_eq!(validation["fragmentCount"], 3);
        assert_eq!(validation["actualWordCount"], 1500);
        assert_eq!(validation["structurePassed"], true);
    }

    #[test]
    fn multi_fragment_commit_rolls_back_every_published_target_on_failure() {
        let directory = tempdir().expect("tempdir");
        let chapter = directory.path().join("chapters/第1章 未命名");
        let writes = (1..=3)
            .map(|index| {
                (
                    chapter.join(format!("{index:03}.md")),
                    format!("fragment-{index}\n"),
                )
            })
            .collect::<Vec<_>>();
        let mut publication = 0usize;
        let result = atomic_create_many_with(&writes, |temporary, target| {
            publication += 1;
            if publication == 2 {
                return Err(io::Error::other("injected second publish failure"));
            }
            fs::hard_link(temporary, target)
        });
        assert!(result.is_err());
        assert!(writes.iter().all(|(target, _)| !target.exists()));
        assert!(!chapter.exists());
    }

    #[test]
    fn modify_existing_planner_selects_only_existing_contiguous_fragments() {
        let directory = tempdir().expect("tempdir");
        let chapter = directory.path().join("chapters/第1章 既有");
        fs::create_dir_all(&chapter).expect("chapter");
        for index in 1..=4 {
            fs::write(
                chapter.join(format!("{index:03}.md")),
                format!("before-{index}\n"),
            )
            .expect("fragment");
        }
        let root = directory.path().canonicalize().expect("root");
        let targets =
            plan_modify_existing_targets(directory.path(), "chapters/第1章 既有/002.md", 2)
                .expect("existing targets");
        assert_eq!(
            targets
                .iter()
                .map(|target| target
                    .strip_prefix(&root)
                    .expect("relative")
                    .to_string_lossy()
                    .replace('\\', "/"))
                .collect::<Vec<_>>(),
            vec!["chapters/第1章 既有/002.md", "chapters/第1章 既有/003.md",]
        );
        assert!(
            plan_modify_existing_targets(directory.path(), "chapters/第1章 既有/004.md", 2,)
                .is_err()
        );
    }

    #[test]
    fn modify_existing_validation_has_no_new_fragment_length_minimum() {
        let directory = tempdir().expect("tempdir");
        let chapter = directory.path().join("chapters/第1章 既有");
        fs::create_dir_all(&chapter).expect("chapter");
        let targets = [chapter.join("001.md"), chapter.join("002.md")];
        fs::write(&targets[0], "old-one\n").expect("old one");
        fs::write(&targets[1], "old-two\n").expect("old two");
        let validation = validate_existing_candidate(
            &targets,
            &["新一。".to_owned(), "新二。".to_owned()],
            "replay",
            "short",
            SHORT_RUNTIME_MAXIMUM,
            12,
        );
        assert_eq!(validation["passed"], true);
        assert_eq!(validation["hardMinimum"], 0);
        assert_eq!(validation["wordCountScope"], "edited_existing");
        assert_eq!(validation["operationType"], "modify_existing");
        assert_eq!(validation["initialWordCount"], 6);
        assert_eq!(validation["normalBandPassed"], false);
        assert_eq!(validation["precisionAchieved"], Value::Null);
    }

    #[test]
    fn existing_story_runtime_ceiling_follows_project_target_without_tier() {
        let directory = tempdir().expect("tempdir");
        fs::create_dir_all(directory.path().join(".storydex/config")).expect("config");
        fs::write(
            directory
                .path()
                .join(".storydex/config/project-settings.json"),
            r#"{"chapterWordCountTarget": 4200}"#,
        )
        .expect("settings");
        assert_eq!(
            existing_story_runtime_safety_maximum(directory.path()),
            8400
        );
    }

    #[tokio::test]
    async fn existing_story_draft_event_omits_create_new_tier_fields() {
        let (sender, mut receiver) = mpsc::channel(2);
        let mut events = Vec::new();
        emit_existing_story_draft_measured(
            &sender,
            &mut events,
            "trace-existing",
            "session-existing",
            306,
            Some(210),
        )
        .await
        .expect("draft event");
        assert_eq!(events[0].1["chapterLengthTier"], Value::Null);
        assert_eq!(events[0].1["tierHit"], Value::Null);
        assert_eq!(events[0].1["wordCountScope"], Value::Null);
        assert!(receiver.recv().await.expect("draft SSE").contains("306"));
    }

    #[test]
    fn modify_existing_baseline_guard_detects_edits_and_deletions_before_commit() {
        let directory = tempdir().expect("tempdir");
        let chapter = directory.path().join("chapters/第1章 既有");
        fs::create_dir_all(&chapter).expect("chapter");
        let edited = chapter.join("001.md");
        let deleted = chapter.join("002.md");
        fs::write(&edited, "before-one\n").expect("edited baseline");
        fs::write(&deleted, "before-two\n").expect("deleted baseline");
        let targets = vec![edited.clone(), deleted.clone()];
        let hashes = targets
            .iter()
            .map(|target| {
                format!(
                    "{:x}",
                    Sha256::digest(fs::read(target).expect("baseline bytes"))
                )
            })
            .collect::<Vec<_>>();

        assert!(existing_story_baseline_mismatches(&targets, &hashes).is_empty());
        fs::write(&edited, "external-edit\n").expect("external edit");
        fs::remove_file(&deleted).expect("external delete");

        let mismatches = existing_story_baseline_mismatches(&targets, &hashes);
        assert_eq!(mismatches.len(), 2);
        assert!(mismatches[0].contains("baseline changed"));
        assert!(mismatches[1].contains("baseline is unavailable"));
        assert_eq!(
            fs::read_to_string(&edited).expect("edit preserved"),
            "external-edit\n"
        );
        assert!(!deleted.exists());
    }

    #[test]
    fn modify_existing_transaction_restores_all_original_bytes_on_failure() {
        let directory = tempdir().expect("tempdir");
        let chapter = directory.path().join("chapters/第1章 既有");
        fs::create_dir_all(&chapter).expect("chapter");
        let writes = (1..=3)
            .map(|index| {
                let target = chapter.join(format!("{index:03}.md"));
                fs::write(&target, format!("before-{index}\n")).expect("before");
                (target, format!("after-{index}\n"))
            })
            .collect::<Vec<_>>();
        let mut publication = 0usize;
        let result = atomic_replace_many_with(&writes, |temporary, target| {
            publication += 1;
            if publication == 2 {
                return Err(io::Error::other("injected replacement failure"));
            }
            fs::rename(temporary, target)
        });
        assert!(result.is_err());
        for (index, (target, _)) in writes.iter().enumerate() {
            assert_eq!(
                fs::read_to_string(target).expect("restored target"),
                format!("before-{}\n", index + 1)
            );
        }
        assert!(
            fs::read_dir(&chapter)
                .expect("chapter entries")
                .filter_map(Result::ok)
                .all(|entry| {
                    let name = entry.file_name().to_string_lossy().to_string();
                    !name.contains("storydex-replace") && !name.contains("storydex-backup")
                })
        );
    }

    #[test]
    fn modify_existing_transaction_reports_incomplete_rollback_and_keeps_backup() {
        let directory = tempdir().expect("tempdir");
        let chapter = directory.path().join("chapters/第1章 既有");
        fs::create_dir_all(&chapter).expect("chapter");
        let writes = (1..=2)
            .map(|index| {
                let target = chapter.join(format!("{index:03}.md"));
                fs::write(&target, format!("before-{index}\n")).expect("before");
                (target, format!("after-{index}\n"))
            })
            .collect::<Vec<_>>();
        let first_target = writes[0].0.clone();
        let mut publication = 0usize;
        let error = atomic_replace_many_with(&writes, |temporary, target| {
            publication += 1;
            if publication == 2 {
                fs::remove_file(&first_target).expect("remove first published target");
                fs::create_dir(&first_target).expect("block first target restoration");
                fs::write(first_target.join("blocker"), "rollback blocker")
                    .expect("rollback blocker");
                return Err(io::Error::other("injected replacement failure"));
            }
            fs::rename(temporary, target)
        })
        .expect_err("replacement and rollback must fail");

        assert!(format!("{error:#}").contains("rollback incomplete"));
        assert_eq!(
            fs::read_to_string(&writes[1].0).expect("second target restored"),
            "before-2\n"
        );
        assert!(
            fs::read_dir(&chapter)
                .expect("chapter entries")
                .filter_map(Result::ok)
                .any(|entry| entry
                    .file_name()
                    .to_string_lossy()
                    .contains("storydex-backup"))
        );
    }

    #[test]
    fn create_new_target_is_programmatic_and_atomic() {
        let directory = tempdir().expect("tempdir");
        let chapters = directory.path().join("chapters");
        fs::create_dir_all(chapters.join("第2章 已有")).expect("existing chapter");
        let target = plan_create_new_targets(directory.path(), "default_chapter_directory", 1)
            .expect("planned target")
            .into_iter()
            .next()
            .expect("first target");
        let canonical_root = directory.path().canonicalize().expect("canonical root");
        assert_eq!(
            target
                .strip_prefix(canonical_root)
                .expect("relative target")
                .to_string_lossy()
                .replace('\\', "/"),
            "chapters/第3章 未命名/001.md"
        );
        atomic_create_many(&[(target.clone(), "正文\n".to_owned())]).expect("atomic create");
        assert_eq!(
            fs::read_to_string(&target).expect("read created file"),
            "正文\n"
        );
        assert!(atomic_create_many(&[(target.clone(), "replacement\n".to_owned())]).is_err());
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
