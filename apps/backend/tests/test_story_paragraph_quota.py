from __future__ import annotations

from pathlib import Path

import pytest

from services.coomi_agent_service import (
    _render_turn_contract,
    _split_paragraph_quota,
    _story_paragraph_quota_prompt_line,
)
from services.story_bounded_generation_service import build_draft_messages
from services.story_length_calibration_service import StoryLengthCalibrationService
from services.story_preset_length_policy_service import (
    DEFAULT_CHARS_PER_PARAGRAPH,
    classify_paragraph_density,
    classify_paragraph_density_text,
    strip_quantitative_length_directives,
)
from services.story_word_count_service import count_story_text_paragraphs


PROVIDER = "test-provider"
MODEL = "test-model"


@pytest.fixture(autouse=True)
def _disable_tier_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORY_LENGTH_TIER_ENABLED", "0")


def _write_preset(root: Path, modules: list[dict]) -> None:
    import json

    active = root / ".storydex" / "presets" / "active"
    active.mkdir(parents=True, exist_ok=True)
    (active / "sample.preset.json").write_text(
        json.dumps({"modules": modules}, ensure_ascii=False),
        encoding="utf-8",
    )


def _set_paragraph_quota_flag(root: Path, enabled: bool) -> None:
    import json

    config_dir = root / ".storydex" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "feature-flags.json").write_text(
        json.dumps({"PARAGRAPH_QUOTA_GENERATION_ENABLED": enabled}),
        encoding="utf-8",
    )


def _write_runtime_preset(root: Path, content: str) -> None:
    active = root / ".storydex" / "presets" / "active"
    active.mkdir(parents=True, exist_ok=True)
    (active / "dialogue.md").write_text(content, encoding="utf-8")


def test_paragraph_count_uses_blank_line_rule() -> None:
    assert count_story_text_paragraphs("一段。\n\n二段。\n\n三段。") == 3
    # 单换行是同一段内的软换行，不能计成新段，否则配额口径与评测 harness 不一致。
    assert count_story_text_paragraphs("一行。\n紧接一行。") == 1
    assert count_story_text_paragraphs("   \n\n  \n\n") == 0
    assert count_story_text_paragraphs("") == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("正文采用完整的长自然段叙述，不要单句成段", "long"),
        ("严禁频繁分段。你应该做到输出的段落以大段为主，小段较少。", "long"),
        ("段落以长句为主，尽量避免单句成段的短文风格。", "long"),
        ("每句话都要单句成段，制造节奏感", "short"),
        ("用一两个小段描写环境碎片和物品细节。", "medium"),
        ("内容要详实且有新意，不要重复前文的描写", "medium"),
    ],
)
def test_paragraph_density_band_classification(text: str, expected: str) -> None:
    assert classify_paragraph_density_text([text])["band"] == expected


def test_disabled_preset_modules_do_not_move_the_density_band(tmp_path: Path) -> None:
    _write_preset(
        tmp_path,
        [
            {"title": "长段落", "content": "正文采用完整的长自然段叙述", "enabledByDefault": False},
            {"title": "文风", "content": "保持轻松活泼的情感基调", "enabledByDefault": True},
        ],
    )
    result = classify_paragraph_density(tmp_path)
    assert result["band"] == "medium"
    assert result["reason"] == "no_preset_signal"


def test_enabled_long_paragraph_module_moves_the_band(tmp_path: Path) -> None:
    _write_preset(
        tmp_path,
        [{"title": "长段落", "content": "正文采用完整的长自然段叙述", "enabledByDefault": True}],
    )
    assert classify_paragraph_density(tmp_path)["band"] == "long"


def test_missing_preset_directory_falls_back_to_default_band(tmp_path: Path) -> None:
    assert classify_paragraph_density(tmp_path)["band"] == "medium"


def test_quantitative_length_directives_are_stripped() -> None:
    cleaned, removed = strip_quantitative_length_directives(
        "字数：每次回复不少于2000字。文风要生动。"
    )
    assert cleaned == "文风要生动。"
    assert removed == ["字数：每次回复不少于2000字。"]


def test_paragraph_count_directives_are_stripped() -> None:
    cleaned, removed = strip_quantitative_length_directives("请分5-8个自然段输出，每段都要有画面感")
    assert "5-8" not in cleaned
    assert cleaned == "每段都要有画面感"
    assert removed


def test_qualitative_density_and_summary_length_survive_stripping() -> None:
    # 定性密度是文风，删掉会伤质量；总结长度约束的是另一个产物，不是正文长度。
    for text in (
        "内容要详实且有新意，不要重复前文的描写",
        "正文结束之后写一个100字左右的总结",
        "通过大篇幅的环境描写渲染人物的深层心理",
        "严禁频繁分段，段落以大段为主",
    ):
        cleaned, removed = strip_quantitative_length_directives(text)
        assert cleaned == text
        assert removed == []


