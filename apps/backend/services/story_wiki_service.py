from __future__ import annotations

import json
import os
import re
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

WIKI_CATEGORY_SCHEMA_VERSION = "story-wiki-v5-evidence-grounded-graph"
PROJECTION_SCHEMA_VERSION = 2
EVIDENCE_GROUNDED_GRAPH_POLICY = {
    "mode": "evidence_grounded_local_v1",
    "agentGraphAccepted": False,
    "coOccurrenceIsRelationship": False,
    "coOccurrenceEdgesAccepted": False,
    "unknownRelationTypesAccepted": False,
    "syntheticRelationshipMetricsAccepted": False,
}
KNOWLEDGE_STATUSES = {"planned", "observed", "inferred", "review_required"}
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
ALLOWED_WIKI_CATEGORIES = {"overview", "characters", "setting", "plot", "relationships"}
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
    "relationships": "relationships",
    "overview": "overview",
    "index": "overview",
}

CATEGORY_LABELS: Dict[str, str] = {
    "overview": "\u9879\u76ee\u6982\u89c8",
    "characters": "\u89d2\u8272\u6863\u6848",
    "setting": "\u8bbe\u5b9a",
    "plot": "\u5267\u60c5",
    "relationships": "\u89d2\u8272\u5173\u7cfb",
}

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
                    if self.validate_graph_invariants(data, root=root, source_documents=sources):
                        return self.rebuild(root, sources=sources)
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
            entries.append(self._entry(
                "plot:mainline",
                "\u4e3b\u7ebf\u5267\u60c5",
                "plot",
                self._build_plot_summary(chapter_sources),
                plot_details,
                [item["relativePath"] for item in chapter_sources[:12]],
                knowledge_status="observed",
            ))
            graph_nodes.append({
                "id": "plot:mainline",
                "label": "\u4e3b\u7ebf",
                "type": "event",
                "category": "plot",
                "entryId": "plot:mainline",
                "summary": "\u9879\u76ee\u5df2\u6709\u7ae0\u8282\u4e32\u8054\u7684\u4e3b\u7ebf\u5267\u60c5\u3002",
                "knowledgeStatus": "observed",
            })
            graph_edges.append(self._edge(project_id, "plot:mainline", "\u63a8\u8fdb", "plot"))

        chapter_mentions = self._chapter_mentions_by_path(registry, chapter_sources, character_names)

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
            })
            graph_edges.append(self._edge("plot:mainline", node_id, "\u7ae0\u8282", "timeline", weight=max(1, index + 1)))

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
        if self.validate_graph_invariants(before, root=root, source_documents=sources):
            return self.rebuild(root, sources=sources)

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

        # 确保 plot:mainline 节点存在（如果有章节的话）
        if chapter_sources:
            nodes.append({
                "id": "plot:mainline",
                "label": "主线",
                "type": "event",
                "category": "plot",
                "entryId": "plot:mainline",
                "summary": "项目已有章节串联的主线剧情。",
                "knowledgeStatus": "observed",
            })

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
            })
            edges.append(self._edge("plot:mainline", entry_id, "章节", "timeline", weight=max(1, index + 1)))

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
                agent_result = {
                    "attempted": True,
                    "completed": False,
                    "errorMessage": str(exc),
                    "reply": "",
                    "events": [],
                    "traceId": trace_id,
                }

        before = self._read_existing_payload(root)
        status = "completed"
        fallback_used = False
        review_report: Dict[str, Any] | None = None

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
            elif incoming is None:
                fallback_used = True
                payload["generationMode"] = "local evidence-grounded"

        if fallback_used and agent_result.get("attempted"):
            status = "fallback"
        elif not agent_result.get("attempted") and not deterministic_graph_refresh:
            status = "fallback"

        payload = self._persist_payload(
            root,
            payload,
            workflow=normalized_workflow,
            status=status,
            agent_result=agent_result,
            sources=sources,
            changed_paths=changed_paths,
        )
        result = {
            "ok": True,
            "workflow": normalized_workflow,
            "status": status,
            "traceId": agent_result.get("traceId") or trace_id,
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
        if review_report is not None:
            result["review"] = review_report
            result["writtenPaths"].append(self.review_report_path(root).relative_to(root).as_posix())
        return result

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
    ) -> Dict[str, Any]:
        root = workspace_root.resolve()
        payload = self.read_or_build(root)
        entries = [entry for entry in payload.get("entries", []) if isinstance(entry, dict)]
        graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
        # 在 query 时对节点和边跑一次规范化兜底，
        # 让缓存里旧的中文 type（如 "动机"/"定位"/"小说"）自动归一到 "character"，
        # 避免 category=characters 的节点因 type 不规范而漏出角色关系视图。
        nodes = [self._normalize_node(node) for node in graph.get("nodes", []) if isinstance(node, dict)]
        edges = [self._normalize_graph_edge(edge) for edge in graph.get("edges", []) if isinstance(edge, dict)]
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
        valid_edges = [
            edge
            for edge in edges
            if str(edge.get("source") or "") in node_by_id
            and str(edge.get("target") or "") in node_by_id
            and str(edge.get("source") or "") != str(edge.get("target") or "")
        ]
        content_edges = [
            edge
            for edge in valid_edges
            if not self._wiki_edge_touches_hub(edge, node_by_id)
        ]

        max_depth = max(1, min(2, self._safe_int(depth, fallback=1)))
        max_items = max(1, min(120, self._safe_int(limit, fallback=60)))
        normalized_q = str(q or "").strip()
        normalized_category = str(category or "").strip()
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
                    entries=entries,
                    entry_by_id=entry_by_id,
                    nodes=nodes,
                    valid_edges=content_edges,
                    category_labels=category_labels,
                ),
            )
        else:
            return self._attach_projection_metadata(
                payload,
                self._query_wiki_overview_graph(
                    payload,
                    normalized_q=normalized_q,
                    normalized_category=normalized_category,
                    normalized_entry_id=normalized_entry_id,
                    normalized_node_id=normalized_node_id,
                    max_depth=max_depth,
                    max_items=max_items,
                    entries=entries,
                    entry_by_id=entry_by_id,
                    category_labels=category_labels,
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
        ][:max_items]
        visible_node_ids = set(ordered_node_ids)
        visible_edges = [
            edge
            for edge in content_edges
            if str(edge.get("source") or "") in visible_node_ids
            and str(edge.get("target") or "") in visible_node_ids
        ][:max_items]

        if matched_entry_ids:
            visible_entry_ids = matched_entry_ids[:max_items]
        else:
            visible_entry_ids = []
            for current_id in ordered_node_ids:
                entry_ref = str(node_by_id[current_id].get("entryId") or "")
                if entry_ref and entry_ref in entry_by_id and entry_ref not in visible_entry_ids:
                    visible_entry_ids.append(entry_ref)
                if len(visible_entry_ids) >= max_items:
                    break

        return self._attach_projection_metadata(payload, {
            "mode": mode,
            "query": normalized_q,
            "category": normalized_category,
            "entryId": normalized_entry_id,
            "nodeId": normalized_node_id,
            "depth": max_depth,
            "limit": max_items,
            "entries": [entry_by_id[entry_ref] for entry_ref in visible_entry_ids if entry_ref in entry_by_id],
            "graph": {
                "nodes": [self._wiki_content_node(node_by_id[current_id]) for current_id in ordered_node_ids],
                "edges": visible_edges,
            },
            "matchedEntryIds": matched_entry_ids[:max_items],
            "total": {
                "entryCount": len(visible_entry_ids),
                "nodeCount": len(ordered_node_ids),
                "edgeCount": len(visible_edges),
            },
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
    def _graph_diagnostic(code: str, message: str, path: str) -> Dict[str, Any]:
        return {
            "code": code,
            "severity": "error",
            "message": message,
            "path": path,
        }

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
                ))
            elif count > 1:
                diagnostics.append(self._graph_diagnostic(
                    "graph.entry.duplicate_id",
                    f"WIKI 条目 ID {entry_id} 重复 {count} 次。",
                    f"entries.{entry_id}",
                ))

        for index, entry in enumerate(entries):
            title = str(entry.get("title") or "").strip()
            if self._is_internal_display_label(title):
                diagnostics.append(self._graph_diagnostic(
                    "graph.entry.internal_label",
                    f"条目显示名泄漏内部 ID：{title}",
                    f"entries[{index}].title",
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
                ))
            elif count > 1:
                diagnostics.append(self._graph_diagnostic(
                    "graph.node.duplicate_id",
                    f"图节点 ID {node_id} 重复 {count} 次。",
                    f"graph.nodes.{node_id}",
                ))

        for index, node in enumerate(nodes):
            node_id = str(node.get("id") or "").strip()
            label = str(node.get("label") or "").strip()
            if self._is_internal_display_label(label):
                diagnostics.append(self._graph_diagnostic(
                    "graph.node.internal_label",
                    f"节点显示名泄漏内部 ID：{label}",
                    f"graph.nodes[{index}].label",
                ))
            selectable = bool(node.get("selectable", True)) and not bool(node.get("synthetic", False))
            if not selectable:
                continue
            if not label:
                diagnostics.append(self._graph_diagnostic(
                    "graph.node.missing_label",
                    f"可点击节点 {node_id or index} 缺少显示名。",
                    f"graph.nodes[{index}].label",
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
                ))
                continue
            entry = entry_by_id.get(entry_id)
            if entry is None:
                diagnostics.append(self._graph_diagnostic(
                    "graph.node.missing_entry",
                    f"节点 {node_id or index} 引用了不存在的条目 {entry_id}。",
                    f"graph.nodes[{index}].entryId",
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
                ))
            if source and source == target and not bool(edge.get("allowSelfLoop", False)):
                diagnostics.append(self._graph_diagnostic(
                    "graph.edge.self_loop",
                    f"边 {source} 未声明允许自环。",
                    f"graph.edges[{index}]",
                ))
            relation_type = str(edge.get("relationType") or "").strip()
            if relation_type and relation_type not in ALLOWED_RELATION_TYPES:
                diagnostics.append(self._graph_diagnostic(
                    "graph.edge.invalid_relation_type",
                    f"关系类型 {relation_type} 不在受控词表中。",
                    f"graph.edges[{index}].relationType",
                ))
            edge_type = str(edge.get("type") or "").strip()
            if edge.get("coOccurrence"):
                diagnostics.append(self._graph_diagnostic(
                    "graph.edge.cooccurrence_removed",
                    f"已停用的同章共现边不能发布：{source} -> {target}。",
                    f"graph.edges[{index}]",
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
                    ))
                if not relation_type or relation_type == "unknown" or str(edge.get("status") or "") != "asserted":
                    diagnostics.append(self._graph_diagnostic(
                        "graph.edge.unresolved_relationship",
                        f"未解析关系不能发布：{source} -> {target}。",
                        f"graph.edges[{index}]",
                    ))
                unsupported_metrics = [
                    key
                    for key in ("level", "strength", "confidence", "polarity")
                    if key in edge
                ]
                if unsupported_metrics:
                    diagnostics.append(self._graph_diagnostic(
                        "graph.edge.synthetic_relationship_metric",
                        f"角色关系边 {source} -> {target} 含无证据量化值：{', '.join(unsupported_metrics)}。",
                        f"graph.edges[{index}]",
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
                    ))
            elif edge_type == "fact":
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
                        "graph.edge.ungrounded_fact",
                        f"事实边 {source} -> {target} 没有可核对证据。",
                        f"graph.edges[{index}].evidence",
                    ))
                elif not self._edge_evidence_anchors_endpoints(
                    root,
                    edge,
                    nodes=nodes,
                    sources=evidence_sources,
                ):
                    diagnostics.append(self._graph_diagnostic(
                        "graph.edge.unanchored_fact",
                        f"事实边 {source} -> {target} 的证据没有明确锚定两个端点。",
                        f"graph.edges[{index}].evidence",
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
                count = character_nodes_by_id.get(record.entity_id, 0)
                if count != 1:
                    diagnostics.append(self._graph_diagnostic(
                        "graph.character.canonical_count",
                        f"active 角色 {record.canonical_name} ({record.entity_id}) 的 canonical 节点数为 {count}，应为 1。",
                        f"graph.nodes.{record.entity_id}",
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
        raw_diagnostics = self.validate_graph_invariants(
            payload,
            root=root,
            source_documents=sources,
        )
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
        payload["status"] = "ready"
        payload["diagnostics"] = []
        payload["lastSuccessfulRevision"] = knowledge_revision
        payload["sourceStats"] = self._source_stats(payload, sources)
        if agent_result is not None:
            payload["agent"] = {
                "attempted": bool(agent_result.get("attempted")),
                "completed": bool(agent_result.get("completed")),
                "traceId": str(agent_result.get("traceId") or ""),
                "errorMessage": str(agent_result.get("errorMessage") or ""),
                "eventCount": len(agent_result.get("events") or []),
            }
        payload["graphChecksum"] = self._graph_checksum(payload)
        normalized_diagnostics = self.validate_graph_invariants(
            payload,
            root=root,
            source_documents=sources,
        )
        diagnostics_by_key: Dict[tuple[str, str, str], Dict[str, Any]] = {}
        for diagnostic in [*raw_diagnostics, *normalized_diagnostics]:
            key = (
                str(diagnostic.get("code") or ""),
                str(diagnostic.get("path") or ""),
                str(diagnostic.get("message") or ""),
            )
            diagnostics_by_key.setdefault(key, diagnostic)
        diagnostics = list(diagnostics_by_key.values())
        if diagnostics:
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
                and not self.validate_graph_invariants(
                    previous,
                    root=root,
                    source_documents=sources,
                )
            ):
                return self._with_projection_status(root, previous)
            return self._safe_projection_after_failure(
                root,
                sources=sources,
                diagnostics=diagnostics,
                last_successful_revision=last_successful_revision,
            )

        index_payload = self._build_index(
            root,
            payload,
            sources=sources,
            workflow=workflow,
            status=status,
            changed_paths=changed_paths,
        )
        self._write_projection_bundle(root, payload=payload, index_payload=index_payload)
        self._write_json_atomic(self.projection_status_path(root), {
            "schemaVersion": PROJECTION_SCHEMA_VERSION,
            "status": "ready",
            "diagnostics": [],
            "knowledgeRevision": knowledge_revision,
            "builtFromRevision": knowledge_revision,
            "lastSuccessfulRevision": knowledge_revision,
            "sourceSetChecksum": source_checksum,
            "graphChecksum": payload["graphChecksum"],
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        })
        return payload

    def _write_projection_bundle(
        self,
        root: Path,
        *,
        payload: Dict[str, Any],
        index_payload: Dict[str, Any],
    ) -> None:
        targets = {
            self.wiki_json_path(root): json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            self.wiki_markdown_path(root): self._render_markdown(payload),
            self.wiki_index_path(root): json.dumps(index_payload, ensure_ascii=False, indent=2) + "\n",
        }
        temporary_paths: Dict[Path, Path] = {}
        try:
            for target, content in targets.items():
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
                with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary_paths[target] = temporary
            for target, temporary in temporary_paths.items():
                os.replace(temporary, target)
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
        sources_index: Dict[str, Any] = {}
        for source in sources:
            rel = str(source.get("relativePath") or "")
            related_entries = [
                str(entry.get("id"))
                for entry in entries
                if rel in [str(item) for item in entry.get("sourcePaths", [])]
            ]
            related_nodes = [
                str(node.get("id"))
                for node in nodes
                if str(node.get("entryId") or "") in related_entries
            ]
            sources_index[rel] = {
                "sha256": source.get("sha256"),
                "kind": source.get("kind"),
                "size": source.get("size"),
                "mtime": source.get("mtime"),
                "lastAnalyzedAt": datetime.now(timezone.utc).isoformat(),
                "relatedEntryIds": related_entries,
                "relatedNodeIds": related_nodes,
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
            "category 只允许五类: overview(总览)、characters(角色)、setting(设定)、plot(剧情)、relationships(关系)。"
            "章节/事件/时间线归入 plot；世界/地点/物品/势力/伏笔归入 setting；不要输出其他 category。\n"
            "图结构由后端从实体注册表、角色档案和逐字可核对证据中确定性生成；你不得新增节点或边，graph 必须返回空数组。\n"
            "不要根据同章出现、常见剧情套路、姓氏、身份或语气推断人物关系。没有明示证据就保持沉默。\n"
            "输出 JSON schema: {summary, entries:[{id,title,category,categoryLabel,summary,details,sourcePaths,confidence,needsReview}], "
            "graph:{nodes:[],edges:[]}, review:{issues,recommendations}}。\n"
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
        # 交叉规范化：仅 category=characters 的节点强制 type=character，
        # 修正 Agent 输出 "动机"/"定位"/"小说" 等中文 type。
        # relationships 类目的节点（关系条目、索引等）不是角色，
        # 强转会把它们混入角色关系网络，因此保持原 type。
        if category == "characters":
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
            "source": str(item.get("source") or ""),
            "target": str(item.get("target") or ""),
            "label": str(item.get("label") or item.get("type") or "\u5173\u8054"),
            "type": str(item.get("type") or "related"),
            "weight": int(item.get("weight") or 1),
            "evidence": str(item.get("evidence") or ""),
            "sourcePath": str(item.get("sourcePath") or item.get("source_path") or ""),
            "needsReview": bool(item.get("needsReview", False)),
        }
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

    def _query_wiki_overview_graph(
        self,
        payload: Dict[str, Any],
        *,
        normalized_q: str,
        normalized_category: str,
        normalized_entry_id: str,
        normalized_node_id: str,
        max_depth: int,
        max_items: int,
        entries: Sequence[Dict[str, Any]],
        entry_by_id: Dict[str, Dict[str, Any]],
        category_labels: Dict[str, Any],
    ) -> Dict[str, Any]:
        content_entries = [
            entry
            for entry in entries
            if str(entry.get("category") or "") not in {"", "overview", "index"}
        ]
        overview_entry_ids = [
            str(entry.get("id") or "")
            for entry in entries
            if str(entry.get("category") or "") == "overview" and str(entry.get("id") or "")
        ][:max_items]
        category_to_entries: Dict[str, List[Dict[str, Any]]] = {}
        for entry in content_entries:
            category = str(entry.get("category") or "").strip()
            if not category:
                continue
            category_to_entries.setdefault(category, []).append(entry)

        ordered_categories: List[str] = []
        for category in CATEGORY_LABELS:
            if category in category_to_entries and category not in {"overview", "index"}:
                ordered_categories.append(category)
        for category in sorted(category_to_entries):
            if category not in ordered_categories:
                ordered_categories.append(category)

        project_hub = self._wiki_project_hub_node(payload, content_entries)
        category_hubs = [
            self._wiki_category_hub_node(category, category_labels, category_to_entries[category])
            for category in ordered_categories
        ][: max(0, max_items - 1)]
        nodes = [project_hub, *category_hubs][:max_items]
        edges = [
            {
                "source": "project:root",
                "target": str(node.get("id") or ""),
                "label": "\u5206\u7ec4",
                "type": "group",
                "weight": max(1, self._safe_int(node.get("count"), fallback=1)),
                "synthetic": True,
            }
            for node in category_hubs
            if str(node.get("id") or "")
        ][:max_items]

        return {
            "mode": "overview",
            "query": normalized_q,
            "category": normalized_category,
            "entryId": normalized_entry_id,
            "nodeId": normalized_node_id,
            "depth": max_depth,
            "limit": max_items,
            "entries": [entry_by_id[entry_ref] for entry_ref in overview_entry_ids if entry_ref in entry_by_id],
            "graph": {
                "nodes": nodes,
                "edges": edges,
            },
            "matchedEntryIds": [],
            "total": {
                "entryCount": len(overview_entry_ids),
                "nodeCount": len(nodes),
                "edgeCount": len(edges),
            },
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
        entries: Sequence[Dict[str, Any]],
        entry_by_id: Dict[str, Dict[str, Any]],
        nodes: Sequence[Dict[str, Any]],
        valid_edges: Sequence[Dict[str, Any]],
        category_labels: Dict[str, Any],
    ) -> Dict[str, Any]:
        if category == "relationships":
            return self._query_wiki_relationship_graph(
                category,
                root=root,
                normalized_q=normalized_q,
                normalized_entry_id=normalized_entry_id,
                normalized_node_id=normalized_node_id,
                max_depth=max_depth,
                max_items=max_items,
                entries=entries,
                entry_by_id=entry_by_id,
                nodes=nodes,
                valid_edges=valid_edges,
                category_labels=category_labels,
            )
        category_entries = [
            entry
            for entry in entries
            if str(entry.get("category") or "") == category and str(entry.get("id") or "")
        ]
        matched_entry_ids = [str(entry.get("id") or "") for entry in category_entries][:max_items]

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
        primary_nodes = primary_nodes[:max_items]
        primary_ids = {str(node.get("id") or "") for node in primary_nodes}

        # \u4e00\u8df3\u8de8\u7c7b\u90bb\u5c45\uff1a\u8865\u5168"\u7ae0\u8282\u91cc\u51fa\u573a\u4e86\u8c01 / \u8bbe\u5b9a\u5173\u8054\u4ec0\u4e48\u4e8b\u4ef6"\u8fd9\u7c7b\u8de8\u7c7b\u4e0a\u4e0b\u6587\uff0c
        # \u6807\u8bb0 neighbor=True \u4f9b\u524d\u7aef\u5f31\u5316\u6e32\u67d3\u3002
        node_by_id = {str(node.get("id") or ""): node for node in nodes if str(node.get("id") or "").strip()}
        neighbor_nodes: List[Dict[str, Any]] = []
        neighbor_ids: set[str] = set()
        neighbor_budget = max(0, max_items - len(primary_nodes))
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
            copied = self._wiki_content_node(other_node)
            copied["neighbor"] = True
            neighbor_nodes.append(copied)
            neighbor_ids.add(other)

        visible_node_ids = primary_ids | neighbor_ids
        # \u4fdd\u7559\u4e3b\u4f53\u5185\u90e8\u8fb9\u4e0e\u4e3b\u4f53-\u90bb\u5c45\u8fb9\uff1b\u90bb\u5c45\u4e4b\u95f4\u7684\u8fb9\u88c1\u6389\uff0c\u907f\u514d\u89c6\u56fe\u53d1\u6563\u3002
        visible_edges = [
            edge
            for edge in valid_edges
            if str(edge.get("source") or "") in visible_node_ids
            and str(edge.get("target") or "") in visible_node_ids
            and (str(edge.get("source") or "") in primary_ids or str(edge.get("target") or "") in primary_ids)
        ][:max_items * 2]
        if category == "characters":
            visible_edges = self._merge_relationship_snapshot_edges(
                root,
                nodes=primary_nodes,
                existing_edges=visible_edges,
                allow_new_nodes=False,
            )

        graph_nodes = [*primary_nodes, *neighbor_nodes][:max_items]

        return {
            "mode": "category",
            "query": normalized_q,
            "category": category,
            "entryId": normalized_entry_id,
            "nodeId": normalized_node_id,
            "depth": max_depth,
            "limit": max_items,
            "entries": [entry_by_id[entry_ref] for entry_ref in matched_entry_ids if entry_ref in entry_by_id],
            "graph": {
                "nodes": graph_nodes,
                "edges": visible_edges,
            },
            "matchedEntryIds": matched_entry_ids,
            "total": {
                "entryCount": len(matched_entry_ids),
                "nodeCount": len(graph_nodes),
                "edgeCount": len(visible_edges),
            },
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
        entries: Sequence[Dict[str, Any]],
        entry_by_id: Dict[str, Dict[str, Any]],
        nodes: Sequence[Dict[str, Any]],
        valid_edges: Sequence[Dict[str, Any]],
        category_labels: Dict[str, Any],
    ) -> Dict[str, Any]:
        """角色关系视图只发布已知角色之间、可逐字核对证据的显式关系。"""
        category_entries = [
            entry
            for entry in entries
            if str(entry.get("category") or "") == category and str(entry.get("id") or "")
        ]
        matched_entry_ids = [str(entry.get("id") or "") for entry in category_entries][:max_items]
        character_nodes = [
            self._wiki_content_node(node)
            for node in nodes
            if str(node.get("id") or "").strip()
            and not self._is_wiki_hub_node(node)
            and str(node.get("type") or "") == "character"
            and not bool(node.get("quarantined"))
        ]
        node_by_id = {str(node.get("id") or ""): node for node in character_nodes}

        source_cache = self._collect_sources(root)
        grounded_edges = [
            edge
            for edge in valid_edges
            if str(edge.get("type") or "") == "relationship"
            and str(edge.get("source") or "") in node_by_id
            and str(edge.get("target") or "") in node_by_id
            and self._is_publishable_relationship_edge(
                root,
                edge,
                nodes=character_nodes,
                sources=source_cache,
            )
        ]

        graph_edges = self._merge_relationship_snapshot_edges(
            root,
            nodes=character_nodes,
            existing_edges=grounded_edges,
            allow_new_nodes=False,
        )[: max_items * 2]

        # \u6709\u5173\u7cfb\u7684\u89d2\u8272\u6392\u524d\u9762\uff0c\u5b64\u7acb\u89d2\u8272\u6bbf\u540e\uff0c\u8d85\u51fa\u9884\u7b97\u7684\u5b64\u7acb\u89d2\u8272\u88c1\u6389\u3002
        connected_ids: set[str] = set()
        for edge in graph_edges:
            connected_ids.add(str(edge.get("source") or ""))
            connected_ids.add(str(edge.get("target") or ""))
        ordered_nodes = [
            *[node for node in character_nodes if str(node.get("id") or "") in connected_ids],
            *[node for node in character_nodes if str(node.get("id") or "") not in connected_ids],
        ][:max_items]
        visible_ids = {str(node.get("id") or "") for node in ordered_nodes}
        graph_edges = [
            edge
            for edge in graph_edges
            if str(edge.get("source") or "") in visible_ids
            and str(edge.get("target") or "") in visible_ids
        ]

        return {
            "mode": "category",
            "query": normalized_q,
            "category": category,
            "entryId": normalized_entry_id,
            "nodeId": normalized_node_id,
            "depth": max_depth,
            "limit": max_items,
            "entries": [entry_by_id[entry_ref] for entry_ref in matched_entry_ids if entry_ref in entry_by_id],
            "graph": {
                "nodes": ordered_nodes,
                "edges": graph_edges,
            },
            "matchedEntryIds": matched_entry_ids,
            "total": {
                "entryCount": len(matched_entry_ids),
                "nodeCount": len(ordered_nodes),
                "edgeCount": len(graph_edges),
            },
        }

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
        if status != "asserted" or bool(edge.get("needsReview")):
            return False
        if any(key in edge for key in ("level", "strength", "confidence", "polarity")):
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
            source = resolve_endpoint(raw_edge.get("source"))
            target = resolve_endpoint(raw_edge.get("target"))
            if not source or not target or source == target:
                continue
            dimension = str(raw_edge.get("dimension") or "").strip().lower()
            semantics = semantics_for_dimension(dimension)
            if semantics.status != "asserted" or semantics.relation_type == "unknown":
                continue
            raw_status = str(raw_edge.get("status") or "asserted").strip()
            if raw_status != "asserted":
                continue
            history = raw_edge.get("history") if isinstance(raw_edge.get("history"), list) else []
            latest = next((item for item in reversed(history) if isinstance(item, dict)), {})
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
            relation_type = semantics.relation_type
            key = (*sorted((source, target)), relation_type)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            relationship_edge = {
                "source": source,
                "target": target,
                "label": RELATIONSHIP_DIMENSION_LABELS[dimension],
                "type": "relationship",
                "weight": 1,
                "dimension": dimension,
                "relationType": relation_type,
                "status": "asserted",
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

    def _wiki_project_hub_node(self, payload: Dict[str, Any], content_entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        label = str(payload.get("projectName") or "").strip() or "\u9879\u76ee"
        needs_review_count = sum(1 for entry in content_entries if bool(entry.get("needsReview")))
        summary = str(payload.get("summary") or "").strip() or f"{len(content_entries)} \u4e2a WIKI \u6761\u76ee"
        return {
            "id": "project:root",
            "label": label,
            "type": "project",
            "category": "overview",
            "entryId": "",
            "summary": summary,
            "synthetic": True,
            "role": "projectHub",
            "selectable": False,
            "count": len(content_entries),
            "needsReviewCount": needs_review_count,
        }

    def _wiki_category_hub_node(
        self,
        category: str,
        category_labels: Dict[str, Any],
        category_entries: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        label = self._wiki_category_label(category, category_labels)
        needs_review_count = sum(1 for entry in category_entries if bool(entry.get("needsReview")))
        summary = f"{label}: {len(category_entries)} \u4e2a\u6761\u76ee"
        if needs_review_count:
            summary = f"{summary}, {needs_review_count} \u4e2a\u5f85\u786e\u8ba4"
        return {
            "id": f"category:{category}",
            "label": label,
            "type": "categoryHub",
            "category": category,
            "entryId": "",
            "summary": summary,
            "synthetic": True,
            "role": "categoryHub",
            "selectable": False,
            "count": len(category_entries),
            "needsReviewCount": needs_review_count,
        }

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
            if not path.is_file() or path.suffix.lower() not in SCAN_SUFFIXES:
                continue
            rel_parts = path.relative_to(root).parts
            if any(part in EXCLUDED_PARTS for part in rel_parts):
                continue
            rel = path.relative_to(root).as_posix()
            if self._should_skip_source_path(rel):
                continue
            text = self._read_source_text(path)
            if not text:
                continue
            kind = self._source_kind(rel)
            sources.append({
                "relativePath": rel,
                "title": path.stem,
                "kind": kind,
                "text": text,
                "size": path.stat().st_size,
                "sha256": sha256(text.encode("utf-8", errors="ignore")).hexdigest(),
                "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            })
        return sorted(sources, key=lambda item: self._source_sort_key(str(item["relativePath"])))

    def _should_skip_source_path(self, relative_path: str) -> bool:
        normalized = relative_path.replace("\\", "/")
        normalized_lower = normalized.lower()
        if Path(normalized).name.lower() == "readme.md":
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
        if "/characters/" in f"/{normalized}":
            return "character"
        if "/worldbook/" in f"/{normalized}":
            return "world"
        if "/presets/" in f"/{normalized}":
            return "preset"
        if "/memory/" in f"/{normalized}":
            return "memory"
        return "project"

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
        character_sources = [source for source in self._collect_sources(root) if source.get("kind") == "character"]
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
            normalized = str(relative_path or "").strip().replace("\\", "/").lower()
            return normalized.startswith(".storydex/characters/")

        has_card_managed_record = any(
            any(is_character_card_path(path) for path in record_source_paths(item))
            for item in entities
        )
        if not character_sources and not has_card_managed_record:
            return

        matched_record_ids: set[int] = set()
        for source in character_sources:
            names = self._character_names_from_source(source)
            if not names:
                continue
            display_name = names[0]
            relative_path = str(source.get("relativePath") or "")
            stable_id = self._stable_entity_id_from_source(source)
            match: Dict[str, Any] | None = None
            if stable_id:
                match = next((item for item in entities if record_id(item) == stable_id), None)
            if match is None:
                match = next(
                    (
                        item
                        for item in entities
                        if relative_path
                        and relative_path in record_source_paths(item)
                    ),
                    None,
                )
            if match is None:
                match = next(
                    (
                        item
                        for item in entities
                        if display_name == record_name(item)
                        or display_name in [str(alias).strip() for alias in item.get("aliases", [])]
                    ),
                    None,
                )
            if match is None:
                match = {}
                entities.append(match)

            previous_name = record_name(match)
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
                "entityId": stable_id or record_id(match) or f"char_{uuid4().hex}",
                "canonical_name": display_name,
                "aliases": list(dict.fromkeys(alias for alias in aliases if alias and alias != display_name)),
                "kind": "character",
                "status": "active",
                "sourcePaths": list(dict.fromkeys(source_paths)),
            })
            match.pop("source_paths", None)
            matched_record_ids.add(id(match))

        # 只有曾由角色卡拥有、且本轮没有任何现存角色卡匹配的记录才归档。
        # 纯 registry 角色没有角色卡路径，仍由 Story Knowledge 自身管理。
        for item in entities:
            if id(item) in matched_record_ids:
                continue
            source_paths = record_source_paths(item)
            if not any(is_character_card_path(path) for path in source_paths):
                continue
            kind = str(item.get("kind") or "").strip().lower()
            if kind and kind not in {"character", "person", "role"}:
                continue
            item["status"] = "archived"
            item["sourcePaths"] = [
                path for path in source_paths if not is_character_card_path(path)
            ]
            item.pop("source_paths", None)

        next_payload = {
            **payload,
            "version": max(2, int(payload.get("version") or 1)),
            "entities": entities,
        }
        if next_payload != loaded:
            self._write_json_atomic(registry_path, next_payload)

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
        for record in registry.load_records():
            self._add_entity(entities_by_name, self._entity_from_record(record))

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

        return sorted(
            entities_by_name.values(),
            key=lambda entity: (
                -self._entity_score(entity, sources),
                str(entity.get("category") or ""),
                str(entity.get("type") or ""),
                str(entity.get("name") or ""),
            ),
        )

    def _collect_character_names(self, root: Path, sources: Sequence[Dict[str, Any]]) -> List[str]:
        return [str(entity["name"]) for entity in self._collect_entities(root, sources) if entity["type"] == "character"]

    def _entity_from_record(self, record: EntityRecord) -> Dict[str, Any]:
        node_type = self._entity_type_for_kind(record.kind)
        return {
            "name": record.canonical_name,
            "entityId": record.entity_id,
            "kind": record.kind or node_type,
            "type": node_type,
            "category": self._entity_category_for_type(node_type),
            "aliases": list(record.aliases),
            "sourcePaths": list(record.source_paths) or [ENTITY_SOURCE_PATH],
            "needsReview": False,
        }

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
        stable_id = str(entity.get("entityId") or entity.get("entity_id") or "").strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{1,159}", stable_id):
            return stable_id
        node_type = str(entity.get("type") or "setting")
        return f"{node_type}:{self._slug(str(entity.get('name') or 'item'))}"

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
