import json
from pathlib import Path

import pytest
from coomi.ui.events import ToolDone

from services.coomi_agent_service import _CoomiEventTranslator
from services.storydex_agent_tools import StorydexApplyStoryIncrementTool
from services.story_project_service import StoryProjectService


def _story_knowledge_bytes(workspace_root: Path) -> dict[str, bytes]:
    memory_root = workspace_root / ".storydex" / "memory"
    return {
        path.relative_to(memory_root).as_posix(): path.read_bytes()
        for path in sorted(memory_root.rglob("*"))
        if path.is_file()
    }


def _tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_memory_and_temp_contracts_are_created_without_sessions(tmp_path: Path):
    service = StoryProjectService()
    service.ensure_project_structure(tmp_path)
    memory = tmp_path / ".storydex" / "memory"
    memory_readme = (memory / "README.md").read_text(encoding="utf-8")
    temp_readme = (tmp_path / ".storydex" / "temp" / "README.md").read_text(encoding="utf-8")
    catalog = json.loads((memory / "catalog.json").read_text(encoding="utf-8"))

    assert "严禁" in memory_readme or "禁止" in memory_readme
    assert ".storydex/.agent/sessions" in memory_readme
    assert ".storydex/temp" in memory_readme
    assert catalog == {"schemaVersion": 1, "revision": 0, "modules": []}
    assert (memory / "change-ledger.jsonl").read_text(encoding="utf-8") == ""
    assert (memory / "checkpoints").is_dir()
    assert not any("session" in path.name.lower() for path in memory.rglob("*"))
    assert "没有索引" in temp_readme
    assert "不要读取" in temp_readme


def test_revisioned_memory_change_registers_module_and_ledger(tmp_path: Path):
    service = StoryProjectService()
    service.ensure_project_structure(tmp_path)
    payload = {
        "schema_version": 2,
        "change_set_id": "change-1",
        "base_revision": 0,
        "revision": 1,
        "segment_path": "chapters/第一章/001.md",
        "created_at": "2026-07-11T00:00:00+00:00",
        "operations": [{"op": "set", "path": "characters.hero.state", "value": "清醒", "evidence": "正文明确描写"}],
        "full_state": {"characters": {"hero": {"state": "清醒"}}},
        "snapshot_comment": "主角恢复清醒",
    }
    service.sync_current_state_from_snapshot_payload(tmp_path, ".storydex/memory/chapters/第一章/001.variables.json", payload)
    current = json.loads(service.current_state_master_path(tmp_path).read_text(encoding="utf-8"))
    catalog = json.loads(service.memory_catalog_path(tmp_path).read_text(encoding="utf-8"))
    ledger = [json.loads(line) for line in service.memory_change_ledger_path(tmp_path).read_text(encoding="utf-8").splitlines()]
    assert current["schemaVersion"] == 2 and current["revision"] == 1
    assert catalog["modules"][0]["id"] == "current-state"
    assert ledger[-1]["changeSetId"] == "change-1"
    assert ledger[-1]["sourcePath"] == "chapters/第一章/001.md"

    conflicting = dict(payload, change_set_id="change-2", base_revision=0, revision=2)
    try:
        service.sync_current_state_from_snapshot_payload(tmp_path, ".storydex/memory/chapters/第一章/002.variables.json", conflicting)
    except ValueError as exc:
        assert "revision conflict" in str(exc).lower()
    else:
        raise AssertionError("revision conflict must be rejected")


def test_explicit_review_required_command_is_reported_without_writing_story_knowledge(tmp_path: Path):
    service = StoryProjectService()
    service.ensure_project_structure(tmp_path)
    before = _story_knowledge_bytes(tmp_path)

    result = service.apply_story_generation_increment(
        tmp_path,
        {
            "segmentPath": "chapters/第一章/001.md",
            "applyVariables": True,
            "variableUpdates": [
                {
                    "op": "set",
                    "path": "characters.hero.state",
                    "value": "清醒",
                    "evidence": "正文明确描写",
                    "requiresReview": True,
                }
            ],
        },
    )

    assert result["knowledgeReview"] == {
        "status": "review_required",
        "code": "knowledge_review_required",
        "items": [
            {
                "path": "characters.hero.state",
                "op": "set",
                "reasons": ["explicit_requires_review"],
            }
        ],
        "appliedCount": 0,
    }
    assert result["fragments"][0]["snapshotWritten"] is False
    assert _story_knowledge_bytes(tmp_path) == before


@pytest.mark.parametrize(
    ("operation", "expected_reason"),
    [
        (
            {"op": "remove", "path": "characters.hero.state", "evidence": "用户要求删除旧状态"},
            "remove_operation",
        ),
        (
            {"op": "set", "path": "characters.hero.state", "value": "清醒", "evidence": ""},
            "missing_evidence",
        ),
    ],
)
def test_implicit_review_required_commands_do_not_write_story_knowledge(
    tmp_path: Path,
    operation: dict,
    expected_reason: str,
):
    service = StoryProjectService()
    service.ensure_project_structure(tmp_path)
    before = _story_knowledge_bytes(tmp_path)

    result = service.apply_story_generation_increment(
        tmp_path,
        {
            "segmentPath": "chapters/第一章/001.md",
            "applyVariables": True,
            "variableUpdates": [operation],
        },
    )

    assert result["knowledgeReview"]["items"] == [
        {
            "path": "characters.hero.state",
            "op": operation["op"],
            "reasons": [expected_reason],
        }
    ]
    assert result["knowledgeReview"]["appliedCount"] == 0
    assert _story_knowledge_bytes(tmp_path) == before


