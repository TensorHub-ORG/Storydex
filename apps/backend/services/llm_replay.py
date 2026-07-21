from __future__ import annotations

import asyncio
import contextvars
import hashlib
import importlib.metadata
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable, Iterator


_MODE_ENV = "STORYDEX_LLM_MODE"
_FIXTURE_DIR_ENV = "STORYDEX_LLM_FIXTURE_DIR"
_FIXTURE_FILE = "calls.jsonl"
_EXTERNAL_TOOL_FIXTURE_FILE = "external-tools.jsonl"
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
_EXTERNAL_TOOL_FIXTURE_LOCK = threading.Lock()
_EXTERNAL_TOOL_FIXTURE_STATES: dict[tuple[str, str], dict[str, Any]] = {}
_KNOWN_PURPOSES = {"intent", "plan", "commit", "memory_recall", "chat", "loop"}
_OPENAI_SDK_USER_AGENT_RE = re.compile(r"^(?:Async)?OpenAI/Python(?:\s|/|$)", re.IGNORECASE)
_COOMI_SESSION_PATH_RE = re.compile(
    r"(?i)([\\/]\.coomi[\\/]sessions[\\/])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_STORYDEX_RUNTIME_LOG_PATH_RE = re.compile(
    r"(?i)((?:[\\/]|(?<![A-Za-z0-9_.-]))\.storydex[\\/]logs[\\/])"
    r"\d{4}-\d{4}-\d{2}-\d{2}-\d{2}\.jsonl"
)
_STORYDEX_AGENT_SESSION_PATH_RE = re.compile(
    r"(?i)([\\/]\.storydex[\\/]\.agent[\\/]sessions[\\/][^\\/\r\n:]+[\\/])"
    r"\d{8}T\d{6}Z_[^\\/\r\n:]+\.json"
)
# Coomi derives these IDs from UUIDs; normalize values while preserving references.
_TEXT_FALLBACK_CALL_ID_RE = re.compile(r"(?i)\btext_call_[0-9a-f]{12}\b")
_WINDOWS_FIND_DIAGNOSTIC_RE = re.compile(
    r"(?im)^FIND: Parameter format not correct\r?\n?"
)
_VOLATILE_TIMESTAMP_FIELD_RE = re.compile(
    r'("(?:createdAt|updatedAt|generatedAt|lastAnalyzedAt|mtime|timestamp)"\s*:\s*")([^"]*)(")'
)
_COOMI_SYSTEM_DATE_LINE_RE = re.compile(r"(?m)^- Date: \d{4}-\d{2}-\d{2}$")
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
        else:
            fixture_path = (Path(fixture_dir) / _FIXTURE_FILE).resolve()
            fixture_key = str(fixture_path)
            for key in [key for key in _FIXTURE_STATES if key[1] == fixture_key]:
                _FIXTURE_STATES.pop(key, None)
    with _EXTERNAL_TOOL_FIXTURE_LOCK:
        if fixture_dir is None:
            _EXTERNAL_TOOL_FIXTURE_STATES.clear()
        else:
            fixture_path = (Path(fixture_dir) / _EXTERNAL_TOOL_FIXTURE_FILE).resolve()
            fixture_key = str(fixture_path)
            for key in [key for key in _EXTERNAL_TOOL_FIXTURE_STATES if key[1] == fixture_key]:
                _EXTERNAL_TOOL_FIXTURE_STATES.pop(key, None)


