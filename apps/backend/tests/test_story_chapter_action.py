"""W2-2: the chapter action and authoritative path pre-gate.

A story turn must know which chapter it writes into *before* the prose call. The
bug this closes wrote "chapter 2" prose into ``chapters/第1章 .../002.md``,
because the planner only recognised an explicit rewrite and otherwise appended to
whatever chapter happened to be active.

Structure is a hard gate. A second word-count call must never be spent trying to
repair a chapter that landed in the wrong directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from services.story_chapter_action_service import (
    CHAPTER_ACTION_CONTINUE_CHAPTER,
    CHAPTER_ACTION_CONTINUE_FRAGMENT,
    CHAPTER_ACTION_CREATE_NEXT_CHAPTER,
    CHAPTER_ACTION_CREATE_SPECIFIC_CHAPTER,
    CHAPTER_ACTION_REWRITE_EXISTING,
    CHAPTER_ACTIONS,
    MAX_CHAPTER_RANGE_SIZE,
    parse_chapter_range,
    resolve_chapter_action,
    validate_chapter_plan,
)
from services.story_project_service import (
    DEFAULT_CHAPTER_TEMPLATE_ID,
    SINGLE_FILE_CHAPTER_TEMPLATE_ID,
    SINGLE_FILE_CONTENT_MODE,
    get_story_project_service,
)


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("生成第121章到第125章", (121, 122, 123, 124, 125)),
        ("请写 3-5章", (3, 4, 5)),
        ("创作第十章至第十二章", (10, 11, 12)),
        ("分析第3章到第5章", ()),
        ("重写第62章到第63章", ()),
    ],
)
def test_parse_chapter_range_only_expands_generation_requests(
    prompt: str,
    expected: tuple[int, ...],
) -> None:
    assert parse_chapter_range(prompt) == expected


def test_parse_chapter_range_rejects_oversized_batches() -> None:
    with pytest.raises(ValueError, match=str(MAX_CHAPTER_RANGE_SIZE + 1)):
        parse_chapter_range(f"生成第1章到第{MAX_CHAPTER_RANGE_SIZE + 1}章")


def _write_chapter(root: Path, name: str, *, segment: str = "001.md", text: str = "正文") -> str:
    path = root / "chapters" / name / segment
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path.relative_to(root).as_posix()


def _resolve(
    prompt: str,
    *,
    active_file: str = "",
    chapter_numbers: tuple[int, ...] = (),
    is_new_story: bool = False,
    content_mode: str = "multi_fragment",
) -> dict[str, Any]:
    return resolve_chapter_action(
        prompt=prompt,
        active_file=active_file,
        chapter_numbers=chapter_numbers,
        content_mode=content_mode,
        is_new_story=is_new_story,
    )


def test_chapter_action_vocabulary_is_closed() -> None:
    # The write path branches on these names. A typo must fail loudly at import
    # rather than silently fall through to "append to the active chapter".
    assert CHAPTER_ACTIONS == (
        CHAPTER_ACTION_CONTINUE_FRAGMENT,
        CHAPTER_ACTION_CONTINUE_CHAPTER,
        CHAPTER_ACTION_CREATE_NEXT_CHAPTER,
        CHAPTER_ACTION_CREATE_SPECIFIC_CHAPTER,
        CHAPTER_ACTION_REWRITE_EXISTING,
    )


def test_new_story_creates_the_first_chapter() -> None:
    action = _resolve("开始写一个新故事", is_new_story=True)
    assert action["action"] == CHAPTER_ACTION_CREATE_NEXT_CHAPTER
    assert action["targetChapterNumber"] == 1


@pytest.mark.parametrize(
    ("prompt", "expected_number"),
    [
        ("请写第二章的剧情", 2),
        ("写第2章", 2),
        ("生成第三章", 3),
        ("创建第十章", 10),
    ],
)
def test_explicit_chapter_number_targets_that_chapter(prompt: str, expected_number: int) -> None:
    # This is the regression: chapter 1 exists and is active, yet the request
    # names a later chapter. The old planner appended into chapter 1.
    action = _resolve(
        prompt,
        active_file="chapters/第1章 开端/001.md",
        chapter_numbers=(1,),
    )
    assert action["action"] == CHAPTER_ACTION_CREATE_SPECIFIC_CHAPTER
    assert action["targetChapterNumber"] == expected_number


@pytest.mark.parametrize("prompt", ["下一章", "写新的一章", "开新章", "接着开一章"])
def test_next_chapter_phrases_advance_past_the_maximum(prompt: str) -> None:
    action = _resolve(prompt, active_file="chapters/第2章 中段/001.md", chapter_numbers=(1, 2))
    assert action["action"] == CHAPTER_ACTION_CREATE_NEXT_CHAPTER
    assert action["targetChapterNumber"] == 3


def test_rewrite_wins_over_the_bare_chapter_number() -> None:
    # "重写第1章" contains "第1章"; classifying it as create_specific_chapter
    # would build a duplicate chapter 1 directory beside the real one.
    action = _resolve("重写第1章", chapter_numbers=(1, 2))
    assert action["action"] == CHAPTER_ACTION_REWRITE_EXISTING
    assert action["targetChapterNumber"] == 1


def test_existing_chapter_number_continues_that_chapter() -> None:
    action = _resolve("补充第1章的细节", active_file="", chapter_numbers=(1, 2))
    assert action["action"] == CHAPTER_ACTION_CONTINUE_CHAPTER
    assert action["targetChapterNumber"] == 1


def test_plain_continuation_stays_in_the_active_chapter() -> None:
    action = _resolve("继续写下去", active_file="chapters/第2章 中段/001.md", chapter_numbers=(1, 2))
    assert action["action"] == CHAPTER_ACTION_CONTINUE_CHAPTER
    assert action["targetChapterNumber"] == 2


def test_single_file_continuation_reports_the_fragment_action() -> None:
    action = _resolve(
        "继续写",
        active_file="chapters/第1章 开端/正文.md",
        chapter_numbers=(1,),
        content_mode=SINGLE_FILE_CONTENT_MODE,
    )
    assert action["action"] == CHAPTER_ACTION_CONTINUE_FRAGMENT
    assert action["targetChapterNumber"] == 1


def test_planner_creates_a_new_directory_for_the_named_chapter(tmp_path: Path) -> None:
    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)
    active = _write_chapter(tmp_path, "第1章 开端")

    targets = service.plan_story_generation_targets(
        tmp_path,
        template=service.default_chapter_directory_template(),
        fragment_count=1,
        active_file=active,
        prompt="请写第二章的剧情",
    )

    assert len(targets) == 1
    path = targets[0]["path"]
    assert path.startswith("chapters/第2章 "), path
    assert "第1章" not in path
    assert targets[0]["writeMode"] == "replace"
    assert targets[0]["baselineWordCount"] == 0


def test_planner_still_continues_the_active_chapter(tmp_path: Path) -> None:
    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)
    active = _write_chapter(tmp_path, "第1章 开端")

    targets = service.plan_story_generation_targets(
        tmp_path,
        template=service.default_chapter_directory_template(),
        fragment_count=1,
        active_file=active,
        prompt="继续写下去",
    )

    assert targets[0]["path"] == "chapters/第1章 开端/002.md"


def test_turn_contract_publishes_the_authoritative_chapter_plan(tmp_path: Path) -> None:
    from services.storydex_orchestration_service import get_storydex_orchestration_service

    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)
    active = _write_chapter(tmp_path, "第1章 开端")

    contract = get_storydex_orchestration_service().build_turn_contract(
        tmp_path,
        prompt="请写第二章的剧情",
        active_file=active,
        story_generation={"chapterTemplateId": DEFAULT_CHAPTER_TEMPLATE_ID},
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
    plan = contract["turnPlan"]
    assert plan["chapterAction"] == CHAPTER_ACTION_CREATE_SPECIFIC_CHAPTER
    assert plan["targetChapterNumber"] == 2
    assert plan["authoritativeChapterPath"].startswith("chapters/第2章 ")
    assert plan["authoritativeFragmentPaths"] == [
        target["path"] for target in plan["fragmentTargets"]
    ]
    for fragment_path in plan["authoritativeFragmentPaths"]:
        assert fragment_path.startswith(plan["authoritativeChapterPath"] + "/")


def test_validation_accepts_a_coherent_plan(tmp_path: Path) -> None:
    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)
    _write_chapter(tmp_path, "第1章 开端")

    report = validate_chapter_plan(
        tmp_path,
        action=CHAPTER_ACTION_CREATE_NEXT_CHAPTER,
        target_chapter_number=2,
        authoritative_chapter_path="chapters/第2章 新章",
        fragment_paths=["chapters/第2章 新章/001.md", "chapters/第2章 新章/002.md"],
        chapter_numbers=(1,),
    )
    assert report["passed"] is True
    assert report["issues"] == []


def test_validation_rejects_a_new_chapter_that_does_not_advance(tmp_path: Path) -> None:
    report = validate_chapter_plan(
        tmp_path,
        action=CHAPTER_ACTION_CREATE_NEXT_CHAPTER,
        target_chapter_number=2,
        authoritative_chapter_path="chapters/第2章 新章",
        fragment_paths=["chapters/第2章 新章/001.md"],
        chapter_numbers=(1, 2, 3),
    )
    assert report["passed"] is False
    assert "chapter_number_not_advancing" in report["issues"]


def test_validation_rejects_fragments_spanning_two_chapters(tmp_path: Path) -> None:
    report = validate_chapter_plan(
        tmp_path,
        action=CHAPTER_ACTION_CREATE_NEXT_CHAPTER,
        target_chapter_number=2,
        authoritative_chapter_path="chapters/第2章 新章",
        fragment_paths=["chapters/第2章 新章/001.md", "chapters/第1章 开端/002.md"],
        chapter_numbers=(1,),
    )
    assert report["passed"] is False
    assert "fragments_outside_authoritative_chapter" in report["issues"]


def test_validation_rejects_a_title_number_that_disagrees_with_the_path(tmp_path: Path) -> None:
    report = validate_chapter_plan(
        tmp_path,
        action=CHAPTER_ACTION_CREATE_NEXT_CHAPTER,
        target_chapter_number=3,
        authoritative_chapter_path="chapters/第2章 新章",
        fragment_paths=["chapters/第2章 新章/001.md"],
        chapter_numbers=(1,),
    )
    assert report["passed"] is False
    assert "chapter_number_path_mismatch" in report["issues"]


def test_validation_rejects_duplicate_fragment_paths(tmp_path: Path) -> None:
    report = validate_chapter_plan(
        tmp_path,
        action=CHAPTER_ACTION_CREATE_NEXT_CHAPTER,
        target_chapter_number=2,
        authoritative_chapter_path="chapters/第2章 新章",
        fragment_paths=["chapters/第2章 新章/001.md", "chapters/第2章 新章/001.md"],
        chapter_numbers=(1,),
    )
    assert report["passed"] is False
    assert "duplicate_fragment_paths" in report["issues"]


def test_validation_rejects_a_new_chapter_directory_that_already_exists(tmp_path: Path) -> None:
    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)
    _write_chapter(tmp_path, "第2章 已存在")

    report = validate_chapter_plan(
        tmp_path,
        action=CHAPTER_ACTION_CREATE_SPECIFIC_CHAPTER,
        target_chapter_number=2,
        authoritative_chapter_path="chapters/第2章 已存在",
        fragment_paths=["chapters/第2章 已存在/001.md"],
        chapter_numbers=(1, 2),
    )
    assert report["passed"] is False
    assert "chapter_directory_already_exists" in report["issues"]


def test_validation_rejects_paths_escaping_the_project(tmp_path: Path) -> None:
    report = validate_chapter_plan(
        tmp_path,
        action=CHAPTER_ACTION_CREATE_NEXT_CHAPTER,
        target_chapter_number=2,
        authoritative_chapter_path="chapters/第2章 新章",
        fragment_paths=["../outside.md"],
        chapter_numbers=(1,),
    )
    assert report["passed"] is False
    assert "fragments_outside_authoritative_chapter" in report["issues"]


def test_validation_rejects_a_rewrite_of_a_missing_chapter(tmp_path: Path) -> None:
    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)
    _write_chapter(tmp_path, "第1章 开端")

    report = validate_chapter_plan(
        tmp_path,
        action=CHAPTER_ACTION_REWRITE_EXISTING,
        target_chapter_number=7,
        authoritative_chapter_path="chapters/第7章 缺失",
        fragment_paths=["chapters/第7章 缺失/001.md"],
        chapter_numbers=(1,),
    )
    assert report["passed"] is False
    assert "rewrite_target_missing" in report["issues"]


def test_single_file_plan_validates_against_the_chapter_directory(tmp_path: Path) -> None:
    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)

    report = validate_chapter_plan(
        tmp_path,
        action=CHAPTER_ACTION_CREATE_NEXT_CHAPTER,
        target_chapter_number=1,
        authoritative_chapter_path="chapters/第1章 开端",
        fragment_paths=["chapters/第1章 开端/正文.md"],
        chapter_numbers=(),
    )
    assert report["passed"] is True


def test_single_file_template_plans_a_new_named_chapter(tmp_path: Path) -> None:
    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)
    active = _write_chapter(tmp_path, "第1章 开端", segment="正文.md")

    targets = service.plan_story_generation_targets(
        tmp_path,
        template=service.find_chapter_template(tmp_path, SINGLE_FILE_CHAPTER_TEMPLATE_ID) or {},
        fragment_count=1,
        active_file=active,
        prompt="写第二章",
    )

    assert targets[0]["path"].startswith("chapters/第2章 ")
    assert targets[0]["writeMode"] == "replace"
