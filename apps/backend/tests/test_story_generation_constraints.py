from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from api import routes_agent as routes
from api.routes_file import StoryProjectSettingsResponse as FileStoryProjectSettingsResponse
from api.routes_story import StoryProjectSettingsResponse as StoryStoryProjectSettingsResponse
from services.agent_git_autocommit_service import AgentGitSnapshot
from services import story_project_service as story_project_module
from services.story_project_service import (
    DEFAULT_CHAPTER_TEMPLATE_ID,
    SINGLE_FILE_CHAPTER_TEMPLATE_ID,
    get_story_project_service,
)
from services.story_length_calibration_service import StoryLengthCalibrationService
from services.story_prose_quality import extract_story_prose
from services.story_word_count_service import (
    STORY_OVER_BUDGET_KEEP_MESSAGE,
    STORY_UNDER_BUDGET_KEEP_MESSAGE,
    STORY_WORD_COUNT_ALGORITHM,
    STORY_WORD_COUNT_RULE,
    count_story_text_paragraphs,
    count_story_text_words,
    strip_non_story_wrappers,
)
from services.storydex_agent_tools import StorydexApplyStoryIncrementTool
from services.storydex_coomi_runtime_tools import StorydexEditTool, StorydexWriteTool
from services.storydex_orchestration_service import get_storydex_orchestration_service
from storage.workspace_io import WorkspaceIO


@pytest.fixture(autouse=True)
def legacy_story_length_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORY_LENGTH_TIER_ENABLED", "0")


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


def test_story_discussion_contract_is_read_only_end_to_end(tmp_path: Path) -> None:
    contract = get_storydex_orchestration_service().build_turn_contract(
        tmp_path,
        prompt="讨论下一章怎么安排，只分析方案，不要写入或修改任何文件。",
        active_file="chapters/第一章/001.md",
        story_generation={"fragmentCount": 2, "chapterWordCountTarget": 3000},
        intent_frame={
            "primary": "story_generation",
            "confidence": "high",
            "signals": ["llm_classifier"],
            "method": "llm",
            "decision": "decided",
            "effect": "respond_only",
            "artifact": "plot_plan",
            "targetScope": "next_chapter",
            "explicitConstraints": ["no_project_write"],
            "ambiguities": [],
            "evidence": ["只分析方案", "不要写入或修改任何文件"],
            "canWrite": False,
            "operationType": "inquiry",
            "complexity": "simple",
        },
    )

    plan = contract["turnPlan"]
    execution = contract["executionPolicy"]
    assert plan["operationType"] == "inquiry"
    assert plan["fragmentTargets"] == []
    assert plan["isNewStory"] is False
    assert plan["chapterAction"] == ""
    assert plan["authoritativeFragmentPaths"] == []
    assert "generationControl" not in plan
    assert execution["directFileWrites"] is False
    assert execution["localGitAutoCommit"] is False
    assert execution["allowedWriteRoots"] == []

    write = StorydexWriteTool(workspace_root=tmp_path, turn_contract=contract).run(
        {"file_path": "chapters/第一章/002.md", "content": "不应写入"}
    )
    assert write.success is False
    assert not (tmp_path / "chapters" / "第一章" / "002.md").exists()


def test_character_write_contract_cannot_escape_its_asset_root(tmp_path: Path) -> None:
    contract = {
        "intentFrame": {
            "primary": "character_work",
            "operationType": "create_new",
            "decision": "decided",
            "effect": "create",
            "canWrite": True,
        },
        "executionPolicy": {
            "directFileWrites": True,
            "allowedWriteRoots": [".storydex/characters/"],
        },
    }
    tool = StorydexWriteTool(workspace_root=tmp_path, turn_contract=contract)

    allowed = tool.run({"file_path": ".storydex/characters/反派.md", "content": "角色卡"})
    denied = tool.run({"file_path": ".storydex/worldbook/魔法.md", "content": "越权"})
    structured_bypass = StorydexApplyStoryIncrementTool(
        workspace_root=tmp_path,
        turn_contract=contract,
    ).run({"fragments": [{"path": "chapters/越权.md", "text": "越权"}]})

    assert allowed.success is True
    assert denied.success is False
    assert structured_bypass.success is False
    assert (tmp_path / ".storydex" / "characters" / "反派.md").is_file()
    assert not (tmp_path / ".storydex" / "worldbook" / "魔法.md").exists()


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


