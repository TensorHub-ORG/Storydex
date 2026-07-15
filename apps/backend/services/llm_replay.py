from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
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
_PURPOSE = contextvars.ContextVar("storydex_llm_purpose", default="unknown")
_CONTEXT_ASSEMBLY = contextvars.ContextVar("storydex_llm_context_assembly", default=None)
_COUNTERS_LOCK = threading.Lock()
_COUNTERS: dict[str, dict[str, Any]] = {}
_FIXTURE_LOCK = threading.Lock()
_FIXTURE_STATES: dict[tuple[str, str], dict[str, Any]] = {}
_KNOWN_PURPOSES = {"intent", "plan", "commit", "memory_recall", "chat", "loop"}
_COOMI_SESSION_PATH_RE = re.compile(
    r"(?i)([\\/]\.coomi[\\/]sessions[\\/])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_VOLATILE_TIMESTAMP_FIELD_RE = re.compile(
    r'("(?:createdAt|updatedAt|generatedAt|lastAnalyzedAt|mtime|timestamp)"\s*:\s*")([^"]*)(")'
)
_CHAPTER_PROGRESS_MANIFEST_ENTRY_RE = re.compile(
    r'(?i)"\.storydex[\\/]memory[\\/]chapter-progress\.json"\s*:\s*\{'
)
_SHA256_FIELD_RE = re.compile(r'(?i)("sha256"\s*:\s*")[0-9a-f]{64}(")')
_ABSOLUTE_PATH_LINE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\\\\)")


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


@contextmanager
def llm_purpose(purpose: str) -> Iterator[None]:
    normalized = str(purpose or "").strip().lower()
    token = _PURPOSE.set(normalized if normalized in _KNOWN_PURPOSES else "unknown")
    try:
        yield
    finally:
        _PURPOSE.reset(token)


@contextmanager
def llm_context_assembly(context_assembly: dict[str, Any] | None) -> Iterator[None]:
    token = _CONTEXT_ASSEMBLY.set(context_assembly if isinstance(context_assembly, dict) else None)
    try:
        yield
    finally:
        _CONTEXT_ASSEMBLY.reset(token)


def reset_llm_metrics(trace_id: str | None = None) -> None:
    with _COUNTERS_LOCK:
        if trace_id is None:
            _COUNTERS.clear()
        else:
            _COUNTERS.pop(str(trace_id or "default"), None)


def reset_llm_fixture_state(fixture_dir: str | Path | None = None) -> None:
    with _FIXTURE_LOCK:
        if fixture_dir is None:
            _FIXTURE_STATES.clear()
            return
        fixture_path = (Path(fixture_dir) / _FIXTURE_FILE).resolve()
        fixture_key = str(fixture_path)
        for key in [key for key in _FIXTURE_STATES if key[1] == fixture_key]:
            _FIXTURE_STATES.pop(key, None)


