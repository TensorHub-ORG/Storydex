from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import feature_flags
from services import coomi_agent_service, retrieval_service, story_wiki_service
from services.coomi_agent_service import (
    DEFAULT_CONTEXT_WINDOW,
    _coomi_binding_path,
    _create_storydex_tool_registry,
    _resolve_context_window,
)
from services.retrieval_service import RetrievalService, reset_retrieval_cache
from services.story_project_service import get_story_project_service
from services.storydex_agent_tools import (
    STORY_FRAGMENT_CHUNK_MAX_CHARS,
    StorydexApplyStoryIncrementTool,
    StorydexProjectSearchTool,
    StorydexStageStoryFragmentTool,
    StorydexSyncWikiTool,
    StorydexWikiQueryTool,
)
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
    assert isinstance(by_name["StorydexStageStoryFragment"], StorydexStageStoryFragmentTool)


def test_sync_wiki_tool_reports_no_change_revision_and_checksums(tmp_path) -> None:
    get_story_project_service().ensure_project_structure(tmp_path)
    chapter = tmp_path / "chapters" / "001.md"
    chapter.parent.mkdir(parents=True, exist_ok=True)
    chapter.write_text("潮汐兽长期栖息于夜港星。\n", encoding="utf-8")
    tool = StorydexSyncWikiTool(workspace_root=tmp_path)

    first = tool.run({})
    first_payload = json.loads(first.output)
    second = tool.run({})
    second_payload = json.loads(second.output)

    assert first.success is True
    assert first_payload["status"] == "ready"
    assert first_payload["noChanges"] is False
    assert "chapters/001.md" in first_payload["changedSourcePaths"]
    assert first_payload["knowledgeRevision"] > 0
    assert first_payload["builtFromRevision"] == first_payload["knowledgeRevision"]
    assert first_payload["lastSuccessfulRevision"] == first_payload["knowledgeRevision"]
    assert first_payload["sourceSetChecksum"].startswith("sha256:")
    assert first_payload["graphChecksum"].startswith("sha256:")

    assert second.success is True
    assert second_payload["status"] == "ready"
    assert second_payload["noChanges"] is True
    assert second_payload["changedSourcePaths"] == []
    assert second_payload["knowledgeRevision"] == first_payload["knowledgeRevision"]
    assert second_payload["sourceSetChecksum"] == first_payload["sourceSetChecksum"]
    assert second_payload["graphChecksum"] == first_payload["graphChecksum"]


def test_sync_wiki_tool_surfaces_projection_errors_and_diagnostics(tmp_path, monkeypatch) -> None:
    get_story_project_service().ensure_project_structure(tmp_path)
    diagnostics = [
        {"severity": "warning", "code": "graph.warning", "message": "可恢复警告"},
        {"severity": "error", "code": "graph.failed", "message": "投影构建失败"},
    ]
    stub = SimpleNamespace(
        sync_local_incremental=lambda _root: {
            "status": "error",
            "changedSourcePaths": [],
            "knowledgeRevision": "invalid",
            "builtFromRevision": 4,
            "lastSuccessfulRevision": 3,
            "sourceSetChecksum": "sha256:source",
            "graphChecksum": "sha256:last-good",
            "diagnostics": diagnostics,
            "entries": [],
            "graph": {"nodes": [], "edges": []},
        }
    )
    monkeypatch.setattr(story_wiki_service, "get_story_wiki_service", lambda: stub)

    result = StorydexSyncWikiTool(workspace_root=tmp_path).run({})
    payload = json.loads(result.output)

    assert result.success is False
    assert result.error == "投影构建失败"
    assert payload["ok"] is False
    assert payload["status"] == "error"
    assert payload["noChanges"] is False
    assert payload["knowledgeRevision"] == 0
    assert payload["builtFromRevision"] == 4
    assert payload["lastSuccessfulRevision"] == 3
    assert payload["diagnostics"] == diagnostics


def test_long_story_fragment_is_staged_in_bounded_chunks_then_applied(tmp_path) -> None:
    stage = StorydexStageStoryFragmentTool(workspace_root=tmp_path)
    fragment_id = "chapter-001-opening"
    first = "甲" * STORY_FRAGMENT_CHUNK_MAX_CHARS
    second = "乙" * 900
    for index, text in enumerate((first, second)):
        result = stage.run(
            {
                "fragmentId": fragment_id,
                "path": "chapters/001.md",
                "chunkIndex": index,
                "chunkCount": 2,
                "text": text,
            }
        )
        assert result.success is True
    apply = StorydexApplyStoryIncrementTool(workspace_root=tmp_path)
    result = apply.run(
        {
            "fragments": [
                {"path": "chapters/001.md", "stagedFragmentId": fragment_id}
            ]
        }
    )
    assert result.success is True
    assert (tmp_path / "chapters" / "001.md").read_text(encoding="utf-8") == first + second + "\n"
    staging = tmp_path / ".storydex/.agent/runtime/story-fragment-staging"
    assert list(staging.glob("*.json")) == []


def test_long_inline_story_fragment_must_use_staging(tmp_path) -> None:
    apply = StorydexApplyStoryIncrementTool(workspace_root=tmp_path)
    result = apply.run(
        {
            "fragments": [
                {
                    "path": "chapters/001.md",
                    "text": "长" * (STORY_FRAGMENT_CHUNK_MAX_CHARS + 1),
                }
            ]
        }
    )
    assert result.success is False
    assert "must be staged" in str(result.error)
    assert not (tmp_path / "chapters" / "001.md").exists()


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


