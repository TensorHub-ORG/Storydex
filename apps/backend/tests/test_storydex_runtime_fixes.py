from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import main as backend_main
from services import retrieval_service
from services.content_pipeline_service import ContentPipelineService
from services.retrieval_service import RetrievalService, reset_retrieval_cache
from services.story_project_service import get_story_project_service
from services.trace_history_service import TraceHistoryService


def test_runtime_identity_failure_precedes_workspace_side_effects(monkeypatch) -> None:
    workspace_requested = False

    def fail_if_workspace_requested():
        nonlocal workspace_requested
        workspace_requested = True
        raise AssertionError("workspace bootstrap must not run")

    monkeypatch.setattr(
        backend_main,
        "check_coomi_version",
        lambda: {"ok": False, "warnings": ["runtime mismatch"]},
    )
    monkeypatch.setattr(backend_main, "get_project_service", fail_if_workspace_requested)

    with pytest.raises(RuntimeError, match="runtime mismatch"):
        backend_main.bootstrap_workspace()

    assert workspace_requested is False






def test_worldbook_relevance_scoring_keeps_deterministic_selection_with_parallel_reads(
    tmp_path,
    monkeypatch,
) -> None:
    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)
    worldbook = tmp_path / ".storydex" / "worldbook"
    for index in range(12):
        (worldbook / f"entry-{index:02d}.md").write_text(
            f"# entry {index}\n\nsetting {index}\n",
            encoding="utf-8",
        )

    calls: list[str] = []
    original = service._score_worldbook_path_relevance

    def scored(path, keywords):
        calls.append(path.name)
        return original(path, keywords)

    monkeypatch.setattr(service, "_score_worldbook_path_relevance", scored)
    result = service._build_worldbook_hard_constraints_context(
        tmp_path,
        max_files=4,
        prompt="entry-03",
    )

    assert "entry-03.md" in result
    assert len(calls) == 12
    assert sorted(calls) == [f"entry-{index:02d}.md" for index in range(12)]


def _write_providers_config(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")






















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