def get_llm_metrics(trace_id: str | None = None) -> dict[str, Any]:
    key = str(trace_id or _TRACE_ID.get() or "default")
    with _COUNTERS_LOCK:
        value = _COUNTERS.get(key, {})
        call_groups = value.get("callGroups") if isinstance(value.get("callGroups"), dict) else {}
        llm_calls: list[dict[str, Any]] = []
        for group in call_groups.values():
            if not isinstance(group, dict):
                continue
            usage_calls = int(group.get("usageCalls", 0))
            llm_calls.append(
                {
                    "purpose": str(group.get("purpose") or "unknown"),
                    "method": str(group.get("method") or "unknown"),
                    "count": int(group.get("count", 0)),
                    "inputTokens": int(group.get("inputTokens", 0)) if usage_calls else None,
                    "outputTokens": int(group.get("outputTokens", 0)) if usage_calls else None,
                }
            )
        provider_requests = value.get("providerRequests") if isinstance(value.get("providerRequests"), list) else []
        return {
            "traceId": key,
            "calls": int(value.get("calls", 0)),
            "byMethod": dict(value.get("byMethod", {})),
            "promptTokens": int(value.get("promptTokens", 0)),
            "completionTokens": int(value.get("completionTokens", 0)),
            "totalTokens": int(value.get("totalTokens", 0)),
            "usageCalls": int(value.get("usageCalls", 0)),
            "llmCalls": llm_calls,
            "providerRequests": [dict(request) for request in provider_requests if isinstance(request, dict)],
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
        object.__setattr__(self, "_fixture_state", self._get_fixture_state())
        state = self._fixture_state if isinstance(self._fixture_state, dict) else {}
        object.__setattr__(self, "_records", state.get("records", []))

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
        request, call_ref = self._request("chat", messages, tools, kwargs)
        if self._mode == "replay":
            record = self._next_record(request)
            response = _decode_chat_response(record.get("response"))
        else:
            response = await self._provider.chat(messages, tools, **kwargs)
            if self._mode == "record":
                self._append_record(request, _encode_value(response))
        self._count_usage(call_ref, _usage_from_chat_response(response))
        return response

    async def chat_stream(self, messages: list[dict[str, Any]], **kwargs: Any) -> AsyncIterator[str]:
        request, _call_ref = self._request("chat_stream", messages, None, kwargs)
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
        request, call_ref = self._request("chat_stream_with_tools", messages, tools, kwargs)
        if self._mode == "replay":
            record = self._next_record(request)
            values = [_sanitize(chunk) for chunk in record.get("response") or []]
            for value in _collapse_stream_usage(values):
                self._count_usage(call_ref, _usage_from_stream_chunk(value))
                yield value
            return

        chunks: list[dict[str, Any]] = []
        latest_usage: dict[str, int] | None = None
        async for chunk in self._provider.chat_stream_with_tools(messages, tools, **kwargs):
            value = _sanitize(chunk)
            usage = _usage_from_stream_chunk(value)
            if usage is not None:
                latest_usage = _prefer_usage_snapshot(latest_usage, usage)
                continue
            chunks.append(value)
            yield chunk
        if latest_usage is not None:
            usage_chunk = {"type": "usage", "data": latest_usage}
            chunks.append(usage_chunk)
            self._count_usage(call_ref, latest_usage)
            yield usage_chunk
        if self._mode == "record":
            self._append_record(request, chunks)

    def assert_replay_complete(self) -> None:
        state = self._fixture_state if isinstance(self._fixture_state, dict) else {}
        consumed = int(state.get("cursor", 0))
        if self._mode == "replay" and consumed != len(self._records):
            raise ReplayMismatch(
                f"Replay has {len(self._records) - consumed} unused record(s): "
                f"consumed={consumed}, total={len(self._records)}"
            )

    def _request(
        self,
        method: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        kwargs: dict[str, Any],
    ) -> tuple[dict[str, Any], tuple[str, str, int]]:
        normalized_tools = _sanitize(tools or [])
        tools_json = _stable_json(normalized_tools)
        request = {
            "method": method,
            "model": str(getattr(self._provider, "model", "") or ""),
            "messages": _sanitize_messages(messages),
            "tools_digest": hashlib.sha256(tools_json.encode("utf-8")).hexdigest(),
            "kwargs": _sanitize(kwargs),
        }
        request_hash = _request_hash(request)
        call_ref = self._count_call(
            method,
            request=request,
            normalized_tools=normalized_tools,
            request_hash=request_hash,
        )
        return request, call_ref

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

    def _get_fixture_state(self) -> dict[str, Any] | None:
        path = self._fixture_path
        if self._mode == "off" or path is None:
            return None
        resolved_path = str(path.resolve())
        key = (self._mode, resolved_path)
        with _FIXTURE_LOCK:
            state = _FIXTURE_STATES.get(key)
            if state is not None:
                return state
            if self._mode == "replay":
                state = {"records": self._load_records(), "cursor": 0}
            else:
                existing_count = 0
                if path.is_file():
                    existing_count = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
                state = {"nextSeq": existing_count}
            _FIXTURE_STATES[key] = state
            return state

    def _append_record(self, request: dict[str, Any], response: Any) -> None:
        path = self._fixture_path
        if path is None:
            raise ReplayError("Record fixture path is not configured")
        path.parent.mkdir(parents=True, exist_ok=True)
        state = self._fixture_state if isinstance(self._fixture_state, dict) else {}
        with _FIXTURE_LOCK:
            sequence = int(state.get("nextSeq", 0)) + 1
            state["nextSeq"] = sequence
            self._sequence += 1
            record = {
                "seq": sequence,
                "request": request,
                "request_hash": _request_hash(request),
                "response": response,
            }
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(_stable_json(record) + "\n")

    def _next_record(self, request: dict[str, Any]) -> dict[str, Any]:
        state = self._fixture_state if isinstance(self._fixture_state, dict) else {}
        with _FIXTURE_LOCK:
            cursor = int(state.get("cursor", 0))
            if cursor >= len(self._records):
                raise ReplayMismatch(
                    f"Replay fixture exhausted before request #{cursor + 1}: "
                    f"{request.get('method')}"
                )
            record = self._records[cursor]
            expected = record.get("request") if isinstance(record.get("request"), dict) else {}
            if _request_hash(expected) != _request_hash(request):
                diff = _diff_values(_sanitize_request(expected), _sanitize_request(request))
                raise ReplayMismatch(
                    f"Replay request #{cursor + 1} mismatch:\n" + "\n".join(diff[:30])
                )
            state["cursor"] = cursor + 1
            self._sequence += 1
        return record

    def _count_call(
        self,
        method: str,
        *,
        request: dict[str, Any],
        normalized_tools: list[Any],
        request_hash: str,
    ) -> tuple[str, str, int]:
        trace_id = str(_TRACE_ID.get() or "default")
        purpose = str(_PURPOSE.get() or "unknown")
        group_key = f"{purpose}\x00{method}"
        with _COUNTERS_LOCK:
            counter = _COUNTERS.setdefault(trace_id, {"calls": 0, "byMethod": {}, "callGroups": {}})
            counter["calls"] = int(counter.get("calls", 0)) + 1
            by_method = counter.setdefault("byMethod", {})
            by_method[method] = int(by_method.get(method, 0)) + 1
            call_groups = counter.setdefault("callGroups", {})
            group = call_groups.setdefault(
                group_key,
                {
                    "purpose": purpose,
                    "method": method,
                    "count": 0,
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "usageCalls": 0,
                },
            )
            group["count"] = int(group.get("count", 0)) + 1
            provider_requests = counter.setdefault("providerRequests", [])
            request_index = len(provider_requests)
            provider_requests.append({"index": request_index})

        from services.context_trace_service import capture_provider_request

        context_assembly = _CONTEXT_ASSEMBLY.get() if purpose in {"chat", "loop"} else None
        request_record = capture_provider_request(
            context_assembly if isinstance(context_assembly, dict) else None,
            request_index=request_index,
            purpose=purpose,
            method=method,
            messages=request.get("messages") if isinstance(request.get("messages"), list) else [],
            tools=normalized_tools,
            kwargs=request.get("kwargs") if isinstance(request.get("kwargs"), dict) else {},
            request_hash=request_hash,
        )
        with _COUNTERS_LOCK:
            counter = _COUNTERS.get(trace_id, {})
            provider_requests = (
                counter.get("providerRequests")
                if isinstance(counter.get("providerRequests"), list)
                else []
            )
            if request_index < len(provider_requests) and isinstance(provider_requests[request_index], dict):
                provider_requests[request_index].update(request_record)
        return trace_id, group_key, request_index

    def _count_usage(self, call_ref: tuple[str, str, int], usage: dict[str, int] | None) -> None:
        normalized_usage = _normalize_usage_snapshot(usage)
        if not normalized_usage:
            return
        trace_id, group_key, request_index = call_ref
        with _COUNTERS_LOCK:
            counter = _COUNTERS.setdefault(trace_id, {"calls": 0, "byMethod": {}, "callGroups": {}})
            prompt_tokens = int(normalized_usage.get("prompt_tokens", 0))
            completion_tokens = int(normalized_usage.get("completion_tokens", 0))
            total_tokens = int(normalized_usage.get("total_tokens", prompt_tokens + completion_tokens))
            counter["promptTokens"] = int(counter.get("promptTokens", 0)) + prompt_tokens
            counter["completionTokens"] = int(counter.get("completionTokens", 0)) + int(
                completion_tokens
            )
            counter["totalTokens"] = int(counter.get("totalTokens", 0)) + total_tokens
            counter["usageCalls"] = int(counter.get("usageCalls", 0)) + 1
            group = counter.setdefault("callGroups", {}).get(group_key)
            if isinstance(group, dict):
                group["inputTokens"] = int(group.get("inputTokens", 0)) + prompt_tokens
                group["outputTokens"] = int(group.get("outputTokens", 0)) + completion_tokens
                group["usageCalls"] = int(group.get("usageCalls", 0)) + 1
            provider_requests = (
                counter.get("providerRequests")
                if isinstance(counter.get("providerRequests"), list)
                else []
            )
            if request_index < len(provider_requests) and isinstance(provider_requests[request_index], dict):
                request_record = provider_requests[request_index]
                request_est_tokens = int(request_record.get("requestEstTokens") or 0)
                estimate_error_tokens = request_est_tokens - prompt_tokens
                request_record.update(
                    {
                        "inputTokens": prompt_tokens,
                        "outputTokens": completion_tokens,
                        "totalTokens": total_tokens,
                        "estimateErrorTokens": estimate_error_tokens,
                        "estimateErrorPct": (
                            round((estimate_error_tokens / prompt_tokens) * 100, 4)
                            if prompt_tokens
                            else None
                        ),
                    }
                )


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_replay_tool_content(value: str, *, tool_name: str = "") -> str:
    normalized = _COOMI_SESSION_PATH_RE.sub(r"\1<session-id>", str(value or ""))
    normalized = _VOLATILE_TIMESTAMP_FIELD_RE.sub(r"\1<timestamp>\3", normalized)
    normalized = _normalize_chapter_progress_manifest_hash(normalized)
    if str(tool_name or "").strip().lower() == "glob":
        normalized = _normalize_glob_path_result(normalized)
    return normalized


def _normalize_chapter_progress_manifest_hash(value: str) -> str:
    """Ignore only the redundant digest in a WIKI source-manifest entry.

    ``chapter-progress.json`` carries a volatile ``updatedAt`` even when its
    chapter data is unchanged. The WIKI index hashes that serialized file, so
    the derived digest changes between equivalent runs. Direct reads of the
    progress file, its manifest fields other than ``sha256``, and all other
    source digests remain byte-for-byte replay inputs.
    """

    text = str(value or "")
    pieces: list[str] = []
    cursor = 0
    search_from = 0
    while True:
        match = _CHAPTER_PROGRESS_MANIFEST_ENTRY_RE.search(text, search_from)
        if match is None:
            break
        object_start = text.find("{", match.start(), match.end())
        object_end = _matching_object_end(text, object_start)
        if object_start < 0 or object_end < 0:
            search_from = match.end()
            continue
        body = text[object_start : object_end + 1]
        normalized_body, replacements = _SHA256_FIELD_RE.subn(
            r"\1<metadata-hash>\2",
            body,
            count=1,
        )
        if replacements:
            pieces.extend((text[cursor:object_start], normalized_body))
            cursor = object_end + 1
        search_from = object_end + 1
    if not pieces:
        return text
    pieces.append(text[cursor:])
    return "".join(pieces)


def _matching_object_end(value: str, object_start: int) -> int:
    if object_start < 0 or object_start >= len(value) or value[object_start] != "{":
        return -1
    depth = 0
    in_string = False
    escaped = False
    for index in range(object_start, len(value)):
        char = value[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _normalize_glob_path_result(value: str) -> str:
    lines = str(value or "").splitlines()
    if len(lines) < 2 or any(not _ABSOLUTE_PATH_LINE_RE.match(line.strip()) for line in lines):
        return value
    return "\n".join(sorted(lines, key=lambda line: line.replace("\\", "/").casefold()))


def _sanitize_messages(messages: Any) -> list[Any]:
    rows = list(messages) if isinstance(messages, (list, tuple)) else []
    tool_names: dict[str, str] = {}
    for raw_message in rows:
        if not isinstance(raw_message, dict):
            continue
        tool_calls = raw_message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for raw_call in tool_calls:
            call = raw_call if isinstance(raw_call, dict) else {}
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            call_id = str(call.get("id") or "")
            if call_id:
                tool_names[call_id] = str(function.get("name") or "")

    normalized: list[Any] = []
    for raw_message in rows:
        message = _sanitize(raw_message)
        if not isinstance(raw_message, dict) or not isinstance(message, dict):
            normalized.append(message)
            continue
        if str(raw_message.get("role") or "").strip().lower() != "tool":
            normalized.append(message)
            continue
        content = raw_message.get("content")
        if isinstance(content, str):
            call_id = str(raw_message.get("tool_call_id") or "")
            message["content"] = normalize_replay_tool_content(content, tool_name=tool_names.get(call_id, ""))
        normalized.append(message)
    return normalized


def _sanitize_request(value: Any) -> Any:
    normalized = _sanitize(value)
    if isinstance(value, dict) and isinstance(normalized, dict) and isinstance(value.get("messages"), list):
        normalized["messages"] = _sanitize_messages(value["messages"])
    return normalized


def _request_hash(request: dict[str, Any]) -> str:
    return hashlib.sha256(_stable_json(_sanitize_request(request)).encode("utf-8")).hexdigest()


def _sanitize(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        is_tool_message = str(value.get("role") or "").strip().lower() == "tool"
        for key, item in value.items():
            normalized_key = str(key)
            if normalized_key.lower().replace("-", "_") in _SENSITIVE_KEYS:
                continue
            if normalized_key.lower() == "arguments" and isinstance(item, str):
                result[normalized_key] = _normalize_json_arguments(item)
            elif normalized_key.lower() == "content" and is_tool_message and isinstance(item, str):
                result[normalized_key] = normalize_replay_tool_content(item)
            else:
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


def _normalize_json_arguments(value: str) -> str:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value
    if not isinstance(parsed, (dict, list)):
        return value
    return _stable_json(_sanitize(parsed))


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
    return _normalize_usage_snapshot(usage)


def _normalize_usage_snapshot(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    usage: dict[str, int] = {}
    aliases = {
        "prompt_tokens": ("prompt_tokens", "promptTokens", "input_tokens", "inputTokens"),
        "completion_tokens": ("completion_tokens", "completionTokens", "output_tokens", "outputTokens"),
        "total_tokens": ("total_tokens", "totalTokens"),
    }
    for key, candidates in aliases.items():
        raw = next((value.get(candidate) for candidate in candidates if candidate in value), None)
        if isinstance(raw, bool):
            continue
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            continue
        usage[key] = max(0, parsed)
    if "total_tokens" not in usage and "prompt_tokens" in usage and "completion_tokens" in usage:
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return usage or None


def _prefer_usage_snapshot(
    current: dict[str, int] | None,
    candidate: dict[str, int],
) -> dict[str, int]:
    if current is None:
        return dict(candidate)

    def rank(usage: dict[str, int]) -> tuple[int, int, int]:
        prompt = int(usage.get("prompt_tokens", 0))
        completion = int(usage.get("completion_tokens", 0))
        total = int(usage.get("total_tokens", prompt + completion))
        return total, completion, prompt

    return dict(candidate) if rank(candidate) >= rank(current) else current


def _collapse_stream_usage(chunks: list[Any]) -> list[Any]:
    normalized: list[Any] = []
    latest_usage: dict[str, int] | None = None
    for chunk in chunks:
        usage = _usage_from_stream_chunk(chunk)
        if usage is not None:
            latest_usage = _prefer_usage_snapshot(latest_usage, usage)
            continue
        normalized.append(chunk)
    if latest_usage is not None:
        normalized.append({"type": "usage", "data": latest_usage})
    return normalized


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
