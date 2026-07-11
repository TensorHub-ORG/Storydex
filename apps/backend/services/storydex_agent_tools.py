from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from coomi.tools.base import BaseTool, ToolAccess, ToolConcurrency, ToolResult

from services.git_service import get_git_service
from services.help_guide_service import get_help_guide_service
from services.story_project_service import get_story_project_service


class _StorydexWorkspaceToolMixin:
    def __init__(self, *, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()

    def set_workspace_root(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()

    def _resolve_workspace_root(self, value: Any) -> Path:
        raw = str(value or "").strip()
        if not raw:
            return self.workspace_root
        try:
            candidate = Path(raw).expanduser().resolve()
        except Exception:
            return self.workspace_root
        if candidate == self.workspace_root:
            return candidate
        return self.workspace_root


class StorydexRuntimePresetStatusTool(_StorydexWorkspaceToolMixin, BaseTool):
    name = "StorydexRuntimePresetStatus"
    description = (
        "Inspect Storydex runtime preset state. Reports active/library presets and the exact "
        "active or compiled-safe preset files eligible for generation context."
    )
    access = ToolAccess.READ_ONLY
    concurrency = ToolConcurrency.PARALLEL
    requires_confirmation = False

    def get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "workspaceRoot": {
                    "type": "string",
                    "description": "Optional active Storydex workspace root. External paths are ignored.",
                },
            },
            "additionalProperties": False,
        }

    def run(self, arguments: Dict[str, Any]) -> ToolResult:
        payload = dict(arguments or {})
        workspace_root = self._resolve_workspace_root(payload.get("workspaceRoot"))
        service = get_story_project_service()
        service.ensure_project_structure(workspace_root)
        runtime_paths = [
            path.relative_to(workspace_root).as_posix()
            for path in service._runtime_preset_files(workspace_root, max_files=8)  # noqa: SLF001
        ]
        compile_errors: list[str] = []
        service._collect_preset_entries(  # noqa: SLF001 - compile health probe
            workspace_root,
            max_files=8,
            max_chars_per_file=720,
            compile_errors=compile_errors,
        )
        result = {
            "ok": True,
            "workspaceRoot": workspace_root.as_posix(),
            "policy": {
                "activePresetsOnly": True,
                "compiledSafePresetsAllowed": True,
                "libraryImportedBlockedExcluded": True,
            },
            "activePointer": service.read_active_pointer(workspace_root),
            "runtimePresetPaths": runtime_paths,
            "compileErrors": compile_errors,
            "compileHealthy": not compile_errors,
            "presets": service.list_presets(workspace_root),
        }
        return ToolResult(success=True, output=json.dumps(result, ensure_ascii=False, indent=2), error=None)


class StorydexVersionStatusTool(_StorydexWorkspaceToolMixin, BaseTool):
    name = "StorydexVersionStatus"
    description = (
        "Read local Git version status for the active Storydex novel project workspace. "
        "This never pushes and never commits; Agent turn-end auto commit remains the write path."
    )
    access = ToolAccess.READ_ONLY
    concurrency = ToolConcurrency.PARALLEL
    requires_confirmation = False

    def get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "workspaceRoot": {
                    "type": "string",
                    "description": "Optional active Storydex workspace root. External paths are ignored.",
                },
                "historyLimit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum recent local commits to return.",
                },
            },
            "additionalProperties": False,
        }

    def run(self, arguments: Dict[str, Any]) -> ToolResult:
        payload = dict(arguments or {})
        workspace_root = self._resolve_workspace_root(payload.get("workspaceRoot"))
        try:
            history_limit = max(1, min(50, int(payload.get("historyLimit") or 12)))
        except (TypeError, ValueError):
            history_limit = 12
        summary = get_git_service().read_summary(workspace_root, history_limit=history_limit)
        result = {
            "ok": True,
            "target": "story_project_workspace",
            "targetLabel": "Storydex 小说项目",
            "workspaceRoot": workspace_root.as_posix(),
            "summary": summary,
        }
        return ToolResult(success=True, output=json.dumps(result, ensure_ascii=False, indent=2), error=None)


class StorydexHelpGuideSearchTool(_StorydexWorkspaceToolMixin, BaseTool):
    name = "StorydexHelpGuideSearch"
    description = (
        "Search the bundled Storydex user guide. Use this read-only tool before answering "
        "questions about Storydex usage, menus, setup, version control, WIKI, presets, or settings."
    )
    access = ToolAccess.READ_ONLY
    concurrency = ToolConcurrency.PARALLEL
    requires_confirmation = False

    def get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The user's Storydex operation question or keywords.",
                },
                "maxResults": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Maximum guide sections to return.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def run(self, arguments: Dict[str, Any]) -> ToolResult:
        payload = dict(arguments or {})
        try:
            max_results = max(1, min(20, int(payload.get("maxResults") or 6)))
        except (TypeError, ValueError):
            max_results = 6
        result = get_help_guide_service().search(str(payload.get("query") or ""), max_results=max_results)
        return ToolResult(success=True, output=json.dumps(result, ensure_ascii=False, indent=2), error=None)


