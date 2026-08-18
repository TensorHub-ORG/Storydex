from __future__ import annotations

import asyncio
import json
import logging
import types
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from services.execution_coordinator import (
    ExecutionBusyError,
    ExecutionCoordinator,
    ExecutionFinalizationContext,
    ExecutionObservation,
    ExecutionStateError,
    SnapshotConfirmationRequired,
)
from services import execution_coordinator as execution_coordinator_module


class _TraceHistory:
    def __init__(self) -> None:
        self.records: list[tuple[object, dict, str]] = []

    def upsert_record_atomic_at_storydex_root(self, root, record, session_id):
        self.records.append((root, dict(record), session_id))
        return record


def _snapshot(*, available: bool, reason: str = ""):
    return types.SimpleNamespace(available=available, reason=reason)


def _finalization(order: list[str], *, pause: asyncio.Event | None = None) -> ExecutionFinalizationContext:
    async def finish_git():
        order.append("git")
        if pause is not None:
            await pause.wait()
        return {"_type": "GitAutoCommit", "status": "success", "created": False}

    def on_git(_payload):
        order.append("git_event")

    def on_terminal(_status, _message):
        order.append("terminal")

    def build(status, error_message, no_restore_point, timings):
        order.append("payload")
        return {
            "record": {
                "traceId": "trace-1",
                "status": status,
                "errorMessage": error_message,
                "noRestorePoint": no_restore_point,
                "timings": timings,
            }
        }

    def persist(record):
        order.append("trace")
        assert record["traceId"] == "trace-1"

    return ExecutionFinalizationContext(
        finish_git=finish_git,
        on_git_payload=on_git,
        on_terminal=on_terminal,
        build_payload=build,
        persist_trace=persist,
    )


def test_finalize_is_unique_and_uses_fixed_order(tmp_path):
    coordinator = ExecutionCoordinator(trace_history_service=_TraceHistory())
    handle = coordinator.begin(tmp_path, "session-1", "trace-1")
    handle.register_snapshot(_snapshot(available=True))
    order: list[str] = []

    async def run():
        result = await handle.finalize(
            ExecutionObservation(completed=True),
            _finalization(order),
        )
        with pytest.raises(ExecutionStateError):
            await handle.finalize(
                ExecutionObservation(completed=True),
                _finalization(order),
            )
        return result

    result = asyncio.run(run())
    assert result.status == "completed"
    assert order == ["git", "git_event", "terminal", "payload", "trace"]
    assert not handle.intent_path.exists()
    assert coordinator.timing_report()["finalize"]["count"] == 1


def test_cancel_is_idempotent_and_forces_cancelled_terminal(tmp_path):
    coordinator = ExecutionCoordinator(trace_history_service=_TraceHistory())
    handle = coordinator.begin(tmp_path, "session-1", "trace-1")
    handle.register_snapshot(_snapshot(available=True))
    reasons: list[str] = []
    handle.bind_cancellation(reasons.append)

    assert handle.cancel("client_disconnected") is True
    assert handle.cancel("again") is False
    payload = json.loads(handle.intent_path.read_text(encoding="utf-8"))
    assert payload["state"] == "cancelling"

    result = asyncio.run(
        handle.finalize(
            ExecutionObservation(completed=True),
            _finalization([]),
        )
    )
    assert result.status == "cancelled"
    assert reasons == ["client_disconnected"]


def test_cancel_and_snapshot_intent_writes_are_serialized(monkeypatch, tmp_path):
    coordinator = ExecutionCoordinator(trace_history_service=_TraceHistory())
    handle = coordinator.begin(tmp_path, "session-1", "trace-concurrent-intent")
    initial = json.loads(handle.intent_path.read_text(encoding="utf-8"))
    snapshot_write_started = Event()
    release_snapshot_write = Event()
    cancel_attempted = Event()
    original_atomic_write = execution_coordinator_module._atomic_write_json

    def block_snapshot_write(path, payload):
        if payload.get("snapshot") and not snapshot_write_started.is_set():
            snapshot_write_started.set()
            assert release_snapshot_write.wait(timeout=2)
        original_atomic_write(path, payload)

    def cancel():
        cancel_attempted.set()
        return handle.cancel("manual_stop")

    monkeypatch.setattr(execution_coordinator_module, "_atomic_write_json", block_snapshot_write)

    with ThreadPoolExecutor(max_workers=2) as executor:
        snapshot_future = executor.submit(handle.register_snapshot, _snapshot(available=True))
        assert snapshot_write_started.wait(timeout=2)
        cancel_future = executor.submit(cancel)
        assert cancel_attempted.wait(timeout=2)
        assert not cancel_future.done()
        release_snapshot_write.set()
        snapshot_future.result(timeout=2)
        assert cancel_future.result(timeout=2) is True

    payload = json.loads(handle.intent_path.read_text(encoding="utf-8"))
    assert payload["state"] == "cancelling"
    assert payload["cancelReason"] == "manual_stop"
    assert payload["snapshotAvailable"] is True
    assert payload["snapshot"]["available"] is True
    assert payload["createdAt"] == initial["createdAt"]


