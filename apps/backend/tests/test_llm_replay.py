from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from services.llm_replay import (
    ReplayMismatch,
    get_llm_metrics,
    get_replayable_llm_provider,
    llm_trace,
    reset_llm_metrics,
)


@dataclass
class FakeResponse:
    content: str
    tool_calls: list | None = None
    usage: dict | None = None


class FakeProvider:
    def __init__(self) -> None:
        self.model = "fake-model"
        self.display = "Fake Provider"
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        return FakeResponse(
            content=f"response-{self.calls}",
            usage={"prompt_tokens": self.calls, "completion_tokens": 2, "total_tokens": self.calls + 2},
        )

    async def chat_stream(self, messages, **kwargs):
        for chunk in ("a", "b"):
            yield chunk

    async def chat_stream_with_tools(self, messages, tools=None, **kwargs):
        yield {"type": "content", "content": "hello"}
        yield {"type": "usage", "data": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}


def test_off_mode_proxies_attributes_and_behavior(monkeypatch):
    monkeypatch.delenv("STORYDEX_LLM_MODE", raising=False)
    provider = FakeProvider()
    replayable = get_replayable_llm_provider(provider)

    assert replayable.model == "fake-model"
    replayable.model = "changed-model"
    assert provider.model == "changed-model"
    response = asyncio.run(replayable.chat([{"role": "user", "content": "hello"}]))
    assert response.content == "response-1"
    assert provider.calls == 1


def test_record_then_replay_two_calls(monkeypatch, tmp_path):
    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    recorder = get_replayable_llm_provider(FakeProvider())
    first = asyncio.run(recorder.chat([{"role": "user", "content": "one"}]))
    second = asyncio.run(recorder.chat([{"role": "user", "content": "two"}]))
    assert (first.content, second.content) == ("response-1", "response-2")

    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    offline = FakeProvider()
    replay = get_replayable_llm_provider(offline)
    replayed_first = asyncio.run(replay.chat([{"role": "user", "content": "one"}]))
    replayed_second = asyncio.run(replay.chat([{"role": "user", "content": "two"}]))
    replay.assert_replay_complete()
    assert (replayed_first.content, replayed_second.content) == ("response-1", "response-2")
    assert offline.calls == 0


def test_replay_mismatch_points_to_changed_message(monkeypatch, tmp_path):
    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    recorder = get_replayable_llm_provider(FakeProvider())
    asyncio.run(recorder.chat([{"role": "user", "content": "original"}]))

    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    replay = get_replayable_llm_provider(FakeProvider())
    with pytest.raises(ReplayMismatch, match=r"request\.messages\[0\]\.content"):
        asyncio.run(replay.chat([{"role": "user", "content": "changed"}]))


def test_stream_record_replay_and_trace_metrics(monkeypatch, tmp_path):
    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    reset_llm_metrics()
    recorder = get_replayable_llm_provider(FakeProvider())

    async def collect(provider):
        return [chunk async for chunk in provider.chat_stream_with_tools([{"role": "user", "content": "stream"}])]

    with llm_trace("trace-1"):
        recorded = asyncio.run(collect(recorder))
    assert get_llm_metrics("trace-1") == {
        "traceId": "trace-1",
        "calls": 1,
        "byMethod": {"chat_stream_with_tools": 1},
        "promptTokens": 3,
        "completionTokens": 4,
        "totalTokens": 7,
    }

    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    replay = get_replayable_llm_provider(FakeProvider())
    assert asyncio.run(collect(replay)) == recorded
    replay.assert_replay_complete()


def test_checked_in_smoke_fixture_replays_without_provider_call(monkeypatch):
    fixture_dir = Path(__file__).parent / "fixtures" / "llm_replay" / "smoke"
    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(fixture_dir))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")

    class OfflineProvider:
        model = "deepseek-v4-flash"

        async def chat(self, *args, **kwargs):
            raise AssertionError("checked-in replay must not call the provider")

    replay = get_replayable_llm_provider(OfflineProvider())
    prompts = ["Reply exactly: Storydex replay smoke one", "Reply exactly: Storydex replay smoke two"]
    responses = [
        asyncio.run(replay.chat([{"role": "user", "content": prompt}], tools=None))
        for prompt in prompts
    ]
    replay.assert_replay_complete()
    assert all(response.content for response in responses)
