from __future__ import annotations

import asyncio
import json
import types

import pytest

from services.execution_coordinator import (
    ExecutionBusyError,
    ExecutionCoordinator,
    ExecutionFinalizationContext,
    ExecutionObservation,
    ExecutionStateError,
    SnapshotConfirmationRequired,
)


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
