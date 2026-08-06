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
        self.options: list[Any] = []

    async def chat(self, messages: list[Dict[str, str]], options: Any) -> Any:
        del messages
        self.calls += 1
        self.options.append(options)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if hasattr(response, "content"):
            return response
        return SimpleNamespace(content=response)


def _tool_response(*, completion_tokens: int | None = None) -> Any:
    response = SimpleNamespace(
        content="",
        tool_calls=[
            SimpleNamespace(
                name="StorydexSubmitLengthPatch",
                arguments={"version": 1, "direction": "expand", "operations": []},
            )
        ],
    )
    if completion_tokens is not None:
        response.usage = {
            "prompt_tokens": 100,
            "completion_tokens": completion_tokens,
            "total_tokens": 100 + completion_tokens,
        }
    return response


@pytest.mark.asyncio
async def test_adapter_passes_reasoning_effort_to_bridge_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import llm_replay

    captured: Dict[str, Any] = {}
    raw_provider = object()

    def bridge_provider(provider_id: str | None = None, **kwargs: Any) -> Any:
        captured.update({"provider_id": provider_id, **kwargs})
        return raw_provider

    monkeypatch.setattr(coomi_agent_service, "get_bridge_provider", bridge_provider)
    monkeypatch.setattr(llm_replay, "get_replayable_llm_provider", lambda provider: provider)
    adapter = CoomiStoryGenerationAdapter(
        trace_id="reasoning",
        provider_id="primary",
        reasoning_effort="xhigh",
    )

    assert await adapter._resolve_provider() is raw_provider
    assert captured == {"provider_id": "primary", "reasoning_effort": "xhigh"}


def test_length_patch_rejects_an_empty_tool_object_without_logging_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)
    response = SimpleNamespace(
        content="",
        finish_reason="tool_calls",
        tool_calls=[
            SimpleNamespace(
                name="StorydexSubmitLengthPatch",
                arguments={},
                raw_arguments="{}",
                parse_error=None,
            )
        ],
    )
    events: list[tuple[str, Dict[str, Any]]] = []
    adapter = CoomiStoryGenerationAdapter(
        trace_id="trace-empty-tool",
        event_sink=lambda name, payload: events.append((name, payload)),
        attempt_event_name="StoryProviderAttempt",
    )
    adapter._provider = FakeProvider([response])

    with pytest.raises(RuntimeError) as error:
        asyncio.run(
            adapter.complete_tool_call(
                messages=[{"role": "user", "content": "test"}],
                tool={"name": "StorydexSubmitLengthPatch", "parameters": {}},
                purpose="story_length_revision",
                tool_name="StorydexSubmitLengthPatch",
                max_completion_tokens=768,
            )
        )

    assert getattr(error.value, "reason", "") == "tool_arguments_empty"
    attempt = events[-1][1]
    assert attempt["outcome"] == "error"
    assert attempt["metadata"] == {
        "toolName": "StorydexSubmitLengthPatch",
        "capApplied": False,
        "completionTokens": None,
        "maxCompletionTokens": 768,
        "toolChoiceApplied": False,
        "toolCallStatus": "tool_arguments_empty",
        "finishReason": "tool_calls",
        "targetToolPresent": True,
        "rawArgumentsLength": 2,
        "toolArgumentsEmpty": True,
        "parseErrorPresent": False,
        "completionCapHit": False,
    }
    assert "rawArguments" not in attempt["metadata"]


