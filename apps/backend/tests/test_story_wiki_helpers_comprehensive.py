import json
from pathlib import Path

import pytest

from services.story_wiki_service import (
    CATEGORY_LABELS,
    WIKI_CATEGORY_SCHEMA_VERSION,
    StoryWikiService,
)


@pytest.fixture()
def service():
    return StoryWikiService()


def _source(path, text, kind="chapter"):
    return {"relativePath": path, "title": Path(path).stem, "kind": kind, "text": text}


def test_schema_normalization_and_reports_cover_malformed_agent_payloads(service):
    assert not service._has_current_category_schema({})
    assert not service._has_current_category_schema({"categorySchemaVersion": WIKI_CATEGORY_SCHEMA_VERSION, "entries": [{"category": ""}]})
    assert not service._has_current_category_schema({"categorySchemaVersion": WIKI_CATEGORY_SCHEMA_VERSION, "entries": [{"category": "character"}]})
    assert not service._has_current_category_schema({"categorySchemaVersion": WIKI_CATEGORY_SCHEMA_VERSION, "graph": {"nodes": [{"category": "character"}]}})
    assert service._has_current_category_schema({"categorySchemaVersion": WIKI_CATEGORY_SCHEMA_VERSION, "entries": [None, {"category": "characters"}], "graph": {"nodes": [None, {"category": ""}]}})

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

    report = service._build_review_report(
        {"entries": [{"id": "a", "needsReview": True}, None], "graph": {"nodes": [1], "edges": [1, 2]}},
        agent_result={"attempted": 1, "completed": 0, "traceId": "t"},
        agent_payload={"review": {"issues": "bad", "recommendations": ["fix"]}},
    )
    assert report["needsReviewEntryIds"] == ["a"]
    assert report["issues"] == [] and report["recommendations"] == ["fix"]
    assert service._workflow_generation_mode("generate_wiki", agent=False) == "local fallback"
    assert service._workflow_generation_mode("generate_wiki", agent=True) == "agent full"
    assert service._workflow_generation_mode("update_wiki", agent=True) == "agent incremental"
    assert service._workflow_generation_mode("refresh_wiki_graph", agent=True) == "agent graph refresh"
    assert service._workflow_generation_mode("review_wiki", agent=True) == "agent review"
    assert service._workflow_generation_mode("repair_wiki", agent=True) == "agent repair"
    assert service._workflow_generation_mode("other", agent=True) == "agent"
    assert "2" in service._workflow_summary("update_wiki", "ok", ["a", "b"])
    assert "WIKI" in service._workflow_summary("review_wiki", "ok", [])
    assert "other" in service._workflow_summary("other", "ok", [])
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


def test_entity_and_text_helpers_cover_optional_paths(service, tmp_path):
    entities = {}
    service._add_entity(entities, {})
    service._add_entity(entities, {"name": " Hero ", "aliases": ["Hero", "H", ""], "sourcePaths": ["a", ""], "needsReview": False})
    service._add_entity(entities, {"name": "Hero", "aliases": ["H", "X"], "sourcePaths": ["a", "b"], "needsReview": True})
    assert entities["Hero"]["aliases"] == ["H", "X"]
    assert entities["Hero"]["sourcePaths"] == ["a", "b"] and entities["Hero"]["needsReview"]

    assert service._character_names_from_source(_source("characters/001_hero.json", '{"name":"Alice","displayName":"Al"}', "character")) == ["hero", "Alice", "Al"]
    assert service._character_names_from_source(_source("characters/README.json", "bad", "character")) == []
    assert service._fallback_character_entities([_source("chapters/1.md", "Alice"),]) == []
    fallback = service._fallback_character_entities([_source("chapters/1.md", "阿离，现身"), _source("chapters/2.md", "阿离，归来")])
    assert any(item["needsReview"] for item in fallback)
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
    assert service._summarize_sources([], "fallback") == "fallback"
    assert service._details_from_sources([_source("a.md", ""), _source("b.md", "body")]) == ["b.md: body"]
    assert service._sources_by_keywords([_source("a.md", "Magic sword"), _source("b.md", "plain")], ["SWORD"])[0]["relativePath"] == "a.md"
    assert service._build_plot_summary([])
    assert "first" in service._build_plot_summary([_source("1.md", "first"), _source("2.md", "latest")])
    assert len(service._chapter_plot_details([_source("chapters/1.md", "line")])) == 1
    assert len(service._chapter_details(_source("chapters/1.md", "line1\n\nline2"), ["Alice", ""])) == 5