def test_wrapper_blocks_are_excluded_from_the_word_count() -> None:
    # 摘要和角色留言随正文一起交付，但它们不是正文：计入既会让偏短正文假性达标，
    # 也能被用来凑字数绕过验收。
    body = "正" * 2000
    content = (
        f"<content>\n{body}\n</content>\n\n"
        "<summary>\n" + "摘" * 200 + "\n</summary>\n\n"
        "<details><summary>作者留言</summary>\n" + "喵" * 100 + "\n</details>\n"
    )
    assert count_story_text_words(content) == 2000
    # <content> 是正文容器：剥标签、留内容。
    assert count_story_text_words(f"<content>\n{body}\n</content>") == 2000
    # 段落计数必须用同一口径，否则段数配额会把摘要算成正文段落。
    assert count_story_text_paragraphs(content) == 1


def test_apply_story_increment_writes_only_the_counted_prose(tmp_path: Path) -> None:
    body = "正" * 2100
    wrapped = (
        f"<content>\n{body}\n</content>\n"
        "<summary>摘要内容</summary>\n"
        "<details><summary>作者留言</summary>包装内容</details>\n"
        "<thinking>思考内容</thinking>\n"
        "<think>内部思考</think>\n"
        "<plan>写作计划</plan>\n"
        "<reasoning>推理过程</reasoning>"
    )
    contract = _story_contract(tmp_path, chapter_word_count_target=3000)
    target_path = contract["turnPlan"]["fragmentTargets"][0]["path"]
    tool = StorydexApplyStoryIncrementTool(workspace_root=tmp_path, turn_contract=contract)

    result = json.loads(tool.run({"fragments": [{"text": wrapped}]}).output)
    written = (tmp_path / target_path).read_text(encoding="utf-8")

    assert result["ok"] is True
    assert result["wordCountValidation"]["generatedWordCount"] == len(body)
    assert result["fragments"][0]["generatedWordCount"] == len(body)
    assert written == strip_non_story_wrappers(wrapped).strip() + "\n"
    assert written == body + "\n"


def test_unclosed_wrapper_is_rejected_before_counting_or_writing() -> None:
    raw = "<summary>\n正文没有闭合"
    extraction = extract_story_prose(raw)

    assert extraction.status == "rejected"
    assert extraction.reason_codes == ("unclosed_known_wrapper",)
    assert count_story_text_words(raw) == 0
    assert strip_non_story_wrappers(raw) == ""


def test_wrapper_stripping_does_not_touch_angle_brackets_in_prose() -> None:
    content = "他在纸上写下 a<b 与 c>d 两个式子。"
    assert count_story_text_words(content) == len("他在纸上写下a<b与c>d两个式子。")


def test_dialogue_paragraph_breaks_do_not_change_count_or_get_normalized(
    tmp_path: Path,
) -> None:
    service = get_story_project_service()
    content = "叙" * 1040 + "\n\n“你好。”\n\n" + "续" * 1055
    assert count_story_text_words(content) == 2100

    contract = _story_contract(tmp_path, chapter_word_count_target=3000)
    target_path = contract["turnPlan"]["fragmentTargets"][0]["path"]
    result = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": content}]},
        generation_contract=contract,
    )

    assert result["ok"] is True
    assert (tmp_path / target_path).read_text(encoding="utf-8") == content + "\n"


def test_project_settings_use_single_target_and_preserve_legacy_ranges(tmp_path: Path) -> None:
    service = get_story_project_service()
    defaults = service.read_project_settings(tmp_path)
    assert defaults["chapterWordCountTarget"] == 3000
    assert defaults["storyFragmentWordCountMin"] == 3000
    assert defaults["storyFragmentWordCountMax"] == 3000
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

    explicitly_preserved = service.write_project_settings(
        tmp_path,
        {"chapterWordCountTarget": 2500},
    )
    assert explicitly_preserved["chapterWordCountTarget"] == 2500
    assert service.read_project_settings(tmp_path)["chapterWordCountTarget"] == 2500


