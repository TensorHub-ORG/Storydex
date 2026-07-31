from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from api import routes_agent as routes
from services.agent_git_autocommit_service import AgentGitSnapshot
from services.story_semantic_budget_controller import (
    SEMANTIC_BUDGET_STRATEGY,
    SemanticBudgetResult,
)
from services.storydex_orchestration_service import get_storydex_orchestration_service


@pytest.fixture(autouse=True)
def _disable_tier_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORY_LENGTH_TIER_ENABLED", "0")


def _decode_sse(chunk: str) -> tuple[str, dict[str, Any]]:
    event_name = ""
    payload: dict[str, Any] = {}
    for line in chunk.splitlines():
        if line.startswith("event: "):
            event_name = line[7:]
        elif line.startswith("data: "):
            payload = json.loads(line[6:])
    return event_name, payload


def _contract(*, semantic: bool = True, target: int = 3000) -> Dict[str, Any]:
    turn_plan: Dict[str, Any] = {
        "fragmentCount": 1,
        "chapterWordCountTarget": target,
        "operationType": "create_new",
        "wordCountPolicy": {"scope": "chapter", "target": target},
        "fragmentTargets": [
            {
                "order": 1,
                "path": "chapters/chapter-1/001.md",
                "writeMode": "replace",
                "baselineWordCount": 0,
            }
        ],
    }
    if semantic:
        turn_plan["generationControl"] = {
            "strategy": SEMANTIC_BUDGET_STRATEGY,
            "productTargetWordCount": target,
            "sceneCount": 4,
            "internalToleranceRatio": 0.20,
            "finalToleranceRatio": 0.15,
            "maximumSceneRevisions": 2,
            "applyMode": "single_commit",
            "rolloutMode": "gated_direct",
        }
    return {
        "_type": "TurnContract",
        "_version": 1,
        "status": "ready",
        "intentFrame": {"primary": "story_generation"},
        "turnPlan": turn_plan,
    }


def _result(status: str = "completed") -> SemanticBudgetResult:
    completed = status == "completed"
    return SemanticBudgetResult(
        status=status,
        strategy=SEMANTIC_BUDGET_STRATEGY,
        target_word_count=3000,
        generated_word_count=3000 if completed else 0,
        acceptance_minimum=2100,
        acceptance_maximum=3900,
        within_acceptance=completed,
        text="generated prose。" if completed else "",
        plan=[],
        scenes=[{"order": 1, "acceptedWordCount": 3000}] if completed else [],
        events=[],
        provider_calls=5 if completed else 1,
        revision_attempts=0,
        revision_acceptances=0,
        duration_ms=10,
        error={} if completed else {"type": "ProviderError", "stage": "planning"},
    )


def test_story_generation_normalizer_preserves_only_known_strategy() -> None:
    semantic = routes._normalize_story_generation_options(
        {"chapterWordCountTarget": 3000, "generationStrategy": SEMANTIC_BUDGET_STRATEGY}
    )
    invalid = routes._normalize_story_generation_options(
        {"chapterWordCountTarget": 3000, "generationStrategy": "unknown"}
    )

    assert semantic["generationStrategy"] == SEMANTIC_BUDGET_STRATEGY
    assert "generationStrategy" not in invalid


def test_turn_contract_adds_generation_control_only_when_explicitly_requested(
    tmp_path: Path,
) -> None:
    service = get_storydex_orchestration_service()
    intent = {
        "primary": "story_generation",
        "operationType": "create_new",
        "complexity": "simple",
        "confidence": 1.0,
        "source": "test",
        "secondary": [],
        "needsTools": True,
        "needsPlanning": False,
        "isAdvisory": False,
    }
    semantic = service.build_turn_contract(
        tmp_path,
        prompt="continue",
        story_generation={
            "fragmentCount": 1,
            "chapterWordCountTarget": 3000,
            "generationStrategy": SEMANTIC_BUDGET_STRATEGY,
        },
        intent_frame=intent,
    )
    short_semantic = service.build_turn_contract(
        tmp_path,
        prompt="continue",
        story_generation={
            "fragmentCount": 1,
            "chapterWordCountTarget": 1500,
            "generationStrategy": SEMANTIC_BUDGET_STRATEGY,
        },
        intent_frame=intent,
    )
    legacy = service.build_turn_contract(
        tmp_path,
        prompt="continue",
        story_generation={"fragmentCount": 1, "chapterWordCountTarget": 3000},
        intent_frame=intent,
    )

    control = semantic["turnPlan"]["generationControl"]
    assert control["strategy"] == SEMANTIC_BUDGET_STRATEGY
    assert control["sceneCount"] == 4
    assert control["maximumSceneRevisions"] == 2
    assert control["applyMode"] == "single_commit"
    short_control = short_semantic["turnPlan"]["generationControl"]
    assert short_control["sceneCount"] == 2
    assert short_control["maximumSceneRevisions"] == 2
    assert "generationControl" not in legacy["turnPlan"]