def test_character_mapping_entry_edges_dedupe_and_render(service):
    sources = [_source("characters/alice.md", "Alice Al", "character"), _source("chapters/1.md", "Alice appears")]
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
    assert service._edge("a", "b", "label", "type", co_occurrence=True)["coOccurrence"]
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
    overview = service.query_graph(tmp_path, depth="bad", limit="bad")
    assert overview["mode"] == "overview" and overview["graph"]["nodes"][0]["id"] == "project:root"
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

    normalized = service.normalize_payload({"projectName": "Demo", "summary": "Only summary", "entries": "bad", "graph": "bad", "version": 0}, root=tmp_path, workflow="repair_wiki")
    assert normalized["generator"] == "agent-wiki-repair"
    assert normalized["entries"][0]["needsReview"] and normalized["graph"]["nodes"][0]["id"] == "project:root"
    normalized2 = service.normalize_payload({"entries": [{"id": "e", "title": "E", "category": "plot"}], "graph": {"nodes": [{"id": "n"}], "edges": []}, "sourceStats": {"x": 1}}, root=tmp_path, workflow="generate_wiki")
    assert normalized2["sourceStats"] == {"x": 1}

    base = {
        "entries": [
            {"id": "same", "summary": "old", "details": ["old"], "sourcePaths": ["old.md"]},
            {"id": "removed", "summary": "gone", "sourcePaths": ["removed.md"]},
            {"id": "kept", "summary": "kept", "sourcePaths": []},
        ],
        "graph": {
            "nodes": [{"id": "same-node", "entryId": "same"}, {"id": "removed-node", "entryId": "removed"}],
            "edges": [
                {"source": "same-node", "target": "removed-node", "label": "old", "type": "fact"},
                {"source": "same-node", "target": "removed-node", "label": "co", "coOccurrence": True, "evidence": "chapter.md"},
            ],
        },
    }
    incoming = {
        "summary": "incoming",
        "entries": [{"id": "same", "summary": "new", "details": ["new"], "sourcePaths": ["old.md"]}, {"id": "new", "summary": "new"}, {}, None],
        "graph": {
            "nodes": [{"id": "same-node", "entryId": "same", "label": "updated"}, {"id": "new-node", "entryId": "new"}, {}, None],
            "edges": [{"source": "same-node", "target": "new-node", "label": "co2", "coOccurrence": True, "evidence": "chapter.md"}],
        },
        "_replaceFactEdges": True,
    }
    merged = service.merge_payloads(base, incoming, removed_source_paths=["removed.md"], mark_conflicts=True)
    assert {e["id"] for e in merged["entries"]} == {"same", "kept", "new"}
    assert next(e for e in merged["entries"] if e["id"] == "same")["needsReview"]
    assert {n["id"] for n in merged["graph"]["nodes"]} == {"same-node", "new-node"}
    assert [e["label"] for e in merged["graph"]["edges"]] == ["co2"]
    graph_only = service.merge_payloads(base, {"graph": {}}, graph_only=True)
    assert graph_only["entries"] == base["entries"]
    assert service._entry_conflicts({"summary": "a"}, {"summary": "b"})
    assert service._entry_conflicts({"details": ["a"]}, {"details": ["b"]})
    assert not service._entry_conflicts({"summary": ""}, {"summary": "b"})
    assert not service._entry_fully_removed({"sourcePaths": []}, {"a"})
    assert not service._entry_fully_removed({"sourcePaths": ["a", "b"]}, {"a"})
    assert service._entry_fully_removed({"sourcePaths": ["a"]}, {"a"})
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

    snapshot = {
        "edges": [
            None,
            {"source": "", "target": "Bob"},
            {"source": "Alice", "target": "Alice"},
            {"source": "Alice", "target": "Bob", "dimension": "trust", "current_level": -3, "history": [{"detail": "met"}]},
            {"source": "Alice", "target": "Bob", "dimension": "trust", "current_level": 4},
            {"source": "character:alice", "target": "Carol", "dimension": "custom", "current_level": "bad", "history": ["bad"]},
            {"source": "Alice", "target": "Dave", "dimension": ""},
        ]
    }
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    no_new = service._merge_relationship_snapshot_edges(tmp_path, nodes=list(nodes), existing_edges=[], allow_new_nodes=False)
    assert no_new == []
    expanded_nodes = list(nodes)
    merged = service._merge_relationship_snapshot_edges(tmp_path, nodes=expanded_nodes, existing_edges=[], allow_new_nodes=True)
    assert {node["label"] for node in expanded_nodes} == {"Alice", "Bob", "Carol", "Dave"}
    assert len(merged) == 3
    trust = next(edge for edge in merged if edge["dimension"] == "trust")
    assert trust["weight"] == 3 and trust["evidence"] == "met"
    assert next(edge for edge in merged if edge["dimension"] == "custom")["weight"] == 1

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
        {"source": "character:alice", "target": "character:bob", "type": "relationship", "label": "friend"},
        {"source": "character:alice", "target": "character:bob", "type": "relationship", "label": "co", "coOccurrence": True},
        {"source": "character:bob", "target": "character:eve", "type": "relationship", "label": "co", "coOccurrence": True},
        {"source": "character:alice", "target": "place", "type": "appearance"},
    ]
    result = service._query_wiki_relationship_graph(
        "relationships", root=tmp_path, normalized_q="", normalized_entry_id="", normalized_node_id="",
        max_depth=1, max_items=2, entries=entries, entry_by_id={e["id"]: e for e in entries},
        nodes=query_nodes, valid_edges=query_edges, category_labels=CATEGORY_LABELS,
    )
    assert result["category"] == "relationships" and len(result["graph"]["nodes"]) == 2
    assert any(edge["label"] == "friend" for edge in result["graph"]["edges"])
    assert not any(edge.get("coOccurrence") and {edge["source"], edge["target"]} == {"character:alice", "character:bob"} for edge in result["graph"]["edges"])

    category = service._query_wiki_category_graph(
        "characters", root=tmp_path, normalized_q="", normalized_entry_id="", normalized_node_id="",
        max_depth=1, max_items=4, entries=entries, entry_by_id={e["id"]: e for e in entries},
        nodes=[*query_nodes, query_nodes[1], {"id": "", "type": "character"}], valid_edges=query_edges,
        category_labels=CATEGORY_LABELS,
    )
    assert category["mode"] == "category"
    assert all(node["type"] == "character" for node in category["graph"]["nodes"] if not node.get("neighbor"))

    hub = service._wiki_project_hub_node({"projectName": "", "summary": ""}, [{"needsReview": True}, {}])
    assert hub["label"] and hub["count"] == 2 and hub["needsReviewCount"] == 1
    category_hub = service._wiki_category_hub_node("custom", {}, [{"needsReview": True}, {}])
    assert category_hub["count"] == 2 and category_hub["needsReviewCount"] == 1
