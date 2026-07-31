"""W2-1: the precise word count switch, its bands, and contract compatibility.

Three intervals carry three separate jobs and must not be conflated:

* the product target ``W`` is what the user asked for;
* the normal interval ``[0.85W, 1.30W]`` is the product SLO and calibration
  observation window;
* the precision band ``[0.90W, 1.10W]`` only decides whether the optional second
  call may run, and whether its candidate may be accepted.

The switch itself defaults to off everywhere. Projects, requests and turn
contracts written before this card must keep reading as off rather than silently
gaining a second provider call.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from api import routes_agent
from api.routes_agent import _normalize_story_generation_options
from api.routes_file import StoryProjectSettingsResponse as FileStoryProjectSettingsResponse
from api.routes_story import StoryProjectSettingsResponse as StoryStoryProjectSettingsResponse
from services.story_project_service import (
    DEFAULT_CHAPTER_TEMPLATE_ID,
    DEFAULT_CHAPTER_WORD_COUNT_TARGET,
    get_story_project_service,
)
from services.story_bounded_generation_service import BoundedStoryGeneration
from services.story_word_count_service import (
    PRECISION_REVISION_STRATEGY,
    WORD_COUNT_POLICY_VERSION,
    chapter_normal_band,
    chapter_precision_band,
    classify_chapter_word_count,
)
from services.storydex_orchestration_service import get_storydex_orchestration_service


@pytest.fixture(autouse=True)
def legacy_story_length_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORY_LENGTH_TIER_ENABLED", "0")


def _story_contract(root: Path, **story_generation: Any) -> dict[str, Any]:
    options: dict[str, Any] = {"chapterTemplateId": DEFAULT_CHAPTER_TEMPLATE_ID}
    options.update(story_generation)
    return get_storydex_orchestration_service().build_turn_contract(
        root,
        prompt="请续写剧情",
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
    )


def _word_count_policy(contract: dict[str, Any]) -> dict[str, Any]:
    return contract["turnPlan"]["wordCountPolicy"]


# --- pure bands -------------------------------------------------------------


def test_default_target_resolves_the_documented_bands() -> None:
    assert chapter_normal_band(3000) == (2550, 3900)
    assert chapter_precision_band(3000) == (2700, 3300)


@pytest.mark.parametrize(
    ("count", "normal_passed", "precision_passed"),
    [
        (2549, False, False),
        (2550, True, False),
        (2699, True, False),
        (2700, True, True),
        (3000, True, True),
        (3300, True, True),
        (3301, True, False),
        (3900, True, False),
        (3901, False, False),
    ],
)
def test_band_boundaries_are_inclusive(
    count: int,
    normal_passed: bool,
    precision_passed: bool,
) -> None:
    status = classify_chapter_word_count(count, target=3000)
    assert status["normalBandPassed"] is normal_passed
    assert status["precisionBandPassed"] is precision_passed


def test_classification_reports_both_bands_and_the_target() -> None:
    status = classify_chapter_word_count(2600, target=3000)
    assert status["target"] == 3000
    assert status["actualWordCount"] == 2600
    assert (status["normalMinimum"], status["normalMaximum"]) == (2550, 3900)
    assert (status["precisionMinimum"], status["precisionMaximum"]) == (2700, 3300)
    # A draft inside the wide band but short of precision is exactly the case the
    # optional second call exists for; it must be distinguishable from a miss.
    assert status["normalBandPassed"] is True
    assert status["precisionBandPassed"] is False
    assert status["direction"] == "expand"


def test_classification_reports_the_compression_direction() -> None:
    assert classify_chapter_word_count(4200, target=3000)["direction"] == "compress"
    assert classify_chapter_word_count(3000, target=3000)["direction"] == ""


@pytest.mark.parametrize("target", [100, 500, 1500, 3000, 5000, 20000])
def test_precision_band_stays_nested_inside_the_normal_band(target: int) -> None:
    normal_minimum, normal_maximum = chapter_normal_band(target)
    precision_minimum, precision_maximum = chapter_precision_band(target)
    assert normal_minimum <= precision_minimum
    assert precision_maximum <= normal_maximum
    assert precision_minimum <= target <= precision_maximum


# --- project settings -------------------------------------------------------


def test_default_project_settings_disable_precise_word_count(tmp_path: Path) -> None:
    service = get_story_project_service()
    defaults = service.default_project_settings()
    assert defaults["chapterWordCountTarget"] == DEFAULT_CHAPTER_WORD_COUNT_TARGET
    assert defaults["preciseWordCountEnabled"] is False
    assert service.read_project_settings(tmp_path)["preciseWordCountEnabled"] is False


def test_legacy_project_settings_without_the_switch_read_as_disabled(tmp_path: Path) -> None:
    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)
    settings_path = service.project_settings_path(tmp_path)
    settings_path.write_text(
        json.dumps({"version": 1, "storySegmentFormat": "md"}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert service.read_project_settings(tmp_path)["preciseWordCountEnabled"] is False


def test_project_settings_retire_the_switch_on_write(tmp_path: Path) -> None:
    service = get_story_project_service()
    saved = service.write_project_settings(tmp_path, {"preciseWordCountEnabled": True})
    assert saved["preciseWordCountEnabled"] is False
    assert service.read_project_settings(tmp_path)["preciseWordCountEnabled"] is False
    unchanged = service.write_project_settings(tmp_path, {"autoUpdateWiki": True})
    assert unchanged["preciseWordCountEnabled"] is False


def test_settings_response_models_expose_the_switch() -> None:
    for model in (StoryStoryProjectSettingsResponse, FileStoryProjectSettingsResponse):
        field = model.model_fields["precise_word_count_enabled"]
        assert field.alias == "preciseWordCountEnabled"
        assert field.default is False


# --- turn contract ----------------------------------------------------------


def test_agent_request_normalization_retires_precision_overrides() -> None:
    assert _normalize_story_generation_options(
        {"preciseWordCountEnabled": True}
    )["preciseWordCountEnabled"] is False
    assert _normalize_story_generation_options(
        {"precise_word_count_enabled": False}
    )["preciseWordCountEnabled"] is False
    assert _normalize_story_generation_options({})["preciseWordCountEnabled"] is False


def test_turn_contract_snapshots_policy_version_and_bands(tmp_path: Path) -> None:
    policy = _word_count_policy(_story_contract(tmp_path, chapterWordCountTarget=3000))
    assert policy["version"] == WORD_COUNT_POLICY_VERSION == 5
    assert policy["scope"] == "chapter"
    assert policy["target"] == 3000
    assert policy["normalMinimum"] == 2550
    assert policy["normalMaximum"] == 3900


def test_turn_contract_disables_precision_by_default(tmp_path: Path) -> None:
    precision = _word_count_policy(_story_contract(tmp_path, chapterWordCountTarget=3000))["precision"]
    assert precision["enabled"] is False
    assert precision["minimum"] == 2700
    assert precision["maximum"] == 3300
    assert precision["maximumRevisionCalls"] == 1
    assert precision["revisionStrategy"] == PRECISION_REVISION_STRATEGY


def test_turn_contract_enables_precision_from_the_request(tmp_path: Path) -> None:
    contract = _story_contract(
        tmp_path,
        chapterWordCountTarget=2000,
        preciseWordCountEnabled=True,
    )
    precision = _word_count_policy(contract)["precision"]
    assert precision["enabled"] is True
    assert (precision["minimum"], precision["maximum"]) == (1800, 2200)


def test_turn_contract_accepts_the_snake_case_request_alias(tmp_path: Path) -> None:
    contract = _story_contract(tmp_path, precise_word_count_enabled=True)
    assert _word_count_policy(contract)["precision"]["enabled"] is True


def test_turn_contract_does_not_revive_precision_from_project_settings(tmp_path: Path) -> None:
    get_story_project_service().write_project_settings(
        tmp_path,
        {"preciseWordCountEnabled": True},
    )
    assert _word_count_policy(_story_contract(tmp_path))["precision"]["enabled"] is False


def test_request_switch_overrides_the_project_setting(tmp_path: Path) -> None:
    get_story_project_service().write_project_settings(
        tmp_path,
        {"preciseWordCountEnabled": True},
    )
    contract = _story_contract(tmp_path, preciseWordCountEnabled=False)
    assert _word_count_policy(contract)["precision"]["enabled"] is False


def test_legacy_range_requests_keep_precision_disabled(tmp_path: Path) -> None:
    # A min/max range has no single centre, so there is no precision band to
    # revise towards. Asking for precision must not enable a second call.
    contract = _story_contract(
        tmp_path,
        fragmentWordCountMin=2000,
        fragmentWordCountMax=2500,
        preciseWordCountEnabled=True,
    )
    policy = _word_count_policy(contract)
    assert policy["mode"] == "range"
    assert policy["precision"]["enabled"] is False
    assert policy["precision"]["reason"] == "legacy_range_mode"


def test_acceptance_band_still_matches_the_normal_band(tmp_path: Path) -> None:
    # The acceptance keys stay for existing consumers; they must not drift from
    # the frozen normal band.
    policy = _word_count_policy(_story_contract(tmp_path, chapterWordCountTarget=3000))
    assert policy["acceptanceMinimum"] == policy["normalMinimum"]
    assert policy["acceptanceMaximum"] == policy["normalMaximum"]


def test_revision_event_keeps_unknown_completion_usage_as_null(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    class DraftAdapter:
        provider_retries = 0
        last_completion_tokens = None
        last_cap_applied = True

        async def complete(self, **_kwargs: Any) -> str:
            return "首稿正文。"

    class RevisionAdapter:
        last_completion_tokens = None
        last_cap_applied = False

        async def revision_budget_policy(self) -> dict[str, Any]:
            return {
                "name": "openai_compatible_non_streaming",
                "deadlineRatio": 1.25,
                "deadlineMinimumSeconds": 30,
                "deadlineMaximumSeconds": 60,
            }

        async def complete_tool_call(self, **kwargs: Any) -> None:
            assert kwargs["max_completion_tokens"] == 512
            return None

    class Controller:
        async def revise(self, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            assert kwargs["budget_policy"] == {
                "name": "openai_compatible_non_streaming",
                "deadlineRatio": 1.25,
                "deadlineMinimumSeconds": 30,
                "deadlineMaximumSeconds": 60,
            }
            await kwargs["call_provider"](
                messages=[],
                tool={},
                max_completion_tokens=512,
            )
            return {
                "fragments": [],
                "qualityPassed": False,
                "qualityIssues": ["tool_arguments_truncated"],
                "budget": {"maxCompletionTokens": 512},
            }

    class Pipeline:
        async def run(self, _root: Path, **kwargs: Any) -> dict[str, Any]:
            await kwargs["generate_draft"]()
            await kwargs["revise"](
                {
                    "direction": "expand",
                    "draftWordCount": 2200,
                    "draftPayload": {"fragments": [{"text": "首稿正文。"}]},
                    "target": 3000,
                }
            )
            return {
                "selection": {"draftStatus": {}},
                "draftWordCount": 2200,
                "draftGeneratedWordCount": 2200,
                "retainedWordCount": 0,
                "target": 3000,
            }

    contract = _story_contract(
        tmp_path,
        chapterWordCountTarget=3000,
        preciseWordCountEnabled=True,
    )
    runner = BoundedStoryGeneration(
        adapter=DraftAdapter(),
        revision_adapter=RevisionAdapter(),
        pipeline=Pipeline(),
        controller=Controller(),
        event_sink=lambda name, payload: events.append((name, payload)),
    )

    result = asyncio.run(
        runner.run(
            tmp_path,
            trace_id="trace-null-usage",
            turn_contract=contract,
            prompt="继续当前章",
        )
    )
    revision_event = next(payload for name, payload in events if name == "StoryLengthRevisionResult")
    draft_event = next(payload for name, payload in events if name == "StoryDraftMeasured")

    assert draft_event["providerDurationMs"] == result["draftDurationMs"]
    assert draft_event["capApplied"] is True
    assert revision_event["completionTokens"] is None
    assert revision_event["capApplied"] is False
    assert revision_event["budget"]["capApplied"] is False
    assert revision_event["rejectionReasons"] == ["tool_arguments_truncated"]
    assert result["revisionCompletionTokens"] is None
    assert result["revisionCapApplied"] is False


def test_bounded_runner_forwards_the_controller_selected_redraft_tool_name(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []
    events: list[tuple[str, dict[str, Any]]] = []

    class DraftAdapter:
        provider_retries = 0
        last_completion_tokens = 9000
        last_cap_applied = False

        async def complete(self, **_kwargs: Any) -> str:
            return "甲" * 5000

    class RevisionAdapter:
        last_completion_tokens = 4800
        last_cap_applied = True

        async def complete_tool_call(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"version": 1, "strategy": "feedback_bounded_redraft", "paragraphs": []}

    class Controller:
        async def revise(self, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            await kwargs["call_provider"](
                messages=[],
                tool={"name": "StorydexSubmitBoundedRedraft", "parameters": {}},
                max_completion_tokens=5600,
            )
            return {
                "fragments": [],
                "qualityPassed": False,
                "qualityIssues": ["tool_arguments_invalid_redraft"],
                "strategy": "feedback_bounded_redraft",
                "redraftParagraphCount": 34,
                "suggestedRedraftParagraphRange": [35, 39],
                "redraftParagraphRangeAdhered": False,
                "budget": {"maxCompletionTokens": 5600},
            }

    class Pipeline:
        async def run(self, _root: Path, **kwargs: Any) -> dict[str, Any]:
            await kwargs["generate_draft"]()
            await kwargs["revise"](
                {
                    "direction": "compress",
                    "draftWordCount": 5000,
                    "draftPayload": {"fragments": [{"text": "甲" * 5000}]},
                    "target": 3000,
                }
            )
            return {
                "selection": {"draftStatus": {}},
                "draftWordCount": 5000,
                "draftGeneratedWordCount": 5000,
                "retainedWordCount": 0,
                "target": 3000,
            }

    runner = BoundedStoryGeneration(
        adapter=DraftAdapter(),
        revision_adapter=RevisionAdapter(),
        pipeline=Pipeline(),
        controller=Controller(),
        event_sink=lambda name, payload: events.append((name, payload)),
    )
    contract = _story_contract(
        tmp_path,
        chapterWordCountTarget=3000,
        preciseWordCountEnabled=True,
    )

    result = asyncio.run(
        runner.run(
            tmp_path,
            trace_id="trace-redraft-tool-name",
            turn_contract=contract,
            prompt="继续当前章",
        )
    )

    assert len(calls) == 1
    assert calls[0]["tool_name"] == "StorydexSubmitBoundedRedraft"
    assert calls[0]["max_completion_tokens"] == 5600
    assert calls[0]["metadata"]["strategy"] == "feedback_bounded_redraft"
    started = next(
        payload for name, payload in events if name == "StoryLengthRevisionStarted"
    )
    finished = next(
        payload for name, payload in events if name == "StoryLengthRevisionResult"
    )
    assert started["strategy"] == "feedback_bounded_redraft"
    assert finished["strategy"] == "feedback_bounded_redraft"
    assert finished["redraftParagraphCount"] == 34
    assert finished["suggestedRedraftParagraphRange"] == [35, 39]
    assert finished["redraftParagraphRangeAdhered"] is False
    assert result["revisionStrategy"] == "feedback_bounded_redraft"


@pytest.mark.parametrize("elastic_enabled", [False, True])
def test_bounded_story_route_configures_zero_transport_retries_and_elastic_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    elastic_enabled: bool,
) -> None:
    adapter_retries: list[int] = []
    runner_options: list[dict[str, Any]] = []
    if elastic_enabled:
        flag_path = tmp_path / ".storydex" / "config" / "feature-flags.json"
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        flag_path.write_text(
            json.dumps({"ELASTIC_STORY_MANUSCRIPT_ENABLED": True}),
            encoding="utf-8",
        )

    class Adapter:
        def __init__(self, **kwargs: Any) -> None:
            adapter_retries.append(int(kwargs["maximum_transport_retries"]))

    class Runner:
        def __init__(self, **kwargs: Any) -> None:
            runner_options.append(kwargs)
            self.accounting = type("Accounting", (), {"payload": lambda self: {}})()

        async def run(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"committed": False, "callAccounting": {}}

    monkeypatch.setattr(routes_agent, "CoomiStoryGenerationAdapter", Adapter)
    monkeypatch.setattr(routes_agent, "BoundedStoryGeneration", Runner)
    monkeypatch.setattr(routes_agent, "get_story_generation_pipeline", lambda: object())
    monkeypatch.setattr(
        routes_agent,
        "get_story_length_precision_controller",
        lambda: object(),
    )

    result = asyncio.run(
        routes_agent._execute_bounded_story_generation(
            prompt="继续当前章",
            trace_id="trace-zero-retries",
            active_file="",
            workspace_root=tmp_path,
            turn_contract=_story_contract(
                tmp_path,
                chapterWordCountTarget=3000,
                preciseWordCountEnabled=True,
            ),
            event_sink=lambda _name, _payload: None,
        )
    )

    assert result["ok"] is False
    assert adapter_retries == [0, 0]
    assert runner_options[0]["elastic_enabled"] is elastic_enabled
