from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from api.routes_agent import _normalize_story_generation_options
from core.config import FEATURE_FLAG_DEFAULTS
from services.story_bounded_generation_service import (
    BoundedStoryGeneration,
    build_draft_messages,
)
from services.story_generation_pipeline import StoryGenerationPipeline
from services.story_length_tier_calibration_service import (
    LENGTH_TIER_CALIBRATION_RELATIVE_PATH,
    StoryLengthTierCalibrationService,
)
from services.story_project_service import StoryProjectService
from services.story_word_count_service import (
    STORY_LENGTH_TIER_POLICIES,
    STORY_LENGTH_TIER_PROMPTS,
    chapter_length_tier_policy_payload,
    classify_chapter_length_tier,
    migrate_chapter_word_count_target,
)
from services.storydex_orchestration_service import StorydexOrchestrationService


def _intent() -> dict[str, object]:
    return {
        "primary": "story_generation",
        "confidence": "high",
        "operationType": "create_new",
        "complexity": "simple",
        "canWrite": True,
        "assetTargets": ["chapters/"],
        "matchedSkills": [],
        "signals": [],
    }


def _contract(
    root: Path,
    *,
    tier: str | None = None,
    legacy_target: int | None = None,
) -> tuple[StoryProjectService, dict[str, object]]:
    flag_path = root / ".storydex" / "config" / "feature-flags.json"
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(
        json.dumps({"STORY_LENGTH_TIER_ENABLED": True}),
        encoding="utf-8",
    )
    project = StoryProjectService()
    options: dict[str, object] = {"fragmentCount": 1}
    if tier is not None:
        options["chapterLengthTier"] = tier
    if legacy_target is not None:
        options["chapterWordCountTarget"] = legacy_target
    contract = StorydexOrchestrationService(
        project,
        length_tier_calibration_service=StoryLengthTierCalibrationService(),
    ).build_turn_contract(
        root,
        prompt="写第一章，主角在雨夜发现一封旧信。",
        story_generation=options,
        intent_frame=_intent(),
        provider="TEST",
        model="test-model",
    )
    return project, contract


def test_tier_feature_stays_disabled_until_acceptance_passes() -> None:
    assert FEATURE_FLAG_DEFAULTS["STORY_LENGTH_TIER_ENABLED"] is False


@pytest.mark.parametrize("tier", ["short", "medium", "long"])
def test_tier_boundaries_are_inclusive(tier: str) -> None:
    bounds = STORY_LENGTH_TIER_POLICIES[tier]
    cases = {
        bounds["hardMinimum"] - 1: (False, False),
        bounds["hardMinimum"]: (False, True),
        bounds["preferredMinimum"]: (True, True),
        bounds["preferredMaximum"]: (True, True),
        bounds["runtimeSafetyMaximum"]: (False, True),
        bounds["runtimeSafetyMaximum"] + 1: (False, False),
    }
    for count, (tier_hit, committable) in cases.items():
        status = classify_chapter_length_tier(count, tier=tier)
        assert status["tierHit"] is tier_hit
        assert status["committable"] is committable


@pytest.mark.parametrize(
    ("target", "tier"),
    [(1500, "short"), (2000, "short"), (2001, "medium"), (3000, "medium"), (4000, "medium"), (5000, "long")],
)
def test_legacy_numeric_targets_migrate_to_tiers(target: int, tier: str) -> None:
    assert migrate_chapter_word_count_target(target) == tier
    assert _normalize_story_generation_options(
        {"chapterWordCountTarget": target}
    )["chapterLengthTier"] == tier


def test_explicit_tier_wins_over_legacy_target(tmp_path: Path) -> None:
    normalized = _normalize_story_generation_options(
        {"chapterLengthTier": "long", "chapterWordCountTarget": 1500}
    )
    assert normalized["chapterLengthTier"] == "long"
    assert "chapterWordCountTarget" not in normalized
    _, contract = _contract(tmp_path, tier="long", legacy_target=1500)
    assert contract["turnPlan"]["chapterLengthTier"] == "long"


