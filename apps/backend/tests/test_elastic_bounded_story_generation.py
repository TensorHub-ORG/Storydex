from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

from core.config import FEATURE_FLAG_DEFAULTS
from services.story_bounded_generation_service import BoundedStoryGeneration
from services.story_elastic_manuscript_service import (
    ELASTIC_DRAFT_TOOL_NAME,
    ELASTIC_LENGTH_CONTROL_STRATEGY,
    ELASTIC_REPAIR_TOOL_NAME,
)
from services.story_generation_pipeline import CANDIDATE_SOURCE_REVISION, StoryGenerationPipeline


TARGET = 100


def _unique_chars(start: int, count: int) -> str:
    return "".join(chr(0x5200 + start + offset) for offset in range(count))


def _paragraphs(*lengths: int) -> tuple[str, list[str]]:
    parts: list[str] = []
    offset = 0
    for length in lengths:
        parts.append(_unique_chars(offset, length - 1) + "。")
        offset += length - 1
    return "\n\n".join(parts), parts


def _contract(
    *,
    precise: bool,
    natural_baseline_completion_tokens: int | None = None,
) -> Dict[str, Any]:
    contract: Dict[str, Any] = {
        "intentFrame": {"primary": "story_generation", "operationType": "create_new"},
        "turnPlan": {
            "fragmentCount": 1,
            "chapterWordCountTarget": TARGET,
            "operationType": "create_new",
            "authoritativeChapterPath": "chapters/first",
            "wordCountPolicy": {
                "scope": "chapter",
                "target": TARGET,
                "retainedWordCount": 0,
                "precision": {"enabled": precise},
            },
            "fragmentTargets": [
                {
                    "order": 1,
                    "path": "chapters/first/001.md",
                    "writeMode": "replace",
                    "baselineWordCount": 0,
                }
            ],
        },
        "contextAssembly": {"promptBlocks": []},
    }
    if natural_baseline_completion_tokens is not None:
        contract["turnPlan"]["wordCountPolicy"]["naturalBaselineCompletionTokens"] = (
            natural_baseline_completion_tokens
        )
    return contract


def _elastic_draft(text: str) -> Dict[str, Any]:
    paragraphs = text.split("\n\n")
    return {
        "version": 1,
        "canonicalText": text,
        "compactReplacements": [],
        "expansionModules": [],
        "endingHook": paragraphs[-1][-8:],
    }


class _ProjectService:
    def __init__(self) -> None:
        self.writes: list[Dict[str, Any]] = []

    @staticmethod
    def agent_temp_root(root: Path) -> Path:
        return Path(root) / ".storydex" / ".agent" / "temp"

    def apply_story_generation_increment(
        self,
        _root: Path,
        payload: Dict[str, Any],
        *,
        generation_contract: Dict[str, Any],
    ) -> Dict[str, Any]:
        self.writes.append({"payload": payload, "contract": generation_contract})
        return {"ok": True}