def test_mixed_knowledge_command_batch_only_persists_accepted_operations(tmp_path: Path):
    service = StoryProjectService()
    service.ensure_project_structure(tmp_path)

    result = service.apply_story_generation_increment(
        tmp_path,
        {
            "segmentPath": "chapters/第一章/001.md",
            "applyVariables": True,
            "variableUpdates": [
                {
                    "op": "set",
                    "path": "characters.hero.state",
                    "value": "清醒",
                    "evidence": "第一章明确描写主角苏醒",
                },
                {
                    "op": "remove",
                    "path": "characters.villain.state",
                    "evidence": "第一章未再提及反派",
                },
            ],
        },
    )

    snapshot = json.loads((tmp_path / result["fragments"][0]["snapshotPath"]).read_text(encoding="utf-8"))
    current = json.loads(service.current_state_master_path(tmp_path).read_text(encoding="utf-8"))
    ledger = [
        json.loads(line)
        for line in service.memory_change_ledger_path(tmp_path).read_text(encoding="utf-8").splitlines()
    ]
    assert snapshot["operations"] == [
        {
            "op": "set",
            "path": "characters.hero.state",
            "value": "清醒",
            "evidence": "第一章明确描写主角苏醒",
            "confidence": 1.0,
            "requiresReview": False,
        }
    ]
    assert current["fullState"] == {"characters": {"hero": {"state": "清醒"}}}
    assert ledger[-1]["operationCount"] == 1
    assert result["knowledgeReview"] == {
        "status": "review_required",
        "code": "knowledge_review_required",
        "items": [
            {
                "path": "characters.villain.state",
                "op": "remove",
                "reasons": ["remove_operation"],
            }
        ],
        "appliedCount": 1,
    }


def test_all_review_required_batch_does_not_refresh_wiki_projection(tmp_path: Path):
    service = StoryProjectService()
    service.ensure_project_structure(tmp_path)
    before_memory = _story_knowledge_bytes(tmp_path)
    wiki_root = tmp_path / ".storydex" / "wiki"
    before_wiki = _tree_bytes(wiki_root)

    result = service.apply_story_generation_increment(
        tmp_path,
        {
            "segmentPath": "chapters/第一章/001.md",
            "applyVariables": True,
            "applyWiki": True,
            "variableThoughts": "待审命令不应进入变量思考文件。",
            "variableUpdates": [
                {
                    "op": "set",
                    "path": "characters.hero.state",
                    "value": "清醒",
                    "evidence": "正文明确描写",
                    "requiresReview": True,
                }
            ],
        },
    )

    assert result["applied"]["wiki"] is False
    assert result["fragments"][0]["variableThoughtWritten"] is False
    assert _story_knowledge_bytes(tmp_path) == before_memory
    assert _tree_bytes(wiki_root) == before_wiki


def test_review_required_result_is_preserved_in_tool_done_trace_without_permission_request(tmp_path: Path):
    tool = StorydexApplyStoryIncrementTool(workspace_root=tmp_path)
    tool_result = tool.run(
        {
            "segmentPath": "chapters/第一章/001.md",
            "applyVariables": True,
            "variableUpdates": [
                {
                    "op": "remove",
                    "path": "characters.hero.state",
                    "evidence": "用户提出删除，但删除仍需复核",
                }
            ],
        }
    )
    knowledge_review = json.loads(tool_result.output)["knowledgeReview"]

    translated = _CoomiEventTranslator(session_id="session-1").translate(
        ToolDone(
            tool_name=tool.name,
            elapsed=0.01,
            result_preview=tool_result.output[:500],
        )
    )

    assert tool.requires_confirmation is False
    assert translated is not None
    event_name, trace_data = translated
    assert event_name == "ToolDone"
    assert trace_data["knowledge_review"] == knowledge_review


def test_non_command_snapshot_updates_keep_existing_write_behavior(tmp_path: Path):
    service = StoryProjectService()

    result = service.apply_story_generation_increment(
        tmp_path,
        {
            "segmentPath": "chapters/第一章/001.md",
            "applyVariables": True,
            "memoryUpdates": [{"summary": "主角记住了旧桥的位置"}],
        },
    )

    assert result["fragments"][0]["snapshotWritten"] is True
    assert "knowledgeReview" not in result
    snapshot_path = tmp_path / result["fragments"][0]["snapshotPath"]
    assert json.loads(snapshot_path.read_text(encoding="utf-8"))["memory_updates"] == [
        {"summary": "主角记住了旧桥的位置"}
    ]
