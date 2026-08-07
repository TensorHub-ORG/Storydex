from __future__ import annotations

import json
import os
import re
import shutil
from hashlib import sha256
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence
from uuid import uuid4

from services.entity_registry import EntityRecord, EntityRegistry
from services.story_relationship_semantics import (
    parse_relationship_markdown,
    semantics_for_dimension,
)


TEXT_SUFFIXES = {".md", ".txt"}
DATA_SUFFIXES = {".json", ".jsonl"}
SCAN_SUFFIXES = TEXT_SUFFIXES | DATA_SUFFIXES
EXCLUDED_PARTS = {".git", "__pycache__", ".cache", "traces", "sessions"}
EXCLUDED_RELATIVE_PREFIXES = (
    ".storydex/wiki/",
    ".storydex/.agent/",
    ".storydex/templates/",
    ".storydex/presets/",
    ".storydex/config/",
    ".storydex/temp/",
)
ENTITY_SOURCE_PATH = ".storydex/memory/current/entities.json"
FACT_SOURCE_PATH = ".storydex/memory/current/facts.json"

WIKI_CATEGORY_SCHEMA_VERSION = "story-wiki-v7-auditable-relations"
PROJECTION_SCHEMA_VERSION = 3
EVIDENCE_GROUNDED_GRAPH_POLICY = {
    "mode": "evidence_grounded_local_v1",
    "agentGraphAccepted": False,
    "agentCandidatesAccepted": True,
    "explicitMarkdownRelationsAccepted": True,
    "coOccurrenceIsRelationship": False,
    "coOccurrenceEdgesAccepted": False,
    "unknownRelationTypesAccepted": False,
    "syntheticRelationshipMetricsAccepted": False,
}
KNOWLEDGE_STATUSES = {"planned", "observed", "inferred"}
INTERNAL_LABEL_PREFIXES = ("entity:", "character:", "char:", "event:", "mention:", "rel:")
PROJECTION_STATUSES = {"ready", "stale", "rebuilding", "error"}
ALLOWED_RELATION_TYPES = {
    "professional_collaboration",
    "family",
    "trust",
    "intimacy",
    "hostility",
    "loyalty",
    "alliance",
    "rivalry",
}
AGENT_RELATIONSHIP_EDGE_DIMENSIONS = {
    "ally": "alliance",
    "alliance": "alliance",
    "hostile": "hostility",
    "hostility": "hostility",
    "rivalry": "rivalry",
    "trust": "trust",
    "intimacy": "intimacy",
    "loyalty": "loyalty",
    "family": "family",
    "professional": "professional",
    "professional_collaboration": "professional",
}
GRAPH_CHECKSUM_VOLATILE_KEYS = {
    "generatedAt",
    "lastUpdatedAt",
    "updatedAt",
    "mtime",
    "lastAnalyzedAt",
    "x",
    "y",
    "fx",
    "fy",
    "vx",
    "vy",
    "layout",
}
# 图谱边总量上限：超过后按权重淘汰（原 300 条硬截断会静默丢弃长篇项目的新增边）。
MAX_WIKI_GRAPH_EDGES = 1200
# 可见分类收敛为三类：角色 / 剧情 / 设定。关系不再是独立分类，而是角色图里的连线。
WIKI_VISIBLE_CATEGORIES = ("characters", "plot", "setting")
# overview 只用于项目总览条目与 project:root 节点，不作为可见 tab。
ALLOWED_WIKI_CATEGORIES = {"overview", *WIKI_VISIBLE_CATEGORIES}
CATEGORY_ALIASES: Dict[str, str] = {
    "chapters": "plot",
    "events": "plot",
    "timeline": "plot",
    "world": "setting",
    "locations": "setting",
    "items": "setting",
    "factions": "setting",
    "foreshadow": "setting",
    "characters": "characters",
    # 旧数据与旧 URL：关系视图并入角色视图。
    "relationships": "characters",
    "overview": "overview",
    "index": "overview",
}

CATEGORY_LABELS: Dict[str, str] = {
    "overview": "项目总览",
    "characters": "角色",
    "plot": "剧情",
    "setting": "设定",
}

# 各视图允许带出的一跳跨类邻居节点类型（前端弱化渲染）。
# 角色图不带任何跨类邻居——章节/情节混进角色图正是用户报告的问题之一。
CATEGORY_NEIGHBOR_NODE_TYPES: Dict[str, frozenset[str]] = {
    "characters": frozenset(),
    "plot": frozenset({"character"}),
    "setting": frozenset({"character", "chapter"}),
}

# 发布闸门：只有真正的结构性错误才阻断发布；其余诊断只摘掉对应对象并降级为 warning，
# 一条坏边不该让整个知识库消失。
BLOCKING_GRAPH_DIAGNOSTIC_CODES = frozenset({"graph.revision.mismatch"})
# 无法通过摘除对象修复、只用于提示的诊断：既不隔离，也不触发全量重建。
REPORT_ONLY_GRAPH_DIAGNOSTIC_CODES = frozenset({"graph.character.canonical_count"})
# 条目/节点里只有「根本没法寻址」的才摘除；证据缺失、显示名不规范这类问题保留内容
# 并标 needsReview——否则一条缺证据的记录又会让角色从图上消失。边一律摘除。
UNADDRESSABLE_GRAPH_DIAGNOSTIC_CODES = frozenset({
    "graph.entry.missing_id",
    "graph.entry.duplicate_id",
    "graph.node.missing_id",
    "graph.node.duplicate_id",
    "graph.node.missing_entry",
})

NODE_TYPE_LABELS: Dict[str, str] = {
    "project": "\u9879\u76ee",
    "chapter": "\u7ae0\u8282",
    "character": "\u89d2\u8272",
    "world": "\u4e16\u754c\u89c2",
    "faction": "\u52bf\u529b",
    "location": "\u5730\u70b9",
    "item": "\u7269\u54c1/\u529f\u6cd5",
    "event": "\u4e8b\u4ef6",
    "foreshadow": "\u4f0f\u7b14",
    "timeline": "\u65f6\u95f4",
    "setting": "\u8bbe\u5b9a",
}

# \u5199\u4f5c\u6f14\u8fdb\u7ba1\u7ebf\uff08relationship_graph.json\uff09\u7684\u5173\u7cfb\u7ef4\u5ea6 -> \u4e2d\u6587\u8fb9\u6807\u7b7e\u3002
RELATIONSHIP_DIMENSION_LABELS: Dict[str, str] = {
    "trust": "\u4fe1\u4efb",
    "intimacy": "\u4eb2\u5bc6",
    "hostility": "\u654c\u5bf9",
    "loyalty": "\u5fe0\u8bda",
    "alliance": "\u540c\u76df",
    "rivalry": "\u7ade\u4e89",
    "family": "\u5bb6\u65cf",
    "professional": "\u804c\u4e1a",
}

ENTITY_KIND_NODE_TYPES: Dict[str, str] = {
    "character": "character",
    "person": "character",
    "role": "character",
    "location": "location",
    "place": "location",
    "scene": "location",
    "faction": "faction",
    "organization": "faction",
    "sect": "faction",
    "item": "item",
    "artifact": "item",
    "object": "item",
    "world": "world",
    "worldbook": "world",
    "setting": "setting",
    "event": "event",
    "plot": "event",
    "foreshadow": "foreshadow",
    "thread": "foreshadow",
}

# 角色名识别黑名单：这些高频中文 token 是角色模板的章节标题/字段名/通用文档词，
# 不应被 _collect_character_names 的高频 token 提取误判为角色名。
CHARACTER_TOKEN_BLACKLIST: frozenset[str] = frozenset({
    # 角色模板章节标题
    "定位", "动机", "秘密", "边界", "叙事功能", "基本信息", "性格与行为模式",
    "关系网络", "外貌", "身份", "住处", "年龄", "补充设定", "行为模式",
    "性格", "关系", "网络", "基本信息", "出场安排",
    # 通用文档/项目词
    "小说", "项目", "规则", "章节", "正文", "目录", "模板", "默认",
    "默认角色模板", "角色模板", "项目规则", "项目命名", "命名约定",
    "剧情变量", "变量更新", "更新规范", "故事气质", "正文章节",
    "第一章", "第二章", "第三章", "第四章", "第五章", "第六章",
    "未命名", "README", "概览", "索引",
})

WIKI_WORKFLOW_DEFINITIONS: Dict[str, Dict[str, str]] = {
    "generate_wiki": {
        "label": "\u9996\u6b21\u5168\u91cf\u751f\u6210 WIKI",
        "description": "\u7531 Agent \u5168\u91cf\u8bfb\u53d6\u9879\u76ee\u5185\u5bb9\uff0c\u91cd\u65b0\u6784\u5efa WIKI \u6761\u76ee\u548c\u77e5\u8bc6\u56fe\u8c31\u3002",
    },
    "update_wiki": {
        "label": "\u57fa\u4e8e\u53d8\u66f4\u589e\u91cf\u66f4\u65b0 WIKI",
        "description": "\u4f9d\u636e index.json \u4e2d\u7684\u6e90\u6587\u4ef6 hash \u53ea\u5206\u6790\u53d8\u66f4\u5185\u5bb9\uff0c\u5e76\u5408\u5e76\u5230\u65e2\u6709 WIKI\u3002",
    },
    "refresh_wiki_graph": {
        "label": "\u91cd\u65b0\u6821\u9a8c\u56fe\u7ed3\u6784",
        "description": "\u4fdd\u7559 WIKI \u6761\u76ee\u6587\u672c\uff0c\u4ece\u6743\u5a01\u5b9e\u4f53\u3001\u89d2\u8272\u6863\u6848\u548c\u53ef\u6838\u5bf9\u8bc1\u636e\u4e2d\u786e\u5b9a\u6027\u91cd\u5efa\u8282\u70b9/\u8fb9\u5173\u7cfb\u3002",
    },
    "review_wiki": {
        "label": "\u5ba1\u9605 WIKI",
        "description": "\u68c0\u67e5 WIKI \u9057\u6f0f\u3001\u51b2\u7a81\u3001\u8fc7\u65f6\u548c\u9700\u8981\u4eba\u5de5\u786e\u8ba4\u7684\u6761\u76ee\u3002",
    },
    "repair_wiki": {
        "label": "\u4fee\u590d WIKI",
        "description": "\u5bf9\u635f\u574f JSON\u3001\u7f3a\u5c11\u5b57\u6bb5\u6216\u4e0d\u5b8c\u6574\u56fe\u8c31\u8fdb\u884c schema normalization \u548c\u4fee\u590d\u3002",
    },
}
WIKI_WORKFLOWS = set(WIKI_WORKFLOW_DEFINITIONS)

AgentWikiRunner = Callable[..., Awaitable[Dict[str, Any]]]


