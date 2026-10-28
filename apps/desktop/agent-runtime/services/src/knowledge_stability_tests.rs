use super::*;

fn write_json(root: &Path, path: &str, value: &Value) {
    fs::write(root.join(path), serde_json::to_vec(value).expect("JSON")).expect("write fixture");
}

fn node_id(graph: &Value, label: &str) -> Value {
    graph["graph"]["nodes"]
        .as_array()
        .expect("nodes")
        .iter()
        .find(|node| node["label"] == label)
        .expect("node")["id"]
        .clone()
}

fn fixture() -> (tempfile::TempDir, StorydexKnowledge, Value) {
    let dir = tempfile::tempdir().expect("directory");
    let root = dir.path();
    fs::create_dir_all(root.join("characters")).expect("characters");
    fs::create_dir_all(root.join("chapters")).expect("chapters");
    fs::create_dir_all(root.join(".storydex/knowledge")).expect("knowledge");
    fs::write(root.join("characters/a.md"), "Alice profile").expect("a");
    fs::write(root.join("characters/b.md"), "Bob profile").expect("b");
    fs::write(root.join("chapters/1.md"), "Alice trusts Bob.").expect("chapter");
    let service = StorydexKnowledge::new(root).expect("knowledge");
    let graph = service.sync(false).expect("initial sync");
    let candidate = json!({"id": "trust", "subjectId": node_id(&graph, "a"), "objectId": node_id(&graph, "b"),
        "predicate": "trusts", "sourceRefs": [{"path": "chapters/1.md", "quote": "Alice trusts Bob."}]});
    write_json(
        root,
        ".storydex/knowledge/relations.json",
        &json!({"relations": [candidate]}),
    );
    let pending = service.sync(false).expect("candidate");
    let confirmed = service
        .review_relation(
            "trust",
            pending["relations"][0]["fingerprint"]
                .as_str()
                .expect("fingerprint"),
            "confirmed",
            &json!({"predicate": "信任"}),
        )
        .expect("confirm");
    (dir, service, confirmed)
}

#[test]
fn knowledge_moves_keep_identity_evidence_review_and_survive_projection_rebuild() {
    let (dir, service, initial) = fixture();
    let root = dir.path();
    service
        .move_source("characters/a.md", "characters/alice.md")
        .expect("rename character");
    // The editor may rewrite a card before the next graph read.
    fs::write(root.join("characters/alice.md"), "Alice profile, revised.").expect("edit card");
    // External unchanged rename uses the unique source digest.
    fs::rename(root.join("chapters/1.md"), root.join("chapters/2.md")).expect("external rename");
    let moved = service.sync(false).expect("sync moves");
    assert_eq!(node_id(&moved, "alice"), node_id(&initial, "a"));
    assert_eq!(moved["relations"][0]["subject"], "alice");
    assert_eq!(moved["relations"][0]["reviewStatus"], "confirmed");
    assert_eq!(
        moved["relations"][0]["sourceRefs"][0]["path"],
        "chapters/2.md"
    );
    assert_eq!(
        moved["relations"][0]["fingerprint"],
        initial["relations"][0]["fingerprint"]
    );

    // Reusing the old path must not rebind the original evidence or entity.
    fs::write(
        root.join("chapters/1.md"),
        "Alice trusts Bob. A different scene.",
    )
    .expect("new source");
    fs::write(root.join("characters/a.md"), "Another character").expect("new character");
    let reused = service.sync(false).expect("sync reused paths");
    assert_ne!(node_id(&reused, "a"), node_id(&initial, "a"));
    assert_eq!(
        reused["relations"][0]["sourceRefs"][0]["path"],
        "chapters/2.md"
    );

    // Generated files can be discarded. Identity and source bindings are canonical.
    fs::remove_dir_all(root.join(WIKI_ROOT)).expect("discard generated projection");
    let rebuilt = service.sync(true).expect("rebuild");
    assert_eq!(node_id(&rebuilt, "alice"), node_id(&initial, "a"));
    assert_eq!(
        rebuilt["relations"][0]["sourceRefs"][0]["path"],
        "chapters/2.md"
    );
    assert_eq!(rebuilt["relations"][0]["reviewStatus"], "confirmed");
    assert_eq!(service.sync(false).expect("no-op")["noChanges"], true);

    fs::write(
        root.join("chapters/2.md"),
        "They no longer trust each other.",
    )
    .expect("change evidence");
    let changed = service.sync(false).expect("invalidate quote");
    assert_eq!(changed["relations"][0]["evidenceValid"], false);
    assert_eq!(changed["relations"][0]["reviewStatus"], "review_required");
}

