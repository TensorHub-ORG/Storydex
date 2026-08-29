import json
from pathlib import Path

import pytest

from services.story_wiki_service import (
    CATEGORY_LABELS,
    PROJECTION_SCHEMA_VERSION,
    WIKI_CATEGORY_SCHEMA_VERSION,
    StoryWikiService,
)


@pytest.fixture()
def service():
    return StoryWikiService()


def _source(path, text, kind="chapter"):
    return {"relativePath": path, "title": Path(path).stem, "kind": kind, "text": text}


def test_schema_normalization_covers_malformed_payloads(service):
    assert not service._has_current_category_schema({})
    current_schema = {
        "schemaVersion": PROJECTION_SCHEMA_VERSION,
        "categorySchemaVersion": WIKI_CATEGORY_SCHEMA_VERSION,
    }
    assert not service._has_current_category_schema({**current_schema, "entries": [{"category": ""}]})
    assert not service._has_current_category_schema({**current_schema, "entries": [{"category": "character"}]})
    assert not service._has_current_category_schema({**current_schema, "graph": {"nodes": [{"category": "character"}]}})
    assert service._has_current_category_schema({**current_schema, "entries": [None, {"category": "characters"}], "graph": {"nodes": [None, {"category": ""}]}})

    payload = service._normalize_wiki_payload({
        "entries": [None, {"title": "Hero", "category": "characters", "details": ["", 2], "sourcePaths": "bad", "confidence": 8}],
        "graph": {
            "nodes": [None, {"label": "Hero", "type": "motivation", "category": "characters"}, {"id": "n2", "entryId": "characters:Hero"}],
            "edges": [None, {"source": "a", "target": "b", "type": "knows", "coOccurrence": 1, "confidence": -2}],
        },
    })
    entry = payload["entries"][0]
    assert entry["category"] == "characters" and entry["confidence"] == 1.0
    assert payload["graph"]["nodes"][0]["type"] == "character"
    assert payload["graph"]["edges"][0]["coOccurrence"] is True
    assert service._normalize_wiki_category(None) == "overview"
    assert service._normalize_wiki_category("unknown") == "overview"

    assert service._summary_from_entries([{}, {"summary": " summary "}]) == "summary"
    assert service._summary_from_entries([])
    assert service._confidence("bad") == 0.68
    assert service._confidence(-1) == 0 and service._confidence(2) == 1


def test_query_matching_neighborhood_and_hub_helpers(service):
    entry = {"id": "hero", "title": "Hero One", "category": "characters", "categoryLabel": "People", "summary": "brave", "details": ["blue eyes"], "sourcePaths": ["characters/hero.md"]}
    node = {"id": "n1", "label": "Hero", "type": "character", "category": "characters", "entryId": "hero", "summary": "brave"}
    edge = {"source": "n1", "target": "n2", "label": "knows", "type": "relation", "evidence": "school"}
    assert service._query_tokens(" Hero   blue ") == ["hero", "blue"]
    assert service._query_tokens("") == []
    assert service._wiki_entry_matches(entry, ["hero", "blue"])
    assert not service._wiki_entry_matches({"details": "bad", "sourcePaths": "bad"}, ["hero"])
    assert service._wiki_node_matches(node, ["brave"])
    assert service._wiki_edge_matches(edge, ["knows", "school"])
    assert not service._wiki_text_matches(["anything"], [])
    assert service._safe_int("4", fallback=1) == 4
    assert service._safe_int(None, fallback=3) == 3

    nodes = {
        "hub": {"id": "project:root", "type": "project"},
        "n1": node,
        "n2": {"id": "n2", "label": "Friend"},
        "n3": {"id": "n3", "label": "Third"},
    }
    edges = [edge, {"source": "n2", "target": "n3"}, {"source": "missing", "target": "n1"}]
    assert service._expand_wiki_node_neighborhood(["missing"], node_by_id=nodes, edges=edges, depth=1) == set()
    assert service._expand_wiki_node_neighborhood(["hub"], node_by_id=nodes, edges=edges, depth=2) == {"hub"}
    assert service._expand_wiki_node_neighborhood(["n1"], node_by_id=nodes, edges=edges, depth=2) == {"n1", "n2", "n3"}
    assert service._is_wiki_hub_node({"id": "project:root"})
    assert service._is_wiki_hub_node({"category": "overview"})
    assert service._is_wiki_hub_node({"category": "index"})
    assert service._is_wiki_hub_node({"type": "project"})
    assert service._is_wiki_hub_node({"role": "categoryHub"})
    assert not service._is_wiki_hub_node(node)
    copied = service._wiki_content_node({"id": "x"})
    assert copied["entryId"] == "" and copied["selectable"] is True and copied["synthetic"] is False
    assert service._wiki_category_label("characters", {}) == CATEGORY_LABELS["characters"]
    assert service._wiki_category_label("custom", {"custom": "Custom"}) == "Custom"
    assert service._wiki_edge_touches_hub({"source": "hub", "target": "n1"}, nodes)
    assert not service._wiki_edge_touches_hub(edge, nodes)