def test_length_patch_distinguishes_a_present_tool_with_missing_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)
    response = SimpleNamespace(
        content="",
        finish_reason="tool_calls",
        tool_calls=[
            SimpleNamespace(
                name="StorydexSubmitLengthPatch",
                arguments=None,
                raw_arguments="",
                parse_error=None,
            )
        ],
    )
    events: list[tuple[str, Dict[str, Any]]] = []
    adapter = CoomiStoryGenerationAdapter(
        trace_id="trace-missing-arguments",
        event_sink=lambda name, payload: events.append((name, payload)),
        attempt_event_name="StoryProviderAttempt",
    )
    adapter._provider = FakeProvider([response])

    with pytest.raises(RuntimeError) as error:
        asyncio.run(
            adapter.complete_tool_call(
                messages=[{"role": "user", "content": "test"}],
                tool={"name": "StorydexSubmitLengthPatch", "parameters": {}},
                purpose="story_length_revision",
                tool_name="StorydexSubmitLengthPatch",
            )
        )

    assert getattr(error.value, "reason", "") == "tool_arguments_empty"
    metadata = events[-1][1]["metadata"]
    assert metadata["targetToolPresent"] is True
    assert metadata["toolArgumentsEmpty"] is True
    assert metadata["rawArgumentsLength"] == 0


def test_length_patch_rejects_valid_json_that_is_not_an_argument_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)
    response = SimpleNamespace(
        content="",
        finish_reason="tool_calls",
        tool_calls=[
            SimpleNamespace(
                name="StorydexSubmitLengthPatch",
                arguments=[],
                raw_arguments="[]",
                parse_error=None,
            )
        ],
    )
    adapter = CoomiStoryGenerationAdapter(trace_id="trace-non-object-arguments")
    adapter._provider = FakeProvider([response])

    with pytest.raises(RuntimeError) as error:
        asyncio.run(
            adapter.complete_tool_call(
                messages=[{"role": "user", "content": "test"}],
                tool={"name": "StorydexSubmitLengthPatch", "parameters": {}},
                purpose="story_length_revision",
                tool_name="StorydexSubmitLengthPatch",
            )
        )

    assert getattr(error.value, "reason", "") == "tool_arguments_invalid_patch"


def test_length_patch_classifies_invalid_json_at_the_completion_cap_as_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)
    response = SimpleNamespace(
        content="",
        finish_reason="length",
        usage={"prompt_tokens": 100, "completion_tokens": 768, "total_tokens": 868},
        tool_calls=[
            SimpleNamespace(
                name="StorydexSubmitLengthPatch",
                arguments={},
                raw_arguments='{"operations":[',
                parse_error="Expecting value",
            )
        ],
    )

    class CapProvider:
        async def chat(
            self,
            _messages: list[Dict[str, str]],
            _tools: Any,
            max_completion_tokens: int | None = None,
        ) -> Any:
            assert max_completion_tokens == 768
            return response

    events: list[tuple[str, Dict[str, Any]]] = []
    adapter = CoomiStoryGenerationAdapter(
        trace_id="trace-truncated-tool",
        event_sink=lambda name, payload: events.append((name, payload)),
        attempt_event_name="StoryProviderAttempt",
    )
    adapter._provider = CapProvider()

    with pytest.raises(RuntimeError) as error:
        asyncio.run(
            adapter.complete_tool_call(
                messages=[{"role": "user", "content": "test"}],
                tool={"name": "StorydexSubmitLengthPatch", "parameters": {}},
                purpose="story_length_revision",
                tool_name="StorydexSubmitLengthPatch",
                max_completion_tokens=768,
            )
        )

    assert getattr(error.value, "reason", "") == "tool_arguments_truncated"
    metadata = events[-1][1]["metadata"]
    assert events[-1][1]["outcome"] == "error"
    assert metadata["toolCallStatus"] == "tool_arguments_truncated"
    assert metadata["finishReason"] == "length"
    assert metadata["targetToolPresent"] is True
    assert metadata["rawArgumentsLength"] == 15
    assert metadata["toolArgumentsEmpty"] is True
    assert metadata["parseErrorPresent"] is True
    assert metadata["completionCapHit"] is True
    assert "rawArguments" not in metadata


