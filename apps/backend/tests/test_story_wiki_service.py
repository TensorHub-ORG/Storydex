import copy
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

    assert payload["categorySchemaVersion"] == "story-wiki-v4-stable-chapter-ids"
    assert not any(node["id"] == "stale:node" for node in payload["graph"]["nodes"])
    assert payload["sourceStats"]["chapterFiles"] == 1


def test_character_relationship_mentions_do_not_crosswire_primary_cards(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    cards.mkdir(parents=True)
    cards.joinpath("01_林澈.md").write_text(
        "# 林澈\n\n记者。\n\n## 关系网络\n- **苏晚**（char:suwan）：盟友\n- **顾衡**（char:guheng）：协作\n",
        encoding="utf-8",
    )
    cards.joinpath("02_苏晚.md").write_text(
        "# 苏晚\n\n法医。\n\n## 关系网络\n- **林澈**（char:linche）：盟友\n- **顾衡**（char:guheng）：同事\n",
        encoding="utf-8",
    )
    cards.joinpath("03_顾衡.md").write_text(
        "# 顾衡\n\n警探。\n\n## 关系网络\n- **林澈**（char:linche）：协作\n- **苏晚**（char:suwan）：同事\n",
        encoding="utf-8",
    )

    service = StoryWikiService()
    payload = service.rebuild(tmp_path)
    characters = {
        entry["title"]: entry
        for entry in payload["entries"]
        if entry["category"] == "characters"
    }

    assert set(characters) == {"林澈", "苏晚", "顾衡"}
    for name, filename in (("林澈", "01_林澈.md"), ("苏晚", "02_苏晚.md"), ("顾衡", "03_顾衡.md")):
        entry = characters[name]
        assert entry["summary"].startswith(f"# {name}")
        character_cards = [path for path in entry["sourcePaths"] if "/characters/" in f"/{path}"]
        assert character_cards == [f".storydex/characters/{filename}"]

    relationship_query = service.query_graph(tmp_path, category="relationships")
    relationship_edges = [
        edge
        for edge in relationship_query["graph"]["edges"]
        if edge.get("type") == "relationship" and not edge.get("coOccurrence")
    ]
    assert {node["label"] for node in relationship_query["graph"]["nodes"]} == {"林澈", "苏晚", "顾衡"}
    assert len(relationship_edges) == 3
    assert all(edge.get("status") == "asserted" for edge in relationship_edges)
    assert all(edge.get("relationType") != "unknown" for edge in relationship_edges)


def test_rebuild_layers_planned_and_observed_sources_with_aligned_revision(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    scripts = tmp_path / ".storydex" / "scripts"
    chapters = tmp_path / "chapters"
    cards.mkdir(parents=True)
    scripts.mkdir(parents=True)
    chapters.mkdir(parents=True)
    cards.joinpath("林澈.md").write_text(
        "# 林澈\n\n> 稳定实体ID: `char:linche`\n\n调查记者。\n",
        encoding="utf-8",
    )
    scripts.joinpath("故事大纲.md").write_text(
        "# 故事大纲\n\n林澈计划调查机械鸟失窃案。\n",
        encoding="utf-8",
    )
    chapter_path = chapters / "001.md"
    chapter_path.write_text("# 雾港失窃案\n\n林澈抵达失窃现场。\n", encoding="utf-8")

    service = StoryWikiService()
    initial = service.rebuild(tmp_path)

    assert initial["schemaVersion"] == 2
    assert initial["status"] == "ready"
    assert initial["knowledgeRevision"] == initial["builtFromRevision"]
    assert initial["sourceSetChecksum"].startswith("sha256:")
    character = next(node for node in initial["graph"]["nodes"] if node.get("label") == "林澈")
    assert character["id"] == "char:linche"
    planned = [entry for entry in initial["entries"] if entry.get("knowledgeStatus") == "planned"]
    observed = [entry for entry in initial["entries"] if entry.get("knowledgeStatus") == "observed"]
    assert [entry["title"] for entry in planned] == ["故事大纲"]
    assert any("失窃现场" in entry["summary"] for entry in observed)

    chapter_path.write_text("# 雾港失窃案\n\n林澈在失窃现场发现蓝色羽毛。\n", encoding="utf-8")
    refreshed = service.read_or_build(tmp_path)

    assert refreshed["knowledgeRevision"] == initial["knowledgeRevision"] + 1
    assert refreshed["builtFromRevision"] == refreshed["knowledgeRevision"]
    assert refreshed["status"] == "ready"
    assert any(
        "蓝色羽毛" in entry["summary"]
        for entry in refreshed["entries"]
        if entry.get("knowledgeStatus") == "observed"
    )
    index = service.read_index(tmp_path)
    for key in (
        "schemaVersion",
        "knowledgeRevision",
        "builtFromRevision",
        "sourceSetChecksum",
        "graphChecksum",
        "status",
    ):
        assert index[key] == refreshed[key]
    assert index["sourceStats"] == refreshed["sourceStats"]


def test_invalid_projection_keeps_last_good_and_reports_diagnostics(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    cards.mkdir(parents=True)
    cards.joinpath("林澈.md").write_text(
        "# 林澈\n\n> 稳定实体ID: `char:linche`\n\n调查记者。\n",
        encoding="utf-8",
    )
    service = StoryWikiService()
    baseline = service.rebuild(tmp_path)
    sources = service._collect_sources(tmp_path)

    candidates = []

    dangling = copy.deepcopy(baseline)
    dangling["graph"]["edges"].append({
        "source": "char:linche",
        "target": "char:missing",
        "label": "认识",
        "type": "relationship",
        "relationType": "unknown",
    })
    candidates.append((dangling, "graph.edge.missing_endpoint"))

    internal_label = copy.deepcopy(baseline)
    next(
        node for node in internal_label["graph"]["nodes"]
        if node.get("id") == "char:linche"
    )["label"] = "entity:char:linche"
    candidates.append((internal_label, "graph.node.internal_label"))

    missing_source = copy.deepcopy(baseline)
    next(
        entry for entry in missing_source["entries"]
        if entry.get("id") == "char:linche"
    )["sourcePaths"] = []
    candidates.append((missing_source, "graph.entry.missing_source"))

    for candidate, expected_code in candidates:
        diagnostics = service.validate_graph_invariants(candidate, root=tmp_path)
        assert expected_code in {item["code"] for item in diagnostics}

        rejected = service._persist_payload(
            tmp_path,
            candidate,
            workflow="test_invalid_projection",
            status="completed",
            agent_result=None,
            sources=sources,
            changed_paths=[],
        )
        persisted = json.loads(service.wiki_json_path(tmp_path).read_text(encoding="utf-8"))

        assert rejected["status"] == "error"
        assert expected_code in {item["code"] for item in rejected["diagnostics"]}
        assert rejected["lastSuccessfulRevision"] == baseline["knowledgeRevision"]
        assert persisted["graphChecksum"] == baseline["graphChecksum"]
        assert persisted["status"] == "ready"


def test_character_rename_keeps_structured_entity_id_and_old_alias(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    cards.mkdir(parents=True)
    old_path = cards / "林澈.md"
    old_path.write_text("# 林澈\n\n> 稳定实体ID: `char:linche`\n", encoding="utf-8")
    service = StoryWikiService()

    initial = service.rebuild(tmp_path)
    assert any(node.get("id") == "char:linche" and node.get("label") == "林澈" for node in initial["graph"]["nodes"])

    old_path.unlink()
    cards.joinpath("林岚.md").write_text("# 林岚\n\n> 稳定实体ID: `char:linche`\n", encoding="utf-8")
    renamed = service.sync_local_incremental(tmp_path)
    node = next(node for node in renamed["graph"]["nodes"] if node.get("id") == "char:linche")
    entry = next(entry for entry in renamed["entries"] if entry.get("id") == "char:linche")

    assert node["label"] == "林岚"
    assert entry["title"] == "林岚"
    assert "林澈" in entry["aliases"]


def test_character_card_registry_lifecycle_archives_and_reactivates_without_touching_registry_only_roles(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    cards.mkdir(parents=True)
    card_path = cards / "顾衡.md"
    card_path.write_text("# 顾衡\n\n> 稳定实体ID: `char:guheng`\n", encoding="utf-8")
    registry_path = tmp_path / ".storydex" / "memory" / "current" / "entities.json"
    _write_json(
        registry_path,
        {
            "version": 2,
            "entities": [
                {
                    "entityId": "char:guheng",
                    "canonical_name": "顾衡",
                    "kind": "character",
                    "status": "active",
                    "sourcePaths": [".storydex/characters/顾衡.md"],
                },
                {
                    "entityId": "char:registry-only",
                    "canonical_name": "档案角色",
                    "kind": "character",
                    "status": "active",
                },
            ],
        },
    )
    service = StoryWikiService()

    initial = service.rebuild(tmp_path)
    assert {"char:guheng", "char:registry-only"} <= {
        node.get("id") for node in initial["graph"]["nodes"]
    }

    card_path.unlink()
    deleted = service.sync_local_incremental(tmp_path)
    assert "char:guheng" not in {node.get("id") for node in deleted["graph"]["nodes"]}
    assert "char:registry-only" in {node.get("id") for node in deleted["graph"]["nodes"]}
    deleted_records = {
        item["entityId"]: item
        for item in json.loads(registry_path.read_text(encoding="utf-8"))["entities"]
    }
    assert deleted_records["char:guheng"]["status"] == "archived"
    assert deleted_records["char:guheng"]["sourcePaths"] == []
    assert deleted_records["char:registry-only"]["status"] == "active"

    cards.joinpath("顾衡归来.md").write_text(
        "# 顾衡归来\n\n> 稳定实体ID: `char:guheng`\n",
        encoding="utf-8",
    )
    restored = service.sync_local_incremental(tmp_path)
    restored_node = next(node for node in restored["graph"]["nodes"] if node.get("id") == "char:guheng")
    assert restored_node["label"] == "顾衡归来"
    restored_records = {
        item["entityId"]: item
        for item in json.loads(registry_path.read_text(encoding="utf-8"))["entities"]
    }
    assert restored_records["char:guheng"]["status"] == "active"
    assert "顾衡" in restored_records["char:guheng"]["aliases"]
    assert restored_records["char:guheng"]["sourcePaths"] == [".storydex/characters/顾衡归来.md"]


def test_incremental_projection_matches_cold_rebuild_canonical_checksum(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    scripts = tmp_path / ".storydex" / "scripts"
    chapters = tmp_path / "chapters"
    cards.mkdir(parents=True)
    scripts.mkdir(parents=True)
    chapters.mkdir(parents=True)
    cards.joinpath("林澈.md").write_text(
        "# 林澈\n\n> 稳定实体ID: `char:linche`\n\n调查记者。\n",
        encoding="utf-8",
    )
    scripts.joinpath("故事大纲.md").write_text(
        "# 故事大纲\n\n林澈计划调查失窃案。\n",
        encoding="utf-8",
    )
    chapter = chapters / "001.md"
    chapter.write_text("# 第一章\n\n林澈抵达现场。\n", encoding="utf-8")
    service = StoryWikiService()
    service.rebuild(tmp_path)

    chapter.write_text("# 第一章\n\n林澈抵达现场并发现蓝色羽毛。\n", encoding="utf-8")
    cards.joinpath("苏晚.md").write_text(
        "# 苏晚\n\n> 稳定实体ID: `char:suwan`\n\n法医。\n",
        encoding="utf-8",
    )
    incremental = service.sync_local_incremental(tmp_path)

    service.wiki_json_path(tmp_path).unlink()
    service.wiki_index_path(tmp_path).unlink()
    service.wiki_markdown_path(tmp_path).unlink()
    cold = service.read_or_build(tmp_path)

    assert cold["graphChecksum"] == incremental["graphChecksum"]
    assert cold["sourceSetChecksum"] == incremental["sourceSetChecksum"]
    assert cold["knowledgeRevision"] == incremental["knowledgeRevision"]
    assert cold["sourceStats"] == incremental["sourceStats"]


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


def test_sync_local_incremental_keeps_chapter_entries_stable_when_inserting(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    chapters.joinpath("001.md").write_text("第一章：沈青出发。\n", encoding="utf-8")
    chapters.joinpath("003.md").write_text("第三章：沈青抵达云桥。\n", encoding="utf-8")
    service = StoryWikiService()

    initial = service.rebuild(tmp_path)
    initial_titles = {
        entry["id"]: entry["sourcePaths"]
        for entry in initial["entries"]
        if str(entry["id"]).startswith("chapter:")
    }
    assert len(initial_titles) == 2

    # 在两章之间插入新章节：旧实现按排序位置命名 chapter ID，
    # 会让新章顶掉 003 的条目并导致内容错位。
    chapters.joinpath("002.md").write_text("第二章：沈青夜宿荒村。\n", encoding="utf-8")
    updated = service.sync_local_incremental(tmp_path)

    chapter_entries = {
        entry["id"]: entry
        for entry in updated["entries"]
        if str(entry["id"]).startswith("chapter:")
    }
    assert len(chapter_entries) == 3
    by_source = {
        tuple(entry["sourcePaths"]): entry["id"]
        for entry in chapter_entries.values()
    }
    assert ("chapters/001.md",) in by_source
    assert ("chapters/002.md",) in by_source
    assert ("chapters/003.md",) in by_source
    # 每个章节的条目摘要必须与自身内容一致，未变更章节不被新章覆盖。
    for entry in chapter_entries.values():
        source = entry["sourcePaths"][0]
        if source.endswith("001.md"):
            assert "出发" in entry["summary"]
        elif source.endswith("002.md"):
            assert "夜宿荒村" in entry["summary"]
        elif source.endswith("003.md"):
            assert "云桥" in entry["summary"]


def test_dedupe_edges_evicts_by_weight_over_limit():
    from services.story_wiki_service import MAX_WIKI_GRAPH_EDGES

    service = StoryWikiService()
    edges = []
    for index in range(MAX_WIKI_GRAPH_EDGES + 50):
        edges.append(
            {
                "source": f"a{index}",
                "target": f"b{index}",
                "label": "共现",
                "type": "relationship",
                "weight": 1,
                "coOccurrence": True,
            }
        )
    heavy = {
        "source": "hero",
        "target": "rival",
        "label": "宿敌",
        "type": "relationship",
        "weight": 99,
    }
    edges.append(heavy)

    result = service._dedupe_edges(edges)

    assert len(result) == MAX_WIKI_GRAPH_EDGES
    assert any(edge["source"] == "hero" and edge["target"] == "rival" for edge in result)
