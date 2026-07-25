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
    chapter_word_count_target: int | None = None,
    fragment_word_count: int | None = None,
    fragment_word_count_min: int = 100,
    fragment_word_count_max: int = 100,
    template_id: str = DEFAULT_CHAPTER_TEMPLATE_ID,
    active_file: str = "",
    prompt: str = "请续写剧情",
) -> dict[str, Any]:
    # 兼容旧的单值入参：等价于把区间上下界都设为该值
    if fragment_word_count is not None:
        fragment_word_count_min = fragment_word_count
        fragment_word_count_max = fragment_word_count
    story_generation: dict[str, Any] = {
        "fragmentCount": fragment_count,
        "fragmentWordCountMin": fragment_word_count_min,
        "fragmentWordCountMax": fragment_word_count_max,
        "chapterTemplateId": template_id,
    }
    if chapter_word_count_target is not None:
        story_generation["chapterWordCountTarget"] = chapter_word_count_target
    return get_storydex_orchestration_service().build_turn_contract(
        root,
        prompt=prompt,
        active_file=active_file,
        story_generation=story_generation,
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


def _modify_existing_contract(
    root: Path,
    *,
    fragment_count: int = 3,
    fragment_word_count_min: int = 2000,
    fragment_word_count_max: int = 2500,
    prompt: str = "第一个片段和后面的剧情不连贯，重构一下，重构后更新变量和WIKI",
) -> dict[str, Any]:
    # 模拟快速模型把"重构现有片段"判为 modify_existing 的意图帧。
    return get_storydex_orchestration_service().build_turn_contract(
        root,
        prompt=prompt,
        story_generation={
            "fragmentCount": fragment_count,
            "fragmentWordCountMin": fragment_word_count_min,
            "fragmentWordCountMax": fragment_word_count_max,
            "chapterTemplateId": DEFAULT_CHAPTER_TEMPLATE_ID,
        },
        intent_frame={
            "primary": "story_generation",
            "confidence": "high",
            "signals": ["llm_classifier"],
            "method": "llm",
            "operationType": "modify_existing",
            "complexity": "complex",
        },
    )


def test_modify_existing_intent_plans_no_new_fragments(tmp_path: Path) -> None:
    # 核心修复：重构现有文件的意图，即使含"片段/剧情"，也绝不规划新片段目标。
    contract = _modify_existing_contract(tmp_path)
    plan = contract["turnPlan"]
    assert plan["operationType"] == "modify_existing"
    assert plan["complexity"] == "complex"
    assert plan["fragmentTargets"] == []
    assert plan["nextSegmentPath"] == ""
    assert plan["isNewStory"] is False
    assert plan["requiresChapterTemplateSelection"] is False


def test_modify_existing_turn_skips_story_word_count_validation(tmp_path: Path) -> None:
    # 重构请求不该被"必须 N 段 × 字数区间"的硬校验反复打回。
    service = get_story_project_service()
    contract = _modify_existing_contract(tmp_path)
    validation = service.validate_story_generation_turn(tmp_path, contract)
    assert validation["applicable"] is False
    assert validation["passed"] is True


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


def test_project_settings_use_single_target_and_preserve_legacy_ranges(tmp_path: Path) -> None:
    service = get_story_project_service()
    defaults = service.read_project_settings(tmp_path)
    assert defaults["chapterWordCountTarget"] == 2500
    assert defaults["storyFragmentWordCountMin"] == 2500
    assert defaults["storyFragmentWordCountMax"] == 2500
    assert defaults["wordCountSettingMode"] == "target"

    legacy = service.write_project_settings(
        tmp_path,
        {"storyFragmentWordCountMin": 2000, "storyFragmentWordCountMax": 2500},
    )
    assert legacy["storyFragmentWordCountMin"] == 2000
    assert legacy["storyFragmentWordCountMax"] == 2500
    assert legacy["wordCountSettingMode"] == "legacy_range"

    preserved = service.write_project_settings(tmp_path, {"storyFragmentCount": 2})
    assert preserved["storyFragmentWordCountMin"] == 2000
    assert preserved["storyFragmentWordCountMax"] == 2500
    assert preserved["wordCountSettingMode"] == "legacy_range"

    targeted = service.write_project_settings(tmp_path, {"chapterWordCountTarget": 1800})
    assert targeted["chapterWordCountTarget"] == 1800
    assert targeted["storyFragmentWordCountMin"] == 1800
    assert targeted["storyFragmentWordCountMax"] == 1800
    assert targeted["wordCountSettingMode"] == "target"


@pytest.mark.parametrize(
    ("written_word_count", "remaining_fragment_count", "expected"),
    [
        (0, 4, 625),
        (900, 3, 533),
        (2000, 2, 312),
        (3000, 1, 312),
    ],
)
def test_fragment_reference_allocation_uses_remaining_budget_with_a_half_share_floor(
    written_word_count: int,
    remaining_fragment_count: int,
    expected: int,
) -> None:
    service = get_story_project_service()
    assert service.allocate_story_fragment_reference_word_count(
        2500,
        2500,
        written_word_count=written_word_count,
        remaining_fragment_count=remaining_fragment_count,
        total_fragment_count=4,
    ) == expected


def test_multi_fragment_contract_carries_soft_reference_lengths(tmp_path: Path) -> None:
    contract = _story_contract(
        tmp_path,
        fragment_count=4,
        chapter_word_count_target=2500,
    )
    targets = contract["turnPlan"]["fragmentTargets"]
    assert [item["referenceWordCount"] for item in targets] == [625, 625, 625, 625]
    assert all(item["referenceWordCountIsHardLimit"] is False for item in targets)


def test_correction_prompt_uses_program_measurements_without_hard_counting_commands() -> None:
    prompt = routes._story_generation_correction_prompt(
        {
            "generatedWordCount": 1600,
            "acceptWordCountMin": 1875,
            "acceptWordCountMax": 3125,
            "fragments": [],
        },
        correction_attempt=1,
    )
    assert "本章当前约 1600 字" in prompt
    assert "还需补写约 275 字" in prompt
    assert "场景细节" in prompt
    assert "人物心理" in prompt
    assert "推动情节的对话" in prompt
    assert '"additionalWordCountNeeded":275' in prompt
    assert "1875-3125" not in prompt
    assert "不要自行估算字数" not in prompt
    assert "未通过不得结束" not in prompt
    assert "must fall within" not in prompt.lower()


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
def test_near_target_story_fragment_is_accepted_within_tolerance(
    tmp_path: Path,
    actual_word_count: int,
) -> None:
    # 字数校验改为宽容带后，紧挨目标（100 字）的片段应直接放行落盘，
    # 避免模型为凑到精确字数反复重写导致的“抠字数”死循环。
    service = get_story_project_service()
    contract = _story_contract(tmp_path, fragment_word_count=100)
    target_path = contract["turnPlan"]["fragmentTargets"][0]["path"]
    result = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "字" * actual_word_count}]},
        generation_contract=contract,
    )
    assert result["ok"] is True
    assert result["fragments"][0]["wordCountStatus"] == "passed"
    assert (tmp_path / target_path).exists()


