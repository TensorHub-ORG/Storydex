from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from api import routes_agent as routes
from services.agent_git_autocommit_service import AgentGitSnapshot
from services.story_project_service import (
    DEFAULT_CHAPTER_TEMPLATE_ID,
    SINGLE_FILE_CHAPTER_TEMPLATE_ID,
    get_story_project_service,
)
from services.story_word_count_service import STORY_WORD_COUNT_ALGORITHM, count_story_text_words
from services.storydex_coomi_runtime_tools import StorydexEditTool, StorydexWriteTool
from services.storydex_orchestration_service import get_storydex_orchestration_service
from storage.workspace_io import WorkspaceIO


def _story_contract(
    root: Path,
    *,
    fragment_count: int = 1,
    fragment_word_count: int = 100,
    template_id: str = DEFAULT_CHAPTER_TEMPLATE_ID,
    active_file: str = "",
    prompt: str = "请续写剧情",
) -> dict[str, Any]:
    return get_storydex_orchestration_service().build_turn_contract(
        root,
        prompt=prompt,
        active_file=active_file,
        story_generation={
            "fragmentCount": fragment_count,
            "fragmentWordCount": fragment_word_count,
            "chapterTemplateId": template_id,
        },
        intent_frame={
            "primary": "story_generation",
            "confidence": 1.0,
            "source": "test",
            "secondary": [],
            "needsTools": True,
            "needsPlanning": True,
            "isAdvisory": False,
        },
    )


def _decode_sse(chunk: str) -> tuple[str, dict[str, Any]]:
    event_name = ""
    payload: dict[str, Any] = {}
    for line in chunk.splitlines():
        if line.startswith("event: "):
            event_name = line[7:]
        elif line.startswith("data: "):
            payload = json.loads(line[6:])
    return event_name, payload


def test_story_word_count_is_shared_with_workspace_file_statistics() -> None:
    content = "甲 乙\nCoomi\t🙂"
    expected = len("甲乙Coomi🙂")
    assert count_story_text_words(content) == expected
    assert WorkspaceIO._count_story_text_words(content) == expected


def test_built_in_chapter_templates_cover_multi_and_single_file(tmp_path: Path) -> None:
    templates = {item["id"]: item for item in get_story_project_service().list_chapter_templates(tmp_path)}
    assert templates[DEFAULT_CHAPTER_TEMPLATE_ID]["contentMode"] == "multi_fragment"
    assert templates[DEFAULT_CHAPTER_TEMPLATE_ID]["segmentNaming"] == "001.md"
    assert templates[SINGLE_FILE_CHAPTER_TEMPLATE_ID]["contentMode"] == "single_file"
    assert templates[SINGLE_FILE_CHAPTER_TEMPLATE_ID]["segmentNaming"] == "正文.md"


def test_multi_fragment_contract_keeps_more_than_three_files_in_one_chapter(tmp_path: Path) -> None:
    contract = _story_contract(tmp_path, fragment_count=6)
    plan = contract["turnPlan"]
    targets = plan["fragmentTargets"]
    assert plan["fragmentCount"] == 6
    assert len(targets) == 6
    assert len({Path(item["path"]).parent.as_posix() for item in targets}) == 1
    assert [Path(item["path"]).name for item in targets] == [f"{index:03d}.md" for index in range(1, 7)]


def test_single_file_contract_forces_one_file_and_persists_template_setting(tmp_path: Path) -> None:
    service = get_story_project_service()
    settings = service.write_project_settings(
        tmp_path,
        {"storyChapterTemplateId": SINGLE_FILE_CHAPTER_TEMPLATE_ID},
    )
    contract = _story_contract(
        tmp_path,
        fragment_count=7,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
    )
    plan = contract["turnPlan"]
    assert settings["storyChapterTemplateId"] == SINGLE_FILE_CHAPTER_TEMPLATE_ID
    assert service.read_project_settings(tmp_path)["storyChapterTemplateId"] == SINGLE_FILE_CHAPTER_TEMPLATE_ID
    assert plan["requestedFragmentCount"] == 7
    assert plan["fragmentCount"] == 1
    assert plan["chapterContentMode"] == "single_file"
    assert len(plan["fragmentTargets"]) == 1
    assert Path(plan["fragmentTargets"][0]["path"]).name == "正文.md"