class StorydexProjectSearchTool(_StorydexWorkspaceToolMixin, BaseTool):
    name = "StorydexProjectSearch"
    description = (
        "Rank-ordered full-text search (BM25, Chinese-aware) over the novel project: chapters, "
        "characters, worldbook, and memory notes. Use this to locate earlier plot details, "
        "foreshadowing, items, or names before referencing them in new prose. Prefer this over "
        "Grep when you need relevance ranking instead of exact regex matching."
    )
    access = ToolAccess.READ_ONLY
    concurrency = ToolConcurrency.PARALLEL
    requires_confirmation = False

    def get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Names, places, items, or short phrases to search for. Avoid full instructions.",
                },
                "maxResults": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Maximum passages to return (default 5).",
                },
                "pathPrefix": {
                    "type": "string",
                    "description": "Optional workspace-relative prefix filter, e.g. 'chapters/' or '.storydex/worldbook/'.",
                },
                "workspaceRoot": {
                    "type": "string",
                    "description": "Optional active Storydex workspace root. External paths are ignored.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def run(self, arguments: Dict[str, Any]) -> ToolResult:
        payload = dict(arguments or {})
        workspace_root = self._resolve_workspace_root(payload.get("workspaceRoot"))
        query = str(payload.get("query") or "").strip()
        if not query:
            return ToolResult(success=False, output="", error="query is required")
        try:
            max_results = max(1, min(10, int(payload.get("maxResults") or 5)))
        except (TypeError, ValueError):
            max_results = 5
        path_prefix = str(payload.get("pathPrefix") or "").strip().replace("\\", "/") or None
        from services.retrieval_service import get_retrieval_service

        service = get_retrieval_service(workspace_root)
        service.watch_files()
        hits = service.search(query, top_k=max_results, path_prefix=path_prefix)
        result = {
            "ok": True,
            "workspaceRoot": workspace_root.as_posix(),
            "query": query,
            "resultCount": len(hits),
            "results": [
                {"path": path, "score": round(float(score), 4), "snippet": snippet}
                for path, score, snippet in hits
            ],
            "note": (
                "Snippets are short excerpts around the first match; read the file for full context. "
                "Lower score = more relevant (FTS5 bm25)."
            ),
        }
        return ToolResult(success=True, output=json.dumps(result, ensure_ascii=False, indent=2), error=None)


class StorydexWikiQueryTool(_StorydexWorkspaceToolMixin, BaseTool):
    name = "StorydexWikiQuery"
    description = (
        "Query the project WIKI knowledge graph: search entries by keyword, or expand a node's "
        "relationship neighborhood. Returns distilled entries (characters, settings, plot, "
        "foreshadowing) with confidence and evidence. Use this to verify entity facts and "
        "relationships before writing; treat low-confidence or needsReview entries as hints, "
        "not canon."
    )
    access = ToolAccess.READ_ONLY
    concurrency = ToolConcurrency.PARALLEL
    requires_confirmation = False

    _MAX_DETAIL_CHARS = 400

    def get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword search over WIKI entries/nodes/edges, e.g. a character or place name.",
                },
                "nodeId": {
                    "type": "string",
                    "description": "Expand this graph node's neighborhood instead of keyword search.",
                },
                "entryId": {
                    "type": "string",
                    "description": "Fetch this WIKI entry and its linked nodes.",
                },
                "depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2,
                    "description": "Neighborhood expansion depth (default 1).",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                    "description": "Maximum entries/nodes to return (default 12).",
                },
                "workspaceRoot": {
                    "type": "string",
                    "description": "Optional active Storydex workspace root. External paths are ignored.",
                },
            },
            "additionalProperties": False,
        }

    def run(self, arguments: Dict[str, Any]) -> ToolResult:
        payload = dict(arguments or {})
        workspace_root = self._resolve_workspace_root(payload.get("workspaceRoot"))
        query = str(payload.get("query") or "").strip()
        node_id = str(payload.get("nodeId") or "").strip()
        entry_id = str(payload.get("entryId") or "").strip()
        if not query and not node_id and not entry_id:
            return ToolResult(success=False, output="", error="one of query/nodeId/entryId is required")
        try:
            depth = max(1, min(2, int(payload.get("depth") or 1)))
        except (TypeError, ValueError):
            depth = 1
        try:
            limit = max(1, min(30, int(payload.get("limit") or 12)))
        except (TypeError, ValueError):
            limit = 12
        get_story_project_service().ensure_project_structure(workspace_root)
        from services.story_wiki_service import get_story_wiki_service

        graph_result = get_story_wiki_service().query_graph(
            workspace_root,
            q=query,
            node_id=node_id,
            entry_id=entry_id,
            depth=depth,
            limit=limit,
        )
        result = {
            "ok": True,
            "workspaceRoot": workspace_root.as_posix(),
            "mode": graph_result.get("mode"),
            "entries": [self._compact_entry(entry) for entry in graph_result.get("entries", []) if isinstance(entry, dict)],
            "graph": self._compact_graph(graph_result.get("graph")),
            "total": graph_result.get("total"),
            "caveat": (
                "WIKI content may include model inference. Canonical facts live in chapters, "
                "character files, and variable memory; verify there when confidence is low or needsReview is true."
            ),
        }
        return ToolResult(success=True, output=json.dumps(result, ensure_ascii=False, indent=2), error=None)

    @classmethod
    def _compact_entry(cls, entry: Dict[str, Any]) -> Dict[str, Any]:
        details = entry.get("details") if isinstance(entry.get("details"), list) else []
        detail_text = " / ".join(str(item) for item in details if str(item).strip())
        if len(detail_text) > cls._MAX_DETAIL_CHARS:
            detail_text = detail_text[: cls._MAX_DETAIL_CHARS].rstrip() + "…"
        source_paths = entry.get("sourcePaths") if isinstance(entry.get("sourcePaths"), list) else []
        return {
            "id": entry.get("id"),
            "title": entry.get("title"),
            "category": entry.get("category"),
            "summary": entry.get("summary"),
            "details": detail_text,
            "confidence": entry.get("confidence"),
            "needsReview": bool(entry.get("needsReview")),
            "sourcePaths": [str(path) for path in source_paths[:6]],
        }

    @staticmethod
    def _compact_graph(graph: Any) -> Dict[str, Any]:
        payload = graph if isinstance(graph, dict) else {}
        nodes = [node for node in payload.get("nodes", []) if isinstance(node, dict)]
        edges = [edge for edge in payload.get("edges", []) if isinstance(edge, dict)]
        return {
            "nodes": [
                {
                    "id": node.get("id"),
                    "label": node.get("label"),
                    "type": node.get("type"),
                    "entryId": node.get("entryId"),
                    "summary": node.get("summary"),
                }
                for node in nodes
            ],
            "edges": [
                {
                    "source": edge.get("source"),
                    "target": edge.get("target"),
                    "label": edge.get("label"),
                    "type": edge.get("type"),
                    "weight": edge.get("weight"),
                    "evidence": edge.get("evidence"),
                }
                for edge in edges
            ],
        }