@pytest.mark.parametrize("actual_word_count", [10])
def test_story_fragment_far_outside_band_is_rejected_before_any_file_write(
    tmp_path: Path,
    actual_word_count: int,
) -> None:
    # 偏短内容低于目标 100 的章级放行带下界 75 时拦截落盘。
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
    assert result["fragments"][0]["targetWordCountMin"] == 100
    assert result["fragments"][0]["targetWordCountMax"] == 100
    assert result["fragments"][0]["wordCountStatus"] == "passed"
    assert result["fragments"][0]["wordCountAlgorithm"] == STORY_WORD_COUNT_ALGORITHM
    assert validation["passed"] is True
    assert validation["fragments"][0]["generatedWordCount"] == 100


@pytest.mark.parametrize("actual_word_count", [2000, 2250, 2500])
def test_story_fragment_within_range_is_accepted(
    tmp_path: Path,
    actual_word_count: int,
) -> None:
    service = get_story_project_service()
    contract = _story_contract(tmp_path, fragment_word_count_min=2000, fragment_word_count_max=2500)
    plan = contract["turnPlan"]
    assert plan["fragmentWordCountMin"] == 2000
    assert plan["fragmentWordCountMax"] == 2500
    assert plan["wordCountPolicy"]["mode"] == "range"
    result = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "字" * actual_word_count}]},
        generation_contract=contract,
    )
    validation = service.validate_story_generation_turn(tmp_path, contract)
    assert result["ok"] is True
    assert result["fragments"][0]["wordCountStatus"] == "passed"
    assert result["fragments"][0]["targetWordCountMin"] == 2000
    assert result["fragments"][0]["targetWordCountMax"] == 2500
    assert validation["passed"] is True


