"""Run one provider-network-free Agent runtime contract against any HTTP base URL."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx


HEALTH_CONTRACT_ID = "agent.sys.health.v1"
COOMI_STATUS_CONTRACT_ID = "agent.coomi.status.v1"
CONTRACT_IDS = {
    "health": HEALTH_CONTRACT_ID,
    "coomi-status": COOMI_STATUS_CONTRACT_ID,
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


def run_contract(
    *,
    base_url: str,
    token: str,
    implementation: str,
    contract: str = "health",
    expected_provider: str = "",
    expected_model: str = "",
) -> dict[str, Any]:
    contract_id = CONTRACT_IDS.get(contract)
    if contract_id is None:
        raise ContractError(f"unknown Agent runtime contract: {contract}")
    path = (
        "/api/v1/sys/health"
        if contract == "health"
        else "/api/v1/agent/coomi/status"
    )
    started = time.perf_counter()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(timeout=15.0) as client:
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
