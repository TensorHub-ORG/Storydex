from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict

from services.git_service import get_git_service
from services.help_guide_service import get_help_guide_service
from services.story_project_service import get_story_project_service
from services.storydex_tool_types import BaseTool, ToolAccess, ToolConcurrency, ToolResult
from services.story_word_count_service import STORY_WORD_COUNT_RULE


STORY_FRAGMENT_CHUNK_MAX_CHARS = 1800
STORY_FRAGMENT_CHUNK_MAX_COUNT = 64
_STORY_FRAGMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_STORY_FRAGMENT_STAGE_DIRECTORY = Path(".storydex/.agent/runtime/story-fragment-staging")


def _stage_path(workspace_root: Path, fragment_id: str) -> Path:
    digest = hashlib.sha256(fragment_id.encode("utf-8")).hexdigest()
    return workspace_root / _STORY_FRAGMENT_STAGE_DIRECTORY / f"{digest}.json"


def _atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _normalized_chapter_path(workspace_root: Path, raw_path: Any) -> str:
    service = get_story_project_service()
    relative_path = service._normalize_relative_path(str(raw_path or ""))  # noqa: SLF001
    candidate = (workspace_root / relative_path).resolve() if relative_path else workspace_root
    try:
        candidate.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError("staged story fragment path escapes the workspace") from exc
    if not relative_path.startswith("chapters/"):
        raise ValueError("staged story fragments must target chapters/")
    return relative_path


def _read_complete_staged_fragment(workspace_root: Path, fragment_id: str) -> tuple[str, str]:
    if not _STORY_FRAGMENT_ID.fullmatch(fragment_id):
        raise ValueError("stagedFragmentId is invalid")
    path = _stage_path(workspace_root, fragment_id)
    if not path.is_file():
        raise ValueError(f"staged story fragment {fragment_id!r} was not found")
    value = json.loads(path.read_text(encoding="utf-8"))
    chunk_count = int(value.get("chunkCount") or 0)
    chunks = value.get("chunks") if isinstance(value.get("chunks"), dict) else {}
    missing = [index for index in range(chunk_count) if str(index) not in chunks]
    if chunk_count <= 0 or missing:
        raise ValueError(
            f"staged story fragment {fragment_id!r} is incomplete; missing chunks: {missing}"
        )
    text = "".join(str(chunks[str(index)]) for index in range(chunk_count))
    return str(value.get("path") or ""), text


def _delete_staged_fragment(workspace_root: Path, fragment_id: str) -> None:
    try:
        _stage_path(workspace_root, fragment_id).unlink()
    except FileNotFoundError:
        pass


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
        from services.retrieval_service import RECALL_CANDIDATE_LIMIT, get_retrieval_service

        service = get_retrieval_service(workspace_root)
        service.watch_files()
        hits, candidate_paths = service.search_with_candidates(
            query,
            top_k=max_results,
            candidate_limit=RECALL_CANDIDATE_LIMIT,
            path_prefix=path_prefix,
        )
        result = {
            "ok": True,
            "workspaceRoot": workspace_root.as_posix(),
            "query": query,
            "resultCount": len(hits),
            "candidateCount": len(candidate_paths),
            "candidatePaths": candidate_paths,
            "results": [
                {"path": path, "score": round(float(score), 4), "snippet": snippet}
                for path, score, snippet in hits
            ],
            "note": (
                "Snippets are short excerpts around the first match; read the file for full context. "
                "Lower score = more relevant (FTS5 bm25). Candidate paths include lower-ranked matches "
                "without excerpts; read selectively before concluding evidence is absent."
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


class StorydexWordCountTool(_StorydexWorkspaceToolMixin, BaseTool):
    name = "StorydexWordCount"
    description = (
        "Read Storydex's authoritative fiction word count for chapter files. "
        "The returned value is program-measured with the same non-whitespace Unicode-character algorithm as the editor."
    )
    access = ToolAccess.READ_ONLY
    concurrency = ToolConcurrency.PARALLEL
    requires_confirmation = False

    def get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "One workspace-relative chapter file path."},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of workspace-relative chapter file paths.",
                },
            },
            "additionalProperties": False,
        }

    def run(self, arguments: Dict[str, Any]) -> ToolResult:
        payload = dict(arguments or {})
        raw_paths = payload.get("paths") if isinstance(payload.get("paths"), list) else []
        if str(payload.get("path") or "").strip():
            raw_paths = [payload.get("path"), *raw_paths]
        service = get_story_project_service()
        items = []
        for raw_path in raw_paths[:100]:
            relative_path = service._normalize_relative_path(str(raw_path or ""))  # noqa: SLF001
            candidate = (self.workspace_root / relative_path).resolve() if relative_path else self.workspace_root
            try:
                candidate.relative_to(self.workspace_root)
            except ValueError:
                continue
            if not relative_path.startswith("chapters/") or not candidate.is_file():
                items.append({"path": relative_path, "exists": False, "wordCount": 0})
                continue
            items.append(
                {
                    "path": relative_path,
                    "exists": True,
                    "wordCount": service.count_story_file_words(candidate),
                }
            )
        result = {
            "ok": True,
            "algorithm": "storydex_visible_characters_v1",
            "countingRule": STORY_WORD_COUNT_RULE,
            "items": items,
        }
        return ToolResult(success=True, output=json.dumps(result, ensure_ascii=False, indent=2), error=None)


