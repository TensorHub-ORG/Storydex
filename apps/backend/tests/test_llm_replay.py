from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from services.context_trace_service import (
    build_context_trace,
    create_context_source,
    finalize_context_source,
    merge_llm_metrics,
)
from services.llm_replay import (
    ReplayMismatch,
    get_llm_metrics,
    get_replayable_llm_provider,
    llm_context_assembly,
    llm_purpose,
    llm_trace,
    normalize_replay_tool_content,
    replayable_external_tool_call,
    reset_llm_fixture_state,
    reset_llm_metrics,
    truncate_external_tool_fixture,
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
            usage={
                "source": "provider_response",
                "protocol": "openai_chat",
                "prompt_tokens": self.calls,
                "completion_tokens": 2,
                "total_tokens": self.calls + 2,
            },
        )

    async def chat_stream(self, messages, **kwargs):
        for chunk in ("a", "b"):
            yield chunk

    async def chat_stream_with_tools(self, messages, tools=None, **kwargs):
        yield {"type": "content", "content": "hello"}
        yield {
            "type": "usage",
            "data": {
                "source": "provider_response",
                "protocol": "openai_chat",
                "prompt_tokens": 3,
                "completion_tokens": 4,
                "total_tokens": 7,
            },
        }


class CumulativeStreamUsageProvider(FakeProvider):
    async def chat_stream_with_tools(self, messages, tools=None, **kwargs):
        yield {
            "type": "usage",
            "data": {
                "source": "provider_response",
                "protocol": "openai_chat",
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "total_tokens": 11,
            },
        }
        yield {"type": "content", "content": "hello"}
        yield {
            "type": "usage",
            "data": {
                "source": "provider_response",
                "protocol": "openai_chat",
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
            },
        }
        yield {"type": "tool_call_start", "tool_name": "probe", "index": 0}
        yield {
            "type": "usage",
            "data": {
                "source": "provider_response",
                "protocol": "openai_chat",
                "prompt_tokens": 10,
                "completion_tokens": 6,
                "total_tokens": 16,
            },
        }
        yield {
            "type": "tool_call",
            "data": {
                "id": "call-1",
                "name": "probe",
                "arguments": {"value": "ok"},
                "parse_error": None,
            },
        }


class MixedUsageProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.usages = [
            {
                "_type": "LLMUsage",
                "_version": 1,
                "source": "provider_response",
                "protocol": "openai_chat",
                "request_id": "req-rich",
                "requested_model": "fake-model",
                "reported_model": "routed-model",
                "input_tokens": 10,
                "output_tokens": 2,
                "total_tokens": 12,
                "cache_read_input_tokens": 4,
                "cache_creation_input_tokens": 3,
                "reasoning_tokens": 1,
                "estimated_input_tokens": 9,
                "estimator": "coomi:test",
                "usage_snapshot_count": 1,
                "provider_details": {"relay": "公益站"},
            },
            {
                "_type": "LLMUsage",
                "_version": 1,
                "source": "missing",
                "protocol": "openai_chat",
                "cache_read_input_tokens": 999,
                "cache_creation_input_tokens": 998,
                "reasoning_tokens": 997,
                "estimated_input_tokens": 20,
                "estimator": "coomi:test",
            },
            {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8},
        ]

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        return FakeResponse(content=f"mixed-{self.calls}", usage=self.usages.pop(0))


class TransientModelCatalogProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.stream_calls = 0

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("model_not_supported: not supported on the lite model list; use GET /models")
        return FakeResponse(content="recovered", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})

    async def chat_stream_with_tools(self, messages, tools=None, **kwargs):
        self.stream_calls += 1
        if self.stream_calls == 1:
            raise RuntimeError("Model fake-model is not supported on the lite model list. Use GET /v1/models")
        yield {"type": "content", "content": "recovered"}


class ScriptedStreamProvider(FakeProvider):
    def __init__(self, outcomes: list[str]) -> None:
        super().__init__()
        self.outcomes = list(outcomes)
        self.stream_calls = 0

    async def chat_stream_with_tools(self, messages, tools=None, **kwargs):
        outcome = self.outcomes[self.stream_calls]
        self.stream_calls += 1
        if outcome == "error":
            raise RuntimeError("Internal Server Error")
        if outcome == "usage_then_error":
            yield {
                "type": "usage",
                "data": {
                    "source": "provider_response",
                    "protocol": "openai_chat",
                    "prompt_tokens": 11,
                    "completion_tokens": 2,
                    "total_tokens": 13,
                },
            }
            raise RuntimeError("stream closed after usage")
        yield {"type": "content", "content": "recovered"}
        yield {
            "type": "usage",
            "data": {
                "source": "provider_response",
                "protocol": "openai_chat",
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
            },
        }


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


def test_model_catalog_mismatch_retries_once_before_failing(monkeypatch):
    monkeypatch.delenv("STORYDEX_LLM_MODE", raising=False)
    reset_llm_metrics("retry-observation")

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr("services.llm_replay.asyncio.sleep", no_delay)
    provider = TransientModelCatalogProvider()
    replayable = get_replayable_llm_provider(provider)

    with llm_trace("retry-observation"):
        response = asyncio.run(replayable.chat([{"role": "user", "content": "hello"}]))
    assert response.content == "recovered"
    assert provider.calls == 2

    async def collect_stream():
        return [item async for item in replayable.chat_stream_with_tools([{"role": "user", "content": "hello"}])]

    with llm_trace("retry-observation"):
        chunks = asyncio.run(collect_stream())
    assert provider.stream_calls == 2
    assert chunks[0] == {"type": "content", "content": "recovered"}
    requests = get_llm_metrics("retry-observation")["providerRequests"]
    assert [request["providerAttemptCount"] for request in requests] == [2, 2]
    assert [request["providerRetryCount"] for request in requests] == [1, 1]
    assert [[attempt["outcome"] for attempt in request["providerAttempts"]] for request in requests] == [
        ["error", "success"],
        ["error", "success"],
    ]
    assert all(request["providerAttempts"][0]["modelCatalogMismatch"] for request in requests)


