import asyncio
import json
from pathlib import Path

import pytest

from services.story_project_service import (
    SINGLE_FILE_CHAPTER_TEMPLATE_ID,
    get_story_project_service,
)
from services.story_prose_quality import (
    clean_generated_text,
    extract_story_prose,
    mechanical_issues,
)
from services.story_bounded_generation_service import BoundedStoryGeneration, draft_payload
from services.story_generation_pipeline import StoryGenerationPipeline
from services.story_word_count_service import classify_chapter_word_count
from services.storydex_orchestration_service import get_storydex_orchestration_service


@pytest.fixture(autouse=True)
def _disable_tier_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORY_LENGTH_TIER_ENABLED", "0")


def test_asymmetric_chapter_boundaries_are_inclusive() -> None:
    below = classify_chapter_word_count(2549, target=3000)
    minimum = classify_chapter_word_count(2550, target=3000)
    soft_maximum = classify_chapter_word_count(3900, target=3000)
    above_soft_maximum = classify_chapter_word_count(3901, target=3000)
    safety_maximum = classify_chapter_word_count(6000, target=3000)
    above_safety_maximum = classify_chapter_word_count(6001, target=3000)

    assert below["productGatePassed"] is False
    assert minimum["productGatePassed"] is True
    assert minimum["hardMinimum"] == 2550
    assert soft_maximum["aboveSoftMaximum"] is False
    assert above_soft_maximum["productGatePassed"] is True
    assert above_soft_maximum["aboveSoftMaximum"] is True
    assert safety_maximum["runtimeSafetyExceeded"] is False
    assert safety_maximum["runtimeSafetyMaximum"] == 6000
    assert above_safety_maximum["productGatePassed"] is False
    assert above_safety_maximum["runtimeSafetyExceeded"] is True


def test_complete_provider_content_envelope_yields_only_publishable_prose() -> None:
    raw = (
        "<content>\n第一段正文。\n\n第二段自然收束。\n</content>\n\n"
        "<summary>模型摘要，不属于正文。</summary>\n\n"
        "<details><summary>模型留言</summary>不要写入章节。</details>"
    )

    cleaned = clean_generated_text(raw)

    assert cleaned == "第一段正文。\n\n第二段自然收束。"
    assert mechanical_issues(cleaned) == []
    assert clean_generated_text("<content>只有包装，没有完整信封。</content>") == (
        "只有包装，没有完整信封。"
    )


def test_observed_unicode_thinking_envelope_yields_only_publishable_prose() -> None:
    raw = (
        "<｜begin▁of▁thinking｜>\n模型内部规划，不属于正文。\n"
        "</｜end▁of▁thinking｜>\n\n"
        "<content>\n第一段正文。\n\n第二段自然收束。\n</content>\n\n"
        "<summary>模型摘要，不属于正文。</summary>\n"
        "<details><summary>模型留言</summary>不要写入章节。</details>"
    )

    assert clean_generated_text(raw) == "第一段正文。\n\n第二段自然收束。"


def test_observed_mismatched_unicode_thinking_close_keeps_following_prose() -> None:
    raw = (
        "<｜begin▁of▁thinking｜>\n"
        "规划里提到了 <content> 和 <summary>，这些都不是正文容器。\n"
        "</｜begin▁of▁thinking｜>"
        "第一段正文。\n\n第二段自然收束。"
    )

    assert clean_generated_text(raw) == "第一段正文。\n\n第二段自然收束。"


def test_unclosed_unicode_thinking_wrapper_is_explicitly_rejected() -> None:
    result = extract_story_prose(
        "<｜begin▁of▁thinking｜>\n模型内部规划。\n\n真正正文尚未形成信封。"
    )

    assert result.status == "rejected"
    assert result.prose == ""
    assert result.reason_codes == ("unclosed_thinking_wrapper",)


def test_unknown_meta_wrapper_is_explicitly_rejected() -> None:
    result = extract_story_prose(
        "<meta-note>这是给作者的说明。</meta-note>\n\n第一段正文。"
    )

    assert result.status == "rejected"
    assert result.prose == ""
    assert result.reason_codes == ("unknown_wrapper",)


def test_closed_standard_meta_wrappers_are_removed_from_implicit_prose() -> None:
    result = extract_story_prose(
        "<thinking>模型内部规划。</thinking>\n\n"
        "第一段正文。\n\n第二段自然收束。\n\n"
        "<summary>模型摘要，不属于正文。</summary>"
    )

    assert result.status == "accepted"
    assert result.prose == "第一段正文。\n\n第二段自然收束。"


