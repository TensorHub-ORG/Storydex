from __future__ import annotations

import pytest

from services.coomi_bridge_client import (
    _decode_lines,
    _wire_messages,
    _wire_tools,
)


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