def test_story_settings_transport_defaults_match_the_domain_default() -> None:
    for response_model in (FileStoryProjectSettingsResponse, StoryStoryProjectSettingsResponse):
        assert response_model.model_fields["chapter_word_count_target"].default == 3000
        assert response_model.model_fields["story_fragment_word_count"].default == 3000
        assert response_model.model_fields["story_fragment_word_count_min"].default == 3000
        assert response_model.model_fields["story_fragment_word_count_max"].default == 3000


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


def test_single_file_continuation_uses_only_the_remaining_chapter_budget(
    tmp_path: Path,
) -> None:
    initial = _story_contract(
        tmp_path,
        chapter_word_count_target=3000,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
    )
    target_path = initial["turnPlan"]["fragmentTargets"][0]["path"]
    chapter_file = tmp_path / target_path
    chapter_file.parent.mkdir(parents=True, exist_ok=True)
    chapter_file.write_text("甲" * 2600, encoding="utf-8")

    continuation = _story_contract(
        tmp_path,
        chapter_word_count_target=3000,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
        active_file=target_path,
        prompt="请继续当前章",
    )
    plan = continuation["turnPlan"]
    policy = plan["wordCountPolicy"]

    assert policy["retainedWordCount"] == 2600
    assert policy["remainingWordCount"] == 400
    assert policy["modelReferenceWordCount"] == 400
    assert [item["referenceWordCount"] for item in plan["fragmentTargets"]] == [400]

    applied = get_story_project_service().apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "乙" * 400}]},
        generation_contract=continuation,
    )
    assert applied["ok"] is True
    assert applied["wordCountValidation"]["generatedWordCount"] == 400
    assert applied["wordCountValidation"]["retainedWordCount"] == 2600
    assert applied["wordCountValidation"]["resultingWordCount"] == 3000


def test_multi_fragment_continuation_counts_all_prose_in_the_authoritative_chapter(
    tmp_path: Path,
) -> None:
    initial = _story_contract(
        tmp_path,
        fragment_count=2,
        chapter_word_count_target=3000,
        template_id=DEFAULT_CHAPTER_TEMPLATE_ID,
    )
    initial_targets = initial["turnPlan"]["fragmentTargets"]
    assert len(initial_targets) == 2
    for target, count in zip(initial_targets, (1200, 1400)):
        path = tmp_path / target["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("甲" * count, encoding="utf-8")

    continuation = _story_contract(
        tmp_path,
        fragment_count=1,
        chapter_word_count_target=3000,
        template_id=DEFAULT_CHAPTER_TEMPLATE_ID,
        active_file=initial_targets[0]["path"],
        prompt="请继续当前章",
    )
    plan = continuation["turnPlan"]
    policy = plan["wordCountPolicy"]

    assert policy["retainedWordCount"] == 2600
    assert policy["remainingWordCount"] == 400
    assert policy["modelReferenceWordCount"] == 400
    assert [item["referenceWordCount"] for item in plan["fragmentTargets"]] == [400]

    applied = get_story_project_service().apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "乙" * 400}]},
        generation_contract=continuation,
    )
    assert applied["ok"] is True
    assert applied["wordCountValidation"]["generatedWordCount"] == 400
    assert applied["wordCountValidation"]["retainedWordCount"] == 2600
    assert applied["wordCountValidation"]["resultingWordCount"] == 3000


def test_turn_contract_uses_calibrated_model_reference_without_changing_product_target(
    tmp_path: Path,
) -> None:
    calibration = StoryLengthCalibrationService()
    for _ in range(3):
        assert calibration.append_sample(
            tmp_path,
            product_target_word_count=3000,
            model_reference_word_count=3000,
            actual_word_count=3600,
            provider="chy",
            model="deepseek-v4-flash",
        )

    contract = get_storydex_orchestration_service().build_turn_contract(
        tmp_path,
        prompt="请续写剧情",
        story_generation={
            "fragmentCount": 1,
            "chapterWordCountTarget": 3000,
            "chapterTemplateId": DEFAULT_CHAPTER_TEMPLATE_ID,
        },
        intent_frame={
            "primary": "story_generation",
            "operationType": "create_new",
            "confidence": 1.0,
        },
        provider="chy",
        model="deepseek-v4-flash",
    )

    plan = contract["turnPlan"]
    policy = plan["wordCountPolicy"]
    assert plan["chapterWordCountTarget"] == 3000
    assert (policy["acceptanceMinimum"], policy["acceptanceMaximum"]) == (2550, 3900)
    assert policy["overBudgetAction"] == "warn_and_keep"
    # 3 个样本还不足 FULL_STRENGTH_CALIBRATION_SAMPLES，只按半强度纠偏：
    # 实测偏置 1.20 -> 应用 1.10 -> 参考字数 3000 / 1.10 = 2727。
    assert policy["calibration"]["correctionStrength"] == 0.50
    assert policy["modelReferenceWordCount"] == 2727
    assert policy["calibration"]["status"] == "applied"
    assert policy["calibration"]["reason"] == "same_target_grade"
    assert [item["referenceWordCount"] for item in plan["fragmentTargets"]] == [2727]


