//! Storydex project primitives shared by the Rust backend.
//!
//! This module deliberately stops at the project boundary.  It does not own
//! HTTP routes or the WIKI graph builder; it provides the deterministic file
//! and Git contracts those layers use.  Keeping the primitives here makes it
//! possible to migrate callers incrementally without introducing a second
//! source of truth.

use anyhow::{Context, Result, bail, ensure};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};
#[cfg(unix)]
use std::fs::File;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

const WIKI_ROOT: &str = ".storydex/wiki";
const INTERNAL_PATH_PREFIXES: [&str; 2] = [".storydex/.agent/", ".storydex/.cache/"];
const DEFAULT_BRANCH: &str = "develop";
const DIFF_MAX_LINES: usize = 2_000;
const HISTORY_LIMIT: usize = 24;
const GRAPH_CHECKSUM_VOLATILE_KEYS: [&str; 12] = [
    "generatedAt",
    "lastUpdatedAt",
    "updatedAt",
    "mtime",
    "lastAnalyzedAt",
    "x",
    "y",
    "fx",
    "fy",
    "vx",
    "vy",
    "layout",
];

/// A source entry used by the WIKI projection checksum.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ProjectionSource {
    pub relative_path: String,
    pub sha256: String,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub size: u64,
    #[serde(default)]
    pub mtime: String,
}

