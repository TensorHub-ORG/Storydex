import json

import pytest

from services.entity_registry import EntityRegistry
from services.content_catalog_service import get_content_catalog_service
from services.story_project_service import StoryProjectService
from services.story_wiki_service import StoryWikiService


def _write(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, dict) else value,
        encoding="utf-8",
    )
    return path


def _card(root, filename, entity_id, name, **fields):
    return _write(root, f".storydex/characters/{filename}.json", {
        "id": entity_id, "name": name, **fields,
    })


def test_same_name_characters_keep_ids_cards_and_unique_alias_mentions(tmp_path):
    _card(tmp_path, "north", "char:north", "林澈", aliases=["北境医师"], summary="北境医师的独立档案")
    _card(tmp_path, "south", "char:south", "林澈", aliases=["南境船长"], summary="南境船长的独立档案")
    _write(tmp_path, "chapters/001.md", "北境医师登上高塔。")
    _write(tmp_path, "chapters/002.md", "南境船长返回港口。")
    _write(tmp_path, "chapters/003.md", "林澈站在门外。")
    service = StoryWikiService()

    payload = service.rebuild(tmp_path)
    entries = {entry["id"]: entry for entry in payload["entries"] if entry["category"] == "characters"}
    assert set(entries) == {"char:north", "char:south"}
    assert "北境医师的独立档案" in entries["char:north"]["summary"]
    assert "南境船长的独立档案" in entries["char:south"]["summary"]
    assert entries["char:north"]["primarySourcePath"] == ".storydex/characters/north.json"
    assert entries["char:south"]["primarySourcePath"] == ".storydex/characters/south.json"
    appearances = {(edge["source"], edge["target"]) for edge in payload["graph"]["edges"] if edge["type"] == "appearance"}
    assert appearances == {
        ("char:north", service._chapter_entry_id("chapters/001.md")),
        ("char:south", service._chapter_entry_id("chapters/002.md")),
    }
    assert all(entry["needsReview"] for entry in entries.values())
    assert not any(item["code"] == "graph.character.canonical_count" for item in payload["diagnostics"])
    assert service.sync_local_incremental(tmp_path)["graph"] == payload["graph"]


def test_character_card_does_not_claim_same_name_location(tmp_path):
    _write(tmp_path, ".storydex/memory/current/entities.json", {
        "version": 2, "schemaVersion": 2,
        "entities": [{"entityId": "location:dawn", "canonical_name": "黎明", "kind": "location", "status": "active"}],
    })
    _card(tmp_path, "dawn", "char:dawn", "黎明", summary="这是人物档案")

    payload = StoryWikiService().rebuild(tmp_path)

    assert {(n["id"], n["type"]) for n in payload["graph"]["nodes"]} >= {
        ("location:dawn", "location"), ("char:dawn", "character"),
    }
    records = EntityRegistry(tmp_path).load_records()
    assert {(r.entity_id, r.kind) for r in records} == {
        ("location:dawn", "location"), ("char:dawn", "character"),
    }


@pytest.mark.parametrize("reverse_order", [False, True])
def test_alias_collision_keeps_each_card_owned_by_its_id(tmp_path, reverse_order):
    alice_file, bob_file = ("a", "b") if reverse_order else ("b", "a")
    _card(tmp_path, alice_file, "char:alice", "Alice", aliases=["Bob", "Chief"], summary="Alice is a captain.")
    _card(tmp_path, bob_file, "char:bob", "Bob", aliases=["Chief"], summary="Bob is a doctor.")
    _write(tmp_path, "chapters/001.md", "Chief came home.")
    _write(tmp_path, "chapters/002.md", "Bob arrived alone.")
    service = StoryWikiService()

    payload = service.rebuild(tmp_path)

    registry = EntityRegistry(tmp_path)
    assert registry.canonicalize_many(["Bob"]) == ("Bob",)
    assert registry.canonicalize_many(["Chief"]) == ()
    assert registry.resolve_mentions("Chief came home.", fallback_names=["Chief"]) == ()
    assert registry.resolve_mentions("Bob arrived alone.") == ("Bob",)
    entries = {entry["id"]: entry for entry in payload["entries"] if entry["category"] == "characters"}
    for entity_id, filename, summary in (
        ("char:alice", alice_file, "Alice is a captain."),
        ("char:bob", bob_file, "Bob is a doctor."),
    ):
        assert summary in entries[entity_id]["summary"]
        assert entries[entity_id]["primarySourcePath"] == f".storydex/characters/{filename}.json"
        assert entries[entity_id]["needsReview"] is True
    assert {(e["source"], e["target"]) for e in payload["graph"]["edges"] if e["type"] == "appearance"} == {
        ("char:bob", service._chapter_entry_id("chapters/002.md")),
    }


