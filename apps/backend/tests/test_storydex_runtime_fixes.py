from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from services import coomi_agent_service
from services.coomi_agent_service import (
    DEFAULT_CONTEXT_WINDOW,
    _coomi_binding_path,
    _create_storydex_tool_registry,
    _resolve_context_window,
)
from services.retrieval_service import reset_retrieval_cache
from services.story_project_service import get_story_project_service
from services.storydex_agent_tools import StorydexProjectSearchTool, StorydexWikiQueryTool
from services.storydex_context_assembler_service import StorydexContextAssemblerService
from services.trace_history_service import TraceHistoryService


def _write_providers_config(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_resolve_context_window_uses_active_rust_provider(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "providers.json"
    _write_providers_config(
        config_path,
        {
            "active": "local",
            "context_window": 64000,
            "providers": {"local": {"model": "m", "context_window": 32000}},
        },
    )
    monkeypatch.setattr(coomi_agent_service, "STORYDEX_COOMI_CONFIG", config_path)
    assert _resolve_context_window() == 32000
    _write_providers_config(config_path, {"active": "missing", "context_window": 64000})
    assert _resolve_context_window() == 64000
    _write_providers_config(config_path, {"active": "missing", "context_window": "bad"})
    assert _resolve_context_window() == DEFAULT_CONTEXT_WINDOW


def test_rust_session_binding_is_project_isolated(tmp_path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    assert _coomi_binding_path(first, "same-session") != _coomi_binding_path(second, "same-session")


def test_preset_compile_failure_surfaces_in_context(tmp_path, monkeypatch) -> None:
    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)
    active_dir = tmp_path / ".storydex" / "presets" / "active"
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "main.md").write_text("# Main\n", encoding="utf-8")
    (active_dir / "main.preset.json").write_text(
        json.dumps({"version": 2, "meta": {"name": "Main"}, "modules": []}),
        encoding="utf-8",
    )
    import services.preset_compiler as preset_compiler

    monkeypatch.setattr(
        preset_compiler,
        "compile_preset",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("macro expansion failed")),
    )
    assembly = StorydexContextAssemblerService(service).assemble(tmp_path, prompt="continue")
    assert assembly.get("compileErrors") or any(
        "macro expansion failed" in str(value)
        for value in assembly.values()
    )


def test_storydex_registry_contains_domain_retrieval_tools(tmp_path) -> None:
    registry = _create_storydex_tool_registry(tmp_path)
    by_name = {tool.name: tool for tool in registry.list_tools()}
    assert isinstance(by_name["StorydexProjectSearch"], StorydexProjectSearchTool)
    assert isinstance(by_name["StorydexWikiQuery"], StorydexWikiQueryTool)


def test_project_search_tool_returns_ranked_workspace_hits(tmp_path) -> None:
    chapters = tmp_path / "chapters"
    chapters.mkdir(parents=True)
    (chapters / "001.md").write_text("沈青抵达云桥，在藏经阁外驻足良久。\n", encoding="utf-8")
    (chapters / "002.md").write_text("阿离在荒村夜宿，遇见了旧识。\n", encoding="utf-8")
    reset_retrieval_cache()
    result = StorydexProjectSearchTool(workspace_root=tmp_path).run({"query": "云桥 藏经阁"})
    assert result.success is True
    payload = json.loads(result.output)
    assert payload["results"]
    assert payload["results"][0]["path"] == "chapters/001.md"


def test_execution_trace_is_persisted_to_its_workspace(tmp_path) -> None:
    workspace_a = (tmp_path / "workspace-a").resolve()
    workspace_b = (tmp_path / "workspace-b").resolve()
    history_a = TraceHistoryService()
    history_a.project_service = SimpleNamespace(storydex_root=workspace_a / ".storydex")
    history_b = TraceHistoryService()
    history_b.project_service = SimpleNamespace(storydex_root=workspace_b / ".storydex")
    history_a.upsert_record(
        {"traceId": "trace-a", "workspaceRoot": workspace_a.as_posix(), "status": "completed"},
        "shared",
    )
    history_b.upsert_record(
        {"traceId": "trace-b", "workspaceRoot": workspace_b.as_posix(), "status": "completed"},
        "shared",
    )
    assert history_a.read_record("trace-a", "shared")
    assert history_b.read_record("trace-b", "shared")
    assert history_a.read_record("trace-b", "shared") is None
