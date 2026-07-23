from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from api import routes_agent as routes
from services.agent_git_autocommit_service import AgentGitSnapshot
from services.execution_coordinator import ExecutionCoordinator
from services.followup_mailbox_service import FollowupMailboxService


class _ConnectedRequest:
    headers: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        return False


class _DisconnectAfterFirstChunkRequest:
    headers: dict[str, str] = {}

    def __init__(self) -> None:
        self.calls = 0

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self.calls >= 2


def _decode(chunk: str) -> tuple[str, dict[str, Any]]:
    event = ""
    data: dict[str, Any] = {}
    for line in chunk.splitlines():
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data = json.loads(line.removeprefix("data: "))
    return event, data


def _patch_finalization(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    class Git:
        def finish_turn(self, snapshot: AgentGitSnapshot, **kwargs: Any) -> dict[str, Any]:
            return {"_type": "GitAutoCommit", "status": "info", "created": False}

    def build_payload(**kwargs: Any) -> dict[str, Any]:
        captured["events"] = copy.deepcopy(kwargs["events"])
        return {
            "record": {
                "traceId": kwargs["trace_id"],
                "sessionId": kwargs["session_id"],
                "status": kwargs["status"],
            }
        }

    monkeypatch.setattr(routes, "agent_git_autocommit_service", Git())
    monkeypatch.setattr(routes, "_build_chat_payload", build_payload)
    monkeypatch.setattr(routes, "_persist_execution_trace", lambda root, record, session_id: captured.setdefault("records", []).append(copy.deepcopy(record)))


def test_fifo_followups_are_drained_once_with_one_transport_terminal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mailbox = FollowupMailboxService()
    mailbox.enqueue(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-1",
        content="first follow-up",
    )
    mailbox.enqueue(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-2",
        content="second follow-up",
    )
    prompts: list[tuple[str, str]] = []
    releases: list[bool] = []

    async def fake_turn(**kwargs: Any):
        prompts.append((kwargs["trace_id"], kwargs["payload"].prompt))
        trace_id = kwargs["trace_id"]
        yield routes._encode_sse("RunAccepted", {"_type": "RunAccepted", "traceId": trace_id})
        yield routes._encode_sse("TurnContract", {"_type": "TurnContract", "traceId": trace_id, "status": "ready"})
        yield routes._encode_sse("AgentCompleted", {"_type": "AgentCompleted", "traceId": trace_id})
        yield routes._encode_sse("done", {"type": "done"})

    monkeypatch.setattr(routes, "followup_mailbox_service", mailbox)
    monkeypatch.setattr(routes, "_resolve_agent_workspace_root", lambda payload: tmp_path)
    monkeypatch.setattr(routes, "_stream_agent_chat_request_sse", fake_turn)
    monkeypatch.setattr(routes, "_try_acquire_agent_generation_slot", lambda: True)
    monkeypatch.setattr(routes, "_release_agent_generation_slot", lambda: releases.append(True))

    async def collect() -> list[tuple[str, dict[str, Any]]]:
        return [
            _decode(chunk)
            async for chunk in routes._stream_agent_chat_with_followups_sse(
                payload=routes.AgentChatRequest(prompt="initial", workspaceRoot=str(tmp_path)),
                request=_ConnectedRequest(),
                trace_id="trace-1",
                session_id="session-1",
                cancellation_token=routes._CancellationToken(),
            )
        ]

    packets = asyncio.run(collect())
    assert [prompt for _, prompt in prompts] == ["initial", "first follow-up", "second follow-up"]
    assert len({trace_id for trace_id, _ in prompts}) == 3
    assert [event for event, _ in packets].count("ContinuationStarted") == 2
    assert [event for event, _ in packets].count("done") == 1
    assert releases == [True]
    state = mailbox.list_mailbox(workspace_root=tmp_path, session_id="session-1")
    assert [message["status"] for message in state["messages"]] == ["sent", "sent"]


def test_manual_stop_pauses_fifo_without_losing_pending_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mailbox = FollowupMailboxService()
    mailbox.enqueue(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-1",
        content="keep me",
    )

    async def cancelled_turn(**kwargs: Any):
        yield routes._encode_sse("AgentCancelled", {"_type": "AgentCancelled", "reason": "manual_stop"})
        yield routes._encode_sse("done", {"type": "done"})

    monkeypatch.setattr(routes, "followup_mailbox_service", mailbox)
    monkeypatch.setattr(routes, "_resolve_agent_workspace_root", lambda payload: tmp_path)
    monkeypatch.setattr(routes, "_stream_agent_chat_request_sse", cancelled_turn)

    async def collect() -> list[tuple[str, dict[str, Any]]]:
        return [
            _decode(chunk)
            async for chunk in routes._stream_agent_chat_with_followups_sse(
                payload=routes.AgentChatRequest(prompt="initial", workspaceRoot=str(tmp_path)),
                request=_ConnectedRequest(),
                trace_id="trace-1",
                session_id="session-1",
                cancellation_token=routes._CancellationToken(),
            )
        ]

    packets = asyncio.run(collect())
    assert [event for event, _ in packets] == ["AgentCancelled", "done"]
    state = mailbox.list_mailbox(workspace_root=tmp_path, session_id="session-1")
    assert state["paused"] is True
    assert state["pauseReason"] == "execution_stopped"
    assert state["messages"][0]["status"] == "pending"


def test_client_disconnect_pauses_mailbox_without_losing_pending_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mailbox = FollowupMailboxService()
    mailbox.enqueue(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-1",
        content="send after reconnect",
    )
    coordinator = ExecutionCoordinator()

    async def fake_worker(**kwargs: Any):
        del kwargs
        yield routes._encode_sse("TextChunk", {"_type": "TextChunk", "content": "partial"})

    class Service:
        def cancel_execution(self, **kwargs: Any) -> bool:
            del kwargs
            return True

    monkeypatch.setattr(routes, "followup_mailbox_service", mailbox)
    monkeypatch.setattr(routes, "execution_coordinator", coordinator)
    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: Service())
    monkeypatch.setattr(routes, "_stream_coomi_sse_worker", fake_worker)

    async def collect() -> list[tuple[str, dict[str, Any]]]:
        return [
            _decode(chunk)
            async for chunk in routes._stream_coomi_sse(
                prompt="initial",
                trace_id="trace-1",
                session_id="session-1",
                active_file="",
                workspace_root=tmp_path,
                story_generation={},
                turn_contract={},
                git_snapshot=AgentGitSnapshot(workspace_root=tmp_path, available=False),
                request=_DisconnectAfterFirstChunkRequest(),
                cancellation_token=routes._CancellationToken(),
            )
        ]

    packets = asyncio.run(collect())
    assert [event for event, _ in packets] == ["TextChunk"]
    state = mailbox.list_mailbox(workspace_root=tmp_path, session_id="session-1")
    assert state["paused"] is True
    assert state["pauseReason"] == "client_disconnected"
    assert state["messages"][0]["status"] == "pending"
    assert state["messages"][0]["content"] == "send after reconnect"