@pytest.mark.parametrize("actual_word_count", [1999, 2501])
def test_story_fragment_near_range_edges_is_accepted(
    tmp_path: Path,
    actual_word_count: int,
) -> None:
    # 目标区间 2000-2500 只是建议值：贴着边缘（1999/2501）不应被当成失败反复重写，
    # 落在宽容带内即视为达标并放行落盘。
    service = get_story_project_service()
    contract = _story_contract(tmp_path, fragment_word_count_min=2000, fragment_word_count_max=2500)
    target_path = contract["turnPlan"]["fragmentTargets"][0]["path"]
    result = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "字" * actual_word_count}]},
        generation_contract=contract,
    )
    assert result["ok"] is True
    assert result["fragments"][0]["wordCountStatus"] == "passed"
    assert (tmp_path / target_path).exists()


@pytest.mark.parametrize(
    ("actual_word_count", "expected_passed", "expected_over_budget"),
    [
        (1874, False, False),
        (1875, True, False),
        (2500, True, False),
        (3125, True, False),
        (3126, True, True),
    ],
)
def test_chapter_target_uses_one_acceptance_band_before_and_after_write(
    tmp_path: Path,
    actual_word_count: int,
    expected_passed: bool,
    expected_over_budget: bool,
) -> None:
    service = get_story_project_service()
    contract = _story_contract(tmp_path, chapter_word_count_target=2500)
    target_path = contract["turnPlan"]["fragmentTargets"][0]["path"]
    result = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "字" * actual_word_count}]},
        generation_contract=contract,
    )
    validation = service.validate_story_generation_turn(tmp_path, contract)
    preflight = result["wordCountValidation"]

    assert result["ok"] is expected_passed
    assert preflight["passed"] is expected_passed
    assert preflight["generatedWordCount"] == actual_word_count
    assert preflight["chapterWordCountTarget"] == 2500
    assert (preflight["acceptWordCountMin"], preflight["acceptWordCountMax"]) == (1875, 3125)
    assert preflight["overBudget"] is expected_over_budget
    assert validation["passed"] is expected_passed
    assert validation["overBudget"] is expected_over_budget
    assert (tmp_path / target_path).exists() is expected_passed


@pytest.mark.parametrize(
    ("actual_word_count", "expected_passed", "expected_over_budget"),
    [
        (1499, False, False),
        (1500, True, False),
        (3125, True, False),
        (3126, True, True),
    ],
)
def test_legacy_range_settings_use_chapter_scope_acceptance_band(
    tmp_path: Path,
    actual_word_count: int,
    expected_passed: bool,
    expected_over_budget: bool,
) -> None:
    service = get_story_project_service()
    contract = _story_contract(
        tmp_path,
        fragment_word_count_min=2000,
        fragment_word_count_max=2500,
    )
    plan = contract["turnPlan"]
    assert plan["wordCountPolicy"]["scope"] == "chapter"
    assert plan["wordCountPolicy"]["mode"] == "range"

    result = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "字" * actual_word_count}]},
        generation_contract=contract,
    )
    validation = service.validate_story_generation_turn(tmp_path, contract)
    preflight = result["wordCountValidation"]
    assert result["ok"] is expected_passed
    assert (preflight["targetWordCountMin"], preflight["targetWordCountMax"]) == (2000, 2500)
    assert (preflight["acceptWordCountMin"], preflight["acceptWordCountMax"]) == (1500, 3125)
    assert preflight["overBudget"] is expected_over_budget
    assert validation["passed"] is expected_passed
    assert validation["overBudget"] is expected_over_budget