def test_unrelated_character_update_preserves_same_name_registered_entities(tmp_path):
    records = [
        {"entityId": "char:one", "canonical_name": "林澈", "kind": "character"},
        {"entityId": "char:two", "canonical_name": "林澈", "kind": "character"},
        {"entityId": "location:lin", "canonical_name": "林澈", "kind": "location"},
    ]
    path = _write(tmp_path, ".storydex/memory/current/entities.json", {"version": 2, "entities": records})
    service = StoryProjectService()
    service._upsert_entities_from_character_updates(tmp_path, [{"character": "苏晚", "aliases": ["小苏"]}], updated_at="2026-09-26")
    service._upsert_entities_from_character_updates(tmp_path, [{"character": "林澈", "aliases": ["未经消歧的别名"]}], updated_at="2026-09-26")

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == 2
    by_id = {item.get("entityId"): item for item in saved["entities"] if item.get("entityId")}
    assert set(by_id) == {"char:one", "char:two", "location:lin"}
    assert all("未经消歧的别名" not in item.get("aliases", []) for item in by_id.values())
    assert by_id["location:lin"]["kind"] == "location"


@pytest.mark.parametrize("paths", [
    ("chapters/volume-1/001.md", "chapters/volume/1-001.md"),
    ("chapters/序章.md", "chapters/序章.txt"),
    ("chapters/01 序章.md", "chapters/01-序章.md"),
    (".storydex/scripts/大纲.md", ".storydex/scripts/大纲.txt"),
])
def test_distinct_source_paths_keep_distinct_plot_entries_after_sync(tmp_path, paths):
    for index, relative in enumerate(paths):
        _write(tmp_path, relative, f"独立剧情 {index}。")
    service = StoryWikiService()
    service.rebuild(tmp_path)
    _write(tmp_path, paths[1], "第二份剧情已更新。")

    payload = service.sync_local_incremental(tmp_path)

    entries = [e for e in payload["entries"] if e["sourcePaths"] in ([paths[0]], [paths[1]])]
    assert len(entries) == 2
    assert len({e["id"] for e in entries}) == 2
    node_ids = {node["id"] for node in payload["graph"]["nodes"]}
    assert all(entry["id"] in node_ids for entry in entries)
    assert any("已更新" in entry["summary"] for entry in entries)
    assert not any(item["code"] == "graph.quarantine" for item in payload["diagnostics"])


@pytest.mark.parametrize("detail", ["不信任", "不再信任", "不愿合作", "曾经信任，但现在不信任", "不忠诚"])
def test_negative_card_relationship_is_never_published_as_positive(tmp_path, detail):
    _write(tmp_path, ".storydex/characters/Alice.md", f"# Alice\n\n## 关系网络\n- Bob：{detail}\n")
    _write(tmp_path, ".storydex/characters/Bob.md", "# Bob\n")
    service = StoryWikiService()
    service.rebuild(tmp_path)

    assert service.query_graph(tmp_path, category="characters")["graph"]["edges"] == []


def test_old_catalog_projection_is_rebuilt_even_without_source_changes(tmp_path):
    _card(tmp_path, "one", "char:one", "林澈")
    _card(tmp_path, "two", "char:two", "林澈")
    service = StoryWikiService()
    service.prepare_catalog_sources(tmp_path, reconcile_entities=True)
    snapshot = get_content_catalog_service(tmp_path).snapshot()
    published = service.refresh_from_catalog(tmp_path, snapshot)
    published["categorySchemaVersion"] = "story-wiki-v7-auditable-relations"
    published["graph"]["nodes"] = [n for n in published["graph"]["nodes"] if n["id"] != "char:two"]
    _write(tmp_path, ".storydex/wiki/knowledge_graph.json", published)

    assert service.read_or_build(tmp_path)["status"] == "stale"
    rebuilt = service.refresh_from_catalog(tmp_path, snapshot, changed_paths=[])

    assert rebuilt["status"] == "ready"
    assert {n["id"] for n in rebuilt["graph"]["nodes"]} >= {"char:one", "char:two"}


def test_conflicting_card_ids_keep_stable_ownership_after_repeated_rebuilds(tmp_path):
    _write(tmp_path, ".storydex/characters/01_Alice.md", "# Alice\n\n> entityId: char:shared\n")
    _write(tmp_path, ".storydex/characters/02_Bob.md", "# Bob\n\n> entityId: char:shared\n\n## 关系网络\n- Carol：一直信任\n")
    _write(tmp_path, ".storydex/characters/03_Carol.md", "# Carol\n\n> entityId: char:carol\n")
    service = StoryWikiService()
    first = service.rebuild(tmp_path)
    ids = {node["label"]: node["id"] for node in first["graph"]["nodes"] if node["type"] == "character"}

    for _ in range(2):
        rebuilt = service.rebuild(tmp_path)
        assert {node["label"]: node["id"] for node in rebuilt["graph"]["nodes"] if node["type"] == "character"} == ids
        edges = service.query_graph(tmp_path, category="characters")["graph"]["edges"]
        assert {(edge["source"], edge["target"]) for edge in edges} == {(ids["Bob"], ids["Carol"])}


