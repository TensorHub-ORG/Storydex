"""Run one provider-network-free Agent runtime contract against any HTTP base URL."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.agent_stream_contract import (  # noqa: E402
    AgentStreamContractError,
    load_fixture,
    parse_sse_events,
    validate_chat_stream_events,
)


HEALTH_CONTRACT_ID = "agent.sys.health.v1"
COOMI_STATUS_CONTRACT_ID = "agent.coomi.status.v1"
CHAT_STREAM_CONTRACT_ID = "agent.chat.stream.v1"
DEFAULT_CHAT_STREAM_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "fixtures"
    / "agent-chat-stream-read-only-v1"
    / "scenario.json"
)
CONTRACT_IDS = {
    "health": HEALTH_CONTRACT_ID,
    "coomi-status": COOMI_STATUS_CONTRACT_ID,
    "chat-stream": CHAT_STREAM_CONTRACT_ID,
}


class ContractError(RuntimeError):
    pass


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{label} must be a JSON object")
    return value


def validate_health_response(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != 200:
        raise ContractError(f"health returned HTTP {response.status_code}")
    try:
        payload = _require_mapping(response.json(), "health response")
    except ValueError as exc:
        raise ContractError("health response is not valid JSON") from exc
    required_envelope = {"ok", "data", "error", "trace", "audit"}
    missing = sorted(required_envelope.difference(payload))
    if missing:
        raise ContractError(f"health envelope is missing fields: {missing}")
    if payload.get("ok") is not True or payload.get("error") is not None:
        raise ContractError("health envelope must be a successful response")
    data = _require_mapping(payload.get("data"), "health data")
    if data.get("status") != "ok":
        raise ContractError("health data.status must be 'ok'")
    if data.get("service") != "Storydex Backend":
        raise ContractError("health data.service does not match the Stable contract")
    if not isinstance(data.get("time"), str) or not str(data.get("time") or "").strip():
        raise ContractError("health data.time must be a non-empty string")
    trace = _require_mapping(payload.get("trace"), "health trace")
    if not isinstance(trace.get("traceId"), str) or not str(trace.get("traceId") or "").strip():
        raise ContractError("health trace.traceId must be a non-empty string")
    duration = trace.get("durationMs")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
        raise ContractError("health trace.durationMs must be non-negative")
    if not isinstance(payload.get("audit"), list):
        raise ContractError("health audit must be an array")
    return {
        "httpStatus": response.status_code,
        "envelopeFields": sorted(str(key) for key in payload),
        "dataStatus": data["status"],
        "service": data["service"],
        "hasTime": True,
        "hasTraceId": True,
        "traceDurationNonNegative": True,
        "auditCount": len(payload["audit"]),
    }


def _sensitive_field_paths(value: Any, *, path: str = "") -> list[str]:
    sensitive = {
        "api_key",
        "apikey",
        "authorization",
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "client_secret",
        "password",
    }
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            if key_text.lower() in sensitive:
                found.append(child_path)
            found.extend(_sensitive_field_paths(item, path=child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_sensitive_field_paths(item, path=f"{path}[{index}]"))
    return found


def validate_coomi_status_response(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != 200:
        raise ContractError(f"Coomi status returned HTTP {response.status_code}")
    try:
        payload = _require_mapping(response.json(), "Coomi status response")
    except ValueError as exc:
        raise ContractError("Coomi status response is not valid JSON") from exc
    required_envelope = {"ok", "data", "error", "trace", "audit"}
    missing = sorted(required_envelope.difference(payload))
    if missing:
        raise ContractError(f"Coomi status envelope is missing fields: {missing}")
    if payload.get("ok") is not True or payload.get("error") is not None:
        raise ContractError("Coomi status envelope must be a successful response")
    sensitive_paths = _sensitive_field_paths(payload)
    if sensitive_paths:
        raise ContractError(
            f"Coomi status response exposes sensitive fields: {sensitive_paths}"
        )
    data = _require_mapping(payload.get("data"), "Coomi status data")
    expected_strings = ("runtime", "providerId", "providerType", "model", "display")
    for field in expected_strings:
        if not isinstance(data.get(field), str) or not str(data.get(field) or "").strip():
            raise ContractError(f"Coomi status data.{field} must be a non-empty string")
    if data.get("runtime") != "storydex-coomi-rs":
        raise ContractError("Coomi status data.runtime does not match the Rust runtime")
    if data.get("installed") is not True:
        raise ContractError("Coomi status data.installed must be true")
    models = data.get("models")
    if not isinstance(models, list):
        raise ContractError("Coomi status data.models must be an array")
    active_listed = any(
        isinstance(item, Mapping)
        and item.get("providerId") == data.get("providerId")
        and item.get("model") == data.get("model")
        for item in models
    )
    if not active_listed:
        raise ContractError("Coomi status models do not contain the active provider/model")
    capabilities = _require_mapping(
        data.get("providerCapabilities"), "Coomi status providerCapabilities"
    )
    reasoning = _require_mapping(
        data.get("reasoningCapability"), "Coomi status reasoningCapability"
    )
    reasoning_plan = _require_mapping(
        data.get("reasoningRequestPlan"), "Coomi status reasoningRequestPlan"
    )
    capability_fields = {
        "context_window",
        "effective_context_window_percent",
        "max_output_tokens",
        "supports_native_tools",
    }
    missing_capabilities = sorted(capability_fields.difference(capabilities))
    if missing_capabilities:
        raise ContractError(
            f"Coomi status providerCapabilities is missing fields: {missing_capabilities}"
        )
    reasoning_fields = {
        "support",
        "levels",
        "source",
        "promptFallback",
        "routeSensitive",
        "fallbackReason",
    }
    missing_reasoning = sorted(reasoning_fields.difference(reasoning))
    if missing_reasoning:
        raise ContractError(
            f"Coomi status reasoningCapability is missing fields: {missing_reasoning}"
        )
    reasoning_plan_fields = {
        "requested",
        "control",
        "sent",
        "promptApplied",
        "wireFields",
        "support",
        "source",
        "routeSensitive",
        "fallbackReason",
    }
    missing_reasoning_plan = sorted(reasoning_plan_fields.difference(reasoning_plan))
    if missing_reasoning_plan:
        raise ContractError(
            f"Coomi status reasoningRequestPlan is missing fields: {missing_reasoning_plan}"
        )
    trace = _require_mapping(payload.get("trace"), "Coomi status trace")
    if not isinstance(trace.get("traceId"), str) or not str(trace.get("traceId") or "").strip():
        raise ContractError("Coomi status trace.traceId must be a non-empty string")
    duration = trace.get("durationMs")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
        raise ContractError("Coomi status trace.durationMs must be non-negative")
    audit = payload.get("audit")
    if not isinstance(audit, list):
        raise ContractError("Coomi status audit must be an array")
    if not any(
        isinstance(item, Mapping) and item.get("action") == "read_coomi_status"
        for item in audit
    ):
        raise ContractError("Coomi status audit is missing read_coomi_status")
    return {
        "httpStatus": response.status_code,
        "envelopeFields": sorted(str(key) for key in payload),
        "runtime": data["runtime"],
        "installed": data["installed"],
        "providerId": data["providerId"],
        "providerType": data["providerType"],
        "model": data["model"],
        "display": data["display"],
        "activeModelListed": True,
        "modelCount": len(models),
        "capabilityFields": sorted(str(key) for key in capabilities),
        "reasoningCapabilityFields": sorted(str(key) for key in reasoning),
        "reasoningPlanFields": sorted(str(key) for key in reasoning_plan),
        "hasTraceId": True,
        "traceDurationNonNegative": True,
        "auditCount": len(audit),
        "sensitiveFieldCount": 0,
    }


def safe_origin(base_url: str) -> str:
    parsed = urlsplit(base_url)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{host}{port}"


def _interaction_event_matches(
    *,
    event_name: str,
    event_payload: Mapping[str, Any],
    after_event: str,
    after_event_fields: Mapping[str, Any],
) -> bool:
    return event_name == after_event and all(
        event_payload.get(field) == expected
        for field, expected in after_event_fields.items()
    )


def _safe_interaction_tail(
    events: list[tuple[str, Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    tail: list[dict[str, Any]] = []
    for event_name, payload in events[-12:]:
        summary: dict[str, Any] = {"event": event_name}
        for field in ("code", "error_type", "phase", "status", "reason"):
            value = payload.get(field)
            if value not in (None, ""):
                summary[field] = str(value)[:160]
        details = payload.get("details")
        if isinstance(details, Mapping):
            for field in ("stage", "providerHttpStatus", "statusCode", "httpStatus"):
                value = details.get(field)
                if value not in (None, ""):
                    summary[field] = value
        tail.append(summary)
    return tail


def _read_stream_with_interaction(
    *,
    client: httpx.Client,
    response: httpx.Response,
    base_url: str,
    token: str,
    fixture: Mapping[str, Any],
    trace_id: str,
    session_id: str,
    workspace_root: str,
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    interaction = fixture.get("interaction")
    interaction = interaction if isinstance(interaction, Mapping) else {}
    action = str(interaction.get("action") or "").strip()
    after_event = str(interaction.get("afterEvent") or "").strip()
    raw_after_event_fields = interaction.get("afterEventFields")
    if raw_after_event_fields is None:
        after_event_fields: dict[str, Any] = {}
    elif isinstance(raw_after_event_fields, Mapping):
        after_event_fields = {
            str(field): expected for field, expected in raw_after_event_fields.items()
        }
        if any(not field.strip() for field in after_event_fields):
            raise ContractError(
                "chat stream interaction.afterEventFields keys must not be empty"
            )
    else:
        raise ContractError(
            "chat stream interaction.afterEventFields must be an object"
        )
    if action and action not in {
        "stop",
        "steer",
        "disconnect",
        "approval",
        "approval_timeout",
    }:
        raise ContractError(f"unsupported chat stream interaction: {action}")
    if action and not after_event:
        raise ContractError("chat stream interaction.afterEvent is required")
    trigger_observation: dict[str, Any] = {"afterEvent": after_event}
    if after_event_fields:
        trigger_observation["afterEventFields"] = dict(after_event_fields)

    lines: list[str] = []
    current_event = ""
    current_data_lines: list[str] = []
    triggered = False
    observation: dict[str, Any] = {}
    late_approval_id = ""
    late_approval_kind = ""
    for raw_line in response.iter_lines():
        line = str(raw_line)
        lines.append(line)
        if line.startswith("event:"):
            current_event = line[6:].strip()
        elif line.startswith("data:"):
            current_data_lines.append(line[5:].lstrip())
        if line or not current_event:
            continue
        completed_event = current_event
        completed_payload: Mapping[str, Any] = {}
        if current_data_lines:
            try:
                decoded_payload = json.loads("\n".join(current_data_lines))
            except json.JSONDecodeError as exc:
                raise ContractError(
                    f"SSE event {completed_event} has invalid JSON data"
                ) from exc
            completed_payload = _require_mapping(decoded_payload, "SSE event payload")
        current_event = ""
        current_data_lines = []
        if (
            not action
            or triggered
            or not _interaction_event_matches(
                event_name=completed_event,
                event_payload=completed_payload,
                after_event=after_event,
                after_event_fields=after_event_fields,
            )
        ):
            continue
        triggered = True
        stop_payload = {
            "sessionId": session_id,
            "expectedTraceId": trace_id,
            "workspaceRoot": workspace_root,
        }
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if action == "stop":
            try:
                repeat = max(1, min(3, int(interaction.get("repeat") or 1)))
            except (TypeError, ValueError) as exc:
                raise ContractError("stop interaction.repeat must be an integer") from exc
            stop_attempts: list[tuple[httpx.Response, Mapping[str, Any]]] = []
            for _ in range(repeat):
                control_response = client.post(
                    base_url.rstrip("/") + "/api/v1/agent/executions/stop",
                    headers=headers,
                    json=stop_payload,
                    timeout=30.0,
                )
                try:
                    control_payload = _require_mapping(
                        control_response.json(), "execution stop response"
                    )
                except ValueError as exc:
                    raise ContractError(
                        "execution stop response is not valid JSON"
                    ) from exc
                control_data = _require_mapping(
                    control_payload.get("data"), "execution stop data"
                )
                if (
                    control_response.status_code != 200
                    or control_payload.get("ok") is not True
                ):
                    raise ContractError(
                        "execution stop was rejected: "
                        f"HTTP {control_response.status_code} {str(control_payload.get('error') or '')[:500]}"
                    )
                stop_attempts.append((control_response, control_data))
            control_response, control_data = stop_attempts[0]
            accepted_attempts = [data.get("accepted") is True for _, data in stop_attempts]
            if not accepted_attempts[0]:
                raise ContractError("execution stop did not accept the active trace")
            if str(control_data.get("activeTraceId") or "") != trace_id:
                raise ContractError("execution stop changed the active trace id")
            if any(accepted_attempts[1:]):
                raise ContractError("repeated execution stop was accepted more than once")
            observation = {
                "action": action,
                **trigger_observation,
                "httpStatus": control_response.status_code,
                "accepted": True,
                "activeTraceMatches": True,
                "mailboxPaused": bool(control_data.get("mailboxPaused")),
                "pauseReason": str(control_data.get("pauseReason") or ""),
            }
            if repeat > 1:
                observation["attemptAccepted"] = accepted_attempts
        elif action == "steer":
            message_id = str(interaction.get("messageId") or "").strip()
            content = str(interaction.get("content") or "").strip()
            if not message_id or not content:
                raise ContractError(
                    "steer interaction requires messageId and content"
                )
            control_response = client.post(
                base_url.rstrip("/") + "/api/v1/agent/followups",
                headers=headers,
                json={
                    "messageId": message_id,
                    "sessionId": session_id,
                    "expectedTraceId": trace_id,
                    "workspaceRoot": workspace_root,
                    "content": content,
                    "mode": "steer",
                },
                timeout=30.0,
            )
            try:
                control_payload = _require_mapping(
                    control_response.json(), "steer response"
                )
            except ValueError as exc:
                raise ContractError("steer response is not valid JSON") from exc
            control_data = _require_mapping(
                control_payload.get("data"), "steer response data"
            )
            message = _require_mapping(
                control_data.get("message"), "steer response message"
            )
            if control_response.status_code != 200 or control_payload.get("ok") is not True:
                raise ContractError(
                    "steer was rejected: "
                    f"HTTP {control_response.status_code} {str(control_payload.get('error') or '')[:500]}"
                )
            if control_data.get("steerRequested") is not True:
                raise ContractError("steer did not interrupt the active trace")
            if str(message.get("activeTraceId") or "") != trace_id:
                raise ContractError("steer changed the active trace id")
            observation = {
                "action": action,
                **trigger_observation,
                "httpStatus": control_response.status_code,
                "accepted": True,
                "activeTraceMatches": True,
                "messageId": str(message.get("messageId") or ""),
                "mode": str(message.get("mode") or ""),
                "status": str(message.get("status") or ""),
            }
        elif action == "disconnect":
            interaction_payload = interaction.get("settleMs", 1000)
            try:
                settle_ms = max(0, min(10000, int(interaction_payload)))
            except (TypeError, ValueError) as exc:
                raise ContractError("disconnect interaction.settleMs must be an integer") from exc
            # Closing the response is the client action. Use a separate HTTP
            # connection for the evidence probe because the SSE connection is
            # intentionally no longer available after this point.
            response.close()
            if settle_ms:
                time.sleep(settle_ms / 1000.0)
            settled_response: httpx.Response | None = None
            settled_payload: Mapping[str, Any] | None = None
            settled_data: Mapping[str, Any] | None = None
            for attempt in range(10):
                with httpx.Client(timeout=30.0, trust_env=False) as probe_client:
                    settled_response = probe_client.post(
                        base_url.rstrip("/") + "/api/v1/agent/executions/stop",
                        headers=headers,
                        json=stop_payload,
                    )
                try:
                    candidate_payload = _require_mapping(
                        settled_response.json(), "execution stop response"
                    )
                    candidate_data = _require_mapping(
                        candidate_payload.get("data"), "execution stop data"
                    )
                except ValueError as exc:
                    raise ContractError("execution stop response is not valid JSON") from exc
                if (
                    settled_response.status_code != 200
                    or candidate_payload.get("ok") is not True
                ):
                    raise ContractError(
                        "disconnect stop probe was rejected: "
                        f"HTTP {settled_response.status_code} {str(candidate_payload.get('error') or '')[:500]}"
                    )
                settled_payload = candidate_payload
                settled_data = candidate_data
                if (
                    candidate_data.get("accepted") is False
                    and str(candidate_data.get("reason") or "") == "no_active_execution"
                ):
                    break
                if attempt < 9:
                    time.sleep(0.1)
            if settled_response is None or settled_payload is None or settled_data is None:
                raise ContractError("disconnect stop probe returned no response")
            if not (
                settled_data.get("accepted") is False
                and str(settled_data.get("reason") or "") == "no_active_execution"
            ):
                raise ContractError(
                    "client disconnect did not settle the active execution: "
                    f"{dict(settled_data)}"
                )
            observation = {
                "action": action,
                **trigger_observation,
                "httpStatus": settled_response.status_code,
                "accepted": False,
                "activeTraceMatches": False,
                "reason": "no_active_execution",
                "clientClosed": True,
                "settleMs": settle_ms,
            }
            break
        else:
            approval_id = str(
                completed_payload.get("approvalId")
                or completed_payload.get("approval_id")
                or completed_payload.get("requestId")
                or ""
            ).strip()
            if not approval_id:
                raise ContractError(
                    f"approval interaction event {after_event!r} has no approvalId"
                )
            if action == "approval_timeout":
                late_approval_id = approval_id
                late_approval_kind = str(completed_payload.get("kind") or "")
                continue
            decision = str(interaction.get("decision") or "allow").strip()
            response_value = interaction.get("response")
            response_value = response_value if isinstance(response_value, Mapping) else {}
            try:
                repeat = max(
                    1,
                    min(3, int(interaction.get("repeatDecision") or 1)),
                )
            except (TypeError, ValueError) as exc:
                raise ContractError(
                    "approval interaction.repeatDecision must be an integer"
                ) from exc
            approval_attempts: list[tuple[httpx.Response, Mapping[str, Any]]] = []
            for _ in range(repeat):
                approval_response = client.post(
                    base_url.rstrip("/") + "/api/v1/agent/coomi/approval",
                    headers=headers,
                    json={
                        "approvalId": approval_id,
                        "decision": decision,
                        "response": dict(response_value),
                        "sessionId": session_id,
                        "expectedTraceId": trace_id,
                        "workspaceRoot": workspace_root,
                    },
                    timeout=30.0,
                )
                try:
                    approval_payload = _require_mapping(
                        approval_response.json(), "approval response"
                    )
                except ValueError as exc:
                    raise ContractError("approval response is not valid JSON") from exc
                approval_data = _require_mapping(
                    approval_payload.get("data"), "approval response data"
                )
                if (
                    approval_response.status_code != 200
                    or approval_payload.get("ok") is not True
                ):
                    raise ContractError(
                        "approval was rejected: "
                        f"HTTP {approval_response.status_code} {str(approval_payload.get('error') or '')[:500]}"
                    )
                approval_attempts.append((approval_response, approval_data))
            approval_response, _ = approval_attempts[0]
            accepted_attempts = [
                data.get("accepted") is True or data.get("resolved") is True
                for _, data in approval_attempts
            ]
            if not accepted_attempts[0]:
                raise ContractError("approval response did not accept the pending request")
            if any(accepted_attempts[1:]):
                raise ContractError("repeated approval decision was accepted more than once")
            observation = {
                "action": action,
                **trigger_observation,
                "httpStatus": approval_response.status_code,
                "accepted": True,
                "approvalIdPresent": True,
                "decision": decision,
                "kind": str(completed_payload.get("kind") or ""),
            }
            if repeat > 1:
                observation["attemptAccepted"] = accepted_attempts
    if action == "approval_timeout" and triggered:
        decision = str(interaction.get("lateDecision") or "deny").strip()
        approval_response = client.post(
            base_url.rstrip("/") + "/api/v1/agent/coomi/approval",
            headers={"Authorization": f"Bearer {token}"} if token else {},
            json={
                "approvalId": late_approval_id,
                "decision": decision,
                "response": {},
                "sessionId": session_id,
                "expectedTraceId": trace_id,
                "workspaceRoot": workspace_root,
            },
            timeout=30.0,
        )
        try:
            approval_payload = _require_mapping(
                approval_response.json(), "late approval response"
            )
        except ValueError as exc:
            raise ContractError("late approval response is not valid JSON") from exc
        approval_data = _require_mapping(
            approval_payload.get("data"), "late approval response data"
        )
        accepted = (
            approval_data.get("accepted") is True
            or approval_data.get("resolved") is True
        )
        if approval_response.status_code != 200 or approval_payload.get("ok") is not True:
            raise ContractError(
                "late approval probe was rejected: "
                f"HTTP {approval_response.status_code} {str(approval_payload.get('error') or '')[:500]}"
            )
        if accepted:
            raise ContractError("late approval was accepted after execution timeout")
        observation = {
            "action": action,
            **trigger_observation,
            "httpStatus": approval_response.status_code,
            "accepted": False,
            "approvalIdPresent": bool(late_approval_id),
            "decision": decision,
            "kind": late_approval_kind,
        }
    parsed_events = parse_sse_events(lines)
    if action and not triggered:
        raise ContractError(
            f"chat stream interaction did not observe trigger event {after_event!r}; "
            f"tail={_safe_interaction_tail(parsed_events)!r}"
        )
    return parsed_events, observation


def _replacement_session_directory(workspace: str, session_id: str) -> Path:
    root = Path(workspace).resolve() / ".storydex" / ".agent" / "sessions"
    normalized = str(session_id or "default").strip() or "default"
    portable = normalized.replace("\\", "/")
    unsafe = (
        normalized in {".", ".."}
        or portable.startswith("/")
        or portable.startswith("//")
        or (len(portable) >= 2 and portable[0].isalpha() and portable[1] == ":")
        or any(part == ".." for part in portable.split("/"))
    )
    directory = (
        f"_session_{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]}"
        if unsafe
        else normalized
    )
    return root / directory


def _replacement_trace_summary(directory: Path, trace_id: str) -> dict[str, Any]:
    normalized = str(trace_id or "").strip()
    empty = {
        "exists": False,
        "traceId": normalized,
        "status": "",
        "superseded": False,
        "replacementStatus": "",
        "replacementTraceId": "",
        "prompt": "",
    }
    if not normalized or not directory.is_dir():
        return empty
    candidates: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(value, Mapping) or str(value.get("traceId") or "") != normalized:
            continue
        candidates.append(dict(value))
    if not candidates:
        return empty
    candidates.sort(
        key=lambda item: (
            str(item.get("updatedAt") or ""),
            str(item.get("createdAt") or ""),
        )
    )
    value = candidates[-1]
    replacement = (
        value.get("replacement")
        if isinstance(value.get("replacement"), Mapping)
        else {}
    )
    return {
        "exists": True,
        "traceId": normalized,
        "status": str(value.get("status") or ""),
        "superseded": bool(value.get("superseded")),
        "replacementStatus": str(replacement.get("status") or ""),
        "replacementTraceId": str(
            replacement.get("replacementTraceId")
            or value.get("supersededByTraceId")
            or ""
        ),
        "prompt": str(value.get("prompt") or ""),
    }


def _replacement_runtime_snapshot(workspace: str, session_id: str) -> dict[str, Any]:
    root = Path(workspace).resolve()
    normalized = str(session_id or "default").strip() or "default"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    binding_path = (
        root
        / ".storydex"
        / ".agent"
        / "runtime"
        / "coomi-sessions"
        / f"{digest}.json"
    )

    def file_snapshot(path: Path | None) -> dict[str, Any]:
        if path is None or not path.is_file():
            return {"exists": False, "size": 0, "sha256": ""}
        content = path.read_bytes()
        return {
            "exists": True,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    binding: Mapping[str, Any] = {}
    if binding_path.is_file():
        try:
            value = json.loads(binding_path.read_text(encoding="utf-8"))
            binding = value if isinstance(value, Mapping) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            binding = {}
    raw_session_path = str(
        binding.get("sessionPath") or binding.get("historyPath") or ""
    ).strip()
    session_path = Path(raw_session_path).resolve() if raw_session_path else None
    session_value: Mapping[str, Any] = {}
    if session_path is not None and session_path.is_file():
        try:
            value = json.loads(session_path.read_text(encoding="utf-8"))
            session_value = value if isinstance(value, Mapping) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            session_value = {}
    messages = (
        session_value.get("messages")
        if isinstance(session_value.get("messages"), list)
        else []
    )
    markers = [
        str(item.get("content") or "")
        for item in messages
        if isinstance(item, Mapping) and str(item.get("content") or "")
    ]
    return {
        "binding": file_snapshot(binding_path),
        "session": file_snapshot(session_path),
        "sessionPath": str(session_path) if session_path is not None else "",
        "messageCount": len(messages),
        "contentMarkers": markers,
    }


def _replacement_persistence_snapshot(
    *,
    workspace: str,
    session_id: str,
    old_trace_id: str,
    new_trace_id: str,
    runtime_before: Mapping[str, Any],
) -> dict[str, Any]:
    directory = _replacement_session_directory(workspace, session_id)
    runtime_after = _replacement_runtime_snapshot(workspace, session_id)
    before_session = (
        runtime_before.get("session")
        if isinstance(runtime_before.get("session"), Mapping)
        else {}
    )
    after_session = (
        runtime_after.get("session")
        if isinstance(runtime_after.get("session"), Mapping)
        else {}
    )
    return {
        "oldTrace": _replacement_trace_summary(directory, old_trace_id),
        "newTrace": _replacement_trace_summary(directory, new_trace_id),
        "runtimeSessionBefore": dict(runtime_before),
        "runtimeSessionAfter": runtime_after,
        "runtimeSessionChanged": before_session.get("sha256")
        != after_session.get("sha256"),
        "runtimeSessionUnchanged": before_session.get("sha256")
        == after_session.get("sha256"),
        "sessionContentMarkers": list(runtime_after.get("contentMarkers") or []),
        "sessionMessageCount": int(runtime_after.get("messageCount") or 0),
    }


def _validate_replacement_persistence(
    actual: Mapping[str, Any], expected: Mapping[str, Any]
) -> None:
    old_trace = (
        actual.get("oldTrace") if isinstance(actual.get("oldTrace"), Mapping) else {}
    )
    new_trace = (
        actual.get("newTrace") if isinstance(actual.get("newTrace"), Mapping) else {}
    )
    checks = {
        "oldStatus": old_trace.get("status"),
        "oldReplacementStatus": old_trace.get("replacementStatus"),
        "oldSuperseded": old_trace.get("superseded"),
        "replacementTargetsNewTrace": bool(new_trace.get("traceId"))
        and old_trace.get("replacementTraceId") == new_trace.get("traceId"),
        "newTracePresent": new_trace.get("exists"),
        "newTraceStatus": new_trace.get("status"),
        "runtimeSessionChanged": actual.get("runtimeSessionChanged"),
        "runtimeSessionUnchanged": actual.get("runtimeSessionUnchanged"),
        "sessionMessageCount": actual.get("sessionMessageCount"),
    }
    mismatches = {
        key: {"expected": value, "actual": checks.get(key)}
        for key, value in expected.items()
        if key in checks and checks.get(key) != value
    }
    markers = [str(value) for value in actual.get("sessionContentMarkers") or []]
    for marker in expected.get("sessionContains") or []:
        if not any(str(marker) in value for value in markers):
            mismatches[f"sessionContains:{marker}"] = {
                "expected": True,
                "actual": False,
            }
    for marker in expected.get("sessionAbsent") or []:
        if any(str(marker) in value for value in markers):
            mismatches[f"sessionAbsent:{marker}"] = {
                "expected": True,
                "actual": False,
            }
    if mismatches:
        raise ContractError(
            f"replacement persistence did not match fixture: {mismatches}"
        )


def _run_chat_stream_contract(
    *,
    base_url: str,
    token: str,
    workspace_root: str,
    fixture_path: str,
    session_id: str,
    expected_provider: str,
    expected_model: str,
    timeout_seconds: float,
    replacement_after_setup: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    workspace = str(workspace_root or "").strip()
    if not workspace:
        raise ContractError("--workspace-root is required for the chat-stream contract")
    fixture = load_fixture(fixture_path or DEFAULT_CHAT_STREAM_FIXTURE)
    request_payload = fixture.get("request")
    if not isinstance(request_payload, Mapping):
        raise ContractError("chat stream fixture request must be an object")
    payload = dict(request_payload)
    payload["workspaceRoot"] = workspace
    trace_id = str(uuid4())
    resolved_session_id = str(session_id or "").strip() or f"runtime-contract-{uuid4().hex[:12]}"
    headers = {
        "x-trace-id": trace_id,
        "x-session-id": resolved_session_id,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    timeout = httpx.Timeout(
        connect=min(30.0, max(1.0, timeout_seconds)),
        read=None,
        write=30.0,
        pool=30.0,
    )
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            replacement_setup = fixture.get("replacementSetup")
            replacement_setup_observation: dict[str, Any] | None = None
            replacement_runtime_before: dict[str, Any] | None = None
            replacement_old_trace_id = ""
            if replacement_setup is not None:
                if not isinstance(replacement_setup, Mapping):
                    raise ContractError("chat stream replacementSetup must be an object")
                setup_request = replacement_setup.get("request")
                if not isinstance(setup_request, Mapping):
                    raise ContractError("chat stream replacementSetup.request must be an object")
                setup_payload = dict(setup_request)
                setup_payload["workspaceRoot"] = workspace
                setup_payload["replaceLatestTraceId"] = ""
                replacement_old_trace_id = str(
                    replacement_setup.get("traceId") or uuid4()
                ).strip()
                setup_fixture = dict(fixture)
                setup_fixture["request"] = setup_payload
                setup_fixture["expected"] = dict(
                    replacement_setup.get("expected")
                    if isinstance(replacement_setup.get("expected"), Mapping)
                    else {}
                )
                setup_fixture.pop("replacementSetup", None)
                setup_fixture.pop("interaction", None)
                setup_headers = dict(headers)
                setup_headers["x-trace-id"] = replacement_old_trace_id
                with client.stream(
                    "POST",
                    base_url.rstrip("/") + "/api/v1/agent/chat/stream",
                    headers=setup_headers,
                    json=setup_payload,
                ) as setup_response:
                    if setup_response.status_code != 200:
                        body = setup_response.read().decode("utf-8", errors="replace")[:1000]
                        raise ContractError(
                            f"replacement setup stream returned HTTP {setup_response.status_code}: {body}"
                        )
                    setup_events, setup_interaction = _read_stream_with_interaction(
                        client=client,
                        response=setup_response,
                        base_url=base_url,
                        token=token,
                        fixture=setup_fixture,
                        trace_id=replacement_old_trace_id,
                        session_id=resolved_session_id,
                        workspace_root=workspace,
                    )
                    try:
                        setup_expected = setup_fixture.get("expected")
                        setup_expected = (
                            setup_expected
                            if isinstance(setup_expected, Mapping)
                            else {}
                        )
                        replacement_setup_observation = validate_chat_stream_events(
                            setup_events,
                            status_code=setup_response.status_code,
                            headers=setup_response.headers,
                            trace_id=replacement_old_trace_id,
                            session_id=resolved_session_id,
                            fixture=setup_fixture,
                            expected_provider=expected_provider,
                            expected_model=expected_model,
                            require_turn_contract=(
                                setup_expected.get("turnContract", True) is not False
                            ),
                        )
                    except AgentStreamContractError as exc:
                        raise ContractError(f"replacement setup stream failed: {exc}") from exc
                    if setup_interaction:
                        replacement_setup_observation["interaction"] = setup_interaction
                replacement_runtime_before = _replacement_runtime_snapshot(
                    workspace, resolved_session_id
                )
                after_setup = replacement_setup.get("afterSetup")
                if after_setup is not None:
                    if not isinstance(after_setup, Mapping):
                        raise ContractError(
                            "chat stream replacementSetup.afterSetup must be an object"
                        )
                    if replacement_after_setup is None:
                        raise ContractError(
                            "chat stream replacement after-setup action has no runtime handler"
                        )
                    replacement_after_setup(after_setup)
                payload["replaceLatestTraceId"] = replacement_old_trace_id
            setup_observations: list[dict[str, Any]] = []
            setup = fixture.get("setupInteractions")
            if setup is not None and not isinstance(setup, list):
                raise ContractError("chat stream fixture setupInteractions must be an array")
            for item in setup or []:
                setup_item = _require_mapping(item, "chat stream setup interaction")
                setup_action = str(setup_item.get("action") or "").strip()
                storage_probe: dict[str, Any] | None = None
                if setup_action == "enqueue_followup":
                    setup_response = client.post(
                        base_url.rstrip("/") + "/api/v1/agent/followups",
                        headers=headers,
                        json={
                            "messageId": str(setup_item.get("messageId") or ""),
                            "sessionId": resolved_session_id,
                            "workspaceRoot": workspace,
                            "content": str(setup_item.get("content") or ""),
                            "mode": str(setup_item.get("mode") or "queued"),
                            "expectedTraceId": str(setup_item.get("expectedTraceId") or ""),
                        },
                        timeout=30.0,
                    )
                elif setup_action == "update_followup":
                    message_id = str(setup_item.get("messageId") or "").strip()
                    if not message_id:
                        raise ContractError(
                            "update_followup setup interaction requires messageId"
                        )
                    update_payload: dict[str, Any] = {
                        "sessionId": resolved_session_id,
                        "workspaceRoot": workspace,
                        "expectedTraceId": str(
                            setup_item.get("expectedTraceId") or ""
                        ),
                    }
                    if "content" in setup_item:
                        update_payload["content"] = setup_item.get("content")
                    if "mode" in setup_item:
                        update_payload["mode"] = setup_item.get("mode")
                    setup_response = client.patch(
                        base_url.rstrip("/")
                        + f"/api/v1/agent/followups/{message_id}",
                        headers=headers,
                        json=update_payload,
                        timeout=30.0,
                    )
                elif setup_action == "delete_followup":
                    message_id = str(setup_item.get("messageId") or "").strip()
                    if not message_id:
                        raise ContractError(
                            "delete_followup setup interaction requires messageId"
                        )
                    setup_response = client.delete(
                        base_url.rstrip("/")
                        + f"/api/v1/agent/followups/{message_id}",
                        headers=headers,
                        params={
                            "sessionId": resolved_session_id,
                            "workspaceRoot": workspace,
                        },
                        timeout=30.0,
                    )
                elif setup_action == "resume_followups":
                    setup_response = client.post(
                        base_url.rstrip("/") + "/api/v1/agent/followups/resume",
                        headers=headers,
                        json={
                            "sessionId": resolved_session_id,
                            "workspaceRoot": workspace,
                            "expectedTraceId": str(setup_item.get("expectedTraceId") or ""),
                        },
                        timeout=30.0,
                    )
                elif setup_action == "probe_followup_storage_error":
                    obstacle = (
                        Path(workspace)
                        / ".storydex"
                        / ".agent"
                        / "followups"
                    )
                    obstacle.parent.mkdir(parents=True, exist_ok=True)
                    if obstacle.exists():
                        raise ContractError(
                            "follow-up storage probe requires an unused mailbox directory"
                        )
                    marker = b"STORYDEX_FOLLOWUP_STORAGE_OBSTACLE_V1\n"
                    obstacle.write_bytes(marker)
                    try:
                        setup_response = client.post(
                            base_url.rstrip("/") + "/api/v1/agent/followups",
                            headers=headers,
                            json={
                                "messageId": str(
                                    setup_item.get("messageId")
                                    or "followup-storage-probe"
                                ),
                                "sessionId": resolved_session_id,
                                "workspaceRoot": workspace,
                                "content": str(
                                    setup_item.get("content")
                                    or "This message must not be persisted."
                                ),
                                "mode": "queued",
                            },
                            timeout=30.0,
                        )
                        unchanged = obstacle.is_file() and obstacle.read_bytes() == marker
                        artifacts = sorted(
                            path.name
                            for path in obstacle.parent.iterdir()
                            if path.name.endswith((".tmp", ".bak"))
                        )
                        storage_probe = {
                            "obstacleUnchanged": unchanged,
                            "temporaryArtifacts": artifacts,
                        }
                    finally:
                        if obstacle.is_file():
                            obstacle.unlink()
                else:
                    raise ContractError(
                        f"unsupported chat stream setup interaction: {setup_action}"
                    )
                try:
                    setup_payload = _require_mapping(
                        setup_response.json(), "setup interaction response"
                    )
                except ValueError as exc:
                    raise ContractError(
                        "setup interaction response is not valid JSON"
                    ) from exc
                if setup_action == "probe_followup_storage_error":
                    error = setup_payload.get("error")
                    error = error if isinstance(error, Mapping) else {}
                    if (
                        setup_response.status_code != 500
                        or setup_payload.get("ok") is not False
                        or error.get("code") != "followup_storage_error"
                    ):
                        raise ContractError(
                            "follow-up storage probe did not fail closed: "
                            f"HTTP {setup_response.status_code} {dict(error)}"
                        )
                    if not storage_probe or not storage_probe["obstacleUnchanged"]:
                        raise ContractError(
                            "follow-up storage probe changed the obstacle file"
                        )
                    if storage_probe["temporaryArtifacts"]:
                        raise ContractError(
                            "follow-up storage probe left temporary artifacts: "
                            f"{storage_probe['temporaryArtifacts']}"
                        )
                    setup_observations.append(
                        {
                            "action": setup_action,
                            "httpStatus": setup_response.status_code,
                            "errorCode": str(error.get("code") or ""),
                            **storage_probe,
                        }
                    )
                    continue
                if setup_response.status_code != 200 or setup_payload.get("ok") is not True:
                    raise ContractError(
                        f"setup interaction {setup_action} failed: "
                        f"HTTP {setup_response.status_code} {str(setup_payload.get('error') or '')[:500]}"
                    )
                setup_data = _require_mapping(
                    setup_payload.get("data"), "setup interaction data"
                )
                message = setup_data.get("message")
                message = message if isinstance(message, Mapping) else {}
                setup_observations.append(
                    {
                        "action": setup_action,
                        "httpStatus": setup_response.status_code,
                        "messageId": str(message.get("messageId") or setup_item.get("messageId") or ""),
                        "mode": str(message.get("mode") or ""),
                        "status": str(message.get("status") or ""),
                        "content": str(message.get("content") or ""),
                        "paused": bool(setup_data.get("paused")),
                    }
                )
            with client.stream(
                "POST",
                base_url.rstrip("/") + "/api/v1/agent/chat/stream",
                headers=headers,
                json=payload,
            ) as response:
                if response.status_code != 200:
                    body = response.read().decode("utf-8", errors="replace")[:1000]
                    raise ContractError(
                        f"chat stream returned HTTP {response.status_code}: {body}"
                    )
                events, interaction_observation = _read_stream_with_interaction(
                    client=client,
                    response=response,
                    base_url=base_url,
                    token=token,
                    fixture=fixture,
                    trace_id=trace_id,
                    session_id=resolved_session_id,
                    workspace_root=workspace,
                )
                observation = validate_chat_stream_events(
                    events,
                    status_code=response.status_code,
                    headers=response.headers,
                    trace_id=trace_id,
                    session_id=resolved_session_id,
                    fixture=fixture,
                    expected_provider=expected_provider,
                    expected_model=expected_model,
                    require_turn_contract=(
                        not isinstance(fixture.get("expected"), Mapping)
                        or fixture["expected"].get("turnContract", True) is not False
                    ),
                    allow_client_disconnect=(
                        isinstance(fixture.get("expected"), Mapping)
                        and bool(fixture["expected"].get("clientDisconnected"))
                    ),
                )
                if interaction_observation:
                    observation["interaction"] = interaction_observation
            if setup_observations:
                observation["setupInteractions"] = setup_observations
            expected = fixture.get("expected")
            expected = expected if isinstance(expected, Mapping) else {}
            if replacement_setup_observation is not None:
                observation["replacementSetup"] = replacement_setup_observation
                replacement_persistence = _replacement_persistence_snapshot(
                    workspace=workspace,
                    session_id=resolved_session_id,
                    old_trace_id=replacement_old_trace_id,
                    new_trace_id=trace_id,
                    runtime_before=replacement_runtime_before or {},
                )
                observation["replacementPersistence"] = replacement_persistence
                expected_persistence = expected.get("replacementPersistence")
                if isinstance(expected_persistence, Mapping):
                    _validate_replacement_persistence(
                        replacement_persistence,
                        expected_persistence,
                    )
            if expected.get("followupPersistence") is True:
                mailbox_response = client.get(
                    base_url.rstrip("/") + "/api/v1/agent/followups",
                    headers=headers,
                    params={
                        "sessionId": resolved_session_id,
                        "workspaceRoot": workspace,
                    },
                    timeout=30.0,
                )
                try:
                    mailbox_payload = _require_mapping(
                        mailbox_response.json(), "follow-up mailbox response"
                    )
                except ValueError as exc:
                    raise ContractError(
                        "follow-up mailbox response is not valid JSON"
                    ) from exc
                if mailbox_response.status_code != 200 or mailbox_payload.get("ok") is not True:
                    raise ContractError(
                        "follow-up mailbox read failed: "
                        f"HTTP {mailbox_response.status_code} {str(mailbox_payload.get('error') or '')[:500]}"
                    )
                mailbox = _require_mapping(
                    mailbox_payload.get("data"), "follow-up mailbox data"
                )
                messages = mailbox.get("messages")
                messages = messages if isinstance(messages, list) else []
                events_value = mailbox.get("events")
                events_value = events_value if isinstance(events_value, list) else []
                observation["followupMailbox"] = {
                    "revision": int(mailbox.get("revision") or 0),
                    "revisionPositive": int(mailbox.get("revision") or 0) > 0,
                    "paused": bool(mailbox.get("paused")),
                    "pauseReason": str(mailbox.get("pauseReason") or ""),
                    "activeTraceEmpty": not str(mailbox.get("activeTraceId") or "").strip(),
                    "messages": [
                        {
                            "messageId": str(item.get("messageId") or ""),
                            "mode": str(item.get("mode") or ""),
                            "status": str(item.get("status") or ""),
                            "content": str(item.get("content") or ""),
                            "dispatchTracePresent": bool(
                                str(item.get("dispatchTraceId") or "").strip()
                            ),
                        }
                        for item in messages
                        if isinstance(item, Mapping)
                    ],
                    "eventTypes": [
                        str(item.get("_type") or "")
                        for item in events_value
                        if isinstance(item, Mapping)
                    ],
                }
                event_type_counts: dict[str, int] = {}
                for event_type in observation["followupMailbox"]["eventTypes"]:
                    event_type_counts[event_type] = (
                        event_type_counts.get(event_type, 0) + 1
                    )
                observation["followupMailbox"]["eventTypeCounts"] = dict(
                    sorted(event_type_counts.items())
                )
                expected_messages = expected.get("followupMessages")
                if isinstance(expected_messages, list):
                    actual_messages = observation["followupMailbox"]["messages"]
                    if actual_messages != expected_messages:
                        raise ContractError(
                            "follow-up messages did not match the fixture expectation: "
                            f"expected={expected_messages!r} actual={actual_messages!r}"
                        )
                expected_mailbox = expected.get("followupMailbox")
                if isinstance(expected_mailbox, Mapping):
                    actual_mailbox = observation["followupMailbox"]
                    mismatches = {
                        str(key): {
                            "expected": value,
                            "actual": actual_mailbox.get(str(key)),
                        }
                        for key, value in expected_mailbox.items()
                        if actual_mailbox.get(str(key)) != value
                    }
                    if mismatches:
                        raise ContractError(
                            "follow-up mailbox did not match the fixture expectation: "
                            f"{mismatches}"
                        )
            return observation
    except AgentStreamContractError as exc:
        raise ContractError(str(exc)) from exc


def run_contract(
    *,
    base_url: str,
    token: str,
    implementation: str,
    contract: str = "health",
    expected_provider: str = "",
    expected_model: str = "",
    workspace_root: str = "",
    fixture_path: str = "",
    session_id: str = "",
    timeout_seconds: float = 300.0,
    replacement_after_setup: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    contract_id = CONTRACT_IDS.get(contract)
    if contract_id is None:
        raise ContractError(f"unknown Agent runtime contract: {contract}")
    started = time.perf_counter()
    if contract == "chat-stream":
        observation = _run_chat_stream_contract(
            base_url=base_url,
            token=token,
            workspace_root=workspace_root,
            fixture_path=fixture_path,
            session_id=session_id,
            expected_provider=expected_provider,
            expected_model=expected_model,
            timeout_seconds=timeout_seconds,
            replacement_after_setup=replacement_after_setup,
        )
    else:
        path = (
            "/api/v1/sys/health"
            if contract == "health"
            else "/api/v1/agent/coomi/status"
        )
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with httpx.Client(timeout=15.0, trust_env=False) as client:
            response = client.get(base_url.rstrip("/") + path, headers=headers)
        observation = (
            validate_health_response(response)
            if contract == "health"
            else validate_coomi_status_response(response)
        )
        if expected_provider and observation.get("providerId") != expected_provider:
            raise ContractError(
                f"Coomi status providerId {observation.get('providerId')!r} "
                f"does not match expected {expected_provider!r}"
            )
        if expected_model and observation.get("model") != expected_model:
            raise ContractError(
                f"Coomi status model {observation.get('model')!r} "
                f"does not match expected {expected_model!r}"
            )
    return {
        "schemaVersion": 1,
        "contractId": contract_id,
        "status": "passed",
        "implementation": implementation,
        "origin": safe_origin(base_url),
        "elapsedMs": int((time.perf_counter() - started) * 1000),
        "observation": observation,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--implementation", required=True)
    parser.add_argument("--contract", choices=sorted(CONTRACT_IDS), default="health")
    parser.add_argument("--token", default="")
    parser.add_argument("--expected-provider", default="")
    parser.add_argument("--expected-model", default="")
    parser.add_argument("--workspace-root", default="")
    parser.add_argument("--fixture", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--output", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    contract_id = CONTRACT_IDS[str(args.contract)]
    try:
        report = run_contract(
            base_url=str(args.base_url),
            token=str(args.token),
            implementation=str(args.implementation),
            contract=str(args.contract),
            expected_provider=str(args.expected_provider),
            expected_model=str(args.expected_model),
            workspace_root=str(args.workspace_root),
            fixture_path=str(args.fixture),
            session_id=str(args.session_id),
            timeout_seconds=float(args.timeout),
        )
        if str(args.output or "").strip():
            output = Path(args.output).resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (ContractError, httpx.HTTPError) as exc:
        print(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "contractId": contract_id,
                    "status": "failed",
                    "implementation": str(args.implementation),
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
