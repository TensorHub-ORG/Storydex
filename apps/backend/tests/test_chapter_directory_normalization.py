"""章节落盘规范化的回归测试。

背景：旧版规范名用中文数字（第一章），默认模板与 LLM 落盘路径用
阿拉伯数字（第1章），`_normalize_chapter_directories` 的重命名与
增量写入互相打架，每轮生成都会留下一个同章号的空目录。

覆盖：
1. 规范名与新章命名统一为阿拉伯数字。
2. 同章号异体命名的落盘路径归一到现有目录，不再分裂。
3. 与非空章节同章号的空目录被自动清理；章号唯一的空目录保留。
"""
from __future__ import annotations

import json
import os

from services.storydex_agent_tools import StorydexApplyStoryIncrementTool
from services.story_project_service import get_story_project_service


def _write_segment(root, chapter: str, name: str = "001.md", text: str = "正文内容。") -> None:
    chapter_dir = root / "chapters" / chapter
    chapter_dir.mkdir(parents=True, exist_ok=True)
    (chapter_dir / name).write_text(text, encoding="utf-8")


def test_display_name_uses_arabic_numerals(tmp_path):
    service = get_story_project_service()
    assert service._build_chapter_display_name("第一章 苏家少年") == "第1章 苏家少年"
    assert service._build_chapter_display_name("第十二章 决战") == "第12章 决战"
    assert service._build_new_chapter_name(3, title="风起") == "第3章 风起"


def test_new_chapter_path_matches_template_style(tmp_path):
    service = get_story_project_service()
    _write_segment(tmp_path, "第1章 苏家少年")
    _write_segment(tmp_path, "第1章 苏家少年", name="002.md")
    _write_segment(tmp_path, "第1章 苏家少年", name="003.md")
    # 第1章已写满（默认每章 3 段），新章应延续阿拉伯数字风格
    next_path = service.compute_next_segment_path(tmp_path)
    assert next_path.startswith("chapters/第2章 ")


def test_variant_chapter_name_redirects_to_existing_dir(tmp_path):
    service = get_story_project_service()
    _write_segment(tmp_path, "第1章 苏家少年")
    settings = service.read_project_settings(tmp_path)
    resolved = service._resolve_story_increment_segment_path(
        tmp_path,
        {"path": "chapters/第一章 苏家少年/002.md"},
        active_file="",
        prompt="",
        settings=settings,
    )
    assert resolved == "chapters/第1章 苏家少年/002.md"
    # 磁盘上不应出现第二个章节目录
    assert not (tmp_path / "chapters" / "第一章 苏家少年").exists()


def test_apply_increment_does_not_leave_empty_duplicate_dirs(tmp_path):
    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)
    result = service.apply_story_generation_increment(
        tmp_path,
        {
            "fragments": [
                {"path": "chapters/第1章 苏家少年/001.md", "text": "少年立于山巅。"}
            ]
        },
    )
    assert result["ok"] is True
    # 模拟旧 bug / LLM mkdir 留下的同章号空目录
    (tmp_path / "chapters" / "第一章 苏家少年").mkdir()
    result = service.apply_story_generation_increment(
        tmp_path,
        {
            "fragments": [
                {"path": "chapters/第一章 苏家少年/002.md", "text": "他转身下山。"}
            ]
        },
    )
    assert result["ok"] is True
    chapters = sorted(p.name for p in (tmp_path / "chapters").iterdir() if p.is_dir())
    assert chapters == ["第1章 苏家少年"]
    assert (tmp_path / "chapters" / "第1章 苏家少年" / "002.md").exists()


def test_prune_keeps_unique_number_empty_chapter(tmp_path):
    service = get_story_project_service()
    _write_segment(tmp_path, "第1章 苏家少年")
    # 用户手动新建的待写作章节（章号唯一）不能被清理
    (tmp_path / "chapters" / "第2章 青州城").mkdir()
    # 与第1章同号的空目录应被清理
    (tmp_path / "chapters" / "第一章 苏家少年").mkdir()
    removed = service._prune_duplicate_empty_chapter_dirs(tmp_path)
    assert removed == ["chapters/第一章 苏家少年"]
    assert (tmp_path / "chapters" / "第2章 青州城").exists()


def test_story_diagnostics_do_not_require_optional_fragment_memory(tmp_path):
    service = get_story_project_service()
    _write_segment(tmp_path, "第1章 序章")

    diagnostics = service.collect_story_diagnostics(tmp_path)

    assert diagnostics == {}