def test_reconnected_followup_dispatch_uses_persisted_content_and_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mailbox = FollowupMailboxService()
    mailbox.enqueue(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-1",
        content="authoritative content",
    )
    monkeypatch.setattr(routes, "followup_mailbox_service", mailbox)
    monkeypatch.setattr(routes, "_latest_session_trace_id", lambda session_id: "trace-previous")
    payload = routes.AgentChatRequest(
        prompt="stale browser content",
        workspaceRoot=str(tmp_path),
        sourceFollowupMessageId="message-1",
        sourceFollowupExpectedTraceId="trace-previous",
    )

    claimed_payload, claimed = routes._claim_initial_followup_dispatch(
        payload=payload,
        workspace_root=tmp_path,
        session_id="session-1",
        trace_id="trace-next",
    )
    replay_payload, replay = routes._claim_initial_followup_dispatch(
        payload=payload,
        workspace_root=tmp_path,
        session_id="session-1",
        trace_id="trace-next",
    )

    assert claimed_payload.prompt == "authoritative content"
    assert replay_payload.prompt == "authoritative content"
    assert claimed == replay
    assert claimed is not None and claimed["status"] == "dispatching"
    with pytest.raises(routes.StorydexError) as duplicate:
        routes._claim_initial_followup_dispatch(
            payload=payload,
            workspace_root=tmp_path,
            session_id="session-1",
            trace_id="different-trace",
        )
    assert duplicate.value.code == "invalid_followup_transition"


