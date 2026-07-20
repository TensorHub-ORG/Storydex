from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from coomi.services.llm.config import ProviderConfig
from coomi.services.llm.generic import GenericOpenAIProvider
from coomi.services.llm.openai import OpenAIProvider
from services.llm_replay import get_replayable_llm_provider


_SDK_USER_AGENT_PREFIXES = ("AsyncOpenAI/Python", "OpenAI/Python")


def _agent_messages() -> list[dict[str, str]]:
    system_prompt = (
        "You are Coomi Agent inside Storydex. Inspect the workspace before answering.\n"
        + "Use Read and Grep to ground every conclusion in project files. " * 240
    )
    assert len(system_prompt) >= 12_000
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                "Read README.md and the project configuration, then identify the documented "
                "runtime and cite the file you used. Do not modify the workspace."
            ),
        },
    ]


def _agent_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "Read",
                "description": "Read a UTF-8 text file from the current workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                    "required": ["file_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "Grep",
                "description": "Search project files with a regular expression.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["pattern"],
                },
            },
        },
    ]


def _tool_call_stream() -> bytes:
    chunks = [
        {
            "id": "chatcmpl-agent-compat",
            "object": "chat.completion.chunk",
            "created": 1_721_000_000,
            "model": "agent-model-routed",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-read-readme",
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "arguments": '{"file_path":"README.md"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-agent-compat",
            "object": "chat.completion.chunk",
            "created": 1_721_000_000,
            "model": "agent-model-routed",
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "tool_calls",
                }
            ],
        },
        {
            "id": "chatcmpl-agent-compat",
            "object": "chat.completion.chunk",
            "created": 1_721_000_000,
            "model": "agent-model-routed",
            "choices": [],
            "usage": {
                "prompt_tokens": 3_280,
                "completion_tokens": 18,
                "total_tokens": 3_298,
            },
        },
    ]
    events = [f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n" for chunk in chunks]
    events.append("data: [DONE]\n\n")
    return "".join(events).encode("utf-8")


@pytest.mark.integration
@pytest.mark.parametrize("provider_type", [GenericOpenAIProvider, OpenAIProvider])
async def test_default_openai_sdk_user_agent_is_replaced_for_realistic_agent_stream(provider_type):
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        user_agent = request.headers.get("user-agent", "")
        if user_agent.startswith(_SDK_USER_AGENT_PREFIXES):
            return httpx.Response(
                403,
                text="Your request was blocked.",
                headers={"content-type": "text/plain"},
            )

        payload = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        assert request.headers["x-partner-route"] == "keep-me"
        assert len(payload["messages"][0]["content"]) >= 12_000
        assert [tool["function"]["name"] for tool in payload["tools"]] == ["Read", "Grep"]
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        return httpx.Response(
            200,
            content=_tool_call_stream(),
            headers={"content-type": "text/event-stream"},
        )

    config = ProviderConfig(
        id="relay",
        type="generic",
        display="Agent relay",
        api_key="test-key",
        base_url="https://relay.test/v1",
        model="agent-model",
        tool_protocol="native",
    )
    provider = provider_type(config)
    await provider.client.close()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    provider.client = AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        default_headers={"X-Partner-Route": "keep-me"},
        http_client=http_client,
        max_retries=0,
    )

    try:
        replayable = get_replayable_llm_provider(provider)
        chunks = [
            chunk
            async for chunk in replayable.chat_stream_with_tools(
                _agent_messages(),
                tools=_agent_tools(),
            )
        ]
    finally:
        await provider.client.close()

    assert len(requests) == 1
    assert requests[0].headers["user-agent"].startswith("Storydex-Coomi/")
    assert chunks[:2] == [
        {"type": "tool_call_start", "tool_name": "Read", "index": 0},
        {
            "type": "tool_call",
            "data": {
                "id": "call-read-readme",
                "name": "Read",
                "arguments": {"file_path": "README.md"},
                "raw_arguments": '{"file_path":"README.md"}',
                "parse_error": None,
            },
        },
    ]
    assert chunks[2]["type"] == "usage"
    assert chunks[2]["data"]["inputTokens"] == 3_280
    assert chunks[2]["data"]["outputTokens"] == 18
    assert chunks[2]["data"]["totalTokens"] == 3_298


@pytest.mark.integration
async def test_existing_provider_user_agent_is_preserved():
    config = ProviderConfig(
        id="partner",
        type="generic",
        display="Partner relay",
        api_key="test-key",
        base_url="https://partner.test/v1",
        model="agent-model",
    )
    provider = GenericOpenAIProvider(config)
    await provider.client.close()
    provider.client = AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        default_headers={"User-Agent": "PartnerClient/9.0"},
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
        max_retries=0,
    )

    try:
        get_replayable_llm_provider(provider)
        assert provider.client.default_headers["User-Agent"] == "PartnerClient/9.0"
    finally:
        await provider.client.close()