def test_explicit_content_survives_multiple_known_sidecars() -> None:
    result = extract_story_prose(
        "<content>第一段正文。\n\n第二段自然收束。</content>\n"
        "<summary>模型摘要。</summary>\n"
        "<details><summary>留言</summary>模型留言。</details>\n"
        "<thinking>内部思考。</thinking>\n"
        "<plan>写作计划。</plan>"
    )

    assert result.status == "accepted"
    assert result.prose == "第一段正文。\n\n第二段自然收束。"


def test_draft_payload_audits_rejected_prose_envelope() -> None:
    payload = draft_payload(
        "<summary>\n未闭合的模型摘要。",
        turn_contract={"turnPlan": {"fragmentCount": 1}},
    )

    assert payload["fragments"] == []
    assert payload["proseExtraction"] == {
        "status": "rejected",
        "accepted": False,
        "reasonCodes": ["unclosed_known_wrapper"],
    }


def test_turn_contract_snapshots_the_asymmetric_policy_when_enabled(
    tmp_path: Path,
) -> None:
    flag_path = tmp_path / ".storydex" / "config" / "feature-flags.json"
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(
        json.dumps({"ASYMMETRIC_STORY_LENGTH_ENABLED": True}),
        encoding="utf-8",
    )

    contract = get_storydex_orchestration_service().build_turn_contract(
        tmp_path,
        prompt="请续写剧情",
        story_generation={
            "chapterTemplateId": SINGLE_FILE_CHAPTER_TEMPLATE_ID,
            "chapterWordCountTarget": 3000,
            "preciseWordCountEnabled": True,
        },
        intent_frame={
            "primary": "story_generation",
            "operationType": "create_new",
            "confidence": 1.0,
            "source": "test",
            "secondary": [],
            "needsTools": True,
            "needsPlanning": True,
            "isAdvisory": False,
        },
    )

    policy = contract["turnPlan"]["wordCountPolicy"]
    assert policy["version"] == 5
    assert policy["normalMinimum"] == 2550
    assert policy["normalMaximum"] == 3900
    assert policy["asymmetric"] == {
        "enabled": True,
        "hardMinimum": 2550,
        "softMaximum": 3900,
        "runtimeSafetyMaximum": 6000,
        "maximumSecondDrafts": 1,
        "selectionStrategy": "asymmetric_length_loss_v1",
    }
    assert policy["precision"]["enabled"] is False
    assert policy["precision"]["reason"] == "asymmetric_story_length_enabled"


@pytest.mark.parametrize(
    ("word_count", "accepted", "above_soft_maximum", "safety_exceeded"),
    [
        (2549, False, False, False),
        (2550, True, False, False),
        (3900, True, False, False),
        (3901, True, True, False),
        (6001, False, True, True),
    ],
)
def test_asymmetric_prewrite_and_postwrite_use_the_same_gate(
    tmp_path: Path,
    word_count: int,
    accepted: bool,
    above_soft_maximum: bool,
    safety_exceeded: bool,
) -> None:
    flag_path = tmp_path / ".storydex" / "config" / "feature-flags.json"
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(
        json.dumps({"ASYMMETRIC_STORY_LENGTH_ENABLED": True}),
        encoding="utf-8",
    )
    contract = get_storydex_orchestration_service().build_turn_contract(
        tmp_path,
        prompt="请续写剧情",
        story_generation={
            "chapterTemplateId": SINGLE_FILE_CHAPTER_TEMPLATE_ID,
            "chapterWordCountTarget": 3000,
        },
        intent_frame={
            "primary": "story_generation",
            "operationType": "create_new",
            "confidence": 1.0,
            "source": "test",
            "secondary": [],
            "needsTools": True,
            "needsPlanning": True,
            "isAdvisory": False,
        },
    )
    target_path = contract["turnPlan"]["fragmentTargets"][0]["path"]
    prose = ("甲" * (word_count - 1)) + "。"

    applied = get_story_project_service().apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": prose}]},
        generation_contract=contract,
    )

    prewrite = applied["wordCountValidation"]
    assert applied["ok"] is accepted
    assert prewrite["aboveSoftMaximum"] is above_soft_maximum
    assert prewrite["runtimeSafetyExceeded"] is safety_exceeded
    assert (tmp_path / target_path).is_file() is accepted
    if not accepted:
        assert applied["writtenPaths"] == []
        return

    postwrite = get_story_project_service().validate_story_generation_turn(
        tmp_path,
        contract,
    )
    assert postwrite["passed"] is True
    for key in (
        "resultingWordCount",
        "hardMinimumPassed",
        "aboveSoftMaximum",
        "runtimeSafetyExceeded",
    ):
        assert postwrite[key] == prewrite[key]