def test_length_patch_classifies_a_missing_required_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)
    response = SimpleNamespace(
        content="",
        finish_reason="tool_calls",
        tool_calls=[SimpleNamespace(name="SomeOtherTool", arguments={"ok": True})],
    )
    events: list[tuple[str, Dict[str, Any]]] = []
    adapter = CoomiStoryGenerationAdapter(
        trace_id="trace-missing-tool",
        event_sink=lambda name, payload: events.append((name, payload)),
        attempt_event_name="StoryProviderAttempt",
    )
    adapter._provider = FakeProvider([response])

    with pytest.raises(RuntimeError) as error:
        asyncio.run(
            adapter.complete_tool_call(
                messages=[{"role": "user", "content": "test"}],
                tool={"name": "StorydexSubmitLengthPatch", "parameters": {}},
                purpose="story_length_revision",
                tool_name="StorydexSubmitLengthPatch",
            )
        )

    assert getattr(error.value, "reason", "") == "tool_call_absent"
    attempt = events[-1][1]
    assert attempt["outcome"] == "error"
    assert attempt["metadata"]["toolCallStatus"] == "tool_call_absent"
    assert attempt["metadata"]["targetToolPresent"] is False
    assert attempt["metadata"]["rawArgumentsLength"] is None


def test_length_patch_classifies_invalid_json_without_evidence_of_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)
    response = SimpleNamespace(
        content="",
        finish_reason="tool_calls",
        tool_calls=[
            SimpleNamespace(
                name="StorydexSubmitLengthPatch",
                arguments={},
                raw_arguments="not-json",
                parse_error="Expecting value",
            )
        ],
    )
    events: list[tuple[str, Dict[str, Any]]] = []
    adapter = CoomiStoryGenerationAdapter(
        trace_id="trace-invalid-json",
        event_sink=lambda name, payload: events.append((name, payload)),
        attempt_event_name="StoryProviderAttempt",
    )
    adapter._provider = FakeProvider([response])

    with pytest.raises(RuntimeError) as error:
        asyncio.run(
            adapter.complete_tool_call(
                messages=[{"role": "user", "content": "test"}],
                tool={"name": "StorydexSubmitLengthPatch", "parameters": {}},
                purpose="story_length_revision",
                tool_name="StorydexSubmitLengthPatch",
            )
        )

    assert getattr(error.value, "reason", "") == "tool_arguments_invalid_json"
    metadata = events[-1][1]["metadata"]
    assert metadata["toolCallStatus"] == "tool_arguments_invalid_json"
    assert metadata["parseErrorPresent"] is True
    assert metadata["completionCapHit"] is False
    assert "rawArguments" not in metadata


def test_adapter_uses_bounded_paragraph_tool_and_restores_plain_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)
    paragraphs = {
        f"paragraph_{index}": f"第{index}段正文。"
        for index in range(1, 7)
    }
    response = SimpleNamespace(
        content="",
        tool_calls=[
            SimpleNamespace(
                name="submit_story_scene",
                arguments=paragraphs,
                parse_error=None,
            )
        ],
    )
    provider = FakeProvider([response])
    adapter = CoomiStoryGenerationAdapter(trace_id="trace")
    adapter._provider = provider

    content = asyncio.run(
        adapter.complete(
            messages=[{"role": "user", "content": "test"}],
            purpose="semantic_budget_scene",
            metadata={"scene": 2, "desiredWordCount": 562},
        )
    )

    assert content == "\n\n".join(paragraphs.values())
    assert len(provider.options) == 1
    tools = provider.options[0]
    assert len(tools) == 1
    function = tools[0]["function"]
    assert function["name"] == "submit_story_scene"
    properties = function["parameters"]["properties"]
    assert list(properties) == list(paragraphs)
    assert all(spec["minLength"] == 70 for spec in properties.values())
    assert all(spec["maxLength"] == 113 for spec in properties.values())


def test_adapter_keeps_plain_content_when_scene_tool_is_not_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)
    provider = FakeProvider([SimpleNamespace(content="普通正文。", tool_calls=[])])
    adapter = CoomiStoryGenerationAdapter(trace_id="trace")
    adapter._provider = provider

    content = asyncio.run(
        adapter.complete(
            messages=[{"role": "user", "content": "test"}],
            purpose="semantic_budget_revision",
            metadata={"scene": 1, "desiredWordCount": 750},
        )
    )

    assert content == "普通正文。"
    assert provider.options[0]