def test_cold_start_quota_uses_band_defaults(tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()
    quota = service.resolve_paragraph_quota(
        tmp_path,
        product_target_word_count=1500,
        provider=PROVIDER,
        model=MODEL,
        density_band="medium",
    )
    assert quota["charsPerParagraph"] == float(DEFAULT_CHARS_PER_PARAGRAPH["medium"])
    assert quota["paragraphQuota"] == 34
    assert quota["paragraphQuotaMinimum"] < quota["paragraphQuota"] < quota["paragraphQuotaMaximum"]
    assert quota["calibration"]["status"] == "fallback"


def test_long_band_yields_fewer_paragraphs_for_the_same_target(tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()
    short = service.resolve_paragraph_quota(
        tmp_path,
        product_target_word_count=3000,
        provider=PROVIDER,
        model=MODEL,
        density_band="short",
    )
    long = service.resolve_paragraph_quota(
        tmp_path,
        product_target_word_count=3000,
        provider=PROVIDER,
        model=MODEL,
        density_band="long",
    )
    assert short["paragraphQuota"] > long["paragraphQuota"]


def test_samples_calibrate_chars_per_paragraph_within_the_band(tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()
    for characters, paragraphs in ((2480, 40), (1922, 31), (2170, 35)):
        assert service.append_paragraph_sample(
            tmp_path,
            product_target_word_count=1500,
            actual_word_count=characters,
            actual_paragraph_count=paragraphs,
            provider=PROVIDER,
            model=MODEL,
            density_band="medium",
        )
    calibrated = service.resolve_paragraph_quota(
        tmp_path,
        product_target_word_count=1500,
        provider=PROVIDER,
        model=MODEL,
        density_band="medium",
    )
    assert calibrated["calibration"]["status"] == "applied"
    assert calibrated["calibration"]["sampleCount"] == 3
    assert calibrated["charsPerParagraph"] == pytest.approx(62.0, abs=0.5)
    # 实测每段更长 -> 同样的字数目标需要更少的段。
    assert calibrated["paragraphQuota"] == 24

    other_band = service.resolve_paragraph_quota(
        tmp_path,
        product_target_word_count=1500,
        provider=PROVIDER,
        model=MODEL,
        density_band="long",
    )
    assert other_band["calibration"]["status"] == "fallback"


def test_degenerate_paragraph_samples_are_rejected(tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()
    assert not service.append_paragraph_sample(
        tmp_path,
        product_target_word_count=1500,
        actual_word_count=900,
        actual_paragraph_count=0,
        provider=PROVIDER,
        model=MODEL,
    )
    assert not service.append_paragraph_sample(
        tmp_path,
        product_target_word_count=1500,
        actual_word_count=9000,
        actual_paragraph_count=1,
        provider=PROVIDER,
        model=MODEL,
    )
    assert not service.append_paragraph_sample(
        tmp_path,
        product_target_word_count=1500,
        actual_word_count=1500,
        actual_paragraph_count=30,
        provider="",
        model="",
    )


def test_paragraph_samples_do_not_disturb_the_character_ratio_path(tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()
    service.append_paragraph_sample(
        tmp_path,
        product_target_word_count=1500,
        actual_word_count=1500,
        actual_paragraph_count=34,
        provider=PROVIDER,
        model=MODEL,
    )
    guidance = service.resolve_generation_guidance(
        tmp_path,
        product_target_word_count=1500,
        provider=PROVIDER,
        model=MODEL,
    )
    assert guidance["modelReferenceWordCount"] == 1500
    assert guidance["calibration"]["status"] == "fallback"


def test_off_target_runs_still_feed_the_paragraph_calibration(tmp_path: Path) -> None:
    # 段落形态是风格观测；只采样达标结果会让偏掉的密度档永远无法自我纠正。
    service = StoryLengthCalibrationService()
    recorded = service.record_paragraph_generation_result(
        tmp_path,
        turn_contract={
            "turnPlan": {
                "chapterWordCountTarget": 1500,
                "wordCountPolicy": {"scope": "chapter", "paragraphDensityBand": "medium"},
            }
        },
        validation={
            "applicable": True,
            "passed": False,
            "structurePassed": True,
            "generatedWordCount": 2600,
            "generatedParagraphCount": 60,
        },
        provider=PROVIDER,
        model=MODEL,
    )
    assert recorded


def test_prompt_line_omitted_without_a_quota() -> None:
    assert _story_paragraph_quota_prompt_line({}) == ""
    assert _story_paragraph_quota_prompt_line({"paragraphQuota": 0}) == ""


def test_prompt_line_gives_paragraphs_and_never_a_character_target() -> None:
    line = _story_paragraph_quota_prompt_line(
        {"paragraphQuota": 34, "paragraphQuotaMinimum": 31, "paragraphQuotaMaximum": 37}
    )
    assert "31-37 paragraphs" in line
    assert "aim for 34" in line
    # 关键不变量：段数是唯一长度指令，每段长度归预设。
    assert "ONLY length instruction" in line
    assert "do not aim at any character or word count" in line
    assert "owned by the active preset" in line


def test_bounded_draft_uses_paragraph_quota_as_its_only_length_instruction() -> None:
    messages = build_draft_messages(
        prompt="续写下一章",
        turn_contract={
            "turnPlan": {
                "fragmentCount": 1,
                "wordCountPolicy": {
                    "target": 3000,
                    "modelReferenceWordCount": 2400,
                    "paragraphQuota": 40,
                    "paragraphQuotaMinimum": 36,
                    "paragraphQuotaMaximum": 44,
                },
            }
        },
    )

    system_prompt = messages[0]["content"]
    assert "36-44 个自然段" in system_prompt
    assert "以 40 段为参考" in system_prompt
    assert "本轮唯一的篇幅指令" in system_prompt
    assert "本章参考长度约为" not in system_prompt
    assert "计数规则" not in system_prompt


def test_paragraph_quota_splits_across_fragment_files() -> None:
    assert _split_paragraph_quota(34, 3) == [12, 11, 11]
    assert _split_paragraph_quota(34, 1) == [34]
    assert sum(_split_paragraph_quota(68, 5)) == 68


def _story_orchestration():
    import types

    from services.storydex_orchestration_service import StorydexOrchestrationService
    from services.story_project_service import get_story_project_service

    return StorydexOrchestrationService(
        get_story_project_service(),
        global_config_service=types.SimpleNamespace(
            read_agent_settings=lambda: {"coomiMemoryEnabled": False, "wikiContextEnabled": False}
        ),
    )


def _turn_contract_for(tmp_path: Path, target: int) -> dict:
    return _story_orchestration().build_turn_contract(
        tmp_path,
        prompt="继续写下一章正文",
        story_generation={"fragmentCount": 1, "chapterWordCountTarget": target},
        intent_frame={"primary": "story_generation", "operationType": "create_new"},
        provider=PROVIDER,
        model=MODEL,
    )


def _turn_plan_for(tmp_path: Path, target: int) -> dict:
    return _turn_contract_for(tmp_path, target)["turnPlan"]


def test_turn_contract_defaults_to_character_guidance_without_paragraph_quota(
    tmp_path: Path,
) -> None:
    _write_runtime_preset(tmp_path, "人物对话必须单独成段。")
    contract = _turn_contract_for(tmp_path, 1500)
    plan = contract["turnPlan"]
    policy = plan["wordCountPolicy"]
    rendered = _render_turn_contract(contract)

    assert policy["paragraphQuota"] == 0
    assert policy["paragraphDensityBand"] == "medium"
    assert policy["paragraphDensityReason"] == "paragraph_quota_disabled"
    assert policy["acceptanceMinimum"] == 1275
    assert policy["acceptanceMaximum"] == 1950
    assert "wordCountGuidance:" in rendered
    assert "paragraphQuota:" not in rendered
    assert "paragraph quota" not in rendered.lower()
    assert "人物对话必须单独成段。" in rendered


def test_enabled_long_paragraph_preset_lowers_the_contract_quota(tmp_path: Path) -> None:
    _set_paragraph_quota_flag(tmp_path, True)
    _write_preset(
        tmp_path,
        [{"title": "长段落", "content": "正文采用完整的长自然段叙述", "enabledByDefault": True}],
    )
    calibration = StoryLengthCalibrationService()
    for characters, paragraphs in ((1540, 14), (1650, 15), (1430, 13)):
        assert calibration.append_paragraph_sample(
            tmp_path,
            product_target_word_count=1500,
            actual_word_count=characters,
            actual_paragraph_count=paragraphs,
            provider=PROVIDER,
            model=MODEL,
            density_band="long",
        )
    policy = _turn_plan_for(tmp_path, 1500)["wordCountPolicy"]
    assert policy["paragraphDensityBand"] == "long"
    assert policy["paragraphCalibration"]["status"] == "applied"
    assert policy["paragraphQuota"] == 14


def test_feature_flag_keeps_character_guidance_until_density_is_calibrated(
    tmp_path: Path,
) -> None:
    _set_paragraph_quota_flag(tmp_path, True)

    contract = _turn_contract_for(tmp_path, 3000)
    policy = contract["turnPlan"]["wordCountPolicy"]
    system_prompt = build_draft_messages(
        prompt="续写下一章",
        turn_contract=contract,
    )[0]["content"]

    assert policy["paragraphCalibration"]["status"] == "fallback"
    assert policy["paragraphCalibration"]["sampleCount"] == 0
    assert policy["paragraphQuota"] == 0
    assert "本章参考长度约为 3000" in system_prompt
    assert "本轮唯一的篇幅指令" not in system_prompt


def test_feature_flag_off_falls_back_to_character_guidance(tmp_path: Path) -> None:
    _set_paragraph_quota_flag(tmp_path, False)
    policy = _turn_plan_for(tmp_path, 1500)["wordCountPolicy"]
    assert policy["paragraphQuota"] == 0
    assert _story_paragraph_quota_prompt_line(policy) == ""