def test_followup_snapshot_confirmation_returns_to_pending_and_retries_same_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mailbox = FollowupMailboxService()
    mailbox.enqueue(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-1",
        content="retry after snapshot confirmation",
    )
    source = mailbox.claim_queued_by_id(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-1",
        previous_trace_id="trace-previous",
        next_trace_id="trace-first-attempt",
        expected_trace_id="trace-previous",
    )

    async def rejected_turn(**kwargs: Any):
        mailbox.pause(
            workspace_root=tmp_path,
            session_id="session-1",
            reason="snapshot_confirmation",
        )
        yield routes._encode_sse(
            "AgentError",
            {"_type": "AgentError", "error_type": "SNAPSHOT_FAILED", "message": "confirm snapshot risk"},
        )
        yield routes._encode_sse("done", {"type": "done"})

    monkeypatch.setattr(routes, "followup_mailbox_service", mailbox)
    monkeypatch.setattr(routes, "_resolve_agent_workspace_root", lambda payload: tmp_path)
    monkeypatch.setattr(routes, "_stream_agent_chat_request_sse", rejected_turn)

    async def collect() -> list[tuple[str, dict[str, Any]]]:
        return [
            _decode(chunk)
            async for chunk in routes._stream_agent_chat_with_followups_sse(
                payload=routes.AgentChatRequest(prompt=source["content"], workspaceRoot=str(tmp_path)),
                request=_ConnectedRequest(),
                trace_id="trace-first-attempt",
                session_id="session-1",
                cancellation_token=routes._CancellationToken(),
                initial_source_message=source,
            )
        ]

    asyncio.run(collect())
    paused = mailbox.list_mailbox(workspace_root=tmp_path, session_id="session-1")
    assert paused["paused"] is True
    assert paused["pauseReason"] == "snapshot_confirmation"
    assert paused["messages"][0]["status"] == "pending"

    monkeypatch.setattr(routes, "_latest_session_trace_id", lambda session_id: "trace-previous")
    retry_payload, retried = routes._claim_initial_followup_dispatch(
        payload=routes.AgentChatRequest(
            prompt="browser copy",
            workspaceRoot=str(tmp_path),
            confirmNoSnapshot=True,
            sourceFollowupMessageId="message-1",
            sourceFollowupExpectedTraceId="trace-previous",
        ),
        workspace_root=tmp_path,
        session_id="session-1",
        trace_id="trace-second-attempt",
    )
    assert retry_payload.prompt == "retry after snapshot confirmation"
    assert retried is not None and retried["status"] == "dispatching"
    assert mailbox.list_mailbox(workspace_root=tmp_path, session_id="session-1")["paused"] is False


