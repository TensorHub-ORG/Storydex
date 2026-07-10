from __future__ import annotations

import base64
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import routes_file, routes_story, routes_wiki
from main import app
from services.git_service import GitService


pytestmark = pytest.mark.integration


@pytest.fixture
def workspace_client(tmp_path: Path, monkeypatch):
    root = tmp_path / "story"
    root.mkdir()
    project = routes_file.project_service
    monkeypatch.setattr(project, "_current_workspace_root", root.resolve())
    monkeypatch.setattr(project, "_default_workspace_root", root.resolve())
    monkeypatch.setattr(project.global_config, "record_recent_project", lambda **kwargs: None)
    project.ensure_project_structure(root)
    monkeypatch.setattr(routes_story, "project_service", project)
    monkeypatch.setattr(routes_wiki, "project_service", project)
    monkeypatch.setattr(routes_file.editor_service.workspace, "project_service", project)
    monkeypatch.setattr(routes_file.editor_service.workspace, "story_project_service", routes_file.story_project_service)
    executable = shutil.which("git")
    if executable:
        monkeypatch.setenv("STORYDEX_GIT_EXECUTABLE", executable)
        GitService._resolve_git_executable.cache_clear()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, root, project
    GitService._resolve_git_executable.cache_clear()


def ok(response, status=200):
    assert response.status_code == status, response.text
    payload = response.json()
    assert payload["ok"] is True, payload
    return payload["data"]


def test_project_and_workspace_file_crud_contract(workspace_client):
    client, root, project = workspace_client
    current = ok(client.get("/api/v1/workspace/project"))
    assert Path(current["workspaceRoot"]) == root
    tree = ok(client.get("/api/v1/workspace/tree"))
    assert tree["hasStorydexConfig"] is True and tree["roots"]

    created = ok(client.post("/api/v1/workspace/file/create", json={"relativePath": "notes/a.md", "content": "hello"}))
    assert created["relativePath"] == "notes/a.md"
    partial = ok(client.post("/api/v1/file/read", json={"relativePath": "notes/a.md", "offset": 0, "limit": 2}))
    assert partial["content"].startswith("he")
    assert partial["size"] == 5
    written = ok(client.post("/api/v1/file/write", json={"relativePath": "notes/a.md", "content": "updated"}))
    assert written["content"] == "updated"

    directory = ok(client.post("/api/v1/workspace/directory/create", json={"relativePath": "imports"}))
    assert directory["kind"] == "directory"
    encoded = base64.b64encode("中文内容".encode()).decode()
    imported = ok(client.post("/api/v1/workspace/files/import", json={
        "targetDirectory": "imports",
        "files": [{"name": "sample.txt", "contentBase64": f"data:text/plain;base64,{encoded}"}],
    }))
    assert imported["items"][0]["relativePath"] == "imports/sample.txt"

    renamed = ok(client.post("/api/v1/workspace/path/rename", json={"fromRelativePath": "notes/a.md", "toRelativePath": "notes/b.md"}))
    assert renamed["relativePath"] == "notes/b.md"
    copied = ok(client.post("/api/v1/workspace/path/copy", json={"fromRelativePath": "notes/b.md", "toRelativePath": "notes/c.md"}))
    assert copied["relativePath"] == "notes/c.md"
    moved = ok(client.post("/api/v1/workspace/path/move", json={"fromRelativePath": "notes/c.md", "toRelativePath": "imports/c.md"}))
    assert moved["relativePath"] == "imports/c.md"
    deleted = ok(client.post("/api/v1/workspace/path/delete", json={"relativePath": "imports/c.md"}))
    assert deleted["relativePath"] == "imports/c.md"
    assert not (root / "imports" / "c.md").exists()
    diagnostics = ok(client.post("/api/v1/workspace/diagnostics", json={"relativePaths": ["notes/b.md", "missing.md"]}))
    assert isinstance(diagnostics["items"], list)

    new_project = root.parent / "created-project"
    created_project = ok(client.post("/api/v1/workspace/project/create", json={"projectPath": str(new_project)}))
    assert Path(created_project["workspaceRoot"]) == new_project
    opened = ok(client.post("/api/v1/workspace/project/open", json={"projectPath": str(root)}))
    assert Path(opened["workspaceRoot"]) == root
    initialized = ok(client.post("/api/v1/workspace/project/initialize", json={"projectPath": str(root)}))
    assert initialized["requiresInitialization"] is False
    assert project.workspace_root == root.resolve()


