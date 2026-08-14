from __future__ import annotations

from api import routes_agent as routes
from api.response import ApiTrace
from services.agent_lifecycle_trace import build_agent_lifecycle_trace


BASE = "2026-08-08T00:00:00+00:00"


def stamp(seconds: int) -> str:
    whole = max(0, int(seconds))
    return f"2026-08-08T00:00:{whole:02d}.000000+00:00"


def event(name: str, seconds: int, **data: object) -> dict:
    return {"event": name, "timestamp": stamp(seconds), "data": data}


def test_lifecycle_trace_groups_rounds_tools_phases_and_retries() -> None:
    events = [
        event(
            "RunAccepted",
            0,
            status="running",
        ),
        event(
            "TurnPhase",
            0,
            phase="intent_classification",
            status="running",
            startedAt=stamp(0),
        ),
        event(
            "TurnPhase",
            1,
            phase="intent_classification",
            status="success",
            startedAt=stamp(0),
            elapsedMs=1000,
        ),
        event("TurnPhase", 2, phase="model", status="running", current=1),
        event("TextChunk", 4, content="hello"),
        event("ToolStart", 5, tool_call_id="read-1", tool_name="read_file"),
        event(
            "ToolDone",
            6,
            tool_call_id="read-1",
            tool_name="read_file",
            duration_ms=1000,
            is_error=False,
        ),
        event(
            "ModelCompleted",
            7,
            round=1,
            responseModel="deepseek-v4-flash",
            finishReason="tool_calls",
            nativeReasoning=True,
            usage={
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "output_tokens": 20,
                "reasoning_tokens": 12,
            },
        ),
        event("ConnectionRetry", 8, attempt=1, maxAttempts=3, resetTextCharacters=2),
        event("TurnPhase", 9, phase="model", status="running", current=2),
        event("TextChunk", 10, content="done"),
        event(
            "ModelCompleted",
            11,
            round=2,
            responseModel="deepseek-v4-flash",
            finishReason="stop",
            usage={"input_tokens": 140, "output_tokens": 8},
        ),
        event("AgentCompleted", 12, status="completed", total_tokens=268),
    ]

    lifecycle = build_agent_lifecycle_trace(events, request_started_at=BASE)

    assert lifecycle["firstByteSource"] == "first_output_delta"
    assert lifecycle["timeToFirstByteMs"] == 4000
    assert lifecycle["timeToFirstVisibleOutputMs"] == 4000
    assert lifecycle["durationMs"] == 12000
    assert lifecycle["modelRounds"] == 2
    assert lifecycle["toolCalls"] == 1
    assert lifecycle["toolExecutionMs"] == 1000
    assert lifecycle["retryCount"] == 1
    assert lifecycle["rounds"][0]["inputTokens"] == 100
    assert lifecycle["rounds"][0]["reasoningTokens"] == 12
    assert lifecycle["rounds"][0]["toolCalls"] == 1
    assert lifecycle["rounds"][1]["finishReason"] == "stop"
    assert lifecycle["phaseTotalsMs"]["intent_classification"] == 1000
    assert lifecycle["terminal"]["event"] == "AgentCompleted"
    assert lifecycle["terminal"]["at"] == "2026-08-08T00:00:12+00:00"
    assert lifecycle["terminal"]["status"] == "completed"
    assert lifecycle["terminal"]["code"] == ""
    assert lifecycle["terminal"]["reason"] == ""
    assert all("content" not in item for item in lifecycle["rounds"])


def test_lifecycle_trace_uses_observed_clock_when_server_timestamp_is_absent() -> None:
    events = [
        {"event": "RunAccepted", "observedAt": stamp(0)},
        {"event": "TextChunk", "observedAt": stamp(2), "visibleChars": 3},
        {"event": "AgentError", "observedAt": stamp(3), "error_type": "provider"},
    ]

    lifecycle = build_agent_lifecycle_trace(events, request_started_at=BASE)

    assert lifecycle["clock"] == "observed_at"
    assert lifecycle["firstByteSource"] == "first_output_delta"
    assert lifecycle["visibleOutputChars"] == 3
    assert lifecycle["terminal"]["event"] == "AgentError"
    assert lifecycle["terminal"]["code"] == "provider"