def test_pipeline_skips_the_second_draft_when_the_first_candidate_passes(
    tmp_path: Path,
) -> None:
    flag_path = tmp_path / ".storydex" / "config" / "feature-flags.json"
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(
        json.dumps({"ASYMMETRIC_STORY_LENGTH_ENABLED": True}),
        encoding="utf-8",
    )
    contract = get_storydex_orchestration_service().build_turn_contract(
        tmp_path,
        prompt="请续写剧情",
        story_generation={
            "chapterTemplateId": SINGLE_FILE_CHAPTER_TEMPLATE_ID,
            "chapterWordCountTarget": 3000,
        },
        intent_frame={
            "primary": "story_generation",
            "operationType": "create_new",
            "confidence": 1.0,
            "source": "test",
            "secondary": [],
            "needsTools": True,
            "needsPlanning": True,
            "isAdvisory": False,
        },
    )
    target_path = contract["turnPlan"]["fragmentTargets"][0]["path"]
    second_draft_calls: list[bool] = []

    result = asyncio.run(
        StoryGenerationPipeline(get_story_project_service()).run(
            tmp_path,
            trace_id="asymmetric-first-pass",
            turn_contract=contract,
            generate_draft=lambda: {
                "fragments": [{"text": ("甲" * 2999) + "。"}],
                "qualityPassed": True,
                "qualityIssues": [],
            },
            generate_second_draft=lambda: second_draft_calls.append(True),
        )
    )

    assert second_draft_calls == []
    assert result["committed"] is True
    assert result["asymmetricLengthEnabled"] is True
    assert result["selection"]["reason"] == "first_draft_accepted"
    assert result["selection"]["source"] == "draft"
    assert result["callAccounting"]["logicalStoryCalls"] == 1
    assert result["callAccounting"]["secondDraftCalls"] == 0
    assert get_story_project_service().count_story_file_words(
        tmp_path / target_path
    ) == 3000


def test_pipeline_uses_one_independent_second_draft_after_a_short_first_draft(
    tmp_path: Path,
) -> None:
    flag_path = tmp_path / ".storydex" / "config" / "feature-flags.json"
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(
        json.dumps({"ASYMMETRIC_STORY_LENGTH_ENABLED": True}),
        encoding="utf-8",
    )
    contract = get_storydex_orchestration_service().build_turn_contract(
        tmp_path,
        prompt="请续写剧情",
        story_generation={
            "chapterTemplateId": SINGLE_FILE_CHAPTER_TEMPLATE_ID,
            "chapterWordCountTarget": 3000,
        },
        intent_frame={
            "primary": "story_generation",
            "operationType": "create_new",
            "confidence": 1.0,
            "source": "test",
            "secondary": [],
            "needsTools": True,
            "needsPlanning": True,
            "isAdvisory": False,
        },
    )
    target_path = contract["turnPlan"]["fragmentTargets"][0]["path"]
    second_draft_calls: list[bool] = []

    def generate_second_draft() -> dict[str, object]:
        second_draft_calls.append(True)
        return {
            "fragments": [{"text": ("乙" * 2999) + "。"}],
            "qualityPassed": True,
            "qualityIssues": [],
        }

    result = asyncio.run(
        StoryGenerationPipeline(get_story_project_service()).run(
            tmp_path,
            trace_id="asymmetric-second-draft",
            turn_contract=contract,
            generate_draft=lambda: {
                "fragments": [{"text": ("甲" * 2548) + "。"}],
                "qualityPassed": True,
                "qualityIssues": [],
            },
            generate_second_draft=generate_second_draft,
        )
    )

    assert second_draft_calls == [True]
    assert result["committed"] is True
    assert result["selection"]["reason"] == "second_draft_selected"
    assert result["selection"]["source"] == "second_draft"
    assert result["selection"]["finalWordCount"] == 3000
    assert result["callAccounting"]["logicalStoryCalls"] == 2
    assert result["callAccounting"]["initialGenerationCalls"] == 1
    assert result["callAccounting"]["secondDraftCalls"] == 1
    assert result["callAccounting"]["lengthRevisionCalls"] == 0
    assert result["contractViolations"] == []
    assert get_story_project_service().count_story_file_words(
        tmp_path / target_path
    ) == 3000