@pytest.mark.security
@pytest.mark.parametrize("relative_path", ["../escape.md", "..\\escape.md", "/absolute.md", "C:\\absolute.md"])
def test_workspace_api_rejects_path_escape(workspace_client, relative_path):
    client, root, _ = workspace_client
    response = client.post("/api/v1/file/write", json={"relativePath": relative_path, "content": "escape"})
    assert response.status_code in {400, 403, 422}
    assert response.json()["ok"] is False
    assert not (root.parent / "escape.md").exists()


def test_workspace_git_endpoints_use_local_only_repository(workspace_client):
    client, root, _ = workspace_client
    if not shutil.which("git"):
        pytest.skip("git unavailable")
    initialized = ok(client.post("/api/v1/workspace/git/init"))
    assert initialized["initialized"] is True and initialized["branch"] == "develop"
    (root / "chapters" / "001.md").write_text("first", encoding="utf-8")
    diff = ok(client.get("/api/v1/workspace/git/diff"))
    assert diff["totals"]["files"] >= 1
    first = ok(client.post("/api/v1/workspace/git/commit", json={"message": "story: first"}))
    assert first["created"] is True
    first_id = first["commit"]["id"]
    (root / "chapters" / "001.md").write_text("second", encoding="utf-8")
    second = ok(client.post("/api/v1/workspace/git/commit", json={"message": "story: second"}))
    assert second["created"] is True
    restored = ok(client.post("/api/v1/workspace/git/restore", json={"commitId": first_id, "createBackup": True}))
    assert restored["restored"] is True
    summary = ok(client.get("/api/v1/workspace/git/summary"))
    assert summary["initialized"] is True
    assert not (root / ".git" / "config").read_text(encoding="utf-8").lower().count("remote ")


def test_story_settings_templates_chapters_and_state_contract(workspace_client):
    client, root, _ = workspace_client
    settings = ok(client.get("/api/v1/story/settings"))
    updated = ok(client.put("/api/v1/story/settings", json={
        "storySegmentFormat": "txt",
        "defaultDialogueQuote": "“”",
        "segmentNamingMode": "numeric",
        "maxSegmentsPerChapter": 4,
        "storyFragmentCount": 2,
        "storyFragmentWordCount": 1200,
        "autoUpdateVariables": True,
        "autoUpdateWiki": True,
        "agentCommitPromptEnabled": False,
        "autoNameChapterTitle": True,
        "contextConcisionMinCalls": 1,
        "contextConcisionMaxCalls": 3,
        "contextConcisionMaxInputTokens": 16000,
    }))
    assert updated["storySegmentFormat"] == "txt" and updated["storyFragmentCount"] == 2
    assert settings["settingsPath"] and updated["currentStateRoot"]

    template = ok(client.get("/api/v1/story/templates/character"))
    assert template["markdown"]
    changed_template = ok(client.put("/api/v1/story/templates/character", json={"markdown": "# 角色模板\n\n## 姓名\n- 测试"}))
    assert "角色模板" in changed_template["markdown"]
    chapter_templates = ok(client.get("/api/v1/story/templates/chapters"))
    assert chapter_templates["items"]

    chapter = root / "chapters" / "第一章"
    chapter.mkdir(parents=True, exist_ok=True)
    (chapter / "001.txt").write_text("opening", encoding="utf-8")
    chapters = ok(client.get("/api/v1/story/chapters"))
    chapter_path = next(item["relativePath"] for item in chapters["items"] if item["relativePath"].startswith("chapters/"))
    completion = ok(client.post("/api/v1/story/chapter-completion", json={"chapterRelativePath": chapter_path, "completed": True}))
    assert completion["completed"] is True
    progress = ok(client.get("/api/v1/story/chapter-progress"))
    assert progress["chapters"]
    current = ok(client.get("/api/v1/story/current-state"))
    assert current["currentStatePath"]