def test_snapshot_cannot_restore_stale_running_intent_after_cancel(monkeypatch, tmp_path):
    coordinator = ExecutionCoordinator(trace_history_service=_TraceHistory())
    handle = coordinator.begin(tmp_path, "session-1", "trace-stale-snapshot-state")
    snapshot_write_called = Event()
    cancel_attempted = Event()
    cancel_finished = Event()
    original_write_intent = handle._write_intent

    def delay_snapshot_before_intent_lock(*, state, extra=None):
        if extra and extra.get("snapshot"):
            snapshot_write_called.set()
            assert cancel_attempted.wait(timeout=2)
            cancel_finished.wait(timeout=0.5)
        return original_write_intent(state=state, extra=extra)

    def cancel():
        cancel_attempted.set()
        try:
            return handle.cancel("manual_stop")
        finally:
            cancel_finished.set()

    monkeypatch.setattr(handle, "_write_intent", delay_snapshot_before_intent_lock)

    with ThreadPoolExecutor(max_workers=2) as executor:
        snapshot_future = executor.submit(handle.register_snapshot, _snapshot(available=True))
        assert snapshot_write_called.wait(timeout=2)
        cancel_future = executor.submit(cancel)
        snapshot_future.result(timeout=2)
        assert cancel_future.result(timeout=2) is True

    payload = json.loads(handle.intent_path.read_text(encoding="utf-8"))
    assert payload["state"] == "cancelling"
    assert payload["cancelReason"] == "manual_stop"
    assert payload["snapshotAvailable"] is True


def test_cancel_is_rejected_after_finalization_deletes_intent(monkeypatch, tmp_path):
    coordinator = ExecutionCoordinator(trace_history_service=_TraceHistory())
    handle = coordinator.begin(tmp_path, "session-1", "trace-late-cancel")
    handle.register_snapshot(_snapshot(available=True))
    reasons: list[str] = []
    handle.bind_cancellation(reasons.append)
    intent_deleted = Event()
    release_finalizer = Event()
    original_delete_intent = handle._delete_intent

    def pause_after_delete():
        original_delete_intent()
        intent_deleted.set()
        assert release_finalizer.wait(timeout=2)

    monkeypatch.setattr(handle, "_delete_intent", pause_after_delete)

    with ThreadPoolExecutor(max_workers=2) as executor:
        finalize_future = executor.submit(
            lambda: asyncio.run(
                handle.finalize(ExecutionObservation(completed=True), _finalization([]))
            )
        )
        assert intent_deleted.wait(timeout=2)
        cancel_future = executor.submit(handle.cancel, "late-stop")
        release_finalizer.set()
        result = finalize_future.result(timeout=2)
        accepted = cancel_future.result(timeout=2)

    assert accepted is False
    assert reasons == []
    assert result.status == "completed"
    assert handle.state == "completed"
    assert not handle.intent_path.exists()


def test_atomic_intent_write_retries_transient_windows_file_sharing_error(monkeypatch, tmp_path):
    path = tmp_path / "execution-intent.json"
    original_replace = execution_coordinator_module.os.replace
    calls = []

    def replace_once_locked(source, target):
        calls.append((source, target))
        if len(calls) == 1:
            raise PermissionError("temporary file sharing violation")
        return original_replace(source, target)

    monkeypatch.setattr(execution_coordinator_module.os, "replace", replace_once_locked)
    monkeypatch.setattr(execution_coordinator_module.time, "sleep", lambda _seconds: None)

    execution_coordinator_module._atomic_write_json(path, {"state": "cancelling"})

    assert len(calls) == 2
    assert json.loads(path.read_text(encoding="utf-8")) == {"state": "cancelling"}


