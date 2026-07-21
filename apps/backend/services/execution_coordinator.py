from __future__ import annotations

import asyncio
import contextvars
import hashlib
import inspect
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Awaitable, Callable, Dict, Iterable, TypeVar
from uuid import uuid4


_LOGGER = logging.getLogger(__name__)
_PREPARATION_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="storydex-execution-preparation")
_OUTCOMES = {"completed", "failed", "cancelled"}
_TERMINAL_STATES = _OUTCOMES | {"rejected", "unfinished"}
_T = TypeVar("_T")


class ExecutionBusyError(RuntimeError):
    pass


class ExecutionStateError(RuntimeError):
    pass


class SnapshotConfirmationRequired(ExecutionStateError):
    code = "SNAPSHOT_FAILED"

    def __init__(self, message: str, *, details: Dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


@dataclass(frozen=True)
class ExecutionObservation:
    completed: bool = False
    error_message: str = ""
    error_code: str = ""
    cancelled: bool = False


@dataclass
class ExecutionFinalizationContext:
    finish_git: Callable[[], Dict[str, Any] | Awaitable[Dict[str, Any]]]
    build_payload: Callable[
        [str, str, bool, Dict[str, float]],
        Dict[str, Any] | Awaitable[Dict[str, Any]],
    ]
    on_git_payload: Callable[[Dict[str, Any]], Any] | None = None
    on_terminal: Callable[[str, str], Any] | None = None
    persist_trace: Callable[[Dict[str, Any]], Any] | None = None
    write_timing: Callable[[Dict[str, Any]], Any] | None = None


@dataclass(frozen=True)
class ExecutionFinalizationResult:
    status: str
    error_message: str
    git_payload: Dict[str, Any]
    payload_data: Dict[str, Any]
    timings_ms: Dict[str, float]


class ExecutionCoordinator:
    """Own the single top-level Storydex execution and its finalization."""

    def __init__(self, *, trace_history_service: Any = None) -> None:
        self._slot = Lock()
        self._state_lock = Lock()
        self._active: ExecutionHandle | None = None
        self._reserved = False
        self._trace_history_service = trace_history_service
        self._timing_samples: Dict[str, list[float]] = {}

    def begin(self, workspace_root: Path, session_id: str, trace_id: str) -> "ExecutionHandle":
        started = time.perf_counter()
        if not self._slot.acquire(blocking=False):
            raise ExecutionBusyError("another Storydex execution is already active")
        try:
            self._reconcile_workspace_unlocked(Path(workspace_root).resolve())
            handle = self._create_handle(workspace_root, session_id, trace_id)
        except Exception:
            self._slot.release()
            raise
        handle._record_timing("begin", started)
        return handle

    def try_reserve(self) -> bool:
        if not self._slot.acquire(blocking=False):
            return False
        with self._state_lock:
            self._reserved = True
        return True

    def release_reservation(self) -> None:
        with self._state_lock:
            if not self._reserved:
                return
            self._reserved = False
        self._slot.release()

    def adopt_reservation(
        self,
        workspace_root: Path,
        session_id: str,
        trace_id: str,
    ) -> "ExecutionHandle":
        started = time.perf_counter()
        with self._state_lock:
            if not self._reserved:
                raise ExecutionStateError("there is no reserved execution slot")
            self._reserved = False
        try:
            self._reconcile_workspace_unlocked(Path(workspace_root).resolve())
            handle = self._create_handle(workspace_root, session_id, trace_id)
        except Exception:
            self._slot.release()
            raise
        handle._record_timing("begin", started)
        return handle

    def adopt_reservation_or_begin(
        self,
        workspace_root: Path,
        session_id: str,
        trace_id: str,
    ) -> "ExecutionHandle":
        with self._state_lock:
            reserved = self._reserved
        if reserved:
            return self.adopt_reservation(workspace_root, session_id, trace_id)
        return self.begin(workspace_root, session_id, trace_id)

    async def run_serialized_preparation(
        self,
        operation: Callable[[], Awaitable[_T]],
    ) -> _T:
        """Run cold async setup on one worker loop, away from request loops."""

        context = contextvars.copy_context()

        def run() -> _T:
            return context.run(asyncio.run, operation())

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_PREPARATION_EXECUTOR, run)

    async def classify_intent(self, intent_service: Any, **kwargs: Any) -> Dict[str, Any]:
        return await self.run_serialized_preparation(
            lambda: intent_service.classify_intent(**kwargs)
        )

    def reconcile_workspace(self, workspace_root: Path) -> list[Dict[str, Any]]:
        if not self._slot.acquire(blocking=False):
            return []
        try:
            return self._reconcile_workspace_unlocked(Path(workspace_root).resolve())
        finally:
            self._slot.release()

    def reconcile_workspaces(self, workspace_roots: Iterable[Path]) -> list[Dict[str, Any]]:
        reconciled: list[Dict[str, Any]] = []
        for workspace_root in workspace_roots:
            reconciled.extend(self.reconcile_workspace(workspace_root))
        return reconciled

    def timing_report(self) -> Dict[str, Any]:
        with self._state_lock:
            samples = {key: list(values) for key, values in self._timing_samples.items()}
        return {
            key: {
                "count": len(values),
                "meanMs": round(sum(values) / len(values), 4) if values else 0.0,
                "maxMs": round(max(values), 4) if values else 0.0,
            }
            for key, values in sorted(samples.items())
        }

    def _create_handle(self, workspace_root: Path, session_id: str, trace_id: str) -> "ExecutionHandle":
        handle = ExecutionHandle(
            coordinator=self,
            workspace_root=Path(workspace_root).resolve(),
            session_id=str(session_id or "default").strip() or "default",
            trace_id=str(trace_id or "").strip(),
        )
        if not handle.trace_id:
            raise ValueError("trace_id is required")
        with self._state_lock:
            if self._active is not None:
                raise ExecutionBusyError("another Storydex execution is already active")
            self._active = handle
        try:
            handle._write_intent(state="running")
        except Exception:
            with self._state_lock:
                self._active = None
            raise
        return handle

    def _release(self, handle: "ExecutionHandle") -> None:
        with self._state_lock:
            if self._active is not handle:
                return
            self._active = None
        self._slot.release()

    def _record_sample(self, name: str, elapsed_ms: float) -> None:
        normalized = round(max(0.0, float(elapsed_ms)), 4)
        with self._state_lock:
            values = self._timing_samples.setdefault(name, [])
            values.append(normalized)
            if len(values) > 200:
                del values[:-200]

    def _trace_service(self) -> Any:
        if self._trace_history_service is None:
            from services.trace_history_service import get_trace_history_service

            self._trace_history_service = get_trace_history_service()
        return self._trace_history_service

    def _reconcile_workspace_unlocked(self, workspace_root: Path) -> list[Dict[str, Any]]:
        intent_root = _intent_root(workspace_root)
        if not intent_root.is_dir():
            return []
        reconciled: list[Dict[str, Any]] = []
        for path in sorted(intent_root.glob("*.json")):
            payload = _read_json(path)
            state = str(payload.get("state") or "").strip()
            if not payload or state in _TERMINAL_STATES:
                continue
            trace_id = str(payload.get("traceId") or "").strip()
            session_id = str(payload.get("sessionId") or "default").strip() or "default"
            if not trace_id:
                continue
            now = _now_iso()
            record = {
                "traceId": trace_id,
                "sessionId": session_id,
                "workspaceRoot": workspace_root.as_posix(),
                "status": "unfinished",
                "createdAt": str(payload.get("createdAt") or now),
                "updatedAt": now,
                "errorMessage": "Execution finalization was interrupted before completion.",
                "errorCode": "execution_unfinished",
                "unfinished": True,
                "execution": {
                    "state": "unfinished",
                    "reconciledAt": now,
                    "replayed": False,
                    "noRestorePoint": bool(payload.get("noRestorePoint")),
                },
            }
            service = self._trace_service()
            storydex_root = workspace_root / ".storydex"
            writer = getattr(service, "upsert_record_atomic_at_storydex_root", None)
            if callable(writer):
                writer(storydex_root, record, session_id)
            else:
                service.upsert_record(record, session_id)
            payload.update({"state": "unfinished", "reconciledAt": now, "replayed": False})
            _atomic_write_json(path, payload)
            reconciled.append(record)
        return reconciled