def test_steer_waits_for_tool_checkpoint_and_continues_same_execution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mailbox = FollowupMailboxService()
    mailbox.set_active_trace(workspace_root=tmp_path, session_id="session-1", trace_id="trace-1")
    mailbox.enqueue(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="steer-1",
        content="new guidance",
        mode="steer",
        expected_trace_id="trace-1",
    )
    coordinator = ExecutionCoordinator()
    captured: dict[str, Any] = {}
    replacement_calls: list[str] = []

    class Replacement:
        accepted = False
        restored = False
        expected_trace_id = "old-trace"

        def accept(self) -> None:
            self.accepted = True
            replacement_calls.append("accepted")

        def restore(self, *, reason: str) -> None:
            self.restored = True
            replacement_calls.append(f"restored:{reason}")

    replacement = Replacement()

    class Service:
        def __init__(self) -> None:
            self.prompts: list[tuple[str, str]] = []
            self.cancel_requested: asyncio.Event | None = None

        def cancel_execution(self, **kwargs: Any) -> bool:
            return False

        def request_steer(self, **kwargs: Any) -> bool:
            assert self.cancel_requested is not None
            self.cancel_requested.set()
            return True

        async def stream_events(self, **kwargs: Any):
            self.prompts.append((kwargs["trace_id"], kwargs["prompt"]))
            if len(self.prompts) == 1:
                self.cancel_requested = asyncio.Event()
                yield "AgentStarted", {"_type": "AgentStarted"}
                yield "ToolStart", {"_type": "ToolStart", "tool_name": "Write"}
                await self.cancel_requested.wait()
                await asyncio.sleep(0.01)
                yield "ToolDone", {"_type": "ToolDone", "tool_name": "Write", "is_error": False}
                yield "AgentCancelled", {"_type": "AgentCancelled", "reason": "steer"}
                return
            yield "AgentStarted", {"_type": "AgentStarted"}
            yield "ReasoningChunk", {"_type": "ReasoningChunk", "content": "hidden reasoning"}
            yield "TextChunk", {"_type": "TextChunk", "content": "continued"}
            yield "AgentCompleted", {"_type": "AgentCompleted", "total_tokens": 1}

    service = Service()
    monkeypatch.setattr(routes, "followup_mailbox_service", mailbox)
    monkeypatch.setattr(routes, "execution_coordinator", coordinator)
    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: service)
    monkeypatch.setattr(routes, "_PHASE_HEARTBEAT_SECONDS", 0.002)
    _patch_finalization(monkeypatch, captured)

    snapshot = AgentGitSnapshot(workspace_root=tmp_path, available=True)
    handle = coordinator.begin(tmp_path, "session-1", "trace-1")
    handle.register_snapshot(snapshot)

    async def collect() -> list[tuple[str, dict[str, Any]]]:
        return [
            _decode(chunk)
            async for chunk in routes._stream_coomi_sse(
                prompt="initial request",
                trace_id="trace-1",
                session_id="session-1",
                active_file="",
                workspace_root=tmp_path,
                story_generation={},
                turn_contract={"status": "ready", "intentFrame": {"primary": "general"}},
                git_snapshot=snapshot,
                request=_ConnectedRequest(),
                cancellation_token=routes._CancellationToken(),
                execution_handle=handle,
                replacement=replacement,
            )
        ]

    packets = asyncio.run(collect())
    event_names = [event for event, _ in packets]
    assert service.prompts == [("trace-1", "initial request"), ("trace-1", "new guidance")]
    assert event_names.index("ToolDone") < event_names.index("SteerApplied")
    assert event_names.count("SteerApplied") == 1
    assert event_names.count("ContinuationStarted") == 1
    steer_index = event_names.index("SteerApplied")
    continuation_index = event_names.index("ContinuationStarted")
    continued_agent_index = event_names.index("AgentStarted", continuation_index)
    assert steer_index < continuation_index < continued_agent_index
    assert packets[steer_index][1]["status"] == "sent"
    assert event_names.count("AgentCancelled") == 0
    assert event_names.count("AgentCompleted") == 1
    assert event_names.count("ReasoningChunk") == 0
    assert replacement_calls == ["accepted"]
    assert coordinator.active_handle(session_id="session-1") is None

    state = mailbox.list_mailbox(workspace_root=tmp_path, session_id="session-1")
    assert state["messages"][0]["status"] == "sent"
    assert state["messages"][0]["segmentId"] == "trace-1-segment-2"
    captured_event_names = [event["event"] for event in captured["events"]]
    assert "ReasoningChunk" not in captured_event_names
    assert "SteerRequested" in captured_event_names
    assert "SteerApplied" in captured_event_names
    assert any(item["action"] == "agent_followup" for item in routes._build_audit(captured["events"]))