def test_semantic_gate_requires_both_explicit_strategy_and_project_flag(tmp_path: Path) -> None:
    contract = _contract()

    disabled = routes._semantic_budget_gate(tmp_path, contract)
    flag_path = tmp_path / ".storydex" / "config" / "feature-flags.json"
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(
        json.dumps({"SEMANTIC_BUDGET_GENERATION_ENABLED": True}),
        encoding="utf-8",
    )
    enabled = routes._semantic_budget_gate(tmp_path, contract)

    assert disabled == {"requested": True, "enabled": False, "reason": "feature_flag_disabled"}
    assert enabled == {"requested": True, "enabled": True, "reason": "enabled"}
    assert routes._semantic_budget_gate(tmp_path, _contract(semantic=False))["requested"] is False

    inquiry = _contract()
    inquiry["turnPlan"]["operationType"] = "inquiry"
    assert routes._semantic_budget_gate(tmp_path, inquiry) == {
        "requested": True,
        "enabled": False,
        "reason": "operation_not_create_new",
    }


def test_execute_semantic_budget_applies_completed_text_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _result()
    emitted: list[tuple[str, Dict[str, Any]]] = []

    class FakeController:
        async def generate(self, _request: Any, _adapter: Any, *, event_sink: Any) -> SemanticBudgetResult:
            event_sink("SemanticBudgetProgress", {"state": "PLANNING"})
            return result

    class FakeAdapter:
        provider_attempts = 5
        provider_retries = 0

        def __init__(self, **_kwargs: Any) -> None:
            pass

    class ProjectService:
        def __init__(self) -> None:
            self.apply_calls: list[tuple[Path, Dict[str, Any], Dict[str, Any]]] = []
            self.validation_calls = 0

        def apply_story_generation_increment(
            self,
            root: Path,
            payload: Dict[str, Any],
            *,
            generation_contract: Dict[str, Any],
        ) -> Dict[str, Any]:
            self.apply_calls.append((root, payload, generation_contract))
            return {"ok": True, "writtenPaths": ["chapters/chapter-1/001.md"]}

        def validate_story_generation_turn(
            self,
            _root: Path,
            _contract: Dict[str, Any],
        ) -> Dict[str, Any]:
            self.validation_calls += 1
            return {"passed": True, "generatedWordCount": 3000}

    project = ProjectService()
    monkeypatch.setattr(routes, "SemanticBudgetController", FakeController)
    monkeypatch.setattr(routes, "CoomiStoryGenerationAdapter", FakeAdapter)
    monkeypatch.setattr(routes, "story_project_service", project)
    monkeypatch.setattr(routes, "read_scene_constraint_context", lambda _root: ("style", []))

    outcome = asyncio.run(
        routes._execute_semantic_budget_generation(
            prompt="continue",
            trace_id="trace",
            active_file="chapters/chapter-0/001.md",
            workspace_root=tmp_path,
            turn_contract=_contract(),
            event_sink=lambda name, payload: emitted.append((name, payload)),
        )
    )

    assert outcome["ok"] is True
    assert len(project.apply_calls) == 1
    assert project.validation_calls == 1
    _root, payload, contract = project.apply_calls[0]
    assert contract is not None
    assert payload["fragments"] == [
        {"path": "chapters/chapter-1/001.md", "text": "generated prose。"}
    ]
    assert payload["applyVariables"] is False
    assert [name for name, _payload in emitted].count("SemanticBudgetResult") == 1
    progress_states = [
        payload["state"]
        for name, payload in emitted
        if name == "SemanticBudgetProgress"
    ]
    assert progress_states[-2:] == ["APPLYING", "COMPLETED"]
    assert emitted[-1][0] == "SemanticBudgetResult"
    assert emitted[-1][1]["state"] == "COMPLETED"


