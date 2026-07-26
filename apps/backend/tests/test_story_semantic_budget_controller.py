from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from services.story_semantic_budget_controller import (
    SemanticBudgetController,
    SemanticBudgetRequest,
    automatic_scene_count,
    contextual_quality_issues,
    dynamic_scene_budget,
    initial_scene_budgets,
    mechanical_issues,
    parse_scene_plan,
    within_run_model_reference,
)
from services.story_semantic_budget_context import read_scene_constraint_context


def _plan(scene_count: int) -> str:
    return json.dumps(
        {
            "scenes": [
                {
                    "title": f"scene-{index}",
                    "purpose": f"advance causal step {index}",
                    "development": f"perform distinct action {index}",
                    "exitHook": f"lead to step {index + 1}",
                    "weight": 1.0,
                }
                for index in range(1, scene_count + 1)
            ]
        }
    )


def _prose(count: int, seed: int = 0) -> str:
    assert count >= 1
    start = seed * 1500
    body = "".join(chr(0x4E00 + ((start + index) % 19000)) for index in range(count - 1))
    return body + "。"


def _perspective_prose(pronoun: str, seed: int = 0, paragraphs: int = 6) -> str:
    return "\n\n".join(
        f"{pronoun}{_prose(75, seed + index)}" for index in range(paragraphs)
    )


class FakeAdapter:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[Dict[str, Any]] = []

    async def complete(
        self,
        *,
        messages: list[Dict[str, str]],
        purpose: str,
        metadata: Dict[str, Any],
    ) -> str:
        self.calls.append({"messages": messages, "purpose": purpose, "metadata": metadata})
        if not self.responses:
            raise AssertionError("fake adapter ran out of responses")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return str(response(purpose, metadata))
        return str(response)


def _request(**overrides: Any) -> SemanticBudgetRequest:
    values = {
        "product_target_word_count": 3000,
        "user_task": "continue the chapter with one new danger and a causal resolution",
        "source_context": "existing chapter ending.",
        "constraint_context": "keep restrained prose and causal action",
    }
    values.update(overrides)
    return SemanticBudgetRequest(**values)


@pytest.mark.parametrize(
    ("target", "expected_scene_count"),
    [(1500, 3), (3000, 4), (5000, 5)],
)
def test_scene_mapping_and_initial_budget_sum(target: int, expected_scene_count: int) -> None:
    assert automatic_scene_count(target) == expected_scene_count
    scenes = parse_scene_plan(_plan(expected_scene_count), expected_scene_count)
    budgets = initial_scene_budgets(target, scenes)
    average = target / expected_scene_count

    assert len(budgets) == expected_scene_count
    assert sum(budgets) == target
    assert all(max(220, round(average * 0.80)) <= item <= round(average * 1.25) for item in budgets)


def test_extreme_scene_weights_are_clamped_and_budget_still_sums_to_target() -> None:
    payload = json.loads(_plan(4))
    payload["scenes"][0]["weight"] = -100
    payload["scenes"][1]["weight"] = 100
    scenes = parse_scene_plan(json.dumps(payload), 4)
    budgets = initial_scene_budgets(3000, scenes)

    assert [scene["weight"] for scene in scenes[:2]] == [0.8, 1.2]
    assert sum(budgets) == 3000
    assert all(600 <= item <= 938 for item in budgets)


def test_duplicate_causal_steps_are_rejected() -> None:
    payload = json.loads(_plan(3))
    payload["scenes"][1]["purpose"] = payload["scenes"][0]["purpose"]
    payload["scenes"][1]["development"] = payload["scenes"][0]["development"]

    with pytest.raises(ValueError, match="duplicates"):
        parse_scene_plan(json.dumps(payload), 3)


def test_dynamic_budget_moves_opposite_to_previous_scene_deviation() -> None:
    initial = [750, 750, 750, 750]

    after_overwrite = dynamic_scene_budget(target=3000, written=1000, initial=initial, index=1)
    after_underwrite = dynamic_scene_budget(target=3000, written=500, initial=initial, index=1)

    assert after_overwrite < initial[1]
    assert after_underwrite > initial[1]
    assert dynamic_scene_budget(target=3000, written=5000, initial=initial, index=3) == 562


