use anyhow::{Context, Result, ensure};
use chrono::{DateTime, Duration, Utc};
use fs2::FileExt;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};
use uuid::Uuid;

const CALIBRATION_VERSION: u64 = 2;
const PROMPT_VERSION: &str = "story_length_tier_v1";
const WORD_COUNT_SCOPE: &str = "candidate";
const ATTEMPT_KIND: &str = "initial";
const MIN_SAMPLES: usize = 12;
const MAX_SAMPLE_AGE_DAYS: i64 = 90;
const MAX_RECENT_SAMPLES: usize = 30;
const MAX_STORED_SAMPLES_PER_TIER: usize = 60;
const TIERS: [&str; 3] = ["short", "medium", "long"];

#[derive(Clone, Debug, Eq, PartialEq)]
struct Identity {
    provider: String,
    model: String,
    prompt_version: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ShortCalibrationSummary {
    pub(crate) status: String,
    pub(crate) preferred_minimum: usize,
    pub(crate) preferred_maximum: usize,
}

pub(crate) struct CalibrationSampleInput<'a> {
    pub(crate) provider: &'a str,
    pub(crate) model: &'a str,
    pub(crate) actual_word_count: usize,
    pub(crate) tier_hit: bool,
    pub(crate) structure_passed: bool,
    pub(crate) machine_quality_passed: bool,
    pub(crate) logical_prose_calls: u64,
    pub(crate) completion_tokens: Option<u64>,
    pub(crate) duration_ms: Option<u64>,
    pub(crate) trace_id: &'a str,
}

/// Read the preferred band for one supported story length tier.
pub(crate) fn read_tier_summary(
    workspace: &Path,
    provider: &str,
    model: &str,
    tier: &str,
) -> Result<ShortCalibrationSummary> {
    ensure!(
        TIERS.contains(&tier),
        "unsupported story length tier {tier}"
    );
    let root = workspace
        .canonicalize()
        .context("story calibration workspace is unavailable")?;
    let path = resolve_inside_workspace(&root, &calibration_path(&root))?;
    let payload = read_payload(&path)?;
    let identity = identity(provider, model, PROMPT_VERSION);
    let observation = compute_observation(&payload, &identity, Utc::now());
    let band = observation
        .get("bands")
        .and_then(Value::as_object)
        .and_then(|bands| bands.get(tier))
        .and_then(Value::as_array)
        .with_context(|| format!("{tier} calibration observation has no band"))?;
    let defaults = fixed_band(tier);
    let mut preferred_minimum =
        band.first().and_then(Value::as_u64).unwrap_or(defaults[0]) as usize;
    let mut preferred_maximum = band.get(1).and_then(Value::as_u64).unwrap_or(defaults[1]) as usize;
    preferred_minimum = preferred_minimum.max(1);
    preferred_maximum = preferred_maximum.max(1);
    if preferred_minimum > preferred_maximum {
        std::mem::swap(&mut preferred_minimum, &mut preferred_maximum);
    }
    Ok(ShortCalibrationSummary {
        status: observation
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("cold_start")
            .to_owned(),
        preferred_minimum,
        preferred_maximum,
    })
}

