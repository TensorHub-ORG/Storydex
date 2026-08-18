from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from api.routes_agent import AgentChatRequest
from scripts.run_agent_runtime_contract import (
    ContractError,
    _interaction_event_matches,
    _safe_interaction_tail,
    validate_coomi_status_response,
    validate_health_response,
)
from services.agent_stream_contract import (
    AgentStreamContractError,
    load_fixture,
    parse_sse_events,
    validate_chat_stream_events,
)


def _response(payload: dict, *, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", "http://127.0.0.1/api/v1/sys/health"),
    )


def test_interaction_trigger_can_match_event_payload_fields() -> None:
    expected = {"phase": "model", "status": "running"}

    assert _interaction_event_matches(
        event_name="TurnPhase",
        event_payload={"phase": "model", "status": "running", "current": 1},
        after_event="TurnPhase",
        after_event_fields=expected,
    )
    assert not _interaction_event_matches(
        event_name="TurnPhase",
        event_payload={"phase": "task_planning", "status": "running"},
        after_event="TurnPhase",
        after_event_fields=expected,
    )


def test_interaction_failure_tail_keeps_safe_error_fields_only() -> None:
    tail = _safe_interaction_tail(
        [
            ("TurnPhase", {"phase": "model", "status": "running"}),
            (
                "AgentError",
                {
                    "error_type": "BridgeError",
                    "code": "bridge_error",
                    "message": "Authorization: Bearer must-not-leak",
                    "details": {"stage": "bridge_events", "providerHttpStatus": 503},
                },
            ),
        ]
    )

    assert tail[-1] == {
        "event": "AgentError",
        "code": "bridge_error",
        "error_type": "BridgeError",
        "stage": "bridge_events",
        "providerHttpStatus": 503,
    }
    assert "must-not-leak" not in json.dumps(tail)


def test_health_contract_accepts_shared_storydex_envelope() -> None:
    observed = validate_health_response(
        _response(
            {
                "ok": True,
                "data": {
                    "status": "ok",
                    "service": "Storydex Backend",
                    "time": "2026-08-16T00:00:00Z",
                },
                "error": None,
                "trace": {"traceId": "trace", "durationMs": 1},
                "audit": [],
            }
        )
    )

    assert observed["httpStatus"] == 200
    assert observed["dataStatus"] == "ok"
    assert observed["hasTraceId"] is True


@pytest.mark.parametrize(
    "payload, message",
    (
        ({"ok": True}, "missing fields"),
        (
            {
                "ok": True,
                "data": {"status": "degraded", "service": "Storydex Backend", "time": "now"},
                "error": None,
                "trace": {"traceId": "trace", "durationMs": 0},
                "audit": [],
            },
            "data.status",
        ),
    ),
)
def test_health_contract_rejects_semantic_drift(payload: dict, message: str) -> None:
    with pytest.raises(ContractError, match=message):
        validate_health_response(_response(payload))


def test_coomi_status_contract_accepts_active_storydex_model() -> None:
    observed = validate_coomi_status_response(
        _response(
            {
                "ok": True,
                "data": {
                    "runtime": "storydex-coomi-rs",
                    "installed": True,
                    "providerId": "OPENCODE",
                    "providerType": "openai_compatible",
                    "model": "deepseek-v4-flash",
                    "display": "OpenCode",
                    "models": [
                        {
                            "providerId": "OPENCODE",
                            "model": "deepseek-v4-flash",
                        }
                    ],
                    "providerCapabilities": {
                        "context_window": 256000,
                        "effective_context_window_percent": 95,
                        "max_output_tokens": 8192,
                        "supports_native_tools": True,
                    },
                    "reasoningCapability": {
                        "support": "unknown",
                        "levels": [],
                        "source": "unknown",
                        "promptFallback": False,
                        "routeSensitive": False,
                        "fallbackReason": "",
                    },
                    "reasoningRequestPlan": {
                        "requested": "auto",
                        "control": "auto",
                        "sent": False,
                        "promptApplied": False,
                        "wireFields": [],
                        "support": "unknown",
                        "source": "unknown",
                        "routeSensitive": False,
                        "fallbackReason": "",
                    },
                },
                "error": None,
                "trace": {"traceId": "trace", "durationMs": 1},
                "audit": [{"action": "read_coomi_status"}],
            }
        )
    )

    assert observed["providerId"] == "OPENCODE"
    assert observed["model"] == "deepseek-v4-flash"
    assert observed["sensitiveFieldCount"] == 0