def test_multi_fragment_chapter_is_validated_by_aggregate_word_count(tmp_path: Path) -> None:
    service = get_story_project_service()
    contract = _story_contract(
        tmp_path,
        fragment_count=4,
        chapter_word_count_target=2500,
    )
    result = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "字" * 700} for _ in range(4)]},
        generation_contract=contract,
    )
    validation = service.validate_story_generation_turn(tmp_path, contract)

    assert result["ok"] is True
    assert result["wordCountValidation"]["generatedWordCount"] == 2800
    assert all(item["wordCountStatus"] == "passed" for item in result["fragments"])
    assert validation["passed"] is True
    assert validation["generatedWordCount"] == 2800


def test_one_correction_can_land_below_minimum_and_preserves_budget_status(tmp_path: Path) -> None:
    service = get_story_project_service()
    contract = _story_contract(tmp_path, chapter_word_count_target=2500)
    target_path = contract["turnPlan"]["fragmentTargets"][0]["path"]

    rejected = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "甲" * 1600}]},
        generation_contract=contract,
    )
    assert rejected["ok"] is False
    corrected_contract = routes._rebuild_story_generation_contract_for_correction(
        tmp_path,
        contract,
        rejected["wordCountValidation"],
    )
    assert "allowBelowMinimumAfterCorrection" not in contract["turnPlan"]["wordCountPolicy"]
    assert corrected_contract["turnPlan"]["wordCountPolicy"]["allowBelowMinimumAfterCorrection"] is True

    corrected = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "乙" * 1700}]},
        generation_contract=corrected_contract,
    )
    validation = service.validate_story_generation_turn(tmp_path, corrected_contract)
    assert corrected["ok"] is True
    assert corrected["wordCountValidation"]["passed"] is True
    assert corrected["wordCountValidation"]["belowBudget"] is True
    assert corrected["wordCountValidation"]["correctionApplied"] is True
    assert validation["passed"] is True
    assert validation["generatedWordCount"] == 1700
    assert validation["belowBudget"] is True
    assert validation["correctionApplied"] is True
    assert service.count_story_file_words(tmp_path / target_path) == 1700


def test_contract_without_scope_keeps_legacy_per_fragment_gate(tmp_path: Path) -> None:
    service = get_story_project_service()
    contract = _story_contract(
        tmp_path,
        fragment_count=4,
        chapter_word_count_target=2500,
    )
    contract["turnPlan"]["wordCountPolicy"].pop("scope", None)
    result = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "字" * 700} for _ in range(4)]},
        generation_contract=contract,
    )

    assert result["ok"] is False
    assert result["wordCountValidation"]["wordCountScope"] == "fragment"
    assert all(item["status"] == "failed" for item in result["wordCountValidation"]["fragments"])


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