def test_source_collection_kind_sort_and_read_failures(service, tmp_path, monkeypatch):
    assert service._collect_sources(tmp_path / "missing") == []
    (tmp_path / "chapters").mkdir()
    (tmp_path / "chapters" / "10.md").write_text("ten", encoding="utf-8")
    (tmp_path / "chapters" / "2.txt").write_text("two", encoding="utf-8")
    (tmp_path / "README.md").write_text("skip", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "hidden.md").write_text("skip", encoding="utf-8")
    (tmp_path / ".storydex" / "wiki").mkdir(parents=True)
    (tmp_path / ".storydex" / "wiki" / "generated.json").write_text("{}", encoding="utf-8")
    (tmp_path / "characters").mkdir()
    (tmp_path / "characters" / "hero.json").write_text(json.dumps({"name": "Hero"}), encoding="utf-8")
    sources = service._collect_sources(tmp_path)
    assert [s["relativePath"] for s in sources][:2] == ["chapters/2.txt", "chapters/10.md"]
    assert [s["relativePath"] for s in service._collect_character_sources(tmp_path)] == ["characters/hero.json"]
    assert all(s["relativePath"] != "README.md" for s in sources)
    assert service._should_skip_source_path("folder/README.md")
    assert service._should_skip_source_path(".storydex/wiki/a.json")
    assert not service._should_skip_source_path("chapters/a.md")
    assert service._source_kind("chapters/a.md") == "chapter"
    assert service._source_kind("x/templates/a.md") == "project"
    assert service._source_kind("x/characters/a.md") == "character"
    assert service._source_kind("x/worldbook/a.json") == "world"
    assert service._source_kind("x/presets/a.json") == "preset"
    assert service._source_kind("x/memory/a.json") == "memory"
    assert service._source_kind("misc/a.md") == "project"
    assert service._source_sort_key("a2") < service._source_sort_key("a10")

    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert service._read_source_text(bad) == "not json"
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("fail")))
    assert service._read_source_text(bad) == ""