def test_coomi_status_contract_rejects_secret_fields() -> None:
    with pytest.raises(ContractError, match="sensitive fields"):
        validate_coomi_status_response(
            _response(
                {
                    "ok": True,
                    "data": {
                        "runtime": "storydex-coomi-rs",
                        "installed": True,
                        "providerId": "OPENCODE",
                        "providerType": "openai_compatible",
                        "model": "deepseek-v4-flash",
                        "display": "OpenCode",
                        "api_key": "must-not-leak",
                    },
                    "error": None,
                    "trace": {"traceId": "trace", "durationMs": 1},
                    "audit": [],
                }
            )
        )


def test_agent_runtime_manifest_has_unique_known_states() -> None:
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads(
        (root / "apps/backend/contracts/agent-runtime-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    allowed = set(manifest["allowedStates"])
    contracts = manifest["contracts"]

    assert contracts
    assert len({item["id"] for item in contracts}) == len(contracts)
    assert all(item["state"] in allowed for item in contracts)
    assert any(item["id"] == "agent.chat.stream.v1" for item in contracts)


def _event(name: str, **payload: object) -> tuple[str, dict]:
    return name, {"_type": name, "_version": 1, **payload}


def _valid_chat_stream_events() -> list[tuple[str, dict]]:
    trace_id = "trace-contract"
    session_id = "session-contract"
    return [
        _event(
            "RunAccepted",
            traceId=trace_id,
            sessionId=session_id,
            phase="accepted",
            status="running",
        ),
        _event(
            "TurnPhase",
            traceId=trace_id,
            sessionId=session_id,
            phase="intent_classification",
            status="running",
            heartbeat=False,
            elapsedMs=0,
        ),
        _event("TurnContract", traceId=trace_id, sessionId=session_id),
        _event(
            "AgentStarted",
            traceId=trace_id,
            sessionId=session_id,
            llmProvider="OPENCODE",
            llmModel="deepseek-v4-flash",
        ),
        _event("RuntimeMetrics", providerMode="replay"),
        _event(
            "ToolStart",
            tool_name="read_file",
            tool_call_id="contract-read-1",
            arguments={"path": "chapters/agent-stream-contract.md"},
        ),
        _event(
            "ToolDone",
            tool_name="read_file",
            tool_call_id="contract-read-1",
            is_error=False,
        ),
        _event("TextChunk", content="STORYDEX_AGENT_STREAM_CONTRACT_FILE_91C7"),
        _event("AgentCompleted", traceId=trace_id, session_id=session_id),
        ("done", {"type": "done"}),
    ]


def test_chat_stream_contract_accepts_ordered_read_only_replay() -> None:
    root = Path(__file__).resolve().parents[3]
    fixture = load_fixture(
        root
        / "apps/backend/contracts/fixtures/agent-chat-stream-read-only-v1/scenario.json"
    )

    observed = validate_chat_stream_events(
        _valid_chat_stream_events(),
        status_code=200,
        headers={
            "content-type": "text/event-stream; charset=utf-8",
            "cache-control": "no-cache",
            "x-accel-buffering": "no",
        },
        trace_id="trace-contract",
        session_id="session-contract",
        fixture=fixture,
    )

    assert observed["terminalEvent"] == "AgentCompleted"
    assert observed["toolSequence"] == ["read_file"]
    assert observed["providerModes"] == ["replay"]
    assert observed["doneCount"] == 1


def test_chat_stream_contract_freezes_safe_turn_contract_subset() -> None:
    events = _valid_chat_stream_events()
    for name, payload in events:
        if name == "TurnContract":
            payload.update(
                {
                    "status": "ready",
                    "reasoningEffort": "low",
                    "intentFrame": {
                        "primary": "story_generation",
                        "operationType": "modify_existing",
                        "canWrite": True,
                    },
                    "executionPolicy": {
                        "capabilityMode": "scoped_write",
                        "allowedWriteRoots": ["chapters/"],
                    },
                    "contextAssembly": {
                        "activeFile": "chapters/fixture.md",
                        "budget": {
                            "maxTotalChars": 10000,
                            "totalChars": 24,
                            "blockCount": 1,
                        },
                        "contextTrace": {"sources": []},
                        "promptBlocks": [
                            {
                                "id": "active_file",
                                "title": "Active file",
                                "content": "must-not-enter-the-report",
                                "charCount": 24,
                                "sourcePaths": ["chapters/fixture.md"],
                            }
                        ],
                    },
                }
            )

    observed = validate_chat_stream_events(
        events,
        status_code=200,
        headers={
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
            "x-accel-buffering": "no",
        },
        trace_id="trace-contract",
        session_id="session-contract",
        fixture={
            "expected": {
                "terminalEvent": "AgentCompleted",
                "turnContract": {
                    "intentFrame": {
                        "primary": "story_generation",
                        "operationType": "modify_existing",
                        "canWrite": True,
                    },
                    "executionPolicy": {
                        "capabilityMode": "scoped_write",
                        "allowedWriteRoots": ["chapters/"],
                    },
                    "contextAssembly": {
                        "activeFile": "chapters/fixture.md",
                        "budget": {"blockCount": 1},
                        "promptBlocks": [
                            {
                                "id": "active_file",
                                "sourcePaths": ["chapters/fixture.md"],
                            }
                        ],
                    },
                },
            }
        },
    )

    assert observed["turnContract"]["contextAssembly"]["promptBlocks"] == [
        {
            "id": "active_file",
            "title": "Active file",
            "charCount": 24,
            "sourcePaths": ["chapters/fixture.md"],
        }
    ]
    assert "must-not-enter-the-report" not in json.dumps(observed)


def test_chat_stream_contract_rejects_turn_contract_semantic_drift() -> None:
    with pytest.raises(AgentStreamContractError, match="TurnContract.intentFrame.primary"):
        validate_chat_stream_events(
            _valid_chat_stream_events(),
            status_code=200,
            headers={
                "content-type": "text/event-stream",
                "cache-control": "no-cache",
                "x-accel-buffering": "no",
            },
            trace_id="trace-contract",
            session_id="session-contract",
            fixture={
                "expected": {
                    "terminalEvent": "AgentCompleted",
                    "turnContract": {
                        "intentFrame": {"primary": "story_generation"}
                    },
                }
            },
        )


def test_chat_stream_contract_enforces_required_and_forbidden_events() -> None:
    events = _valid_chat_stream_events()
    story_events = [
        _event("StoryProviderAttempt"),
        _event("StoryCommitStarted"),
        _event("StoryCommitFinished"),
        _event("StoryDraftMeasured"),
        _event("StoryGenerationValidation"),
        _event("StoryCallAccounting"),
    ]
    text_index = next(
        index for index, (name, _) in enumerate(events) if name == "TextChunk"
    )
    events[text_index:text_index] = story_events
    expected_sequence = [name for name, _ in story_events]
    fixture = {
        "expected": {
            "terminalEvent": "AgentCompleted",
            "toolSequence": ["read_file"],
            "replyContains": ["STORYDEX_AGENT_STREAM_CONTRACT_FILE_91C7"],
            "requiredEventSequence": expected_sequence,
            "forbiddenEvents": ["StoryGenerationFailed"],
        }
    }
    headers = {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
        "x-accel-buffering": "no",
    }

    observed = validate_chat_stream_events(
        events,
        status_code=200,
        headers=headers,
        trace_id="trace-contract",
        session_id="session-contract",
        fixture=fixture,
    )
    assert observed["eventNames"].count("StoryCommitStarted") == 1

    reordered = list(events)
    started = next(
        index for index, (name, _) in enumerate(reordered) if name == "StoryCommitStarted"
    )
    finished = next(
        index for index, (name, _) in enumerate(reordered) if name == "StoryCommitFinished"
    )
    reordered[started], reordered[finished] = reordered[finished], reordered[started]
    with pytest.raises(AgentStreamContractError, match="required event sequence"):
        validate_chat_stream_events(
            reordered,
            status_code=200,
            headers=headers,
            trace_id="trace-contract",
            session_id="session-contract",
            fixture=fixture,
        )

    forbidden = list(events)
    forbidden.insert(-2, _event("StoryGenerationFailed"))
    with pytest.raises(AgentStreamContractError, match="forbidden event"):
        validate_chat_stream_events(
            forbidden,
            status_code=200,
            headers=headers,
            trace_id="trace-contract",
            session_id="session-contract",
            fixture=fixture,
        )


def test_chat_stream_contract_allows_explicit_mutating_fixture() -> None:
    events = _valid_chat_stream_events()
    for index, (name, payload) in enumerate(events):
        if name in {"ToolStart", "ToolDone"}:
            events[index] = (
                name,
                {
                    **payload,
                    "tool_name": "write_file",
                    "tool_call_id": "scoped-write-1",
                },
            )

    observed = validate_chat_stream_events(
        events,
        status_code=200,
        headers={
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
            "x-accel-buffering": "no",
        },
        trace_id="trace-contract",
        session_id="session-contract",
        fixture={
            "allowMutatingTools": True,
            "expected": {
                "terminalEvent": "AgentCompleted",
                "toolSequence": ["write_file"],
                "replyContains": ["STORYDEX_AGENT_STREAM_CONTRACT_FILE_91C7"],
            },
        },
    )

    assert observed["toolSequence"] == ["write_file"]


def test_chat_stream_contract_allows_declared_tool_error() -> None:
    events = _valid_chat_stream_events()
    for name, payload in events:
        if name == "ToolDone":
            payload["is_error"] = True

    observed = validate_chat_stream_events(
        events,
        status_code=200,
        headers={
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
            "x-accel-buffering": "no",
        },
        trace_id="trace-contract",
        session_id="session-contract",
        fixture={
            "expected": {
                "terminalEvent": "AgentCompleted",
                "toolSequence": ["read_file"],
                "toolErrorSequence": ["read_file"],
                "replyContains": ["STORYDEX_AGENT_STREAM_CONTRACT_FILE_91C7"],
            }
        },
    )

    assert observed["toolErrorSequence"] == ["read_file"]


def test_chat_stream_contract_allows_declared_interrupted_tool_on_cancel() -> None:
    events = [
        item
        for item in _valid_chat_stream_events()
        if item[0] not in {"ToolDone", "TextChunk", "AgentCompleted", "done"}
    ]
    events.extend(
        [
            _event("AgentCancelled", reason="timeout"),
            ("done", {"type": "done"}),
        ]
    )

    observed = validate_chat_stream_events(
        events,
        status_code=200,
        headers={
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
            "x-accel-buffering": "no",
        },
        trace_id="trace-contract",
        session_id="session-contract",
        fixture={
            "expected": {
                "terminalEvent": "AgentCancelled",
                "terminalReason": "timeout",
                "toolSequence": ["read_file"],
                "interruptedToolSequence": ["read_file"],
                "replyContains": [],
            }
        },
    )

    assert observed["interruptedToolSequence"] == ["read_file"]


def test_chat_stream_contract_rejects_mutating_tool_without_fixture_opt_in() -> None:
    events = _valid_chat_stream_events()
    for _, payload in events:
        if payload.get("tool_name") == "read_file":
            payload["tool_name"] = "write_file"

    with pytest.raises(AgentStreamContractError, match="mutating tool write_file"):
        validate_chat_stream_events(
            events,
            status_code=200,
            headers={
                "content-type": "text/event-stream",
                "cache-control": "no-cache",
                "x-accel-buffering": "no",
            },
            trace_id="trace-contract",
            session_id="session-contract",
        )


def test_chat_stream_contract_accepts_failure_only_after_finalization() -> None:
    root = Path(__file__).resolve().parents[3]
    fixture = load_fixture(
        root
        / "apps/backend/contracts/fixtures/agent-chat-stream-provider-error-v1/scenario.json"
    )
    events = _valid_chat_stream_events()[:4]
    events.extend(
        [
            _event("RuntimeMetrics", providerMode="replay"),
            _event("GitCommitPrompt", status="pending"),
            _event(
                "AgentError",
                error_type="storydex_coomi_bridge_error",
                message="provider replay step 1 did not find expected message marker",
            ),
            ("done", {"type": "done"}),
        ]
    )

    observed = validate_chat_stream_events(
        events,
        status_code=200,
        headers={
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
            "x-accel-buffering": "no",
        },
        trace_id="trace-contract",
        session_id="session-contract",
        fixture=fixture,
    )

    assert observed["terminalEvent"] == "AgentError"
    assert observed["eventNames"][-3:] == ["GitCommitPrompt", "AgentError", "done"]
    assert observed["errorCount"] == 1


def test_chat_stream_contract_rejects_duplicate_terminal_event() -> None:
    events = _valid_chat_stream_events()
    events.insert(-1, _event("AgentCancelled", reason="late-cancel"))

    with pytest.raises(AgentStreamContractError, match="exactly one semantic terminal"):
        validate_chat_stream_events(
            events,
            status_code=200,
            headers={
                "content-type": "text/event-stream",
                "cache-control": "no-cache",
                "x-accel-buffering": "no",
            },
            trace_id="trace-contract",
            session_id="session-contract",
        )


def test_chat_stream_contract_rejects_invalid_heartbeat_state() -> None:
    events = _valid_chat_stream_events()
    events.insert(
        2,
        _event(
            "TurnPhase",
            phase="context_assembly",
            status="success",
            heartbeat=True,
            elapsedMs=12,
        ),
    )

    with pytest.raises(AgentStreamContractError, match="heartbeat must have status=running"):
        validate_chat_stream_events(
            events,
            status_code=200,
            headers={
                "content-type": "text/event-stream",
                "cache-control": "no-cache",
                "x-accel-buffering": "no",
            },
            trace_id="trace-contract",
            session_id="session-contract",
        )


def test_chat_stream_parser_requires_complete_json_object_frames() -> None:
    with pytest.raises(AgentStreamContractError, match="payload must be an object"):
        parse_sse_events(["event: TextChunk", 'data: "not-an-object"', ""])


def test_chat_stream_request_contract_matches_fastapi_alias_schema() -> None:
    root = Path(__file__).resolve().parents[3]
    contract = json.loads(
        (root / "apps/backend/contracts/agent-chat-stream-v1.json").read_text(
            encoding="utf-8"
        )
    )
    route_schema = AgentChatRequest.model_json_schema(by_alias=True)

    assert contract["contractId"] == "agent.chat.stream.v1"
    assert set(contract["request"]["properties"]) == set(route_schema["properties"])
    assert contract["request"]["required"] == route_schema["required"]
    assert contract["transport"]["terminalEvents"] == [
        "AgentCompleted",
        "AgentError",
        "AgentCancelled",
    ]
    assert contract["transport"]["transportTerminalEvent"] == "done"