def test_append_correction_can_land_once_below_minimum_without_disabling_duplicate_guard(
    tmp_path: Path,
) -> None:
    service = get_story_project_service()
    initial = _story_contract(
        tmp_path,
        chapter_word_count_target=2500,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
    )
    target_path = initial["turnPlan"]["fragmentTargets"][0]["path"]
    target = tmp_path / target_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("甲" * 2200, encoding="utf-8")

    continuation = _story_contract(
        tmp_path,
        chapter_word_count_target=2500,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
        active_file=target_path,
    )
    rejected = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "乙" * 1000}]},
        generation_contract=continuation,
    )
    assert rejected["ok"] is False
    assert service.count_story_file_words(target) == 2200

    corrected_contract = routes._rebuild_story_generation_contract_for_correction(
        tmp_path,
        continuation,
        rejected["wordCountValidation"],
    )
    corrected_target = corrected_contract["turnPlan"]["fragmentTargets"][0]
    assert corrected_target["baselineWordCount"] == 2200
    assert corrected_contract["turnPlan"]["wordCountPolicy"]["allowBelowMinimumAfterCorrection"] is True

    corrected = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "丙" * 1200}]},
        generation_contract=corrected_contract,
    )
    validation = service.validate_story_generation_turn(tmp_path, corrected_contract)
    duplicate = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "丁" * 1200}]},
        generation_contract=corrected_contract,
    )
    assert corrected["ok"] is True
    assert validation["passed"] is True
    assert validation["belowBudget"] is True
    assert validation["correctionApplied"] is True
    assert duplicate["ok"] is False
    assert duplicate["wordCountValidation"]["fragments"][0]["baselineMatches"] is False
    assert service.count_story_file_words(target) == 3400


def test_append_correction_rebuilds_baseline_without_disabling_duplicate_guard(tmp_path: Path) -> None:
    service = get_story_project_service()
    initial = _story_contract(
        tmp_path,
        fragment_word_count=2200,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
    )
    target_path = initial["turnPlan"]["fragmentTargets"][0]["path"]
    target = tmp_path / target_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("甲" * 2200, encoding="utf-8")

    continuation = _story_contract(
        tmp_path,
        fragment_word_count_min=2000,
        fragment_word_count_max=2500,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
        active_file=target_path,
    )
    # W1-1 specifically covers the serialized pre-W1 contract path. New
    # contracts use a unified chapter-level acceptance band and would accept
    # 1999 directly.
    continuation["turnPlan"]["wordCountPolicy"].pop("scope", None)
    assert continuation["turnPlan"]["fragmentTargets"][0]["baselineWordCount"] == 2200

    first_result = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "乙" * 1999}]},
        generation_contract=continuation,
    )
    first_validation = service.validate_story_generation_turn(tmp_path, continuation)
    assert first_result["ok"] is True
    assert first_validation["passed"] is False
    assert service.count_story_file_words(target) == 4199

    corrected_contract = routes._rebuild_story_generation_contract_for_correction(
        tmp_path,
        continuation,
        first_validation,
    )
    corrected_target = corrected_contract["turnPlan"]["fragmentTargets"][0]
    assert corrected_target["baselineWordCount"] == 4199
    assert continuation["turnPlan"]["fragmentTargets"][0]["baselineWordCount"] == 2200

    corrected = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "丙" * 2200}]},
        generation_contract=corrected_contract,
    )
    corrected_validation = service.validate_story_generation_turn(tmp_path, corrected_contract)
    duplicate = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "丁" * 2200}]},
        generation_contract=corrected_contract,
    )
    final_text = target.read_text(encoding="utf-8")
    assert corrected["ok"] is True
    assert corrected_validation["passed"] is True
    assert duplicate["ok"] is False
    assert duplicate["wordCountValidation"]["fragments"][0]["baselineMatches"] is False
    assert service.count_story_file_words(target) == 6399
    assert final_text.count("乙") == 1999
    assert final_text.count("丙") == 2200


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


def _chapter_validation_payload(
    generated_word_count: int,
    *,
    passed: bool,
    below_budget: bool,
    over_budget: bool = False,
) -> dict[str, Any]:
    return {
        "_type": "StoryGenerationValidation",
        "_version": 1,
        "applicable": True,
        "passed": passed,
        "status": "success" if passed else "error",
        "algorithm": STORY_WORD_COUNT_ALGORITHM,
        "countingRule": "count every non-whitespace Unicode character",
        "exact": True,
        "wordCountScope": "chapter",
        "fragmentCount": 1,
        "generatedWordCount": generated_word_count,
        "chapterWordCountTarget": 2500,
        "targetWordCountMin": 2500,
        "targetWordCountMax": 2500,
        "acceptWordCountMin": 1875,
        "acceptWordCountMax": 3125,
        "chapterContentMode": "multi_fragment",
        "structurePassed": True,
        "belowBudget": below_budget,
        "overBudget": over_budget,
        "fragments": [
            {
                "order": 1,
                "path": "chapters/第1章/001.md",
                "exists": True,
                "writeMode": "replace",
                "baselineWordCount": 0,
                "generatedWordCount": generated_word_count,
                "status": "failed" if below_budget else "passed",
            }
        ],
        "message": "passed" if passed else "needs correction",
    }