class StorydexStageStoryFragmentTool(_StorydexWorkspaceToolMixin, BaseTool):
    name = "StorydexStageStoryFragment"
    description = (
        "Stage one bounded chunk of a long chapter fragment before final application. Use one call per "
        f"chunk, keep text at or below {STORY_FRAGMENT_CHUNK_MAX_CHARS} characters, and reuse the same "
        "fragmentId/path/chunkCount for every chunk. After all chunks succeed, call "
        "StorydexApplyStoryIncrement with stagedFragmentId instead of repeating the full text."
    )
    access = ToolAccess.WRITE
    concurrency = ToolConcurrency.BLOCKING
    requires_confirmation = False

    def get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["fragmentId", "path", "chunkIndex", "chunkCount", "text"],
            "properties": {
                "fragmentId": {
                    "type": "string",
                    "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$",
                    "description": "Stable ID reused for every chunk of this fragment.",
                },
                "path": {
                    "type": "string",
                    "description": "Workspace-relative chapters/ target path.",
                },
                "chunkIndex": {"type": "integer", "minimum": 0, "maximum": 63},
                "chunkCount": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": STORY_FRAGMENT_CHUNK_MAX_COUNT,
                },
                "text": {
                    "type": "string",
                    "maxLength": STORY_FRAGMENT_CHUNK_MAX_CHARS,
                    "description": "One consecutive UTF-8 text chunk; do not repeat adjacent content.",
                },
            },
            "additionalProperties": False,
        }

    def run(self, arguments: Dict[str, Any]) -> ToolResult:
        payload = dict(arguments or {})
        fragment_id = str(payload.get("fragmentId") or "").strip()
        if not _STORY_FRAGMENT_ID.fullmatch(fragment_id):
            return ToolResult(success=False, output="", error="fragmentId is invalid")
        try:
            relative_path = _normalized_chapter_path(self.workspace_root, payload.get("path"))
            chunk_index = int(payload.get("chunkIndex"))
            chunk_count = int(payload.get("chunkCount"))
        except (TypeError, ValueError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
        text = str(payload.get("text") or "")
        if not 1 <= chunk_count <= STORY_FRAGMENT_CHUNK_MAX_COUNT:
            return ToolResult(success=False, output="", error="chunkCount is out of range")
        if not 0 <= chunk_index < chunk_count:
            return ToolResult(success=False, output="", error="chunkIndex is out of range")
        if len(text) > STORY_FRAGMENT_CHUNK_MAX_CHARS:
            return ToolResult(
                success=False,
                output="",
                error=f"story fragment chunk exceeds {STORY_FRAGMENT_CHUNK_MAX_CHARS} characters",
            )
        stage_path = _stage_path(self.workspace_root, fragment_id)
        try:
            existing = (
                json.loads(stage_path.read_text(encoding="utf-8"))
                if stage_path.is_file()
                else {
                    "version": 1,
                    "fragmentId": fragment_id,
                    "path": relative_path,
                    "chunkCount": chunk_count,
                    "chunks": {},
                }
            )
        except (OSError, json.JSONDecodeError) as exc:
            return ToolResult(success=False, output="", error=f"failed to read staged fragment: {exc}")
        if str(existing.get("fragmentId") or "") != fragment_id:
            return ToolResult(success=False, output="", error="staged fragment identity mismatch")
        if str(existing.get("path") or "") != relative_path:
            return ToolResult(success=False, output="", error="staged fragment path changed between chunks")
        if int(existing.get("chunkCount") or 0) != chunk_count:
            return ToolResult(success=False, output="", error="staged fragment chunkCount changed")
        chunks = existing.get("chunks") if isinstance(existing.get("chunks"), dict) else {}
        previous = chunks.get(str(chunk_index))
        if previous is not None and str(previous) != text:
            return ToolResult(success=False, output="", error="staged fragment chunk content changed")
        chunks[str(chunk_index)] = text
        existing["chunks"] = chunks
        try:
            _atomic_write_json(stage_path, existing)
        except OSError as exc:
            return ToolResult(success=False, output="", error=f"failed to stage story fragment: {exc}")
        missing = [index for index in range(chunk_count) if str(index) not in chunks]
        result = {
            "ok": True,
            "fragmentId": fragment_id,
            "path": relative_path,
            "received": len(chunks),
            "chunkCount": chunk_count,
            "complete": not missing,
            "missingChunks": missing,
        }
        return ToolResult(
            success=True,
            output=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            error=None,
        )


class StorydexApplyStoryIncrementTool(_StorydexWorkspaceToolMixin, BaseTool):
    name = "StorydexApplyStoryIncrement"
    description = (
        "Apply a Storydex post-generation increment: write story fragments, store readable "
        "Markdown variable thinking, merge safe machine-readable variable operations, "
        "create or update character files, merge facts and relationships, and optionally "
        "sync the local WIKI knowledge graph. Safe memory deltas accompanying newly generated "
        "fragments are applied immediately unless applyVariables is explicitly false."
    )
    access = ToolAccess.WRITE
    concurrency = ToolConcurrency.BLOCKING
    requires_confirmation = False

    def __init__(self, *, workspace_root: Path, turn_contract: Dict[str, Any] | None = None) -> None:
        super().__init__(workspace_root=workspace_root)
        self.turn_contract = dict(turn_contract) if isinstance(turn_contract, dict) else {}

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
                    "description": (
                        "Whether to apply variable thinking and fact/item/relationship memory. Omit for newly "
                        "generated fragments to apply their safe memory deltas immediately; set false only to "
                        "explicitly defer memory organization."
                    ),
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
                            "text": {
                                "type": "string",
                                "maxLength": STORY_FRAGMENT_CHUNK_MAX_CHARS,
                                "description": "Inline text for a short fragment only.",
                            },
                            "stagedFragmentId": {
                                "type": "string",
                                "description": "Completed StorydexStageStoryFragment ID for long text.",
                            },
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
                "segmentText": {
                    "type": "string",
                    "maxLength": STORY_FRAGMENT_CHUNK_MAX_CHARS,
                    "description": "Inline text for a short fragment only.",
                },
                "segmentStagedFragmentId": {
                    "type": "string",
                    "description": "Completed StorydexStageStoryFragment ID for long segment text.",
                },
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
        staged_ids: list[str] = []
        try:
            fragments = payload.get("fragments") if isinstance(payload.get("fragments"), list) else []
            resolved_fragments = []
            for raw_fragment in fragments:
                fragment = dict(raw_fragment) if isinstance(raw_fragment, dict) else {}
                staged_id = str(fragment.get("stagedFragmentId") or "").strip()
                if staged_id:
                    staged_path, staged_text = _read_complete_staged_fragment(workspace_root, staged_id)
                    supplied_path = str(fragment.get("path") or "").strip()
                    if supplied_path and _normalized_chapter_path(workspace_root, supplied_path) != staged_path:
                        raise ValueError(f"staged fragment {staged_id!r} path does not match apply path")
                    fragment["path"] = staged_path
                    fragment["text"] = staged_text
                    fragment.pop("stagedFragmentId", None)
                    staged_ids.append(staged_id)
                elif len(str(fragment.get("text") or "")) > STORY_FRAGMENT_CHUNK_MAX_CHARS:
                    raise ValueError(
                        "long story fragment must be staged with StorydexStageStoryFragment before apply"
                    )
                resolved_fragments.append(fragment)
            if isinstance(payload.get("fragments"), list):
                payload["fragments"] = resolved_fragments
            segment_staged_id = str(payload.get("segmentStagedFragmentId") or "").strip()
            if segment_staged_id:
                staged_path, staged_text = _read_complete_staged_fragment(
                    workspace_root, segment_staged_id
                )
                supplied_path = str(payload.get("segmentPath") or "").strip()
                if supplied_path and _normalized_chapter_path(workspace_root, supplied_path) != staged_path:
                    raise ValueError(
                        f"staged fragment {segment_staged_id!r} path does not match segmentPath"
                    )
                payload["segmentPath"] = staged_path
                payload["segmentText"] = staged_text
                payload.pop("segmentStagedFragmentId", None)
                staged_ids.append(segment_staged_id)
            elif len(str(payload.get("segmentText") or "")) > STORY_FRAGMENT_CHUNK_MAX_CHARS:
                raise ValueError(
                    "long segmentText must be staged with StorydexStageStoryFragment before apply"
                )
            result = get_story_project_service().apply_story_generation_increment(
                workspace_root,
                payload,
                generation_contract=self.turn_contract,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
        if bool(result.get("ok", True)):
            for staged_id in set(staged_ids):
                _delete_staged_fragment(workspace_root, staged_id)
        knowledge_review = result.get("knowledgeReview") if isinstance(result.get("knowledgeReview"), dict) else None
        if knowledge_review is not None:
            result = {
                "knowledgeReview": knowledge_review,
                **{key: value for key, value in result.items() if key != "knowledgeReview"},
            }
        return ToolResult(
            success=bool(result.get("ok", True)),
            output=json.dumps(
                result,
                ensure_ascii=False,
                indent=None if knowledge_review is not None else 2,
                separators=(",", ":") if knowledge_review is not None else None,
            ),
            error=None if bool(result.get("ok", True)) else str(result.get("message") or "Story generation constraints were not met."),
        )
