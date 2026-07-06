from __future__ import annotations

import json
import re
from hashlib import sha256
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence
from uuid import uuid4

from services.entity_registry import EntityRecord, EntityRegistry


TEXT_SUFFIXES = {".md", ".txt"}
DATA_SUFFIXES = {".json", ".jsonl"}
SCAN_SUFFIXES = TEXT_SUFFIXES | DATA_SUFFIXES
EXCLUDED_PARTS = {".git", "__pycache__", ".cache", "traces", "sessions"}
EXCLUDED_RELATIVE_PREFIXES = (
    ".storydex/wiki/",
    ".storydex/.agent/",
    ".storydex/templates/",
    ".storydex/presets/",
    ".storydex/scripts/",
    ".storydex/config/",
    ".storydex/temp/",
)
ENTITY_SOURCE_PATH = ".storydex/memory/current/entities.json"
FACT_SOURCE_PATH = ".storydex/memory/current/facts.json"

WIKI_CATEGORY_SCHEMA_VERSION = "story-wiki-v3-entity-source"
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
        "label": "\u4ec5\u5237\u65b0\u56fe\u7ed3\u6784",
        "description": "\u4fdd\u7559 WIKI \u6761\u76ee\u6587\u672c\uff0c\u7531 Agent \u91cd\u65b0\u68c0\u67e5\u548c\u4fee\u6b63\u8282\u70b9/\u8fb9\u5173\u7cfb\u3002",
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
        wiki_path = self.wiki_json_path(root)
        if not force and wiki_path.exists():
            try:
                data = json.loads(wiki_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("entries") and data.get("graph"):
                    if not self._has_current_category_schema(data):
                        return self.rebuild(root)
                    return data
            except Exception:
                pass
        return self.rebuild(root)

    def rebuild(self, workspace_root: Path) -> Dict[str, Any]:
        root = workspace_root.resolve()
        sources = self._collect_sources(root)
        registry = EntityRegistry(root)
        entities = self._collect_entities(root, sources)
        character_entities = [entity for entity in entities if entity["type"] == "character"]
        character_names = [str(entity["name"]) for entity in character_entities]
        chapter_sources = [item for item in sources if item["kind"] == "chapter"]
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

        if not chapter_sources and not entities:
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
                "generator": "local-fallback-wiki",
                "generationMode": "local fallback",
                "llmStatus": "not_configured_or_not_required",
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
                workflow="generate_wiki",
                status="completed",
                agent_result=None,
                sources=sources,
                changed_paths=[item["relativePath"] for item in sources],
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

        world_sources = self._sources_by_keywords(sources, ["world", "\u4e16\u754c", "\u4fee", "\u5b97", "\u95e8", "\u6cd5", "\u7075"])
        if world_sources:
            entries.append(self._entry(
                "world:system",
                "\u4e16\u754c\u89c2\u4e0e\u4fee\u70bc\u4f53\u7cfb",
                "setting",
                self._summarize_sources(world_sources, "\u4e16\u754c\u89c2\u4e0e\u4fee\u70bc\u4f53\u7cfb\u4ecd\u5728\u9879\u76ee\u8d44\u6599\u4e2d\u9010\u6b65\u5f62\u6210\u3002"),
                self._details_from_sources(world_sources, limit=10),
                [item["relativePath"] for item in world_sources[:8]],
            ))
            graph_nodes.append({
                "id": "world:system",
                "label": "\u4fee\u771f\u4e16\u754c",
                "type": "world",
                "category": "setting",
                "entryId": "world:system",
                "summary": "\u4ece\u8bb0\u5fc6\u3001\u89d2\u8272\u8bbe\u5b9a\u548c\u6b63\u6587\u4e2d\u62bd\u53d6\u7684\u4e16\u754c\u89c2\u6838\u5fc3\u3002",
            })
            graph_edges.append(self._edge(project_id, "world:system", "\u5305\u542b", "world"))

        if chapter_sources:
            plot_details = self._chapter_plot_details(chapter_sources)
            entries.append(self._entry(
                "plot:mainline",
                "\u4e3b\u7ebf\u5267\u60c5",
                "plot",
                self._build_plot_summary(chapter_sources),
                plot_details,
                [item["relativePath"] for item in chapter_sources[:12]],
            ))
            graph_nodes.append({
                "id": "plot:mainline",
                "label": "\u4e3b\u7ebf",
                "type": "event",
                "category": "plot",
                "entryId": "plot:mainline",
                "summary": "\u9879\u76ee\u5df2\u6709\u7ae0\u8282\u4e32\u8054\u7684\u4e3b\u7ebf\u5267\u60c5\u3002",
            })
            graph_edges.append(self._edge(project_id, "plot:mainline", "\u63a8\u8fdb", "plot"))

        chapter_mentions = self._chapter_mentions_by_path(registry, chapter_sources, character_names)

        for index, source in enumerate(chapter_sources):
            entry_id = f"chapter:{index + 1}"
            chapter_title = self._display_title(source["relativePath"], source["title"])
            summary = self._compress_text(source["text"], 260) or "\u7ae0\u8282\u5185\u5bb9\u6682\u672a\u586b\u5145\u3002"
            entries.append(self._entry(
                entry_id,
                chapter_title,
                "plot",
                summary,
                self._chapter_details(source, chapter_mentions.get(source["relativePath"], ())),
                [source["relativePath"]],
            ))
            node_id = f"chapter:{index + 1}"
            graph_nodes.append({
                "id": node_id,
                "label": chapter_title,
                "type": "chapter",
                "category": "plot",
                "entryId": entry_id,
                "summary": summary,
            })
            graph_edges.append(self._edge("plot:mainline", node_id, "\u7ae0\u8282", "timeline", weight=max(1, index + 1)))

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
                chapter_idx = chapter_sources.index(source) + 1 if source in chapter_sources else 0
                if chapter_idx:
                    graph_edges.append(self._edge(node_id, f"chapter:{chapter_idx}", "\u51fa\u573a", "appearance"))

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

        # \u5171\u73b0\u6309\u89d2\u8272\u5bf9\u805a\u5408\u6210\u4e00\u6761\u8fb9\uff1aweight=\u5171\u73b0\u7ae0\u8282\u6570\uff0cevidence=\u7ae0\u8282\u6e05\u5355\uff0c\u907f\u514d\u9010\u7ae0\u5237\u5c4f\u3002
        co_occurrence: Dict[tuple, List[str]] = {}
        for source in chapter_sources:
            mentioned = list(chapter_mentions.get(source["relativePath"], ()))
            for left_idx, left in enumerate(mentioned):
                for right in mentioned[left_idx + 1:]:
                    key = (self._slug(left), self._slug(right))
                    co_occurrence.setdefault(key, []).append(source["relativePath"])
        for (left_slug, right_slug), chapters in co_occurrence.items():
            graph_edges.append(self._edge(
                f"character:{left_slug}",
                f"character:{right_slug}",
                "\u5171\u73b0",
                "relationship",
                evidence="\u3001".join(chapters[:6]),
                weight=len(chapters),
                co_occurrence=True,
            ))

        self._append_fact_edges(root, graph_nodes, graph_edges, registry=registry, entities=entities)
        self._append_topic_entries(entries, graph_nodes, graph_edges, project_id, sources)
        if chapter_sources:
            self._append_timeline(entries, graph_nodes, graph_edges, chapter_sources)
        self._append_index(entries, graph_nodes, graph_edges)

        payload = {
            "version": 1,
            "categorySchemaVersion": WIKI_CATEGORY_SCHEMA_VERSION,
            "projectName": root.name,
            "workspaceRoot": root.as_posix(),
            "generatedAt": now,
            "generator": "local-fallback-wiki",
            "generationMode": "local fallback",
            "llmStatus": "not_configured_or_not_required",
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
            workflow="generate_wiki",
            status="completed",
            agent_result=None,
            sources=sources,
            changed_paths=[item["relativePath"] for item in sources],
        )

    def sync_local_incremental(self, workspace_root: Path) -> Dict[str, Any]:
        """保存/写作后自动同步：纯本地确定性增量合并，不触发 Agent、免 token、毫秒级。

        Agent 深度生成/更新仍由手动按钮走 run_agent_workflow，这里只保证图谱跟上文件变更。
        """
        root = workspace_root.resolve()
        before = self._read_existing_payload(root)
        sources = self._collect_sources(root)
        if before is None:
            # 尚无图谱，首次直接全量构建。
            return self.rebuild(root)

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
            return before

        incoming = self._build_incremental_payload(root, sources, changed_paths)
        merged = self.merge_payloads(
            before,
            incoming,
            removed_source_paths=removed_paths,
            mark_conflicts=True,
        )
        merged["generatedAt"] = datetime.now(timezone.utc).isoformat()
        merged["generator"] = before.get("generator") or "local-incremental-sync"
        merged["generationMode"] = "local incremental sync"
        merged["llmStatus"] = before.get("llmStatus") or "local"
        return self._persist_payload(
            root,
            merged,
            workflow="sync_local",
            status="completed",
            agent_result=None,
            sources=sources,
            changed_paths=changed_paths,
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
        chapter_mentions = self._chapter_mentions_by_path(registry, chapter_sources, character_names)
        entries: List[Dict[str, Any]] = []
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []

        for index, source in enumerate(chapter_sources):
            if source["relativePath"] not in changed_set:
                continue
            entry_id = f"chapter:{index + 1}"
            chapter_title = self._display_title(source["relativePath"], source["title"])
            summary = self._compress_text(source["text"], 260) or "章节内容暂未填充。"
            entries.append(self._entry(
                entry_id,
                chapter_title,
                "plot",
                summary,
                self._chapter_details(source, chapter_mentions.get(source["relativePath"], ())),
                [source["relativePath"]],
            ))
            nodes.append({
                "id": entry_id,
                "label": chapter_title,
                "type": "chapter",
                "category": "plot",
                "entryId": entry_id,
                "summary": summary,
            })
            edges.append(self._edge("plot:mainline", entry_id, "章节", "timeline", weight=max(1, index + 1)))
            mentioned = list(chapter_mentions.get(source["relativePath"], ()))
            for left_idx, left in enumerate(mentioned):
                for right in mentioned[left_idx + 1:]:
                    edges.append(self._edge(
                        f"character:{self._slug(left)}",
                        f"character:{self._slug(right)}",
                        "共现",
                        "relationship",
                        evidence=source["relativePath"],
                        co_occurrence=True,
                    ))

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
                chapter_idx = chapter_sources.index(source) + 1 if source in chapter_sources else 0
                if chapter_idx:
                    edges.append(self._edge(entry_id, f"chapter:{chapter_idx}", "出场", "appearance"))

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

        self._append_fact_edges(root, nodes, edges, registry=registry, entities=entities)

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
            "errorMessage": "Agent runner was not available.",
            "reply": "",
            "events": [],
            "traceId": trace_id,
        }
        agent_payload: Dict[str, Any] | None = None
        if agent_runner is not None:
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
            payload = before or self.rebuild(root)
            review_report = self._build_review_report(payload, agent_result=agent_result, agent_payload=agent_payload)
            self.review_report_path(root).write_text(
                json.dumps(review_report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            payload = self._annotate_payload(payload, workflow=normalized_workflow, agent_result=agent_result)
        elif agent_payload:
            incoming = self.normalize_payload(agent_payload, root=root, workflow=normalized_workflow)
            if normalized_workflow == "update_wiki" and before:
                payload = self.merge_payloads(before, incoming)
            elif normalized_workflow == "refresh_wiki_graph" and before:
                payload = self.merge_payloads(before, {"graph": incoming.get("graph", {})}, graph_only=True)
            else:
                payload = incoming
            payload = self._annotate_payload(payload, workflow=normalized_workflow, agent_result=agent_result)
        else:
            fallback_used = True
            if normalized_workflow == "update_wiki" and before:
                payload = self._annotate_payload(before, workflow=normalized_workflow, agent_result=agent_result)
                payload["generationMode"] = "local fallback incremental"
                payload["changedSourcePaths"] = changed_paths
            elif normalized_workflow == "refresh_wiki_graph" and before:
                payload = self._annotate_payload(before, workflow=normalized_workflow, agent_result=agent_result)
                payload["generationMode"] = "local fallback graph refresh"
            elif normalized_workflow == "repair_wiki" and before:
                payload = self.normalize_payload(before, root=root, workflow=normalized_workflow)
                payload = self._annotate_payload(payload, workflow=normalized_workflow, agent_result=agent_result)
                payload["generationMode"] = "local fallback repair"
            else:
                payload = self.rebuild(root)
                payload = self._annotate_payload(payload, workflow=normalized_workflow, agent_result=agent_result)

        if fallback_used and agent_result.get("attempted"):
            status = "fallback"
        elif not agent_result.get("attempted"):
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
            return self._query_wiki_category_graph(
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
            )
        else:
            return self._query_wiki_overview_graph(
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

        return {
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
        }

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
                    preserved.update({key: value for key, value in entry.items() if value not in ("", [], None)})
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
        payload = self._normalize_wiki_payload(payload)
        payload["categorySchemaVersion"] = WIKI_CATEGORY_SCHEMA_VERSION
        payload["categoryLabels"] = CATEGORY_LABELS
        payload.setdefault("nodeTypeLabels", NODE_TYPE_LABELS)
        payload.setdefault("workflowDefinitions", WIKI_WORKFLOW_DEFINITIONS)
        payload["lastWorkflow"] = workflow
        payload["lastWorkflowStatus"] = status
        payload["lastUpdatedAt"] = datetime.now(timezone.utc).isoformat()
        payload["changedSourcePaths"] = list(changed_paths)
        if agent_result is not None:
            payload["agent"] = {
                "attempted": bool(agent_result.get("attempted")),
                "completed": bool(agent_result.get("completed")),
                "traceId": str(agent_result.get("traceId") or ""),
                "errorMessage": str(agent_result.get("errorMessage") or ""),
                "eventCount": len(agent_result.get("events") or []),
            }
        self.wiki_json_path(root).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.wiki_markdown_path(root).write_text(self._render_markdown(payload), encoding="utf-8")
        self.wiki_index_path(root).write_text(
            json.dumps(
                self._build_index(root, payload, sources=sources, workflow=workflow, status=status, changed_paths=changed_paths),
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        return payload

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
            "version": 1,
            "projectName": root.name,
            "workspaceRoot": root.as_posix(),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "lastWorkflow": workflow,
            "lastStatus": status,
            "workflowDefinitions": WIKI_WORKFLOW_DEFINITIONS,
            "categorySchemaVersion": WIKI_CATEGORY_SCHEMA_VERSION,
            "allowedCategories": list(CATEGORY_LABELS),
            "changedSourcePaths": list(changed_paths),
            "sources": sources_index,
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
            "- generate_wiki: 全量阅读并重建 WIKI entries 与 graph。\n"
            "- update_wiki: 优先分析 changedSourcePaths，再结合 existingWiki 合并更新；不要粗暴覆盖旧条目。\n"
            "- refresh_wiki_graph: 保留已有 entries 语义，重点修正 graph.nodes 与 graph.edges。\n"
            "- review_wiki: 输出 review.issues 与 review.recommendations，标记缺漏、冲突、过时和需人工确认内容。\n"
            "- repair_wiki: 修复缺失字段、坏结构、不完整关系，保持稳定 id。\n"
            "角色节点只能来自权威实体、角色档案，或正文中明确出现且可被证据支持的人物；不确定节点必须 needsReview=true。\n"
            "category 只允许五类: overview(总览)、characters(角色)、setting(设定)、plot(剧情)、relationships(关系)。"
            "章节/事件/时间线归入 plot；世界/地点/物品/势力/伏笔归入 setting；不要输出其他 category。\n"
            "角色之间的关系必须表达为 graph.edges 中的角色-角色边(type=relationship，label 用关系名)；"
            "不要为关系本身创建节点，graph.nodes 中只放实体（角色/章节/设定等）。\n"
            "输出 JSON schema: {summary, entries:[{id,title,category,categoryLabel,summary,details,sourcePaths,confidence,needsReview}], "
            "graph:{nodes:[{id,label,type,category,entryId,summary,confidence,needsReview}], "
            "edges:[{source,target,label,type,weight,evidence,confidence,needsReview}]}, review:{issues,recommendations}}。\n"
            "保持 entry id 和 node id 稳定；sourcePaths 必须引用真实项目相对路径；不确定事实必须 needsReview=true；不要把猜测写成定论。\n"
            "如果发现旧 WIKI 中有高质量内容且相关源文件未变化，应保留其 id、摘要和关系，只补充必要的新证据。\n"
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
        title = str(item.get("title") or item.get("id") or "\u672a\u547d\u540d\u6761\u76ee").strip()
        entry_id = str(item.get("id") or f"{category}:{self._slug(title)}").strip()
        return {
            "id": entry_id,
            "title": title,
            "category": category,
            "categoryLabel": str(item.get("categoryLabel") or CATEGORY_LABELS.get(category, category)),
            "summary": str(item.get("summary") or ""),
            "details": [str(value) for value in item.get("details", []) if str(value).strip()] if isinstance(item.get("details"), list) else [],
            "sourcePaths": [str(value) for value in item.get("sourcePaths", []) if str(value).strip()] if isinstance(item.get("sourcePaths"), list) else [],
            "confidence": self._confidence(item.get("confidence")),
            "needsReview": bool(item.get("needsReview", False)),
            "updatedAt": str(item.get("updatedAt") or datetime.now(timezone.utc).isoformat()),
        }

    def _normalize_node(self, item: Dict[str, Any]) -> Dict[str, Any]:
        label = str(item.get("label") or item.get("id") or "\u8282\u70b9").strip()
        node_type = str(item.get("type") or "event").strip() or "event"
        node_id = str(item.get("id") or f"{node_type}:{self._slug(label)}").strip()
        category = self._normalize_wiki_category(item.get("category")) if str(item.get("category") or "").strip() else ""
        # 交叉规范化：仅 category=characters 的节点强制 type=character，
        # 修正 Agent 输出 "动机"/"定位"/"小说" 等中文 type。
        # relationships 类目的节点（关系条目、索引等）不是角色，
        # 强转会把它们混入角色关系网络，因此保持原 type。
        if category == "characters":
            node_type = "character"
        return {
            "id": node_id,
            "label": label,
            "type": node_type,
            "category": category,
            "entryId": str(item.get("entryId") or ""),
            "summary": str(item.get("summary") or ""),
            "confidence": self._confidence(item.get("confidence")),
            "needsReview": bool(item.get("needsReview", False)),
        }

    def _normalize_graph_edge(self, item: Dict[str, Any]) -> Dict[str, Any]:
        edge: Dict[str, Any] = {
            "source": str(item.get("source") or ""),
            "target": str(item.get("target") or ""),
            "label": str(item.get("label") or item.get("type") or "\u5173\u8054"),
            "type": str(item.get("type") or "related"),
            "weight": int(item.get("weight") or 1),
            "evidence": str(item.get("evidence") or ""),
            "confidence": self._confidence(item.get("confidence")),
            "needsReview": bool(item.get("needsReview", False)),
        }
        if item.get("coOccurrence"):
            edge["coOccurrence"] = True
        return edge

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
            return "local fallback"
        return {
            "generate_wiki": "agent full",
            "update_wiki": "agent incremental",
            "refresh_wiki_graph": "agent graph refresh",
            "review_wiki": "agent review",
            "repair_wiki": "agent repair",
        }.get(workflow, "agent")

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
        """\u5173\u7cfb\u89c6\u56fe = \u7eaf\u89d2\u8272\u7f51\u7edc\uff1a\u53ea\u6709\u89d2\u8272\u8282\u70b9\uff0c\u53ea\u6709\u89d2\u8272-\u89d2\u8272\u8fb9\uff0c\u65e0\u4efb\u4f55 hub/\u7d22\u5f15\u8282\u70b9\u3002

        \u8fb9\u4f18\u5148\u7ea7\uff1a\u5199\u4f5c\u6f14\u8fdb\u7ba1\u7ebf\u7684\u771f\u5b9e\u5173\u7cfb\uff08relationship_graph.json\uff0c\u5e26\u7ef4\u5ea6\u4e0e\u5f3a\u5ea6\uff09
        > Agent/\u672c\u5730\u751f\u6210\u7684 wiki \u5173\u7cfb\u8fb9 > \u7ae0\u8282\u5171\u73b0\uff08\u5f31\u5316\uff0c\u4e14\u4ec5\u5f53\u8be5\u89d2\u8272\u5bf9\u6ca1\u6709\u771f\u5b9e\u5173\u7cfb\u65f6\uff09\u3002
        """
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
        ]
        node_by_id = {str(node.get("id") or ""): node for node in character_nodes}

        wiki_relation_edges = [
            edge
            for edge in valid_edges
            if str(edge.get("type") or "") == "relationship"
            and str(edge.get("source") or "") in node_by_id
            and str(edge.get("target") or "") in node_by_id
        ]
        real_edges = [edge for edge in wiki_relation_edges if not edge.get("coOccurrence")]
        co_edges = [edge for edge in wiki_relation_edges if edge.get("coOccurrence")]

        # \u5408\u5e76\u5199\u4f5c\u6f14\u8fdb\u7ba1\u7ebf\u7684\u771f\u5b9e\u5173\u7cfb\uff1b\u7f3a\u5931\u7684\u89d2\u8272\u8282\u70b9\u4f1a\u8865\u5efa\u3002
        snapshot_edges = self._merge_relationship_snapshot_edges(
            root,
            nodes=character_nodes,
            existing_edges=[],
            allow_new_nodes=True,
        )

        def pair_key(edge: Dict[str, Any]) -> tuple:
            return tuple(sorted((str(edge.get("source") or ""), str(edge.get("target") or ""))))

        real_pairs = {pair_key(edge) for edge in snapshot_edges} | {pair_key(edge) for edge in real_edges}
        kept_co_edges = [edge for edge in co_edges if pair_key(edge) not in real_pairs]
        graph_edges = [*snapshot_edges, *real_edges, *kept_co_edges][: max_items * 2]

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

    def _merge_relationship_snapshot_edges(
        self,
        root: Path,
        *,
        nodes: List[Dict[str, Any]],
        existing_edges: List[Dict[str, Any]],
        allow_new_nodes: bool,
    ) -> List[Dict[str, Any]]:
        """\u628a\u5199\u4f5c\u6f14\u8fdb\u7ba1\u7ebf\u7ef4\u62a4\u7684\u771f\u5b9e\u89d2\u8272\u5173\u7cfb\uff08trust/hostility \u7b49\u7ef4\u5ea6\uff09\u5408\u5e76\u8fdb wiki \u56fe\u3002

        nodes \u5217\u8868\u4f1a\u88ab\u539f\u5730\u6269\u5145\uff08allow_new_nodes=True \u65f6\u4e3a\u5feb\u7167\u91cc\u51fa\u73b0\u4f46 wiki
        \u5c1a\u672a\u6536\u5f55\u7684\u89d2\u8272\u8865\u5efa\u8282\u70b9\uff09\uff0c\u8fd4\u56de existing_edges + \u8f6c\u6362\u540e\u7684\u7ef4\u5ea6\u8fb9\u3002
        """
        snapshot_path = self._relationship_snapshot_path(root)
        if not snapshot_path.exists():
            return existing_edges
        try:
            loaded = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            return existing_edges
        raw_edges = loaded.get("edges") if isinstance(loaded, dict) else None
        if not isinstance(raw_edges, list):
            return existing_edges

        label_to_id: Dict[str, str] = {}
        for node in nodes:
            node_id = str(node.get("id") or "")
            label = str(node.get("label") or "").strip()
            if node_id:
                label_to_id.setdefault(node_id, node_id)
            if label:
                label_to_id.setdefault(label, node_id)

        def resolve_endpoint(raw_name: Any) -> str:
            name = str(raw_name or "").strip()
            if not name:
                return ""
            if name in label_to_id:
                return label_to_id[name]
            slug_id = f"character:{self._slug(name)}"
            if slug_id in label_to_id:
                return slug_id
            if not allow_new_nodes:
                return ""
            node = {
                "id": slug_id,
                "label": name,
                "type": "character",
                "category": "characters",
                "entryId": "",
                "summary": "\u6765\u81ea\u5199\u4f5c\u6f14\u8fdb\u5173\u7cfb\u56fe\u7684\u89d2\u8272\u3002",
                "synthetic": False,
                "selectable": True,
            }
            nodes.append(node)
            label_to_id[name] = slug_id
            label_to_id[slug_id] = slug_id
            return slug_id

        merged = list(existing_edges)
        seen_keys = {
            (str(edge.get("source") or ""), str(edge.get("target") or ""), str(edge.get("label") or ""))
            for edge in merged
        }
        for raw_edge in raw_edges:
            if not isinstance(raw_edge, dict):
                continue
            source = resolve_endpoint(raw_edge.get("source"))
            target = resolve_endpoint(raw_edge.get("target"))
            if not source or not target or source == target:
                continue
            dimension = str(raw_edge.get("dimension") or "").strip().lower()
            label = RELATIONSHIP_DIMENSION_LABELS.get(dimension, dimension or "\u5173\u7cfb")
            key = (source, target, label)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            level = self._safe_int(raw_edge.get("current_level"), fallback=0)
            history = raw_edge.get("history") if isinstance(raw_edge.get("history"), list) else []
            latest = history[-1] if history and isinstance(history[-1], dict) else {}
            merged.append({
                "source": source,
                "target": target,
                "label": label,
                "type": "relationship",
                "weight": max(1, abs(level)),
                "level": level,
                "dimension": dimension,
                "evidence": str(latest.get("detail") or ""),
                "confidence": 0.9,
                "needsReview": False,
            })
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
                        "kind": "character",
                        "type": "character",
                        "category": "characters",
                        "aliases": [name] if name != canonical_name else [],
                        "sourcePaths": [source["relativePath"]],
                        "needsReview": False,
                    })

        if not entities_by_name:
            for entity in self._fallback_character_entities(sources):
                self._add_entity(entities_by_name, entity)

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
            "kind": record.kind or node_type,
            "type": node_type,
            "category": self._entity_category_for_type(node_type),
            "aliases": list(record.aliases),
            "sourcePaths": [ENTITY_SOURCE_PATH],
            "needsReview": False,
        }

    def _add_entity(self, entities_by_name: Dict[str, Dict[str, Any]], entity: Dict[str, Any]) -> None:
        name = str(entity.get("name") or "").strip()
        if not name:
            return
        incoming = dict(entity)
        incoming["name"] = name
        incoming["aliases"] = [str(item).strip() for item in incoming.get("aliases", []) if str(item).strip() and str(item).strip() != name]
        incoming["sourcePaths"] = [str(item) for item in incoming.get("sourcePaths", []) if str(item).strip()]
        existing = entities_by_name.get(name)
        if existing is None:
            entities_by_name[name] = incoming
            return
        existing["aliases"] = list(dict.fromkeys([*existing.get("aliases", []), *incoming.get("aliases", [])]))
        existing["sourcePaths"] = list(dict.fromkeys([*existing.get("sourcePaths", []), *incoming.get("sourcePaths", [])]))
        existing["needsReview"] = bool(existing.get("needsReview") or incoming.get("needsReview"))

    def _character_names_from_source(self, source: Dict[str, Any]) -> List[str]:
        names: List[str] = []
        stem = Path(str(source["relativePath"])).stem
        stem = re.sub(r"^\d+[_\-\s]*", "", stem)
        stem = re.sub(r"^角色[_\-\s]*", "", stem)
        if stem and stem.upper() != "README":
            names.append(stem)
        try:
            loaded = json.loads(str(source.get("text") or ""))
        except Exception:
            loaded = None
        if isinstance(loaded, dict):
            for key in ("name", "character_name", "characterName", "displayName"):
                value = str(loaded.get(key) or "").strip()
                if value:
                    names.append(value)
        for key in ("name", "character_name", "displayName"):
            match = re.search(rf'"{key}"\s*:\s*"([^"]+)"', str(source.get("text") or ""))
            if match:
                names.append(match.group(1).strip())
        return list(dict.fromkeys(name for name in names if name))

    def _fallback_character_entities(self, sources: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        counter: Counter[str] = Counter()
        token_sources: Dict[str, List[str]] = {}
        chapter_sources = [source for source in sources if source.get("kind") == "chapter"]
        for source in sources:
            if source.get("kind") != "chapter":
                continue
            body_text = re.sub(r"^\s*#+\s+.*$", "", source["text"][:20000], flags=re.MULTILINE)
            seen_in_source = set()
            for token in re.findall(r"[\u4e00-\u9fff]{2,5}", body_text):
                if token in CHARACTER_TOKEN_BLACKLIST:
                    continue
                if token.endswith(("\u4e4b\u4f53", "\u4e4b\u8c1c")):
                    continue
                seen_in_source.add(token)
            for token in seen_in_source:
                counter[token] += 1
                token_sources.setdefault(token, []).append(str(source["relativePath"]))
        if len(chapter_sources) < 2:
            return []
        return [
            {
                "name": name,
                "kind": "character",
                "type": "character",
                "category": "characters",
                "aliases": [],
                "sourcePaths": token_sources.get(name, []),
                "needsReview": True,
            }
            for name, count in counter.most_common(12)
            if count >= 2
        ]

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
    ) -> Dict[str, Any]:
        return {
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

    def _summarize_sources(self, sources: Sequence[Dict[str, Any]], fallback: str) -> str:
        snippets = [self._compress_text(str(source["text"]), 120) for source in sources[:4]]
        return " ".join(filter(None, snippets)) or fallback

    def _details_from_sources(self, sources: Sequence[Dict[str, Any]], *, limit: int = 8) -> List[str]:
        details: List[str] = []
        for source in sources[:limit]:
            snippet = self._compress_text(str(source["text"]), 180)
            if snippet:
                details.append(f"{source['relativePath']}: {snippet}")
        return details

    def _sources_by_keywords(self, sources: Sequence[Dict[str, Any]], keywords: Sequence[str]) -> List[Dict[str, Any]]:
        result = []
        for source in sources:
            haystack = f"{source['relativePath']} {source['text'][:5000]}".lower()
            if any(keyword.lower() in haystack for keyword in keywords):
                result.append(source)
        return result

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
        for source in sources:
            if source["kind"] != "character":
                continue
            haystack = f"{source['relativePath']}\n{source['text'][:2000]}"
            for entity in entities:
                name = str(entity["name"])
                aliases = [str(alias) for alias in entity.get("aliases", []) if str(alias).strip()]
                if any(token and token in haystack for token in [name, *aliases]):
                    mapping[name].append(source)
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
            evidence_parts = [FACT_SOURCE_PATH]
            established_in = str(item.get("established_in") or item.get("establishedIn") or "").strip()
            evidence_text = str(item.get("evidence") or "").strip()
            if established_in:
                evidence_parts.append(established_in)
            if evidence_text:
                evidence_parts.append(evidence_text)
            edge = self._edge(
                source_id,
                target_id,
                predicate,
                "fact",
                evidence=" | ".join(evidence_parts),
            )
            confidence = str(item.get("confidence") or "").strip().lower()
            edge["confidence"] = 0.86 if confidence in {"canon", "confirmed", ""} else 0.62
            edge["needsReview"] = confidence not in {"canon", "confirmed", ""}
            graph_edges.append(edge)

    def _append_topic_entries(
        self,
        entries: List[Dict[str, Any]],
        graph_nodes: List[Dict[str, Any]],
        graph_edges: List[Dict[str, Any]],
        project_id: str,
        sources: Sequence[Dict[str, Any]],
    ) -> None:
        # \u4e3b\u9898\u6761\u76ee\u76f4\u63a5\u843d\u76d8\u4e94\u5206\u7c7b category\uff1b\u8282\u70b9 type \u4fdd\u7559\u7ec6\u7c92\u5ea6\u4ee5\u4fbf\u7740\u8272\u3002
        # \u4e0d\u518d\u751f\u6210 "relationships:main" \u7d22\u5f15\u8282\u70b9\uff1a\u5173\u7cfb\u89c6\u56fe\u76f4\u63a5\u5c55\u793a\u89d2\u8272\u7f51\u7edc\uff0c
        # \u8be5\u5360\u4f4d\u8282\u70b9\u66fe\u88ab\u8bef\u5f52\u4e3a\u89d2\u8272\u800c\u6df7\u5165\u5173\u7cfb\u56fe\u3002
        topics = [
            ("factions:main", "\u52bf\u529b\u4e0e\u9635\u8425", "setting", "faction", ["\u5b97", "\u95e8", "\u6d3e", "\u7ec4\u7ec7", "\u52bf\u529b"]),
            ("locations:main", "\u5730\u70b9\u4e0e\u573a\u666f", "setting", "location", ["\u5c71", "\u6d1e", "\u6bbf", "\u9635", "\u5893", "\u573a", "\u591c"]),
            ("items:main", "\u7269\u54c1\u3001\u529f\u6cd5\u4e0e\u672f\u6cd5", "setting", "item", ["\u4e39", "\u836f", "\u9732", "\u6cd5", "\u529f", "\u672f", "\u7075", "\u94fe"]),
            ("events:main", "\u5173\u952e\u4e8b\u4ef6", "plot", "event", ["\u9047", "\u6218", "\u95ee", "\u5931\u5fc6", "\u5c01", "\u7981", "\u53d1\u73b0"]),
            ("foreshadow:main", "\u4f0f\u7b14\u4e0e\u672a\u89e3\u8c1c\u56e2", "setting", "foreshadow", ["\u8c1c", "\u4f0f\u7b14", "\u7981", "\u5c01", "\u771f\u76f8", "\u8eab\u4efd"]),
        ]
        for entry_id, title, category, node_type, keywords in topics:
            matched = self._sources_by_keywords(sources, keywords)
            if not matched:
                continue
            entries.append(self._entry(
                entry_id,
                title,
                category,
                self._summarize_sources(matched, f"{title}\u6682\u65f6\u4ee5\u672c\u5730\u7d22\u5f15\u65b9\u5f0f\u7ef4\u62a4\uff0c\u7b49\u5f85\u66f4\u591a\u521b\u4f5c\u5185\u5bb9\u7ec6\u5316\u3002"),
                self._details_from_sources(matched, limit=8),
                [item["relativePath"] for item in matched[:8]],
            ))
            graph_nodes.append({
                "id": entry_id,
                "label": title,
                "type": node_type,
                "category": category,
                "entryId": entry_id,
                "summary": title,
            })
            graph_edges.append(self._edge(project_id, entry_id, CATEGORY_LABELS.get(category, category), category))

    def _append_timeline(
        self,
        entries: List[Dict[str, Any]],
        graph_nodes: List[Dict[str, Any]],
        graph_edges: List[Dict[str, Any]],
        chapter_sources: Sequence[Dict[str, Any]],
    ) -> None:
        details = [
            f"{idx}. {self._display_title(source['relativePath'], source['title'])}: {self._compress_text(source['text'], 160)}"
            for idx, source in enumerate(chapter_sources, start=1)
        ]
        entries.append(self._entry(
            "timeline:main",
            "\u65f6\u95f4\u7ebf",
            "plot",
            f"\u5f53\u524d\u65f6\u95f4\u7ebf\u6309 {len(chapter_sources)} \u4e2a\u7ae0\u8282/\u7247\u6bb5\u7684\u81ea\u7136\u987a\u5e8f\u7ec4\u7ec7\u3002",
            details,
            [source["relativePath"] for source in chapter_sources],
        ))
        graph_nodes.append({
            "id": "timeline:main",
            "label": "\u65f6\u95f4\u7ebf",
            "type": "timeline",
            "category": "plot",
            "entryId": "timeline:main",
            "summary": "\u7ae0\u8282\u987a\u5e8f\u548c\u4e8b\u4ef6\u63a8\u8fdb\u8109\u7edc\u3002",
        })
        graph_edges.append(self._edge("plot:mainline", "timeline:main", "\u987a\u5e8f", "timeline"))

    def _append_index(
        self,
        entries: List[Dict[str, Any]],
        graph_nodes: List[Dict[str, Any]],
        graph_edges: List[Dict[str, Any]],
    ) -> None:
        index_details = [f"{entry['categoryLabel']} / {entry['title']} / {entry['id']}" for entry in entries]
        entries.append(self._entry(
            "index:entries",
            "\u53ef\u6301\u7eed\u66f4\u65b0\u7684\u6761\u76ee\u7d22\u5f15",
            "overview",
            "\u6240\u6709 WIKI \u6761\u76ee\u90fd\u4fdd\u6301\u7a33\u5b9a ID\uff0c\u53ef\u88ab\u540e\u7eed LLM \u589e\u91cf\u66f4\u65b0\u6216\u524d\u7aef\u8282\u70b9\u5b9a\u4f4d\u590d\u7528\u3002",
            index_details,
            [],
        ))
        graph_nodes.append({
            "id": "index:entries",
            "label": "\u6761\u76ee\u7d22\u5f15",
            "type": "project",
            "category": "overview",
            "entryId": "index:entries",
            "summary": "\u77e5\u8bc6\u5e93\u6761\u76ee\u7d22\u5f15\u3002",
        })
        graph_edges.append(self._edge("project:root", "index:entries", "\u7d22\u5f15", "index"))

    def _edge(
        self,
        source: str,
        target: str,
        label: str,
        edge_type: str,
        *,
        weight: int = 1,
        evidence: str = "",
        co_occurrence: bool = False,
    ) -> Dict[str, Any]:
        edge: Dict[str, Any] = {
            "source": source,
            "target": target,
            "label": label,
            "type": edge_type,
            "weight": weight,
            "evidence": evidence,
        }
        if co_occurrence:
            edge["coOccurrence"] = True
        return edge

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
            if not key[0] or not key[1] or key in seen:
                continue
            seen[key] = edge
        return list(seen.values())[:300]

    def _slug(self, value: str) -> str:
        cleaned = re.sub(r"\s+", "-", value.strip())
        cleaned = re.sub(r"[^\w\-\u4e00-\u9fff]", "", cleaned)
        return cleaned or "item"

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
