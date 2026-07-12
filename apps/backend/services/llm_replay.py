from __future__ import annotations

import contextvars
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterator


_MODE_ENV = "STORYDEX_LLM_MODE"
_FIXTURE_DIR_ENV = "STORYDEX_LLM_FIXTURE_DIR"
_FIXTURE_FILE = "calls.jsonl"
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "headers",
    "password",
    "secret",
    "token",
}
_TRACE_ID = contextvars.ContextVar("storydex_llm_trace_id", default="default")
_COUNTERS_LOCK = threading.Lock()
_COUNTERS: dict[str, dict[str, Any]] = {}


class ReplayError(RuntimeError):
    pass


class ReplayMismatch(ReplayError):
    pass


@contextmanager
def llm_trace(trace_id: str) -> Iterator[None]:
    token = _TRACE_ID.set(str(trace_id or "default"))
    try:
        yield
    finally:
        _TRACE_ID.reset(token)


def reset_llm_metrics(trace_id: str | None = None) -> None:
    with _COUNTERS_LOCK:
        if trace_id is None:
            _COUNTERS.clear()
        else:
            _COUNTERS.pop(str(trace_id or "default"), None)


def get_llm_metrics(trace_id: str | None = None) -> dict[str, Any]:
    key = str(trace_id or _TRACE_ID.get() or "default")
    with _COUNTERS_LOCK:
        value = _COUNTERS.get(key, {})
        return {
            "traceId": key,
            "calls": int(value.get("calls", 0)),
            "byMethod": dict(value.get("byMethod", {})),
            "promptTokens": int(value.get("promptTokens", 0)),
            "completionTokens": int(value.get("completionTokens", 0)),
            "totalTokens": int(value.get("totalTokens", 0)),
        }


def get_replayable_llm_provider(provider: Any = None) -> Any:
    if provider is None:
        from coomi.services import get_llm_provider

        provider = get_llm_provider()
    return ReplayableLLMProvider(provider)


