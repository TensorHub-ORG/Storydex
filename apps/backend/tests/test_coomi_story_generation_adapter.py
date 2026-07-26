from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from services import coomi_agent_service
from services.coomi_agent_service import CoomiStoryGenerationAdapter


class ProviderError(Exception):
    def __init__(self, status_code: int, message: str = "provider error") -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeProvider:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def chat(self, messages: list[Dict[str, str]], options: Any) -> Any:
        del messages, options
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(content=response)


def test_adapter_retries_one_retryable_504_and_emits_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)
    provider = FakeProvider([ProviderError(504, "{'retry_after': 120}"), "ok"])
    sleeps: list[float] = []
    events: list[tuple[str, Dict[str, Any]]] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    adapter = CoomiStoryGenerationAdapter(
        trace_id="trace",
        event_sink=lambda name, payload: events.append((name, payload)),
        sleep=fake_sleep,
    )
    adapter._provider = provider

    content = asyncio.run(
        adapter.complete(
            messages=[{"role": "user", "content": "test"}],
            purpose="semantic_budget_scene",
            metadata={"scene": 2},
        )
    )

    assert content == "ok"
    assert provider.calls == 2
    assert adapter.provider_attempts == 2
    assert adapter.provider_retries == 1
    assert sleeps == [120]
    assert [payload["outcome"] for _name, payload in events] == ["error", "success"]
    assert events[0][1]["retryScheduled"] is True
    assert events[0][1]["statusCode"] == 504


def test_adapter_does_not_retry_authentication_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)
    provider = FakeProvider([ProviderError(401, "secret body must not be emitted")])
    events: list[tuple[str, Dict[str, Any]]] = []
    adapter = CoomiStoryGenerationAdapter(
        trace_id="trace",
        event_sink=lambda name, payload: events.append((name, payload)),
    )
    adapter._provider = provider

    with pytest.raises(ProviderError):
        asyncio.run(
            adapter.complete(
                messages=[{"role": "user", "content": "test"}],
                purpose="semantic_budget_plan",
                metadata={},
            )
        )

    assert provider.calls == 1
    assert adapter.provider_retries == 0
    assert events[0][1]["retryScheduled"] is False
    assert "secret body" not in str(events[0][1])