@pytest.mark.parametrize("actual_word_count", [99, 101])
def test_inexact_story_fragment_is_rejected_before_any_file_write(
    tmp_path: Path,
    actual_word_count: int,
) -> None:
    service = get_story_project_service()
    contract = _story_contract(tmp_path, fragment_word_count=100)
    target_path = contract["turnPlan"]["fragmentTargets"][0]["path"]
    result = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "字" * actual_word_count}]},
        generation_contract=contract,
    )
    assert result["ok"] is False
    assert result["code"] == "story_generation_constraints_not_met"
    assert result["wordCountValidation"]["fragments"][0]["difference"] == actual_word_count - 100
    assert not (tmp_path / target_path).exists()


def test_exact_story_fragment_writes_and_validates_with_objective_count(tmp_path: Path) -> None:
    service = get_story_project_service()
    contract = _story_contract(tmp_path, fragment_word_count=100)
    result = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "字" * 100}]},
        generation_contract=contract,
    )
    validation = service.validate_story_generation_turn(tmp_path, contract)
    assert result["ok"] is True
    assert result["fragments"][0]["generatedWordCount"] == 100
    assert result["fragments"][0]["targetWordCount"] == 100
    assert result["fragments"][0]["wordCountStatus"] == "passed"
    assert result["fragments"][0]["wordCountAlgorithm"] == STORY_WORD_COUNT_ALGORITHM
    assert validation["passed"] is True
    assert validation["fragments"][0]["generatedWordCount"] == 100


def test_single_file_continuation_uses_baseline_and_cannot_append_twice(tmp_path: Path) -> None:
    service = get_story_project_service()
    first = _story_contract(
        tmp_path,
        fragment_word_count=100,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
    )
    first_result = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "甲" * 100}]},
        generation_contract=first,
    )
    target_path = first["turnPlan"]["fragmentTargets"][0]["path"]
    assert first_result["ok"] is True

    continuation = _story_contract(
        tmp_path,
        fragment_word_count=100,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
        active_file=target_path,
    )
    target = continuation["turnPlan"]["fragmentTargets"][0]
    assert target["writeMode"] == "append"
    assert target["baselineWordCount"] == 100
    appended = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "乙" * 100}]},
        generation_contract=continuation,
    )
    duplicate = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "丙" * 100}]},
        generation_contract=continuation,
    )
    assert appended["ok"] is True
    assert service.count_story_file_words(tmp_path / target_path) == 200
    assert duplicate["ok"] is False
    assert duplicate["wordCountValidation"]["fragments"][0]["baselineMatches"] is False
    assert service.count_story_file_words(tmp_path / target_path) == 200


def test_plain_write_and_edit_tools_cannot_bypass_story_generation_contract(tmp_path: Path) -> None:
    contract = {"intentFrame": {"primary": "story_generation"}}
    write_result = StorydexWriteTool(workspace_root=tmp_path, turn_contract=contract).run(
        {"file_path": "chapters/第1章/001.md", "content": "正文"}
    )
    edit_result = StorydexEditTool(workspace_root=tmp_path, turn_contract=contract).run(
        {"file_path": "chapters/第1章/001.md", "old_string": "正", "new_string": "改"}
    )
    assert write_result.success is False
    assert edit_result.success is False
    assert "StorydexApplyStoryIncrement" in str(write_result.error)
    assert not (tmp_path / "chapters/第1章/001.md").exists()