def test_execute_semantic_budget_never_applies_failed_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeController:
        async def generate(self, _request: Any, _adapter: Any, *, event_sink: Any) -> SemanticBudgetResult:
            del event_sink
            return _result("failed_provider")

    class FakeAdapter:
        provider_attempts = 1
        provider_retries = 0

        def __init__(self, **_kwargs: Any) -> None:
            pass

    class ProjectService:
        def apply_story_generation_increment(self, *_args: Any, **_kwargs: Any) -> Dict[str, Any]:
            raise AssertionError("failed semantic candidate must not be applied")

    monkeypatch.setattr(routes, "SemanticBudgetController", FakeController)
    monkeypatch.setattr(routes, "CoomiStoryGenerationAdapter", FakeAdapter)
    monkeypatch.setattr(routes, "story_project_service", ProjectService())
    monkeypatch.setattr(routes, "read_scene_constraint_context", lambda _root: ("", []))

    outcome = asyncio.run(
        routes._execute_semantic_budget_generation(
            prompt="continue",
            trace_id="trace",
            active_file="",
            workspace_root=tmp_path,
            turn_contract=_contract(),
            event_sink=lambda _name, _payload: None,
        )
    )

    assert outcome["ok"] is False
    assert outcome["applyResult"] == {}


def test_execute_semantic_budget_reports_apply_failure_without_false_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    emitted: list[tuple[str, Dict[str, Any]]] = []

    class FakeController:
        async def generate(self, _request: Any, _adapter: Any, *, event_sink: Any) -> SemanticBudgetResult:
            event_sink(
                "SemanticBudgetProgress",
                {"_type": "SemanticBudgetProgress", "state": "COMPLETED"},
            )
            return _result()

    class FakeAdapter:
        provider_attempts = 5
        provider_retries = 0

        def __init__(self, **_kwargs: Any) -> None:
            pass

    class ProjectService:
        def apply_story_generation_increment(self, *_args: Any, **_kwargs: Any) -> Dict[str, Any]:
            raise OSError("disk unavailable")

    monkeypatch.setattr(routes, "SemanticBudgetController", FakeController)
    monkeypatch.setattr(routes, "CoomiStoryGenerationAdapter", FakeAdapter)
    monkeypatch.setattr(routes, "story_project_service", ProjectService())
    monkeypatch.setattr(routes, "read_scene_constraint_context", lambda _root: ("", []))

    outcome = asyncio.run(
        routes._execute_semantic_budget_generation(
            prompt="continue",
            trace_id="trace",
            active_file="",
            workspace_root=tmp_path,
            turn_contract=_contract(),
            event_sink=lambda name, payload: emitted.append((name, payload)),
        )
    )
    progress_states = [
        payload["state"]
        for name, payload in emitted
        if name == "SemanticBudgetProgress"
    ]
    result_packets = [payload for name, payload in emitted if name == "SemanticBudgetResult"]

    assert outcome["ok"] is False
    assert progress_states[-2:] == ["APPLYING", "FAILED"]
    assert "COMPLETED" not in progress_states
    assert len(result_packets) == 1
    assert result_packets[0]["state"] == "FAILED"
    assert result_packets[0]["status"] == "failed_apply"
    assert result_packets[0]["error"] == {
        "type": "StoryGenerationApplyError",
        "causeType": "OSError",
    }