def replayable_external_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    live_call: Callable[[], Any],
) -> Any:
    mode = str(os.getenv(_MODE_ENV, "off") or "off").strip().lower()
    if mode == "off":
        return live_call()
    if mode not in {"record", "replay"}:
        raise ReplayError(f"Unsupported {_MODE_ENV} value: {mode!r}")
    fixture_dir = str(os.getenv(_FIXTURE_DIR_ENV, "") or "").strip()
    if not fixture_dir:
        raise ReplayError(f"{_FIXTURE_DIR_ENV} is required when {_MODE_ENV}={mode}")
    fixture_path = Path(fixture_dir) / _EXTERNAL_TOOL_FIXTURE_FILE
    state = _get_external_tool_fixture_state(mode, fixture_path)
    request = {
        "tool": str(tool_name or ""),
        "arguments": _sanitize(arguments),
    }
    if mode == "replay":
        record = _next_external_tool_record(state, request)
        return _decode_external_tool_result(record.get("result"))

    with _EXTERNAL_TOOL_FIXTURE_LOCK:
        sequence = int(state.get("nextSeq", 0)) + 1
        state["nextSeq"] = sequence
    result = live_call()
    record = {
        "seq": sequence,
        "request": request,
        "request_hash": _external_tool_request_hash(request),
        "result": _encode_external_tool_result(result),
    }
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    with _EXTERNAL_TOOL_FIXTURE_LOCK:
        with fixture_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(_stable_json(record) + "\n")
    return result


def truncate_external_tool_fixture(fixture_dir: str | Path, expected_rows: int) -> None:
    path = Path(fixture_dir) / _EXTERNAL_TOOL_FIXTURE_FILE
    expected = max(0, int(expected_rows))
    if not path.is_file():
        if expected:
            raise ReplayError(
                f"External tool fixture is missing {expected} completed record(s): {path}"
            )
        reset_llm_fixture_state(fixture_dir)
        return
    records = _load_external_tool_records(path)
    if len(records) < expected:
        raise ReplayError(
            f"External tool fixture is shorter than its atomic checkpoint: "
            f"rows={len(records)}, expected={expected}"
        )
    retained = records[:expected]
    if retained:
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write("".join(_stable_json(record) + "\n" for record in retained))
    else:
        path.unlink(missing_ok=True)
    reset_llm_fixture_state(fixture_dir)


def _get_external_tool_fixture_state(mode: str, path: Path) -> dict[str, Any]:
    resolved_path = str(path.resolve())
    key = (mode, resolved_path)
    with _EXTERNAL_TOOL_FIXTURE_LOCK:
        state = _EXTERNAL_TOOL_FIXTURE_STATES.get(key)
        if state is not None:
            return state
        if mode == "replay":
            state = {"records": _load_external_tool_records(path), "consumed": set()}
        else:
            records = _load_external_tool_records(path) if path.is_file() else []
            state = {"nextSeq": max((int(record.get("seq") or 0) for record in records), default=0)}
        _EXTERNAL_TOOL_FIXTURE_STATES[key] = state
        return state