def test_outer_retry_of_same_failed_request_is_one_logical_request(monkeypatch):
    monkeypatch.setenv("STORYDEX_LLM_MODE", "off")
    monkeypatch.delenv("STORYDEX_LLM_FIXTURE_DIR", raising=False)
    trace_id = "outer-provider-retry"
    reset_llm_metrics(trace_id)
    provider = get_replayable_llm_provider(ScriptedStreamProvider(["error", "success"]))
    messages = [{"role": "user", "content": "retry exactly this request"}]

    async def collect_once():
        return [chunk async for chunk in provider.chat_stream_with_tools(messages)]

    with llm_trace(trace_id), llm_purpose("loop"):
        with pytest.raises(RuntimeError, match="Internal Server Error"):
            asyncio.run(collect_once())
        chunks = asyncio.run(collect_once())

    assert [chunk["type"] for chunk in chunks] == ["content", "usage"]
    metrics = get_llm_metrics(trace_id)
    assert metrics["calls"] == 1
    assert metrics["byMethod"] == {"chat_stream_with_tools": 1}
    assert metrics["usageCalls"] == 1
    assert metrics["totalTokens"] == 10
    assert len(metrics["providerRequests"]) == 1
    request = metrics["providerRequests"][0]
    assert request["providerRetryObserved"] is True
    assert request["providerAttemptCount"] == 2
    assert request["providerRetryCount"] == 1
    assert [attempt["attempt"] for attempt in request["providerAttempts"]] == [1, 2]
    assert [attempt["outcome"] for attempt in request["providerAttempts"]] == [
        "error",
        "success",
    ]
    assert request["usageSource"] == "provider_response"
    assert request["providerReportedTotalTokens"] == 10


def test_repeated_failures_keep_missing_usage_on_one_logical_request(monkeypatch):
    monkeypatch.setenv("STORYDEX_LLM_MODE", "off")
    monkeypatch.delenv("STORYDEX_LLM_FIXTURE_DIR", raising=False)
    trace_id = "repeated-provider-failure"
    reset_llm_metrics(trace_id)
    provider = get_replayable_llm_provider(
        ScriptedStreamProvider(["error", "error", "error"])
    )
    messages = [{"role": "user", "content": "retry exactly this failed request"}]

    async def collect_once():
        return [chunk async for chunk in provider.chat_stream_with_tools(messages)]

    with llm_trace(trace_id), llm_purpose("loop"):
        for _attempt in range(3):
            with pytest.raises(RuntimeError, match="Internal Server Error"):
                asyncio.run(collect_once())

    metrics = get_llm_metrics(trace_id)
    assert metrics["calls"] == 1
    assert metrics["usageCalls"] == 0
    assert metrics["totalTokens"] == 0
    assert len(metrics["providerRequests"]) == 1
    request = metrics["providerRequests"][0]
    assert request["providerAttemptCount"] == 3
    assert request["providerRetryCount"] == 2
    assert [attempt["attempt"] for attempt in request["providerAttempts"]] == [1, 2, 3]
    assert [attempt["outcome"] for attempt in request["providerAttempts"]] == [
        "error",
        "error",
        "error",
    ]
    assert request["usageSource"] == "missing"
    assert request["providerReportedInputTokens"] is None
    assert request["providerReportedOutputTokens"] is None
    assert request["providerReportedTotalTokens"] is None


def test_stream_error_after_reported_usage_preserves_reported_usage(monkeypatch):
    monkeypatch.setenv("STORYDEX_LLM_MODE", "off")
    monkeypatch.delenv("STORYDEX_LLM_FIXTURE_DIR", raising=False)
    trace_id = "stream-usage-before-error"
    reset_llm_metrics(trace_id)
    provider = get_replayable_llm_provider(ScriptedStreamProvider(["usage_then_error"]))

    async def collect_once():
        return [
            chunk
            async for chunk in provider.chat_stream_with_tools(
                [{"role": "user", "content": "preserve provider usage"}]
            )
        ]

    with llm_trace(trace_id), llm_purpose("loop"):
        with pytest.raises(RuntimeError, match="stream closed after usage"):
            asyncio.run(collect_once())

    metrics = get_llm_metrics(trace_id)
    assert metrics["calls"] == 1
    assert metrics["usageCalls"] == 1
    assert metrics["totalTokens"] == 13
    request = metrics["providerRequests"][0]
    assert request["providerAttemptCount"] == 1
    assert request["providerAttempts"][0]["outcome"] == "error"
    assert request["providerAttempts"][0]["emittedOutput"] is True
    assert request["usageSource"] == "provider_response"
    assert request["providerReportedInputTokens"] == 11
    assert request["providerReportedOutputTokens"] == 2
    assert request["providerReportedTotalTokens"] == 13


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


def test_record_replay_preserves_unicode_line_separator_inside_jsonl(monkeypatch, tmp_path):
    class UnicodeSeparatorProvider(FakeProvider):
        async def chat(self, messages, tools=None, **kwargs):
            self.calls += 1
            return FakeResponse(
                content="left\u2028right",
                usage={
                    "source": "provider_response",
                    "protocol": "openai_chat",
                    "prompt_tokens": 1,
                    "completion_tokens": 2,
                    "total_tokens": 3,
                },
            )

    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    recorded = asyncio.run(
        get_replayable_llm_provider(UnicodeSeparatorProvider()).chat(
            [{"role": "user", "content": "prompt"}]
        )
    )
    assert recorded.content == "left\u2028right"

    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    replay = get_replayable_llm_provider(FakeProvider())
    response = asyncio.run(replay.chat([{"role": "user", "content": "prompt"}]))
    replay.assert_replay_complete()
    assert response.content == "left\u2028right"


