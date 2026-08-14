from __future__ import annotations

import pytest

from scripts.run_agent_lifecycle_baseline import (
    AcceptanceError,
    MULTI_MARKERS,
    enable_parallel_tool_calls_in_isolated_config,
    output_limit_observation,
    parallel_tool_calls_observation,
    prepare_fixture,
    validate_baseline_turn,
)
from scripts.run_graph_live_acceptance import load_isolated_provider, summarize_event


def valid_turn() -> dict:
    return {
        "toolCallCount": 1,
        "toolNames": ["read_file"],
        "toolSequence": ["read_file"],
        "uniqueToolInvocationCount": 1,
        "duplicateToolInvocationCount": 0,
        "markerObserved": True,
        "markersObserved": True,
        "lifecycle": {"tools": [{"tool": "read_file", "error": False}]},
    }


def test_baseline_validation_accepts_exact_read_only_turn() -> None:
    validate_baseline_turn(valid_turn())


@pytest.mark.parametrize(
    "change",
    (
        {"toolSequence": ["read_file", "update_plan"]},
        {"toolCallCount": 2},
        {"markersObserved": False},
        {"lifecycle": {"tools": [{"tool": "read_file", "error": True}]}},
    ),
)
def test_baseline_validation_rejects_contract_breaks(change: dict) -> None:
    turn = valid_turn()
    turn.update(change)
    with pytest.raises(AcceptanceError):
        validate_baseline_turn(turn)


def test_provider_stream_summary_keeps_only_redacted_metrics() -> None:
    summary = summarize_event(
        "ProviderStream",
        {
            "phase": "first_byte",
            "attempt": 1,
            "elapsedMs": 120,
            "requestBytes": 2048,
            "responseBytes": 128,
            "maxOutputTokens": 8192,
            "httpStatus": 200,
            "authorization": "must-not-survive",
            "content": "must-not-survive",
        },
    )

    assert summary == {
        "event": "ProviderStream",
        "phase": "first_byte",
        "elapsedMs": 120,
        "attempt": 1,
        "requestBytes": 2048,
        "responseBytes": 128,
        "maxOutputTokens": 8192,
        "httpStatus": 200,
    }


def test_output_limit_observation_uses_real_wire_evidence() -> None:
    observed = output_limit_observation(
        {},
        {
            "rounds": [
                {"wireMaxOutputTokens": 8192},
                {"wireMaxOutputTokens": 8192},
            ]
        },
    )

    assert observed["wireObserved"] is True
    assert observed["observedWireMaxOutputTokens"] == [8192]
    assert observed["mismatch"] is False


def test_parallel_tool_calls_observation_requires_wire_evidence() -> None:
    observed = parallel_tool_calls_observation(
        {"supports_parallel_tool_calls": True},
        {"rounds": [{"wireParallelToolCalls": True}]},
    )

    assert observed == {
        "configured": True,
        "wireObserved": True,
        "observedWireValues": [True],
        "mismatch": False,
    }


def test_parallel_tool_calls_override_only_changes_isolated_provider(tmp_path) -> None:
    config_path = tmp_path / "config" / "providers.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '{"version":1,"active":"p","providers":{"p":{"model":"m"}}}',
        encoding="utf-8",
    )

    provider = enable_parallel_tool_calls_in_isolated_config(tmp_path, "p")

    assert provider["supports_parallel_tool_calls"] is True
    document = __import__("json").loads(config_path.read_text(encoding="utf-8"))
    assert document["providers"]["p"]["supports_parallel_tool_calls"] is True


def test_load_isolated_provider_converts_opencode_config_without_retaining_other_providers(
    tmp_path,
) -> None:
    source = tmp_path / "opencode.json"
    source.write_text(
        __import__("json").dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "provider": {
                    "ds": {
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {
                            "apiKey": "test-secret",
                            "baseURL": "https://example.test/v1",
                        },
                        "models": {
                            "deepseek-v4-flash": {
                                "name": "v4f",
                                "limit": {"context": 200000, "output": 4096},
                            }
                        },
                    },
                    "unused": {
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {
                            "apiKey": "other",
                            "baseURL": "https://unused.test/v1",
                        },
                        "models": {"other": {"name": "other"}},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    summary = load_isolated_provider(
        source,
        tmp_path / "coomi-home",
        "ds",
        "deepseek-v4-flash",
    )

    assert summary["sourceFormat"] == "opencode"
    assert summary["providerId"] == "ds"
    assert summary["model"] == "deepseek-v4-flash"
    isolated = __import__("json").loads(
        (tmp_path / "coomi-home" / "config" / "providers.json").read_text(encoding="utf-8")
    )
    assert list(isolated["providers"]) == ["ds"]
    assert isolated["providers"]["ds"] == {
        "type": "openai_compatible",
        "display": "ds",
        "api_key": "test-secret",
        "base_url": "https://example.test/v1",
        "model": "deepseek-v4-flash",
        "tool_protocol": "auto",
        "context_window": 200000,
        "max_output_tokens": 4096,
    }


def test_multi_read_fixture_and_validation_require_three_unique_reads(tmp_path) -> None:
    fixture = prepare_fixture(tmp_path, "strict-multi-read")
    turn = {
        "toolCallCount": 3,
        "toolNames": ["read_file"],
        "toolSequence": ["read_file", "read_file", "read_file"],
        "uniqueToolInvocationCount": 3,
        "duplicateToolInvocationCount": 0,
        "markersObserved": True,
        "lifecycle": {
            "tools": [
                {"tool": "read_file", "error": False},
                {"tool": "read_file", "error": False},
                {"tool": "read_file", "error": False},
            ]
        },
    }

    assert fixture["markers"] == list(MULTI_MARKERS)
    validate_baseline_turn(turn, fixture)

    turn["duplicateToolInvocationCount"] = 1
    with pytest.raises(AcceptanceError, match="repeated"):
        validate_baseline_turn(turn, fixture)