/// Record one candidate sample in the v2 calibration payload for `tier`.
pub(crate) fn record_tier_sample(
    workspace: &Path,
    tier: &str,
    input: CalibrationSampleInput<'_>,
) -> Result<bool> {
    ensure!(
        TIERS.contains(&tier),
        "unsupported story length tier {tier}"
    );
    if !input.structure_passed || input.actual_word_count == 0 {
        return Ok(false);
    }
    let root = workspace
        .canonicalize()
        .context("story calibration workspace is unavailable")?;
    let path = prepare_calibration_path(&root)?;
    let _lock = lock_calibration(&root)?;
    let mut payload = read_payload(&path)?;
    let identity = identity(input.provider, input.model, PROMPT_VERSION);
    let trace_id = input.trace_id.trim();
    if !trace_id.is_empty()
        && payload
            .get("samples")
            .and_then(Value::as_array)
            .is_some_and(|samples| {
                samples.iter().any(|sample| {
                    sample.get("traceId").and_then(Value::as_str) == Some(trace_id)
                        && sample.get("tier").and_then(Value::as_str) == Some(tier)
                        && sample.get("promptVersion").and_then(Value::as_str)
                            == Some(identity.prompt_version.as_str())
                })
            })
    {
        return Ok(false);
    }

    let now = Utc::now();
    let timestamp = now.to_rfc3339();
    let sample = json!({
        "sampleId": Uuid::new_v4().to_string(),
        "traceId": trace_id,
        "provider": identity.provider,
        "model": identity.model,
        "tier": tier,
        "promptVersion": identity.prompt_version,
        "wordCountScope": WORD_COUNT_SCOPE,
        "actualWordCount": input.actual_word_count,
        "tierHit": input.tier_hit,
        "structurePassed": true,
        "machineQualityPassed": input.machine_quality_passed,
        "attemptKind": ATTEMPT_KIND,
        "logicalProseCalls": input.logical_prose_calls,
        "completionTokens": input.completion_tokens,
        "durationMs": input.duration_ms,
        "timestamp": timestamp,
    });
    let samples = payload
        .get_mut("samples")
        .and_then(Value::as_array_mut)
        .context("story calibration samples are unavailable")?;
    samples.push(sample);
    *samples = trim_samples(samples);

    let observation = compute_observation(&payload, &identity, now);
    upsert_observation(&mut payload, &identity, &observation, &timestamp)?;
    payload["updatedAt"] = Value::String(Utc::now().to_rfc3339());
    write_payload_atomic(&path, &payload)?;
    Ok(true)
}

fn calibration_path(root: &Path) -> PathBuf {
    root.join(".storydex")
        .join("memory")
        .join("length_tier_calibration.json")
}

fn prepare_calibration_path(root: &Path) -> Result<PathBuf> {
    let path = resolve_inside_workspace(root, &calibration_path(root))?;
    let parent = path
        .parent()
        .context("story calibration path has no parent")?;
    fs::create_dir_all(parent)?;
    let canonical_parent = parent
        .canonicalize()
        .context("story calibration directory is unavailable")?;
    ensure!(
        canonical_parent.starts_with(root),
        "story calibration directory escaped workspace"
    );
    let path = canonical_parent.join("length_tier_calibration.json");
    resolve_inside_workspace(root, &path)
}

fn resolve_inside_workspace(root: &Path, path: &Path) -> Result<PathBuf> {
    let mut existing = path.to_path_buf();
    let mut missing = Vec::new();
    loop {
        match fs::symlink_metadata(&existing) {
            Ok(_) => break,
            Err(error) if error.kind() == ErrorKind::NotFound => {
                let name = existing
                    .file_name()
                    .context("story calibration path has no existing ancestor")?
                    .to_os_string();
                missing.push(name);
                ensure!(
                    existing.pop(),
                    "story calibration path has no existing ancestor"
                );
            }
            Err(error) => {
                return Err(error).with_context(|| {
                    format!(
                        "unable to inspect story calibration path {}",
                        existing.display()
                    )
                });
            }
        }
    }
    let mut resolved = existing.canonicalize().with_context(|| {
        format!(
            "unable to resolve story calibration path {}",
            existing.display()
        )
    })?;
    for name in missing.into_iter().rev() {
        resolved.push(name);
    }
    ensure!(
        resolved.starts_with(root),
        "story calibration path escaped workspace"
    );
    Ok(resolved)
}

fn lock_calibration(root: &Path) -> Result<std::fs::File> {
    let directory =
        resolve_inside_workspace(root, &root.join(".storydex").join(".agent").join("locks"))?;
    fs::create_dir_all(&directory)?;
    let directory = directory.canonicalize()?;
    ensure!(
        directory.starts_with(root),
        "story calibration lock directory escaped workspace"
    );
    let lock_path =
        resolve_inside_workspace(root, &directory.join("length_tier_calibration.lock"))?;
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(lock_path)?;
    FileExt::lock_exclusive(&file)?;
    Ok(file)
}

fn empty_payload() -> Value {
    json!({
        "_type": "StoryLengthTierCalibration",
        "_version": CALIBRATION_VERSION,
        "samples": [],
        "observations": [],
        "updatedAt": "",
    })
}