def _collect_semantic_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    success: bool,
    semantic_enabled: bool = True,
    cancel_after_progress: bool = False,
    cancel_during_apply: bool = False,
) -> Dict[str, Any]:
    class RuntimeService:
        stream_calls = 0

        def get_status(self, *, workspace_root: Path) -> Dict[str, Any]:
            del workspace_root
            return {"providerId": "test-provider", "model": "test-model"}

        async def stream_events(self, **_kwargs: Any):
            self.stream_calls += 1
            if semantic_enabled:
                raise AssertionError("legacy runtime must not run for an enabled semantic request")
            yield "AgentStarted", {
                "_type": "AgentStarted",
                "llmProvider": "test-provider",
                "llmModel": "test-model",
            }
            yield "TextChunk", {"_type": "TextChunk", "content": "legacy reply"}
            yield "AgentCompleted", {"_type": "AgentCompleted", "total_tokens": 1}

    class ProjectService:
        def read_project_settings(self, _root: Path) -> Dict[str, Any]:
            return {"agentCommitPromptEnabled": False}

        def validate_story_generation_turn(
            self,
            _root: Path,
            _contract: Dict[str, Any],
        ) -> Dict[str, Any]:
            return {"applicable": False, "passed": True}

    class CalibrationService:
        def __init__(self) -> None:
            self.calls: list[Dict[str, Any]] = []

        def record_generation_result(self, _root: Path, **kwargs: Any) -> bool:
            self.calls.append(kwargs)
            return True

    class GitService:
        def finish_turn(self, _snapshot: AgentGitSnapshot, **_kwargs: Any) -> Dict[str, Any]:
            return {"_type": "GitAutoCommit", "status": "info", "created": False}

    class Handle:
        is_cancelled = False
        cancel_reason = ""

        def __init__(self) -> None:
            self.finalize_calls = 0

        def cancel(self, reason: str) -> bool:
            self.is_cancelled = True
            self.cancel_reason = reason
            return True

        async def finalize(self, observation: Any, context: Any) -> None:
            self.finalize_calls += 1
            status = "failed" if observation.error_message else "cancelled" if observation.cancelled else "completed"
            context.on_git_payload(context.finish_git())
            context.on_terminal(status, observation.error_message)
            payload = context.build_payload(status, observation.error_message, False, {})
            if isinstance(payload.get("record"), dict):
                context.persist_trace(payload["record"])

    result = _result() if success else _result("failed_quality")
    result_packet = {
        "_type": "SemanticBudgetResult",
        "state": "COMPLETED" if success else "FAILED",
        "status": result.status,
        "providerCalls": result.provider_calls,
    }
    execute_calls = 0
    cancellation_observed = False
    apply_cancelled = False
    apply_finished = False
    apply_release: asyncio.Event | None = None

    async def fake_execute(**kwargs: Any) -> Dict[str, Any]:
        nonlocal execute_calls, cancellation_observed, apply_cancelled, apply_finished
        execute_calls += 1
        kwargs["event_sink"](
            "SemanticBudgetProgress",
            {"_type": "SemanticBudgetProgress", "state": "PLANNING"},
        )
        if cancel_after_progress:
            try:
                await asyncio.Future()
            finally:
                cancellation_observed = True
        if cancel_during_apply:
            assert apply_release is not None
            kwargs["event_sink"](
                "SemanticBudgetProgress",
                {"_type": "SemanticBudgetProgress", "state": "APPLYING"},
            )
            try:
                await apply_release.wait()
            except asyncio.CancelledError:
                apply_cancelled = True
                raise
            apply_finished = True
        return {
            "ok": success,
            "result": result,
            "resultPacket": result_packet,
            "applyResult": {"ok": success},
            "validation": {"passed": success, "generatedWordCount": 3000},
            "targetPath": "chapters/chapter-1/001.md",
            "applyError": {},
        }

    runtime = RuntimeService()
    calibration = CalibrationService()
    handle = Handle()
    monkeypatch.setattr(
        routes,
        "_semantic_budget_gate",
        lambda *_args: {
            "requested": True,
            "enabled": semantic_enabled,
            "reason": "enabled" if semantic_enabled else "feature_flag_disabled",
        },
    )
    monkeypatch.setattr(routes, "_execute_semantic_budget_generation", fake_execute)
    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: runtime)
    monkeypatch.setattr(routes, "story_project_service", ProjectService())
    monkeypatch.setattr(routes, "story_length_calibration_service", calibration)
    monkeypatch.setattr(routes, "agent_git_autocommit_service", GitService())
    monkeypatch.setattr(routes, "_reconcile_story_knowledge_projection", lambda _root: {"_type": "KnowledgeProjectionUpdated", "ok": True, "changedSourcePaths": []})
    monkeypatch.setattr(routes, "_persist_execution_trace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        routes,
        "_build_chat_payload",
        lambda **kwargs: {"record": {"status": kwargs["status"], "traceId": kwargs["trace_id"]}},
    )

    async def collect() -> tuple[list[tuple[str, Dict[str, Any]]], list[str]]:
        nonlocal apply_release
        apply_release = asyncio.Event()
        packets: list[tuple[str, Dict[str, Any]]] = []
        async for chunk in routes._stream_coomi_sse_worker(
                prompt="continue",
                trace_id="trace",
                session_id="session",
                active_file="",
                workspace_root=tmp_path,
                story_generation={"generationStrategy": SEMANTIC_BUDGET_STRATEGY},
                turn_contract=_contract(),
                git_snapshot=AgentGitSnapshot(workspace_root=tmp_path, available=False),
                cancellation_token=routes._CancellationToken(),
                execution_handle=handle,
            ):
            packet = _decode_sse(chunk)
            packets.append(packet)
            if cancel_after_progress and packet[0] == "SemanticBudgetProgress":
                handle.cancel("test_cancel")
            if (
                cancel_during_apply
                and packet[0] == "SemanticBudgetProgress"
                and packet[1].get("state") == "APPLYING"
            ):
                handle.cancel("test_cancel_during_apply")
                assert apply_release is not None
                asyncio.get_running_loop().call_later(0.01, apply_release.set)
        semantic_tasks = [
            task.get_name()
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("storydex-semantic-budget-")
        ]
        return packets, semantic_tasks

    packets, semantic_tasks = asyncio.run(collect())

    return {
        "packets": packets,
        "runtime": runtime,
        "calibration": calibration,
        "handle": handle,
        "executeCalls": execute_calls,
        "cancellationObserved": cancellation_observed,
        "semanticTasks": semantic_tasks,
        "applyCancelled": apply_cancelled,
        "applyFinished": apply_finished,
    }


