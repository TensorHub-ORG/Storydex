import json
from pathlib import Path

from services.story_project_service import StoryProjectService


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