def test_story_increment_snapshot_memory_wiki_and_sync(workspace_client):
    client, root, _ = workspace_client
    service = routes_story.story_project_service
    result = service.apply_story_generation_increment(root, {
        "prompt": "继续第一章",
        "applyVariables": True,
        "applyWiki": True,
        "chapterSummary": "林舟抵达云港，并获得铜钥匙。",
        "fragments": [
            {
                "path": "chapters/第一章/001.md",
                "text": "林舟抵达云港。",
                "variableThoughts": [{"人物": "林舟抵达云港", "物品": ["铜钥匙"]}],
                "variableUpdates": [{"op": "set", "path": "plot.location", "value": "云港"}],
                "characterUpdates": [{
                    "character": "林舟", "role": "主角", "summary": "来自北境的旅人",
                    "state": {"location": "云港", "mood": "警觉"}, "aliases": ["阿舟"],
                }],
                "factUpdates": [{"subject": "林舟", "predicate": "位于", "object": "云港", "evidence": "001"}],
                "relationshipUpdates": [{"source": "林舟", "target": "守门人", "delta": "increase", "magnitude": "minor", "detail": "得到帮助"}],
                "itemUpdates": [{"item": "铜钥匙", "owner": "林舟", "status": "active", "summary": "可开启旧仓库"}],
            },
            {"path": "chapters/第一章/002.md", "text": "他用铜钥匙打开旧仓库。"},
        ],
    })
    assert result["ok"] is True and result["applied"]["variables"] is True
    assert result["fragments"][0]["snapshotWritten"] is True
    assert result["fragments"][0]["variableThoughtWritten"] is True
    assert result["chapterSummaryPath"]
    assert any(path.endswith("entities.json") for path in result["writtenPaths"])
    assert any(path.endswith("facts.json") for path in result["writtenPaths"])
    assert result["applied"]["relationships"] is True, result
    assert any(path.endswith("items.json") for path in result["writtenPaths"])

    latest = ok(client.get("/api/v1/story/snapshots/latest"))
    assert latest["relativePath"] and latest["snapshot"]
    synced = ok(client.post("/api/v1/story/current-state/sync"))
    assert synced["writtenPaths"] and synced["latestSnapshotPath"] == latest["relativePath"]
    current = ok(client.get("/api/v1/story/current-state"))
    assert current["data"]["latestSnapshotPath"]

    wiki = ok(client.get("/api/v1/story/wiki"))
    assert isinstance(wiki.get("entries"), list)
    rebuilt = ok(client.post("/api/v1/story/wiki/rebuild"))
    assert isinstance(rebuilt.get("graph"), dict)
    synced_wiki = ok(client.post("/api/v1/story/wiki/sync"))
    assert isinstance(synced_wiki.get("entries"), list)
    graph = ok(client.get("/api/v1/story/wiki/graph?q=林舟&depth=2&limit=20"))
    assert graph.get("mode")


def test_story_increment_requires_decisions_and_rejects_unsafe_path(workspace_client):
    _, root, _ = workspace_client
    service = routes_story.story_project_service
    pending = service.apply_story_generation_increment(root, {
        "segmentPath": "chapters/001.md",
        "segmentText": "draft",
        "applyVariables": False,
        "variableUpdates": [{"op": "set", "path": "x", "value": 1}],
    })
    assert pending["applied"]["variables"] is False
    assert pending["requiredDecisions"][0]["type"] == "update_variables"
    with pytest.raises(Exception):
        service.apply_story_generation_increment(root, {"segmentPath": "../escape.md", "segmentText": "bad"})


def test_story_autopilot_happy_invalid_corrupt_abort_and_done(workspace_client):
    client, root, _ = workspace_client
    invalid = ok(client.post("/api/v1/story/autopilot/start", json={"promptTemplate": "", "maxSegments": 0}))
    assert invalid["ok"] is False
    started = ok(client.post("/api/v1/story/autopilot/start", json={"promptTemplate": "继续 {index}", "maxSegments": 2, "activeFile": "chapters/001.md"}))
    run_id = started["runId"]
    assert ok(client.get(f"/api/v1/story/autopilot/{run_id}/status"))["state"]["status"] == "queued"
    running = ok(client.post(f"/api/v1/story/autopilot/{run_id}/advance", json={"outcome": "ok", "note": "first"}))
    assert running["state"]["status"] == "running"
    done = ok(client.post(f"/api/v1/story/autopilot/{run_id}/advance", json={"outcome": "ok"}))
    assert done["state"]["status"] == "done"
    assert ok(client.get("/api/v1/story/autopilot/missing/status"))["reason"] == "run_not_found"
    assert ok(client.post("/api/v1/story/autopilot/missing/advance", json={}))["reason"] == "run_not_found"

    aborted = ok(client.post("/api/v1/story/autopilot/start", json={"promptTemplate": "x", "maxSegments": 20}))
    abort_state = ok(client.post(f"/api/v1/story/autopilot/{aborted['runId']}/advance", json={"outcome": "abort"}))
    assert abort_state["state"]["status"] == "aborted"
    state_path = routes_story.story_project_service.agent_root(root) / "autopilot" / aborted["runId"] / "state.json"
    state_path.write_text("{broken", encoding="utf-8")
    assert ok(client.get(f"/api/v1/story/autopilot/{aborted['runId']}/status"))["reason"] == "state_corrupted"
    assert ok(client.post(f"/api/v1/story/autopilot/{aborted['runId']}/advance", json={}))["reason"] == "state_corrupted"