def test_correction_prompt_avoids_exact_gap_and_foreshadowing_requirements() -> None:
    prompt = routes._story_generation_correction_prompt(
        {
            "generatedWordCount": 1600,
            "acceptWordCountMin": 1875,
            "acceptWordCountMax": 3125,
            "fragments": [
                {
                    "order": 1,
                    "path": "chapters/第一章/001.md",
                    "exists": False,
                    "writeMode": "replace",
                    "baselineWordCount": 0,
                    "generatedWordCount": 1600,
                    "status": "failed",
                }
            ],
        },
        correction_attempt=1,
    )
    correction = json.loads(prompt.split("STORYDEX_OBJECTIVE_VALIDATION=", 1)[1])

    assert "1600" not in prompt
    assert "1875" not in prompt
    assert "3125" not in prompt
    assert "currentProgramWordCount" not in prompt
    assert "还需补写" not in prompt
    assert "275" not in prompt
    assert "additionalWordCountNeeded" not in prompt
    assert "acceptWordCountMin" not in prompt
    assert "baselineWordCount" not in prompt
    assert "generatedWordCount" not in prompt
    assert "情节没有写完" not in prompt
    assert "兑现" not in prompt
    assert "伏笔" not in prompt
    assert (
        "在不改变既定剧情计划、不新增无关支线、不重复已有信息的前提下，"
        "补充与本轮核心事件直接相关的动作后果、角色下一步决定和必要的场景收束。"
    ) in prompt
    assert correction["expansionDirections"] == [
        "当前冲突的直接后果",
        "角色的下一步决定",
        "必要的场景收束",
    ]
    assert "不要写摘要、留言" in prompt
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
def test_story_fragment_far_outside_band_is_written_with_a_warning(
    tmp_path: Path,
    actual_word_count: int,
) -> None:
    # v3 章级正文低于放行带时保留结构合法候选，并明确标记偏短。
    service = get_story_project_service()
    contract = _story_contract(tmp_path, fragment_word_count=100)
    target_path = contract["turnPlan"]["fragmentTargets"][0]["path"]
    result = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "字" * actual_word_count}]},
        generation_contract=contract,
    )
    assert result["ok"] is True
    assert result["wordCountValidation"]["passed"] is True
    assert result["wordCountValidation"]["belowBudget"] is True
    assert result["wordCountValidation"]["status"] == "warning"
    assert result["wordCountValidation"]["message"] == STORY_UNDER_BUDGET_KEEP_MESSAGE
    assert (tmp_path / target_path).exists()


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
    ("actual_word_count", "expected_below_budget", "expected_over_budget"),
    [
        (2549, True, False),
        (2550, False, False),
        (3000, False, False),
        (3900, False, False),
        # 超上限只告警：仍然放行并落盘，overBudget 标记保留给作者看。
        (3901, False, True),
    ],
)
def test_chapter_target_uses_one_acceptance_band_before_and_after_write(
    tmp_path: Path,
    actual_word_count: int,
    expected_below_budget: bool,
    expected_over_budget: bool,
) -> None:
    service = get_story_project_service()
    prewrite_root = tmp_path / "prewrite"
    contract = _story_contract(prewrite_root, chapter_word_count_target=3000)
    target_path = contract["turnPlan"]["fragmentTargets"][0]["path"]
    result = service.apply_story_generation_increment(
        prewrite_root,
        {"fragments": [{"text": "字" * actual_word_count}]},
        generation_contract=contract,
    )
    preflight = result["wordCountValidation"]

    assert result["ok"] is True
    assert preflight["passed"] is True
    assert preflight["generatedWordCount"] == actual_word_count
    assert preflight["chapterWordCountTarget"] == 3000
    assert (preflight["acceptWordCountMin"], preflight["acceptWordCountMax"]) == (2550, 3900)
    assert preflight["belowBudget"] is expected_below_budget
    assert preflight["overBudget"] is expected_over_budget
    assert preflight["status"] == ("warning" if expected_below_budget or expected_over_budget else "success")
    assert (prewrite_root / target_path).read_text(encoding="utf-8") == "字" * actual_word_count + "\n"

    postwrite_root = tmp_path / "postwrite"
    postwrite_contract = _story_contract(postwrite_root, chapter_word_count_target=3000)
    postwrite_target = postwrite_contract["turnPlan"]["fragmentTargets"][0]["path"]
    postwrite_path = postwrite_root / postwrite_target
    postwrite_path.parent.mkdir(parents=True, exist_ok=True)
    postwrite_path.write_text("字" * actual_word_count, encoding="utf-8")
    validation = service.validate_story_generation_turn(postwrite_root, postwrite_contract)

    assert validation["passed"] is True
    assert validation["belowBudget"] is expected_below_budget
    assert validation["overBudget"] is expected_over_budget
    assert validation["status"] == ("warning" if expected_below_budget or expected_over_budget else "success")
    if expected_below_budget:
        assert preflight["message"] == STORY_UNDER_BUDGET_KEEP_MESSAGE
        assert validation["message"] == STORY_UNDER_BUDGET_KEEP_MESSAGE
    if expected_over_budget:
        assert preflight["message"] == STORY_OVER_BUDGET_KEEP_MESSAGE
        assert validation["message"] == STORY_OVER_BUDGET_KEEP_MESSAGE
        assert "不能结束本轮" not in preflight["message"]
        assert "不能结束本轮" not in validation["message"]