def _run_story_generation_sequence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    tool_outcomes: list[bool | None],
    validation_results: list[dict[str, Any]],
) -> dict[str, Any]:
    class RuntimeService:
        def __init__(self, outcomes: list[bool | None]) -> None:
            self.calls = 0
            self.outcomes = outcomes
            self.prompts: list[str] = []
            self.contracts: list[dict[str, Any]] = []

        async def stream_events(self, **kwargs: Any):
            self.calls += 1
            self.prompts.append(str(kwargs.get("prompt") or ""))
            self.contracts.append(kwargs.get("turn_contract") or {})
            yield "AgentStarted", {
                "_type": "AgentStarted",
                "_version": 1,
                "llmProvider": "test-provider",
                "llmModel": "test-model",
            }
            yield "TextChunk", {"_type": "TextChunk", "_version": 1, "content": f"attempt-{self.calls}"}
            outcome = self.outcomes[self.calls - 1] if self.calls <= len(self.outcomes) else None
            if outcome is not None:
                yield "ToolDone", {
                    "_type": "ToolDone",
                    "_version": 1,
                    "tool_name": "StorydexApplyStoryIncrement",
                    "tool_call_id": f"write-{self.calls}",
                    "is_error": not outcome,
                }
            yield "AgentCompleted", {"_type": "AgentCompleted", "_version": 1, "total_tokens": 1}

    class ProjectService:
        def __init__(self, results: list[dict[str, Any]]) -> None:
            self.validations = 0
            self.results = results

        def read_project_settings(self, _root: Path) -> dict[str, Any]:
            return {"agentCommitPromptEnabled": False}

        def validate_story_generation_turn(self, _root: Path, _contract: dict[str, Any]) -> dict[str, Any]:
            index = min(self.validations, len(self.results) - 1)
            self.validations += 1
            return json.loads(json.dumps(self.results[index], ensure_ascii=False))

    class GitService:
        def finish_turn(self, _snapshot: AgentGitSnapshot, **_kwargs: Any) -> dict[str, Any]:
            return {"_type": "GitAutoCommit", "status": "info", "created": False}

    class CalibrationService:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def record_generation_result(self, _root: Path, **kwargs: Any) -> bool:
            self.calls.append(kwargs)
            return True

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

    runtime = RuntimeService(tool_outcomes)
    project = ProjectService(validation_results)
    calibration = CalibrationService()
    handle = Handle(runtime)
    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: runtime)
    monkeypatch.setattr(routes, "story_project_service", project)
    monkeypatch.setattr(routes, "story_length_calibration_service", calibration)
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
        "turnPlan": {
            "fragmentCount": 1,
            "fragmentWordCount": 2500,
            "chapterWordCountTarget": 2500,
            "chapterContentMode": "multi_fragment",
            "wordCountPolicy": {"scope": "chapter", "target": 2500},
            "fragmentTargets": [
                {
                    "order": 1,
                    "path": "chapters/第1章/001.md",
                    "writeMode": "replace",
                    "baselineWordCount": 0,
                }
            ],
        },
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
                story_generation={"fragmentCount": 1, "chapterWordCountTarget": 2500},
                turn_contract=turn_contract,
                git_snapshot=AgentGitSnapshot(workspace_root=tmp_path, available=False),
                cancellation_token=routes._CancellationToken(),
                execution_handle=handle,
            )
        ]

    return {
        "packets": asyncio.run(collect()),
        "runtime": runtime,
        "project": project,
        "calibration": calibration,
        "handle": handle,
    }