def test_adapter_keeps_non_scene_generation_tool_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)
    provider = FakeProvider([SimpleNamespace(content="计划正文。", tool_calls=[])])
    adapter = CoomiStoryGenerationAdapter(trace_id="trace")
    adapter._provider = provider

    content = asyncio.run(
        adapter.complete(
            messages=[{"role": "user", "content": "test"}],
            purpose="semantic_budget_plan",
            metadata={"sceneCount": 4, "targetWordCount": 3000},
        )
    )

    assert content == "计划正文。"
    assert provider.options == [None]


def test_adapter_rejects_an_empty_scene_response_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)
    provider = FakeProvider([SimpleNamespace(content="", tool_calls=[])])
    events: list[tuple[str, Dict[str, Any]]] = []
    adapter = CoomiStoryGenerationAdapter(
        trace_id="trace",
        event_sink=lambda name, payload: events.append((name, payload)),
    )
    adapter._provider = provider

    with pytest.raises(coomi_agent_service.StorydexCoomiEmptyResponse):
        asyncio.run(
            adapter.complete(
                messages=[{"role": "user", "content": "test"}],
                purpose="semantic_budget_revision",
                metadata={"scene": 1, "desiredWordCount": 750},
            )
        )

    assert provider.calls == 1
    assert adapter.provider_retries == 0
    assert events[0][1]["outcome"] == "error"
    assert events[0][1]["retryScheduled"] is False


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


def test_length_patch_applies_a_cap_only_when_the_provider_explicitly_supports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)

    class CapProvider:
        def __init__(self) -> None:
            self.received_cap: int | None = None

        async def chat(
            self,
            _messages: list[Dict[str, str]],
            _tools: Any,
            max_completion_tokens: int | None = None,
        ) -> Any:
            self.received_cap = max_completion_tokens
            return _tool_response(completion_tokens=137)

    events: list[tuple[str, Dict[str, Any]]] = []
    provider = CapProvider()
    adapter = CoomiStoryGenerationAdapter(
        trace_id="trace-cap",
        event_sink=lambda name, payload: events.append((name, payload)),
        attempt_event_name="StoryProviderAttempt",
    )
    adapter._provider = provider

    result = asyncio.run(
        adapter.complete_tool_call(
            messages=[{"role": "user", "content": "test"}],
            tool={"name": "StorydexSubmitLengthPatch", "parameters": {}},
            purpose="story_length_revision",
            tool_name="StorydexSubmitLengthPatch",
            max_completion_tokens=768,
        )
    )

    assert result is not None
    assert provider.received_cap == 768
    assert adapter.last_cap_applied is True
    assert adapter.last_completion_tokens == 137
    assert events[-1][1]["metadata"]["capApplied"] is True
    assert events[-1][1]["metadata"]["completionTokens"] == 137


def test_feedback_bounded_redraft_forwards_its_dynamic_cap_and_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)
    response = SimpleNamespace(
        content="",
        tool_calls=[
            SimpleNamespace(
                name="StorydexSubmitBoundedRedraft",
                arguments={
                    "version": 1,
                    "strategy": "feedback_bounded_redraft",
                    "paragraphs": ["重写后的第一段。", "重写后的第二段。"],
                },
            )
        ],
        usage={
            "prompt_tokens": 3000,
            "completion_tokens": 4200,
            "total_tokens": 7200,
        },
    )

    class CapProvider:
        def __init__(self) -> None:
            self.calls = 0
            self.received_cap: int | None = None

        async def chat(
            self,
            _messages: list[Dict[str, str]],
            _tools: Any,
            max_completion_tokens: int | None = None,
        ) -> Any:
            self.calls += 1
            self.received_cap = max_completion_tokens
            return response

    events: list[tuple[str, Dict[str, Any]]] = []
    provider = CapProvider()
    adapter = CoomiStoryGenerationAdapter(
        trace_id="trace-feedback-redraft-cap",
        event_sink=lambda name, payload: events.append((name, payload)),
        attempt_event_name="StoryProviderAttempt",
    )
    adapter._provider = provider

    result = asyncio.run(
        adapter.complete_tool_call(
            messages=[{"role": "user", "content": "test"}],
            tool={"name": "StorydexSubmitBoundedRedraft", "parameters": {}},
            purpose="story_length_revision",
            tool_name="StorydexSubmitBoundedRedraft",
            max_completion_tokens=5600,
            metadata={"strategy": "feedback_bounded_redraft"},
        )
    )

    assert provider.calls == 1
    assert provider.received_cap == 5600
    assert result == {
        "version": 1,
        "strategy": "feedback_bounded_redraft",
        "paragraphs": ["重写后的第一段。", "重写后的第二段。"],
    }
    assert adapter.last_cap_applied is True
    assert adapter.last_completion_tokens == 4200
    metadata = events[-1][1]["metadata"]
    assert metadata["toolName"] == "StorydexSubmitBoundedRedraft"
    assert metadata["strategy"] == "feedback_bounded_redraft"
    assert metadata["maxCompletionTokens"] == 5600


