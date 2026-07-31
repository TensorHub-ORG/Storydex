"""W2-0: freeze the current story generation baseline before changing it.

Every gap recorded here was reproduced against the tree, not inferred from the
plan. Tests that describe behaviour W2-2..W2-4 will change are marked
``xfail(strict=True)``: they keep the suite honest today and turn red the moment
the gap closes, which is the signal to promote them to plain assertions rather
than leave a stale expectation in place.

Four gaps are frozen:

1. call accounting has no notion of a prose call budget, so nothing can prove a
   turn spent one logical call;
2. calibration only samples accepted results, so a short draft is invisible
   until a correction has already inflated it;
3. ``create_next_chapter`` for a numbered chapter lands inside the previous
   chapter's directory;
4. a rejected draft is discarded and its correction appends a full chapter on
   top of the previous one.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from api import routes_agent
from services.story_chapter_action_service import (
    CHAPTER_ACTION_CREATE_SPECIFIC_CHAPTER,
    CHAPTER_ACTIONS,
)
from services.story_call_accounting import (
    MAXIMUM_LENGTH_REVISION_CALLS,
    STORY_INITIAL_GENERATION_PURPOSE,
    STORY_LENGTH_REVISION_PURPOSE,
    StoryCallAccounting,
    is_story_prose_purpose,
)
from services.story_generation_pipeline import get_story_generation_pipeline
from services.story_length_calibration_service import get_story_length_calibration_service
from services.story_project_service import (
    DEFAULT_CHAPTER_TEMPLATE_ID,
    SINGLE_FILE_CHAPTER_TEMPLATE_ID,
    get_story_project_service,
)
from services.story_word_count_service import STORY_UNDER_BUDGET_KEEP_MESSAGE
from services.storydex_orchestration_service import get_storydex_orchestration_service

PROVIDER = "chy"
MODEL = "deepseek-v4-flash"


@pytest.fixture(autouse=True)
def _disable_tier_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORY_LENGTH_TIER_ENABLED", "0")


def _contract(
    root: Path,
    *,
    prompt: str = "请续写剧情",
    active_file: str = "",
    template_id: str = DEFAULT_CHAPTER_TEMPLATE_ID,
    **story_generation: Any,
) -> Dict[str, Any]:
    options: Dict[str, Any] = {"chapterTemplateId": template_id}
    options.update(story_generation)
    return get_storydex_orchestration_service().build_turn_contract(
        root,
        prompt=prompt,
        active_file=active_file,
        story_generation=options,
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


def _targets(contract: Dict[str, Any]) -> list[Dict[str, Any]]:
    targets = contract["turnPlan"].get("fragmentTargets")
    return list(targets) if isinstance(targets, list) else []


# --------------------------------------------------------------------------
# Gap 1: the call budget has no authoritative counter.
# --------------------------------------------------------------------------


def test_prose_purposes_are_separated_from_other_provider_work() -> None:
    assert is_story_prose_purpose(STORY_INITIAL_GENERATION_PURPOSE) is True
    assert is_story_prose_purpose(STORY_LENGTH_REVISION_PURPOSE) is True
    # These calls already exist and must not be counted against the prose
    # budget, nor grow because precise word count was switched on.
    for purpose in ("intent", "memory_recall", "plan", "chat", "commit", "loop"):
        assert is_story_prose_purpose(purpose) is False


def test_normal_mode_turn_records_exactly_one_prose_call() -> None:
    accounting = StoryCallAccounting()
    accounting.record_logical_call("intent")
    accounting.record_provider_attempt("intent")
    accounting.record_logical_call(STORY_INITIAL_GENERATION_PURPOSE)
    accounting.record_provider_attempt(STORY_INITIAL_GENERATION_PURPOSE)

    assert accounting.contract_violations(precision_enabled=False) == []
    payload = accounting.payload()
    assert payload["logicalStoryCalls"] == 1
    assert payload["lengthRevisionCalls"] == 0
    # The intent call stays visible instead of being folded into the prose count.
    assert payload["nonProseCalls"] == {"intent": 1}


def test_call_accounting_transport_retries_do_not_inflate_the_logical_call_count() -> None:
    accounting = StoryCallAccounting()
    accounting.record_logical_call(STORY_INITIAL_GENERATION_PURPOSE)
    accounting.record_provider_attempt(STORY_INITIAL_GENERATION_PURPOSE)
    accounting.record_transport_retry(STORY_INITIAL_GENERATION_PURPOSE)
    accounting.record_provider_attempt(STORY_INITIAL_GENERATION_PURPOSE)

    assert accounting.contract_violations(precision_enabled=False) == []
    assert accounting.logical_story_calls == 1
    assert accounting.provider_attempts == 2
    assert accounting.transport_retries == 1


def test_precision_mode_allows_one_revision_and_no_more() -> None:
    accounting = StoryCallAccounting()
    accounting.record_logical_call(STORY_INITIAL_GENERATION_PURPOSE)
    accounting.record_provider_attempt(STORY_INITIAL_GENERATION_PURPOSE)
    accounting.record_logical_call(STORY_LENGTH_REVISION_PURPOSE)
    accounting.record_provider_attempt(STORY_LENGTH_REVISION_PURPOSE)
    assert accounting.contract_violations(precision_enabled=True) == []
    assert accounting.logical_story_calls == 2

    accounting.record_logical_call(STORY_LENGTH_REVISION_PURPOSE)
    violations = accounting.contract_violations(precision_enabled=True)
    assert violations
    assert any(str(MAXIMUM_LENGTH_REVISION_CALLS) in item for item in violations)


def test_revision_call_while_precision_disabled_is_a_violation() -> None:
    accounting = StoryCallAccounting()
    accounting.record_logical_call(STORY_INITIAL_GENERATION_PURPOSE)
    accounting.record_provider_attempt(STORY_INITIAL_GENERATION_PURPOSE)
    accounting.record_logical_call(STORY_LENGTH_REVISION_PURPOSE)
    accounting.record_provider_attempt(STORY_LENGTH_REVISION_PURPOSE)

    violations = accounting.contract_violations(precision_enabled=False)
    assert violations
    assert any("disabled" in item for item in violations)


def test_revision_transport_retry_is_a_violation() -> None:
    accounting = StoryCallAccounting()
    accounting.record_logical_call(STORY_INITIAL_GENERATION_PURPOSE)
    accounting.record_provider_attempt(STORY_INITIAL_GENERATION_PURPOSE)
    accounting.record_logical_call(STORY_LENGTH_REVISION_PURPOSE)
    accounting.record_provider_attempt(STORY_LENGTH_REVISION_PURPOSE)
    accounting.record_transport_retry(STORY_LENGTH_REVISION_PURPOSE)
    accounting.record_provider_attempt(STORY_LENGTH_REVISION_PURPOSE)

    violations = accounting.contract_violations(precision_enabled=True)
    assert violations
    assert any("transport retries" in item for item in violations)


def test_missing_draft_call_is_a_violation() -> None:
    # A turn that produced prose without a recorded draft call means the
    # accounting was bypassed, which must fail loudly rather than read as clean.
    assert StoryCallAccounting().contract_violations(precision_enabled=False)


def _trace(*names: str) -> list[Dict[str, Any]]:
    return [{"event": name, "data": {}} for name in names]


def test_agent_turn_reports_the_call_contract(tmp_path: Path) -> None:
    # A default turn spends one prose call, and the report says so rather than
    # leaving the audit to infer it from HTTP traffic.
    payload = routes_agent.story_call_accounting_payload(
        _trace("AgentStarted", "TextChunk", "ToolDone", "AgentCompleted"),
        turn_contract=_contract(tmp_path, chapterWordCountTarget=3000),
    )

    assert payload["logicalStoryCalls"] == 1
    assert payload["initialGenerationCalls"] == 1
    assert payload["lengthRevisionCalls"] == 0
    assert payload["providerAttempts"] == 1
    assert payload["transportRetries"] == 0
    assert payload["preciseWordCountEnabled"] is False
    assert payload["contractViolations"] == []


def test_transport_retries_do_not_inflate_the_logical_call_count() -> None:
    # One logical call that retried twice on a 429 is still one call against the
    # budget. Reporting three would make a healthy turn look over budget.
    payload = routes_agent.story_call_accounting_payload(
        _trace("AgentStarted", "ConnectionRetry", "ConnectionRetry", "AgentCompleted"),
    )

    assert payload["logicalStoryCalls"] == 1
    assert payload["transportRetries"] == 2
    assert payload["providerAttempts"] == 3
    assert payload["contractViolations"] == []


def test_a_second_prose_call_without_precision_is_reported_as_a_violation() -> None:
    # The inverse mistake is the one that hid the correction loop: a turn that
    # quietly spent a second prose call must not read as clean.
    payload = routes_agent.story_call_accounting_payload(
        _trace("AgentStarted", "AgentCompleted", "AgentStarted", "AgentCompleted"),
    )

    assert payload["logicalStoryCalls"] == 2
    assert payload["lengthRevisionCalls"] == 1
    assert any("disabled" in item for item in payload["contractViolations"])


# --------------------------------------------------------------------------
# Gap 2: calibration only learns from accepted results.
# --------------------------------------------------------------------------


def _calibration_samples(root: Path) -> list[Dict[str, Any]]:
    service = get_story_length_calibration_service()
    path = service.calibration_path(root)
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    buckets = payload.get("buckets") if isinstance(payload.get("buckets"), list) else []
    samples: list[Dict[str, Any]] = []
    for bucket in buckets:
        if isinstance(bucket, dict) and isinstance(bucket.get("samples"), list):
            samples.extend(item for item in bucket["samples"] if isinstance(item, dict))
    return samples


def test_a_short_draft_is_recorded_even_though_it_missed_the_band(tmp_path: Path) -> None:
    service = get_story_length_calibration_service()
    contract = _contract(tmp_path, chapterWordCountTarget=3000)
    # A structurally valid draft at 1900 characters. It fails the word-count
    # band, but that is the observation calibration exists to learn from, so
    # sampling is gated on structure alone.
    recorded = service.record_generation_result(
        tmp_path,
        turn_contract=contract,
        validation={
            "applicable": True,
            "passed": False,
            "structurePassed": True,
            "chapterWordCountTarget": 3000,
            "generatedWordCount": 1900,
        },
        provider=PROVIDER,
        model=MODEL,
    )

    assert recorded is True
    samples = _calibration_samples(tmp_path)
    assert [item["actualWordCount"] for item in samples] == [1900]
    assert samples[0]["normalBandPassed"] is False


def test_a_structurally_invalid_draft_is_still_not_recorded(tmp_path: Path) -> None:
    service = get_story_length_calibration_service()
    contract = _contract(tmp_path, chapterWordCountTarget=3000)
    # Structure stays a hard gate: a wrong path or fragment count means the
    # measured length describes something other than the planned chapter.
    recorded = service.record_generation_result(
        tmp_path,
        turn_contract=contract,
        validation={
            "applicable": True,
            "passed": False,
            "structurePassed": False,
            "chapterWordCountTarget": 3000,
            "generatedWordCount": 1900,
        },
        provider=PROVIDER,
        model=MODEL,
    )

    assert recorded is False
    assert _calibration_samples(tmp_path) == []


def test_samples_distinguish_a_draft_from_a_precision_revision(
    tmp_path: Path,
) -> None:
    service = get_story_length_calibration_service()
    contract = _contract(tmp_path, chapterWordCountTarget=3000)
    for attempt_kind, count in (("initial", 1900), ("precision_revision", 2900)):
        assert (
            service.record_generation_result(
                tmp_path,
                turn_contract=contract,
                validation={
                    "applicable": True,
                    "passed": True,
                    "structurePassed": True,
                    "chapterWordCountTarget": 3000,
                    "generatedWordCount": count,
                    "attemptKind": attempt_kind,
                },
                provider=PROVIDER,
                model=MODEL,
            )
            is True
        )

    samples = _calibration_samples(tmp_path)
    assert {item["attemptKind"] for item in samples} == {"initial", "precision_revision"}
    # The revised total is kept for reporting but must not be readable as the
    # model's unassisted output, which is what taught the old reference that the
    # model already writes to target.
    initial = [item for item in samples if item["attemptKind"] == "initial"]
    assert [item["actualWordCount"] for item in initial] == [1900]


def test_calibration_records_the_draft_before_any_revision(tmp_path: Path) -> None:
    service = get_story_length_calibration_service()
    contract = _contract(tmp_path, chapterWordCountTarget=3000)
    recorded = service.record_generation_result(
        tmp_path,
        turn_contract=contract,
        validation={
            "applicable": True,
            "passed": False,
            "chapterWordCountTarget": 3000,
            "generatedWordCount": 1900,
            "structurePassed": True,
            "attemptKind": "initial",
        },
        provider=PROVIDER,
        model=MODEL,
    )

    assert recorded is True
    samples = _calibration_samples(tmp_path)
    assert [item["actualWordCount"] for item in samples] == [1900]
    assert samples[0]["attemptKind"] == "initial"


# --------------------------------------------------------------------------
# Gap 3: a numbered next chapter lands in the previous chapter's directory.
# --------------------------------------------------------------------------


def test_first_chapter_planning_is_already_correct(tmp_path: Path) -> None:
    service = get_story_project_service()
    contract = _contract(tmp_path, prompt="开始写第一章", chapterWordCountTarget=3000)
    targets = _targets(contract)
    assert [item["path"] for item in targets] == ["chapters/第1章 未命名/001.md"]

    result = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "甲" * 2900}]},
        generation_contract=contract,
    )
    assert result["ok"] is True
    assert result["writtenPaths"] == ["chapters/第1章 未命名/001.md"]


def test_next_chapter_request_no_longer_writes_into_chapter_one(tmp_path: Path) -> None:
    """W2-2 closed this gap; the assertion now pins the fix.

    The frozen bug planned ``chapters/第1章 未命名/002.md`` for a request naming
    chapter two, because the chapter number never reached path planning.
    """

    service = get_story_project_service()
    first = _contract(tmp_path, prompt="开始写第一章", chapterWordCountTarget=3000)
    service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "甲" * 2900}]},
        generation_contract=first,
    )

    second = _contract(
        tmp_path,
        prompt="写第二章",
        active_file="chapters/第1章 未命名/001.md",
        chapterWordCountTarget=3000,
    )
    planned = [item["path"] for item in _targets(second)]
    assert len(planned) == 1
    assert Path(planned[0]).parent.name.startswith("第2章"), planned
    assert "第1章" not in planned[0]


def test_turn_plan_publishes_the_chapter_action_pre_gate(tmp_path: Path) -> None:
    turn_plan = _contract(tmp_path, chapterWordCountTarget=3000)["turnPlan"]
    # These keys are what makes a wrong target discoverable before the prose
    # call rather than after prose has been paid for.
    assert turn_plan["chapterAction"] in CHAPTER_ACTIONS
    assert turn_plan["authoritativeChapterPath"].startswith("chapters/")
    assert turn_plan["authoritativeFragmentPaths"] == [
        target["path"] for target in turn_plan["fragmentTargets"]
    ]
    assert turn_plan["chapterPlanValidation"]["passed"] is True


def test_next_chapter_request_plans_a_new_chapter_directory(tmp_path: Path) -> None:
    service = get_story_project_service()
    first = _contract(tmp_path, prompt="开始写第一章", chapterWordCountTarget=3000)
    service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "甲" * 2900}]},
        generation_contract=first,
    )

    second = _contract(
        tmp_path,
        prompt="写第二章",
        active_file="chapters/第1章 未命名/001.md",
        chapterWordCountTarget=3000,
    )
    turn_plan = second["turnPlan"]
    # "写第二章" names its chapter, so it is a specific creation rather than a
    # bare "next chapter". The distinction matters once chapters 1-3 exist and a
    # request asks for chapter 5.
    assert turn_plan["chapterAction"] == CHAPTER_ACTION_CREATE_SPECIFIC_CHAPTER
    assert turn_plan["targetChapterNumber"] == 2
    assert Path(turn_plan["authoritativeChapterPath"]).name.startswith("第2章")
    assert all(
        Path(path).parent.name.startswith("第2章")
        for path in turn_plan["authoritativeFragmentPaths"]
    )


# --------------------------------------------------------------------------
# W2 bounded write contract: structurally valid drafts are never lost to length.
# --------------------------------------------------------------------------


def test_a_structurally_valid_short_draft_is_written_with_a_warning(tmp_path: Path) -> None:
    service = get_story_project_service()
    contract = _contract(
        tmp_path,
        chapterWordCountTarget=3000,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
    )
    target_path = _targets(contract)[0]["path"]

    result = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "甲" * 1900}]},
        generation_contract=contract,
    )

    assert result["ok"] is True
    assert result["writtenPaths"] == [target_path]
    assert result["wordCountValidation"]["passed"] is True
    assert result["wordCountValidation"]["belowBudget"] is True
    assert result["wordCountValidation"]["status"] == "warning"
    assert result["wordCountValidation"]["message"] == STORY_UNDER_BUDGET_KEEP_MESSAGE
    assert service.count_story_file_words(tmp_path / target_path) == 1900
    assert not list((tmp_path / ".storydex" / "temp").glob("story-generation/**/initial.json"))


def test_append_is_judged_by_the_resulting_chapter_not_this_turn_alone(tmp_path: Path) -> None:
    service = get_story_project_service()
    first = _contract(
        tmp_path,
        chapterWordCountTarget=3000,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
    )
    target_path = _targets(first)[0]["path"]
    assert (
        service.apply_story_generation_increment(
            tmp_path,
            {"fragments": [{"text": "甲" * 2600}]},
            generation_contract=first,
        )["ok"]
        is True
    )

    continuation = _contract(
        tmp_path,
        chapterWordCountTarget=3000,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
        active_file=target_path,
    )
    assert _targets(continuation)[0]["writeMode"] == "append"
    assert _targets(continuation)[0]["baselineWordCount"] == 2600

    # 2600 已在盘上，补 500 字之后整章 3100 字，对着 3000 目标是一章正常长度的
    # 正文。旧门禁只看本轮新增的 500 字，把它判成"偏短"，从而逼模型再写一整章。
    topped_up = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "乙" * 500}]},
        generation_contract=continuation,
    )
    assert topped_up["ok"] is True
    validation = topped_up["wordCountValidation"]
    assert validation["generatedWordCount"] == 500
    assert validation["retainedWordCount"] == 2600
    assert validation["resultingWordCount"] == 3100
    assert validation["belowBudget"] is False
    assert validation["overBudget"] is False
    # 关键回归：整章停在一章的长度，不会堆成 5200 字的两章。
    assert service.count_story_file_words(tmp_path / target_path) == 3100


def test_append_that_leaves_the_chapter_short_is_committed_with_a_warning(tmp_path: Path) -> None:
    service = get_story_project_service()
    first = _contract(
        tmp_path,
        chapterWordCountTarget=3000,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
    )
    target_path = _targets(first)[0]["path"]
    assert (
        service.apply_story_generation_increment(
            tmp_path,
            {"fragments": [{"text": "甲" * 1200}]},
            generation_contract=first,
        )["ok"]
        is True
    )

    continuation = _contract(
        tmp_path,
        chapterWordCountTarget=3000,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
        active_file=target_path,
    )
    # 1200 + 300 = 1500 字，仍在 2100 下界以下；结构合法正文必须保留，
    # 字数状态通过 warning 暴露给作者，而不是丢弃本轮新增内容。
    short = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "乙" * 300}]},
        generation_contract=continuation,
    )
    assert short["ok"] is True
    assert short["wordCountValidation"]["passed"] is True
    assert short["wordCountValidation"]["status"] == "warning"
    assert short["wordCountValidation"]["resultingWordCount"] == 1500
    assert short["wordCountValidation"]["belowBudget"] is True
    assert service.count_story_file_words(tmp_path / target_path) == 1500


def test_short_draft_commits_without_leaving_staged_prose(tmp_path: Path) -> None:
    # 暂存目录用 .storydex/.agent/temp/，不是 .storydex/temp/：后者是用户自己的
    # 草稿空间，Agent 中间产物落在那里会被当成用户正文，也会被上下文检索回注。
    pipeline = get_story_generation_pipeline()
    contract = _contract(
        tmp_path,
        chapterWordCountTarget=3000,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
    )
    draft = {"fragments": [{"text": "甲" * 1900}]}

    result = asyncio.run(
        pipeline.run(
            tmp_path,
            trace_id="trace-staging",
            turn_contract=contract,
            generate_draft=lambda: draft,
        )
    )

    # 首稿偏短但结构完整：字数不达标不该丢掉本轮唯一一份可用正文。
    assert result["draftWordCount"] == 1900
    assert result["committed"] is True
    assert result["stagedCandidates"] == {}
    assert get_story_project_service().count_story_file_words(tmp_path / _targets(contract)[0]["path"]) == 1900
    staged = list(
        (tmp_path / ".storydex" / ".agent" / "temp").glob("story-generation/*/initial.json")
    )
    assert staged == []
    assert not (tmp_path / ".storydex" / "temp" / "story-generation").exists()


def test_a_committed_chapter_leaves_no_staged_prose(tmp_path: Path) -> None:
    # 提交成功后暂存正文必须清掉，否则同一章正文在磁盘上留两份。
    pipeline = get_story_generation_pipeline()
    contract = _contract(
        tmp_path,
        chapterWordCountTarget=3000,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
    )

    result = asyncio.run(
        pipeline.run(
            tmp_path,
            trace_id="trace-committed",
            turn_contract=contract,
            generate_draft=lambda: {"fragments": [{"text": "甲" * 2900}]},
        )
    )

    assert result["committed"] is True
    assert result["stagedCandidates"] == {}
    assert not list(
        (tmp_path / ".storydex" / ".agent" / "temp").glob("story-generation/**/*.json")
    )


def test_normal_mode_spends_exactly_one_prose_call(tmp_path: Path) -> None:
    # 精确控制默认关闭：正文模型只调用一次，且不得出现修订调用。
    pipeline = get_story_generation_pipeline()
    contract = _contract(
        tmp_path,
        chapterWordCountTarget=3000,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
    )
    assert contract["turnPlan"]["wordCountPolicy"]["precision"]["enabled"] is False
    revisions_attempted = 0

    def revise(_request: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal revisions_attempted
        revisions_attempted += 1
        return {"fragments": [{"text": "乙" * 3000}]}

    # 首稿 2200 字落在 ±10% 之外，但精确控制关闭时不得因此多调一次模型。
    result = asyncio.run(
        pipeline.run(
            tmp_path,
            trace_id="trace-normal",
            turn_contract=contract,
            generate_draft=lambda: {"fragments": [{"text": "甲" * 2200}]},
            revise=revise,
        )
    )

    assert revisions_attempted == 0
    assert result["committed"] is True
    assert result["callAccounting"]["logicalStoryCalls"] == 1
    assert result["callAccounting"]["initialGenerationCalls"] == 1
    assert result["callAccounting"]["lengthRevisionCalls"] == 0
    assert result["contractViolations"] == []
    assert result["selection"]["reason"] == "precision_disabled"