def test_negative_generation_evidence_is_not_a_confirmed_trust_edge(tmp_path):
    project = StoryProjectService()
    project.ensure_project_structure(tmp_path)
    _card(tmp_path, "alice", "char:alice", "Alice")
    _card(tmp_path, "bob", "char:bob", "Bob")
    service = StoryWikiService()
    service.rebuild(tmp_path)
    quote = "Alice does not trust Bob."
    project.apply_story_generation_increment(tmp_path, {
        "segmentPath": "chapters/001.md", "segmentText": quote, "applyVariables": True,
        "relationshipUpdates": [{
            "source": "Alice", "target": "Bob", "dimension": "trust", "currentLevel": -3,
            "evidence": quote, "lastUpdatedIn": "chapters/001.md",
        }],
    }, generation_contract={
        "traceId": "negative-evidence", "sessionId": "test", "providerId": "test", "model": "test",
        "knowledgeWritePolicy": {"mode": "standard"},
    })
    service.rebuild(tmp_path)

    assert service.query_graph(tmp_path, category="characters")["graph"]["edges"] == []


@pytest.mark.parametrize("same_name", [False, True])
def test_relationship_evidence_cannot_borrow_another_characters_alias(tmp_path, same_name):
    _card(tmp_path, "one", "char:one", "林澈" if same_name else "Alice", aliases=["Chief"])
    _card(tmp_path, "two", "char:two", "林澈" if same_name else "Bob", aliases=["Chief", "南境船长"])
    _card(tmp_path, "carol", "char:carol", "Carol")
    quote = "南境船长信任Carol。" if same_name else "Chief trusts Carol."
    _write(tmp_path, "chapters/001.md", quote)
    _write(tmp_path, ".storydex/memory/current/relationship_graph.json", {"edges": [{
        "sourceId": "char:one", "targetId": "char:carol", "dimension": "trust",
        "evidence": quote, "last_updated_in": "chapters/001.md",
    }]})
    service = StoryWikiService()
    service.rebuild(tmp_path)

    assert service.query_graph(tmp_path, category="characters")["graph"]["edges"] == []


def test_explicit_relationship_ids_disambiguate_two_same_name_characters(tmp_path):
    _write(tmp_path, ".storydex/characters/north.md", "# 林澈\n\n> entityId: char:north\n\n## 关系网络\n- 林澈（char:south）：一直信任\n")
    _write(tmp_path, ".storydex/characters/south.md", "# 林澈\n\n> entityId: char:south\n")
    service = StoryWikiService()
    service.rebuild(tmp_path)

    edges = service.query_graph(tmp_path, category="characters")["graph"]["edges"]
    assert {(edge["source"], edge["target"]) for edge in edges} == {("char:north", "char:south")}


def test_legacy_sanitized_id_keeps_character_card_relationships(tmp_path):
    _write(tmp_path, ".storydex/characters/Alice.md", "# Alice\n\n## 关系网络\n- Bob：一直信任\n")
    _write(tmp_path, ".storydex/characters/Bob.md", "# Bob\n")
    _write(tmp_path, ".storydex/memory/current/entities.json", {"version": 2, "entities": [{
        "entityId": "legacy/人物一", "canonical_name": "Alice", "kind": "character",
        "sourcePaths": [".storydex/characters/Alice.md"],
    }]})
    service = StoryWikiService()
    payload = service.rebuild(tmp_path)
    ids = {node["label"]: node["id"] for node in payload["graph"]["nodes"] if node["type"] == "character"}

    edges = service.query_graph(tmp_path, category="characters")["graph"]["edges"]
    assert {(edge["source"], edge["target"]) for edge in edges} == {(ids["Alice"], ids["Bob"])}


def test_unique_relationship_endpoints_keep_case_insensitive_evidence_matching(tmp_path):
    _card(tmp_path, "alice", "char:alice", "Alice")
    _card(tmp_path, "bob", "char:bob", "Bob")
    quote = "alice trusts bob."
    _write(tmp_path, "chapters/001.md", quote)
    _write(tmp_path, ".storydex/memory/current/relationship_graph.json", {"edges": [{
        "source": "Alice", "target": "Bob", "dimension": "trust",
        "evidence": quote, "last_updated_in": "chapters/001.md",
    }]})
    service = StoryWikiService()
    service.rebuild(tmp_path)

    edges = service.query_graph(tmp_path, category="characters")["graph"]["edges"]
    assert {(edge["source"], edge["target"]) for edge in edges} == {("char:alice", "char:bob")}


def test_bom_registry_preserves_existing_entities_when_character_cards_are_reconciled(tmp_path):
    path = _write(tmp_path, ".storydex/memory/current/entities.json", {"version": 2, "entities": [{
        "entityId": "location:harbor", "canonical_name": "旧港", "kind": "location",
    }]})
    path.write_text("\ufeff" + path.read_text(encoding="utf-8"), encoding="utf-8")
    _card(tmp_path, "alice", "char:alice", "Alice")

    payload = StoryWikiService().rebuild(tmp_path)

    assert {node["id"] for node in payload["graph"]["nodes"]} >= {"location:harbor", "char:alice"}
    assert {record.entity_id for record in EntityRegistry(tmp_path).load_records()} == {"location:harbor", "char:alice"}