def test_entity_reconciliation_does_not_call_full_source_scan(service, tmp_path, monkeypatch):
    cards = tmp_path / ".storydex" / "characters"
    cards.mkdir(parents=True)
    cards.joinpath("沈青.md").write_text(
        "# 沈青\n\n> 稳定实体ID: `char:shenqing`\n",
        encoding="utf-8",
    )
    states = cards / "states"
    states.mkdir()
    states.joinpath("derived.json").write_text(
        json.dumps({"name": "不应成为角色", "entityId": "char:derived"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        service,
        "_collect_sources",
        lambda _root: (_ for _ in ()).throw(AssertionError("full source scan should not run")),
    )

    service._reconcile_entity_registry(tmp_path)

    registry = json.loads(
        (tmp_path / ".storydex" / "memory" / "current" / "entities.json").read_text(encoding="utf-8")
    )
    assert [item["entityId"] for item in registry["entities"]] == ["char:shenqing"]
    assert registry["entities"][0]["sourcePaths"] == [".storydex/characters/沈青.md"]


def test_build_index_preserves_entry_and_node_order_with_reverse_maps(service, tmp_path):
    source_path = "chapters/001.md"
    payload = {
        "schemaVersion": 2,
        "entries": [
            {"id": "entry-a", "sourcePaths": [source_path, source_path]},
            {"id": "entry-b", "sourcePaths": [source_path]},
            {"id": "entry-c", "sourcePaths": ["chapters/002.md"]},
        ],
        "graph": {
            "nodes": [
                {"id": "node-b", "entryId": "entry-b"},
                {"id": "node-a", "entryId": "entry-a"},
                {"id": "node-c", "entryId": "entry-c"},
            ],
            "edges": [],
        },
    }
    sources = [
        {
            "relativePath": source_path,
            "sha256": "hash",
            "kind": "chapter",
            "size": 10,
            "mtime": "2026-01-01T00:00:00+00:00",
        }
    ]

    index = service._build_index(
        tmp_path,
        payload,
        sources=sources,
        workflow="test",
        status="completed",
        changed_paths=[],
    )

    source_index = index["sources"][source_path]
    assert source_index["relatedEntryIds"] == ["entry-a", "entry-b"]
    assert source_index["relatedNodeIds"] == ["node-b", "node-a"]


def test_entity_and_text_helpers_cover_optional_paths(service, tmp_path):
    entities = {}
    service._add_entity(entities, {})
    service._add_entity(entities, {"name": " Hero ", "aliases": ["Hero", "H", ""], "sourcePaths": ["a", ""], "needsReview": False})
    service._add_entity(entities, {"name": "Hero", "aliases": ["H", "X"], "sourcePaths": ["a", "b"], "needsReview": True})
    assert entities["Hero"]["aliases"] == ["H", "X"]
    assert entities["Hero"]["sourcePaths"] == ["a", "b"] and entities["Hero"]["needsReview"]

    assert service._character_names_from_source(_source("characters/001_hero.json", '{"name":"Alice","displayName":"Al"}', "character")) == ["Alice", "Al"]
    assert service._character_names_from_source(_source("characters/001_Alice.md", "# 角色档案\n", "character")) == ["Alice"]
    assert service._character_names_from_source(_source("characters/README.json", "bad", "character")) == []
    assert service._name_score("Alice", [_source("a", "Alice Alice")]) == 2
    assert service._entity_score({"name": "Alice", "aliases": ["Al"]}, [_source("a", "Alice Al")]) == 3
    assert service._entity_type_for_kind("character") == "character"
    assert service._entity_type_for_kind("unknown") == "setting"
    assert service._entity_category_for_type("character") == "characters"
    assert service._entity_category_for_type("event") == "plot"
    assert service._entity_category_for_type("timeline") == "plot"
    assert service._entity_category_for_type("item") == "setting"

    entity = {"name": "Alice", "type": "character", "aliases": ["Al", ""], "sourcePaths": ["chars/a.md", ""]}
    assert service._entity_node_id(entity).startswith("character:")
    assert "Alice" in service._entity_summary(entity)
    details = service._entity_details(entity)
    assert any("Al" in value for value in details) and any("chars/a.md" in value for value in details)
    assert service._display_title("notes/a.md", "fallback") == "notes/a"
    assert service._display_title("chapters/a.md", "fallback") == "a"
    assert service._compress_text(" a   b ", 20) == "a b"
    assert service._compress_text("abcdefgh", 4) == "abcd..."
    assert service._details_from_sources([_source("a.md", ""), _source("b.md", "body")]) == ["b.md: body"]
    assert service._build_plot_summary([])
    assert "first" in service._build_plot_summary([_source("1.md", "first"), _source("2.md", "latest")])
    assert len(service._chapter_plot_details([_source("chapters/1.md", "line")])) == 1
    assert len(service._chapter_details(_source("chapters/1.md", "line1\n\nline2"), ["Alice", ""])) == 5


def test_character_mapping_entry_edges_dedupe_and_render(service):
    sources = [_source("characters/alice.md", "# Alice\n\nAl", "character"), _source("chapters/1.md", "Alice appears")]
    entities = [{"name": "Alice", "aliases": ["Al"]}, {"name": "Bob", "aliases": []}]
    mapping = service._character_sources(Path("."), sources, entities)
    assert mapping["Alice"] and mapping["Bob"] == []
    assert service._mentioning_sources(sources, "Alice") == sources
    assert service._mentioning_sources(sources, "") == []
    assert service._character_summary("Alice", [sources[0]], sources)
    assert "2" in service._character_summary("Alice", [], sources)
    assert len(service._character_details("Alice", [sources[0]], sources)) >= 3

    entry = service._entry("e", "Title", "custom", "summary", ["", "d"], ["a", "a"], confidence=3, needs_review=1)
    assert entry["categoryLabel"] == "custom" and entry["details"] == ["d"] and entry["sourcePaths"] == ["a"]
    assert "coOccurrence" not in service._edge("a", "b", "label", "type")
    assert service._dedupe_nodes([{}, {"id": "a"}, {"id": "a", "label": "new"}]) == [{"id": "a"}]
    edges = service._dedupe_edges([{"source": "", "target": "b", "label": "x"}, {"source": "a", "target": "b", "label": "x", "weight": 1}, {"source": "a", "target": "b", "label": "x", "weight": 2}])
    assert edges == [{"source": "a", "target": "b", "label": "x", "weight": 2}]
    assert service._slug("  !!! ") == "item"
    assert service._slug("Hello world!") == "Hello-world"
    assert service._chapter_entry_id(r"chapters\001.MD") == "chapter:chapters-001"
    markdown = service._render_markdown({"projectName": "Demo", "summary": "Summary", "entries": [{"title": "One", "summary": "Body", "details": list(map(str, range(30)))}]})
    assert markdown.startswith("# Demo WIKI") and "- 19" in markdown and "- 20" not in markdown


def test_query_graph_all_modes_and_merge_branches(service, tmp_path, monkeypatch):
    payload = {
        "projectName": "Demo",
        "summary": "demo summary",
        "categoryLabels": CATEGORY_LABELS,
        "entries": [
            {"id": "overview", "title": "Overview", "category": "overview", "summary": "demo"},
            {"id": "hero", "title": "Hero", "category": "characters", "summary": "brave hero", "details": ["blue"]},
            {"id": "place", "title": "Castle", "category": "setting", "summary": "old castle"},
        ],
        "graph": {
            "nodes": [
                {"id": "project:root", "label": "Demo", "type": "project", "category": "overview"},
                {"id": "hero-node", "label": "Hero", "type": "person", "category": "characters", "entryId": "hero"},
                {"id": "place-node", "label": "Castle", "type": "location", "category": "setting", "entryId": "place"},
                {"id": "orphan", "label": "Orphan"},
            ],
            "edges": [
                {"source": "project:root", "target": "hero-node", "label": "group"},
                {"source": "hero-node", "target": "place-node", "label": "visits", "evidence": "hero castle"},
                {"source": "hero-node", "target": "hero-node", "label": "self"},
                {"source": "missing", "target": "hero-node", "label": "bad"},
            ],
        },
    }
    monkeypatch.setattr(service, "read_or_build", lambda root: payload)
    # 分类为空时不再返回人造 hub 总览图，而是默认落到角色视图。
    default_lens = service.query_graph(tmp_path, depth="bad", limit="bad")
    assert default_lens["mode"] == "category" and default_lens["category"] == "characters"
    assert [node["id"] for node in default_lens["graph"]["nodes"]] == ["hero-node"]
    # relationships 是旧别名，结果必须与直接查 characters 一致。
    aliased = service.query_graph(tmp_path, category="relationships", depth="bad", limit="bad")
    assert aliased["category"] == "characters"
    assert aliased["graph"] == default_lens["graph"]
    category = service.query_graph(tmp_path, category="setting", limit=2)
    assert category["mode"] == "category" and category["graph"]["nodes"][0]["id"] == "place-node"
    by_node = service.query_graph(tmp_path, node_id="hero-node", depth=9, limit=999)
    assert by_node["mode"] == "node" and len(by_node["graph"]["nodes"]) == 2
    assert service.query_graph(tmp_path, node_id="project:root")["graph"]["nodes"] == []
    by_entry = service.query_graph(tmp_path, entry_id="hero")
    assert by_entry["mode"] == "entry" and by_entry["matchedEntryIds"] == ["hero"]
    assert service.query_graph(tmp_path, entry_id="missing")["entries"] == []
    search = service.query_graph(tmp_path, q="hero castle")
    assert search["mode"] == "search" and {n["id"] for n in search["graph"]["nodes"]} == {"hero-node", "place-node"}

    current_sources = [{"relativePath": "chapters/2.md", "sha256": "new"}, {"relativePath": "chapters/10.md", "sha256": "same"}]
    previous = {"sources": {"chapters/1.md": {"sha256": "old"}, "chapters/2.md": {"sha256": "old"}, "chapters/10.md": {"sha256": "same"}}}
    assert service.changed_source_paths(tmp_path, sources=current_sources, previous_index=previous) == ["chapters/1.md", "chapters/2.md"]
    assert service.changed_source_paths(tmp_path, sources=[], previous_index={"sources": []}) == []

    assert service._node_orphaned_by_removal({"entryId": ""}, {"x"}) is False
    assert service._node_orphaned_by_removal({"entryId": "x"}, {"x"}) is False
    assert service._node_orphaned_by_removal({"entryId": "gone"}, {"x"}) is True


def test_relationship_snapshot_and_category_edge_cases(service, tmp_path):
    nodes = [{"id": "character:alice", "label": "Alice", "type": "character", "category": "characters", "entryId": "alice"}]
    existing = [{"source": "character:alice", "target": "character:bob", "label": "trust"}]
    assert service._merge_relationship_snapshot_edges(tmp_path, nodes=nodes, existing_edges=existing, allow_new_nodes=True) is existing

    path = tmp_path / ".storydex" / "memory" / "current" / "relationship_graph.json"
    path.parent.mkdir(parents=True)
    path.write_text("bad", encoding="utf-8")
    assert service._merge_relationship_snapshot_edges(tmp_path, nodes=nodes, existing_edges=existing, allow_new_nodes=True) is existing
    path.write_text("[]", encoding="utf-8")
    assert service._merge_relationship_snapshot_edges(tmp_path, nodes=nodes, existing_edges=existing, allow_new_nodes=True) is existing
    path.write_text(json.dumps({"edges": "bad"}), encoding="utf-8")
    assert service._merge_relationship_snapshot_edges(tmp_path, nodes=nodes, existing_edges=existing, allow_new_nodes=True) is existing

    chapters = tmp_path / "chapters"
    chapters.mkdir()
    chapters.joinpath("001.md").write_text(
        "Alice和Bob约定互相信任。\n",
        encoding="utf-8",
    )
    known_nodes = [
        *nodes,
        {"id": "character:bob", "label": "Bob", "type": "character", "category": "characters", "entryId": "bob"},
        {"id": "character:carol", "label": "Carol", "type": "character", "category": "characters", "entryId": "carol"},
    ]
    path.with_name("entities.json").write_text(json.dumps({
        "entities": [
            {"entityId": "character:alice", "canonical_name": "Alice", "kind": "character", "aliases": ["Shared"]},
            {"entityId": "character:bob", "canonical_name": "Bob", "kind": "character", "aliases": ["Shared"]},
            {"entityId": "character:carol", "canonical_name": "Carol", "kind": "character"},
        ],
    }), encoding="utf-8")
    snapshot = {
        "edges": [
            None,
            {"source": "", "target": "Bob"},
            {"source": "Alice", "target": "Alice"},
            {
                "source": "Alice",
                "target": "Bob",
                "dimension": "trust",
                "current_level": -3,
                "history": [{"evidence": "Alice和Bob约定互相信任", "last_updated_in": "chapters/001.md"}],
            },
            {"source": "Alice", "target": "Bob", "dimension": "trust", "current_level": 4},
            {
                "source": "character:alice",
                "target": "Carol",
                "dimension": "custom",
                "history": [{"evidence": "Alice和Bob约定互相信任", "last_updated_in": "chapters/001.md"}],
            },
            {
                "source": "Shared",
                "target": "Carol",
                "dimension": "trust",
                "history": [{"evidence": "Alice和Bob约定互相信任", "last_updated_in": "chapters/001.md"}],
            },
            {
                "source": "Alice",
                "target": "Carol",
                "dimension": "trust",
                "history": [{"evidence": "Alice和Bob约定互相信任", "last_updated_in": "chapters/001.md"}],
            },
            {"source": "Alice", "target": "Dave", "dimension": ""},
        ]
    }
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    no_new = service._merge_relationship_snapshot_edges(tmp_path, nodes=list(known_nodes), existing_edges=[], allow_new_nodes=False)
    expanded_nodes = list(known_nodes)
    merged = service._merge_relationship_snapshot_edges(tmp_path, nodes=expanded_nodes, existing_edges=[], allow_new_nodes=True)
    assert no_new == merged
    assert {node["label"] for node in expanded_nodes} == {"Alice", "Bob", "Carol"}
    assert len(merged) == 1
    trust = merged[0]
    assert trust["weight"] == 1
    assert trust["evidence"] == "Alice和Bob约定互相信任"
    assert trust["sourcePath"] == "chapters/001.md"
    assert {"level", "strength", "polarity"}.isdisjoint(trust)
    assert trust["reviewStatus"] == "confirmed"
    assert trust["knowledgeStatus"] == "observed"
    assert trust["confidence"] == "confirmed"
    assert trust["sourceRefs"] == [{
        "path": "chapters/001.md",
        "quote": "Alice和Bob约定互相信任",
        "role": "dynamic_relationship",
    }]
    assert trust["provenance"] == {
        "origin": "dynamic_relationship_graph",
        "extractorVersion": "storydex-relationship-graph-v1",
    }
    assert trust["id"].startswith("relationship:")
    assert len(trust["fingerprint"]) == 64

    entries = [
        {"id": "alice", "category": "characters"},
        {"id": "bob", "category": "characters"},
        {"id": "rel", "category": "relationships"},
    ]
    query_nodes = [
        {"id": "project:root", "type": "project"},
        {"id": "character:alice", "label": "Alice", "type": "character", "category": "characters", "entryId": "alice"},
        {"id": "character:bob", "label": "Bob", "type": "character", "category": "characters", "entryId": "bob"},
        {"id": "character:eve", "label": "Eve", "type": "character", "category": "characters"},
        {"id": "place", "label": "Place", "type": "location", "category": "setting"},
    ]
    query_edges = [
        {
            "source": "character:alice", "target": "character:bob", "type": "relationship", "label": "朋友",
            "relationType": "intimacy", "status": "asserted", "evidence": "Alice和Bob约定互相信任",
            "sourcePath": "chapters/001.md",
        },
        {"source": "character:alice", "target": "character:bob", "type": "relationship", "label": "co", "coOccurrence": True},
        {"source": "character:bob", "target": "character:eve", "type": "relationship", "label": "co", "coOccurrence": True},
        {"source": "character:alice", "target": "place", "type": "appearance"},
    ]
    result = service._query_wiki_relationship_graph(
        "characters", root=tmp_path, normalized_q="", normalized_entry_id="", normalized_node_id="",
        max_depth=1, max_items=2, entries=entries, entry_by_id={e["id"]: e for e in entries},
        nodes=query_nodes, valid_edges=query_edges, category_labels=CATEGORY_LABELS,
    )
    assert result["category"] == "characters" and len(result["graph"]["nodes"]) == 2
    assert any(edge["label"] == "朋友" for edge in result["graph"]["edges"])
    assert not any(edge.get("coOccurrence") and {edge["source"], edge["target"]} == {"character:alice", "character:bob"} for edge in result["graph"]["edges"])

    category = service._query_wiki_category_graph(
        "characters", root=tmp_path, normalized_q="", normalized_entry_id="", normalized_node_id="",
        max_depth=1, max_items=4, entries=entries, entry_by_id={e["id"]: e for e in entries},
        nodes=[*query_nodes, query_nodes[1], {"id": "", "type": "character"}], valid_edges=query_edges,
        category_labels=CATEGORY_LABELS,
    )
    assert category["mode"] == "category"
    # 角色图不带任何跨类邻居：章节/地点不该出现在这里。
    assert all(node["type"] == "character" for node in category["graph"]["nodes"])
    assert not any(node.get("neighbor") for node in category["graph"]["nodes"])