def test_below_budget_gets_one_correction_and_still_short_write_can_finish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _run_story_generation_sequence(
        monkeypatch,
        tmp_path,
        tool_outcomes=[False, True],
        validation_results=[
            _chapter_validation_payload(1600, passed=False, below_budget=True),
            _chapter_validation_payload(1700, passed=False, below_budget=True),
        ],
    )
    packets = result["packets"]
    event_names = [name for name, _payload in packets]
    validations = [payload for name, payload in packets if name == "StoryGenerationValidation"]
    continuations = [payload for name, payload in packets if name == "ContinuationStarted"]
    assert result["runtime"].calls == 2
    assert result["project"].validations == 2
    assert [item["passed"] for item in validations] == [False, True]
    assert validations[1]["belowBudget"] is True
    assert validations[1]["correctionApplied"] is True
    assert validations[1]["maximumCorrectionAttempts"] == 1
    assert len(continuations) == 1
    assert continuations[0]["continuationMode"] == "story_generation_correction"
    assert continuations[0]["maximumCorrectionAttempts"] == 1
    assert "本章当前约 1600 字" in result["runtime"].prompts[1]
    assert "还需补写约 275 字" in result["runtime"].prompts[1]
    correction_policy = result["runtime"].contracts[1]["turnPlan"]["wordCountPolicy"]
    assert correction_policy["allowBelowMinimumAfterCorrection"] is True
    assert event_names.count("AgentCompleted") == 1
    assert "AgentError" not in event_names
    assert result["handle"].finalize_calls == 1
    assert result["handle"].runtime_calls_at_finalize == 2
    assert len(result["calibration"].calls) == 1
    assert result["calibration"].calls[0]["provider"] == "test-provider"
    assert result["calibration"].calls[0]["model"] == "test-model"


def test_over_budget_finishes_without_correction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _run_story_generation_sequence(
        monkeypatch,
        tmp_path,
        tool_outcomes=[True],
        validation_results=[
            _chapter_validation_payload(3300, passed=True, below_budget=False, over_budget=True),
        ],
    )
    packets = result["packets"]
    validations = [payload for name, payload in packets if name == "StoryGenerationValidation"]
    assert result["runtime"].calls == 1
    assert len(validations) == 1
    assert validations[0]["passed"] is True
    assert validations[0]["overBudget"] is True
    assert validations[0]["correctionApplied"] is False
    assert not [payload for name, payload in packets if name == "ContinuationStarted"]
    assert not [payload for name, payload in packets if name == "AgentError"]


def test_correction_segment_cannot_reuse_previous_segment_successful_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _run_story_generation_sequence(
        monkeypatch,
        tmp_path,
        tool_outcomes=[True, None],
        validation_results=[
            _chapter_validation_payload(1600, passed=False, below_budget=True),
            _chapter_validation_payload(1700, passed=False, below_budget=True),
        ],
    )
    packets = result["packets"]
    validations = [payload for name, payload in packets if name == "StoryGenerationValidation"]
    errors = [payload for name, payload in packets if name == "AgentError"]
    assert result["runtime"].calls == 2
    assert [item["passed"] for item in validations] == [False, False]
    assert validations[0]["writeToolApplied"] is True
    assert validations[1]["writeToolApplied"] is False
    assert validations[1]["correctionApplied"] is False
    assert len(errors) == 1
    assert errors[0]["error_type"] == "StoryGenerationValidationFailed"
    assert len([payload for name, payload in packets if name == "ContinuationStarted"]) == 1


def test_only_chapter_below_budget_requests_length_correction() -> None:
    assert routes._story_generation_needs_length_correction(
        {"wordCountScope": "chapter", "belowBudget": True, "overBudget": False}
    )
    assert not routes._story_generation_needs_length_correction(
        {"wordCountScope": "chapter", "belowBudget": False, "overBudget": True}
    )
    assert not routes._story_generation_needs_length_correction(
        {"wordCountScope": "fragment", "belowBudget": True, "overBudget": False}
    )