def test_fact_memory_update_is_not_reported_as_missing_fragment_snapshot(tmp_path):
    service = get_story_project_service()
    result = service.apply_story_generation_increment(
        tmp_path,
        {
            "segmentPath": "chapters/第1章 序章/001.md",
            "segmentText": "林舟在渡口认出了沈青。",
            "applyVariables": True,
            "factUpdates": [
                {
                    "subject": "林舟",
                    "predicate": "认识",
                    "object": "沈青",
                    "evidence": "林舟在渡口认出了沈青。",
                }
            ],
        },
    )

    assert result["applied"]["facts"] is True
    assert ".storydex/memory/current/facts.json" in result["writtenPaths"]
    assert service.collect_story_diagnostics(tmp_path) == {}


def test_existing_fragment_memory_is_still_reported_when_stale(tmp_path):
    service = get_story_project_service()
    _write_segment(tmp_path, "第1章 序章")
    segment = tmp_path / "chapters" / "第1章 序章" / "001.md"
    thought = tmp_path / service.variable_thought_relative_path(
        tmp_path,
        "chapters/第1章 序章/001.md",
    )
    thought.parent.mkdir(parents=True, exist_ok=True)
    thought.write_text("# 变量思考\n", encoding="utf-8")
    old_time = min(segment.stat().st_mtime, thought.stat().st_mtime) - 10
    os.utime(thought, (old_time, old_time))

    diagnostics = service.collect_story_diagnostics(tmp_path)

    assert [item["code"] for item in diagnostics["chapters/第1章 序章/001.md"]] == [
        "story_snapshot_stale"
    ]
    assert [item["code"] for item in diagnostics["chapters/第1章 序章"]] == [
        "story_snapshot_stale_in_chapter"
    ]


def test_increment_paths_follow_noncanonical_chapter_directory_rename(tmp_path):
    service = get_story_project_service()
    result = service.apply_story_generation_increment(
        tmp_path,
        {
            "segmentPath": "chapters/Prologue/001.md",
            "segmentText": "林舟抵达渡口。",
            "applyVariables": True,
            "variableUpdates": [
                {
                    "op": "set",
                    "path": "plot.location",
                    "value": "渡口",
                    "evidence": "林舟抵达渡口。",
                }
            ],
        },
    )

    fragment = result["fragments"][0]
    assert fragment["segmentPath"] == "chapters/第1章 Prologue/001.md"
    assert fragment["snapshotPath"] == ".storydex/memory/chapters/第1章 Prologue/001.variables.json"
    assert (tmp_path / fragment["segmentPath"]).is_file()
    assert (tmp_path / fragment["snapshotPath"]).is_file()
    assert not (tmp_path / ".storydex" / "memory" / "chapters" / "Prologue").exists()
    assert service.collect_story_diagnostics(tmp_path) == {}


def test_multiple_fragments_reuse_normalized_chapter_alias(tmp_path):
    service = get_story_project_service()
    result = service.apply_story_generation_increment(
        tmp_path,
        {
            "fragments": [
                {"path": "chapters/Prologue/001.md", "text": "林舟抵达渡口。"},
                {"path": "chapters/Prologue/002.md", "text": "沈青随后现身。"},
            ]
        },
    )

    assert [fragment["segmentPath"] for fragment in result["fragments"]] == [
        "chapters/第1章 Prologue/001.md",
        "chapters/第1章 Prologue/002.md",
    ]
    chapter_dirs = [path.name for path in (tmp_path / "chapters").iterdir() if path.is_dir()]
    assert chapter_dirs == ["第1章 Prologue"]