class StoryWikiService:
    """Builds a deterministic project WIKI when no LLM wiki artifact exists."""

    def read_or_build(self, workspace_root: Path, *, force: bool = False) -> Dict[str, Any]:
        root = workspace_root.resolve()
        self._migrate_knowledge_relation_storage(root)
        self._reconcile_entity_registry(root)
        wiki_path = self.wiki_json_path(root)
        if not force and wiki_path.exists():
            try:
                data = json.loads(wiki_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("entries") and data.get("graph"):
                    if not self._has_current_category_schema(data):
                        return self.rebuild(root)
                    sources = self._collect_sources(root)
                    if str(data.get("sourceSetChecksum") or "") != self._source_set_checksum(sources):
                        return self.sync_local_incremental(root)
                    diagnostics = self.validate_graph_invariants(
                        data,
                        root=root,
                        source_documents=sources,
                    )
                    if self._has_blocking_diagnostics(diagnostics):
                        return self.rebuild(root, sources=sources)
                    if diagnostics:
                        # 只读路径：坏对象在内存里摘掉后照常返回，不改盘上的 last-good。
                        data, _ = self._quarantine_graph_objects(data, diagnostics)
                    return self._with_projection_status(root, data)
            except Exception:
                pass
        return self.rebuild(root)

    def rebuild(
        self,
        workspace_root: Path,
        *,
        workflow: str = "generate_wiki",
        changed_paths: Sequence[str] | None = None,
        sources: Sequence[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        root = workspace_root.resolve()
        self._backup_legacy_projection(root)
        self._migrate_knowledge_relation_storage(root)
        if sources is None:
            # reconcile 可能改写 entities.json，而它本身也是被索引的来源，
            # 所以必须先归并再扫描。调用方复用扫描结果时同样要保证这个顺序。
            self._reconcile_entity_registry(root)
            sources = self._collect_sources(root)
        else:
            sources = list(sources)
        persisted_changed_paths = (
            list(changed_paths)
            if changed_paths is not None
            else [str(item["relativePath"]) for item in sources]
        )
        registry = EntityRegistry(root)
        entities = self._collect_entities(root, sources)
        character_entities = [entity for entity in entities if entity["type"] == "character"]
        character_names = [str(entity["name"]) for entity in character_entities]
        chapter_sources = [item for item in sources if item["kind"] == "chapter"]
        planned_sources = [item for item in sources if item["kind"] == "planned"]
        now = datetime.now(timezone.utc).isoformat()

        entries: List[Dict[str, Any]] = []
        graph_nodes: List[Dict[str, Any]] = []
        graph_edges: List[Dict[str, Any]] = []

        project_id = "project:root"
        graph_nodes.append({
            "id": project_id,
            "label": root.name or "\u672a\u547d\u540d\u9879\u76ee",
            "type": "project",
            "category": "overview",
            "entryId": "overview:project",
            "summary": f"{root.name} \u9879\u76ee\u77e5\u8bc6\u5e93",
        })

        if not chapter_sources and not planned_sources and not entities:
            overview_summary = f"《{root.name}》暂无故事内容，创建章节/角色后图谱将自动构建。"
            entries.append(self._entry(
                "overview:project",
                "\u9879\u76ee\u603b\u89c8",
                "overview",
                overview_summary,
                [
                    f"\u5de5\u4f5c\u533a: {root.as_posix()}",
                    "\u6682\u65e0\u6545\u4e8b\u5185\u5bb9\uff0c\u521b\u5efa\u7ae0\u8282/\u89d2\u8272\u540e\u56fe\u8c31\u5c06\u81ea\u52a8\u6784\u5efa\u3002",
                ],
                [],
                confidence=0.9,
            ))
            payload = {
                "version": 1,
                "categorySchemaVersion": WIKI_CATEGORY_SCHEMA_VERSION,
                "projectName": root.name,
                "workspaceRoot": root.as_posix(),
                "generatedAt": now,
                "generator": "local-evidence-grounded-wiki",
                "generationMode": "local evidence-grounded",
                "llmStatus": "not_required",
                "categoryLabels": CATEGORY_LABELS,
                "nodeTypeLabels": NODE_TYPE_LABELS,
                "workflowDefinitions": WIKI_WORKFLOW_DEFINITIONS,
                "summary": overview_summary,
                "entries": entries,
                "graph": {
                    "nodes": graph_nodes,
                    "edges": graph_edges,
                },
                "sourceStats": {
                    "scannedFiles": len(sources),
                    "chapterFiles": 0,
                    "characters": 0,
                },
            }
            return self._persist_payload(
                root,
                payload,
                workflow=workflow,
                status="completed",
                agent_result=None,
                sources=sources,
                changed_paths=persisted_changed_paths,
            )

        overview_summary = self._overview_summary(root, sources, chapter_sources, character_names)
        entries.append(self._entry(
            "overview:project",
            "\u9879\u76ee\u603b\u89c8",
            "overview",
            overview_summary,
            [
                f"\u5de5\u4f5c\u533a: {root.as_posix()}",
                f"\u7ae0\u8282/\u6b63\u6587\u6587\u4ef6: {len(chapter_sources)}",
                f"\u8bc6\u522b\u89d2\u8272: {len(character_names)}",
                f"\u7d22\u5f15\u6765\u6e90\u6587\u4ef6: {len(sources)}",
                "\u672c WIKI \u7531\u672c\u5730\u964d\u7ea7\u751f\u6210\u5668\u6784\u5efa\uff1b\u5f53 LLM \u914d\u7f6e\u4e0d\u53ef\u7528\u65f6\u4ecd\u53ef\u4fdd\u6301\u53ef\u8bfb\u53ef\u5c55\u793a\u3002",
            ],
            [item["relativePath"] for item in sources[:12]],
        ))

        if chapter_sources:
            plot_details = self._chapter_plot_details(chapter_sources)
            # 只保留主线剧情的文本条目：人造 hub 节点会把剧情图拉成星形，
            # 章节之间真正的叙事顺序反而看不出来。
            entries.append(self._entry(
                "plot:mainline",
                "\u4e3b\u7ebf\u5267\u60c5",
                "plot",
                self._build_plot_summary(chapter_sources),
                plot_details,
                [item["relativePath"] for item in chapter_sources[:12]],
                knowledge_status="observed",
            ))

        chapter_mentions = self._chapter_mentions_by_path(registry, chapter_sources, character_names)

        previous_chapter_node_id = ""
        for index, source in enumerate(chapter_sources):
            entry_id = self._chapter_entry_id(source["relativePath"])
            chapter_title = self._display_title(source["relativePath"], source["title"])
            summary = self._compress_text(source["text"], 260) or "\u7ae0\u8282\u5185\u5bb9\u6682\u672a\u586b\u5145\u3002"
            entries.append(self._entry(
                entry_id,
                chapter_title,
                "plot",
                summary,
                self._chapter_details(source, chapter_mentions.get(source["relativePath"], ())),
                [source["relativePath"]],
                knowledge_status="observed",
            ))
            node_id = entry_id
            graph_nodes.append({
                "id": node_id,
                "label": chapter_title,
                "type": "chapter",
                "category": "plot",
                "entryId": entry_id,
                "summary": summary,
                "knowledgeStatus": "observed",
                "narrativeOrder": index + 1,
            })
            if previous_chapter_node_id:
                # 章节按叙事顺序链式相连，读者视线沿着故事走。
                graph_edges.append(self._edge(
                    previous_chapter_node_id,
                    node_id,
                    "承接",
                    "timeline",
                    weight=max(1, index + 1),
                ))
            previous_chapter_node_id = node_id

        for index, source in enumerate(planned_sources, start=1):
            entry_id = self._planned_entry_id(str(source["relativePath"]))
            title = str(source.get("title") or "规划剧情")
            summary = self._compress_text(str(source.get("text") or ""), 260) or "规划剧情尚未填写。"
            entries.append(self._entry(
                entry_id,
                title,
                "plot",
                summary,
                [f"路径: {source['relativePath']}", f"规划顺序: {index}"],
                [str(source["relativePath"])],
                knowledge_status="planned",
            ))
            graph_nodes.append({
                "id": entry_id,
                "label": title,
                "type": "event",
                "category": "plot",
                "entryId": entry_id,
                "summary": summary,
                "knowledgeStatus": "planned",
                "narrativeOrder": index,
            })
            graph_edges.append(self._edge(project_id, entry_id, "规划", "planned", weight=index))

        character_sources = self._character_sources(root, sources, character_entities)
        mention_sources_by_character: Dict[str, List[Dict[str, Any]]] = {name: [] for name in character_names}
        for source in chapter_sources:
            for name in chapter_mentions.get(source["relativePath"], ()):
                mention_sources_by_character.setdefault(name, []).append(source)

        for entity in character_entities:
            name = str(entity["name"])
            related = character_sources.get(name, [])
            mentions = mention_sources_by_character.get(name, [])
            entry_id = self._entity_node_id(entity)
            node_id = entry_id
            summary = self._character_summary(name, related, mentions)
            entries.append(self._entry(
                entry_id,
                name,
                "characters",
                summary,
                self._character_details(name, related, mentions),
                [
                    *[str(path) for path in entity.get("sourcePaths", [])],
                    *[item["relativePath"] for item in (related + mentions)[:10]],
                ],
                confidence=0.82 if entity.get("needsReview") else 0.9,
                needs_review=bool(entity.get("needsReview")),
                aliases=[str(alias) for alias in entity.get("aliases", [])],
                primary_source_path=(str(related[0].get("relativePath") or "") if related else ""),
            ))
            graph_nodes.append({
                "id": node_id,
                "label": name,
                "type": "character",
                "category": "characters",
                "entryId": entry_id,
                "summary": summary,
                "needsReview": bool(entity.get("needsReview")),
            })
            graph_edges.append(self._edge(project_id, node_id, "\u89d2\u8272", "character"))
            for source in mentions[:6]:
                if source in chapter_sources:
                    graph_edges.append(self._edge(node_id, self._chapter_entry_id(source["relativePath"]), "\u51fa\u573a", "appearance"))

        for entity in [item for item in entities if item["type"] != "character"]:
            entry_id = self._entity_node_id(entity)
            summary = self._entity_summary(entity)
            entries.append(self._entry(
                entry_id,
                str(entity["name"]),
                str(entity["category"]),
                summary,
                self._entity_details(entity),
                [str(path) for path in entity.get("sourcePaths", [])],
                confidence=0.86 if entity.get("needsReview") else 0.92,
                needs_review=bool(entity.get("needsReview")),
            ))
            graph_nodes.append({
                "id": entry_id,
                "label": str(entity["name"]),
                "type": str(entity["type"]),
                "category": str(entity["category"]),
                "entryId": entry_id,
                "summary": summary,
                "needsReview": bool(entity.get("needsReview")),
            })
            graph_edges.append(self._edge(project_id, entry_id, CATEGORY_LABELS.get(str(entity["category"]), "\u5173\u8054"), str(entity["type"])))

        # 同章出现只证明角色出场，不证明角色之间存在关系。
        # 角色-角色边只能来自显式角色档案或有正文证据的关系快照。
        graph_edges = self._merge_relationship_snapshot_edges(
            root,
            nodes=graph_nodes,
            existing_edges=graph_edges,
            allow_new_nodes=False,
            sources=sources,
        )
        self._append_fact_edges(
            root,
            graph_nodes,
            graph_edges,
            registry=registry,
            entities=entities,
            sources=sources,
        )
        # 统一关系领域层是正式关系和候选关系的唯一适配入口。旧 facts/relationship
        # snapshot 仍由上面的兼容路径读取；v2 关系、显式 Markdown 关联块和审阅
        # 账本在这里补充可审计字段，避免模型返回的任意 graph 直接进入投影。
        relation_projection_diagnostics = self._append_knowledge_relation_edges(
            root,
            graph_nodes,
            graph_edges,
            include_review=True,
        )

        payload = {
            "version": 1,
            "categorySchemaVersion": WIKI_CATEGORY_SCHEMA_VERSION,
            "projectName": root.name,
            "workspaceRoot": root.as_posix(),
            "generatedAt": now,
            "generator": "local-evidence-grounded-wiki",
            "generationMode": "local evidence-grounded",
            "llmStatus": "not_required",
            "categoryLabels": CATEGORY_LABELS,
            "nodeTypeLabels": NODE_TYPE_LABELS,
            "workflowDefinitions": WIKI_WORKFLOW_DEFINITIONS,
            "summary": overview_summary,
            "entries": entries,
            "graph": {
                "nodes": self._dedupe_nodes(graph_nodes),
                "edges": self._dedupe_edges(graph_edges),
            },
            "sourceStats": {
                "scannedFiles": len(sources),
                "chapterFiles": len(chapter_sources),
                "characters": len(character_names),
            },
            "_inputDiagnostics": relation_projection_diagnostics,
        }

        return self._persist_payload(
            root,
            payload,
            workflow=workflow,
            status="completed",
            agent_result=None,
            sources=sources,
            changed_paths=persisted_changed_paths,
        )

    def sync_local_incremental(self, workspace_root: Path) -> Dict[str, Any]:
        """保存/写作后自动同步：纯本地确定性增量合并，不触发 Agent、免 token、毫秒级。

        Agent 深度生成/更新仍由手动按钮走 run_agent_workflow，这里只保证图谱跟上文件变更。
        """
        root = workspace_root.resolve()
        self._migrate_knowledge_relation_storage(root)
        self._reconcile_entity_registry(root)
        before = self._read_existing_payload(root)
        sources = self._collect_sources(root)
        # 下面几条全量分支都复用本次已经扫好的 sources：reconcile 在扫描之前
        # 已经跑过，重扫只会得到同一份结果。
        if before is None:
            # 尚无图谱，首次直接全量构建。
            return self.rebuild(root, sources=sources)
        if not self._has_current_category_schema(before):
            # 旧 schema（如按排序位置命名的章节 ID）需要全量重建迁移。
            return self.rebuild(root, sources=sources)
        diagnostics = self.validate_graph_invariants(before, root=root, source_documents=sources)
        if self._has_blocking_diagnostics(diagnostics):
            return self.rebuild(root, sources=sources)
        if diagnostics:
            # 非阻断问题不该让每次保存都全量重扫重建：坏对象在内存里摘掉即可。
            before, _ = self._quarantine_graph_objects(before, diagnostics)

        previous_index = self.read_index(root)
        changed_paths = self.changed_source_paths(root, sources=sources, previous_index=previous_index)
        current_rel = {str(item.get("relativePath") or "") for item in sources}
        previous_sources = previous_index.get("sources") if isinstance(previous_index, dict) else {}
        previous_by_path = previous_sources if isinstance(previous_sources, dict) else {}
        removed_paths = sorted(
            (rel for rel in previous_by_path if rel not in current_rel),
            key=self._source_sort_key,
        )

        if not changed_paths and not removed_paths:
            # 无任何变更：快速 no-op，避免高频保存反复写盘。
            current = self._with_projection_status(root, before)
            current["changedSourcePaths"] = []
            return current

        # 本地编译器不调用模型。ChangeSet 命中后完整重算派生投影，避免任何条目
        # 或关系残留旧内容；发布仍只递增一次 revision，并保留精确 changedSourcePaths。
        return self.rebuild(
            root,
            workflow="sync_local",
            changed_paths=sorted(
                set(changed_paths) | set(removed_paths),
                key=self._source_sort_key,
            ),
            sources=sources,
        )

    def _build_incremental_payload(
        self,
        root: Path,
        sources: Sequence[Dict[str, Any]],
        changed_paths: Sequence[str],
    ) -> Dict[str, Any]:
        """仅为受变更影响的章节/角色局部重建条目、节点与边；id 用全量排序位置保持稳定。"""
        changed_set = {str(path) for path in changed_paths}
        registry = EntityRegistry(root)
        entities = self._collect_entities(root, sources)
        character_entities = [entity for entity in entities if entity["type"] == "character"]
        character_names = [str(entity["name"]) for entity in character_entities]
        chapter_sources = [item for item in sources if item["kind"] == "chapter"]
        planned_sources = [item for item in sources if item["kind"] == "planned"]
        chapter_mentions = self._chapter_mentions_by_path(registry, chapter_sources, character_names)
        entries: List[Dict[str, Any]] = []
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []

        for index, source in enumerate(chapter_sources):
            if source["relativePath"] not in changed_set:
                continue
            entry_id = self._chapter_entry_id(source["relativePath"])
            chapter_title = self._display_title(source["relativePath"], source["title"])
            summary = self._compress_text(source["text"], 260) or "章节内容暂未填充。"
            entries.append(self._entry(
                entry_id,
                chapter_title,
                "plot",
                summary,
                self._chapter_details(source, chapter_mentions.get(source["relativePath"], ())),
                [source["relativePath"]],
                knowledge_status="observed",
            ))
            nodes.append({
                "id": entry_id,
                "label": chapter_title,
                "type": "chapter",
                "category": "plot",
                "entryId": entry_id,
                "summary": summary,
                "knowledgeStatus": "observed",
                "narrativeOrder": index + 1,
            })
            # 章节链：与上一章相接，变更章节两侧的接续边都要重建。
            if index > 0:
                edges.append(self._edge(
                    self._chapter_entry_id(chapter_sources[index - 1]["relativePath"]),
                    entry_id,
                    "承接",
                    "timeline",
                    weight=max(1, index + 1),
                ))
            if index + 1 < len(chapter_sources):
                edges.append(self._edge(
                    entry_id,
                    self._chapter_entry_id(chapter_sources[index + 1]["relativePath"]),
                    "承接",
                    "timeline",
                    weight=max(1, index + 2),
                ))

        for index, source in enumerate(planned_sources, start=1):
            if source["relativePath"] not in changed_set:
                continue
            entry_id = self._planned_entry_id(str(source["relativePath"]))
            title = str(source.get("title") or "规划剧情")
            summary = self._compress_text(str(source.get("text") or ""), 260) or "规划剧情尚未填写。"
            entries.append(self._entry(
                entry_id,
                title,
                "plot",
                summary,
                [f"路径: {source['relativePath']}", f"规划顺序: {index}"],
                [str(source["relativePath"])],
                knowledge_status="planned",
            ))
            nodes.append({
                "id": entry_id,
                "label": title,
                "type": "event",
                "category": "plot",
                "entryId": entry_id,
                "summary": summary,
                "knowledgeStatus": "planned",
                "narrativeOrder": index,
            })
            edges.append(self._edge("project:root", entry_id, "规划", "planned", weight=index))

        entity_changed = ENTITY_SOURCE_PATH in changed_set
        character_sources = self._character_sources(root, sources, character_entities)
        mention_sources_by_character: Dict[str, List[Dict[str, Any]]] = {name: [] for name in character_names}
        for source in chapter_sources:
            for name in chapter_mentions.get(source["relativePath"], ()):
                mention_sources_by_character.setdefault(name, []).append(source)

        for entity in character_entities:
            name = str(entity["name"])
            related = character_sources.get(name, [])
            mentions = mention_sources_by_character.get(name, [])
            related_changed = any(item["relativePath"] in changed_set for item in related)
            mention_changed = any(item["relativePath"] in changed_set for item in mentions)
            if not (entity_changed or related_changed or mention_changed):
                continue
            entry_id = self._entity_node_id(entity)
            summary = self._character_summary(name, related, mentions)
            entries.append(self._entry(
                entry_id,
                name,
                "characters",
                summary,
                self._character_details(name, related, mentions),
                [
                    *[str(path) for path in entity.get("sourcePaths", [])],
                    *[item["relativePath"] for item in (related + mentions)[:10]],
                ],
                confidence=0.82 if entity.get("needsReview") else 0.9,
                needs_review=bool(entity.get("needsReview")),
                aliases=[str(alias) for alias in entity.get("aliases", [])],
                primary_source_path=(str(related[0].get("relativePath") or "") if related else ""),
            ))
            nodes.append({
                "id": entry_id,
                "label": name,
                "type": "character",
                "category": "characters",
                "entryId": entry_id,
                "summary": summary,
                "needsReview": bool(entity.get("needsReview")),
            })
            edges.append(self._edge("project:root", entry_id, "角色", "character"))
            for source in mentions[:6]:
                if source in chapter_sources:
                    edges.append(self._edge(entry_id, self._chapter_entry_id(source["relativePath"]), "出场", "appearance"))

        if entity_changed:
            for entity in [item for item in entities if item["type"] != "character"]:
                entry_id = self._entity_node_id(entity)
                summary = self._entity_summary(entity)
                entries.append(self._entry(
                    entry_id,
                    str(entity["name"]),
                    str(entity["category"]),
                    summary,
                    self._entity_details(entity),
                    [str(path) for path in entity.get("sourcePaths", [])],
                    confidence=0.86 if entity.get("needsReview") else 0.92,
                    needs_review=bool(entity.get("needsReview")),
                ))
                nodes.append({
                    "id": entry_id,
                    "label": str(entity["name"]),
                    "type": str(entity["type"]),
                    "category": str(entity["category"]),
                    "entryId": entry_id,
                    "summary": summary,
                    "needsReview": bool(entity.get("needsReview")),
                })
                edges.append(self._edge("project:root", entry_id, CATEGORY_LABELS.get(str(entity["category"]), "关联"), str(entity["type"])))

        self._append_fact_edges(
            root,
            nodes,
            edges,
            registry=registry,
            entities=entities,
            sources=sources,
        )

        return {
            # summary 留空，merge 时回退保留既有 overview。
            "summary": "",
            "entries": entries,
            "graph": {"nodes": nodes, "edges": edges},
            "_replaceFactEdges": FACT_SOURCE_PATH in changed_set or ENTITY_SOURCE_PATH in changed_set,
        }

    async def run_agent_workflow(
        self,
        workspace_root: Path,
        *,
        workflow: str,
        agent_runner: AgentWikiRunner | None = None,
    ) -> Dict[str, Any]:
        normalized_workflow = str(workflow or "").strip()
        if normalized_workflow not in WIKI_WORKFLOWS:
            raise ValueError(f"Unsupported WIKI workflow: {workflow}")

        root = workspace_root.resolve()
        sources = self._collect_sources(root)
        previous_index = self.read_index(root)
        changed_paths = self.changed_source_paths(root, sources=sources, previous_index=previous_index)
        trace_id = self._new_trace_id(normalized_workflow)
        prompt = self._build_agent_prompt(
            root,
            workflow=normalized_workflow,
            sources=sources,
            changed_paths=changed_paths,
        )

        agent_result: Dict[str, Any] = {
            "attempted": False,
            "completed": False,
            "errorMessage": "",
            "reply": "",
            "events": [],
            "traceId": trace_id,
        }
        agent_payload: Dict[str, Any] | None = None
        deterministic_graph_refresh = normalized_workflow == "refresh_wiki_graph"
        if not deterministic_graph_refresh and agent_runner is None:
            raise RuntimeError("WIKI Agent workflow requires an available provider runner.")
        if agent_runner is not None and not deterministic_graph_refresh:
            try:
                agent_result = await agent_runner(
                    prompt=prompt,
                    trace_id=trace_id,
                    session_id=f"story-wiki-{normalized_workflow}",
                    workspace_root=root,
                )
                agent_result["attempted"] = True
                agent_result.setdefault("traceId", trace_id)
                agent_payload = self._extract_agent_payload(str(agent_result.get("reply") or ""))
            except Exception as exc:
                raise RuntimeError(f"WIKI Agent provider execution failed: {exc}") from exc

            if not bool(agent_result.get("completed")):
                reason = str(agent_result.get("errorMessage") or "provider did not complete the workflow")
                raise RuntimeError(f"WIKI Agent provider execution failed: {reason}")
            if agent_payload is None:
                raise ValueError("WIKI Agent returned no valid structured JSON payload.")
            self._validate_agent_candidate_payload(agent_payload, root=root)
            if bool(agent_result.get("metadataRequired")):
                if not str(agent_result.get("providerId") or "").strip():
                    raise RuntimeError("WIKI Agent completed without provider metadata.")
                if not str(agent_result.get("model") or "").strip():
                    raise RuntimeError("WIKI Agent completed without model metadata.")

        before = self._read_existing_payload(root)
        status = "completed"
        fallback_used = False
        review_report: Dict[str, Any] | None = None
        candidate_result: Dict[str, Any] | None = None
        entity_candidate_result: Dict[str, Any] | None = None

        entity_candidates = (
            agent_payload.get("entityCandidates")
            if isinstance(agent_payload, dict) and isinstance(agent_payload.get("entityCandidates"), list)
            else []
        )
        if entity_candidates:
            entity_candidate_result = {
                "status": "review_required",
                "submittedCount": len(entity_candidates),
                "publishedCount": 0,
                "reason": "entity_candidates_are_not_published_automatically",
                "candidates": [dict(item) for item in entity_candidates if isinstance(item, dict)],
            }

        relation_candidates = (
            agent_payload.get("relationCandidates")
            if isinstance(agent_payload, dict) and isinstance(agent_payload.get("relationCandidates"), list)
            else []
        )
        if relation_candidates:
            from services.story_knowledge_relation_service import get_story_knowledge_relation_service

            candidate_result = get_story_knowledge_relation_service().submit_candidates(
                root,
                relation_candidates,
                trace_id=str(agent_result.get("traceId") or trace_id),
                provider_id=str(agent_result.get("providerId") or ""),
                model=str(agent_result.get("model") or ""),
                extractor_version="storydex-wiki-agent-v1",
            )

        if normalized_workflow == "review_wiki":
            payload = self.read_or_build(root)
            review_report = self._build_review_report(payload, agent_result=agent_result, agent_payload=agent_payload)
            self.review_report_path(root).write_text(
                json.dumps(review_report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            payload = self._annotate_payload(payload, workflow=normalized_workflow, agent_result=agent_result)
        else:
            # Agent 只可整理已存在条目的文本；节点和边始终由本地权威源重建。
            # 这使同一批源文件无论模型输出如何波动，都得到同一张图。
            canonical = self.rebuild(
                root,
                workflow=normalized_workflow,
                changed_paths=changed_paths,
            )
            incoming = (
                self.normalize_payload(agent_payload, root=root, workflow=normalized_workflow)
                if agent_payload
                else None
            )
            payload = self._compose_grounded_payload(
                canonical,
                previous=before,
                incoming=incoming,
            )
            payload = self._annotate_payload(payload, workflow=normalized_workflow, agent_result=agent_result)
            if deterministic_graph_refresh:
                payload["generationMode"] = "local evidence-grounded graph refresh"

        # Candidate submission and migration/reconciliation performed during
        # rebuild may create authoritative memory files after the initial
        # source scan. Persist against a fresh source set so the just-written
        # projection is immediately current instead of forcing a spurious
        # follow-up incremental revision on the next read.
        projection_sources = self._collect_sources(root)
        payload = self._persist_payload(
            root,
            payload,
            workflow=normalized_workflow,
            status=status,
            agent_result=agent_result,
            sources=projection_sources,
            changed_paths=changed_paths,
        )
        result = {
            "ok": True,
            "workflow": normalized_workflow,
            "status": status,
            "traceId": agent_result.get("traceId") or trace_id,
            "providerId": str(agent_result.get("providerId") or ""),
            "model": str(agent_result.get("model") or ""),
            "usage": dict(agent_result.get("usage") or {}) if isinstance(agent_result.get("usage"), dict) else {},
            "toolCalls": [
                dict(item)
                for item in agent_result.get("toolCalls", [])
                if isinstance(item, dict)
            ],
            "agentAttempted": bool(agent_result.get("attempted")),
            "agentCompleted": bool(agent_result.get("completed")),
            "fallbackUsed": fallback_used,
            "summary": self._workflow_summary(normalized_workflow, status, changed_paths),
            "workflowDefinitions": WIKI_WORKFLOW_DEFINITIONS,
            "changedSourcePaths": changed_paths,
            "writtenPaths": [
                self.wiki_json_path(root).relative_to(root).as_posix(),
                self.wiki_markdown_path(root).relative_to(root).as_posix(),
                self.wiki_index_path(root).relative_to(root).as_posix(),
            ],
            "errorMessage": str(agent_result.get("errorMessage") or ""),
            "wiki": payload,
        }
        if candidate_result is not None:
            result["candidateSubmission"] = candidate_result
            result["writtenPaths"].extend(candidate_result.get("writtenPaths") or [])
            result["writtenPaths"] = list(dict.fromkeys(result["writtenPaths"]))
        if entity_candidate_result is not None:
            result["entityCandidateSubmission"] = entity_candidate_result
        if review_report is not None:
            result["review"] = review_report
            result["writtenPaths"].append(self.review_report_path(root).relative_to(root).as_posix())
        return result

    @staticmethod
    def _validate_agent_candidate_payload(payload: Dict[str, Any], *, root: Path) -> None:
        required_keys = {"entries", "entityCandidates", "relationCandidates", "review"}
        missing = sorted(required_keys - set(payload))
        if missing:
            raise ValueError(f"WIKI Agent payload missing required field(s): {', '.join(missing)}.")
        for key in ("entries", "entityCandidates", "relationCandidates"):
            value = payload.get(key)
            if not isinstance(value, list):
                raise ValueError(f"WIKI Agent field {key} must be an array.")
        if not isinstance(payload.get("review"), dict):
            raise ValueError("WIKI Agent field review must be an object.")
        graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
        if graph.get("nodes") or graph.get("edges"):
            raise ValueError("WIKI Agent must not return graph nodes or edges.")

        resolved_root = root.resolve()
        for entry in payload.get("entries", []):
            if not isinstance(entry, dict):
                raise ValueError("WIKI Agent entries must contain objects.")
            source_paths = entry.get("sourcePaths") if isinstance(entry.get("sourcePaths"), list) else []
            for raw_path in source_paths:
                relative = str(raw_path or "").strip().replace("\\", "/").lstrip("/")
                if not relative or ".." in Path(relative).parts:
                    raise ValueError("WIKI Agent entry sourcePaths contains an invalid path.")
                candidate = (resolved_root / relative).resolve()
                try:
                    candidate.relative_to(resolved_root)
                except ValueError as exc:
                    raise ValueError("WIKI Agent entry sourcePaths escapes the workspace.") from exc
                if not candidate.is_file():
                    raise ValueError(f"WIKI Agent entry source path does not exist: {relative}.")

    def wiki_root(self, workspace_root: Path) -> Path:
        return workspace_root / ".storydex" / "wiki"

    def wiki_json_path(self, workspace_root: Path) -> Path:
        return self.wiki_root(workspace_root) / "knowledge_graph.json"

    def wiki_markdown_path(self, workspace_root: Path) -> Path:
        return self.wiki_root(workspace_root) / "WIKI.md"

    def wiki_index_path(self, workspace_root: Path) -> Path:
        return self.wiki_root(workspace_root) / "index.json"

    def review_report_path(self, workspace_root: Path) -> Path:
        return self.wiki_root(workspace_root) / "review_report.json"

    def projection_status_path(self, workspace_root: Path) -> Path:
        return self.wiki_root(workspace_root) / "projection_status.json"

    def _source_set_checksum(self, sources: Sequence[Dict[str, Any]]) -> str:
        canonical_sources = sorted(
            (
                {
                    "relativePath": str(source.get("relativePath") or "").replace("\\", "/"),
                    "sha256": str(source.get("sha256") or ""),
                    "kind": str(source.get("kind") or ""),
                }
                for source in sources
            ),
            key=lambda item: (item["relativePath"], item["kind"], item["sha256"]),
        )
        encoded = json.dumps(
            canonical_sources,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{sha256(encoded).hexdigest()}"

    def _graph_checksum(self, payload: Dict[str, Any]) -> str:
        def stable_value(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    str(key): stable_value(item)
                    for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
                    if str(key) not in GRAPH_CHECKSUM_VOLATILE_KEYS
                }
            if isinstance(value, list):
                return [stable_value(item) for item in value]
            return value

        entries = [
            stable_value(item)
            for item in payload.get("entries", [])
            if isinstance(item, dict)
        ]
        graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
        nodes = [
            stable_value(item)
            for item in graph.get("nodes", [])
            if isinstance(item, dict)
        ]
        edges = [
            stable_value(item)
            for item in graph.get("edges", [])
            if isinstance(item, dict)
        ]
        entries.sort(key=lambda item: str(item.get("id") or ""))
        nodes.sort(key=lambda item: str(item.get("id") or ""))
        edges.sort(
            key=lambda item: (
                str(item.get("source") or ""),
                str(item.get("target") or ""),
                str(item.get("relationType") or item.get("type") or ""),
                str(item.get("label") or ""),
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        )
        encoded = json.dumps(
            {"entries": entries, "nodes": nodes, "edges": edges},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{sha256(encoded).hexdigest()}"

    def _read_projection_status(self, root: Path) -> Dict[str, Any]:
        path = self.projection_status_path(root)
        if not path.exists():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}

    def _with_projection_status(self, root: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(payload)
        sidecar = self._read_projection_status(root)
        sidecar_status = str(sidecar.get("status") or "").strip()
        if sidecar_status in PROJECTION_STATUSES:
            result["status"] = sidecar_status
        else:
            result["status"] = str(result.get("status") or "ready")
        diagnostics = sidecar.get("diagnostics")
        if isinstance(diagnostics, list):
            result["diagnostics"] = [dict(item) for item in diagnostics if isinstance(item, dict)]
        else:
            result["diagnostics"] = [
                dict(item)
                for item in result.get("diagnostics", [])
                if isinstance(item, dict)
            ]
        last_successful = self._safe_int(
            sidecar.get("lastSuccessfulRevision"),
            fallback=self._safe_int(result.get("knowledgeRevision"), fallback=0),
        )
        result["lastSuccessfulRevision"] = max(0, last_successful)
        if sidecar_status in {"stale", "error", "rebuilding"}:
            attempted_checksum = str(sidecar.get("attemptedSourceSetChecksum") or "").strip()
            if attempted_checksum:
                result["attemptedSourceSetChecksum"] = attempted_checksum
        return result

    @staticmethod
    def _projection_metadata(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: payload.get(key)
            for key in (
                "schemaVersion",
                "knowledgeRevision",
                "builtFromRevision",
                "sourceSetChecksum",
                "graphChecksum",
                "status",
                "diagnostics",
                "lastSuccessfulRevision",
                "sourceStats",
                "graphStats",
            )
            if key in payload
        }

    def _attach_projection_metadata(
        self,
        payload: Dict[str, Any],
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {**self._projection_metadata(payload), **result}

    def read_index(self, workspace_root: Path) -> Dict[str, Any]:
        path = self.wiki_index_path(workspace_root)
        if not path.exists():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}

    def query_graph(
        self,
        workspace_root: Path,
        *,
        q: str = "",
        category: str = "",
        entry_id: str = "",
        node_id: str = "",
        depth: int = 1,
        limit: int = 60,
        offset: int = 0,
        include_review: bool = False,
    ) -> Dict[str, Any]:
        root = workspace_root.resolve()
        payload = self.read_or_build(root)
        entries = [entry for entry in payload.get("entries", []) if isinstance(entry, dict)]
        graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
        # 在 query 时对节点和边跑一次规范化兜底，
        # 让缓存里旧的中文 type（如 "动机"/"定位"/"小说"）自动归一到 "character"，
        # 避免 category=characters 的节点因 type 不规范而漏出角色关系视图。
        nodes = [self._normalize_node(node) for node in graph.get("nodes", []) if isinstance(node, dict)]
        all_edges = [self._normalize_graph_edge(edge) for edge in graph.get("edges", []) if isinstance(edge, dict)]
        edges = list(all_edges)
        if not include_review:
            edges = [
                edge
                for edge in edges
                if str(edge.get("reviewStatus") or "confirmed") != "review_required"
                and not bool(edge.get("needsReview"))
            ]
        category_labels = payload.get("categoryLabels") if isinstance(payload.get("categoryLabels"), dict) else CATEGORY_LABELS

        entry_by_id = {
            str(entry.get("id") or ""): entry
            for entry in entries
            if str(entry.get("id") or "").strip()
        }
        node_by_id = {
            str(node.get("id") or ""): node
            for node in nodes
            if str(node.get("id") or "").strip()
        }
        all_valid_edges = [
            edge
            for edge in all_edges
            if str(edge.get("source") or "") in node_by_id
            and str(edge.get("target") or "") in node_by_id
            and str(edge.get("source") or "") != str(edge.get("target") or "")
        ]
        valid_edges = [
            edge
            for edge in edges
            if str(edge.get("source") or "") in node_by_id
            and str(edge.get("target") or "") in node_by_id
            and str(edge.get("source") or "") != str(edge.get("target") or "")
        ]
        all_content_edges = [
            edge
            for edge in all_valid_edges
            if not self._wiki_edge_touches_hub(edge, node_by_id)
        ]
        content_edges = [
            edge
            for edge in valid_edges
            if not self._wiki_edge_touches_hub(edge, node_by_id)
        ]

        max_depth = max(1, min(2, self._safe_int(depth, fallback=1)))
        max_items = max(1, min(120, self._safe_int(limit, fallback=60)))
        page_offset = max(0, self._safe_int(offset, fallback=0))
        normalized_q = str(q or "").strip()
        raw_category = str(category or "").strip()
        # 旧 URL 与旧缓存里的 relationships/chapters 等分类在这里归一到三视图。
        normalized_category = self._normalize_wiki_category(raw_category) if raw_category else ""
        normalized_entry_id = str(entry_id or "").strip()
        normalized_node_id = str(node_id or "").strip()

        mode = "overview"
        matched_entry_ids: List[str] = []
        seed_node_ids: set[str] = set()

        if normalized_node_id:
            mode = "node"
            if normalized_node_id in node_by_id and not self._is_wiki_hub_node(node_by_id[normalized_node_id]):
                seed_node_ids.add(normalized_node_id)
        elif normalized_entry_id:
            mode = "entry"
            if normalized_entry_id in entry_by_id:
                matched_entry_ids.append(normalized_entry_id)
                seed_node_ids.update(
                    str(node.get("id") or "")
                    for node in nodes
                    if str(node.get("entryId") or "") == normalized_entry_id
                    and not self._is_wiki_hub_node(node)
                )
        elif normalized_q:
            mode = "search"
            query_tokens = self._query_tokens(normalized_q)
            for entry in entries:
                current_entry_id = str(entry.get("id") or "")
                if current_entry_id and self._wiki_entry_matches(entry, query_tokens):
                    matched_entry_ids.append(current_entry_id)
            matched_entry_id_set = set(matched_entry_ids)
            seed_node_ids.update(
                str(node.get("id") or "")
                for node in nodes
                if not self._is_wiki_hub_node(node)
                and (
                    str(node.get("entryId") or "") in matched_entry_id_set
                    or self._wiki_node_matches(node, query_tokens)
                )
            )
            for edge in content_edges:
                if self._wiki_edge_matches(edge, query_tokens):
                    seed_node_ids.add(str(edge.get("source") or ""))
                    seed_node_ids.add(str(edge.get("target") or ""))
        elif normalized_category and normalized_category != "overview":
            return self._attach_projection_metadata(
                payload,
                self._query_wiki_category_graph(
                    normalized_category,
                    root=root,
                    normalized_q=normalized_q,
                    normalized_entry_id=normalized_entry_id,
                    normalized_node_id=normalized_node_id,
                    max_depth=max_depth,
                    max_items=max_items,
                    offset=page_offset,
                    entries=entries,
                    entry_by_id=entry_by_id,
                    nodes=nodes,
                    valid_edges=content_edges,
                    stats_edges=all_content_edges,
                    category_labels=category_labels,
                    allow_agent_relationship_aliases=self._allows_agent_relationship_aliases(payload),
                    include_review=include_review,
                ),
            )
        else:
            # 没有检索词也没有指定可见分类：默认落到角色视图，不再返回人造 hub 总览图。
            return self._attach_projection_metadata(
                payload,
                self._query_wiki_category_graph(
                    "characters",
                    root=root,
                    normalized_q=normalized_q,
                    normalized_entry_id=normalized_entry_id,
                    normalized_node_id=normalized_node_id,
                    max_depth=max_depth,
                    max_items=max_items,
                    offset=page_offset,
                    entries=entries,
                    entry_by_id=entry_by_id,
                    nodes=nodes,
                    valid_edges=content_edges,
                    stats_edges=all_content_edges,
                    category_labels=category_labels,
                    allow_agent_relationship_aliases=self._allows_agent_relationship_aliases(payload),
                    include_review=include_review,
                ),
            )

        selected_node_ids = self._expand_wiki_node_neighborhood(
            seed_node_ids,
            node_by_id=node_by_id,
            edges=content_edges,
            depth=max_depth,
        )
        ordered_node_ids = [
            current_id
            for current_id in node_by_id
            if current_id in selected_node_ids and not self._is_wiki_hub_node(node_by_id[current_id])
        ]
        all_ordered_node_ids = list(ordered_node_ids)
        ordered_node_ids = ordered_node_ids[page_offset : page_offset + max_items]
        visible_node_ids = set(ordered_node_ids)
        visible_edges = [
            edge
            for edge in content_edges
            if str(edge.get("source") or "") in visible_node_ids
            and str(edge.get("target") or "") in visible_node_ids
        ][:max_items]

        if matched_entry_ids:
            visible_entry_ids = matched_entry_ids[page_offset : page_offset + max_items]
        else:
            visible_entry_ids = []
            for current_id in ordered_node_ids:
                entry_ref = str(node_by_id[current_id].get("entryId") or "")
                if entry_ref and entry_ref in entry_by_id and entry_ref not in visible_entry_ids:
                    visible_entry_ids.append(entry_ref)
                if len(visible_entry_ids) >= max_items:
                    break

        stats = self._graph_stats(
            [self._wiki_content_node(node_by_id[current_id]) for current_id in all_ordered_node_ids],
            all_content_edges,
            entry_count=len(matched_entry_ids) if matched_entry_ids else len(all_ordered_node_ids),
        )
        return self._attach_projection_metadata(payload, {
            "mode": mode,
            "query": normalized_q,
            "category": normalized_category,
            "entryId": normalized_entry_id,
            "nodeId": normalized_node_id,
            "depth": max_depth,
            "limit": max_items,
            "offset": page_offset,
            "includeReview": include_review,
            "returnedNodeCount": len(ordered_node_ids),
            "hasMore": page_offset + len(ordered_node_ids) < len(all_ordered_node_ids),
            "nextOffset": (
                page_offset + len(ordered_node_ids)
                if page_offset + len(ordered_node_ids) < len(all_ordered_node_ids)
                else None
            ),
            "entries": [entry_by_id[entry_ref] for entry_ref in visible_entry_ids if entry_ref in entry_by_id],
            "graph": {
                "nodes": [self._wiki_content_node(node_by_id[current_id]) for current_id in ordered_node_ids],
                "edges": visible_edges,
            },
            "matchedEntryIds": matched_entry_ids[page_offset : page_offset + max_items],
            "total": stats,
            "graphStats": stats,
            "pagination": self._pagination_payload(
                offset=page_offset,
                limit=max_items,
                returned_count=len(ordered_node_ids),
                total_count=len(all_ordered_node_ids),
            ),
        })

    def changed_source_paths(
        self,
        workspace_root: Path,
        *,
        sources: Sequence[Dict[str, Any]] | None = None,
        previous_index: Dict[str, Any] | None = None,
    ) -> List[str]:
        root = workspace_root.resolve()
        current_sources = list(sources) if sources is not None else self._collect_sources(root)
        previous = previous_index if previous_index is not None else self.read_index(root)
        previous_sources = previous.get("sources") if isinstance(previous, dict) else {}
        previous_by_path = previous_sources if isinstance(previous_sources, dict) else {}
        changed: List[str] = []
        for source in current_sources:
            rel = str(source.get("relativePath") or "")
            old = previous_by_path.get(rel) if isinstance(previous_by_path.get(rel), dict) else {}
            if old.get("sha256") != source.get("sha256"):
                changed.append(rel)
        for rel in previous_by_path:
            if rel not in {str(source.get("relativePath") or "") for source in current_sources}:
                changed.append(str(rel))
        return sorted(set(changed), key=self._source_sort_key)

    def normalize_payload(self, payload: Dict[str, Any], *, root: Path, workflow: str) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        entries = [self._normalize_entry(item) for item in payload.get("entries", []) if isinstance(item, dict)]
        graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
        nodes = [self._normalize_node(item) for item in graph.get("nodes", []) if isinstance(item, dict)]
        edges = [self._normalize_graph_edge(item) for item in graph.get("edges", []) if isinstance(item, dict)]

        if not entries and payload.get("summary"):
            entries.append(self._entry(
                "overview:project",
                "\u9879\u76ee\u603b\u89c8",
                "overview",
                str(payload.get("summary") or ""),
                [],
                [],
                confidence=0.55,
                needs_review=True,
            ))
        if not nodes:
            nodes.append({
                "id": "project:root",
                "label": str(payload.get("projectName") or root.name or "Storydex"),
                "type": "project",
                "category": "overview",
                "entryId": entries[0]["id"] if entries else "overview:project",
                "summary": str(payload.get("summary") or ""),
                "confidence": 0.55,
                "needsReview": True,
            })

        return {
            "version": int(payload.get("version") or 1),
            "categorySchemaVersion": WIKI_CATEGORY_SCHEMA_VERSION,
            "projectName": str(payload.get("projectName") or root.name),
            "workspaceRoot": root.as_posix(),
            "generatedAt": now,
            "generator": "agent-wiki" if workflow != "repair_wiki" else "agent-wiki-repair",
            "generationMode": self._workflow_generation_mode(workflow, agent=True),
            "llmStatus": "agent_completed",
            "categoryLabels": CATEGORY_LABELS,
            "nodeTypeLabels": NODE_TYPE_LABELS,
            "workflowDefinitions": WIKI_WORKFLOW_DEFINITIONS,
            "summary": str(payload.get("summary") or self._summary_from_entries(entries)),
            "entries": entries,
            "graph": {
                "nodes": self._dedupe_nodes(nodes),
                "edges": self._dedupe_edges(edges),
            },
            "sourceStats": payload.get("sourceStats") if isinstance(payload.get("sourceStats"), dict) else {},
        }

    def merge_payloads(
        self,
        base: Dict[str, Any],
        incoming: Dict[str, Any],
        *,
        graph_only: bool = False,
        removed_source_paths: Sequence[str] | None = None,
        mark_conflicts: bool = False,
    ) -> Dict[str, Any]:
        merged = dict(base)
        merged["generatedAt"] = datetime.now(timezone.utc).isoformat()
        merged["generator"] = incoming.get("generator") or base.get("generator") or "agent-wiki"
        merged["generationMode"] = incoming.get("generationMode") or base.get("generationMode") or "agent incremental"
        merged["llmStatus"] = incoming.get("llmStatus") or base.get("llmStatus") or "agent_completed"
        merged["categorySchemaVersion"] = WIKI_CATEGORY_SCHEMA_VERSION

        removed_set = {str(path) for path in (removed_source_paths or [])}

        if not graph_only:
            by_id = {str(entry.get("id")): dict(entry) for entry in base.get("entries", []) if isinstance(entry, dict)}
            for entry in incoming.get("entries", []):
                if not isinstance(entry, dict):
                    continue
                entry_id = str(entry.get("id") or "")
                if not entry_id:
                    continue
                if entry_id in by_id:
                    preserved = by_id[entry_id]
                    conflicted = mark_conflicts and self._entry_conflicts(preserved, entry)
                    previous_title = str(preserved.get("title") or "").strip()
                    incoming_title = str(entry.get("title") or "").strip()
                    aliases = [
                        *(
                            [str(alias) for alias in preserved.get("aliases", [])]
                            if isinstance(preserved.get("aliases"), list)
                            else []
                        ),
                        *(
                            [str(alias) for alias in entry.get("aliases", [])]
                            if isinstance(entry.get("aliases"), list)
                            else []
                        ),
                    ]
                    if previous_title and incoming_title and previous_title != incoming_title:
                        aliases.append(previous_title)
                    preserved.update({key: value for key, value in entry.items() if value not in ("", [], None)})
                    if aliases:
                        preserved["aliases"] = list(
                            dict.fromkeys(alias for alias in aliases if alias and alias != incoming_title)
                        )
                    if conflicted:
                        preserved["needsReview"] = True
                    by_id[entry_id] = preserved
                else:
                    by_id[entry_id] = dict(entry)
            if removed_set:
                by_id = {
                    entry_id: entry
                    for entry_id, entry in by_id.items()
                    if not self._entry_fully_removed(entry, removed_set)
                }
            merged["entries"] = list(by_id.values())
            merged["summary"] = incoming.get("summary") or base.get("summary") or self._summary_from_entries(merged["entries"])

        surviving_entry_ids = {str(entry.get("id")) for entry in merged.get("entries", []) if isinstance(entry, dict)}
        base_graph = base.get("graph") if isinstance(base.get("graph"), dict) else {}
        incoming_graph = incoming.get("graph") if isinstance(incoming.get("graph"), dict) else {}
        nodes_by_id: Dict[str, Dict[str, Any]] = {}
        for node in [
            *(base_graph.get("nodes", []) if isinstance(base_graph.get("nodes"), list) else []),
            *(incoming_graph.get("nodes", []) if isinstance(incoming_graph.get("nodes"), list) else []),
        ]:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id") or "").strip()
            if not node_id:
                continue
            # incoming 覆盖 base 的同 id 节点，以反映最新 summary/type。
            nodes_by_id[node_id] = node
        if not graph_only and removed_set:
            nodes_by_id = {
                node_id: node
                for node_id, node in nodes_by_id.items()
                if not self._node_orphaned_by_removal(node, surviving_entry_ids)
            }
        surviving_node_ids = set(nodes_by_id)
        base_edges = list(base_graph.get("edges", []) if isinstance(base_graph.get("edges"), list) else [])
        incoming_edges = list(incoming_graph.get("edges", []) if isinstance(incoming_graph.get("edges"), list) else [])
        if incoming.get("_replaceFactEdges"):
            base_edges = [edge for edge in base_edges if str(edge.get("type") or "") != "fact"]
        merged_edges = self._dedupe_edges([*base_edges, *incoming_edges])
        # Ghosting 修复：incoming 已为变更章节重建共现边，
        # base 中指向同一 evidence 章节的旧共现边应被清理，避免陈旧残留。
        if not graph_only:
            incoming_edges_list = incoming_graph.get("edges", []) if isinstance(incoming_graph.get("edges"), list) else []
            incoming_co_occurrence_evidence = {
                str(edge.get("evidence") or "")
                for edge in incoming_edges_list
                if edge.get("coOccurrence")
            }
            if incoming_co_occurrence_evidence:
                incoming_edge_ids = {
                    (str(edge.get("source") or ""), str(edge.get("target") or ""), str(edge.get("label") or ""))
                    for edge in incoming_edges_list
                }
                merged_edges = [
                    edge
                    for edge in merged_edges
                    if not (
                        edge.get("coOccurrence")
                        and str(edge.get("evidence") or "") in incoming_co_occurrence_evidence
                        and (str(edge.get("source") or ""), str(edge.get("target") or ""), str(edge.get("label") or "")) not in incoming_edge_ids
                    )
                ]
        if not graph_only and removed_set:
            merged_edges = [
                edge
                for edge in merged_edges
                if str(edge.get("source")) in surviving_node_ids and str(edge.get("target")) in surviving_node_ids
            ]
        merged["graph"] = {
            "nodes": list(nodes_by_id.values()),
            "edges": merged_edges,
        }
        return merged

    def _compose_grounded_payload(
        self,
        canonical: Dict[str, Any],
        *,
        previous: Dict[str, Any] | None,
        incoming: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        """Keep model-authored prose only on canonical entries; never accept its graph."""
        payload = dict(canonical)
        canonical_entries = [
            dict(entry)
            for entry in canonical.get("entries", [])
            if isinstance(entry, dict) and str(entry.get("id") or "").strip()
        ]
        entries_by_id = {str(entry["id"]): entry for entry in canonical_entries}
        enriched = False

        for candidate_payload in (previous, incoming):
            if not isinstance(candidate_payload, dict):
                continue
            candidate_entries = (
                candidate_payload.get("entries")
                if isinstance(candidate_payload.get("entries"), list)
                else []
            )
            for candidate in candidate_entries:
                if not isinstance(candidate, dict):
                    continue
                entry_id = str(candidate.get("id") or "").strip()
                base = entries_by_id.get(entry_id)
                if base is None:
                    continue
                if self._normalize_wiki_category(candidate.get("category")) != str(base.get("category") or ""):
                    continue

                base_sources = {
                    str(path).replace("\\", "/")
                    for path in base.get("sourcePaths", [])
                    if str(path).strip()
                }
                candidate_sources = {
                    str(path).replace("\\", "/")
                    for path in candidate.get("sourcePaths", [])
                    if str(path).strip()
                }
                if str(base.get("category") or "") != "overview":
                    if not candidate_sources or not base_sources.intersection(candidate_sources):
                        continue

                summary = str(candidate.get("summary") or "").strip()
                details = candidate.get("details") if isinstance(candidate.get("details"), list) else []
                if summary:
                    base["summary"] = summary
                    enriched = True
                if details:
                    base["details"] = [str(item) for item in details if str(item).strip()]
                    enriched = True
                base["needsReview"] = bool(base.get("needsReview") or candidate.get("needsReview"))
                base["sourcePaths"] = list(dict.fromkeys(base.get("sourcePaths", [])))

            candidate_summary = str(candidate_payload.get("summary") or "").strip()
            if candidate_summary and enriched:
                payload["summary"] = candidate_summary

        payload["entries"] = canonical_entries
        payload["graph"] = canonical.get("graph", {"nodes": [], "edges": []})
        payload["graphPolicy"] = dict(EVIDENCE_GROUNDED_GRAPH_POLICY)
        return payload

    def _entry_conflicts(self, base_entry: Dict[str, Any], incoming_entry: Dict[str, Any]) -> bool:
        """同 id 条目的核心文本实质不同则视为冲突（需人工确认），避免静默覆盖。"""
        base_summary = re.sub(r"\s+", " ", str(base_entry.get("summary") or "")).strip()
        incoming_summary = re.sub(r"\s+", " ", str(incoming_entry.get("summary") or "")).strip()
        if base_summary and incoming_summary and base_summary != incoming_summary:
            return True
        base_details = [re.sub(r"\s+", " ", str(item)).strip() for item in base_entry.get("details", []) if str(item).strip()]
        incoming_details = [re.sub(r"\s+", " ", str(item)).strip() for item in incoming_entry.get("details", []) if str(item).strip()]
        if base_details and incoming_details and base_details != incoming_details:
            return True
        return False

    def _entry_fully_removed(self, entry: Dict[str, Any], removed_set: set[str]) -> bool:
        """条目全部来源均已被删除，且非 Agent 高置信内容时，移除该条目。"""
        source_paths = [str(path) for path in entry.get("sourcePaths", []) if str(path).strip()]
        if not source_paths:
            return False
        if any(path not in removed_set for path in source_paths):
            return False
        confidence = self._confidence(entry.get("confidence"))
        if str(entry.get("generator") or "").startswith("agent") and confidence >= 0.75:
            return False
        return True

    def _node_orphaned_by_removal(self, node: Dict[str, Any], surviving_entry_ids: set[str]) -> bool:
        """节点绑定的条目已被删除则视为孤儿；无 entryId 的结构性节点保留。"""
        entry_id = str(node.get("entryId") or "").strip()
        if not entry_id:
            return False
        return entry_id not in surviving_entry_ids

    @staticmethod
    def _graph_diagnostic(
        code: str,
        message: str,
        path: str,
        *,
        scope: str = "payload",
        index: int = -1,
        blocking: bool = False,
    ) -> Dict[str, Any]:
        """结构化诊断：scope/index 指出坏对象在哪，blocking 决定它是否阻断发布。

        scope ∈ {"payload", "entry", "node", "edge"}；index 是对应数组下标，
        -1 表示这条诊断适用于整类对象（例如「所有空 ID 的条目」）。
        """
        return {
            "code": code,
            "severity": "error" if blocking else "warning",
            "message": message,
            "path": path,
            "scope": scope,
            "index": index,
            "blocking": blocking,
        }

    @staticmethod
    def _has_blocking_diagnostics(diagnostics: Sequence[Dict[str, Any]]) -> bool:
        """只有阻断性诊断才代表整份投影不可信，需要回退或全量重建。"""
        return any(
            bool(item.get("blocking"))
            or str(item.get("code") or "") in BLOCKING_GRAPH_DIAGNOSTIC_CODES
            for item in diagnostics
            if isinstance(item, dict)
        )

    @staticmethod
    def _unaddressable_indices(items: Sequence[Dict[str, Any]]) -> set[int]:
        """缺 ID 与重复 ID 的下标：空 ID 全丢，重复 ID 只留第一条。"""
        dropped: set[int] = set()
        seen: set[str] = set()
        for index, item in enumerate(items):
            identifier = str(item.get("id") or "").strip()
            if not identifier or identifier in seen:
                dropped.add(index)
                continue
            seen.add(identifier)
        return dropped

    def _quarantine_graph_objects(
        self,
        payload: Dict[str, Any],
        diagnostics: Sequence[Dict[str, Any]],
    ) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """按诊断摘掉坏对象并级联清理，而不是让一条坏边把整个知识库拖下水。

        边级诊断丢边，节点级诊断丢节点及其关联边，条目缺 ID / 重复 ID 丢条目，
        条目的证据与显示名问题只标 needsReview 保留内容。返回 (清理后 payload, 摘除说明)。
        """
        graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
        entries = [item for item in payload.get("entries", []) if isinstance(item, dict)]
        nodes = [item for item in graph.get("nodes", []) if isinstance(item, dict)]
        edges = [item for item in graph.get("edges", []) if isinstance(item, dict)]

        drop: Dict[str, set[int]] = {"entry": set(), "node": set(), "edge": set()}
        flagged: Dict[str, set[int]] = {"entry": set(), "node": set()}
        reasons: List[Dict[str, Any]] = []
        buckets = {"entry": entries, "node": nodes, "edge": edges}
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, dict):
                continue
            code = str(diagnostic.get("code") or "")
            if code in BLOCKING_GRAPH_DIAGNOSTIC_CODES or code in REPORT_ONLY_GRAPH_DIAGNOSTIC_CODES:
                continue
            scope = str(diagnostic.get("scope") or "payload")
            items = buckets.get(scope)
            if items is None:
                continue
            index = self._safe_int(diagnostic.get("index"), fallback=-1)
            if scope != "edge" and code not in UNADDRESSABLE_GRAPH_DIAGNOSTIC_CODES:
                # 条目/节点是正文内容：证据缺失或显示名不规范时保留内容，交给人工确认。
                if 0 <= index < len(items):
                    flagged[scope].add(index)
                continue
            targets = (
                {index}
                if 0 <= index < len(items)
                # 缺 ID / 重复 ID 是整类诊断，没有下标，只能按 ID 规则挑出该丢的那些。
                else self._unaddressable_indices(items)
            )
            if not targets:
                continue
            drop[scope].update(targets)
            reasons.append({"scope": scope, "code": code, "count": len(targets)})

        review_count = 0
        for scope, indices in flagged.items():
            for index in indices:
                if index in drop[scope]:
                    continue
                buckets[scope][index]["needsReview"] = True
                review_count += 1

        kept_entries = [entry for index, entry in enumerate(entries) if index not in drop["entry"]]
        surviving_entry_ids = {
            str(entry.get("id") or "").strip()
            for entry in kept_entries
            if str(entry.get("id") or "").strip()
        }
        kept_nodes = [
            node
            for index, node in enumerate(nodes)
            if index not in drop["node"] and not self._node_orphaned_by_removal(node, surviving_entry_ids)
        ]
        surviving_node_ids = {
            str(node.get("id") or "").strip()
            for node in kept_nodes
            if str(node.get("id") or "").strip()
        }
        kept_edges = [
            edge
            for index, edge in enumerate(edges)
            if index not in drop["edge"]
            and str(edge.get("source") or "").strip() in surviving_node_ids
            and str(edge.get("target") or "").strip() in surviving_node_ids
        ]

        dropped_counts = {
            "entries": len(entries) - len(kept_entries),
            "nodes": len(nodes) - len(kept_nodes),
            "edges": len(edges) - len(kept_edges),
        }
        if not any(dropped_counts.values()) and not review_count:
            return payload, []

        cleaned = dict(payload)
        cleaned["entries"] = kept_entries
        cleaned["graph"] = {**graph, "nodes": kept_nodes, "edges": kept_edges}
        summary = [
            {
                "code": "graph.quarantine",
                "severity": "warning",
                "message": (
                    f"已隔离 {dropped_counts['entries']} 条条目 / {dropped_counts['nodes']} 个节点 / "
                    f"{dropped_counts['edges']} 条边，其余内容正常发布。"
                ),
                "path": "graph",
                "scope": "payload",
                "index": -1,
                "blocking": False,
                "dropped": dropped_counts,
                "reasons": reasons,
            }
        ] if any(dropped_counts.values()) else []
        return cleaned, summary

    def validate_graph_invariants(
        self,
        payload: Dict[str, Any],
        *,
        root: Path | None = None,
        source_documents: Sequence[Dict[str, Any]] | None = None,
    ) -> List[Dict[str, Any]]:
        """Validate a candidate projection before it can replace the last-good graph."""
        diagnostics: List[Dict[str, Any]] = []
        entries = [item for item in payload.get("entries", []) if isinstance(item, dict)]
        graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
        nodes = [item for item in graph.get("nodes", []) if isinstance(item, dict)]
        edges = [item for item in graph.get("edges", []) if isinstance(item, dict)]

        entry_counts = Counter(str(entry.get("id") or "").strip() for entry in entries)
        entry_by_id = {
            str(entry.get("id") or "").strip(): entry
            for entry in entries
            if str(entry.get("id") or "").strip()
        }
        for entry_id, count in entry_counts.items():
            if not entry_id:
                diagnostics.append(self._graph_diagnostic(
                    "graph.entry.missing_id",
                    "WIKI 条目缺少稳定 ID。",
                    "entries",
                    scope="entry",
                ))
            elif count > 1:
                diagnostics.append(self._graph_diagnostic(
                    "graph.entry.duplicate_id",
                    f"WIKI 条目 ID {entry_id} 重复 {count} 次。",
                    f"entries.{entry_id}",
                    scope="entry",
                ))

        for index, entry in enumerate(entries):
            title = str(entry.get("title") or "").strip()
            if self._is_internal_display_label(title):
                diagnostics.append(self._graph_diagnostic(
                    "graph.entry.internal_label",
                    f"条目显示名泄漏内部 ID：{title}",
                    f"entries[{index}].title",
                    scope="entry",
                    index=index,
                ))
            category = self._normalize_wiki_category(entry.get("category"))
            source_refs = entry.get("sourcePaths")
            if not isinstance(source_refs, list):
                source_refs = entry.get("sourceRefs") if isinstance(entry.get("sourceRefs"), list) else []
            if category != "overview" and not any(str(value).strip() for value in source_refs):
                diagnostics.append(self._graph_diagnostic(
                    "graph.entry.missing_source",
                    f"条目 {entry.get('id') or index} 没有 sourcePath/sourceRef。",
                    f"entries[{index}].sourcePaths",
                    scope="entry",
                    index=index,
                ))

        node_counts = Counter(str(node.get("id") or "").strip() for node in nodes)
        node_by_id = {
            str(node.get("id") or "").strip(): node
            for node in nodes
            if str(node.get("id") or "").strip()
        }
        for node_id, count in node_counts.items():
            if not node_id:
                diagnostics.append(self._graph_diagnostic(
                    "graph.node.missing_id",
                    "图节点缺少稳定 ID。",
                    "graph.nodes",
                    scope="node",
                ))
            elif count > 1:
                diagnostics.append(self._graph_diagnostic(
                    "graph.node.duplicate_id",
                    f"图节点 ID {node_id} 重复 {count} 次。",
                    f"graph.nodes.{node_id}",
                    scope="node",
                ))

        for index, node in enumerate(nodes):
            node_id = str(node.get("id") or "").strip()
            label = str(node.get("label") or "").strip()
            if self._is_internal_display_label(label):
                diagnostics.append(self._graph_diagnostic(
                    "graph.node.internal_label",
                    f"节点显示名泄漏内部 ID：{label}",
                    f"graph.nodes[{index}].label",
                    scope="node",
                    index=index,
                ))
            selectable = bool(node.get("selectable", True)) and not bool(node.get("synthetic", False))
            if not selectable:
                continue
            if not label:
                diagnostics.append(self._graph_diagnostic(
                    "graph.node.missing_label",
                    f"可点击节点 {node_id or index} 缺少显示名。",
                    f"graph.nodes[{index}].label",
                    scope="node",
                    index=index,
                ))
            node_type = str(node.get("type") or "").strip()
            if node_type == "project":
                continue
            entry_id = str(node.get("entryId") or "").strip()
            if not entry_id:
                diagnostics.append(self._graph_diagnostic(
                    "graph.node.missing_entry",
                    f"可点击节点 {node_id or index} 没有 entryId。",
                    f"graph.nodes[{index}].entryId",
                    scope="node",
                    index=index,
                ))
                continue
            entry = entry_by_id.get(entry_id)
            if entry is None:
                diagnostics.append(self._graph_diagnostic(
                    "graph.node.missing_entry",
                    f"节点 {node_id or index} 引用了不存在的条目 {entry_id}。",
                    f"graph.nodes[{index}].entryId",
                    scope="node",
                    index=index,
                ))
                continue
            source_refs = entry.get("sourcePaths")
            if not isinstance(source_refs, list):
                source_refs = entry.get("sourceRefs") if isinstance(entry.get("sourceRefs"), list) else []
            if not any(str(value).strip() for value in source_refs):
                diagnostics.append(self._graph_diagnostic(
                    "graph.node.missing_source",
                    f"可点击节点 {node_id or index} 的条目没有 sourcePath/sourceRef。",
                    f"graph.nodes[{index}].entryId",
                    scope="node",
                    index=index,
                ))

        evidence_sources = (
            list(source_documents)
            if source_documents is not None
            else self._collect_sources(root)
            if root is not None
            else []
        )
        for index, edge in enumerate(edges):
            source = str(edge.get("source") or "").strip()
            target = str(edge.get("target") or "").strip()
            missing = [endpoint for endpoint in (source, target) if endpoint not in node_by_id]
            if missing:
                diagnostics.append(self._graph_diagnostic(
                    "graph.edge.missing_endpoint",
                    f"边 {source or '<empty>'} -> {target or '<empty>'} 含不存在端点：{', '.join(missing)}。",
                    f"graph.edges[{index}]",
                    scope="edge",
                    index=index,
                ))
            if source and source == target and not bool(edge.get("allowSelfLoop", False)):
                diagnostics.append(self._graph_diagnostic(
                    "graph.edge.self_loop",
                    f"边 {source} 未声明允许自环。",
                    f"graph.edges[{index}]",
                    scope="edge",
                    index=index,
                ))
            relation_type = str(edge.get("relationType") or "").strip()
            if relation_type and relation_type not in ALLOWED_RELATION_TYPES:
                diagnostics.append(self._graph_diagnostic(
                    "graph.edge.invalid_relation_type",
                    f"关系类型 {relation_type} 不在受控词表中。",
                    f"graph.edges[{index}].relationType",
                    scope="edge",
                    index=index,
                ))
            edge_type = str(edge.get("type") or "").strip()
            if edge.get("coOccurrence"):
                diagnostics.append(self._graph_diagnostic(
                    "graph.edge.cooccurrence_removed",
                    f"已停用的同章共现边不能发布：{source} -> {target}。",
                    f"graph.edges[{index}]",
                    scope="edge",
                    index=index,
                ))
            if edge_type == "relationship":
                endpoint_types = {
                    str((node_by_id.get(endpoint) or {}).get("type") or "")
                    for endpoint in (source, target)
                    if endpoint in node_by_id
                }
                if endpoint_types and endpoint_types != {"character"}:
                    diagnostics.append(self._graph_diagnostic(
                        "graph.edge.relationship_non_character",
                        f"角色关系边 {source} -> {target} 含非角色端点。",
                        f"graph.edges[{index}]",
                        scope="edge",
                        index=index,
                    ))
                if not relation_type or relation_type == "unknown" or str(edge.get("status") or "") != "asserted":
                    diagnostics.append(self._graph_diagnostic(
                        "graph.edge.unresolved_relationship",
                        f"未解析关系不能发布：{source} -> {target}。",
                        f"graph.edges[{index}]",
                        scope="edge",
                        index=index,
                    ))
                unsupported_metrics = self._unsupported_relationship_metrics(edge)
                if unsupported_metrics:
                    diagnostics.append(self._graph_diagnostic(
                        "graph.edge.synthetic_relationship_metric",
                        f"角色关系边 {source} -> {target} 含无证据量化值：{', '.join(unsupported_metrics)}。",
                        f"graph.edges[{index}]",
                        scope="edge",
                        index=index,
                    ))
                grounded = bool(
                    self._grounded_evidence_source_path(
                        root,
                        evidence=edge.get("evidence"),
                        requested_path=edge.get("sourcePath"),
                        sources=evidence_sources,
                    )
                ) if root is not None else bool(edge.get("evidence") and edge.get("sourcePath"))
                if not grounded:
                    diagnostics.append(self._graph_diagnostic(
                        "graph.edge.ungrounded_relationship",
                        f"角色关系边 {source} -> {target} 没有可在项目源文件中逐字核对的证据。",
                        f"graph.edges[{index}].evidence",
                        scope="edge",
                        index=index,
                    ))
                elif not self._edge_evidence_anchors_endpoints(
                    root,
                    edge,
                    nodes=nodes,
                    sources=evidence_sources,
                ):
                    diagnostics.append(self._graph_diagnostic(
                        "graph.edge.unanchored_relationship",
                        f"角色关系边 {source} -> {target} 的证据没有明确锚定两端角色。",
                        f"graph.edges[{index}].evidence",
                        scope="edge",
                        index=index,
                    ))
            elif edge_type == "fact":
                formal_grounded = bool(
                    root is not None
                    and self._formal_relation_edge_is_grounded(
                        root,
                        edge,
                        nodes=nodes,
                    )
                )
                grounded = bool(
                    self._grounded_evidence_source_path(
                        root,
                        evidence=edge.get("evidence"),
                        requested_path=edge.get("sourcePath"),
                        sources=evidence_sources,
                    )
                ) if root is not None else bool(edge.get("evidence") and edge.get("sourcePath"))
                if not grounded and not formal_grounded:
                    diagnostics.append(self._graph_diagnostic(
                        "graph.edge.ungrounded_fact",
                        f"事实边 {source} -> {target} 没有可核对证据。",
                        f"graph.edges[{index}].evidence",
                        scope="edge",
                        index=index,
                    ))
                elif not formal_grounded and not self._edge_evidence_anchors_endpoints(
                    root,
                    edge,
                    nodes=nodes,
                    sources=evidence_sources,
                ):
                    diagnostics.append(self._graph_diagnostic(
                        "graph.edge.unanchored_fact",
                        f"事实边 {source} -> {target} 的证据没有明确锚定两个端点。",
                        f"graph.edges[{index}].evidence",
                        scope="edge",
                        index=index,
                    ))

        revision_present = "knowledgeRevision" in payload or "builtFromRevision" in payload
        if revision_present:
            knowledge_revision = self._safe_int(payload.get("knowledgeRevision"), fallback=-1)
            built_from_revision = self._safe_int(payload.get("builtFromRevision"), fallback=-2)
            if knowledge_revision < 0 or built_from_revision != knowledge_revision:
                diagnostics.append(self._graph_diagnostic(
                    "graph.revision.mismatch",
                    f"builtFromRevision={built_from_revision} 与 knowledgeRevision={knowledge_revision} 不一致。",
                    "builtFromRevision",
                    blocking=True,
                ))

        if root is not None:
            character_nodes_by_id: Counter[str] = Counter(
                str(node.get("id") or "").strip()
                for node in nodes
                if str(node.get("type") or "") == "character"
            )
            for record in EntityRegistry(root).load_records():
                if record.kind not in {"character", "person", "role"} or not record.entity_id:
                    continue
                # 节点 ID 由 _entity_node_id 清洗而来，比对时要用同一套规则。
                resolved_id = self._sanitize_node_id(record.entity_id) or record.entity_id
                count = character_nodes_by_id.get(resolved_id, 0)
                if count != 1:
                    diagnostics.append(self._graph_diagnostic(
                        "graph.character.canonical_count",
                        f"active 角色 {record.canonical_name} ({record.entity_id}) 的 canonical 节点数为 {count}，应为 1。",
                        f"graph.nodes.{resolved_id}",
                    ))

        unique: Dict[tuple[str, str, str], Dict[str, Any]] = {}
        for diagnostic in diagnostics:
            key = (
                str(diagnostic.get("code") or ""),
                str(diagnostic.get("path") or ""),
                str(diagnostic.get("message") or ""),
            )
            unique.setdefault(key, diagnostic)
        return list(unique.values())

    def _source_stats(
        self,
        payload: Dict[str, Any],
        sources: Sequence[Dict[str, Any]],
    ) -> Dict[str, int]:
        graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
        character_ids = {
            str(node.get("id") or "")
            for node in graph.get("nodes", [])
            if isinstance(node, dict)
            and str(node.get("type") or "") == "character"
            and not bool(node.get("quarantined", False))
        }
        return {
            "scannedFiles": len(sources),
            "chapterFiles": sum(1 for source in sources if source.get("kind") == "chapter"),
            "characters": len(character_ids),
        }

    def _safe_projection_after_failure(
        self,
        root: Path,
        *,
        sources: Sequence[Dict[str, Any]],
        diagnostics: Sequence[Dict[str, Any]],
        last_successful_revision: int,
    ) -> Dict[str, Any]:
        revision = max(0, last_successful_revision)
        now = datetime.now(timezone.utc).isoformat()
        payload: Dict[str, Any] = {
            "version": 1,
            "schemaVersion": PROJECTION_SCHEMA_VERSION,
            "categorySchemaVersion": WIKI_CATEGORY_SCHEMA_VERSION,
            "projectName": root.name,
            "workspaceRoot": root.as_posix(),
            "generatedAt": now,
            "lastUpdatedAt": now,
            "categoryLabels": CATEGORY_LABELS,
            "nodeTypeLabels": NODE_TYPE_LABELS,
            "workflowDefinitions": WIKI_WORKFLOW_DEFINITIONS,
            "summary": "知识图谱候选版本校验失败，当前没有可用的 last-good 投影。",
            "entries": [{
                "id": "overview:projection-error",
                "title": "知识图谱暂不可用",
                "category": "overview",
                "categoryLabel": CATEGORY_LABELS["overview"],
                "summary": "请根据诊断修复源文件后重新构建。",
                "details": [],
                "sourcePaths": [],
                "confidence": 1.0,
                "needsReview": True,
                "updatedAt": now,
            }],
            "graph": {
                "nodes": [{
                    "id": "project:root",
                    "label": root.name or "项目",
                    "type": "project",
                    "category": "overview",
                    "entryId": "overview:projection-error",
                    "summary": "知识图谱候选版本校验失败。",
                    "selectable": False,
                }],
                "edges": [],
            },
            "knowledgeRevision": revision,
            "builtFromRevision": revision,
            "sourceSetChecksum": self._source_set_checksum(sources),
            "status": "error",
            "diagnostics": [dict(item) for item in diagnostics],
            "lastSuccessfulRevision": revision,
        }
        payload["sourceStats"] = self._source_stats(payload, sources)
        payload_graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
        payload["graphStats"] = self._graph_stats(
            [node for node in payload_graph.get("nodes", []) if isinstance(node, dict)],
            [edge for edge in payload_graph.get("edges", []) if isinstance(edge, dict)],
            entry_count=sum(
                1
                for entry in payload.get("entries", [])
                if isinstance(entry, dict) and str(entry.get("category") or "") != "overview"
            ),
        )
        payload["graphChecksum"] = self._graph_checksum(payload)
        return payload

    def _persist_payload(
        self,
        root: Path,
        payload: Dict[str, Any],
        *,
        workflow: str,
        status: str,
        agent_result: Dict[str, Any] | None,
        sources: Sequence[Dict[str, Any]],
        changed_paths: Sequence[str],
    ) -> Dict[str, Any]:
        wiki_root = self.wiki_root(root)
        wiki_root.mkdir(parents=True, exist_ok=True)
        previous = self._read_existing_payload(root)
        previous_status = self._read_projection_status(root)
        payload = self._normalize_wiki_payload(payload)
        payload["schemaVersion"] = PROJECTION_SCHEMA_VERSION
        payload["categorySchemaVersion"] = WIKI_CATEGORY_SCHEMA_VERSION
        payload["categoryLabels"] = CATEGORY_LABELS
        payload["graphPolicy"] = dict(EVIDENCE_GROUNDED_GRAPH_POLICY)
        payload.setdefault("nodeTypeLabels", NODE_TYPE_LABELS)
        payload.setdefault("workflowDefinitions", WIKI_WORKFLOW_DEFINITIONS)
        payload["lastWorkflow"] = workflow
        payload["lastWorkflowStatus"] = status
        payload["lastUpdatedAt"] = datetime.now(timezone.utc).isoformat()
        payload["changedSourcePaths"] = list(changed_paths)
        source_checksum = self._source_set_checksum(sources)
        previous_revision = max(
            self._safe_int(previous.get("knowledgeRevision"), fallback=0) if previous else 0,
            self._safe_int(previous_status.get("lastSuccessfulRevision"), fallback=0),
        )
        previous_checksum = str(
            (previous or {}).get("sourceSetChecksum")
            or previous_status.get("sourceSetChecksum")
            or ""
        )
        if previous_revision <= 0:
            knowledge_revision = 1
        elif source_checksum != previous_checksum:
            knowledge_revision = previous_revision + 1
        else:
            knowledge_revision = previous_revision
        payload["knowledgeRevision"] = knowledge_revision
        payload["builtFromRevision"] = knowledge_revision
        payload["sourceSetChecksum"] = source_checksum
        payload["lastSuccessfulRevision"] = knowledge_revision
        if agent_result is not None:
            payload["agent"] = {
                "attempted": bool(agent_result.get("attempted")),
                "completed": bool(agent_result.get("completed")),
                "traceId": str(agent_result.get("traceId") or ""),
                "providerId": str(agent_result.get("providerId") or ""),
                "model": str(agent_result.get("model") or ""),
                "usage": dict(agent_result.get("usage") or {}) if isinstance(agent_result.get("usage"), dict) else {},
                "toolCalls": [
                    dict(item)
                    for item in agent_result.get("toolCalls", [])
                    if isinstance(item, dict)
                ],
                "errorMessage": str(agent_result.get("errorMessage") or ""),
                "eventCount": len(agent_result.get("events") or []),
            }
        # 隔离而非全盘否定：先按诊断摘掉坏对象，复检后只有阻断性问题才回退到 last-good。
        payload, quarantine_notes = self._quarantine_graph_objects(
            payload,
            self.validate_graph_invariants(payload, root=root, source_documents=sources),
        )
        input_diagnostics = (
            [dict(item) for item in payload.get("_inputDiagnostics", []) if isinstance(item, dict)]
            if isinstance(payload.get("_inputDiagnostics"), list)
            else []
        )
        payload.pop("_inputDiagnostics", None)
        diagnostics = [
            *self.validate_graph_invariants(payload, root=root, source_documents=sources),
            *quarantine_notes,
            *input_diagnostics,
        ]
        payload["sourceStats"] = self._source_stats(payload, sources)
        payload_graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
        payload["graphStats"] = self._graph_stats(
            [node for node in payload_graph.get("nodes", []) if isinstance(node, dict)],
            [edge for edge in payload_graph.get("edges", []) if isinstance(edge, dict)],
            entry_count=sum(
                1
                for entry in payload.get("entries", [])
                if isinstance(entry, dict) and str(entry.get("category") or "") != "overview"
            ),
        )
        payload["graphChecksum"] = self._graph_checksum(payload)
        if self._has_blocking_diagnostics(diagnostics):
            last_successful_revision = previous_revision
            failure_status = {
                "schemaVersion": PROJECTION_SCHEMA_VERSION,
                "status": "error",
                "diagnostics": diagnostics,
                "lastSuccessfulRevision": last_successful_revision,
                "knowledgeRevision": self._safe_int((previous or {}).get("knowledgeRevision"), fallback=0),
                "builtFromRevision": self._safe_int((previous or {}).get("builtFromRevision"), fallback=0),
                "sourceSetChecksum": previous_checksum,
                "attemptedSourceSetChecksum": source_checksum,
                "graphChecksum": str((previous or {}).get("graphChecksum") or ""),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }
            self._write_json_atomic(self.projection_status_path(root), failure_status)
            if (
                previous
                and self._has_current_category_schema(previous)
                and not self._has_blocking_diagnostics(self.validate_graph_invariants(
                    previous,
                    root=root,
                    source_documents=sources,
                ))
            ):
                return self._with_projection_status(root, previous)
            return self._safe_projection_after_failure(
                root,
                sources=sources,
                diagnostics=diagnostics,
                last_successful_revision=last_successful_revision,
            )

        payload["status"] = "ready"
        payload["diagnostics"] = diagnostics
        index_payload = self._build_index(
            root,
            payload,
            sources=sources,
            workflow=workflow,
            status=status,
            changed_paths=changed_paths,
        )
        projection_status = {
            "schemaVersion": PROJECTION_SCHEMA_VERSION,
            "status": "ready",
            "diagnostics": diagnostics,
            "knowledgeRevision": knowledge_revision,
            "builtFromRevision": knowledge_revision,
            "lastSuccessfulRevision": knowledge_revision,
            "sourceSetChecksum": source_checksum,
            "graphChecksum": payload["graphChecksum"],
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        self._write_projection_bundle(
            root,
            payload=payload,
            index_payload=index_payload,
            status_payload=projection_status,
        )
        return payload

    def _write_projection_bundle(
        self,
        root: Path,
        *,
        payload: Dict[str, Any],
        index_payload: Dict[str, Any],
        status_payload: Dict[str, Any],
    ) -> None:
        targets = {
            self.wiki_json_path(root): json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            self.wiki_markdown_path(root): self._render_markdown(payload),
            self.wiki_index_path(root): json.dumps(index_payload, ensure_ascii=False, indent=2) + "\n",
            self.projection_status_path(root): json.dumps(status_payload, ensure_ascii=False, indent=2) + "\n",
        }
        temporary_paths: Dict[Path, Path] = {}
        originals: Dict[Path, bytes | None] = {}
        committed: List[Path] = []
        try:
            for target, content in targets.items():
                target.parent.mkdir(parents=True, exist_ok=True)
                originals[target] = target.read_bytes() if target.exists() else None
                temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
                with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary_paths[target] = temporary
            for target, temporary in temporary_paths.items():
                os.replace(temporary, target)
                committed.append(target)
        except Exception:
            for target in reversed(committed):
                original = originals.get(target)
                try:
                    if original is None:
                        target.unlink(missing_ok=True)
                        continue
                    restore = target.with_name(f".{target.name}.{uuid4().hex}.restore")
                    try:
                        with restore.open("wb") as stream:
                            stream.write(original)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(restore, target)
                    finally:
                        restore.unlink(missing_ok=True)
                except Exception:
                    if original is None:
                        target.unlink(missing_ok=True)
                    else:
                        target.write_bytes(original)
            raise
        finally:
            for temporary in temporary_paths.values():
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def _build_index(
        self,
        root: Path,
        payload: Dict[str, Any],
        *,
        sources: Sequence[Dict[str, Any]],
        workflow: str,
        status: str,
        changed_paths: Sequence[str],
    ) -> Dict[str, Any]:
        entries = [entry for entry in payload.get("entries", []) if isinstance(entry, dict)]
        nodes = [node for node in payload.get("graph", {}).get("nodes", []) if isinstance(node, dict)]

        entry_ids_by_source: Dict[str, List[str]] = {}
        source_paths_by_entry_id: Dict[str, List[str]] = {}
        for entry in entries:
            entry_id = str(entry.get("id") or "")
            raw_source_paths = entry.get("sourcePaths")
            source_paths = (
                list(
                    dict.fromkeys(
                        str(item)
                        for item in raw_source_paths
                        if str(item)
                    )
                )
                if isinstance(raw_source_paths, list)
                else []
            )
            for rel in source_paths:
                entry_ids_by_source.setdefault(rel, []).append(entry_id)
            source_paths_by_entry_id[entry_id] = list(
                dict.fromkeys([*source_paths_by_entry_id.get(entry_id, []), *source_paths])
            )

        node_ids_by_source: Dict[str, List[str]] = {}
        for node in nodes:
            entry_id = str(node.get("entryId") or "")
            node_id = str(node.get("id") or "")
            for rel in source_paths_by_entry_id.get(entry_id, []):
                node_ids_by_source.setdefault(rel, []).append(node_id)

        sources_index: Dict[str, Any] = {}
        for source in sources:
            rel = str(source.get("relativePath") or "")
            sources_index[rel] = {
                "sha256": source.get("sha256"),
                "kind": source.get("kind"),
                "size": source.get("size"),
                "mtime": source.get("mtime"),
                "lastAnalyzedAt": datetime.now(timezone.utc).isoformat(),
                "relatedEntryIds": list(entry_ids_by_source.get(rel, [])),
                "relatedNodeIds": list(node_ids_by_source.get(rel, [])),
            }
        return {
            "version": 2,
            "schemaVersion": payload.get("schemaVersion"),
            "projectName": root.name,
            "workspaceRoot": root.as_posix(),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "lastWorkflow": workflow,
            "lastStatus": status,
            "status": payload.get("status"),
            "diagnostics": payload.get("diagnostics", []),
            "knowledgeRevision": payload.get("knowledgeRevision"),
            "builtFromRevision": payload.get("builtFromRevision"),
            "lastSuccessfulRevision": payload.get("lastSuccessfulRevision"),
            "sourceSetChecksum": payload.get("sourceSetChecksum"),
            "graphChecksum": payload.get("graphChecksum"),
            "workflowDefinitions": WIKI_WORKFLOW_DEFINITIONS,
            "categorySchemaVersion": WIKI_CATEGORY_SCHEMA_VERSION,
            "allowedCategories": list(CATEGORY_LABELS),
            "changedSourcePaths": list(changed_paths),
            "sources": sources_index,
            "sourceStats": payload.get("sourceStats", {}),
            "entryCount": len(entries),
            "nodeCount": len(nodes),
            "edgeCount": len(payload.get("graph", {}).get("edges", [])),
        }

    def _read_existing_payload(self, root: Path) -> Dict[str, Any] | None:
        path = self.wiki_json_path(root)
        if not path.exists():
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else None
        except Exception:
            return None

    @staticmethod
    def _migrate_knowledge_relation_storage(root: Path) -> Dict[str, Any]:
        """Upgrade legacy entity/fact memory before any projection reads it.

        Migration is idempotent and owns its own pre-migration backup.  Errors
        deliberately propagate so callers keep the previous WIKI bundle instead
        of publishing a projection built from partially upgraded state.
        """
        from services.story_knowledge_relation_service import get_story_knowledge_relation_service

        return get_story_knowledge_relation_service().migrate_v1(root)

    def _backup_legacy_projection(self, root: Path) -> None:
        previous = self._read_existing_payload(root)
        if previous is None or self._has_current_category_schema(previous):
            return
        raw_schema = str(
            previous.get("categorySchemaVersion")
            or f"projection-v{self._safe_int(previous.get('schemaVersion'), fallback=0)}"
        )
        schema_name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_schema).strip("-.") or "legacy"
        backup_root = self.wiki_root(root) / "migration-backups" / schema_name[:96]
        for source in (
            self.wiki_json_path(root),
            self.wiki_markdown_path(root),
            self.wiki_index_path(root),
        ):
            if not source.is_file():
                continue
            target = backup_root / source.name
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            try:
                shutil.copy2(source, temporary)
                os.replace(temporary, target)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def _build_agent_prompt(
        self,
        root: Path,
        *,
        workflow: str,
        sources: Sequence[Dict[str, Any]],
        changed_paths: Sequence[str],
    ) -> str:
        source_by_path = {str(item.get("relativePath") or ""): item for item in sources}
        sample_paths = list(dict.fromkeys([
            *[path for path in changed_paths if path in source_by_path],
            *[str(item.get("relativePath") or "") for item in sources],
        ]))
        source_sample = [
            {
                "relativePath": item["relativePath"],
                "kind": item["kind"],
                "sha256": item.get("sha256"),
                "size": item.get("size"),
                "mtime": item.get("mtime"),
                "preview": self._compress_text(str(item.get("text") or ""), 420),
            }
            for item in [source_by_path[path] for path in sample_paths[:48] if path in source_by_path]
        ]
        source_manifest = [
            {
                "relativePath": item["relativePath"],
                "kind": item["kind"],
                "sha256": item.get("sha256"),
                "size": item.get("size"),
                "mtime": item.get("mtime"),
            }
            for item in sources
        ]
        existing_wiki = self._build_existing_wiki_context(root)
        return (
            "你是 Storydex 的知识图谱 / LLM WIKI Agent。请执行指定 workflow，并只输出一个 JSON 对象。\n"
            "你需要主动读取项目中和小说设定相关的文件，包括章节、角色、世界观、记忆和已有 WIKI 上下文。\n"
            "权威实体与关系来源是 .storydex/memory/current/entities.json、relationship_graph.json、facts.json；"
            "README、模板、预设和 Storydex 框架配置不是故事事实，不得据此创建角色或设定。\n"
            "后端会负责最终写入文件；你不要直接写文件，只返回结构化 JSON。\n"
            f"workflow: {workflow}\n"
            f"project: {root.as_posix()}\n"
            f"workflowDefinitions: {json.dumps(WIKI_WORKFLOW_DEFINITIONS, ensure_ascii=False)}\n"
            "workflowProtocol:\n"
            "- generate_wiki: 全量阅读并整理已有 canonical entries 的文本。\n"
            "- update_wiki: 优先分析 changedSourcePaths，再结合 existingWiki 合并更新；不要粗暴覆盖旧条目。\n"
            "- refresh_wiki_graph: 由后端本地投影执行，不会调用你。\n"
            "- review_wiki: 输出 review.issues 与 review.recommendations，标记缺漏、冲突、过时和需人工确认内容。\n"
            "- repair_wiki: 修复缺失字段、坏结构、不完整关系，保持稳定 id。\n"
            "category 只允许三类: characters(角色)、plot(剧情)、setting(设定)。"
            "章节/事件/时间线归入 plot；世界/地点/物品/势力/伏笔归入 setting；"
            "角色关系是角色图里的连线，不要单列 relationships 分类；不要输出其他 category。\n"
            "图结构由后端从实体注册表、正式 Markdown、facts 和审阅账本确定性生成；你不得返回 graph.nodes/edges。\n"
            "不要根据同章出现、常见剧情套路、姓氏、身份或语气推断人物关系。没有明示证据就保持沉默。\n"
            "正文中发现的关系只能放入 relationCandidates，绝不能作为 confirmed fact。每个关系候选必须包含"
            " subjectId/subject、predicate、objectId/object、knowledgeStatus、confidence，以及逐字 sourceRefs"
            "[{path,quote,lineStart?,lineEnd?,role}]。否定和假设不要输出；传闻最多输出 knowledgeStatus=inferred。\n"
            "新实体只能放入 entityCandidates；后端不会自动发布实体候选，应说明来源与未发布原因。\n"
            "输出必须是且只能是这个顶层 JSON 契约："
            "{\"entries\":[],\"entityCandidates\":[],\"relationCandidates\":[],\"review\":{}}。"
            "entries 项允许字段 {id,title,category,categoryLabel,summary,details,sourcePaths,confidence,needsReview}；"
            "review 可包含 issues/recommendations。\n"
            "只返回 existingWiki 或源清单能够映射到的 entry id；sourcePaths 必须引用真实项目相对路径；不确定事实必须 needsReview=true。\n"
            "如果发现旧 WIKI 中有高质量内容且相关源文件未变化，应保留其 id 和摘要，只补充必要的新证据。\n"
            f"changedSourcePaths: {json.dumps(list(changed_paths), ensure_ascii=False)}\n"
            f"existingWiki: {json.dumps(existing_wiki, ensure_ascii=False)}\n"
            f"completeSourceManifest: {json.dumps(source_manifest, ensure_ascii=False)}\n"
            f"sourceSample: {json.dumps(source_sample, ensure_ascii=False)}"
        )

    def _build_existing_wiki_context(self, root: Path) -> Dict[str, Any]:
        payload = self._read_existing_payload(root) or {}
        index = self.read_index(root)
        graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
        entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
        nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
        edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
        return {
            "exists": bool(payload),
            "knowledgeGraphPath": self.wiki_json_path(root).relative_to(root).as_posix(),
            "markdownPath": self.wiki_markdown_path(root).relative_to(root).as_posix(),
            "indexPath": self.wiki_index_path(root).relative_to(root).as_posix(),
            "summary": str(payload.get("summary") or ""),
            "generator": str(payload.get("generator") or ""),
            "generationMode": str(payload.get("generationMode") or ""),
            "lastWorkflow": str(payload.get("lastWorkflow") or index.get("lastWorkflow") or ""),
            "lastWorkflowStatus": str(payload.get("lastWorkflowStatus") or index.get("lastStatus") or ""),
            "lastUpdatedAt": str(payload.get("lastUpdatedAt") or index.get("updatedAt") or ""),
            "entryCount": len(entries),
            "nodeCount": len(nodes),
            "edgeCount": len(edges),
            "entries": [
                {
                    "id": str(entry.get("id") or ""),
                    "title": str(entry.get("title") or ""),
                    "category": str(entry.get("category") or ""),
                    "summary": self._compress_text(str(entry.get("summary") or ""), 220),
                    "sourcePaths": entry.get("sourcePaths") if isinstance(entry.get("sourcePaths"), list) else [],
                    "confidence": entry.get("confidence"),
                    "needsReview": bool(entry.get("needsReview", False)),
                    "updatedAt": str(entry.get("updatedAt") or ""),
                }
                for entry in entries[:100]
                if isinstance(entry, dict)
            ],
            "index": {
                "sourceCount": len(index.get("sources", {})) if isinstance(index.get("sources"), dict) else 0,
                "changedSourcePaths": index.get("changedSourcePaths") if isinstance(index.get("changedSourcePaths"), list) else [],
            },
        }

    def _extract_agent_payload(self, reply: str) -> Dict[str, Any] | None:
        text = str(reply or "").strip()
        if not text:
            return None
        candidates = [text]
        fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
        candidates.extend(fenced)
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            candidates.append(text[first:last + 1])
        for candidate in candidates:
            try:
                loaded = json.loads(candidate)
            except Exception:
                continue
            if isinstance(loaded, dict):
                return loaded
        return None

    def _has_current_category_schema(self, payload: Dict[str, Any]) -> bool:
        if self._safe_int(payload.get("schemaVersion"), fallback=0) != PROJECTION_SCHEMA_VERSION:
            return False
        if payload.get("categorySchemaVersion") != WIKI_CATEGORY_SCHEMA_VERSION:
            return False
        entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            category = str(entry.get("category") or "").strip()
            if not category or self._normalize_wiki_category(category) != category:
                return False
        graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
        nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            category = str(node.get("category") or "").strip()
            if category and self._normalize_wiki_category(category) != category:
                return False
        return True

    def _normalize_wiki_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(payload)
        entries = [self._normalize_entry(item) for item in payload.get("entries", []) if isinstance(item, dict)]
        entry_category_by_id = {str(entry.get("id") or ""): str(entry.get("category") or "") for entry in entries}
        graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
        nodes: List[Dict[str, Any]] = []
        for item in graph.get("nodes", []) if isinstance(graph.get("nodes"), list) else []:
            if not isinstance(item, dict):
                continue
            node = self._normalize_node(item)
            entry_id = str(node.get("entryId") or "")
            if entry_id in entry_category_by_id:
                node["category"] = entry_category_by_id[entry_id]
            nodes.append(node)
        edges = [self._normalize_graph_edge(item) for item in graph.get("edges", []) if isinstance(item, dict)]
        normalized["entries"] = entries
        normalized["graph"] = {
            "nodes": self._dedupe_nodes(nodes),
            "edges": self._dedupe_edges(edges),
        }
        normalized["categorySchemaVersion"] = WIKI_CATEGORY_SCHEMA_VERSION
        normalized["categoryLabels"] = CATEGORY_LABELS
        normalized.setdefault("nodeTypeLabels", NODE_TYPE_LABELS)
        normalized.setdefault("workflowDefinitions", WIKI_WORKFLOW_DEFINITIONS)
        return normalized

    @staticmethod
    def _normalize_wiki_category(category: Any) -> str:
        raw = str(category or "").strip()
        if not raw:
            return "overview"
        return CATEGORY_ALIASES.get(raw, raw if raw in ALLOWED_WIKI_CATEGORIES else "overview")

    def _normalize_entry(self, item: Dict[str, Any]) -> Dict[str, Any]:
        category = self._normalize_wiki_category(item.get("category"))
        raw_title = str(item.get("title") or "").strip()
        title = raw_title if raw_title and not self._is_internal_display_label(raw_title) else "未解析实体"
        entry_id = str(item.get("id") or f"{category}:{self._slug(title)}").strip()
        raw_source_paths = (
            item.get("sourcePaths")
            if isinstance(item.get("sourcePaths"), list)
            else item.get("sourceRefs")
            if isinstance(item.get("sourceRefs"), list)
            else []
        )
        entry = {
            "id": entry_id,
            "title": title,
            "category": category,
            "categoryLabel": str(item.get("categoryLabel") or CATEGORY_LABELS.get(category, category)),
            "summary": str(item.get("summary") or ""),
            "details": [str(value) for value in item.get("details", []) if str(value).strip()] if isinstance(item.get("details"), list) else [],
            "sourcePaths": [str(value) for value in raw_source_paths if str(value).strip()],
            "confidence": self._confidence(item.get("confidence")),
            "needsReview": bool(item.get("needsReview", False) or title == "未解析实体"),
            "updatedAt": str(item.get("updatedAt") or datetime.now(timezone.utc).isoformat()),
        }
        knowledge_status = str(item.get("knowledgeStatus") or "").strip()
        if knowledge_status in KNOWLEDGE_STATUSES:
            entry["knowledgeStatus"] = knowledge_status
        aliases = [str(value).strip() for value in item.get("aliases", []) if str(value).strip()] if isinstance(item.get("aliases"), list) else []
        if aliases:
            entry["aliases"] = list(dict.fromkeys(aliases))
        primary_source_path = str(item.get("primarySourcePath") or "").strip()
        if primary_source_path:
            entry["primarySourcePath"] = primary_source_path
        return entry

    def _normalize_node(self, item: Dict[str, Any]) -> Dict[str, Any]:
        raw_label = str(item.get("label") or "").strip()
        quarantined = not raw_label or self._is_internal_display_label(raw_label)
        label = "未解析实体" if quarantined else raw_label
        node_type = str(item.get("type") or "event").strip() or "event"
        node_id = str(item.get("id") or f"{node_type}:{self._slug(label)}").strip()
        category = self._normalize_wiki_category(item.get("category")) if str(item.get("category") or "").strip() else ""
        # 交叉规范化：仅原始 category 就是 characters 的节点强制 type=character，
        # 修正 Agent 输出 "动机"/"定位"/"小说" 等中文 type。
        # 别名归一过来的旧 relationships 节点（关系条目、索引等）不是角色，
        # 强转会把它们混进角色图，因此保持原 type。
        if str(item.get("category") or "").strip() == "characters":
            node_type = "character"
        node = {
            "id": node_id,
            "label": label,
            "type": node_type,
            "category": category,
            "entryId": str(item.get("entryId") or ""),
            "summary": str(item.get("summary") or ""),
            "confidence": self._confidence(item.get("confidence")),
            "needsReview": bool(item.get("needsReview", False) or quarantined),
            "selectable": bool(item.get("selectable", True)) and not quarantined,
            "quarantined": quarantined,
        }
        knowledge_status = str(item.get("knowledgeStatus") or "").strip()
        if knowledge_status in KNOWLEDGE_STATUSES:
            node["knowledgeStatus"] = knowledge_status
        if item.get("narrativeOrder") is not None:
            node["narrativeOrder"] = self._safe_int(item.get("narrativeOrder"), fallback=0)
        if item.get("synthetic"):
            node["synthetic"] = True
        return node

    def _normalize_graph_edge(self, item: Dict[str, Any]) -> Dict[str, Any]:
        edge: Dict[str, Any] = {
            "id": str(item.get("id") or ""),
            "source": str(item.get("source") or ""),
            "target": str(item.get("target") or ""),
            "label": str(item.get("label") or item.get("type") or "\u5173\u8054"),
            "predicate": str(item.get("predicate") or item.get("label") or ""),
            "type": str(item.get("type") or "related"),
            "weight": int(item.get("weight") or 1),
            "evidence": str(item.get("evidence") or ""),
            "sourcePath": str(item.get("sourcePath") or item.get("source_path") or ""),
            "needsReview": bool(item.get("needsReview", False)),
        }
        review_status = str(item.get("reviewStatus") or "").strip()
        if review_status in {"confirmed", "review_required", "rejected", "superseded"}:
            edge["reviewStatus"] = review_status
            edge["needsReview"] = review_status == "review_required"
        knowledge_status = str(item.get("knowledgeStatus") or "").strip()
        if knowledge_status in KNOWLEDGE_STATUSES:
            edge["knowledgeStatus"] = knowledge_status
        source_refs = item.get("sourceRefs") if isinstance(item.get("sourceRefs"), list) else []
        if source_refs:
            edge["sourceRefs"] = [dict(ref) for ref in source_refs if isinstance(ref, dict)]
        provenance = item.get("provenance") if isinstance(item.get("provenance"), dict) else {}
        if provenance:
            edge["provenance"] = dict(provenance)
        for key in ("traceId", "fingerprint"):
            value = str(item.get(key) or "").strip()
            if value:
                edge[key] = value
        if item.get("confidence") is not None:
            edge["confidence"] = self._confidence(item.get("confidence"))
        if item.get("coOccurrence"):
            edge["coOccurrence"] = True
        if item.get("allowSelfLoop"):
            edge["allowSelfLoop"] = True
        for key in ("relationType", "polarity", "status", "dimension"):
            value = str(item.get(key) or "").strip()
            if value:
                edge[key] = value
        if item.get("strength") is not None:
            try:
                edge["strength"] = max(0.0, min(1.0, float(item.get("strength"))))
            except (TypeError, ValueError):
                pass
        if item.get("level") is not None:
            try:
                edge["level"] = int(item.get("level"))
            except (TypeError, ValueError):
                pass
        return edge

    @staticmethod
    def _is_internal_display_label(value: Any) -> bool:
        normalized = str(value or "").strip().lower()
        return any(normalized.startswith(prefix) for prefix in INTERNAL_LABEL_PREFIXES)

    def _annotate_payload(self, payload: Dict[str, Any], *, workflow: str, agent_result: Dict[str, Any]) -> Dict[str, Any]:
        next_payload = dict(payload)
        next_payload["generatedAt"] = datetime.now(timezone.utc).isoformat()
        next_payload["generationMode"] = self._workflow_generation_mode(workflow, agent=bool(agent_result.get("completed")))
        next_payload["llmStatus"] = "agent_completed" if agent_result.get("completed") else "agent_unavailable_or_failed"
        return next_payload

    def _build_review_report(
        self,
        payload: Dict[str, Any],
        *,
        agent_result: Dict[str, Any],
        agent_payload: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        entries = payload.get("entries", []) if isinstance(payload.get("entries"), list) else []
        needs_review = [entry.get("id") for entry in entries if isinstance(entry, dict) and entry.get("needsReview")]
        report = agent_payload.get("review") if isinstance(agent_payload, dict) and isinstance(agent_payload.get("review"), dict) else {}
        return {
            "version": 1,
            "reviewedAt": datetime.now(timezone.utc).isoformat(),
            "agentAttempted": bool(agent_result.get("attempted")),
            "agentCompleted": bool(agent_result.get("completed")),
            "traceId": str(agent_result.get("traceId") or ""),
            "issues": report.get("issues") if isinstance(report.get("issues"), list) else [],
            "recommendations": report.get("recommendations") if isinstance(report.get("recommendations"), list) else [],
            "needsReviewEntryIds": needs_review,
            "entryCount": len(entries),
            "nodeCount": len(payload.get("graph", {}).get("nodes", [])),
            "edgeCount": len(payload.get("graph", {}).get("edges", [])),
        }

    def _workflow_generation_mode(self, workflow: str, *, agent: bool) -> str:
        if not agent:
            return "local evidence-grounded"
        return {
            "generate_wiki": "local evidence-grounded + agent prose",
            "update_wiki": "local evidence-grounded + agent prose",
            "refresh_wiki_graph": "local evidence-grounded graph refresh",
            "review_wiki": "local evidence-grounded + agent review",
            "repair_wiki": "local evidence-grounded + agent prose",
        }.get(workflow, "local evidence-grounded + agent prose")

    def _workflow_summary(self, workflow: str, status: str, changed_paths: Sequence[str]) -> str:
        if workflow == "update_wiki":
            return f"\u589e\u91cf\u66f4\u65b0\u5b8c\u6210\uff0c\u68c0\u6d4b\u5230 {len(changed_paths)} \u4e2a\u53d8\u66f4\u6765\u6e90\uff0c\u72b6\u6001: {status}\u3002"
        if workflow == "review_wiki":
            return f"WIKI \u5ba1\u9605\u5b8c\u6210\uff0c\u72b6\u6001: {status}\u3002"
        return f"{workflow} \u5b8c\u6210\uff0c\u72b6\u6001: {status}\u3002"

    def _summary_from_entries(self, entries: Sequence[Dict[str, Any]]) -> str:
        for entry in entries:
            summary = str(entry.get("summary") or "").strip()
            if summary:
                return summary
        return "\u77e5\u8bc6\u56fe\u8c31\u5df2\u66f4\u65b0\u3002"

    def _confidence(self, value: Any) -> float:
        try:
            number = float(value)
        except Exception:
            return 0.68
        return max(0.0, min(1.0, number))

    @staticmethod
    def _pagination_payload(
        *,
        offset: int,
        limit: int,
        returned_count: int,
        total_count: int,
        consumed_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        normalized_offset = max(0, int(offset))
        normalized_limit = max(1, int(limit))
        normalized_total = max(0, int(total_count))
        returned = max(0, int(returned_count))
        consumed = max(0, int(consumed_count if consumed_count is not None else returned))
        has_more = normalized_offset + consumed < normalized_total
        return {
            "offset": normalized_offset,
            "limit": normalized_limit,
            "returnedNodeCount": returned,
            "hasMore": has_more,
            "nextOffset": normalized_offset + consumed if has_more else None,
        }

    @staticmethod
    def _graph_stats(
        nodes: Sequence[Dict[str, Any]],
        edges: Sequence[Dict[str, Any]],
        *,
        entry_count: Optional[int] = None,
    ) -> Dict[str, int]:
        content_nodes = [
            node
            for node in nodes
            if isinstance(node, dict)
            and str(node.get("id") or "")
            and str(node.get("type") or "") != "project"
            and not bool(node.get("synthetic"))
        ]
        node_ids = {str(node.get("id") or "") for node in content_nodes}
        graph_edges = [
            edge
            for edge in edges
            if isinstance(edge, dict)
            and str(edge.get("source") or "")
            and str(edge.get("target") or "")
            and not bool(edge.get("coOccurrence"))
            and not bool(edge.get("synthetic"))
            and str(edge.get("source") or "") in node_ids
            and str(edge.get("target") or "") in node_ids
        ]
        semantic_edges = [
            edge
            for edge in graph_edges
            if str(edge.get("type") or "").strip().lower() in {"relationship", "fact"}
        ]
        confirmed_edges = [
            edge
            for edge in semantic_edges
            if str(edge.get("reviewStatus") or "confirmed") == "confirmed"
            and not bool(edge.get("needsReview"))
        ]
        connected = {
            endpoint
            for edge in confirmed_edges
            for endpoint in (str(edge.get("source") or ""), str(edge.get("target") or ""))
            if endpoint
        }
        isolated = [node for node in content_nodes if str(node.get("id") or "") not in connected]
        return {
            "entryCount": max(0, int(entry_count if entry_count is not None else len(content_nodes))),
            "nodeCount": len(content_nodes),
            "edgeCount": len(semantic_edges),
            "confirmedEdgeCount": len(confirmed_edges),
            "reviewRequiredEdgeCount": sum(
                1 for edge in semantic_edges if str(edge.get("reviewStatus") or "") == "review_required" or bool(edge.get("needsReview"))
            ),
            "connectedNodeCount": len(connected & {str(node.get("id") or "") for node in content_nodes}),
            "isolatedNodeCount": len(isolated),
        }

    def _query_wiki_category_graph(
        self,
        category: str,
        *,
        root: Path,
        normalized_q: str,
        normalized_entry_id: str,
        normalized_node_id: str,
        max_depth: int,
        max_items: int,
        offset: int = 0,
        entries: Sequence[Dict[str, Any]],
        entry_by_id: Dict[str, Dict[str, Any]],
        nodes: Sequence[Dict[str, Any]],
        valid_edges: Sequence[Dict[str, Any]],
        stats_edges: Sequence[Dict[str, Any]] | None = None,
        category_labels: Dict[str, Any],
        allow_agent_relationship_aliases: bool = False,
        include_review: bool = False,
    ) -> Dict[str, Any]:
        if category == "characters":
            return self._query_wiki_relationship_graph(
                category,
                root=root,
                normalized_q=normalized_q,
                normalized_entry_id=normalized_entry_id,
                normalized_node_id=normalized_node_id,
                max_depth=max_depth,
                max_items=max_items,
                offset=offset,
                entries=entries,
                entry_by_id=entry_by_id,
                nodes=nodes,
                valid_edges=valid_edges,
                stats_edges=stats_edges,
                category_labels=category_labels,
                allow_agent_relationship_aliases=allow_agent_relationship_aliases,
                include_review=include_review,
            )
        category_entries = [
            entry
            for entry in entries
            if str(entry.get("category") or "") == category and str(entry.get("id") or "")
        ]
        all_matched_entry_ids = [str(entry.get("id") or "") for entry in category_entries]
        matched_entry_ids = all_matched_entry_ids[max(0, offset) : max(0, offset) + max_items]

        # \u5206\u7c7b\u4e3b\u4f53\u8282\u70b9\uff1acategory \u547d\u4e2d\u6216\u6240\u5c5e\u6761\u76ee category \u547d\u4e2d\uff0c\u4e0d\u518d\u6302\u5206\u7c7b hub\u3002
        primary_nodes: List[Dict[str, Any]] = []
        primary_ids: set[str] = set()
        for node in nodes:
            node_id = str(node.get("id") or "").strip()
            if not node_id or node_id in primary_ids or self._is_wiki_hub_node(node):
                continue
            entry_ref = str(node.get("entryId") or "")
            entry = entry_by_id.get(entry_ref)
            if str(node.get("category") or "") != category and (
                not entry or str(entry.get("category") or "") != category
            ):
                continue
            primary_nodes.append(self._wiki_content_node(node))
            primary_ids.add(node_id)
        all_primary_nodes = list(primary_nodes)
        all_primary_ids = {str(node.get("id") or "") for node in all_primary_nodes}
        primary_nodes = all_primary_nodes[max(0, offset) : max(0, offset) + max_items]
        primary_ids = {str(node.get("id") or "") for node in primary_nodes}

        # \u4e00\u8df3\u8de8\u7c7b\u90bb\u5c45\uff1a\u8865\u5168"\u7ae0\u8282\u91cc\u51fa\u573a\u4e86\u8c01 / \u8bbe\u5b9a\u5173\u8054\u4ec0\u4e48\u4e8b\u4ef6"\u8fd9\u7c7b\u8de8\u7c7b\u4e0a\u4e0b\u6587\uff0c
        # 标记 neighbor=True 供前端弱化渲染。只允许白名单里的节点类型进来，
        # 否则剧情/设定的杂项又会漏进本视图。
        allowed_neighbor_types = CATEGORY_NEIGHBOR_NODE_TYPES.get(category, frozenset())
        node_by_id = {str(node.get("id") or ""): node for node in nodes if str(node.get("id") or "").strip()}
        neighbor_nodes: List[Dict[str, Any]] = []
        neighbor_ids: set[str] = set()
        neighbor_budget = max(0, max_items - len(primary_nodes)) if allowed_neighbor_types else 0
        for edge in valid_edges:
            if len(neighbor_ids) >= neighbor_budget:
                break
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            other = ""
            if source in primary_ids and target not in primary_ids:
                other = target
            elif target in primary_ids and source not in primary_ids:
                other = source
            if not other or other in neighbor_ids:
                continue
            other_node = node_by_id.get(other)
            if not other_node or self._is_wiki_hub_node(other_node):
                continue
            if str(other_node.get("type") or "").strip().lower() not in allowed_neighbor_types:
                continue
            copied = self._wiki_content_node(other_node)
            copied["neighbor"] = True
            neighbor_nodes.append(copied)
            neighbor_ids.add(other)

        visible_node_ids = primary_ids | neighbor_ids
        # \u4fdd\u7559\u4e3b\u4f53\u5185\u90e8\u8fb9\u4e0e\u4e3b\u4f53-\u90bb\u5c45\u8fb9\uff1b\u90bb\u5c45\u4e4b\u95f4\u7684\u8fb9\u88c1\u6389\uff0c\u907f\u514d\u89c6\u56fe\u53d1\u6563\u3002
        visible_edges = [
            edge
            for edge in valid_edges
            if (
                (
                    str(edge.get("source") or "") in visible_node_ids
                    and str(edge.get("target") or "") in visible_node_ids
                )
                or (
                    str(edge.get("type") or "").strip().lower() in {"relationship", "fact"}
                    and str(edge.get("source") or "") in all_primary_ids
                    and str(edge.get("target") or "") in all_primary_ids
                    and (
                        str(edge.get("source") or "") in primary_ids
                        or str(edge.get("target") or "") in primary_ids
                    )
                )
            )
            and (
                str(edge.get("source") or "") in primary_ids
                or str(edge.get("target") or "") in primary_ids
            )
        ][:max_items * 2]

        graph_nodes = [*primary_nodes, *neighbor_nodes][:max_items]

        stats = self._graph_stats(
            [self._wiki_content_node(node) for node in all_primary_nodes],
            stats_edges if stats_edges is not None else valid_edges,
            entry_count=len(all_matched_entry_ids),
        )
        return {
            "mode": "category",
            "query": normalized_q,
            "category": category,
            "entryId": normalized_entry_id,
            "nodeId": normalized_node_id,
            "depth": max_depth,
            "limit": max_items,
            "offset": max(0, offset),
            "includeReview": include_review,
            "returnedNodeCount": len(graph_nodes),
            "hasMore": max(0, offset) + len(primary_nodes) < len(all_primary_nodes),
            "nextOffset": (
                max(0, offset) + len(primary_nodes)
                if max(0, offset) + len(primary_nodes) < len(all_primary_nodes)
                else None
            ),
            "entries": [entry_by_id[entry_ref] for entry_ref in matched_entry_ids if entry_ref in entry_by_id],
            "graph": {
                "nodes": graph_nodes,
                "edges": visible_edges,
            },
            "matchedEntryIds": matched_entry_ids,
            "total": stats,
            "graphStats": stats,
            "pagination": self._pagination_payload(
                offset=max(0, offset),
                limit=max_items,
                returned_count=len(graph_nodes),
                total_count=len(all_primary_nodes),
                consumed_count=len(primary_nodes),
            ),
        }

    def _query_wiki_relationship_graph(
        self,
        category: str,
        *,
        root: Path,
        normalized_q: str,
        normalized_entry_id: str,
        normalized_node_id: str,
        max_depth: int,
        max_items: int,
        offset: int = 0,
        entries: Sequence[Dict[str, Any]],
        entry_by_id: Dict[str, Dict[str, Any]],
        nodes: Sequence[Dict[str, Any]],
        valid_edges: Sequence[Dict[str, Any]],
        stats_edges: Sequence[Dict[str, Any]] | None = None,
        category_labels: Dict[str, Any],
        allow_agent_relationship_aliases: bool = False,
        include_review: bool = False,
    ) -> Dict[str, Any]:
        """角色关系视图只发布已知角色之间、可逐字核对证据的显式关系。"""
        category_entries = [
            entry
            for entry in entries
            if str(entry.get("category") or "") == category and str(entry.get("id") or "")
        ]
        # 被隔离/未解析的角色照样显示（带 needsReview），否则 tab 计数和画布对不上，
        # 用户看到的就是「角色又少了一个」。
        character_nodes = [
            self._wiki_content_node(node)
            for node in nodes
            if str(node.get("id") or "").strip()
            and not self._is_wiki_hub_node(node)
            and str(node.get("type") or "").strip().lower() == "character"
        ]
        node_by_id = {str(node.get("id") or ""): node for node in character_nodes}

        # 持久化的图已经过发布闸门，默认路径只读只过滤，不再为每次查询全量重扫项目。
        # 只有 Agent 别名归一分支才需要回读源文件做逐字核对。
        source_cache = self._collect_sources(root) if allow_agent_relationship_aliases else []
        def collect_grounded(source_edges: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
            collected: List[Dict[str, Any]] = []
            for edge in source_edges:
                if (
                    str(edge.get("source") or "") not in node_by_id
                    or str(edge.get("target") or "") not in node_by_id
                ):
                    continue
                if str(edge.get("type") or "").strip().lower() == "relationship":
                    if self._is_query_visible_relationship_edge(edge):
                        collected.append(edge)
                    continue
                # v3 的通用关系以 auditable fact edge 表示；候选是否显示由
                # includeReview 控制，但完整统计始终基于未过滤集合。
                if (
                    str(edge.get("type") or "").strip().lower() == "fact"
                    and str(edge.get("reviewStatus") or "") in {"confirmed", "review_required"}
                    and str(edge.get("predicate") or edge.get("label") or "").strip()
                ):
                    collected.append(edge)
                    continue
                if not allow_agent_relationship_aliases:
                    continue
                normalized_edge = self._normalize_agent_relationship_alias(
                    root,
                    edge,
                    nodes=character_nodes,
                    sources=source_cache,
                )
                if normalized_edge is not None:
                    collected.append(normalized_edge)
            return collected

        grounded_edges = collect_grounded(valid_edges)
        stats_grounded_edges = collect_grounded(stats_edges if stats_edges is not None else valid_edges)

        if allow_agent_relationship_aliases:
            graph_edges = self._merge_relationship_snapshot_edges(
                root,
                nodes=character_nodes,
                existing_edges=grounded_edges,
                allow_new_nodes=False,
                sources=source_cache,
            )
            stats_grounded_edges = self._merge_relationship_snapshot_edges(
                root,
                nodes=character_nodes,
                existing_edges=stats_grounded_edges,
                allow_new_nodes=False,
                sources=source_cache,
            )
        else:
            graph_edges = grounded_edges

        # 有关系的角色排前面，孤立角色殿后，超出预算的孤立角色裁掉。
        connected_ids: set[str] = set()
        for edge in stats_grounded_edges:
            if str(edge.get("reviewStatus") or "confirmed") != "confirmed" or bool(edge.get("needsReview")):
                continue
            connected_ids.add(str(edge.get("source") or ""))
            connected_ids.add(str(edge.get("target") or ""))
        all_ordered_nodes = [
            *[node for node in character_nodes if str(node.get("id") or "") in connected_ids],
            *[node for node in character_nodes if str(node.get("id") or "") not in connected_ids],
        ]
        ordered_nodes = all_ordered_nodes[max(0, offset) : max(0, offset) + max_items]
        visible_ids = {str(node.get("id") or "") for node in ordered_nodes}
        graph_edges = [
            edge
            for edge in graph_edges
            if (
                str(edge.get("source") or "") in visible_ids
                and str(edge.get("target") or "") in visible_ids
            )
            or (
                str(edge.get("type") or "").strip().lower() in {"relationship", "fact"}
                and str(edge.get("source") or "") in node_by_id
                and str(edge.get("target") or "") in node_by_id
                and (
                    str(edge.get("source") or "") in visible_ids
                    or str(edge.get("target") or "") in visible_ids
                )
            )
        ][: max_items * 2]

        matched_entry_ids: List[str] = []
        for node in ordered_nodes:
            entry_ref = str(node.get("entryId") or "").strip()
            if entry_ref in entry_by_id and entry_ref not in matched_entry_ids:
                matched_entry_ids.append(entry_ref)

        stats = self._graph_stats(
            [self._wiki_content_node(node) for node in all_ordered_nodes],
            stats_grounded_edges,
            entry_count=len(category_entries),
        )
        return {
            "mode": "category",
            "query": normalized_q,
            "category": category,
            "entryId": normalized_entry_id,
            "nodeId": normalized_node_id,
            "depth": max_depth,
            "limit": max_items,
            "offset": max(0, offset),
            "includeReview": include_review,
            "returnedNodeCount": len(ordered_nodes),
            "hasMore": max(0, offset) + len(ordered_nodes) < len(all_ordered_nodes),
            "nextOffset": (
                max(0, offset) + len(ordered_nodes)
                if max(0, offset) + len(ordered_nodes) < len(all_ordered_nodes)
                else None
            ),
            "entries": [entry_by_id[entry_ref] for entry_ref in matched_entry_ids if entry_ref in entry_by_id],
            "graph": {
                "nodes": ordered_nodes,
                "edges": graph_edges,
            },
            "matchedEntryIds": matched_entry_ids,
            "total": stats,
            "graphStats": stats,
            "pagination": self._pagination_payload(
                offset=max(0, offset),
                limit=max_items,
                returned_count=len(ordered_nodes),
                total_count=len(all_ordered_nodes),
            ),
        }

    @staticmethod
    def _allows_agent_relationship_aliases(payload: Dict[str, Any]) -> bool:
        policy = payload.get("graphPolicy") if isinstance(payload.get("graphPolicy"), dict) else {}
        generation_mode = str(payload.get("generationMode") or "").strip().lower().replace("_", "-")
        llm_status = str(payload.get("llmStatus") or "").strip().lower()
        return (
            generation_mode == "agent-evidence-grounded"
            and llm_status == "agent_reviewed"
            and policy.get("agentGraphAccepted") is True
        )

    def _normalize_agent_relationship_alias(
        self,
        root: Path,
        edge: Dict[str, Any],
        *,
        nodes: Sequence[Dict[str, Any]],
        sources: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any] | None:
        """Convert reviewed Agent domain edges into the canonical relationship schema."""
        edge_type = re.sub(r"[\s-]+", "_", str(edge.get("type") or "").strip().lower())
        dimension = AGENT_RELATIONSHIP_EDGE_DIMENSIONS.get(edge_type)
        if not dimension or bool(edge.get("needsReview")):
            return None
        if any(key in edge for key in ("level", "strength", "confidence", "polarity")):
            return None
        evidence, source_path = self._ground_agent_relationship_evidence(
            edge,
            sources=sources,
        )
        if not evidence or not source_path:
            return None
        semantics = semantics_for_dimension(dimension)
        normalized = dict(edge)
        normalized.update({
            "type": "relationship",
            "dimension": semantics.dimension,
            "relationType": semantics.relation_type,
            "status": semantics.status,
            "evidence": evidence,
            "sourcePath": source_path,
            "needsReview": False,
        })
        if not self._is_publishable_relationship_edge(
            root,
            normalized,
            nodes=nodes,
            sources=sources,
        ):
            return None
        return normalized

    def _ground_agent_relationship_evidence(
        self,
        edge: Dict[str, Any],
        *,
        sources: Sequence[Dict[str, Any]],
    ) -> tuple[str, str]:
        """Resolve an Agent line citation to the exact source paragraph it quotes."""
        raw_evidence = re.sub(r"\s+", " ", str(edge.get("evidence") or "")).strip()
        if not raw_evidence:
            return "", ""
        requested_path = str(edge.get("sourcePath") or "").strip().replace("\\", "/")
        candidates = [raw_evidence]
        candidates.extend(
            match.strip()
            for match in re.findall(r"[\"'“‘](.*?)[\"'”’]", raw_evidence)
            if match.strip()
        )
        ordered_sources = [
            *[
                source
                for source in sources
                if str(source.get("relativePath") or "") == requested_path
            ],
            *[
                source
                for source in sources
                if str(source.get("relativePath") or "") != requested_path
            ],
        ]
        for source in ordered_sources:
            if source.get("kind") not in {"chapter", "character"}:
                continue
            paragraphs = [
                paragraph.strip()
                for paragraph in re.split(r"\n\s*\n", str(source.get("text") or ""))
                if paragraph.strip()
            ]
            minimum = 2 if source.get("kind") == "character" else 6
            for candidate in candidates:
                compact_candidate = re.sub(r"\s+", "", candidate)
                if len(compact_candidate) < minimum:
                    continue
                for paragraph in paragraphs:
                    if compact_candidate in re.sub(r"\s+", "", paragraph):
                        return paragraph, str(source.get("relativePath") or "")
        return "", ""

    def _relationship_snapshot_path(self, root: Path) -> Path:
        return root / ".storydex" / "memory" / "current" / "relationship_graph.json"

    def _grounded_evidence_source_path(
        self,
        root: Path,
        *,
        evidence: Any,
        requested_path: Any = "",
        sources: Sequence[Dict[str, Any]] | None = None,
    ) -> str:
        needle = re.sub(r"\s+", " ", str(evidence or "")).strip().strip("`'\"“”‘’")
        if not needle:
            return ""
        available = list(sources) if sources is not None else self._collect_sources(root)
        available = [
            source
            for source in available
            if source.get("kind") in {"chapter", "character"}
        ]
        normalized_requested = str(requested_path or "").strip().replace("\\", "/")
        ordered = [
            *[
                source
                for source in available
                if str(source.get("relativePath") or "") == normalized_requested
            ],
            *[
                source
                for source in available
                if str(source.get("relativePath") or "") != normalized_requested
            ],
        ]
        compact_needle = re.sub(r"\s+", "", needle)
        for source in ordered:
            minimum = 2 if source.get("kind") == "character" else 6
            if len(compact_needle) < minimum:
                continue
            compact_source = re.sub(r"\s+", "", str(source.get("text") or ""))
            if compact_needle in compact_source:
                return str(source.get("relativePath") or "")
        return ""

    def _edge_evidence_anchors_endpoints(
        self,
        root: Path,
        edge: Dict[str, Any],
        *,
        nodes: Sequence[Dict[str, Any]],
        sources: Sequence[Dict[str, Any]],
    ) -> bool:
        source_id = str(edge.get("source") or "").strip()
        target_id = str(edge.get("target") or "").strip()
        endpoint_ids = {source_id, target_id}
        if len(endpoint_ids) != 2:
            return False

        grounded_path = self._grounded_evidence_source_path(
            root,
            evidence=edge.get("evidence"),
            requested_path=edge.get("sourcePath"),
            sources=sources,
        )
        if not grounded_path:
            return False
        source_document = next(
            (
                item
                for item in sources
                if str(item.get("relativePath") or "") == grounded_path
            ),
            None,
        )
        if source_document is None:
            return False

        names_by_id: Dict[str, set[str]] = {endpoint_id: set() for endpoint_id in endpoint_ids}
        label_candidates: Dict[str, set[str]] = {}
        for node in nodes:
            node_id = str(node.get("id") or "").strip()
            label = str(node.get("label") or "").strip()
            if node_id not in endpoint_ids or not label:
                continue
            names_by_id[node_id].add(label)
            label_candidates.setdefault(label, set()).add(node_id)

        for record in EntityRegistry(root).load_records():
            node_id = ""
            if record.entity_id in endpoint_ids:
                node_id = record.entity_id
            else:
                candidates = label_candidates.get(record.canonical_name, set())
                if len(candidates) == 1:
                    node_id = next(iter(candidates))
            if node_id:
                names_by_id[node_id].update(record.names())

        def contains_name(text: str, name: str) -> bool:
            normalized_name = re.sub(r"\s+", " ", str(name or "")).strip()
            if len(re.sub(r"\s+", "", normalized_name)) < 2:
                return False
            if re.fullmatch(r"[A-Za-z0-9_ -]+", normalized_name):
                return bool(re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(normalized_name)}(?![A-Za-z0-9_])",
                    text,
                    flags=re.IGNORECASE,
                ))
            return re.sub(r"\s+", "", normalized_name) in re.sub(r"\s+", "", text)

        evidence = re.sub(r"\s+", " ", str(edge.get("evidence") or "")).strip().strip("`'\"“”‘’")
        if source_document.get("kind") == "chapter":
            return all(
                any(contains_name(evidence, name) for name in names_by_id[endpoint_id])
                for endpoint_id in endpoint_ids
            )

        if source_document.get("kind") != "character":
            return False
        card_endpoint_ids: set[str] = set()
        stable_card_id = self._stable_entity_id_from_source(source_document)
        if stable_card_id in endpoint_ids:
            card_endpoint_ids.add(stable_card_id)
        for card_name in self._character_names_from_source(source_document):
            card_endpoint_ids.update(
                endpoint_id
                for endpoint_id, names in names_by_id.items()
                if card_name in names
            )
        if len(card_endpoint_ids) != 1:
            return False
        card_endpoint_id = next(iter(card_endpoint_ids))
        other_endpoint_id = target_id if card_endpoint_id == source_id else source_id
        other_names = {*names_by_id[other_endpoint_id], other_endpoint_id}
        compact_evidence = re.sub(r"\s+", "", evidence)
        for raw_line in str(source_document.get("text") or "").splitlines():
            if compact_evidence not in re.sub(r"\s+", "", raw_line):
                continue
            if any(contains_name(raw_line, name) for name in other_names):
                return True
        return False

    def _formal_relation_edge_is_grounded(
        self,
        root: Path,
        edge: Dict[str, Any],
        *,
        nodes: Sequence[Dict[str, Any]],
    ) -> bool:
        """Validate one-direction formal Markdown evidence.

        A canonical relation line lives only in the subject's file, so the
        quote naturally names the predicate and object but not necessarily the
        subject.  The subject is anchored by ownership of that source file (or
        a JSON character card sidecar frontmatter binding).
        """
        source_id = str(edge.get("source") or "").strip()
        target_id = str(edge.get("target") or "").strip()
        predicate = str(edge.get("predicate") or edge.get("label") or "").strip()
        if not source_id or not target_id or source_id == target_id or not predicate:
            return False
        refs = edge.get("sourceRefs") if isinstance(edge.get("sourceRefs"), list) else []
        formal_refs = [
            ref
            for ref in refs
            if isinstance(ref, dict) and str(ref.get("role") or "") == "formal_relation"
        ]
        if not formal_refs:
            return False
        try:
            from services.story_knowledge_relation_service import (
                FORMAL_RELATION_PREFIXES,
                get_story_knowledge_relation_service,
            )

            relation_service = get_story_knowledge_relation_service()
            entities = relation_service.load_entities(root).get("entities", [])
        except Exception:
            return False
        entity_by_id = {
            str(item.get("entityId") or ""): item
            for item in entities
            if isinstance(item, dict) and str(item.get("entityId") or "")
        }
        subject = entity_by_id.get(source_id)
        obj = entity_by_id.get(target_id)
        if not isinstance(subject, dict) or not isinstance(obj, dict):
            return False
        subject_paths = {
            str(path).replace("\\", "/").lstrip("/")
            for path in subject.get("sourcePaths", [])
            if str(path).strip()
        }
        expanded_subject_paths = set(subject_paths)
        expanded_subject_paths.update(
            str(Path(path).with_suffix(".md")).replace("\\", "/")
            for path in subject_paths
            if Path(path).suffix.lower() == ".txt"
        )
        object_names = {
            str(obj.get("canonical_name") or "").strip(),
            *{
                str(alias).strip()
                for alias in obj.get("aliases", [])
                if str(alias).strip()
            },
        }
        node_label = next(
            (
                str(node.get("label") or "").strip()
                for node in nodes
                if str(node.get("id") or "").strip() == target_id
            ),
            "",
        )
        if node_label:
            object_names.add(node_label)

        resolved_root = root.resolve()
        for ref in formal_refs:
            relative = str(ref.get("path") or "").strip().replace("\\", "/").lstrip("/")
            if not relative or not any(relative.startswith(prefix) for prefix in FORMAL_RELATION_PREFIXES):
                continue
            path = (resolved_root / relative).resolve()
            try:
                path.relative_to(resolved_root)
            except ValueError:
                continue
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                continue
            quote = str(ref.get("quote") or "")
            if not quote or quote not in content or predicate not in quote:
                continue
            owns_source = relative in expanded_subject_paths
            if not owns_source and relative.lower().endswith(".relations.md"):
                owns_source = relation_service._frontmatter_entity_id(content) == source_id  # noqa: SLF001
            if not owns_source:
                continue
            if any(name and name in quote for name in object_names):
                return True
        return False

    @staticmethod
    def _unsupported_relationship_metrics(edge: Dict[str, Any]) -> List[str]:
        """Separate forbidden social-strength metrics from v3 audit confidence."""

        unsupported = [
            key
            for key in ("level", "strength", "polarity")
            if key in edge
        ]
        review_status = str(edge.get("reviewStatus") or "").strip()
        source_refs = edge.get("sourceRefs") if isinstance(edge.get("sourceRefs"), list) else []
        if (
            "confidence" in edge
            and (
                review_status not in {"confirmed", "review_required"}
                or not any(isinstance(ref, dict) for ref in source_refs)
            )
        ):
            unsupported.append("confidence")
        return unsupported

    @staticmethod
    def _is_query_visible_relationship_edge(edge: Dict[str, Any]) -> bool:
        """查询路径上的关系边过滤：只做不读盘的结构判断。

        证据的逐字核对已经在发布闸门里做过，这里再做一遍只会让每次查询全量重扫项目。
        """
        if str(edge.get("type") or "") != "relationship":
            return True
        if edge.get("coOccurrence"):
            return False
        relation_type = str(edge.get("relationType") or "").strip()
        if relation_type not in ALLOWED_RELATION_TYPES or relation_type == "unknown":
            return False
        if str(edge.get("reviewStatus") or "confirmed").strip() != "confirmed":
            return False
        if str(edge.get("status") or "").strip() != "asserted" or bool(edge.get("needsReview")):
            return False
        return not StoryWikiService._unsupported_relationship_metrics(edge)

    def _is_publishable_relationship_edge(
        self,
        root: Path,
        edge: Dict[str, Any],
        *,
        nodes: Sequence[Dict[str, Any]],
        sources: Sequence[Dict[str, Any]] | None = None,
    ) -> bool:
        if str(edge.get("type") or "") != "relationship":
            return True
        if edge.get("coOccurrence"):
            return False
        relation_type = str(edge.get("relationType") or "").strip()
        status = str(edge.get("status") or "").strip()
        if relation_type not in ALLOWED_RELATION_TYPES or relation_type == "unknown":
            return False
        if str(edge.get("reviewStatus") or "confirmed").strip() != "confirmed":
            return False
        if status != "asserted" or bool(edge.get("needsReview")):
            return False
        if self._unsupported_relationship_metrics(edge):
            return False
        source_documents = list(sources) if sources is not None else self._collect_sources(root)
        return self._edge_evidence_anchors_endpoints(
            root,
            edge,
            nodes=nodes,
            sources=source_documents,
        )

    @staticmethod
    def _relationship_lines_from_character_source(source: Dict[str, Any]) -> List[str]:
        lines: List[str] = []
        in_relationship_section = False
        for raw_line in str(source.get("text") or "").splitlines():
            heading = re.match(r"^\s*(#{1,6})\s+(.+?)\s*$", raw_line)
            if heading:
                level = len(heading.group(1))
                title = re.sub(r"[*_`]+", "", heading.group(2)).strip().lower()
                if level <= 2:
                    in_relationship_section = any(
                        token in title
                        for token in ("关系网络", "人物关系", "角色关系", "relationships")
                    )
                continue
            if in_relationship_section and re.match(r"^\s*[-*+•]\s+", raw_line):
                lines.append(raw_line)
        return lines

    def _append_character_card_relationship_edges(
        self,
        *,
        sources: Sequence[Dict[str, Any]],
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
    ) -> None:
        endpoint_candidates: Dict[str, set[str]] = {}

        def register_endpoint(name: Any, node_id: str) -> None:
            normalized = str(name or "").strip()
            if normalized and node_id:
                endpoint_candidates.setdefault(normalized, set()).add(node_id)

        def resolve_endpoint(name: Any) -> str:
            candidates = endpoint_candidates.get(str(name or "").strip(), set())
            return next(iter(candidates)) if len(candidates) == 1 else ""

        for node in nodes:
            node_id = str(node.get("id") or "").strip()
            label = str(node.get("label") or "").strip()
            if not node_id:
                continue
            register_endpoint(node_id, node_id)
            register_endpoint(label, node_id)

        seen_relations = {
            (
                *sorted((str(edge.get("source") or ""), str(edge.get("target") or ""))),
                str(edge.get("relationType") or edge.get("dimension") or "unknown"),
            )
            for edge in edges
            if str(edge.get("source") or "") and str(edge.get("target") or "")
        }
        for source in sources:
            stable_source_id = self._stable_entity_id_from_source(source)
            if stable_source_id:
                source_id = resolve_endpoint(stable_source_id)
            else:
                source_names = self._character_names_from_source(source)
                source_id = ""
                for name in source_names:
                    source_id = resolve_endpoint(name)
                    if source_id:
                        break
            if not source_id:
                continue
            for line in self._relationship_lines_from_character_source(source):
                statement = parse_relationship_markdown(line)
                if statement is None:
                    continue
                target_id = resolve_endpoint(
                    statement.stable_target
                    if statement.stable_target
                    else statement.display_target
                )
                if not target_id or source_id == target_id:
                    continue
                semantics = statement.semantics
                if semantics.status != "asserted" or semantics.relation_type == "unknown":
                    continue
                relation_key = (
                    *sorted((source_id, target_id)),
                    semantics.relation_type,
                )
                if relation_key in seen_relations:
                    continue
                seen_relations.add(relation_key)
                relationship_edge = {
                    "source": source_id,
                    "target": target_id,
                    "label": RELATIONSHIP_DIMENSION_LABELS.get(
                        semantics.dimension,
                        semantics.relation_type,
                    ),
                    "type": "relationship",
                    "weight": 1,
                    "dimension": semantics.dimension,
                    "relationType": semantics.relation_type,
                    "status": "asserted",
                    "evidence": statement.detail,
                    "sourcePath": str(source.get("relativePath") or ""),
                    "needsReview": False,
                }
                edges.append(relationship_edge)

    def _merge_relationship_snapshot_edges(
        self,
        root: Path,
        *,
        nodes: List[Dict[str, Any]],
        existing_edges: List[Dict[str, Any]],
        allow_new_nodes: bool,
        sources: Sequence[Dict[str, Any]] | None = None,
    ) -> List[Dict[str, Any]]:
        """Merge only known endpoints and relationships backed by verbatim project evidence."""
        del allow_new_nodes
        snapshot_path = self._relationship_snapshot_path(root)
        raw_edges: List[Any] = []
        if snapshot_path.exists():
            try:
                loaded = json.loads(snapshot_path.read_text(encoding="utf-8"))
            except Exception:
                loaded = None
            loaded_edges = loaded.get("edges") if isinstance(loaded, dict) else None
            if isinstance(loaded_edges, list):
                raw_edges = loaded_edges
        all_sources = list(sources) if sources is not None else self._collect_sources(root)
        character_sources = [
            source
            for source in all_sources
            if source.get("kind") == "character"
        ]
        if not raw_edges and not character_sources:
            return existing_edges

        endpoint_candidates: Dict[str, set[str]] = {}

        def register_endpoint(name: Any, node_id: str) -> None:
            normalized = str(name or "").strip()
            if normalized and node_id:
                endpoint_candidates.setdefault(normalized, set()).add(node_id)

        for node in nodes:
            node_id = str(node.get("id") or "")
            label = str(node.get("label") or "").strip()
            if node_id:
                register_endpoint(node_id, node_id)
                register_endpoint(label, node_id)

        known_node_ids = {
            str(node.get("id") or "").strip()
            for node in nodes
            if str(node.get("id") or "").strip()
        }
        for record in EntityRegistry(root).load_records():
            canonical_candidates = endpoint_candidates.get(record.canonical_name, set())
            node_id = (
                record.entity_id
                if record.entity_id in known_node_ids
                else next(iter(canonical_candidates))
                if len(canonical_candidates) == 1
                else ""
            )
            if not node_id:
                continue
            for name in record.names():
                register_endpoint(name, node_id)

        def resolve_endpoint(raw_name: Any) -> str:
            name = str(raw_name or "").strip()
            if not name:
                return ""
            candidates = endpoint_candidates.get(name, set())
            return next(iter(candidates)) if len(candidates) == 1 else ""

        merged = [
            edge
            for edge in existing_edges
            if isinstance(edge, dict)
            and self._is_publishable_relationship_edge(
                root,
                edge,
                nodes=nodes,
                sources=all_sources,
            )
        ]
        seen_keys = {
            (
                *sorted((str(edge.get("source") or ""), str(edge.get("target") or ""))),
                str(edge.get("relationType") or edge.get("dimension") or ""),
            )
            for edge in merged
            if isinstance(edge, dict)
        }
        for raw_edge in raw_edges:
            if not isinstance(raw_edge, dict):
                continue
            history = raw_edge.get("history") if isinstance(raw_edge.get("history"), list) else []
            latest = next((item for item in reversed(history) if isinstance(item, dict)), {})
            review_status = str(
                raw_edge.get("reviewStatus")
                or latest.get("reviewStatus")
                or "confirmed"
            ).strip()
            # Dynamic relationship snapshots are authoritative projection inputs
            # only after confirmation. Review candidates live in the dedicated
            # relation ledger, while rejected/superseded records remain audit-only.
            if review_status != "confirmed":
                continue
            source = resolve_endpoint(raw_edge.get("sourceId") or raw_edge.get("source"))
            target = resolve_endpoint(raw_edge.get("targetId") or raw_edge.get("target"))
            if not source or not target or source == target:
                continue
            dimension = str(raw_edge.get("dimension") or "").strip().lower()
            semantics = semantics_for_dimension(dimension)
            if semantics.status != "asserted" or semantics.relation_type == "unknown":
                continue
            raw_status = str(raw_edge.get("status") or "asserted").strip()
            if raw_status != "asserted":
                continue
            raw_source_refs = (
                latest.get("sourceRefs")
                if isinstance(latest.get("sourceRefs"), list)
                else raw_edge.get("sourceRefs")
                if isinstance(raw_edge.get("sourceRefs"), list)
                else []
            )
            grounded_refs: List[Dict[str, Any]] = []
            evidence = ""
            source_path = ""
            for raw_ref in raw_source_refs:
                if not isinstance(raw_ref, dict):
                    continue
                quote = str(raw_ref.get("quote") or raw_ref.get("evidence") or "").strip()
                requested_path = str(raw_ref.get("path") or raw_ref.get("sourcePath") or "").strip()
                grounded_path = self._grounded_evidence_source_path(
                    root,
                    evidence=quote,
                    requested_path=requested_path,
                    sources=all_sources,
                )
                if not grounded_path:
                    continue
                candidate_edge = {
                    "source": source,
                    "target": target,
                    "evidence": quote,
                    "sourcePath": grounded_path,
                }
                if not self._edge_evidence_anchors_endpoints(
                    root,
                    candidate_edge,
                    nodes=nodes,
                    sources=all_sources,
                ):
                    continue
                normalized_ref = dict(raw_ref)
                normalized_ref["path"] = grounded_path
                normalized_ref["quote"] = quote
                normalized_ref.setdefault("role", "dynamic_relationship")
                if normalized_ref not in grounded_refs:
                    grounded_refs.append(normalized_ref)
                if not evidence:
                    evidence = quote
                    source_path = grounded_path

            if not evidence:
                evidence = str(
                    latest.get("evidence")
                    or latest.get("detail")
                    or raw_edge.get("evidence")
                    or raw_edge.get("detail")
                    or ""
                ).strip()
                requested_path = str(
                    latest.get("last_updated_in")
                    or raw_edge.get("last_updated_in")
                    or ""
                ).strip()
                source_path = self._grounded_evidence_source_path(
                    root,
                    evidence=evidence,
                    requested_path=requested_path,
                    sources=all_sources,
                )
                if not source_path:
                    continue
                grounded_refs = [{
                    "path": source_path,
                    "quote": evidence,
                    "role": "dynamic_relationship",
                }]
            relation_type = semantics.relation_type
            key = (*sorted((source, target)), relation_type)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            knowledge_status = str(raw_edge.get("knowledgeStatus") or "observed").strip()
            if knowledge_status not in KNOWLEDGE_STATUSES:
                knowledge_status = "observed"
            provenance = (
                dict(raw_edge.get("provenance"))
                if isinstance(raw_edge.get("provenance"), dict)
                else dict(latest.get("provenance"))
                if isinstance(latest.get("provenance"), dict)
                else {}
            )
            provenance.setdefault("origin", "dynamic_relationship_graph")
            provenance.setdefault("extractorVersion", "storydex-relationship-graph-v1")
            trace_id = str(raw_edge.get("traceId") or latest.get("traceId") or "").strip()
            relation_key = f"{'|'.join(sorted((source, target)))}|{relation_type}"
            fingerprint = str(raw_edge.get("fingerprint") or "").strip() or sha256(
                json.dumps(
                    {
                        "relationKey": relation_key,
                        "reviewStatus": review_status,
                        "knowledgeStatus": knowledge_status,
                        "sourceRefs": grounded_refs,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            relationship_edge = {
                "id": str(raw_edge.get("id") or "").strip()
                or f"relationship:{sha256(relation_key.encode('utf-8')).hexdigest()[:24]}",
                "source": source,
                "target": target,
                "label": RELATIONSHIP_DIMENSION_LABELS[dimension],
                "predicate": relation_type,
                "type": "relationship",
                "weight": 1,
                "dimension": dimension,
                "relationType": relation_type,
                "status": "asserted",
                "reviewStatus": review_status,
                "knowledgeStatus": knowledge_status,
                "confidence": raw_edge.get("confidence", "confirmed"),
                "sourceRefs": grounded_refs,
                "provenance": provenance,
                "traceId": trace_id,
                "fingerprint": fingerprint,
                "evidence": evidence,
                "sourcePath": source_path,
                "needsReview": False,
            }
            if not self._edge_evidence_anchors_endpoints(
                root,
                relationship_edge,
                nodes=nodes,
                sources=all_sources,
            ):
                continue
            merged.append(relationship_edge)
        self._append_character_card_relationship_edges(
            sources=character_sources,
            nodes=nodes,
            edges=merged,
        )
        return merged

    @staticmethod
    def _wiki_category_label(category: str, category_labels: Dict[str, Any]) -> str:
        value = category_labels.get(category) if isinstance(category_labels, dict) else None
        return str(value or CATEGORY_LABELS.get(category, category))

    @staticmethod
    def _wiki_content_node(node: Dict[str, Any]) -> Dict[str, Any]:
        copied = dict(node)
        copied["entryId"] = str(copied.get("entryId") or "")
        copied.setdefault("synthetic", False)
        copied.setdefault("selectable", True)
        return copied

    def _wiki_edge_touches_hub(self, edge: Dict[str, Any], node_by_id: Dict[str, Dict[str, Any]]) -> bool:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        return self._is_wiki_hub_node(node_by_id.get(source, {})) or self._is_wiki_hub_node(node_by_id.get(target, {}))

    @staticmethod
    def _safe_int(value: Any, *, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _query_tokens(query: str) -> List[str]:
        tokens = [token.strip().lower() for token in str(query or "").split() if token.strip()]
        return tokens or ([str(query or "").strip().lower()] if str(query or "").strip() else [])

    @classmethod
    def _wiki_entry_matches(cls, entry: Dict[str, Any], tokens: Sequence[str]) -> bool:
        details = entry.get("details") if isinstance(entry.get("details"), list) else []
        sources = entry.get("sourcePaths") if isinstance(entry.get("sourcePaths"), list) else []
        return cls._wiki_text_matches(
            [
                entry.get("id"),
                entry.get("title"),
                entry.get("category"),
                entry.get("categoryLabel"),
                entry.get("summary"),
                *details,
                *sources,
            ],
            tokens,
        )

    @classmethod
    def _wiki_node_matches(cls, node: Dict[str, Any], tokens: Sequence[str]) -> bool:
        return cls._wiki_text_matches(
            [
                node.get("id"),
                node.get("label"),
                node.get("type"),
                node.get("category"),
                node.get("entryId"),
                node.get("summary"),
            ],
            tokens,
        )

    @classmethod
    def _wiki_edge_matches(cls, edge: Dict[str, Any], tokens: Sequence[str]) -> bool:
        return cls._wiki_text_matches(
            [
                edge.get("source"),
                edge.get("target"),
                edge.get("label"),
                edge.get("type"),
                edge.get("evidence"),
            ],
            tokens,
        )

    @staticmethod
    def _wiki_text_matches(values: Sequence[Any], tokens: Sequence[str]) -> bool:
        if not tokens:
            return False
        haystack = " ".join(str(value or "") for value in values).lower()
        return all(token in haystack for token in tokens)

    def _expand_wiki_node_neighborhood(
        self,
        seed_node_ids: Iterable[str],
        *,
        node_by_id: Dict[str, Dict[str, Any]],
        edges: Sequence[Dict[str, Any]],
        depth: int,
    ) -> set[str]:
        selected = {node_id for node_id in seed_node_ids if node_id in node_by_id}
        frontier = set(selected)
        max_depth = max(1, min(2, int(depth or 1)))
        for _hop in range(max_depth):
            next_frontier: set[str] = set()
            expanding = {
                node_id
                for node_id in frontier
                if not self._is_wiki_hub_node(node_by_id.get(node_id, {}))
            }
            if not expanding:
                break
            for edge in edges:
                source = str(edge.get("source") or "")
                target = str(edge.get("target") or "")
                if source in expanding and target in node_by_id and target not in selected:
                    next_frontier.add(target)
                if target in expanding and source in node_by_id and source not in selected:
                    next_frontier.add(source)
            selected.update(next_frontier)
            frontier = next_frontier
            if not frontier:
                break
        return selected

    @staticmethod
    def _is_wiki_hub_node(node: Dict[str, Any]) -> bool:
        node_id = str(node.get("id") or "")
        category = str(node.get("category") or "")
        node_type = str(node.get("type") or "")
        role = str(node.get("role") or "")
        return (
            node_id == "project:root"
            or category in {"overview", "index"}
            or node_type == "project"
            or role in {"projectHub", "categoryHub"}
        )

    def _new_trace_id(self, workflow: str) -> str:
        return f"wiki-{workflow}-{uuid4()}"

    def _collect_sources(self, root: Path) -> List[Dict[str, Any]]:
        sources: List[Dict[str, Any]] = []
        if not root.exists():
            return sources
        for path in root.rglob("*"):
            source = self._source_from_path(root, path)
            if source is not None:
                sources.append(source)
        return sorted(sources, key=lambda item: self._source_sort_key(str(item["relativePath"])))

    def _collect_character_sources(self, root: Path) -> List[Dict[str, Any]]:
        """实体归并只读取规范/兼容角色卡目录，避免重复全库源扫描。"""
        sources: List[Dict[str, Any]] = []
        character_roots = (root / ".storydex" / "characters", root / "characters")
        for character_root in character_roots:
            if not character_root.is_dir():
                continue
            candidates = [path for path in character_root.iterdir() if path.is_file()]
            cards_root = character_root / "cards"
            if cards_root.is_dir():
                candidates.extend(path for path in cards_root.iterdir() if path.is_file())
            for path in candidates:
                source = self._source_from_path(root, path)
                if source is not None and source.get("kind") == "character":
                    sources.append(source)
        return sorted(sources, key=lambda item: self._source_sort_key(str(item["relativePath"])))

    def _source_from_path(self, root: Path, path: Path) -> Dict[str, Any] | None:
        if not path.is_file() or path.suffix.lower() not in SCAN_SUFFIXES:
            return None
        try:
            relative = path.relative_to(root)
        except ValueError:
            return None
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            return None
        rel = relative.as_posix()
        if self._should_skip_source_path(rel):
            return None
        text = self._read_source_text(path)
        if not text:
            return None
        stat = path.stat()
        return {
            "relativePath": rel,
            "title": path.stem,
            "kind": self._source_kind(rel),
            "text": text,
            "size": stat.st_size,
            "sha256": sha256(text.encode("utf-8", errors="ignore")).hexdigest(),
            "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        }

    def _should_skip_source_path(self, relative_path: str) -> bool:
        normalized = relative_path.replace("\\", "/")
        normalized_lower = normalized.lower()
        if Path(normalized).name.lower() == "readme.md":
            return True
        # JSON 角色卡的 .relations.md 是正式关系 sidecar，不是独立角色实体。
        # 关系领域服务会按 frontmatter 的 entityId 读取它，WIKI 源扫描必须排除，
        # 否则同一角色会被投影成重复节点。
        if normalized_lower.endswith(".relations.md"):
            return True
        return any(normalized_lower.startswith(prefix) for prefix in EXCLUDED_RELATIVE_PREFIXES)

    def _read_source_text(self, path: Path) -> str:
        try:
            if path.suffix.lower() == ".json":
                data = json.loads(path.read_text(encoding="utf-8"))
                return json.dumps(data, ensure_ascii=False, indent=2)
            return path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            try:
                return path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                return ""

    def _source_kind(self, relative_path: str) -> str:
        normalized = relative_path.replace("\\", "/")
        if normalized.startswith("chapters/") and Path(normalized).suffix.lower() in TEXT_SUFFIXES:
            return "chapter"
        if normalized.startswith(".storydex/scripts/") and Path(normalized).suffix.lower() in SCAN_SUFFIXES:
            return "planned"
        if "/templates/" in f"/{normalized}/":
            return "project"
        if self._is_character_card_path(normalized):
            return "character"
        if "/characters/" in f"/{normalized}":
            # characters/states/<id>.json 这类派生文件只有 id、没有 name，
            # 被当成角色卡会顶掉角色的真实中文名（甚至让角色整个消失）。
            return "memory"
        if "/worldbook/" in f"/{normalized}":
            return "world"
        if "/presets/" in f"/{normalized}":
            return "preset"
        if "/memory/" in f"/{normalized}":
            return "memory"
        return "project"

    @staticmethod
    def _is_character_card_path(relative_path: str) -> bool:
        """角色卡白名单：`characters/<名字>.{md,txt,json}` 与 `characters/cards/<id>.*`。

        白名单而非黑名单：characters/ 下将来新增的任何派生目录都不会被误当成角色卡。
        """
        normalized = str(relative_path or "").replace("\\", "/")
        if normalized.lower().endswith(".relations.md"):
            return False
        parts = [part for part in normalized.split("/") if part]
        if "characters" not in parts:
            return False
        tail = parts[parts.index("characters") + 1:]
        if len(tail) == 1:
            return Path(tail[0]).suffix.lower() in SCAN_SUFFIXES
        return len(tail) == 2 and tail[0] == "cards"

    def _source_sort_key(self, value: str) -> List[Any]:
        parts: List[Any] = []
        for token in re.split(r"(\d+)", value):
            if token.isdigit():
                parts.append((0, int(token)))
            else:
                parts.append((1, token.lower()))
        return parts

    def _reconcile_entity_registry(self, root: Path) -> None:
        """把角色卡身份归并到 canonical registry，保证改名不更换 entityId。"""
        registry_path = root / ENTITY_SOURCE_PATH
        character_sources = self._collect_character_sources(root)
        if not character_sources and not registry_path.exists():
            return

        try:
            loaded = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else {}
        except Exception:
            loaded = {}
        payload = dict(loaded) if isinstance(loaded, dict) else {}
        raw_entities = payload.get("entities") if isinstance(payload.get("entities"), list) else []
        entities = [dict(item) for item in raw_entities if isinstance(item, dict)]

        def record_id(item: Dict[str, Any]) -> str:
            return str(
                item.get("entityId")
                or item.get("entity_id")
                or item.get("stableId")
                or item.get("stable_id")
                or item.get("id")
                or ""
            ).strip()

        def record_name(item: Dict[str, Any]) -> str:
            return str(item.get("canonical_name") or item.get("canonicalName") or item.get("name") or "").strip()

        def record_source_paths(item: Dict[str, Any]) -> List[str]:
            raw_paths = (
                item.get("sourcePaths")
                if isinstance(item.get("sourcePaths"), list)
                else item.get("source_paths")
                if isinstance(item.get("source_paths"), list)
                else []
            )
            return list(
                dict.fromkeys(
                    normalized
                    for normalized in (str(path).strip().replace("\\", "/") for path in raw_paths)
                    if normalized
                )
            )

        def is_character_card_path(relative_path: str) -> bool:
            # 与 _source_kind 用同一套角色卡白名单，否则根级 characters/ 布局下
            # 删卡不会归档，而 states/ 这类派生文件反倒会被当成卡。
            return self._is_character_card_path(relative_path)

        has_card_managed_record = any(
            any(is_character_card_path(path) for path in record_source_paths(item))
            for item in entities
        )
        if not character_sources and not has_card_managed_record:
            return

        # 一张卡认领一条记录，绝不从别的卡手里夺取：撞 id 的第二张卡另建记录，
        # 否则第二章写完时第一个角色会被静默改名塌成同一个人。
        claimed_record_ids: set[int] = set()
        taken_entity_ids: set[str] = set()

        def find_record(predicate: Callable[[Dict[str, Any]], bool]) -> Dict[str, Any] | None:
            return next(
                (
                    item
                    for item in entities
                    if id(item) not in claimed_record_ids and predicate(item)
                ),
                None,
            )

        for source in character_sources:
            names = self._character_names_from_source(source)
            relative_path = str(source.get("relativePath") or "")
            stable_id = self._stable_entity_id_from_source(source)
            match: Dict[str, Any] | None = None
            if stable_id:
                match = find_record(lambda item: record_id(item) == stable_id)
            if match is None and relative_path:
                match = find_record(lambda item: relative_path in record_source_paths(item))
            if match is None and names:
                display = names[0]
                match = find_record(
                    lambda item: display == record_name(item)
                    or display in [str(alias).strip() for alias in item.get("aliases", [])]
                )
            if match is None:
                if not names:
                    # 全新卡且认不出名字：不凭空造实体（正文人名抽取不在本次范围内）。
                    continue
                match = {}
                entities.append(match)
            claimed_record_ids.add(id(match))

            previous_name = record_name(match)
            # 认不出名字时保留既有 canonical_name：识别失败不等于角色不存在。
            display_name = names[0] if names else previous_name
            id_conflict_with = ""
            resolved_id = stable_id or record_id(match)
            if resolved_id and resolved_id in taken_entity_ids:
                # 撞车的一方改用路径派生的确定性 id（冷重建可复现，不用 uuid4）。
                id_conflict_with = resolved_id
                resolved_id = ""
            if not resolved_id:
                resolved_id = self._path_derived_entity_id(relative_path)
            taken_entity_ids.add(resolved_id)

            aliases = [str(alias).strip() for alias in match.get("aliases", []) if str(alias).strip()]
            if previous_name and previous_name != display_name:
                aliases.append(previous_name)
            aliases.extend(name for name in names[1:] if name != display_name)
            source_paths = [
                path
                for path in record_source_paths(match)
                if (root / path).exists()
            ]
            if relative_path:
                source_paths.append(relative_path)
            match.update({
                "entityId": resolved_id,
                "canonical_name": display_name,
                "aliases": list(dict.fromkeys(alias for alias in aliases if alias and alias != display_name)),
                "kind": "character",
                "status": "active",
                "sourcePaths": list(dict.fromkeys(source_paths)),
                "needsReview": bool(id_conflict_with) or not names or not display_name,
            })
            if id_conflict_with:
                match["idConflictWith"] = id_conflict_with
            else:
                match.pop("idConflictWith", None)
            match.pop("source_paths", None)

        # 归档只看文件在不在：卡还在盘上但本轮没认出名字/被别的记录认领，角色必须留下。
        # 纯 registry 角色没有角色卡路径，仍由 Story Knowledge 自身管理。
        for item in entities:
            if id(item) in claimed_record_ids:
                continue
            source_paths = record_source_paths(item)
            card_paths = [path for path in source_paths if is_character_card_path(path)]
            if not card_paths:
                continue
            kind = str(item.get("kind") or "").strip().lower()
            if kind and kind not in {"character", "person", "role"}:
                continue
            surviving_cards = [path for path in card_paths if (root / path).exists()]
            item.pop("source_paths", None)
            if surviving_cards:
                # 卡还在，只是这一轮没认出来：保留 active 与原名，标记待人工确认。
                item["status"] = "active"
                item["needsReview"] = True
                item["sourcePaths"] = [
                    path
                    for path in source_paths
                    if not is_character_card_path(path) or path in surviving_cards
                ]
                continue
            item["status"] = "archived"
            item["needsReview"] = False
            item["sourcePaths"] = [
                path for path in source_paths if not is_character_card_path(path)
            ]

        next_payload = {
            **payload,
            "version": max(2, int(payload.get("version") or 1)),
            "entities": entities,
        }
        if next_payload != loaded:
            self._write_json_atomic(registry_path, next_payload)

    @staticmethod
    def _path_derived_entity_id(relative_path: str) -> str:
        """撞 id / 无 id 时的兜底实体 ID：由角色卡路径确定性派生，冷重建可复现。"""
        digest = sha256(str(relative_path or "").replace("\\", "/").encode("utf-8")).hexdigest()
        return f"char_{digest[:12]}"

    def _stable_entity_id_from_source(self, source: Dict[str, Any]) -> str:
        text = str(source.get("text") or "")
        try:
            loaded = json.loads(text)
        except Exception:
            loaded = None
        candidates: List[Any] = []
        if isinstance(loaded, dict):
            candidates.extend(
                loaded.get(key)
                for key in ("entityId", "entity_id", "stableId", "stable_id", "id")
            )
        match = re.search(
            r"(?:稳定实体\s*ID|entity[_ ]?id|stable[_ ]?id)\s*[:：]\s*`?([A-Za-z][A-Za-z0-9_.:-]{1,159})`?",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            candidates.insert(0, match.group(1))
        for candidate in candidates:
            normalized = str(candidate or "").strip().strip("`")
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{1,159}", normalized):
                return normalized
        return ""

    @staticmethod
    def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _collect_entities(self, root: Path, sources: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        entities_by_name: Dict[str, Dict[str, Any]] = {}
        registry = EntityRegistry(root)
        review_flags = self._registry_review_flags(root)
        for record in registry.load_records():
            self._add_entity(
                entities_by_name,
                self._entity_from_record(record, review_flags.get(record.entity_id, {})),
            )

        for source in sources:
            if source["kind"] == "character":
                for name in self._character_names_from_source(source):
                    canonical = registry.canonicalize_many([name])
                    canonical_name = canonical[0] if canonical else name
                    self._add_entity(entities_by_name, {
                        "name": canonical_name,
                        "entityId": self._stable_entity_id_from_source(source),
                        "kind": "character",
                        "type": "character",
                        "category": "characters",
                        "aliases": [name] if name != canonical_name else [],
                        "sourcePaths": [source["relativePath"]],
                        "needsReview": False,
                    })

        entities = sorted(
            entities_by_name.values(),
            key=lambda entity: (
                -self._entity_score(entity, sources),
                str(entity.get("category") or ""),
                str(entity.get("type") or ""),
                str(entity.get("name") or ""),
            ),
        )
        self._assign_unique_node_ids(entities)
        return entities

    def _registry_review_flags(self, root: Path) -> Dict[str, Dict[str, Any]]:
        """读取 registry 里 reconcile 打的待确认标记（EntityRecord 不携带这些字段）。"""
        path = root / ENTITY_SOURCE_PATH
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        records = payload.get("entities") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            return {}
        flags: Dict[str, Dict[str, Any]] = {}
        for item in records:
            if not isinstance(item, dict):
                continue
            entity_id = str(
                item.get("entityId")
                or item.get("entity_id")
                or item.get("stableId")
                or item.get("stable_id")
                or item.get("id")
                or ""
            ).strip()
            if not entity_id:
                continue
            flags[entity_id] = {
                "needsReview": bool(item.get("needsReview")),
                "idConflictWith": str(item.get("idConflictWith") or "").strip(),
            }
        return flags

    def _collect_character_names(self, root: Path, sources: Sequence[Dict[str, Any]]) -> List[str]:
        return [str(entity["name"]) for entity in self._collect_entities(root, sources) if entity["type"] == "character"]

    def _entity_from_record(
        self,
        record: EntityRecord,
        review_flags: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        node_type = self._entity_type_for_kind(record.kind)
        flags = review_flags or {}
        entity = {
            "name": record.canonical_name,
            "entityId": record.entity_id,
            "kind": record.kind or node_type,
            "type": node_type,
            "category": self._entity_category_for_type(node_type),
            "aliases": list(record.aliases),
            "sourcePaths": list(record.source_paths) or [ENTITY_SOURCE_PATH],
            "needsReview": bool(flags.get("needsReview")),
        }
        conflict = str(flags.get("idConflictWith") or "").strip()
        if conflict:
            entity["idConflictWith"] = conflict
        return entity

    def _add_entity(self, entities_by_name: Dict[str, Dict[str, Any]], entity: Dict[str, Any]) -> None:
        name = str(entity.get("name") or "").strip()
        if not name:
            return
        incoming = dict(entity)
        incoming["name"] = name
        incoming["entityId"] = str(incoming.get("entityId") or "").strip()
        incoming["aliases"] = [str(item).strip() for item in incoming.get("aliases", []) if str(item).strip() and str(item).strip() != name]
        incoming["sourcePaths"] = [str(item) for item in incoming.get("sourcePaths", []) if str(item).strip()]
        existing = entities_by_name.get(name)
        if existing is None:
            entities_by_name[name] = incoming
            return
        existing["aliases"] = list(dict.fromkeys([*existing.get("aliases", []), *incoming.get("aliases", [])]))
        existing["sourcePaths"] = list(dict.fromkeys([*existing.get("sourcePaths", []), *incoming.get("sourcePaths", [])]))
        if not str(existing.get("entityId") or "").strip() and incoming.get("entityId"):
            existing["entityId"] = incoming["entityId"]
        existing["needsReview"] = bool(existing.get("needsReview") or incoming.get("needsReview"))

    def _character_names_from_source(self, source: Dict[str, Any]) -> List[str]:
        text = str(source.get("text") or "")
        try:
            loaded = json.loads(text)
        except Exception:
            loaded = None

        structured_names: List[str] = []
        explicit_aliases: List[str] = []
        if isinstance(loaded, dict):
            for key in ("name", "character_name", "characterName", "displayName"):
                value = str(loaded.get(key) or "").strip()
                if self._is_plausible_character_card_name(value):
                    structured_names.append(value)
            aliases = loaded.get("aliases") if isinstance(loaded.get("aliases"), list) else []
            explicit_aliases.extend(
                str(value).strip()
                for value in aliases
                if self._is_plausible_character_card_name(value)
            )

        if not structured_names:
            for key in ("name", "character_name", "characterName", "displayName"):
                match = re.search(rf'"{key}"\s*:\s*"([^"]+)"', text)
                if match and self._is_plausible_character_card_name(match.group(1)):
                    structured_names.append(match.group(1).strip())

        heading_name = ""
        heading = re.search(r"^\s*#\s+(.+?)\s*$", text, flags=re.MULTILINE)
        if heading:
            candidate = re.sub(r"[*_`]+", "", heading.group(1)).strip()
            if self._is_plausible_character_card_name(candidate):
                heading_name = candidate
        stem = Path(str(source["relativePath"])).stem
        stem = re.sub(r"^\d+[_\-\s]*", "", stem)
        stem = re.sub(r"^角色[_\-\s]*", "", stem)
        stem_name = stem if self._is_plausible_character_card_name(stem) else ""

        primary = next(iter(structured_names), "") or heading_name or stem_name
        if not primary:
            return []
        aliases = [
            *structured_names[1:],
            *explicit_aliases,
        ]
        return [primary, *list(dict.fromkeys(alias for alias in aliases if alias != primary))]

    @staticmethod
    def _is_plausible_character_card_name(value: Any) -> bool:
        name = re.sub(r"\s+", " ", str(value or "")).strip().strip("#*_`")
        if not name or len(name) > 80 or name.upper() == "README":
            return False
        generic = {
            "角色", "人物", "角色卡", "人物卡", "角色档案", "人物档案",
            "角色设定", "人物设定", "character", "character profile", "未命名角色",
        }
        return name.lower() not in generic and name not in CHARACTER_TOKEN_BLACKLIST

    def _name_score(self, name: str, sources: Sequence[Dict[str, Any]]) -> int:
        return sum(str(source["text"]).count(name) for source in sources)

    def _entity_score(self, entity: Dict[str, Any], sources: Sequence[Dict[str, Any]]) -> int:
        names = [str(entity.get("name") or ""), *[str(alias) for alias in entity.get("aliases", [])]]
        return sum(self._name_score(name, sources) for name in names if name)

    @staticmethod
    def _entity_type_for_kind(kind: str) -> str:
        normalized = str(kind or "").strip().lower()
        return ENTITY_KIND_NODE_TYPES.get(normalized, "setting")

    @staticmethod
    def _entity_category_for_type(node_type: str) -> str:
        if node_type == "character":
            return "characters"
        if node_type in {"event", "timeline"}:
            return "plot"
        return "setting"

    def _entity_node_id(self, entity: Dict[str, Any]) -> str:
        assigned = str(entity.get("nodeId") or "").strip()
        if assigned:
            return assigned
        sanitized = self._sanitize_node_id(entity.get("entityId") or entity.get("entity_id"))
        if sanitized:
            return sanitized
        node_type = str(entity.get("type") or "setting")
        return f"{node_type}:{self._slug(str(entity.get('name') or 'item'))}"

    @staticmethod
    def _sanitize_node_id(value: Any) -> str:
        """把 registry 里的 entityId 清洗成可用节点 ID，而不是整条丢弃。

        旧实现只接受 ASCII 开头的严格 ID，一条 `林北-1` 就会让节点 ID 回退成
        `character:林北-1`，和 registry 对不上，进而判定「canonical 节点数 != 1」，
        最后整张图被作废。
        """
        raw = re.sub(r"\s+", "_", str(value or "").strip())
        if not raw:
            return ""
        cleaned = re.sub(r"[^\w.:\-]", "", raw, flags=re.UNICODE).strip("._:-")
        return cleaned[:160]

    def _assign_unique_node_ids(self, entities: Sequence[Dict[str, Any]]) -> None:
        """同一个节点 ID 只能属于一个实体：后来者降级为按名字派生的 ID 并标待确认。"""
        taken: set[str] = set()
        for entity in entities:
            entity.pop("nodeId", None)
            node_id = self._entity_node_id(entity)
            if node_id in taken:
                node_type = str(entity.get("type") or "setting")
                fallback = f"{node_type}:{self._slug(str(entity.get('name') or 'item'))}"
                candidate = fallback
                suffix = 2
                while candidate in taken:
                    candidate = f"{fallback}-{suffix}"
                    suffix += 1
                node_id = candidate
                entity["needsReview"] = True
            taken.add(node_id)
            entity["nodeId"] = node_id

    def _entity_summary(self, entity: Dict[str, Any]) -> str:
        node_type = str(entity.get("type") or "setting")
        label = NODE_TYPE_LABELS.get(node_type, NODE_TYPE_LABELS["setting"])
        return f"{entity.get('name')} 是来自权威实体记忆的{label}。"

    def _entity_details(self, entity: Dict[str, Any]) -> List[str]:
        aliases = [str(alias) for alias in entity.get("aliases", []) if str(alias).strip()]
        details = [
            f"类型: {NODE_TYPE_LABELS.get(str(entity.get('type') or 'setting'), NODE_TYPE_LABELS['setting'])}",
            f"规范名: {entity.get('name')}",
        ]
        if aliases:
            details.append("别名: " + "、".join(aliases))
        source_paths = [str(path) for path in entity.get("sourcePaths", []) if str(path).strip()]
        if source_paths:
            details.append("来源: " + "、".join(source_paths[:6]))
        return details

    def _chapter_mentions_by_path(
        self,
        registry: EntityRegistry,
        chapter_sources: Sequence[Dict[str, Any]],
        character_names: Sequence[str],
    ) -> Dict[str, tuple[str, ...]]:
        return {
            str(source["relativePath"]): self._resolve_character_mentions(registry, str(source.get("text") or ""), character_names)
            for source in chapter_sources
        }

    def _resolve_character_mentions(
        self,
        registry: EntityRegistry,
        text: str,
        character_names: Sequence[str],
    ) -> tuple[str, ...]:
        known = {str(name) for name in character_names if str(name).strip()}
        if not known:
            return ()
        resolved = registry.resolve_mentions(text, fallback_names=character_names)
        return tuple(name for name in resolved if name in known)

    def _overview_summary(
        self,
        root: Path,
        sources: Sequence[Dict[str, Any]],
        chapter_sources: Sequence[Dict[str, Any]],
        character_names: Sequence[str],
    ) -> str:
        return (
            f"\u300a{root.name}\u300b\u7684\u77e5\u8bc6\u56fe\u8c31\u5df2\u4ece {len(sources)} \u4e2a\u9879\u76ee\u6587\u4ef6\u4e2d\u6784\u5efa\uff0c"
            f"\u8986\u76d6 {len(chapter_sources)} \u4e2a\u6b63\u6587\u7ae0\u8282/\u7247\u6bb5\u4e0e {len(character_names)} \u4e2a\u5019\u9009\u89d2\u8272\u6761\u76ee\u3002"
            "\u5b83\u5c06\u7ae0\u8282\u3001\u89d2\u8272\u3001\u8bbe\u5b9a\u3001\u4e8b\u4ef6\u3001\u4f0f\u7b14\u548c\u65f6\u95f4\u7ebf\u7edf\u4e00\u4e3a\u53ef\u6301\u7eed\u66f4\u65b0\u7684 WIKI\u3002"
        )

    def _entry(
        self,
        entry_id: str,
        title: str,
        category: str,
        summary: str,
        details: Sequence[str],
        source_paths: Sequence[str],
        *,
        confidence: float = 0.72,
        needs_review: bool = False,
        knowledge_status: str = "",
        aliases: Sequence[str] = (),
        primary_source_path: str = "",
    ) -> Dict[str, Any]:
        entry = {
            "id": entry_id,
            "title": title,
            "category": category,
            "categoryLabel": CATEGORY_LABELS.get(category, category),
            "summary": summary,
            "details": [detail for detail in details if str(detail).strip()],
            "sourcePaths": list(dict.fromkeys(source_paths)),
            "confidence": self._confidence(confidence),
            "needsReview": bool(needs_review),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        if knowledge_status in KNOWLEDGE_STATUSES:
            entry["knowledgeStatus"] = knowledge_status
        normalized_aliases = [str(alias).strip() for alias in aliases if str(alias).strip() and str(alias).strip() != title]
        if normalized_aliases:
            entry["aliases"] = list(dict.fromkeys(normalized_aliases))
        if primary_source_path:
            entry["primarySourcePath"] = str(primary_source_path)
        return entry

    def _display_title(self, relative_path: str, fallback: str) -> str:
        path = Path(relative_path)
        if path.parent.name and path.parent.name != "chapters":
            return f"{path.parent.name}/{path.stem}"
        return path.stem or fallback

    def _compress_text(self, text: str, limit: int) -> str:
        normalized = re.sub(r"\s+", " ", text).strip()
        if len(normalized) <= limit:
            return normalized
        return normalized[:limit].rstrip() + "..."

    def _details_from_sources(self, sources: Sequence[Dict[str, Any]], *, limit: int = 8) -> List[str]:
        details: List[str] = []
        for source in sources[:limit]:
            snippet = self._compress_text(str(source["text"]), 180)
            if snippet:
                details.append(f"{source['relativePath']}: {snippet}")
        return details

    def _build_plot_summary(self, chapter_sources: Sequence[Dict[str, Any]]) -> str:
        if not chapter_sources:
            return "\u5c1a\u672a\u68c0\u6d4b\u5230\u6b63\u6587\u7ae0\u8282\uff0c\u4e3b\u7ebf\u5267\u60c5\u7b49\u5f85\u521b\u4f5c\u3002"
        first = self._compress_text(chapter_sources[0]["text"], 120)
        latest = self._compress_text(chapter_sources[-1]["text"], 120)
        return f"\u4e3b\u7ebf\u4ece\u201c{first}\u201d\u5c55\u5f00\uff0c\u5f53\u524d\u6700\u65b0\u8fdb\u5c55\u805a\u7126\u4e8e\u201c{latest}\u201d\u3002"

    def _chapter_plot_details(self, chapter_sources: Sequence[Dict[str, Any]]) -> List[str]:
        details = []
        for index, source in enumerate(chapter_sources, start=1):
            details.append(f"{index}. {self._display_title(source['relativePath'], source['title'])}: {self._compress_text(source['text'], 180)}")
        return details

    def _chapter_details(self, source: Dict[str, Any], mentioned_characters: Sequence[str]) -> List[str]:
        mentions = [str(name) for name in mentioned_characters if str(name).strip()]
        details = [
            f"\u8def\u5f84: {source['relativePath']}",
            f"\u5b57\u7b26\u6570: {len(source['text'])}",
        ]
        if mentions:
            details.append("\u51fa\u573a/\u88ab\u63d0\u53ca\u89d2\u8272: " + "\u3001".join(mentions[:12]))
        key_lines = [line.strip() for line in source["text"].splitlines() if line.strip()]
        details.extend([f"\u6458\u8981: {self._compress_text(line, 120)}" for line in key_lines[:5]])
        return details

    def _character_sources(
        self,
        root: Path,
        sources: Sequence[Dict[str, Any]],
        entities: Sequence[Dict[str, Any]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        mapping: Dict[str, List[Dict[str, Any]]] = {str(entity["name"]): [] for entity in entities}
        character_sources = [source for source in sources if source.get("kind") == "character"]
        source_by_path = {str(source.get("relativePath") or ""): source for source in character_sources}
        claimed_paths: set[str] = set()

        # ``_collect_entities`` 已把角色卡的真实路径记录为 sourcePaths。这个结构化
        # 所有权优先级最高，关系段落里提到其他角色不再改变主档案归属。
        for entity in entities:
            name = str(entity.get("name") or "")
            for relative_path in entity.get("sourcePaths", []):
                normalized = str(relative_path or "")
                source = source_by_path.get(normalized)
                if source is None or normalized in claimed_paths:
                    continue
                mapping[name].append(source)
                claimed_paths.add(normalized)

        # 兼容仅来自 EntityRegistry 的角色：按卡片自身 JSON name、一级标题/文件名
        # 识别唯一主角色。正文中的普通提及只由章节 mention 流程处理。
        for source in character_sources:
            relative_path = str(source.get("relativePath") or "")
            if relative_path in claimed_paths:
                continue
            declared_names = self._character_names_from_source(source)
            owner = next(
                (
                    str(entity.get("name") or "")
                    for entity in entities
                    if str(entity.get("name") or "") in declared_names
                    or any(str(alias) in declared_names for alias in entity.get("aliases", []))
                ),
                "",
            )
            if owner:
                mapping[owner].append(source)
                claimed_paths.add(relative_path)
        return mapping

    def _mentioning_sources(self, sources: Sequence[Dict[str, Any]], name: str) -> List[Dict[str, Any]]:
        return [source for source in sources if name and name in source["text"]]

    def _character_summary(self, name: str, related: Sequence[Dict[str, Any]], mentions: Sequence[Dict[str, Any]]) -> str:
        if related:
            return self._compress_text(related[0]["text"], 220) or f"{name}\u7684\u89d2\u8272\u8bbe\u5b9a\u6765\u81ea\u9879\u76ee\u89d2\u8272\u6863\u6848\u3002"
        return f"{name}\u5728 {len(mentions)} \u4e2a\u7ae0\u8282/\u7247\u6bb5\u4e2d\u88ab\u63d0\u53ca\uff0c\u5df2\u7eb3\u5165\u77e5\u8bc6\u56fe\u8c31\u8ddf\u8e2a\u3002"

    def _character_details(self, name: str, related: Sequence[Dict[str, Any]], mentions: Sequence[Dict[str, Any]]) -> List[str]:
        details = [f"\u540d\u79f0: {name}", f"\u51fa\u573a/\u63d0\u53ca\u6b21\u6570: {sum(source['text'].count(name) for source in mentions)}"]
        details.extend(self._details_from_sources(related, limit=3))
        if mentions:
            details.append("\u76f8\u5173\u7ae0\u8282: " + "\u3001".join(source["relativePath"] for source in mentions[:8]))
        return details

    def _append_fact_edges(
        self,
        root: Path,
        graph_nodes: List[Dict[str, Any]],
        graph_edges: List[Dict[str, Any]],
        *,
        registry: EntityRegistry,
        entities: Sequence[Dict[str, Any]],
        sources: Sequence[Dict[str, Any]] | None = None,
    ) -> None:
        facts_path = root / FACT_SOURCE_PATH
        if not facts_path.exists():
            return
        try:
            payload = json.loads(facts_path.read_text(encoding="utf-8"))
        except Exception:
            return
        raw_facts = payload.get("facts") if isinstance(payload, dict) else None
        if not isinstance(raw_facts, list):
            return

        endpoint_by_name: Dict[str, str] = {}
        for entity in entities:
            node_id = self._entity_node_id(entity)
            names = [str(entity.get("name") or ""), *[str(alias) for alias in entity.get("aliases", [])]]
            for name in names:
                if name.strip():
                    endpoint_by_name.setdefault(name, node_id)
        for node in graph_nodes:
            node_id = str(node.get("id") or "")
            label = str(node.get("label") or "").strip()
            if node_id:
                endpoint_by_name.setdefault(node_id, node_id)
            if label and node_id:
                endpoint_by_name.setdefault(label, node_id)

        source_cache = list(sources) if sources is not None else self._collect_sources(root)
        for item in raw_facts:
            if not isinstance(item, dict):
                continue
            subject_raw = str(item.get("subject") or "").strip()
            predicate = str(item.get("predicate") or "").strip()
            object_raw = str(item.get("object") or "").strip()
            if not subject_raw or not predicate or not object_raw:
                continue
            subject = (registry.canonicalize_many([subject_raw]) or (subject_raw,))[0]
            obj = (registry.canonicalize_many([object_raw]) or (object_raw,))[0]
            source_id = endpoint_by_name.get(subject)
            target_id = endpoint_by_name.get(obj)
            if not source_id or not target_id or source_id == target_id:
                continue
            established_in = str(item.get("established_in") or item.get("establishedIn") or "").strip()
            evidence_text = str(item.get("evidence") or "").strip()
            source_path = self._grounded_evidence_source_path(
                root,
                evidence=evidence_text,
                requested_path=established_in,
                sources=source_cache,
            )
            if not source_path:
                continue
            edge = self._edge(
                source_id,
                target_id,
                predicate,
                "fact",
                evidence=evidence_text,
            )
            edge["sourcePath"] = source_path
            confidence = str(item.get("confidence") or "").strip().lower()
            edge["confidence"] = 0.86 if confidence in {"canon", "confirmed", ""} else 0.62
            edge["needsReview"] = confidence not in {"canon", "confirmed", ""}
            if not self._edge_evidence_anchors_endpoints(
                root,
                edge,
                nodes=graph_nodes,
                sources=source_cache,
            ):
                continue
            graph_edges.append(edge)

    def _append_knowledge_relation_edges(
        self,
        root: Path,
        graph_nodes: List[Dict[str, Any]],
        graph_edges: List[Dict[str, Any]],
        *,
        include_review: bool = True,
    ) -> List[Dict[str, Any]]:
        """Merge v2 facts, formal Markdown relations and review candidates.

        This adapter deliberately trusts only records already validated by
        ``StoryKnowledgeRelationService``.  It never promotes free-form prose
        or an Agent supplied ``graph.nodes/edges`` payload.
        """
        try:
            from services.story_knowledge_relation_service import (
                StoryKnowledgeRelationService,
                get_story_knowledge_relation_service,
            )

            relation_service: StoryKnowledgeRelationService = get_story_knowledge_relation_service()
            facts_payload = relation_service.load_facts(root)
            formal_relations = relation_service.scan_formal_markdown_relations(root)
            review_payload = relation_service.load_review_ledger(root)
        except Exception as exc:
            # Do not silently publish a relation-less graph over the last-good
            # projection.  A blocking input diagnostic makes _persist_payload
            # retain the previous bundle while exposing the concrete failure.
            return [self._graph_diagnostic(
                "graph.relation_source_error",
                f"统一关系数据读取失败：{type(exc).__name__}: {exc}",
                ".storydex/memory",
                blocking=True,
            )]

        node_ids = {
            str(node.get("id") or "").strip()
            for node in graph_nodes
            if str(node.get("id") or "").strip()
        }
        node_by_id = {
            str(node.get("id") or "").strip(): node
            for node in graph_nodes
            if str(node.get("id") or "").strip()
        }
        existing_by_key: Dict[tuple[str, str, str], Dict[str, Any]] = {}
        for edge in graph_edges:
            source = str(edge.get("source") or "").strip()
            target = str(edge.get("target") or "").strip()
            label = str(edge.get("predicate") or edge.get("label") or "").strip()
            if source and target and label:
                existing_by_key[(source, label, target)] = edge

        def append_relation(relation: Dict[str, Any], *, review_allowed: bool) -> None:
            review_status = str(relation.get("reviewStatus") or "review_required")
            if review_status == "review_required" and not review_allowed:
                return
            if review_status in {"rejected", "superseded"}:
                return
            source = str(relation.get("subjectId") or "").strip()
            target = str(relation.get("objectId") or "").strip()
            predicate = str(relation.get("predicate") or "").strip()
            if not source or not target or not predicate or source == target:
                return
            if source not in node_ids or target not in node_ids:
                return
            key = (source, predicate, target)
            current = existing_by_key.get(key)
            edge = relation_service.graph_edge_from_relation(relation)
            # Confirmed facts always win over a review candidate with the same
            # endpoints; otherwise keep the first deterministic record.
            if current is not None:
                current_status = str(current.get("reviewStatus") or "")
                if current_status == "confirmed" or review_status != "confirmed":
                    return
                try:
                    graph_edges.remove(current)
                except ValueError:
                    pass
            existing_by_key[key] = edge
            graph_edges.append(edge)

        for raw_fact in facts_payload.get("facts", []):
            if isinstance(raw_fact, dict):
                append_relation(raw_fact, review_allowed=include_review)
        for relation in formal_relations:
            if isinstance(relation, dict):
                append_relation(relation, review_allowed=False)
        if include_review:
            for candidate in review_payload.get("relations", []):
                if isinstance(candidate, dict):
                    append_relation(candidate, review_allowed=True)
        return []

    def _edge(
        self,
        source: str,
        target: str,
        label: str,
        edge_type: str,
        *,
        weight: int = 1,
        evidence: str = "",
    ) -> Dict[str, Any]:
        return {
            "source": source,
            "target": target,
            "label": label,
            "type": edge_type,
            "weight": weight,
            "evidence": evidence,
        }

    def _dedupe_nodes(self, nodes: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen: Dict[str, Dict[str, Any]] = {}
        for node in nodes:
            node_id = str(node.get("id") or "").strip()
            if node_id and node_id not in seen:
                seen[node_id] = node
        return list(seen.values())

    def _dedupe_edges(self, edges: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen: Dict[tuple[str, str, str], Dict[str, Any]] = {}
        for edge in edges:
            key = (str(edge.get("source")), str(edge.get("target")), str(edge.get("label")))
            if not key[0] or not key[1]:
                continue
            # 后见覆盖：merge 时 incoming 边在 base 之后，保证局部重建的
            # 新 evidence/weight 能替换陈旧数据。
            seen[key] = edge
        result = list(seen.values())
        if len(result) <= MAX_WIKI_GRAPH_EDGES:
            return result
        # 超出上限时按权重淘汰，而不是按输入顺序静默截断。
        result.sort(
            key=lambda edge: self._safe_int(edge.get("weight"), fallback=1),
            reverse=True,
        )
        return result[:MAX_WIKI_GRAPH_EDGES]

    def _slug(self, value: str) -> str:
        cleaned = re.sub(r"\s+", "-", value.strip())
        cleaned = re.sub(r"[^\w\-\u4e00-\u9fff]", "", cleaned)
        return cleaned or "item"

    def _chapter_entry_id(self, relative_path: str) -> str:
        """\u7ae0\u8282\u6761\u76ee/\u8282\u70b9 ID \u57fa\u4e8e\u6587\u4ef6\u8def\u5f84\uff0c\u4e0d\u968f\u6392\u5e8f\u4f4d\u7f6e\u6f02\u79fb\u3002

        \u65e7\u5b9e\u73b0\u7528 `chapter:{\u6392\u5e8f\u4f4d\u7f6e}`\uff0c\u63d2\u5165/\u5220\u9664/\u91cd\u547d\u540d\u7ae0\u8282\u4f1a\u8ba9\u6240\u6709\u540e\u7eed
        \u7ae0\u8282\u7684 ID \u79fb\u4f4d\uff0c\u589e\u91cf\u5408\u5e76\u65f6\u5185\u5bb9\u4e92\u76f8\u8986\u76d6\u3002\u8def\u5f84 slug \u662f\u7a33\u5b9a\u6807\u8bc6\u3002
        """
        normalized = str(relative_path or "").replace("\\", "/").strip("/")
        normalized = re.sub(r"\.(md|txt)$", "", normalized, flags=re.IGNORECASE)
        return f"chapter:{self._slug(normalized.replace('/', '-'))}"

    def _planned_entry_id(self, relative_path: str) -> str:
        normalized = str(relative_path or "").replace("\\", "/").strip("/")
        normalized = re.sub(r"\.(md|txt|json|jsonl)$", "", normalized, flags=re.IGNORECASE)
        return f"planned:{self._slug(normalized.replace('/', '-'))}"

    def _render_markdown(self, payload: Dict[str, Any]) -> str:
        lines = [f"# {payload.get('projectName', 'Storydex')} WIKI", "", str(payload.get("summary", "")), ""]
        for entry in payload.get("entries", []):
            lines.extend([f"## {entry.get('title')}", "", str(entry.get("summary", "")), ""])
            for detail in entry.get("details", [])[:20]:
                lines.append(f"- {detail}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


_story_wiki_service: Optional[StoryWikiService] = None


def get_story_wiki_service() -> StoryWikiService:
    global _story_wiki_service
    if _story_wiki_service is None:
        _story_wiki_service = StoryWikiService()
    return _story_wiki_service
