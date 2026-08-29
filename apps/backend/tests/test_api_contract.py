from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import routes_help, routes_sys
from main import app


pytestmark = pytest.mark.contract


class _Project:
    workspace_root = Path("/").resolve()

    def current_project(self):
        return {
            "workspaceRoot": self.workspace_root.as_posix(),
            "storydexRoot": (self.workspace_root / ".storydex").as_posix(),
            "projectName": "isolated",
            "hasStorydexConfig": False,
            "requiresInitialization": True,
            "missingDirectories": ["chapters"],
        }


class _GlobalConfig:
    def __init__(self, root: Path):
        self.root = root
        self.preferences = {
            "theme": "default",
            "activeActivity": "resources",
            "workbenchMode": "storydex",
            "sidebarWidth": 320,
            "sidebarCollapsed": False,
            "agentCollapsed": False,
            "agentWidth": 560,
            "leftPaneFontScale": 100,
            "centerPaneFontScale": 100,
            "rightPaneFontScale": 100,
            "fileFontSize": 16,
            "playerFontSize": 14,
            "updatedAt": "2026-01-01T00:00:00Z",
        }
        self.agent_settings = {
            "coomiMemoryEnabled": True,
            "wikiContextEnabled": True,
            "updatedAt": "2026-01-01T00:00:00Z",
        }

    def read_ui_preferences(self):
        return dict(self.preferences)

    def write_ui_preferences(self, payload):
        self.preferences.update(payload)
        return dict(self.preferences)

    def read_workspace_state(self):
        return {"lastProjectPath": "", "recentProjects": [], "updatedAt": ""}

    def read_agent_settings(self):
        return dict(self.agent_settings)

    def write_agent_settings(self, payload):
        self.agent_settings.update(payload)
        return dict(self.agent_settings)


class _Help:
    def read_guide(self):
        return {"items": [{"title": "Start"}]}

    def search(self, query, *, max_results):
        return {"query": query, "items": [{"title": "Result"}] * min(max_results, 2)}

    def read_repository(self, *, query, category):
        return {
            "query": query,
            "category": category,
            "categories": [{"id": "项目包装", "label": "项目包装", "count": 1}],
            "items": [{"id": "项目包装/简介", "title": "生成简介", "promptText": "请生成简介"}],
        }


@pytest.fixture
def client(monkeypatch, tmp_path):
    project = _Project()
    project.workspace_root = tmp_path.resolve()
    global_config = _GlobalConfig(tmp_path / "global")
    monkeypatch.setattr(routes_sys, "get_project_service", lambda: project)
    monkeypatch.setattr(routes_sys, "get_global_config_service", lambda: global_config)
    monkeypatch.setattr(routes_help, "get_help_guide_service", lambda: _Help())
    monkeypatch.setattr(routes_help, "get_prompt_repository_service", lambda: _Help())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def assert_success(response, *, status=200):
    assert response.status_code == status
    payload = response.json()
    assert payload["ok"] is True
    assert payload["error"] is None
    return payload


def test_system_envelopes_and_preferences_round_trip(client):
    health = assert_success(client.get("/api/v1/sys/health"))
    assert health["data"]["status"] == "ok"
    assert health["data"]["memoryUsageMb"] is None or isinstance(health["data"]["memoryUsageMb"], int)
    assert health["trace"]["traceId"]
    bootstrap = assert_success(client.get("/api/v1/sys/bootstrap"))
    assert bootstrap["data"]["uiPreferences"]["theme"] == "default"
    updated = assert_success(client.put("/api/v1/sys/ui-preferences", json={
        "theme": "dark",
        "sidebarWidth": 420,
        "leftPaneFontScale": 90,
        "centerPaneFontScale": 115,
        "rightPaneFontScale": 130,
    }))
    assert updated["data"]["theme"] == "dark"
    assert updated["data"]["sidebarWidth"] == 420
    assert updated["data"]["leftPaneFontScale"] == 90
    assert updated["data"]["centerPaneFontScale"] == 115
    assert updated["data"]["rightPaneFontScale"] == 130
    agent_settings = assert_success(client.get("/api/v1/sys/agent-settings"))
    assert agent_settings["data"]["coomiMemoryEnabled"] is True
    updated_agent = assert_success(
        client.put(
            "/api/v1/sys/agent-settings",
            json={"coomiMemoryEnabled": False, "wikiContextEnabled": True},
        )
    )
    assert updated_agent["data"]["coomiMemoryEnabled"] is False
    assert_success(client.get("/api/v1/sys/workspace-state"))


def test_help_contract_and_validation_error_envelope(client):
    assert len(assert_success(client.get("/api/v1/help/guide"))["data"]["items"]) == 1
    assert len(assert_success(client.get("/api/v1/help/guide/search?q=agent&limit=2"))["data"]["items"]) == 2
    prompts = assert_success(client.get("/api/v1/help/prompts?q=简介&category=项目包装"))["data"]
    assert prompts["items"][0]["title"] == "生成简介"
    invalid = client.get("/api/v1/help/guide/search?limit=0", headers={"x-trace-id": "trace-validation"})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "request_validation_error"
    assert invalid.json()["error"]["message"] == "query.limit 不能小于 1。"
    assert invalid.json()["trace"]["traceId"] == "trace-validation"