def test_within_run_reference_uses_bounded_recent_gain() -> None:
    reference, gain = within_run_model_reference(800, [1.2, 4.0, 1.4])
    assert gain == 1.4
    assert reference == 571

    bounded_reference, bounded_gain = within_run_model_reference(800, [9.0, 9.0, 9.0])
    assert bounded_gain == 1.7
    assert bounded_reference == 480


@pytest.mark.parametrize("ending", ["。”", "！’", "？”", "？』", "。)", "。】"])
def test_sentence_ending_allows_trailing_closing_delimiters(ending: str) -> None:
    assert "incomplete_ending" not in mechanical_issues(f"正文{ending}")


def test_closing_delimiter_does_not_replace_sentence_punctuation() -> None:
    assert "incomplete_ending" in mechanical_issues("正文”")


def test_contextual_quality_detects_perspective_shift_and_unrequested_explicit_content() -> None:
    source = _perspective_prose("他", 20, paragraphs=10)
    shifted = _perspective_prose("你", 40)
    explicit = f"{_perspective_prose('他', 60)}阴茎。"

    assert "narrative_perspective_shift" in contextual_quality_issues(
        shifted,
        source_context=source,
    )
    assert "unexpected_explicit_content" in contextual_quality_issues(
        explicit,
        source_context=source,
        user_task="遭遇山中危险",
    )
    assert "unexpected_explicit_content" not in contextual_quality_issues(
        explicit,
        source_context=source,
        user_task="续写明确的阴茎性爱场景",
    )


def test_perspective_shift_uses_a_local_revision() -> None:
    source = _perspective_prose("他", 20, paragraphs=10)
    shifted = _perspective_prose("你", 40)
    corrected = _perspective_prose("他", 50)
    adapter = FakeAdapter(
        [
            _plan(3),
            shifted,
            corrected,
            _perspective_prose("他", 60, paragraphs=7),
            _perspective_prose("他", 70, paragraphs=7),
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(product_target_word_count=1500, source_context=source),
            adapter,
        )
    )

    assert result.completed
    assert result.revision_attempts == 1
    assert result.revision_acceptances == 1
    assert "narrative_perspective_shift" in result.scenes[0]["originalMechanicalIssues"]
    assert result.scenes[0]["mechanicalIssues"] == []
    revision_prompt = adapter.calls[2]["messages"][1]["content"]
    assert "narrative_perspective_shift" in revision_prompt


def test_happy_path_generates_four_scenes_and_completes_without_writes() -> None:
    adapter = FakeAdapter(
        [
            _plan(4),
            _prose(750, 0),
            _prose(750, 1),
            _prose(750, 2),
            _prose(750, 3),
        ]
    )

    result = asyncio.run(SemanticBudgetController().generate(_request(), adapter))

    assert result.completed
    assert result.generated_word_count == 3000
    assert result.within_acceptance
    assert result.provider_calls == 5
    assert result.revision_attempts == 0
    assert len(result.scenes) == 4
    assert adapter.responses == []
    assert result.events[0]["state"] == "PLANNING"
    assert result.events[-1]["state"] == "COMPLETED"


def test_invalid_plan_gets_one_structure_repair() -> None:
    adapter = FakeAdapter(
        [
            "not json",
            _plan(4),
            _prose(750, 0),
            _prose(750, 1),
            _prose(750, 2),
            _prose(750, 3),
        ]
    )

    result = asyncio.run(SemanticBudgetController().generate(_request(), adapter))

    assert result.completed
    assert result.provider_calls == 6
    assert [call["purpose"] for call in adapter.calls[:2]] == [
        "semantic_budget_plan",
        "semantic_budget_plan_repair",
    ]


def test_content_wrapper_requires_a_clean_local_revision() -> None:
    wrapped = f"<content>{_prose(730, 0)}</content>"
    adapter = FakeAdapter(
        [
            _plan(4),
            wrapped,
            _prose(750, 0),
            _prose(750, 1),
            _prose(750, 2),
            _prose(750, 3),
        ]
    )

    result = asyncio.run(SemanticBudgetController().generate(_request(), adapter))

    assert result.completed
    assert result.provider_calls == 6
    assert result.revision_attempts == 1
    assert result.revision_acceptances == 1
    assert result.scenes[0]["originalMechanicalIssues"] == ["content_wrapper", "incomplete_ending"]
    assert result.scenes[0]["mechanicalIssues"] == []