def test_turn_contract_freezes_single_candidate_tier_policy(tmp_path: Path) -> None:
    _, contract = _contract(tmp_path, tier="medium")
    plan = contract["turnPlan"]
    policy = plan["wordCountPolicy"]
    assert plan["chapterLengthTier"] == "medium"
    assert "chapterWordCountTarget" not in plan
    assert policy["version"] == 5
    assert policy["mode"] == "tier"
    assert policy["scope"] == "candidate"
    assert policy["tier"] == "medium"
    assert policy["promptVersion"] == "story_length_tier_v1"
    assert policy["maximumProseCalls"] == 1
    assert policy["retryOnLengthMiss"] is False
    assert policy["precision"]["enabled"] is False
    assert policy["asymmetric"]["enabled"] is False
    assert policy["paragraphQuota"] == 0
    assert "generationControl" not in plan
    assert all("referenceWordCount" not in item for item in plan["fragmentTargets"])


@pytest.mark.parametrize("tier", ["short", "medium", "long"])
def test_tier_prompt_is_one_semantic_line_without_numeric_quota(
    tmp_path: Path,
    tier: str,
) -> None:
    _, contract = _contract(tmp_path, tier=tier)
    messages = build_draft_messages(
        prompt="写第一章，主角在雨夜发现一封旧信。",
        turn_contract=contract,
    )
    rendered = "\n".join(str(item["content"]) for item in messages)
    assert STORY_LENGTH_TIER_PROMPTS[tier] in rendered
    for forbidden in (
        "chapterWordCountTarget",
        "paragraphQuota",
        "参考长度约为",
        "non-whitespace characters",
        "计数规则（由程序执行",
        "第二稿",
        "second draft",
        "beat 配额",
    ):
        assert forbidden not in rendered
    for number in (700, 1000, 1800, 2200, 2500, 3000, 4000, 5000, 6000, 7200, 9000):
        assert str(number) not in rendered


def test_tier_draft_strips_numeric_length_directives_from_author_request(
    tmp_path: Path,
) -> None:
    _, contract = _contract(tmp_path, tier="medium")
    messages = build_draft_messages(
        prompt="写第一章，主角在雨夜发现一封旧信，本章目标 3000 字。",
        turn_contract=contract,
    )
    rendered = "\n".join(str(item["content"]) for item in messages)
    assert "主角在雨夜发现一封旧信" in rendered
    assert "3000" not in rendered
    assert STORY_LENGTH_TIER_PROMPTS["medium"] in rendered


def test_tier_draft_strips_numeric_preset_length_directives(
    tmp_path: Path,
) -> None:
    active = tmp_path / ".storydex" / "presets" / "active"
    active.mkdir(parents=True, exist_ok=True)
    (active / "style.md").write_text(
        "本章目标 3000 字。保持克制冷峻的叙述风格。",
        encoding="utf-8",
    )
    _, contract = _contract(tmp_path, tier="long")
    messages = build_draft_messages(
        prompt="写第一章，主角在雨夜发现一封旧信。",
        turn_contract=contract,
    )
    rendered = "\n".join(str(item["content"]) for item in messages)
    assert "保持克制冷峻的叙述风格" in rendered
    assert "3000" not in rendered
    assert any(
        "preset_length_directives_stripped" in str(note)
        for note in contract["contextAssembly"]["notes"]
    )


def _payload(count: int, *, quality_passed: bool = True) -> dict[str, object]:
    return {
        "fragments": [{"text": "甲" * count}],
        "qualityPassed": quality_passed,
        "qualityIssues": [] if quality_passed else ["incomplete_ending"],
    }


