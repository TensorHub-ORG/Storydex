from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scripts.run_agent_runtime_contract import ContractError, validate_health_response
from scripts.run_agent_runtime_contract import validate_coomi_status_response


def _response(payload: dict, *, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", "http://127.0.0.1/api/v1/sys/health"),
    )


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
