from __future__ import annotations

import asyncio
import sys

import pytest

from services import coomi_bridge_client as bridge


def test_bridge_provider_normalizes_openai_style_tools(monkeypatch) -> None:
    monkeypatch.setattr(
        bridge,
        "_provider_settings",
        lambda *_args, **_kwargs: type(
            "Config",
            (),
            {"id": "relay", "display": "Relay", "model": "gpt-test"},
        )(),
    )
    provider = bridge.BridgeProvider()
    assert provider.get_model_display_name() == "Relay / gpt-test"
    tools = bridge._wire_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "Read",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ]
    )
    assert tools == [
        {
            "name": "Read",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]


@pytest.mark.asyncio
async def test_bridge_provider_maps_completion_tool_calls_and_usage(monkeypatch) -> None:
    monkeypatch.setattr(
        bridge,
        "_provider_settings",
        lambda *_args, **_kwargs: type(
            "Config",
            (),
            {"id": "relay", "display": "Relay", "model": "gpt-test"},
        )(),
    )
    captured = {}

    async def fake_request(payload, **_kwargs):
        captured.update(payload)
        return {
            "type": "completion",
            "data": {
                "content": "done",
                "toolCalls": [{"id": "c1", "name": "Read", "arguments": {"path": "README.md"}}],
                "usage": {"input_tokens": 11, "output_tokens": 7},
            },
        }

    monkeypatch.setattr(bridge, "request_once", fake_request)
    provider = bridge.BridgeProvider()
    response = await provider.chat(
        [{"role": "user", "content": "inspect"}],
        tools=[{"function": {"name": "Read", "parameters": {"type": "object"}}}],
        tool_choice={"function": {"name": "Read"}},
        max_completion_tokens=256,
    )
    assert response.content == "done"
    assert response.tool_calls and response.tool_calls[0].arguments == {"path": "README.md"}
    assert response.usage == {
        "input_tokens": 11,
        "output_tokens": 7,
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
    }
    assert captured["requiredTool"] == "Read"
    assert captured["maxOutputTokens"] == 256


@pytest.mark.asyncio
async def test_bridge_stream_contract_emits_content_tools_and_usage(monkeypatch) -> None:
    monkeypatch.setattr(
        bridge,
        "_provider_settings",
        lambda *_args, **_kwargs: type(
            "Config",
            (),
            {"id": "relay", "display": "Relay", "model": "gpt-test"},
        )(),
    )
    provider = bridge.BridgeProvider()

    async def fake_chat(*_args, **_kwargs):
        return bridge.BridgeLLMResponse(
            content="text",
            tool_calls=[bridge.BridgeToolCall(id="c1", name="Read", arguments={})],
            usage={"total_tokens": 3},
        )

    monkeypatch.setattr(provider, "chat", fake_chat)
    chunks = [chunk async for chunk in provider.chat_stream_with_tools([], [])]
    assert [chunk["type"] for chunk in chunks] == ["content", "tool_call", "usage"]


@pytest.mark.asyncio
async def test_live_bridge_close_reaps_process_and_closes_pipes(monkeypatch) -> None:
    child = (
        "import sys; "
        "sys.stdin.readline(); "
        "print('{\"type\":\"completed\",\"data\":{}}', flush=True)"
    )
    monkeypatch.setattr(bridge, "bridge_command", lambda: [sys.executable, "-c", child])
    process = await bridge.LiveBridgeProcess.start({"action": "run"})
    packets = [packet async for packet in process.events()]

    await process.close()
    await process.close()

    assert packets == [{"type": "completed", "data": {}}]
    assert process.process.returncode == 0
    assert process.process.stdin is not None
    assert process.process.stdin.closed
    assert process._stderr_task.done()


@pytest.mark.asyncio
async def test_request_once_reaps_cancelled_process(monkeypatch) -> None:
    child = "import sys, time; sys.stdin.readline(); time.sleep(30)"
    monkeypatch.setattr(bridge, "bridge_command", lambda: [sys.executable, "-c", child])
    original_create = bridge._create_bridge_subprocess
    created = []

    async def capture_process():
        process = await original_create()
        created.append(process)
        return process

    monkeypatch.setattr(bridge, "_create_bridge_subprocess", capture_process)
    task = asyncio.create_task(bridge.request_once({"action": "status"}))
    await asyncio.sleep(0.1)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(created) == 1
    assert created[0].poll() is not None


@pytest.mark.asyncio
async def test_request_once_reaps_timed_out_process(monkeypatch) -> None:
    child = "import sys, time; sys.stdin.readline(); time.sleep(30)"
    monkeypatch.setattr(bridge, "bridge_command", lambda: [sys.executable, "-c", child])
    original_create = bridge._create_bridge_subprocess
    created = []

    async def capture_process():
        process = await original_create()
        created.append(process)
        return process

    monkeypatch.setattr(bridge, "_create_bridge_subprocess", capture_process)

    with pytest.raises(bridge.CoomiBridgeError, match="timed out"):
        await bridge.request_once({"action": "status"}, timeout=0.1)

    assert len(created) == 1
    assert created[0].poll() is not None


@pytest.mark.asyncio
async def test_bridge_transport_does_not_use_asyncio_subprocess(monkeypatch) -> None:
    async def unsupported(*_args, **_kwargs):
        raise NotImplementedError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unsupported)
    child = "import sys; sys.stdin.readline(); print('{\"type\":\"ok\"}', flush=True)"
    monkeypatch.setattr(bridge, "bridge_command", lambda: [sys.executable, "-c", child])

    packet = await bridge.request_once({"action": "status"})

    assert packet == {"type": "ok"}


def test_live_bridge_runs_on_selector_event_loop(monkeypatch) -> None:
    child = (
        "import sys; "
        "sys.stdin.readline(); "
        "print('{\"type\":\"completed\",\"data\":{}}', flush=True)"
    )
    monkeypatch.setattr(bridge, "bridge_command", lambda: [sys.executable, "-c", child])

    async def exercise() -> list[dict]:
        process = await bridge.LiveBridgeProcess.start({"action": "run"})
        try:
            return [packet async for packet in process.events()]
        finally:
            await process.close()

    loop = asyncio.SelectorEventLoop()
    try:
        packets = loop.run_until_complete(exercise())
        loop.run_until_complete(loop.shutdown_default_executor())
    finally:
        loop.close()

    assert packets == [{"type": "completed", "data": {}}]