@pytest.mark.parametrize(
    ("actual_word_count", "expected_below_budget", "expected_over_budget"),
    [
        (1399, True, False),
        (1400, False, False),
        (3250, False, False),
        (3251, False, True),
    ],
)
def test_legacy_range_settings_use_chapter_scope_acceptance_band(
    tmp_path: Path,
    actual_word_count: int,
    expected_below_budget: bool,
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
    target_path = plan["fragmentTargets"][0]["path"]

    result = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "字" * actual_word_count}]},
        generation_contract=contract,
    )
    validation = service.validate_story_generation_turn(tmp_path, contract)
    preflight = result["wordCountValidation"]
    assert result["ok"] is True
    assert (preflight["targetWordCountMin"], preflight["targetWordCountMax"]) == (2000, 2500)
    assert (preflight["acceptWordCountMin"], preflight["acceptWordCountMax"]) == (1400, 3250)
    assert preflight["belowBudget"] is expected_below_budget
    assert preflight["overBudget"] is expected_over_budget
    assert validation["passed"] is True
    assert validation["belowBudget"] is expected_below_budget
    assert validation["overBudget"] is expected_over_budget
    assert (tmp_path / target_path).exists()


@pytest.mark.parametrize(
    ("actual_word_count", "expected_passed", "expected_over_budget"),
    [
        (1399, False, False),
        (1400, True, False),
        (3250, True, False),
        (3251, True, True),
    ],
)
def test_serialized_legacy_contract_uses_same_band_before_and_after_write(
    tmp_path: Path,
    actual_word_count: int,
    expected_passed: bool,
    expected_over_budget: bool,
) -> None:
    service = get_story_project_service()
    prewrite_root = tmp_path / "prewrite"
    contract = _story_contract(
        prewrite_root,
        fragment_word_count_min=2000,
        fragment_word_count_max=2500,
    )
    contract["turnPlan"]["wordCountPolicy"].pop("scope", None)
    target_path = contract["turnPlan"]["fragmentTargets"][0]["path"]
    result = service.apply_story_generation_increment(
        prewrite_root,
        {"fragments": [{"text": "字" * actual_word_count}]},
        generation_contract=contract,
    )

    preflight = result["wordCountValidation"]
    assert result["ok"] is expected_passed
    assert preflight["passed"] is expected_passed
    assert (preflight["acceptWordCountMin"], preflight["acceptWordCountMax"]) == (1400, 3250)
    assert preflight["overBudget"] is expected_over_budget
    assert (prewrite_root / target_path).exists() is expected_passed

    postwrite_root = tmp_path / "postwrite"
    postwrite_contract = _story_contract(
        postwrite_root,
        fragment_word_count_min=2000,
        fragment_word_count_max=2500,
    )
    postwrite_contract["turnPlan"]["wordCountPolicy"].pop("scope", None)
    postwrite_target = postwrite_contract["turnPlan"]["fragmentTargets"][0]["path"]
    postwrite_path = postwrite_root / postwrite_target
    postwrite_path.parent.mkdir(parents=True, exist_ok=True)
    postwrite_path.write_text("字" * actual_word_count, encoding="utf-8")
    validation = service.validate_story_generation_turn(postwrite_root, postwrite_contract)

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