def test_external_tool_record_replay_freezes_result_without_live_delegate(monkeypatch, tmp_path):
    from coomi.tools.base import ToolResult

    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    calls = []

    def live_result():
        calls.append("live")
        return ToolResult(success=True, output="search-result-v1")

    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    recorded = replayable_external_tool_call("WebSearch", {"query": "江南制造局"}, live_result)
    assert recorded.output == "search-result-v1"
    assert calls == ["live"]

    reset_llm_fixture_state(tmp_path)
    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    replayed = replayable_external_tool_call(
        "WebSearch",
        {"query": "江南制造局"},
        lambda: (_ for _ in ()).throw(AssertionError("replay must not call WebSearch")),
    )
    assert replayed == ToolResult(success=True, output="search-result-v1")


def test_external_tool_replay_rejects_changed_arguments(monkeypatch, tmp_path):
    from coomi.tools.base import ToolResult

    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    replayable_external_tool_call(
        "WebSearch",
        {"query": "江南制造局"},
        lambda: ToolResult(success=True, output="same"),
    )

    reset_llm_fixture_state(tmp_path)
    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    with pytest.raises(ReplayMismatch, match=r"request\.arguments\.query"):
        replayable_external_tool_call(
            "WebSearch",
            {"query": "保民船"},
            lambda: ToolResult(success=True, output="must-not-run"),
        )


def test_external_tool_replay_matches_parallel_calls_by_arguments(monkeypatch, tmp_path):
    from coomi.tools.base import ToolResult

    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    replayable_external_tool_call(
        "WebSearch",
        {"query": "first"},
        lambda: ToolResult(success=True, output="first-result"),
    )
    replayable_external_tool_call(
        "WebSearch",
        {"query": "second"},
        lambda: ToolResult(success=True, output="second-result"),
    )

    reset_llm_fixture_state(tmp_path)
    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    second = replayable_external_tool_call(
        "WebSearch",
        {"query": "second"},
        lambda: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    first = replayable_external_tool_call(
        "WebSearch",
        {"query": "first"},
        lambda: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    assert second.output == "second-result"
    assert first.output == "first-result"


def test_external_tool_fixture_truncates_failed_prompt_tail(monkeypatch, tmp_path):
    from coomi.tools.base import ToolResult

    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    for query in ("completed", "failed-tail"):
        replayable_external_tool_call(
            "WebSearch",
            {"query": query},
            lambda query=query: ToolResult(success=True, output=f"result-{query}"),
        )

    truncate_external_tool_fixture(tmp_path, 1)
    rows = [
        json.loads(line)
        for line in (tmp_path / "external-tools.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["request"]["arguments"]["query"] for row in rows] == ["completed"]


def test_external_tool_replay_complete_rejects_unused_records(monkeypatch, tmp_path):
    from coomi.tools.base import ToolResult

    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    replayable_external_tool_call(
        "WebSearch",
        {"query": "unused"},
        lambda: ToolResult(success=True, output="unused-result"),
    )
    (tmp_path / "calls.jsonl").write_text("", encoding="utf-8")

    reset_llm_fixture_state(tmp_path)
    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    replay = get_replayable_llm_provider(FakeProvider())
    with pytest.raises(ReplayMismatch, match="External tool replay has 1 unused record"):
        replay.assert_replay_complete()


def test_record_and_replay_sequence_is_shared_across_provider_wrappers(monkeypatch, tmp_path):
    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    reset_llm_fixture_state(tmp_path)
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    first_recorder = get_replayable_llm_provider(FakeProvider())
    second_recorder = get_replayable_llm_provider(FakeProvider())
    asyncio.run(first_recorder.chat([{"role": "user", "content": "one"}]))
    asyncio.run(second_recorder.chat([{"role": "user", "content": "two"}]))
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["seq"] for row in rows] == [1, 2]

    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    first_offline = FakeProvider()
    second_offline = FakeProvider()
    first_replay = get_replayable_llm_provider(first_offline)
    second_replay = get_replayable_llm_provider(second_offline)
    asyncio.run(first_replay.chat([{"role": "user", "content": "one"}]))
    asyncio.run(second_replay.chat([{"role": "user", "content": "two"}]))
    second_replay.assert_replay_complete()
    assert first_offline.calls == second_offline.calls == 0


def test_replay_mismatch_points_to_changed_message(monkeypatch, tmp_path):
    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    recorder = get_replayable_llm_provider(FakeProvider())
    asyncio.run(recorder.chat([{"role": "user", "content": "original"}]))

    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    replay = get_replayable_llm_provider(FakeProvider())
    with pytest.raises(ReplayMismatch, match=r"request\.messages\[0\]\.content"):
        asyncio.run(replay.chat([{"role": "user", "content": "changed"}]))


def test_replay_normalizes_coomi_system_prompt_date(monkeypatch, tmp_path):
    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    recorder = get_replayable_llm_provider(FakeProvider())
    asyncio.run(
        recorder.chat(
            [
                {"role": "system", "content": "## Environment\n- Date: 2026-07-20\n- Model: fake"},
                {"role": "user", "content": "same request"},
            ]
        )
    )

    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    offline = FakeProvider()
    replay = get_replayable_llm_provider(offline)
    response = asyncio.run(
        replay.chat(
            [
                {"role": "system", "content": "## Environment\n- Date: 2026-07-21\n- Model: fake"},
                {"role": "user", "content": "same request"},
            ]
        )
    )
    replay.assert_replay_complete()
    assert response.content == "response-1"
    assert offline.calls == 0


def test_replay_normalizes_volatile_text_fallback_call_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    recorded_id = "text_call_814fa786f7a0"
    replayed_id = "text_call_5cdbc8c1f015"
    recorded_messages = [
        {
            "role": "assistant",
            "content": f"Tool call id: {recorded_id}\nArguments: {{}}",
        },
        {
            "role": "tool",
            "tool_call_id": recorded_id,
            "content": f"Text fallback tool result:\nTool call id: {recorded_id}",
        },
    ]
    replayed_messages = [
        {
            "role": "assistant",
            "content": f"Tool call id: {replayed_id}\nArguments: {{}}",
        },
        {
            "role": "tool",
            "tool_call_id": replayed_id,
            "content": f"Text fallback tool result:\nTool call id: {replayed_id}",
        },
    ]

    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    recorder = get_replayable_llm_provider(FakeProvider())
    asyncio.run(recorder.chat(recorded_messages))

    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    replay = get_replayable_llm_provider(FakeProvider())
    response = asyncio.run(replay.chat(replayed_messages))
    replay.assert_replay_complete()
    assert response.content == "response-1"


def test_replay_keeps_distinct_text_fallback_call_id_relationships(monkeypatch, tmp_path):
    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    recorded_messages = [
        {"role": "assistant", "content": "first text_call_aaaaaaaaaaaa then text_call_bbbbbbbbbbbb"},
        {"role": "tool", "tool_call_id": "text_call_aaaaaaaaaaaa", "content": "first"},
    ]
    changed_relationship = [
        {"role": "assistant", "content": "first text_call_cccccccccccc then text_call_cccccccccccc"},
        {"role": "tool", "tool_call_id": "text_call_cccccccccccc", "content": "first"},
    ]

    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    recorder = get_replayable_llm_provider(FakeProvider())
    asyncio.run(recorder.chat(recorded_messages))

    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    replay = get_replayable_llm_provider(FakeProvider())
    with pytest.raises(ReplayMismatch):
        asyncio.run(replay.chat(changed_relationship))


def test_replay_normalizes_semantically_equal_tool_argument_json(monkeypatch, tmp_path):
    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    recorder = get_replayable_llm_provider(FakeProvider())
    recorded_messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "Glob", "arguments": '{"pattern":"chapters/*.txt","path":"C:/book"}'},
                }
            ],
        }
    ]
    asyncio.run(recorder.chat(recorded_messages))

    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    replay = get_replayable_llm_provider(FakeProvider())
    reordered_messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "Glob", "arguments": '{"path":"C:/book","pattern":"chapters/*.txt"}'},
                }
            ],
        }
    ]
    response = asyncio.run(replay.chat(reordered_messages))
    replay.assert_replay_complete()
    assert response.content == "response-1"


