from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import scripts.run_agent_lifecycle_baseline as baseline_module
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


def test_baseline_writes_report_when_post_turn_validation_fails(
    tmp_path, monkeypatch
) -> None:
    source_config = tmp_path / "providers.json"
    source_config.write_text(
        json.dumps(
            {
                "version": 1,
                "active": "provider",
                "providers": {
                    "provider": {
                        "type": "openai_compatible",
                        "base_url": "https://example.test/v1",
                        "api_key": "fixture-value",
                        "model": "model",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    bridge = tmp_path / "storydex-coomi-bridge.exe"
    bridge.write_bytes(b"bridge")
    output_dir = tmp_path / "output"

    class FakeBackendProcess:
        base_url = "http://127.0.0.1:1"

        def __init__(self, **_kwargs) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class FakeClient:
        def close(self) -> None:
            pass

    monkeypatch.setattr(baseline_module, "BackendProcess", FakeBackendProcess)
    monkeypatch.setattr(baseline_module.httpx, "Client", FakeClient)
    monkeypatch.setattr(baseline_module, "free_port", lambda: 1)
    monkeypatch.setattr(
        baseline_module,
        "run_turn",
        lambda *_args, **_kwargs: {
            "traceId": "trace",
            "sessionId": "session",
            "elapsedMs": 10,
            "eventCount": 2,
            "events": [],
            "protocol": {},
            "replyPreview": baseline_module.MARKER,
            "usage": {},
            "errors": [],
            "lifecycle": {
                "toolCalls": 1,
                "tools": [{"tool": "read_file", "error": True}],
            },
            "toolCalls": [
                {
                    "event": "ToolStart",
                    "toolName": "read_file",
                    "toolCallId": "call-1",
                    "arguments": {"path": "chapters/lifecycle-baseline.md"},
                },
                {
                    "event": "ToolDone",
                    "toolName": "read_file",
                    "toolCallId": "call-1",
                    "isError": True,
                },
            ],
        },
    )
    args = SimpleNamespace(
        output_dir=str(output_dir),
        config=str(source_config),
        bridge=str(bridge),
        scenario="strict-single-read",
        provider_id="provider",
        model="model",
        enable_parallel_tool_calls=False,
        intent_routing_mode="",
        backend_repository_root="",
        reasoning_effort="low",
        turn_timeout=30,
    )

    with pytest.raises(AcceptanceError, match="report="):
        baseline_module.run_baseline(args)

    report = json.loads((output_dir / "baseline-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["error"] == "baseline validation failed: baseline task reported a tool error"
    assert report["turn"]["toolSequence"] == ["read_file"]


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


def test_agent_error_summary_keeps_provider_status_without_error_details() -> None:
    summary = summarize_event(
        "AgentError",
        {
            "message": "provider returned HTTP 403: Forbidden",
            "details": {
                "statusCode": 403,
                "providerHttpStatus": 403,
                "exceptionMessage": "Authorization: Bearer should-not-survive",
            },
        },
    )

    assert summary["statusCode"] == 403
    assert "exceptionMessage" not in summary
    assert "should-not-survive" not in str(summary)


def test_turn_contract_summary_records_structure_context_without_content() -> None:
    summary = summarize_event(
        "TurnContract",
        {
            "intentFrame": {
                "primary": "character_work",
                "operationType": "modify_existing",
                "canWrite": True,
            },
            "executionPolicy": {
                "capabilityMode": "scoped_write",
                "allowedWriteRoots": [".storydex/characters/"],
            },
            "contextAssembly": {
                "budget": {
                    "maxTotalChars": 10_000,
                    "totalChars": 1_234,
                    "blockCount": 2,
                },
                "contextTrace": {
                    "totals": {"assembleMs": 87.5},
                    "sources": [
                        {
                            "kind": "active_characters",
                            "policy": "structure_map_matched_spans_jit_read",
                            "structureMapCount": 1,
                            "matchedSpanCount": 1,
                            "requiresFullReadBeforeWrite": True,
                            "relevanceMatched": True,
                            "unmatchedFallbackUsed": False,
                            "queryTerms": ["must-not-survive"],
                            "candidateChars": 900,
                            "chars": 850,
                            "elapsedMs": 12.5,
                            "included": True,
                            "truncated": False,
                        }
                    ],
                },
                "promptBlocks": [
                    {
                        "id": "active_characters",
                        "title": "Relevant character structure maps and matched evidence",
                        "content": (
                            "revision=sha256:secret\nStructure map (Markdown headings):\n"
                            "Matched evidence span: secret manuscript\nRead-before-write: secret"
                        ),
                        "sourcePaths": [".storydex/characters/secret.md"],
                        "charCount": 850,
                        "truncated": False,
                    }
                ],
            },
        },
    )

    context = summary["turnContract"]["contextAssembly"]
    assert context["assembleMs"] == 87.5
    assert context["sources"] == [
        {
            "kind": "active_characters",
            "policy": "structure_map_matched_spans_jit_read",
            "candidateChars": 900,
            "chars": 850,
            "included": True,
            "truncated": False,
            "dropReason": "",
            "elapsedMs": 12.5,
            "structureMapCount": 1,
            "matchedSpanCount": 1,
            "requiresFullReadBeforeWrite": True,
            "relevanceMatched": True,
            "unmatchedFallbackUsed": False,
            "queryTermCount": 1,
        }
    ]
    assert context["blocks"][0]["hasStructureMap"] is True
    assert context["blocks"][0]["hasMatchedEvidence"] is True
    assert context["blocks"][0]["hasRevisionMetadata"] is True
    assert context["blocks"][0]["hasReadBeforeWrite"] is True
    assert "must-not-survive" not in str(summary)
    assert "secret manuscript" not in str(summary)
    assert ".storydex/characters/secret.md" not in str(summary)


def test_turn_contract_summary_records_related_span_without_path_or_excerpt() -> None:
    summary = summarize_event(
        "TurnContract",
        {
            "contextAssembly": {
                "contextTrace": {
                    "sources": [
                        {
                            "kind": "related_passages",
                            "policy": "fts5_v3_chunk_bm25",
                            "candidateChars": 700,
                            "chars": 650,
                            "included": True,
                            "retrieval": {
                                "status": "ok",
                                "resultState": "hits",
                                "query": "must-not-survive",
                                "candidateSpans": [
                                    {
                                        "path": "chapters/secret.md",
                                        "revision": "sha256:secret",
                                    }
                                ],
                            },
                        }
                    ]
                },
                "promptBlocks": [
                    {
                        "id": "related_passages",
                        "title": "Related project passages (retrieval)",
                        "content": (
                            "### chapters/secret.md lines 20-22 chars 400-520 "
                            "revision sha256:secret\nsecret manuscript excerpt"
                        ),
                        "sourcePaths": ["chapters/secret.md"],
                    }
                ],
            }
        },
    )

    context = summary["turnContract"]["contextAssembly"]
    assert context["sources"][0]["retrievalStatus"] == "ok"
    assert context["sources"][0]["retrievalResultState"] == "hits"
    assert context["sources"][0]["candidateSpanCount"] == 1
    assert context["blocks"][0]["hasRevisionMetadata"] is True
    assert context["blocks"][0]["hasSpanMetadata"] is True
    assert "must-not-survive" not in str(summary)
    assert "chapters/secret.md" not in str(summary)
    assert "secret manuscript excerpt" not in str(summary)


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
        (tmp_path / "coomi-home" / "config" / "providers.json").read_text(
            encoding="utf-8"
        )
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


def test_chapter_middle_fixture_keeps_marker_outside_head_and_tail_previews(
    tmp_path,
) -> None:
    fixture = prepare_fixture(tmp_path, "chapter-middle-read")
    relative = fixture["workspaceFiles"][0]
    content = (tmp_path / relative).read_text(encoding="utf-8")
    marker = fixture["markers"][0]

    assert relative == "chapters/lifecycle-middle.md"
    assert content.index(marker) > 1_500
    assert len(content) - content.index(marker) > 1_500
    assert "中段锚点" in fixture["prompt"]