class StorydexSyncWikiTool(_StorydexWorkspaceToolMixin, BaseTool):
    name = "StorydexSyncWiki"
    description = (
        "Synchronize the local Storydex WIKI and knowledge graph from project files, memory, "
        "characters, facts, relationships, and item memory."
    )
    access = ToolAccess.WRITE
    concurrency = ToolConcurrency.BLOCKING
    requires_confirmation = False

    def get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "workspaceRoot": {
                    "type": "string",
                    "description": "Optional active Storydex workspace root. External paths are ignored.",
                },
            },
            "additionalProperties": False,
        }

    def run(self, arguments: Dict[str, Any]) -> ToolResult:
        payload = dict(arguments or {})
        workspace_root = self._resolve_workspace_root(payload.get("workspaceRoot"))
        get_story_project_service().ensure_project_structure(workspace_root)
        from services.story_wiki_service import get_story_wiki_service

        result = get_story_wiki_service().sync_local_incremental(workspace_root)
        wiki_payload = result if isinstance(result, dict) else {}
        graph = wiki_payload.get("graph") if isinstance(wiki_payload.get("graph"), dict) else {}
        summary = {
            "ok": True,
            "workspaceRoot": workspace_root.as_posix(),
            "wiki": {
                "entryCount": len(wiki_payload.get("entries", []) or []),
                "graphNodeCount": len(graph.get("nodes", []) or []),
                "graphEdgeCount": len(graph.get("edges", []) or []),
            },
            "paths": {
                "json": ".storydex/wiki/knowledge_graph.json",
                "markdown": ".storydex/wiki/knowledge_graph.md",
                "index": ".storydex/wiki/source_index.json",
            },
        }
        return ToolResult(success=True, output=json.dumps(summary, ensure_ascii=False, indent=2), error=None)