def test_safe_tier_miss_is_committed_without_revision(tmp_path: Path) -> None:
    project, contract = _contract(tmp_path, tier="short")
    revision_called = False

    async def revise(_: dict[str, object]) -> dict[str, object]:
        nonlocal revision_called
        revision_called = True
        raise AssertionError("tier mode must not revise")

    result = asyncio.run(
        StoryGenerationPipeline(project).run(
            tmp_path,
            trace_id="tier-miss",
            turn_contract=contract,
            generate_draft=lambda: _payload(3500),
            generate_second_draft=lambda: (_ for _ in ()).throw(
                AssertionError("tier mode must not generate a second draft")
            ),
            revise=revise,
        )
    )
    assert result["committed"] is True
    assert result["tierHit"] is False
    assert result["tierDeviation"] == "above_preferred"
    assert revision_called is False
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
    validation = project.validate_story_generation_turn(tmp_path, contract)
    assert validation["passed"] is True
    assert validation["tierHit"] is False
    assert validation["resultingWordCount"] == 3500


def test_tier_pipeline_classifies_only_the_current_candidate(tmp_path: Path) -> None:
    project, contract = _contract(tmp_path, tier="short")
    contract["turnPlan"]["wordCountPolicy"]["retainedWordCount"] = 2600

    result = asyncio.run(
        StoryGenerationPipeline(project).run(
            tmp_path,
            trace_id="candidate-scope",
            turn_contract=contract,
            generate_draft=lambda: _payload(1200),
        )
    )

    assert result["committed"] is True
    assert result["draftGeneratedWordCount"] == 1200
    assert result["draftWordCount"] == 1200
    assert result["tierHit"] is True
    validation = result["selection"]["draftValidation"]
    assert validation["wordCountScope"] == "candidate"
    assert validation["actualWordCount"] == 1200
    assert validation["generatedWordCount"] == 1200
    assert validation["retainedWordCount"] == 2600
    assert validation["resultingWordCount"] == 3800


def test_tier_post_write_validation_classifies_only_the_current_candidate(
    tmp_path: Path,
) -> None:
    project, contract = _contract(tmp_path, tier="short")
    contract["turnPlan"]["wordCountPolicy"]["retainedWordCount"] = 2600
    result = asyncio.run(
        StoryGenerationPipeline(project).run(
            tmp_path,
            trace_id="candidate-scope-post-write",
            turn_contract=contract,
            generate_draft=lambda: _payload(1200),
        )
    )
    assert result["committed"] is True

    validation = project.validate_story_generation_turn(tmp_path, contract)
    assert validation["passed"] is True
    assert validation["wordCountScope"] == "candidate"
    assert validation["actualWordCount"] == 1200
    assert validation["generatedWordCount"] == 1200
    assert validation["retainedWordCount"] == 2600
    assert validation["resultingWordCount"] == 3800
    assert validation["tierHit"] is True


def test_tier_draft_measurement_reports_candidate_and_resulting_counts(
    tmp_path: Path,
) -> None:
    _, contract = _contract(tmp_path, tier="short")
    contract["turnPlan"]["wordCountPolicy"]["retainedWordCount"] = 2600
    events: list[tuple[str, dict[str, object]]] = []

    class Adapter:
        provider_retries = 0
        last_completion_tokens = 400
        last_cap_applied = False

        async def complete(self, **_kwargs: object) -> str:
            return "灯" * 1200

    class Pipeline:
        async def run(self, _root: Path, **kwargs: object) -> dict[str, object]:
            await kwargs["generate_draft"]()
            return {
                "selection": {
                    "draftStatus": {
                        "tierHit": True,
                        "tierDeviation": "within_preferred",
                    },
                    "draftQualityPassed": True,
                },
                "draftWordCount": 1200,
                "draftGeneratedWordCount": 1200,
                "retainedWordCount": 2600,
                "resultingWordCount": 3800,
                "callAccounting": {},
            }

    runner = BoundedStoryGeneration(
        adapter=Adapter(),
        pipeline=Pipeline(),
        controller=object(),
        event_sink=lambda name, payload: events.append((name, payload)),
    )
    asyncio.run(
        runner.run(
            tmp_path,
            trace_id="candidate-measured",
            turn_contract=contract,
            prompt="继续写。",
        )
    )

    measured = next(
        payload for name, payload in events if name == "StoryDraftMeasured"
    )
    assert measured["wordCountScope"] == "candidate"
    assert measured["actualWordCount"] == 1200
    assert measured["generatedWordCount"] == 1200
    assert measured["retainedWordCount"] == 2600
    assert measured["resultingWordCount"] == 3800