def test_atomic_intent_write_raises_persistent_error_and_cleans_temporary(monkeypatch, tmp_path):
    path = tmp_path / "execution-intent.json"
    path.write_text('{"state":"running"}\n', encoding="utf-8")
    original = path.read_bytes()
    temporary_paths = []

    def always_locked(source, _target):
        temporary_paths.append(source)
        raise PermissionError("persistent file sharing violation")

    monkeypatch.setattr(execution_coordinator_module.os, "replace", always_locked)
    monkeypatch.setattr(execution_coordinator_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(PermissionError, match="persistent file sharing violation"):
        execution_coordinator_module._atomic_write_json(path, {"state": "cancelling"})

    assert len(temporary_paths) == len(execution_coordinator_module._ATOMIC_REPLACE_RETRY_DELAYS) + 1
    assert not temporary_paths[0].exists()
    assert path.read_bytes() == original


def test_active_intent_read_error_does_not_replace_existing_file(monkeypatch, tmp_path):
    coordinator = ExecutionCoordinator(trace_history_service=_TraceHistory())
    handle = coordinator.begin(tmp_path, "session-1", "trace-read-error")
    handle.register_snapshot(_snapshot(available=True))
    reasons: list[str] = []
    handle.bind_cancellation(reasons.append)
    original = handle.intent_path.read_bytes()
    path_type = type(handle.intent_path)
    original_read_text = path_type.read_text

    def deny_intent_read(path, *args, **kwargs):
        if path == handle.intent_path:
            raise PermissionError("intent is temporarily locked")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "read_text", deny_intent_read)

    with pytest.raises(PermissionError, match="temporarily locked"):
        handle.cancel("manual_stop")

    assert handle.intent_path.read_bytes() == original
    assert handle.state == "cancelling"
    assert reasons == ["manual_stop"]


@pytest.mark.parametrize(
    ("malformed", "message"),
    [(b"{broken\n", "invalid JSON"), (b"[]\n", "JSON object")],
)
def test_malformed_active_intent_is_not_rebuilt(malformed, message, tmp_path):
    coordinator = ExecutionCoordinator(trace_history_service=_TraceHistory())
    handle = coordinator.begin(tmp_path, "session-1", "trace-malformed-intent")
    handle.intent_path.write_bytes(malformed)

    with pytest.raises(ExecutionStateError, match=message):
        handle.register_snapshot(_snapshot(available=True))

    assert handle.intent_path.read_bytes() == malformed


def test_closed_intent_rejects_late_write(tmp_path):
    coordinator = ExecutionCoordinator(trace_history_service=_TraceHistory())
    handle = coordinator.begin(tmp_path, "session-1", "trace-closed-intent")
    handle.reject_preflight("test")

    with pytest.raises(ExecutionStateError, match="already closed"):
        handle._write_intent(state="running")


def test_finalization_failure_intent_write_error_is_logged(monkeypatch, caplog, tmp_path):
    coordinator = ExecutionCoordinator(trace_history_service=_TraceHistory())
    handle = coordinator.begin(tmp_path, "session-1", "trace-finalization-log")
    original_write_intent = handle._write_intent

    def fail_failure_intent(*, state, extra=None):
        if state == "finalization_failed":
            raise PermissionError("failure intent is locked")
        return original_write_intent(state=state, extra=extra)

    def fail_payload(_status, _error_message, _no_restore_point, _timings):
        raise ValueError("payload failed")

    context = ExecutionFinalizationContext(
        finish_git=lambda: {"_type": "GitAutoCommit", "status": "success", "created": False},
        build_payload=fail_payload,
    )
    monkeypatch.setattr(handle, "_write_intent", fail_failure_intent)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(ValueError, match="payload failed"):
            asyncio.run(handle.finalize(ExecutionObservation(completed=True), context))

    records = [
        record
        for record in caplog.records
        if "Unable to persist finalization failure intent" in record.getMessage()
    ]
    assert len(records) == 1
    assert records[0].exc_info is not None