def test_replay_normalizes_only_volatile_tool_result_metadata(monkeypatch, tmp_path):
    first_hash = "a" * 64
    second_hash = "b" * 64
    manifest_padding = "x" * 400
    first = (
        'Full output saved to: C:\\book\\.coomi\\sessions\\11111111-1111-1111-1111-111111111111\\tool_results\\call-1.txt\n'
        '{"generatedAt":"2026-07-15T08:00:00+00:00","lastAnalyzedAt":"2026-07-15T08:00:01+00:00",'
        '"mtime":"2026-07-15T08:00:02+00:00","fact":"same","sources":{'
        '".storydex/memory/chapter-progress.json":{"metadata":{"note":"'
        + manifest_padding
        + '"},"sha256":"'
        + first_hash
        + '","kind":"memory","size":157}}}'
    )
    second = (
        'Full output saved to: C:\\book\\.coomi\\sessions\\22222222-2222-2222-2222-222222222222\\tool_results\\call-1.txt\n'
        '{"generatedAt":"2026-07-15T09:30:00+00:00","lastAnalyzedAt":"2026-07-15T09:30:01+00:00",'
        '"mtime":"2026-07-15T09:30:02+00:00","fact":"same","sources":{'
        '".storydex/memory/chapter-progress.json":{"metadata":{"note":"'
        + manifest_padding
        + '"},"sha256":"'
        + second_hash
        + '","kind":"memory","size":157}}}'
    )
    assert normalize_replay_tool_content(first) == normalize_replay_tool_content(second)
    without_size_first = first.replace(',"size":157', '')
    without_size_second = second.replace(',"size":157', '')
    assert normalize_replay_tool_content(without_size_first) == normalize_replay_tool_content(without_size_second)
    assert normalize_replay_tool_content(first.replace('"size":157', '"size":158')) != normalize_replay_tool_content(
        second
    )
    unrelated_first = first.replace("chapter-progress.json", "facts.json")
    unrelated_second = second.replace("chapter-progress.json", "facts.json")
    assert normalize_replay_tool_content(unrelated_first) != normalize_replay_tool_content(unrelated_second)
    direct_progress_first = '{"version":1,"chapters":{"chapters/001.md":{"state":"draft"}}}'
    direct_progress_second = '{"version":1,"chapters":{"chapters/001.md":{"state":"final"}}}'
    assert normalize_replay_tool_content(direct_progress_first) != normalize_replay_tool_content(
        direct_progress_second
    )
    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    recorder = get_replayable_llm_provider(FakeProvider())
    asyncio.run(recorder.chat([{"role": "tool", "tool_call_id": "call-1", "content": first}]))

    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    replay = get_replayable_llm_provider(FakeProvider())
    response = asyncio.run(replay.chat([{"role": "tool", "tool_call_id": "call-1", "content": second}]))
    replay.assert_replay_complete()
    assert response.content == "response-1"


