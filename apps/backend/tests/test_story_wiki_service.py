import asyncio
import copy
import json
import os

import pytest

from services.story_project_service import StoryProjectService
from services.story_knowledge_relation_service import StoryKnowledgeRelationService
from services.content_catalog_service import ContentCatalogService, get_content_catalog_service, reset_content_catalog_services
from services.story_wiki_service import WIKI_CATEGORY_SCHEMA_VERSION, StoryWikiService


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_catalog_backed_wiki_bootstrap_and_warm_query_do_not_collect_sources(
    tmp_path,
    monkeypatch,
):
    chapter = tmp_path / "chapters" / "001.md"
    chapter.parent.mkdir(parents=True)
    chapter.write_text("星核密钥位于北塔。\n", encoding="utf-8")
    reset_content_catalog_services()
    catalog = get_content_catalog_service(tmp_path)
    snapshot = catalog.snapshot()
    service = StoryWikiService()

    published = service.refresh_from_catalog(
        tmp_path,
        snapshot,
        changed_paths=tuple(snapshot.entries),
    )
    assert published["status"] == "ready"
    assert published["catalogGeneration"] == snapshot.generation

    monkeypatch.setattr(service, "_collect_sources", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("warm catalog query collected workspace sources")
    ))
    monkeypatch.setattr(service, "_migrate_knowledge_relation_storage", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("warm catalog query migrated storage")
    ))
    monkeypatch.setattr(service, "_reconcile_entity_registry", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("warm catalog query reconciled entities")
    ))

    result = service.query_graph(tmp_path, q="星核密钥")
    assert result["status"] == "ready"
    assert result["queryStatus"] if "queryStatus" in result else True
    assert any("星核密钥" in str(entry) for entry in result["entries"])


def test_catalog_wiki_incremental_update_reads_only_changed_file_and_handles_rename_delete(
    tmp_path,
    monkeypatch,
):
    first = tmp_path / "chapters" / "001.md"
    second = tmp_path / "chapters" / "002.md"
    first.parent.mkdir(parents=True)
    first.write_text("stable-alpha\n", encoding="utf-8")
    second.write_text("stable-beta\n", encoding="utf-8")
    catalog = ContentCatalogService(tmp_path)
    initial = catalog.snapshot()
    service = StoryWikiService()
    service.refresh_from_catalog(tmp_path, initial, changed_paths=tuple(initial.entries))

    first.write_text("changed-alpha\n", encoding="utf-8")
    catalog.notify_external_changes([first])
    updated_snapshot = catalog.refresh_dirty()
    changed = catalog.changed_paths(initial, updated_snapshot)
    reads = []
    original = service._source_from_path

    def tracked(root, path):
        reads.append(path.relative_to(root).as_posix())
        return original(root, path)

    monkeypatch.setattr(service, "_source_from_path", tracked)
    service.refresh_from_catalog(tmp_path, updated_snapshot, changed_paths=changed)
    assert reads == ["chapters/001.md"]
    assert service.query_graph(tmp_path, q="changed-alpha")["status"] == "ready"
    assert service.query_graph(tmp_path, q="stable-beta")["status"] == "ready"

    renamed = first.with_name("renamed.md")
    first.rename(renamed)
    catalog.notify_external_changes([first, renamed])
    renamed_snapshot = catalog.refresh_dirty()
    service.refresh_from_catalog(
        tmp_path,
        renamed_snapshot,
        changed_paths=catalog.changed_paths(updated_snapshot, renamed_snapshot),
    )
    assert service.query_graph(tmp_path, q="changed-alpha")["status"] == "ready"
    assert "chapters/001.md" not in {
        path
        for source in service._read_source_snapshot(tmp_path).values()
        for path in [str(source.get("relativePath") or "")]
    }

    renamed.unlink()
    catalog.notify_external_changes([renamed])
    deleted_snapshot = catalog.refresh_dirty()
    service.refresh_from_catalog(
        tmp_path,
        deleted_snapshot,
        changed_paths=catalog.changed_paths(renamed_snapshot, deleted_snapshot),
    )
    assert service.query_graph(tmp_path, q="changed-alpha")["status"] == "ready"
    assert service.query_graph(tmp_path, q="stable-beta")["status"] == "ready"


def test_catalog_generation_mismatch_is_explicitly_unavailable(tmp_path):
    chapter = tmp_path / "chapters" / "001.md"
    chapter.parent.mkdir(parents=True)
    chapter.write_text("generation-one\n", encoding="utf-8")
    reset_content_catalog_services()
    catalog = get_content_catalog_service(tmp_path)
    first = catalog.snapshot()
    service = StoryWikiService()
    service.refresh_from_catalog(tmp_path, first, changed_paths=tuple(first.entries))

    chapter.write_text("generation-two\n", encoding="utf-8")
    catalog.notify_external_changes([chapter])
    second = catalog.refresh_dirty()
    result = service.query_graph(tmp_path, q="generation-one")

    assert result["status"] == "stale"
    assert result["queryStatus"] == "unavailable"
    assert result["entries"] == []
    assert result["catalogGeneration"] == second.generation


def test_catalog_wiki_blocking_publish_keeps_last_good_projection(tmp_path, monkeypatch):
    chapter = tmp_path / "chapters" / "001.md"
    chapter.parent.mkdir(parents=True)
    chapter.write_text("last-good\n", encoding="utf-8")
    catalog = ContentCatalogService(tmp_path)
    first = catalog.snapshot()
    service = StoryWikiService()
    service.refresh_from_catalog(tmp_path, first, changed_paths=tuple(first.entries))
    old_bytes = service.wiki_json_path(tmp_path).read_bytes()

    chapter.write_text("broken-publish\n", encoding="utf-8")
    catalog.notify_external_changes([chapter])
    second = catalog.refresh_dirty()
    original_validate = service.validate_graph_invariants

    def blocking(*args, **kwargs):
        return [
            {
                "code": "test.catalog.publish_failure",
                "message": "forced catalog publish failure",
                "path": "graph",
                "blocking": True,
            }
        ]

    monkeypatch.setattr(service, "validate_graph_invariants", blocking)
    rejected = service.refresh_from_catalog(
        tmp_path,
        second,
        changed_paths=catalog.changed_paths(first, second),
    )
    assert rejected["status"] == "error"
    assert service.wiki_json_path(tmp_path).read_bytes() == old_bytes
    assert service._read_projection_status(tmp_path)["status"] == "error"
    assert service.query_graph(tmp_path, q="last-good")["queryStatus"] == "unavailable"
    monkeypatch.setattr(service, "validate_graph_invariants", original_validate)


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
                {
                    "subject": "沈青",
                    "predicate": "位于",
                    "object": "云桥",
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
        and edge["evidence"] == "沈青交给阿离一枚令牌"
        and edge["sourcePath"] == "chapters/002.md"
        for edge in edges
    )
    assert not any(
        edge.get("source") == shen_id
        and edge.get("target") == node_by_label["云桥"]["id"]
        and edge.get("type") == "fact"
        for edge in edges
    )

    assert not any(edge.get("coOccurrence") for edge in edges)
    assert not any(
        edge.get("type") == "relationship"
        and {edge["source"], edge["target"]} == {lin_id, shen_id}
        for edge in edges
    )