def test_asymmetric_selection_filters_hard_gates_before_length_loss() -> None:
    selection = StoryGenerationPipeline(
        get_story_project_service()
    ).select_asymmetric_candidate(
        target=3000,
        candidates=[
            {
                "source": "draft",
                "wordCount": 3000,
                "validationPassed": True,
                "qualityPassed": False,
            },
            {
                "source": "second_draft",
                "wordCount": 3901,
                "validationPassed": True,
                "qualityPassed": True,
            },
        ],
    )

    assert selection["source"] == "second_draft"
    assert selection["wordCount"] == 3901
    assert selection["eligibleCandidateCount"] == 1


def test_asymmetric_length_loss_penalizes_underlength_twice_as_much() -> None:
    selection = StoryGenerationPipeline(
        get_story_project_service()
    ).select_asymmetric_candidate(
        target=3000,
        candidates=[
            {
                "source": "draft",
                "wordCount": 2550,
                "validationPassed": True,
                "qualityPassed": True,
            },
            {
                "source": "second_draft",
                "wordCount": 3901,
                "validationPassed": True,
                "qualityPassed": True,
            },
        ],
    )

    assert selection["source"] == "draft"
    assert selection["asymmetricLengthLoss"] == 900


def test_bounded_runner_uses_the_same_whole_chapter_prompt_for_the_second_draft(
    tmp_path: Path,
) -> None:
    flag_path = tmp_path / ".storydex" / "config" / "feature-flags.json"
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(
        json.dumps({"ASYMMETRIC_STORY_LENGTH_ENABLED": True}),
        encoding="utf-8",
    )
    contract = get_storydex_orchestration_service().build_turn_contract(
        tmp_path,
        prompt="请续写剧情",
        story_generation={
            "chapterTemplateId": SINGLE_FILE_CHAPTER_TEMPLATE_ID,
            "chapterWordCountTarget": 3000,
        },
        intent_frame={
            "primary": "story_generation",
            "operationType": "create_new",
            "confidence": 1.0,
            "source": "test",
            "secondary": [],
            "needsTools": True,
            "needsPlanning": True,
            "isAdvisory": False,
        },
    )

    def varied_prose(count: int, *, offset: int) -> str:
        return "".join(chr(0x4E00 + offset + index) for index in range(count - 1)) + "。"

    class Adapter:
        provider_retries = 0
        last_cap_applied = False

        def __init__(self, response: str, *, completion_tokens: int) -> None:
            self.response = response
            self.last_completion_tokens = completion_tokens
            self.calls: list[dict[str, object]] = []

        async def complete(self, **kwargs: object) -> str:
            self.calls.append(dict(kwargs))
            return self.response

        async def complete_tool_call(self, **_kwargs: object) -> object:
            raise AssertionError("asymmetric mode must not call a repair tool")

    class Controller:
        async def revise(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("asymmetric mode must not call the precision controller")

    first = Adapter(varied_prose(2549, offset=0), completion_tokens=1000)
    second = Adapter(varied_prose(3000, offset=4000), completion_tokens=1400)
    events: list[tuple[str, dict[str, object]]] = []
    result = asyncio.run(
        BoundedStoryGeneration(
            adapter=first,
            revision_adapter=second,
            pipeline=StoryGenerationPipeline(get_story_project_service()),
            controller=Controller(),
            event_sink=lambda name, payload: events.append((name, payload)),
        ).run(
            tmp_path,
            trace_id="asymmetric-runner",
            turn_contract=contract,
            prompt="请续写剧情",
        )
    )

    assert result["committed"] is True
    assert len(first.calls) == 1
    assert len(second.calls) == 1
    assert first.calls[0]["messages"] == second.calls[0]["messages"]
    assert first.calls[0]["purpose"] == "story_initial_generation"
    assert second.calls[0]["purpose"] == "story_second_draft"
    assert result["callAccounting"]["secondDraftCalls"] == 1
    assert result["selection"]["source"] == "second_draft"
    assert result["draftCompletionTokens"] == 1000
    assert result["secondDraftCompletionTokens"] == 1400
    assert result["revisionCompletionTokens"] is None
    assert isinstance(result["secondDraftDurationMs"], int)
    measured = next(
        payload for name, payload in events if name == "StorySecondDraftMeasured"
    )
    assert measured["secondDraftWordCount"] == 3000
    assert measured["completionTokens"] == 1400
    assert measured["providerDurationMs"] == result["secondDraftDurationMs"]
    assert measured["selected"] is True


@pytest.mark.parametrize("first_failure", ["quality", "structure"])
def test_pipeline_retries_once_after_quality_or_structure_failure(
    tmp_path: Path,
    first_failure: str,
) -> None:
    flag_path = tmp_path / ".storydex" / "config" / "feature-flags.json"
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(
        json.dumps({"ASYMMETRIC_STORY_LENGTH_ENABLED": True}),
        encoding="utf-8",
    )
    contract = get_storydex_orchestration_service().build_turn_contract(
        tmp_path,
        prompt="请续写剧情",
        story_generation={
            "chapterTemplateId": SINGLE_FILE_CHAPTER_TEMPLATE_ID,
            "chapterWordCountTarget": 3000,
        },
        intent_frame={
            "primary": "story_generation",
            "operationType": "create_new",
            "confidence": 1.0,
            "source": "test",
            "secondary": [],
            "needsTools": True,
            "needsPlanning": True,
            "isAdvisory": False,
        },
    )
    first_payload: dict[str, object] = {
        "fragments": [{"text": ("甲" * 2999) + "。"}],
        "qualityPassed": first_failure != "quality",
        "qualityIssues": ["narrative_perspective_shift"]
        if first_failure == "quality"
        else [],
    }
    if first_failure == "structure":
        first_payload["fragments"] = [
            {"text": "甲" * 1500},
            {"text": ("乙" * 1499) + "。"},
        ]
    second_calls: list[bool] = []

    result = asyncio.run(
        StoryGenerationPipeline(get_story_project_service()).run(
            tmp_path,
            trace_id=f"asymmetric-{first_failure}-failure",
            turn_contract=contract,
            generate_draft=lambda: first_payload,
            generate_second_draft=lambda: second_calls.append(True)
            or {
                "fragments": [{"text": ("丙" * 2799) + "。"}],
                "qualityPassed": True,
                "qualityIssues": [],
            },
        )
    )

    assert second_calls == [True]
    assert result["committed"] is True
    assert result["selection"]["source"] == "second_draft"
    assert result["selection"]["finalWordCount"] == 2800
    assert result["callAccounting"]["logicalStoryCalls"] == 2


def test_pipeline_writes_nothing_when_both_whole_chapter_candidates_fail(
    tmp_path: Path,
) -> None:
    flag_path = tmp_path / ".storydex" / "config" / "feature-flags.json"
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(
        json.dumps({"ASYMMETRIC_STORY_LENGTH_ENABLED": True}),
        encoding="utf-8",
    )
    contract = get_storydex_orchestration_service().build_turn_contract(
        tmp_path,
        prompt="请续写剧情",
        story_generation={
            "chapterTemplateId": SINGLE_FILE_CHAPTER_TEMPLATE_ID,
            "chapterWordCountTarget": 3000,
        },
        intent_frame={
            "primary": "story_generation",
            "operationType": "create_new",
            "confidence": 1.0,
            "source": "test",
            "secondary": [],
            "needsTools": True,
            "needsPlanning": True,
            "isAdvisory": False,
        },
    )
    target_path = contract["turnPlan"]["fragmentTargets"][0]["path"]

    result = asyncio.run(
        StoryGenerationPipeline(get_story_project_service()).run(
            tmp_path,
            trace_id="asymmetric-both-fail",
            turn_contract=contract,
            generate_draft=lambda: {
                "fragments": [{"text": ("甲" * 2548) + "。"}],
                "qualityPassed": True,
            },
            generate_second_draft=lambda: {
                "fragments": [{"text": ("乙" * 6000) + "。"}],
                "qualityPassed": True,
            },
        )
    )

    assert result["committed"] is False
    assert result["selection"]["reason"] == "no_eligible_candidate"
    assert result["selection"]["source"] == ""
    assert result["applied"]["writtenPaths"] == []
    assert set(result["stagedCandidates"]) == {"initial", "second-draft"}
    assert result["callAccounting"]["logicalStoryCalls"] == 2
    assert result["contractViolations"] == []
    assert not (tmp_path / target_path).exists()