def test_replay_normalizes_nondeterministic_windows_find_diagnostic(monkeypatch, tmp_path):
    assistant = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "call-bash",
                "type": "function",
                "function": {
                    "name": "Bash",
                    "arguments": '{"command":"find . -type d | head -40"}',
                },
            }
        ],
    }
    stable_error = (
        "'head' is not recognized as an internal or external command,\n"
        "operable program or batch file.\n\n"
        "Error: Command exited with code 255\n"
        "  Command: find . -type d | head -40"
    )
    recorded = "\n[stderr]\nFIND: Parameter format not correct\n" + stable_error
    replayed = "\n[stderr]\n" + stable_error

    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    recorder = get_replayable_llm_provider(FakeProvider())
    asyncio.run(
        recorder.chat(
            [assistant, {"role": "tool", "tool_call_id": "call-bash", "content": recorded}]
        )
    )

    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    replay = get_replayable_llm_provider(FakeProvider())
    response = asyncio.run(
        replay.chat(
            [assistant, {"role": "tool", "tool_call_id": "call-bash", "content": replayed}]
        )
    )
    replay.assert_replay_complete()
    assert response.content == "response-1"


def test_replay_normalizes_storydex_runtime_log_filenames(monkeypatch, tmp_path):
    recorded = (
        "C:/book/.storydex/logs/2026-0717-04-00-16.jsonl\n"
        "C:/book/.storydex/logs/2026-0717-04-01-17.jsonl\n"
        "C:/book/.storydex/wiki/WIKI.md"
    )
    replayed = (
        "C:/book/.storydex/logs/2026-0717-04-13-24.jsonl\n"
        "C:/book/.storydex/logs/2026-0717-04-13-27.jsonl\n"
        "C:/book/.storydex/wiki/WIKI.md"
    )
    block_content = "stable assembled context"

    assert normalize_replay_tool_content(
        recorded,
        tool_name="Glob",
    ) == normalize_replay_tool_content(replayed, tool_name="Glob")

    recorded_messages = [
        {"role": "system", "content": f"prefix\n{block_content}\nsuffix"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-glob",
                    "type": "function",
                    "function": {"name": "Glob", "arguments": '{"pattern":".storydex/**/*"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-glob", "content": recorded},
    ]
    replayed_messages = [
        dict(recorded_messages[0]),
        dict(recorded_messages[1]),
        {"role": "tool", "tool_call_id": "call-glob", "content": replayed},
    ]
    record_source = create_context_source("related_passages", ["chapters/001.md"])
    finalize_context_source(record_source, content=block_content, included=True)
    record_assembly = {
        "promptBlocks": [{"id": "related_passages", "content": block_content}],
        "contextTrace": build_context_trace(
            [record_source],
            [{"id": "related_passages", "content": block_content}],
            assemble_ms=0,
        ),
    }
    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    reset_llm_metrics("runtime-log-record")
    with (
        llm_trace("runtime-log-record"),
        llm_purpose("chat"),
        llm_context_assembly(record_assembly),
    ):
        asyncio.run(get_replayable_llm_provider(FakeProvider()).chat(recorded_messages))

    replay_source = create_context_source("related_passages", ["chapters/001.md"])
    finalize_context_source(replay_source, content=block_content, included=True)
    replay_assembly = {
        "promptBlocks": [{"id": "related_passages", "content": block_content}],
        "contextTrace": build_context_trace(
            [replay_source],
            [{"id": "related_passages", "content": block_content}],
            assemble_ms=0,
        ),
    }
    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    reset_llm_metrics("runtime-log-replay")
    replay = get_replayable_llm_provider(FakeProvider())
    with (
        llm_trace("runtime-log-replay"),
        llm_purpose("chat"),
        llm_context_assembly(replay_assembly),
    ):
        asyncio.run(replay.chat(replayed_messages))
    replay.assert_replay_complete()

    record_hash = get_llm_metrics("runtime-log-record")["providerRequests"][0]["requestHash"]
    replay_hash = get_llm_metrics("runtime-log-replay")["providerRequests"][0]["requestHash"]
    assert record_hash == replay_hash
    assert (
        record_assembly["contextTrace"]["totals"]["contextRequestHash"]
        == replay_assembly["contextTrace"]["totals"]["contextRequestHash"]
        == record_hash
    )


def test_replay_normalizes_relative_storydex_runtime_log_paths(monkeypatch, tmp_path):
    recorded = (
        '{"sourcePaths":[".storydex/logs/2026-0717-12-58-59.jsonl",'
        '".storydex/logs/2026-0717-12-59-47.jsonl"],"fact":"same"}'
    )
    replayed = (
        '{"sourcePaths":[".storydex/logs/2026-0717-13-11-12.jsonl",'
        '".storydex/logs/2026-0717-13-11-15.jsonl"],"fact":"same"}'
    )
    assistant = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "Read", "arguments": '{"file_path":".storydex/wiki/index.json"}'},
            }
        ],
    }

    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    recorder = get_replayable_llm_provider(FakeProvider())
    asyncio.run(
        recorder.chat(
            [assistant, {"role": "tool", "tool_call_id": "call-1", "content": recorded}]
        )
    )

    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    replay = get_replayable_llm_provider(FakeProvider())
    response = asyncio.run(
        replay.chat(
            [assistant, {"role": "tool", "tool_call_id": "call-1", "content": replayed}]
        )
    )
    replay.assert_replay_complete()
    assert response.content == "response-1"