class _DraftAdapter:
    provider_retries = 0
    last_cap_applied = False
    last_completion_tokens = 420
    last_usage = {"outputTokens": 420, "source": "provider_response"}

    def __init__(self, response: Dict[str, Any]) -> None:
        self.response = response
        self.calls: list[Dict[str, Any]] = []

    async def complete_tool_call(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


class _RevisionAdapter:
    provider_retries = 0
    last_cap_applied = True
    last_completion_tokens = 240
    last_usage = {"outputTokens": 240, "source": "provider_response"}

    def __init__(self, response: Dict[str, Any] | Exception | None = None) -> None:
        self.response = response
        self.calls: list[Dict[str, Any]] = []

    async def complete_tool_call(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return dict(self.response or {})


class _ForbiddenController:
    async def revise(self, *_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        raise AssertionError("elastic path must not call the legacy precision controller")


def _runner(
    draft_response: Dict[str, Any],
    *,
    revision_response: Dict[str, Any] | Exception | None = None,
) -> tuple[BoundedStoryGeneration, _ProjectService, _DraftAdapter, _RevisionAdapter]:
    project = _ProjectService()
    draft = _DraftAdapter(draft_response)
    revision = _RevisionAdapter(revision_response)
    runner = BoundedStoryGeneration(
        adapter=draft,
        revision_adapter=revision,
        pipeline=StoryGenerationPipeline(project),
        controller=_ForbiddenController(),
        elastic_enabled=True,
    )
    return runner, project, draft, revision


def test_elastic_feature_flag_stays_disabled_until_live_acceptance_passes() -> None:
    assert FEATURE_FLAG_DEFAULTS["ELASTIC_STORY_MANUSCRIPT_ENABLED"] is False


def test_normal_mode_uses_one_required_tool_call_and_writes_outside_band_text_once(
    tmp_path: Path,
) -> None:
    canonical, _parts = _paragraphs(20, 30, 30, 30, 20, 20)
    runner, project, draft, revision = _runner(_elastic_draft(canonical))

    result = asyncio.run(
        runner.run(
            tmp_path,
            trace_id="elastic-normal",
            turn_contract=_contract(precise=False),
            prompt="write the next chapter",
        )
    )

    assert len(draft.calls) == 1
    assert draft.calls[0]["tool_name"] == ELASTIC_DRAFT_TOOL_NAME
    assert revision.calls == []
    assert len(project.writes) == 1
    assert result["committed"] is True
    assert result["callAccounting"]["logicalStoryCalls"] == 1
    assert result["contractViolations"] == []
    assert result["lengthControlStrategy"] == ELASTIC_LENGTH_CONTROL_STRATEGY
    assert result["canonicalWordCount"] == 150
    assert result["selection"]["finalWordCount"] == 150
    assert result["normalBandPassed"] is False
    assert result["precisionAchieved"] is None
    assert result["selectedEditIds"] == []
    assert result["rejectedEditIds"] == []
    assert result["rejectedEditReasonCounts"] == {}
    assert result["evaluatedCombinationCount"] == 1
    assert result["lengthFallbackReason"] == "no_valid_edits"
    assert result["generatedOverheadRatio"] is None


def test_precise_mode_invalid_repair_keeps_the_first_text_and_still_writes_once(
    tmp_path: Path,
) -> None:
    canonical, parts = _paragraphs(20, 20, 20, 20, 20, 20, 20)
    bad_repair = {
        "version": 1,
        "operations": [
            {
                "id": "bad-ending",
                "op": "replace_paragraph_range",
                "sourceStart": parts[-1][:4],
                "sourceEnd": parts[-1][-4:],
                "replacementText": "结尾被改写。",
            }
        ],
    }
    runner, project, draft, revision = _runner(
        _elastic_draft(canonical),
        revision_response=bad_repair,
    )

    result = asyncio.run(
        runner.run(
            tmp_path,
            trace_id="elastic-repair-rejected",
            turn_contract=_contract(precise=True),
            prompt="write the next chapter precisely",
        )
    )

    assert len(draft.calls) == 1
    assert len(revision.calls) == 1
    assert revision.calls[0]["tool_name"] == ELASTIC_REPAIR_TOOL_NAME
    assert 512 <= revision.calls[0]["max_completion_tokens"] <= 8192
    assert len(project.writes) == 1
    assert result["callAccounting"]["logicalStoryCalls"] == 2
    assert result["callAccounting"]["lengthRevisionCalls"] == 1
    assert result["callAccounting"]["transportRetries"] == 0
    assert result["contractViolations"] == []
    assert result["selection"]["finalWordCount"] == 140
    assert result["precisionAchieved"] is False
    assert result["lengthFallbackReason"] == "repair_failed"
    assert result["revisionOutcome"]["qualityIssues"] == [
        "repair_touches_protected_region"
    ]


def test_precise_mode_accepts_one_local_repair_that_hits_the_precision_band(
    tmp_path: Path,
) -> None:
    canonical, parts = _paragraphs(20, 20, 20, 20, 20, 20)
    source = parts[2][2:16]
    repair = {
        "version": 1,
        "operations": [
            {
                "id": "repair-middle",
                "op": "replace_paragraph_range",
                "sourceStart": source[:4],
                "sourceEnd": source[-4:],
                "replacementText": _unique_chars(1600, 4),
            }
        ],
    }
    runner, project, _draft, revision = _runner(
        _elastic_draft(canonical),
        revision_response=repair,
    )

    result = asyncio.run(
        runner.run(
            tmp_path,
            trace_id="elastic-repair-accepted",
            turn_contract=_contract(precise=True),
            prompt="write the next chapter precisely",
        )
    )

    assert len(revision.calls) == 1
    assert len(project.writes) == 1
    assert result["selection"]["source"] == CANDIDATE_SOURCE_REVISION
    assert result["selection"]["finalWordCount"] == 110
    assert result["precisionAchieved"] is True
    assert result["normalBandPassed"] is True
    assert result["selectedEditIds"] == ["repair-middle"]
    assert result["lengthFallbackReason"] == "repair_in_band"


def test_precise_mode_in_band_first_call_never_calls_repair(tmp_path: Path) -> None:
    canonical, _parts = _paragraphs(20, 20, 20, 20, 20)
    runner, project, _draft, revision = _runner(
        _elastic_draft(canonical),
        revision_response=AssertionError("repair must not run"),
    )

    result = asyncio.run(
        runner.run(
            tmp_path,
            trace_id="elastic-precise-first-hit",
            turn_contract=_contract(precise=True),
            prompt="write the next chapter precisely",
        )
    )

    assert revision.calls == []
    assert len(project.writes) == 1
    assert result["callAccounting"]["logicalStoryCalls"] == 1
    assert result["precisionAchieved"] is True
    assert result["contractViolations"] == []


def test_precise_mode_provider_error_keeps_the_first_call_text(tmp_path: Path) -> None:
    canonical, _parts = _paragraphs(20, 20, 20, 20, 20, 20, 20)
    runner, project, _draft, revision = _runner(
        _elastic_draft(canonical),
        revision_response=RuntimeError("provider unavailable"),
    )

    result = asyncio.run(
        runner.run(
            tmp_path,
            trace_id="elastic-repair-provider-error",
            turn_contract=_contract(precise=True),
            prompt="write the next chapter precisely",
        )
    )

    assert len(revision.calls) == 1
    assert len(project.writes) == 1
    assert result["selection"]["finalWordCount"] == 140
    assert result["lengthFallbackReason"] == "repair_failed"
    assert result["callAccounting"]["logicalStoryCalls"] == 2
    assert result["contractViolations"] == []


def test_generated_overhead_ratio_uses_provider_tokens_only_with_a_real_baseline(
    tmp_path: Path,
) -> None:
    canonical, _parts = _paragraphs(20, 20, 20, 20, 20)
    runner, _project, _draft, _revision = _runner(_elastic_draft(canonical))

    result = asyncio.run(
        runner.run(
            tmp_path,
            trace_id="elastic-token-overhead",
            turn_contract=_contract(
                precise=False,
                natural_baseline_completion_tokens=350,
            ),
            prompt="write the next chapter",
        )
    )

    assert result["generatedOverheadRatio"] == 1.2


def test_precise_mode_can_adopt_a_normal_band_repair_with_fifty_percent_improvement(
    tmp_path: Path,
) -> None:
    canonical, parts = _paragraphs(20, 20, 20, 20, 20, 20)
    source = parts[2][2:12]
    repair = {
        "version": 1,
        "operations": [
            {
                "id": "half-precision-gap",
                "op": "replace_paragraph_range",
                "sourceStart": source[:4],
                "sourceEnd": source[-4:],
                "replacementText": _unique_chars(1700, 5),
            }
        ],
    }
    runner, _project, _draft, _revision = _runner(
        _elastic_draft(canonical),
        revision_response=repair,
    )

    result = asyncio.run(
        runner.run(
            tmp_path,
            trace_id="elastic-normal-recovery",
            turn_contract=_contract(precise=True),
            prompt="write the next chapter precisely",
        )
    )

    assert result["selection"]["source"] == CANDIDATE_SOURCE_REVISION
    assert result["selection"]["finalWordCount"] == 115
    assert result["normalBandPassed"] is True
    assert result["precisionAchieved"] is False
    assert result["selectedEditIds"] == ["half-precision-gap"]
    assert result["lengthFallbackReason"] == "repair_normal_recovery"