@pytest.mark.parametrize("preexisting", [True, False])
def test_bounded_multi_fragment_commit_rolls_back_when_the_second_replace_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    preexisting: bool,
) -> None:
    service = get_story_project_service()
    contract = _story_contract(
        tmp_path,
        fragment_count=2,
        chapter_word_count_target=2500,
    )
    targets = [tmp_path / item["path"] for item in contract["turnPlan"]["fragmentTargets"]]
    originals = ["旧" * 1200 + "一", "旧" * 1200 + "二"]
    if preexisting:
        assert len(contract["turnPlan"]["fragmentTargets"]) == len(targets) == len(originals)
        for target, path, content in zip(
            contract["turnPlan"]["fragmentTargets"],
            targets,
            originals,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            target["baselineWordCount"] = service.count_story_file_words(path)

    real_replace = story_project_module.os.replace
    failed = False

    def fail_second_replace(source: Any, destination: Any) -> None:
        nonlocal failed
        if Path(destination).resolve() == targets[1].resolve() and not failed:
            failed = True
            raise OSError("injected second replacement failure")
        real_replace(source, destination)

    monkeypatch.setattr(story_project_module.os, "replace", fail_second_replace)

    with pytest.raises(OSError, match="second replacement failure"):
        service.apply_story_generation_increment(
            tmp_path,
            {"fragments": [{"text": "新" * 1250}, {"text": "文" * 1250}]},
            generation_contract=contract,
        )

    if preexisting:
        assert [path.read_text(encoding="utf-8") for path in targets] == originals
    else:
        assert all(not path.exists() for path in targets)


def test_v3_short_candidate_is_committed_without_legacy_append_correction(tmp_path: Path) -> None:
    service = get_story_project_service()
    contract = _story_contract(tmp_path, chapter_word_count_target=2500)
    target_path = contract["turnPlan"]["fragmentTargets"][0]["path"]

    applied = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "甲" * 1600}]},
        generation_contract=contract,
    )
    assert applied["ok"] is True
    assert applied["wordCountValidation"]["passed"] is True
    assert applied["wordCountValidation"]["belowBudget"] is True
    assert applied["wordCountValidation"]["status"] == "warning"
    assert routes._supports_correction_continuation(contract) is False
    assert "allowBelowMinimumAfterCorrection" not in contract["turnPlan"]["wordCountPolicy"]
    assert service.count_story_file_words(tmp_path / target_path) == 1600


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