def test_replay_normalizes_storydex_agent_session_filenames_in_grep_preview(monkeypatch, tmp_path):
    recorded = (
        "[Large tool result stored]\n"
        "Output too large (90537 characters). Full output saved to: "
        "C:\\book\\.coomi\\sessions\\11111111-1111-1111-1111-111111111111\\tool_results\\call-1.txt\n\n"
        "Preview:\n"
        "C:/book/.storydex\\.agent\\sessions\\story-session\\"
        "20260717T031229Z_t2-52225b88f0f64d13.json:3:   \"prompt\": \"same\",\n"
        "C:/book/.storydex\\.agent\\sessions\\story-session\\"
        "20260717T031229Z_t2-52225b88f0f64d13.json:10:  \"reply\": \"same\""
    )
    replayed = (
        "[Large tool result stored]\n"
        "Output too large (90537 characters). Full output saved to: "
        "C:\\book\\.coomi\\sessions\\22222222-2222-2222-2222-222222222222\\tool_results\\call-1.txt\n\n"
        "Preview:\n"
        "C:/book/.storydex\\.agent\\sessions\\story-session\\"
        "20260717T032207Z_t2-fa3419342d42310e.json:3:   \"prompt\": \"same\",\n"
        "C:/book/.storydex\\.agent\\sessions\\story-session\\"
        "20260717T032207Z_t2-fa3419342d42310e.json:10:  \"reply\": \"same\""
    )
    assistant = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "Grep", "arguments": '{"pattern":"same"}'},
            }
        ],
    }

    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    recorder = get_replayable_llm_provider(FakeProvider())
    asyncio.run(
        recorder.chat(
            [assistant, {"role": "tool", "tool_call_id": "call-1", "content": recorded}]
        )
    )

    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    replay = get_replayable_llm_provider(FakeProvider())
    response = asyncio.run(
        replay.chat(
            [assistant, {"role": "tool", "tool_call_id": "call-1", "content": replayed}]
        )
    )
    replay.assert_replay_complete()
    assert response.content == "response-1"


def test_replay_treats_only_glob_path_results_as_unordered(monkeypatch, tmp_path):
    glob_fixture = tmp_path / "glob"
    glob_messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-glob",
                    "type": "function",
                    "function": {"name": "Glob", "arguments": '{"pattern":".storydex/**/*"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-glob", "content": "C:\\book\\z.txt\nC:\\book\\a.txt"},
    ]
    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(glob_fixture))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    asyncio.run(get_replayable_llm_provider(FakeProvider()).chat(glob_messages))

    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    replay = get_replayable_llm_provider(FakeProvider())
    reordered = [dict(glob_messages[0]), {**glob_messages[1], "content": "C:\\book\\a.txt\nC:\\book\\z.txt"}]
    asyncio.run(replay.chat(reordered))
    replay.assert_replay_complete()

    read_fixture = tmp_path / "read"
    read_messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-read",
                    "type": "function",
                    "function": {"name": "Read", "arguments": '{"file_path":"paths.txt"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-read", "content": "C:\\book\\z.txt\nC:\\book\\a.txt"},
    ]
    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(read_fixture))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    asyncio.run(get_replayable_llm_provider(FakeProvider()).chat(read_messages))

    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    replay = get_replayable_llm_provider(FakeProvider())
    reordered = [dict(read_messages[0]), {**read_messages[1], "content": "C:\\book\\a.txt\nC:\\book\\z.txt"}]
    with pytest.raises(ReplayMismatch, match=r"request\.messages\[1\]\.content"):
        asyncio.run(replay.chat(reordered))


def test_stream_record_replay_and_trace_metrics(monkeypatch, tmp_path):
    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "record")
    reset_llm_metrics()
    recorder = get_replayable_llm_provider(FakeProvider())

    async def collect(provider):
        return [chunk async for chunk in provider.chat_stream_with_tools([{"role": "user", "content": "stream"}])]

    with llm_trace("trace-1"), llm_purpose("chat"):
        recorded = asyncio.run(collect(recorder))
    metrics = get_llm_metrics("trace-1")
    provider_requests = metrics.pop("providerRequests")
    assert metrics == {
        "traceId": "trace-1",
        "calls": 1,
        "byMethod": {"chat_stream_with_tools": 1},
        "promptTokens": 3,
        "completionTokens": 4,
        "totalTokens": 7,
        "usageCalls": 1,
        "llmCalls": [
            {
                "purpose": "chat",
                "method": "chat_stream_with_tools",
                "count": 1,
                "inputTokens": 3,
                "outputTokens": 4,
            }
        ],
    }
    assert len(provider_requests) == 1
    assert provider_requests[0]["purpose"] == "chat"
    assert provider_requests[0]["method"] == "chat_stream_with_tools"
    assert provider_requests[0]["inputTokens"] == 3
    assert provider_requests[0]["outputTokens"] == 4
    assert provider_requests[0]["requestEstTokens"] > 0
    assert provider_requests[0]["estimateErrorTokens"] == provider_requests[0]["requestEstTokens"] - 3

    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    replay = get_replayable_llm_provider(FakeProvider())
    assert asyncio.run(collect(replay)) == recorded
    replay.assert_replay_complete()


