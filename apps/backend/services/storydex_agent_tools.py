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
        summary = {
            "ok": True,
            "workspaceRoot": workspace_root.as_posix(),
            "wiki": {
                "entryCount": len(result.get("entries", [])) if isinstance(result, dict) else 0,
                "graphNodeCount": len(result.get("graph", {}).get("nodes", [])) if isinstance(result.get("graph"), dict) else 0,
                "graphEdgeCount": len(result.get("graph", {}).get("edges", [])) if isinstance(result.get("graph"), dict) else 0,
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
                                "items": {"type": "object"},
                                "description": "Optional machine operations only when safe to merge.",
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
                    "items": {"type": "object"},
                    "description": "Optional machine operations only when safe to merge.",
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
