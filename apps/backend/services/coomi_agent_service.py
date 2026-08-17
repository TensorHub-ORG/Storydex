from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import math
import os
import re
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Iterator
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx

from services.agent_capability_policy import (
    FULL_ACCESS,
    READ_ONLY,
    SCOPED_WRITE,
    WORKSPACE_WRITE,
    AgentCapabilityPolicy,
    resolve_capability_policy,
)
from services.coomi_bridge_client import (
    STORYDEX_COOMI_CONFIG,
    STORYDEX_COOMI_HOME,
    STORYDEX_COOMI_RUNTIME_VERSION,
    CoomiBridgeError,
    LiveBridgeProcess,
    bridge_command,
    get_bridge_provider,
    request_status_sync,
)
from services.context_policy import ContextPolicy, context_policy_from_turn_contract
from services.story_project_service import DEFAULT_CHAPTER_WORD_COUNT_TARGET
from services.story_word_count_service import (
    chapter_length_tier_prompt,
    normalize_chapter_length_tier,
)
from services.source_contract import normalize_source_path, validate_source_revision
from services.storydex_tool_types import StorydexToolRegistry, ToolAccess


logger = logging.getLogger(__name__)


STORYDEX_COOMI_SESSIONS = STORYDEX_COOMI_HOME / "sessions"
DEFAULT_CONTEXT_WINDOW = 256_000
MIN_CONTEXT_WINDOW = 8_000
MAX_CONTEXT_WINDOW = 4_000_000
CONTEXT_WINDOW_KEYS = ("context_window", "contextWindow", "max_context_tokens", "maxContextTokens")
COMPACT_THRESHOLD_RATIO = 0.9
WARNING_THRESHOLD_RATIO = 0.6
_SEMANTIC_STORY_TOOL_NAME = "submit_story_scene"
_SEMANTIC_STORY_PARAGRAPH_IDEAL_CHARS = 100
_SEMANTIC_STORY_PARAGRAPH_MINIMUM = 3
_SEMANTIC_STORY_PARAGRAPH_MAXIMUM = 14
_SEMANTIC_STORY_LENGTH_MIN_RATIO = 0.75
_SEMANTIC_STORY_LENGTH_MAX_RATIO = 1.20
_BRIDGE_STATUS_CACHE_LOCK = threading.Lock()
_BRIDGE_STATUS_PROBE_LOCK = threading.Lock()
_BRIDGE_STATUS_CACHE: tuple[tuple[str, int, int, str, int, int], Dict[str, Any]] | None = None
_PROVIDER_REPLAY_FIXTURE_ENV = "STORYDEX_AGENT_PROVIDER_REPLAY_FIXTURE"


class StorydexCoomiUnavailable(RuntimeError):
    pass


class StorydexCoomiEmptyResponse(RuntimeError):
    pass


class StorydexCoomiSessionRestoreError(RuntimeError):
    pass


