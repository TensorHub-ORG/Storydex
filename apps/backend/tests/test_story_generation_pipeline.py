"""W2-2/W2-4: the bounded pipeline's call contract and candidate selection.

Two properties are worth more than any single assertion here:

* a turn spends exactly one prose call unless precision is on *and* the draft
  missed the precision band, in which case it spends exactly two;
* having spent the second call is never a reason to keep a worse candidate.

The second property is the quality insurance of the whole mechanism, so the
rejection paths (bad quality, insufficient improvement, provider failure) get as
much coverage as the happy path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List

import pytest

from services.story_generation_pipeline import (
    CANDIDATE_SOURCE_DRAFT,
    CANDIDATE_SOURCE_REVISION,
    SELECTION_DRAFT_IN_BAND,
    SELECTION_DRAFT_KEPT,
    SELECTION_PRECISION_ACHIEVED,
    SELECTION_PRECISION_DISABLED,
    SELECTION_REVISION_REJECTED,
    SELECTION_REVISION_UNAVAILABLE,
    SELECTION_WIDE_RECOVERED,
    StoryGenerationPipeline,
    StoryGenerationPipelineError,
    get_story_generation_pipeline,
)
from services.story_project_service import (
    SINGLE_FILE_CHAPTER_TEMPLATE_ID,
    get_story_project_service,
)
from services.storydex_orchestration_service import get_storydex_orchestration_service

PROVIDER = "chy"
MODEL = "deepseek-v4-flash"
TARGET = 3000


@pytest.fixture(autouse=True)
def legacy_story_length_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORY_LENGTH_TIER_ENABLED", "0")


def _contract(root: Path, *, precise: bool, active_file: str = "") -> Dict[str, Any]:
    return get_storydex_orchestration_service().build_turn_contract(
        root,
        prompt="请续写剧情",
        active_file=active_file,
        story_generation={
            "chapterTemplateId": SINGLE_FILE_CHAPTER_TEMPLATE_ID,
            "chapterWordCountTarget": TARGET,
            "preciseWordCountEnabled": precise,
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
        provider=PROVIDER,
        model=MODEL,
    )


def _prose(count: int) -> Dict[str, Any]:
    return {"fragments": [{"text": "甲" * count}]}


def _run(
    root: Path,
    *,
    precise: bool,
    draft: int,
    revision: Any = None,
    quality_passed: bool = True,
    trace_id: str = "trace-pipeline",
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Run one turn and report the revision requests the pipeline issued."""

    pipeline = get_story_generation_pipeline()
    contract = _contract(root, precise=precise)
    requests: List[Dict[str, Any]] = []

    def revise(request: Dict[str, Any]) -> Any:
        requests.append(request)
        if isinstance(revision, Exception):
            raise revision
        if revision is None:
            return None
        payload = _prose(int(revision))
        payload["qualityPassed"] = quality_passed
        return payload

    result = asyncio.run(
        pipeline.run(
            root,
            trace_id=trace_id,
            turn_contract=contract,
            generate_draft=lambda: _prose(draft),
            revise=None if revision is None and not precise else revise,
        )
    )
    return result, requests


# --------------------------------------------------------------------------
# Call contract
# --------------------------------------------------------------------------


def test_precision_disabled_never_spends_a_second_call(tmp_path: Path) -> None:
    # 2200 字离 ±10% 很远，但精确控制关闭时这不是花第二次调用的理由。
    result, requests = _run(tmp_path, precise=False, draft=2200, revision=3000)

    assert requests == []
    assert result["callAccounting"]["logicalStoryCalls"] == 1
    assert result["callAccounting"]["lengthRevisionCalls"] == 0
    assert result["contractViolations"] == []
    assert result["selection"]["reason"] == SELECTION_PRECISION_DISABLED
    assert result["selection"]["source"] == CANDIDATE_SOURCE_DRAFT
    assert result["committed"] is True


def test_precision_enabled_draft_in_band_spends_no_revision(tmp_path: Path) -> None:
    # 2900 已在 2700-3300 精确带内，没有缺口可修，第二次调用不得发生。
    result, requests = _run(tmp_path, precise=True, draft=2900, revision=3000)

    assert requests == []
    assert result["callAccounting"]["logicalStoryCalls"] == 1
    assert result["contractViolations"] == []
    assert result["selection"]["reason"] == SELECTION_DRAFT_IN_BAND
    assert result["selection"]["precisionAchieved"] is True