def test_length_patch_reports_an_unapplied_cap_and_unknown_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)
    events: list[tuple[str, Dict[str, Any]]] = []
    provider = FakeProvider([_tool_response()])
    adapter = CoomiStoryGenerationAdapter(
        trace_id="trace-no-cap",
        event_sink=lambda name, payload: events.append((name, payload)),
        attempt_event_name="StoryProviderAttempt",
    )
    adapter._provider = provider

    asyncio.run(
        adapter.complete_tool_call(
            messages=[{"role": "user", "content": "test"}],
            tool={"name": "StorydexSubmitLengthPatch", "parameters": {}},
            purpose="story_length_revision",
            tool_name="StorydexSubmitLengthPatch",
            max_completion_tokens=768,
        )
    )

    assert adapter.last_cap_applied is False
    assert adapter.last_completion_tokens is None
    assert events[-1][1]["metadata"]["capApplied"] is False
    assert events[-1][1]["metadata"]["completionTokens"] is None


def test_openai_compatible_length_patch_transmits_one_real_completion_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)

    class OpenAICompatibleBoundary:
        model = "deepseek-v4-flash"

        def __init__(self) -> None:
            self.chat_calls = 0
            self.requests: list[dict[str, Any]] = []
            self.client = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(create=self._create),
                )
            )

        def _build_params(
            self,
            messages: list[Dict[str, str]],
            tools: Any,
            *,
            stream: bool,
            tool_choice: str = "auto",
        ) -> dict[str, Any]:
            assert stream is False
            return {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
            }

        async def _create(self, **params: Any) -> Any:
            self.requests.append(params)
            return SimpleNamespace(raw=True)

        def _parse_response(self, _response: Any, *, tools_enabled: bool) -> Any:
            assert tools_enabled is True
            return _tool_response(completion_tokens=137)

        async def chat(
            self,
            _messages: list[Dict[str, str]],
            _tools: Any,
            **_kwargs: Any,
        ) -> Any:
            # This reproduces Coomi's generic Provider seam: variadic options are
            # accepted but never reach the HTTP request.
            self.chat_calls += 1
            return _tool_response()

    provider = OpenAICompatibleBoundary()
    adapter = CoomiStoryGenerationAdapter(
        trace_id="trace-opencode-cap",
        provider=provider,
    )

    result = asyncio.run(
        adapter.complete_tool_call(
            messages=[{"role": "user", "content": "test"}],
            tool={"name": "StorydexSubmitLengthPatch", "parameters": {}},
            purpose="story_length_revision",
            tool_name="StorydexSubmitLengthPatch",
            max_completion_tokens=768,
        )
    )

    assert result is not None
    assert provider.chat_calls == 0
    assert len(provider.requests) == 1
    assert provider.requests[0]["max_tokens"] == 768
    assert provider.requests[0]["tool_choice"] == "required"
    assert provider.requests[0]["extra_body"] == {
        "thinking": {"type": "disabled"},
    }
    assert adapter.last_cap_applied is True
    assert adapter.last_completion_tokens == 137
    assert asyncio.run(adapter.revision_budget_policy()) == {
        "name": "openai_compatible_non_streaming",
        "deadlineRatio": 1.25,
        "deadlineMinimumSeconds": 30,
        "deadlineMaximumSeconds": 60,
    }