def _load_external_tool_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ReplayError(f"External tool replay fixture is missing: {path}")
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReplayError(
                f"Invalid external tool replay JSONL at line {line_no}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise ReplayError(f"External tool replay record at line {line_no} must be an object")
        records.append(value)
    return sorted(records, key=lambda record: int(record.get("seq") or 0))


def _next_external_tool_record(
    state: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    with _EXTERNAL_TOOL_FIXTURE_LOCK:
        records = state.get("records") if isinstance(state.get("records"), list) else []
        consumed = state.get("consumed") if isinstance(state.get("consumed"), set) else set()
        request_hash = _external_tool_request_hash(request)
        matching_index = next(
            (
                index
                for index, record in enumerate(records)
                if index not in consumed
                and _external_tool_request_hash(
                    record.get("request") if isinstance(record.get("request"), dict) else {}
                )
                == request_hash
            ),
            None,
        )
        if matching_index is None:
            remaining = [
                record.get("request") if isinstance(record.get("request"), dict) else {}
                for index, record in enumerate(records)
                if index not in consumed
            ]
            if not remaining:
                raise ReplayMismatch(
                    f"External tool replay fixture exhausted before call #{len(consumed) + 1}: "
                    f"{request.get('tool')}"
                )
            expected = remaining[0]
            diff = _diff_values(_sanitize(expected), _sanitize(request))
            raise ReplayMismatch(
                f"External tool replay call #{len(consumed) + 1} mismatch:\n"
                + "\n".join(diff[:30])
            )
        record = records[matching_index]
        consumed.add(matching_index)
        state["consumed"] = consumed
        return record


def _external_tool_request_hash(request: dict[str, Any]) -> str:
    return hashlib.sha256(_stable_json(_sanitize(request)).encode("utf-8")).hexdigest()


def _encode_external_tool_result(result: Any) -> dict[str, Any]:
    return {
        "success": bool(getattr(result, "success", False)),
        "output": str(getattr(result, "output", "") or ""),
        "error": (
            None
            if getattr(result, "error", None) is None
            else str(getattr(result, "error", ""))
        ),
    }


def _decode_external_tool_result(value: Any) -> Any:
    if not isinstance(value, dict):
        raise ReplayError("External tool replay result must be an object")
    from coomi.tools.base import ToolResult

    error = value.get("error")
    return ToolResult(
        success=bool(value.get("success")),
        output=str(value.get("output") or ""),
        error=None if error is None else str(error),
    )


def _assert_external_tool_replay_complete(fixture_dir: Path) -> None:
    path = fixture_dir / _EXTERNAL_TOOL_FIXTURE_FILE
    if not path.is_file():
        return
    state = _get_external_tool_fixture_state("replay", path)
    records = state.get("records") if isinstance(state.get("records"), list) else []
    consumed = state.get("consumed") if isinstance(state.get("consumed"), set) else set()
    if len(consumed) != len(records):
        raise ReplayMismatch(
            f"External tool replay has {len(records) - len(consumed)} unused record(s): "
            f"consumed={len(consumed)}, total={len(records)}"
        )


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
    provider = _apply_storydex_openai_user_agent(provider)
    return ReplayableLLMProvider(provider)


def _apply_storydex_openai_user_agent(provider: Any) -> Any:
    client = getattr(provider, "client", None)
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return provider
    if not isinstance(client, AsyncOpenAI):
        return provider

    headers = getattr(client, "default_headers", {})
    user_agent = str(headers.get("User-Agent") or headers.get("user-agent") or "")
    if not _OPENAI_SDK_USER_AGENT_RE.match(user_agent):
        return provider

    try:
        coomi_version = importlib.metadata.version("coomi-agent")
    except importlib.metadata.PackageNotFoundError:
        coomi_version = "unknown"
    provider.client = client.with_options(
        default_headers={"User-Agent": f"Storydex-Coomi/{coomi_version}"}
    )
    return provider


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
            self._count_usage(
                call_ref,
                _usage_from_chat_response(
                    response,
                    requested_model=str(request.get("model") or ""),
                ),
            )
        else:
            try:
                response = await _call_with_model_catalog_retry(
                    lambda: self._provider.chat(messages, tools, **kwargs),
                    recorder=lambda **details: self._record_provider_attempt(call_ref, **details),
                )
            except Exception:
                self._count_usage(
                    call_ref,
                    _missing_usage(requested_model=str(request.get("model") or "")),
                )
                raise
            normalized_usage = self._count_usage(
                call_ref,
                _usage_from_chat_response(
                    response,
                    requested_model=str(request.get("model") or ""),
                ),
            )
            if self._mode == "record":
                encoded_response = _encode_value(response)
                if isinstance(encoded_response, dict):
                    encoded_response["usage"] = normalized_usage
                self._append_record(request, encoded_response)
        return response

    async def chat_stream(self, messages: list[dict[str, Any]], **kwargs: Any) -> AsyncIterator[str]:
        request, call_ref = self._request("chat_stream", messages, None, kwargs)
        if self._mode == "replay":
            record = self._next_record(request)
            for chunk in record.get("response") or []:
                yield str(chunk)
            self._count_usage(
                call_ref,
                _missing_usage(requested_model=str(request.get("model") or "")),
            )
            return

        chunks: list[str] = []
        try:
            async for chunk in _iterate_with_model_catalog_retry(
                lambda: self._provider.chat_stream(messages, **kwargs),
                recorder=lambda **details: self._record_provider_attempt(call_ref, **details),
            ):
                value = str(chunk)
                chunks.append(value)
                yield value
        except Exception:
            self._count_usage(
                call_ref,
                _missing_usage(requested_model=str(request.get("model") or "")),
            )
            raise
        self._count_usage(
            call_ref,
            _missing_usage(requested_model=str(request.get("model") or "")),
        )
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
            normalized = _collapse_stream_usage(values)
            usage_seen = False
            for value in normalized:
                usage = _usage_from_stream_chunk(value)
                if usage is None:
                    yield value
                    continue
                usage_seen = True
                counted_usage = self._count_usage(call_ref, usage)
                yield {"type": "usage", "data": counted_usage}
            if not usage_seen:
                counted_usage = self._count_usage(
                    call_ref,
                    _missing_usage(requested_model=str(request.get("model") or "")),
                )
                yield {"type": "usage", "data": counted_usage}
            return

        chunks: list[dict[str, Any]] = []
        latest_usage: dict[str, Any] | None = None
        usage_snapshot_count = 0
        try:
            async for chunk in _iterate_with_model_catalog_retry(
                lambda: self._provider.chat_stream_with_tools(messages, tools, **kwargs),
                recorder=lambda **details: self._record_provider_attempt(call_ref, **details),
            ):
                value = _sanitize(chunk)
                usage = _usage_from_stream_chunk(value)
                if usage is not None:
                    latest_usage = _prefer_usage_snapshot(latest_usage, usage)
                    usage_snapshot_count += _usage_snapshot_increment(usage)
                    continue
                chunks.append(value)
                yield chunk
        except Exception:
            failed_usage = latest_usage or _missing_usage(
                requested_model=str(request.get("model") or "")
            )
            self._count_usage(call_ref, failed_usage)
            raise
        if latest_usage is None:
            latest_usage = _missing_usage(requested_model=str(request.get("model") or ""))
        else:
            latest_usage = dict(latest_usage)
            latest_usage["usageSnapshotCount"] = usage_snapshot_count
        counted_usage = self._count_usage(call_ref, latest_usage)
        usage_chunk = {"type": "usage", "data": counted_usage}
        chunks.append(usage_chunk)
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
        if self._mode == "replay" and self._fixture_path is not None:
            _assert_external_tool_replay_complete(self._fixture_path.parent)
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
        for line_no, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
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
                    existing_count = sum(1 for line in path.read_text(encoding="utf-8").split("\n") if line.strip())
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
            provider_requests = counter.setdefault("providerRequests", [])
            previous = provider_requests[-1] if provider_requests and isinstance(provider_requests[-1], dict) else None
            previous_attempts = (
                previous.get("providerAttempts")
                if isinstance(previous, dict) and isinstance(previous.get("providerAttempts"), list)
                else []
            )
            is_provider_retry = bool(
                isinstance(previous, dict)
                and str(previous.get("requestHash") or "") == request_hash
                and str(previous.get("purpose") or "") == purpose
                and str(previous.get("method") or "") == method
                and str(previous.get("usageSource") or "") == "missing"
                and previous_attempts
                and str(previous_attempts[-1].get("outcome") or "") == "error"
            )
            if is_provider_retry:
                request_index = len(provider_requests) - 1
                previous["providerRetryObserved"] = True
            else:
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
                request_index = len(provider_requests)
                provider_requests.append({"index": request_index})

        if is_provider_retry:
            return trace_id, group_key, request_index

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
        request_record["requestedModel"] = str(request.get("model") or "")
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

    def _count_usage(
        self,
        call_ref: tuple[str, str, int],
        usage: dict[str, Any] | None,
    ) -> dict[str, Any]:
        normalized_usage = _normalize_usage_snapshot(usage) or _missing_usage()
        trace_id, group_key, request_index = call_ref
        with _COUNTERS_LOCK:
            counter = _COUNTERS.setdefault(trace_id, {"calls": 0, "byMethod": {}, "callGroups": {}})
            provider_requests = (
                counter.get("providerRequests")
                if isinstance(counter.get("providerRequests"), list)
                else []
            )
            request_record = (
                provider_requests[request_index]
                if request_index < len(provider_requests)
                and isinstance(provider_requests[request_index], dict)
                else None
            )
            if isinstance(request_record, dict):
                if normalized_usage.get("estimatedInputTokens") is None:
                    normalized_usage = dict(normalized_usage)
                    normalized_usage["estimatedInputTokens"] = int(
                        request_record.get("requestEstTokens") or 0
                    )
                    normalized_usage["estimator"] = "storydex:tiktoken:cl100k_base"
                if not str(normalized_usage.get("requestedModel") or ""):
                    normalized_usage = dict(normalized_usage)
                    normalized_usage["requestedModel"] = str(
                        request_record.get("requestedModel") or ""
                    )

            source = str(normalized_usage.get("source") or "legacy_unknown")
            is_reported = source in {"provider_response", "provider_query"}
            reported_input_tokens = normalized_usage.get("inputTokens")
            reported_output_tokens = normalized_usage.get("outputTokens")
            reported_total_tokens = normalized_usage.get("totalTokens")
            prompt_tokens = int(reported_input_tokens or 0)
            completion_tokens = int(reported_output_tokens or 0)
            total_tokens = int(reported_total_tokens or 0)
            if is_reported:
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

            if isinstance(request_record, dict):
                request_record = provider_requests[request_index]
                request_est_tokens = int(request_record.get("requestEstTokens") or 0)
                estimate_error_tokens = (
                    request_est_tokens - prompt_tokens
                    if is_reported and reported_input_tokens is not None
                    else None
                )
                request_record.update(
                    {
                        "usage": dict(normalized_usage),
                        "usageSource": source,
                        "protocol": str(normalized_usage.get("protocol") or "unknown"),
                        "requestId": str(normalized_usage.get("requestId") or ""),
                        "requestedModel": str(normalized_usage.get("requestedModel") or ""),
                        "reportedModel": str(normalized_usage.get("reportedModel") or ""),
                        "providerReportedInputTokens": (
                            reported_input_tokens if is_reported else None
                        ),
                        "providerReportedOutputTokens": (
                            reported_output_tokens if is_reported else None
                        ),
                        "providerReportedTotalTokens": (
                            reported_total_tokens if is_reported else None
                        ),
                        "cacheReadInputTokens": normalized_usage.get("cacheReadInputTokens"),
                        "cacheCreationInputTokens": normalized_usage.get(
                            "cacheCreationInputTokens"
                        ),
                        "reasoningTokens": normalized_usage.get("reasoningTokens"),
                        "estimatedInputTokens": normalized_usage.get("estimatedInputTokens"),
                        "estimator": str(normalized_usage.get("estimator") or ""),
                        "totalDerived": bool(normalized_usage.get("totalDerived")),
                        "usageSnapshotCount": int(
                            normalized_usage.get("usageSnapshotCount") or 0
                        ),
                        "providerDetails": dict(
                            normalized_usage.get("providerDetails")
                            if isinstance(normalized_usage.get("providerDetails"), dict)
                            else {}
                        ),
                        # Deprecated aliases retained for existing T1 readers.
                        "inputTokens": reported_input_tokens if is_reported else None,
                        "outputTokens": reported_output_tokens if is_reported else None,
                        "totalTokens": reported_total_tokens if is_reported else None,
                        "estimateErrorTokens": estimate_error_tokens,
                        "estimateErrorPct": (
                            round((estimate_error_tokens / prompt_tokens) * 100, 4)
                            if estimate_error_tokens is not None and prompt_tokens
                            else None
                        ),
                    }
                )
        return normalized_usage

    @staticmethod
    def _record_provider_attempt(
        call_ref: tuple[str, str, int],
        *,
        attempt: int,
        outcome: str,
        elapsed_ms: float,
        error: Exception | None = None,
        retry_scheduled: bool = False,
        emitted_output: bool = False,
    ) -> None:
        trace_id, _group_key, request_index = call_ref
        with _COUNTERS_LOCK:
            counter = _COUNTERS.get(trace_id, {})
            provider_requests = (
                counter.get("providerRequests")
                if isinstance(counter.get("providerRequests"), list)
                else []
            )
            if request_index >= len(provider_requests) or not isinstance(provider_requests[request_index], dict):
                return
            request = provider_requests[request_index]
            attempts = request.setdefault("providerAttempts", [])
            if not isinstance(attempts, list):
                attempts = []
                request["providerAttempts"] = attempts
            message = str(error or "")
            attempt_number = len(attempts) + 1
            attempts.append(
                {
                    "attempt": attempt_number,
                    "outcome": str(outcome or "error"),
                    "elapsedMs": round(max(0.0, float(elapsed_ms or 0.0)), 3),
                    "errorType": type(error).__name__ if error is not None else "",
                    "errorMessageHash": hashlib.sha256(message.encode("utf-8")).hexdigest() if message else "",
                    "modelCatalogMismatch": bool(error is not None and _is_model_catalog_mismatch(error)),
                    "retryScheduled": bool(retry_scheduled),
                    "emittedOutput": bool(emitted_output),
                }
            )
            request["providerAttemptCount"] = len(attempts)
            request["providerRetryCount"] = max(0, len(attempts) - 1)


async def _call_with_model_catalog_retry(factory: Any, *, recorder: Any = None) -> Any:
    for attempt in range(1, 3):
        started = time.perf_counter()
        try:
            response = await factory()
        except Exception as exc:
            retry_scheduled = attempt == 1 and _is_model_catalog_mismatch(exc)
            if callable(recorder):
                recorder(
                    attempt=attempt,
                    outcome="error",
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    error=exc,
                    retry_scheduled=retry_scheduled,
                    emitted_output=False,
                )
            if not retry_scheduled:
                raise
            await asyncio.sleep(0.65)
            continue
        if callable(recorder):
            recorder(
                attempt=attempt,
                outcome="success",
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
        return response
    raise RuntimeError("model catalog retry exhausted")


async def _iterate_with_model_catalog_retry(factory: Any, *, recorder: Any = None) -> AsyncIterator[Any]:
    for attempt in range(1, 3):
        started = time.perf_counter()
        emitted = False
        try:
            async for item in factory():
                emitted = True
                yield item
            if callable(recorder):
                recorder(
                    attempt=attempt,
                    outcome="success",
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    emitted_output=emitted,
                )
            return
        except Exception as exc:
            retry_scheduled = attempt == 1 and not emitted and _is_model_catalog_mismatch(exc)
            if callable(recorder):
                recorder(
                    attempt=attempt,
                    outcome="error",
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    error=exc,
                    retry_scheduled=retry_scheduled,
                    emitted_output=emitted,
                )
            if not retry_scheduled:
                raise
            await asyncio.sleep(0.65)


def _is_model_catalog_mismatch(error: Exception) -> bool:
    message = str(error or "").lower()
    return (
        "model_not_supported" in message
        or "not supported on the lite model list" in message
        or ("model" in message and "use get" in message and "/models" in message)
    )


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_replay_tool_content(value: str, *, tool_name: str = "") -> str:
    normalized = _COOMI_SESSION_PATH_RE.sub(r"\1<session-id>", str(value or ""))
    normalized = _STORYDEX_RUNTIME_LOG_PATH_RE.sub(
        r"\1<runtime-log>.jsonl",
        normalized,
    )
    normalized = _STORYDEX_AGENT_SESSION_PATH_RE.sub(
        r"\1<runtime-trace>.json",
        normalized,
    )
    normalized = _VOLATILE_TIMESTAMP_FIELD_RE.sub(r"\1<timestamp>\3", normalized)
    normalized = _normalize_chapter_progress_manifest_hash(normalized)
    normalized_tool_name = str(tool_name or "").strip().lower()
    if normalized_tool_name == "bash":
        normalized = _WINDOWS_FIND_DIAGNOSTIC_RE.sub("", normalized)
    if normalized_tool_name == "glob":
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


def _normalize_text_fallback_call_ids(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            raw_id = match.group(0).casefold()
            return replacements.setdefault(
                raw_id,
                f"text_call_<volatile-{len(replacements) + 1}>",
            )

        return _TEXT_FALLBACK_CALL_ID_RE.sub(replace, value)
    if isinstance(value, dict):
        return {
            key: _normalize_text_fallback_call_ids(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_text_fallback_call_ids(item, replacements) for item in value]
    return value


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
    fallback_id_replacements: dict[str, str] = {}
    for raw_message in rows:
        message = _sanitize(raw_message)
        if not isinstance(raw_message, dict) or not isinstance(message, dict):
            normalized.append(
                _normalize_text_fallback_call_ids(message, fallback_id_replacements)
            )
            continue
        if str(raw_message.get("role") or "").strip().lower() == "tool":
            content = raw_message.get("content")
            if isinstance(content, str):
                call_id = str(raw_message.get("tool_call_id") or "")
                message["content"] = normalize_replay_tool_content(
                    content,
                    tool_name=tool_names.get(call_id, ""),
                )
        elif str(raw_message.get("role") or "").strip().lower() == "system":
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = _COOMI_SYSTEM_DATE_LINE_RE.sub("- Date: <date>", content)
        normalized.append(
            _normalize_text_fallback_call_ids(message, fallback_id_replacements)
        )
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


def _usage_from_chat_response(
    response: Any,
    *,
    requested_model: str = "",
) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    normalized = _normalize_usage_snapshot(usage)
    if normalized is None:
        return _missing_usage(requested_model=requested_model)
    if not str(normalized.get("requestedModel") or ""):
        normalized["requestedModel"] = str(requested_model or "")
    return normalized


def _usage_from_stream_chunk(chunk: Any) -> dict[str, Any] | None:
    if not isinstance(chunk, dict) or chunk.get("type") != "usage":
        return None
    usage = chunk.get("data")
    return _normalize_usage_snapshot(usage)


def _normalize_usage_snapshot(value: Any) -> dict[str, Any] | None:
    payload = _sanitize(value)
    if not isinstance(payload, dict):
        return None

    raw_source = str(payload.get("source") or "legacy_unknown").strip().lower()
    source = (
        raw_source
        if raw_source
        in {"provider_response", "provider_query", "missing", "legacy_unknown"}
        else "legacy_unknown"
    )
    raw_protocol = str(payload.get("protocol") or "unknown").strip().lower()
    protocol = (
        raw_protocol
        if raw_protocol in {"openai_chat", "anthropic_messages", "unknown"}
        else "unknown"
    )
    input_tokens = _usage_int(
        payload,
        "inputTokens",
        "input_tokens",
        "promptTokens",
        "prompt_tokens",
    )
    output_tokens = _usage_int(
        payload,
        "outputTokens",
        "output_tokens",
        "completionTokens",
        "completion_tokens",
    )
    total_tokens = _usage_int(payload, "totalTokens", "total_tokens")
    cache_read_input_tokens = _usage_int(
        payload,
        "cacheReadInputTokens",
        "cache_read_input_tokens",
    )
    cache_creation_input_tokens = _usage_int(
        payload,
        "cacheCreationInputTokens",
        "cache_creation_input_tokens",
    )
    reasoning_tokens = _usage_int(payload, "reasoningTokens", "reasoning_tokens")
    total_derived = bool(payload.get("totalDerived") or payload.get("total_derived"))
    if source == "missing":
        input_tokens = None
        output_tokens = None
        total_tokens = None
        cache_read_input_tokens = None
        cache_creation_input_tokens = None
        reasoning_tokens = None
        total_derived = False
    elif total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
        total_derived = True

    provider_details = payload.get("providerDetails", payload.get("provider_details"))
    canonical: dict[str, Any] = {
        "_type": "LLMUsage",
        "_version": 1,
        "source": source,
        "protocol": protocol,
        "requestId": str(payload.get("requestId") or payload.get("request_id") or ""),
        "requestedModel": str(
            payload.get("requestedModel") or payload.get("requested_model") or ""
        ),
        "reportedModel": str(
            payload.get("reportedModel") or payload.get("reported_model") or ""
        ),
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": total_tokens,
        "cacheReadInputTokens": cache_read_input_tokens,
        "cacheCreationInputTokens": cache_creation_input_tokens,
        "reasoningTokens": reasoning_tokens,
        "estimatedInputTokens": _usage_int(
            payload,
            "estimatedInputTokens",
            "estimated_input_tokens",
        ),
        "estimator": str(payload.get("estimator") or ""),
        "totalDerived": total_derived,
        "usageSnapshotCount": _usage_int(
            payload,
            "usageSnapshotCount",
            "usage_snapshot_count",
        ),
        "providerDetails": (
            dict(provider_details) if isinstance(provider_details, dict) else {}
        ),
    }
    if canonical["usageSnapshotCount"] is None:
        canonical["usageSnapshotCount"] = 0 if source == "missing" else 1
    # Coomi <= 1.1.5 reads these aliases. Omit null aliases so legacy addition stays safe.
    if input_tokens is not None:
        canonical["prompt_tokens"] = input_tokens
    if output_tokens is not None:
        canonical["completion_tokens"] = output_tokens
    if total_tokens is not None:
        canonical["total_tokens"] = total_tokens
    return canonical


def _missing_usage(
    *,
    requested_model: str = "",
    estimated_input_tokens: int | None = None,
    estimator: str = "",
) -> dict[str, Any]:
    return {
        "_type": "LLMUsage",
        "_version": 1,
        "source": "missing",
        "protocol": "unknown",
        "requestId": "",
        "requestedModel": str(requested_model or ""),
        "reportedModel": "",
        "inputTokens": None,
        "outputTokens": None,
        "totalTokens": None,
        "cacheReadInputTokens": None,
        "cacheCreationInputTokens": None,
        "reasoningTokens": None,
        "estimatedInputTokens": estimated_input_tokens,
        "estimator": str(estimator or "") if estimated_input_tokens is not None else "",
        "totalDerived": False,
        "usageSnapshotCount": 0,
        "providerDetails": {},
    }


def _usage_int(value: dict[str, Any], *names: str) -> int | None:
    for name in names:
        if name not in value or isinstance(value.get(name), bool):
            continue
        try:
            parsed = int(value.get(name))
        except (TypeError, ValueError, OverflowError):
            continue
        if parsed >= 0:
            return parsed
    return None


def _prefer_usage_snapshot(
    current: dict[str, Any] | None,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    if current is None:
        return dict(candidate)

    def rank(usage: dict[str, Any]) -> tuple[int, int, int, int]:
        source_rank = {
            "missing": 0,
            "legacy_unknown": 1,
            "provider_response": 2,
            "provider_query": 3,
        }.get(str(usage.get("source") or "legacy_unknown"), 0)
        prompt = int(usage.get("inputTokens") or 0)
        completion = int(usage.get("outputTokens") or 0)
        total = int(usage.get("totalTokens") or prompt + completion)
        return source_rank, total, completion, prompt

    return dict(candidate) if rank(candidate) >= rank(current) else current


def _usage_snapshot_increment(usage: dict[str, Any]) -> int:
    count = int(usage.get("usageSnapshotCount") or 0)
    if count > 0:
        return count
    return 0 if str(usage.get("source") or "") == "missing" else 1


def _collapse_stream_usage(chunks: list[Any]) -> list[Any]:
    normalized: list[Any] = []
    latest_usage: dict[str, Any] | None = None
    usage_snapshot_count = 0
    for chunk in chunks:
        usage = _usage_from_stream_chunk(chunk)
        if usage is not None:
            latest_usage = _prefer_usage_snapshot(latest_usage, usage)
            usage_snapshot_count += _usage_snapshot_increment(usage)
            continue
        normalized.append(chunk)
    if latest_usage is not None:
        latest_usage = dict(latest_usage)
        latest_usage["usageSnapshotCount"] = usage_snapshot_count
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
