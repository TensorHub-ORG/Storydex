import asyncio
import json
import sys
import types

from services import coomi_agent_service as coomi_module
from services.coomi_agent_service import StorydexCoomiAgentService
from api import routes_agent


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


def test_list_models_derives_models_url_from_chat_completions_endpoint():
    seen = {}

    def fake_get(url, *, headers, timeout):
        seen["url"] = url
        seen["headers"] = headers
        seen["timeout"] = timeout
        return _FakeModelResponse()

    result = StorydexCoomiAgentService().list_models(
        base_url="https://opencode.ai/zen/go/v1/chat/completions",
        api_key="sk-test",
        http_get=fake_get,
    )

    assert seen["url"] == "https://opencode.ai/zen/go/v1/models"
    assert seen["headers"]["Authorization"] == "Bearer sk-test"
    assert seen["headers"]["Accept"] == "application/json"
    assert result == {
        "endpoint": "https://opencode.ai/zen/go/v1/models",
        "models": ["claude-sonnet-4", "gpt-4.1"],
    }


def test_list_models_accepts_common_response_variants():
    class FakeVariantResponse:
        status_code = 200
        text = '{"models":[]}'

        def json(self):
            return {"models": ["deepseek-chat", {"name": "deepseek-reasoner"}, {"model": "qwen-max"}]}

    result = StorydexCoomiAgentService().list_models(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        http_get=lambda url, *, headers, timeout: FakeVariantResponse(),
    )

    assert result["endpoint"] == "https://api.example.com/v1/models"
    assert result["models"] == ["deepseek-chat", "deepseek-reasoner", "qwen-max"]


def test_list_models_sanitizes_transport_errors():
    def fake_get(url, *, headers, timeout):
        raise RuntimeError("network down for sk-secret")

    try:
        StorydexCoomiAgentService().list_models(
            base_url="https://api.example.com/v1",
            api_key="sk-secret",
            http_get=fake_get,
        )
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected ValueError")

    assert "sk-secret" not in message
    assert "Model list request failed" in message


def test_write_config_preserves_user_chat_completion_url(monkeypatch, tmp_path):
    config_path = tmp_path / ".storydex" / ".coomi" / "config" / "providers.json"
    monkeypatch.setattr(coomi_module, "STORYDEX_COOMI_HOME", tmp_path / ".storydex")
    monkeypatch.setattr(coomi_module, "STORYDEX_COOMI_CONFIG", config_path)

    updated = StorydexCoomiAgentService().write_config(
        json.dumps(
            {
                "version": 1,
                "active": "opencode",
                "providers": {
                    "opencode": {
                        "type": "openai",
                        "display": "OpenCode",
                        "api_key": "sk-test",
                        "base_url": "https://opencode.ai/zen/go/v1/chat/completions",
                        "model": "deepseek-v4-flash",
                    }
                },
            }
        )
    )

    assert updated["parsed"]["providers"]["opencode"]["base_url"] == (
        "https://opencode.ai/zen/go/v1/chat/completions"
    )
    assert json.loads(config_path.read_text(encoding="utf-8"))["providers"]["opencode"]["base_url"] == (
        "https://opencode.ai/zen/go/v1/chat/completions"
    )


def test_storydex_coomi_home_normalizes_provider_config_runtime_base_url(monkeypatch, tmp_path):
    config_path = tmp_path / ".storydex" / ".coomi" / "config" / "providers.json"
    monkeypatch.setattr(coomi_module, "STORYDEX_COOMI_HOME", tmp_path / ".storydex")
    monkeypatch.setattr(coomi_module, "STORYDEX_COOMI_CONFIG", config_path)

    with coomi_module._storydex_coomi_home():
        from coomi.services.llm.config import ProviderConfig

        config = ProviderConfig.from_dict(
            "opencode",
            {
                "type": "openai",
                "display": "OpenCode",
                "api_key": "sk-test",
                "base_url": "https://opencode.ai/zen/go/v1/chat/completions",
                "model": "deepseek-v4-flash",
            },
        )

    assert config.base_url == "https://opencode.ai/zen/go/v1"


def test_agent_coomi_models_route_returns_model_list(monkeypatch):
    class FakeService:
        def list_models(self, *, base_url, api_key):
            assert base_url == "https://api.example.com/v1/chat/completions"
            assert api_key == "sk-test"
            return {"endpoint": "https://api.example.com/v1/models", "models": ["model-a"]}

    monkeypatch.setattr(routes_agent, "get_storydex_coomi_agent_service", lambda: FakeService())

    payload = routes_agent.AgentCoomiModelListRequest(
        baseUrl="https://api.example.com/v1/chat/completions",
        apiKey="sk-test",
    )
    response = routes_agent.agent_list_coomi_models(payload, request=None)

    assert response.ok is True
    assert response.data["endpoint"] == "https://api.example.com/v1/models"
    assert response.data["models"] == ["model-a"]


def test_generate_commit_message_awaits_async_provider_chat(monkeypatch, tmp_path):
    class FakeResponse:
        content = "agent: update generated story files"

    class FakeProvider:
        async def chat(self, messages, options):
            assert messages
            assert options is None
            return FakeResponse()

    fake_services = types.ModuleType("coomi.services")
    fake_services.get_llm_provider = lambda: FakeProvider()
    monkeypatch.setitem(sys.modules, "coomi", types.ModuleType("coomi"))
    monkeypatch.setitem(sys.modules, "coomi.services", fake_services)

    service = StorydexCoomiAgentService()
    monkeypatch.setattr(service, "_ensure_coomi_installed", lambda: None)

    message = asyncio.run(
        service.generate_commit_message(
            workspace_root=tmp_path,
            changed_files=[".storydex/project.json"],
            diff_summary="+ generated",
            prompt="continue story",
        )
    )

    assert message == "agent: update generated story files"