class StorydexApplyStoryIncrementTool(_StorydexWorkspaceToolMixin, BaseTool):
    name = "StorydexApplyStoryIncrement"
    description = (
        "Apply a Storydex post-generation increment: write story fragments, store readable "
        "Markdown variable thinking, optionally merge machine-readable variable operations, "
        "create or update character files, merge facts and relationships, and optionally "
        "sync the local WIKI knowledge graph."
    )
    access = ToolAccess.WRITE
    concurrency = ToolConcurrency.BLOCKING
    requires_confirmation = False

    def get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "workspaceRoot": {
                    "type": "string",
                    "description": "Optional active Storydex workspace root. External paths are ignored.",
                },
                "activeFile": {
                    "type": "string",
                    "description": "Current active file path relative to the workspace, if any.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Original user task summary; used only for naming when a segment path is omitted.",
                },
                "applyVariables": {
                    "type": "boolean",
                    "description": "Whether to write variable thinking and optional fact/relationship memory.",
                },
                "applyWiki": {
                    "type": "boolean",
                    "description": "Whether to run deterministic local WIKI sync after variable updates.",
                },
                "chapterSummary": {
                    "type": "string",
                    "description": (
                        "150-300 character rolling summary of the chapter after this increment: main events, "
                        "key entity actions, conflict changes, new foreshadowing. Overwrites the chapter's "
                        "rolling summary file used as mid-range plot context."
                    ),
                },
                "fragments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "text": {"type": "string"},
                            "variableThoughts": {
                                "type": "string",
                                "description": "Readable Markdown variable thinking for this fragment.",
                            },
                            "variableNotes": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Readable variable notes; not a fixed JSON update schema.",
                            },
                            "variableUpdates": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["op", "path", "evidence"],
                                    "properties": {
                                        "op": {"type": "string", "enum": ["set", "replace", "add", "remove"]},
                                        "path": {"type": "string", "description": "Stable-ID based dotted path; never use a mutable display name as an entity key."},
                                        "value": {},
                                        "evidence": {"type": "string"},
                                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                        "requiresReview": {"type": "boolean"},
                                    },
                                },
                                "description": "Optional revisioned change-set operations only when safe to merge and grounded in source evidence.",
                            },
                            "characterUpdates": {"type": "array", "items": {"type": "object"}},
                            "newCharacters": {"type": "array", "items": {"type": "string"}},
                            "itemUpdates": {"type": "array", "items": {"type": "object"}},
                            "newItems": {"type": "array", "items": {"type": "string"}},
                            "factUpdates": {"type": "array", "items": {"type": "object"}},
                            "relationshipUpdates": {"type": "array", "items": {"type": "object"}},
                        },
                    },
                    "description": "Generated story fragments and optional per-fragment increment payloads.",
                },
                "segmentPath": {"type": "string"},
                "segmentText": {"type": "string"},
                "variableThoughts": {
                    "type": "string",
                    "description": "Readable Markdown variable thinking. Prefer this over fixed JSON path/value entries.",
                },
                "variableNotes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Readable variable notes. These are written as Markdown.",
                },
                "variableUpdates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["op", "path", "evidence"],
                        "properties": {
                            "op": {"type": "string", "enum": ["set", "replace", "add", "remove"]},
                            "path": {"type": "string", "description": "Stable-ID based dotted path; never use a mutable display name as an entity key."},
                            "value": {},
                            "evidence": {"type": "string"},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "requiresReview": {"type": "boolean"},
                        },
                    },
                    "description": "Optional revisioned change-set operations only when safe to merge and grounded in source evidence.",
                },
                "characterUpdates": {"type": "array", "items": {"type": "object"}},
                "newCharacters": {"type": "array", "items": {"type": "string"}},
                "itemUpdates": {"type": "array", "items": {"type": "object"}},
                "newItems": {"type": "array", "items": {"type": "string"}},
                "factUpdates": {"type": "array", "items": {"type": "object"}},
                "relationshipUpdates": {"type": "array", "items": {"type": "object"}},
            },
            "additionalProperties": True,
        }

    def run(self, arguments: Dict[str, Any]) -> ToolResult:
        payload = dict(arguments or {})
        workspace_root = self._resolve_workspace_root(payload.get("workspaceRoot"))
        result = get_story_project_service().apply_story_generation_increment(workspace_root, payload)
        return ToolResult(
            success=True,
            output=json.dumps(result, ensure_ascii=False, indent=2),
            error=None,
        )