fn read_payload(path: &Path) -> Result<Value> {
    let bytes = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(empty_payload()),
        Err(error) => {
            return Err(error)
                .with_context(|| format!("unable to read story calibration {}", path.display()));
        }
    };
    let value = serde_json::from_slice::<Value>(&bytes)
        .with_context(|| format!("invalid story calibration JSON {}", path.display()))?;
    let Value::Object(raw) = value else {
        anyhow::bail!("story calibration payload must be an object");
    };
    ensure!(
        raw.get("_type").and_then(Value::as_str) == Some("StoryLengthTierCalibration"),
        "story calibration payload has an unsupported type"
    );
    ensure!(
        raw.get("_version").and_then(Value::as_u64) == Some(CALIBRATION_VERSION),
        "story calibration payload has an unsupported version"
    );
    ensure!(
        raw.get("samples").is_some_and(Value::is_array),
        "story calibration samples must be an array"
    );
    ensure!(
        raw.get("observations").is_some_and(Value::is_array),
        "story calibration observations must be an array"
    );
    let mut payload = empty_payload().as_object().cloned().unwrap_or_default();
    for (key, value) in &raw {
        payload.insert(key.clone(), value.clone());
    }
    payload.insert("_type".into(), json!("StoryLengthTierCalibration"));
    payload.insert("_version".into(), json!(CALIBRATION_VERSION));
    payload.insert(
        "samples".into(),
        Value::Array(
            raw.get("samples")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default(),
        ),
    );
    payload.insert(
        "observations".into(),
        Value::Array(
            raw.get("observations")
                .and_then(Value::as_array)
                .map(|values| {
                    values
                        .iter()
                        .filter(|value| {
                            value.get("wordCountScope").and_then(Value::as_str)
                                == Some(WORD_COUNT_SCOPE)
                        })
                        .cloned()
                        .collect()
                })
                .unwrap_or_default(),
        ),
    );
    Ok(Value::Object(payload))
}

fn identity(provider: &str, model: &str, prompt_version: &str) -> Identity {
    let provider = provider.trim().to_uppercase();
    let model = model.trim().to_lowercase();
    let prompt_version = prompt_version.trim();
    Identity {
        provider: if provider.is_empty() {
            "UNKNOWN".to_owned()
        } else {
            provider
        },
        model: if model.is_empty() {
            "unknown".to_owned()
        } else {
            model
        },
        prompt_version: if prompt_version.is_empty() {
            PROMPT_VERSION.to_owned()
        } else {
            prompt_version.to_owned()
        },
    }
}

fn sample_identity(value: &Value) -> Identity {
    identity(
        value.get("provider").and_then(Value::as_str).unwrap_or(""),
        value.get("model").and_then(Value::as_str).unwrap_or(""),
        value
            .get("promptVersion")
            .and_then(Value::as_str)
            .unwrap_or(""),
    )
}

