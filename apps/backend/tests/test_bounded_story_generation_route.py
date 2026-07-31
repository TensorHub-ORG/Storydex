"""W2-4: the bounded prose path as it is actually wired into the SSE worker.

The pipeline and the precision controller are covered on their own. What these
tests protect is the wiring, where the regressions are the expensive ones:

* the legacy Agent tool loop must not also run for a bounded turn (that would
  spend a second draft and write the chapter twice);
* the ``StoryCallAccounting`` event must carry the recorded ledger, because a
  count derived from the trace cannot see a call nobody announced;
* calibration must learn from the *draft*, never from the revised candidate;
* the write-then-append correction continuation must not fire for a current
  bounded word-count contract, which is the double-write this work removed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from api import routes_agent as routes
from services.agent_git_autocommit_service import AgentGitSnapshot
from services.story_call_accounting import (
    STORY_INITIAL_GENERATION_PURPOSE,
    STORY_LENGTH_REVISION_PURPOSE,
    STORY_SECOND_DRAFT_PURPOSE,
    StoryCallAccounting,
)
from services.story_generation_pipeline import (
    CANDIDATE_SOURCE_DRAFT,
    CANDIDATE_SOURCE_REVISION,
    SELECTION_PRECISION_ACHIEVED,
    SELECTION_PRECISION_DISABLED,
)
from services.story_semantic_budget_controller import SEMANTIC_BUDGET_STRATEGY
from services.story_word_count_service import (
    STORY_OVER_BUDGET_KEEP_MESSAGE,
    STORY_UNDER_BUDGET_KEEP_MESSAGE,
    WORD_COUNT_POLICY_VERSION,
)

TARGET = 3000


def _decode_sse(chunk: str) -> tuple[str, Dict[str, Any]]:
    event_name = ""
    payload: Dict[str, Any] = {}
    for line in chunk.splitlines():
        if line.startswith("event: "):
            event_name = line[7:]
        elif line.startswith("data: "):
            payload = json.loads(line[6:])
    return event_name, payload


def _contract(
    *,
    precise: bool = False,
    version: int = WORD_COUNT_POLICY_VERSION,
    target: int = TARGET,
) -> Dict[str, Any]:
    chapter_path = "chapters/第1章 未命名"
    fragment_path = f"{chapter_path}/001.md"
    return {
        "_type": "TurnContract",
        "_version": 1,
        "status": "ready",
        "intentFrame": {"primary": "story_generation"},
        "turnPlan": {
            "fragmentCount": 1,
            "chapterWordCountTarget": target,
            "operationType": "create_new",
            "chapterAction": "create_next_chapter",
            "targetChapterNumber": 1,
            "authoritativeChapterPath": chapter_path,
            "authoritativeFragmentPaths": [fragment_path],
            "chapterPlanValidation": {
                "_type": "ChapterPlanValidation",
                "_version": 1,
                "passed": True,
                "action": "create_next_chapter",
                "targetChapterNumber": 1,
                "authoritativeChapterPath": chapter_path,
                "authoritativeFragmentPaths": [fragment_path],
                "issues": [],
            },
            "wordCountPolicy": {
                "version": version,
                "scope": "chapter",
                "target": target,
                "retainedWordCount": 0,
                "remainingWordCount": target,
                "precision": {"enabled": precise},
            },
            "fragmentTargets": [
                {
                    "order": 1,
                    "path": fragment_path,
                    "writeMode": "replace",
                    "baselineWordCount": 0,
                }
            ],
        },
    }


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_gate_accepts_a_chapter_scoped_current_word_count_contract(tmp_path: Path) -> None:
    gate = routes._bounded_story_generation_gate(tmp_path, _contract())

    assert gate["enabled"] is True
    assert gate["precisionEnabled"] is False


def test_gate_reports_the_precision_switch_it_read(tmp_path: Path) -> None:
    gate = routes._bounded_story_generation_gate(tmp_path, _contract(precise=True))

    assert gate["enabled"] is True
    assert gate["precisionEnabled"] is True


def test_gate_closes_execution_when_the_published_chapter_plan_failed(
    tmp_path: Path,
) -> None:
    contract = _contract()
    contract["turnPlan"]["chapterPlanValidation"].update(
        {"passed": False, "issues": ["chapter_directory_already_exists"]}
    )

    gate = routes._bounded_story_generation_gate(tmp_path, contract)

    assert gate["enabled"] is False
    assert gate["terminal"] is True
    assert gate["reason"] == "chapter_plan_validation_failed"
    assert gate["error"]["type"] == "ChapterPlanValidationFailed"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda c: c["intentFrame"].update({"primary": "chat"}), "not_story_generation"),
        (
            lambda c: c["turnPlan"]["wordCountPolicy"].update({"version": 2}),
            "legacy_word_count_policy",
        ),
        (
            lambda c: c["turnPlan"]["wordCountPolicy"].update({"scope": "fragment"}),
            "not_chapter_scoped",
        ),
        (
            lambda c: c["turnPlan"].update({"operationType": "modify_existing"}),
            "operation_not_create_new",
        ),
        (
            lambda c: c["turnPlan"].update({"operationType": "inquiry"}),
            "operation_not_create_new",
        ),
        (lambda c: c["turnPlan"].update({"fragmentTargets": []}), "no_fragment_targets"),
        (
            lambda c: c["turnPlan"].update({"authoritativeChapterPath": ""}),
            "no_authoritative_chapter_path",
        ),
        (
            lambda c: c["turnPlan"].pop("chapterPlanValidation"),
            "chapter_plan_validation_missing",
        ),
        (
            lambda c: c["turnPlan"]["chapterPlanValidation"].update(
                {"authoritativeChapterPath": "chapters/其他章节"}
            ),
            "chapter_plan_contract_mismatch",
        ),
    ],
)
def test_gate_declines_turns_the_bounded_path_must_not_take(
    tmp_path: Path,
    mutate: Any,
    reason: str,
) -> None:
    contract = _contract()
    mutate(contract)

    gate = routes._bounded_story_generation_gate(tmp_path, contract)

    assert gate["enabled"] is False
    assert gate["reason"] == reason


def test_gate_yields_to_an_explicit_semantic_budget_request(tmp_path: Path) -> None:
    # Both paths replace the Agent loop; only one may claim a turn.
    contract = _contract()
    contract["turnPlan"]["generationControl"] = {"strategy": SEMANTIC_BUDGET_STRATEGY}

    gate = routes._bounded_story_generation_gate(tmp_path, contract)

    assert gate == {"enabled": False, "reason": "semantic_budget_requested"}


def test_gate_respects_the_feature_flag(tmp_path: Path) -> None:
    flags = tmp_path / ".storydex" / "config"
    flags.mkdir(parents=True, exist_ok=True)
    (flags / "feature-flags.json").write_text(
        json.dumps({"BOUNDED_STORY_GENERATION_ENABLED": False}),
        encoding="utf-8",
    )

    gate = routes._bounded_story_generation_gate(tmp_path, _contract())

    assert gate == {"enabled": False, "reason": "feature_flag_disabled"}


def test_bounded_execution_reports_structured_elastic_draft_rejection_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    flags = tmp_path / ".storydex" / "config"
    flags.mkdir(parents=True, exist_ok=True)
    (flags / "feature-flags.json").write_text(
        json.dumps({"ELASTIC_STORY_MANUSCRIPT_ENABLED": True}),
        encoding="utf-8",
    )

    class _ProviderBoundary:
        provider_retries = 0
        last_cap_applied = False
        last_completion_tokens = 100
        last_usage = {"outputTokens": 100, "source": "provider_response"}

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def complete_tool_call(self, **_kwargs: Any) -> Dict[str, Any]:
            return {
                "version": 2,
                "canonicalText": "完整正文。",
                "compactReplacements": [],
                "expansionModules": [],
                "endingHook": "完整正文。",
            }

    monkeypatch.setattr(routes, "CoomiStoryGenerationAdapter", _ProviderBoundary)

    outcome = asyncio.run(
        routes._execute_bounded_story_generation(
            prompt="写下一章",
            trace_id="elastic-rejection-reason",
            active_file="",
            workspace_root=tmp_path,
            turn_contract=_contract(),
            event_sink=lambda _event, _payload: None,
        )
    )

    assert outcome["error"] == {
        "type": "StoryDraftGenerationFailed",
        "causeType": "ElasticManuscriptRejected",
        "reason": "elastic_draft_version_mismatch",
    }


def _tier_contract(tier: str = "short") -> Dict[str, Any]:
    contract = _contract()
    plan = contract["turnPlan"]
    plan.pop("chapterWordCountTarget", None)
    plan["chapterLengthTier"] = tier
    plan["wordCountPolicy"] = {
        "version": WORD_COUNT_POLICY_VERSION,
        "mode": "tier",
        "scope": "candidate",
        "tier": tier,
        "promptVersion": "story_length_tier_v1",
        "preferredMinimum": 1000,
        "preferredMaximum": 3000,
        "hardMinimum": 700,
        "runtimeSafetyMaximum": 4000,
        "retainedWordCount": 0,
        "maximumProseCalls": 1,
        "retryOnLengthMiss": False,
        "precision": {"enabled": False, "maximumRevisionCalls": 0},
        "asymmetric": {"enabled": False, "maximumSecondDrafts": 0},
    }
    return contract


def test_gate_accepts_a_candidate_scoped_tier_contract(tmp_path: Path) -> None:
    gate = routes._bounded_story_generation_gate(tmp_path, _tier_contract())

    assert gate["enabled"] is True
    assert gate["chapterLengthTier"] == "short"
    assert gate["precisionEnabled"] is False


def test_bounded_execution_reports_canonical_quality_issue_codes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    flags = tmp_path / ".storydex" / "config"
    flags.mkdir(parents=True, exist_ok=True)
    (flags / "feature-flags.json").write_text(
        json.dumps({"ELASTIC_STORY_MANUSCRIPT_ENABLED": True}),
        encoding="utf-8",
    )

    class _ProviderBoundary:
        provider_retries = 0
        last_cap_applied = False
        last_completion_tokens = 100
        last_usage = {"outputTokens": 100, "source": "provider_response"}

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def complete_tool_call(self, **_kwargs: Any) -> Dict[str, Any]:
            return {
                "version": 1,
                "canonicalText": "这一段正文没有句末标点",
                "compactReplacements": [],
                "expansionModules": [],
                "endingHook": "这一段正文没有句末标点",
            }

    monkeypatch.setattr(routes, "CoomiStoryGenerationAdapter", _ProviderBoundary)

    outcome = asyncio.run(
        routes._execute_bounded_story_generation(
            prompt="写下一章",
            trace_id="elastic-quality-issues",
            active_file="",
            workspace_root=tmp_path,
            turn_contract=_contract(),
            event_sink=lambda _event, _payload: None,
        )
    )

    assert outcome["error"] == {
        "type": "StoryDraftGenerationFailed",
        "causeType": "ElasticManuscriptRejected",
        "reason": "canonical_quality_rejected",
        "issues": ["incomplete_ending"],
    }


# --------------------------------------------------------------------------
# The worker branch
# --------------------------------------------------------------------------


class _Handle:
    def __init__(self) -> None:
        self.is_cancelled = False
        self.cancel_reason = ""
        self.finalize_calls = 0

    def cancel(self, reason: str) -> bool:
        self.is_cancelled = True
        self.cancel_reason = reason
        return True

    async def finalize(self, observation: Any, context: Any) -> None:
        self.finalize_calls += 1
        status = (
            "failed"
            if observation.error_message
            else "cancelled"
            if observation.cancelled
            else "completed"
        )
        context.on_git_payload(context.finish_git())
        context.on_terminal(status, observation.error_message)
        payload = context.build_payload(status, observation.error_message, False, {})
        if isinstance(payload.get("record"), dict):
            context.persist_trace(payload["record"])


class _Runtime:
    def __init__(self, *, allow_stream: bool) -> None:
        self.stream_calls = 0
        self._allow_stream = allow_stream

    def get_status(self, *, workspace_root: Path) -> Dict[str, Any]:
        del workspace_root
        return {"providerId": "test-provider", "model": "test-model"}

    async def stream_events(self, **_kwargs: Any):
        self.stream_calls += 1
        if not self._allow_stream:
            raise AssertionError("the Agent tool loop must not run for a bounded turn")
        yield "AgentStarted", {
            "_type": "AgentStarted",
            "llmProvider": "test-provider",
            "llmModel": "test-model",
        }
        yield "TextChunk", {"_type": "TextChunk", "content": "legacy reply"}
        yield "AgentCompleted", {"_type": "AgentCompleted", "total_tokens": 1}


class _Calibration:
    def __init__(self) -> None:
        self.chapter_calls: List[Dict[str, Any]] = []
        self.paragraph_calls: List[Dict[str, Any]] = []

    def record_generation_result(self, _root: Path, **kwargs: Any) -> bool:
        self.chapter_calls.append(kwargs)
        return True

    def record_paragraph_generation_result(self, _root: Path, **kwargs: Any) -> bool:
        self.paragraph_calls.append(kwargs)
        return True


class _TierCalibration:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def record_sample(self, _root: Path, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return True


class _Git:
    def finish_turn(self, _snapshot: AgentGitSnapshot, **_kwargs: Any) -> Dict[str, Any]:
        return {"_type": "GitAutoCommit", "status": "info", "created": False}


def _bounded_result(
    *,
    precise: bool,
    draft: int,
    final: int | None = None,
    committed: bool = True,
) -> Dict[str, Any]:
    revised = final is not None and int(final) != int(draft)
    ledger = StoryCallAccounting()
    ledger.record_logical_call(STORY_INITIAL_GENERATION_PURPOSE)
    ledger.record_provider_attempt(STORY_INITIAL_GENERATION_PURPOSE)
    if revised:
        ledger.record_logical_call(STORY_LENGTH_REVISION_PURPOSE)
        ledger.record_provider_attempt(STORY_LENGTH_REVISION_PURPOSE)
    return {
        "_type": "StoryGenerationPipelineResult",
        "committed": committed,
        "precisionEnabled": precise,
        "draftWordCount": draft,
        "revisionWordCount": final if revised else None,
        "selection": {
            "source": CANDIDATE_SOURCE_REVISION if revised else CANDIDATE_SOURCE_DRAFT,
            "finalWordCount": int(final if final is not None else draft),
            "draftWordCount": draft,
            "precisionAchieved": revised,
            "reason": SELECTION_PRECISION_ACHIEVED if revised else SELECTION_PRECISION_DISABLED,
        },
        "stagedCandidates": {} if committed else {"initial": ".storydex/temp/initial.json"},
        "callAccounting": ledger.payload(),
        "contractViolations": ledger.contract_violations(precision_enabled=precise),
    }


def _collect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    contract: Dict[str, Any],
    outcome: Dict[str, Any] | None = None,
    bounded_enabled: bool = True,
    validation: Dict[str, Any] | None = None,
    cancel_after_first_event: bool = False,
    cancel_after_commit_started: bool = False,
    use_real_gate: bool = False,
    runtime_chapter_numbers: List[int] | None = None,
    runtime_retained_word_count: int = 0,
) -> Dict[str, Any]:
    runtime = _Runtime(allow_stream=not bounded_enabled)
    calibration = _Calibration()
    tier_calibration = _TierCalibration()
    handle = _Handle()
    execute_calls = 0
    cancellation_observed = False

    class _Project:
        def read_project_settings(self, _root: Path) -> Dict[str, Any]:
            return {"agentCommitPromptEnabled": False}

        def validate_story_generation_turn(
            self,
            _root: Path,
            _contract: Dict[str, Any],
        ) -> Dict[str, Any]:
            return {"applicable": False, "passed": True}

        def list_chapter_states(self, _root: Path) -> List[Any]:
            return [
                type("ChapterState", (), {"chapter_number": number})()
                for number in list(runtime_chapter_numbers or [])
            ]

        def count_chapter_story_words(self, _root: Path, _path: str) -> int:
            return runtime_retained_word_count

    async def fake_execute(**kwargs: Any) -> Dict[str, Any]:
        nonlocal execute_calls, cancellation_observed
        execute_calls += 1
        kwargs["event_sink"](
            "StoryDraftMeasured",
            {"_type": "StoryDraftMeasured", "draftWordCount": 2300},
        )
        if cancel_after_first_event:
            try:
                await asyncio.Future()
            finally:
                cancellation_observed = True
        if cancel_after_commit_started:
            commit_state = kwargs.get("commit_state")
            if isinstance(commit_state, dict):
                commit_state["started"] = True
            kwargs["event_sink"](
                "StoryCommitStarted",
                {"_type": "StoryCommitStarted"},
            )
            await asyncio.sleep(0.2)
            (tmp_path / "commit-marker.txt").write_text("committed", encoding="utf-8")
            if isinstance(commit_state, dict):
                commit_state["finished"] = True
        return dict(outcome or {})

    if not use_real_gate:
        monkeypatch.setattr(
            routes,
            "_bounded_story_generation_gate",
            lambda *_args: {
                "enabled": bounded_enabled,
                "reason": "enabled" if bounded_enabled else "feature_flag_disabled",
                "precisionEnabled": bool(
                    contract["turnPlan"]["wordCountPolicy"]["precision"]["enabled"]
                ),
            },
        )
    monkeypatch.setattr(routes, "_execute_bounded_story_generation", fake_execute)
    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: runtime)
    monkeypatch.setattr(routes, "story_project_service", _Project())
    monkeypatch.setattr(routes, "story_length_calibration_service", calibration)
    monkeypatch.setattr(
        routes,
        "story_length_tier_calibration_service",
        tier_calibration,
    )
    monkeypatch.setattr(routes, "agent_git_autocommit_service", _Git())
    monkeypatch.setattr(
        routes,
        "_reconcile_story_knowledge_projection",
        lambda _root: {"_type": "KnowledgeProjectionUpdated", "ok": True, "changedSourcePaths": []},
    )
    monkeypatch.setattr(routes, "_persist_execution_trace", lambda *_a, **_k: None)
    monkeypatch.setattr(
        routes,
        "_build_chat_payload",
        lambda **kwargs: {"record": {"status": kwargs["status"], "traceId": kwargs["trace_id"]}},
    )

    async def collect() -> List[tuple[str, Dict[str, Any]]]:
        packets: List[tuple[str, Dict[str, Any]]] = []
        async for chunk in routes._stream_coomi_sse_worker(
            prompt="请续写剧情",
            trace_id="trace",
            session_id="session",
            active_file="",
            workspace_root=tmp_path,
            story_generation={"chapterWordCountTarget": TARGET},
            turn_contract=contract,
            git_snapshot=AgentGitSnapshot(workspace_root=tmp_path, available=False),
            cancellation_token=routes._CancellationToken(),
            execution_handle=handle,
        ):
            packet = _decode_sse(chunk)
            packets.append(packet)
            if cancel_after_first_event and packet[0] == "StoryDraftMeasured":
                handle.cancel("test_cancel")
            if cancel_after_commit_started and packet[0] == "StoryCommitStarted":
                handle.cancel("test_cancel_after_commit")
        return packets

    packets = asyncio.run(collect())
    return {
        "packets": packets,
        "names": [name for name, _payload in packets],
        "runtime": runtime,
        "calibration": calibration,
        "tierCalibration": tier_calibration,
        "handle": handle,
        "executeCalls": execute_calls,
        "cancellationObserved": cancellation_observed,
    }


def _payload(collected: Dict[str, Any], name: str) -> Dict[str, Any]:
    for event_name, packet in collected["packets"]:
        if event_name == name:
            return packet
    raise AssertionError(f"{name} was never emitted: {collected['names']}")


def test_a_bounded_turn_replaces_the_agent_tool_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The regression this pins is a double write: the bounded path commits the
    # chapter itself, so a legacy loop running afterwards would write a second one.
    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=_contract(),
        outcome={
            "ok": True,
            "result": _bounded_result(precise=False, draft=2900),
            "validation": {"applicable": True, "passed": True, "generatedWordCount": 2900},
            "callAccounting": _bounded_result(precise=False, draft=2900)["callAccounting"],
            "error": {},
        },
    )

    assert collected["runtime"].stream_calls == 0
    assert collected["executeCalls"] == 1
    assert collected["names"].count("AgentCompleted") == 1
    assert "AgentError" not in collected["names"]
    assert collected["handle"].finalize_calls == 1


@pytest.mark.parametrize(
    "case",
    ["published_validation_failed", "missing_authoritative_path", "directory_competition"],
)
def test_invalid_version_three_plan_stops_before_every_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
) -> None:
    contract = _contract()
    if case == "published_validation_failed":
        contract["turnPlan"]["chapterPlanValidation"].update(
            {"passed": False, "issues": ["chapter_number_path_mismatch"]}
        )
    elif case == "missing_authoritative_path":
        contract["turnPlan"]["authoritativeChapterPath"] = ""
    else:
        (tmp_path / contract["turnPlan"]["authoritativeChapterPath"]).mkdir(
            parents=True,
            exist_ok=True,
        )

    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=contract,
        use_real_gate=True,
    )

    assert collected["executeCalls"] == 0
    assert collected["runtime"].stream_calls == 0
    assert "AgentStarted" not in collected["names"]
    error = _payload(collected, "AgentError")
    assert error["error_type"] == "ChapterPlanValidationFailed"
    assert error["details"]["providerCalls"] == 0


def test_reached_chapter_target_stops_before_every_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = _contract()
    turn_plan = contract["turnPlan"]
    turn_plan.update(
        {
            "operationType": "create_new",
            "chapterAction": "continue_current_chapter",
        }
    )
    turn_plan["chapterPlanValidation"].update(
        {"action": "continue_current_chapter"}
    )
    turn_plan["wordCountPolicy"].update(
        {"retainedWordCount": TARGET, "remainingWordCount": 0}
    )
    chapter_path = tmp_path / turn_plan["authoritativeChapterPath"]
    chapter_path.mkdir(parents=True)
    (chapter_path / "001.md").write_text("正文", encoding="utf-8")

    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=contract,
        use_real_gate=True,
        runtime_chapter_numbers=[1],
        runtime_retained_word_count=TARGET,
    )

    assert collected["executeCalls"] == 0
    assert collected["runtime"].stream_calls == 0
    assert "AgentStarted" not in collected["names"]
    error = _payload(collected, "AgentError")
    assert error["error_type"] == "ChapterWordCountTargetReached"
    assert "超写" in error["message"]
    assert "下一章" in error["message"]
    assert error["details"]["providerCalls"] == 0


def test_tier_state_change_does_not_publish_a_remaining_numeric_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = _tier_contract()
    plan = contract["turnPlan"]
    plan.update({"chapterAction": "continue_current_chapter"})
    plan["chapterPlanValidation"].update({"action": "continue_current_chapter"})
    plan["wordCountPolicy"]["retainedWordCount"] = 100
    chapter_path = tmp_path / plan["authoritativeChapterPath"]
    chapter_path.mkdir(parents=True)
    (chapter_path / "001.md").write_text("正文", encoding="utf-8")

    class _Project:
        def list_chapter_states(self, _root: Path) -> List[Any]:
            return [type("ChapterState", (), {"chapter_number": 1})()]

        def count_chapter_story_words(self, _root: Path, _path: str) -> int:
            return 200

    monkeypatch.setattr(routes, "story_project_service", _Project())
    gate = routes._bounded_story_generation_gate(tmp_path, contract)

    assert gate["reason"] == "chapter_word_count_state_changed"
    validation = gate["error"]["validation"]
    assert validation["retainedWordCount"] == 200
    assert "remainingWordCount" not in validation


def test_the_worker_forwards_the_pipeline_progress_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=_contract(),
        outcome={
            "ok": True,
            "result": _bounded_result(precise=False, draft=2900),
            "validation": {"applicable": True, "passed": True},
            "callAccounting": {},
            "error": {},
        },
    )

    assert "StoryDraftMeasured" in collected["names"]
    assert _payload(collected, "StoryDraftMeasured")["draftWordCount"] == 2300


def test_accounting_event_reports_one_call_when_precision_is_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _bounded_result(precise=False, draft=2200)
    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=_contract(),
        outcome={
            "ok": True,
            "result": result,
            "validation": {"applicable": True, "passed": True},
            "callAccounting": result["callAccounting"],
            "error": {},
        },
    )

    accounting = _payload(collected, "StoryCallAccounting")
    # 2200 字离目标很远，但精确控制关闭时这不是花第二次正文调用的理由。
    assert accounting["logicalStoryCalls"] == 1
    assert accounting["lengthRevisionCalls"] == 0
    assert accounting["preciseWordCountEnabled"] is False
    assert accounting["contractViolations"] == []


def test_accounting_event_reports_two_calls_for_a_revised_precise_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _bounded_result(precise=True, draft=2300, final=2950)
    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=_contract(precise=True),
        outcome={
            "ok": True,
            "result": result,
            "validation": {"applicable": True, "passed": True},
            "callAccounting": result["callAccounting"],
            "error": {},
        },
    )

    accounting = _payload(collected, "StoryCallAccounting")
    assert accounting["logicalStoryCalls"] == 2
    assert accounting["initialGenerationCalls"] == 1
    assert accounting["lengthRevisionCalls"] == 1
    assert accounting["preciseWordCountEnabled"] is True
    assert accounting["contractViolations"] == []


def test_the_recorded_ledger_wins_over_trace_derivation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The bounded turn emits one AgentStarted but spends two prose calls. A count
    # derived from the trace would report one and hide the revision entirely.
    result = _bounded_result(precise=True, draft=2300, final=2950)
    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=_contract(precise=True),
        outcome={
            "ok": True,
            "result": result,
            "validation": {"applicable": True, "passed": True},
            "callAccounting": result["callAccounting"],
            "error": {},
        },
    )
    trace = [
        {"event": name, "data": packet}
        for name, packet in collected["packets"]
    ]

    derived = routes.story_call_accounting_payload(
        [item for item in trace if item["event"] != "StoryCallAccounting"],
        turn_contract=_contract(precise=True),
    )
    recorded = routes.story_call_accounting_payload(
        trace,
        turn_contract=_contract(precise=True),
    )

    assert derived["logicalStoryCalls"] == 1
    assert recorded["logicalStoryCalls"] == 2
    assert recorded["lengthRevisionCalls"] == 1
    assert recorded["source"] == "recorded"
    assert recorded["contractViolations"] == []


def test_validation_packet_carries_the_bounded_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _bounded_result(precise=True, draft=2300, final=2950)
    result.update(
        {
            "lengthControlStrategy": "elastic_manuscript_v1",
            "canonicalWordCount": 2300,
            "normalBandPassed": True,
            "precisionAchieved": True,
            "selectedEditIds": ["compact-a", "repair-a"],
            "rejectedEditIds": ["compact-b"],
            "rejectedEditReasonCounts": {"anchor_not_unique": 1},
            "evaluatedCombinationCount": 8,
            "lengthFallbackReason": "repair_in_band",
            "generatedOverheadRatio": 1.12,
        }
    )
    result["selection"].update(
        {
            key: result[key]
            for key in (
                "lengthControlStrategy",
                "canonicalWordCount",
                "normalBandPassed",
                "precisionAchieved",
                "selectedEditIds",
                "rejectedEditIds",
                "rejectedEditReasonCounts",
                "evaluatedCombinationCount",
                "lengthFallbackReason",
                "generatedOverheadRatio",
            )
        }
    )
    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=_contract(precise=True),
        outcome={
            "ok": True,
            "result": result,
            "validation": {
                "applicable": True,
                "passed": True,
                "wordCountScope": "chapter",
                "generatedWordCount": 2950,
            },
            "callAccounting": result["callAccounting"],
            "error": {},
        },
    )

    validation = _payload(collected, "StoryGenerationValidation")
    assert validation["initialWordCount"] == 2300
    assert validation["finalWordCount"] == 2950
    assert validation["revisionApplied"] is True
    assert validation["precisionAchieved"] is True
    assert validation["preciseWordCountEnabled"] is True
    assert validation["lengthControlStrategy"] == "elastic_manuscript_v1"
    assert validation["canonicalWordCount"] == 2300
    assert validation["normalBandPassed"] is True
    assert validation["selectedEditIds"] == ["compact-a", "repair-a"]
    assert validation["rejectedEditIds"] == ["compact-b"]
    assert validation["rejectedEditReasonCounts"] == {"anchor_not_unique": 1}
    assert validation["evaluatedCombinationCount"] == 8
    assert validation["lengthFallbackReason"] == "repair_in_band"
    assert validation["generatedOverheadRatio"] == pytest.approx(1.12)
    # 有界路径的写入走 pipeline 的单次 apply，不经过 Agent 工具调用，
    # 所以工具信号在这里如实报告为已满足，而不是等一个永不到来的 ToolDone。
    assert validation["writeToolApplied"] is True


def test_asymmetric_route_exposes_selected_second_draft_and_length_gates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = _contract()
    contract["turnPlan"]["wordCountPolicy"]["asymmetric"] = {
        "enabled": True,
        "hardMinimum": 2550,
        "softMaximum": 3900,
        "runtimeSafetyMaximum": 6000,
    }
    ledger = StoryCallAccounting()
    for purpose in (
        STORY_INITIAL_GENERATION_PURPOSE,
        STORY_SECOND_DRAFT_PURPOSE,
    ):
        ledger.record_logical_call(purpose)
        ledger.record_provider_attempt(purpose)
    result = {
        "_type": "StoryGenerationPipelineResult",
        "committed": True,
        "precisionEnabled": False,
        "asymmetricLengthEnabled": True,
        "draftWordCount": 2549,
        "secondDraftWordCount": 3901,
        "selection": {
            "source": "second_draft",
            "reason": "second_draft_selected",
            "finalWordCount": 3901,
            "secondDraftWordCount": 3901,
            "asymmetricLengthLoss": 901,
            "secondDraftStatus": {
                "hardMinimumPassed": True,
                "aboveSoftMaximum": True,
                "runtimeSafetyExceeded": False,
                "runtimeSafetyMaximum": 6000,
            },
        },
        "callAccounting": ledger.payload(),
        "contractViolations": ledger.contract_violations(
            precision_enabled=False,
            asymmetric_enabled=True,
        ),
    }
    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=contract,
        outcome={
            "ok": True,
            "result": result,
            "validation": {
                "applicable": True,
                "passed": True,
                "wordCountScope": "chapter",
                "generatedWordCount": 3901,
            },
            "callAccounting": result["callAccounting"],
            "error": {},
        },
    )

    validation = _payload(collected, "StoryGenerationValidation")
    assert validation["asymmetricLengthEnabled"] is True
    assert validation["hardMinimumPassed"] is True
    assert validation["aboveSoftMaximum"] is True
    assert validation["runtimeSafetyExceeded"] is False
    assert validation["runtimeSafetyMaximum"] == 6000
    assert validation["secondDraftWordCount"] == 3901
    assert validation["secondDraftApplied"] is True
    assert validation["asymmetricLengthLoss"] == 901
    assert validation["secondDraftCalls"] == 1

    accounting = _payload(collected, "StoryCallAccounting")
    assert accounting["asymmetricLengthEnabled"] is True
    assert accounting["secondDraftCalls"] == 1

    recorded = routes.story_call_accounting_payload(
        [
            {"event": name, "data": packet}
            for name, packet in collected["packets"]
        ],
        turn_contract=contract,
    )
    assert recorded["source"] == "recorded"
    assert recorded["asymmetricLengthEnabled"] is True
    assert recorded["secondDraftCalls"] == 1


def test_validation_packet_reports_a_committed_short_draft_as_a_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _bounded_result(precise=False, draft=1900)
    result.update(
        {
            "lengthControlStrategy": "elastic_manuscript_v1",
            "canonicalWordCount": 1900,
            "normalBandPassed": False,
            "precisionAchieved": None,
            "selectedEditIds": [],
            "lengthFallbackReason": "no_valid_edits",
            "generatedOverheadRatio": None,
        }
    )
    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=_contract(),
        outcome={
            "ok": True,
            "result": result,
            "validation": {
                "applicable": True,
                "passed": True,
                "status": "warning",
                "wordCountScope": "chapter",
                "generatedWordCount": 1900,
                "belowBudget": True,
                "overBudget": False,
                "message": STORY_UNDER_BUDGET_KEEP_MESSAGE,
            },
            "callAccounting": result["callAccounting"],
            "error": {},
        },
    )

    validation = _payload(collected, "StoryGenerationValidation")
    assert validation["passed"] is True
    assert validation["belowBudget"] is True
    assert validation["status"] == "warning"
    assert validation["message"] == STORY_UNDER_BUDGET_KEEP_MESSAGE
    assert validation["normalBandPassed"] is False
    assert validation["precisionAchieved"] is None
    assert validation["lengthFallbackReason"] == "no_valid_edits"


def test_tier_miss_keeps_one_completion_message_and_one_prose_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _bounded_result(precise=False, draft=3500)
    result.update(
        {
            "chapterLengthTier": "short",
            "tierHit": False,
            "tierDeviation": "above_preferred",
            "retainedWordCount": 2600,
            "resultingWordCount": 6100,
        }
    )
    result["selection"].update(
        {
            "chapterLengthTier": "short",
            "tierHit": False,
            "tierDeviation": "above_preferred",
            "draftQualityPassed": True,
            "draftValidation": {"passed": True, "structurePassed": True},
        }
    )
    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=_tier_contract(),
        outcome={
            "ok": True,
            "result": result,
            "validation": {
                "applicable": True,
                "passed": True,
                "wordCountScope": "candidate",
                "actualWordCount": 3500,
                "generatedWordCount": 3500,
                "retainedWordCount": 2600,
                "resultingWordCount": 6100,
                "structurePassed": True,
                "belowBudget": False,
                "overBudget": True,
                "chapterLengthTier": "short",
                "tierHit": False,
                "tierDeviation": "above_preferred",
                "message": STORY_OVER_BUDGET_KEEP_MESSAGE,
            },
            "callAccounting": result["callAccounting"],
            "error": {},
        },
    )

    validation = _payload(collected, "StoryGenerationValidation")
    assert validation["wordCountScope"] == "candidate"
    assert validation["actualWordCount"] == 3500
    assert validation["generatedWordCount"] == 3500
    assert validation["retainedWordCount"] == 2600
    assert validation["resultingWordCount"] == 6100
    assert validation["tierHit"] is False
    assert validation["machineQualityPassed"] is True
    for key in (
        "chapterWordCountTarget",
        "targetWordCountMin",
        "targetWordCountMax",
        "acceptWordCountMin",
        "acceptWordCountMax",
    ):
        assert key not in validation
    reply = _payload(collected, "TextChunk")["content"]
    assert "本次续写 3500 字" in reply
    assert "短档未命中，正文按原稿保留" in reply
    assert STORY_OVER_BUDGET_KEEP_MESSAGE not in reply
    completed = _payload(collected, "AgentCompleted")
    assert completed["message"] == reply
    assert completed["chapterLengthTier"] == "short"
    assert completed["tierHit"] is False
    assert completed["wordCountScope"] == "candidate"
    assert completed["actualWordCount"] == 3500
    assert completed["retainedWordCount"] == 2600
    assert completed["resultingWordCount"] == 6100
    accounting = _payload(collected, "StoryCallAccounting")
    assert accounting["logicalStoryCalls"] == 1
    assert accounting["lengthRevisionCalls"] == 0
    assert accounting["secondDraftCalls"] == 0
    assert len(collected["tierCalibration"].calls) == 1
    sample = collected["tierCalibration"].calls[0]
    assert sample["actual_word_count"] == 3500
    assert sample["word_count_scope"] == "candidate"
    assert sample["structure_passed"] is True
    assert sample["machine_quality_passed"] is True


def test_precision_miss_reports_write_success_and_the_precision_range(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _bounded_result(precise=True, draft=3707)
    result.update(
        {
            "lengthControlStrategy": "elastic_manuscript_v1",
            "canonicalWordCount": 3707,
            "normalBandPassed": True,
            "precisionAchieved": False,
            "selectedEditIds": [],
            "lengthFallbackReason": "repair_outside_band",
            "generatedOverheadRatio": None,
        }
    )
    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=_contract(precise=True),
        outcome={
            "ok": True,
            "result": result,
            "validation": {
                "applicable": True,
                "passed": True,
                "wordCountScope": "chapter",
                "generatedWordCount": 3707,
                "belowBudget": False,
                "overBudget": False,
            },
            "callAccounting": result["callAccounting"],
            "error": {},
        },
    )

    validation = _payload(collected, "StoryGenerationValidation")
    assert validation["passed"] is True
    assert validation["status"] == "warning"
    assert validation["normalBandPassed"] is True
    assert validation["precisionAchieved"] is False
    reply = _payload(collected, "TextChunk")["content"]
    assert "章节已写入" in reply
    assert "3707" in reply
    assert "未达到精确范围 2700-3300" in reply
    completed = _payload(collected, "AgentCompleted")
    assert completed["status"] == "completed"
    assert completed["lengthControlStrategy"] == "elastic_manuscript_v1"
    assert completed["precisionAchieved"] is False
    assert routes._status_for_event("StoryGenerationValidation", validation) == "warning"


def test_calibration_learns_from_the_draft_not_the_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _bounded_result(precise=True, draft=2300, final=2950)
    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=_contract(precise=True),
        outcome={
            "ok": True,
            "result": result,
            "validation": {
                "applicable": True,
                "passed": True,
                "wordCountScope": "chapter",
                "generatedWordCount": 2950,
            },
            "callAccounting": result["callAccounting"],
            "error": {},
        },
    )

    chapter_calls = collected["calibration"].chapter_calls
    assert len(chapter_calls) == 1
    sampled = chapter_calls[0]["validation"]
    # A reference tuned on the revised 2950 would teach the next draft that the
    # model already writes to target, and the drift would compound.
    assert sampled["generatedWordCount"] == 2300
    assert sampled["attemptKind"] == "initial"


def test_paragraph_calibration_samples_even_a_short_draft(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _bounded_result(precise=False, draft=2100)
    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=_contract(),
        outcome={
            "ok": True,
            "result": result,
            "validation": {
                "applicable": True,
                "passed": True,
                "wordCountScope": "chapter",
                "generatedWordCount": 2100,
            },
            "callAccounting": result["callAccounting"],
            "error": {},
        },
    )

    assert len(collected["calibration"].paragraph_calls) == 1


def test_a_failed_write_reports_the_staged_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _bounded_result(precise=False, draft=2900, committed=False)
    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=_contract(),
        outcome={
            "ok": False,
            "result": result,
            "validation": {},
            "callAccounting": result["callAccounting"],
            "error": {"type": "StoryGenerationApplyRejected"},
        },
    )

    error = _payload(collected, "AgentError")
    assert error["error_type"] == "BoundedStoryGenerationFailed"
    # 暂存候选保留下来，恢复这一章不需要再花一次正文调用。
    assert error["details"]["stagedCandidates"] == {"initial": ".storydex/temp/initial.json"}
    assert "AgentCompleted" not in collected["names"]


def test_cancelling_mid_draft_writes_nothing_and_reports_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=_contract(),
        outcome={"ok": True, "result": {}, "validation": {}, "callAccounting": {}, "error": {}},
        cancel_after_first_event=True,
    )

    assert collected["cancellationObserved"] is True
    assert "AgentCancelled" in collected["names"]
    assert "StoryGenerationValidation" not in collected["names"]
    assert collected["calibration"].chapter_calls == []


def test_cancellation_after_commit_started_waits_and_reports_the_disk_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _bounded_result(precise=False, draft=2900)
    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=_contract(),
        outcome={
            "ok": True,
            "result": result,
            "validation": {"applicable": True, "passed": True},
            "callAccounting": result["callAccounting"],
            "error": {},
        },
        cancel_after_commit_started=True,
    )

    assert (tmp_path / "commit-marker.txt").read_text(encoding="utf-8") == "committed"
    assert "AgentCompleted" in collected["names"]
    assert "AgentCancelled" not in collected["names"]


def test_a_legacy_contract_still_runs_the_agent_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    collected = _collect(
        monkeypatch,
        tmp_path,
        contract=_contract(version=2),
        bounded_enabled=False,
    )

    assert collected["runtime"].stream_calls == 1
    assert collected["executeCalls"] == 0


# --------------------------------------------------------------------------
# The correction continuation must not survive for bounded contracts
# --------------------------------------------------------------------------


def test_a_version_three_contract_no_longer_appends_a_correction() -> None:
    # This is the append double-write: the first draft is already on disk and the
    # correction round writes a second chapter's worth of prose after it.
    assert routes._supports_correction_continuation(_contract()) is False


def test_a_legacy_contract_keeps_its_one_correction_round() -> None:
    assert routes._supports_correction_continuation(_contract(version=2)) is True


def test_a_missing_policy_is_treated_as_legacy() -> None:
    assert routes._supports_correction_continuation({}) is True
    assert routes._supports_correction_continuation(None) is True
