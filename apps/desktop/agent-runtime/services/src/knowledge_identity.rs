//! Persistent source identity, independent of the generated WIKI and file names.
use super::{ProjectionSource, hex_digest, unique_suffix};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, HashSet};

#[derive(Default, Serialize, Deserialize)]
pub(super) struct SourceIdentities {
    pub sources: BTreeMap<String, SourceIdentity>,
    #[serde(default)]
    relation_sources: BTreeMap<String, Vec<SourceBinding>>,
}

#[derive(Serialize, Deserialize)]
struct SourceBinding {
    reference: Value,
    source_id: Option<String>,
}

#[derive(Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(super) struct SourceIdentity {
    pub path: String,
    pub sha256: String,
    #[serde(default)]
    pub previous_paths: Vec<String>,
    #[serde(default)]
    pub missing: bool,
}

impl SourceIdentities {
    pub fn bind_references(&mut self, relation: &str, refs: &[Value]) -> Vec<Option<String>> {
        let bindings: Vec<_> = refs
            .iter()
            .map(|reference| {
                let previous = self.relation_sources.get(relation).and_then(|bindings| {
                    bindings
                        .iter()
                        .find(|binding| binding.reference == *reference)
                });
                let source_id = reference["sourceId"]
                    .as_str()
                    .map(str::to_owned)
                    .or_else(|| previous.and_then(|binding| binding.source_id.clone()))
                    .or_else(|| {
                        reference["path"]
                            .as_str()
                            .and_then(|path| self.resolve_path(&path.replace('\\', "/")))
                            .map(str::to_owned)
                    });
                SourceBinding {
                    reference: reference.clone(),
                    source_id,
                }
            })
            .collect();
        let ids = bindings
            .iter()
            .map(|binding| binding.source_id.clone())
            .collect();
        self.relation_sources.insert(relation.to_owned(), bindings);
        ids
    }

    pub fn from_snapshot(snapshot: Option<&Value>) -> Self {
        let mut result = Self::default();
        if let Some(sources) = snapshot.and_then(|s| s["sources"].as_object()) {
            for (path, source) in sources {
                if path.starts_with(".storydex/knowledge/") {
                    continue;
                }
                result.sources.insert(
                    source["sourceId"]
                        .as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| legacy_id(path)),
                    SourceIdentity {
                        path: path.clone(),
                        sha256: source["sha256"].as_str().unwrap_or_default().to_owned(),
                        previous_paths: Vec::new(),
                        missing: false,
                    },
                );
            }
        }
        result
    }

    pub fn reconcile(&mut self, sources: &[ProjectionSource]) {
        let sources: Vec<_> = sources
            .iter()
            .filter(|s| !s.relative_path.starts_with(".storydex/knowledge/"))
            .collect();
        let mut assigned = HashSet::new();
        // Existing paths win. Identical copies must never steal their identity.
        for source in &sources {
            if let Some((id, record)) = self
                .sources
                .iter_mut()
                .find(|(_, r)| !r.missing && r.path == source.relative_path)
            {
                record.sha256 = source.sha256.clone();
                assigned.insert(id.clone());
            }
        }
        let new_sources: Vec<_> = sources
            .iter()
            .filter(|s| self.id_at(&s.relative_path).is_none())
            .collect();
        let mut matches = Vec::new();
        // External renames are recognized only for a unique, unchanged source.
        // Ambiguous copies or rename+rewrite operations require explicit identity.
        for source in new_sources {
            let candidates: Vec<_> = self
                .sources
                .iter()
                .filter(|(id, r)| !assigned.contains(*id) && r.sha256 == source.sha256)
                .map(|(id, _)| id.clone())
                .collect();
            let same_content = sources
                .iter()
                .filter(|s| s.sha256 == source.sha256 && self.id_at(&s.relative_path).is_none())
                .count();
            if candidates.len() == 1 && same_content == 1 {
                matches.push((candidates[0].clone(), source.relative_path.clone()));
            }
        }
        for (id, path) in matches {
            self.move_identity(&id, &path);
            assigned.insert(id);
        }
        for source in sources {
            if self.id_at(&source.relative_path).is_some() {
                continue;
            }
            let mut id = legacy_id(&source.relative_path);
            if self.sources.contains_key(&id) {
                id = format!(
                    "source:{}",
                    &hex_digest(format!("{}:{}", source.relative_path, unique_suffix()).as_bytes())
                        [..16]
                );
            }
            assigned.insert(id.clone());
            self.sources.insert(
                id,
                SourceIdentity {
                    path: source.relative_path.clone(),
                    sha256: source.sha256.clone(),
                    previous_paths: Vec::new(),
                    missing: false,
                },
            );
        }
        for (id, record) in &mut self.sources {
            record.missing = !assigned.contains(id);
        }
    }

    pub fn id_at(&self, path: &str) -> Option<&str> {
        self.sources
            .iter()
            .find(|(_, r)| !r.missing && r.path == path)
            .map(|(id, _)| id.as_str())
    }

    pub fn resolve_path(&self, path: &str) -> Option<&str> {
        if let Some(id) = self.id_at(path) {
            return Some(id);
        }
        let mut matches = self
            .sources
            .iter()
            .filter(|(_, r)| r.path == path || r.previous_paths.iter().any(|p| p == path));
        let (id, _) = matches.next()?;
        matches.next().is_none().then_some(id.as_str())
    }

    pub fn current_path(&self, id: &str) -> Option<&str> {
        self.sources
            .get(id)
            .filter(|r| !r.missing)
            .map(|r| r.path.as_str())
    }

    pub fn move_paths(&mut self, from: &str, to: &str) {
        let moves: Vec<_> = self
            .sources
            .iter()
            .filter(|(_, r)| !r.missing)
            .filter_map(|(id, r)| {
                if r.path == from {
                    Some((id.clone(), to.to_owned()))
                } else {
                    r.path
                        .strip_prefix(&format!("{from}/"))
                        .map(|suffix| (id.clone(), format!("{to}/{suffix}")))
                }
            })
            .collect();
        for (id, path) in moves {
            self.move_identity(&id, &path);
        }
    }

    fn move_identity(&mut self, id: &str, path: &str) {
        // A patch may replace an existing destination. Its old identity must
        // remain missing instead of attaching its relations to the replacement.
        for (other_id, record) in &mut self.sources {
            if other_id != id && record.path == path {
                record.missing = true;
            }
        }
        let record = self.sources.get_mut(id).expect("known source identity");
        if record.path != path && !record.previous_paths.contains(&record.path) {
            record.previous_paths.push(record.path.clone());
        }
        record.path = path.to_owned();
        record.missing = false;
    }
}

fn legacy_id(path: &str) -> String {
    format!("source:{}", &hex_digest(path.as_bytes())[..16])
}