/// The generated WIKI bundle and optional source/change snapshots are written
/// as one transaction.  Callers build the semantic payload; this type only
/// handles deterministic serialization and replacement.
#[derive(Clone, Debug, Default)]
pub struct ProjectionBundle {
    pub payload: Value,
    pub markdown: String,
    pub index: Value,
    pub status: Value,
    pub source_snapshot: Option<Value>,
    /// Optional revision/change-set metadata persisted alongside the bundle.
    /// Existing callers may leave this unset; the Rust sync path uses it to
    /// make revision transitions and domain events durable and inspectable.
    pub change_set: Option<Value>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProjectionWriteResult {
    pub changed_paths: Vec<String>,
}

/// Return compact, recursively key-sorted JSON bytes.
///
/// Python's projection implementation uses `sort_keys=True` and compact
/// separators for checksums.  `serde_json::Value` retains insertion order, so
/// we explicitly rebuild objects as `BTreeMap`s before serializing.
pub fn canonical_json_bytes(value: &Value) -> Result<Vec<u8>> {
    let canonical = canonical_value(value, false);
    serde_json::to_vec(&canonical).context("failed to encode canonical JSON")
}

/// Resolve an existing candidate path and fail closed unless it remains under
/// the canonical boundary root.  This handles Windows drive-letter casing and
/// separators while still rejecting sibling-prefix and junction escapes.
pub fn resolve_bounded_path(
    boundary_root: impl AsRef<Path>,
    candidate: impl AsRef<Path>,
) -> Result<PathBuf> {
    let boundary = canonical_existing_dir(boundary_root.as_ref())?;
    let candidate = candidate.as_ref().canonicalize().with_context(|| {
        format!(
            "bounded path does not exist: {}",
            candidate.as_ref().display()
        )
    })?;
    ensure!(
        path_is_within(&boundary, &candidate),
        "path escapes the configured Storydex boundary: {}",
        candidate.display()
    );
    Ok(candidate)
}

/// Calculate the checksum of the source set used to build a WIKI projection.
pub fn source_set_checksum(sources: &[ProjectionSource]) -> Result<String> {
    let mut canonical = sources
        .iter()
        .map(|source| {
            json!({
                "relativePath": source.relative_path.replace('\\', "/"),
                "sha256": source.sha256,
                "kind": source.kind,
            })
        })
        .collect::<Vec<_>>();
    canonical.sort_by(|left, right| {
        let key = |value: &Value| {
            (
                value
                    .get("relativePath")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_owned(),
                value
                    .get("kind")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_owned(),
                value
                    .get("sha256")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_owned(),
            )
        };
        key(left).cmp(&key(right))
    });
    let bytes = canonical_json_bytes(&Value::Array(canonical))?;
    Ok(format!("sha256:{}", hex_digest(&bytes)))
}

/// Calculate the stable graph checksum used by the Python WIKI service.
/// Volatile layout/timestamp keys are removed recursively, while entries,
/// nodes and edges are sorted by their semantic identifiers.
pub fn graph_checksum(payload: &Value) -> Result<String> {
    let entries = sorted_objects(payload.get("entries"), |item| {
        item.get("id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned()
    });
    let graph = payload.get("graph").and_then(Value::as_object);
    let nodes = sorted_objects(graph.and_then(|value| value.get("nodes")), |item| {
        item.get("id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned()
    });
    let edges = {
        let mut values = objects(graph.and_then(|value| value.get("edges")));
        values.sort_by_key(edge_sort_key);
        values
    };
    let canonical = json!({
        "entries": entries,
        "nodes": nodes,
        "edges": edges,
    });
    let bytes = canonical_json_bytes(&canonical)?;
    Ok(format!("sha256:{}", hex_digest(&bytes)))
}

/// Writer for the generated files under `.storydex/wiki`.
#[derive(Clone, Debug)]
pub struct ProjectionBundleWriter {
    project_root: PathBuf,
}

impl ProjectionBundleWriter {
    pub fn new(project_root: impl AsRef<Path>) -> Result<Self> {
        let project_root = canonical_existing_dir(project_root.as_ref())?;
        Ok(Self { project_root })
    }

    pub fn project_root(&self) -> &Path {
        &self.project_root
    }

    pub fn wiki_root(&self) -> PathBuf {
        self.project_root.join(WIKI_ROOT)
    }

    pub fn status_path(&self) -> PathBuf {
        self.wiki_root().join("projection_status.json")
    }

    pub fn read_status(&self) -> Result<Option<Value>> {
        read_json_if_present(&self.status_path())
    }

    pub fn write(&self, bundle: &ProjectionBundle) -> Result<ProjectionWriteResult> {
        // Keep the same observable order as Python's projection writer:
        // graph, markdown, index, status, then the optional source snapshot.
        let mut targets = vec![
            (
                self.wiki_root().join("knowledge_graph.json"),
                pretty_json_bytes(&bundle.payload)?,
            ),
            (
                self.wiki_root().join("WIKI.md"),
                bundle.markdown.as_bytes().to_vec(),
            ),
            (
                self.wiki_root().join("index.json"),
                pretty_json_bytes(&bundle.index)?,
            ),
            (self.status_path(), pretty_json_bytes(&bundle.status)?),
        ];
        if let Some(snapshot) = &bundle.source_snapshot {
            targets.push((
                self.wiki_root().join("source_snapshot.json"),
                pretty_json_bytes(snapshot)?,
            ));
        }
        if let Some(change_set) = &bundle.change_set {
            targets.push((
                self.wiki_root().join("change_set.json"),
                pretty_json_bytes(change_set)?,
            ));
        }
        self.write_targets(targets)
    }

    /// Atomically update only the projection status sidecar.  This is used on
    /// failed rebuilds so the previous graph remains the last-good projection
    /// while callers still get an explicit stale/error state and diagnostics.
    pub fn write_status(&self, status: &Value) -> Result<ProjectionWriteResult> {
        self.write_targets(vec![(self.status_path(), pretty_json_bytes(status)?)])
    }

    pub fn read_change_set(&self) -> Result<Option<Value>> {
        read_json_if_present(&self.wiki_root().join("change_set.json"))
    }

    fn write_targets(&self, targets: Vec<(PathBuf, Vec<u8>)>) -> Result<ProjectionWriteResult> {
        let root = self.project_root.canonicalize()?;
        let mut originals = BTreeMap::<PathBuf, Option<Vec<u8>>>::new();
        let mut temporary_paths = Vec::<(PathBuf, PathBuf)>::new();
        let mut committed = Vec::<PathBuf>::new();
        let mut changed_paths = Vec::new();

        let result = (|| -> Result<()> {
            for (target, content) in &targets {
                ensure_inside(&root, target)?;
                if let Some(parent) = target.parent() {
                    fs::create_dir_all(parent)
                        .with_context(|| format!("failed to create {}", parent.display()))?;
                }
                let original = read_bytes_if_present(target)?;
                if original.as_deref() == Some(content.as_slice()) {
                    continue;
                }
                originals.insert(target.clone(), original);
                let temporary = temporary_path(target)?;
                let mut file = OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(&temporary)
                    .with_context(|| format!("failed to create {}", temporary.display()))?;
                file.write_all(content)
                    .with_context(|| format!("failed to write {}", temporary.display()))?;
                file.flush()?;
                file.sync_all()?;
                temporary_paths.push((target.clone(), temporary));
            }
            for (target, temporary) in &temporary_paths {
                // Mark the target before replacement.  On Windows the helper
                // may remove an existing destination before `rename`; if the
                // rename then fails, rollback must restore that destination
                // too.
                committed.push(target.clone());
                replace_path(temporary, target)
                    .with_context(|| format!("failed to replace {}", target.display()))?;
                changed_paths.push(relative_to_project(&root, target));
            }
            sync_directory(&self.wiki_root())?;
            Ok(())
        })();

        if let Err(error) = result {
            for target in committed.iter().rev() {
                if let Some(original) = originals.get(target).and_then(Clone::clone) {
                    let restore = temporary_path_with_suffix(target, ".restore")?;
                    write_and_sync(&restore, &original)?;
                    let _ = replace_path(&restore, target);
                    let _ = fs::remove_file(&restore);
                } else {
                    let _ = fs::remove_file(target);
                }
            }
            for (_, temporary) in &temporary_paths {
                let _ = fs::remove_file(temporary);
            }
            return Err(error.context("projection bundle transaction rolled back"));
        }
        for (_, temporary) in &temporary_paths {
            let _ = fs::remove_file(temporary);
        }
        Ok(ProjectionWriteResult { changed_paths })
    }
}

/// Deterministic, local Story Knowledge/WIKI synchronizer used by the Rust
/// candidate.  It intentionally keeps the projection format small and
/// evidence based: every entry is backed by one source path and every
/// revision is derived from the canonical source-set checksum.  The service
/// does not call a provider and therefore works for cold builds, no-op reads,
/// incremental updates and failure recovery alike.
#[derive(Clone, Debug)]
pub struct StorydexKnowledge {
    project_root: PathBuf,
}

impl StorydexKnowledge {
    pub fn new(project_root: impl AsRef<Path>) -> Result<Self> {
        Ok(Self {
            project_root: canonical_existing_dir(project_root.as_ref())?,
        })
    }

    pub fn project_root(&self) -> &Path {
        &self.project_root
    }

    pub fn sync(&self, force: bool) -> Result<Value> {
        let writer = ProjectionBundleWriter::new(&self.project_root)?;
        let sources = self.collect_sources()?;
        let source_snapshot = self.source_snapshot(&sources);
        let existing = read_json_if_present(&writer.wiki_root().join("knowledge_graph.json"))?;
        let previous_snapshot =
            read_json_if_present(&writer.wiki_root().join("source_snapshot.json"))?;
        let previous_status = writer.read_status()?.unwrap_or_else(|| json!({}));
        let previous_revision = previous_revision(existing.as_ref(), Some(&previous_status));
        let diff = source_change_set(
            previous_snapshot.as_ref(),
            &source_snapshot,
            previous_revision,
        )?;
        let existing_ready = existing
            .as_ref()
            .and_then(Value::as_object)
            .is_some_and(|value| {
                value
                    .get("status")
                    .and_then(Value::as_str)
                    .unwrap_or("ready")
                    == "ready"
            })
            && previous_status
                .get("status")
                .and_then(Value::as_str)
                .is_none_or(|status| status == "ready");
        if !force
            && existing_ready
            && diff["changedSourcePaths"]
                .as_array()
                .is_some_and(Vec::is_empty)
        {
            let mut result = existing.unwrap_or_else(|| json!({}));
            if let Some(object) = result.as_object_mut() {
                object.insert("status".to_owned(), Value::String("ready".to_owned()));
                object.insert(
                    "projectionFreshness".to_owned(),
                    Value::String("fresh".to_owned()),
                );
                object.insert("noChanges".to_owned(), Value::Bool(true));
                object.insert("changedSourcePaths".to_owned(), json!([]));
                object.insert("changeSet".to_owned(), diff.clone());
                object.insert(
                    "event".to_owned(),
                    json!({
                        "type": "KnowledgeProjectionSynchronized",
                        "revision": object.get("knowledgeRevision").cloned().unwrap_or(json!(0)),
                        "baseRevision": diff.get("baseRevision").cloned().unwrap_or(json!(0)),
                        "changeSetId": diff.get("changeSetId").cloned().unwrap_or(Value::Null),
                        "sourceSetChecksum": object.get("sourceSetChecksum").cloned().unwrap_or(Value::Null),
                        "graphChecksum": object.get("graphChecksum").cloned().unwrap_or(Value::Null),
                        "noChanges": true,
                        "occurredAt": now_rfc3339(),
                    }),
                );
            }
            return Ok(result);
        }

        let source_checksum = source_set_checksum(&sources)?;
        let changed = diff
            .get("changedSourcePaths")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let next_revision = if previous_revision == 0 || !changed.is_empty() {
            previous_revision + 1
        } else {
            previous_revision
        };
        let mut payload = self.build_payload(&sources, next_revision, &source_checksum)?;
        let graph = graph_checksum(&payload)?;
        if let Some(object) = payload.as_object_mut() {
            object.insert("graphChecksum".to_owned(), Value::String(graph.clone()));
            object.insert(
                "sourceSetChecksum".to_owned(),
                Value::String(source_checksum.clone()),
            );
            object.insert("knowledgeRevision".to_owned(), json!(next_revision));
            object.insert("builtFromRevision".to_owned(), json!(next_revision));
            object.insert("lastSuccessfulRevision".to_owned(), json!(next_revision));
            object.insert(
                "projectionFreshness".to_owned(),
                Value::String("fresh".to_owned()),
            );
            object.insert("noChanges".to_owned(), Value::Bool(false));
            object.insert(
                "changedSourcePaths".to_owned(),
                Value::Array(changed.clone()),
            );
            object.insert("changeSet".to_owned(), diff.clone());
            object.insert(
                "event".to_owned(),
                json!({
                    "type": "KnowledgeProjectionUpdated",
                    "revision": next_revision,
                    "baseRevision": diff.get("baseRevision").cloned().unwrap_or(json!(0)),
                    "changeSetId": diff.get("changeSetId").cloned().unwrap_or(Value::Null),
                    "sourceSetChecksum": source_checksum,
                    "graphChecksum": graph,
                    "changedSourcePaths": changed,
                    "noChanges": false,
                    "mode": if force { "rebuild" } else { "sync" },
                    "occurredAt": now_rfc3339(),
                }),
            );
        }
        let status = json!({
            "schemaVersion": 3,
            "status": "ready",
            "knowledgeRevision": next_revision,
            "builtFromRevision": next_revision,
            "lastSuccessfulRevision": next_revision,
            "sourceSetChecksum": source_checksum,
            "graphChecksum": graph,
            "projectionFreshness": "fresh",
            "diagnostics": [],
            "updatedAt": now_rfc3339(),
        });
        let index = self.build_index(&sources, &payload, &status, &diff);
        let markdown = self.render_markdown(&payload);
        match writer.write(&ProjectionBundle {
            payload: payload.clone(),
            markdown,
            index,
            status,
            source_snapshot: Some(source_snapshot),
            change_set: Some(diff),
        }) {
            Ok(_) => Ok(payload),
            Err(error) => {
                let last_good = previous_revision;
                let failure = json!({
                    "schemaVersion": 3,
                    "status": "error",
                    "projectionFreshness": "stale",
                    "knowledgeRevision": existing.as_ref().and_then(|value| value.get("knowledgeRevision")).cloned().unwrap_or(json!(last_good)),
                    "builtFromRevision": existing.as_ref().and_then(|value| value.get("builtFromRevision")).cloned().unwrap_or(json!(last_good)),
                    "lastSuccessfulRevision": last_good,
                    "sourceSetChecksum": existing.as_ref().and_then(|value| value.get("sourceSetChecksum")).cloned().unwrap_or(Value::Null),
                    "attemptedSourceSetChecksum": source_checksum,
                    "diagnostics": [{"code": "wiki.write_failed", "severity": "error", "message": error.to_string()}],
                    "updatedAt": now_rfc3339(),
                });
                let _ = writer.write_status(&failure);
                Err(error
                    .context("Rust WIKI projection write failed; last-good projection preserved"))
            }
        }
    }

    fn collect_sources(&self) -> Result<Vec<ProjectionSource>> {
        let mut files = Vec::new();
        collect_source_files(&self.project_root, &self.project_root, &mut files)?;
        files.sort_by(|left, right| left.relative_path.cmp(&right.relative_path));
        Ok(files)
    }

    fn source_snapshot(&self, sources: &[ProjectionSource]) -> Value {
        json!({
            "version": 1,
            "updatedAt": now_rfc3339(),
            "sources": sources.iter().map(|source| (
                source.relative_path.clone(),
                serde_json::to_value(source).unwrap_or_else(|_| json!({}))
            )).collect::<Map<_, _>>(),
        })
    }

    fn build_payload(
        &self,
        sources: &[ProjectionSource],
        revision: u64,
        checksum: &str,
    ) -> Result<Value> {
        let project_name = self
            .project_root
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("Storydex");
        let chapter_count = sources
            .iter()
            .filter(|source| source.kind == "chapter")
            .count();
        let character_count = sources
            .iter()
            .filter(|source| source.kind == "character")
            .count();
        let overview_summary = if sources.is_empty() {
            format!("《{project_name}》暂无故事内容，创建章节/角色后图谱将自动构建。")
        } else {
            format!(
                "覆盖 {chapter_count} 个正文章节/片段、{character_count} 个角色来源与 {} 个可索引文件。",
                sources.len()
            )
        };
        let mut entries = vec![json!({
            "id": "overview:project",
            "title": "项目总览",
            "category": "overview",
            "categoryLabel": "项目总览",
            "summary": overview_summary,
            "details": [format!("工作区: {}", normalized_path(&self.project_root))],
            "sourcePaths": [],
            "knowledgeStatus": "observed",
        })];
        let mut nodes = vec![json!({
            "id": "project:root",
            "label": project_name,
            "type": "project",
            "category": "overview",
            "entryId": "overview:project",
            "summary": format!("{project_name} 项目知识库"),
        })];
        let mut edges = Vec::new();
        let mut previous_chapter_id = None::<String>;
        let mut chapter_order = 0usize;
        for source in sources {
            let title = if source.title.trim().is_empty() {
                Path::new(&source.relative_path)
                    .file_stem()
                    .and_then(|value| value.to_str())
                    .unwrap_or("未命名")
                    .to_owned()
            } else {
                source.title.clone()
            };
            let id = format!(
                "source:{}",
                hex_digest(source.relative_path.as_bytes())[..16].to_owned()
            );
            let kind = source.kind.as_str();
            let node_type = match kind {
                "chapter" => "chapter",
                "character" => "character",
                "planned" => "event",
                "world" | "setting" => "setting",
                _ => "document",
            };
            let category = match kind {
                "character" => "characters",
                "chapter" | "planned" => "plot",
                _ => "setting",
            };
            let summary = compress_source_text(&source.text, 260);
            entries.push(json!({
                "id": id,
                "title": title,
                "category": category,
                "categoryLabel": category_label(category),
                "summary": summary,
                "details": [format!("来源: {}", source.relative_path)],
                "sourcePaths": [source.relative_path],
                "knowledgeStatus": "observed",
            }));
            if kind == "chapter" {
                chapter_order += 1;
            }
            nodes.push(json!({
                "id": id,
                "label": title,
                "type": node_type,
                "category": category,
                "entryId": id,
                "summary": summary,
                "knowledgeStatus": "observed",
                "narrativeOrder": if kind == "chapter" { chapter_order } else { 0 },
            }));
            if kind == "chapter" {
                if let Some(previous_id) = previous_chapter_id.as_ref() {
                    edges.push(json!({"source": previous_id, "target": id, "type": "timeline", "label": "承接"}));
                }
                previous_chapter_id = Some(id);
            }
        }
        Ok(json!({
            "version": 1,
            "schemaVersion": 3,
            "categorySchemaVersion": "story-wiki-rust-v1",
            "projectName": project_name,
            "workspaceRoot": normalized_path(&self.project_root),
            "generatedAt": now_rfc3339(),
            "generator": "storydex-rust-knowledge",
            "generationMode": "local evidence-grounded",
            "llmStatus": "not_required",
            "summary": overview_summary,
            "categoryLabels": {"overview": "项目总览", "characters": "角色", "plot": "剧情", "setting": "设定"},
            "nodeTypeLabels": {"project": "项目", "chapter": "章节", "character": "角色", "event": "事件", "setting": "设定", "document": "文档"},
            "graphPolicy": {"mode": "evidence_grounded_local_v1", "agentGraphAccepted": false, "coOccurrenceIsRelationship": false},
            "status": "ready",
            "projectionFreshness": "fresh",
            "knowledgeRevision": revision,
            "builtFromRevision": revision,
            "lastSuccessfulRevision": revision,
            "sourceSetChecksum": checksum,
            "entries": entries,
            "graph": {"nodes": nodes, "edges": edges},
            "sourceStats": {"scannedFiles": sources.len(), "chapterFiles": chapter_count, "characters": character_count},
            "diagnostics": [],
        }))
    }

    fn build_index(
        &self,
        sources: &[ProjectionSource],
        payload: &Value,
        status: &Value,
        change_set: &Value,
    ) -> Value {
        let mut source_index = Map::new();
        for source in sources {
            let related_id = format!(
                "source:{}",
                hex_digest(source.relative_path.as_bytes())[..16].to_owned()
            );
            source_index.insert(
                source.relative_path.clone(),
                json!({
                    "sha256": source.sha256,
                    "kind": source.kind,
                    "size": source.size,
                    "mtime": source.mtime,
                    "lastAnalyzedAt": now_rfc3339(),
                    "relatedEntryIds": [related_id],
                    "relatedNodeIds": [related_id],
                }),
            );
        }
        json!({
            "version": 2,
            "schemaVersion": 3,
            "projectName": self.project_root.file_name().and_then(|value| value.to_str()).unwrap_or("Storydex"),
            "status": status.get("status").cloned().unwrap_or(json!("ready")),
            "knowledgeRevision": payload.get("knowledgeRevision").cloned().unwrap_or(json!(0)),
            "builtFromRevision": payload.get("builtFromRevision").cloned().unwrap_or(json!(0)),
            "lastSuccessfulRevision": payload.get("lastSuccessfulRevision").cloned().unwrap_or(json!(0)),
            "sourceSetChecksum": payload.get("sourceSetChecksum").cloned().unwrap_or(Value::Null),
            "graphChecksum": payload.get("graphChecksum").cloned().unwrap_or(Value::Null),
            "changedSourcePaths": change_set.get("changedSourcePaths").cloned().unwrap_or(json!([])),
            "sourceStats": payload.get("sourceStats").cloned().unwrap_or_else(|| json!({})),
            "entryCount": payload.get("entries").and_then(Value::as_array).map(Vec::len).unwrap_or(0),
            "nodeCount": payload.get("graph").and_then(|value| value.get("nodes")).and_then(Value::as_array).map(Vec::len).unwrap_or(0),
            "edgeCount": payload.get("graph").and_then(|value| value.get("edges")).and_then(Value::as_array).map(Vec::len).unwrap_or(0),
            "sources": source_index,
        })
    }

    fn render_markdown(&self, payload: &Value) -> String {
        let mut output = format!(
            "# {}\n\n",
            payload
                .get("projectName")
                .and_then(Value::as_str)
                .unwrap_or("Storydex WIKI")
        );
        if let Some(entries) = payload.get("entries").and_then(Value::as_array) {
            for entry in entries {
                let title = entry
                    .get("title")
                    .and_then(Value::as_str)
                    .unwrap_or("未命名");
                let summary = entry.get("summary").and_then(Value::as_str).unwrap_or("");
                output.push_str(&format!("## {title}\n\n{summary}\n\n"));
            }
        }
        output
    }
}

fn collect_source_files(
    root: &Path,
    directory: &Path,
    output: &mut Vec<ProjectionSource>,
) -> Result<()> {
    for entry in fs::read_dir(directory)
        .with_context(|| format!("failed to read {}", directory.display()))?
    {
        let entry = entry?;
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() {
            bail!(
                "symlinked Storydex source is not accepted: {}",
                path.display()
            );
        }
        let name = entry.file_name().to_string_lossy().to_string();
        if matches!(
            name.as_str(),
            ".git" | "__pycache__" | ".cache" | "traces" | "sessions" | "target"
        ) {
            continue;
        }
        let relative = path
            .strip_prefix(root)
            .unwrap_or(&path)
            .to_string_lossy()
            .replace('\\', "/");
        if relative.starts_with(".storydex/wiki/")
            || relative.starts_with(".storydex/.agent/")
            || relative.starts_with(".storydex/.cache/")
            || relative.starts_with(".storydex/config/")
            || relative.starts_with(".storydex/templates/")
            || relative.starts_with(".storydex/presets/")
            || relative.starts_with(".storydex/temp/")
            || relative.starts_with(".storydex/memory/backups/")
        {
            continue;
        }
        if path.is_dir() {
            collect_source_files(root, &path, output)?;
            continue;
        }
        let extension = path
            .extension()
            .and_then(|value| value.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        if !matches!(extension.as_str(), "md" | "txt" | "json" | "jsonl") {
            continue;
        }
        if name.eq_ignore_ascii_case("README.md")
            || relative.to_ascii_lowercase().ends_with(".relations.md")
        {
            continue;
        }
        let bytes = fs::read(&path)?;
        let raw_text = String::from_utf8(bytes.clone())
            .with_context(|| format!("Storydex source is not valid UTF-8: {}", path.display()))?;
        if raw_text.is_empty() {
            continue;
        }
        let text = if extension == "json" {
            match serde_json::from_str::<Value>(&raw_text) {
                Ok(value) => serde_json::to_string_pretty(&value)
                    .context("failed to normalize JSON Storydex source")?,
                Err(_) => normalize_source_text(&raw_text),
            }
        } else {
            normalize_source_text(&raw_text)
        };
        let kind =
            if relative.starts_with("chapters/") && matches!(extension.as_str(), "md" | "txt") {
                "chapter"
            } else if relative.starts_with(".storydex/scripts/") {
                "planned"
            } else if is_character_card_path(&relative) {
                "character"
            } else if relative.contains("/characters/") || relative.starts_with("characters/") {
                "memory"
            } else if relative.contains("/worldbook/") || relative.starts_with("worldbook/") {
                "world"
            } else if relative.contains("/presets/") || relative.starts_with("presets/") {
                "preset"
            } else if relative.contains("/memory/") || relative.starts_with("memory/") {
                "memory"
            } else {
                "project"
            };
        let title = path
            .file_stem()
            .and_then(|value| value.to_str())
            .unwrap_or("未命名")
            .to_owned();
        output.push(ProjectionSource {
            relative_path: relative,
            sha256: hex_digest(text.as_bytes()),
            kind: kind.to_owned(),
            title,
            text,
            size: metadata.len(),
            mtime: metadata
                .modified()
                .map(DateTime::<Utc>::from)
                .map(|value| value.to_rfc3339())
                .unwrap_or_default(),
        });
    }
    Ok(())
}

fn is_character_card_path(relative_path: &str) -> bool {
    let normalized = relative_path.replace('\\', "/");
    if normalized.to_ascii_lowercase().ends_with(".relations.md") {
        return false;
    }
    let parts = normalized
        .split('/')
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>();
    let Some(index) = parts.iter().position(|part| *part == "characters") else {
        return false;
    };
    let tail = &parts[index + 1..];
    if tail.len() == 1 {
        return matches!(
            Path::new(tail[0])
                .extension()
                .and_then(|value| value.to_str())
                .unwrap_or("")
                .to_ascii_lowercase()
                .as_str(),
            "md" | "txt" | "json" | "jsonl"
        );
    }
    tail.len() == 2 && tail[0] == "cards"
}

fn compress_source_text(text: &str, limit: usize) -> String {
    let normalized = text.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut chars = normalized.chars();
    let prefix = chars.by_ref().take(limit).collect::<String>();
    if chars.next().is_some() {
        format!("{}...", prefix.trim_end())
    } else {
        prefix
    }
}

fn normalize_source_text(text: &str) -> String {
    text.replace("\r\n", "\n").replace('\r', "\n")
}

fn category_label(category: &str) -> &'static str {
    match category {
        "characters" => "角色",
        "plot" => "剧情",
        "setting" => "设定",
        _ => "项目总览",
    }
}

fn normalized_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn now_rfc3339() -> String {
    Utc::now().to_rfc3339()
}

fn previous_revision(existing: Option<&Value>, status: Option<&Value>) -> u64 {
    existing
        .and_then(|value| value.get("knowledgeRevision"))
        .and_then(Value::as_u64)
        .unwrap_or(0)
        .max(
            status
                .and_then(|value| value.get("lastSuccessfulRevision"))
                .and_then(Value::as_u64)
                .unwrap_or(0),
        )
}

fn source_change_set(
    previous: Option<&Value>,
    current: &Value,
    base_revision: u64,
) -> Result<Value> {
    let old = previous
        .and_then(|value| value.get("sources"))
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let new = current
        .get("sources")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut added = Vec::new();
    let mut modified = Vec::new();
    let mut deleted = Vec::new();
    for (path, value) in &new {
        if !old.contains_key(path) {
            added.push(Value::String(path.clone()));
        } else if old.get(path).and_then(|item| item.get("sha256")) != value.get("sha256") {
            modified.push(Value::String(path.clone()));
        }
    }
    for path in old.keys() {
        if !new.contains_key(path) {
            deleted.push(Value::String(path.clone()));
        }
    }
    added.sort_by(|a, b| a.as_str().cmp(&b.as_str()));
    modified.sort_by(|a, b| a.as_str().cmp(&b.as_str()));
    deleted.sort_by(|a, b| a.as_str().cmp(&b.as_str()));
    let mut changed = Vec::new();
    changed.extend(added.iter().cloned());
    changed.extend(modified.iter().cloned());
    changed.extend(deleted.iter().cloned());
    let revision = if base_revision == 0 || !changed.is_empty() {
        base_revision + 1
    } else {
        base_revision
    };
    let mut source_changes = Vec::new();
    for (kind, paths) in [
        ("added", &added),
        ("modified", &modified),
        ("deleted", &deleted),
    ] {
        for path in paths {
            let relative_path = path.as_str().unwrap_or_default();
            source_changes.push(json!({
                "path": relative_path,
                "changeKind": kind,
                "beforeSha256": old.get(relative_path).and_then(|value| value.get("sha256")).cloned().unwrap_or(Value::Null),
                "afterSha256": new.get(relative_path).and_then(|value| value.get("sha256")).cloned().unwrap_or(Value::Null),
            }));
        }
    }
    let identity = json!({
        "baseRevision": base_revision,
        "revision": revision,
        "sourceChanges": source_changes,
    });
    let id = format!(
        "changeset:{}",
        &hex_digest(&canonical_json_bytes(&identity)?)[..16]
    );
    Ok(json!({
        "schemaVersion": 1,
        "changeSetId": id,
        "baseRevision": base_revision,
        "revision": revision,
        "addedSourcePaths": added,
        "modifiedSourcePaths": modified,
        "deletedSourcePaths": deleted,
        "changedSourcePaths": changed,
        "sourceChanges": identity.get("sourceChanges").cloned().unwrap_or_else(|| json!([])),
        "noChanges": changed.is_empty(),
    }))
}

/// A small local Git adapter.  It intentionally rejects a project nested in a
/// parent repository, matching the Storydex boundary contract.
#[derive(Clone, Debug, Default)]
pub struct StorydexGit;

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitChangedFile {
    pub status: String,
    pub relative_path: String,
    pub staged: bool,
    pub unstaged: bool,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitSummary {
    pub available: bool,
    pub git_installed: bool,
    pub initialized: bool,
    pub branch: String,
    pub clean: bool,
    pub changed_paths: Vec<String>,
    pub changed_files: Vec<GitChangedFile>,
    pub recent_commits: Vec<GitCommit>,
    pub graph_lines: Vec<String>,
    pub default_branch: String,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub head: Option<GitCommit>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub generated_at: Option<u64>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitCommit {
    pub id: String,
    pub short_id: String,
    pub author_name: String,
    pub authored_at: String,
    pub refs: String,
    pub subject: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub on_current_branch: Option<bool>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitCommitResult {
    pub created: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub commit: Option<GitCommit>,
    pub summary: GitSummary,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub worldline_branch: Option<String>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitRestoreResult {
    pub restored: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub restored_commit: Option<GitCommit>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub backup_commit: Option<GitCommit>,
    pub backup_ref: String,
    pub summary: GitSummary,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitDiffLine {
    pub kind: String,
    pub old_line: Option<usize>,
    pub new_line: Option<usize>,
    pub content: String,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitDiffHunk {
    pub header: String,
    pub old_start: usize,
    pub old_lines: usize,
    pub new_start: usize,
    pub new_lines: usize,
    pub lines: Vec<GitDiffLine>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitDiffFile {
    pub relative_path: String,
    pub status: String,
    pub added: usize,
    pub removed: usize,
    pub hunks: Vec<GitDiffHunk>,
    pub truncated: bool,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitDiffTotals {
    pub files: usize,
    pub added: usize,
    pub removed: usize,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitDiff {
    pub available: bool,
    pub git_installed: bool,
    pub initialized: bool,
    pub branch: String,
    pub files: Vec<GitDiffFile>,
    pub totals: GitDiffTotals,
    pub message: String,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitBranch {
    pub name: String,
    pub current: bool,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitBranches {
    pub current: String,
    pub branches: Vec<GitBranch>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub summary: Option<GitSummary>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitTimelineBranch {
    pub name: String,
    pub head: String,
    pub is_current: bool,
    pub lane: usize,
    pub fork_column: usize,
    pub tip_column: usize,
    pub commit_count: usize,
    pub total_count: usize,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitTimelineNode {
    pub id: String,
    pub short_id: String,
    pub author_name: String,
    pub authored_at: String,
    pub subject: String,
    pub refs: String,
    pub parents: Vec<String>,
    pub branches: Vec<String>,
    pub head_branches: Vec<String>,
    pub is_branch_head: bool,
    pub is_current: bool,
    pub column: usize,
    pub row: usize,
    pub lane_branch: String,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitTimelineEdge {
    pub from: String,
    pub to: String,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitTimeline {
    pub available: bool,
    pub git_installed: bool,
    pub initialized: bool,
    pub current_branch: String,
    pub current_head: Option<GitCommit>,
    pub detached: bool,
    pub branches: Vec<GitTimelineBranch>,
    pub nodes: Vec<GitTimelineNode>,
    pub edges: Vec<GitTimelineEdge>,
    pub message: String,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitJumpResult {
    pub detached: bool,
    pub branch: String,
    pub commit: Option<GitCommit>,
    pub summary: GitSummary,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitWorldlineResult {
    pub current: String,
    pub branches: Vec<GitBranch>,
    pub summary: GitSummary,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub worldline: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub from_commit: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub renamed_from: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub renamed_to: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub deleted: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exclusive_commits: Option<usize>,
}

#[derive(Clone, Debug)]
struct TimelineCommit {
    commit: GitCommit,
    parents: Vec<String>,
}

impl StorydexGit {
    pub fn initialize(&self, project_root: impl AsRef<Path>) -> Result<GitSummary> {
        let root = canonical_existing_dir(project_root.as_ref())?;
        ensure!(git_available(), "Storydex Git executable is not available");
        if repository_top_level(&root)?.is_none() {
            run_git(&root, &["init"])?;
            // Keep the historical Storydex branch name on fresh repositories.
            run_git(&root, &["branch", "-M", DEFAULT_BRANCH])?;
        } else {
            self.assert_project_repository(&root)?;
        }
        self.assert_project_repository(&root)?;
        configure_local_identity(&root)?;
        self.ensure_internal_paths_untracked(&root)?;
        self.summary(&root)
    }

    pub fn summary(&self, project_root: impl AsRef<Path>) -> Result<GitSummary> {
        let root = canonical_existing_dir(project_root.as_ref())?;
        if !git_available() {
            return Ok(uninitialized_summary(
                false,
                "Storydex bundled Git is not available.",
            ));
        }
        let Some(top) = repository_top_level(&root)? else {
            return Ok(uninitialized_summary(
                true,
                "Local repository is not initialized yet.",
            ));
        };
        if !same_path(&root, &top) {
            bail!("refusing Git operation: project root is nested in a parent repository")
        }
        let status = run_git(
            &root,
            &[
                "-c",
                "core.quotePath=false",
                "status",
                "--porcelain=v1",
                "--branch",
                "-uall",
            ],
        )?;
        let (branch, changed_files) = parse_status(&status);
        let changed_paths = changed_files
            .iter()
            .map(|item| item.relative_path.clone())
            .collect::<Vec<_>>();
        let head = self.read_head_commit(&root)?;
        let recent_commits = if head.is_some() {
            self.read_recent_commits(&root, HISTORY_LIMIT)?
        } else {
            Vec::new()
        };
        let graph_lines = if head.is_some() {
            self.read_graph_lines(&root, HISTORY_LIMIT.min(16))?
        } else {
            Vec::new()
        };
        Ok(GitSummary {
            available: true,
            git_installed: true,
            initialized: true,
            branch,
            clean: changed_files.is_empty(),
            changed_paths,
            changed_files,
            recent_commits,
            graph_lines,
            default_branch: DEFAULT_BRANCH.to_owned(),
            message: String::new(),
            head,
            generated_at: unix_timestamp_millis(),
        })
    }

    pub fn diff(&self, project_root: impl AsRef<Path>) -> Result<GitDiff> {
        let root = canonical_existing_dir(project_root.as_ref())?;
        if !git_available() {
            return Ok(uninitialized_diff(
                false,
                "Storydex bundled Git is not available.",
            ));
        }
        let Some(top) = repository_top_level(&root)? else {
            return Ok(uninitialized_diff(
                true,
                "Local repository is not initialized yet.",
            ));
        };
        ensure!(
            same_path(&root, &top),
            "refusing Git operation: project root is nested in a parent repository"
        );

        let summary = self.summary(&root)?;
        let has_head = summary.head.is_some();
        let mut files = Vec::new();
        for changed in &summary.changed_files {
            let relative = validate_git_relative_path(&root, &changed.relative_path)?;
            let status = {
                let value = changed.status.trim();
                if value.is_empty() { "M" } else { value }
            };
            let file = if status == "??" || !has_head {
                build_untracked_diff(&root, &relative, status)?
            } else {
                let output = run_git_raw(
                    &root,
                    &[
                        "-c",
                        "core.quotePath=false",
                        "diff",
                        "--unified=3",
                        "--no-ext-diff",
                        "--no-color",
                        "HEAD",
                        "--",
                        &relative,
                    ],
                )?;
                parse_unified_diff_file(&output, &relative, status)
            };
            files.push(file);
        }
        Ok(complete_diff(summary.branch, files))
    }

    pub fn commit_diff(&self, project_root: impl AsRef<Path>, commit_id: &str) -> Result<GitDiff> {
        let root = canonical_existing_dir(project_root.as_ref())?;
        if !git_available() {
            return Ok(uninitialized_diff(
                false,
                "Storydex bundled Git is not available.",
            ));
        }
        let Some(top) = repository_top_level(&root)? else {
            return Ok(uninitialized_diff(
                true,
                "Local repository is not initialized yet.",
            ));
        };
        ensure!(
            same_path(&root, &top),
            "refusing Git operation: project root is nested in a parent repository"
        );
        let commit_id = commit_id.trim();
        ensure!(
            !commit_id.is_empty(),
            "commit id is required for commit diff"
        );
        let commit = self.read_commit(&root, commit_id)?;
        let changed_files = read_commit_changed_files(&root, &commit.id)?;
        let mut files = Vec::new();
        for (status, relative) in changed_files {
            let relative = validate_git_relative_path(&root, &relative)?;
            let args = vec![
                "-c".to_owned(),
                "core.quotePath=false".to_owned(),
                "show".to_owned(),
                "--format=".to_owned(),
                "--unified=3".to_owned(),
                "--no-ext-diff".to_owned(),
                "--no-color".to_owned(),
                "--find-renames".to_owned(),
                "--end-of-options".to_owned(),
                commit.id.clone(),
                "--".to_owned(),
                relative.clone(),
            ];
            let output = run_git_owned_raw(&root, &args)?;
            files.push(parse_unified_diff_file(&output, &relative, &status));
        }
        let branch = self.summary(&root)?.branch;
        Ok(complete_diff(branch, files))
    }

    pub fn branches(&self, project_root: impl AsRef<Path>) -> Result<GitBranches> {
        let root = canonical_existing_dir(project_root.as_ref())?;
        self.initialize(&root)?;
        self.branch_result(&root)
    }

    fn branch_result(&self, root: &Path) -> Result<GitBranches> {
        let (current, branches) = self.read_branches(root)?;
        Ok(GitBranches {
            current,
            branches,
            summary: Some(self.summary(root)?),
        })
    }

    pub fn create_branch(
        &self,
        project_root: impl AsRef<Path>,
        name: &str,
        checkout: bool,
    ) -> Result<GitBranches> {
        let root = canonical_existing_dir(project_root.as_ref())?;
        self.initialize(&root)?;
        let branch = validate_branch_name(name)?;
        ensure!(
            !self.branch_exists(&root, &branch),
            "branch already exists: {branch}"
        );
        let has_head = self.has_head_commit(&root);
        if checkout && !self.is_worktree_clean(&root)? {
            bail!("cannot switch branches with uncommitted changes")
        }
        if has_head {
            run_git_owned(&root, &["branch".to_owned(), branch.clone()])?;
            if checkout {
                self.clean_internal_paths(&root)?;
                run_git_owned(&root, &["checkout".to_owned(), branch])?;
            }
        } else if checkout {
            run_git_owned(
                &root,
                &[
                    "symbolic-ref".to_owned(),
                    "HEAD".to_owned(),
                    format!("refs/heads/{branch}"),
                ],
            )?;
        } else {
            bail!("cannot create a non-current branch before the first commit")
        }
        self.branch_result(&root)
    }

    pub fn switch_branch(&self, project_root: impl AsRef<Path>, name: &str) -> Result<GitBranches> {
        let root = canonical_existing_dir(project_root.as_ref())?;
        self.initialize(&root)?;
        let branch = validate_branch_name(name)?;
        let current = self.current_branch(&root)?;
        if branch == current {
            return self.branch_result(&root);
        }
        ensure!(
            self.branch_exists(&root, &branch),
            "branch does not exist: {branch}"
        );
        ensure!(
            self.is_worktree_clean(&root)?,
            "cannot switch branches with uncommitted changes"
        );
        self.clean_internal_paths(&root)?;
        run_git_owned(&root, &["checkout".to_owned(), branch])?;
        self.branch_result(&root)
    }

    pub fn timeline(&self, project_root: impl AsRef<Path>) -> Result<GitTimeline> {
        let root = canonical_existing_dir(project_root.as_ref())?;
        if !git_available() {
            return Ok(uninitialized_timeline(
                false,
                "Storydex bundled Git is not available.",
            ));
        }
        let Some(top) = repository_top_level(&root)? else {
            return Ok(uninitialized_timeline(
                true,
                "Local repository is not initialized yet.",
            ));
        };
        ensure!(
            same_path(&root, &top),
            "refusing Git operation: project root is nested in a parent repository"
        );

        let branch_heads = self.read_branch_heads(&root)?;
        let current_branch = self.current_branch(&root)?;
        let current_head = self.read_head_commit(&root)?;
        let detached = current_branch.is_empty() && current_head.is_some();
        let Some(current_head_value) = current_head.clone() else {
            let branches = branch_heads
                .iter()
                .enumerate()
                .map(|(lane, (name, head))| GitTimelineBranch {
                    name: name.clone(),
                    head: head.clone(),
                    is_current: false,
                    lane,
                    fork_column: 0,
                    tip_column: 0,
                    commit_count: 0,
                    total_count: 0,
                })
                .collect();
            return Ok(GitTimeline {
                available: true,
                git_installed: true,
                initialized: true,
                current_branch,
                current_head: None,
                detached,
                branches,
                nodes: Vec::new(),
                edges: Vec::new(),
                message: String::new(),
            });
        };

        let raw = run_git_raw(
            &root,
            &[
                "log",
                "--all",
                "HEAD",
                "--date=iso-strict",
                "--pretty=format:%H%x1f%P%x1f%h%x1f%an%x1f%ad%x1f%D%x1f%s",
            ],
        )?;
        let commits = parse_timeline_commits(&raw);
        let commit_ids = commits
            .iter()
            .map(|item| item.commit.id.clone())
            .collect::<HashSet<_>>();
        let parents_by_id = commits
            .iter()
            .map(|item| {
                (
                    item.commit.id.clone(),
                    item.parents
                        .iter()
                        .filter(|parent| commit_ids.contains(*parent))
                        .cloned()
                        .collect::<Vec<_>>(),
                )
            })
            .collect::<HashMap<_, _>>();

        let branch_reachable = branch_heads
            .iter()
            .map(|(name, head)| (name.clone(), reachable_commits(head, &parents_by_id)))
            .collect::<HashMap<_, _>>();
        let columns = topology_columns(&commit_ids, &parents_by_id);
        let mut containing_count = HashMap::<String, usize>::new();
        for reachable in branch_reachable.values() {
            for commit_id in reachable {
                *containing_count.entry(commit_id.clone()).or_default() += 1;
            }
        }

        let mut fork_columns = HashMap::<String, usize>::new();
        let mut exclusive_counts = HashMap::<String, usize>::new();
        for (name, head) in &branch_heads {
            let exclusive = branch_reachable
                .get(name)
                .into_iter()
                .flatten()
                .filter(|commit_id| containing_count.get(*commit_id).copied() == Some(1))
                .collect::<Vec<_>>();
            exclusive_counts.insert(name.clone(), exclusive.len());
            let fork = exclusive
                .iter()
                .map(|commit_id| columns.get(*commit_id).copied().unwrap_or_default())
                .min()
                .unwrap_or_else(|| columns.get(head).copied().unwrap_or_default());
            fork_columns.insert(name.clone(), fork);
        }

        let mut sorted_branches = branch_heads.clone();
        sorted_branches.sort_by(|left, right| {
            let left_key = (
                usize::from(left.0 != current_branch),
                fork_columns.get(&left.0).copied().unwrap_or_default(),
                left.0.as_str(),
            );
            let right_key = (
                usize::from(right.0 != current_branch),
                fork_columns.get(&right.0).copied().unwrap_or_default(),
                right.0.as_str(),
            );
            left_key.cmp(&right_key)
        });
        let branch_lanes = sorted_branches
            .iter()
            .enumerate()
            .map(|(lane, (name, _))| (name.clone(), lane))
            .collect::<HashMap<_, _>>();
        let lane_names = sorted_branches
            .iter()
            .enumerate()
            .map(|(lane, (name, _))| (lane, name.clone()))
            .collect::<HashMap<_, _>>();
        let fallback_lane = sorted_branches.len();

        let mut head_to_branches = HashMap::<String, Vec<String>>::new();
        for (name, head) in &branch_heads {
            head_to_branches
                .entry(head.clone())
                .or_default()
                .push(name.clone());
        }
        for names in head_to_branches.values_mut() {
            names.sort();
        }

        let mut nodes = Vec::new();
        let mut edges = Vec::new();
        for item in commits {
            let commit_id = item.commit.id.clone();
            let mut containing_branches = branch_reachable
                .iter()
                .filter(|(_, reachable)| reachable.contains(&commit_id))
                .map(|(name, _)| name.clone())
                .collect::<Vec<_>>();
            containing_branches.sort_by_key(|name| {
                (
                    branch_lanes.get(name).copied().unwrap_or(fallback_lane),
                    name.clone(),
                )
            });
            let row = containing_branches
                .iter()
                .filter_map(|name| branch_lanes.get(name).copied())
                .min()
                .unwrap_or(fallback_lane);
            let lane_branch = lane_names.get(&row).cloned().unwrap_or_default();
            let head_branches = head_to_branches
                .get(&commit_id)
                .cloned()
                .unwrap_or_default();
            for parent in &item.parents {
                if commit_ids.contains(parent) {
                    edges.push(GitTimelineEdge {
                        from: parent.clone(),
                        to: commit_id.clone(),
                    });
                }
            }
            nodes.push(GitTimelineNode {
                id: commit_id.clone(),
                short_id: item.commit.short_id,
                author_name: item.commit.author_name,
                authored_at: item.commit.authored_at,
                subject: item.commit.subject,
                refs: item.commit.refs,
                parents: item.parents,
                branches: containing_branches,
                is_branch_head: !head_branches.is_empty(),
                head_branches,
                is_current: commit_id == current_head_value.id,
                column: columns.get(&commit_id).copied().unwrap_or_default(),
                row,
                lane_branch,
            });
        }
        nodes.sort_by(|left, right| {
            (left.column, left.row, left.id.as_str()).cmp(&(
                right.column,
                right.row,
                right.id.as_str(),
            ))
        });
        edges.sort_by(|left, right| {
            (left.from.as_str(), left.to.as_str()).cmp(&(right.from.as_str(), right.to.as_str()))
        });
        let branches = sorted_branches
            .into_iter()
            .map(|(name, head)| GitTimelineBranch {
                is_current: name == current_branch,
                lane: branch_lanes.get(&name).copied().unwrap_or(fallback_lane),
                fork_column: fork_columns.get(&name).copied().unwrap_or_default(),
                tip_column: columns.get(&head).copied().unwrap_or_default(),
                commit_count: exclusive_counts.get(&name).copied().unwrap_or_default(),
                total_count: branch_reachable.get(&name).map_or(0, HashSet::len),
                name,
                head,
            })
            .collect();

        Ok(GitTimeline {
            available: true,
            git_installed: true,
            initialized: true,
            current_branch,
            current_head,
            detached,
            branches,
            nodes,
            edges,
            message: String::new(),
        })
    }

    pub fn jump_to_commit(
        &self,
        project_root: impl AsRef<Path>,
        commit_id: &str,
    ) -> Result<GitJumpResult> {
        let root = canonical_existing_dir(project_root.as_ref())?;
        self.initialize(&root)?;
        let commit_id = commit_id.trim();
        ensure!(
            !commit_id.is_empty(),
            "target commit id is required for jump"
        );
        ensure!(
            self.has_head_commit(&root),
            "repository has no commits yet, so there is nothing to jump to"
        );
        let target = self.read_commit(&root, commit_id)?;
        ensure!(
            self.is_worktree_clean(&root)?,
            "cannot jump to commit with uncommitted changes"
        );
        let current_branch = self.current_branch(&root)?;
        let current_head = self.read_head_commit(&root)?;
        let target_branches = self.branches_at_commit(&root, &target.id)?;
        let landing_branch = if target_branches.contains(&current_branch) {
            current_branch.clone()
        } else {
            target_branches.first().cloned().unwrap_or_default()
        };
        let already_there = current_head
            .as_ref()
            .is_some_and(|current| current.id == target.id)
            && ((landing_branch.is_empty() && current_branch.is_empty())
                || landing_branch == current_branch);
        if !already_there {
            self.clean_internal_paths(&root)?;
            if landing_branch.is_empty() {
                run_git_owned(
                    &root,
                    &[
                        "checkout".to_owned(),
                        "--detach".to_owned(),
                        target.id.clone(),
                    ],
                )?;
            } else {
                run_git_owned(&root, &["checkout".to_owned(), landing_branch.clone()])?;
            }
        }
        Ok(GitJumpResult {
            detached: landing_branch.is_empty(),
            branch: landing_branch,
            commit: self.read_head_commit(&root)?,
            summary: self.summary(&root)?,
        })
    }

    pub fn create_worldline(
        &self,
        project_root: impl AsRef<Path>,
        from_commit: &str,
        name: &str,
    ) -> Result<GitWorldlineResult> {
        let root = canonical_existing_dir(project_root.as_ref())?;
        self.initialize(&root)?;
        let branch = validate_branch_name(name)?;
        ensure!(
            self.has_head_commit(&root),
            "repository has no commits yet, so there is no node to branch from"
        );
        ensure!(
            !self.branch_exists(&root, &branch),
            "a worldline with this name already exists"
        );
        let target = self.read_commit(&root, from_commit.trim())?;
        ensure!(
            self.is_worktree_clean(&root)?,
            "cannot open a new worldline with uncommitted changes"
        );
        self.clean_internal_paths(&root)?;
        run_git_owned(
            &root,
            &[
                "checkout".to_owned(),
                "-b".to_owned(),
                branch.clone(),
                target.id.clone(),
            ],
        )?;
        self.worldline_result(&root, Some(branch), Some(target.id), None, None, None, None)
    }

    pub fn rename_worldline(
        &self,
        project_root: impl AsRef<Path>,
        name: &str,
        new_name: &str,
    ) -> Result<GitWorldlineResult> {
        let root = canonical_existing_dir(project_root.as_ref())?;
        self.initialize(&root)?;
        let current_name = validate_branch_name(name)?;
        let target_name = validate_branch_name(new_name)?;
        if current_name == target_name {
            return self.worldline_result(&root, None, None, None, None, None, None);
        }
        ensure!(
            self.branch_exists(&root, &current_name),
            "worldline does not exist: {current_name}"
        );
        ensure!(
            !self.branch_exists(&root, &target_name),
            "a worldline with this name already exists: {target_name}"
        );
        run_git_owned(
            &root,
            &[
                "branch".to_owned(),
                "-m".to_owned(),
                current_name.clone(),
                target_name.clone(),
            ],
        )?;
        self.worldline_result(
            &root,
            None,
            None,
            Some(current_name),
            Some(target_name),
            None,
            None,
        )
    }

    pub fn delete_worldline(
        &self,
        project_root: impl AsRef<Path>,
        name: &str,
    ) -> Result<GitWorldlineResult> {
        let root = canonical_existing_dir(project_root.as_ref())?;
        self.initialize(&root)?;
        let branch = validate_branch_name(name)?;
        ensure!(
            self.branch_exists(&root, &branch),
            "worldline does not exist: {branch}"
        );
        ensure!(
            self.current_branch(&root)? != branch,
            "cannot delete the worldline you are currently on"
        );
        let (_, branches) = self.read_branches(&root)?;
        ensure!(
            branches.len() > 1,
            "cannot delete the only worldline in the project"
        );
        let others = branches
            .iter()
            .filter(|item| item.name != branch)
            .map(|item| item.name.clone())
            .collect::<Vec<_>>();
        let exclusive = count_exclusive_commits(&root, &branch, &others)?;
        run_git_owned(
            &root,
            &["branch".to_owned(), "-D".to_owned(), branch.clone()],
        )?;
        self.worldline_result(&root, None, None, None, None, Some(branch), Some(exclusive))
    }

    pub fn commit_all(
        &self,
        project_root: impl AsRef<Path>,
        message: &str,
    ) -> Result<GitCommitResult> {
        let root = canonical_existing_dir(project_root.as_ref())?;
        self.assert_project_repository(&root)?;
        run_git(&root, &["add", "-A"])?;
        self.commit_staged(&root, message)
    }

    pub fn commit_paths<I, S>(
        &self,
        project_root: impl AsRef<Path>,
        paths: I,
        message: &str,
    ) -> Result<GitCommitResult>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let root = canonical_existing_dir(project_root.as_ref())?;
        self.assert_project_repository(&root)?;
        let normalized = normalize_paths(&root, paths)?;
        if normalized.is_empty() {
            return Ok(GitCommitResult {
                created: false,
                commit: None,
                summary: self.summary(&root)?,
                worldline_branch: None,
            });
        }
        let stageable = normalized
            .iter()
            .filter(|path| root.join(path).exists() || git_path_is_tracked(&root, path))
            .cloned()
            .collect::<Vec<_>>();
        if stageable.is_empty() {
            return Ok(GitCommitResult {
                created: false,
                commit: None,
                summary: self.summary(&root)?,
                worldline_branch: None,
            });
        }
        let mut args = vec!["add", "--"];
        args.extend(stageable.iter().map(String::as_str));
        run_git(&root, &args)?;
        // `initialize` may have just created or amended the local ignore file;
        // keep that repository-boundary metadata in the same commit as the
        // first project write, matching the Python service contract.
        if root.join(".gitignore").is_file() && !normalized.iter().any(|path| path == ".gitignore")
        {
            run_git(&root, &["add", "--", ".gitignore"])?;
        }
        self.commit_staged(&root, message)
    }

    /// Restore a commit while keeping an optional local backup ref.
    /// Callers must opt into this operation explicitly; it never targets a
    /// parent repository because `assert_project_repository` is mandatory.
    pub fn restore_to_commit(
        &self,
        project_root: impl AsRef<Path>,
        commit: &str,
        create_backup: bool,
    ) -> Result<GitRestoreResult> {
        let root = canonical_existing_dir(project_root.as_ref())?;
        self.initialize(&root)?;
        ensure!(!commit.trim().is_empty(), "target commit id is required");
        let target = self.read_commit(&root, commit)?;
        let mut current = self
            .read_head_commit(&root)?
            .context("repository head could not be resolved")?;

        let mut dirty_backup_commit = None;
        if create_backup && !self.is_worktree_clean(&root)? {
            let backup = self.commit_all(
                &root,
                &format!("workspace: backup before restore to {}", target.short_id),
            )?;
            dirty_backup_commit = backup.commit;
            current = self
                .read_head_commit(&root)?
                .context("repository head could not be resolved after backup")?;
        }

        if current.id == target.id && self.is_worktree_clean(&root)? {
            return Ok(GitRestoreResult {
                restored: false,
                restored_commit: Some(current),
                backup_commit: dirty_backup_commit,
                backup_ref: String::new(),
                summary: self.summary(&root)?,
            });
        }

        let mut backup_ref = String::new();
        if create_backup {
            backup_ref = self.create_backup_ref(&root, &current.id, &target.short_id)?;
        }
        run_git_owned(&root, &["reset".to_owned(), "--hard".to_owned(), target.id])?;
        run_git(&root, &["clean", "-fd"])?;
        let restored_commit = self.read_head_commit(&root)?;
        Ok(GitRestoreResult {
            restored: true,
            restored_commit,
            backup_commit: Some(current),
            backup_ref,
            summary: self.summary(&root)?,
        })
    }

    fn assert_project_repository(&self, root: &Path) -> Result<()> {
        ensure!(git_available(), "Storydex Git executable is not available");
        let Some(top) = repository_top_level(root)? else {
            bail!("Storydex project is not an initialized Git repository")
        };
        ensure!(
            same_path(root, &top),
            "refusing Git operation: project root is nested in a parent repository"
        );
        Ok(())
    }

    fn commit_staged(&self, root: &Path, message: &str) -> Result<GitCommitResult> {
        let staged = Command::new(git_executable())
            .args(["diff", "--cached", "--quiet"])
            .current_dir(root)
            .status()
            .context("failed to inspect staged Git changes")?;
        if staged.success() {
            return Ok(GitCommitResult {
                created: false,
                commit: None,
                summary: self.summary(root)?,
                worldline_branch: None,
            });
        }
        let worldline_branch = self.ensure_branch_before_commit(root)?;
        let message = if message.trim().is_empty() {
            "storydex: local snapshot"
        } else {
            message.trim()
        };
        run_git(root, &["commit", "--no-gpg-sign", "-m", message])?;
        let commit = self.read_head_commit(root)?;
        Ok(GitCommitResult {
            created: true,
            commit,
            summary: self.summary(root)?,
            worldline_branch,
        })
    }

    fn read_commit(&self, root: &Path, commit_id: &str) -> Result<GitCommit> {
        ensure!(!commit_id.trim().is_empty(), "target commit id is required");
        let revision = format!("{}^{{commit}}", commit_id.trim());
        let args = vec![
            "show".to_owned(),
            "-s".to_owned(),
            "--date=iso-strict".to_owned(),
            "--pretty=format:%H%x1f%h%x1f%an%x1f%ad%x1f%D%x1f%s".to_owned(),
            "--end-of-options".to_owned(),
            revision,
        ];
        let output = run_git_owned_raw(root, &args)?;
        parse_commit(&output).context("target commit could not be resolved")
    }

    fn read_head_commit(&self, root: &Path) -> Result<Option<GitCommit>> {
        if !self.has_head_commit(root) {
            return Ok(None);
        }
        self.read_commit(root, "HEAD").map(Some)
    }

    fn read_recent_commits(&self, root: &Path, limit: usize) -> Result<Vec<GitCommit>> {
        let reachable = run_git_raw(root, &["rev-list", &format!("-n{}", limit * 8), "HEAD"])?
            .lines()
            .map(str::trim)
            .filter(|line| !line.is_empty())
            .map(str::to_owned)
            .collect::<HashSet<_>>();
        let limit = format!("-n{}", limit.max(1));
        let output = run_git_raw(
            root,
            &[
                "log",
                "--all",
                &limit,
                "--decorate=short",
                "--date=iso-strict",
                "--pretty=format:%H%x1f%h%x1f%an%x1f%ad%x1f%D%x1f%s",
            ],
        )?;
        Ok(output
            .lines()
            .filter_map(parse_commit)
            .map(|mut commit| {
                commit.on_current_branch =
                    Some(reachable.is_empty() || reachable.contains(&commit.id));
                commit
            })
            .collect())
    }

    fn read_graph_lines(&self, root: &Path, limit: usize) -> Result<Vec<String>> {
        let limit = format!("-n{}", limit.max(1));
        Ok(run_git_raw(
            root,
            &["log", "--graph", "--decorate", "--oneline", "--all", &limit],
        )?
        .lines()
        .map(str::trim_end)
        .filter(|line| !line.trim().is_empty())
        .map(str::to_owned)
        .collect())
    }

    fn current_branch(&self, root: &Path) -> Result<String> {
        run_git(root, &["branch", "--show-current"])
    }

    fn read_branch_heads(&self, root: &Path) -> Result<Vec<(String, String)>> {
        let output = run_git_raw(
            root,
            &[
                "for-each-ref",
                "--format=%(refname:short) %(objectname)",
                "refs/heads/",
            ],
        )?;
        let mut heads = output
            .lines()
            .filter_map(|line| {
                let mut parts = line.split_whitespace();
                let name = parts.next()?;
                let head = parts.next()?;
                if parts.next().is_some() {
                    return None;
                }
                Some((name.to_owned(), head.to_owned()))
            })
            .collect::<Vec<_>>();
        heads.sort_by(|left, right| left.0.cmp(&right.0));
        Ok(heads)
    }

    fn read_branches(&self, root: &Path) -> Result<(String, Vec<GitBranch>)> {
        let current = self.current_branch(root)?;
        let mut names = self
            .read_branch_heads(root)?
            .into_iter()
            .map(|(name, _)| name)
            .collect::<Vec<_>>();
        if !current.is_empty() && !names.contains(&current) {
            names.push(current.clone());
            names.sort();
        }
        let branches = names
            .into_iter()
            .map(|name| GitBranch {
                current: name == current,
                name,
            })
            .collect();
        Ok((current, branches))
    }

    fn branch_exists(&self, root: &Path, branch: &str) -> bool {
        Command::new(git_executable())
            .args(["show-ref", "--verify", &format!("refs/heads/{branch}")])
            .current_dir(root)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|status| status.success())
            .unwrap_or(false)
    }

    fn has_head_commit(&self, root: &Path) -> bool {
        Command::new(git_executable())
            .args(["rev-parse", "--verify", "HEAD"])
            .current_dir(root)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|status| status.success())
            .unwrap_or(false)
    }

    fn is_worktree_clean(&self, root: &Path) -> Result<bool> {
        Ok(run_git_raw(
            root,
            &[
                "-c",
                "core.quotePath=false",
                "status",
                "--porcelain=v1",
                "-uall",
            ],
        )?
        .trim()
        .is_empty())
    }

    fn ensure_internal_paths_untracked(&self, root: &Path) -> Result<()> {
        let tracked = tracked_internal_paths(root)?;
        if tracked.is_empty() {
            ensure_gitignore(root)?;
            return Ok(());
        }

        let has_head = self.has_head_commit(root);
        if has_head {
            ensure!(
                index_is_clean(root)?,
                "cannot migrate tracked internal Storydex paths while staged changes exist"
            );
            ensure!(
                git_path_status(root, ".gitignore")?.is_empty(),
                "cannot migrate tracked internal Storydex paths while .gitignore has uncommitted changes"
            );
        }

        let ignore_path = root.join(".gitignore");
        let original_ignore = fs::read(&ignore_path).ok();
        let ignore_changed = ensure_gitignore(root)?;
        let migration = (|| -> Result<()> {
            run_git(
                root,
                &[
                    "rm",
                    "-r",
                    "--cached",
                    "--ignore-unmatch",
                    "--",
                    ".storydex/.agent",
                    ".storydex/.cache",
                ],
            )?;
            if has_head {
                if ignore_changed {
                    run_git(root, &["add", "--", ".gitignore"])?;
                }
                run_git(
                    root,
                    &[
                        "commit",
                        "--no-gpg-sign",
                        "-m",
                        "chore: untrack internal Storydex paths",
                    ],
                )?;
            }
            Ok(())
        })();

        if let Err(error) = migration {
            if has_head {
                let _ = run_git(
                    root,
                    &[
                        "reset",
                        "--quiet",
                        "HEAD",
                        "--",
                        ".gitignore",
                        ".storydex/.agent",
                        ".storydex/.cache",
                    ],
                );
            }
            if ignore_changed {
                restore_file(&ignore_path, original_ignore.as_deref())?;
            }
            return Err(error).context("failed to untrack legacy Storydex internal paths");
        }
        Ok(())
    }

    fn create_backup_ref(
        &self,
        root: &Path,
        current_head: &str,
        target_short: &str,
    ) -> Result<String> {
        let base = format!("storydex-backup-{}-{target_short}", unique_suffix());
        let mut candidate = base.clone();
        let mut index = 2usize;
        while self.branch_exists(root, &candidate) {
            candidate = format!("{base}-{index}");
            index += 1;
        }
        run_git_owned(
            root,
            &[
                "branch".to_owned(),
                candidate.clone(),
                current_head.to_owned(),
            ],
        )?;
        Ok(candidate)
    }

    fn clean_internal_paths(&self, root: &Path) -> Result<()> {
        for prefix in INTERNAL_PATH_PREFIXES {
            run_git(root, &["clean", "-fd", "--", prefix.trim_end_matches('/')])?;
        }
        Ok(())
    }

    fn branches_at_commit(&self, root: &Path, commit_id: &str) -> Result<Vec<String>> {
        let mut names = self
            .read_branch_heads(root)?
            .into_iter()
            .filter(|(_, head)| head == commit_id)
            .map(|(name, _)| name)
            .collect::<Vec<_>>();
        names.sort();
        Ok(names)
    }

    fn ensure_branch_before_commit(&self, root: &Path) -> Result<Option<String>> {
        if !self.current_branch(root)?.is_empty() || !self.has_head_commit(root) {
            return Ok(None);
        }
        let base = format!("worldline/{}", unique_suffix());
        let mut branch = base.clone();
        let mut index = 2usize;
        while self.branch_exists(root, &branch) {
            branch = format!("{base}-{index}");
            index += 1;
        }
        run_git_owned(
            root,
            &["checkout".to_owned(), "-b".to_owned(), branch.clone()],
        )?;
        Ok(Some(branch))
    }

    #[allow(clippy::too_many_arguments)]
    fn worldline_result(
        &self,
        root: &Path,
        worldline: Option<String>,
        from_commit: Option<String>,
        renamed_from: Option<String>,
        renamed_to: Option<String>,
        deleted: Option<String>,
        exclusive_commits: Option<usize>,
    ) -> Result<GitWorldlineResult> {
        let (current, branches) = self.read_branches(root)?;
        Ok(GitWorldlineResult {
            current,
            branches,
            summary: self.summary(root)?,
            worldline,
            from_commit,
            renamed_from,
            renamed_to,
            deleted,
            exclusive_commits,
        })
    }
}

fn uninitialized_summary(git_installed: bool, message: &str) -> GitSummary {
    GitSummary {
        available: git_installed,
        git_installed,
        initialized: false,
        branch: DEFAULT_BRANCH.to_owned(),
        clean: true,
        changed_paths: Vec::new(),
        changed_files: Vec::new(),
        recent_commits: Vec::new(),
        graph_lines: Vec::new(),
        default_branch: DEFAULT_BRANCH.to_owned(),
        message: message.to_owned(),
        head: None,
        generated_at: unix_timestamp_millis(),
    }
}

fn uninitialized_diff(git_installed: bool, message: &str) -> GitDiff {
    GitDiff {
        available: git_installed,
        git_installed,
        initialized: false,
        branch: if git_installed {
            DEFAULT_BRANCH.to_owned()
        } else {
            String::new()
        },
        files: Vec::new(),
        totals: GitDiffTotals::default(),
        message: message.to_owned(),
    }
}

fn complete_diff(branch: String, files: Vec<GitDiffFile>) -> GitDiff {
    let totals = GitDiffTotals {
        files: files.len(),
        added: files.iter().map(|item| item.added).sum(),
        removed: files.iter().map(|item| item.removed).sum(),
    };
    GitDiff {
        available: true,
        git_installed: true,
        initialized: true,
        branch,
        files,
        totals,
        message: String::new(),
    }
}

fn uninitialized_timeline(git_installed: bool, message: &str) -> GitTimeline {
    GitTimeline {
        available: git_installed,
        git_installed,
        initialized: false,
        current_branch: if git_installed {
            DEFAULT_BRANCH.to_owned()
        } else {
            String::new()
        },
        current_head: None,
        detached: false,
        branches: Vec::new(),
        nodes: Vec::new(),
        edges: Vec::new(),
        message: message.to_owned(),
    }
}

fn unix_timestamp_millis() -> Option<u64> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .and_then(|duration| u64::try_from(duration.as_millis()).ok())
}

fn validate_git_relative_path(root: &Path, raw: &str) -> Result<String> {
    let raw = raw.trim();
    ensure!(!raw.is_empty(), "Git path must not be empty");
    let path = Path::new(raw);
    ensure!(!path.is_absolute(), "Git path must be relative: {raw}");
    ensure!(
        !path.components().any(|component| matches!(
            component,
            Component::ParentDir | Component::RootDir | Component::Prefix(_)
        )),
        "Git path must not escape the project root: {raw}"
    );
    let relative = raw.replace('\\', "/").trim_start_matches("./").to_owned();
    ensure!(!relative.is_empty(), "Git path must not be empty");
    ensure!(
        relative != ".git" && !relative.starts_with(".git/"),
        "Git metadata paths are not readable"
    );
    let target = root.join(&relative);
    if target.exists() {
        let canonical = target.canonicalize()?;
        ensure!(
            path_is_within(root, &canonical),
            "Git path resolves outside project root: {relative}"
        );
    }
    Ok(relative)
}

fn validate_branch_name(name: &str) -> Result<String> {
    let branch = name.trim();
    ensure!(
        !branch.is_empty()
            && branch.len() <= 120
            && !branch.starts_with('-')
            && !branch.contains("..")
            && branch
                .chars()
                .all(|value| value.is_ascii_alphanumeric() || ".-_/".contains(value)),
        "invalid branch name: {branch}"
    );
    Ok(branch.to_owned())
}

fn build_untracked_diff(root: &Path, relative_path: &str, status: &str) -> Result<GitDiffFile> {
    let relative_path = validate_git_relative_path(root, relative_path)?;
    let target = root.join(&relative_path);
    if !target.is_file() {
        return Ok(GitDiffFile {
            relative_path,
            status: status.to_owned(),
            ..GitDiffFile::default()
        });
    }
    let raw = fs::read(&target)
        .with_context(|| format!("failed to read untracked file {}", target.display()))?;
    if raw.iter().take(4_096).any(|value| *value == 0) {
        return Ok(GitDiffFile {
            relative_path,
            status: status.to_owned(),
            hunks: vec![GitDiffHunk {
                header: "Binary file not shown".to_owned(),
                lines: vec![GitDiffLine {
                    kind: "context".to_owned(),
                    content: "Binary file changed.".to_owned(),
                    ..GitDiffLine::default()
                }],
                ..GitDiffHunk::default()
            }],
            ..GitDiffFile::default()
        });
    }
    let text = String::from_utf8_lossy(&raw).into_owned();
    let lines = text.lines().collect::<Vec<_>>();
    let visible = lines
        .iter()
        .take(DIFF_MAX_LINES)
        .copied()
        .collect::<Vec<_>>();
    let mut diff_lines = visible
        .iter()
        .enumerate()
        .map(|(index, line)| GitDiffLine {
            kind: "added".to_owned(),
            old_line: None,
            new_line: Some(index + 1),
            content: (*line).to_owned(),
        })
        .collect::<Vec<_>>();
    if visible.is_empty() && !text.is_empty() {
        diff_lines.push(GitDiffLine {
            kind: "added".to_owned(),
            old_line: None,
            new_line: Some(1),
            content: text.clone(),
        });
    }
    let truncated = lines.len() > DIFF_MAX_LINES;
    if truncated {
        diff_lines.push(GitDiffLine {
            kind: "context".to_owned(),
            old_line: None,
            new_line: None,
            content: format!("... truncated {} lines", lines.len() - DIFF_MAX_LINES),
        });
    }
    let added = if lines.is_empty() {
        usize::from(!text.is_empty())
    } else {
        lines.len()
    };
    Ok(GitDiffFile {
        relative_path,
        status: status.to_owned(),
        added,
        removed: 0,
        hunks: vec![GitDiffHunk {
            header: format!("@@ -0,0 +1,{} @@", lines.len().max(1)),
            old_start: 0,
            old_lines: 0,
            new_start: 1,
            new_lines: lines.len().max(1),
            lines: diff_lines,
        }],
        truncated,
    })
}

fn parse_unified_diff_file(output: &str, relative_path: &str, status: &str) -> GitDiffFile {
    let mut hunks = Vec::<GitDiffHunk>::new();
    let mut current = None::<GitDiffHunk>;
    let mut old_line = 0usize;
    let mut new_line = 0usize;
    let mut added = 0usize;
    let mut removed = 0usize;
    for raw_line in output.lines() {
        if let Some((old_start, old_lines, new_start, new_lines)) = parse_hunk_header(raw_line) {
            if let Some(hunk) = current.take() {
                hunks.push(hunk);
            }
            current = Some(GitDiffHunk {
                header: raw_line.to_owned(),
                old_start,
                old_lines,
                new_start,
                new_lines,
                lines: Vec::new(),
            });
            old_line = old_start;
            new_line = new_start;
            continue;
        }
        let Some(hunk) = current.as_mut() else {
            continue;
        };
        if raw_line.starts_with("\\ No newline") {
            hunk.lines.push(GitDiffLine {
                kind: "context".to_owned(),
                old_line: None,
                new_line: None,
                content: raw_line.to_owned(),
            });
            continue;
        }
        let marker = raw_line.as_bytes().first().copied();
        let content = if matches!(marker, Some(b' ' | b'+' | b'-')) {
            &raw_line[1..]
        } else {
            raw_line
        };
        match marker {
            Some(b'+') => {
                hunk.lines.push(GitDiffLine {
                    kind: "added".to_owned(),
                    old_line: None,
                    new_line: Some(new_line),
                    content: content.to_owned(),
                });
                new_line += 1;
                added += 1;
            }
            Some(b'-') => {
                hunk.lines.push(GitDiffLine {
                    kind: "removed".to_owned(),
                    old_line: Some(old_line),
                    new_line: None,
                    content: content.to_owned(),
                });
                old_line += 1;
                removed += 1;
            }
            _ => {
                hunk.lines.push(GitDiffLine {
                    kind: "context".to_owned(),
                    old_line: Some(old_line),
                    new_line: Some(new_line),
                    content: content.to_owned(),
                });
                old_line += 1;
                new_line += 1;
            }
        }
    }
    if let Some(hunk) = current {
        hunks.push(hunk);
    }
    if hunks.is_empty() && !output.trim().is_empty() {
        hunks.push(GitDiffHunk {
            header: "File changed".to_owned(),
            lines: output
                .lines()
                .filter(|line| !line.trim().is_empty())
                .map(|line| GitDiffLine {
                    kind: "context".to_owned(),
                    content: line.to_owned(),
                    ..GitDiffLine::default()
                })
                .collect(),
            ..GitDiffHunk::default()
        });
    }
    GitDiffFile {
        relative_path: relative_path
            .replace('\\', "/")
            .trim_matches('/')
            .to_owned(),
        status: status.to_owned(),
        added,
        removed,
        hunks,
        truncated: false,
    }
}

fn parse_hunk_header(header: &str) -> Option<(usize, usize, usize, usize)> {
    let body = header.strip_prefix("@@ ")?;
    let end = body.find(" @@")?;
    let mut ranges = body[..end].split_whitespace();
    let old = parse_diff_range(ranges.next()?, '-')?;
    let new = parse_diff_range(ranges.next()?, '+')?;
    Some((old.0, old.1, new.0, new.1))
}

fn parse_diff_range(raw: &str, prefix: char) -> Option<(usize, usize)> {
    let raw = raw.strip_prefix(prefix)?;
    let mut parts = raw.splitn(2, ',');
    let start = parts.next()?.parse().ok()?;
    let lines = parts.next().map_or(Some(1), |value| value.parse().ok())?;
    Some((start, lines))
}

fn read_commit_changed_files(root: &Path, commit_id: &str) -> Result<Vec<(String, String)>> {
    let args = vec![
        "-c".to_owned(),
        "core.quotePath=false".to_owned(),
        "diff-tree".to_owned(),
        "--no-commit-id".to_owned(),
        "--name-status".to_owned(),
        "-r".to_owned(),
        "--root".to_owned(),
        "-M".to_owned(),
        commit_id.to_owned(),
    ];
    let output = run_git_owned_raw(root, &args)?;
    let mut files = Vec::new();
    for line in output.lines() {
        let parts = line.split('\t').collect::<Vec<_>>();
        if parts.len() < 2 {
            continue;
        }
        let raw_status = parts[0].trim();
        let (status, relative) = if raw_status.starts_with('R') && parts.len() >= 3 {
            ("R", parts[2])
        } else {
            (
                &raw_status[..raw_status.len().min(1)],
                parts[parts.len() - 1],
            )
        };
        let relative = normalize_status_path(relative).trim().to_owned();
        if !relative.is_empty() {
            files.push((
                if status.is_empty() { "M" } else { status }.to_owned(),
                relative,
            ));
        }
    }
    Ok(files)
}

fn parse_commit(raw: &str) -> Option<GitCommit> {
    let parts = raw
        .trim_end_matches(['\r', '\n'])
        .splitn(6, '\x1f')
        .collect::<Vec<_>>();
    if parts.len() != 6 {
        return None;
    }
    Some(GitCommit {
        id: parts[0].trim().to_owned(),
        short_id: parts[1].trim().to_owned(),
        author_name: parts[2].trim().to_owned(),
        authored_at: parts[3].trim().to_owned(),
        refs: parts[4].trim().to_owned(),
        subject: parts[5].trim().to_owned(),
        on_current_branch: None,
    })
}

fn parse_timeline_commits(output: &str) -> Vec<TimelineCommit> {
    output
        .lines()
        .filter_map(|line| {
            let parts = line.splitn(7, '\x1f').collect::<Vec<_>>();
            if parts.len() != 7 {
                return None;
            }
            Some(TimelineCommit {
                commit: GitCommit {
                    id: parts[0].trim().to_owned(),
                    short_id: parts[2].trim().to_owned(),
                    author_name: parts[3].trim().to_owned(),
                    authored_at: parts[4].trim().to_owned(),
                    refs: parts[5].trim().to_owned(),
                    subject: parts[6].trim().to_owned(),
                    on_current_branch: None,
                },
                parents: parts[1].split_whitespace().map(str::to_owned).collect(),
            })
        })
        .collect()
}

fn reachable_commits(head: &str, parents_by_id: &HashMap<String, Vec<String>>) -> HashSet<String> {
    if !parents_by_id.contains_key(head) {
        return HashSet::new();
    }
    let mut seen = HashSet::from([head.to_owned()]);
    let mut pending = VecDeque::from([head.to_owned()]);
    while let Some(current) = pending.pop_front() {
        for parent in parents_by_id.get(&current).into_iter().flatten() {
            if seen.insert(parent.clone()) {
                pending.push_back(parent.clone());
            }
        }
    }
    seen
}

fn topology_columns(
    commit_ids: &HashSet<String>,
    parents_by_id: &HashMap<String, Vec<String>>,
) -> HashMap<String, usize> {
    let mut children = HashMap::<String, Vec<String>>::new();
    let mut indegree = HashMap::<String, usize>::new();
    for commit_id in commit_ids {
        let parents = parents_by_id.get(commit_id).cloned().unwrap_or_default();
        indegree.insert(commit_id.clone(), parents.len());
        for parent in parents {
            children.entry(parent).or_default().push(commit_id.clone());
        }
    }
    let mut columns = commit_ids
        .iter()
        .map(|commit_id| (commit_id.clone(), 0usize))
        .collect::<HashMap<_, _>>();
    let mut ready = indegree
        .iter()
        .filter(|(_, degree)| **degree == 0)
        .map(|(commit_id, _)| commit_id.clone())
        .collect::<VecDeque<_>>();
    while let Some(current) = ready.pop_front() {
        let next_column = columns.get(&current).copied().unwrap_or_default() + 1;
        for child in children.get(&current).into_iter().flatten() {
            let column = columns.entry(child.clone()).or_default();
            *column = (*column).max(next_column);
            if let Some(degree) = indegree.get_mut(child) {
                *degree = degree.saturating_sub(1);
                if *degree == 0 {
                    ready.push_back(child.clone());
                }
            }
        }
    }
    columns
}

fn count_exclusive_commits(root: &Path, branch: &str, others: &[String]) -> Result<usize> {
    let mut args = vec![
        "rev-list".to_owned(),
        "--count".to_owned(),
        branch.to_owned(),
    ];
    if !others.is_empty() {
        args.push("--not".to_owned());
        args.extend(others.iter().cloned());
    }
    run_git_owned(root, &args)?
        .lines()
        .next()
        .context("git rev-list did not return an exclusive commit count")?
        .trim()
        .parse()
        .context("git rev-list returned an invalid exclusive commit count")
}

fn canonical_value(value: &Value, strip_volatile: bool) -> Value {
    match value {
        Value::Object(object) => {
            let mut sorted = BTreeMap::new();
            for (key, item) in object {
                if strip_volatile && GRAPH_CHECKSUM_VOLATILE_KEYS.contains(&key.as_str()) {
                    continue;
                }
                sorted.insert(key.clone(), canonical_value(item, strip_volatile));
            }
            Value::Object(sorted.into_iter().collect::<Map<String, Value>>())
        }
        Value::Array(items) => Value::Array(
            items
                .iter()
                .map(|item| canonical_value(item, strip_volatile))
                .collect(),
        ),
        _ => value.clone(),
    }
}

fn objects(value: Option<&Value>) -> Vec<Value> {
    value
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|item| item.is_object())
        .map(|item| canonical_value(item, true))
        .collect()
}

fn sorted_objects<F>(value: Option<&Value>, key: F) -> Vec<Value>
where
    F: Fn(&Value) -> String,
{
    let mut values = objects(value);
    values.sort_by_key(key);
    values
}

fn edge_sort_key(value: &Value) -> (String, String, String, String, String) {
    let object = value.as_object();
    let relation = object
        .and_then(|item| item.get("relationType").or_else(|| item.get("type")))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    (
        object
            .and_then(|item| item.get("source"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned(),
        object
            .and_then(|item| item.get("target"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned(),
        relation,
        object
            .and_then(|item| item.get("label"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned(),
        serde_json::to_string(value).unwrap_or_default(),
    )
}

fn hex_digest(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

fn pretty_json_bytes(value: &Value) -> Result<Vec<u8>> {
    let mut bytes = serde_json::to_vec_pretty(value).context("failed to encode projection JSON")?;
    bytes.push(b'\n');
    Ok(bytes)
}

fn canonical_existing_dir(path: &Path) -> Result<PathBuf> {
    let canonical = path
        .canonicalize()
        .with_context(|| format!("project root does not exist: {}", path.display()))?;
    ensure!(
        canonical.is_dir(),
        "project root is not a directory: {}",
        canonical.display()
    );
    Ok(canonical)
}

fn ensure_inside(root: &Path, target: &Path) -> Result<()> {
    let candidate = target
        .parent()
        .unwrap_or(target)
        .canonicalize()
        .unwrap_or_else(|_| target.to_path_buf());
    ensure!(
        path_is_within(root, &candidate),
        "path escapes project root: {}",
        target.display()
    );
    Ok(())
}

fn relative_to_project(root: &Path, target: &Path) -> String {
    target
        .strip_prefix(root)
        .unwrap_or(target)
        .to_string_lossy()
        .replace('\\', "/")
}

fn read_bytes_if_present(path: &Path) -> Result<Option<Vec<u8>>> {
    match fs::read(path) {
        Ok(bytes) => Ok(Some(bytes)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error).with_context(|| format!("failed to read {}", path.display())),
    }
}

fn read_json_if_present(path: &Path) -> Result<Option<Value>> {
    let Some(bytes) = read_bytes_if_present(path)? else {
        return Ok(None);
    };
    serde_json::from_slice(&bytes)
        .with_context(|| format!("invalid JSON in {}", path.display()))
        .map(Some)
}

fn temporary_path(path: &Path) -> Result<PathBuf> {
    temporary_path_with_suffix(path, ".tmp")
}

fn temporary_path_with_suffix(path: &Path, suffix: &str) -> Result<PathBuf> {
    let parent = path
        .ancestors()
        .find(|candidate| {
            candidate.file_name().and_then(|value| value.to_str()) == Some(".storydex")
        })
        .map(|storydex| storydex.join(".agent").join("temp").join("wiki-atomic"))
        .or_else(|| path.parent().map(Path::to_path_buf))
        .context("target path has no parent")?;
    fs::create_dir_all(&parent)?;
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("projection");
    for attempt in 0..32u32 {
        let candidate = parent.join(format!(
            ".{name}.storydex-{}-{attempt}{suffix}",
            unique_suffix()
        ));
        if !candidate.exists() {
            return Ok(candidate);
        }
    }
    bail!("unable to allocate temporary projection path")
}

fn write_and_sync(path: &Path, bytes: &[u8]) -> Result<()> {
    let mut file = OpenOptions::new().write(true).create_new(true).open(path)?;
    file.write_all(bytes)?;
    file.flush()?;
    file.sync_all()?;
    Ok(())
}

fn replace_path(source: &Path, target: &Path) -> io::Result<()> {
    #[cfg(windows)]
    {
        // `std::fs::rename` cannot replace an existing file on Windows.  The
        // projection lock serializes readers/writers; remove+rename keeps the
        // operation recoverable via the transaction rollback path.
        if target.exists() {
            fs::remove_file(target)?;
        }
    }
    fs::rename(source, target)
}

#[cfg(unix)]
fn sync_directory(path: &Path) -> Result<()> {
    File::open(path)?.sync_all()?;
    Ok(())
}

#[cfg(not(unix))]
fn sync_directory(_path: &Path) -> Result<()> {
    Ok(())
}

fn git_executable() -> String {
    std::env::var("STORYDEX_GIT_EXECUTABLE")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| "git".to_owned())
}

fn git_available() -> bool {
    Command::new(git_executable())
        .arg("--version")
        .output()
        .map(|output| output.status.success())
        .unwrap_or(false)
}

fn git_path_is_tracked(root: &Path, relative: &str) -> bool {
    Command::new(git_executable())
        .args(["ls-files", "--error-unmatch", "--", relative])
        .current_dir(root)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

fn tracked_internal_paths(root: &Path) -> Result<Vec<String>> {
    Ok(run_git_raw(
        root,
        &[
            "-c",
            "core.quotePath=false",
            "ls-files",
            "-z",
            "--",
            ".storydex/.agent",
            ".storydex/.cache",
        ],
    )?
    .split('\0')
    .map(str::trim)
    .filter(|path| !path.is_empty())
    .map(str::to_owned)
    .collect())
}

fn index_is_clean(root: &Path) -> Result<bool> {
    let output = Command::new(git_executable())
        .args(["diff", "--cached", "--quiet"])
        .current_dir(root)
        .output()
        .context("failed to inspect staged Git changes")?;
    match output.status.code() {
        Some(0) => Ok(true),
        Some(1) => Ok(false),
        _ => {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
            bail!("git diff --cached --quiet failed: {stderr}")
        }
    }
}

fn git_path_status(root: &Path, relative: &str) -> Result<String> {
    run_git(
        root,
        &[
            "-c",
            "core.quotePath=false",
            "status",
            "--porcelain=v1",
            "--",
            relative,
        ],
    )
}

fn run_git(root: &Path, args: &[&str]) -> Result<String> {
    Ok(run_git_raw(root, args)?.trim().to_owned())
}

fn run_git_raw(root: &Path, args: &[&str]) -> Result<String> {
    let output = Command::new(git_executable())
        .args(args)
        .current_dir(root)
        .output()
        .with_context(|| format!("failed to start git {}", args.join(" ")))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        bail!("git {} failed: {}", args.join(" "), stderr);
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

fn run_git_owned(root: &Path, args: &[String]) -> Result<String> {
    Ok(run_git_owned_raw(root, args)?.trim().to_owned())
}

fn run_git_owned_raw(root: &Path, args: &[String]) -> Result<String> {
    let borrowed = args.iter().map(String::as_str).collect::<Vec<_>>();
    run_git_raw(root, &borrowed)
}

fn repository_top_level(root: &Path) -> Result<Option<PathBuf>> {
    if !git_available() {
        return Ok(None);
    }
    let output = Command::new(git_executable())
        .args(["rev-parse", "--show-toplevel"])
        .current_dir(root)
        .output()
        .context("failed to inspect Git repository boundary")?;
    if !output.status.success() {
        return Ok(None);
    }
    let text = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    if text.is_empty() {
        Ok(None)
    } else {
        Ok(Some(PathBuf::from(text).canonicalize()?))
    }
}

fn same_path(left: &Path, right: &Path) -> bool {
    let left = left.to_string_lossy().replace('\\', "/");
    let right = right.to_string_lossy().replace('\\', "/");
    if cfg!(windows) {
        left.eq_ignore_ascii_case(&right)
    } else {
        left == right
    }
}

fn path_is_within(root: &Path, target: &Path) -> bool {
    if same_path(root, target) {
        return true;
    }
    let root = root.to_string_lossy().replace('\\', "/");
    let target = target.to_string_lossy().replace('\\', "/");
    let prefix = if root.ends_with('/') {
        root
    } else {
        format!("{root}/")
    };
    if cfg!(windows) {
        target
            .get(..prefix.len())
            .is_some_and(|value| value.eq_ignore_ascii_case(&prefix))
    } else {
        target.starts_with(&prefix)
    }
}

fn configure_local_identity(root: &Path) -> Result<()> {
    run_git(root, &["config", "user.name", "Storydex Local"])?;
    run_git(root, &["config", "user.email", "storydex@local"])?;
    Ok(())
}

fn ensure_gitignore(root: &Path) -> Result<bool> {
    let path = root.join(".gitignore");
    let mut existing = fs::read_to_string(&path).unwrap_or_default();
    let mut changed = false;
    for line in INTERNAL_PATH_PREFIXES {
        if !existing.lines().any(|value| value.trim() == line) {
            if !existing.is_empty() && !existing.ends_with('\n') {
                existing.push('\n');
            }
            existing.push_str(line);
            existing.push('\n');
            changed = true;
        }
    }
    if changed {
        fs::write(path, existing)?;
    }
    Ok(changed)
}

fn restore_file(path: &Path, original: Option<&[u8]>) -> Result<()> {
    if let Some(bytes) = original {
        fs::write(path, bytes)?;
    } else if path.exists() {
        fs::remove_file(path)?;
    }
    Ok(())
}

fn normalize_paths<I, S>(root: &Path, paths: I) -> Result<Vec<String>>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut normalized = BTreeSet::new();
    for raw in paths {
        let raw = raw.as_ref().trim();
        ensure!(!raw.is_empty(), "Git path must not be empty");
        let path = Path::new(raw);
        ensure!(!path.is_absolute(), "Git path must be relative: {raw}");
        ensure!(
            !path
                .components()
                .any(|component| matches!(component, Component::ParentDir)),
            "Git path must not escape the project root: {raw}"
        );
        let relative = raw.replace('\\', "/");
        ensure!(
            relative != ".git" && !relative.starts_with(".git/"),
            "Git metadata paths are not writable"
        );
        if relative.starts_with(".storydex/.agent/") || relative.starts_with(".storydex/.cache/") {
            continue;
        }
        let target = root.join(&relative);
        if target.exists() {
            let canonical = target.canonicalize()?;
            ensure!(
                path_is_within(root, &canonical),
                "Git path resolves outside project root: {relative}"
            );
        }
        normalized.insert(relative);
    }
    Ok(normalized.into_iter().collect())
}

fn parse_status(output: &str) -> (String, Vec<GitChangedFile>) {
    let mut branch = String::new();
    let mut changed = Vec::new();
    for line in output.lines() {
        if let Some(header) = line.strip_prefix("## ") {
            branch = if let Some(name) = header.strip_prefix("No commits yet on ") {
                name.trim().to_owned()
            } else if header.starts_with("HEAD (no branch)") {
                String::new()
            } else {
                header
                    .split_once("...")
                    .map_or(header, |(name, _)| name)
                    .trim()
                    .to_owned()
            };
            continue;
        }
        if line.len() < 3 {
            continue;
        }
        let status = line[..2].to_owned();
        let path = normalize_status_path(&line[3..]);
        let path = path.rsplit_once(" -> ").map_or(path.as_str(), |(_, to)| to);
        if path.is_empty()
            || INTERNAL_PATH_PREFIXES
                .iter()
                .any(|prefix| path.starts_with(prefix))
        {
            continue;
        }
        changed.push(GitChangedFile {
            status: status.clone(),
            relative_path: path.to_owned(),
            staged: status
                .as_bytes()
                .first()
                .is_some_and(|value| !matches!(value, b' ' | b'?')),
            unstaged: status.as_bytes().get(1).is_some_and(|value| *value != b' '),
        });
    }
    changed.sort_by(|left, right| left.relative_path.cmp(&right.relative_path));
    changed.dedup_by(|left, right| left.relative_path == right.relative_path);
    (branch, changed)
}

fn normalize_status_path(relative_path: &str) -> String {
    let normalized = relative_path.trim();
    let normalized = normalized
        .strip_prefix('"')
        .and_then(|value| value.strip_suffix('"'))
        .unwrap_or(normalized);
    normalized.replace("\\\"", "\"").replace('\\', "/")
}

fn unique_suffix() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_nanos());
    format!("{}-{}", std::process::id(), nanos)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::tempdir;

    #[test]
    fn source_checksum_is_order_and_separator_independent() {
        let first = vec![
            ProjectionSource {
                relative_path: "chapters\\002.md".into(),
                sha256: "b".into(),
                kind: "chapter".into(),
                title: String::new(),
                text: String::new(),
                size: 0,
                mtime: String::new(),
            },
            ProjectionSource {
                relative_path: "chapters/001.md".into(),
                sha256: "a".into(),
                kind: "chapter".into(),
                title: String::new(),
                text: String::new(),
                size: 0,
                mtime: String::new(),
            },
        ];
        let second = vec![first[1].clone(), first[0].clone()];
        assert_eq!(
            source_set_checksum(&first).expect("checksum"),
            source_set_checksum(&second).expect("checksum")
        );
        assert_eq!(
            source_set_checksum(&first).expect("python-compatible checksum"),
            "sha256:b857919c068c1b66865adc9a41b622ba0bb2f8cea935ead24dc97a8d7c33c7bc"
        );
    }

    #[test]
    fn bounded_path_rejects_sibling_prefixes() {
        let directory = tempdir().expect("boundary parent");
        let boundary = directory.path().join("fixture");
        let inside = boundary.join("project");
        let sibling = directory.path().join("fixture-other");
        fs::create_dir_all(&inside).expect("inside");
        fs::create_dir_all(&sibling).expect("sibling");
        assert_eq!(
            resolve_bounded_path(&boundary, &inside).expect("inside path"),
            inside.canonicalize().expect("canonical inside")
        );
        assert!(resolve_bounded_path(&boundary, &sibling).is_err());
    }

    #[test]
    fn git_status_parser_preserves_status_flags_and_unicode_paths() {
        let (branch, changed) = parse_status(
            "## No commits yet on develop\nR  chapters/旧章.md -> chapters/新章.md\n?? 设定/门派.md\n",
        );
        assert_eq!(branch, "develop");
        assert_eq!(changed.len(), 2);
        assert_eq!(changed[0].relative_path, "chapters/新章.md");
        assert_eq!(changed[0].status, "R ");
        assert!(changed[0].staged);
        assert!(!changed[0].unstaged);
        assert_eq!(changed[1].relative_path, "设定/门派.md");
        assert_eq!(changed[1].status, "??");
        assert!(!changed[1].staged);
        assert!(changed[1].unstaged);
    }

    #[test]
    fn graph_checksum_ignores_layout_and_timestamp_noise() {
        let left = json!({
            "generatedAt": "one",
            "entries": [{"id": "b", "updatedAt": "x", "title": "B"}, {"id": "a", "title": "A"}],
            "graph": {"nodes": [{"id": "n2", "x": 1}, {"id": "n1", "label": "one"}], "edges": [{"source": "n2", "target": "n1", "type": "knows"}]}
        });
        let right = json!({
            "lastUpdatedAt": "two",
            "entries": [{"title": "A", "id": "a"}, {"title": "B", "id": "b", "updatedAt": "different"}],
            "graph": {"nodes": [{"label": "one", "id": "n1", "y": 99}, {"id": "n2", "x": 4}], "edges": [{"type": "knows", "target": "n1", "source": "n2"}]}
        });
        assert_eq!(
            graph_checksum(&left).expect("checksum"),
            graph_checksum(&right).expect("checksum")
        );
    }

    #[test]
    fn projection_bundle_writes_all_files_and_reads_status() {
        let directory = tempdir().expect("temp directory");
        let writer = ProjectionBundleWriter::new(directory.path()).expect("writer");
        let result = writer
            .write(&ProjectionBundle {
                payload: json!({"schemaVersion": 3}),
                markdown: "# WIKI\n".into(),
                index: json!({"entries": []}),
                status: json!({"status": "ready", "lastSuccessfulRevision": 1}),
                source_snapshot: Some(json!({"sources": []})),
                change_set: None,
            })
            .expect("write bundle");
        assert_eq!(result.changed_paths.len(), 5);
        assert_eq!(
            writer.read_status().expect("status").expect("status value")["status"],
            "ready"
        );
        assert_eq!(
            fs::read_to_string(directory.path().join(".storydex/wiki/WIKI.md")).expect("markdown"),
            "# WIKI\n"
        );
    }

    #[test]
    fn projection_bundle_rolls_back_when_a_target_is_unwritable() {
        let directory = tempdir().expect("temp directory");
        let writer = ProjectionBundleWriter::new(directory.path()).expect("writer");
        writer
            .write(&ProjectionBundle {
                payload: json!({"version": 1}),
                markdown: "old\n".into(),
                index: json!({"version": 1}),
                status: json!({"status": "ready"}),
                source_snapshot: None,
                change_set: None,
            })
            .expect("initial bundle");
        // A directory at the target path makes replacement fail after the
        // other temporary files have already been prepared.
        let blocked = directory.path().join(".storydex/wiki/WIKI.md");
        fs::remove_file(&blocked).expect("remove markdown");
        fs::create_dir(&blocked).expect("block markdown");
        let error = writer.write(&ProjectionBundle {
            payload: json!({"version": 2}),
            markdown: "new\n".into(),
            index: json!({"version": 2}),
            status: json!({"status": "rebuilding"}),
            source_snapshot: None,
            change_set: None,
        });
        assert!(error.is_err());
        assert_eq!(
            fs::read_to_string(directory.path().join(".storydex/wiki/knowledge_graph.json"))
                .expect("old payload"),
            "{\n  \"version\": 1\n}\n"
        );
    }

    #[test]
    fn knowledge_sync_supports_cold_incremental_and_no_change_paths() {
        let directory = tempdir().expect("knowledge project");
        let chapters = directory.path().join("chapters");
        fs::create_dir_all(&chapters).expect("chapters");
        fs::write(chapters.join("001.md"), "# 第一章\n林澈抵达雾港。\n").expect("chapter");

        let knowledge = StorydexKnowledge::new(directory.path()).expect("knowledge service");
        let cold = knowledge.sync(false).expect("cold sync");
        assert_eq!(cold["knowledgeRevision"], 1);
        assert_eq!(cold["status"], "ready");
        assert_eq!(cold["noChanges"], false);
        assert!(
            cold["changedSourcePaths"]
                .as_array()
                .is_some_and(|paths| paths.iter().any(|path| path == "chapters/001.md"))
        );
        assert!(
            cold["changeSet"]["addedSourcePaths"]
                .as_array()
                .is_some_and(|paths| paths.iter().any(|path| path == "chapters/001.md"))
        );

        let wiki_root = directory.path().join(".storydex/wiki");
        let projection_files = [
            "knowledge_graph.json",
            "index.json",
            "WIKI.md",
            "projection_status.json",
            "source_snapshot.json",
            "change_set.json",
        ];
        let before_noop = projection_files
            .iter()
            .map(|name| {
                let path = wiki_root.join(name);
                (
                    name.to_string(),
                    fs::read(&path).expect("projection bytes"),
                    fs::metadata(&path)
                        .expect("projection metadata")
                        .modified()
                        .expect("projection modified time"),
                )
            })
            .collect::<Vec<_>>();
        let no_change = knowledge.sync(false).expect("no-op sync");
        assert_eq!(no_change["knowledgeRevision"], 1);
        assert_eq!(no_change["noChanges"], true);
        assert_eq!(no_change["changedSourcePaths"], json!([]));
        for (name, bytes, modified) in before_noop {
            let path = wiki_root.join(name);
            assert_eq!(fs::read(&path).expect("no-op bytes"), bytes);
            assert_eq!(
                fs::metadata(path)
                    .expect("no-op metadata")
                    .modified()
                    .expect("no-op modified time"),
                modified
            );
        }

        fs::write(chapters.join("001.md"), "# 第一章\n林澈离开雾港。\n").expect("chapter edit");
        let incremental = knowledge.sync(false).expect("incremental sync");
        assert_eq!(incremental["knowledgeRevision"], 2);
        assert_eq!(
            incremental["changeSet"]["modifiedSourcePaths"],
            json!(["chapters/001.md"])
        );
        assert_eq!(incremental["event"]["type"], "KnowledgeProjectionUpdated");
        let first_change_set_id = incremental["changeSet"]["changeSetId"]
            .as_str()
            .expect("first change set id")
            .to_owned();

        fs::write(chapters.join("001.md"), "# 第一章\n林澈返回雾港。\n")
            .expect("second chapter edit");
        let second_incremental = knowledge.sync(false).expect("second incremental sync");
        assert_eq!(second_incremental["knowledgeRevision"], 3);
        assert_ne!(
            second_incremental["changeSet"]["changeSetId"],
            first_change_set_id
        );
        assert_eq!(
            second_incremental["event"]["changeSetId"],
            second_incremental["changeSet"]["changeSetId"]
        );

        for name in ["knowledge_graph.json", "index.json", "WIKI.md"] {
            let path = wiki_root.join(name);
            if path.exists() {
                fs::remove_file(path).expect("remove projection file");
            }
        }
        let rebuilt = knowledge
            .sync(false)
            .expect("cold rebuild after cache loss");
        assert_eq!(rebuilt["knowledgeRevision"], 3);
        assert_eq!(
            rebuilt["graphChecksum"],
            second_incremental["graphChecksum"]
        );
    }

    #[test]
    fn knowledge_sync_failure_keeps_last_good_graph_and_records_error_status() {
        let directory = tempdir().expect("knowledge failure project");
        let chapters = directory.path().join("chapters");
        fs::create_dir_all(&chapters).expect("chapters");
        fs::write(chapters.join("001.md"), "# 第一章\n正文。\n").expect("chapter");
        let knowledge = StorydexKnowledge::new(directory.path()).expect("knowledge service");
        let baseline = knowledge.sync(false).expect("baseline");
        let blocked = directory.path().join(".storydex/wiki/WIKI.md");
        fs::remove_file(&blocked).expect("remove markdown");
        fs::create_dir(&blocked).expect("block markdown");
        fs::write(chapters.join("001.md"), "# 第一章\n故障注入。\n").expect("edit chapter");

        let error = knowledge.sync(false).expect_err("write must fail");
        assert!(
            error.to_string().contains("last-good") || error.to_string().contains("projection")
        );
        let status = ProjectionBundleWriter::new(directory.path())
            .expect("writer")
            .read_status()
            .expect("status read")
            .expect("status exists");
        assert_eq!(status["status"], "error");
        assert_eq!(
            status["lastSuccessfulRevision"],
            baseline["knowledgeRevision"]
        );
        assert!(
            directory
                .path()
                .join(".storydex/wiki/knowledge_graph.json")
                .is_file()
        );
    }

    #[test]
    fn knowledge_source_snapshot_keeps_stable_source_fields_and_exclusions() {
        let directory = tempdir().expect("knowledge compatibility project");
        fs::write(directory.path().join("README.md"), "framework notes\n").expect("readme");
        fs::create_dir_all(directory.path().join("chapters")).expect("chapters");
        fs::write(
            directory.path().join("chapters/001.md"),
            "# 第一章\n正文。\n",
        )
        .expect("chapter");
        fs::create_dir_all(directory.path().join(".storydex/characters")).expect("characters");
        fs::write(
            directory.path().join(".storydex/characters/林澈.md"),
            "# 林澈\n角色档案。\n",
        )
        .expect("character");
        fs::create_dir_all(directory.path().join(".storydex/scripts")).expect("scripts");
        fs::write(
            directory.path().join(".storydex/scripts/大纲.md"),
            "# 大纲\n规划剧情。\n",
        )
        .expect("script");
        fs::create_dir_all(directory.path().join(".storydex/config")).expect("config");
        fs::write(
            directory.path().join(".storydex/config/runtime.json"),
            "{\"ignored\":true}\n",
        )
        .expect("config");
        fs::create_dir_all(directory.path().join(".storydex/worldbook")).expect("worldbook");
        fs::write(
            directory.path().join(".storydex/worldbook/港口.json"),
            "{\n  \"name\": \"雾港\",\n  \"tags\": [\"setting\"]\n}\n",
        )
        .expect("worldbook JSON");

        StorydexKnowledge::new(directory.path())
            .expect("knowledge")
            .sync(false)
            .expect("sync");
        let snapshot =
            read_json_if_present(&directory.path().join(".storydex/wiki/source_snapshot.json"))
                .expect("snapshot read")
                .expect("snapshot exists");
        let sources = snapshot["sources"].as_object().expect("source map");
        assert_eq!(sources.len(), 4);
        assert_eq!(sources["chapters/001.md"]["kind"], "chapter");
        assert_eq!(sources[".storydex/characters/林澈.md"]["kind"], "character");
        assert_eq!(sources[".storydex/scripts/大纲.md"]["kind"], "planned");
        assert_eq!(sources[".storydex/worldbook/港口.json"]["kind"], "world");
        assert_eq!(
            sources[".storydex/worldbook/港口.json"]["text"],
            "{\n  \"name\": \"雾港\",\n  \"tags\": [\n    \"setting\"\n  ]\n}"
        );
        assert_eq!(sources["chapters/001.md"]["title"], "001");
        assert_eq!(sources["chapters/001.md"]["text"], "# 第一章\n正文。\n");
        assert!(
            sources["chapters/001.md"]["sha256"]
                .as_str()
                .is_some_and(|value| value.len() == 64 && !value.starts_with("sha256:"))
        );
        assert!(sources["chapters/001.md"]["size"].as_u64().is_some());
        assert!(
            sources["chapters/001.md"]["mtime"]
                .as_str()
                .is_some_and(|value| !value.is_empty())
        );
    }

    #[test]
    fn git_rejects_parent_repository_and_commits_temp_project() {
        if !git_available() {
            return;
        }
        let parent = tempdir().expect("parent");
        let child = parent.path().join("project");
        fs::create_dir(&child).expect("child");
        let git = StorydexGit;
        git.initialize(parent.path()).expect("parent init");
        let error = git
            .initialize(&child)
            .expect_err("nested repository must fail");
        assert!(error.to_string().contains("nested"));

        let standalone = tempdir().expect("standalone");
        let summary = git.initialize(standalone.path()).expect("init");
        assert!(summary.initialized);
        fs::write(standalone.path().join("chapter.md"), "正文\n").expect("write chapter");
        let committed = git
            .commit_paths(standalone.path(), ["chapter.md"], "故事：初始章节")
            .expect("commit");
        assert!(committed.created);
        assert!(committed.commit.is_some());
        assert!(committed.summary.clean);
    }

    #[test]
    fn git_restore_creates_backup_and_removes_untracked_files() {
        if !git_available() {
            return;
        }
        let directory = tempdir().expect("project");
        let git = StorydexGit;
        git.initialize(directory.path()).expect("init");
        fs::write(directory.path().join("chapter.md"), "一\n").expect("write first");
        let first = git
            .commit_all(directory.path(), "故事：第一版")
            .expect("first commit");
        let first_id = first.commit.as_ref().expect("first id").id.clone();
        fs::write(directory.path().join("chapter.md"), "二\n").expect("write second");
        let second = git
            .commit_all(directory.path(), "故事：第二版")
            .expect("second commit");
        fs::write(directory.path().join("scratch.tmp"), "discard\n").expect("scratch");
        let restored = git
            .restore_to_commit(directory.path(), &first_id, true)
            .expect("restore");
        assert!(restored.restored);
        assert_eq!(
            restored
                .restored_commit
                .as_ref()
                .map(|commit| commit.id.as_str()),
            Some(first_id.as_str())
        );
        let backup_commit = restored.backup_commit.as_ref().expect("backup commit");
        assert!(
            backup_commit
                .subject
                .starts_with("workspace: backup before restore to ")
        );
        assert!(restored.backup_ref.starts_with("storydex-backup-"));
        assert_eq!(
            run_git(directory.path(), &["rev-parse", &restored.backup_ref]).expect("backup ref"),
            backup_commit.id
        );
        assert_eq!(
            restored
                .summary
                .head
                .as_ref()
                .map(|commit| commit.id.as_str()),
            Some(first_id.as_str())
        );
        let restored_text = fs::read_to_string(directory.path().join("chapter.md"))
            .expect("chapter")
            .replace("\r\n", "\n");
        assert_eq!(restored_text, "一\n");
        assert!(!directory.path().join("scratch.tmp").exists());
        assert_ne!(second.commit, first.commit);

        let no_op = git
            .restore_to_commit(directory.path(), &first_id, true)
            .expect("no-op restore");
        assert!(!no_op.restored);
        assert_eq!(
            no_op
                .restored_commit
                .as_ref()
                .map(|commit| commit.id.as_str()),
            Some(first_id.as_str())
        );
        assert!(no_op.backup_commit.is_none());
        assert!(no_op.backup_ref.is_empty());
        assert!(no_op.summary.clean);
    }

    #[test]
    fn git_initialize_untracks_legacy_internal_paths_and_keeps_disk_files() {
        if !git_available() {
            return;
        }
        let directory = tempdir().expect("legacy project");
        let root = directory.path();
        run_git(root, &["init"]).expect("init legacy repository");
        run_git(root, &["branch", "-M", DEFAULT_BRANCH]).expect("legacy branch");
        configure_local_identity(root).expect("legacy identity");
        fs::create_dir_all(root.join(".storydex/.agent/session")).expect("agent directory");
        fs::create_dir_all(root.join(".storydex/.cache")).expect("cache directory");
        fs::write(root.join("story.md"), "legacy story\n").expect("legacy story");
        fs::write(
            root.join(".storydex/.agent/session/state.json"),
            b"agent-v1",
        )
        .expect("legacy agent state");
        fs::write(root.join(".storydex/.cache/retrieval.db"), b"cache-v1").expect("legacy cache");
        run_git(root, &["add", "-A"]).expect("stage legacy files");
        run_git(
            root,
            &["commit", "--no-gpg-sign", "-m", "legacy: tracked internals"],
        )
        .expect("commit legacy files");

        fs::write(root.join("story.md"), "local story edit\n").expect("dirty story");
        fs::write(
            root.join(".storydex/.agent/session/state.json"),
            b"agent-v2-kept",
        )
        .expect("updated agent state");
        fs::write(root.join(".storydex/.cache/retrieval.db"), b"cache-v2-kept")
            .expect("updated cache");

        let git = StorydexGit;
        let summary = git.initialize(root).expect("migrate legacy internals");
        let migration_head = summary.head.as_ref().expect("migration head").id.clone();
        assert_eq!(
            summary.head.as_ref().map(|commit| commit.subject.as_str()),
            Some("chore: untrack internal Storydex paths")
        );
        assert_eq!(summary.changed_paths, vec!["story.md"]);
        assert_eq!(
            fs::read(root.join(".storydex/.agent/session/state.json")).expect("agent state"),
            b"agent-v2-kept"
        );
        assert_eq!(
            fs::read(root.join(".storydex/.cache/retrieval.db")).expect("cache"),
            b"cache-v2-kept"
        );
        assert!(
            run_git(
                root,
                &["ls-files", "--", ".storydex/.agent", ".storydex/.cache"]
            )
            .expect("tracked internals")
            .is_empty()
        );
        let ignore = fs::read_to_string(root.join(".gitignore")).expect("gitignore");
        assert!(ignore.contains(".storydex/.agent/"));
        assert!(ignore.contains(".storydex/.cache/"));
        let migration_paths =
            run_git(root, &["show", "--format=", "--name-only", "HEAD"]).expect("migration paths");
        assert!(migration_paths.contains(".gitignore"));
        assert!(migration_paths.contains(".storydex/.agent/session/state.json"));
        assert!(migration_paths.contains(".storydex/.cache/retrieval.db"));

        let repeated = git.initialize(root).expect("repeat migration");
        assert_eq!(
            repeated.head.as_ref().map(|commit| commit.id.as_str()),
            Some(migration_head.as_str())
        );
    }

    #[test]
    fn git_initialize_refuses_legacy_migration_with_staged_user_changes() {
        if !git_available() {
            return;
        }
        let directory = tempdir().expect("legacy project");
        let root = directory.path();
        run_git(root, &["init"]).expect("init legacy repository");
        run_git(root, &["branch", "-M", DEFAULT_BRANCH]).expect("legacy branch");
        configure_local_identity(root).expect("legacy identity");
        fs::create_dir_all(root.join(".storydex/.cache")).expect("cache directory");
        fs::write(root.join("story.md"), "baseline\n").expect("story");
        fs::write(root.join(".storydex/.cache/retrieval.db"), b"cache-kept").expect("cache");
        run_git(root, &["add", "-A"]).expect("stage legacy files");
        run_git(
            root,
            &["commit", "--no-gpg-sign", "-m", "legacy: tracked cache"],
        )
        .expect("commit legacy files");
        let original_head = run_git(root, &["rev-parse", "HEAD"]).expect("original head");

        fs::write(root.join("story.md"), "staged user edit\n").expect("user edit");
        run_git(root, &["add", "--", "story.md"]).expect("stage user edit");
        let error = StorydexGit
            .initialize(root)
            .expect_err("staged user changes must block migration");
        assert!(error.to_string().contains("staged changes"));
        assert_eq!(
            run_git(root, &["rev-parse", "HEAD"]).expect("head after refusal"),
            original_head
        );
        assert_eq!(
            run_git(root, &["ls-files", "--", ".storydex/.cache/retrieval.db"])
                .expect("tracked cache"),
            ".storydex/.cache/retrieval.db"
        );
        assert_eq!(
            fs::read(root.join(".storydex/.cache/retrieval.db")).expect("cache kept"),
            b"cache-kept"
        );
        assert!(!root.join(".gitignore").exists());
    }

    #[test]
    fn git_commit_paths_ignores_only_stale_missing_paths() {
        if !git_available() {
            return;
        }
        let directory = tempdir().expect("project");
        let git = StorydexGit;
        git.initialize(directory.path()).expect("init");
        fs::write(directory.path().join("baseline.md"), "baseline\n").expect("baseline");
        git.commit_all(directory.path(), "故事：基线")
            .expect("baseline commit");
        let result = git
            .commit_paths(
                directory.path(),
                [".storydex/temp/already-removed.md"],
                "无变更",
            )
            .expect("stale path should be a no-op");
        assert!(!result.created);
        assert!(result.summary.clean);
    }

    #[test]
    fn git_diff_includes_tracked_and_unicode_untracked_files() {
        if !git_available() {
            return;
        }
        let directory = tempdir().expect("project");
        let git = StorydexGit;
        git.initialize(directory.path()).expect("init");
        fs::write(directory.path().join("chapter.md"), "第一行\n第二行\n")
            .expect("baseline chapter");
        git.commit_all(directory.path(), "故事：差分基线")
            .expect("baseline commit");

        fs::write(directory.path().join("chapter.md"), "第一行\n改写行\n")
            .expect("changed chapter");
        fs::write(directory.path().join("新章.md"), "新内容\n第二段\n").expect("unicode chapter");
        let diff = git.diff(directory.path()).expect("working tree diff");
        assert_eq!(diff.totals.files, 2);
        let tracked = diff
            .files
            .iter()
            .find(|item| item.relative_path == "chapter.md")
            .expect("tracked diff");
        assert_eq!(tracked.added, 1);
        assert_eq!(tracked.removed, 1);
        assert!(tracked.hunks.iter().any(|hunk| {
            hunk.lines
                .iter()
                .any(|line| line.kind == "added" && line.content == "改写行")
        }));
        let untracked = diff
            .files
            .iter()
            .find(|item| item.relative_path == "新章.md")
            .expect("unicode untracked diff");
        assert_eq!(untracked.status, "??");
        assert_eq!(untracked.added, 2);
        assert_eq!(untracked.hunks[0].lines[0].content, "新内容");
    }

    #[test]
    fn git_commit_diff_reports_the_selected_commit_only() {
        if !git_available() {
            return;
        }
        let directory = tempdir().expect("project");
        let git = StorydexGit;
        git.initialize(directory.path()).expect("init");
        fs::write(directory.path().join("chapter.md"), "旧版本\n").expect("first version");
        git.commit_all(directory.path(), "故事：旧版本")
            .expect("first commit");
        fs::write(directory.path().join("chapter.md"), "新版本\n").expect("second version");
        let second = git
            .commit_all(directory.path(), "故事：新版本")
            .expect("second commit");
        let second_id = &second.commit.as_ref().expect("second id").id;

        let diff = git
            .commit_diff(directory.path(), second_id)
            .expect("commit diff");
        assert_eq!(diff.totals.files, 1);
        assert_eq!(diff.files[0].relative_path, "chapter.md");
        assert_eq!(diff.files[0].added, 1);
        assert_eq!(diff.files[0].removed, 1);
    }

    #[test]
    fn git_branches_timeline_jump_and_worldlines_are_bounded() {
        if !git_available() {
            return;
        }
        let directory = tempdir().expect("project");
        let git = StorydexGit;
        git.initialize(directory.path()).expect("init");
        fs::write(directory.path().join("story.md"), "共同前史\n").expect("baseline");
        let baseline = git
            .commit_all(directory.path(), "故事：共同前史")
            .expect("baseline commit");
        let baseline_id = baseline.commit.as_ref().expect("baseline id").id.clone();

        let branches = git
            .create_branch(directory.path(), "alternate", true)
            .expect("create alternate");
        assert_eq!(branches.current, "alternate");
        fs::write(directory.path().join("story.md"), "支线版本\n").expect("alternate story");
        let alternate = git
            .commit_all(directory.path(), "故事：支线版本")
            .expect("alternate commit");
        let alternate_id = alternate.commit.as_ref().expect("alternate id").id.clone();

        git.switch_branch(directory.path(), DEFAULT_BRANCH)
            .expect("switch develop");
        fs::write(directory.path().join("story.md"), "主线版本\n").expect("develop story");
        let develop = git
            .commit_all(directory.path(), "故事：主线版本")
            .expect("develop commit");
        let develop_id = develop.commit.as_ref().expect("develop id").id.clone();

        let timeline = git.timeline(directory.path()).expect("timeline");
        assert_eq!(timeline.current_branch, DEFAULT_BRANCH);
        assert_eq!(timeline.nodes.len(), 3);
        assert_eq!(
            timeline
                .branches
                .iter()
                .find(|branch| branch.name == DEFAULT_BRANCH)
                .expect("develop branch")
                .lane,
            0
        );
        for name in [DEFAULT_BRANCH, "alternate"] {
            let branch = timeline
                .branches
                .iter()
                .find(|branch| branch.name == name)
                .expect("timeline branch");
            assert_eq!(branch.fork_column, 1);
            assert_eq!(branch.tip_column, 1);
            assert_eq!(branch.commit_count, 1);
            assert_eq!(branch.total_count, 2);
        }
        assert_eq!(
            timeline
                .nodes
                .iter()
                .find(|node| node.id == baseline_id)
                .expect("baseline node")
                .column,
            0
        );

        let observed = git
            .jump_to_commit(directory.path(), &baseline_id)
            .expect("jump historical");
        assert!(observed.detached);
        assert!(observed.branch.is_empty());
        let landed = git
            .jump_to_commit(directory.path(), &alternate_id)
            .expect("jump branch tip");
        assert!(!landed.detached);
        assert_eq!(landed.branch, "alternate");

        fs::write(directory.path().join("dirty.md"), "未提交\n").expect("dirty file");
        let dirty_error = git
            .switch_branch(directory.path(), DEFAULT_BRANCH)
            .expect_err("dirty checkout must fail");
        assert!(dirty_error.to_string().contains("uncommitted"));
        fs::remove_file(directory.path().join("dirty.md")).expect("remove dirty file");

        let worldline = git
            .create_worldline(directory.path(), &baseline_id, "rewrite")
            .expect("create worldline");
        assert_eq!(worldline.worldline.as_deref(), Some("rewrite"));
        assert_eq!(worldline.from_commit.as_deref(), Some(baseline_id.as_str()));
        let renamed = git
            .rename_worldline(directory.path(), "rewrite", "rewrite-v2")
            .expect("rename worldline");
        assert_eq!(renamed.current, "rewrite-v2");
        assert_eq!(renamed.renamed_from.as_deref(), Some("rewrite"));
        assert_eq!(renamed.renamed_to.as_deref(), Some("rewrite-v2"));

        git.switch_branch(directory.path(), DEFAULT_BRANCH)
            .expect("return develop");
        let deleted = git
            .delete_worldline(directory.path(), "alternate")
            .expect("delete alternate");
        assert_eq!(deleted.deleted.as_deref(), Some("alternate"));
        assert_eq!(deleted.exclusive_commits, Some(1));
        assert!(
            deleted
                .branches
                .iter()
                .all(|branch| branch.name != "alternate")
        );
        assert_eq!(
            deleted.summary.head.as_ref().map(|head| &head.id),
            Some(&develop_id)
        );
    }
}