def test_stream_collapses_cumulative_usage_snapshots(monkeypatch):
    monkeypatch.setenv("STORYDEX_LLM_MODE", "off")
    monkeypatch.delenv("STORYDEX_LLM_FIXTURE_DIR", raising=False)
    reset_llm_metrics()
    provider = get_replayable_llm_provider(CumulativeStreamUsageProvider())

    async def collect():
        return [
            chunk
            async for chunk in provider.chat_stream_with_tools(
                [{"role": "user", "content": "exercise a real streamed tool turn"}]
            )
        ]

    with llm_trace("cumulative-usage"), llm_purpose("chat"):
        chunks = asyncio.run(collect())

    assert [chunk["type"] for chunk in chunks] == [
        "content",
        "tool_call_start",
        "tool_call",
        "usage",
    ]
    assert chunks[-1]["data"]["source"] == "provider_response"
    assert chunks[-1]["data"]["protocol"] == "openai_chat"
    assert chunks[-1]["data"]["prompt_tokens"] == 10
    assert chunks[-1]["data"]["completion_tokens"] == 6
    assert chunks[-1]["data"]["total_tokens"] == 16
    assert chunks[-1]["data"]["usageSnapshotCount"] == 3
    metrics = get_llm_metrics("cumulative-usage")
    provider_requests = metrics.pop("providerRequests")
    assert metrics == {
        "traceId": "cumulative-usage",
        "calls": 1,
        "byMethod": {"chat_stream_with_tools": 1},
        "promptTokens": 10,
        "completionTokens": 6,
        "totalTokens": 16,
        "usageCalls": 1,
        "llmCalls": [
            {
                "purpose": "chat",
                "method": "chat_stream_with_tools",
                "count": 1,
                "inputTokens": 10,
                "outputTokens": 6,
            }
        ],
    }
    assert len(provider_requests) == 1
    assert provider_requests[0]["inputTokens"] == 10
    assert provider_requests[0]["outputTokens"] == 6
    assert provider_requests[0]["totalTokens"] == 16
    assert provider_requests[0]["estimateErrorPct"] is not None


def test_chat_requests_capture_context_position_and_aggregate_per_request_estimate_error(monkeypatch):
    monkeypatch.setenv("STORYDEX_LLM_MODE", "off")
    monkeypatch.delenv("STORYDEX_LLM_FIXTURE_DIR", raising=False)
    block_content = "同一条可核验的项目事实。"
    source = create_context_source(
        "related_passages",
        ["chapters/001.md"],
        candidate=block_content,
        elapsed_ms=1.25,
    )
    finalize_context_source(source, content=block_content, included=True)
    block = {"id": "related_passages", "content": block_content}
    assembly = {
        "promptBlocks": [block],
        "contextTrace": build_context_trace([source], [block], assemble_ms=2.5),
    }
    provider = get_replayable_llm_provider(FakeProvider())

    messages = [
        {"role": "system", "content": f"prefix\n{block_content}\nsuffix"},
        {"role": "user", "content": "完成一次完整审校"},
    ]

    async def run_two_requests():
        first = await provider.chat(
            messages,
            tools=[{"type": "function", "function": {"name": "Search"}}],
        )
        second = await provider.chat(
            [*messages, {"role": "assistant", "content": first.content}],
            tools=[{"type": "function", "function": {"name": "Search"}}],
        )
        return first, second

    reset_llm_metrics("context-request")
    with llm_trace("context-request"), llm_purpose("chat"), llm_context_assembly(assembly):
        asyncio.run(run_two_requests())

    captured = merge_llm_metrics(assembly["contextTrace"], get_llm_metrics("context-request"))
    captured_source = captured["sources"][0]
    provider_requests = captured["providerRequests"]
    totals = captured["totals"]
    assert captured_source["messageIndex"] == 0
    assert 0 < captured_source["startEstToken"] < captured_source["endEstToken"]
    assert totals["contextRequestIndex"] == 0
    assert totals["contextRequestChars"] > len(block_content)
    assert totals["contextRequestEstTokens"] > captured_source["estTokens"]
    assert len(totals["contextRequestHash"]) == 64
    assert "finalRequestHash" not in totals
    assert len(provider_requests) == 2
    assert [request["inputTokens"] for request in provider_requests] == [1, 2]
    assert totals["providerRequestCount"] == 2
    assert totals["providerUsageRequestCount"] == 2
    assert totals["providerUsageRequestEstTokens"] == sum(
        request["requestEstTokens"] for request in provider_requests
    )
    assert totals["providerInputEstimateErrorTokens"] == (
        totals["providerUsageRequestEstTokens"] - totals["providerInputTokens"]
    )
    assert totals["providerInputEstimateErrorPct"] is not None


def test_mixed_usage_provenance_only_aggregates_reported_requests(monkeypatch):
    monkeypatch.setenv("STORYDEX_LLM_MODE", "off")
    monkeypatch.delenv("STORYDEX_LLM_FIXTURE_DIR", raising=False)
    reset_llm_metrics("mixed-provenance")
    provider = get_replayable_llm_provider(MixedUsageProvider())

    with llm_trace("mixed-provenance"), llm_purpose("chat"):
        for index in range(3):
            asyncio.run(
                provider.chat(
                    [{"role": "user", "content": f"growing conversation turn {index}"}]
                )
            )

    metrics = get_llm_metrics("mixed-provenance")
    assert metrics["promptTokens"] == 10
    assert metrics["completionTokens"] == 2
    assert metrics["totalTokens"] == 12
    assert metrics["usageCalls"] == 1
    requests = metrics["providerRequests"]
    assert [request["usageSource"] for request in requests] == [
        "provider_response",
        "missing",
        "legacy_unknown",
    ]
    assert requests[0]["requestId"] == "req-rich"
    assert requests[0]["reportedModel"] == "routed-model"
    assert requests[0]["cacheReadInputTokens"] == 4
    assert requests[0]["cacheCreationInputTokens"] == 3
    assert requests[0]["reasoningTokens"] == 1
    assert requests[0]["providerDetails"] == {"relay": "公益站"}
    assert requests[1]["providerReportedInputTokens"] is None
    assert requests[1]["estimatedInputTokens"] == 20
    assert requests[1]["cacheReadInputTokens"] is None
    assert requests[1]["cacheCreationInputTokens"] is None
    assert requests[1]["reasoningTokens"] is None
    assert requests[2]["providerReportedInputTokens"] is None
    assert requests[2]["usage"]["inputTokens"] == 7

    trace = build_context_trace([], [], assemble_ms=0)
    merged = merge_llm_metrics(trace, metrics)
    totals = merged["totals"]
    assert totals["providerReportedUsageRequestCount"] == 1
    assert totals["providerReportedInputTokens"] == 10
    assert totals["providerReportedOutputTokens"] == 2
    assert totals["providerReportedTotalTokens"] == 12
    assert totals["providerReportedCacheReadInputTokens"] == 4
    assert totals["providerReportedCacheCreationInputTokens"] == 3
    assert totals["providerReportedReasoningTokens"] == 1
    assert totals["estimatedUsageRequestCount"] == 3
    assert totals["missingUsageRequestCount"] == 1
    assert totals["legacyUnknownUsageRequestCount"] == 1
    assert totals["reportedUsageCoveragePct"] == pytest.approx(33.3333)
    assert totals["providerUsageRequestCount"] == 1
    assert totals["providerTotalTokens"] == 12