def test_continuation_classifies_retained_prose_plus_this_turn_candidate(
    tmp_path: Path,
) -> None:
    initial = _contract(tmp_path, precise=False)
    target_path = initial["turnPlan"]["fragmentTargets"][0]["path"]
    chapter_file = tmp_path / target_path
    chapter_file.parent.mkdir(parents=True, exist_ok=True)
    chapter_file.write_text("甲" * 2600, encoding="utf-8")
    continuation = _contract(tmp_path, precise=True, active_file=target_path)
    revision_requests: List[Dict[str, Any]] = []

    result = asyncio.run(
        get_story_generation_pipeline().run(
            tmp_path,
            trace_id="trace-continuation-count",
            turn_contract=continuation,
            generate_draft=lambda: _prose(400),
            revise=lambda request: revision_requests.append(request) or _prose(3000),
        )
    )

    assert revision_requests == []
    assert result["retainedWordCount"] == 2600
    assert result["draftGeneratedWordCount"] == 400
    assert result["draftWordCount"] == 3000
    assert result["selection"]["finalGeneratedWordCount"] == 400
    assert result["selection"]["finalWordCount"] == 3000
    assert result["callAccounting"]["logicalStoryCalls"] == 1
    assert result["committed"] is True
    assert get_story_project_service().count_story_file_words(chapter_file) == 3000


def test_precision_enabled_short_draft_spends_exactly_two_calls(tmp_path: Path) -> None:
    result, requests = _run(tmp_path, precise=True, draft=2300, revision=2950)

    assert len(requests) == 1
    assert requests[0]["direction"] == "expand"
    assert requests[0]["draftWordCount"] == 2300
    assert requests[0]["target"] == TARGET
    accounting = result["callAccounting"]
    assert accounting["logicalStoryCalls"] == 2
    assert accounting["initialGenerationCalls"] == 1
    assert accounting["lengthRevisionCalls"] == 1
    assert accounting["transportRetries"] == 0
    assert result["contractViolations"] == []
    assert result["selection"]["reason"] == SELECTION_PRECISION_ACHIEVED
    assert result["selection"]["source"] == CANDIDATE_SOURCE_REVISION
    assert result["selection"]["finalWordCount"] == 2950


def test_a_long_draft_asks_for_compression(tmp_path: Path) -> None:
    result, requests = _run(tmp_path, precise=True, draft=3800, revision=3100)

    assert requests[0]["direction"] == "compress"
    assert result["selection"]["reason"] == SELECTION_PRECISION_ACHIEVED
    assert result["callAccounting"]["logicalStoryCalls"] == 2


def test_preflight_rejection_does_not_increment_provider_accounting(
    tmp_path: Path,
) -> None:
    pipeline = get_story_generation_pipeline()
    contract = _contract(tmp_path, precise=True)
    revision_requests: List[Dict[str, Any]] = []

    def revise(request: Dict[str, Any]) -> Dict[str, Any]:
        revision_requests.append(request)
        return {
            "fragments": [],
            "qualityPassed": False,
            "qualityIssues": ["revision_preflight_rejected"],
            "rejectedReason": "revision_preflight_rejected",
            "providerCallMade": False,
        }

    result = asyncio.run(
        pipeline.run(
            tmp_path,
            trace_id="trace-local-patch-corridor",
            turn_contract=contract,
            generate_draft=lambda: _prose(5000),
            revise=revise,
        )
    )

    assert len(revision_requests) == 1
    assert result["selection"]["reason"] == SELECTION_REVISION_UNAVAILABLE
    assert result["callAccounting"] == {
        "logicalStoryCalls": 1,
        "providerAttempts": 1,
        "transportRetries": 0,
        "initialGenerationCalls": 1,
        "lengthRevisionCalls": 0,
        "secondDraftCalls": 0,
        "nonProseCalls": {},
    }
    assert result["contractViolations"] == []


def test_failed_feedback_redraft_counts_one_revision_and_keeps_the_draft(
    tmp_path: Path,
) -> None:
    pipeline = get_story_generation_pipeline()
    contract = _contract(tmp_path, precise=True)

    result = asyncio.run(
        pipeline.run(
            tmp_path,
            trace_id="trace-feedback-redraft-rejected",
            turn_contract=contract,
            generate_draft=lambda: _prose(5000),
            revise=lambda _request: {
                **_prose(3400),
                "qualityPassed": False,
                "qualityIssues": ["redraft_outside_precision_band"],
                "strategy": "feedback_bounded_redraft",
            },
        )
    )

    assert result["revisionStrategy"] == "feedback_bounded_redraft"
    assert result["selection"]["reason"] == SELECTION_REVISION_REJECTED
    assert result["selection"]["source"] == CANDIDATE_SOURCE_DRAFT
    assert result["selection"]["finalWordCount"] == 5000
    assert result["callAccounting"]["logicalStoryCalls"] == 2
    assert result["callAccounting"]["lengthRevisionCalls"] == 1
    assert result["callAccounting"]["transportRetries"] == 0
    assert result["contractViolations"] == []


