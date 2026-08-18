//! Storydex project primitives shared by the Rust backend.
//!
//! This module deliberately stops at the project boundary.  It does not own
//! HTTP routes or the WIKI graph builder; it provides the deterministic file
//! and Git contracts those layers use.  Keeping the primitives here makes it
//! possible to migrate callers incrementally without introducing a second
//! source of truth.

use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
#[cfg(unix)]
use std::fs::File;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

const WIKI_ROOT: &str = ".storydex/wiki";
const INTERNAL_PATH_PREFIXES: [&str; 2] = [".storydex/.agent/", ".storydex/.cache/"];
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
}

/// The four generated WIKI files and the optional source snapshot are written
/// as one transaction.  Callers build the semantic payload; this type only
/// handles deterministic serialization and replacement.
#[derive(Clone, Debug, Default)]
pub struct ProjectionBundle {
    pub payload: Value,
    pub markdown: String,
    pub index: Value,
    pub status: Value,
    pub source_snapshot: Option<Value>,
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
        self.write_targets(targets)
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
                originals.insert(target.clone(), read_bytes_if_present(target)?);
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
    pub initialized: bool,
    pub branch: String,
    pub clean: bool,
    pub changed_paths: Vec<String>,
    pub changed_files: Vec<GitChangedFile>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub head: Option<String>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GitCommitResult {
    pub created: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub commit: Option<String>,
    pub summary: GitSummary,
}

impl StorydexGit {
    pub fn initialize(&self, project_root: impl AsRef<Path>) -> Result<GitSummary> {
        let root = canonical_existing_dir(project_root.as_ref())?;
        if repository_top_level(&root)?.is_none() {
            run_git(&root, &["init"])?;
            // Keep the historical Storydex branch name on fresh repositories.
            let _ = run_git(&root, &["branch", "-M", "develop"]);
        } else {
            self.assert_project_repository(&root)?;
        }
        self.assert_project_repository(&root)?;
        configure_local_identity(&root)?;
        ensure_gitignore(&root)?;
        self.summary(&root)
    }

    pub fn summary(&self, project_root: impl AsRef<Path>) -> Result<GitSummary> {
        let root = canonical_existing_dir(project_root.as_ref())?;
        let Some(top) = repository_top_level(&root)? else {
            return Ok(GitSummary {
                available: git_available(),
                initialized: false,
                branch: "develop".to_owned(),
                clean: true,
                ..GitSummary::default()
            });
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
        let head = run_git(&root, &["rev-parse", "HEAD"]).ok();
        Ok(GitSummary {
            available: true,
            initialized: true,
            branch,
            clean: changed_files.is_empty(),
            changed_paths,
            changed_files,
            head,
        })
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
    ) -> Result<GitSummary> {
        let root = canonical_existing_dir(project_root.as_ref())?;
        self.assert_project_repository(&root)?;
        ensure!(!commit.trim().is_empty(), "target commit id is required");
        run_git(&root, &["cat-file", "-e", &format!("{commit}^{{commit}}")])?;
        let current = run_git(&root, &["rev-parse", "HEAD"])?;
        if create_backup {
            let suffix = unique_suffix();
            let name = format!("storydex-backup-{suffix}");
            run_git(&root, &["branch", &name, &current])?;
        }
        run_git(&root, &["reset", "--hard", commit])?;
        run_git(&root, &["clean", "-fd"])?;
        self.summary(&root)
    }

    fn assert_project_repository(&self, root: &Path) -> Result<()> {
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
            });
        }
        let message = if message.trim().is_empty() {
            "storydex: local snapshot"
        } else {
            message.trim()
        };
        run_git(root, &["commit", "--no-gpg-sign", "-m", message])?;
        let commit = run_git(root, &["rev-parse", "HEAD"])?;
        Ok(GitCommitResult {
            created: true,
            commit: Some(commit),
            summary: self.summary(root)?,
        })
    }
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

fn run_git(root: &Path, args: &[&str]) -> Result<String> {
    ensure!(git_available(), "Storydex Git executable is not available");
    let output = Command::new(git_executable())
        .args(args)
        .current_dir(root)
        .output()
        .with_context(|| format!("failed to start git {}", args.join(" ")))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        bail!("git {} failed: {}", args.join(" "), stderr);
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
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

fn ensure_gitignore(root: &Path) -> Result<()> {
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
            },
            ProjectionSource {
                relative_path: "chapters/001.md".into(),
                sha256: "a".into(),
                kind: "chapter".into(),
            },
        ];
        let second = vec![first[1].clone(), first[0].clone()];
        assert_eq!(
            source_set_checksum(&first).expect("checksum"),
            source_set_checksum(&second).expect("checksum")
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
        });
        assert!(error.is_err());
        assert_eq!(
            fs::read_to_string(directory.path().join(".storydex/wiki/knowledge_graph.json"))
                .expect("old payload"),
            "{\n  \"version\": 1\n}\n"
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
        fs::write(directory.path().join("chapter.md"), "二\n").expect("write second");
        let second = git
            .commit_all(directory.path(), "故事：第二版")
            .expect("second commit");
        fs::write(directory.path().join("scratch.tmp"), "discard\n").expect("scratch");
        let restored = git
            .restore_to_commit(
                directory.path(),
                first.commit.as_deref().expect("first id"),
                true,
            )
            .expect("restore");
        assert_eq!(restored.head.as_deref(), first.commit.as_deref());
        let restored_text = fs::read_to_string(directory.path().join("chapter.md"))
            .expect("chapter")
            .replace("\r\n", "\n");
        assert_eq!(restored_text, "一\n");
        assert!(!directory.path().join("scratch.tmp").exists());
        assert_ne!(second.commit, first.commit);
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
}
