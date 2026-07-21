from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Iterator
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from services.context_policy import ContextPolicy, context_policy_from_turn_contract


STORYDEX_COOMI_HOME = Path.home() / ".storydex"
STORYDEX_COOMI_SESSIONS = STORYDEX_COOMI_HOME / ".coomi" / "sessions"
STORYDEX_COOMI_CONFIG = STORYDEX_COOMI_HOME / ".coomi" / "config" / "providers.json"
DEFAULT_CONTEXT_WINDOW = 256_000
MIN_CONTEXT_WINDOW = 8_000
MAX_CONTEXT_WINDOW = 4_000_000
CONTEXT_WINDOW_KEYS = ("context_window", "contextWindow", "max_context_tokens", "maxContextTokens")
COMPACT_THRESHOLD_RATIO = 0.9
WARNING_THRESHOLD_RATIO = 0.6
_COOMI_ENDPOINT_COMPAT_INSTALLED = False
_COOMI_HOME_REDIRECTS_INSTALLED = False
_COOMI_REDIRECT_INSTALL_LOCK = threading.Lock()
_LOGGER = logging.getLogger(__name__)


class StorydexCoomiUnavailable(RuntimeError):
    pass


def _coomi_binding_path(workspace_root: Path, storydex_session_id: str) -> Path:
    workspace = Path(workspace_root).resolve()
    normalized_session = str(storydex_session_id or "default").strip() or "default"
    digest = sha256(normalized_session.encode("utf-8")).hexdigest()[:24]
    return workspace / ".storydex" / ".agent" / "runtime" / "coomi-sessions" / f"{digest}.json"


def _read_coomi_session_binding(*, workspace_root: Path, storydex_session_id: str) -> Dict[str, Any]:
    path = _coomi_binding_path(workspace_root, storydex_session_id)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _LOGGER.warning("Unable to read Coomi session binding %s: %s", path, exc)
        return {}
    if not isinstance(payload, dict):
        return {}
    expected_workspace = str(Path(workspace_root).resolve())
    if str(payload.get("workspaceRoot") or "") != expected_workspace:
        _LOGGER.warning("Ignored cross-workspace Coomi session binding: %s", path)
        return {}
    if str(payload.get("storydexSessionId") or "") != (str(storydex_session_id or "default").strip() or "default"):
        return {}
    return payload