def test_openai_compatible_length_patch_preserves_raw_finish_reason_for_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)

    class OpenAICompatibleBoundary:
        model = "deepseek-v4-flash"

        def __init__(self) -> None:
            self.client = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(create=self._create),
                )
            )

        def _build_params(
            self,
            messages: list[Dict[str, str]],
            tools: Any,
            *,
            stream: bool,
            tool_choice: str = "auto",
        ) -> dict[str, Any]:
            return {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "stream": stream,
                "tool_choice": tool_choice,
            }

        async def _create(self, **_params: Any) -> Any:
            return SimpleNamespace(
                choices=[SimpleNamespace(finish_reason="length")],
            )

        def _parse_response(self, _response: Any, *, tools_enabled: bool) -> Any:
            assert tools_enabled is True
            return SimpleNamespace(
                content="",
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 767,
                    "total_tokens": 867,
                },
                tool_calls=[
                    SimpleNamespace(
                        name="StorydexSubmitLengthPatch",
                        arguments={},
                        raw_arguments='{"operations":[',
                        parse_error="Expecting value",
                    )
                ],
            )

    events: list[tuple[str, Dict[str, Any]]] = []
    adapter = CoomiStoryGenerationAdapter(
        trace_id="trace-finish-reason",
        provider=OpenAICompatibleBoundary(),
        event_sink=lambda name, payload: events.append((name, payload)),
        attempt_event_name="StoryProviderAttempt",
    )

    with pytest.raises(RuntimeError) as error:
        asyncio.run(
            adapter.complete_tool_call(
                messages=[{"role": "user", "content": "test"}],
                tool={"name": "StorydexSubmitLengthPatch", "parameters": {}},
                purpose="story_length_revision",
                tool_name="StorydexSubmitLengthPatch",
                max_completion_tokens=768,
            )
        )

    assert getattr(error.value, "reason", "") == "tool_arguments_truncated"
    metadata = events[-1][1]["metadata"]
    assert metadata["finishReason"] == "length"
    assert metadata["completionCapHit"] is False


def test_length_patch_deadline_cancels_one_attempt_and_keeps_usage_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", nullcontext)

    class SlowProvider:
        model = "deepseek-v4-flash"

        def __init__(self) -> None:
            self.calls = 0
            self.cancelled = 0

        async def chat(
            self,
            _messages: list[Dict[str, str]],
            _tools: Any,
            max_tokens: int | None = None,
        ) -> Any:
            assert max_tokens == 768
            self.calls += 1
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise

    events: list[tuple[str, Dict[str, Any]]] = []
    provider = SlowProvider()
    adapter = CoomiStoryGenerationAdapter(
        trace_id="trace-timeout",
        provider=provider,
        event_sink=lambda name, payload: events.append((name, payload)),
        attempt_event_name="StoryProviderAttempt",
    )

    async def scenario() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                adapter.complete_tool_call(
                    messages=[{"role": "user", "content": "test"}],
                    tool={"name": "StorydexSubmitLengthPatch", "parameters": {}},
                    purpose="story_length_revision",
                    tool_name="StorydexSubmitLengthPatch",
                    max_completion_tokens=768,
                ),
                timeout=0.01,
            )

    asyncio.run(scenario())

    assert provider.calls == 1
    assert provider.cancelled == 1
    assert adapter.provider_attempts == 1
    assert adapter.last_completion_tokens is None
    assert len(events) == 1
    assert events[0][1]["outcome"] == "error"
    assert events[0][1]["errorType"] == "CancelledError"
    assert events[0][1]["metadata"]["capApplied"] is True
    assert events[0][1]["metadata"]["completionTokens"] is None
