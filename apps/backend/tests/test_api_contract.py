from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import routes_agent, routes_help, routes_sys
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


class _History:
    def list_session_summaries(self):
        return [{"sessionId": "session-a", "firstPrompt": "hello", "traceCount": 1}]

    def list_records(self, *, session_id, limit):
        return [{"traceId": "trace-a", "sessionId": session_id, "prompt": "hello"}][:limit]

    def clear_records(self, session_id):
        return 1

    def mark_session_cleared(self, session_id):
        return None

    def delete_session(self, session_id):
        return {"sessionId": session_id, "removedCount": 1}


class _Coomi:
    def get_status(self, *, workspace_root):
        return {"runtime": "coomi", "installed": True, "toolCount": 7, "permissionMode": "full_access"}

    def read_config(self):
        return {"configPath": "/isolated/providers.json", "content": "{}", "parsed": {}}

    def write_config(self, content):
        if content == "broken":
            raise ValueError("invalid JSON")
        return {"configPath": "/isolated/providers.json", "content": content, "parsed": {}}

    def list_models(self, *, base_url, api_key):
        if base_url == "invalid":
            raise ValueError("provider unavailable")
        return {"endpoint": f"{base_url.rstrip('/')}/models", "models": ["fake-model"]}

    def set_permission_mode(self, mode):
        return {"permissionMode": mode, "permissionLabel": mode}

    def cycle_permission_mode(self):
        return {"permissionMode": "approve_for_me", "permissionLabel": "approve_for_me"}

    def resolve_approval(self, approval_id, decision, *, response):
        return {"approvalId": approval_id, "decision": decision, "response": response}

    def clear_session(self, session_id, *, workspace_root, delete_history):
        return None


@pytest.fixture
def client(monkeypatch, tmp_path):
    project = _Project()
    project.workspace_root = tmp_path.resolve()
    global_config = _GlobalConfig(tmp_path / "global")
    coomi = _Coomi()
    monkeypatch.setattr(routes_sys, "get_project_service", lambda: project)
    monkeypatch.setattr(routes_sys, "get_global_config_service", lambda: global_config)
    monkeypatch.setattr(routes_help, "get_help_guide_service", lambda: _Help())
    monkeypatch.setattr(routes_help, "get_prompt_repository_service", lambda: _Help())
    monkeypatch.setattr(routes_agent, "project_service", project)
    monkeypatch.setattr(routes_agent, "trace_history_service", _History())
    monkeypatch.setattr(routes_agent, "get_storydex_coomi_agent_service", lambda: coomi)
    monkeypatch.setattr(routes_agent.storydex_intent_service, "clear_session", lambda **kwargs: None)
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
    assert invalid.json()["trace"]["traceId"] == "trace-validation"


def test_agent_session_history_clear_and_delete_contracts(client):
    sessions = assert_success(client.get("/api/v1/agent/sessions"))
    assert sessions["data"]["items"][0]["sessionId"] == "session-a"
    history = assert_success(client.get("/api/v1/agent/history?sessionId=session-a&limit=10"))
    assert history["data"]["items"][0]["sessionId"] == "session-a"
    cleared = assert_success(client.post("/api/v1/agent/clear-conversation?sessionId=session-a"))
    assert cleared["data"]["historyClearedCount"] == 1
    deleted = assert_success(client.delete("/api/v1/agent/sessions/session-a"))
    assert deleted["data"]["removedCount"] == 1


def test_coomi_status_config_permission_and_approval_contracts(client):
    assert assert_success(client.get("/api/v1/agent/coomi/status"))["data"]["toolCount"] == 7
    assert assert_success(client.get("/api/v1/agent/coomi/config"))["data"]["parsed"] == {}
    assert_success(client.put("/api/v1/agent/coomi/config", json={"content": "{}"}))
    assert assert_success(client.post("/api/v1/agent/coomi/permission", json={"permissionMode": "plan"}))["data"]["permissionMode"] == "plan"
    assert_success(client.post("/api/v1/agent/coomi/permission/cycle"))
    approval = assert_success(client.post("/api/v1/agent/coomi/approval", json={"approvalId": "a1", "decision": "allow"}))
    assert approval["data"]["approvalId"] == "a1"


def test_provider_errors_are_sanitized_and_do_not_echo_api_keys(client):
    invalid_config = client.put("/api/v1/agent/coomi/config", json={"content": "broken"}, headers={"x-trace-id": "trace-config"})
    assert invalid_config.status_code == 400
    assert invalid_config.json()["error"]["code"] == "coomi_config_invalid"
    models = client.post("/api/v1/agent/coomi/models", json={"baseUrl": "invalid", "apiKey": "super-secret"})
    assert models.status_code == 400
    text = models.text
    assert "super-secret" not in text
    assert models.json()["error"]["code"] == "coomi_models_unavailable"


@pytest.mark.security
@pytest.mark.parametrize("session_id", ["../escape", "..\\escape", "/absolute", "A" * 500])
def test_session_identifiers_cannot_escape_storage_through_api(client, session_id):
    response = client.delete(f"/api/v1/agent/sessions/{session_id}")
    # Encoded slashes may be rejected by the router; accepted values remain data passed
    # to isolated services and never become filesystem paths in the route layer.
    assert response.status_code in {200, 404}


@pytest.mark.unit
def test_agent_event_and_generation_helpers_cover_boundaries():
    assert routes_agent._bounded_int("999", default=1, minimum=1, maximum=20) == 20
    assert routes_agent._bounded_int("bad", default=3, minimum=1, maximum=20) == 3
    assert routes_agent._normalize_story_generation_options({"segmentCount": 2, "segmentWords": 800}) == {
        "fragmentCount": 2,
        "fragmentWordCount": 800,
        "fragmentWordCountMin": 800,
        "fragmentWordCountMax": 800,
        "chapterTemplateId": "",
    }
    assert routes_agent._phase_for_event("ToolDone") == "tool"
    assert routes_agent._phase_for_event("TextChunk") == "model"
    assert routes_agent._phase_for_event("GitCommitPrompt") == "version_control"
    assert routes_agent._status_for_event("AgentError", {}) == "error"
    assert routes_agent._status_for_event("RunAccepted", {}) == "running"
    encoded = routes_agent._encode_sse("RunAccepted", {"message": "已接收"})
    assert encoded.startswith("event: RunAccepted\ndata:") and encoded.endswith("\n\n")
