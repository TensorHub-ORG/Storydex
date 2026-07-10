from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.feature_flags as feature_flags
import services.job_queue as job_queue
from services.trace_history_service import TraceHistoryService


pytestmark = pytest.mark.integration


@pytest.fixture
def service(tmp_path: Path, monkeypatch) -> TraceHistoryService:
    storydex_root = tmp_path / "story" / ".storydex"
    storydex_root.mkdir(parents=True)
    instance = TraceHistoryService()
    instance.project_service = SimpleNamespace(storydex_root=storydex_root)
    monkeypatch.setattr(feature_flags, "get_flags", lambda: SimpleNamespace(get_bool=lambda name: False))
    return instance


def record(trace_id: str, prompt: str, timestamp: str, **extra):
    return {
        "traceId": trace_id,
        "prompt": prompt,
        "createdAt": timestamp,
        "updatedAt": timestamp,
        **extra,
    }


def test_upsert_list_read_merge_clear_marker_and_delete(service: TraceHistoryService):
    first = record("trace-1", "first prompt", "2026-01-01T00:00:00Z", reply="one")
    second = record("trace-2", "second prompt", "2026-01-02T00:00:00Z", reply="two")
    assert service.upsert_record({}, "session-a") == {}
    assert service.upsert_record(first, "session-a")["sessionId"] == "session-a"
    service.upsert_record(second, "session-a")
    merged = service.upsert_record({"traceId": "trace-1", "reply": "updated"}, "session-a")
    assert merged["prompt"] == "first prompt" and merged["reply"] == "updated"
    assert service.read_record("", "session-a") is None
    assert service.read_record("missing", "session-a") is None
    assert service.read_record("trace-1", "session-a")["reply"] == "updated"
    assert [item["traceId"] for item in service.list_records("session-a", limit=1)] == ["trace-1"]
    summaries = service.list_session_summaries()
    assert summaries[0]["sessionId"] == "session-a"
    assert summaries[0]["firstPrompt"] == "first prompt"
    assert summaries[0]["traceCount"] == 2
    assert service.list_sessions() == ["session-a"]

    assert service.was_session_cleared_after("session-a", "") is False
    assert service.was_session_cleared_after("session-a", "2025-01-01T00:00:00Z") is False
    assert service.clear_records("session-a") == 2
    marker = service.mark_session_cleared("session-a")
    assert service.was_session_cleared_after("session-a", "2025-01-01T00:00:00Z") is True
    assert service.list_session_summaries()[0]["traceCount"] == 0
    assert marker["clearedAt"]
    deleted = service.delete_session("session-a")
    assert deleted["deleted"] is True and deleted["removedCount"] >= 1
    assert service.list_sessions() == []


@pytest.mark.security
@pytest.mark.parametrize("session_id", ["../escape", "..\\escape", "/absolute", "C:\\absolute", ".", ".."])
def test_session_directory_is_safe_for_hostile_ids(service: TraceHistoryService, session_id: str):
    root = service.get_session_root(session_id)
    base = service.project_service.storydex_root / ".agent" / "sessions"
    assert root.resolve().is_relative_to(base.resolve())
    assert root.name.startswith("_session_")
    assert (root / "README.md").exists()
    other = service.get_session_root_for_storydex_root(service.project_service.storydex_root, session_id)
    assert other == root


def test_legacy_trace_migration_corruption_and_newer_record_wins(service: TraceHistoryService):
    trace_root = service.project_service.storydex_root / "traces" / "20260101"
    trace_root.mkdir(parents=True)
    old = record("legacy", "legacy prompt", "2026-01-01T00:00:00Z", sessionId="legacy-session")
    (trace_root / "legacy.json").write_text(json.dumps(old), encoding="utf-8")
    (trace_root / "broken.json").write_text("{broken", encoding="utf-8")
    summaries = service.list_session_summaries()
    assert summaries[0]["sessionId"] == "legacy-session"
    assert service.read_record("legacy", "legacy-session")["prompt"] == "legacy prompt"
    assert not (service.project_service.storydex_root / "traces").exists()

    previous = {"updatedAt": "2026-01-02T00:00:00Z", "prompt": "new"}
    older = {"updatedAt": "2026-01-01T00:00:00Z", "prompt": "old"}
    same_missing = {"updatedAt": "2026-01-02T00:00:00Z", "prompt": ""}
    assert service._should_replace_record(previous, older) is False
    assert service._should_replace_record(older, previous) is True
    assert service._should_replace_record(same_missing, previous) is True
    assert service._timestamp_value("broken") == 0
    assert service._read_json(None) == {}