def _write_coomi_session_binding(
    *,
    workspace_root: Path,
    storydex_session_id: str,
    session: Any,
) -> Path:
    path = _coomi_binding_path(workspace_root, storydex_session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "workspaceRoot": str(Path(workspace_root).resolve()),
        "storydexSessionId": str(storydex_session_id or "default").strip() or "default",
        "coomiSessionId": str(getattr(session, "id", "") or ""),
        "historyPath": str(getattr(session, "history_path", "") or ""),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _restore_bound_coomi_session(*, manager: Any, workspace_root: Path, storydex_session_id: str) -> Any | None:
    binding = _read_coomi_session_binding(
        workspace_root=workspace_root,
        storydex_session_id=storydex_session_id,
    )
    if not binding:
        return None
    history_path = Path(str(binding.get("historyPath") or ""))
    if not history_path.exists() or not history_path.is_file():
        _LOGGER.warning("Coomi session history is missing for Storydex session %s", storydex_session_id)
        return None
    try:
        from coomi.services.session_history import load_session_from_jsonl

        session = load_session_from_jsonl(history_path)
        expected_id = str(binding.get("coomiSessionId") or "")
        if expected_id and str(getattr(session, "id", "") or "") != expected_id:
            raise ValueError("Coomi session id does not match the Storydex binding")
        manager.register_session(session)
        return session
    except Exception as exc:
        _LOGGER.warning("Unable to restore Coomi session %s: %s", storydex_session_id, exc)
        return None


def _delete_coomi_session_binding(
    *,
    workspace_root: Path,
    storydex_session_id: str,
    delete_history: bool,
) -> None:
    path = _coomi_binding_path(workspace_root, storydex_session_id)
    binding = _read_coomi_session_binding(
        workspace_root=workspace_root,
        storydex_session_id=storydex_session_id,
    )
    if delete_history:
        raw_history_path = str(binding.get("historyPath") or "").strip()
        if raw_history_path:
            try:
                history_path = Path(raw_history_path).resolve()
                sessions_root = STORYDEX_COOMI_SESSIONS.resolve()
                history_path.relative_to(sessions_root)
                if history_path.is_file():
                    history_path.unlink()
            except (OSError, ValueError):
                _LOGGER.warning("Refused or failed to delete Coomi history path: %s", raw_history_path)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _LOGGER.warning("Unable to delete Coomi session binding %s: %s", path, exc)


class StorydexCoomiAgentService:
    def __init__(self) -> None:
        self._sessions: dict[str, Any] = {}
        self._agents: dict[str, Any] = {}
        self._permissions: dict[str, Any] = {}
        self._approval_waiters: dict[str, asyncio.Future[Dict[str, Any]]] = {}
        self._permission_mode = "full_access"
        self._lock: asyncio.Lock | None = None
        self._execution_cancel_lock = threading.Lock()
        self._execution_cancellers: dict[str, list[Callable[[], Any]]] = {}

    def _runtime_lock(self) -> asyncio.Lock:
        running_loop = asyncio.get_running_loop()
        lock = self._lock
        if lock is None or getattr(lock, "_loop", running_loop) not in {None, running_loop}:
            lock = asyncio.Lock()
            self._lock = lock
        return lock

    @staticmethod
    def _runtime_key(*, session_id: str, workspace_root: Path) -> str:
        workspace = str(Path(workspace_root).resolve())
        normalized_session = str(session_id or "default").strip() or "default"
        return f"{workspace}::{normalized_session}"

    def _register_execution_canceller(
        self,
        *,
        session_id: str,
        workspace_root: Path,
        callback: Callable[[], Any],
    ) -> str:
        key = self._runtime_key(session_id=session_id, workspace_root=workspace_root)
        with self._execution_cancel_lock:
            self._execution_cancellers.setdefault(key, []).append(callback)
        return key

    def _unregister_execution_canceller(self, key: str, callback: Callable[[], Any]) -> None:
        with self._execution_cancel_lock:
            callbacks = self._execution_cancellers.get(key)
            if not callbacks:
                return
            self._execution_cancellers[key] = [item for item in callbacks if item is not callback]
            if not self._execution_cancellers[key]:
                self._execution_cancellers.pop(key, None)

    def cancel_execution(self, *, session_id: str, workspace_root: Path, reason: str = "cancelled") -> bool:
        """Cancel the currently running Coomi/Loop producer for one workspace."""
        del reason
        key = self._runtime_key(session_id=session_id, workspace_root=workspace_root)
        with self._execution_cancel_lock:
            callbacks = list(self._execution_cancellers.get(key, []))
        cancelled = False
        for callback in callbacks:
            try:
                callback()
                cancelled = True
            except Exception as exc:
                _LOGGER.warning("Coomi execution cancellation failed for %s: %s", key, exc)
        return cancelled

    async def create_task_plan(
        self,
        *,
        prompt: str,
        trace_id: str,
        session_id: str,
        workspace_root: Path,
        active_file: str = "",
        story_generation: Dict[str, Any] | None = None,
        turn_contract: Dict[str, Any] | None = None,
    ) -> list[Dict[str, Any]]:
        workspace = Path(workspace_root).resolve()
        with _storydex_coomi_home():
            self._ensure_coomi_installed()
            try:
                from services.llm_replay import get_replayable_llm_provider, llm_purpose, llm_trace

                with llm_trace(trace_id), llm_purpose("plan"):
                    provider = get_replayable_llm_provider()
                    response = await _call_provider_chat(
                        provider,
                        _task_planner_messages(
                            prompt=prompt,
                            workspace_root=workspace,
                            active_file=active_file,
                            story_generation=story_generation,
                            turn_contract=turn_contract,
                        ),
                        None,
                    )
                content = str(getattr(response, "content", "") or "")
                tasks = _parse_task_plan_content(content, trace_id=trace_id)
                if tasks:
                    return tasks
            except Exception:
                pass
        return []

    async def generate_commit_message(
        self,
        *,
        workspace_root: Path,
        changed_files: list[str],
        diff_summary: str = "",
        prompt: str = "",
        trace_id: str = "",
    ) -> str:
        with _storydex_coomi_home():
            self._ensure_coomi_installed()
            try:
                from services.llm_replay import get_replayable_llm_provider, llm_purpose, llm_trace

                with llm_trace(trace_id or "default"), llm_purpose("commit"):
                    provider = get_replayable_llm_provider()
                    response = await _call_provider_chat(
                        provider,
                        _commit_message_messages(
                            changed_files=changed_files,
                            diff_summary=diff_summary,
                            prompt=prompt,
                        ),
                        None,
                    )
                message = _parse_commit_message_content(str(getattr(response, "content", "") or ""))
            except Exception as exc:
                raise StorydexCoomiUnavailable(f"Failed to generate commit message: {exc}") from exc
        if not message:
            raise StorydexCoomiUnavailable("Failed to generate a usable commit message.")
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
        del active_file
        started = time.perf_counter()
        workspace = Path(workspace_root).resolve()
        with _storydex_coomi_home():
            self._ensure_coomi_installed()
            command = _parse_slash_command(prompt)
            if command["name"] in {"plan", "exit_plan"}:
                async for item in self._stream_plan_command(
                    command=command["name"],
                    prompt=prompt,
                    trace_id=trace_id,
                    session_id=session_id,
                    workspace_root=workspace,
                    turn_contract=turn_contract,
                ):
                    yield item
                return
            if command["name"] == "loop":
                async for item in self._stream_loop_command(
                    command_body=command["body"],
                    prompt=prompt,
                    trace_id=trace_id,
                    session_id=session_id,
                    workspace_root=workspace,
                    cancellation_token=cancellation_token,
                    started=started,
                    turn_contract=turn_contract,
                ):
                    yield item
                return

            event_queue: asyncio.Queue[tuple[str, Dict[str, Any]] | None] = asyncio.Queue()
            app_context = _StorydexApprovalContext(
                service=self,
                event_queue=event_queue,
                trace_id=trace_id,
                session_id=session_id,
            )
            from services.llm_replay import llm_context_assembly, llm_purpose, llm_trace

            context_assembly = _dict_value(_dict_value(turn_contract).get("contextAssembly"))
            with llm_trace(trace_id), llm_context_assembly(context_assembly):
                agent, session = await self._get_or_create_runtime(
                    session_id=session_id,
                    workspace_root=workspace,
                    prompt=prompt,
                    story_generation=story_generation,
                    turn_contract=turn_contract,
                    app_context=app_context,
                )
            cancel_callback = agent.cancel_token.cancel
            cancel_key = self._register_execution_canceller(
                session_id=session_id,
                workspace_root=workspace,
                callback=cancel_callback,
            )
            status = self.get_status(workspace_root=workspace)
            yield _agent_started(session_id=session_id, prompt=prompt, status=status, mode="coomi")

            translator = _CoomiEventTranslator(session_id=session_id)
            async def produce_events() -> None:
                try:
                    with llm_trace(trace_id), llm_context_assembly(context_assembly), llm_purpose("chat"):
                        async for event in agent.run_stream(session, prompt):
                            if _is_cancelled(cancellation_token):
                                try:
                                    agent.cancel_token.cancel()
                                except Exception:
                                    pass
                                await event_queue.put((
                                    "AgentCancelled",
                                    {
                                        "_type": "AgentCancelled",
                                        "_version": 1,
                                        "session_id": session_id,
                                        "reason": "client_disconnected",
                                    },
                                ))
                                return
                            translated = translator.translate(event)
                            if translated is not None:
                                if translated[0] == "UsageUpdate":
                                    _attach_context_snapshot(translated[1], session=session, agent=agent)
                                elif translated[0] == "CompressionEvent":
                                    _attach_context_snapshot(translated[1], session=session, agent=agent, compressed=True)
                                await event_queue.put(translated)
                except Exception as exc:
                    await event_queue.put((
                        "AgentError",
                        {
                            "_type": "AgentError",
                            "_version": 1,
                            "error_type": type(exc).__name__,
                            "message": _coomi_error_message(exc),
                            "details": {"traceId": trace_id, "runtime": "coomi"},
                        },
                    ))
                    return
                else:
                    usage = getattr(session, "token_usage", None)
                    total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
                    await event_queue.put((
                        "AgentCompleted",
                        {
                            "_type": "AgentCompleted",
                            "_version": 1,
                            "session_id": session_id,
                            "route": "coomi",
                            "total_tokens": total_tokens,
                            "duration_ms": int((time.perf_counter() - started) * 1000),
                        },
                    ))
                finally:
                    await event_queue.put(None)

            producer = asyncio.create_task(produce_events())
            try:
                while True:
                    item = await event_queue.get()
                    if item is None:
                        break
                    yield item
            finally:
                if not producer.done():
                    producer.cancel()
                app_context.cancel_pending()
                self._unregister_execution_canceller(cancel_key, cancel_callback)

    def resolve_approval(self, approval_id: str, decision: str, *, response: Dict[str, Any] | None = None) -> Dict[str, Any]:
        normalized_id = str(approval_id or "").strip()
        normalized_decision = str(decision or "").strip().lower()
        future = self._approval_waiters.get(normalized_id)
        if future is None or future.done():
            return {"accepted": False, "approvalId": normalized_id, "decision": normalized_decision}
        answer = _approval_answer(normalized_decision, response)
        future.get_loop().call_soon_threadsafe(future.set_result, answer)
        return {"accepted": True, "approvalId": normalized_id, "decision": normalized_decision}

    def _create_permission_system(
        self,
        permission_level_enum: Any,
        permission_mode_enum: Any,
        permission_system_cls: Any,
        workspace_root: Path,
    ) -> Any:
        permissions = permission_system_cls()
        permissions.set_mode(_coomi_permission_mode(permission_mode_enum, self._permission_mode))
        original_check = permissions.check_permission

        def check_permission(tool_name: str, arguments: dict[str, Any]) -> Any:
            return _storydex_check_permission(
                permission_level_enum,
                permissions,
                original_check,
                tool_name,
                arguments,
            )

        permissions.check_permission = check_permission
        _sync_storydex_permission_context(
            permissions,
            workspace_root=workspace_root,
            mode=self._permission_mode,
        )
        return permissions

    async def _get_or_create_runtime(
        self,
        *,
        session_id: str,
        workspace_root: Path,
        prompt: str,
        story_generation: Dict[str, Any] | None = None,
        turn_contract: Dict[str, Any] | None = None,
        app_context: Any = None,
    ) -> tuple[Any, Any]:
        context_policy = context_policy_from_turn_contract(turn_contract)
        async with self._runtime_lock():
            runtime_key = self._runtime_key(session_id=session_id, workspace_root=workspace_root)
            session = self._sessions.get(runtime_key)
            agent = self._agents.get(runtime_key)
            if session is not None and agent is not None:
                session.system_prompt = await _build_coomi_system_prompt(
                    workspace_root=workspace_root,
                    prompt=prompt,
                    story_generation=story_generation,
                    turn_contract=turn_contract,
                    plan_mode=bool(getattr(agent, "plan_mode", False)),
                )
                # providers.json 的 context_window 可能在会话中途被修改，保持跟随。
                setattr(agent, "context_window_size", _resolve_context_window())
                _replace_runtime_tool_registry(
                    agent,
                    _create_storydex_tool_registry(workspace_root, context_policy),
                )
                _sync_coomi_runtime_workspace(
                    agent=agent,
                    session=session,
                    workspace_root=workspace_root,
                    app_context=app_context,
                )
                return agent, session

            from coomi.engine.loop import AgentLoop
            from coomi.engine.session import SessionManager
            from coomi.security import PermissionLevel, PermissionMode, PermissionSystem
            from services.llm_replay import get_replayable_llm_provider

            provider = get_replayable_llm_provider()
            registry = _create_storydex_tool_registry(workspace_root, context_policy)
            permissions = self._permissions.get(runtime_key)
            if permissions is None:
                permissions = self._create_permission_system(
                    PermissionLevel,
                    PermissionMode,
                    PermissionSystem,
                    workspace_root,
                )
                self._permissions[runtime_key] = permissions
            else:
                _sync_storydex_permission_context(permissions, workspace_root=workspace_root, mode=self._permission_mode)

            system_prompt = await _build_coomi_system_prompt(
                workspace_root=workspace_root,
                prompt=prompt,
                story_generation=story_generation,
                turn_contract=turn_contract,
            )
            manager = SessionManager(history_dir=STORYDEX_COOMI_SESSIONS, persist_history=True)
            session = _restore_bound_coomi_session(
                manager=manager,
                workspace_root=workspace_root,
                storydex_session_id=session_id,
            )
            if session is None:
                session = manager.create_session(
                    system_prompt=system_prompt,
                    cwd=workspace_root.as_posix(),
                    model=getattr(provider, "model", "coomi"),
                )
            else:
                session.system_prompt = system_prompt
                setattr(session, "current_model", getattr(provider, "model", "coomi"))
            _write_coomi_session_binding(
                workspace_root=workspace_root,
                storydex_session_id=session_id,
                session=session,
            )
            agent = AgentLoop(
                provider,
                registry,
                context_window_size=_resolve_context_window(),
                app_context=app_context,
                permission_system=permissions,
                project_path=workspace_root.as_posix(),
            )
            _sync_coomi_runtime_workspace(
                agent=agent,
                session=session,
                workspace_root=workspace_root,
                app_context=app_context,
            )
            self._sessions[runtime_key] = session
            self._agents[runtime_key] = agent
            return agent, session

    async def _stream_plan_command(
        self,
        *,
        command: str,
        prompt: str,
        trace_id: str,
        session_id: str,
        workspace_root: Path,
        turn_contract: Dict[str, Any] | None = None,
    ) -> AsyncIterator[tuple[str, Dict[str, Any]]]:
        started = time.perf_counter()
        from services.llm_replay import llm_trace

        with llm_trace(trace_id):
            agent, session = await self._get_or_create_runtime(
                session_id=session_id,
                workspace_root=workspace_root,
                prompt=prompt,
                turn_contract=turn_contract,
            )
        plan_mode = command == "plan"
        setter = getattr(agent, "set_plan_mode", None)
        if callable(setter):
            setter(plan_mode)
        with llm_trace(trace_id):
            session.system_prompt = await _build_coomi_system_prompt(
                workspace_root=workspace_root,
                prompt=prompt,
                plan_mode=plan_mode,
                turn_contract=turn_contract,
            )
        _sync_coomi_runtime_workspace(
            agent=agent,
            session=session,
            workspace_root=workspace_root,
            app_context=None,
        )
        status = self.get_status(workspace_root=workspace_root)
        yield _agent_started(session_id=session_id, prompt=prompt, status=status, mode="coomi")
        content = (
            "Coomi Plan Mode enabled. Send /exit_plan to return to normal execution."
            if plan_mode
            else "Coomi Plan Mode disabled."
        )
        yield "TextChunk", {"_type": "TextChunk", "_version": 1, "content": content}
        yield "AgentCompleted", {
            "_type": "AgentCompleted",
            "_version": 1,
            "session_id": session_id,
            "route": "coomi",
            "total_tokens": 0,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "planMode": plan_mode,
            "traceId": trace_id,
        }

    async def _stream_loop_command(
        self,
        *,
        command_body: str,
        prompt: str,
        trace_id: str,
        session_id: str,
        workspace_root: Path,
        cancellation_token: Any,
        started: float,
        turn_contract: Dict[str, Any] | None = None,
    ) -> AsyncIterator[tuple[str, Dict[str, Any]]]:
        status = self.get_status(workspace_root=workspace_root)
        yield _agent_started(session_id=session_id, prompt=prompt, status=status, mode="coomi-loop")
        if not command_body.strip():
            yield "TextChunk", {"_type": "TextChunk", "_version": 1, "content": "Usage: /loop <task or path-to-spec.md>"}
            yield "AgentCompleted", {
                "_type": "AgentCompleted",
                "_version": 1,
                "session_id": session_id,
                "route": "coomi-loop",
                "total_tokens": 0,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "traceId": trace_id,
            }
            return

        from coomi.engine.loop_runner import LoopRunner
        from services.llm_replay import get_replayable_llm_provider, llm_purpose, llm_trace

        provider = get_replayable_llm_provider()
        context_policy = context_policy_from_turn_contract(turn_contract)
        registry = _create_storydex_tool_registry(workspace_root, context_policy)
        permissions = self._permissions.get(session_id)
        if permissions is None:
            from coomi.security import PermissionLevel, PermissionMode, PermissionSystem

            permissions = self._create_permission_system(
                PermissionLevel,
                PermissionMode,
                PermissionSystem,
                workspace_root,
            )
            self._permissions[session_id] = permissions
        else:
            _sync_storydex_permission_context(permissions, workspace_root=workspace_root, mode=self._permission_mode)
        event_queue: asyncio.Queue[tuple[str, Dict[str, Any]] | None] = asyncio.Queue()
        app_context = _StorydexApprovalContext(
            service=self,
            event_queue=event_queue,
            trace_id=trace_id,
            session_id=session_id,
        )
        runner = LoopRunner(
            provider,
            registry,
            context_window_size=_resolve_context_window(),
            app_context=app_context,
            permission_system=permissions,
        )
        cancel_callback = runner.cancel_token.cancel
        cancel_key = self._register_execution_canceller(
            session_id=session_id,
            workspace_root=workspace_root,
            callback=cancel_callback,
        )
        memory_manager, memory_recall = _build_coomi_memory(
            workspace_root,
            context_policy,
            provider=provider,
        )
        spec_path, spec = _resolve_loop_spec(workspace_root, command_body)
        translator = _CoomiEventTranslator(session_id=session_id)

        async def produce_loop_events() -> None:
            try:
                with llm_trace(trace_id), llm_purpose("loop"):
                    async for event in runner.start_loop(
                        cwd=workspace_root.as_posix(),
                        spec_path=spec_path,
                        spec=spec,
                        memory_manager=memory_manager,
                        memory_recall=memory_recall,
                        display_name=_model_display(provider),
                    ):
                        if _is_cancelled(cancellation_token):
                            try:
                                runner.cancel_token.cancel()
                            except Exception:
                                pass
                            await event_queue.put((
                                "AgentCancelled",
                                {
                                    "_type": "AgentCancelled",
                                    "_version": 1,
                                    "session_id": session_id,
                                    "reason": "client_disconnected",
                                },
                            ))
                            return
                        translated = translator.translate(event)
                        if translated is not None:
                            await event_queue.put(translated)
            except Exception as exc:
                await event_queue.put((
                    "AgentError",
                    {
                        "_type": "AgentError",
                        "_version": 1,
                        "error_type": type(exc).__name__,
                        "message": _coomi_error_message(exc),
                        "details": {"traceId": trace_id, "runtime": "coomi-loop"},
                    },
                ))
                return
            else:
                await event_queue.put((
                    "AgentCompleted",
                    {
                        "_type": "AgentCompleted",
                        "_version": 1,
                        "session_id": session_id,
                        "route": "coomi-loop",
                        "total_tokens": 0,
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        "traceId": trace_id,
                    },
                ))
            finally:
                await event_queue.put(None)

        producer = asyncio.create_task(produce_loop_events())
        try:
            while True:
                item = await event_queue.get()
                if item is None:
                    break
                yield item
        finally:
            if not producer.done():
                producer.cancel()
            app_context.cancel_pending()
            self._unregister_execution_canceller(cancel_key, cancel_callback)

    @staticmethod
    def _ensure_coomi_installed() -> None:
        try:
            import coomi  # noqa: F401
        except Exception as exc:
            raise StorydexCoomiUnavailable(
                "Coomi is not installed in the active Storydex Python environment. "
                "Install the dependencies pinned by requirements.txt via requirements.lock."
            ) from exc

    def get_status(self, *, workspace_root: Path) -> Dict[str, Any]:
        workspace = Path(workspace_root).resolve()
        with _storydex_coomi_home():
            self._ensure_coomi_installed()
            from coomi.services.llm.config import ConfigManager

            config = ConfigManager()
            active = config.get_active()
            registry = _create_storydex_tool_registry(workspace)
            context = self._context_status()
            return {
                "runtime": "coomi",
                "installed": True,
                "home": (STORYDEX_COOMI_HOME / ".coomi").as_posix(),
                "configPath": STORYDEX_COOMI_CONFIG.as_posix(),
                "sessionsPath": STORYDEX_COOMI_SESSIONS.as_posix(),
                "providerId": getattr(active, "id", "") if active else "",
                "providerType": getattr(active, "type", "") if active else "",
                "model": getattr(active, "model", "") if active else "",
                "display": getattr(active, "display", "") if active else "",
                "permissionMode": self._permission_mode,
                "permissionLabel": _permission_label(self._permission_mode),
                "planMode": any(bool(getattr(agent, "plan_mode", False)) for agent in self._agents.values()),
                "toolCount": len(registry.list_tools()),
                **context,
            }

    def _context_status(self) -> Dict[str, Any]:
        session = next(iter(self._sessions.values()), None)
        agent = next(iter(self._agents.values()), None)
        if session is None and agent is None:
            context_window = _resolve_context_window()
            return {
                "contextWindow": context_window,
                "usedTokens": 0,
                "usageRatio": 0.0,
                "cumulativeTokens": 0,
                "compactThreshold": int(context_window * COMPACT_THRESHOLD_RATIO),
                "warningThreshold": int(context_window * WARNING_THRESHOLD_RATIO),
                "compressionStatus": "idle",
            }
        snapshot = _context_snapshot(session=session, agent=agent)
        snapshot.setdefault("compressionStatus", "idle")
        return snapshot

    def read_config(self) -> Dict[str, Any]:
        with _storydex_coomi_home():
            content = STORYDEX_COOMI_CONFIG.read_text(encoding="utf-8")
            parsed = json.loads(content) if content.strip() else {}
            stat = STORYDEX_COOMI_CONFIG.stat()
            return {
                "configPath": STORYDEX_COOMI_CONFIG.as_posix(),
                "content": content,
                "parsed": parsed if isinstance(parsed, dict) else {},
                "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
            }

    def write_config(self, content: str) -> Dict[str, Any]:
        normalized_content = str(content or "").strip()
        if not normalized_content:
            raise ValueError("Coomi providers config cannot be empty.")
        parsed = json.loads(normalized_content)
        if not isinstance(parsed, dict):
            raise ValueError("Coomi providers config must be a JSON object.")
        STORYDEX_COOMI_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        STORYDEX_COOMI_CONFIG.write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._sessions.clear()
        self._agents.clear()
        self._permissions.clear()
        return self.read_config()

    def list_models(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 20.0,
        http_get: Callable[..., Any] | None = None,
    ) -> Dict[str, Any]:
        endpoint = _coomi_models_endpoint(base_url)
        normalized_key = str(api_key or "").strip()
        if not normalized_key:
            raise ValueError("Coomi API key is required to fetch models.")

        headers = {
            "Authorization": f"Bearer {normalized_key}",
            "Accept": "application/json",
        }
        try:
            response = (
                http_get(endpoint, headers=headers, timeout=timeout)
                if http_get is not None
                else _httpx_get(endpoint, headers=headers, timeout=timeout)
            )
        except Exception as exc:
            raise ValueError(f"Model list request failed ({exc.__class__.__name__}).") from exc
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code < 200 or status_code >= 300:
            raise ValueError(f"Model list request failed ({status_code}).")
        try:
            payload = response.json()
        except Exception as exc:
            raise ValueError("Model list response is not valid JSON.") from exc
        return {"endpoint": endpoint, "models": _extract_model_ids(payload)}

    def clear_session(
        self,
        session_id: str,
        *,
        workspace_root: Path | None = None,
        delete_history: bool = False,
    ) -> None:
        normalized = str(session_id or "default").strip() or "default"
        if workspace_root is not None:
            runtime_key = self._runtime_key(session_id=normalized, workspace_root=workspace_root)
            self._sessions.pop(runtime_key, None)
            self._agents.pop(runtime_key, None)
            self._permissions.pop(runtime_key, None)
            if delete_history:
                _delete_coomi_session_binding(
                    workspace_root=workspace_root,
                    storydex_session_id=normalized,
                    delete_history=True,
                )
            return
        suffix = f"::{normalized}"
        for cache in (self._sessions, self._agents, self._permissions):
            for key in [item for item in cache if item.endswith(suffix)]:
                cache.pop(key, None)

    def rollback_last_turn(self, session_id: str, *, workspace_root: Path) -> Dict[str, Any]:
        normalized_session_id = str(session_id or "default").strip() or "default"
        resolved_workspace = Path(workspace_root).resolve()
        result = {"rolledBack": False, "sessionId": normalized_session_id}
        binding = _read_coomi_session_binding(
            workspace_root=resolved_workspace,
            storydex_session_id=normalized_session_id,
        )
        raw_history_path = str(binding.get("historyPath") or "").strip()
        if not raw_history_path:
            return result

        history_path = Path(raw_history_path).expanduser().resolve()
        sessions_root = STORYDEX_COOMI_SESSIONS.resolve()
        try:
            history_path.relative_to(sessions_root)
        except ValueError as exc:
            raise ValueError("Coomi session history path is outside the Storydex sessions directory.") from exc
        if not history_path.is_file():
            return result

        lines = history_path.read_bytes().splitlines(keepends=True)
        last_user_index: int | None = None
        for index, line in enumerate(lines):
            try:
                entry = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(entry, dict) or entry.get("type") != "message":
                continue
            message = entry.get("message")
            if isinstance(message, dict) and message.get("role") == "user":
                last_user_index = index

        if last_user_index is None:
            return result

        temporary = history_path.with_name(f".{history_path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as stream:
                stream.writelines(lines[:last_user_index])
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, history_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

        self.clear_session(
            normalized_session_id,
            workspace_root=resolved_workspace,
            delete_history=False,
        )
        result["rolledBack"] = True
        return result

    def set_permission_mode(self, mode: str) -> Dict[str, Any]:
        normalized = _normalize_permission_mode(mode)
        with _storydex_coomi_home():
            self._ensure_coomi_installed()
            from coomi.security import PermissionMode

            self._permission_mode = normalized
            coomi_mode = _coomi_permission_mode(PermissionMode, normalized)
            for permission in self._permissions.values():
                permission.set_mode(coomi_mode)
                setattr(permission, "_storydex_mode", normalized)
        return {"permissionMode": self._permission_mode, "permissionLabel": _permission_label(self._permission_mode)}

    def cycle_permission_mode(self) -> Dict[str, Any]:
        order = ["ask_approval", "approve_for_me", "full_access"]
        current = _normalize_permission_mode(self._permission_mode)
        next_mode = order[(order.index(current) + 1) % len(order)]
        return self.set_permission_mode(next_mode)


def _httpx_get(url: str, *, headers: Dict[str, str], timeout: float) -> Any:
    import httpx

    return httpx.get(url, headers=headers, timeout=timeout)


def _read_providers_config_payload() -> Dict[str, Any]:
    try:
        content = STORYDEX_COOMI_CONFIG.read_text(encoding="utf-8")
        parsed = json.loads(content) if content.strip() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_context_window(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return max(MIN_CONTEXT_WINDOW, min(MAX_CONTEXT_WINDOW, parsed))


def _resolve_context_window() -> int:
    """Resolve the model context window from providers.json.

    Lookup order: active provider's `context_window` (or aliases) -> top-level
    `contextWindow` default -> DEFAULT_CONTEXT_WINDOW. Without this, Coomi's
    compressor thresholds are computed against a hardcoded 256k window and
    never fire for smaller models — the API errors out first.
    """
    payload = _read_providers_config_payload()
    providers = payload.get("providers") if isinstance(payload.get("providers"), dict) else {}
    active_id = str(payload.get("active") or "")
    provider = providers.get(active_id) if isinstance(providers.get(active_id), dict) else {}
    for source in (provider, payload):
        for key in CONTEXT_WINDOW_KEYS:
            parsed = _parse_context_window(source.get(key))
            if parsed is not None:
                return parsed
    return DEFAULT_CONTEXT_WINDOW


async def _call_provider_chat(provider: Any, messages: list[Dict[str, Any]], options: Any) -> Any:
    chat = getattr(provider, "chat")
    if inspect.iscoroutinefunction(chat):
        return await chat(messages, options)

    response = await asyncio.to_thread(chat, messages, options)
    if inspect.isawaitable(response):
        return await response
    return response


def _coomi_api_base_url(base_url: str) -> str:
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return raw

    path = (parsed.path or "").rstrip("/")
    lowered = path.lower()
    for suffix in ("/chat/completions", "/completions", "/responses", "/models"):
        if lowered.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _install_coomi_endpoint_compat() -> None:
    global _COOMI_ENDPOINT_COMPAT_INSTALLED
    if _COOMI_ENDPOINT_COMPAT_INSTALLED:
        return
    try:
        from coomi.services.llm.config import ProviderConfig
    except Exception:
        return

    if getattr(ProviderConfig, "_storydex_endpoint_compat_installed", False):
        _COOMI_ENDPOINT_COMPAT_INSTALLED = True
        return

    original_from_dict = ProviderConfig.from_dict

    @classmethod
    def from_dict_with_endpoint_compat(cls: Any, provider_id: str, data: dict) -> Any:
        if isinstance(data, dict):
            next_data = dict(data)
            base_url = next_data.get("base_url")
            if isinstance(base_url, str):
                next_data["base_url"] = _coomi_api_base_url(base_url)
            data = next_data
        return original_from_dict(provider_id, data)

    ProviderConfig.from_dict = from_dict_with_endpoint_compat
    setattr(ProviderConfig, "_storydex_endpoint_compat_installed", True)
    _COOMI_ENDPOINT_COMPAT_INSTALLED = True


def _coomi_models_endpoint(base_url: str) -> str:
    raw = str(base_url or "").strip()
    if not raw:
        raise ValueError("Coomi base URL is required to fetch models.")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Coomi base URL must be a complete http(s) URL.")

    path = (parsed.path or "").rstrip("/")
    lowered = path.lower()
    replacements = (
        "/chat/completions",
        "/completions",
        "/responses",
    )
    for suffix in replacements:
        if lowered.endswith(suffix):
            path = f"{path[:-len(suffix)]}/models"
            break
    else:
        if not lowered.endswith("/models"):
            path = f"{path}/models" if path else "/models"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _extract_model_ids(payload: Any) -> list[str]:
    candidates: Any = payload
    if isinstance(payload, dict):
        for key in ("data", "models", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
        else:
            candidates = [payload.get("model")] if payload.get("model") else []
    if not isinstance(candidates, list):
        return []

    result: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        model_id = ""
        if isinstance(item, str):
            model_id = item
        elif isinstance(item, dict):
            for key in ("id", "name", "model"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    model_id = value
                    break
        model_id = model_id.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        result.append(model_id)
    return result


def _commit_message_messages(
    *,
    changed_files: list[str],
    diff_summary: str,
    prompt: str,
) -> list[Dict[str, Any]]:
    system_prompt = (
        "You write concise Git commit subjects for Storydex novel-project changes. "
        "Return exactly one subject line, no markdown, no quotes, no explanation. "
        "Keep it under 72 characters when possible. Use Chinese if the changes are mainly Chinese story content; "
        "otherwise English is fine."
    )
    user_prompt = (
        "Original Agent request:\n"
        f"{prompt or '(empty)'}\n\n"
        "Changed files:\n"
        + "\n".join(f"- {path}" for path in changed_files[:80])
        + "\n\nDiff summary:\n"
        + (diff_summary or "(not available)")
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
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


def _task_planner_messages(
    *,
    prompt: str,
    workspace_root: Path,
    active_file: str,
    story_generation: Dict[str, Any] | None,
    turn_contract: Dict[str, Any] | None,
) -> list[Dict[str, Any]]:
    contract = _dict_value(turn_contract)
    intent = _dict_value(contract.get("intentFrame"))
    turn_plan = _dict_value(contract.get("turnPlan"))
    skill_registry = _dict_value(contract.get("skillRegistry"))
    tool_registry = _dict_value(contract.get("toolRegistry"))
    update_policy = _dict_value(contract.get("updatePolicy"))
    story_generation = story_generation if isinstance(story_generation, dict) else {}
    context = {
        "workspaceRoot": workspace_root.as_posix(),
        "activeFile": str(active_file or ""),
        "intent": str(intent.get("primary") or "general"),
        "turnStatus": str(contract.get("status") or "ready"),
        "fragmentCount": _bounded_int(story_generation.get("fragmentCount") or turn_plan.get("fragmentCount"), default=1, minimum=1, maximum=20),
        "fragmentWordCount": _bounded_int(
            story_generation.get("fragmentWordCount") or turn_plan.get("fragmentWordCount"),
            default=2000,
            minimum=100,
            maximum=20000,
        ),
        "requiresChapterTemplateSelection": bool(turn_plan.get("requiresChapterTemplateSelection")),
        "nextSegmentPath": str(turn_plan.get("nextSegmentPath") or ""),
        "chapterCount": int(turn_plan.get("chapterCount") or 0),
        "autoUpdateVariables": bool(update_policy.get("autoUpdateVariables", False)),
        "autoUpdateWiki": bool(update_policy.get("autoUpdateWiki", False)),
        "skillCount": int(skill_registry.get("skillCount") or 0),
        "toolCount": int(tool_registry.get("toolCount") or 0),
    }
    system_prompt = (
        "You are Storydex's execution task planner. Return only a JSON object with a `tasks` array. "
        "Do not include reasoning, markdown, comments, or chain-of-thought. "
        "Create only concrete, non-generic execution tasks that are genuinely useful for this turn. "
        "If there is no real multi-step plan, return an empty tasks array instead of padding the list. "
        "Each task must include `title` and optional `detail`. Avoid generic titles such as analysis, execute task, finish reply. "
        "Do not add a Git/version-recording task unless the user explicitly asks for it."
    )
    user_prompt = (
        "User request:\n"
        f"{prompt}\n\n"
        "Compact Storydex context:\n"
        f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
        "Expected JSON shape:\n"
        '{"tasks":[{"title":"specific task title","detail":"short implementation detail"}]} or {"tasks":[]}'
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _parse_task_plan_content(content: str, *, trace_id: str) -> list[Dict[str, Any]]:
    payload = _extract_json_payload(content)
    if payload is None:
        return []
    raw_tasks: Any
    if isinstance(payload, dict):
        raw_tasks = payload.get("tasks")
    else:
        raw_tasks = payload
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
    for index, item in enumerate(raw_tasks[:10]):
        if isinstance(item, str):
            title = item.strip()
            detail = ""
        elif isinstance(item, dict):
            title = str(item.get("title") or item.get("name") or item.get("task") or "").strip()
            detail = str(item.get("detail") or item.get("description") or item.get("notes") or "").strip()
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
        next_task["taskId"] = str(next_task.get("taskId") or f"{trace_id}-task-{index + 1}")
        next_task["traceId"] = str(next_task.get("traceId") or trace_id)
        next_task["order"] = index + 1
        next_task["status"] = str(next_task.get("status") or "pending")
        result.append(next_task)
    return result


READ_TOOLS = {"Read", "Glob", "Grep", "WebFetch", "WebSearch"}
WRITE_TOOLS = {"Write", "Edit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
SENSITIVE_NAME_TOKENS = (
    ".env",
    ".npmrc",
    ".pypirc",
    "secret",
    "secrets",
    "credential",
    "credentials",
    "token",
    "tokens",
    "apikey",
    "api_key",
    "private_key",
    "id_rsa",
    "id_ed25519",
    "providers.json",
)


def _create_storydex_permission_system(
    permission_level_enum: Any,
    permission_mode_enum: Any,
    permission_system_cls: Any,
    workspace_root: Path,
    mode: str,
) -> Any:
    permissions = permission_system_cls()
    permissions.set_mode(_coomi_permission_mode(permission_mode_enum, mode))
    original_check = permissions.check_permission

    def check_permission(tool_name: str, arguments: dict[str, Any]) -> Any:
        return _storydex_check_permission(
            permission_level_enum,
            permissions,
            original_check,
            tool_name,
            arguments,
        )

    permissions.check_permission = check_permission
    _sync_storydex_permission_context(permissions, workspace_root=workspace_root, mode=mode)
    return permissions


def _sync_storydex_permission_context(
    permissions: Any,
    *,
    workspace_root: Path,
    mode: str,
    plan_mode: bool | None = None,
) -> None:
    setattr(permissions, "_storydex_workspace_root", Path(workspace_root).resolve())
    setattr(permissions, "_storydex_mode", _normalize_permission_mode(mode))
    if plan_mode is not None:
        setattr(permissions, "_storydex_plan_mode", bool(plan_mode))


def _storydex_check_permission(
    permission_level_enum: Any,
    permissions: Any,
    original_check: Any,
    tool_name: str,
    arguments: dict[str, Any],
) -> Any:
    del original_check
    normalized_tool = str(tool_name or "")
    if normalized_tool == "AskUserQuestion":
        return permission_level_enum.AUTO

    # 硬边界：无论权限模式如何，Write/Edit 不得落到小说项目工作区之外。
    if normalized_tool in WRITE_TOOLS and _write_paths_escape_workspace(permissions, arguments):
        return permission_level_enum.DENY

    mode = _normalize_permission_mode(str(getattr(permissions, "_storydex_mode", "full_access") or "full_access"))
    if bool(getattr(permissions, "_storydex_plan_mode", False)):
        return _storydex_plan_permission(permission_level_enum, permissions, normalized_tool, arguments)

    if mode == "full_access":
        return permission_level_enum.AUTO

    if mode == "ask_approval":
        return permission_level_enum.ASK

    if mode == "approve_for_me":
        return _storydex_auto_permission(permission_level_enum, permissions, normalized_tool, arguments)

    return permission_level_enum.ASK


_WRITE_PATH_ARGUMENT_KEYS = (
    "path",
    "file",
    "file_path",
    "relative_path",
    "target_path",
    "from_path",
    "to_path",
    "fromRelativePath",
    "toRelativePath",
    "relativePath",
)


def _write_paths_escape_workspace(permissions: Any, arguments: dict[str, Any]) -> bool:
    workspace_root = Path(getattr(permissions, "_storydex_workspace_root", Path.cwd())).resolve()
    for key in _WRITE_PATH_ARGUMENT_KEYS:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if _resolve_permission_path(workspace_root, value) is None:
            return True
    return False


def _storydex_plan_permission(
    permission_level_enum: Any,
    permissions: Any,
    tool_name: str,
    arguments: dict[str, Any],
) -> Any:
    if tool_name in READ_TOOLS:
        return permission_level_enum.ASK if _has_sensitive_path(permissions, arguments) else permission_level_enum.AUTO
    if tool_name in WRITE_TOOLS:
        return permission_level_enum.AUTO if _is_plan_document_write(permissions, arguments) else permission_level_enum.DENY
    return permission_level_enum.DENY


def _storydex_auto_permission(
    permission_level_enum: Any,
    permissions: Any,
    tool_name: str,
    arguments: dict[str, Any],
) -> Any:
    if tool_name in READ_TOOLS:
        return permission_level_enum.ASK if _has_sensitive_path(permissions, arguments) else permission_level_enum.AUTO
    if tool_name in WRITE_TOOLS:
        return permission_level_enum.ASK if _has_sensitive_path(permissions, arguments) else permission_level_enum.AUTO
    if tool_name in SHELL_TOOLS:
        command = str(arguments.get("command", ""))
        result = permissions._bash_safety.check_command(command)
        if result.risk_level != "low" or _command_mentions_sensitive_path(command):
            return permission_level_enum.ASK
        return permission_level_enum.AUTO
    return permission_level_enum.ASK


def _has_sensitive_path(permissions: Any, arguments: dict[str, Any]) -> bool:
    return any(_is_sensitive_path(permissions, path) for path in _argument_paths(arguments))


def _argument_paths(arguments: dict[str, Any]) -> list[str]:
    keys = (
        "path",
        "file",
        "file_path",
        "relative_path",
        "target_path",
        "from_path",
        "to_path",
        "fromRelativePath",
        "toRelativePath",
        "relativePath",
        "pattern",
        "query",
    )
    values: list[str] = []
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return values


def _is_sensitive_path(permissions: Any, value: str) -> bool:
    normalized = _normalize_permission_path(value)
    if not normalized:
        return False
    parts = [part for part in normalized.split("/") if part]
    if any(part in {".ssh", ".aws", ".azure", ".gcp"} for part in parts):
        return True
    compact = normalized.replace("-", "_")
    return any(token in compact for token in SENSITIVE_NAME_TOKENS)


def _command_mentions_sensitive_path(command: str) -> bool:
    compact = _normalize_permission_path(command)
    if not compact:
        return False
    return any(token in compact.replace("-", "_") for token in SENSITIVE_NAME_TOKENS)


def _is_plan_document_write(permissions: Any, arguments: dict[str, Any]) -> bool:
    workspace_root = Path(getattr(permissions, "_storydex_workspace_root", Path.cwd())).resolve()
    allowed_root = (workspace_root / ".storydex" / ".agent" / "plans").resolve()
    for raw_path in _argument_paths(arguments):
        resolved = _resolve_permission_path(workspace_root, raw_path)
        if resolved is None:
            continue
        if resolved == allowed_root or allowed_root in resolved.parents:
            return True
        path_text = resolved.as_posix().casefold()
        name_text = resolved.name.casefold()
        if ("/plan/" in path_text or "/计划/" in path_text or "plan" in name_text or "计划" in name_text) and resolved.suffix.casefold() in {".md", ".txt"}:
            return True
    return False


def _resolve_permission_path(workspace_root: Path, value: str) -> Path | None:
    raw = str(value or "").strip().strip("\"'")
    if not raw:
        return None
    try:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = workspace_root / raw
        resolved = candidate.resolve()
    except OSError:
        return None
    try:
        resolved.relative_to(workspace_root)
    except ValueError:
        return None
    return resolved


def _normalize_permission_path(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").casefold()


def _sync_coomi_runtime_workspace(
    *,
    agent: Any,
    session: Any,
    workspace_root: Path,
    app_context: Any,
) -> None:
    workspace = Path(workspace_root).resolve()
    workspace_text = workspace.as_posix()
    setattr(session, "cwd", workspace_text)
    setattr(agent, "project_path", workspace_text)
    setattr(agent, "app_context", app_context)
    tool_executor = getattr(agent, "tool_executor", None)
    _sync_storydex_tools_workspace(getattr(agent, "tool_registry", None), workspace)
    if tool_executor is not None:
        setattr(tool_executor, "project_path", workspace)
        setattr(tool_executor, "app_context", app_context)
        setattr(tool_executor, "read_only_mode", False)
        _sync_storydex_tools_workspace(getattr(tool_executor, "tool_registry", None), workspace)
        permission_system = getattr(tool_executor, "permission_system", None)
        if permission_system is not None:
            _sync_storydex_permission_context(
                permission_system,
                workspace_root=workspace,
                mode=str(getattr(permission_system, "_storydex_mode", "full_access") or "full_access"),
                plan_mode=bool(getattr(agent, "plan_mode", False)),
            )


def _create_storydex_tool_registry(
    workspace_root: Path,
    policy: ContextPolicy | None = None,
) -> Any:
    from coomi.tools.registry import create_default_registry
    from services.storydex_agent_tools import (
        StorydexApplyStoryIncrementTool,
        StorydexHelpGuideSearchTool,
        StorydexProjectSearchTool,
        StorydexRuntimePresetStatusTool,
        StorydexSyncWikiTool,
        StorydexVersionStatusTool,
        StorydexWikiQueryTool,
    )
    import importlib

    runtime_tools = importlib.import_module("services.storydex_coomi_runtime_tools")

    root = Path(workspace_root).resolve()
    effective_policy = policy if isinstance(policy, ContextPolicy) else ContextPolicy()
    registry = create_default_registry()
    # 同名覆盖默认工具：文件/Shell 工具全部显式绑定工作区，
    # 使 Agent 轮次不再依赖进程级 os.chdir。
    for tool in runtime_tools.create_workspace_bound_tool_overrides(root):
        registry.register(tool)
    external_tool_overrides = getattr(
        runtime_tools,
        "create_replayable_external_tool_overrides",
        lambda: (),
    )
    for tool in external_tool_overrides():
        registry.register(tool)
    registry.register(StorydexRuntimePresetStatusTool(workspace_root=root))
    registry.register(StorydexVersionStatusTool(workspace_root=root))
    registry.register(StorydexHelpGuideSearchTool(workspace_root=root))
    if effective_policy.active_retrieval_tools:
        registry.register(StorydexProjectSearchTool(workspace_root=root))
        registry.register(StorydexWikiQueryTool(workspace_root=root))
    registry.register(StorydexSyncWikiTool(workspace_root=root))
    registry.register(StorydexApplyStoryIncrementTool(workspace_root=root))
    return registry


def _replace_runtime_tool_registry(agent: Any, registry: Any) -> None:
    setattr(agent, "tool_registry", registry)
    tool_executor = getattr(agent, "tool_executor", None)
    if tool_executor is not None:
        setattr(tool_executor, "tool_registry", registry)


def _sync_storydex_tools_workspace(registry: Any, workspace_root: Path) -> None:
    if registry is None:
        return
    lister = getattr(registry, "list_tools", None)
    tools = lister() if callable(lister) else []
    resolved_root = Path(workspace_root).resolve()
    for tool in tools or []:
        setter = getattr(tool, "set_workspace_root", None)
        if callable(setter):
            setter(resolved_root)


def _build_coomi_memory(
    workspace_root: Path,
    policy: ContextPolicy,
    *,
    provider: Any = None,
) -> tuple[Any | None, Any | None]:
    effective_policy = policy if isinstance(policy, ContextPolicy) else ContextPolicy()
    if not effective_policy.coomi_memory:
        return None, None
    from coomi.services.memory import MemoryManager, MemoryRecall
    from services.llm_replay import get_replayable_llm_provider

    memory_provider = provider if provider is not None else get_replayable_llm_provider()
    manager = MemoryManager(project_path=Path(workspace_root).resolve().as_posix())
    return manager, MemoryRecall(memory_provider, manager)


async def _build_coomi_system_prompt(
    *,
    workspace_root: Path,
    prompt: str,
    story_generation: Dict[str, Any] | None = None,
    turn_contract: Dict[str, Any] | None = None,
    plan_mode: bool = False,
) -> str:
    from coomi.engine.session import build_system_prompt
    from services.llm_replay import get_replayable_llm_provider, llm_purpose
    from services.context_trace_service import capture_coomi_memory_source

    provider = get_replayable_llm_provider()
    context_policy = context_policy_from_turn_contract(turn_contract)
    memory_manager, memory_recall = _build_coomi_memory(
        workspace_root,
        context_policy,
        provider=provider,
    )
    with llm_purpose("memory_recall"):
        system_prompt = await build_system_prompt(
            memory_manager=memory_manager,
            memory_recall=memory_recall,
            current_context=prompt,
            cwd=workspace_root.as_posix(),
            model_display=_model_display(provider),
        )
    context_assembly = _dict_value(_dict_value(turn_contract).get("contextAssembly"))
    capture_coomi_memory_source(
        context_assembly,
        system_prompt=system_prompt,
        enabled=context_policy.coomi_memory,
    )
    skills_dir = (workspace_root / ".storydex" / ".agent" / "skills").as_posix()
    story_options = _render_story_generation_options(story_generation)
    contract_options = _render_turn_contract(turn_contract)
    retrieval_tools_prompt = (
        "Storydex registers domain tools outside Coomi: `StorydexRuntimePresetStatus`, "
        "`StorydexVersionStatus`, `StorydexHelpGuideSearch`, `StorydexProjectSearch`, "
        "`StorydexWikiQuery`, `StorydexSyncWiki`, and `StorydexApplyStoryIncrement`. "
        "When the user asks how to use Storydex, where a feature is, or how a menu/settings/WIKI/version workflow works, "
        "call `StorydexHelpGuideSearch` before answering and ground the answer in the guide.\n"
        "Retrieval policy for story continuity: before referencing earlier plot details, foreshadowing, "
        "items, or settings that are NOT already present in the assembled context blocks, verify them first — "
        "use `StorydexProjectSearch` (relevance-ranked full-text search over chapters and project assets) to find "
        "the original passages, or `StorydexWikiQuery` to check entity facts and relationships with evidence. "
        "Never invent past plot facts; if retrieval finds nothing, treat the detail as unestablished and either "
        "avoid it or establish it explicitly as new canon. WIKI query results may contain model inference — "
        "when confidence is low or needsReview is true, confirm against chapters, character files, or variable memory.\n"
        if context_policy.active_retrieval_tools
        else
        "Storydex registers domain tools outside Coomi, but active story retrieval is disabled for this execution: "
        "`StorydexProjectSearch` and `StorydexWikiQuery` are not available. Other Storydex tools remain available. "
        "Do not claim to have called either disabled tool; rely only on the assembled context and ordinary workspace reads.\n"
    )
    storydex_runtime_prompt = (
        "\n\n## Storydex Project Runtime\n\n"
        + f"Storydex project skills live under `{skills_dir}`. "
        + "When a Storydex skill is needed, read the matching skill file from that directory before applying it. "
        + "Do not treat hardcoded prompt text as the skill source of truth.\n"
        + "Authorized Storydex project file edits are direct writes. Do not create preview/pending-write approval artifacts. "
        + "At the end of the turn, Storydex records project file changes with a local Git commit automatically; never push to a remote.\n"
        + retrieval_tools_prompt
        + "Storydex memory governance: `.storydex/memory/` is only for durable story memory and variables. Never write chat history, "
        + "session transcripts, execution logs, plans, tool output, or temporary drafts there; sessions belong under `.storydex/.agent/sessions/`. "
        + "Before reading or writing memory, follow `.storydex/memory/README.md` and its adaptive module catalog. Reuse an existing module when possible, "
        + "keep canonical/derived/index data distinct, require stable entity IDs and evidence, and apply canonical changes through a validated revisioned change set. "
        + "`.storydex/temp/` is only a plain optional creative scratch folder: do not inspect or inject it during normal work unless the user asks or the active task explicitly depends on it.\n"
        + "For story creation or continuation turns, use `StorydexApplyStoryIncrement` after drafting fragments to apply structured increments: "
        + "fragments, variableThoughts or variableNotes as readable Markdown, characterUpdates/newCharacters, "
        + "itemUpdates/newItems, factUpdates, relationshipUpdates, chapterSummary (a 150-300 character rolling "
        + "summary of the chapter so far — pass it every continuation turn to keep mid-range plot context fresh), "
        + "and optional WIKI sync. "
        + "Do not force variable thinking into fixed JSON path/value entries; variableUpdates are optional machine operations only "
        + "when the change is clear enough to merge safely. "
        + "If project settings do not auto-update variables, ask the user before passing applyVariables=true; "
        + "if WIKI is not automatic, ask after variables are applied before passing applyWiki=true. "
        + "All newly mentioned characters must be included in newCharacters or characterUpdates, even when every unknown field is only `未知`.\n"
        + story_options
        + contract_options
    )
    if plan_mode:
        plan_dir = (workspace_root / ".storydex" / ".agent" / "plans").as_posix()
        return (
            system_prompt
            + storydex_runtime_prompt
            + "\n\n## Plan Mode\n\n"
            + "You are in plan mode. You may read project files and ask the user questions. "
            + "Do not modify normal project files. You may write plan documents only, "
            + f"preferably under `{plan_dir}/`, unless the user explicitly names another plan-document path."
        )
    return system_prompt + storydex_runtime_prompt


def _render_story_generation_options(value: Dict[str, Any] | None) -> str:
    payload = value if isinstance(value, dict) else {}
    fragment_count = _bounded_int(payload.get("fragmentCount"), default=1, minimum=1, maximum=20)
    fragment_word_count = _bounded_int(payload.get("fragmentWordCount"), default=2000, minimum=100, maximum=20000)
    return (
        "\nStory generation turn options:\n"
        + f"- fragmentCount: {fragment_count}\n"
        + f"- fragmentWordCount: {fragment_word_count}\n"
        + "Use these values only for story creation or continuation turns; for other tasks, keep them as metadata.\n"
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

    primary = str(intent.get("primary") or "general")
    confidence = str(intent.get("confidence") or "low")
    intent_targets = [str(item) for item in (intent.get("assetTargets") if isinstance(intent.get("assetTargets"), list) else []) if str(item)]
    intent_skills = [str(item) for item in (intent.get("matchedSkills") if isinstance(intent.get("matchedSkills"), list) else []) if str(item)]
    intent_line = f"- intent: {primary} (confidence: {confidence})"
    if intent_targets:
        intent_line += f"; write this intent's outputs under: {', '.join(intent_targets)}"
    if intent_skills:
        intent_line += f"; matching skills: {', '.join(intent_skills)}"
    status = str(contract.get("status") or "ready")
    fragment_count = _bounded_int(turn_plan.get("fragmentCount"), default=1, minimum=1, maximum=20)
    fragment_word_count = _bounded_int(turn_plan.get("fragmentWordCount"), default=2000, minimum=100, maximum=20000)
    requires_template = bool(turn_plan.get("requiresChapterTemplateSelection"))
    selected_template = str(turn_plan.get("selectedChapterTemplate") or "").strip()
    selected_template_detail = _dict_value(turn_plan.get("selectedChapterTemplateDetail"))
    invalid_template = str(turn_plan.get("invalidChapterTemplate") or "").strip()
    next_segment_path = str(turn_plan.get("nextSegmentPath") or "").strip()

    lines = [
        "\nStorydex turn contract:",
        f"- status: {status}",
        intent_line,
        (
            "- execution: "
            f"directFileWrites={bool(execution.get('directFileWrites', True))}, "
            f"pendingWriteApproval={bool(execution.get('pendingWriteApproval', False))}, "
            f"localGitAutoCommit={bool(execution.get('localGitAutoCommit', True))}, "
            f"remotePush={bool(execution.get('remotePush', False))}"
        ),
        f"- storyFragments: count={fragment_count}, wordsEach={fragment_word_count}",
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
        lines.append(f"- selectedChapterTemplate: {_chapter_template_detail_label(selected_template_detail, selected_template)}")
        template_rules = _chapter_template_rules(selected_template_detail)
        if template_rules:
            lines.append(f"- selectedTemplateRules: {template_rules}")
    if next_segment_path:
        lines.append(f"- nextSegmentPath: {next_segment_path}")

    lines.append(
        "- context: inject active or compiled-safe presets only; use recent active characters and relevant facts, "
        "not a full memory dump. The active preset block below contains binding creative rules for this turn; "
        "follow it faithfully when writing story content."
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
    context_blocks = _render_context_assembly_blocks(context_assembly)
    if context_blocks:
        lines.extend(["", "Storydex assembled context blocks:", context_blocks])
    return "\n".join(lines) + "\n"


def _dict_value(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _skill_registry_summary(value: Dict[str, Any]) -> str:
    if not value:
        return ""
    registry_path = str(value.get("registryPath") or ".storydex/.agent/skills/registry.json").strip()
    skills = value.get("skills") if isinstance(value.get("skills"), list) else []
    labels: list[str] = []
    for item in skills[:10]:
        skill = _dict_value(item)
        skill_id = str(skill.get("id") or "").strip()
        file_name = str(skill.get("file") or "").strip()
        if skill_id or file_name:
            labels.append(f"{skill_id or file_name}:{file_name or skill_id}")
    count = int(value.get("skillCount") or len(skills))
    return f"{count} skills at {registry_path}" + (f" ({', '.join(labels)})" if labels else "")


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
    # 组装器最多产出 12 类块（含 wiki_reference / rolling_summaries）；
    # 渲染上限需覆盖全部并留余量，避免尾部块被静默丢弃。
    for raw in blocks[:14]:
        block = _dict_value(raw)
        content = str(block.get("content") or "").strip()
        if not content:
            continue
        title = str(block.get("title") or block.get("id") or "Context").strip()
        source_paths = block.get("sourcePaths") if isinstance(block.get("sourcePaths"), list) else []
        source_suffix = ", ".join(str(path) for path in source_paths[:4] if str(path).strip())
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
    return f"{name} ({template_id})" if name and template_id and name != template_id else template_id or name or fallback


def _chapter_template_rules(value: Dict[str, Any]) -> str:
    pieces: list[str] = []
    chapter_mode = str(value.get("chapterMode") or "").strip()
    chapter_pattern = str(value.get("chapterNamePattern") or "").strip()
    segment_naming = str(value.get("segmentNaming") or "").strip()
    initial_directory = str(value.get("initialChapterDirectory") or "").strip()
    initial_segment = str(value.get("initialChapterFirstSegment") or "").strip()
    if chapter_mode:
        pieces.append(f"mode={chapter_mode}")
    if chapter_pattern:
        pieces.append(f"chapterNamePattern={chapter_pattern}")
    if segment_naming:
        pieces.append(f"segmentNaming={segment_naming}")
    if initial_directory or initial_segment:
        pieces.append(f"initial={initial_directory}/{initial_segment}".strip("/"))
    return ", ".join(pieces)


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


class _StorydexApprovalContext:
    def __init__(
        self,
        *,
        service: StorydexCoomiAgentService,
        event_queue: asyncio.Queue[tuple[str, Dict[str, Any]] | None],
        trace_id: str,
        session_id: str,
    ) -> None:
        self.service = service
        self.event_queue = event_queue
        self.trace_id = trace_id
        self.session_id = session_id
        self.pending_ids: set[str] = set()

    async def _handle_ask_questions(self, questions: list[Dict[str, Any]]) -> Dict[Any, Any]:
        answers: Dict[Any, Any] = {}
        total = len(questions)
        pending: list[tuple[int, str, asyncio.Future[Dict[str, Any]]]] = []
        # 先把全部问题一次性发给前端：前端可以在最终提交前来回切换、修改每题答案。
        for index, question in enumerate(questions):
            approval_id = f"{self.trace_id}-{uuid4().hex}"
            future: asyncio.Future[Dict[str, Any]] = asyncio.get_running_loop().create_future()
            self.service._approval_waiters[approval_id] = future
            self.pending_ids.add(approval_id)
            is_permission = _is_permission_question(question)
            await self.event_queue.put((
                "PermissionRequest",
                {
                    "_type": "PermissionRequest",
                    "_version": 1,
                    "kind": "permission" if is_permission else "question",
                    "approval_id": approval_id,
                    "approvalId": approval_id,
                    "session_id": self.session_id,
                    "sessionId": self.session_id,
                    "header": str(question.get("header") or ("权限" if is_permission else f"Q{index + 1}")),
                    "question": str(question.get("question") or "允许 Coomi 执行这个操作吗？"),
                    "options": _approval_options(question.get("options"), is_permission=is_permission),
                    "allowText": not is_permission,
                    "multiSelect": bool(question.get("multiSelect")),
                    "questionIndex": index + 1,
                    "questionTotal": total,
                },
            ))
            pending.append((index, approval_id, future))
        cancelled = False
        for index, approval_id, future in pending:
            try:
                answer = await future
            finally:
                self.service._approval_waiters.pop(approval_id, None)
                self.pending_ids.discard(approval_id)
            if answer.get("__cancelled__"):
                cancelled = True
                break
            answers[index] = answer
        if cancelled:
            self.cancel_pending()
            return {"__cancelled__": True}
        return answers

    def cancel_pending(self) -> None:
        for approval_id in list(self.pending_ids):
            future = self.service._approval_waiters.pop(approval_id, None)
            if future is not None and not future.done():
                future.get_loop().call_soon_threadsafe(future.set_result, {"__cancelled__": True})
            self.pending_ids.discard(approval_id)


def _knowledge_review_from_tool_preview(tool_name: str, result_preview: str) -> Dict[str, Any] | None:
    if tool_name != "StorydexApplyStoryIncrement":
        return None
    marker = '"knowledgeReview":'
    marker_index = result_preview.find(marker)
    if marker_index < 0:
        return None
    value_start = marker_index + len(marker)
    try:
        value, _ = json.JSONDecoder().raw_decode(result_preview[value_start:])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("code") != "knowledge_review_required":
        return None
    return value


class _CoomiEventTranslator:
    def __init__(self, *, session_id: str) -> None:
        self.session_id = session_id
        self.sequence = 0
        self.active_by_tool: dict[str, list[str]] = {}
        self.awaiting_execution_by_tool: dict[str, list[str]] = {}
        self.ready_by_tool: dict[str, list[str]] = {}
        self.running_by_tool: dict[str, list[str]] = {}

    def translate(self, event: Any) -> tuple[str, Dict[str, Any]] | None:
        name = type(event).__name__
        if name == "TextChunk":
            return "TextChunk", {"_type": "TextChunk", "_version": 1, "content": str(getattr(event, "content", ""))}
        if name == "ReasoningChunk":
            return "ReasoningChunk", {"_type": "ReasoningChunk", "_version": 1, "content": str(getattr(event, "content", ""))}
        if name == "ToolStart":
            tool_name = str(getattr(event, "tool_name", ""))
            event_tool_call_id = str(getattr(event, "tool_call_id", "") or "").strip()
            announced_id = self._claim_announced_tool(tool_name, event_tool_call_id or None)
            if announced_id is not None:
                return None
            tool_call_id = self._start_tool(tool_name, event_tool_call_id or None)
            return "ToolStart", {
                "_type": "ToolStart",
                "_version": 1,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "arguments": getattr(event, "arguments", {}) or {},
            }
        if name == "ToolRunning":
            tool_name = str(getattr(event, "tool_name", ""))
            event_tool_call_id = str(getattr(event, "tool_call_id", "") or "").strip()
            return "ToolRunning", {
                "_type": "ToolRunning",
                "_version": 1,
                "tool_name": tool_name,
                "tool_call_id": self._mark_tool_running(tool_name, event_tool_call_id or None),
                "progress": "running",
            }
        if name == "ToolDone":
            tool_name = str(getattr(event, "tool_name", ""))
            elapsed = float(getattr(event, "elapsed", 0.0) or 0.0)
            event_tool_call_id = str(getattr(event, "tool_call_id", "") or "").strip()
            tool_call_id = self._finish_tool(tool_name, event_tool_call_id or None)
            result_preview = str(getattr(event, "result_preview", "") or "")
            payload = {
                "_type": "ToolDone",
                "_version": 1,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "is_error": bool(getattr(event, "is_error", False)),
                "result_preview": result_preview,
                "duration_ms": int(elapsed * 1000),
                "metrics": {"durationMs": int(elapsed * 1000)},
            }
            knowledge_review = _knowledge_review_from_tool_preview(tool_name, result_preview)
            if knowledge_review is not None:
                payload["knowledge_review"] = knowledge_review
            return "ToolDone", payload
        if name == "UsageUpdate":
            self.awaiting_execution_by_tool = {
                tool_name: list(tool_call_ids)
                for tool_name, tool_call_ids in self.active_by_tool.items()
                if tool_call_ids
            }
            return "UsageUpdate", {"_type": "UsageUpdate", "_version": 1, "usage": getattr(event, "usage", {}) or {}}
        if name == "CompressionEvent":
            before_count = int(getattr(event, "before", 0) or 0)
            after_count = int(getattr(event, "after", 0) or 0)
            return "CompressionEvent", {
                "_type": "CompressionEvent",
                "_version": 1,
                "strategy": "coomi",
                "original_messages": before_count,
                "compressed_messages": after_count,
                "compact_status": "completed",
                "summary": f"Coomi compressed conversation context: {before_count} -> {after_count} messages.",
            }
        if name.startswith("Loop"):
            return "TurnPhase", {
                "_type": "TurnPhase",
                "_version": 1,
                "phase": "loop",
                "label": name,
                "status": "running",
                "current": int(getattr(event, "current_step", getattr(event, "step_index", 0)) or 0),
                "total": int(getattr(event, "total_steps", 0) or 0),
                "detail": str(getattr(event, "step_description", "") or ""),
            }
        if name == "AgentCancelled":
            return "AgentCancelled", {"_type": "AgentCancelled", "_version": 1, "session_id": self.session_id, "reason": "cancelled"}
        if name == "AgentError":
            return "AgentError", {
                "_type": "AgentError",
                "_version": 1,
                "error_type": "CoomiAgentError",
                "message": _coomi_error_message(getattr(event, "message", "") or ""),
                "details": {"fatal": bool(getattr(event, "is_fatal", False))},
            }
        return None

    def _new_tool_call_id(self, tool_name: str) -> str:
        self.sequence += 1
        return f"coomi-{self.sequence}-{tool_name or 'tool'}"

    def _start_tool(self, tool_name: str, tool_call_id: str | None = None) -> str:
        tool_call_id = tool_call_id or self._new_tool_call_id(tool_name)
        self.active_by_tool.setdefault(tool_name, []).append(tool_call_id)
        return tool_call_id

    def _current_tool_id(self, tool_name: str) -> str:
        active = self.active_by_tool.get(tool_name)
        if active:
            return active[0]
        return self._start_tool(tool_name)

    def _claim_announced_tool(self, tool_name: str, tool_call_id: str | None = None) -> str | None:
        awaiting = self.awaiting_execution_by_tool.get(tool_name)
        if not awaiting:
            return None
        if tool_call_id and tool_call_id in awaiting:
            awaiting.remove(tool_call_id)
            resolved = tool_call_id
        else:
            resolved = awaiting.pop(0)
        if not awaiting:
            self.awaiting_execution_by_tool.pop(tool_name, None)
        self.ready_by_tool.setdefault(tool_name, []).append(resolved)
        return resolved

    def _mark_tool_running(self, tool_name: str, tool_call_id: str | None = None) -> str:
        resolved = tool_call_id
        ready = self.ready_by_tool.get(tool_name)
        if resolved and ready and resolved in ready:
            ready.remove(resolved)
        elif resolved is None and ready:
            resolved = ready.pop(0)
        if ready is not None and not ready:
            self.ready_by_tool.pop(tool_name, None)

        if resolved is None:
            awaiting = self.awaiting_execution_by_tool.get(tool_name)
            if awaiting:
                resolved = awaiting.pop(0)
                if not awaiting:
                    self.awaiting_execution_by_tool.pop(tool_name, None)
        if resolved is None:
            resolved = self._current_tool_id(tool_name)
        running = self.running_by_tool.setdefault(tool_name, [])
        if resolved not in running:
            running.append(resolved)
        return resolved

    def _finish_tool(self, tool_name: str, tool_call_id: str | None = None) -> str:
        resolved = tool_call_id
        running = self.running_by_tool.get(tool_name)
        if resolved and running and resolved in running:
            running.remove(resolved)
        elif resolved is None and running:
            resolved = running.pop(0)
        if running is not None and not running:
            self.running_by_tool.pop(tool_name, None)

        if resolved is None:
            ready = self.ready_by_tool.get(tool_name)
            if ready:
                resolved = ready.pop(0)
                if not ready:
                    self.ready_by_tool.pop(tool_name, None)
        if resolved is None:
            awaiting = self.awaiting_execution_by_tool.get(tool_name)
            if awaiting:
                resolved = awaiting.pop(0)
                if not awaiting:
                    self.awaiting_execution_by_tool.pop(tool_name, None)

        active = self.active_by_tool.get(tool_name)
        if resolved and active and resolved in active:
            active.remove(resolved)
            if not active:
                self.active_by_tool.pop(tool_name, None)
            return resolved
        if active:
            resolved = active.pop(0)
            if not active:
                self.active_by_tool.pop(tool_name, None)
            return resolved
        return resolved or self._new_tool_call_id(tool_name)


def _install_coomi_home_redirects() -> bool:
    """Point Coomi's Path.home()-based storage at ~/.storydex/.coomi via patches.

    Coomi 0.1.x offers no explicit config/home parameters, so Storydex used to
    swap the HOME/USERPROFILE environment variables around every call — a
    process-global race. These class-level patches redirect the same paths
    once, without touching the environment. Returns False when the Coomi
    internals have drifted, in which case the caller falls back to the legacy
    env swap so functionality is preserved.
    """
    global _COOMI_HOME_REDIRECTS_INSTALLED
    if _COOMI_HOME_REDIRECTS_INSTALLED:
        return True
    with _COOMI_REDIRECT_INSTALL_LOCK:
        if _COOMI_HOME_REDIRECTS_INSTALLED:
            return True
        coomi_root = STORYDEX_COOMI_HOME / ".coomi"
        try:
            from coomi.services import session_history as coomi_session_history
            from coomi.services.llm.config import ConfigManager
            from coomi.services.memory.manager import MemoryManager
        except Exception:
            return False

        try:
            if not getattr(ConfigManager, "_storydex_home_redirect", False):
                original_config_init = ConfigManager.__init__

                def config_init_with_redirect(self: Any) -> None:
                    # 原 __init__ 可能在真实 HOME 下建一个空模板文件（无害），
                    # 之后强制指向 Storydex 目录并重新加载。
                    original_config_init(self)
                    self.config_dir = coomi_root / "config"
                    self.config_path = self.config_dir / "providers.json"
                    self.data = self._load()

                ConfigManager.__init__ = config_init_with_redirect
                setattr(ConfigManager, "_storydex_home_redirect", True)

            if not getattr(MemoryManager, "_storydex_home_redirect", False):
                def global_memory_dir(self: Any) -> Path:
                    return coomi_root / "memory"

                def project_memory_dir(self: Any, project_path: Any) -> Path:
                    if not project_path:
                        return self._get_global_memory_dir()
                    resolved = Path(project_path).resolve()
                    return coomi_root / "projects" / self._generate_project_hash(resolved) / "memory"

                MemoryManager._get_global_memory_dir = global_memory_dir
                MemoryManager._get_project_memory_dir = project_memory_dir
                setattr(MemoryManager, "_storydex_home_redirect", True)

            if not getattr(coomi_session_history, "_storydex_home_redirect", False):
                coomi_session_history.default_sessions_dir = lambda: STORYDEX_COOMI_SESSIONS
                setattr(coomi_session_history, "_storydex_home_redirect", True)
        except Exception:
            return False

        _COOMI_HOME_REDIRECTS_INSTALLED = True
        return True


@contextmanager
def _storydex_coomi_home() -> Iterator[None]:
    STORYDEX_COOMI_HOME.mkdir(parents=True, exist_ok=True)
    _ensure_storydex_coomi_config()
    _install_coomi_endpoint_compat()
    if _install_coomi_home_redirects():
        yield
        return
    # 兜底：Coomi 内部结构变化导致重定向补丁失效时，退回旧的环境变量切换。
    previous_home = os.environ.get("HOME")
    previous_userprofile = os.environ.get("USERPROFILE")
    os.environ["HOME"] = str(STORYDEX_COOMI_HOME)
    os.environ["USERPROFILE"] = str(STORYDEX_COOMI_HOME)
    try:
        yield
    finally:
        _restore_env("HOME", previous_home)
        _restore_env("USERPROFILE", previous_userprofile)


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def _ensure_storydex_coomi_config() -> None:
    if STORYDEX_COOMI_CONFIG.exists():
        return
    STORYDEX_COOMI_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    STORYDEX_COOMI_CONFIG.write_text('{\n  "active": "",\n  "providers": {}\n}\n', encoding="utf-8")


def _context_snapshot(*, session: Any = None, agent: Any = None) -> Dict[str, Any]:
    context_window = int(getattr(agent, "context_window_size", 0) or _resolve_context_window())
    usage = getattr(session, "token_usage", None)
    used_tokens = int(getattr(session, "last_prompt_tokens", 0) or 0)
    cumulative_tokens = int(getattr(usage, "total_tokens", 0) or 0)
    if used_tokens <= 0:
        used_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    usage_ratio = (used_tokens / context_window) if context_window > 0 else 0.0
    compact_threshold = int(context_window * COMPACT_THRESHOLD_RATIO)
    warning_threshold = int(context_window * WARNING_THRESHOLD_RATIO)
    return {
        "context_window": context_window,
        "contextWindow": context_window,
        "used_tokens": used_tokens,
        "usedTokens": used_tokens,
        "usage_ratio": usage_ratio,
        "usageRatio": usage_ratio,
        "cumulative_tokens": cumulative_tokens,
        "cumulativeTokens": cumulative_tokens,
        "compact_threshold": compact_threshold,
        "compactThreshold": compact_threshold,
        "warning_threshold": warning_threshold,
        "warningThreshold": warning_threshold,
    }


def _attach_context_snapshot(
    payload: Dict[str, Any],
    *,
    session: Any = None,
    agent: Any = None,
    compressed: bool = False,
) -> None:
    snapshot = _context_snapshot(session=session, agent=agent)
    usage = payload.get("usage")
    if isinstance(usage, dict):
        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("promptTokens") or 0)
        total_tokens = int(usage.get("total_tokens") or usage.get("totalTokens") or 0)
        if prompt_tokens > 0:
            snapshot["used_tokens"] = prompt_tokens
            snapshot["usedTokens"] = prompt_tokens
            snapshot["usage_ratio"] = prompt_tokens / max(1, int(snapshot["contextWindow"]))
            snapshot["usageRatio"] = snapshot["usage_ratio"]
        if total_tokens > 0:
            snapshot["last_total_tokens"] = total_tokens
            snapshot["lastTotalTokens"] = total_tokens
    payload.update(snapshot)
    payload["compression_status"] = "compressed" if compressed else payload.get("compression_status", "idle")
    payload["compressionStatus"] = payload["compression_status"]


def _parse_slash_command(prompt: str) -> Dict[str, str]:
    text = str(prompt or "").strip()
    if not text.startswith("/"):
        return {"name": "", "body": ""}
    head, _, body = text.partition(" ")
    return {"name": head.strip().lower().lstrip("/"), "body": body.strip()}


def _normalize_permission_mode(mode: str) -> str:
    normalized = str(mode or "").strip().lower().replace("-", "_")
    aliases = {
        "ask": "ask_approval",
        "askapproval": "ask_approval",
        "approve": "approve_for_me",
        "auto": "approve_for_me",
        "full": "full_access",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in {"ask_approval", "approve_for_me", "full_access"} else "full_access"


def _coomi_permission_mode(permission_mode_enum: Any, mode: str) -> Any:
    return {
        "ask_approval": permission_mode_enum.ASK_APPROVAL,
        "approve_for_me": permission_mode_enum.APPROVE_FOR_ME,
        "full_access": permission_mode_enum.FULL_ACCESS,
    }[_normalize_permission_mode(mode)]


def _permission_label(mode: str) -> str:
    return {
        "ask_approval": "询问确认",
        "approve_for_me": "自动批准",
        "full_access": "完全访问",
    }.get(_normalize_permission_mode(mode), "Full access")


def _approval_answer(decision: str, response: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if decision == "cancel":
        return {"__cancelled__": True}
    if isinstance(response, dict) and response:
        return dict(response)
    if decision in {"allow", "deny"}:
        return {
            "option": decision,
            "label": "Allow" if decision == "allow" else "Deny",
            "other_text": None,
        }
    return {
        "option": decision or "answer",
        "label": decision or "Answer",
        "other_text": None,
    }


def _is_permission_question(question: Dict[str, Any]) -> bool:
    options = question.get("options")
    if not isinstance(options, list):
        return False
    values = {
        str(item.get("value") or item.get("option") or item.get("label") or "").strip().lower()
        for item in options
        if isinstance(item, dict)
    }
    return {"allow", "deny"}.issubset(values)


def _approval_options(value: Any, *, is_permission: bool = True) -> list[Dict[str, Any]]:
    permission_defaults = [
        {
            "label": "Allow",
            "value": "allow",
            "description": "Run this tool call once.",
            "isRecommended": True,
        },
        {
            "label": "Deny",
            "value": "deny",
            "description": "Return a permission denied result to the model.",
            "isRecommended": False,
        },
    ]
    question_defaults = [
        {
            "label": "回复",
            "value": "answer",
            "description": "输入回复后确认。",
            "isRecommended": True,
        }
    ]
    defaults = permission_defaults if is_permission else question_defaults
    if not isinstance(value, list):
        return defaults
    options: list[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        option_value = str(item.get("value") or item.get("option") or label).strip()
        if not option_value and not label:
            continue
        options.append({
            "label": label or option_value,
            "value": option_value,
            "description": str(item.get("description") or ""),
            "isRecommended": bool(item.get("is_recommended") or item.get("isRecommended")),
        })
    return options or defaults


def _resolve_loop_spec(workspace_root: Path, body: str) -> tuple[str | None, Any]:
    from coomi.types import Spec

    text = str(body or "").strip()
    candidate = _safe_loop_spec_path(workspace_root, text)
    if candidate is not None and candidate.exists() and candidate.is_file():
        return candidate.as_posix(), None
    steps = [line.strip().lstrip("-").strip() for line in text.splitlines() if line.strip()] or [text]
    title = steps[0][:80] or "Coomi Loop Task"
    return None, Spec(
        title=title,
        goal=text,
        steps=steps,
        constraints=[],
        acceptance_criteria=[],
        resources={"workspace": workspace_root.as_posix()},
        tools_allowed=[],
        tools_forbidden=[],
    )


def _safe_loop_spec_path(workspace_root: Path, value: str) -> Path | None:
    raw = str(value or "").strip().strip('"')
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = workspace_root / raw
    try:
        resolved = path.resolve()
    except OSError:
        return None
    root = workspace_root.resolve()
    return resolved if resolved == root or root in resolved.parents else None


def _model_display(provider: Any) -> str:
    display = getattr(provider, "get_model_display_name", None)
    if callable(display):
        try:
            return str(display())
        except Exception:
            pass
    return str(getattr(provider, "model", "") or "coomi")


def _coomi_error_message(error: Any) -> str:
    message = str(error or "").strip()
    lowered = message.lower()
    if (
        "model_not_supported" in lowered
        or "not supported on the lite model list" in lowered
        or ("model" in lowered and "use get" in lowered and "/models" in lowered)
    ):
        matched = re.search(r"model\s+([^\s'\"},]+)\s+is\s+not\s+supported", message, flags=re.IGNORECASE)
        model = matched.group(1) if matched else "当前模型"
        return (
            f"模型 {model} 暂时未被服务端模型目录接受。Storydex 已自动重试一次仍未成功；"
            "请打开 Coomi 设置，重新获取模型列表，选择可用模型后点击“应用”。"
        )
    return message or "Coomi 执行失败。"


def _is_cancelled(token: Any) -> bool:
    checker = getattr(token, "is_cancelled", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return False


def _agent_started(*, session_id: str, prompt: str, status: Dict[str, Any], mode: str) -> tuple[str, Dict[str, Any]]:
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


_SERVICE = StorydexCoomiAgentService()


def get_storydex_coomi_agent_service() -> StorydexCoomiAgentService:
    return _SERVICE