@pytest.mark.parametrize(
    ("count", "quality_passed", "expected_issue"),
    [(699, True, "hard_minimum"), (4001, True, "runtime_safety"), (1500, False, "quality")],
)
def test_unsafe_or_low_quality_candidate_is_staged_without_write(
    tmp_path: Path,
    count: int,
    quality_passed: bool,
    expected_issue: str,
) -> None:
    project, contract = _contract(tmp_path, tier="short")
    result = asyncio.run(
        StoryGenerationPipeline(project).run(
            tmp_path,
            trace_id=f"reject-{expected_issue}",
            turn_contract=contract,
            generate_draft=lambda: _payload(
                count,
                quality_passed=quality_passed,
            ),
            generate_second_draft=lambda: (_ for _ in ()).throw(
                AssertionError("tier mode must not generate a second draft")
            ),
            revise=lambda _: (_ for _ in ()).throw(
                AssertionError("tier mode must not revise")
            ),
        )
    )
    assert result["committed"] is False
    assert result["callAccounting"]["logicalStoryCalls"] == 1
    assert result["callAccounting"]["lengthRevisionCalls"] == 0
    assert result["callAccounting"]["secondDraftCalls"] == 0
    staged = result["stagedCandidates"]["initial"]
    assert (tmp_path / staged).is_file()
    authoritative_path = contract["turnPlan"]["authoritativeFragmentPaths"][0]
    assert not (tmp_path / authoritative_path).exists()


def test_tier_calibration_is_separate_and_uses_quality_valid_initial_samples(
    tmp_path: Path,
) -> None:
    service = StoryLengthTierCalibrationService()
    values = {
        "short": range(1200, 2400, 100),
        "medium": range(2800, 4000, 100),
        "long": range(4400, 5600, 100),
    }
    for tier, samples in values.items():
        for index, actual in enumerate(samples):
            assert service.record_sample(
                tmp_path,
                provider="TEST",
                model="model",
                tier=tier,
                actual_word_count=actual,
                tier_hit=index != 0,
                structure_passed=True,
                machine_quality_passed=True,
                trace_id=f"{tier}-{index}",
            )
    assert service.record_sample(
        tmp_path,
        provider="TEST",
        model="model",
        tier="short",
        actual_word_count=3900,
        tier_hit=False,
        structure_passed=True,
        machine_quality_passed=False,
        trace_id="quality-failed",
    )
    assert not service.record_sample(
        tmp_path,
        provider="TEST",
        model="model",
        tier="short",
        actual_word_count=2000,
        tier_hit=True,
        structure_passed=True,
        machine_quality_passed=True,
        attempt_kind="precision_revision",
        trace_id="revision",
    )
    assert not service.record_sample(
        tmp_path,
        provider="TEST",
        model="model",
        tier="short",
        actual_word_count=2000,
        tier_hit=True,
        structure_passed=True,
        machine_quality_passed=True,
        word_count_scope="chapter",
        trace_id="chapter-scoped",
    )
    summary = service.read_summary(
        tmp_path,
        provider="TEST",
        model="model",
    )
    assert summary["status"] == "applied"
    assert summary["sampleCounts"] == {"short": 12, "medium": 12, "long": 12}
    assert summary["medians"]["short"] < summary["medians"]["medium"] < summary["medians"]["long"]
    calibration_path = tmp_path / LENGTH_TIER_CALIBRATION_RELATIVE_PATH
    assert calibration_path.is_file()
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    assert {sample["wordCountScope"] for sample in calibration["samples"]} == {
        "candidate"
    }
    assert not (tmp_path / ".storydex/memory/length_calibration.json").exists()
    policy = service.resolve_policy(
        tmp_path,
        tier="medium",
        provider="TEST",
        model="model",
    )
    assert policy["calibration"]["status"] == "applied"
    assert policy["hardMinimum"] == chapter_length_tier_policy_payload("medium")["hardMinimum"]
    assert policy["runtimeSafetyMaximum"] == chapter_length_tier_policy_payload("medium")["runtimeSafetyMaximum"]