#[test]
fn knowledge_reviews_ignore_unrelated_prose_but_track_candidate_edits() {
    let (dir, service, initial) = fixture();
    fs::write(
        dir.path().join("chapters/1.md"),
        "A rainy morning.\nAlice trusts Bob.\nThey leave together.",
    )
    .expect("expand prose");
    let expanded = service.sync(false).expect("sync");
    assert_eq!(expanded["relations"][0]["reviewStatus"], "confirmed");
    assert_eq!(
        expanded["relations"][0]["fingerprint"],
        initial["relations"][0]["fingerprint"]
    );
    assert_ne!(
        expanded["relations"][0]["sourceRefs"][0]["sha256"],
        initial["relations"][0]["sourceRefs"][0]["sha256"]
    );
    let mut candidate = initial["relations"][0]["candidate"].clone();
    candidate["predicate"] = json!("distrusts");
    write_json(
        dir.path(),
        ".storydex/knowledge/relations.json",
        &json!({"relations": [candidate]}),
    );
    assert_eq!(
        service.sync(false).expect("candidate changed")["relations"][0]["reviewStatus"],
        "review_required"
    );
}

#[test]
fn knowledge_explicit_directory_move_handles_identical_files_without_merging() {
    let dir = tempfile::tempdir().expect("directory");
    fs::create_dir_all(dir.path().join("characters")).expect("characters");
    for name in ["a", "b"] {
        fs::write(
            dir.path().join(format!("characters/{name}.md")),
            "same profile",
        )
        .expect("profile");
    }
    let service = StorydexKnowledge::new(dir.path()).expect("service");
    let initial = service.sync(false).expect("initial");
    service
        .move_source("characters", "cast")
        .expect("directory rename");
    let moved = service.sync(false).expect("moved");
    for name in ["a", "b"] {
        assert_eq!(node_id(&moved, name), node_id(&initial, name));
    }
    fs::rename(dir.path().join("cast/a.md"), dir.path().join("cast/c.md")).expect("rename a");
    fs::rename(dir.path().join("cast/b.md"), dir.path().join("cast/d.md")).expect("rename b");
    let ambiguous = service.sync(false).expect("ambiguous moves");
    for name in ["c", "d"] {
        assert_ne!(node_id(&ambiguous, name), node_id(&initial, "a"));
        assert_ne!(node_id(&ambiguous, name), node_id(&initial, "b"));
    }
}

#[test]
fn knowledge_v2_upgrade_preserves_only_the_matching_review() {
    let (dir, service, mut initial) = fixture();
    let relation = &mut initial["relations"][0];
    for reference in relation["sourceRefs"].as_array_mut().expect("refs") {
        reference.as_object_mut().expect("ref").remove("sourceId");
    }
    let legacy_base = hex_digest(
        &canonical_json_bytes(&json!({"candidate": relation["candidate"],
        "evidence": relation["sourceRefs"], "removed": false}))
        .expect("legacy base"),
    );
    relation["baseFingerprint"] = json!(legacy_base);
    relation
        .as_object_mut()
        .expect("relation")
        .remove("reviewBaseFingerprint");
    initial["categorySchemaVersion"] = json!("story-wiki-rust-v2");
    write_json(dir.path(), ".storydex/wiki/knowledge_graph.json", &initial);
    write_json(
        dir.path(),
        ".storydex/knowledge/relation-reviews.json",
        &json!({"trust": {
        "baseFingerprint": legacy_base, "decision": "confirmed", "changes": {"predicate": "信任"}}}),
    );
    fs::remove_file(dir.path().join(SOURCE_IDENTITIES_PATH)).expect("legacy fixture");
    let upgraded = service.sync(false).expect("upgrade");
    assert_eq!(upgraded["relations"][0]["reviewStatus"], "confirmed");
    assert_eq!(
        service.sync(false).expect("no-op after migration")["noChanges"],
        true
    );
    assert_eq!(
        service.sync(true).expect("repeat rebuild")["relations"][0]["reviewStatus"],
        "confirmed"
    );
    fs::remove_dir_all(dir.path().join(WIKI_ROOT)).expect("discard projection after migration");
    assert_eq!(
        service.sync(true).expect("rebuild migrated review")["relations"][0]["reviewStatus"],
        "confirmed"
    );
    fs::write(dir.path().join("chapters/1.md"), "Different evidence.").expect("edit");
    assert_eq!(
        service.sync(false).expect("changed")["relations"][0]["reviewStatus"],
        "review_required"
    );
}
