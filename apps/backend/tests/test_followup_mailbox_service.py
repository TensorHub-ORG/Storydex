from __future__ import annotations

from pathlib import Path

import pytest

from services.followup_mailbox_service import FollowupMailboxError, FollowupMailboxService


def _enqueue(
    service: FollowupMailboxService,
    root: Path,
    message_id: str,
    content: str,
    *,
    mode: str = "queued",
    expected_trace_id: str = "",
) -> dict:
    return service.enqueue(
        workspace_root=root,
        session_id="session-1",
        message_id=message_id,
        content=content,
        mode=mode,
        expected_trace_id=expected_trace_id,
    )


def test_queued_followups_are_fifo_idempotent_and_persisted(tmp_path: Path) -> None:
    service = FollowupMailboxService()

    first = _enqueue(service, tmp_path, "message-1", "first")
    replay = _enqueue(service, tmp_path, "message-1", "first")
    second = _enqueue(service, tmp_path, "message-2", "second")

    assert replay == first
    assert first["sequence"] < second["sequence"]

    reloaded = FollowupMailboxService()
    mailbox = reloaded.list_mailbox(workspace_root=tmp_path, session_id="session-1")
    assert [item["messageId"] for item in mailbox["messages"]] == ["message-1", "message-2"]
    assert mailbox["revision"] >= 2

    claimed_first = reloaded.claim_next_queued(
        workspace_root=tmp_path,
        session_id="session-1",
        previous_trace_id="trace-1",
        next_trace_id="trace-2",
    )
    assert claimed_first is not None
    assert claimed_first["messageId"] == "message-1"
    assert claimed_first["status"] == "dispatching"
    assert (
        reloaded.claim_next_queued(
            workspace_root=tmp_path,
            session_id="session-1",
            previous_trace_id="trace-1",
            next_trace_id="trace-racing",
        )
        is None
    )
    with pytest.raises(FollowupMailboxError) as racing_dispatch:
        reloaded.claim_queued_by_id(
            workspace_root=tmp_path,
            session_id="session-1",
            message_id="message-2",
            previous_trace_id="trace-1",
            next_trace_id="trace-racing",
        )
    assert racing_dispatch.value.code == "followup_dispatch_in_progress"

    sent_first = reloaded.mark_dispatch_sent(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-1",
        trace_id="trace-2",
    )
    assert sent_first["status"] == "sent"

    claimed_second = reloaded.claim_next_queued(
        workspace_root=tmp_path,
        session_id="session-1",
        previous_trace_id="trace-2",
        next_trace_id="trace-3",
    )
    assert claimed_second is not None
    assert claimed_second["messageId"] == "message-2"
    assert claimed_second["status"] == "dispatching"

    reloaded.mark_dispatch_sent(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-2",
        trace_id="trace-3",
    )
    assert (
        reloaded.claim_next_queued(
            workspace_root=tmp_path,
            session_id="session-1",
            previous_trace_id="trace-3",
            next_trace_id="trace-4",
        )
        is None
    )

    persisted = FollowupMailboxService().list_mailbox(workspace_root=tmp_path, session_id="session-1")
    assert [item["status"] for item in persisted["messages"]] == ["sent", "sent"]
    assert all(event["_version"] == 1 for event in persisted["events"])
    assert [event["_type"] for event in persisted["events"]].count("ContinuationStarted") == 2


def test_followups_can_be_edited_cancelled_and_promoted_to_steer(tmp_path: Path) -> None:
    service = FollowupMailboxService()
    service.set_active_trace(workspace_root=tmp_path, session_id="session-1", trace_id="trace-active")
    _enqueue(service, tmp_path, "message-1", "queued content")
    _enqueue(service, tmp_path, "message-2", "delete me")

    edited = service.update_message(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-1",
        content="edited content",
    )
    assert edited["content"] == "edited content"

    with pytest.raises(FollowupMailboxError, match="active execution changed") as stale:
        service.update_message(
            workspace_root=tmp_path,
            session_id="session-1",
            message_id="message-1",
            mode="steer",
            expected_trace_id="trace-stale",
        )
    assert stale.value.code == "stale_trace"

    steering = service.update_message(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-1",
        mode="steer",
        expected_trace_id="trace-active",
    )
    assert steering["mode"] == "steer"
    assert steering["status"] == "steering"

    claimed = service.claim_steer(
        workspace_root=tmp_path,
        session_id="session-1",
        trace_id="trace-active",
    )
    assert claimed is not None
    assert claimed["messageId"] == "message-1"
    event_count = len(service.list_mailbox(workspace_root=tmp_path, session_id="session-1")["events"])
    replayed_steer = service.update_message(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-1",
        mode="steer",
        expected_trace_id="trace-active",
    )
    assert replayed_steer["steerClaimToken"] == claimed["steerClaimToken"]
    assert len(service.list_mailbox(workspace_root=tmp_path, session_id="session-1")["events"]) == event_count
    assert service.claim_steer(workspace_root=tmp_path, session_id="session-1", trace_id="trace-active") is None

    service.release_steer_claim(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-1",
    )
    assert service.claim_steer(workspace_root=tmp_path, session_id="session-1", trace_id="trace-active") is not None

    applied = service.apply_steer(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-1",
        trace_id="trace-active",
        segment_id="trace-active-segment-2",
    )
    assert applied["status"] == "sent"
    assert applied["segmentId"] == "trace-active-segment-2"

    cancelled = service.cancel_message(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-2",
    )
    assert cancelled["status"] == "cancelled"
    assert service.cancel_message(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-2",
    ) == cancelled

    mailbox = FollowupMailboxService().list_mailbox(workspace_root=tmp_path, session_id="session-1")
    event_types = [event["_type"] for event in mailbox["events"]]
    assert "SteerRequested" in event_types
    assert "SteerApplied" in event_types
    assert "ContinuationStarted" in event_types


def test_paused_mailbox_preserves_pending_message_and_resumes_exactly_once(tmp_path: Path) -> None:
    service = FollowupMailboxService()
    _enqueue(service, tmp_path, "message-1", "resume me")

    paused = service.pause(workspace_root=tmp_path, session_id="session-1", reason="manual_stop")
    assert paused["paused"] is True
    assert (
        service.claim_next_queued(
            workspace_root=tmp_path,
            session_id="session-1",
            previous_trace_id="trace-1",
            next_trace_id="trace-2",
        )
        is None
    )
    assert FollowupMailboxService().list_mailbox(workspace_root=tmp_path, session_id="session-1")["messages"][0][
        "status"
    ] == "pending"

    service.resume(workspace_root=tmp_path, session_id="session-1")
    with pytest.raises(FollowupMailboxError) as stale:
        service.claim_queued_by_id(
            workspace_root=tmp_path,
            session_id="session-1",
            message_id="message-1",
            previous_trace_id="trace-1",
            next_trace_id="trace-2",
            expected_trace_id="trace-stale",
        )
    assert stale.value.code == "stale_trace"

    claimed = service.claim_queued_by_id(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-1",
        previous_trace_id="trace-1",
        next_trace_id="trace-2",
        expected_trace_id="trace-1",
    )
    replay = service.claim_queued_by_id(
        workspace_root=tmp_path,
        session_id="session-1",
        message_id="message-1",
        previous_trace_id="trace-1",
        next_trace_id="trace-2",
        expected_trace_id="trace-1",
    )
    assert claimed == replay
    assert replay["status"] == "dispatching"
