from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from api import routes_agent
from services import coomi_agent_service as coomi_module
from services.coomi_agent_service import StorydexCoomiAgentService


class _FakeModelResponse:
    status_code = 200
    text = '{"data":[]}'

    def json(self):
        return {
            "data": [
                {"id": "claude-sonnet-4"},
                {"id": "gpt-4.1"},
                {"id": "claude-sonnet-4"},
                {"object": "model"},
            ]
        }


def test_list_models_derives_endpoint_and_deduplicates_ids() -> None:
    seen = {}

    def fake_get(url, *, headers, timeout):
        seen.update(url=url, headers=headers, timeout=timeout)
        return _FakeModelResponse()

    result = StorydexCoomiAgentService().list_models(
        base_url="https://opencode.ai/zen/go/v1/chat/completions",
        api_key="sk-test",
        provider_type="openai_compatible",
        http_get=fake_get,
    )
    assert seen["url"] == "https://opencode.ai/zen/go/v1/models"
    assert seen["headers"]["Authorization"] == "Bearer sk-test"
    assert result["models"] == ["claude-sonnet-4", "gpt-4.1"]


def test_list_models_uses_anthropic_authentication() -> None:
    seen = {}

    def fake_get(url, *, headers, timeout):
        seen.update(url=url, headers=headers)
        return _FakeModelResponse()

    StorydexCoomiAgentService().list_models(
        base_url="https://api.anthropic.com",
        api_key="sk-ant-test",
        provider_type="anthropic_messages",
        http_get=fake_get,
    )
    assert seen["url"] == "https://api.anthropic.com/v1/models"
    assert seen["headers"]["x-api-key"] == "sk-ant-test"
    assert "Authorization" not in seen["headers"]


def test_list_models_sanitizes_transport_errors() -> None:
    def fake_get(url, *, headers, timeout):
        raise RuntimeError("network down for sk-secret")

    try:
        StorydexCoomiAgentService().list_models(
            base_url="https://api.example.com/v1",
            api_key="sk-secret",
            provider_type="openai_compatible",
            http_get=fake_get,
        )
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected ValueError")
    assert "sk-secret" not in message
    assert "Model list request failed" in message


def test_write_config_preserves_chat_completion_url(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / ".storydex" / ".coomi" / "config" / "providers.json"
    monkeypatch.setattr(coomi_module, "STORYDEX_COOMI_HOME", tmp_path / ".storydex" / ".coomi")
    monkeypatch.setattr(coomi_module, "STORYDEX_COOMI_CONFIG", config_path)
    updated = StorydexCoomiAgentService().write_config(
        json.dumps(
            {
                "version": 1,
                "active": "opencode",
                "providers": {
                    "opencode": {
                        "type": "openai_compatible",
                        "display": "OpenCode",
                        "api_key": "sk-test",
                        "base_url": "https://opencode.ai/zen/go/v1/chat/completions",
                        "model": "deepseek-v4-flash",
                    }
                },
            }
        )
    )
    assert updated["parsed"]["providers"]["opencode"]["base_url"].endswith("/chat/completions")


def test_agent_models_route_returns_model_list(monkeypatch) -> None:
    class FakeService:
        def list_models(self, *, base_url, api_key, provider_type):
            assert provider_type == "anthropic_messages"
            return {"endpoint": "https://api.example.com/v1/models", "models": ["model-a"]}

    monkeypatch.setattr(routes_agent, "get_storydex_coomi_agent_service", lambda: FakeService())
    payload = routes_agent.AgentCoomiModelListRequest(
        baseUrl="https://api.example.com/v1/chat/completions",
        apiKey="sk-test",
        providerType="anthropic_messages",
    )
    response = routes_agent.agent_list_coomi_models(payload, request=None)
    assert response.ok is True
    assert response.data["models"] == ["model-a"]


def test_generate_commit_message_uses_bridge_provider(monkeypatch, tmp_path) -> None:
    class Provider:
        async def chat(self, messages, tools=None):
            assert messages
            assert tools is None
            return SimpleNamespace(content="agent: update generated story files")

    monkeypatch.setattr(coomi_module, "get_bridge_provider", lambda **_kwargs: Provider())
    import services.llm_replay as llm_replay

    monkeypatch.setattr(llm_replay, "get_replayable_llm_provider", lambda provider=None: provider)
    message = asyncio.run(
        StorydexCoomiAgentService().generate_commit_message(
            workspace_root=tmp_path,
            changed_files=[".storydex/project.json"],
            diff_summary="+ generated",
            prompt="continue story",
        )
    )
    assert message == "agent: update generated story files"
