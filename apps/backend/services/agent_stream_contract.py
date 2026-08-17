"""Black-box validation for the versioned Agent chat/SSE contract.

The validator deliberately works on parsed event frames rather than importing
the FastAPI route.  The same checks can therefore be run against Stable
Python, the Rust Refactor service, or a small test server without granting the
validator access to runtime internals.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


TERMINAL_EVENTS = frozenset({"AgentCompleted", "AgentError", "AgentCancelled"})
READ_ONLY_TOOLS = frozenset(
    {
        "ask_user",
        "ask_user_question",
        "get_loop",
        "glob",
        "grep",
        "grep_files",
        "list_dir",
        "list_skills",
        "memory_list",
        "memory_read",
        "memory_search",
        "read",
        "read_file",
        "read_skill",
        "request_user_input",
        "search",
        "storydexprojectsearch",
        "storydexruntimepresetstatus",
        "storydexversionstatus",
        "storydexwikiquery",
        "storydexwordcount",
        "todo",
        "todo_write",
        "todowrite",
        "update_plan",
        "view_image",
        "wait_agent",
        "web_fetch",
        "web_search",
        "webfetch",
        "websearch",
    }
)
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
    }
)


class AgentStreamContractError(ValueError):
    """A response violated the externally observable Agent stream contract."""


def load_fixture(path: str | Path) -> dict[str, Any]:
    fixture_path = Path(path)
    try:
        value = json.loads(fixture_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AgentStreamContractError(
            f"chat stream fixture is unavailable: {fixture_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise AgentStreamContractError(
            f"chat stream fixture is invalid JSON: {fixture_path}"
        ) from exc
    if not isinstance(value, dict):
        raise AgentStreamContractError("chat stream fixture must be a JSON object")
    if value.get("schemaVersion") != 1:
        raise AgentStreamContractError("chat stream fixture schemaVersion must be 1")
    if value.get("contractId") != "agent.chat.stream.v1":
        raise AgentStreamContractError("chat stream fixture contractId is not agent.chat.stream.v1")
    return value


def parse_sse_events(lines: Iterable[str]) -> list[tuple[str, dict[str, Any]]]:
    """Parse standard SSE frames while rejecting malformed JSON payloads."""

    event_name = ""
    data_lines: list[str] = []
    events: list[tuple[str, dict[str, Any]]] = []
    for raw_line in lines:
        line = str(raw_line)
        if line.startswith("event:"):
            if event_name:
                raise AgentStreamContractError("SSE event frame started before the previous frame ended")
            event_name = line[6:].strip()
            if not event_name:
                raise AgentStreamContractError("SSE event name must not be empty")
        elif line.startswith("data:"):
            if not event_name:
                raise AgentStreamContractError("SSE data appeared before an event name")
            data_lines.append(line[5:].lstrip())
        elif not line and event_name:
            raw_data = "\n".join(data_lines)
            try:
                payload = json.loads(raw_data) if raw_data else {}
            except json.JSONDecodeError as exc:
                raise AgentStreamContractError(
                    f"SSE event {event_name} has invalid JSON data"
                ) from exc
            if not isinstance(payload, dict):
                raise AgentStreamContractError(f"SSE event {event_name} payload must be an object")
            events.append((event_name, payload))
            event_name = ""
            data_lines = []
    if event_name:
        raise AgentStreamContractError(f"SSE event {event_name} was not terminated by a blank line")
    return events


def _header(headers: Mapping[str, Any], name: str) -> str:
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted:
            return str(value or "")
    return ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _event_trace(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None:
        value = payload.get({"traceId": "trace_id", "sessionId": "session_id"}.get(key, ""))
    return _text(value)


def _tool_name(payload: Mapping[str, Any]) -> str:
    return _text(payload.get("toolName") or payload.get("tool_name"))


def _tool_id(payload: Mapping[str, Any]) -> str:
    return _text(payload.get("toolCallId") or payload.get("tool_call_id"))


def _is_error(payload: Mapping[str, Any]) -> bool:
    return bool(payload.get("isError") or payload.get("is_error"))


def _safe_event_summary(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"event": name}
    fields = (
        "_version",
        "traceId",
        "sessionId",
        "phase",
        "status",
        "heartbeat",
        "label",
        "detail",
        "providerId",
        "llmProvider",
        "llmModel",
        "responseModel",
        "providerMode",
        "toolName",
        "tool_name",
        "toolCallId",
        "tool_call_id",
        "isError",
        "is_error",
        "code",
        "error_type",
        "attempt",
        "maxAttempts",
        "httpStatus",
        "round",
        "elapsedMs",
        "durationMs",
        "duration_ms",
        "reason",
    )
    for key in fields:
        if key in payload and payload[key] not in (None, ""):
            value = payload[key]
            summary[key] = str(value)[:240] if isinstance(value, str) else value
    if name == "TextChunk":
        summary["charCount"] = len(_text(payload.get("content")))
    if name == "AgentError":
        summary["hasMessage"] = bool(_text(payload.get("message")))
        details = payload.get("details") if isinstance(payload.get("details"), Mapping) else {}
        for key in ("stage", "providerHttpStatus", "statusCode", "httpStatus"):
            if key in details and details[key] not in (None, ""):
                summary[key] = details[key]
    if name == "done":
        summary["type"] = _text(payload.get("type"))
    return summary


def _assert_common_payload(name: str, payload: Mapping[str, Any]) -> None:
    if name == "done":
        return
    if _text(payload.get("_type")) != name:
        raise AgentStreamContractError(
            f"event {name} _type must equal its SSE event name"
        )
    version = payload.get("_version")
    if version != 1:
        raise AgentStreamContractError(f"event {name} _version must be 1")


def _first_index(names: Sequence[str], value: str) -> int | None:
    try:
        return names.index(value)
    except ValueError:
        return None


def validate_chat_stream_events(
    events: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    status_code: int,
    headers: Mapping[str, Any],
    trace_id: str,
    session_id: str,
    fixture: Mapping[str, Any] | None = None,
    expected_provider: str = "",
    expected_model: str = "",
    require_turn_contract: bool = True,
    allow_client_disconnect: bool | None = None,
) -> dict[str, Any]:
    """Validate one trace and return a redacted, machine-readable observation."""

    if status_code != 200:
        raise AgentStreamContractError(f"chat stream returned HTTP {status_code}")
    content_type = _header(headers, "content-type").casefold()
    if not content_type.startswith("text/event-stream"):
        raise AgentStreamContractError("chat stream content-type must start with text/event-stream")
    for name, expected in (
        ("cache-control", "no-cache"),
        ("x-accel-buffering", "no"),
    ):
        if _header(headers, name).casefold() != expected:
            raise AgentStreamContractError(f"chat stream header {name} must be {expected!r}")
    if not events:
        raise AgentStreamContractError("chat stream returned no SSE events")

    expected = fixture.get("expected") if isinstance(fixture, Mapping) else {}
    expected = expected if isinstance(expected, Mapping) else {}
    allow_mutating_tools = bool(
        fixture.get("allowMutatingTools") if isinstance(fixture, Mapping) else False
    )
    client_disconnected = (
        bool(expected.get("clientDisconnected"))
        if allow_client_disconnect is None
        else bool(allow_client_disconnect)
    )
    names = [str(name) for name, _ in events]
    if names[0] != "RunAccepted":
        raise AgentStreamContractError("RunAccepted must be the first SSE event")
    done_count = names.count("done")
    if client_disconnected:
        if done_count or any(name in TERMINAL_EVENTS for name in names):
            raise AgentStreamContractError(
                "client-disconnect stream must close before a terminal or done event"
            )
    elif done_count != 1 or names[-1] != "done":
        raise AgentStreamContractError("chat stream must end with exactly one done event")
    terminals = [index for index, name in enumerate(names) if name in TERMINAL_EVENTS]
    if client_disconnected and terminals:
        raise AgentStreamContractError(
            "client-disconnect stream must not expose a semantic terminal"
        )
    if not client_disconnected and len(terminals) != 1:
        raise AgentStreamContractError(
            f"chat stream must contain exactly one semantic terminal event; got {len(terminals)}"
        )
    terminal_index = terminals[0] if terminals else None
    if terminal_index is not None and terminal_index != len(names) - 2:
        raise AgentStreamContractError(
            "no SSE event may appear after the semantic terminal before done; "
            f"tail={names[terminal_index:]}"
        )
    expected_terminal = _text(expected.get("terminalEvent"))
    if expected_terminal and terminal_index is not None and names[terminal_index] != expected_terminal:
        raise AgentStreamContractError(
            f"terminal event {names[terminal_index]!r} does not match {expected_terminal!r}"
        )
    if terminal_index is not None and names[terminal_index] == "AgentCancelled" and expected_terminal != "AgentCancelled":
        raise AgentStreamContractError("read-only success fixture ended with AgentCancelled")
    terminal_reason = _text(events[terminal_index][1].get("reason")) if terminal_index is not None else ""
    expected_reason = _text(expected.get("terminalReason"))
    if expected_reason and terminal_reason != expected_reason:
        raise AgentStreamContractError(
            f"terminal reason {terminal_reason!r} does not match {expected_reason!r}"
        )

    for name, payload in events:
        if not isinstance(payload, Mapping):
            raise AgentStreamContractError(f"event {name} payload must be an object")
        _assert_common_payload(name, payload)

    accepted = events[0][1]
    accepted_trace = _event_trace(accepted, "traceId")
    accepted_session = _event_trace(accepted, "sessionId")
    if not accepted_trace or accepted_trace != trace_id:
        raise AgentStreamContractError("RunAccepted traceId does not match the request")
    if not accepted_session or accepted_session != session_id:
        raise AgentStreamContractError("RunAccepted sessionId does not match the request")
    for name, payload in events:
        event_trace = _event_trace(payload, "traceId")
        event_session = _event_trace(payload, "sessionId")
        if event_trace and event_trace != accepted_trace:
            raise AgentStreamContractError(f"event {name} changed traceId mid-stream")
        if event_session and event_session != accepted_session:
            raise AgentStreamContractError(f"event {name} changed sessionId mid-stream")

    turn_contract_index = _first_index(names, "TurnContract")
    if require_turn_contract and turn_contract_index is None:
        raise AgentStreamContractError("chat stream did not emit TurnContract")
    if turn_contract_index is not None and turn_contract_index <= 0:
        raise AgentStreamContractError("TurnContract must follow RunAccepted")
    agent_started_index = _first_index(names, "AgentStarted")
    expected_agent_started = expected.get("agentStarted", True)
    if fixture is not None and expected_agent_started is not False and agent_started_index is None:
        raise AgentStreamContractError("replay fixture stream did not emit AgentStarted")
    if expected_agent_started is False and agent_started_index is not None:
        raise AgentStreamContractError(
            "pre-runtime failure fixture emitted AgentStarted"
        )
    if (
        turn_contract_index is not None
        and agent_started_index is not None
        and agent_started_index <= turn_contract_index
    ):
        raise AgentStreamContractError("AgentStarted must follow TurnContract")

    phase_first_seen: dict[str, int] = {}
    heartbeat_count = 0
    for index, (name, payload) in enumerate(events):
        if name != "TurnPhase":
            continue
        phase = _text(payload.get("phase"))
        if phase:
            phase_first_seen.setdefault(phase, index)
        if payload.get("heartbeat") is True:
            heartbeat_count += 1
            if _text(payload.get("status")) != "running":
                raise AgentStreamContractError("TurnPhase heartbeat must have status=running")
            elapsed = payload.get("elapsedMs")
            if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or elapsed < 0:
                raise AgentStreamContractError("TurnPhase heartbeat elapsedMs must be non-negative")

    tool_starts: list[tuple[str, str, int]] = []
    tool_dones: list[tuple[str, str, int, bool]] = []
    for index, (name, payload) in enumerate(events):
        if name in {"ToolStart", "ToolStarted"}:
            tool_starts.append((_tool_name(payload), _tool_id(payload), index))
        elif name == "ToolDone":
            tool_dones.append((_tool_name(payload), _tool_id(payload), index, _is_error(payload)))
    expected_error_tools = [str(value) for value in expected.get("toolErrorSequence") or []]
    expected_interrupted_tools = [
        str(value) for value in expected.get("interruptedToolSequence") or []
    ]
    interrupted_tools: list[str] = []
    for tool_name, tool_id, start_index in tool_starts:
        match = next(
            (
                item
                for item in tool_dones
                if item[0] == tool_name and item[1] == tool_id and item[2] > start_index
            ),
            None,
        )
        if match is None:
            if (
                terminal_index is not None
                and names[terminal_index] == "AgentCancelled"
                and tool_name in expected_interrupted_tools
            ):
                interrupted_tools.append(tool_name)
                if not allow_mutating_tools and tool_name.casefold() not in READ_ONLY_TOOLS:
                    raise AgentStreamContractError(
                        f"read-only fixture emitted mutating tool {tool_name}"
                    )
                continue
            raise AgentStreamContractError(
                f"ToolStart {tool_name}/{tool_id} has no later matching ToolDone"
            )
        if match[3] and tool_name not in expected_error_tools:
            raise AgentStreamContractError(f"tool {tool_name} returned an error")
        if not allow_mutating_tools and tool_name.casefold() not in READ_ONLY_TOOLS:
            raise AgentStreamContractError(f"read-only fixture emitted mutating tool {tool_name}")

    tool_sequence = [item[0] for item in tool_starts]
    tool_error_sequence = [item[0] for item in tool_dones if item[3]]
    if "toolErrorSequence" in expected and tool_error_sequence != expected_error_tools:
        raise AgentStreamContractError(
            f"tool error sequence {tool_error_sequence!r} does not match fixture "
            f"{expected_error_tools!r}"
        )
    if (
        "interruptedToolSequence" in expected
        and interrupted_tools != expected_interrupted_tools
    ):
        raise AgentStreamContractError(
            f"interrupted tool sequence {interrupted_tools!r} does not match fixture "
            f"{expected_interrupted_tools!r}"
        )
    errors = [
        _text(payload.get("message") or payload.get("error_type"))
        for name, payload in events
        if name == "AgentError"
    ]
    if errors and expected_terminal != "AgentError":
        raise AgentStreamContractError(f"stream emitted AgentError: {errors[0]}")
    if expected_terminal == "AgentError" and not errors:
        raise AgentStreamContractError("failure fixture did not emit AgentError")
    for marker in expected.get("errorContains") or []:
        if str(marker).casefold() not in "\n".join(errors).casefold():
            raise AgentStreamContractError(f"AgentError is missing fixture marker {marker!r}")
    expected_tools = [str(value) for value in expected.get("toolSequence") or []]
    if "toolSequence" in expected and tool_sequence != expected_tools:
        raise AgentStreamContractError(
            f"tool sequence {tool_sequence!r} does not match fixture {expected_tools!r}; "
            f"events={names!r}"
        )

    reply = "".join(
        _text(payload.get("content"))
        for name, payload in events
        if name == "TextChunk"
    )
    for marker in expected.get("replyContains") or []:
        if str(marker) not in reply:
            raise AgentStreamContractError(f"reply is missing fixture marker {marker!r}")

    provider_ids: set[str] = set()
    models: set[str] = set()
    provider_modes: set[str] = set()
    for name, payload in events:
        for key in ("providerId", "llmProvider", "provider"):
            value = _text(payload.get(key))
            if value:
                provider_ids.add(value)
        for key in ("model", "llmModel", "responseModel"):
            value = _text(payload.get(key))
            if value:
                models.add(value)
        mode = _text(payload.get("providerMode")).casefold()
        if mode:
            provider_modes.add(mode)
        status = payload.get("coomiStatus") if isinstance(payload.get("coomiStatus"), Mapping) else {}
        provider = _text(status.get("providerId"))
        model = _text(status.get("model"))
        if provider:
            provider_ids.add(provider)
        if model:
            models.add(model)
    expected_provider = _text(expected_provider or expected.get("providerId"))
    expected_model = _text(expected_model or expected.get("model"))
    if expected_provider and provider_ids and provider_ids != {expected_provider}:
        raise AgentStreamContractError(
            f"stream used unexpected provider(s): {sorted(provider_ids)!r}"
        )
    if expected_model and models and {item.casefold() for item in models} != {expected_model.casefold()}:
        raise AgentStreamContractError(f"stream used unexpected model(s): {sorted(models)!r}")
    expected_mode = _text(expected.get("providerMode")).casefold()
    if expected_mode and provider_modes != {expected_mode}:
        raise AgentStreamContractError(
            f"stream providerMode {sorted(provider_modes)!r} does not match {expected_mode!r}"
        )

    safe_events = [_safe_event_summary(name, payload) for name, payload in events]
    return {
        "schemaVersion": 1,
        "contractId": "agent.chat.stream.v1",
        "status": "passed",
        "httpStatus": status_code,
        "traceId": accepted_trace,
        "sessionId": accepted_session,
        "eventCount": len(events),
        "eventNames": names,
        "terminalEvent": names[terminal_index] if terminal_index is not None else "",
        "terminalReason": terminal_reason,
        "doneCount": done_count,
        "clientDisconnected": client_disconnected,
        "phaseFirstSeen": phase_first_seen,
        "heartbeatCount": heartbeat_count,
        "toolSequence": tool_sequence,
        "toolErrorSequence": tool_error_sequence,
        "interruptedToolSequence": interrupted_tools,
        "providerIds": sorted(provider_ids),
        "models": sorted(models),
        "providerModes": sorted(provider_modes),
        "errorCount": len(errors),
        "replyChars": len(reply),
        "replyPreview": reply[-400:],
        "events": safe_events,
    }