def test_failed_post_run_validation_continues_in_same_execution_before_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class RuntimeService:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_events(self, **_kwargs: Any):
            self.calls += 1
            yield "AgentStarted", {"_type": "AgentStarted", "_version": 1}
            yield "TextChunk", {"_type": "TextChunk", "_version": 1, "content": f"attempt-{self.calls}"}
            if self.calls >= 2:
                yield "ToolDone", {
                    "_type": "ToolDone",
                    "_version": 1,
                    "tool_name": "StorydexApplyStoryIncrement",
                    "tool_call_id": "write-1",
                    "is_error": False,
                }
            yield "AgentCompleted", {"_type": "AgentCompleted", "_version": 1, "total_tokens": 1}

    class ProjectService:
        def __init__(self) -> None:
            self.validations = 0

        def read_project_settings(self, _root: Path) -> dict[str, Any]:
            return {"agentCommitPromptEnabled": False}

        def validate_story_generation_turn(self, _root: Path, _contract: dict[str, Any]) -> dict[str, Any]:
            self.validations += 1
            passed = self.validations >= 2
            return {
                "_type": "StoryGenerationValidation",
                "_version": 1,
                "applicable": True,
                "passed": passed,
                "status": "success" if passed else "error",
                "algorithm": STORY_WORD_COUNT_ALGORITHM,
                "countingRule": "count every non-whitespace Unicode character",
                "exact": True,
                "fragmentCount": 1,
                "targetWordCount": 100,
                "chapterContentMode": "multi_fragment",
                "structurePassed": True,
                "fragments": [
                    {
                        "order": 1,
                        "path": "chapters/第1章/001.md",
                        "exists": passed,
                        "writeMode": "replace",
                        "baselineWordCount": 0,
                        "generatedWordCount": 100 if passed else 90,
                        "targetWordCount": 100,
                        "difference": 0 if passed else -10,
                        "status": "passed" if passed else "failed",
                    }
                ],
                "message": "passed" if passed else "needs correction",
            }

    class GitService:
        def finish_turn(self, _snapshot: AgentGitSnapshot, **_kwargs: Any) -> dict[str, Any]:
            return {"_type": "GitAutoCommit", "status": "info", "created": False}

    class Handle:
        is_cancelled = False
        cancel_reason = ""

        def __init__(self, runtime: RuntimeService) -> None:
            self.runtime = runtime
            self.finalize_calls = 0
            self.runtime_calls_at_finalize = 0

        def cancel(self, reason: str) -> bool:
            self.is_cancelled = True
            self.cancel_reason = reason
            return True

        async def finalize(self, observation: Any, context: Any) -> None:
            self.finalize_calls += 1
            self.runtime_calls_at_finalize = self.runtime.calls
            status = "failed" if observation.error_message else "cancelled" if observation.cancelled else "completed"
            git_payload = context.finish_git()
            if context.on_git_payload:
                context.on_git_payload(git_payload)
            if context.on_terminal:
                context.on_terminal(status, observation.error_message)
            payload = context.build_payload(status, observation.error_message, False, {})
            if context.persist_trace and isinstance(payload.get("record"), dict):
                context.persist_trace(payload["record"])

    runtime = RuntimeService()
    project = ProjectService()
    handle = Handle(runtime)
    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: runtime)
    monkeypatch.setattr(routes, "story_project_service", project)
    monkeypatch.setattr(routes, "agent_git_autocommit_service", GitService())
    monkeypatch.setattr(routes, "_persist_execution_trace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        routes,
        "_build_chat_payload",
        lambda **kwargs: {
            "record": {
                "traceId": kwargs["trace_id"],
                "sessionId": kwargs["session_id"],
                "status": kwargs["status"],
            }
        },
    )

    turn_contract = {
        "_type": "TurnContract",
        "_version": 1,
        "status": "ready",
        "intentFrame": {"primary": "story_generation"},
        "turnPlan": {"fragmentCount": 1, "fragmentWordCount": 100},
    }

    async def collect() -> list[tuple[str, dict[str, Any]]]:
        return [
            _decode_sse(chunk)
            async for chunk in routes._stream_coomi_sse_worker(
                prompt="generate",
                trace_id="trace-story",
                session_id="session-story",
                active_file="",
                workspace_root=tmp_path,
                story_generation={"fragmentCount": 1, "fragmentWordCount": 100},
                turn_contract=turn_contract,
                git_snapshot=AgentGitSnapshot(workspace_root=tmp_path, available=False),
                cancellation_token=routes._CancellationToken(),
                execution_handle=handle,
            )
        ]

    packets = asyncio.run(collect())
    event_names = [name for name, _payload in packets]
    validations = [payload for name, payload in packets if name == "StoryGenerationValidation"]
    continuations = [payload for name, payload in packets if name == "ContinuationStarted"]
    assert runtime.calls == 2
    assert project.validations == 2
    assert [item["passed"] for item in validations] == [False, True]
    assert len(continuations) == 1
    assert continuations[0]["continuationMode"] == "story_generation_correction"
    assert event_names.count("AgentCompleted") == 1
    assert "AgentError" not in event_names
    assert handle.finalize_calls == 1
    assert handle.runtime_calls_at_finalize == 2