def test_partial_reported_usage_preserves_missing_fields(monkeypatch):
    monkeypatch.setenv("STORYDEX_LLM_MODE", "off")
    monkeypatch.delenv("STORYDEX_LLM_FIXTURE_DIR", raising=False)
    reset_llm_metrics("partial-reported")

    class PartialUsageProvider(FakeProvider):
        async def chat(self, messages, tools=None, **kwargs):
            return FakeResponse(
                content="partial",
                usage={
                    "source": "provider_response",
                    "protocol": "openai_chat",
                    "output_tokens": 5,
                    "total_tokens": 5,
                },
            )

    provider = get_replayable_llm_provider(PartialUsageProvider())
    with llm_trace("partial-reported"), llm_purpose("chat"):
        asyncio.run(provider.chat([{"role": "user", "content": "partial usage"}]))

    metrics = get_llm_metrics("partial-reported")
    request = metrics["providerRequests"][0]
    assert request["usageSource"] == "provider_response"
    assert request["providerReportedInputTokens"] is None
    assert request["providerReportedOutputTokens"] == 5
    assert request["providerReportedTotalTokens"] == 5
    assert request["estimateErrorTokens"] is None

    merged = merge_llm_metrics(build_context_trace([], [], assemble_ms=0), metrics)
    totals = merged["totals"]
    assert totals["providerReportedUsageRequestCount"] == 1
    assert totals["providerReportedUsageRequestEstTokens"] == request["requestEstTokens"]
    assert totals["providerReportedInputTokens"] is None
    assert totals["providerReportedOutputTokens"] == 5
    assert totals["providerReportedTotalTokens"] == 5
    assert totals["providerReportedInputEstimateErrorTokens"] is None
    assert totals["reportedUsageCoveragePct"] == 100.0


def test_checked_in_smoke_fixture_replays_without_provider_call(monkeypatch):
    fixture_dir = Path(__file__).parent / "fixtures" / "llm_replay" / "smoke"
    monkeypatch.setenv("STORYDEX_LLM_FIXTURE_DIR", str(fixture_dir))
    monkeypatch.setenv("STORYDEX_LLM_MODE", "replay")
    reset_llm_metrics("default")

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
    metrics = get_llm_metrics("default")
    assert metrics["usageCalls"] == 0
    assert metrics["totalTokens"] == 0
    assert [request["usageSource"] for request in metrics["providerRequests"]] == [
        "legacy_unknown",
        "legacy_unknown",
    ]
    assert all(
        request["providerReportedTotalTokens"] is None
        for request in metrics["providerRequests"]
    )


def test_get_after_read_returns_metrics_then_reset_clears_exact_trace_and_leaves_others():
    """Verify the lifecycle contract:
    - get_llm_metrics returns populated metrics after calls
    - reset_llm_metrics(trace_id) clears that trace
    - get_llm_metrics(trace_id) returns empty after reset
    - other trace IDs with non-zero data are NOT affected
    """
    reset_llm_metrics()
    provider = get_replayable_llm_provider(FakeProvider())

    # Empty baseline for both traces
    assert get_llm_metrics("trace-a")["calls"] == 0
    assert get_llm_metrics("trace-b")["calls"] == 0

    # Write real usage into trace-a
    with llm_trace("trace-a"), llm_purpose("chat"):
        asyncio.run(provider.chat([{"role": "user", "content": "hello"}]))
    # Write real usage into trace-b under its own trace context
    with llm_trace("trace-b"), llm_purpose("intent"):
        asyncio.run(provider.chat([{"role": "user", "content": "world"}]))
    # A second call under trace-b to confirm accumulation
    with llm_trace("trace-b"), llm_purpose("chat"):
        asyncio.run(provider.chat([{"role": "user", "content": "again"}]))

    metrics_a = get_llm_metrics("trace-a")
    assert metrics_a["calls"] >= 1
    assert metrics_a["promptTokens"] > 0

    metrics_b = get_llm_metrics("trace-b")
    assert metrics_b["calls"] >= 2
    assert metrics_b["promptTokens"] > 0

    reset_llm_metrics("trace-a")
    after_reset_a = get_llm_metrics("trace-a")
    assert after_reset_a["calls"] == 0
    assert after_reset_a["promptTokens"] == 0
    assert after_reset_a["completionTokens"] == 0

    # trace-b's non-zero snapshot must be intact after trace-a is cleaned
    metrics_b_after = get_llm_metrics("trace-b")
    assert metrics_b_after == metrics_b

    # Also check no trace-a key remains in _COUNTERS (not just default-empty response)
    from services.llm_replay import _COUNTERS
    assert "trace-a" not in _COUNTERS
    reset_llm_metrics("trace-b")
