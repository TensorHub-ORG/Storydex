from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from api import routes_agent as routes
from core.exceptions import StorydexError
from services import coomi_agent_service as coomi
from services.execution_coordinator import ExecutionCoordinator
from services.trace_history_service import TraceHistoryService


def _trace_record(trace_id: str, prompt: str, timestamp: str) -> dict:
    return {
        "traceId": trace_id,
        "sessionId": "session-a",
        "prompt": prompt,
        "reply": f"reply:{prompt}",
        "status": "completed",
        "createdAt": timestamp,
        "updatedAt": timestamp,
    }


def test_rollback_last_turn_atomically_truncates_real_coomi_jsonl(monkeypatch, tmp_path: Path):
    from coomi.engine.session import SessionManager, add_assistant_message, add_tool_result, add_user_message
    from coomi.services.session_history import append_session_state, load_session_from_jsonl

    sessions_root = tmp_path / "coomi-sessions"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(exist_ok=True)
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_SESSIONS", sessions_root)

    session = SessionManager(history_dir=sessions_root).create_session(cwd=str(workspace_root), model="fake")
    add_user_message(session, "first prompt")
    add_assistant_message(session, "first reply")
    add_tool_result(session, "tool-1", "first tool result")
    session.active_skills = ["story"]
    append_session_state(session)
    add_user_message(session, "second prompt")
    add_assistant_message(session, "second reply")
    add_tool_result(session, "tool-2", "second tool result")
    coomi._write_coomi_session_binding(
        workspace_root=workspace_root,
        storydex_session_id="session-a",
        session=session,
    )

    service = coomi.StorydexCoomiAgentService()
    runtime_key = service._runtime_key(session_id="session-a", workspace_root=workspace_root)
    service._sessions[runtime_key] = session
    service._agents[runtime_key] = object()
    service._permissions[runtime_key] = object()

    result = service.rollback_last_turn("session-a", workspace_root=workspace_root)

    assert result["rolledBack"] is True
    entries = [
        json.loads(line)
        for line in Path(session.history_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert entries[0]["type"] == "session"
    assert [
        entry["message"]["content"]
        for entry in entries
        if entry.get("type") == "message"
    ] == ["first prompt", "first reply", "first tool result"]
    assert entries[-1]["type"] == "state"

    restored = load_session_from_jsonl(session.history_path)
    assert [(message.role, message.content) for message in restored.messages] == [
        ("user", "first prompt"),
        ("assistant", "first reply"),
        ("tool", "first tool result"),
    ]
    assert restored.active_skills == ["story"]
    assert runtime_key not in service._sessions
    assert runtime_key not in service._agents
    assert runtime_key not in service._permissions
    assert coomi._read_coomi_session_binding(
        workspace_root=workspace_root,
        storydex_session_id="session-a",
    )


def test_rollback_last_turn_is_idempotent_without_user_message_or_binding(monkeypatch, tmp_path: Path):
    from coomi.engine.session import SessionManager

    sessions_root = tmp_path / "coomi-sessions"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(exist_ok=True)
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_SESSIONS", sessions_root)

    session = SessionManager(history_dir=sessions_root).create_session(cwd=str(workspace_root), model="fake")
    coomi._write_coomi_session_binding(
        workspace_root=workspace_root,
        storydex_session_id="session-a",
        session=session,
    )
    original = Path(session.history_path).read_bytes()
    service = coomi.StorydexCoomiAgentService()

    assert service.rollback_last_turn("session-a", workspace_root=workspace_root)["rolledBack"] is False
    assert Path(session.history_path).read_bytes() == original
    assert service.rollback_last_turn("missing", workspace_root=workspace_root)["rolledBack"] is False


def test_delete_record_removes_only_the_target_trace_and_is_idempotent(tmp_path: Path):
    service = TraceHistoryService()
    service.project_service = types.SimpleNamespace(storydex_root=tmp_path / ".storydex")
    service.upsert_record(_trace_record("trace-1", "first", "2026-07-21T10:00:00Z"), "session-a")
    service.upsert_record(_trace_record("trace-2", "second", "2026-07-21T11:00:00Z"), "session-a")

    deleted = service.delete_record("trace-2", "session-a")

    assert deleted == {
        "deleted": True,
        "traceId": "trace-2",
        "sessionId": "session-a",
    }
    assert service.read_record("trace-2", "session-a") is None
    assert service.read_record("trace-1", "session-a")["prompt"] == "first"
    assert service.delete_record("trace-2", "session-a")["deleted"] is False


def test_rollback_latest_execution_removes_trace_and_clears_intent_cache(monkeypatch, tmp_path: Path):
    calls: list[tuple] = []
    coordinator = ExecutionCoordinator()
    history = types.SimpleNamespace(
        list_records=lambda **kwargs: [
            _trace_record("trace-2", "second prompt", "2026-07-21T11:00:00Z")
        ],
        delete_record=lambda trace_id, session_id: calls.append(("delete", trace_id, session_id))
        or {"deleted": True, "traceId": trace_id, "sessionId": session_id},
    )
    runtime = types.SimpleNamespace(
        rollback_last_turn=lambda session_id, *, workspace_root: calls.append(
            ("rollback", session_id, workspace_root)
        )
        or {"rolledBack": True, "sessionId": session_id}
    )
    intent = types.SimpleNamespace(
        clear_session=lambda **kwargs: calls.append(("intent", kwargs))
    )
    monkeypatch.setattr(routes, "execution_coordinator", coordinator)
    monkeypatch.setattr(routes, "trace_history_service", history)
    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: runtime)
    monkeypatch.setattr(routes, "storydex_intent_service", intent)
    monkeypatch.setattr(routes, "project_service", types.SimpleNamespace(workspace_root=tmp_path))

    response = routes.agent_rollback_latest_execution(
        routes.AgentExecutionRollbackRequest(sessionId="session-a"),
        object(),
    )

    assert response.data == {
        "rolledBack": True,
        "sessionId": "session-a",
        "removedTraceId": "trace-2",
        "prompt": "second prompt",
    }
    assert calls == [
        ("rollback", "session-a", tmp_path),
        ("delete", "trace-2", "session-a"),
        ("intent", {"session_id": "session-a", "workspace_root": tmp_path}),
    ]
    assert response.audit[0]["action"] == "rollback_latest_execution"
    assert coordinator.try_reserve() is True
    coordinator.release_reservation()


def test_rollback_latest_execution_handles_empty_session_and_busy_slot(monkeypatch, tmp_path: Path):
    coordinator = ExecutionCoordinator()
    history = types.SimpleNamespace(list_records=lambda **kwargs: [])
    monkeypatch.setattr(routes, "execution_coordinator", coordinator)
    monkeypatch.setattr(routes, "trace_history_service", history)
    monkeypatch.setattr(routes, "project_service", types.SimpleNamespace(workspace_root=tmp_path))

    response = routes.agent_rollback_latest_execution(
        routes.AgentExecutionRollbackRequest(sessionId="empty"),
        object(),
    )
    assert response.data == {
        "rolledBack": False,
        "sessionId": "empty",
        "removedTraceId": "",
        "prompt": "",
    }
    assert coordinator.try_reserve() is True
    with pytest.raises(StorydexError) as busy:
        routes.agent_rollback_latest_execution(
            routes.AgentExecutionRollbackRequest(sessionId="empty"),
            object(),
        )
    assert busy.value.code == "agent_busy"
    assert busy.value.status_code == 409
    coordinator.release_reservation()