fn compute_observation(payload: &Value, identity: &Identity, now: DateTime<Utc>) -> Value {
    let oldest = now - Duration::days(MAX_SAMPLE_AGE_DAYS);
    let mut values = BTreeMap::from([
        ("short", Vec::<(DateTime<Utc>, u64)>::new()),
        ("medium", Vec::new()),
        ("long", Vec::new()),
    ]);
    for sample in payload
        .get("samples")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        if sample_identity(sample) != *identity
            || sample.get("attemptKind").and_then(Value::as_str) != Some(ATTEMPT_KIND)
            || sample.get("wordCountScope").and_then(Value::as_str) != Some(WORD_COUNT_SCOPE)
            || sample.get("structurePassed").and_then(Value::as_bool) != Some(true)
            || sample.get("machineQualityPassed").and_then(Value::as_bool) != Some(true)
        {
            continue;
        }
        let Some(sample_time) = sample
            .get("timestamp")
            .and_then(Value::as_str)
            .and_then(parse_timestamp)
        else {
            continue;
        };
        if sample_time < oldest || sample_time > now {
            continue;
        }
        let tier = sample.get("tier").and_then(Value::as_str).unwrap_or("");
        let actual = sample
            .get("actualWordCount")
            .and_then(value_as_u64)
            .unwrap_or(0);
        if actual > 0
            && let Some(items) = values.get_mut(tier)
        {
            items.push((sample_time, actual));
        }
    }

    let mut recent = BTreeMap::<&str, Vec<u64>>::new();
    for tier in TIERS {
        let items = values.get_mut(tier).expect("known tier");
        items.sort_by_key(|(timestamp, _)| std::cmp::Reverse(*timestamp));
        recent.insert(
            tier,
            items
                .iter()
                .take(MAX_RECENT_SAMPLES)
                .map(|(_, value)| *value)
                .collect(),
        );
    }
    let counts = TIERS
        .into_iter()
        .map(|tier| (tier, recent.get(tier).map_or(0, Vec::len)))
        .collect::<BTreeMap<_, _>>();
    let medians = TIERS
        .into_iter()
        .map(|tier| {
            (
                tier,
                recent
                    .get(tier)
                    .and_then(|values| median(values.as_slice())),
            )
        })
        .collect::<BTreeMap<_, _>>();
    let enough = TIERS
        .into_iter()
        .all(|tier| counts.get(tier).copied().unwrap_or(0) >= MIN_SAMPLES);
    let separated = enough
        && medians.get("short").copied().flatten().unwrap_or(0)
            < medians.get("medium").copied().flatten().unwrap_or(0)
        && medians.get("medium").copied().flatten().unwrap_or(0)
            < medians.get("long").copied().flatten().unwrap_or(0);
    let (status, reason) = if !enough {
        ("cold_start", "insufficient_samples")
    } else if !separated {
        ("tiers_not_separated", "median_order_invalid")
    } else {
        ("applied", "p10_p90_observed")
    };
    let bands = TIERS
        .into_iter()
        .map(|tier| {
            let band = if status == "applied" {
                let mut ordered = recent.get(tier).cloned().unwrap_or_default();
                ordered.sort_unstable();
                [
                    (percentile(&ordered, 0.10) / 100.0).floor() as u64 * 100,
                    (percentile(&ordered, 0.90) / 100.0).ceil() as u64 * 100,
                ]
            } else {
                fixed_band(tier)
            };
            (tier, band)
        })
        .collect::<BTreeMap<_, _>>();
    json!({
        "status": status,
        "reason": reason,
        "sampleCounts": counts,
        "medians": medians,
        "bands": bands,
    })
}

fn median(values: &[u64]) -> Option<u64> {
    if values.is_empty() {
        return None;
    }
    let mut ordered = values.to_vec();
    ordered.sort_unstable();
    let middle = ordered.len() / 2;
    if ordered.len() % 2 == 1 {
        Some(ordered[middle])
    } else {
        Some((ordered[middle - 1] + ordered[middle]) / 2)
    }
}

fn percentile(values: &[u64], ratio: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    if values.len() == 1 {
        return values[0] as f64;
    }
    let position = (values.len() - 1) as f64 * ratio.clamp(0.0, 1.0);
    let lower = position.floor() as usize;
    let upper = position.ceil() as usize;
    if lower == upper {
        return values[lower] as f64;
    }
    let fraction = position - lower as f64;
    values[lower] as f64 + (values[upper] - values[lower]) as f64 * fraction
}

fn fixed_band(tier: &str) -> [u64; 2] {
    match tier {
        "medium" => [2_200, 5_000],
        "long" => [3_000, 6_000],
        _ => [1_000, 3_000],
    }
}

fn value_as_u64(value: &Value) -> Option<u64> {
    value
        .as_u64()
        .or_else(|| value.as_str().and_then(|text| text.parse().ok()))
}

fn parse_timestamp(value: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value)
        .ok()
        .map(|value| value.with_timezone(&Utc))
}

fn trim_samples(samples: &[Value]) -> Vec<Value> {
    let mut groups = BTreeMap::<String, Vec<Value>>::new();
    for sample in samples.iter().filter(|sample| sample.is_object()) {
        let identity = sample_identity(sample);
        let tier = sample.get("tier").and_then(Value::as_str).unwrap_or("");
        let scope = sample
            .get("wordCountScope")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_lowercase();
        let key = format!(
            "{}\0{}\0{}\0{}\0{}",
            identity.provider, identity.model, identity.prompt_version, scope, tier
        );
        groups.entry(key).or_default().push(sample.clone());
    }
    let mut kept = Vec::new();
    for group in groups.values_mut() {
        group.sort_by_key(|sample| {
            std::cmp::Reverse(
                sample
                    .get("timestamp")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
            )
        });
        kept.extend(group.iter().take(MAX_STORED_SAMPLES_PER_TIER).cloned());
    }
    kept.sort_by_key(|sample| {
        sample
            .get("timestamp")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned()
    });
    kept
}

