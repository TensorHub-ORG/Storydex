from __future__ import annotations

from pathlib import Path

from api import routes_agent as routes
from api.response import ApiTrace
from services.coomi_agent_service import _CoomiEventTranslator
from services.content_catalog_service import get_content_catalog_service
from services.context_policy import ContextPolicy
from services.performance_trace_service import trace_turn_contract
from services.story_project_service import StoryProjectService
from services.storydex_orchestration_service import StorydexOrchestrationService


def test_turn_performance_trace_is_isolated_and_attached(tmp_path: Path) -> None:
    service = StoryProjectService()
    service.ensure_project_structure(tmp_path)
    for number in range(1, 4):
        chapter = tmp_path / "chapters" / f"第{number}章 测试"
        chapter.mkdir(parents=True)
        (chapter / "001.md").write_text(f"第{number}章\n", encoding="utf-8")

    @trace_turn_contract
    def build() -> dict:
        service.list_chapter_states(tmp_path)
        service.list_chapter_states(tmp_path)
        return {"contextAssembly": {"contextTrace": {"totals": {}}}}

    contract = build()
    performance = contract["performanceTrace"]

    assert performance["chapterSnapshotBuildCount"] == 2
    assert performance["directoryScanCount"] == 6
    assert performance["statCount"] > 0
    assert performance["contractBuildMs"] > 0
    assert contract["contextAssembly"]["contextTrace"]["performance"] == performance


def test_turn_contract_reuses_one_catalog_and_chapter_snapshot(tmp_path: Path) -> None:
    project = StoryProjectService()
    project.ensure_project_structure(tmp_path)
    for number in range(1, 4):
        chapter = tmp_path / "chapters" / f"Chapter {number}"
        chapter.mkdir(parents=True)
        (chapter / "001.md").write_text(f"chapter {number}\n", encoding="utf-8")
    orchestration = StorydexOrchestrationService(story_project_service=project)
    policy = ContextPolicy(
        story_structured_memory=False,
        passive_fts=False,
        wiki_context=False,
        coomi_memory=False,
        active_retrieval_tools=False,
    )

    first = orchestration.build_turn_contract(
        tmp_path,
        prompt="Summarize without writing files.",
        context_policy=policy,
    )
    second = orchestration.build_turn_contract(
        tmp_path,
        prompt="Summarize without writing files.",
        context_policy=policy,
    )

    assert first["performanceTrace"]["chapterSnapshotBuildCount"] == 1
    assert second["performanceTrace"]["chapterSnapshotBuildCount"] == 1
    assert second["performanceTrace"]["directoryScanCount"] == 0
    assert second["performanceTrace"]["statCount"] == 0
    assert second["performanceTrace"]["catalogRefreshMs"] == 0
    assert first["contentCatalog"]["catalogRevision"] == second["contentCatalog"][
        "catalogRevision"
    ]
    assert first["contentCatalog"]["generation"] == second["contentCatalog"][
        "generation"
    ]
    published = get_content_catalog_service(tmp_path).snapshot()
    canonical_first = "chapters/" + project._build_chapter_display_name(
        "Chapter 1",
        fallback_number=1,
    )
    assert canonical_first in published.directories
    assert "chapters/Chapter 1" not in published.directories


def test_runtime_translator_preserves_init_and_read_provenance() -> None:
    translator = _CoomiEventTranslator(session_id="session")
    revision = "sha256:" + ("a" * 64)

    initialized = translator.translate(
        {
            "type": "runtime_initialized",
            "data": {"componentInitMs": 12.3456, "mcpInitMs": 4.5},
        }
    )
    assert initialized == (
        "RuntimeMetrics",
        {
            "_type": "RuntimeMetrics",
            "_version": 1,
            "bridgeStartMs": 0.0,
            "componentInitMs": 12.346,
            "mcpInitMs": 4.5,
        },
    )

    translator.translate(
        {
            "type": "tool_started",
            "data": {
                "call": {
                    "id": "read-1",
                    "name": "read_file",
                    "arguments": {"path": "chapters/001.md", "offset": 1},
                }
            },
        }
    )
    name, completed = translator.translate(
        {
            "type": "tool_finished",
            "data": {
                "call": {
                    "id": "read-1",
                    "name": "read_file",
                    "arguments": {"path": "chapters/001.md", "offset": 1},
                },
                "result": {
                    "success": True,
                    "output": (
                        '{"path":"chapters/001.md","revision":"'
                        + revision
                        + '","span":{"startLine":1,"endLine":3,"revision":"'
                        + revision
                        + '"},"content":"'
                        + ("x" * 5000)
                        + '"}'
                    ),
                },
            },
        }
    )

    assert name == "ToolDone"
    assert completed["source_revision"] == revision
    assert completed["source_span"] == {
        "startLine": 1,
        "endLine": 3,
        "revision": revision,
    }
    assert completed["arguments"]["path"] == "chapters/001.md"

    name, model_completed = translator.translate(
        {"type": "model_completed", "data": {"round": 1, "usage": {}}}
    )
    assert name == "ModelCompleted"
    assert model_completed["runtimeMetrics"]["componentInitMs"] == 12.346


def test_trace_metrics_distinguish_logical_and_transmitted_tokens() -> None:
    revision = "sha256:" + ("b" * 64)
    read_data = {
        "tool_name": "read_file",
        "tool_call_id": "read-1",
        "arguments": {"path": "chapters/001.md", "offset": 1},
        "source_path": "chapters/001.md",
        "source_revision": revision,
        "source_span": {"startLine": 1, "endLine": 3},
    }
    events = [
        {"event": "RuntimeMetrics", "data": {"bridgeStartMs": 1.5}},
        {"event": "RuntimeMetrics", "data": {"componentInitMs": 8.25}},
        {"event": "ToolDone", "data": read_data},
        {"event": "ToolDone", "data": {**read_data, "tool_call_id": "read-2"}},
        {
            "event": "ModelCompleted",
            "data": {"usage": {"input_tokens": 100, "cached_input_tokens": 40}},
        },
        {
            "event": "ModelCompleted",
            "data": {"usage": {"input_tokens": 140, "cached_input_tokens": 90}},
        },
    ]

    metrics = routes._extract_trace_metrics(events, "trace", 20)

    assert metrics["modelRounds"] == 2
    assert metrics["logicalInputTokens"] == 140
    assert metrics["transmittedInputTokens"] == 240
    assert metrics["cachedInputTokens"] == 130
    assert metrics["duplicateToolCallsSameRevision"] == 1
    assert metrics["bridgeStartMs"] == 1.5
    assert metrics["componentInitMs"] == 8.25

    response_trace = ApiTrace(**metrics).model_dump(by_alias=True)
    assert response_trace["transmittedInputTokens"] == 240
    assert response_trace["componentInitMs"] == 8.25