def test_successful_feedback_redraft_is_selected_with_exactly_two_calls(
    tmp_path: Path,
) -> None:
    pipeline = get_story_generation_pipeline()
    contract = _contract(tmp_path, precise=True)

    result = asyncio.run(
        pipeline.run(
            tmp_path,
            trace_id="trace-feedback-redraft-selected",
            turn_contract=contract,
            generate_draft=lambda: _prose(5000),
            revise=lambda _request: {
                **_prose(3000),
                "qualityPassed": True,
                "qualityIssues": [],
                "strategy": "feedback_bounded_redraft",
            },
        )
    )

    assert result["revisionStrategy"] == "feedback_bounded_redraft"
    assert result["selection"]["reason"] == SELECTION_PRECISION_ACHIEVED
    assert result["selection"]["source"] == CANDIDATE_SOURCE_REVISION
    assert result["selection"]["finalWordCount"] == 3000
    assert result["callAccounting"]["logicalStoryCalls"] == 2
    assert result["callAccounting"]["lengthRevisionCalls"] == 1
    assert result["contractViolations"] == []


# --------------------------------------------------------------------------
# Candidate selection (plan §7.5)
# --------------------------------------------------------------------------


def test_a_failed_quality_gate_keeps_the_draft(tmp_path: Path) -> None:
    # 候选正好落在精确带内，但质量门禁没过。花掉的调用不是接受它的理由。
    result, _ = _run(tmp_path, precise=True, draft=2300, revision=3000, quality_passed=False)

    assert result["selection"]["reason"] == SELECTION_REVISION_REJECTED
    assert result["selection"]["source"] == CANDIDATE_SOURCE_DRAFT
    assert result["selection"]["finalWordCount"] == 2300
    # 调用已经发生，账目必须照实记录，不能因为回退首稿就抹掉。
    assert result["callAccounting"]["lengthRevisionCalls"] == 1
    assert result["contractViolations"] == []


def test_a_revision_that_recovers_into_the_release_band_is_not_called_precise(
    tmp_path: Path,
) -> None:
    # 首稿 1400 在普通区间 2550-3900 之外；候选 2600 进入普通区间、偏差从
    # 1600 收到 400，改善超过一半，可以接受，但不得宣称达到精确带。
    result, _ = _run(tmp_path, precise=True, draft=1400, revision=2600)

    selection = result["selection"]
    assert selection["reason"] == SELECTION_WIDE_RECOVERED
    assert selection["source"] == CANDIDATE_SOURCE_REVISION
    assert selection["precisionAchieved"] is False
    assert selection["normalBandPassed"] is True


def test_a_wide_recovery_that_barely_improves_keeps_the_draft(tmp_path: Path) -> None:
    # 首稿 2000 偏差 1000，候选 2450 偏差 550，改善不到一半：花了调用也不接受。
    result, _ = _run(tmp_path, precise=True, draft=2000, revision=2450)

    assert result["selection"]["reason"] == SELECTION_DRAFT_KEPT
    assert result["selection"]["source"] == CANDIDATE_SOURCE_DRAFT
    assert result["selection"]["finalWordCount"] == 2000


def test_a_candidate_that_overshoots_past_the_band_keeps_the_draft(tmp_path: Path) -> None:
    # 修订不能跨过目标落到精确带另一侧之外（计划 §7.4 第 10 条）。
    result, _ = _run(tmp_path, precise=True, draft=2500, revision=4200)

    assert result["selection"]["reason"] == SELECTION_DRAFT_KEPT
    assert result["selection"]["finalWordCount"] == 2500


def test_selection_is_a_pure_function_of_the_two_counts() -> None:
    pipeline = StoryGenerationPipeline(get_story_project_service())

    def choose(draft: int, revision: int | None, *, quality: bool = True) -> str:
        return str(
            pipeline.select_candidate(
                target=TARGET,
                draft_word_count=draft,
                revision_word_count=revision,
                revision_quality_passed=quality,
                precision_enabled=True,
            )["reason"]
        )

    assert choose(3000, None) == SELECTION_DRAFT_IN_BAND
    assert choose(2699, None) == SELECTION_REVISION_UNAVAILABLE
    # 边界包含：2700 与 3300 都算进带。
    assert choose(2700, None) == SELECTION_DRAFT_IN_BAND
    assert choose(3300, None) == SELECTION_DRAFT_IN_BAND
    assert choose(2400, 2800) == SELECTION_PRECISION_ACHIEVED
    assert choose(2400, 2800, quality=False) == SELECTION_REVISION_REJECTED