fn upsert_observation(
    payload: &mut Value,
    identity: &Identity,
    observation: &Value,
    updated_at: &str,
) -> Result<()> {
    let observations = payload
        .get_mut("observations")
        .and_then(Value::as_array_mut)
        .context("story calibration observations are unavailable")?;
    let existing_index = observations.iter().position(|value| {
        sample_identity(value) == *identity
            && value.get("wordCountScope").and_then(Value::as_str) == Some(WORD_COUNT_SCOPE)
    });
    let mut existing = existing_index
        .and_then(|index| observations.get(index).cloned())
        .unwrap_or_else(|| {
            json!({
                "provider": identity.provider,
                "model": identity.model,
                "promptVersion": identity.prompt_version,
                "wordCountScope": WORD_COUNT_SCOPE,
                "calibrationVersion": 1,
            })
        });
    let is_new = existing_index.is_none();
    if !is_new {
        let changed = existing.get("status") != observation.get("status")
            || existing.get("bands") != observation.get("bands");
        if changed {
            existing["calibrationVersion"] = json!(
                existing
                    .get("calibrationVersion")
                    .and_then(value_as_u64)
                    .unwrap_or(0)
                    + 1
            );
        }
    }
    let object = existing
        .as_object_mut()
        .context("story calibration observation is not an object")?;
    let observation = observation
        .as_object()
        .context("computed story calibration observation is not an object")?;
    for (key, value) in observation {
        object.insert(key.clone(), value.clone());
    }
    object.insert("updatedAt".into(), Value::String(updated_at.to_owned()));
    if let Some(index) = existing_index {
        observations[index] = existing;
    } else {
        observations.push(existing);
    }
    Ok(())
}