def test_project_search_returns_snippet_for_match_after_first_4000_chars(tmp_path) -> None:
    chapters = tmp_path / "chapters"
    chapters.mkdir(parents=True)
    chapters.joinpath("001.md").write_text(
        ("普通背景段落。\n" * 900) + "暮色钥印藏在废弃钟楼的暗格中。\n",
        encoding="utf-8",
    )
    reset_retrieval_cache()

    result = StorydexProjectSearchTool(workspace_root=tmp_path).run({"query": "暮色钥印"})

    assert result.success is True
    payload = json.loads(result.output)
    assert payload["results"]
    assert "暮色钥印" in payload["results"][0]["snippet"]


def test_retrieval_watch_compares_stored_mtime_instead_of_index_time(tmp_path) -> None:
    chapters = tmp_path / "chapters"
    chapters.mkdir(parents=True)
    chapter = chapters / "001.md"
    indexed_mtime = 1_700_000_000.0
    changed_mtime = indexed_mtime + 60
    chapter.write_text("oldtoken marker\n", encoding="utf-8")
    os.utime(chapter, (indexed_mtime, indexed_mtime))
    service = RetrievalService(tmp_path)
    assert service.build_index() == 1

    chapter.write_text("newtoken marker\n", encoding="utf-8")
    os.utime(chapter, (changed_mtime, changed_mtime))

    assert service.watch_files() == 1
    assert service.search("newtoken")
    assert service.search("oldtoken") == []


def test_retrieval_full_build_rolls_back_on_indexing_failure(tmp_path, monkeypatch) -> None:
    chapters = tmp_path / "chapters"
    chapters.mkdir(parents=True)
    chapters.joinpath("001.md").write_text("stabletoken remains indexed\n", encoding="utf-8")
    service = RetrievalService(tmp_path)
    assert service.build_index() == 1
    chapters.joinpath("002.md").write_text("explode-token\n", encoding="utf-8")
    original_tokenized = service._tokenized

    def fail_on_marker(text: str) -> str:
        if "explode-token" in text:
            raise RuntimeError("forced indexing failure")
        return original_tokenized(text)

    monkeypatch.setattr(service, "_tokenized", fail_on_marker)
    with pytest.raises(RuntimeError, match="forced indexing failure"):
        service.build_index()

    assert service.search("stabletoken")


def test_agent_recent_segments_use_tail_without_changing_default_preview(tmp_path) -> None:
    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)
    chapters = tmp_path / "chapters"
    chapters.mkdir(exist_ok=True)
    chapters.joinpath("第1章 测试.md").write_text(
        "HEAD_MARKER\n" + ("x" * 3000) + "\nTAIL_MARKER",
        encoding="utf-8",
    )

    default_recent = service.list_recent_segments(
        tmp_path,
        limit=1,
        include_content=True,
        max_chars=160,
    )
    agent_recent = StorydexContextAssemblerService(service)._recent_segments(
        tmp_path,
        generation_context={},
        active_file="",
    )

    assert "HEAD_MARKER" in default_recent[0]["content"]
    assert "TAIL_MARKER" not in default_recent[0]["content"]
    assert "TAIL_MARKER" in agent_recent[0]["content"]
    assert "HEAD_MARKER" not in agent_recent[0]["content"]


def test_active_entity_inference_reads_active_file_head_and_tail(tmp_path) -> None:
    registry_path = tmp_path / ".storydex" / "memory" / "current" / "entities.json"
    _write_providers_config(
        registry_path,
        {
            "version": 2,
            "entities": [
                {
                    "entityId": "char:shenqing",
                    "canonical_name": "沈青",
                    "kind": "character",
                }
            ],
        },
    )
    active_path = tmp_path / "chapters" / "001.md"
    active_path.parent.mkdir(parents=True)
    active_path.write_text("开头没有人物。\n" + ("x" * 5000) + "\n章尾由沈青推门而入。", encoding="utf-8")
    assembler = StorydexContextAssemblerService(get_story_project_service())

    entities = assembler._infer_active_entities(
        tmp_path,
        prompt="继续",
        active_file="chapters/001.md",
    )

    assert entities == ("沈青",)


def test_related_passage_snippets_precede_long_candidate_path_list(tmp_path, monkeypatch) -> None:
    candidate_paths = [
        f"chapters/archive/{index:02d}-{'long-name-' * 6}.md"
        for index in range(30)
    ]
    fake_service = SimpleNamespace(
        watch_files=lambda: 0,
        search_with_candidates=lambda *_args, **_kwargs: (
            [(candidate_paths[0], -1.0, "关键命中证据位于这里。")],
            candidate_paths,
        ),
    )
    monkeypatch.setattr(
        feature_flags,
        "get_flags",
        lambda: SimpleNamespace(get_bool=lambda _name: True),
    )
    monkeypatch.setattr(retrieval_service, "get_retrieval_service", lambda _root: fake_service)
    assembler = StorydexContextAssemblerService(get_story_project_service())

    block, returned_paths = assembler._render_related_passages(
        tmp_path,
        prompt="『暮色钥印』",
        active_entities=(),
        exclude_paths=set(),
    )

    assert returned_paths == candidate_paths
    assert block.index("关键命中证据") < block.index("Additional candidate paths:")
    assert "关键命中证据" in block[:1600]


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
