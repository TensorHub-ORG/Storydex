from __future__ import annotations

import subprocess

import pytest

from services.coomi_agent_service import _CoomiEventTranslator, _create_storydex_tool_registry
from services.coomi_bridge_client import (
    BRIDGE_PROTOCOL_VERSION,
    STORYDEX_COOMI_RUNTIME_VERSION,
    _decode_lines,
    _wire_messages,
    _wire_tools,
    bridge_command,
)
from services.coomi_version_service import read_expected_coomi_version


def test_vendored_rust_bridge_version_contract() -> None:
    completed = subprocess.run(
        [*bridge_command(), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0
    assert f"storydex-coomi-bridge {STORYDEX_COOMI_RUNTIME_VERSION}" in completed.stdout
    assert read_expected_coomi_version() == STORYDEX_COOMI_RUNTIME_VERSION
    assert BRIDGE_PROTOCOL_VERSION == 1


def test_jsonl_and_wire_contract_preserve_tool_history() -> None:
    packets = _decode_lines(b'{"type":"text","data":{"text":"ok"}}\n')
    assert packets == [{"type": "text", "data": {"text": "ok"}}]
    messages = _wire_messages(
        [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "Read"}]},
            {"role": "tool", "content": "done", "tool_call_id": "c1"},
        ]
    )
    assert messages[0]["tool_calls"][0]["id"] == "c1"
    assert messages[1]["tool_call_id"] == "c1"
    assert _wire_tools(
        [{"type": "function", "function": {"name": "StorydexWikiQuery", "parameters": {"type": "object"}}}]
    )[0]["name"] == "StorydexWikiQuery"


def test_invalid_jsonl_is_rejected() -> None:
    with pytest.raises(Exception, match="Invalid JSONL"):
        _decode_lines(b"not-json\n")


def test_bridge_events_translate_to_storydex_public_shapes() -> None:
    translator = _CoomiEventTranslator(session_id="contract-session")
    assert translator.translate({"type": "reasoning_delta", "data": {"text": "hidden"}}) is None
    start = translator.translate(
        {"type": "tool_started", "data": {"call": {"id": "c1", "name": "Read", "arguments": {}}}}
    )
    done = translator.translate(
        {
            "type": "tool_finished",
            "data": {"call": {"id": "c1", "name": "Read"}, "result": {"success": True, "output": "ok"}},
        }
    )
    assert start is not None and start[0] == "ToolStart"
    assert done is not None and done[0] == "ToolDone"
    assert done[1]["tool_call_id"] == "c1"
    assert done[1]["is_error"] is False


def test_storydex_domain_registry_is_independent_of_python_coomi(tmp_path) -> None:
    registry = _create_storydex_tool_registry(tmp_path)
    names = {tool.name for tool in registry.list_tools()}
    assert {"StorydexProjectSearch", "StorydexWikiQuery", "StorydexApplyStoryIncrement"} <= names
    assert all(spec["name"].startswith("Storydex") for spec in registry.specs())