def test_quality_failure_is_never_accepted_when_revision_budget_is_zero() -> None:
    adapter = FakeAdapter([_plan(4), f"<content>{_prose(750, 0)}</content>"])

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(maximum_scene_revisions=0),
            adapter,
        )
    )

    assert result.status == "failed_quality"
    assert not result.completed
    assert result.generated_word_count == 0
    assert result.provider_calls == 2
    assert result.scenes[0]["revisionSkippedReason"] == "quality_revision_limit"


def test_provider_failure_during_revision_keeps_the_original_scene_audit() -> None:
    class GatewayTimeout(Exception):
        status_code = 504

    adapter = FakeAdapter([_plan(4), _prose(1100, 0), GatewayTimeout("hidden upstream detail")])

    result = asyncio.run(SemanticBudgetController().generate(_request(), adapter))

    assert result.status == "failed_provider"
    assert result.provider_calls == 3
    assert len(result.scenes) == 1
    assert result.scenes[0]["originalWordCount"] == 1100
    assert result.scenes[0]["revisionTriggered"] is True
    assert result.scenes[0]["revisionError"]["statusCode"] == 504
    assert "hidden upstream detail" not in json.dumps(result.error)


def test_final_revision_failure_keeps_clean_original_inside_product_range() -> None:
    class GatewayTimeout(Exception):
        status_code = 504

    adapter = FakeAdapter(
        [
            _plan(3),
            _prose(500, 0),
            _prose(500, 1),
            _prose(700, 2),
            GatewayTimeout("hidden upstream detail"),
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(product_target_word_count=1500),
            adapter,
        )
    )

    assert result.completed
    assert result.generated_word_count == 1700
    assert result.provider_calls == 5
    assert result.revision_attempts == 1
    assert result.revision_acceptances == 0
    assert result.scenes[-1]["revisionFallbackAccepted"] is True
    assert result.scenes[-1]["revisionError"]["statusCode"] == 504
    assert any(event["state"] == "REVISION_FALLBACK" for event in result.events)
    assert "hidden upstream detail" not in json.dumps(result.to_dict())


def test_final_revision_failure_stays_failed_outside_product_range() -> None:
    class GatewayTimeout(Exception):
        status_code = 504

    adapter = FakeAdapter(
        [
            _plan(3),
            _prose(500, 0),
            _prose(500, 1),
            _prose(1000, 2),
            GatewayTimeout("hidden upstream detail"),
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(product_target_word_count=1500),
            adapter,
        )
    )

    assert result.status == "failed_provider"
    assert result.generated_word_count == 1000
    assert result.scenes[-1]["revisionFallbackAccepted"] is False
    assert result.scenes[-1]["revisionError"]["statusCode"] == 504


def test_final_revision_failure_rejects_a_tiny_scene_inside_product_range() -> None:
    class GatewayTimeout(Exception):
        status_code = 504

    adapter = FakeAdapter(
        [
            _plan(3),
            _prose(600, 0),
            _prose(600, 1),
            _prose(100, 2),
            GatewayTimeout("hidden upstream detail"),
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(
                product_target_word_count=1500,
                maximum_scene_revisions=1,
            ),
            adapter,
        )
    )

    assert result.status == "failed_provider"
    assert result.generated_word_count == 1200
    assert result.scenes[-1]["originalWordCount"] == 100
    assert result.scenes[-1]["revisionFallbackAccepted"] is False


def test_duplicate_paragraphs_across_scenes_fail_final_assembly() -> None:
    repeated = _prose(750, 0)
    adapter = FakeAdapter([_plan(4), repeated, repeated, repeated, repeated])

    result = asyncio.run(SemanticBudgetController().generate(_request(), adapter))

    assert result.status == "failed_quality"
    assert result.generated_word_count == 3000
    assert "duplicate_paragraph" in result.mechanical_issues
    assert result.provider_calls == 5