def test_lifecycle_trace_prefers_raw_provider_stream_stages() -> None:
    events = [
        event("RunAccepted", 0, status="running"),
        event("TurnPhase", 1, phase="model", status="running", current=1),
        event(
            "ProviderStream",
            1,
            attempt=1,
            phase="request_started",
            elapsedMs=0,
            requestBytes=4096,
            responseBytes=0,
            maxOutputTokens=8192,
            httpStatus=0,
        ),
        event(
            "ProviderStream",
            2,
            attempt=1,
            phase="response_head",
            elapsedMs=1000,
            requestBytes=4096,
            responseBytes=0,
            maxOutputTokens=8192,
            httpStatus=200,
        ),
        event(
            "ProviderStream",
            4,
            attempt=1,
            phase="first_byte",
            elapsedMs=3000,
            requestBytes=4096,
            responseBytes=128,
            maxOutputTokens=8192,
            httpStatus=200,
        ),
        event(
            "ProviderStream",
            5,
            attempt=1,
            phase="first_event",
            elapsedMs=4000,
            requestBytes=4096,
            responseBytes=192,
            maxOutputTokens=8192,
            httpStatus=200,
        ),
        event("TextChunk", 6, content="done"),
        event(
            "ProviderStream",
            7,
            attempt=1,
            phase="completed",
            elapsedMs=6000,
            requestBytes=4096,
            responseBytes=256,
            maxOutputTokens=8192,
            httpStatus=200,
        ),
        event("ModelCompleted", 7, round=1, usage={"input_tokens": 10, "output_tokens": 2}),
        event("AgentCompleted", 8, status="completed"),
    ]

    lifecycle = build_agent_lifecycle_trace(events, request_started_at=BASE)

    assert lifecycle["firstByteSource"] == "provider_raw_stream"
    assert lifecycle["timeToFirstByteMs"] == 4000
    assert lifecycle["providerWaitMs"] == 3000
    assert lifecycle["providerResponseHeadMs"] == 1000
    assert lifecycle["providerFirstByteMs"] == 3000
    assert lifecycle["providerGenerationMs"] == 3000
    assert lifecycle["providerRequestBytes"] == 4096
    assert lifecycle["providerResponseBytes"] == 256
    assert lifecycle["rounds"][0]["wireMaxOutputTokens"] == 8192
    assert lifecycle["rounds"][0]["firstByteSource"] == "provider_raw_stream"


def test_lifecycle_trace_keeps_failed_provider_round_without_fabricating_first_byte() -> None:
    events = [
        event("RunAccepted", 0, status="running"),
        event("TurnPhase", 1, phase="model", status="running", current=1),
        event(
            "ProviderStream",
            1,
            attempt=1,
            phase="request_started",
            elapsedMs=0,
            requestBytes=4096,
            responseBytes=0,
            maxOutputTokens=8192,
            httpStatus=0,
        ),
        event(
            "ProviderStream",
            3,
            attempt=1,
            phase="response_head",
            elapsedMs=2000,
            requestBytes=4096,
            responseBytes=0,
            maxOutputTokens=8192,
            httpStatus=522,
        ),
        event("ProviderRetry", 3, attempt=1, maxAttempts=2, resetTextCharacters=0),
        event(
            "ProviderStream",
            4,
            attempt=2,
            phase="request_started",
            elapsedMs=0,
            requestBytes=4096,
            responseBytes=0,
            maxOutputTokens=8192,
            httpStatus=0,
        ),
        event(
            "ProviderStream",
            7,
            attempt=2,
            phase="response_head",
            elapsedMs=3000,
            requestBytes=4096,
            responseBytes=0,
            maxOutputTokens=8192,
            httpStatus=522,
        ),
        event("AgentError", 7, error_type="provider"),
    ]

    lifecycle = build_agent_lifecycle_trace(events, request_started_at=BASE)

    assert lifecycle["firstByteAt"] == ""
    assert lifecycle["firstByteSource"] == "unavailable_after_response_head"
    assert lifecycle["timeToFirstByteMs"] == 0
    assert lifecycle["modelRounds"] == 1
    assert lifecycle["completedModelRounds"] == 0
    assert lifecycle["failedModelRounds"] == 1
    assert lifecycle["retryCount"] == 1
    assert lifecycle["providerWaitMs"] == 5000
    assert lifecycle["providerResponseHeadMs"] == 5000
    assert lifecycle["rounds"][0]["status"] == "failed"
    assert lifecycle["rounds"][0]["failureHttpStatus"] == 522
    assert [item["httpStatus"] for item in lifecycle["rounds"][0]["providerAttempts"]] == [
        522,
        522,
    ]


def test_route_trace_exposes_lifecycle_without_exposing_event_content() -> None:
    events = [
        event("RunAccepted", 0, status="running"),
        event("TextChunk", 1, content="secret-looking user text"),
        event("AgentCompleted", 2, total_tokens=3),
    ]

    metrics = routes._extract_trace_metrics(events, "lifecycle-route", 2000)
    response = ApiTrace(**metrics).model_dump(by_alias=True)

    assert response["lifecycle"]["visibleOutputChars"] == len("secret-looking user text")
    assert response["lifecycle"]["durationMs"] == 2000
    assert "secret-looking user text" not in str(response["lifecycle"])