def test_generated_increment_auto_applies_memory_without_apply_variables_flag(tmp_path):
    service = get_story_project_service()
    result = service.apply_story_generation_increment(
        tmp_path,
        {
            "segmentPath": "chapters/Chapter1/001.md",
            "segmentText": "A plot event moves the hero to the harbor.",
            "variableThoughts": "The location changes are explicit in the generated fragment.",
            "variableUpdates": [
                {
                    "op": "set",
                    "path": "plot.location",
                    "value": "harbor",
                    "evidence": "The hero arrives at the harbor.",
                }
            ],
            "factUpdates": [
                {
                    "subject": "hero",
                    "predicate": "located_at",
                    "object": "harbor",
                    "evidence": "The hero arrives at the harbor.",
                }
            ],
            "relationshipUpdates": [
                {
                    "source": "hero",
                    "target": "guide",
                    "delta": "increase",
                    "detail": "The guide helps the hero.",
                    "evidence": "The guide helps the hero.",
                }
            ],
            "itemUpdates": [
                {
                    "item": "harbor key",
                    "owner": "hero",
                    "summary": "Opens the old storehouse.",
                    "evidence": "The hero carries the harbor key.",
                }
            ],
        },
    )

    assert result["applied"]["variables"] is True
    assert result["applied"]["facts"] is True
    assert result["applied"]["relationships"] is True
    assert result["applied"]["items"] is True
    assert not any(item["type"] == "update_variables" for item in result["requiredDecisions"])
    assert result["fragments"][0]["snapshotWritten"] is True
    assert result["fragments"][0]["variableThoughtWritten"] is True

    facts = json.loads((tmp_path / ".storydex/memory/current/facts.json").read_text(encoding="utf-8"))
    relationships = json.loads(
        (tmp_path / ".storydex/memory/current/relationship_graph.json").read_text(encoding="utf-8")
    )
    items = json.loads((tmp_path / ".storydex/memory/current/items.json").read_text(encoding="utf-8"))
    assert facts["facts"][0]["subject"] == "hero"
    assert relationships["edges"][0]["source"] == "hero"
    assert items["items"][0]["name"] == "harbor key"


def test_storydex_tool_entrypoint_auto_applies_generated_memory(tmp_path):
    tool = StorydexApplyStoryIncrementTool(workspace_root=tmp_path)
    result = json.loads(
        tool.run(
            {
                "segmentPath": "chapters/Chapter1/001.md",
                "segmentText": "The hero recognizes the guide at the harbor.",
                "variableThoughts": "The fragment explicitly establishes the location.",
                "variableUpdates": [
                    {
                        "op": "set",
                        "path": "plot.location",
                        "value": "harbor",
                        "evidence": "The fragment says the hero is at the harbor.",
                    }
                ],
                "factUpdates": [
                    {
                        "subject": "hero",
                        "predicate": "knows",
                        "object": "guide",
                        "evidence": "The fragment says the hero recognizes the guide.",
                    }
                ],
            }
        ).output
    )

    assert result["applied"]["variables"] is True
    assert result["applied"]["facts"] is True
    assert result["fragments"][0]["snapshotWritten"] is True
    assert not any(item["type"] == "update_variables" for item in result["requiredDecisions"])
    assert get_story_project_service().collect_story_diagnostics(tmp_path) == {}


def test_explicit_false_still_defers_generated_memory_updates(tmp_path):
    service = get_story_project_service()
    result = service.apply_story_generation_increment(
        tmp_path,
        {
            "segmentPath": "chapters/Chapter1/001.md",
            "segmentText": "A plot event.",
            "applyVariables": False,
            "factUpdates": [
                {
                    "subject": "hero",
                    "predicate": "knows",
                    "object": "guide",
                    "evidence": "The generated text states this.",
                }
            ],
        },
    )

    assert result["applied"]["variables"] is False
    assert result["applied"]["facts"] is False
    assert any(item["type"] == "update_variables" for item in result["requiredDecisions"])
    facts = json.loads((tmp_path / ".storydex/memory/current/facts.json").read_text(encoding="utf-8"))
    assert facts["facts"] == []


def test_generated_increment_auto_apply_keeps_review_required_operations_pending(tmp_path):
    service = get_story_project_service()
    result = service.apply_story_generation_increment(
        tmp_path,
        {
            "segmentPath": "chapters/Chapter1/001.md",
            "segmentText": "The old status is no longer mentioned.",
            "variableUpdates": [
                {
                    "op": "remove",
                    "path": "characters.hero.status",
                    "evidence": "The generated fragment no longer mentions it.",
                }
            ],
        },
    )

    assert result["applied"]["variables"] is True
    assert result["knowledgeReview"]["status"] == "review_required"
    assert result["knowledgeReview"]["items"][0]["reasons"] == ["remove_operation"]
    assert result["fragments"][0]["snapshotWritten"] is False
    current = json.loads(service.current_state_master_path(tmp_path).read_text(encoding="utf-8"))
    assert current["revision"] == 0
    assert current["fullState"] == {}