class StorydexToolCallRejected(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = str(reason)


def _coomi_binding_path(workspace_root: Path, storydex_session_id: str) -> Path:
    workspace = Path(workspace_root).resolve()
    normalized = str(storydex_session_id or "default").strip() or "default"
    digest = sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return workspace / ".storydex" / ".agent" / "runtime" / "coomi-sessions" / f"{digest}.json"


def _coomi_usage_ledger_path(workspace_root: Path, storydex_session_id: str) -> Path:
    workspace = Path(workspace_root).resolve()
    normalized = str(storydex_session_id or "default").strip() or "default"
    digest = sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return workspace / ".storydex" / ".agent" / "runtime" / "coomi-usage" / f"{digest}.json"


def _read_coomi_usage_ledger(
    *, workspace_root: Path, storydex_session_id: str
) -> Dict[str, Any]:
    path = _coomi_usage_ledger_path(workspace_root, storydex_session_id)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_coomi_usage_ledger(
    *, workspace_root: Path, storydex_session_id: str, value: Dict[str, Any]
) -> None:
    path = _coomi_usage_ledger_path(workspace_root, storydex_session_id)
    payload = {
        "version": 1,
        "workspaceRoot": str(Path(workspace_root).resolve()),
        "storydexSessionId": str(storydex_session_id or "default").strip() or "default",
        **dict(value),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
    )


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _read_coomi_session_binding(*, workspace_root: Path, storydex_session_id: str) -> Dict[str, Any]:
    path = _coomi_binding_path(workspace_root, storydex_session_id)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    workspace = str(Path(workspace_root).resolve())
    session_id = str(storydex_session_id or "default").strip() or "default"
    if value.get("workspaceRoot") != workspace or value.get("storydexSessionId") != session_id:
        return {}
    return value


def _read_coomi_session_binding_for_execution(
    *, workspace_root: Path, storydex_session_id: str
) -> Dict[str, Any]:
    path = _coomi_binding_path(workspace_root, storydex_session_id)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StorydexCoomiSessionRestoreError(
            f"Coomi session binding contains invalid JSON: {path}"
        ) from exc
    except OSError as exc:
        raise StorydexCoomiSessionRestoreError(
            f"Coomi session binding could not be read: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise StorydexCoomiSessionRestoreError(
            f"Coomi session binding must be a JSON object: {path}"
        )
    workspace = str(Path(workspace_root).resolve())
    session_id = str(storydex_session_id or "default").strip() or "default"
    if value.get("workspaceRoot") != workspace or value.get("storydexSessionId") != session_id:
        raise StorydexCoomiSessionRestoreError(
            "Coomi session binding does not belong to the active workspace/session"
        )
    runtime_id = str(value.get("runtimeSessionId") or value.get("coomiSessionId") or "").strip()
    try:
        UUID(runtime_id)
    except (ValueError, AttributeError) as exc:
        raise StorydexCoomiSessionRestoreError(
            "Coomi session binding has an invalid runtime session id"
        ) from exc
    expected_path = (STORYDEX_COOMI_SESSIONS / f"{runtime_id}.json").resolve()
    try:
        bound_path = _validated_session_path(value) or expected_path
    except ValueError as exc:
        raise StorydexCoomiSessionRestoreError(
            "Coomi session binding points outside the runtime session directory"
        ) from exc
    if bound_path != expected_path:
        raise StorydexCoomiSessionRestoreError(
            "Coomi session binding points to a different runtime session file"
        )
    if not bound_path.is_file():
        raise StorydexCoomiSessionRestoreError(
            f"Bound Coomi session history is missing: {bound_path}"
        )
    return value


def _validated_persisted_session_bound(data: Dict[str, Any]) -> str:
    runtime_id = str(data.get("runtimeSessionId") or "").strip()
    try:
        UUID(runtime_id)
    except (ValueError, AttributeError) as exc:
        raise StorydexCoomiSessionRestoreError(
            "Coomi session_bound event has an invalid runtime session id"
        ) from exc
    if data.get("persisted") is not True:
        raise StorydexCoomiSessionRestoreError(
            "Coomi session_bound event was not persisted"
        )
    if int(data.get("sessionSchemaVersion") or 0) != 1:
        raise StorydexCoomiSessionRestoreError(
            "Coomi session_bound event has an unsupported session schema version"
        )
    raw_path = str(data.get("sessionPath") or "").strip()
    if not raw_path:
        raise StorydexCoomiSessionRestoreError(
            "Coomi session_bound event is missing the persisted session path"
        )
    session_path = Path(raw_path).expanduser().resolve()
    expected_path = (STORYDEX_COOMI_SESSIONS / f"{runtime_id}.json").resolve()
    if not session_path.is_file():
        raise StorydexCoomiSessionRestoreError(
            f"Coomi session_bound history does not exist: {session_path}"
        )
    try:
        same_session_file = session_path.samefile(expected_path)
    except OSError:
        same_session_file = False
    if not same_session_file:
        raise StorydexCoomiSessionRestoreError(
            "Coomi session_bound event points outside the expected session path"
        )
    return runtime_id


def _write_coomi_session_binding(
    *, workspace_root: Path, storydex_session_id: str, runtime_session_id: str
) -> Path:
    path = _coomi_binding_path(workspace_root, storydex_session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    session_path = STORYDEX_COOMI_SESSIONS / f"{runtime_session_id}.json"
    value = {
        "version": 2,
        "runtime": "storydex-coomi-rs",
        "workspaceRoot": str(Path(workspace_root).resolve()),
        "storydexSessionId": str(storydex_session_id or "default").strip() or "default",
        "coomiSessionId": runtime_session_id,
        "runtimeSessionId": runtime_session_id,
        "historyPath": str(session_path),
        "sessionPath": str(session_path),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
    return path


def _delete_coomi_session_binding(
    *, workspace_root: Path, storydex_session_id: str, delete_history: bool
) -> None:
    path = _coomi_binding_path(workspace_root, storydex_session_id)
    binding = _read_coomi_session_binding(
        workspace_root=workspace_root, storydex_session_id=storydex_session_id
    )
    if delete_history:
        session_path = _validated_session_path(binding)
        if session_path is not None:
            try:
                session_path.unlink()
            except FileNotFoundError:
                pass
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _validated_session_path(binding: Dict[str, Any]) -> Path | None:
    raw = str(binding.get("sessionPath") or binding.get("historyPath") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser().resolve()
    root = STORYDEX_COOMI_SESSIONS.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Coomi session path is outside the Storydex runtime directory") from exc
    return path


def _runtime_session_usage(binding: Dict[str, Any]) -> Dict[str, Any] | None:
    """Read the runtime session's cumulative usage from its persisted JSON."""
    path = _validated_session_path(binding)
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else payload
    if not isinstance(usage, dict):
        return None
    return dict(usage)


def _runtime_session_total_tokens(binding: Dict[str, Any]) -> int | None:
    """Read the runtime session's cumulative token total."""
    usage = _runtime_session_usage(binding)
    if usage is None:
        return None
    total = usage.get("total_tokens")
    if total is None:
        total = usage.get("totalTokens")
    if total is not None:
        try:
            return max(0, int(total))
        except (TypeError, ValueError):
            pass
    input_tokens = usage.get("input_tokens")
    if input_tokens is None:
        input_tokens = usage.get("prompt_tokens")
    if input_tokens is None:
        input_tokens = usage.get("inputTokens")
    output_tokens = usage.get("output_tokens")
    if output_tokens is None:
        output_tokens = usage.get("completion_tokens")
    if output_tokens is None:
        output_tokens = usage.get("outputTokens")
    try:
        return max(0, int(input_tokens or 0) + int(output_tokens or 0))
    except (TypeError, ValueError):
        return None


def _usage_total_tokens(value: Dict[str, Any]) -> int | None:
    """Extract a cumulative usage total from a bridge event payload."""
    usage = value.get("usage") if isinstance(value.get("usage"), dict) else value
    if not isinstance(usage, dict):
        return None
    for key in ("total_tokens", "totalTokens"):
        if usage.get(key) is not None:
            try:
                return max(0, int(usage[key]))
            except (TypeError, ValueError):
                return None
    input_tokens = usage.get("input_tokens")
    if input_tokens is None:
        input_tokens = usage.get("prompt_tokens")
    if input_tokens is None:
        input_tokens = usage.get("inputTokens")
    output_tokens = usage.get("output_tokens")
    if output_tokens is None:
        output_tokens = usage.get("completion_tokens")
    if output_tokens is None:
        output_tokens = usage.get("outputTokens")
    if input_tokens is None and output_tokens is None:
        return None
    try:
        return max(0, int(input_tokens or 0) + int(output_tokens or 0))
    except (TypeError, ValueError):
        return None


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class StorydexCoomiAgentService:
    def __init__(self) -> None:
        # Start conservatively. A user may still opt into auto-approval or
        # full access, but an ordinary backend restart must not silently grant
        # the broadest session permission.
        self._permission_mode = "ask_approval"
        self._plan_modes: dict[str, bool] = {}
        self._approval_waiters: dict[str, asyncio.Future[Dict[str, Any]]] = {}
        self._active_lock = threading.Lock()
        self._active: dict[str, tuple[asyncio.AbstractEventLoop, LiveBridgeProcess]] = {}
        self._context_by_session: dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _runtime_key(*, session_id: str, workspace_root: Path) -> str:
        return f"{Path(workspace_root).resolve()}::{str(session_id or 'default').strip() or 'default'}"

    def set_plan_mode(
        self, *, session_id: str, workspace_root: Path, active: bool
    ) -> Dict[str, Any]:
        key = self._runtime_key(session_id=session_id, workspace_root=workspace_root)
        self._plan_modes[key] = bool(active)
        return {
            "sessionId": str(session_id or "default").strip() or "default",
            "planMode": bool(active),
            "permissionMode": "plan_mode" if active else self._permission_mode,
            "permissionLabel": "Plan mode" if active else _permission_label(self._permission_mode),
        }

    def _merge_persistent_context(
        self,
        *,
        workspace_root: Path,
        session_id: str,
        snapshot: Dict[str, Any],
        runtime_total: int | None = None,
    ) -> Dict[str, Any]:
        binding = _read_coomi_session_binding(
            workspace_root=workspace_root,
            storydex_session_id=session_id,
        )
        runtime_id = str(
            binding.get("runtimeSessionId") or binding.get("coomiSessionId") or ""
        )
        ledger = _read_coomi_usage_ledger(
            workspace_root=workspace_root,
            storydex_session_id=session_id,
        )
        ledger_cumulative = _nonnegative_int(ledger.get("cumulativeTokens"))
        cumulative = ledger_cumulative
        previous_runtime_id = str(ledger.get("runtimeSessionId") or "")
        previous_runtime_total = _nonnegative_int(ledger.get("runtimeTotalTokens"))
        if runtime_total is None:
            runtime_total = _runtime_session_total_tokens(binding)
        if runtime_total is not None and runtime_total >= 0 and runtime_id:
            if previous_runtime_id == runtime_id:
                cumulative += max(0, runtime_total - previous_runtime_total)
            else:
                cumulative += runtime_total
            if (
                cumulative != ledger_cumulative
                or previous_runtime_id != runtime_id
                or previous_runtime_total != runtime_total
            ):
                _write_coomi_usage_ledger(
                    workspace_root=workspace_root,
                    storydex_session_id=session_id,
                    value={
                        "cumulativeTokens": cumulative,
                        "runtimeSessionId": runtime_id,
                        "runtimeTotalTokens": runtime_total,
                    },
                )
        return {**snapshot, "cumulativeTokens": cumulative}

    def _register_bridge(
        self, *, session_id: str, workspace_root: Path, bridge: LiveBridgeProcess
    ) -> str:
        key = self._runtime_key(session_id=session_id, workspace_root=workspace_root)
        with self._active_lock:
            self._active[key] = (asyncio.get_running_loop(), bridge)
        return key

    def _unregister_bridge(self, key: str, bridge: LiveBridgeProcess) -> None:
        with self._active_lock:
            active = self._active.get(key)
            if active is not None and active[1] is bridge:
                self._active.pop(key, None)

    def _signal_bridge(self, *, session_id: str, workspace_root: Path, steer: bool) -> bool:
        key = self._runtime_key(session_id=session_id, workspace_root=workspace_root)
        with self._active_lock:
            active = self._active.get(key)
        if active is None:
            return False
        loop, bridge = active
        asyncio.run_coroutine_threadsafe(bridge.cancel(steer=steer), loop)
        return True

    def cancel_execution(
        self, *, session_id: str, workspace_root: Path, reason: str = "cancelled"
    ) -> bool:
        key = self._runtime_key(session_id=session_id, workspace_root=workspace_root)
        with self._active_lock:
            active = self._active.get(key)
        if active is None:
            return False
        loop, bridge = active
        asyncio.run_coroutine_threadsafe(
            bridge.cancel(reason=str(reason or "cancelled")),
            loop,
        )
        return True

    def request_steer(self, *, session_id: str, workspace_root: Path) -> bool:
        return self._signal_bridge(session_id=session_id, workspace_root=workspace_root, steer=True)

    def validate_session_for_execution(
        self, *, session_id: str, workspace_root: Path
    ) -> Dict[str, Any]:
        """Fail closed before a turn is exposed as model execution."""
        return _read_coomi_session_binding_for_execution(
            workspace_root=Path(workspace_root).resolve(),
            storydex_session_id=str(session_id or "default").strip() or "default",
        )

    async def generate_commit_message(
        self,
        *,
        workspace_root: Path,
        changed_files: list[str],
        diff_summary: str = "",
        prompt: str = "",
        trace_id: str = "",
    ) -> str:
        del workspace_root
        from services.llm_replay import get_replayable_llm_provider, llm_purpose, llm_trace

        try:
            provider = get_replayable_llm_provider(get_bridge_provider(fast=True))
            with llm_trace(trace_id or "default"), llm_purpose("commit"):
                response = await _call_provider_chat(
                    provider,
                    _commit_message_messages(
                        changed_files=changed_files,
                        diff_summary=diff_summary,
                        prompt=prompt,
                    ),
                    None,
                )
        except Exception as exc:
            raise StorydexCoomiUnavailable(f"Failed to generate commit message: {exc}") from exc
        message = _parse_commit_message_content(str(getattr(response, "content", "") or ""))
        if not message:
            raise StorydexCoomiUnavailable("Failed to generate a usable commit message")
        return message

    async def stream_events(
        self,
        *,
        prompt: str,
        trace_id: str,
        session_id: str,
        workspace_root: Path,
        active_file: str = "",
        story_generation: Dict[str, Any] | None = None,
        turn_contract: Dict[str, Any] | None = None,
        cancellation_token: Any = None,
    ) -> AsyncIterator[tuple[str, Dict[str, Any]]]:
        started = time.perf_counter()
        workspace = Path(workspace_root).resolve()
        normalized_session = str(session_id or "default").strip() or "default"
        runtime_key = self._runtime_key(session_id=normalized_session, workspace_root=workspace)
        command = _parse_slash_command(prompt)
        if command["name"] == "exit_plan":
            self.set_plan_mode(
                session_id=normalized_session, workspace_root=workspace, active=False
            )
            yield "PlanModeChanged", {
                "_type": "PlanModeChanged",
                "_version": 1,
                "planMode": False,
                "permissionMode": self._permission_mode,
                "message": "已退出计划模式，Coomi 可以按当前权限继续执行。",
                "source": "command",
            }
            yield _completed_event(normalized_session, started, 0)
            return
        if command["name"] == "plan" and not command["body"]:
            self.set_plan_mode(
                session_id=normalized_session, workspace_root=workspace, active=True
            )
            yield "PlanModeChanged", {
                "_type": "PlanModeChanged",
                "_version": 1,
                "planMode": True,
                "permissionMode": "plan_mode",
                "message": "已进入计划模式，本会话当前为只读。",
                "source": "command",
            }
            yield _completed_event(normalized_session, started, 0)
            return
        if command["name"] == "plan":
            self.set_plan_mode(
                session_id=normalized_session, workspace_root=workspace, active=True
            )

        plan_mode = self._plan_modes.get(runtime_key, False)
        # Plan mode is a persistent session overlay. Keep the turn capability
        # intact so exit_plan_mode can reveal only the tools this contract
        # already authorises; exiting plan mode must never broaden it.
        capability_policy = resolve_capability_policy(turn_contract, plan_mode=False)
        registry = _create_storydex_tool_registry(
            workspace,
            policy=context_policy_from_turn_contract(turn_contract),
            turn_contract=turn_contract,
            plan_mode=False,
            capability_mode=capability_policy.mode,
        )
        try:
            binding = self.validate_session_for_execution(
                workspace_root=workspace, session_id=normalized_session
            )
        except StorydexCoomiSessionRestoreError as exc:
            yield "AgentError", _agent_error(
                trace_id,
                exc,
                stage="session_restore",
                session_id=normalized_session,
            )
            return
        system_prompt = await _build_coomi_system_prompt(
            workspace_root=workspace,
            prompt=prompt,
            story_generation=story_generation,
            turn_contract=turn_contract,
            plan_mode=plan_mode,
        )
        effective_prompt = command["body"] if command["name"] == "plan" else prompt
        if command["name"] == "loop":
            effective_prompt = (
                "Create and execute a persistent Coomi Loop for this objective. Use create_loop, "
                "update_loop, and get_loop until the objective reaches a terminal status.\n\n"
                + (command["body"] or "Continue the active Storydex loop.")
            )
        permission_mode = "plan_mode" if plan_mode else self._permission_mode
        mutating_tool_names = [
            tool.name
            for tool in registry.list_tools()
            if tool.access != ToolAccess.READ_ONLY
        ]
        reasoning_effort = str(
            _dict_value(turn_contract).get("reasoningEffort") or "auto"
        ).strip().lower()
        bridge_started = time.perf_counter()
        try:
            replay_fixture = str(os.getenv(_PROVIDER_REPLAY_FIXTURE_ENV, "") or "").strip()
            bridge = await LiveBridgeProcess.start(
                {
                    "action": "run",
                    "cwd": str(workspace),
                    "prompt": effective_prompt,
                    "systemPrompt": system_prompt,
                    "runtimeSessionId": binding.get("runtimeSessionId") or binding.get("coomiSessionId"),
                    "storydexSessionId": normalized_session,
                    "permissionMode": permission_mode,
                    "basePermissionMode": self._permission_mode,
                    "capabilityMode": capability_policy.mode,
                    "reasoningEffort": reasoning_effort,
                    "writesAllowed": capability_policy.writes_allowed,
                    "coreWritesAllowed": capability_policy.core_writes_allowed,
                    "allowedWriteRoots": list(capability_policy.allowed_write_roots),
                    "checkpointContext": _compaction_checkpoint_context(
                        workspace_root=workspace,
                        session_id=normalized_session,
                        permission_mode=permission_mode,
                        prompt=effective_prompt,
                        active_file=active_file,
                        turn_contract=turn_contract,
                    ),
                    "toolSpecs": registry.specs(),
                    "mutatingToolNames": mutating_tool_names,
                    # Replay is an explicit Refactor/CI seam.  The Rust bridge
                    # fails closed when the path is missing or the request does
                    # not match the checked-in fixture; an empty value keeps
                    # the normal live provider path unchanged.
                    **(
                        {"providerReplayFixture": str(Path(replay_fixture).resolve())}
                        if replay_fixture
                        else {}
                    ),
                }
            )
        except Exception as exc:
            try:
                failure_status = self.get_status_for_execution(workspace_root=workspace)
            except Exception:
                failure_status = {}
            yield "AgentError", _agent_error(
                trace_id,
                exc,
                stage="bridge_start",
                session_id=normalized_session,
                provider_id=str(failure_status.get("providerId") or ""),
                model=str(failure_status.get("model") or ""),
            )
            return
        bridge_start_ms = (time.perf_counter() - bridge_started) * 1000

        key = self._register_bridge(
            session_id=normalized_session, workspace_root=workspace, bridge=bridge
        )
        status = self.get_status_for_execution(
            workspace_root=workspace,
            session_id=normalized_session,
        )
        yield _agent_started(
            session_id=normalized_session, prompt=prompt, status=status, mode="coomi"
        )
        bound_runtime_id = str(
            binding.get("runtimeSessionId") or binding.get("coomiSessionId") or ""
        )
        translator = _CoomiEventTranslator(
            session_id=normalized_session,
            trace_id=trace_id,
            workspace_root=workspace,
            usage_baseline=_runtime_session_usage(binding),
            bridge_start_ms=bridge_start_ms,
            provider_id=str(status.get("providerId") or ""),
            model=str(status.get("model") or ""),
        )
        resolution_tasks: set[asyncio.Task[Any]] = set()
        terminal_seen = False
        attempt_text_characters = 0
        bridge_cancel_sent = False
        try:
            async for packet in bridge.events():
                if _is_cancelled(cancellation_token) and not bridge_cancel_sent:
                    await bridge.cancel()
                    bridge_cancel_sent = True
                packet_type = str(packet.get("type") or "")
                data = packet.get("data") if isinstance(packet.get("data"), dict) else {}
                if packet_type == "session_bound":
                    runtime_id = _validated_persisted_session_bound(data)
                    if runtime_id != bound_runtime_id:
                        translator.reset_usage_baseline()
                    _write_coomi_session_binding(
                        workspace_root=workspace,
                        storydex_session_id=normalized_session,
                        runtime_session_id=runtime_id,
                    )
                    continue
                if packet_type == "tool_request":
                    await self._dispatch_tool_request(bridge, registry, data)
                    continue
                if packet_type == "provider_retry":
                    attempt = int(data.get("attempt") or 1)
                    max_attempts = int(data.get("maxAttempts") or max(attempt, 1))
                    yield "ConnectionRetry", {
                        "_type": "ConnectionRetry",
                        "_version": 1,
                        "attempt": attempt,
                        "maxAttempts": max_attempts,
                        "resetTextCharacters": attempt_text_characters,
                        "providerResetTextCharacters": int(
                            data.get("resetTextCharacters") or 0
                        ),
                        "message": (
                            "当前上游提供商服务不稳定，正在自动重试"
                            f"（第 {attempt}/{max_attempts} 次）。"
                        ),
                        "details": {"runtime": "storydex-coomi-rs", "source": "agent"},
                    }
                    attempt_text_characters = 0
                    continue
                if packet_type in {"approval_request", "user_input_request"}:
                    events, task = self._prepare_interaction(
                        bridge=bridge,
                        packet_type=packet_type,
                        data=data,
                        trace_id=trace_id,
                        session_id=normalized_session,
                    )
                    resolution_tasks.add(task)
                    task.add_done_callback(resolution_tasks.discard)
                    for event in events:
                        yield event
                    continue
                if packet_type in {"context_updated", "turn_completed", "completed"}:
                    previous = self._context_by_session.get(runtime_key, {})
                    snapshot = _context_snapshot_from_bridge(
                        data, fallback=previous
                    )
                    self._context_by_session[runtime_key] = self._merge_persistent_context(
                        workspace_root=workspace,
                        session_id=normalized_session,
                        snapshot=snapshot,
                        runtime_total=_usage_total_tokens(data),
                    )
                if packet_type == "plan_mode_changed":
                    active = bool(data.get("active"))
                    if active:
                        yield "AgentWarning", {
                            "_type": "AgentWarning",
                            "_version": 1,
                            "warning_type": "AgentPlanModeEntryRejected",
                            "status": "warning",
                            "message": "已拒绝 Agent 进入计划模式；只有用户输入 /plan 才能开启只读模式。",
                            "details": {"runtime": "storydex-coomi-rs", "source": "agent"},
                        }
                        continue
                    self.set_plan_mode(
                        session_id=normalized_session,
                        workspace_root=workspace,
                        active=False,
                    )
                if packet_type == "model_started":
                    attempt_text_characters = 0
                if packet_type == "tool_finished":
                    _mark_catalog_from_tool_result(workspace, data)
                translated = translator.translate(packet)
                if translated is not None:
                    if translated[0] == "TextChunk":
                        attempt_text_characters += len(str(translated[1].get("content") or ""))
                    if translated[0] in {"AgentCompleted", "AgentCancelled", "AgentError"}:
                        terminal_seen = True
                    yield translated
        except Exception as exc:
            if not terminal_seen:
                yield "AgentError", _agent_error(
                    trace_id,
                    exc,
                    stage="bridge_events",
                    session_id=normalized_session,
                    provider_id=translator.provider_id,
                    model=translator.model,
                    status_code=translator.error_http_status,
                )
        finally:
            self._unregister_bridge(key, bridge)
            for task in resolution_tasks:
                task.cancel()
            for approval_id, future in list(self._approval_waiters.items()):
                if not future.done():
                    future.cancel()
                self._approval_waiters.pop(approval_id, None)
            await bridge.close()

    async def _dispatch_tool_request(
        self, bridge: LiveBridgeProcess, registry: StorydexToolRegistry, data: Dict[str, Any]
    ) -> None:
        request_id = str(data.get("requestId") or "")
        call = data.get("call") if isinstance(data.get("call"), dict) else {}
        result = await asyncio.to_thread(
            registry.dispatch,
            str(call.get("name") or ""),
            call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
        )
        output = result.output if result.success else (result.error or result.output)
        await bridge.resolve(
            request_id,
            {"success": result.success, "output": str(output or "")},
        )

    def _prepare_interaction(
        self,
        *,
        bridge: LiveBridgeProcess,
        packet_type: str,
        data: Dict[str, Any],
        trace_id: str,
        session_id: str,
    ) -> tuple[list[tuple[str, Dict[str, Any]]], asyncio.Task[Any]]:
        bridge_request_id = str(data.get("requestId") or "")
        pending: list[tuple[str, asyncio.Future[Dict[str, Any]], str]] = []
        events: list[tuple[str, Dict[str, Any]]] = []
        if packet_type == "approval_request":
            call = data.get("call") if isinstance(data.get("call"), dict) else {}
            questions = [
                {
                    "id": "approval",
                    "header": "Permission",
                    "question": str(data.get("reason") or f"Allow {call.get('name') or 'tool'}?"),
                    "options": _approval_options(None, is_permission=True),
                    "kind": "permission",
                    "tool": call,
                }
            ]
        else:
            request = data.get("request") if isinstance(data.get("request"), dict) else {}
            questions = [value for value in request.get("questions", []) if isinstance(value, dict)]
        for index, question in enumerate(questions):
            approval_id = f"{trace_id}-{uuid4().hex}"
            future: asyncio.Future[Dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._approval_waiters[approval_id] = future
            question_id = str(question.get("id") or f"question-{index + 1}")
            pending.append((approval_id, future, question_id))
            events.append(
                (
                    "PermissionRequest",
                    {
                        "_type": "PermissionRequest",
                        "_version": 1,
                        "kind": str(question.get("kind") or "question"),
                        "approval_id": approval_id,
                        "approvalId": approval_id,
                        "session_id": session_id,
                        "sessionId": session_id,
                        "header": str(question.get("header") or f"Q{index + 1}"),
                        "question": str(question.get("question") or "Provide an answer."),
                        "options": _approval_options(
                            question.get("options"),
                            is_permission=packet_type == "approval_request",
                        ),
                        "allowText": packet_type != "approval_request",
                        "questionIndex": index + 1,
                        "questionTotal": len(questions),
                    },
                )
            )

        async def forward() -> None:
            values: dict[str, str] = {}
            approved = True
            try:
                for approval_id, future, question_id in pending:
                    answer = await future
                    decision = str(answer.get("decision") or "")
                    response = answer.get("response") if isinstance(answer.get("response"), dict) else {}
                    if packet_type == "approval_request":
                        approved = decision in {"allow", "approve", "approved", "yes"}
                    else:
                        values[question_id] = _user_input_answer(response, decision)
                payload = {"approved": approved} if packet_type == "approval_request" else {"answers": values}
                await bridge.resolve(bridge_request_id, payload)
            finally:
                for approval_id, _, _ in pending:
                    self._approval_waiters.pop(approval_id, None)

        return events, asyncio.create_task(forward())

    @staticmethod
    def _ensure_coomi_installed() -> None:
        bridge_command()

    def get_status(
        self,
        *,
        workspace_root: Path,
        session_id: str = "default",
        probe_bridge: bool = True,
    ) -> Dict[str, Any]:
        installed = True
        try:
            self._ensure_coomi_installed()
        except Exception:
            installed = False
        payload = _read_providers_config_payload()
        providers = payload.get("providers") if isinstance(payload.get("providers"), dict) else {}
        bridge_status = _bridge_status_snapshot(probe=probe_bridge)
        provider_id = str(
            bridge_status.get("activeProvider")
            or payload.get("active")
            or ""
        )
        provider = providers.get(provider_id) if isinstance(providers.get(provider_id), dict) else {}
        normalized_session = str(session_id or "default").strip() or "default"
        runtime_key = self._runtime_key(
            session_id=normalized_session, workspace_root=workspace_root
        )
        context = self._context_by_session.get(runtime_key)
        if context is None:
            context = _context_snapshot_from_session_file(
                workspace_root=workspace_root,
                session_id=normalized_session,
            )
            context = self._merge_persistent_context(
                workspace_root=workspace_root,
                session_id=normalized_session,
                snapshot=context,
            )
            self._context_by_session[runtime_key] = context
        plan_mode = self._plan_modes.get(runtime_key, False)
        return {
            "runtime": str(bridge_status.get("runtime") or "storydex-coomi-rs"),
            "installed": installed,
            "home": str(STORYDEX_COOMI_HOME),
            "configPath": str(STORYDEX_COOMI_CONFIG),
            "sessionsPath": str(STORYDEX_COOMI_SESSIONS),
            "providerId": provider_id,
            "providerType": str(provider.get("type") or ""),
            "model": str(bridge_status.get("activeModel") or provider.get("model") or ""),
            "display": str(provider.get("display") or provider_id),
            "reasoningCapability": bridge_status.get("reasoningCapability") or {},
            "reasoningRequestPlan": bridge_status.get("reasoningRequestPlan") or {},
            "models": bridge_status.get("models") if isinstance(bridge_status.get("models"), list) else [],
            "providerCapabilities": bridge_status.get("capabilities") or {},
            "permissionMode": "plan_mode" if plan_mode else self._permission_mode,
            "permissionLabel": "Plan mode" if plan_mode else _permission_label(self._permission_mode),
            "planMode": plan_mode,
            "toolCount": len(_create_storydex_tool_registry(workspace_root).specs()) + 20,
            **context,
        }

    def get_status_for_execution(
        self, *, workspace_root: Path, session_id: str = "default"
    ) -> Dict[str, Any]:
        """Return cached/config status without starting a diagnostic bridge."""
        return self.get_status(
            workspace_root=workspace_root,
            session_id=session_id,
            probe_bridge=False,
        )

    def read_config(self) -> Dict[str, Any]:
        path = _ensure_storydex_coomi_config()
        content = path.read_text(encoding="utf-8-sig")
        parsed = json.loads(content)
        return {
            "configPath": str(path),
            "content": content,
            "parsed": parsed,
            "updatedAt": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
        }

    def write_config(self, content: str) -> Dict[str, Any]:
        value = json.loads(str(content or ""))
        _validate_provider_document(value)
        path = _ensure_storydex_coomi_config()
        _atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        _invalidate_bridge_status_cache()
        return self.read_config()

    def list_models(
        self,
        *,
        base_url: str,
        api_key: str,
        provider_type: str,
        http_get: Callable[..., Any] | None = None,
    ) -> Dict[str, Any]:
        endpoint = _models_endpoint(base_url, provider_type)
        headers = {"Accept": "application/json", "User-Agent": f"Storydex-Coomi/{STORYDEX_COOMI_RUNTIME_VERSION}"}
        normalized = str(provider_type or "").lower().replace("-", "_")
        params = None
        if normalized in {"gemini", "gemini_native"}:
            if api_key:
                headers["x-goog-api-key"] = api_key
        elif normalized in {"anthropic", "anthropic_messages"}:
            if api_key:
                headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        elif api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            if http_get is None:
                response = httpx.get(endpoint, headers=headers, params=params, timeout=20.0)
            else:
                response = http_get(endpoint, headers=headers, timeout=20.0)
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            elif int(getattr(response, "status_code", 200) or 200) >= 400:
                raise RuntimeError(f"HTTP {response.status_code}: {getattr(response, 'text', '')}")
            payload = response.json()
        except Exception as exc:
            detail = _coomi_error_message(exc)
            if api_key:
                detail = detail.replace(api_key, "***")
            raise ValueError(f"Model list request failed: {detail}") from exc
        return {"endpoint": endpoint, "models": _extract_model_ids(payload)}

    def clear_session(
        self,
        session_id: str,
        *,
        workspace_root: Path | None = None,
        delete_history: bool = False,
        delete_usage: bool = False,
    ) -> None:
        if workspace_root is None:
            return
        runtime_key = self._runtime_key(
            session_id=session_id, workspace_root=workspace_root
        )
        self._plan_modes.pop(runtime_key, None)
        self._context_by_session.pop(runtime_key, None)
        if delete_usage:
            try:
                _coomi_usage_ledger_path(workspace_root, session_id).unlink()
            except FileNotFoundError:
                pass
        _delete_coomi_session_binding(
            workspace_root=workspace_root,
            storydex_session_id=str(session_id or "default"),
            delete_history=delete_history,
        )

    def rollback_last_turn(self, session_id: str, *, workspace_root: Path) -> Dict[str, Any]:
        normalized = str(session_id or "default").strip() or "default"
        result = {"rolledBack": False, "sessionId": normalized}
        binding = _read_coomi_session_binding(
            workspace_root=workspace_root, storydex_session_id=normalized
        )
        path = _validated_session_path(binding)
        if path is None or not path.is_file():
            return result
        value = json.loads(path.read_text(encoding="utf-8"))
        messages = value.get("messages") if isinstance(value.get("messages"), list) else []
        last_user = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if isinstance(messages[index], dict)
                and messages[index].get("role") == "user"
                and not messages[index].get("internal")
            ),
            None,
        )
        if last_user is None:
            return result
        value["messages"] = messages[:last_user]
        value["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))
        result["rolledBack"] = True
        return result

    def snapshot_session_history(self, session_id: str, *, workspace_root: Path) -> Dict[str, Any]:
        normalized = str(session_id or "default").strip() or "default"
        binding = _read_coomi_session_binding(
            workspace_root=workspace_root, storydex_session_id=normalized
        )
        path = _validated_session_path(binding)
        if path is None or not path.is_file():
            return {
                "available": False,
                "sessionId": normalized,
                "workspaceRoot": Path(workspace_root).resolve(),
            }
        return {
            "available": True,
            "sessionId": normalized,
            "workspaceRoot": Path(workspace_root).resolve(),
            "historyPath": path,
            "historyBytes": path.read_bytes(),
        }

    def restore_session_history(self, snapshot: Dict[str, Any]) -> bool:
        if not isinstance(snapshot, dict) or not snapshot.get("available"):
            return False
        path = Path(snapshot.get("historyPath")).resolve()
        path.relative_to(STORYDEX_COOMI_SESSIONS.resolve())
        content = snapshot.get("historyBytes")
        if not isinstance(content, bytes):
            raise ValueError("Coomi session snapshot is invalid")
        _atomic_write(path, content)
        return True

    def set_permission_mode(self, mode: str) -> Dict[str, Any]:
        self._permission_mode = _normalize_permission_mode(mode)
        return {
            "permissionMode": self._permission_mode,
            "permissionLabel": _permission_label(self._permission_mode),
        }

    def cycle_permission_mode(self) -> Dict[str, Any]:
        modes = ["ask_approval", "approve_for_me", "full_access"]
        index = modes.index(self._permission_mode) if self._permission_mode in modes else -1
        return self.set_permission_mode(modes[(index + 1) % len(modes)])

    def resolve_approval(
        self, approval_id: str, decision: str, *, response: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        future = self._approval_waiters.get(str(approval_id or ""))
        resolved = future is not None and not future.done()
        if resolved and future is not None:
            value = {"decision": str(decision or ""), "response": dict(response or {})}
            future.get_loop().call_soon_threadsafe(future.set_result, value)
        return {
            "approvalId": str(approval_id or ""),
            "decision": str(decision or ""),
            "resolved": resolved,
        }


def _completion_cap_kwargs(
    provider: Any,
    max_completion_tokens: int,
) -> tuple[Dict[str, int], bool]:
    """Return a cap only when the concrete provider declares it explicitly."""

    limit = max(0, int(max_completion_tokens or 0))
    if limit <= 0:
        return {}, False
    concrete = getattr(provider, "_provider", provider)
    chat = getattr(concrete, "chat", None)
    if chat is None:
        return {}, False
    try:
        parameters = inspect.signature(chat).parameters
    except (TypeError, ValueError):
        return {}, False
    for name in ("max_completion_tokens", "max_tokens"):
        parameter = parameters.get(name)
        if parameter is not None and parameter.kind is not inspect.Parameter.VAR_KEYWORD:
            return {name: limit}, True
    return {}, False


def _required_tool_choice_kwargs(provider: Any) -> tuple[Dict[str, str], bool]:
    """Force the single revision tool only when the provider exposes the option."""

    concrete = getattr(provider, "_provider", provider)
    chat = getattr(concrete, "chat", None)
    if chat is None:
        return {}, False
    try:
        parameter = inspect.signature(chat).parameters.get("tool_choice")
    except (TypeError, ValueError):
        return {}, False
    if parameter is None or parameter.kind is inspect.Parameter.VAR_KEYWORD:
        return {}, False
    return {"tool_choice": "required"}, True


class _OpenAICompatibleCompletionCapAdapter:
    """Expose a real max_tokens seam for legacy OpenAI-compatible providers."""

    def __init__(self, provider: Any) -> None:
        object.__setattr__(self, "_provider", provider)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_provider":
            object.__setattr__(self, name, value)
        else:
            setattr(self._provider, name, value)

    @staticmethod
    def storydex_revision_budget_policy() -> Dict[str, Any]:
        return {
            "name": "openai_compatible_non_streaming",
            "deadlineRatio": 1.25,
            "deadlineMinimumSeconds": 30,
            "deadlineMaximumSeconds": 60,
        }

    async def chat(
        self,
        messages: list[Dict[str, Any]],
        tools: Any = None,
        max_tokens: int | None = None,
        tool_choice: str = "auto",
    ) -> Any:
        limit = max(0, int(max_tokens or 0))
        if limit <= 0:
            return await _call_provider_chat(self._provider, messages, tools)

        params = self._provider._build_params(
            messages,
            tools,
            stream=False,
            tool_choice=tool_choice,
        )
        request = dict(params) if isinstance(params, dict) else {}
        request["max_tokens"] = limit
        if tools and "deepseek" in str(getattr(self._provider, "model", "")).casefold():
            extra_body = (
                dict(request.get("extra_body"))
                if isinstance(request.get("extra_body"), dict)
                else {}
            )
            extra_body["thinking"] = {"type": "disabled"}
            request["extra_body"] = extra_body
        response = await self._provider.client.chat.completions.create(**request)
        parsed = self._provider._parse_response(response, tools_enabled=bool(tools))
        choices = list(getattr(response, "choices", None) or [])
        finish_reason = getattr(choices[0], "finish_reason", None) if choices else None
        if finish_reason is not None:
            setattr(parsed, "finish_reason", str(finish_reason))
        return parsed


def _adapt_story_generation_provider(provider: Any) -> Any:
    build_params = getattr(provider, "_build_params", None)
    parse_response = getattr(provider, "_parse_response", None)
    client = getattr(provider, "client", None)
    chat = getattr(client, "chat", None)
    completions = getattr(chat, "completions", None)
    create = getattr(completions, "create", None)
    if callable(build_params) and callable(parse_response) and callable(create):
        return _OpenAICompatibleCompletionCapAdapter(provider)
    return provider


class CoomiStoryGenerationAdapter:
    def __init__(
        self,
        *,
        trace_id: str,
        provider_id: str = "",
        reasoning_effort: str = "auto",
        provider: Any = None,
        maximum_transport_retries: int = 1,
        event_sink: Callable[[str, Dict[str, Any]], None] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        attempt_event_name: str = "SemanticBudgetProviderAttempt",
    ) -> None:
        self.trace_id = str(trace_id or "semantic-budget")
        self.provider_id = str(provider_id or "").strip()
        self.reasoning_effort = str(reasoning_effort or "auto").strip().lower()
        self.maximum_transport_retries = max(0, min(2, int(maximum_transport_retries)))
        self.event_sink = event_sink
        self.sleep = sleep
        self.attempt_event_name = str(attempt_event_name or "SemanticBudgetProviderAttempt")
        self.provider_attempts = 0
        self.provider_retries = 0
        self._provider_override = provider
        self._provider: Any = None
        self.last_cap_applied = False
        self.last_completion_tokens: int | None = None
        self.last_usage: Dict[str, Any] | None = None

    async def _resolve_provider(self) -> Any:
        from services.llm_replay import get_replayable_llm_provider

        if self._provider is None:
            raw = self._provider_override or get_bridge_provider(
                self.provider_id or None,
                reasoning_effort=self.reasoning_effort,
            )
            raw = _adapt_story_generation_provider(raw)
            self._provider = get_replayable_llm_provider(raw)
        return self._provider

    async def revision_budget_policy(self) -> Dict[str, Any]:
        provider = await self._resolve_provider()
        concrete = getattr(provider, "_provider", provider)
        resolver = getattr(concrete, "storydex_revision_budget_policy", None)
        if not callable(resolver):
            return {}
        policy = resolver()
        return dict(policy) if isinstance(policy, dict) else {}

    def _capture_response_usage(self, response: Any) -> None:
        from services.llm_replay import normalize_llm_usage

        usage = normalize_llm_usage(getattr(response, "usage", None), source="provider_response")
        self.last_usage = dict(usage) if isinstance(usage, dict) else None
        output_tokens = usage.get("outputTokens") if isinstance(usage, dict) else None
        self.last_completion_tokens = int(output_tokens) if output_tokens is not None else None

    async def complete(
        self, *, messages: list[Dict[str, str]], purpose: str, metadata: Dict[str, Any]
    ) -> str:
        tools, paragraph_count = _semantic_story_output_tool(purpose, metadata)
        prepared = _semantic_story_tool_messages(messages) if tools else messages
        attempt_metadata = dict(metadata)
        if tools is not None:
            attempt_metadata["structuredOutputParagraphCount"] = paragraph_count
        for retry_index in range(self.maximum_transport_retries + 1):
            self.provider_attempts += 1
            try:
                from services.llm_replay import llm_purpose, llm_trace

                provider = await self._resolve_provider()
                with llm_trace(self.trace_id), llm_purpose(purpose):
                    response = await _call_provider_chat(provider, prepared, tools)
                self._capture_response_usage(response)
                content = _semantic_story_response_content(response, paragraph_count)
                if tools and not content.strip():
                    raise StorydexCoomiEmptyResponse("Provider returned no semantic story content")
                self._emit_attempt(purpose, attempt_metadata, retry_index + 1, "success")
                return content
            except Exception as exc:
                retry = _semantic_provider_error_retryable(exc) and retry_index < self.maximum_transport_retries
                delay = _semantic_provider_retry_delay(exc) if retry else 0
                self._emit_attempt(
                    purpose, attempt_metadata, retry_index + 1, "error", exc, retry, delay
                )
                if not retry:
                    raise
                self.provider_retries += 1
                value = self.sleep(delay)
                if inspect.isawaitable(value):
                    await value
        raise RuntimeError("Provider retry loop exited unexpectedly")

    async def complete_tool_call(
        self,
        *,
        messages: list[Dict[str, str]],
        tool: Dict[str, Any],
        purpose: str,
        tool_name: str,
        max_completion_tokens: int = 0,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        attempt_metadata = dict(metadata or {})
        attempt_metadata["toolName"] = str(tool_name)
        attempt_metadata["capApplied"] = False
        attempt_metadata["completionTokens"] = None
        self.last_cap_applied = False
        self.last_completion_tokens = None
        self.last_usage = None
        if int(max_completion_tokens or 0) > 0:
            attempt_metadata["maxCompletionTokens"] = int(max_completion_tokens)
        tools = [{"type": "function", "function": dict(tool)}]
        self.provider_attempts += 1
        try:
            from services.llm_replay import llm_purpose, llm_trace

            provider = await self._resolve_provider()
            if not hasattr(provider, "chat"):
                raise NotImplementedError("provider does not expose a chat interface")
            cap_kwargs, cap_applied = _completion_cap_kwargs(provider, max_completion_tokens)
            tool_choice_kwargs, tool_choice_applied = _required_tool_choice_kwargs(provider)
            self.last_cap_applied = cap_applied
            attempt_metadata["capApplied"] = cap_applied
            attempt_metadata["toolChoiceApplied"] = tool_choice_applied
            with llm_trace(self.trace_id), llm_purpose(purpose):
                response = await _call_provider_chat(
                    provider,
                    messages,
                    tools,
                    **cap_kwargs,
                    **tool_choice_kwargs,
                )
            self._capture_response_usage(response)
            attempt_metadata["completionTokens"] = self.last_completion_tokens
        except asyncio.CancelledError as exc:
            self._emit_attempt(purpose, attempt_metadata, 1, "error", exc)
            raise
        except Exception as exc:
            self._emit_attempt(purpose, attempt_metadata, 1, "error", exc)
            raise

        diagnostics = _tool_call_diagnostics(
            response,
            tool_name,
            cap_applied=self.last_cap_applied,
            max_completion_tokens=max_completion_tokens,
            completion_tokens=self.last_completion_tokens,
        )
        attempt_metadata.update(diagnostics)
        try:
            arguments = _tool_call_arguments(
                response,
                tool_name,
                completion_cap_hit=bool(diagnostics["completionCapHit"]),
            )
            if arguments is None:
                raise StorydexToolCallRejected("tool_call_absent")
        except StorydexToolCallRejected as exc:
            attempt_metadata["toolCallStatus"] = exc.reason
            self._emit_attempt(purpose, attempt_metadata, 1, "error", exc)
            raise
        attempt_metadata["toolCallStatus"] = "success"
        self._emit_attempt(purpose, attempt_metadata, 1, "success")
        return arguments

    def _emit_attempt(
        self,
        purpose: str,
        metadata: Dict[str, Any],
        attempt: int,
        outcome: str,
        error: Exception | None = None,
        retry_scheduled: bool = False,
        retry_delay: int = 0,
    ) -> None:
        if self.event_sink is None:
            return
        self.event_sink(
            self.attempt_event_name,
            {
                "_type": self.attempt_event_name,
                "_version": 1,
                "purpose": purpose,
                "metadata": dict(metadata),
                "attempt": attempt,
                "outcome": outcome,
                "statusCode": _semantic_provider_error_status(error),
                "errorType": type(error).__name__ if error else "",
                "retryScheduled": retry_scheduled,
                "retryDelaySeconds": retry_delay,
            },
        )


def _tool_result_provenance(preview: str) -> Dict[str, Any]:
    try:
        payload = json.loads(str(preview or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    try:
        revision = validate_source_revision(str(payload.get("revision") or ""))
    except ValueError:
        revision = ""
    span = payload.get("span") if isinstance(payload.get("span"), dict) else {}
    try:
        path = normalize_source_path(str(payload.get("path") or ""))
    except ValueError:
        path = ""
    if span and revision:
        span_revision = str(span.get("revision") or revision)
        try:
            if validate_source_revision(span_revision) != revision:
                span = {}
        except ValueError:
            span = {}
    result: Dict[str, Any] = {}
    if revision:
        result["source_revision"] = revision
    if span:
        result["source_span"] = dict(span)
    if path:
        result["source_path"] = path
    return result


def _mark_catalog_from_tool_result(workspace_root: Path, data: Dict[str, Any]) -> None:
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    if not bool(result.get("success")):
        return
    call = data.get("call") if isinstance(data.get("call"), dict) else {}
    name = str(call.get("name") or "").strip().lower()
    if name not in {
        "write_file",
        "edit_file",
        "apply_patch",
        "storydexapplyworkspaceoperations",
        "storydexapplystoryincrement",
        "storydexsyncwiki",
    }:
        return
    arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    paths: list[str] = []
    for key in (
        "path",
        "relativePath",
        "relative_path",
        "fromRelativePath",
        "toRelativePath",
        "segmentRelativePath",
        "snapshotRelativePath",
    ):
        value = str(arguments.get(key) or "").strip()
        if value:
            paths.append(value)
    if name == "apply_patch" or not paths:
        paths.extend(["chapters", ".storydex/characters", ".storydex/worldbook", ".storydex/memory"])
    try:
        from services.content_catalog_service import get_content_catalog_service

        get_content_catalog_service(workspace_root).mark_dirty(paths, source="coomi_tool")
    except Exception:
        logger.exception("Unable to enqueue content catalog updates for %s", name)


class _CoomiEventTranslator:
    def __init__(
        self,
        *,
        session_id: str,
        trace_id: str = "",
        workspace_root: Path | None = None,
        usage_baseline: Dict[str, Any] | None = None,
        bridge_start_ms: float = 0.0,
        provider_id: str = "",
        model: str = "",
    ) -> None:
        self.session_id = session_id
        self.trace_id = str(trace_id or "")
        self.workspace_root = Path(workspace_root).resolve() if workspace_root is not None else None
        self.evidence_ledger = None
        if self.workspace_root is not None:
            try:
                from services.evidence_ledger_service import get_evidence_ledger_service

                self.evidence_ledger = get_evidence_ledger_service(self.workspace_root, session_id)
            except Exception:
                logger.exception("Unable to initialize evidence ledger for %s", session_id)
        self.started_tools: dict[str, float] = {}
        self.usage_baseline = dict(usage_baseline or {})
        self.turn_usage: Dict[str, Any] | None = None
        self.reasoning_plan: Dict[str, Any] | None = None
        self.compaction_checkpoint: Dict[str, Any] = {}
        self.runtime_metrics: Dict[str, float] = {
            "bridgeStartMs": round(max(0.0, float(bridge_start_ms or 0.0)), 3)
        }
        self.provider_id = str(provider_id or "")
        self.model = str(model or "")
        self.provider_mode = "live"
        self.last_http_status: int | None = None
        self.current_stage = "bridge_events"

    @property
    def error_http_status(self) -> int | None:
        status = _http_status_or_none(self.last_http_status)
        return status if status is not None and status >= 400 else None

    def reset_usage_baseline(self) -> None:
        self.usage_baseline = {}
        self.turn_usage = None

    def _usage_since_baseline(self, value: Dict[str, Any]) -> Dict[str, Any]:
        current = _usage_aliases(value)
        baseline = _usage_aliases(self.usage_baseline)
        current_total = int(current.get("total_tokens") or 0)
        baseline_total = int(baseline.get("total_tokens") or 0)
        if current_total < baseline_total:
            baseline = _usage_aliases({})
        result = {
            "input_tokens": max(
                0,
                int(current.get("prompt_tokens") or 0)
                - int(baseline.get("prompt_tokens") or 0),
            ),
            "cached_input_tokens": max(
                0,
                int(current.get("cached_input_tokens") or 0)
                - int(baseline.get("cached_input_tokens") or 0),
            ),
            "output_tokens": max(
                0,
                int(current.get("completion_tokens") or 0)
                - int(baseline.get("completion_tokens") or 0),
            ),
        }
        current_reasoning = current.get("reasoning_tokens")
        baseline_reasoning = baseline.get("reasoning_tokens")
        if current_reasoning is not None:
            result["reasoning_tokens"] = max(
                0,
                int(current_reasoning)
                - (int(baseline_reasoning) if baseline_reasoning is not None else 0),
            )
        return _usage_aliases(result)

    def translate(self, event: Any) -> tuple[str, Dict[str, Any]] | None:
        if not isinstance(event, dict):
            name = type(event).__name__
            if name == "TextChunk":
                return "TextChunk", {"_type": "TextChunk", "_version": 1, "content": str(getattr(event, "content", ""))}
            if name == "ReasoningChunk":
                return None
            if name == "ToolDone":
                return "ToolDone", {
                    "_type": "ToolDone",
                    "_version": 1,
                    "tool_name": str(getattr(event, "tool_name", "")),
                    "tool_call_id": str(getattr(event, "tool_call_id", "")),
                    "is_error": bool(getattr(event, "is_error", False)),
                    "result_preview": str(getattr(event, "result_preview", "")),
                    "duration_ms": int(float(getattr(event, "elapsed", 0) or 0) * 1000),
                }
            return None
        name = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if name in {"text", "text_delta"}:
            return "TextChunk", {"_type": "TextChunk", "_version": 1, "content": str(data.get("text") or "")}
        if name == "reasoning_delta":
            return None
        if name == "provider_stream":
            http_status = _http_status_or_none(data.get("httpStatus"))
            if http_status is not None:
                self.last_http_status = http_status
            self.current_stage = "provider_stream"
            payload = {
                "_type": "ProviderStream",
                "_version": 1,
                "attempt": max(1, int(data.get("attempt") or 1)),
                "phase": str(data.get("phase") or ""),
                "elapsedMs": max(0, int(data.get("elapsedMs") or 0)),
                "requestBytes": max(0, int(data.get("requestBytes") or 0)),
                "responseBytes": max(0, int(data.get("responseBytes") or 0)),
                "maxOutputTokens": max(0, int(data.get("maxOutputTokens") or 0)),
                "httpStatus": http_status or 0,
            }
            if isinstance(data.get("parallelToolCalls"), bool):
                payload["parallelToolCalls"] = bool(data["parallelToolCalls"])
            return "ProviderStream", payload
        if name == "model_started":
            self.provider_id = str(data.get("provider") or self.provider_id)
            self.model = str(data.get("model") or self.model)
            self.current_stage = "model"
            return "TurnPhase", {
                "_type": "TurnPhase",
                "_version": 1,
                "phase": "model",
                "label": f"{data.get('provider') or ''} / {data.get('model') or ''}",
                "status": "running",
                "current": int(data.get("round") or 1),
            }
        if name == "reasoning_plan":
            plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
            self.reasoning_plan = dict(plan)
            return "ReasoningPlan", {
                "_type": "ReasoningPlan",
                "_version": 1,
                "provider": str(data.get("provider") or ""),
                "model": str(data.get("model") or ""),
                "plan": plan,
            }
        if name == "model_completed":
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            raw_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            usage = _usage_aliases(raw_usage)
            packet = {
                "_type": "ModelCompleted",
                "_version": 1,
                "round": max(1, int(data.get("round") or 1)),
                "upstreamResponded": True,
                "metadata": dict(metadata),
                "usage": usage,
                "responseModel": str(metadata.get("responseModel") or ""),
                "finishReason": str(metadata.get("finishReason") or ""),
                "responseStatus": str(metadata.get("responseStatus") or ""),
                "nativeReasoning": bool(metadata.get("nativeReasoning")),
            }
            if usage.get("reasoning_tokens") is not None:
                packet["reasoning_tokens"] = int(usage.get("reasoning_tokens") or 0)
                packet["reasoningTokens"] = int(usage.get("reasoning_tokens") or 0)
            if self.reasoning_plan is not None:
                packet["reasoningRequestPlan"] = dict(self.reasoning_plan)
            packet["runtimeMetrics"] = dict(self.runtime_metrics)
            return "ModelCompleted", packet
        if name == "runtime_initialized":
            self.runtime_metrics.update(
                {
                    key: round(max(0.0, float(value or 0.0)), 3)
                    for key, value in data.items()
                    if str(key).endswith("Ms")
                }
            )
            mode = str(data.get("providerMode") or "").strip().lower()
            if mode:
                self.provider_mode = mode
            return "RuntimeMetrics", {
                "_type": "RuntimeMetrics",
                "_version": 1,
                "providerMode": self.provider_mode,
                **self.runtime_metrics,
            }
        if name == "tool_started":
            self.current_stage = "tool_execution"
            call = data.get("call") if isinstance(data.get("call"), dict) else {}
            call_id = str(call.get("id") or f"coomi-{uuid4().hex[:12]}")
            self.started_tools[call_id] = time.perf_counter()
            return "ToolStart", {
                "_type": "ToolStart",
                "_version": 1,
                "tool_name": str(call.get("name") or ""),
                "tool_call_id": call_id,
                "arguments": call.get("arguments") or {},
            }
        if name == "tool_finished":
            call = data.get("call") if isinstance(data.get("call"), dict) else {}
            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            call_id = str(call.get("id") or "")
            duration = int((time.perf_counter() - self.started_tools.pop(call_id, time.perf_counter())) * 1000)
            raw_output = str(result.get("output") or "")
            preview = raw_output[:4000]
            payload = {
                "_type": "ToolDone",
                "_version": 1,
                "tool_name": str(call.get("name") or ""),
                "tool_call_id": call_id,
                "is_error": not bool(result.get("success")),
                "result_preview": preview,
                "duration_ms": duration,
                "metrics": {"durationMs": duration},
                "arguments": call.get("arguments") or {},
            }
            payload.update(_tool_result_provenance(raw_output))
            if self.evidence_ledger is not None:
                try:
                    payload["evidenceLedger"] = self.evidence_ledger.record_tool_result(
                        tool_name=str(call.get("name") or "unknown"),
                        arguments=call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                        raw_output=raw_output,
                        turn_id=self.trace_id,
                    )
                except Exception:
                    logger.exception("Unable to record tool evidence for %s", call.get("name"))
            review = _knowledge_review_from_tool_preview(str(call.get("name") or ""), preview)
            if review:
                payload["knowledge_review"] = review
            return "ToolDone", payload
        if name == "context_updated":
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else data
            return "UsageUpdate", {
                "_type": "UsageUpdate",
                "_version": 1,
                "usage": _usage_aliases(usage),
                **_context_snapshot_from_bridge(data),
            }
        if name == "turn_completed":
            cumulative_usage = data.get("usage") if isinstance(data.get("usage"), dict) else data
            self.turn_usage = self._usage_since_baseline(cumulative_usage)
            return "UsageUpdate", {
                "_type": "UsageUpdate",
                "_version": 1,
                "usage": self.turn_usage,
                **_context_snapshot_from_bridge(data),
            }
        if name == "compaction_started":
            self.compaction_checkpoint = {
                "checkpointValid": bool(data.get("checkpointValid")),
                "checkpointHash": str(data.get("checkpointHash") or ""),
                "toolCallCount": int(data.get("toolCallCount") or 0),
                "evidenceRevisionCount": int(data.get("evidenceRevisionCount") or 0),
            }
            return "CompressionEvent", {
                "_type": "CompressionEvent",
                "_version": 1,
                "strategy": "coomi-rs",
                "compact_status": "checkpoint_ready",
                "automatic": bool(data.get("automatic")),
                **self.compaction_checkpoint,
            }
        if name == "compaction_completed":
            before = int(data.get("beforeTokens") or 0)
            after = int(data.get("afterTokens") or 0)
            return "CompressionEvent", {
                "_type": "CompressionEvent",
                "_version": 1,
                "strategy": "coomi-rs",
                "original_messages": before,
                "compressed_messages": after,
                "compact_status": "completed",
                "summary": f"Coomi compacted context tokens: {before} -> {after}.",
                **self.compaction_checkpoint,
            }
        if name == "plan_updated":
            steps = data.get("steps") if isinstance(data.get("steps"), list) else []
            tasks = [
                {"title": str(step.get("step") or ""), "status": str(step.get("status") or "pending")}
                for step in steps if isinstance(step, dict)
            ]
            return "TaskPlanUpdated", {"_type": "TaskPlanUpdated", "_version": 1, "tasks": tasks}
        if name == "plan_mode_changed":
            active = bool(data.get("active"))
            return "PlanModeChanged", {
                "_type": "PlanModeChanged",
                "_version": 1,
                "planMode": active,
                "permissionMode": "plan_mode" if active else str(data.get("permissionMode") or ""),
                "message": str(
                    data.get("message")
                    or ("已进入计划模式，本会话当前为只读。" if active else "Coomi 已退出计划模式。")
                ),
                "source": str(data.get("source") or "agent"),
            }
        if name == "loop_updated":
            return "TurnPhase", {
                "_type": "TurnPhase",
                "_version": 1,
                "phase": "loop",
                "label": str(data.get("status") or "loop"),
                "status": "running",
                "detail": str(data.get("objective") or ""),
                "current": int(data.get("turns_completed") or 0),
            }
        if name == "cancelled":
            return "AgentCancelled", {
                "_type": "AgentCancelled",
                "_version": 1,
                "session_id": self.session_id,
                "reason": str(data.get("reason") or "cancelled"),
            }
        if name == "completed":
            cumulative_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            usage = self.turn_usage or self._usage_since_baseline(cumulative_usage)
            packet = {
                "_type": "AgentCompleted",
                "_version": 1,
                "session_id": self.session_id,
                "route": "coomi",
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            }
            if usage.get("reasoning_tokens") is not None:
                packet["reasoning_tokens"] = int(usage.get("reasoning_tokens") or 0)
            return "AgentCompleted", packet
        if name == "error":
            message = _coomi_error_message(data.get("message"))
            status_code = (
                _http_error_status_or_none(data.get("httpStatus"))
                or _http_error_status_or_none(data.get("statusCode"))
                or _http_error_status_or_none(
                    _semantic_provider_error_status(data.get("message"))
                )
                or self.error_http_status
            )
            provider_id = str(
                data.get("providerId") or data.get("provider") or self.provider_id
            )
            model = str(data.get("model") or self.model)
            exception_type = str(
                data.get("exceptionType")
                or data.get("errorType")
                or "CoomiRustError"
            )
            details: Dict[str, Any] = {
                "traceId": self.trace_id,
                "sessionId": self.session_id,
                "runtime": "storydex-coomi-rs",
                "runtimeVersion": STORYDEX_COOMI_RUNTIME_VERSION,
                "stage": str(data.get("stage") or self.current_stage or "bridge_events"),
                "providerId": provider_id,
                "model": model,
                "exceptionType": exception_type,
                "exceptionMessage": message,
            }
            if status_code is not None:
                details["statusCode"] = status_code
                details["providerHttpStatus"] = status_code
            return "AgentError", {
                "_type": "AgentError",
                "_version": 1,
                "error_type": str(data.get("errorType") or "CoomiRustError"),
                "message": message,
                "details": details,
            }
        return None


def _create_storydex_tool_registry(
    workspace_root: Path,
    policy: ContextPolicy | None = None,
    turn_contract: Dict[str, Any] | None = None,
    plan_mode: bool = False,
    capability_mode: str = "",
) -> StorydexToolRegistry:
    from services.storydex_agent_tools import (
        StorydexApplyKnowledgeUpdateTool,
        StorydexApplyStoryIncrementTool,
        StorydexHelpGuideSearchTool,
        StorydexProjectSearchTool,
        StorydexRuntimePresetStatusTool,
        StorydexStageStoryFragmentTool,
        StorydexSyncWikiTool,
        StorydexVersionStatusTool,
        StorydexWikiQueryTool,
        StorydexWordCountTool,
    )

    root = Path(workspace_root).resolve()
    effective_policy = policy if isinstance(policy, ContextPolicy) else ContextPolicy()
    tools = [
        StorydexRuntimePresetStatusTool(workspace_root=root),
        StorydexVersionStatusTool(workspace_root=root),
        StorydexHelpGuideSearchTool(workspace_root=root),
        StorydexWordCountTool(workspace_root=root),
    ]
    if effective_policy.active_retrieval_tools:
        tools.extend(
            [StorydexProjectSearchTool(workspace_root=root), StorydexWikiQueryTool(workspace_root=root)]
        )
    tools.extend(
        [
            StorydexSyncWikiTool(workspace_root=root),
            StorydexApplyKnowledgeUpdateTool(workspace_root=root, turn_contract=turn_contract),
            StorydexStageStoryFragmentTool(workspace_root=root),
            StorydexApplyStoryIncrementTool(workspace_root=root, turn_contract=turn_contract),
        ]
    )
    requested_capability = str(capability_mode or "").strip().lower()
    resolved_capability = (
        resolve_capability_policy(turn_contract, plan_mode=False).mode
        if isinstance(turn_contract, dict)
        else WORKSPACE_WRITE
    )
    effective_capability = (
        READ_ONLY
        if plan_mode or requested_capability == READ_ONLY or resolved_capability == READ_ONLY
        else requested_capability
        if requested_capability in {SCOPED_WRITE, WORKSPACE_WRITE, FULL_ACCESS}
        else resolved_capability
    )
    if effective_capability == SCOPED_WRITE:
        execution = _dict_value(_dict_value(turn_contract).get("executionPolicy"))
        if not execution.get("allowedWriteRoots"):
            effective_capability = READ_ONLY
    if effective_capability == READ_ONLY:
        tools = [tool for tool in tools if tool.access == ToolAccess.READ_ONLY]
    elif effective_capability == SCOPED_WRITE and isinstance(turn_contract, dict):
        execution = _dict_value(turn_contract.get("executionPolicy"))
        knowledge_policy = _dict_value(turn_contract.get("knowledgeWritePolicy"))
        knowledge_mode = str(knowledge_policy.get("mode") or "").strip().lower()
        intent = _dict_value(turn_contract.get("intentFrame"))
        if knowledge_mode == "candidate_extraction":
            # Git/version inspection is unrelated to evidence extraction and
            # is often unavailable in imported or isolated workspaces.
            tools = [tool for tool in tools if tool.name != "StorydexVersionStatus"]
            tools = [tool for tool in tools if tool.access == ToolAccess.READ_ONLY]
        elif knowledge_mode == "explicit_binding":
            allowed = (
                {"StorydexApplyKnowledgeUpdate"}
                if bool(intent.get("canWrite"))
                else set()
            )
            tools = [
                tool for tool in tools if tool.access == ToolAccess.READ_ONLY or tool.name in allowed
            ]
        elif execution.get("allowedWriteRoots"):
            allowed = set()
            if str(intent.get("primary") or "").lower() == "story_generation" and str(intent.get("operationType") or "").lower() == "create_new":
                allowed.update({"StorydexStageStoryFragment", "StorydexApplyStoryIncrement"})
            if str(intent.get("primary") or "").lower() in {"worldbook_work", "character_work", "wiki_work"}:
                allowed.add("StorydexApplyKnowledgeUpdate")
            if str(intent.get("primary") or "").lower() == "wiki_work":
                allowed.add("StorydexSyncWiki")
            tools = [
                tool for tool in tools if tool.access == ToolAccess.READ_ONLY or tool.name in allowed
            ]
    return StorydexToolRegistry(tools)


def _build_coomi_memory(
    workspace_root: Path, policy: ContextPolicy, *, provider: Any = None
) -> tuple[None, None]:
    del workspace_root, policy, provider
    return None, None


def _compaction_checkpoint_context(
    *,
    workspace_root: Path,
    session_id: str,
    permission_mode: str,
    prompt: str,
    active_file: str,
    turn_contract: Dict[str, Any] | None,
) -> Dict[str, Any]:
    contract = _dict_value(turn_contract)
    turn_plan = _dict_value(contract.get("turnPlan"))
    intent = _dict_value(contract.get("intentFrame"))
    targets: list[str] = []
    for value in (
        active_file,
        turn_plan.get("authoritativeChapterPath"),
        turn_plan.get("nextSegmentPath"),
    ):
        normalized = str(value or "").strip().replace("\\", "/")
        if normalized:
            targets.append(normalized)
    for item in turn_plan.get("fragmentTargets", []):
        if isinstance(item, dict) and str(item.get("path") or "").strip():
            targets.append(str(item["path"]).strip().replace("\\", "/"))
    for value in intent.get("assetTargets", []):
        normalized = str(value or "").strip().replace("\\", "/")
        if normalized:
            targets.append(normalized)
    evidence_revisions: list[Dict[str, Any]] = []
    try:
        from services.evidence_ledger_service import get_evidence_ledger_service

        snapshot = get_evidence_ledger_service(workspace_root, session_id).snapshot()
        for item in snapshot.get("entries", [])[:128]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            revision = str(item.get("revision") or "")
            if path and revision:
                evidence_revisions.append(
                    {
                        "path": path,
                        "revision": revision,
                        "spans": [
                            dict(span)
                            for span in item.get("spans", [])[:32]
                            if isinstance(span, dict)
                        ],
                    }
                )
    except Exception:
        logger.exception("Unable to snapshot evidence ledger for compaction checkpoint")
    return {
        "_type": "StorydexCompactionContext",
        "_version": 1,
        "permissionMode": str(permission_mode or ""),
        "target": targets[0] if targets else "",
        "targets": list(dict.fromkeys(targets)),
        "promptHash": "sha256:" + sha256(str(prompt or "").encode("utf-8")).hexdigest(),
        "evidenceRevisions": evidence_revisions,
    }


async def _build_coomi_system_prompt(
    *,
    workspace_root: Path,
    prompt: str,
    story_generation: Dict[str, Any] | None = None,
    turn_contract: Dict[str, Any] | None = None,
    plan_mode: bool = False,
) -> str:
    from services.storydex_agent_tools import STORY_FRAGMENT_CHUNK_MAX_CHARS

    del prompt
    policy = context_policy_from_turn_contract(turn_contract)
    capability_policy = resolve_capability_policy(turn_contract, plan_mode=False)
    registry = _create_storydex_tool_registry(
        workspace_root,
        policy,
        turn_contract,
        plan_mode=False,
        capability_mode=capability_policy.mode,
    )
    visible_tools = registry.list_tools()
    if plan_mode:
        visible_tools = [tool for tool in visible_tools if tool.access == ToolAccess.READ_ONLY]
    names = ", ".join(f"`{tool.name}`" for tool in visible_tools)
    turn_plan = _dict_value(_dict_value(turn_contract).get("turnPlan"))
    intent = _dict_value(_dict_value(turn_contract).get("intentFrame"))
    prompt_parts = [
        "You are Coomi Desktop for Storydex, a local-first professional workspace for long-form fiction creators.",
        f"Storydex workspace: {Path(workspace_root).resolve()}",
        "The author is the creative authority. Inspect relevant manuscript, character, world, timeline, preset, and memory evidence before acting.",
        "Separate canon evidence from inference, flag continuity conflicts, and preserve the author's voice, point of view, pacing, and explicit constraints.",
        "Keep writes scoped and reviewable. Never silently invent canon, overwrite an author's decision, expose manuscript content, or push to a remote.",
        "Use Rust runtime tools for files, search, shell, MCP, skills, memory, planning, and sub-agents.",
        (
            "When the user explicitly names multiple independent files for the same read-only operation, emit all "
            "independent read_file calls in one model response, preserving the requested order. Do not wait for one "
            "read result before requesting the next unless a later path or decision depends on earlier content."
        ),
        "Only use read_skill for a Skill listed by list_skills as installed. Project-local skill documents under "
        "`.storydex/.agent/skills/` are ordinary workspace files and must be read with read_file; if list_skills "
        "reports no installed skills, do not call read_skill for a project skill name.",
        "The canonical relationship graph file is `.storydex/memory/current/relationship_graph.json`; do not probe "
        "the obsolete `.storydex/memory/relationship_graph.json` path.",
        (
            "A structure map or matched excerpt in assembled context is location evidence, not a complete file. "
            "Before modifying any existing file, call read_file for the target path and relevant span, verify the "
            "returned revision, and continue reading while hasMore=true. Never edit solely from an initial excerpt."
        ),
        f"Storydex domain tools available this turn: {names}.",
        "For Storydex usage questions, call StorydexHelpGuideSearch before answering.",
        "For continuity facts not present in assembled context, use StorydexProjectSearch or StorydexWikiQuery when available.",
        (
            "For an incremental WIKI or knowledge-graph request, call StorydexSyncWiki before reading project files. "
            "If it returns status=ready and noChanges=true, and the user did not explicitly request a deep audit, "
            "finish immediately without list_dir, read_file, grep_files, StorydexProjectSearch, or StorydexWikiQuery. "
            "If changedSourcePaths is non-empty, inspect those paths first and expand only when endpoint resolution "
            "or continuity evidence requires another source. Never treat status=error as a no-op."
        ),
        (
            "For story creation, never write chapters with generic file tools. For any fragment longer than "
            f"{STORY_FRAGMENT_CHUNK_MAX_CHARS} characters, call StorydexStageStoryFragment once per consecutive "
            "chunk, then call StorydexApplyStoryIncrement with stagedFragmentId. Inline only shorter fragments."
        ),
        (
            "For explicit knowledge bindings (绑定、关联、隶属、栖息于、位于、属于、拥有、服务于), "
            "use StorydexApplyKnowledgeUpdate operation=prepare_explicit first. Report its exact target paths "
            "and planId, then wait for a later user-confirmation turn before operation=apply_explicit. Never "
            "apply in the same trace. For relation endpoints, never invent subjectId/objectId and never copy "
            "a display name into an Id field: provide subject/object names plus their source paths when IDs are "
            "unknown. targetSourcePath means the formal Markdown destination (normally the subject file), while "
            "objectSourcePath means the object's source file. Relations extracted from free prose must use "
            "submit_candidates with exact sourceRefs; they remain review_required and must not be written to "
            "facts or formal Markdown."
        ),
        "Treat .storydex/memory as durable story state only, never as chat or execution-log storage.",
        _render_capability_boundary(capability_policy, plan_mode=plan_mode),
        _render_story_generation_options(
            story_generation,
            operation_type=str(intent.get("operationType") or ""),
            include_length=not bool(turn_plan),
        ),
        _render_turn_contract(turn_contract),
    ]
    knowledge_policy = _dict_value(_dict_value(turn_contract).get("knowledgeWritePolicy"))
    knowledge_mode = str(knowledge_policy.get("mode") or "").strip().lower()
    if knowledge_mode == "candidate_extraction":
        prompt_parts.append(
            "This is a candidate-extraction turn: do not call shell, local_shell, apply_patch, write_file, "
            "edit_file, or Git/version tools. Use read_file/list_dir/search for evidence and submit all relation "
            "results only through StorydexApplyKnowledgeUpdate operation=submit_candidates."
        )
    elif knowledge_mode == "explicit_binding":
        prompt_parts.append(
            "This is an explicit-binding turn with a guarded knowledge-write boundary. Do not call shell, "
            "local_shell, apply_patch, write_file, edit_file, memory_write, memory_delete, or any Git/version "
            "tool, even to create a report or copy a plan. Use StorydexApplyKnowledgeUpdate as the only "
            "state-changing tool. In the preparation turn call only operation=prepare_explicit; in the later "
            "user-confirmed turn call only operation=apply_explicit. The domain tool already persists its plan "
            "or formal knowledge files, so do not duplicate those writes with generic file tools. Do not include "
            "sessionId, traceId, providerId, model, or extractorVersion in tool arguments; these fields are "
            "server-managed. If the tool reports a contract-metadata mismatch, retry the same operation once "
            "with those metadata fields removed."
        )
    if plan_mode:
        prompt_parts.append(
            "Plan mode is active. Read and reason without modifying project files. "
            "You have permission to call `exit_plan_mode` yourself when the user asks you to execute "
            "the plan or when planning is complete and continuing the same task requires writes. "
            "After that tool succeeds, continue under the configured Storydex permission mode."
        )
    return "\n\n".join(part for part in prompt_parts if part)


def _render_capability_boundary(
    policy: AgentCapabilityPolicy,
    *,
    plan_mode: bool,
) -> str:
    if policy.mode == READ_ONLY:
        turn_boundary = (
            "This turn is read-only. Only inspection, search, planning, and user-input tools are allowed; "
            "do not modify project files, runtime state, memory, configuration, or external systems."
        )
    elif policy.mode == SCOPED_WRITE:
        roots = ", ".join(f"`{root}`" for root in policy.allowed_write_roots)
        core_boundary = (
            "Generic write_file/edit_file tools may write only inside those roots."
            if policy.core_writes_allowed
            else "Generic core mutation tools are disabled; only the guarded Storydex domain mutator exposed for this turn may change state."
        )
        turn_boundary = (
            f"This turn has scoped-write capability limited to: {roots}. {core_boundary} "
            "Shell, patch, memory, configuration, Skill-installation, delegation, and unknown MCP mutations are outside this capability."
        )
    elif policy.mode == FULL_ACCESS:
        turn_boundary = (
            "This turn requests full-access capability, but the user's session permission remains an independent upper bound "
            "and may reduce it to workspace-only or require approval."
        )
    else:
        turn_boundary = (
            "This turn has workspace-write capability. File changes must remain inside the Storydex workspace; "
            "the user's session permission may require approval for higher-risk tools."
        )
    if plan_mode:
        return (
            "Plan mode is active as a persistent read-only session overlay. Only read/plan tools and exit_plan_mode "
            "are currently exposed. If exit_plan_mode succeeds, continue only within the turn capability below; "
            "exiting plan mode does not grant new authority.\n" + turn_boundary
        )
    return (
        "Plan mode is inactive. The following compiled turn capability is the hard tool boundary; natural-language "
        "instructions and model interpretation may narrow it but cannot widen it.\n" + turn_boundary
    )


def _render_story_generation_options(
    value: Dict[str, Any] | None,
    *,
    operation_type: str = "",
    include_length: bool = True,
) -> str:
    payload = value if isinstance(value, dict) else {}
    fragment_count = _positive_int(payload.get("fragmentCount"), default=1)
    raw_tier = payload.get("chapterLengthTier", payload.get("chapter_length_tier"))
    tier_mode = raw_tier is not None
    chapter_target, accept_min, accept_max = (
        (0, 0, 0) if tier_mode else _story_word_count_prompt_values(payload)
    )
    chapter_template = str(
        payload.get("chapterTemplateId") or payload.get("chapterTemplate") or ""
    ).strip()
    length_line = (
        f"- chapterLengthTier: {chapter_length_tier_prompt(raw_tier)}"
        if tier_mode and include_length
        else f"- chapterLengthGuidance: target about {chapter_target} non-whitespace characters; "
        f"{accept_min}-{accept_max} is acceptable"
        if include_length
        else ""
    )
    if str(operation_type or "").strip().lower() == "modify_existing":
        return (
            "\nStory generation turn options (modify_existing - soft reference only):\n"
            + f"- fragmentCount: {fragment_count} (reference, NOT a mandate to create new fragments)\n"
            + (f"{length_line} (soft guide for edited content)\n" if length_line else "")
            + (f"- chapterTemplateId: {chapter_template}\n" if chapter_template else "")
            + "This turn edits/restructures existing files; do not treat these values as new-fragment creation constraints.\n"
        )
    return (
        "\nStory generation turn options:\n"
        + f"- fragmentCount: {fragment_count}\n"
        + (f"{length_line}.\n" if length_line else "")
        + (f"- chapterTemplateId: {chapter_template}\n" if chapter_template else "")
        + "For story creation or continuation turns, fragment paths and the chapter template remain binding.\n"
    )


def _render_turn_contract(value: Dict[str, Any] | None) -> str:
    contract = value if isinstance(value, dict) else {}
    if not contract:
        return ""
    intent = _dict_value(contract.get("intentFrame"))
    execution = _dict_value(contract.get("executionPolicy"))
    turn_plan = _dict_value(contract.get("turnPlan"))
    context_policy = _dict_value(contract.get("contextPolicy"))
    skill_registry = _dict_value(contract.get("skillRegistry"))
    context_assembly = _dict_value(contract.get("contextAssembly"))
    update_policy = _dict_value(contract.get("updatePolicy"))
    knowledge_write_policy = _dict_value(contract.get("knowledgeWritePolicy"))
    route_hints = _dict_value(contract.get("routeHints"))
    intent_routing = _dict_value(contract.get("intentRouting"))
    capability_policy = resolve_capability_policy(contract, plan_mode=False)

    primary = str(intent.get("primary") or "general")
    confidence = str(intent.get("confidence") or "low")
    operation_type = str(intent.get("operationType") or "").strip().lower()
    complexity = str(intent.get("complexity") or "").strip().lower()
    intent_targets = [
        str(item)
        for item in (
            intent.get("assetTargets") if isinstance(intent.get("assetTargets"), list) else []
        )
        if str(item)
    ]
    intent_skills = [
        str(item)
        for item in (
            intent.get("matchedSkills") if isinstance(intent.get("matchedSkills"), list) else []
        )
        if str(item)
    ]
    intent_line = f"- intent: {primary} (confidence: {confidence})"
    if operation_type:
        intent_line += f"; operationType: {operation_type}"
    if complexity:
        intent_line += f"; complexity: {complexity}"
    if intent_targets:
        intent_line += f"; write this intent's outputs under: {', '.join(intent_targets)}"
    if intent_skills:
        intent_line += f"; matching skills: {', '.join(intent_skills)}"
    status = str(contract.get("status") or "ready")
    fragment_count = _positive_int(turn_plan.get("fragmentCount"), default=1)
    word_count_policy = _dict_value(turn_plan.get("wordCountPolicy"))
    tier_mode = str(word_count_policy.get("mode") or "").strip().lower() == "tier"
    chapter_length_tier = normalize_chapter_length_tier(
        turn_plan.get("chapterLengthTier", word_count_policy.get("tier"))
    )
    chapter_target, accept_min, accept_max = (
        (0, 0, 0) if tier_mode else _story_word_count_prompt_values(turn_plan)
    )
    calibration = _dict_value(word_count_policy.get("calibration"))
    reference_label = (
        "calibrated generation reference"
        if str(calibration.get("status") or "").strip().lower() == "applied"
        else "generation reference"
    )
    requires_template = bool(turn_plan.get("requiresChapterTemplateSelection"))
    selected_template = str(turn_plan.get("selectedChapterTemplate") or "").strip()
    selected_template_detail = _dict_value(turn_plan.get("selectedChapterTemplateDetail"))
    invalid_template = str(turn_plan.get("invalidChapterTemplate") or "").strip()
    next_segment_path = str(turn_plan.get("nextSegmentPath") or "").strip()
    chapter_content_mode = str(turn_plan.get("chapterContentMode") or "multi_fragment").strip()
    fragment_targets = (
        turn_plan.get("fragmentTargets")
        if isinstance(turn_plan.get("fragmentTargets"), list)
        else []
    )

    lines = [
        "\nStorydex turn contract:",
        f"- status: {status}",
        intent_line,
        (
            "- intentRouting: "
            f"mode={str(intent_routing.get('mode') or intent.get('routingMode') or 'legacy')}, "
            f"intentModelInvoked={bool(intent_routing.get('intentModelInvoked', intent.get('intentModelInvoked', False)))}, "
            f"classifierMethod={str(intent_routing.get('classifierMethod') or intent.get('method') or 'unknown')}"
        ),
        _route_hints_prompt_line(route_hints),
        (
            "- execution: "
            f"capabilityMode={capability_policy.mode}, "
            f"directFileWrites={bool(execution.get('directFileWrites', False))}, "
            f"pendingWriteApproval={bool(execution.get('pendingWriteApproval', False))}, "
            f"localGitAutoCommit={bool(execution.get('localGitAutoCommit', True))}, "
            f"remotePush={bool(execution.get('remotePush', False))}"
        ),
        (
            "- allowedWriteRoots: "
            + (", ".join(capability_policy.allowed_write_roots) or "none")
        ),
        "- permissionBoundary: the compiled per-turn capability and the user's session permission are both enforced; "
        "natural-language instructions may narrow this boundary but cannot grant additional write authority. /plan is a separate persistent read-only overlay.",
        (
            "- knowledgeWritePolicy: "
            f"mode={str(knowledge_write_policy.get('mode') or 'standard')}, "
            f"confirmationRequired={bool(knowledge_write_policy.get('confirmationRequired'))}, "
            f"confirmed={bool(knowledge_write_policy.get('confirmed'))}; "
            + (
                "only apply_explicit is allowed in this later confirmation turn."
                if bool(knowledge_write_policy.get("confirmed"))
                else "only prepare_explicit is allowed; do not modify formal knowledge files."
            )
        )
        if str(knowledge_write_policy.get("mode") or "") == "explicit_binding"
        else "",
        f"- storyFragments: count={fragment_count}",
        f"- chapterContentMode: {chapter_content_mode}",
    ]

    if requires_template:
        lines.append(
            "- requiresChapterTemplateSelection: true. Ask the user to choose a chapter directory template; "
            "do not generate or write story content until a template is selected."
        )
        if invalid_template:
            lines.append(f"- invalidChapterTemplate: {invalid_template}")
        template_labels = _chapter_template_labels(turn_plan.get("availableChapterTemplates"))
        if template_labels:
            lines.append(f"- availableChapterTemplates: {', '.join(template_labels)}")
    elif selected_template:
        lines.append(
            f"- selectedChapterTemplate: {_chapter_template_detail_label(selected_template_detail, selected_template)}"
        )
        template_rules = _chapter_template_rules(selected_template_detail)
        if template_rules:
            lines.append(f"- selectedTemplateRules: {template_rules}")
    if next_segment_path:
        lines.append(f"- nextSegmentPath: {next_segment_path}")

    target_paths = [
        str(item.get("path") or "")
        for item in fragment_targets
        if isinstance(item, dict) and str(item.get("path") or "")
    ]
    if target_paths:
        lines.append(f"- authoritativeFragmentPaths: {', '.join(target_paths)}")
    reference_labels: list[str] = []
    for index, item in enumerate(fragment_targets):
        if not isinstance(item, dict):
            continue
        reference = _bounded_int(
            item.get("referenceWordCount"), default=0, minimum=0, maximum=20000
        )
        if reference > 0 and not tier_mode:
            reference_labels.append(f"{index + 1}:{str(item.get('path') or '')}~{reference}")
    if not tier_mode and fragment_count > 1 and reference_labels:
        paragraph_quota = _bounded_int(
            word_count_policy.get("paragraphQuota"), default=0, minimum=0, maximum=1000
        )
        if paragraph_quota > 0 and target_paths:
            shares = _split_paragraph_quota(paragraph_quota, len(target_paths))
            lines.append(
                "- softFragmentParagraphReferences: "
                + ", ".join(
                    f"{index + 1}:{path}~{share}"
                    for index, (path, share) in enumerate(zip(target_paths, shares))
                )
                + " paragraphs each; planning references only. Only the chapter-level paragraphQuota below is binding."
            )
        else:
            lines.append(
                "- softFragmentReferences: "
                + ", ".join(reference_labels)
                + " non-whitespace characters each; planning references only, never hard per-fragment limits."
            )

    if operation_type == "modify_existing":
        if not tier_mode:
            lines.append(
                f"- wordCountGuidance: {reference_label} is about {chapter_target} Storydex non-whitespace "
                f"characters; edited chapter content should finish within {accept_min}-{accept_max} when applicable. "
                "Treat this as a soft editing guide, not a mandate to create new text."
            )
        lines.append(
            "- operationDiscipline (modify_existing): the user wants to restructure/reorganize/rewrite/adjust "
            "or clean up files that ALREADY exist. First READ and understand the relevant existing files "
            "(use StorydexProjectSearch / reads) before changing anything. Edit those existing files in place; "
            "when a file is superseded, delete or overwrite the old one - do NOT create parallel new fragments. "
            "Do NOT treat fragmentCount/word-count as a mandate to generate N brand-new fragments. There are no "
            "authoritative new fragment paths this turn; chapter length is a soft guide for edited content, "
            "not a hard new-fragment quota. StorydexApplyStoryIncrement's new-fragment validation is not enforced."
        )
    elif primary == "story_generation" and operation_type in {"", "create_new"}:
        paragraph_quota_line = (
            "" if tier_mode else _story_paragraph_quota_prompt_line(word_count_policy)
        )
        lines.append(
            "- operationDiscipline (create_new): generate new story content into the authoritative fragment paths above. "
            "The fragment count and chapter template are binding creation constraints; "
            + (
                "chapter length follows the semantic tier below."
                if tier_mode
                else "chapter length is set by the paragraphQuota below."
                if paragraph_quota_line
                else "chapter length remains guidance."
            )
        )
        if tier_mode:
            lines.append(chapter_length_tier_prompt(chapter_length_tier))
        elif paragraph_quota_line:
            lines.append(paragraph_quota_line)
        else:
            lines.append(
                f"- wordCountGuidance: {reference_label} is about {chapter_target} Storydex non-whitespace "
                f"characters; the completed chapter should finish within {accept_min}-{accept_max}. "
                "Complete the core turn, then close naturally near the reference; compress repeated explanation first "
                "while preserving necessary action, character thought, dialogue, and causal transitions. "
                "Never pad, repeat, mention the word count, or cut off a scene merely to hit length."
            )
    elif operation_type in {"inquiry", "greeting", "other"}:
        lines.append(
            "- operationGuidance (respond_only): the intent classifier currently predicts an informational turn. "
            "Treat this as routing guidance while obeying the compiled capability boundary. If the actual request "
            "requires broader writes than this turn permits, explain the mismatch instead of attempting them."
        )

    lines.append(
        "- context: inject active or compiled-safe presets only; use recent active characters and relevant facts, "
        "not a full memory dump. The active preset block below contains binding creative rules for this turn; "
        "follow it faithfully when writing story content."
        + (
            " Presets do not decide chapter length: they decide paragraph shape and prose density, "
            "while the paragraphQuota above decides how many paragraphs the chapter has. Ignore any "
            "preset wording that claims priority over it or states a target character/word count."
            if _story_paragraph_quota_prompt_line(word_count_policy)
            else ""
        )
    )
    skill_summary = _skill_registry_summary(skill_registry)
    if skill_summary:
        lines.append(f"- skillRegistry: {skill_summary}")
    context_summary = _context_assembly_summary(context_assembly)
    if context_summary:
        lines.append(f"- contextAssembly: {context_summary}")
    lines.append(
        "- variableThinking: Markdown/natural language first with clear constraints, changes, conflicts, "
        "and manual-confirmation notes; strict JSON path/value entries are not required."
    )
    if str(context_policy.get("machineVariableOperations") or "") == "optional":
        lines.append("- variableUpdates: optional machine sidecar only when the merge is safe and obvious.")
    lines.append(
        "- updates: "
        f"autoUpdateVariables={bool(update_policy.get('autoUpdateVariables', False))}, "
        f"autoUpdateWiki={bool(update_policy.get('autoUpdateWiki', False))}"
    )
    lines.append(
        "- generatedMemory: safe, evidence-grounded memory deltas included with a newly generated fragment "
        "are applied immediately unless the caller explicitly sets applyVariables=false; review-required "
        "operations remain pending."
    )
    context_blocks = _render_context_assembly_blocks(context_assembly)
    if context_blocks:
        lines.extend(["", "Storydex assembled context blocks:", context_blocks])
    return "\n".join(lines) + "\n"


def _route_hints_prompt_line(value: Dict[str, Any]) -> str:
    if not value:
        return ""
    pieces: list[str] = []
    for key in (
        "explicitPaths",
        "candidatePaths",
        "namedEntities",
        "requestedFields",
        "documentKinds",
        "operationSignals",
    ):
        raw = value.get(key)
        labels = [str(item) for item in raw[:8] if str(item)] if isinstance(raw, list) else []
        if labels:
            pieces.append(f"{key}={','.join(labels)}")
    active_file = str(value.get("activeFile") or "").strip()
    if active_file:
        pieces.append(f"activeFile={active_file}")
    detail = "; ".join(pieces) or "no concrete location hint"
    return (
        "- routeHints (advisoryOnly=true): "
        + detail
        + ". Use these only to choose initial reads/searches; verify evidence with tools. "
        "Route hints never grant write permission or prove a story fact."
    )


def _skill_registry_summary(value: Dict[str, Any]) -> str:
    if not value:
        return ""
    registry_path = str(
        value.get("registryPath") or ".storydex/.agent/skills/registry.json"
    ).strip()
    skills = value.get("skills") if isinstance(value.get("skills"), list) else []
    labels: list[str] = []
    for item in skills[:10]:
        skill = _dict_value(item)
        skill_id = str(skill.get("id") or "").strip()
        file_name = str(skill.get("file") or "").strip()
        if skill_id or file_name:
            labels.append(f"{skill_id or file_name}:{file_name or skill_id}")
    count = int(value.get("skillCount") or len(skills))
    return f"{count} skills at {registry_path}" + (
        f" ({', '.join(labels)})" if labels else ""
    )


def _context_assembly_summary(value: Dict[str, Any]) -> str:
    if not value:
        return ""
    budget = _dict_value(value.get("budget"))
    sources = value.get("sources") if isinstance(value.get("sources"), list) else []
    pieces: list[str] = []
    for item in sources[:8]:
        source = _dict_value(item)
        kind = str(source.get("kind") or "").strip()
        count = int(source.get("count") or 0)
        if kind:
            pieces.append(f"{kind}={count}")
    block_count = int(budget.get("blockCount") or 0)
    total_chars = int(budget.get("totalChars") or 0)
    head = f"{block_count} blocks / {total_chars} chars"
    return head + (f" ({', '.join(pieces)})" if pieces else "")


def _render_context_assembly_blocks(value: Dict[str, Any]) -> str:
    if not value:
        return ""
    blocks = value.get("promptBlocks") if isinstance(value.get("promptBlocks"), list) else []
    rendered: list[str] = []
    for raw in blocks[:14]:
        block = _dict_value(raw)
        content = str(block.get("content") or "").strip()
        omitted = bool(block.get("omitted"))
        truncated = bool(block.get("truncated")) and not omitted
        drop_reason = str(block.get("dropReason") or "").strip()
        if omitted:
            content = f"[omitted: {drop_reason or 'context_budget'}]"
        elif not content:
            continue
        elif truncated:
            content = f"[truncated: {drop_reason or 'context_budget'}]\n{content}"
        title = str(block.get("title") or block.get("id") or "Context").strip()
        source_paths = (
            block.get("sourcePaths") if isinstance(block.get("sourcePaths"), list) else []
        )
        source_suffix = ", ".join(
            str(path) for path in source_paths[:4] if str(path).strip()
        )
        heading = f"### {title}"
        if source_suffix:
            heading += f" [{source_suffix}]"
        rendered.extend([heading, content])
    return "\n\n".join(rendered)


def _chapter_template_labels(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    labels: list[str] = []
    for item in value[:5]:
        template = _dict_value(item)
        template_id = str(template.get("id") or "").strip()
        name = str(template.get("name") or "").strip()
        relative_path = str(template.get("relativePath") or "").strip()
        label = name or template_id or relative_path
        if template_id and name and template_id != name:
            label = f"{name} ({template_id})"
        if label:
            labels.append(label)
    return labels


def _chapter_template_detail_label(value: Dict[str, Any], fallback: str) -> str:
    name = str(value.get("name") or "").strip()
    template_id = str(value.get("id") or fallback).strip()
    return (
        f"{name} ({template_id})"
        if name and template_id and name != template_id
        else template_id or name or fallback
    )


def _chapter_template_rules(value: Dict[str, Any]) -> str:
    pieces: list[str] = []
    chapter_mode = str(value.get("chapterMode") or "").strip()
    content_mode = str(value.get("contentMode") or "").strip()
    chapter_pattern = str(value.get("chapterNamePattern") or "").strip()
    segment_naming = str(value.get("segmentNaming") or "").strip()
    initial_directory = str(value.get("initialChapterDirectory") or "").strip()
    initial_segment = str(value.get("initialChapterFirstSegment") or "").strip()
    if chapter_mode:
        pieces.append(f"mode={chapter_mode}")
    if content_mode:
        pieces.append(f"contentMode={content_mode}")
    if chapter_pattern:
        pieces.append(f"chapterNamePattern={chapter_pattern}")
    if segment_naming:
        pieces.append(f"segmentNaming={segment_naming}")
    if initial_directory or initial_segment:
        pieces.append(f"initial={initial_directory}/{initial_segment}".strip("/"))
    rules = value.get("rules") if isinstance(value.get("rules"), list) else []
    if rules:
        pieces.append("rules=" + " | ".join(str(item) for item in rules if str(item).strip()))
    return ", ".join(pieces)


def _resolve_word_count_range(payload: Dict[str, Any]) -> tuple[int, int]:
    policy = _dict_value(payload.get("wordCountPolicy"))
    raw_target = payload.get(
        "chapterWordCountTarget", payload.get("chapter_word_count_target")
    )
    if raw_target is not None and str(policy.get("mode") or "target") != "range":
        target = _bounded_int(
            raw_target,
            default=DEFAULT_CHAPTER_WORD_COUNT_TARGET,
            minimum=100,
            maximum=20000,
        )
        return target, target
    raw_min = payload.get("fragmentWordCountMin")
    raw_max = payload.get("fragmentWordCountMax")
    if raw_min is None and raw_max is None:
        legacy = _bounded_int(
            payload.get("fragmentWordCount"),
            default=DEFAULT_CHAPTER_WORD_COUNT_TARGET,
            minimum=100,
            maximum=20000,
        )
        return legacy, legacy
    min_value = _bounded_int(
        raw_min,
        default=DEFAULT_CHAPTER_WORD_COUNT_TARGET,
        minimum=100,
        maximum=20000,
    )
    max_value = _bounded_int(
        raw_max,
        default=DEFAULT_CHAPTER_WORD_COUNT_TARGET,
        minimum=100,
        maximum=20000,
    )
    if min_value > max_value:
        min_value, max_value = max_value, min_value
    return min_value, max_value


def _story_word_count_prompt_values(payload: Dict[str, Any]) -> tuple[int, int, int]:
    minimum, maximum = _resolve_word_count_range(payload)
    policy = _dict_value(payload.get("wordCountPolicy"))
    raw_target = policy.get(
        "modelReferenceWordCount",
        payload.get("chapterWordCountTarget", policy.get("target")),
    )
    chapter_target = _bounded_int(
        raw_target if raw_target is not None else round((minimum + maximum) / 2),
        default=DEFAULT_CHAPTER_WORD_COUNT_TARGET,
        minimum=50,
        maximum=25000,
    )
    default_accept_min = max(50, round(minimum * 0.70))
    default_accept_max = round(maximum * 1.30)
    accept_min = _bounded_int(
        policy.get("acceptanceMinimum"),
        default=default_accept_min,
        minimum=1,
        maximum=25000,
    )
    accept_max = _bounded_int(
        policy.get("acceptanceMaximum"),
        default=default_accept_max,
        minimum=1,
        maximum=25000,
    )
    if accept_min > accept_max:
        accept_min, accept_max = accept_max, accept_min
    return chapter_target, accept_min, accept_max


def _story_paragraph_quota_prompt_line(policy: Dict[str, Any]) -> str:
    quota = _bounded_int(policy.get("paragraphQuota"), default=0, minimum=0, maximum=1000)
    if quota <= 0:
        return ""
    minimum = _bounded_int(
        policy.get("paragraphQuotaMinimum"), default=quota, minimum=1, maximum=1000
    )
    maximum = _bounded_int(
        policy.get("paragraphQuotaMaximum"), default=quota, minimum=1, maximum=1000
    )
    if minimum > maximum:
        minimum, maximum = maximum, minimum
    return (
        f"- paragraphQuota: write the completed chapter as {minimum}-{maximum} paragraphs separated "
        f"by blank lines (aim for {quota}). This paragraph count is the ONLY length instruction that "
        "applies this turn: do not aim at any character or word count, and never mention the count "
        "in the prose. How long each paragraph runs is a style decision owned by the active preset; "
        "follow the preset for paragraph shape and prose density, and let paragraph lengths vary "
        "naturally. Never split a sentence across paragraphs to reach the count, and never pad, "
        "repeat, or summarise to fill one."
    )


def _split_paragraph_quota(total: int, parts: int) -> list[int]:
    count = max(1, int(parts))
    normalized_total = max(0, int(total))
    base = normalized_total // count
    remainder = normalized_total - base * count
    return [base + (1 if index < remainder else 0) for index in range(count)]


def _semantic_story_output_tool(
    purpose: str, metadata: Dict[str, Any]
) -> tuple[list[Dict[str, Any]] | None, int]:
    if purpose not in {"semantic_budget_scene", "semantic_budget_revision"}:
        return None, 0
    try:
        desired = int(metadata.get("desiredWordCount") or 0)
    except (TypeError, ValueError):
        return None, 0
    if desired <= 0:
        return None, 0
    paragraph_count = max(
        _SEMANTIC_STORY_PARAGRAPH_MINIMUM,
        min(
            _SEMANTIC_STORY_PARAGRAPH_MAXIMUM,
            int(round(desired / _SEMANTIC_STORY_PARAGRAPH_IDEAL_CHARS)),
        ),
    )
    per_paragraph = desired / paragraph_count
    minimum_length = max(20, int(per_paragraph * _SEMANTIC_STORY_LENGTH_MIN_RATIO))
    maximum_length = max(
        minimum_length,
        int(math.ceil(per_paragraph * _SEMANTIC_STORY_LENGTH_MAX_RATIO)),
    )
    properties: Dict[str, Any] = {}
    required: list[str] = []
    for index in range(1, paragraph_count + 1):
        name = f"paragraph_{index}"
        required.append(name)
        if index == 1:
            role = "承接紧邻前文并建立当前场景的直接压力，不复述背景"
        elif index == paragraph_count:
            role = "明确执行场景计划的 exitHook，形成自然离场或衔接"
        elif index == paragraph_count - 1:
            role = "写清行动结果、代价和人物反应，不引入新支线"
        else:
            role = "推进 development 中的因果动作、冲突和人物反应"
        properties[name] = {
            "type": "string",
            "minLength": minimum_length,
            "maxLength": maximum_length,
            "description": f"第 {index} 个自然段：{role}；只写一到两句中文小说正文。",
        }
    tool = {
        "type": "function",
        "function": {
            "name": _SEMANTIC_STORY_TOOL_NAME,
            "description": (
                f"按顺序提交恰好 {paragraph_count} 个自然段的当前小说场景；"
                "字段内容拼接后就是最终正文。"
            ),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }
    return [tool], paragraph_count


def _semantic_story_tool_messages(messages: list[Dict[str, str]]) -> list[Dict[str, str]]:
    output = [dict(message) for message in messages]
    instruction = (
        f"如本次请求提供 {_SEMANTIC_STORY_TOOL_NAME} 工具，必须调用该工具提交正文；"
        "工具字段只是传输结构，不得在字段中输出标题、编号或解释。"
    )
    for message in output:
        if message.get("role") == "system":
            message["content"] = str(message.get("content") or "") + instruction
            break
    else:
        output.insert(0, {"role": "system", "content": instruction.strip()})
    return output


def _semantic_story_response_content(response: Any, paragraph_count: int) -> str:
    for call in list(getattr(response, "tool_calls", None) or []):
        if str(getattr(call, "name", "") or "") != _SEMANTIC_STORY_TOOL_NAME:
            continue
        arguments = getattr(call, "arguments", None)
        if isinstance(arguments, dict):
            return "\n\n".join(
                str(arguments.get(f"paragraph_{index}") or "").strip()
                for index in range(1, paragraph_count + 1)
                if str(arguments.get(f"paragraph_{index}") or "").strip()
            )
    return str(getattr(response, "content", "") or "")


def _tool_call_arguments(
    response: Any,
    tool_name: str,
    *,
    completion_cap_hit: bool = False,
) -> Dict[str, Any] | None:
    wanted = str(tool_name or "")
    for call in list(getattr(response, "tool_calls", None) or []):
        name = str(getattr(call, "name", "") or "")
        if not name:
            name = str(getattr(getattr(call, "function", None), "name", "") or "")
        if name != wanted:
            continue
        arguments = getattr(call, "arguments", None)
        if arguments is None:
            arguments = getattr(getattr(call, "function", None), "arguments", None)
        parse_error = getattr(call, "parse_error", None)
        if parse_error:
            finish_reason = str(
                getattr(response, "finish_reason", None)
                or getattr(response, "finishReason", None)
                or ""
            ).strip().casefold()
            reason = (
                "tool_arguments_truncated"
                if finish_reason == "length" or completion_cap_hit
                else "tool_arguments_invalid_json"
            )
            raise StorydexToolCallRejected(reason)
        if arguments is None or (isinstance(arguments, str) and not arguments.strip()):
            raise StorydexToolCallRejected("tool_arguments_empty")
        if isinstance(arguments, dict):
            if not arguments:
                raise StorydexToolCallRejected("tool_arguments_empty")
            return arguments
        if isinstance(arguments, str) and arguments.strip():
            try:
                decoded = json.loads(arguments)
            except ValueError:
                finish_reason = str(
                    getattr(response, "finish_reason", None)
                    or getattr(response, "finishReason", None)
                    or ""
                ).strip().casefold()
                reason = (
                    "tool_arguments_truncated"
                    if finish_reason == "length" or completion_cap_hit
                    else "tool_arguments_invalid_json"
                )
                raise StorydexToolCallRejected(reason)
            if isinstance(decoded, dict):
                if not decoded:
                    raise StorydexToolCallRejected("tool_arguments_empty")
                return decoded
            raise StorydexToolCallRejected("tool_arguments_invalid_patch")
        raise StorydexToolCallRejected("tool_arguments_invalid_patch")
    return None


def _tool_call_diagnostics(
    response: Any,
    tool_name: str,
    *,
    cap_applied: bool,
    max_completion_tokens: int,
    completion_tokens: int | None,
) -> Dict[str, Any]:
    wanted = str(tool_name or "")
    target_call: Any = None
    for call in list(getattr(response, "tool_calls", None) or []):
        name = str(getattr(call, "name", "") or "")
        if not name:
            name = str(getattr(getattr(call, "function", None), "name", "") or "")
        if name == wanted:
            target_call = call
            break

    arguments = getattr(target_call, "arguments", None) if target_call is not None else None
    if arguments is None and target_call is not None:
        arguments = getattr(getattr(target_call, "function", None), "arguments", None)
    raw_arguments = getattr(target_call, "raw_arguments", None) if target_call is not None else None
    parse_error = getattr(target_call, "parse_error", None) if target_call is not None else None
    finish_reason = str(
        getattr(response, "finish_reason", None)
        or getattr(response, "finishReason", None)
        or ""
    )
    limit = max(0, int(max_completion_tokens or 0))
    cap_hit = bool(
        cap_applied
        and limit > 0
        and completion_tokens is not None
        and int(completion_tokens) >= limit
    )
    return {
        "finishReason": finish_reason,
        "targetToolPresent": target_call is not None,
        "rawArgumentsLength": len(raw_arguments) if isinstance(raw_arguments, str) else None,
        "toolArgumentsEmpty": bool(
            target_call is not None
            and (
                arguments is None
                or (isinstance(arguments, dict) and not arguments)
                or (isinstance(arguments, str) and not arguments.strip())
            )
        ),
        "parseErrorPresent": bool(parse_error),
        "completionCapHit": cap_hit,
    }


async def _call_provider_chat(
    provider: Any, messages: list[Dict[str, Any]], tools: Any, **kwargs: Any
) -> Any:
    chat = getattr(provider, "chat")
    if inspect.iscoroutinefunction(chat):
        return await chat(messages, tools, **kwargs)
    response = await asyncio.to_thread(chat, messages, tools, **kwargs)
    return await response if inspect.isawaitable(response) else response


@contextmanager
def _storydex_coomi_home() -> Iterator[None]:
    _ensure_storydex_coomi_config()
    yield


def _ensure_storydex_coomi_config() -> Path:
    path = Path(STORYDEX_COOMI_CONFIG)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text('{\n  "version": 1,\n  "active": "",\n  "providers": {}\n}\n', encoding="utf-8")
    return path


def _read_providers_config_payload() -> Dict[str, Any]:
    path = _ensure_storydex_coomi_config()
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _invalidate_bridge_status_cache() -> None:
    global _BRIDGE_STATUS_CACHE
    with _BRIDGE_STATUS_CACHE_LOCK:
        _BRIDGE_STATUS_CACHE = None


def _bridge_status_snapshot(*, probe: bool = True) -> Dict[str, Any]:
    """Read capability data once per provider config and bridge build."""
    global _BRIDGE_STATUS_CACHE
    path = Path(STORYDEX_COOMI_CONFIG)
    try:
        stat = path.stat()
        command = bridge_command()
        command_identity = "\0".join(str(part) for part in command)
        runtime_path = Path(command[0])
        try:
            runtime_stat = runtime_path.stat()
            runtime_mtime_ns = int(runtime_stat.st_mtime_ns)
            runtime_size = int(runtime_stat.st_size)
        except OSError:
            runtime_mtime_ns = 0
            runtime_size = 0
        key = (
            str(path.resolve()),
            int(stat.st_mtime_ns),
            int(stat.st_size),
            command_identity,
            runtime_mtime_ns,
            runtime_size,
        )
    except (OSError, CoomiBridgeError):
        return {}
    with _BRIDGE_STATUS_CACHE_LOCK:
        if _BRIDGE_STATUS_CACHE is not None and _BRIDGE_STATUS_CACHE[0] == key:
            return copy.deepcopy(_BRIDGE_STATUS_CACHE[1])
    if not probe:
        return {}
    # Serialize cache misses so simultaneous status endpoints do not spawn
    # duplicate read-only bridge processes. Execution paths never take this
    # lock because they call `_bridge_status_snapshot(probe=False)`.
    with _BRIDGE_STATUS_PROBE_LOCK:
        with _BRIDGE_STATUS_CACHE_LOCK:
            if _BRIDGE_STATUS_CACHE is not None and _BRIDGE_STATUS_CACHE[0] == key:
                return copy.deepcopy(_BRIDGE_STATUS_CACHE[1])
        try:
            packet = request_status_sync()
        except Exception:
            return {}
        data = packet.get("data") if isinstance(packet, dict) else None
        if not isinstance(data, dict) or packet.get("type") != "status":
            return {}
        with _BRIDGE_STATUS_CACHE_LOCK:
            _BRIDGE_STATUS_CACHE = (key, copy.deepcopy(data))
        return data


def _resolve_context_window() -> int:
    payload = _read_providers_config_payload()
    providers = payload.get("providers") if isinstance(payload.get("providers"), dict) else {}
    active = providers.get(str(payload.get("active") or ""))
    provider = active if isinstance(active, dict) else {}
    for source in (provider, payload):
        for key in CONTEXT_WINDOW_KEYS:
            try:
                value = int(source.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if value:
                return max(MIN_CONTEXT_WINDOW, min(MAX_CONTEXT_WINDOW, value))
    return DEFAULT_CONTEXT_WINDOW


def _context_snapshot_from_session_file(
    *, workspace_root: Path, session_id: str
) -> Dict[str, Any]:
    binding = _read_coomi_session_binding(
        workspace_root=workspace_root,
        storydex_session_id=session_id,
    )
    path = _validated_session_path(binding)
    if path is None or not path.is_file():
        return _context_snapshot_from_bridge({})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return _context_snapshot_from_bridge({})
    return _context_snapshot_from_bridge(payload if isinstance(payload, dict) else {})


def _context_snapshot_from_bridge(
    value: Dict[str, Any], *, fallback: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    previous = fallback if isinstance(fallback, dict) else {}
    context = value.get("context") if isinstance(value.get("context"), dict) else value
    window = int(context.get("context_window") or context.get("effective_context_window") or _resolve_context_window())
    used = int(
        context.get("used_tokens")
        or context.get("estimated_active_tokens")
        or previous.get("usedTokens")
        or 0
    )
    usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
    cumulative = int(
        context.get("cumulative_tokens")
        or usage.get("total_tokens")
        or (
            int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            + int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        )
        or previous.get("cumulativeTokens")
        or 0
    )
    ratio = used / max(1, window)
    return {
        "contextWindow": window,
        "usedTokens": used,
        "usageRatio": ratio,
        "cumulativeTokens": cumulative,
        "compactThreshold": int(context.get("auto_compact_token_limit") or window * COMPACT_THRESHOLD_RATIO),
        "warningThreshold": int(window * WARNING_THRESHOLD_RATIO),
        "compressionStatus": "idle",
    }


def _usage_aliases(value: Dict[str, Any]) -> Dict[str, Any]:
    input_tokens = int(value.get("input_tokens") or value.get("prompt_tokens") or 0)
    output_tokens = int(value.get("output_tokens") or value.get("completion_tokens") or 0)
    output = {
        **value,
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "promptTokens": input_tokens,
        "completionTokens": output_tokens,
        "totalTokens": input_tokens + output_tokens,
    }
    reasoning = value.get("reasoning_tokens")
    if reasoning is None:
        reasoning = value.get("reasoningTokens")
    if reasoning is not None:
        output["reasoning_tokens"] = int(reasoning or 0)
        output["reasoningTokens"] = int(reasoning or 0)
    return output


def _normalize_permission_mode(mode: str) -> str:
    normalized = str(mode or "").strip().lower().replace("-", "_")
    aliases = {"ask": "ask_approval", "approve": "approve_for_me", "auto": "approve_for_me", "full": "full_access"}
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in {"ask_approval", "approve_for_me", "full_access"} else "ask_approval"


def _permission_label(mode: str) -> str:
    return {
        "ask_approval": "Ask approval",
        "approve_for_me": "Approve for me",
        "full_access": "Full access",
    }.get(_normalize_permission_mode(mode), "Ask approval")


def _approval_options(value: Any, *, is_permission: bool) -> list[Dict[str, Any]]:
    if isinstance(value, list):
        output = []
        for item in value:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or item.get("value") or "").strip()
            if label:
                output.append(
                    {
                        "label": label,
                        "value": str(item.get("value") or label),
                        "description": str(item.get("description") or ""),
                        "isRecommended": bool(item.get("isRecommended")),
                    }
                )
        if output:
            return output
    if not is_permission:
        return [{"label": "Answer", "value": "answer", "description": "Provide an answer.", "isRecommended": True}]
    return [
        {"label": "Allow", "value": "allow", "description": "Run this tool call once.", "isRecommended": True},
        {"label": "Deny", "value": "deny", "description": "Return permission denied.", "isRecommended": False},
    ]


def _user_input_answer(response: Dict[str, Any], decision: str) -> str:
    """Normalize answer payloads emitted by current and older Storydex clients."""
    for key in ("otherText", "other_text", "answer", "value", "option", "label"):
        value = response.get(key)
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    fallback = str(decision or "").strip()
    return "" if fallback == "answer" else fallback


def _parse_slash_command(prompt: str) -> Dict[str, str]:
    text = str(prompt or "").strip()
    if not text.startswith("/"):
        return {"name": "", "body": ""}
    head, _, body = text.partition(" ")
    return {"name": head.lstrip("/").lower(), "body": body.strip()}


def _is_cancelled(token: Any) -> bool:
    checker = getattr(token, "is_cancelled", None)
    try:
        return bool(checker()) if callable(checker) else False
    except Exception:
        return False


def _agent_started(
    *, session_id: str, prompt: str, status: Dict[str, Any], mode: str
) -> tuple[str, Dict[str, Any]]:
    return "AgentStarted", {
        "_type": "AgentStarted",
        "_version": 1,
        "session_id": session_id,
        "mode": mode,
        "query": prompt,
        "llmModel": str(status.get("model") or ""),
        "llmProvider": str(status.get("providerId") or ""),
        "coomiStatus": status,
    }


def _completed_event(session_id: str, started: float, total_tokens: int) -> tuple[str, Dict[str, Any]]:
    return "AgentCompleted", {
        "_type": "AgentCompleted",
        "_version": 1,
        "session_id": session_id,
        "route": "coomi",
        "total_tokens": total_tokens,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


def _diagnostic_frame_path(filename: str) -> str:
    path = Path(filename)
    repository_root = Path(__file__).resolve().parents[3]
    try:
        return path.resolve().relative_to(repository_root).as_posix()
    except (OSError, ValueError):
        parts = path.parts
        return "/".join(parts[-2:]) if len(parts) >= 2 else path.name


_DIAGNOSTIC_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(
        r"(?i)(api[_-]?key|authorization|access[_-]?token|token|secret)"
        r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    ),
)


def _redact_diagnostic_text(value: Any) -> str:
    text = str(value or "").strip()
    text = _DIAGNOSTIC_SECRET_PATTERNS[0].sub("Bearer [REDACTED]", text)
    text = _DIAGNOSTIC_SECRET_PATTERNS[1].sub("[REDACTED]", text)
    text = _DIAGNOSTIC_SECRET_PATTERNS[2].sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        text,
    )
    return text


def _exception_diagnostics(error: BaseException) -> Dict[str, Any]:
    frames = traceback.extract_tb(error.__traceback__) if error.__traceback__ else []
    rendered_frames = [
        {
            "file": _diagnostic_frame_path(frame.filename),
            "line": int(frame.lineno),
            "function": str(frame.name),
        }
        for frame in frames[-8:]
    ]
    chain: list[Dict[str, str]] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(chain) < 6:
        seen.add(id(current))
        chain.append(
            {
                "type": type(current).__name__,
                "message": _redact_diagnostic_text(current),
            }
        )
        current = current.__cause__ or (
            current.__context__ if not current.__suppress_context__ else None
        )
    origin = rendered_frames[-1] if rendered_frames else {}
    return {
        "exceptionType": type(error).__name__,
        "exceptionMessage": _redact_diagnostic_text(error),
        "exceptionChain": chain,
        "origin": origin,
        "traceback": rendered_frames,
    }


def _agent_error(
    trace_id: str,
    error: Any,
    *,
    stage: str = "runtime",
    session_id: str = "",
    provider_id: str = "",
    model: str = "",
    status_code: int | None = None,
) -> Dict[str, Any]:
    diagnostics = _exception_diagnostics(error) if isinstance(error, BaseException) else {}
    resolved_status = _http_error_status_or_none(status_code)
    if resolved_status is None:
        resolved_status = _http_error_status_or_none(_semantic_provider_error_status(error))
    details = {
        "traceId": trace_id,
        "sessionId": session_id,
        "runtime": "storydex-coomi-rs",
        "runtimeVersion": STORYDEX_COOMI_RUNTIME_VERSION,
        "stage": stage,
        "providerId": provider_id,
        "model": model,
        **diagnostics,
    }
    if resolved_status is not None:
        details["statusCode"] = resolved_status
        details["providerHttpStatus"] = resolved_status
    return {
        "_type": "AgentError",
        "_version": 1,
        "error_type": type(error).__name__,
        "message": _coomi_error_message(error),
        "details": details,
    }


def _coomi_error_message(error: Any) -> str:
    detail = _redact_diagnostic_text(error)
    if detail:
        return detail
    if isinstance(error, BaseException):
        return f"Coomi execution failed ({type(error).__name__})."
    return "Coomi execution failed."


def _validate_provider_document(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("Provider config must be a JSON object")
    providers = value.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("providers must be an object")
    active = str(value.get("active") or "")
    if active and active not in providers:
        raise ValueError("active provider does not exist")
    supported = {"generic", "openai_compatible", "openai_responses", "anthropic_messages", "gemini_native"}
    for provider_id, provider in providers.items():
        if not isinstance(provider, dict):
            raise ValueError(f"provider {provider_id!r} must be an object")
        kind = str(provider.get("type") or "openai_compatible").lower().replace("-", "_")
        if kind not in supported:
            raise ValueError(f"provider {provider_id!r} has unsupported type {kind!r}")
        if not str(provider.get("model") or "").strip():
            raise ValueError(f"provider {provider_id!r} has no model")
        if not str(provider.get("base_url") or "").strip():
            raise ValueError(f"provider {provider_id!r} has no base_url")


def _models_endpoint(base_url: str, provider_type: str) -> str:
    raw = str(base_url or "").strip()
    if not raw:
        raise ValueError("baseUrl is required")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("baseUrl must be an HTTP(S) URL")
    path = (parsed.path or "").rstrip("/")
    lowered = path.lower()
    normalized = str(provider_type or "").lower().replace("-", "_")
    for suffix in ("/chat/completions", "/completions", "/responses", "/messages"):
        if lowered.endswith(suffix):
            path = f"{path[:-len(suffix)]}/models"
            break
    else:
        if lowered.endswith("/models"):
            pass
        elif normalized in {"anthropic", "anthropic_messages"} and not lowered.endswith("/v1"):
            path = f"{path}/v1/models" if path else "/v1/models"
        else:
            path = f"{path}/models" if path else "/models"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _extract_model_ids(payload: Any) -> list[str]:
    values = []
    if isinstance(payload, dict):
        for key in ("data", "models"):
            if isinstance(payload.get(key), list):
                values = payload[key]
                break
    elif isinstance(payload, list):
        values = payload
    models = set()
    for value in values:
        if isinstance(value, str):
            model = value
        elif isinstance(value, dict):
            model = str(value.get("id") or value.get("name") or "")
            if model.startswith("models/"):
                model = model[7:]
        else:
            continue
        if model.strip():
            models.add(model.strip())
    return sorted(models, key=str.casefold)


def _parse_task_plan_content(content: str, *, trace_id: str) -> list[Dict[str, Any]]:
    payload = _extract_json_payload(content)
    if payload is None:
        return []
    raw_tasks = payload.get("tasks") if isinstance(payload, dict) else payload
    return _normalize_planner_tasks(raw_tasks, trace_id=trace_id)


def _extract_json_payload(content: str) -> Any:
    text = str(content or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(?P<body>[\s\S]*?)\s*```", text, re.IGNORECASE)
    if fenced:
        try:
            return json.loads(fenced.group("body"))
        except json.JSONDecodeError:
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _normalize_planner_tasks(raw_tasks: Any, *, trace_id: str) -> list[Dict[str, Any]]:
    if not isinstance(raw_tasks, list):
        return []
    tasks: list[Dict[str, Any]] = []
    for item in raw_tasks[:10]:
        if isinstance(item, str):
            title = item.strip()
            detail = ""
        elif isinstance(item, dict):
            title = str(item.get("title") or item.get("name") or item.get("task") or "").strip()
            detail = str(
                item.get("detail") or item.get("description") or item.get("notes") or ""
            ).strip()
        else:
            continue
        if not title or _is_generic_task_title(title):
            continue
        tasks.append(
            {
                "taskId": f"{trace_id}-task-{len(tasks) + 1}",
                "traceId": trace_id,
                "order": len(tasks) + 1,
                "title": title[:80],
                "detail": detail[:240],
                "status": "pending",
            }
        )
    return _renumber_tasks(tasks, trace_id=trace_id)


def _is_generic_task_title(title: str) -> bool:
    compact = re.sub(r"[\s:：，。,.;；、\-_/]+", "", str(title or "").casefold())
    if compact in {
        "分析需求",
        "执行任务",
        "完成回复",
        "确认需求",
        "处理请求",
        "任务执行",
        "analysis",
        "analyzerequest",
        "executetask",
        "finishreply",
    }:
        return True
    generic_token_groups = (
        ("确认", "目标", "影响", "范围"),
        ("执行", "本轮", "请求"),
        ("检查", "结果", "文件", "状态"),
        ("执行", "修改", "检查", "结果"),
        ("检查", "记录", "本轮", "版本"),
    )
    return any(all(token in compact for token in group) for group in generic_token_groups)


def _renumber_tasks(tasks: list[Dict[str, Any]], *, trace_id: str) -> list[Dict[str, Any]]:
    result: list[Dict[str, Any]] = []
    for index, task in enumerate(tasks[:10]):
        next_task = dict(task)
        next_task["taskId"] = str(
            next_task.get("taskId") or f"{trace_id}-task-{index + 1}"
        )
        next_task["traceId"] = str(next_task.get("traceId") or trace_id)
        next_task["order"] = index + 1
        next_task["status"] = str(next_task.get("status") or "pending")
        result.append(next_task)
    return result


def _commit_message_messages(
    *, changed_files: list[str], diff_summary: str, prompt: str
) -> list[Dict[str, str]]:
    return [
        {
            "role": "system",
            "content": "Write one concise local Git commit subject, no quotes, Markdown, body, or trailing period.",
        },
        {
            "role": "user",
            "content": json.dumps(
                {"files": changed_files[:100], "diff": diff_summary[:8000], "task": prompt[:1000]},
                ensure_ascii=False,
            ),
        },
    ]


def _parse_commit_message_content(content: str) -> str:
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*]\s+", "", line).strip()
        line = line.strip("`\"'“”‘’")
        if line.lower().startswith("commit message:"):
            line = line.split(":", 1)[1].strip()
        if line:
            return line[:160]
    return ""


def _http_status_or_none(value: Any) -> int | None:
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _http_error_status_or_none(value: Any) -> int | None:
    status = _http_status_or_none(value)
    return status if status is not None and status >= 400 else None


def _semantic_provider_error_status(error: Any) -> int | None:
    if error is None:
        return None
    status = _http_status_or_none(getattr(error, "status_code", None))
    if status is not None:
        return status
    response = getattr(error, "response", None)
    status = _http_status_or_none(getattr(response, "status_code", None))
    if status is not None:
        return status
    match = re.search(r"HTTP\s+(\d{3})", str(error), flags=re.IGNORECASE)
    return _http_status_or_none(match.group(1)) if match else None


def _semantic_provider_error_retryable(error: Exception) -> bool:
    status = _semantic_provider_error_status(error)
    if status == 429 or (status is not None and status >= 500):
        return True
    name = type(error).__name__.lower()
    return isinstance(error, (TimeoutError, httpx.TransportError)) or any(
        token in name for token in ("timeout", "connection", "network")
    )


def _semantic_provider_retry_delay(error: Exception) -> int:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            value = headers.get("retry-after")
            if value is not None:
                return max(1, min(120, int(float(value))))
        except (AttributeError, TypeError, ValueError):
            pass
    match = re.search(r"retry_after['\"\s:]+(\d+)", str(error), re.IGNORECASE)
    if match:
        return max(1, min(120, int(match.group(1))))
    return 5


def _knowledge_review_from_tool_preview(tool_name: str, preview: str) -> Dict[str, Any] | None:
    if tool_name != "StorydexApplyStoryIncrement":
        return None
    try:
        value = json.loads(preview)
    except (ValueError, json.JSONDecodeError):
        return None
    review = value.get("knowledgeReview") if isinstance(value, dict) else None
    return review if isinstance(review, dict) and review.get("code") == "knowledge_review_required" else None


def _dict_value(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _positive_int(value: Any, *, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(minimum), min(int(maximum), parsed))


_SERVICE = StorydexCoomiAgentService()


def get_storydex_coomi_agent_service() -> StorydexCoomiAgentService:
    return _SERVICE