def test_story_evolution_conflict_resolution_rollback_and_undo(workspace_client):
    client, root, _ = workspace_client
    chapter = root / "chapters" / "chapter-1"
    chapter.mkdir(parents=True, exist_ok=True)
    for number in (1, 2, 3):
        (chapter / f"{number:03d}.md").write_text(f"segment {number}", encoding="utf-8")
    current = routes_story.story_project_service.storydex_root(root) / "memory" / "current"
    current.mkdir(parents=True, exist_ok=True)
    entries = [{"segment_id": f"{n:03d}", "value": n} for n in (1, 2, 3)]
    for name in ("change_ledger.json", "timeline.json", "character_conflicts.json"):
        (current / name).write_text(__import__("json").dumps({"entries": entries}), encoding="utf-8")
    (current / "relationship_graph.json").write_text(__import__("json").dumps({"nodes": [], "edges": [{
        "source": "a", "target": "b", "history": [
            {"segment_id": "001", "delta": "increase", "magnitude": "minor"},
            {"segment_id": "003", "delta": "decrease", "magnitude": "major"},
        ],
    }]}), encoding="utf-8")
    (current / "foreshadow_ledger.json").write_text(__import__("json").dumps({"threads": {"t": {
        "planted_at": {"segment_id": "001"}, "callbacks": [{"segment_id": "003"}],
        "resolved_at": {"segment_id": "003"}, "status": "resolved",
    }}}), encoding="utf-8")
    (current / "chapter_outline.json").write_text(__import__("json").dumps({"chapters": {"1": {"milestones": entries}}}), encoding="utf-8")

    evolution = ok(client.get("/api/v1/story/evolution-snapshot"))
    assert evolution["changeLedger"]["entries"] and isinstance(evolution["relationshipGraph"]["edges"], list)
    assert ok(client.post("/api/v1/story/rollback", json={}))["reason"] == "missing_target"
    assert ok(client.post("/api/v1/story/rollback", json={"target_segment_relative_path": "chapters/missing.md"}))["reason"] == "target_not_found"
    rolled = ok(client.post("/api/v1/story/rollback", json={
        "target_segment_relative_path": "chapters/chapter-1/001.md", "keep_target": True,
    }))
    assert rolled["ok"] is True and rolled["deletedSegmentCount"] == 2
    assert not (chapter / "002.md").exists() and (chapter / "001.md").exists()
    assert ok(client.post("/api/v1/story/rollback/undo", json={}))["reason"] == "missing_rollback_id"
    assert ok(client.post("/api/v1/story/rollback/undo", json={"rollbackId": "missing"}))["reason"] == "backup_not_found"
    undone = ok(client.post("/api/v1/story/rollback/undo", json={"rollbackId": rolled["rollbackId"]}))
    assert undone["ok"] is True and (chapter / "003.md").exists()

    cards = routes_story.story_project_service.storydex_root(root) / "characters" / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / "hero.json").write_text('{"background":"old"}', encoding="utf-8")
    conflicts = current / "character_conflicts.json"
    conflicts.write_text('{"entries":[{"character_id":"hero","field":"background","incoming":"new"},"bad"]}', encoding="utf-8")
    assert ok(client.post("/api/v1/story/character-conflicts/resolve", json={"entryIndex": 0, "decision": "bad"}))["reason"] == "invalid_decision"
    assert ok(client.post("/api/v1/story/character-conflicts/resolve", json={"entryIndex": 9, "decision": "dismiss"}))["reason"] == "entry_not_found"
    assert ok(client.post("/api/v1/story/character-conflicts/resolve", json={"entryIndex": 1, "decision": "dismiss"}))["reason"] == "entry_invalid"
    resolved = ok(client.post("/api/v1/story/character-conflicts/resolve", json={"entryIndex": 0, "decision": "accept_incoming"}))
    assert resolved["applied"] is True
    assert "new" in (cards / "hero.json").read_text(encoding="utf-8")