def test_replacement_startup_failure_restores_original_before_stream_finishes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    coordinator = ExecutionCoordinator()
    mailbox = FollowupMailboxService()
    captured: dict[str, Any] = {}
    calls: list[str] = []

    class Replacement:
        accepted = False
        restored = False
        expected_trace_id = "trace-original"

        def accept(self) -> None:
            self.accepted = True
            calls.append("accept")

        def restore(self, *, reason: str) -> None:
            self.restored = True
            calls.append(f"restore:{reason}")

    class Service:
        def cancel_execution(self, **kwargs: Any) -> bool:
            return False

        async def stream_events(self, **kwargs: Any):
            yield "AgentStarted", {"_type": "AgentStarted"}
            yield "AgentError", {"_type": "AgentError", "error_type": "ProviderStartup", "message": "startup failed"}

    replacement = Replacement()
    monkeypatch.setattr(routes, "execution_coordinator", coordinator)
    monkeypatch.setattr(routes, "followup_mailbox_service", mailbox)
    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: Service())
    _patch_finalization(monkeypatch, captured)
    snapshot = AgentGitSnapshot(workspace_root=tmp_path, available=True)
    handle = coordinator.begin(tmp_path, "session-1", "trace-new")
    handle.register_snapshot(snapshot)

    async def collect() -> list[tuple[str, dict[str, Any]]]:
        return [
            _decode(chunk)
            async for chunk in routes._stream_coomi_sse(
                prompt="replacement",
                trace_id="trace-new",
                session_id="session-1",
                active_file="",
                workspace_root=tmp_path,
                story_generation={},
                turn_contract={"status": "ready", "intentFrame": {"primary": "general"}},
                git_snapshot=snapshot,
                request=_ConnectedRequest(),
                cancellation_token=routes._CancellationToken(),
                execution_handle=handle,
                replacement=replacement,
            )
        ]

    packets = asyncio.run(collect())
    assert [event for event, _ in packets].count("AgentError") == 1
    assert [event for event, _ in packets][-1] == "done"
    assert calls == ["restore:replacement_start_failed"]
    assert replacement.restored is True
    assert replacement.accepted is False
    assert coordinator.active_handle(session_id="session-1") is None


def test_replacement_transaction_preserves_original_record_and_never_reverts_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original = {
        "traceId": "trace-original",
        "sessionId": "session-1",
        "workspaceRoot": tmp_path.as_posix(),
        "status": "completed",
        "prompt": "original prompt",
        "reply": "original answer",
        "changeLedger": {"changedFiles": ["chapters/001.md"], "changedFileCount": 1},
    }
    persisted: list[dict[str, Any]] = []
    restored_snapshots: list[dict[str, Any]] = []

    class Coomi:
        def snapshot_session_history(self, session_id: str, *, workspace_root: Path) -> dict[str, Any]:
            return {
                "available": True,
                "sessionId": session_id,
                "workspaceRoot": workspace_root,
                "historyPath": tmp_path / "session.jsonl",
                "historyBytes": b"original session",
            }

        def rollback_last_turn(self, session_id: str, *, workspace_root: Path) -> dict[str, Any]:
            return {"rolledBack": True}

        def restore_session_history(self, snapshot: dict[str, Any]) -> bool:
            restored_snapshots.append(snapshot)
            return True

    monkeypatch.setattr(routes, "trace_history_service", type("History", (), {"list_records": lambda self, **kwargs: [copy.deepcopy(original)]})())
    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: Coomi())
    monkeypatch.setattr(routes, "_persist_execution_trace", lambda root, record, session_id: persisted.append(copy.deepcopy(record)))
    monkeypatch.setattr(routes, "storydex_intent_service", type("Intent", (), {"clear_session": lambda self, **kwargs: None})())

    replacement = routes._LatestExecutionReplacement(
        session_id="session-1",
        expected_trace_id="trace-original",
        replacement_trace_id="trace-new",
        workspace_root=tmp_path,
        replacement_prompt="replacement prompt",
    )
    replacement.prepare()
    assert persisted[-1]["status"] == "superseded"
    assert persisted[-1]["replacement"]["status"] == "pending"
    assert persisted[-1]["replacement"]["fileChangesReverted"] is False
    assert persisted[-1]["changeLedger"] == original["changeLedger"]

    replacement.restore(reason="startup_failed")
    restored = persisted[-1]
    assert restored["traceId"] == "trace-original"
    assert restored["status"] == "completed"
    assert restored["reply"] == "original answer"
    assert restored["replacement"]["status"] == "restored"
    assert restored["replacement"]["fileChangesReverted"] is False
    assert restored["changeLedger"] == original["changeLedger"]
    assert len(restored_snapshots) == 1