class ReplayableLLMProvider:
    def __init__(self, provider: Any) -> None:
        object.__setattr__(self, "_provider", provider)
        mode = str(os.getenv(_MODE_ENV, "off") or "off").strip().lower()
        if mode not in {"off", "record", "replay"}:
            raise ReplayError(f"Unsupported {_MODE_ENV} value: {mode!r}")
        object.__setattr__(self, "_mode", mode)
        fixture_dir = str(os.getenv(_FIXTURE_DIR_ENV, "") or "").strip()
        if mode in {"record", "replay"} and not fixture_dir:
            raise ReplayError(f"{_FIXTURE_DIR_ENV} is required when {_MODE_ENV}={mode}")
        object.__setattr__(self, "_fixture_path", Path(fixture_dir) / _FIXTURE_FILE if fixture_dir else None)
        object.__setattr__(self, "_sequence", 0)
        object.__setattr__(self, "_records", self._load_records() if mode == "replay" else [])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._provider, name, value)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any:
        request = self._request("chat", messages, tools, kwargs)
        self._count_call("chat")
        if self._mode == "replay":
            record = self._next_record(request)
            response = _decode_chat_response(record.get("response"))
        else:
            response = await self._provider.chat(messages, tools, **kwargs)
            if self._mode == "record":
                self._append_record(request, _encode_value(response))
        self._count_usage(_usage_from_chat_response(response))
        return response

    async def chat_stream(self, messages: list[dict[str, Any]], **kwargs: Any) -> AsyncIterator[str]:
        request = self._request("chat_stream", messages, None, kwargs)
        self._count_call("chat_stream")
        if self._mode == "replay":
            record = self._next_record(request)
            for chunk in record.get("response") or []:
                yield str(chunk)
            return

        chunks: list[str] = []
        async for chunk in self._provider.chat_stream(messages, **kwargs):
            value = str(chunk)
            chunks.append(value)
            yield value
        if self._mode == "record":
            self._append_record(request, chunks)

    async def chat_stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        request = self._request("chat_stream_with_tools", messages, tools, kwargs)
        self._count_call("chat_stream_with_tools")
        if self._mode == "replay":
            record = self._next_record(request)
            for chunk in record.get("response") or []:
                value = _sanitize(chunk)
                self._count_usage(_usage_from_stream_chunk(value))
                yield value
            return

        chunks: list[dict[str, Any]] = []
        async for chunk in self._provider.chat_stream_with_tools(messages, tools, **kwargs):
            value = _sanitize(chunk)
            chunks.append(value)
            self._count_usage(_usage_from_stream_chunk(value))
            yield chunk
        if self._mode == "record":
            self._append_record(request, chunks)

    def assert_replay_complete(self) -> None:
        if self._mode == "replay" and self._sequence != len(self._records):
            raise ReplayMismatch(
                f"Replay has {len(self._records) - self._sequence} unused record(s): "
                f"consumed={self._sequence}, total={len(self._records)}"
            )

    def _request(
        self,
        method: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_tools = _sanitize(tools or [])
        tools_json = _stable_json(normalized_tools)
        return {
            "method": method,
            "model": str(getattr(self._provider, "model", "") or ""),
            "messages": _sanitize(messages),
            "tools_digest": hashlib.sha256(tools_json.encode("utf-8")).hexdigest(),
            "kwargs": _sanitize(kwargs),
        }

    def _load_records(self) -> list[dict[str, Any]]:
        path = self._fixture_path
        if path is None or not path.is_file():
            raise ReplayError(f"Replay fixture is missing: {path}")
        records: list[dict[str, Any]] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReplayError(f"Invalid replay JSONL at line {line_no}: {exc}") from exc
            if not isinstance(value, dict):
                raise ReplayError(f"Replay record at line {line_no} must be an object")
            records.append(value)
        return records

    def _append_record(self, request: dict[str, Any], response: Any) -> None:
        path = self._fixture_path
        if path is None:
            raise ReplayError("Record fixture path is not configured")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._sequence += 1
        record = {
            "seq": self._sequence,
            "request": request,
            "request_hash": _request_hash(request),
            "response": response,
        }
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(_stable_json(record) + "\n")

    def _next_record(self, request: dict[str, Any]) -> dict[str, Any]:
        if self._sequence >= len(self._records):
            raise ReplayMismatch(
                f"Replay fixture exhausted before request #{self._sequence + 1}: "
                f"{request.get('method')}"
            )
        record = self._records[self._sequence]
        self._sequence += 1
        expected = record.get("request") if isinstance(record.get("request"), dict) else {}
        if _request_hash(expected) != _request_hash(request):
            diff = _diff_values(expected, request)
            raise ReplayMismatch(
                f"Replay request #{self._sequence} mismatch:\n" + "\n".join(diff[:30])
            )
        return record

    def _count_call(self, method: str) -> None:
        trace_id = str(_TRACE_ID.get() or "default")
        with _COUNTERS_LOCK:
            counter = _COUNTERS.setdefault(trace_id, {"calls": 0, "byMethod": {}})
            counter["calls"] = int(counter.get("calls", 0)) + 1
            by_method = counter.setdefault("byMethod", {})
            by_method[method] = int(by_method.get(method, 0)) + 1

    def _count_usage(self, usage: dict[str, int] | None) -> None:
        if not usage:
            return
        trace_id = str(_TRACE_ID.get() or "default")
        with _COUNTERS_LOCK:
            counter = _COUNTERS.setdefault(trace_id, {"calls": 0, "byMethod": {}})
            counter["promptTokens"] = int(counter.get("promptTokens", 0)) + int(usage.get("prompt_tokens", 0))
            counter["completionTokens"] = int(counter.get("completionTokens", 0)) + int(
                usage.get("completion_tokens", 0)
            )
            counter["totalTokens"] = int(counter.get("totalTokens", 0)) + int(usage.get("total_tokens", 0))


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _request_hash(request: dict[str, Any]) -> str:
    return hashlib.sha256(_stable_json(_sanitize(request)).encode("utf-8")).hexdigest()


def _sanitize(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if normalized_key.lower().replace("-", "_") in _SENSITIVE_KEYS:
                continue
            result[normalized_key] = _sanitize(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "model_dump"):
        return _sanitize(value.model_dump())
    if hasattr(value, "__dict__"):
        return _sanitize(vars(value))
    return str(value)


def _encode_value(value: Any) -> Any:
    return _sanitize(value)


def _decode_chat_response(value: Any) -> Any:
    payload = value if isinstance(value, dict) else {}
    tool_calls_payload = payload.get("tool_calls")
    try:
        from coomi.types import LLMResponse, ToolCall

        tool_calls = None
        if isinstance(tool_calls_payload, list):
            tool_calls = [ToolCall(**item) for item in tool_calls_payload if isinstance(item, dict)]
        return LLMResponse(
            content=payload.get("content"),
            tool_calls=tool_calls,
            usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
            reasoning_content=payload.get("reasoning_content"),
        )
    except (ImportError, TypeError):
        return SimpleNamespace(**payload)


def _usage_from_chat_response(response: Any) -> dict[str, int] | None:
    usage = getattr(response, "usage", None)
    return usage if isinstance(usage, dict) else None


def _usage_from_stream_chunk(chunk: Any) -> dict[str, int] | None:
    if not isinstance(chunk, dict) or chunk.get("type") != "usage":
        return None
    usage = chunk.get("data")
    return usage if isinstance(usage, dict) else None


def _diff_values(expected: Any, actual: Any, path: str = "request") -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        lines: list[str] = []
        for key in sorted(set(expected) | set(actual)):
            child = f"{path}.{key}"
            if key not in expected:
                lines.append(f"+ {child}={actual[key]!r}")
            elif key not in actual:
                lines.append(f"- {child}={expected[key]!r}")
            else:
                lines.extend(_diff_values(expected[key], actual[key], child))
        return lines
    if isinstance(expected, list) and isinstance(actual, list):
        lines = []
        for index in range(max(len(expected), len(actual))):
            child = f"{path}[{index}]"
            if index >= len(expected):
                lines.append(f"+ {child}={actual[index]!r}")
            elif index >= len(actual):
                lines.append(f"- {child}={expected[index]!r}")
            else:
                lines.extend(_diff_values(expected[index], actual[index], child))
        return lines
    return [] if expected == actual else [f"~ {path}: expected={expected!r}, actual={actual!r}"]
