import json

from services.story_wiki_service import StoryWikiService


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_rebuild_ignores_framework_files_and_keeps_empty_project_minimal(tmp_path):
    tmp_path.joinpath("README.md").write_text(
        ("存放 文件 更新 条目 用途 要求 势力 物品 禁忌 变量思考\n" * 4),
        encoding="utf-8",
    )
    tmp_path.joinpath(".storydex", ".agent", "skills").mkdir(parents=True)
    tmp_path.joinpath(".storydex", ".agent", "skills", "README.md").write_text("技能模板说明\n", encoding="utf-8")
    tmp_path.joinpath(".storydex", "templates").mkdir(parents=True)
    tmp_path.joinpath(".storydex", "templates", "README.md").write_text("角色模板说明\n", encoding="utf-8")
    _write_json(tmp_path / ".storydex" / "presets" / "default.json", {"name": "默认预设"})
    _write_json(tmp_path / ".storydex" / "config" / "runtime.json", {"name": "运行配置"})

    payload = StoryWikiService().rebuild(tmp_path)

    assert payload["sourceStats"] == {
        "scannedFiles": 0,
        "chapterFiles": 0,
        "characters": 0,
    }
    assert [entry["id"] for entry in payload["entries"]] == ["overview:project"]
    assert [node["id"] for node in payload["graph"]["nodes"]] == ["project:root"]
    assert payload["graph"]["edges"] == []
    assert "暂无故事内容" in payload["entries"][0]["summary"]


def test_rebuild_uses_entity_registry_aliases_and_fact_edges(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    chapters.joinpath("001.md").write_text("阿离推开门，见到了沈青。\n", encoding="utf-8")
    chapters.joinpath("002.md").write_text("离儿与沈青在云桥重逢，沈青交给阿离一枚令牌。\n", encoding="utf-8")
    _write_json(
        tmp_path / ".storydex" / "memory" / "current" / "entities.json",
        {
            "version": 1,
            "entities": [
                {"canonical_name": "林阿离", "aliases": ["阿离", "离儿"], "kind": "character"},
                {"canonical_name": "沈青", "aliases": ["青叔"], "kind": "character"},
                {"canonical_name": "云桥", "kind": "location"},
                {"canonical_name": "令牌", "kind": "item"},
            ],
        },
    )
    _write_json(
        tmp_path / ".storydex" / "memory" / "current" / "facts.json",
        {
            "version": 1,
            "facts": [
                {
                    "subject": "阿离",
                    "predicate": "持有",
                    "object": "令牌",
                    "confidence": "canon",
                    "established_in": "chapters/002.md",
                    "evidence": "沈青交给阿离一枚令牌",
                },
                {"subject": "沈青", "predicate": "认识", "object": "未登记地点", "confidence": "canon"},
            ],
        },
    )

    payload = StoryWikiService().rebuild(tmp_path)
    nodes = payload["graph"]["nodes"]
    edges = payload["graph"]["edges"]
    node_by_label = {node["label"]: node for node in nodes}

    assert {node["label"] for node in nodes if node["type"] == "character"} == {"林阿离", "沈青"}
    assert node_by_label["云桥"]["type"] == "location"
    assert node_by_label["云桥"]["category"] == "setting"
    assert node_by_label["令牌"]["type"] == "item"
    assert "未登记地点" not in node_by_label

    lin_id = node_by_label["林阿离"]["id"]
    shen_id = node_by_label["沈青"]["id"]
    token_id = node_by_label["令牌"]["id"]
    chapter_ids = {node["id"] for node in nodes if node["type"] == "chapter"}

    lin_appearances = {
        edge["target"]
        for edge in edges
        if edge["source"] == lin_id and edge["type"] == "appearance"
    }
    assert chapter_ids <= lin_appearances

    assert any(
        edge["source"] == lin_id
        and edge["target"] == token_id
        and edge["type"] == "fact"
        and edge["label"] == "持有"
        and ".storydex/memory/current/facts.json" in edge["evidence"]
        for edge in edges
    )

    co_occurrence_edges = [
        edge
        for edge in edges
        if edge.get("coOccurrence")
        and {edge["source"], edge["target"]} == {lin_id, shen_id}
    ]
    assert len(co_occurrence_edges) == 1
    assert co_occurrence_edges[0]["weight"] == 2
    assert "chapters/001.md" in co_occurrence_edges[0]["evidence"]
    assert "chapters/002.md" in co_occurrence_edges[0]["evidence"]


def test_read_or_build_rebuilds_old_category_schema_payload(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    chapters.joinpath("001.md").write_text("沈青抵达云桥。\n", encoding="utf-8")
    wiki_path = tmp_path / ".storydex" / "wiki" / "knowledge_graph.json"
    _write_json(
        wiki_path,
        {
            "version": 1,
            "categorySchemaVersion": "story-wiki-v2-five-category",
            "entries": [
                {
                    "id": "stale:entry",
                    "title": "旧污染条目",
                    "category": "characters",
                    "summary": "旧缓存",
                }
            ],
            "graph": {"nodes": [{"id": "stale:node", "label": "旧节点", "type": "character"}], "edges": []},
        },
    )

    payload = StoryWikiService().read_or_build(tmp_path)

    assert payload["categorySchemaVersion"] == "story-wiki-v3-entity-source"
    assert not any(node["id"] == "stale:node" for node in payload["graph"]["nodes"])
    assert payload["sourceStats"]["chapterFiles"] == 1


def test_sync_local_incremental_replaces_fact_edges_when_facts_change(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    chapters.joinpath("001.md").write_text("阿离拿起令牌。\n", encoding="utf-8")
    _write_json(
        tmp_path / ".storydex" / "memory" / "current" / "entities.json",
        {
            "version": 1,
            "entities": [
                {"canonical_name": "林阿离", "aliases": ["阿离"], "kind": "character"},
                {"canonical_name": "令牌", "kind": "item"},
            ],
        },
    )
    facts_path = tmp_path / ".storydex" / "memory" / "current" / "facts.json"
    _write_json(
        facts_path,
        {"version": 1, "facts": [{"subject": "阿离", "predicate": "持有", "object": "令牌"}]},
    )
    service = StoryWikiService()

    initial = service.rebuild(tmp_path)
    assert any(edge["type"] == "fact" and edge["label"] == "持有" for edge in initial["graph"]["edges"])

    _write_json(
        facts_path,
        {"version": 1, "facts": [{"subject": "阿离", "predicate": "交还", "object": "令牌"}]},
    )
    updated = service.sync_local_incremental(tmp_path)

    fact_labels = [edge["label"] for edge in updated["graph"]["edges"] if edge["type"] == "fact"]
    assert fact_labels == ["交还"]