fn write_payload_atomic(path: &Path, payload: &Value) -> Result<()> {
    let parent = path
        .parent()
        .context("story calibration path has no parent")?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("length_tier_calibration.json"),
        Uuid::new_v4()
    ));
    let mut bytes = serde_json::to_vec_pretty(payload)?;
    bytes.push(b'\n');
    let result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)?;
        file.write_all(&bytes)?;
        file.flush()?;
        file.sync_all()?;
        drop(file);
        if path.exists() {
            let backup = parent.join(format!(
                ".{}.{}.bak",
                path.file_name()
                    .and_then(|value| value.to_str())
                    .unwrap_or("length_tier_calibration.json"),
                Uuid::new_v4()
            ));
            fs::rename(path, &backup)?;
            match fs::rename(&temporary, path) {
                Ok(()) => {
                    fs::remove_file(backup)?;
                    Ok(())
                }
                Err(error) => {
                    let _ = fs::rename(backup, path);
                    Err(error.into())
                }
            }
        } else {
            fs::rename(&temporary, path).map_err(Into::into)
        }
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

    fn input<'a>(trace_id: &'a str, actual_word_count: usize) -> CalibrationSampleInput<'a> {
        CalibrationSampleInput {
            provider: "opencode",
            model: "DeepSeek-V4-Flash",
            actual_word_count,
            tier_hit: true,
            structure_passed: true,
            machine_quality_passed: true,
            logical_prose_calls: 1,
            completion_tokens: Some(980),
            duration_ms: Some(12),
            trace_id,
        }
    }

    #[test]
    fn records_candidate_sample_and_rejects_duplicate_trace() {
        let directory = tempdir().expect("tempdir");
        assert!(
            record_tier_sample(directory.path(), "short", input("trace-1", 1519)).expect("record")
        );
        assert!(
            !record_tier_sample(directory.path(), "short", input("trace-1", 1519))
                .expect("duplicate")
        );
        let payload = read_payload(&calibration_path(directory.path())).expect("payload");
        assert_eq!(payload["_version"], 2);
        assert_eq!(payload["samples"].as_array().map(Vec::len), Some(1));
        assert_eq!(payload["samples"][0]["provider"], "OPENCODE");
        assert_eq!(payload["samples"][0]["wordCountScope"], "candidate");
        assert_eq!(payload["observations"][0]["status"], "cold_start");
        assert_eq!(payload["observations"][0]["sampleCounts"]["short"], 1);

        let summary = read_tier_summary(directory.path(), "OPENCODE", "deepseek-v4-flash", "short")
            .expect("summary");
        assert_eq!(summary.status, "cold_start");
        assert_eq!(summary.preferred_minimum, 1_000);
        assert_eq!(summary.preferred_maximum, 3_000);
    }

    #[test]
    fn records_and_reads_medium_and_long_samples_without_cross_tier_deduplication() {
        let directory = tempdir().expect("tempdir");
        assert!(
            record_tier_sample(directory.path(), "medium", input("same-trace", 2_800))
                .expect("medium record")
        );
        assert!(
            !record_tier_sample(directory.path(), "medium", input("same-trace", 2_800))
                .expect("medium duplicate")
        );
        assert!(
            record_tier_sample(directory.path(), "long", input("same-trace", 4_400))
                .expect("long record")
        );

        let medium = read_tier_summary(directory.path(), "OPENCODE", "deepseek-v4-flash", "medium")
            .expect("medium summary");
        let long = read_tier_summary(directory.path(), "OPENCODE", "deepseek-v4-flash", "long")
            .expect("long summary");
        assert_eq!(medium.preferred_minimum, 2_200);
        assert_eq!(medium.preferred_maximum, 5_000);
        assert_eq!(long.preferred_minimum, 3_000);
        assert_eq!(long.preferred_maximum, 6_000);
    }

    #[test]
    fn corrupt_payload_is_rejected_without_overwrite() {
        let directory = tempdir().expect("tempdir");
        let path = calibration_path(directory.path());
        fs::create_dir_all(path.parent().expect("parent")).expect("calibration parent");
        fs::write(&path, b"{not-json").expect("corrupt calibration");

        assert!(read_tier_summary(directory.path(), "OPENCODE", "model", "short").is_err());
        assert!(record_tier_sample(directory.path(), "short", input("trace-1", 1_519)).is_err());
        assert_eq!(
            fs::read(&path).expect("preserved calibration"),
            b"{not-json"
        );
    }

    #[test]
    fn cold_start_bands_match_python_policy() {
        let identity = identity("TEST", "model", PROMPT_VERSION);
        let observation = compute_observation(&empty_payload(), &identity, Utc::now());
        assert_eq!(observation["bands"]["short"], json!([1_000, 3_000]));
        assert_eq!(observation["bands"]["medium"], json!([2_200, 5_000]));
        assert_eq!(observation["bands"]["long"], json!([3_000, 6_000]));
    }

    #[test]
    fn applies_separated_observed_bands_after_twelve_samples_per_tier() {
        let now = Utc::now();
        let identity = identity("TEST", "model", PROMPT_VERSION);
        let samples = [
            ("short", 1_200_u64),
            ("medium", 2_800_u64),
            ("long", 4_400_u64),
        ]
        .into_iter()
        .flat_map(|(tier, start)| {
            (0..12).map(move |index| {
                json!({
                    "provider": "TEST",
                    "model": "model",
                    "tier": tier,
                    "promptVersion": PROMPT_VERSION,
                    "wordCountScope": WORD_COUNT_SCOPE,
                    "actualWordCount": start + index * 100,
                    "tierHit": true,
                    "structurePassed": true,
                    "machineQualityPassed": true,
                    "attemptKind": ATTEMPT_KIND,
                    "timestamp": (now - Duration::seconds(index as i64)).to_rfc3339(),
                })
            })
        })
        .collect::<Vec<_>>();
        let observation = compute_observation(
            &json!({"samples": samples, "observations": []}),
            &identity,
            now,
        );
        assert_eq!(observation["status"], "applied");
        assert_eq!(
            observation["sampleCounts"],
            json!({"short": 12, "medium": 12, "long": 12})
        );
        assert_eq!(observation["bands"]["short"], json!([1_300, 2_200]));
        assert_eq!(observation["bands"]["medium"], json!([2_900, 3_800]));
        assert_eq!(observation["bands"]["long"], json!([4_500, 5_400]));
    }
}
