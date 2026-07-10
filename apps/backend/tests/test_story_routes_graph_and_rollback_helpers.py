from __future__ import annotations

import json
import types
from pathlib import Path

from api import routes_story as routes


def test_character_graph_nodes_paths_lookup_and_relationship_parsers(monkeypatch, tmp_path):
    storydex = tmp_path / ".storydex"
    chars = storydex / "characters"
    cards = chars / "cards"
    cards.mkdir(parents=True)
    alice = cards / "001_alice.json"
    alice.write_text(json.dumps({
        "id": "alice-id", "name": "Alice",
        "stable_relationships": [
            {"target": "Bob", "relation_type": "trusted ally", "note": "saved her"},
            {"target": "Alice", "relation": "self"},
            "bad",
        ],
    }), encoding="utf-8")
    bob = chars / "002_bob.md"
    bob.write_text("# Bob\n\n## Relationships\n- Alice: hostile rival\n- none\ninvalid line\n", encoding="utf-8")
    duplicate = chars / "alice.txt"
    duplicate.write_text("# Alice\n", encoding="utf-8")
    (chars / "README.md").write_text("ignore", encoding="utf-8")
    (chars / "unsupported.csv").write_text("ignore", encoding="utf-8")
    monkeypatch.setattr(routes, "story_project_service", types.SimpleNamespace(storydex_root=lambda root: storydex))

    assert routes._read_optional_json(tmp_path / "missing.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    assert routes._read_optional_json(bad) == {}
    bad.write_text("{broken", encoding="utf-8")
    assert routes._read_optional_json(bad) == {}
    assert routes._compact_graph_text(" a\n b ") == "a b"
    assert routes._relative_project_path(tmp_path, alice).endswith("001_alice.json")
    assert routes._relative_project_path(tmp_path, Path("C:/outside/file.md"))
    assert routes._character_name_from_stem("001_Alice") == "Alice"
    assert routes._character_name_from_stem("") == ""
    assert routes._character_name_from_markdown(bob) == "Bob"
    assert routes._character_graph_node_from_file(tmp_path, alice)["characterId"] == "alice-id"
    assert routes._character_graph_node_from_file(tmp_path, bob)["id"] == "Bob"
    assert routes._character_graph_node_from_file(tmp_path, chars / "unsupported.csv") is None

    paths = routes._iter_character_graph_paths(tmp_path)
    assert alice in paths and bob in paths and duplicate in paths
    nodes = routes._read_character_graph_nodes(tmp_path)
    assert {node["id"] for node in nodes} == {"Alice", "Bob"}
    lookup = routes._relationship_node_lookup([None, {}, *nodes])
    assert lookup["Alice"] == "Alice" and lookup["alice-id"] == "Alice"
    assert routes._is_character_graph_node_kind("") is True
    assert routes._is_character_graph_node_kind("location") is False
    assert routes._character_id_for_raw_relationship_node({"id": "alice-id", "kind": "character"}, lookup) == "Alice"
    assert routes._character_id_for_raw_relationship_node({"id": "x", "kind": "location"}, lookup) == ""
    assert routes._canonical_relationship_endpoint("Bob", lookup) == "Bob"

    dimensions = {
        "enemy": "hostility", "rival": "rivalry", "ally": "alliance", "trust": "trust",
        "loyal": "loyalty", "friend": "intimacy", "mentor": "professional", "family": "family", "unknown": "intimacy",
    }
    for text, expected in dimensions.items():
        assert routes._relationship_dimension_from_text(text) == expected
    assert routes._relationship_level_for_dimension("hostility") == -2
    assert routes._relationship_level_for_dimension("trust") == 2
    assert routes._relationship_level_for_dimension("professional") == 0
    assert routes._clean_relationship_target("与 Bob 的关系") == "Bob"

    edge = routes._build_derived_relationship_edge(
        tmp_path, bob, source="Bob", target="Alice", relation="friend", detail="trusted"
    )
    assert edge["dimension"] == "trust" and edge["current_level"] == 2
    assert routes._build_derived_relationship_edge(tmp_path, bob, source="", target="A", relation="x", detail="") is None
    assert routes._build_derived_relationship_edge(tmp_path, bob, source="A", target="A", relation="x", detail="y") is None
    assert routes._build_derived_relationship_edge(tmp_path, bob, source="A", target="B", relation="", detail="") is None
    mapped = routes._relationship_from_mapping(
        tmp_path, alice, source="Alice", payload={"targetId": "Bob", "status": "ally", "description": "works together"}, node_lookup=lookup
    )
    assert mapped["target"] == "Bob"
    assert routes._parse_markdown_relationship_line(tmp_path, bob, source="Bob", line="1. Alice: hostile", node_lookup=lookup)
    assert routes._parse_markdown_relationship_line(tmp_path, bob, source="Bob", line="none", node_lookup=lookup) is None
    assert routes._parse_markdown_relationship_line(tmp_path, bob, source="Bob", line="invalid", node_lookup=lookup) is None
    assert routes._relationships_from_markdown_card(tmp_path, bob, source="Bob", node_lookup=lookup)
    edges = routes._read_character_relationship_edges(tmp_path, node_lookup=lookup)
    assert len(edges) >= 2
    assert len({routes._relationship_edge_key(item) for item in edges}) == len(edges)


def test_relationship_graph_snapshot_filters_raw_and_merges_derived(monkeypatch, tmp_path):
    storydex = tmp_path / ".storydex"
    chars = storydex / "characters/cards"
    current = storydex / "memory/current"
    chars.mkdir(parents=True)
    current.mkdir(parents=True)
    (chars / "alice.json").write_text(json.dumps({"id": "a", "name": "Alice", "relationships": [{"target": "Bob", "relation": "friend"}]}), encoding="utf-8")
    (chars / "bob.json").write_text(json.dumps({"id": "b", "name": "Bob"}), encoding="utf-8")
    graph = {
        "nodes": [None, {"id": "a", "label": "Alice", "kind": "character"}, {"id": "place", "kind": "location"}],
        "edges": [
            None,
            {"source": "a", "target": "b", "dimension": "trust"},
            {"source": "a", "target": "a", "dimension": "trust"},
            {"source": "a", "target": "missing", "dimension": "trust"},
            {"source": "a", "target": "b", "dimension": "trust"},
        ],
    }
    (current / "relationship_graph.json").write_text(json.dumps(graph), encoding="utf-8")
    monkeypatch.setattr(routes, "story_project_service", types.SimpleNamespace(storydex_root=lambda root: storydex))
    snapshot = routes._read_relationship_graph_snapshot(tmp_path, current)
    assert {node["id"] for node in snapshot["nodes"]} == {"Alice", "Bob"}
    assert all(edge["source"] != edge["target"] for edge in snapshot["edges"])
    assert len({routes._relationship_edge_key(edge) for edge in snapshot["edges"]}) == len(snapshot["edges"])

    (current / "relationship_graph.json").write_text(json.dumps({"edges": "bad", "nodes": "bad"}), encoding="utf-8")
    empty = routes._read_relationship_graph_snapshot(tmp_path, current)
    assert len(empty["nodes"]) == 2


def test_segment_sort_numeric_and_all_truncation_helpers(tmp_path):
    assert routes._segment_sort_key("x") == ("x",)
    assert routes._segment_sort_key("chapters/第2章/seg-10.md") > routes._segment_sort_key("chapters/第2章/seg-2.md")
    assert routes._parse_segment_numeric("第3章-seg-001") == 1
    assert routes._parse_segment_numeric("none") == -1

    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"entries": [None, {"segment_id": "none"}, {"segment_id": "001"}, {"segment_id": "002"}, {"segment_id": "003"}]}), encoding="utf-8")
    routes._truncate_ledger_by_segment(ledger, "entries", 2, True)
    assert len(json.loads(ledger.read_text(encoding="utf-8"))["entries"]) == 4
    routes._truncate_ledger_by_segment(ledger, "entries", 2, False)
    assert len(json.loads(ledger.read_text(encoding="utf-8"))["entries"]) == 3
    routes._truncate_ledger_by_segment(tmp_path / "missing", "entries", -1, True)

    relationships = tmp_path / "relationships.json"
    relationships.write_text(json.dumps({"edges": [None, {"history": [None, {"segment_id": "001", "delta": "increase", "magnitude": "major"}, {"segment_id": "002", "delta": "decrease", "magnitude": "minor"}, {"segment_id": "003", "delta": "forge", "magnitude": "moderate"}]}]}), encoding="utf-8")
    routes._truncate_relationship_history(relationships, 2, True)
    edge = json.loads(relationships.read_text(encoding="utf-8"))["edges"][1]
    assert edge["current_level"] == 2 and len(edge["history"]) == 3

    foreshadow = tmp_path / "foreshadow.json"
    foreshadow.write_text(json.dumps({"threads": {
        "remove": {"planted_at": {"segment_id": "003"}},
        "keep": {"planted_at": {"segment_id": "001"}, "callbacks": [None, {"segment_id": "002"}, {"segment_id": "003"}], "resolved_at": {"segment_id": "003"}, "status": "resolved"},
        "bad": "value",
    }}), encoding="utf-8")
    routes._truncate_foreshadow_threads(foreshadow, 2, True)
    threads = json.loads(foreshadow.read_text(encoding="utf-8"))["threads"]
    assert "remove" not in threads and threads["keep"]["resolved_at"] is None and threads["keep"]["status"] == "recalled"

    outline = tmp_path / "outline.json"
    outline.write_text(json.dumps({"chapters": {"1": {"milestones": [None, {"segment_id": "001"}, {"segment_id": "003"}]}, "bad": "x"}}), encoding="utf-8")
    routes._truncate_chapter_outline(outline, 2, True)
    assert len(json.loads(outline.read_text(encoding="utf-8"))["chapters"]["1"]["milestones"]) == 2

    for path, fn in ((ledger, routes._truncate_ledger_by_segment), (relationships, routes._truncate_relationship_history), (foreshadow, routes._truncate_foreshadow_threads), (outline, routes._truncate_chapter_outline)):
        path.write_text("{broken", encoding="utf-8")
        if fn is routes._truncate_ledger_by_segment:
            fn(path, "entries", 1, True)
        else:
            fn(path, 1, True)