def test_busy_releases_after_finalization(tmp_path):
    coordinator = ExecutionCoordinator(trace_history_service=_TraceHistory())
    first = coordinator.begin(tmp_path / "one", "session-1", "trace-1")
    with pytest.raises(ExecutionBusyError):
        coordinator.begin(tmp_path / "two", "session-2", "trace-2")
    first.register_snapshot(_snapshot(available=True))
    asyncio.run(first.finalize(ExecutionObservation(completed=True), _finalization([])))

    second = coordinator.begin(tmp_path / "two", "session-2", "trace-2")
    second.reject_preflight("test")


def test_snapshot_failure_requires_confirmation_and_is_recorded(tmp_path):
    coordinator = ExecutionCoordinator(trace_history_service=_TraceHistory())
    handle = coordinator.begin(tmp_path, "session-1", "trace-1")
    unavailable = _snapshot(available=False, reason="git failed")
    with pytest.raises(SnapshotConfirmationRequired) as raised:
        handle.register_snapshot(unavailable)
    assert raised.value.code == "SNAPSHOT_FAILED"

    handle.register_snapshot(unavailable, confirm_no_snapshot=True)
    assert handle.no_restore_point is True
    payload = json.loads(handle.intent_path.read_text(encoding="utf-8"))
    assert payload["noRestorePoint"] is True
    result = asyncio.run(
        handle.finalize(ExecutionObservation(completed=True), _finalization([]))
    )
    assert result.payload_data["record"]["noRestorePoint"] is True


def test_startup_reconciliation_marks_unfinished_without_replay(tmp_path):
    trace_history = _TraceHistory()
    coordinator = ExecutionCoordinator(trace_history_service=trace_history)
    intent_root = tmp_path / ".storydex" / ".agent" / "execution-intents"
    intent_root.mkdir(parents=True)
    intent_path = intent_root / "execution-stale.json"
    intent_path.write_text(
        json.dumps(
            {
                "_type": "ExecutionFinalizationIntent",
                "traceId": "trace-stale",
                "sessionId": "session-stale",
                "workspaceRoot": tmp_path.as_posix(),
                "state": "finalizing",
                "createdAt": "2026-07-20T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    reconciled = coordinator.reconcile_workspace(tmp_path)
    assert len(reconciled) == 1
    assert reconciled[0]["status"] == "unfinished"
    assert reconciled[0]["execution"]["replayed"] is False
    assert trace_history.records[0][1]["traceId"] == "trace-stale"
    assert json.loads(intent_path.read_text(encoding="utf-8"))["state"] == "unfinished"


def test_shielded_finalization_survives_waiter_cancellation(tmp_path):
    coordinator = ExecutionCoordinator(trace_history_service=_TraceHistory())
    handle = coordinator.begin(tmp_path, "session-1", "trace-1")
    handle.register_snapshot(_snapshot(available=True))

    async def run():
        pause = asyncio.Event()
        order: list[str] = []
        waiter = asyncio.create_task(
            handle.finalize(
                ExecutionObservation(completed=True),
                _finalization(order, pause=pause),
            )
        )
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        pause.set()
        result = await handle.wait_finalized()
        return result, order

    result, order = asyncio.run(run())
    assert result is not None and result.status == "completed"
    assert order[-1] == "trace"
    assert not handle.intent_path.exists()


def test_abandoned_worker_releases_slot_and_preserves_reconcilable_intent(tmp_path):
    trace_history = _TraceHistory()
    coordinator = ExecutionCoordinator(trace_history_service=trace_history)
    handle = coordinator.begin(tmp_path, "session-1", "trace-abandoned")
    handle.cancel("worker_shutdown")

    assert handle.abandon("worker_cancelled") is True
    assert handle.abandon("again") is False
    assert handle.cancel("late_cancel") is False
    handle.reject_preflight("late_reject")
    with pytest.raises(ExecutionStateError):
        asyncio.run(handle.finalize(ExecutionObservation(completed=True), _finalization([])))

    intent = json.loads(handle.intent_path.read_text(encoding="utf-8"))
    assert intent["state"] == "finalization_failed"
    assert intent["abandonReason"] == "worker_cancelled"

    next_handle = coordinator.begin(tmp_path, "session-2", "trace-next")
    assert trace_history.records[0][1]["traceId"] == "trace-abandoned"
    assert trace_history.records[0][1]["status"] == "unfinished"
    next_handle.reject_preflight("test_complete")