def test_worker_uses_semantic_path_and_emits_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _collect_semantic_worker(monkeypatch, tmp_path, success=True)
    names = [name for name, _payload in result["packets"]]

    assert result["runtime"].stream_calls == 0
    assert result["executeCalls"] == 1
    assert "SemanticBudgetProgress" in names
    assert "StoryGenerationValidation" in names
    assert "TextChunk" in names
    assert "AgentError" not in names
    assert names.count("AgentCompleted") == 1
    assert len(result["calibration"].calls) == 1
    assert result["handle"].finalize_calls == 1


def test_worker_reports_semantic_failure_without_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _collect_semantic_worker(monkeypatch, tmp_path, success=False)
    names = [name for name, _payload in result["packets"]]

    assert result["runtime"].stream_calls == 0
    assert result["executeCalls"] == 1
    assert names.count("AgentError") == 1
    assert "AgentCompleted" not in names
    assert "AgentCancelled" not in names
    assert "StoryGenerationValidation" not in names
    assert not result["calibration"].calls
    assert result["handle"].finalize_calls == 1


def test_worker_falls_back_to_legacy_runtime_when_semantic_gate_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _collect_semantic_worker(
        monkeypatch,
        tmp_path,
        success=True,
        semantic_enabled=False,
    )
    names = [name for name, _payload in result["packets"]]

    assert names.count("SemanticBudgetFallback") == 1
    assert result["runtime"].stream_calls == 1
    assert result["executeCalls"] == 0
    assert "legacy reply" in next(
        payload["content"] for name, payload in result["packets"] if name == "TextChunk"
    )
    assert names.count("AgentCompleted") == 1
    assert "AgentError" not in names
    assert result["handle"].finalize_calls == 1


def test_worker_cancels_semantic_task_without_duplicate_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _collect_semantic_worker(
        monkeypatch,
        tmp_path,
        success=True,
        cancel_after_progress=True,
    )
    names = [name for name, _payload in result["packets"]]

    assert result["runtime"].stream_calls == 0
    assert result["executeCalls"] == 1
    assert result["cancellationObserved"] is True
    assert result["semanticTasks"] == []
    assert names.count("AgentCancelled") == 1
    assert "AgentCompleted" not in names
    assert "AgentError" not in names
    assert "StoryGenerationValidation" not in names
    assert result["handle"].finalize_calls == 1


def test_worker_waits_for_in_flight_apply_before_cancel_finalization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _collect_semantic_worker(
        monkeypatch,
        tmp_path,
        success=True,
        cancel_during_apply=True,
    )
    names = [name for name, _payload in result["packets"]]

    assert result["applyCancelled"] is False
    assert result["applyFinished"] is True
    assert result["semanticTasks"] == []
    assert names.count("AgentCancelled") == 1
    assert "AgentCompleted" not in names
    assert result["handle"].finalize_calls == 1