def test_co_occurring_characters_remain_disconnected_without_explicit_relation(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    chapters.joinpath("001.md").write_text(
        "江言在站台东侧等车。林北从西侧进站，两人没有见面，也没有交谈。\n",
        encoding="utf-8",
    )
    _write_json(tmp_path / ".storydex" / "memory" / "current" / "entities.json", {
        "version": 1,
        "entities": [
            {"entityId": "char:jiangyan", "canonical_name": "江言", "kind": "character"},
            {"entityId": "char:linbei", "canonical_name": "林北", "kind": "character"},
        ],
    })

    service = StoryWikiService()
    payload = service.rebuild(tmp_path)
    relation_view = service.query_graph(tmp_path, category="relationships")

    assert {node["label"] for node in relation_view["graph"]["nodes"]} == {"江言", "林北"}
    assert relation_view["graph"]["edges"] == []
    assert not any(edge.get("coOccurrence") for edge in payload["graph"]["edges"])


def test_relationship_projection_requires_known_endpoint_and_asserted_semantics(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    cards.mkdir(parents=True)
    cards.joinpath("01_江言.md").write_text(
        "# 江言\n\n> 稳定实体ID: `char:jiangyan`\n\n## 关系网络\n"
        "- 沈乔（char:shenqiao）：亲姐姐，二人从小一起生活\n"
        "- 沈乔（char:wrong）：朋友\n"
        "- 林北（char:linbei）：可能是朋友，尚未确认\n"
        "- 林北（char:linbei）：不是朋友，只是互不认识\n"
        "- 陌生人（char:missing）：朋友\n",
        encoding="utf-8",
    )
    cards.joinpath("02_沈乔.md").write_text(
        "# 沈乔\n\n> 稳定实体ID: `char:shenqiao`\n",
        encoding="utf-8",
    )
    cards.joinpath("03_林北.md").write_text(
        "# 林北\n\n> 稳定实体ID: `char:linbei`\n",
        encoding="utf-8",
    )

    service = StoryWikiService()
    payload = service.rebuild(tmp_path)
    relation_view = service.query_graph(tmp_path, category="relationships")
    edges = relation_view["graph"]["edges"]

    assert len(edges) == 1
    assert {edges[0]["source"], edges[0]["target"]} == {"char:jiangyan", "char:shenqiao"}
    assert edges[0]["relationType"] == "family"
    assert edges[0]["sourcePath"] == ".storydex/characters/01_江言.md"
    assert {"level", "strength", "confidence", "polarity"}.isdisjoint(edges[0])
    assert not any(str(node.get("id") or "").startswith("unresolved:") for node in payload["graph"]["nodes"])
    assert not any(edge.get("relationType") == "unknown" for edge in payload["graph"]["edges"])


def test_relationship_query_accepts_agent_domain_edge_types_without_structural_edges(tmp_path, monkeypatch):
    """Regression for the agent-evidence-grounded/agent_reviewed 13/21 projection."""
    chapter_path = "chapters/第1章 雨夜旧水厂/001.md"
    chapter = tmp_path / chapter_path
    chapter.parent.mkdir(parents=True)
    chapter.write_text(
        "沈砚把陆遥往自己身边拉近，压低声音说：\"从现在开始，你跟着我\"。陆遥点头跟随。\n",
        encoding="utf-8",
    )

    entries = [
        {"id": "overview:project", "title": "项目总览", "category": "overview", "sourcePaths": []},
        {"id": "plot:mainline", "title": "主线", "category": "plot", "sourcePaths": [chapter_path]},
        {"id": "chapter:001", "title": "雨夜旧水厂", "category": "plot", "sourcePaths": [chapter_path]},
        {"id": "entity-001", "title": "沈砚", "category": "characters", "sourcePaths": [chapter_path]},
        {"id": "entity-002", "title": "陆遥", "category": "characters", "sourcePaths": [chapter_path]},
        {"id": "entity-003", "title": "灰塔", "category": "setting", "sourcePaths": [chapter_path]},
        {"id": "entity-004", "title": "匿名报信者", "category": "characters", "sourcePaths": [chapter_path]},
        {"id": "item-001", "title": "北门铜钥匙", "category": "setting", "sourcePaths": [chapter_path]},
        {"id": "location-001", "title": "三河镇旧水厂", "category": "setting", "sourcePaths": [chapter_path]},
        {"id": "location-002", "title": "三河镇", "category": "setting", "sourcePaths": [chapter_path]},
        {"id": "foreshadow-001", "title": "三角形符号", "category": "plot", "sourcePaths": [chapter_path]},
        {"id": "foreshadow-002", "title": "沈砚亦是目标", "category": "plot", "sourcePaths": [chapter_path]},
        {"id": "foreshadow-003", "title": "报信者身份", "category": "plot", "sourcePaths": [chapter_path]},
    ]
    node_specs = [
        ("project:root", "test0802", "project", "overview:project"),
        ("plot:mainline", "主线", "event", "plot:mainline"),
        ("chapter:001", "雨夜旧水厂", "chapter", "chapter:001"),
        ("entity-001", "沈砚", "character", "entity-001"),
        ("entity-002", "陆遥", "character", "entity-002"),
        ("entity-003", "灰塔", "faction", "entity-003"),
        ("entity-004", "匿名报信者", "character", "entity-004"),
        ("item-001", "北门铜钥匙", "item", "item-001"),
        ("location-001", "三河镇旧水厂", "location", "location-001"),
        ("location-002", "三河镇", "location", "location-002"),
        ("foreshadow-001", "三角形符号", "foreshadow", "foreshadow-001"),
        ("foreshadow-002", "沈砚亦是目标", "foreshadow", "foreshadow-002"),
        ("foreshadow-003", "报信者身份", "foreshadow", "foreshadow-003"),
    ]
    category_by_entry = {entry["id"]: entry["category"] for entry in entries}
    nodes = [
        {
            "id": node_id,
            "label": label,
            "type": node_type,
            "category": category_by_entry[entry_id],
            "entryId": entry_id,
        }
        for node_id, label, node_type, entry_id in node_specs
    ]

    def graph_edge(source, target, edge_type, label, *, evidence="结构证据", needs_review=False):
        return {
            "source": source,
            "target": target,
            "type": edge_type,
            "label": label,
            "evidence": evidence,
            "sourcePath": chapter_path,
            "needsReview": needs_review,
        }

    edges = [
        graph_edge("project:root", "plot:mainline", "plot", "推进"),
        graph_edge("plot:mainline", "chapter:001", "timeline", "章节"),
        graph_edge("chapter:001", "entity-001", "appearance", "登场"),
        graph_edge("chapter:001", "entity-002", "appearance", "登场"),
        graph_edge("chapter:001", "entity-003", "appearance", "间接登场"),
        graph_edge("chapter:001", "item-001", "introduction", "引入"),
        graph_edge("chapter:001", "location-001", "setting", "场景"),
        graph_edge(
            "entity-001",
            "entity-002",
            "ally",
            "暂时合作",
            evidence="001.md 第7行：'从现在开始，你跟着我'；陆遥点头跟随",
        ),
        graph_edge(
            "entity-001",
            "entity-003",
            "hostile",
            "敌对",
            evidence="001.md 第3行：'我恰好不喜欢他们'",
        ),
        graph_edge(
            "entity-002",
            "entity-003",
            "hostile",
            "被追踪",
            evidence="001.md 第5行：'三天前有人开始追我。他们自称灰塔'",
        ),
        graph_edge("entity-001", "item-001", "possession", "持有"),
        graph_edge("entity-002", "item-001", "discovery", "发现"),
        graph_edge("entity-003", "item-001", "pursuit", "追寻"),
        graph_edge("entity-001", "entity-004", "communication", "收到消息", needs_review=True),
        graph_edge("entity-004", "entity-003", "suspected_link", "可能关联", needs_review=True),
        graph_edge("entity-001", "location-001", "location", "行动于"),
        graph_edge("entity-002", "location-001", "location", "被困于"),
        graph_edge("location-001", "location-002", "spatial", "位于北边"),
        graph_edge("item-001", "foreshadow-001", "foreshadowing", "关联伏笔"),
        graph_edge("entity-001", "foreshadow-002", "foreshadowing", "关联伏笔"),
        graph_edge("entity-004", "foreshadow-003", "foreshadowing", "关联伏笔", needs_review=True),
    ]
    payload = {
        "schemaVersion": 2,
        "categorySchemaVersion": WIKI_CATEGORY_SCHEMA_VERSION,
        "generator": "agent-deep-analysis",
        "generationMode": "agent-evidence-grounded",
        "llmStatus": "agent_reviewed",
        "graphPolicy": {"agentGraphAccepted": True},
        "knowledgeRevision": 3,
        "builtFromRevision": 3,
        "entries": entries,
        "graph": {"nodes": nodes, "edges": edges},
    }
    assert len(nodes) == 13
    assert len(edges) == 21

    service = StoryWikiService()
    monkeypatch.setattr(service, "read_or_build", lambda _root: payload)

    relation_view = service.query_graph(tmp_path, category="relationships")

    assert {node["id"] for node in relation_view["graph"]["nodes"]} == {
        "entity-001", "entity-002", "entity-004",
    }
    assert {entry["id"] for entry in relation_view["entries"]} == {
        "entity-001", "entity-002", "entity-004",
    }
    assert len(relation_view["graph"]["edges"]) == 1
    relation_edge = relation_view["graph"]["edges"][0]
    assert relation_edge["type"] == "relationship"
    assert relation_edge["relationType"] == "alliance"
    assert relation_edge["status"] == "asserted"
    assert relation_edge["needsReview"] is False
    assert "沈砚" in relation_edge["evidence"] and "陆遥" in relation_edge["evidence"]
    assert not {
        "plot", "timeline", "appearance", "introduction", "setting", "possession",
        "discovery", "pursuit", "location", "spatial", "foreshadowing",
    }.intersection(edge["type"] for edge in relation_view["graph"]["edges"])
    assert relation_view["total"] == {
        "entryCount": 3,
        "nodeCount": 3,
        "edgeCount": 1,
        "confirmedEdgeCount": 1,
        "reviewRequiredEdgeCount": 0,
        "connectedNodeCount": 2,
        "isolatedNodeCount": 1,
    }

    payload["graphPolicy"]["agentGraphAccepted"] = False
    unaccepted_view = service.query_graph(tmp_path, category="relationships")
    assert unaccepted_view["graph"]["edges"] == []

    payload["graphPolicy"]["agentGraphAccepted"] = True
    edges[7]["evidence"] = "001.md 第7行：'正文中不存在的合作承诺'"
    ungrounded_view = service.query_graph(tmp_path, category="relationships")
    assert ungrounded_view["graph"]["edges"] == []


def test_rebuild_does_not_guess_character_names_or_create_placeholder_topics(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    chapters.joinpath("001.md").write_text("夜色沉沉，旧站台空无一人。\n", encoding="utf-8")
    chapters.joinpath("002.md").write_text("夜色沉沉，旧站台仍然安静。\n", encoding="utf-8")

    payload = StoryWikiService().rebuild(tmp_path)
    nodes = payload["graph"]["nodes"]
    placeholder_ids = {
        "world:system", "factions:main", "locations:main", "items:main",
        "events:main", "foreshadow:main", "timeline:main", "index:entries",
    }

    assert not any(node.get("type") == "character" for node in nodes)
    assert placeholder_ids.isdisjoint({node.get("id") for node in nodes})


def test_agent_graph_is_ignored_and_graph_refresh_uses_no_provider_call(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    cards.mkdir(parents=True)
    cards.joinpath("Alice.md").write_text(
        "# Alice\n\n> 稳定实体ID: `char:alice`\n\n## 关系网络\n- Bob（char:bob）：朋友\n",
        encoding="utf-8",
    )
    cards.joinpath("Bob.md").write_text(
        "# Bob\n\n> 稳定实体ID: `char:bob`\n",
        encoding="utf-8",
    )
    service = StoryWikiService()

    async def malicious_runner(**_kwargs):
        return {
            "completed": True,
            "reply": json.dumps({
                "summary": "agent summary",
                "entries": [{
                    "id": "character:eve",
                    "title": "Eve",
                    "category": "characters",
                        "sourcePaths": [],
                }],
                    # v3 Agent 契约禁止返回 graph.nodes/edges；正式图谱只
                    # 由本地证据和候选审阅账本投影。
                    "graph": {},
                "entityCandidates": [],
                "relationCandidates": [],
                "review": {},
            }, ensure_ascii=False),
            "events": [],
            "traceId": "agent-malicious",
        }

    generated = asyncio.run(service.run_agent_workflow(
        tmp_path,
        workflow="generate_wiki",
        agent_runner=malicious_runner,
    ))
    graph = generated["wiki"]["graph"]
    assert generated["agentCompleted"] is True
    assert generated["wiki"]["graphPolicy"]["agentGraphAccepted"] is False
    assert "character:eve" not in {node["id"] for node in graph["nodes"]}
    assert len([edge for edge in graph["edges"] if edge.get("type") == "relationship"]) == 1

    refresh_calls = 0

    async def forbidden_refresh_runner(**_kwargs):
        nonlocal refresh_calls
        refresh_calls += 1
        raise AssertionError("refresh must not call the provider")

    refreshed = asyncio.run(service.run_agent_workflow(
        tmp_path,
        workflow="refresh_wiki_graph",
        agent_runner=forbidden_refresh_runner,
    ))
    assert refresh_calls == 0
    assert refreshed["agentAttempted"] is False
    assert refreshed["fallbackUsed"] is False


def test_agent_candidate_projection_is_immediately_current_after_ledger_write(tmp_path):
    worldbook = tmp_path / ".storydex" / "worldbook"
    chapter = tmp_path / "chapters" / "001.md"
    worldbook.mkdir(parents=True)
    chapter.parent.mkdir(parents=True)
    worldbook.joinpath("潮汐兽.md").write_text("# 潮汐兽\n", encoding="utf-8")
    worldbook.joinpath("夜港星.md").write_text("# 夜港星\n", encoding="utf-8")
    quote = "潮汐兽长期栖息在夜港星浅海。"
    chapter.write_text(quote + "\n", encoding="utf-8")

    relation_service = StoryKnowledgeRelationService()
    subject = relation_service.ensure_entity(
        tmp_path,
        "潮汐兽",
        source_path=".storydex/worldbook/潮汐兽.md",
        kind="setting",
    )
    obj = relation_service.ensure_entity(
        tmp_path,
        "夜港星",
        source_path=".storydex/worldbook/夜港星.md",
        kind="setting",
    )
    service = StoryWikiService()
    service.rebuild(tmp_path)

    async def candidate_runner(**_kwargs):
        return {
            "completed": True,
            "reply": json.dumps(
                {
                    "entries": [],
                    "entityCandidates": [],
                    "relationCandidates": [
                        {
                            "subjectId": subject["entityId"],
                            "predicate": "栖息于",
                            "objectId": obj["entityId"],
                            "sourceRefs": [
                                {
                                    "path": "chapters/001.md",
                                    "quote": quote,
                                    "role": "chapter_evidence",
                                }
                            ],
                        }
                    ],
                    "review": {},
                },
                ensure_ascii=False,
            ),
            "events": [],
            "traceId": "agent-candidate",
            "providerId": "test-provider",
            "model": "test-model",
        }

    generated = asyncio.run(
        service.run_agent_workflow(
            tmp_path,
            workflow="generate_wiki",
            agent_runner=candidate_runner,
        )
    )
    assert generated["candidateSubmission"]["acceptedCount"] == 1
    expected_checksum = service._source_set_checksum(service._collect_sources(tmp_path))
    assert generated["wiki"]["sourceSetChecksum"] == expected_checksum

    revision = generated["wiki"]["knowledgeRevision"]
    reloaded = service.read_or_build(tmp_path)
    assert reloaded["knowledgeRevision"] == revision
    assert reloaded["sourceSetChecksum"] == expected_checksum
    graph = service.query_graph(tmp_path, category="setting", include_review=True)
    assert graph["total"]["nodeCount"] == 2
    assert graph["total"]["edgeCount"] == 1
    assert graph["total"]["confirmedEdgeCount"] == 0
    assert graph["total"]["reviewRequiredEdgeCount"] == 1
    assert graph["total"]["isolatedNodeCount"] == 2
    assert any(edge.get("reviewStatus") == "review_required" for edge in graph["graph"]["edges"])


def test_confirmed_fact_with_deleted_quote_is_removed_without_discarding_projection(tmp_path):
    worldbook = tmp_path / ".storydex" / "worldbook"
    chapter = tmp_path / "chapters" / "001.md"
    worldbook.mkdir(parents=True)
    chapter.parent.mkdir(parents=True)
    worldbook.joinpath("潮汐兽.md").write_text("# 潮汐兽\n", encoding="utf-8")
    worldbook.joinpath("夜港星.md").write_text("# 夜港星\n", encoding="utf-8")
    quote = "潮汐兽长期栖息于夜港星。"
    chapter.write_text(quote + "\n", encoding="utf-8")

    relation_service = StoryKnowledgeRelationService()
    subject = relation_service.ensure_entity(
        tmp_path,
        "潮汐兽",
        source_path=".storydex/worldbook/潮汐兽.md",
        kind="setting",
    )
    obj = relation_service.ensure_entity(
        tmp_path,
        "夜港星",
        source_path=".storydex/worldbook/夜港星.md",
        kind="setting",
    )
    fact = relation_service.relation_dto(
        subject=subject,
        predicate="栖息于",
        obj=obj,
        review_status="confirmed",
        knowledge_status="observed",
        source_refs=[{"path": "chapters/001.md", "quote": quote, "role": "chapter"}],
        provenance={"origin": "agent_extraction", "extractorVersion": "test"},
        confidence="confirmed",
    )
    _write_json(
        relation_service.facts_path(tmp_path),
        {"version": 2, "schemaVersion": 2, "facts": [fact]},
    )

    service = StoryWikiService()
    baseline = service.rebuild(tmp_path)
    assert any(edge.get("id") == fact["id"] for edge in baseline["graph"]["edges"])

    chapter.write_text("潮汐兽已经离开夜港星。\n", encoding="utf-8")
    updated = service.sync_local_incremental(tmp_path)

    assert updated["status"] == "ready"
    assert {node["id"] for node in updated["graph"]["nodes"]} >= {
        subject["entityId"],
        obj["entityId"],
    }
    assert not any(edge.get("id") == fact["id"] for edge in updated["graph"]["edges"])
    stale = next(
        item for item in updated["diagnostics"]
        if item.get("code") == "graph.relation.evidence_stale"
    )
    assert stale["severity"] == "warning"
    assert stale["blocking"] is False
    persisted = json.loads(service.wiki_json_path(tmp_path).read_text(encoding="utf-8"))
    assert persisted["status"] == "ready"
    assert persisted["graphChecksum"] == updated["graphChecksum"]


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

    assert payload["categorySchemaVersion"] == WIKI_CATEGORY_SCHEMA_VERSION
    assert not any(node["id"] == "stale:node" for node in payload["graph"]["nodes"])
    assert payload["sourceStats"]["chapterFiles"] == 1


def test_v1_five_category_project_upgrades_without_changing_story_sources(tmp_path):
    chapter = tmp_path / "chapters" / "001.md"
    first_card = tmp_path / ".storydex" / "characters" / "01_沈青.md"
    second_card = tmp_path / ".storydex" / "characters" / "02_林岚.md"
    world = tmp_path / ".storydex" / "worldbook" / "云桥.md"
    chapter.parent.mkdir(parents=True)
    first_card.parent.mkdir(parents=True)
    world.parent.mkdir(parents=True)
    chapter.write_text("沈青与林岚抵达云桥。\n", encoding="utf-8")
    first_card.write_text(
        "# 沈青\n\n> 稳定实体ID: `char:shenqing`\n\n## 关系网络\n"
        "- 林岚（char:linlan）：亲姐姐，两人从小一起生活\n",
        encoding="utf-8",
    )
    second_card.write_text(
        "# 林岚\n\n> 稳定实体ID: `char:linlan`\n",
        encoding="utf-8",
    )
    world.write_text("# 云桥\n\n连接南北城区的石桥。\n", encoding="utf-8")

    wiki_path = tmp_path / ".storydex" / "wiki" / "knowledge_graph.json"
    _write_json(wiki_path, {
        "version": 1,
        "schemaVersion": 2,
        "categorySchemaVersion": "story-wiki-v5-evidence-grounded-graph",
        "entries": [
            {"id": "character:shenqing", "title": "沈青", "category": "characters"},
            {"id": "chapter:001", "title": "第一章", "category": "plot"},
            {"id": "setting:cloud-bridge", "title": "云桥", "category": "setting"},
            {"id": "relationships:shen-lin", "title": "沈青与林岚", "category": "relationships"},
        ],
        "graph": {
            "nodes": [
                {"id": "character:shenqing", "label": "沈青", "type": "character", "category": "characters"}
            ],
            "edges": [],
        },
    })
    legacy_wiki = wiki_path.read_bytes()
    source_paths = [chapter, first_card, second_card, world]
    source_bytes = {path: path.read_bytes() for path in source_paths}

    StoryProjectService().ensure_project_structure(tmp_path)
    service = StoryWikiService()
    payload = service.read_or_build(tmp_path)

    assert payload["status"] == "ready"
    assert payload["categorySchemaVersion"] == WIKI_CATEGORY_SCHEMA_VERSION
    assert {
        str(entry.get("category") or "")
        for entry in payload["entries"]
        if isinstance(entry, dict)
    } <= {"overview", "characters", "plot", "setting"}
    assert all(path.read_bytes() == source_bytes[path] for path in source_paths)

    legacy_backup = (
        tmp_path
        / ".storydex"
        / "wiki"
        / "migration-backups"
        / "story-wiki-v5-evidence-grounded-graph"
        / "knowledge_graph.json"
    )
    assert legacy_backup.read_bytes() == legacy_wiki

    old_route = service.query_graph(tmp_path, category="relationships", limit=60)
    characters = service.query_graph(tmp_path, category="characters", limit=60)
    assert old_route["category"] == "characters"
    assert old_route["graph"] == characters["graph"]
    assert any(
        edge.get("type") == "relationship"
        and {edge.get("source"), edge.get("target")} == {"char:shenqing", "char:linlan"}
        for edge in characters["graph"]["edges"]
    )


def test_legacy_schema_rebuild_failure_keeps_old_projection(tmp_path, monkeypatch):
    chapter = tmp_path / "chapters" / "001.md"
    chapter.parent.mkdir(parents=True)
    chapter.write_text("沈青抵达云桥。\n", encoding="utf-8")
    wiki_path = tmp_path / ".storydex" / "wiki" / "knowledge_graph.json"
    _write_json(wiki_path, {
        "version": 1,
        "schemaVersion": 2,
        "categorySchemaVersion": "story-wiki-v5-evidence-grounded-graph",
        "entries": [
            {"id": "relationships:legacy", "title": "旧关系", "category": "relationships"}
        ],
        "graph": {"nodes": [], "edges": []},
    })
    old_projection = wiki_path.read_bytes()
    service = StoryWikiService()
    monkeypatch.setattr(
        service,
        "validate_graph_invariants",
        lambda *_args, **_kwargs: [
            {
                "code": "graph.revision.mismatch",
                "message": "forced migration failure",
                "path": "builtFromRevision",
                "blocking": True,
            }
        ],
    )

    result = service.read_or_build(tmp_path)

    assert result["status"] == "error"
    assert wiki_path.read_bytes() == old_projection
    backup = (
        tmp_path
        / ".storydex"
        / "wiki"
        / "migration-backups"
        / "story-wiki-v5-evidence-grounded-graph"
        / "knowledge_graph.json"
    )
    assert backup.read_bytes() == old_projection


def test_read_or_build_rebuilds_current_schema_payload_with_invalid_relationship(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    cards.mkdir(parents=True)
    cards.joinpath("林澈.md").write_text(
        "# 林澈\n\n> 稳定实体ID: `char:linche`\n\n调查记者。\n",
        encoding="utf-8",
    )
    cards.joinpath("沈乔.md").write_text(
        "# 沈乔\n\n> 稳定实体ID: `char:shenqiao`\n",
        encoding="utf-8",
    )
    service = StoryWikiService()
    baseline = service.rebuild(tmp_path)
    poisoned = copy.deepcopy(baseline)
    poisoned["graph"]["edges"].append({
        "source": "char:linche",
        "target": "char:shenqiao",
        "label": "共现",
        "type": "relationship",
        "relationType": "unknown",
        "status": "asserted",
        "coOccurrence": True,
    })
    _write_json(service.wiki_json_path(tmp_path), poisoned)

    repaired = service.read_or_build(tmp_path)

    assert repaired["categorySchemaVersion"] == WIKI_CATEGORY_SCHEMA_VERSION
    assert repaired["status"] == "ready"
    assert repaired["graphPolicy"]["coOccurrenceEdgesAccepted"] is False
    assert repaired["graphPolicy"]["syntheticRelationshipMetricsAccepted"] is False
    assert not any(edge.get("coOccurrence") for edge in repaired["graph"]["edges"])
    assert not any(edge.get("relationType") == "unknown" for edge in repaired["graph"]["edges"])

    _write_json(service.wiki_json_path(tmp_path), poisoned)
    synced = service.sync_local_incremental(tmp_path)
    assert synced["status"] == "ready"
    assert not any(edge.get("coOccurrence") for edge in synced["graph"]["edges"])


def test_character_relationship_mentions_do_not_crosswire_primary_cards(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    cards.mkdir(parents=True)
    cards.joinpath("01_林澈.md").write_text(
        "# 林澈\n\n> 稳定实体ID: `char:linche`\n\n记者。\n\n## 关系网络\n"
        "- **苏晚**（char:suwan）：盟友\n- **顾衡**（char:guheng）：协作\n",
        encoding="utf-8",
    )
    cards.joinpath("02_苏晚.md").write_text(
        "# 苏晚\n\n> 稳定实体ID: `char:suwan`\n\n法医。\n\n## 关系网络\n"
        "- **林澈**（char:linche）：盟友\n- **顾衡**（char:guheng）：同事\n",
        encoding="utf-8",
    )
    cards.joinpath("03_顾衡.md").write_text(
        "# 顾衡\n\n> 稳定实体ID: `char:guheng`\n\n警探。\n\n## 关系网络\n"
        "- **林澈**（char:linche）：协作\n- **苏晚**（char:suwan）：同事\n",
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


def test_dynamic_relationship_projection_preserves_audit_fields_and_filters_inactive_records(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    chapter = tmp_path / "chapters" / "001.md"
    cards.mkdir(parents=True)
    chapter.parent.mkdir(parents=True)
    cards.joinpath("Alice.md").write_text(
        "# Alice\n\n> 稳定实体ID: `char:alice`\n",
        encoding="utf-8",
    )
    cards.joinpath("Bob.md").write_text(
        "# Bob\n\n> 稳定实体ID: `char:bob`\n",
        encoding="utf-8",
    )
    cards.joinpath("Carol.md").write_text(
        "# Carol\n\n> 稳定实体ID: `char:carol`\n",
        encoding="utf-8",
    )
    cards.joinpath("Dave.md").write_text(
        "# Dave\n\n> 稳定实体ID: `char:dave`\n",
        encoding="utf-8",
    )
    confirmed_quote = "Alice now trusts Bob."
    rejected_quote = "Alice is hostile toward Carol."
    superseded_quote = "Alice formed an alliance with Dave."
    chapter.write_text(
        f"{confirmed_quote}\n\n{rejected_quote}\n\n{superseded_quote}\n",
        encoding="utf-8",
    )
    _write_json(
        tmp_path / ".storydex" / "memory" / "current" / "relationship_graph.json",
        {
            "version": 1,
            "edges": [
                {
                    # Stable IDs remain authoritative even if legacy display names drift.
                    "source": "Alice Legacy",
                    "target": "Bob Legacy",
                    "sourceId": "char:alice",
                    "targetId": "char:bob",
                    "dimension": "trust",
                    "reviewStatus": "confirmed",
                    "knowledgeStatus": "observed",
                    "confidence": 0.93,
                    "sourceRefs": [
                        {
                            "path": "chapters/001.md",
                            "quote": confirmed_quote,
                            "role": "story_generation",
                        }
                    ],
                    "provenance": {
                        "origin": "story_generation",
                        "extractorVersion": "storydex-story-increment-v1",
                        "providerId": "test-provider",
                        "model": "test-model",
                    },
                    "traceId": "trace-dynamic-confirmed",
                },
                {
                    "source": "Alice",
                    "target": "Carol",
                    "sourceId": "char:alice",
                    "targetId": "char:carol",
                    "dimension": "hostility",
                    "reviewStatus": "rejected",
                    "sourceRefs": [
                        {"path": "chapters/001.md", "quote": rejected_quote, "role": "story_generation"}
                    ],
                },
                {
                    "source": "Alice",
                    "target": "Dave",
                    "sourceId": "char:alice",
                    "targetId": "char:dave",
                    "dimension": "alliance",
                    "reviewStatus": "superseded",
                    "sourceRefs": [
                        {"path": "chapters/001.md", "quote": superseded_quote, "role": "story_generation"}
                    ],
                },
            ],
        },
    )

    service = StoryWikiService()
    payload = service.rebuild(tmp_path)
    relationship_edges = [
        edge
        for edge in payload["graph"]["edges"]
        if edge.get("type") == "relationship"
    ]

    assert len(relationship_edges) == 1
    edge = relationship_edges[0]
    assert edge["source"] == "char:alice"
    assert edge["target"] == "char:bob"
    assert edge["predicate"] == "trust"
    assert edge["reviewStatus"] == "confirmed"
    assert edge["knowledgeStatus"] == "observed"
    assert edge["confidence"] == 0.93
    assert edge["sourceRefs"] == [
        {"path": "chapters/001.md", "quote": confirmed_quote, "role": "story_generation"}
    ]
    assert edge["provenance"]["providerId"] == "test-provider"
    assert edge["provenance"]["model"] == "test-model"
    assert edge["traceId"] == "trace-dynamic-confirmed"
    assert edge["id"].startswith("relationship:")
    assert len(edge["fingerprint"]) == 64

    queried = service.query_graph(tmp_path, category="relationships")
    queried_edge = next(edge for edge in queried["graph"]["edges"] if edge.get("type") == "relationship")
    assert queried_edge["fingerprint"] == edge["fingerprint"]
    assert queried_edge["sourceRefs"] == edge["sourceRefs"]


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
    cards.joinpath("沈乔.md").write_text(
        "# 沈乔\n\n> 稳定实体ID: `char:shenqiao`\n",
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

    assert initial["schemaVersion"] == 3
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


def test_invalid_projection_quarantines_bad_objects_and_keeps_publishing(tmp_path):
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

    missing_relation_type = copy.deepcopy(baseline)
    missing_relation_type["graph"]["edges"].append({
        "source": "char:linche",
        "target": "char:shenqiao",
        "label": "认识",
        "type": "relationship",
        "status": "asserted",
        "evidence": "调查记者。",
        "sourcePath": ".storydex/characters/林澈.md",
    })
    candidates.append((missing_relation_type, "graph.edge.unresolved_relationship"))

    retired_co_occurrence = copy.deepcopy(baseline)
    retired_co_occurrence["graph"]["edges"].append({
        "source": "char:linche",
        "target": "char:shenqiao",
        "label": "共现",
        "type": "appearance",
        "coOccurrence": True,
    })
    candidates.append((retired_co_occurrence, "graph.edge.cooccurrence_removed"))

    synthetic_metric = copy.deepcopy(baseline)
    synthetic_metric["graph"]["edges"].append({
        "source": "char:linche",
        "target": "char:shenqiao",
        "label": "家族",
        "type": "relationship",
        "relationType": "family",
        "status": "asserted",
        "evidence": "调查记者。",
        "sourcePath": ".storydex/characters/林澈.md",
        "level": 3,
    })
    candidates.append((synthetic_metric, "graph.edge.synthetic_relationship_metric"))

    unanchored_relationship = copy.deepcopy(baseline)
    unanchored_relationship["graph"]["edges"].append({
        "source": "char:linche",
        "target": "char:shenqiao",
        "label": "家族",
        "type": "relationship",
        "relationType": "family",
        "status": "asserted",
        "evidence": "调查记者。",
        "sourcePath": ".storydex/characters/林澈.md",
    })
    candidates.append((unanchored_relationship, "graph.edge.unanchored_relationship"))

    unanchored_fact = copy.deepcopy(baseline)
    unanchored_fact["graph"]["edges"].append({
        "source": "char:linche",
        "target": "char:shenqiao",
        "label": "认识",
        "type": "fact",
        "evidence": "调查记者。",
        "sourcePath": ".storydex/characters/林澈.md",
    })
    candidates.append((unanchored_fact, "graph.edge.unanchored_fact"))

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

    baseline_edge_count = len(baseline["graph"]["edges"])
    for candidate, expected_code in candidates:
        diagnostics = service.validate_graph_invariants(candidate, root=tmp_path)
        assert expected_code in {item["code"] for item in diagnostics}

        published = service._persist_payload(
            tmp_path,
            candidate,
            workflow="test_invalid_projection",
            status="completed",
            agent_result=None,
            sources=sources,
            changed_paths=[],
        )
        persisted = json.loads(service.wiki_json_path(tmp_path).read_text(encoding="utf-8"))

        # 坏对象被隔离，其余知识库照常发布：一条坏边不该让整张图消失。
        assert published["status"] == "ready"
        assert persisted["status"] == "ready"
        assert persisted["graphChecksum"] == published["graphChecksum"]
        # 角色始终留在图上，不因为一条坏边/缺证据而消失。
        assert any(node["id"] == "char:linche" for node in published["graph"]["nodes"])
        assert len(published["graph"]["edges"]) <= baseline_edge_count
        assert not any(edge.get("coOccurrence") for edge in published["graph"]["edges"])
        assert not any(
            edge.get("type") == "relationship" and edge.get("relationType") in {None, "", "unknown"}
            for edge in published["graph"]["edges"]
        )
        assert not any(
            "char:missing" in {edge.get("source"), edge.get("target")}
            for edge in published["graph"]["edges"]
        )
        assert not service._has_blocking_diagnostics(published["diagnostics"])


def test_blocking_revision_mismatch_keeps_last_good_projection(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    cards.mkdir(parents=True)
    cards.joinpath("林澈.md").write_text(
        "# 林澈\n\n> 稳定实体ID: `char:linche`\n\n调查记者。\n",
        encoding="utf-8",
    )
    service = StoryWikiService()
    baseline = service.rebuild(tmp_path)
    sources = service._collect_sources(tmp_path)

    broken = copy.deepcopy(baseline)
    original_validate = service.validate_graph_invariants

    def validate_with_revision_mismatch(payload, **kwargs):
        diagnostics = original_validate(payload, **kwargs)
        if payload is not baseline:
            diagnostics.append(service._graph_diagnostic(
                "graph.revision.mismatch",
                "注入的阻断性诊断。",
                "builtFromRevision",
                blocking=True,
            ))
        return diagnostics

    service.validate_graph_invariants = validate_with_revision_mismatch  # type: ignore[assignment]
    try:
        rejected = service._persist_payload(
            tmp_path,
            broken,
            workflow="test_blocking_diagnostic",
            status="completed",
            agent_result=None,
            sources=sources,
            changed_paths=[],
        )
    finally:
        service.validate_graph_invariants = original_validate  # type: ignore[assignment]

    persisted = json.loads(service.wiki_json_path(tmp_path).read_text(encoding="utf-8"))
    assert rejected["lastSuccessfulRevision"] == baseline["knowledgeRevision"]
    assert persisted["graphChecksum"] == baseline["graphChecksum"]
    assert service._read_projection_status(tmp_path)["status"] == "error"


def test_projection_bundle_restores_all_files_when_commit_fails(tmp_path, monkeypatch):
    service = StoryWikiService()
    targets = {
        service.wiki_json_path(tmp_path): None,
        service.wiki_markdown_path(tmp_path): b"old-markdown\n",
        service.wiki_index_path(tmp_path): b"old-index\n",
        service.projection_status_path(tmp_path): b"old-status\n",
    }
    for path, content in targets.items():
        if content is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    original_replace = os.replace
    replace_calls = 0

    def fail_third_replace(source, target):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 3:
            raise OSError("simulated projection commit failure")
        original_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_third_replace)
    with pytest.raises(OSError, match="simulated projection commit failure"):
        service._write_projection_bundle(
            tmp_path,
            payload={"entries": [], "graph": {"nodes": [], "edges": []}},
            index_payload={"version": 2},
            status_payload={"status": "ready"},
        )

    assert {
        path: path.read_bytes() if path.exists() else None
        for path in targets
    } == targets
    assert not list(service.wiki_root(tmp_path).glob(".*.tmp"))
    assert not list(service.wiki_root(tmp_path).glob(".*.restore"))
    assert not list((tmp_path / ".storydex/.agent/temp/wiki-atomic").glob("*"))


def test_invalid_previous_projection_is_never_served_as_is(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    cards.mkdir(parents=True)
    cards.joinpath("林澈.md").write_text(
        "# 林澈\n\n> 稳定实体ID: `char:linche`\n\n调查记者。\n",
        encoding="utf-8",
    )
    service = StoryWikiService()
    baseline = service.rebuild(tmp_path)
    sources = service._collect_sources(tmp_path)

    invalid_previous = copy.deepcopy(baseline)
    invalid_previous["graph"]["edges"].append({
        "source": "project:root",
        "target": "char:linche",
        "label": "共现",
        "type": "appearance",
        "coOccurrence": True,
    })
    service.wiki_json_path(tmp_path).write_text(
        json.dumps(invalid_previous, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    invalid_candidate = copy.deepcopy(baseline)
    invalid_candidate["graph"]["edges"].append({
        "source": "char:linche",
        "target": "char:missing",
        "label": "认识",
        "type": "relationship",
        "relationType": "unknown",
    })
    published = service._persist_payload(
        tmp_path,
        invalid_candidate,
        workflow="test_invalid_projection",
        status="completed",
        agent_result=None,
        sources=sources,
        changed_paths=[],
    )

    assert published["status"] == "ready"
    assert any(node["id"] == "char:linche" for node in published["graph"]["nodes"])
    assert not any(
        "char:missing" in {edge.get("source"), edge.get("target")}
        for edge in published["graph"]["edges"]
    )
    assert not any(edge.get("coOccurrence") for edge in published["graph"]["edges"])

    recovered = service.read_or_build(tmp_path)
    assert recovered["status"] == "ready"
    assert not any(edge.get("coOccurrence") for edge in recovered["graph"]["edges"])
    assert any(node["id"] == "char:linche" for node in recovered["graph"]["nodes"])


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
    _write_json(facts_path, {
        "version": 1,
        "facts": [{
            "subject": "阿离",
            "predicate": "持有",
            "object": "令牌",
            "established_in": "chapters/001.md",
            "evidence": "阿离拿起令牌",
        }],
    })
    service = StoryWikiService()

    initial = service.rebuild(tmp_path)
    assert any(edge["type"] == "fact" and edge["label"] == "持有" for edge in initial["graph"]["edges"])

    chapters.joinpath("001.md").write_text("阿离交还令牌。\n", encoding="utf-8")
    _write_json(facts_path, {
        "version": 1,
        "facts": [{
            "subject": "阿离",
            "predicate": "交还",
            "object": "令牌",
            "established_in": "chapters/001.md",
            "evidence": "阿离交还令牌",
        }],
    })
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


def test_colliding_stable_ids_keep_both_characters_and_flag_the_second(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    cards.mkdir(parents=True)
    cards.joinpath("江言.md").write_text("# 江言\n\n> 稳定实体ID: `char:hero`\n\n少年剑客。\n", encoding="utf-8")
    cards.joinpath("林北.md").write_text("# 林北\n\n> 稳定实体ID: `char:hero`\n\n北境游侠。\n", encoding="utf-8")

    service = StoryWikiService()
    payload = service.rebuild(tmp_path)

    labels = {node["label"] for node in payload["graph"]["nodes"] if node["type"] == "character"}
    assert labels == {"江言", "林北"}

    registry = json.loads(
        (tmp_path / ".storydex" / "memory" / "current" / "entities.json").read_text(encoding="utf-8")
    )
    records = [item for item in registry["entities"] if item.get("kind") == "character"]
    assert len(records) == 2
    assert len({item["entityId"] for item in records}) == 2
    conflicted = [item for item in records if item.get("idConflictWith")]
    assert len(conflicted) == 1
    assert conflicted[0]["idConflictWith"] == "char:hero"
    assert conflicted[0]["needsReview"] is True
    # 兜底 id 由路径确定性派生，冷重建可复现，不是 uuid4。
    assert conflicted[0]["entityId"] == service._path_derived_entity_id(conflicted[0]["sourcePaths"][0])


def test_unrecognizable_card_title_keeps_the_character_and_flags_review(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    cards.mkdir(parents=True)
    # 文件名本身是通用词，名字只能从标题里认：标题一坏就彻底认不出名字。
    card = cards / "人物卡.md"
    card.write_text("# 江言\n\n> 稳定实体ID: `char:jiangyan`\n\n少年剑客。\n", encoding="utf-8")
    service = StoryWikiService()
    service.rebuild(tmp_path)

    card.write_text("# 角色设定\n\n> 稳定实体ID: `char:jiangyan`\n\n少年剑客。\n", encoding="utf-8")
    payload = service.rebuild(tmp_path)

    node = next(node for node in payload["graph"]["nodes"] if node["id"] == "char:jiangyan")
    assert node["label"] == "江言"
    assert node["needsReview"] is True

    registry = json.loads(
        (tmp_path / ".storydex" / "memory" / "current" / "entities.json").read_text(encoding="utf-8")
    )
    record = next(item for item in registry["entities"] if item.get("entityId") == "char:jiangyan")
    assert record["status"] == "active"
    assert record["canonical_name"] == "江言"
    assert record["needsReview"] is True


def test_non_ascii_entity_id_still_publishes_every_character(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    cards.mkdir(parents=True)
    cards.joinpath("江言.md").write_text("# 江言\n\n少年剑客。\n", encoding="utf-8")
    cards.joinpath("林北.md").write_text("# 林北\n\n北境游侠。\n", encoding="utf-8")
    _write_json(
        tmp_path / ".storydex" / "memory" / "current" / "entities.json",
        {
            "version": 2,
            "entities": [
                {
                    "entityId": "林北-1",
                    "canonical_name": "林北",
                    "kind": "character",
                    "status": "active",
                    "sourcePaths": [".storydex/characters/林北.md"],
                },
            ],
        },
    )

    payload = StoryWikiService().rebuild(tmp_path)

    assert payload["status"] == "ready"
    labels = {node["label"] for node in payload["graph"]["nodes"] if node["type"] == "character"}
    assert labels == {"江言", "林北"}
    assert not any(item.get("code") == "graph.character.canonical_count" for item in payload["diagnostics"])


def test_character_state_files_do_not_become_characters(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    cards.joinpath("cards").mkdir(parents=True)
    cards.joinpath("states").mkdir(parents=True)
    _write_json(cards / "cards" / "jiangyan.json", {"name": "江言", "summary": "少年剑客。"})
    _write_json(cards / "states" / "jiangyan.json", {"id": "jiangyan", "mood": "冷静"})

    service = StoryWikiService()
    assert service._source_kind(".storydex/characters/states/jiangyan.json") == "memory"
    assert service._source_kind(".storydex/characters/cards/jiangyan.json") == "character"

    payload = service.rebuild(tmp_path)

    character_labels = [node["label"] for node in payload["graph"]["nodes"] if node["type"] == "character"]
    assert character_labels == ["江言"]


def test_ungrounded_relationship_edge_is_dropped_without_losing_the_graph(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    cards.mkdir(parents=True)
    cards.joinpath("林澈.md").write_text("# 林澈\n\n> 稳定实体ID: `char:linche`\n\n调查记者。\n", encoding="utf-8")
    cards.joinpath("沈乔.md").write_text("# 沈乔\n\n> 稳定实体ID: `char:shenqiao`\n\n法医。\n", encoding="utf-8")
    service = StoryWikiService()
    baseline = service.rebuild(tmp_path)
    sources = service._collect_sources(tmp_path)

    candidate = copy.deepcopy(baseline)
    candidate["graph"]["edges"].append({
        "source": "char:linche",
        "target": "char:shenqiao",
        "label": "家族",
        "type": "relationship",
        "relationType": "family",
        "status": "asserted",
        "evidence": "两人是远房表亲。",
        "sourcePath": ".storydex/characters/林澈.md",
    })

    published = service._persist_payload(
        tmp_path,
        candidate,
        workflow="test_quarantine",
        status="completed",
        agent_result=None,
        sources=sources,
        changed_paths=[],
    )

    assert published["status"] == "ready"
    assert {node["id"] for node in published["graph"]["nodes"]} >= {"char:linche", "char:shenqiao"}
    assert not any(edge.get("label") == "家族" for edge in published["graph"]["edges"])
    quarantine = next(item for item in published["diagnostics"] if item["code"] == "graph.quarantine")
    assert quarantine["severity"] == "warning"
    assert quarantine["blocking"] is False
    assert quarantine["dropped"]["edges"] == 1
    assert any(reason["scope"] == "edge" for reason in quarantine["reasons"])


def test_character_lens_never_contains_chapter_nodes(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    chapters.joinpath("001.md").write_text("林澈在雨夜里翻开旧案卷。\n", encoding="utf-8")
    chapters.joinpath("002.md").write_text("林澈把线索交给沈乔。\n", encoding="utf-8")
    cards = tmp_path / ".storydex" / "characters"
    cards.mkdir(parents=True)
    cards.joinpath("林澈.md").write_text("# 林澈\n\n> 稳定实体ID: `char:linche`\n\n调查记者。\n", encoding="utf-8")
    cards.joinpath("沈乔.md").write_text("# 沈乔\n\n> 稳定实体ID: `char:shenqiao`\n\n法医。\n", encoding="utf-8")

    service = StoryWikiService()
    service.rebuild(tmp_path)
    result = service.query_graph(tmp_path, category="characters", limit=60)

    assert result["category"] == "characters"
    assert {node["type"] for node in result["graph"]["nodes"]} == {"character"}

    plot = service.query_graph(tmp_path, category="plot", limit=60)
    chapter_ids = [node["id"] for node in plot["graph"]["nodes"] if node["type"] == "chapter"]
    assert len(chapter_ids) == 2
    # 剧情图里章节按叙事顺序链式相接，不再挂在人造 hub 上。
    assert not any(node["id"] == "plot:mainline" for node in plot["graph"]["nodes"])
    assert any(
        edge["source"] == chapter_ids[0] and edge["target"] == chapter_ids[1] and edge["label"] == "承接"
        for edge in plot["graph"]["edges"]
    )


def test_relationships_category_alias_matches_characters_lens(tmp_path):
    cards = tmp_path / ".storydex" / "characters"
    cards.mkdir(parents=True)
    cards.joinpath("林澈.md").write_text("# 林澈\n\n> 稳定实体ID: `char:linche`\n\n调查记者。\n", encoding="utf-8")
    cards.joinpath("沈乔.md").write_text("# 沈乔\n\n> 稳定实体ID: `char:shenqiao`\n\n法医。\n", encoding="utf-8")

    service = StoryWikiService()
    service.rebuild(tmp_path)

    aliased = service.query_graph(tmp_path, category="relationships", limit=60)
    direct = service.query_graph(tmp_path, category="characters", limit=60)

    assert aliased["category"] == "characters"
    assert aliased["graph"] == direct["graph"]
    assert aliased["matchedEntryIds"] == direct["matchedEntryIds"]
