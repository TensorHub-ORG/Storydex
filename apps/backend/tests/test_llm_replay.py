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
    reset_llm_fixture_state,
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

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr("services.llm_replay.asyncio.sleep", no_delay)
    provider = TransientModelCatalogProvider()
    replayable = get_replayable_llm_provider(provider)

    response = asyncio.run(replayable.chat([{"role": "user", "content": "hello"}]))
    assert response.content == "recovered"
    assert provider.calls == 2

    async def collect_stream():
        return [item async for item in replayable.chat_stream_with_tools([{"role": "user", "content": "hello"}])]

    chunks = asyncio.run(collect_stream())
    assert provider.stream_calls == 2
    assert chunks[0] == {"type": "content", "content": "recovered"}


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