@dataclass
class ExecutionHandle:
    coordinator: ExecutionCoordinator
    workspace_root: Path
    session_id: str
    trace_id: str
    state: str = "running"
    no_restore_point: bool = False
    snapshot_available: bool | None = None
    snapshot_details: Dict[str, Any] = field(default_factory=dict)
    cancel_reason: str = ""
    _created_at: str = field(default_factory=lambda: _now_iso(), init=False)
    _state_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _cancel_callbacks: list[Callable[[str], Any]] = field(default_factory=list, init=False, repr=False)
    _finalization_task: asyncio.Task[ExecutionFinalizationResult] | None = field(default=None, init=False, repr=False)
    _timings_ms: Dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _released: bool = field(default=False, init=False, repr=False)

    @property
    def intent_path(self) -> Path:
        return _intent_path(self.workspace_root, self.trace_id)

    @property
    def is_cancelled(self) -> bool:
        with self._state_lock:
            return self.state == "cancelling" or self.cancel_reason != ""

    def bind_cancellation(self, callback: Callable[[str], Any]) -> None:
        with self._state_lock:
            if callback not in self._cancel_callbacks:
                self._cancel_callbacks.append(callback)

    def register_snapshot(self, snapshot: Any, *, confirm_no_snapshot: bool = False) -> None:
        started = time.perf_counter()
        available = bool(getattr(snapshot, "available", False))
        details = {
            "available": available,
            "reason": str(getattr(snapshot, "reason", "") or ""),
            "workspaceRoot": self.workspace_root.as_posix(),
        }
        if not available and not confirm_no_snapshot:
            raise SnapshotConfirmationRequired(
                "Storydex could not create a restore point. Confirm this run to continue without one.",
                details=details,
            )
        with self._state_lock:
            self.snapshot_available = available
            self.no_restore_point = not available
            self.snapshot_details = details
        self._write_intent(
            state=self.state,
            extra={
                "snapshotAvailable": available,
                "noRestorePoint": self.no_restore_point,
                "snapshot": details,
            },
        )
        self._record_timing("snapshot_registration", started)

    def cancel(self, reason: str = "cancelled") -> bool:
        started = time.perf_counter()
        callbacks: list[Callable[[str], Any]] = []
        with self._state_lock:
            if self._released or self.state in _TERMINAL_STATES or self.state == "cancelling":
                self._record_timing("cancel", started)
                return False
            self.state = "cancelling"
            self.cancel_reason = str(reason or "cancelled")
            callbacks = list(self._cancel_callbacks)
        try:
            self._write_intent(
                state="cancelling",
                extra={"cancelReason": self.cancel_reason, "cancelledAt": _now_iso()},
            )
        finally:
            for callback in callbacks:
                try:
                    callback(self.cancel_reason)
                except Exception as exc:
                    _LOGGER.warning("Execution cancel callback failed for %s: %s", self.trace_id, exc)
            self._record_timing("cancel", started)
        return True

    def abandon(self, reason: str = "worker_cancelled") -> bool:
        """Release an execution whose worker was forcibly cancelled.

        This is only a crash/loop-shutdown fallback. It deliberately keeps the
        intent file so the next workspace bootstrap can reconcile the execution
        as unfinished instead of manufacturing a terminal trace.
        """
        started = time.perf_counter()
        with self._state_lock:
            if self._released:
                self._record_timing("abandon", started)
                return False
            if self._finalization_task is not None and not self._finalization_task.done():
                # A shielded finalization is still responsible for releasing us.
                self._record_timing("abandon", started)
                return False
            normalized_reason = str(reason or "worker_cancelled")
            if not self.cancel_reason:
                self.cancel_reason = normalized_reason
            self.state = "finalization_failed"
        try:
            self._write_intent(
                state="finalization_failed",
                extra={
                    "unfinished": True,
                    "abandonReason": normalized_reason,
                    "finalizationFailedAt": _now_iso(),
                },
            )
        except Exception as exc:
            _LOGGER.warning("Unable to persist abandoned execution intent for %s: %s", self.trace_id, exc)
        finally:
            self._record_timing("abandon", started)
            self._release()
        return True

    def reject_preflight(self, error_code: str, message: str = "") -> None:
        with self._state_lock:
            if self._released or self.state in _TERMINAL_STATES:
                return
            self.state = "rejected"
        try:
            self._write_intent(
                state="rejected",
                extra={"errorCode": str(error_code or ""), "errorMessage": str(message or "")},
            )
            self._delete_intent()
        finally:
            self._release()

    async def finalize(
        self,
        observation: ExecutionObservation,
        context: ExecutionFinalizationContext,
    ) -> ExecutionFinalizationResult:
        with self._state_lock:
            if self._released or self._finalization_task is not None or self.state in _TERMINAL_STATES:
                raise ExecutionStateError(f"execution {self.trace_id} is already finalized")
            task = asyncio.create_task(
                self._finalize_impl(observation, context),
                name=f"storydex-finalize-{self.trace_id}",
            )
            self._finalization_task = task
        return await asyncio.shield(task)

    async def wait_finalized(self) -> ExecutionFinalizationResult | None:
        with self._state_lock:
            task = self._finalization_task
        return await asyncio.shield(task) if task is not None else None

    async def _finalize_impl(
        self,
        observation: ExecutionObservation,
        context: ExecutionFinalizationContext,
    ) -> ExecutionFinalizationResult:
        finalize_started = time.perf_counter()
        status = self._resolve_status(observation)
        error_message = str(observation.error_message or "")
        git_payload: Dict[str, Any] = {}
        payload_data: Dict[str, Any] = {}
        persisted = False
        try:
            self._write_intent(
                state="finalizing",
                extra={"outcome": status, "finalizationStartedAt": _now_iso()},
            )

            step_started = time.perf_counter()
            try:
                git_payload = await _invoke(context.finish_git, in_thread=True)
                if not isinstance(git_payload, dict):
                    git_payload = {}
            except Exception as exc:
                git_payload = {
                    "_type": "GitAutoCommit",
                    "status": "error",
                    "created": False,
                    "message": str(exc),
                }
            self._record_timing("finish_git", step_started)
            if str(git_payload.get("status") or "") == "error":
                if not error_message:
                    error_message = str(git_payload.get("message") or "Local Git finalization failed.")
                if status != "cancelled":
                    status = "failed"
            if context.on_git_payload is not None:
                await _invoke(lambda: context.on_git_payload(git_payload))

            if context.on_terminal is not None:
                await _invoke(lambda: context.on_terminal(status, error_message))

            self._timings_ms["finalizeBeforePayload"] = round(
                (time.perf_counter() - finalize_started) * 1000,
                4,
            )
            step_started = time.perf_counter()
            payload_data = await _invoke(
                lambda: context.build_payload(
                    status,
                    error_message,
                    self.no_restore_point,
                    dict(self._timings_ms),
                )
            )
            if not isinstance(payload_data, dict):
                raise ExecutionStateError("finalization payload builder returned a non-object")
            self._record_timing("build_payload", step_started)
            record = payload_data.get("record")
            if not isinstance(record, dict):
                raise ExecutionStateError("finalization payload is missing a trace record")
            record.update(
                {
                    "status": status,
                    "errorMessage": error_message,
                    "noRestorePoint": self.no_restore_point,
                    "execution": {
                        "state": status,
                        "traceId": self.trace_id,
                        "sessionId": self.session_id,
                        "workspaceRoot": self.workspace_root.as_posix(),
                        "snapshotAvailable": self.snapshot_available,
                        "noRestorePoint": self.no_restore_point,
                        "cancelReason": self.cancel_reason,
                        "timingsMs": dict(self._timings_ms),
                    },
                }
            )
            if observation.error_code:
                record["errorCode"] = observation.error_code

            step_started = time.perf_counter()
            if context.persist_trace is not None:
                await _invoke(lambda: context.persist_trace(record), in_thread=True)
            else:
                service = self.coordinator._trace_service()
                writer = getattr(service, "upsert_record_atomic_at_storydex_root", None)
                if callable(writer):
                    await asyncio.to_thread(
                        writer,
                        self.workspace_root / ".storydex",
                        record,
                        self.session_id,
                    )
                else:
                    await asyncio.to_thread(service.upsert_record, record, self.session_id)
            persisted = True
            self._record_timing("persist_trace", step_started)

            self._delete_intent()
            with self._state_lock:
                self.state = status
            return ExecutionFinalizationResult(
                status=status,
                error_message=error_message,
                git_payload=dict(git_payload),
                payload_data=payload_data,
                timings_ms=dict(self._timings_ms),
            )
        except Exception:
            with self._state_lock:
                self.state = "failed"
            if not persisted:
                try:
                    self._write_intent(
                        state="finalization_failed",
                        extra={"outcome": status, "finalizationFailedAt": _now_iso()},
                    )
                except Exception:
                    pass
            raise
        finally:
            self._record_timing("finalize", finalize_started)
            timing_payload = {
                "traceId": self.trace_id,
                "sessionId": self.session_id,
                "timingsMs": dict(self._timings_ms),
                "aggregate": self.coordinator.timing_report(),
            }
            if context.write_timing is not None:
                try:
                    await _invoke(lambda: context.write_timing(timing_payload))
                except Exception as exc:
                    _LOGGER.warning("Unable to write coordinator timing for %s: %s", self.trace_id, exc)
            _LOGGER.info("ExecutionCoordinator timing %s", json.dumps(timing_payload, ensure_ascii=False))
            self._release()

    def _resolve_status(self, observation: ExecutionObservation) -> str:
        if observation.cancelled or self.is_cancelled:
            return "cancelled"
        if observation.error_message or not observation.completed:
            return "failed"
        return "completed"

    def _write_intent(self, *, state: str, extra: Dict[str, Any] | None = None) -> None:
        existing = _read_json(self.intent_path)
        payload = {
            **existing,
            "_type": "ExecutionFinalizationIntent",
            "_version": 1,
            "traceId": self.trace_id,
            "sessionId": self.session_id,
            "workspaceRoot": self.workspace_root.as_posix(),
            "state": state,
            "createdAt": str(existing.get("createdAt") or self._created_at),
            "updatedAt": _now_iso(),
            "snapshotAvailable": self.snapshot_available,
            "noRestorePoint": self.no_restore_point,
        }
        payload.update(extra or {})
        _atomic_write_json(self.intent_path, payload)

    def _delete_intent(self) -> None:
        try:
            self.intent_path.unlink()
        except FileNotFoundError:
            pass

    def _record_timing(self, name: str, started: float) -> None:
        elapsed_ms = round(max(0.0, (time.perf_counter() - started) * 1000), 4)
        self._timings_ms[name] = elapsed_ms
        self.coordinator._record_sample(name, elapsed_ms)

    def _release(self) -> None:
        with self._state_lock:
            if self._released:
                return
            self._released = True
        self.coordinator._release(self)


async def _invoke(callback: Callable[[], Any], *, in_thread: bool = False) -> Any:
    value = await asyncio.to_thread(callback) if in_thread else callback()
    if inspect.isawaitable(value):
        return await value
    return value


def _intent_root(workspace_root: Path) -> Path:
    return Path(workspace_root).resolve() / ".storydex" / ".agent" / "execution-intents"


def _intent_path(workspace_root: Path, trace_id: str) -> Path:
    normalized = str(trace_id or "").strip()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return _intent_root(workspace_root) / f"execution-{digest}.json"


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_EXECUTION_COORDINATOR = ExecutionCoordinator()


def get_execution_coordinator() -> ExecutionCoordinator:
    return _EXECUTION_COORDINATOR