def test_async_upsert_registers_once_and_persists_to_captured_project(service: TraceHistoryService, monkeypatch):
    class Queue:
        def __init__(self):
            self.handlers = {}
            self.jobs = []

        def register_handler(self, kind, handler):
            self.handlers[kind] = handler

        def enqueue(self, **job):
            self.jobs.append(job)

    queue = Queue()
    monkeypatch.setattr(feature_flags, "get_flags", lambda: SimpleNamespace(get_bool=lambda name: True))
    monkeypatch.setattr(job_queue, "get_default_queue", lambda: queue)
    payload = service.upsert_record(record("async-1", "queued", "2026-02-01T00:00:00Z"), "async-session")
    service.upsert_record(record("async-2", "queued twice", "2026-02-02T00:00:00Z"), "async-session")
    assert payload["sessionId"] == "async-session"
    assert len(queue.handlers) == 1 and len(queue.jobs) == 2
    queue.handlers["trace_upsert"](queue.jobs[0]["payload"])
    assert service.read_record("async-1", "async-session")["prompt"] == "queued"
    service._async_trace_handler({})


def test_private_helpers_handle_invalid_timestamps_and_paths(service: TraceHistoryService):
    assert service._normalize_session_id("  ") == "default"
    assert service._session_id_needs_safe_directory("safe-session") is False
    assert service._safe_session_directory_name("x") == service._safe_session_directory_name("x")
    assert service._safe_timestamp("broken")
    assert service._record_session_id({"session_id": "s"}) == "s"
    assert service._record_belongs_to_session({"sessionId": "s"}, "s") is True
    summary = service._build_session_summary("empty", [])
    assert summary["traceCount"] == 0 and summary["firstPrompt"] == ""


def test_portable_branch_matrix_for_async_paths_names_and_summaries(service: TraceHistoryService, monkeypatch):
    sync_calls = []
    rooted_calls = []
    monkeypatch.setattr(service, "_upsert_record_sync", lambda **kwargs: sync_calls.append(kwargs))
    monkeypatch.setattr(service, "_upsert_record_sync_at_storydex_root", lambda **kwargs: rooted_calls.append(kwargs))
    service._async_trace_handler({"trace_id": "plain", "record": {"traceId": "plain"}})
    service._async_trace_handler({"trace_id": "rooted", "storydex_root": str(service.project_service.storydex_root)})
    assert sync_calls[0]["trace_id"] == "plain"
    assert rooted_calls[0]["trace_id"] == "rooted"

    base = service.project_service.storydex_root / ".agent" / "sessions"
    monkeypatch.setattr(TraceHistoryService, "_session_id_needs_safe_directory", staticmethod(lambda _value: False))
    escaped = service._resolve_session_root(base, "../forced-escape")
    assert escaped.name.startswith("_session_")

    base.mkdir(parents=True, exist_ok=True)
    (base / "not-a-session.txt").write_text("x", encoding="utf-8")
    (base / "real-session").mkdir()
    assert "real-session" in service._collect_session_names()

    summary = service._build_session_summary(
        "mixed",
        [
            record("blank", "", "2026-01-01T00:00:00Z"),
            record("named", "first nonblank", "2026-01-02T00:00:00Z"),
        ],
    )
    assert summary["firstPrompt"] == "first nonblank"


def test_legacy_migration_branch_matrix_for_directories_older_records_and_cleanup_errors(
    service: TraceHistoryService, monkeypatch
):
    legacy_root = service.project_service.storydex_root / "traces"
    (legacy_root / "directory.json").mkdir(parents=True)
    source = legacy_root / "older.json"
    payload = record("older", "old", "2026-01-01T00:00:00Z", updatedAt="2026-01-01T00:00:00Z")
    source.write_text(json.dumps(payload), encoding="utf-8")
    target = service._build_trace_path_at_storydex_root(
        storydex_root=service.project_service.storydex_root,
        trace_id="older",
        created_at=payload["createdAt"],
        session_id="default",
    )
    target.write_text(
        json.dumps(record("older", "new", "2026-01-02T00:00:00Z", updatedAt="2026-01-02T00:00:00Z")),
        encoding="utf-8",
    )

    original_unlink = Path.unlink
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda path, *args, **kwargs: (_ for _ in ()).throw(OSError("locked"))
        if path == source
        else original_unlink(path, *args, **kwargs),
    )
    monkeypatch.setattr("services.trace_history_service.shutil.rmtree", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("busy")))
    service._migrate_trace_root_locked(legacy_root, service.project_service.storydex_root)
    assert json.loads(target.read_text(encoding="utf-8"))["prompt"] == "new"


def test_clear_records_skips_metadata_and_tolerates_locked_files(service: TraceHistoryService, monkeypatch):
    primary = service.get_session_root("locked")
    legacy = service.project_service.storydex_root / "sessions" / "locked"
    legacy.mkdir(parents=True)
    locked_files = {primary / "trace.json", legacy / "trace.json"}
    for root in (primary, legacy):
        (root / "log.json").write_text("{}", encoding="utf-8")
        (root / "trace.json").write_text("{}", encoding="utf-8")
    original_unlink = Path.unlink
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda path, *args, **kwargs: (_ for _ in ()).throw(OSError("locked"))
        if path in locked_files
        else original_unlink(path, *args, **kwargs),
    )
    assert service.clear_records("locked") == 0
