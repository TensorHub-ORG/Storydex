//! Recovery for file-tool edits in a failed Storydex turn.
use anyhow::{Context, Result};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Default)]
pub(crate) struct EditJournal {
    changes: BTreeMap<PathBuf, Change>,
}

struct Change {
    before: Option<Vec<u8>>,
    after: Option<Vec<u8>>,
    concurrent: bool,
}

impl EditJournal {
    pub(crate) fn record(&mut self, path: &Path, before: Option<Vec<u8>>, after: Option<Vec<u8>>) {
        if let Some(change) = self.changes.get_mut(path) {
            change.concurrent |= change.after != before;
            change.after = after;
        } else {
            self.changes.insert(
                path.to_owned(),
                Change {
                    before,
                    after,
                    concurrent: false,
                },
            );
        }
    }

    pub(crate) fn recover(&mut self, root: &Path) -> Result<Option<Value>> {
        if self.changes.is_empty() {
            return Ok(None);
        }
        let _lock = coomi_services::ProjectWriteLock::acquire(root)?;
        let policy =
            coomi_security::SecurityPolicy::new(root, coomi_security::AccessMode::WorkspaceWrite)?;
        let archive = root
            .join(".storydex/.agent/failed-edits")
            .join(uuid::Uuid::new_v4().to_string());
        fs::create_dir_all(&archive)?;
        let mut entries = Vec::new();
        let mut conflict = false;
        for (index, (path, change)) in self.changes.iter().enumerate() {
            let bounded = policy.resolve_path(path)?;
            anyhow::ensure!(bounded.starts_with(root), "recovery path outside workspace");
            let current = match fs::read(&bounded) {
                Ok(bytes) => Some(bytes),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
                Err(error) => return Err(error.into()),
            };
            conflict |= change.concurrent || current != change.after;
            if let Some(before) = &change.before {
                fs::write(archive.join(format!("{index}.before")), before)?;
            }
            if let Some(after) = &change.after {
                fs::write(archive.join(format!("{index}.draft")), after)?;
            }
            entries.push(
                json!({"path": path.strip_prefix(root)?.to_string_lossy().replace('\\', "/"),
                "before": change.before.as_ref().map(|_| format!("{index}.before")),
                "draft": change.after.as_ref().map(|_| format!("{index}.draft"))}),
            );
        }
        let mut result = json!({"archive": archive.strip_prefix(root)?.to_string_lossy().replace('\\', "/"),
            "restored": false, "conflict": conflict, "files": entries});
        let manifest = archive.join("manifest.json");
        fs::write(&manifest, serde_json::to_vec_pretty(&result)?)?;
        // Preserve all files if another writer intervened; never roll back half
        // of a rename or replace the author's newer work.
        if !conflict {
            for (path, change) in &self.changes {
                match &change.before {
                    Some(bytes) => {
                        if let Some(parent) = path.parent() {
                            fs::create_dir_all(parent)?;
                        }
                        fs::write(path, bytes)
                            .with_context(|| format!("restore {}", path.display()))?;
                    }
                    None if path.exists() => fs::remove_file(path)?,
                    None => {}
                }
            }
            result["restored"] = json!(true);
            fs::write(&manifest, serde_json::to_vec_pretty(&result)?)?;
        }
        self.changes.clear();
        Ok(Some(result))
    }
}