def test_v3_short_append_is_committed_without_legacy_append_correction(
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
    # 章级判定看的是"落盘后整章有多长"，所以偏短场景必须让磁盘正文加本轮新增
    # 之后仍然低于下界 1750，否则续写的是一章正常长度的正文，不该被拒。
    target.write_text("甲" * 800, encoding="utf-8")

    continuation = _story_contract(
        tmp_path,
        chapter_word_count_target=2500,
        template_id=SINGLE_FILE_CHAPTER_TEMPLATE_ID,
        active_file=target_path,
    )
    applied = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "乙" * 300}]},
        generation_contract=continuation,
    )
    assert applied["ok"] is True
    assert applied["wordCountValidation"]["resultingWordCount"] == 1100
    assert applied["wordCountValidation"]["belowBudget"] is True
    assert applied["wordCountValidation"]["status"] == "warning"
    assert routes._supports_correction_continuation(continuation) is False
    assert service.count_story_file_words(target) == 1100


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
    # Serialized pre-W1 contracts keep their fragment scope, but now use the
    # same 70%-130% acceptance band before and after writing.
    continuation["turnPlan"]["wordCountPolicy"].pop("scope", None)
    assert continuation["turnPlan"]["fragmentTargets"][0]["baselineWordCount"] == 2200

    first_result = service.apply_story_generation_increment(
        tmp_path,
        {"fragments": [{"text": "乙" * 1399}]},
        generation_contract=continuation,
    )
    first_validation = service.validate_story_generation_turn(tmp_path, continuation)
    assert first_result["ok"] is False
    assert first_validation["passed"] is False
    assert service.count_story_file_words(target) == 2200

    corrected_contract = routes._rebuild_story_generation_contract_for_correction(
        tmp_path,
        continuation,
        first_validation,
    )
    corrected_target = corrected_contract["turnPlan"]["fragmentTargets"][0]
    assert corrected_target["baselineWordCount"] == 2200
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
    assert service.count_story_file_words(target) == 4400
    assert final_text.count("乙") == 0
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
        "countingRule": STORY_WORD_COUNT_RULE,
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
            self.paragraph_calls: list[dict[str, Any]] = []

        def record_generation_result(self, _root: Path, **kwargs: Any) -> bool:
            self.calls.append(kwargs)
            return True

        def record_paragraph_generation_result(self, _root: Path, **kwargs: Any) -> bool:
            self.paragraph_calls.append(kwargs)
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


def test_below_budget_gets_one_correction_and_still_short_write_fails(
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
    errors = [payload for name, payload in packets if name == "AgentError"]
    assert result["runtime"].calls == 2
    assert result["project"].validations == 2
    assert [item["passed"] for item in validations] == [False, False]
    assert validations[1]["belowBudget"] is True
    assert validations[1]["correctionApplied"] is True
    assert validations[1]["maximumCorrectionAttempts"] == 1
    assert len(continuations) == 1
    assert continuations[0]["continuationMode"] == "story_generation_correction"
    assert continuations[0]["maximumCorrectionAttempts"] == 1
    assert "1600" not in result["runtime"].prompts[1]
    assert "还需补写" not in result["runtime"].prompts[1]
    assert "兑现" not in result["runtime"].prompts[1]
    correction_policy = result["runtime"].contracts[1]["turnPlan"]["wordCountPolicy"]
    assert "allowBelowMinimumAfterCorrection" not in correction_policy
    assert event_names.count("AgentCompleted") == 0
    assert len(errors) == 1
    assert errors[0]["error_type"] == "StoryGenerationValidationFailed"
    assert result["handle"].finalize_calls == 1
    assert result["handle"].runtime_calls_at_finalize == 2
    assert result["calibration"].calls == []


def test_over_budget_completes_with_a_warning_instead_of_failing(
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
    event_names = [name for name, _payload in packets]
    validations = [payload for name, payload in packets if name == "StoryGenerationValidation"]
    completions = [payload for name, payload in packets if name == "AgentCompleted"]
    errors = [payload for name, payload in packets if name == "AgentError"]
    assert result["runtime"].calls == 1
    assert len(validations) == 1
    # 偏长的章节仍然是一次成功生成：overBudget 只作为告警随包一起交给作者。
    assert validations[0]["passed"] is True
    assert validations[0]["overBudget"] is True
    assert validations[0]["correctionApplied"] is False
    assert validations[0]["message"] == STORY_OVER_BUDGET_KEEP_MESSAGE
    # 超上限不触发补写：补写只为偏短而存在。
    assert not [payload for name, payload in packets if name == "ContinuationStarted"]
    assert event_names.count("AgentCompleted") == 1
    assert completions[0]["overBudget"] is True
    assert completions[0]["message"] == STORY_OVER_BUDGET_KEEP_MESSAGE
    assert "不能结束本轮" not in completions[0]["message"]
    assert not errors


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