def test_normal_3000_path_never_exceeds_seven_semantic_calls() -> None:
    def respond(purpose: str, metadata: Dict[str, Any]) -> str:
        desired = int(metadata.get("desiredWordCount") or 750)
        if purpose == "semantic_budget_revision":
            return _prose(desired, int(metadata["scene"]) + 10)
        scene = int(metadata.get("scene") or 1)
        return _prose(int(round(desired * 1.25)), scene)

    adapter = FakeAdapter([_plan(4), respond, respond, respond, respond, respond, respond])

    result = asyncio.run(SemanticBudgetController().generate(_request(), adapter))

    assert result.provider_calls <= 7
    assert result.revision_attempts <= 2
    assert len(result.scenes) == 4


def test_reserved_final_revision_is_used_when_middle_scene_breaks_chapter_capacity() -> None:
    adapter = FakeAdapter(
        [
            _plan(3),
            _prose(900, 0),
            _prose(760, 1),
            _prose(2700, 2),
            _prose(500, 3),
            _prose(500, 4),
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(product_target_word_count=1500),
            adapter,
        )
    )

    assert result.completed
    assert result.generated_word_count == 1760
    assert result.revision_attempts == 2
    assert result.scenes[1]["chapterUpperBoundAtRisk"] is True
    assert result.scenes[1]["revisionTriggered"] is True
    assert result.scenes[1]["revisionAccepted"] is True
    assert result.scenes[2]["revisionTriggered"] is False
    assert result.scenes[2]["revisionSkippedReason"] == "chapter_revision_limit"


def test_middle_scene_uses_reserved_revision_before_it_starves_future_scenes() -> None:
    def respond(purpose: str, metadata: Dict[str, Any]) -> str:
        desired = int(metadata.get("desiredWordCount") or 750)
        scene = int(metadata.get("scene") or 1)
        if purpose == "semantic_budget_revision":
            return _prose(desired, {1: 1, 2: 5}.get(scene, 11))
        if scene == 1:
            return _prose(1500, 0)
        if scene == 2:
            return _prose(2100, 3)
        return _prose(desired, scene * 2 + 1)

    adapter = FakeAdapter([_plan(4), *([respond] * 8)])

    result = asyncio.run(SemanticBudgetController().generate(_request(), adapter))

    assert result.scenes[0]["revisionAccepted"] is True
    assert result.scenes[1].get("chapterInternalUpperBoundAtRisk") is True
    assert result.scenes[1]["revisionTriggered"] is True
    assert result.scenes[1]["revisionAccepted"] is True
    assert result.completed
    assert all(scene["acceptedWordCount"] >= 500 for scene in result.scenes)


def test_scene_constraint_context_keeps_style_and_drops_output_protocol(tmp_path: Path) -> None:
    active = tmp_path / ".storydex" / "presets" / "active"
    active.mkdir(parents=True)
    (active / "test.preset.json").write_text(
        json.dumps(
            {
                "modules": [
                    {
                        "id": "style",
                        "title": "prose style",
                        "enabledByDefault": True,
                        "content": "Keep concrete action.\n字数：800字起步",
                    },
                    {
                        "id": "format",
                        "title": "<content>标签",
                        "enabledByDefault": True,
                        "content": "Wrap prose in <content></content>.",
                    },
                    {
                        "id": "nsfw",
                        "title": "NSFW风格",
                        "enabledByDefault": True,
                        "content": "色情描写要直白。",
                    },
                    {
                        "id": "perspective",
                        "title": "人称控制",
                        "enabledByDefault": True,
                        "content": "使用第二人称称呼主角。",
                    },
                    {
                        "id": "disabled",
                        "title": "disabled style",
                        "enabledByDefault": False,
                        "content": "Never include this.",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    context, audit = read_scene_constraint_context(tmp_path)

    assert "Keep concrete action." in context
    assert "字数" not in context
    assert "<content>" not in context
    assert "色情" not in context
    assert "第二人称" not in context
    assert "Never include this" not in context
    assert [item["moduleId"] for item in audit] == ["style"]