# --------------------------------------------------------------------------
# Failure matrix (plan §8.2)
# --------------------------------------------------------------------------


def test_a_revision_provider_failure_keeps_the_draft_without_retrying(tmp_path: Path) -> None:
    result, requests = _run(
        tmp_path,
        precise=True,
        draft=2400,
        revision=TimeoutError("revision deadline exceeded"),
    )

    # 一次就是一次：超时不重试，首稿仍是可提交的一章。
    assert len(requests) == 1
    assert result["revisionError"] == "TimeoutError"
    assert result["selection"]["reason"] == SELECTION_REVISION_UNAVAILABLE
    assert result["selection"]["source"] == CANDIDATE_SOURCE_DRAFT
    assert result["committed"] is True
    assert result["callAccounting"]["transportRetries"] == 0
    assert result["contractViolations"] == []


def test_an_empty_revision_response_keeps_the_draft(tmp_path: Path) -> None:
    pipeline = get_story_generation_pipeline()
    contract = _contract(tmp_path, precise=True)

    result = asyncio.run(
        pipeline.run(
            tmp_path,
            trace_id="trace-empty-revision",
            turn_contract=contract,
            generate_draft=lambda: _prose(2400),
            revise=lambda _request: {"fragments": []},
        )
    )

    assert result["selection"]["reason"] == SELECTION_REVISION_UNAVAILABLE
    assert result["revisionWordCount"] is None
    assert result["committed"] is True


def test_a_draft_that_returns_nothing_is_an_error_not_an_empty_chapter(tmp_path: Path) -> None:
    pipeline = get_story_generation_pipeline()
    contract = _contract(tmp_path, precise=False)

    with pytest.raises(StoryGenerationPipelineError):
        asyncio.run(
            pipeline.run(
                tmp_path,
                trace_id="trace-no-draft",
                turn_contract=contract,
                generate_draft=lambda: None,
            )
        )


# --------------------------------------------------------------------------
# One write only (plan §8.1)
# --------------------------------------------------------------------------


def test_only_the_selected_candidate_reaches_the_chapter_file(tmp_path: Path) -> None:
    service = get_story_project_service()
    pipeline = get_story_generation_pipeline()
    contract = _contract(tmp_path, precise=True)
    target_path = contract["turnPlan"]["fragmentTargets"][0]["path"]
    writes: List[int] = []
    original = service.apply_story_generation_increment

    def counting_apply(root: Path, payload: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        writes.append(sum(len(str(item.get("text") or "")) for item in payload["fragments"]))
        return original(root, payload, **kwargs)

    service.apply_story_generation_increment = counting_apply  # type: ignore[method-assign]
    try:
        result = asyncio.run(
            pipeline.run(
                tmp_path,
                trace_id="trace-single-write",
                turn_contract=contract,
                generate_draft=lambda: _prose(2300),
                revise=lambda _request: _prose(2950),
            )
        )
    finally:
        service.apply_story_generation_increment = original  # type: ignore[method-assign]

    # 首稿从未落盘：文件只被写过一次，且写的是选中的修订稿。
    assert writes == [2950]
    assert result["committed"] is True
    assert service.count_story_file_words(tmp_path / target_path) == 2950


def test_a_structurally_valid_draft_commits_even_below_the_release_band(
    tmp_path: Path,
) -> None:
    pipeline = get_story_generation_pipeline()
    contract = _contract(tmp_path, precise=False)
    target_path = contract["turnPlan"]["fragmentTargets"][0]["path"]

    result = asyncio.run(
        pipeline.run(
            tmp_path,
            trace_id="trace-recovery",
            turn_contract=contract,
            generate_draft=lambda: _prose(1900),
        )
    )

    # W2 separates structure from length: a usable draft is committed once and
    # reported as short instead of being discarded merely for missing +/-30%.
    assert result["committed"] is True
    assert result["draftWordCount"] == 1900
    assert result["selection"]["normalBandPassed"] is False
    assert result["stagedCandidates"] == {}
    assert get_story_project_service().count_story_file_words(tmp_path / target_path) == 1900
    validation = get_story_project_service().validate_story_generation_turn(
        tmp_path,
        contract,
    )
    assert validation["passed"] is True
    assert validation["structurePassed"] is True
    assert validation["belowBudget"] is True
