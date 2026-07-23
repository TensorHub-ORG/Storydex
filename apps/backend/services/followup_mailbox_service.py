from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterable
from uuid import uuid4


FOLLOWUP_EVENT_VERSION = 1
FOLLOWUP_MAILBOX_VERSION = 1
FOLLOWUP_MODES = {"queued", "steer"}
FOLLOWUP_STATUSES = {"pending", "steering", "dispatching", "sent", "cancelled", "failed"}
_EDITABLE_STATUSES = {"pending", "steering"}
_MAX_EVENTS = 500


class FollowupMailboxError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = str(code or "followup_error")
        self.details = dict(details or {})


class FollowupMailboxService:
    """Durable per-workspace/session mailbox for queued and steering messages."""

    def __init__(self) -> None:
        self._lock = RLock()

    def list_mailbox(self, *, workspace_root: Path, session_id: str) -> Dict[str, Any]:
        with self._lock:
            state = self._load(workspace_root=workspace_root, session_id=session_id)
            return deepcopy(state)

    def enqueue(
        self,
        *,
        workspace_root: Path,
        session_id: str,
        message_id: str,
        content: str,
        mode: str = "queued",
        expected_trace_id: str = "",
    ) -> Dict[str, Any]:
        normalized_id = str(message_id or "").strip()
        normalized_content = str(content or "").strip()
        normalized_mode = self._normalize_mode(mode)
        if not normalized_id:
            raise FollowupMailboxError("missing_message_id", "messageId is required.")
        if not normalized_content:
            raise FollowupMailboxError("empty_followup", "Follow-up content cannot be empty.")

        with self._lock:
            state = self._load(workspace_root=workspace_root, session_id=session_id)
            existing = self._find_message(state, normalized_id)
            if existing is not None:
                if (
                    str(existing.get("content") or "") == normalized_content
                    and str(existing.get("mode") or "") == normalized_mode
                ):
                    return deepcopy(existing)
                raise FollowupMailboxError(
                    "message_id_conflict",
                    "messageId already exists with different content.",
                    details={"messageId": normalized_id},
                )

            active_trace_id = str(state.get("activeTraceId") or "").strip()
            if normalized_mode == "steer":
                self._validate_expected_trace(
                    active_trace_id=active_trace_id,
                    expected_trace_id=expected_trace_id,
                    require_active=True,
                )
            now = _now_iso()
            message = {
                "messageId": normalized_id,
                "sessionId": self._normalize_session_id(session_id),
                "activeTraceId": active_trace_id,
                "expectedTraceId": str(expected_trace_id or active_trace_id).strip(),
                "content": normalized_content,
                "mode": normalized_mode,
                "status": "steering" if normalized_mode == "steer" else "pending",
                "statusDetail": "等待安全中断点" if normalized_mode == "steer" else "等待当前轮完成",
                "createdAt": now,
                "updatedAt": now,
                "sequence": self._next_message_sequence(state),
                "dispatchTraceId": "",
                "segmentId": "",
                "error": "",
            }
            state.setdefault("messages", []).append(message)
            self._append_event(state, "FollowupQueued", message, trace_id=active_trace_id)
            if normalized_mode == "steer":
                self._append_event(state, "SteerRequested", message, trace_id=active_trace_id)
            self._write(state, workspace_root=workspace_root, session_id=session_id)
            return deepcopy(message)

    def update_message(
        self,
        *,
        workspace_root: Path,
        session_id: str,
        message_id: str,
        content: str | None = None,
        mode: str | None = None,
        expected_trace_id: str = "",
    ) -> Dict[str, Any]:
        with self._lock:
            state = self._load(workspace_root=workspace_root, session_id=session_id)
            message = self._require_message(state, message_id)
            if str(message.get("status") or "") not in _EDITABLE_STATUSES:
                raise FollowupMailboxError(
                    "followup_not_editable",
                    "Only pending or steering messages can be edited.",
                    details={"messageId": message_id, "status": message.get("status")},
                )

            normalized_content = None
            if content is not None:
                normalized_content = str(content or "").strip()
                if not normalized_content:
                    raise FollowupMailboxError("empty_followup", "Follow-up content cannot be empty.")
            normalized_mode = self._normalize_mode(mode) if mode is not None else None
            content_unchanged = normalized_content is None or normalized_content == str(message.get("content") or "")
            mode_unchanged = normalized_mode is None or normalized_mode == str(message.get("mode") or "")
            if content_unchanged and mode_unchanged:
                if normalized_mode == "steer" or str(message.get("mode") or "") == "steer":
                    self._validate_expected_trace(
                        active_trace_id=str(state.get("activeTraceId") or "").strip(),
                        expected_trace_id=expected_trace_id or str(message.get("expectedTraceId") or ""),
                        require_active=True,
                    )
                return deepcopy(message)

            if normalized_content is not None:
                message["content"] = normalized_content

            if normalized_mode is not None:
                if normalized_mode == "steer":
                    active_trace_id = str(state.get("activeTraceId") or "").strip()
                    self._validate_expected_trace(
                        active_trace_id=active_trace_id,
                        expected_trace_id=expected_trace_id or str(message.get("expectedTraceId") or ""),
                        require_active=True,
                    )
                    message.update(
                        {
                            "mode": "steer",
                            "status": "steering",
                            "activeTraceId": active_trace_id,
                            "expectedTraceId": str(expected_trace_id or active_trace_id).strip(),
                            "statusDetail": "等待安全中断点",
                            "steerClaimToken": "",
                        }
                    )
                    self._append_event(state, "SteerRequested", message, trace_id=active_trace_id)
                else:
                    message.update(
                        {
                            "mode": "queued",
                            "status": "pending",
                            "statusDetail": "等待当前轮完成",
                            "steerClaimToken": "",
                        }
                    )
            message["updatedAt"] = _now_iso()
            self._append_event(
                state,
                "FollowupUpdated",
                message,
                trace_id=str(state.get("activeTraceId") or message.get("activeTraceId") or ""),
            )
            self._write(state, workspace_root=workspace_root, session_id=session_id)
            return deepcopy(message)

    def cancel_message(
        self,
        *,
        workspace_root: Path,
        session_id: str,
        message_id: str,
    ) -> Dict[str, Any]:
        with self._lock:
            state = self._load(workspace_root=workspace_root, session_id=session_id)
            message = self._require_message(state, message_id)
            if str(message.get("status") or "") == "cancelled":
                return deepcopy(message)
            if str(message.get("status") or "") not in _EDITABLE_STATUSES:
                raise FollowupMailboxError(
                    "followup_not_editable",
                    "A dispatching or sent follow-up cannot be deleted.",
                    details={"messageId": message_id, "status": message.get("status")},
                )
            message.update(
                {
                    "status": "cancelled",
                    "statusDetail": "已删除",
                    "updatedAt": _now_iso(),
                    "steerClaimToken": "",
                }
            )
            self._append_event(
                state,
                "FollowupUpdated",
                message,
                trace_id=str(state.get("activeTraceId") or message.get("activeTraceId") or ""),
            )
            self._write(state, workspace_root=workspace_root, session_id=session_id)
            return deepcopy(message)

    def set_active_trace(
        self,
        *,
        workspace_root: Path,
        session_id: str,
        trace_id: str,
    ) -> Dict[str, Any]:
        with self._lock:
            state = self._load(workspace_root=workspace_root, session_id=session_id)
            state["activeTraceId"] = str(trace_id or "").strip()
            state["updatedAt"] = _now_iso()
            self._write(state, workspace_root=workspace_root, session_id=session_id)
            return deepcopy(state)

    def clear_active_trace(
        self,
        *,
        workspace_root: Path,
        session_id: str,
        expected_trace_id: str,
    ) -> Dict[str, Any]:
        with self._lock:
            state = self._load(workspace_root=workspace_root, session_id=session_id)
            if str(state.get("activeTraceId") or "") == str(expected_trace_id or "").strip():
                state["activeTraceId"] = ""
                state["updatedAt"] = _now_iso()
                self._write(state, workspace_root=workspace_root, session_id=session_id)
            return deepcopy(state)

    def claim_steer(
        self,
        *,
        workspace_root: Path,
        session_id: str,
        trace_id: str,
    ) -> Dict[str, Any] | None:
        normalized_trace = str(trace_id or "").strip()
        with self._lock:
            state = self._load(workspace_root=workspace_root, session_id=session_id)
            for message in self._ordered_messages(state):
                if str(message.get("mode") or "") != "steer" or str(message.get("status") or "") != "steering":
                    continue
                expected = str(message.get("expectedTraceId") or message.get("activeTraceId") or "").strip()
                if expected and expected != normalized_trace:
                    continue
                if str(message.get("steerClaimToken") or "").strip():
                    continue
                message["steerClaimToken"] = uuid4().hex
                message["statusDetail"] = "正在安全中断并追加信息"
                message["updatedAt"] = _now_iso()
                self._write(state, workspace_root=workspace_root, session_id=session_id)
                return deepcopy(message)
            return None

    def release_steer_claim(
        self,
        *,
        workspace_root: Path,
        session_id: str,
        message_id: str,
        error: str = "",
    ) -> None:
        with self._lock:
            state = self._load(workspace_root=workspace_root, session_id=session_id)
            message = self._find_message(state, message_id)
            if message is None or str(message.get("status") or "") != "steering":
                return
            message["steerClaimToken"] = ""
            message["statusDetail"] = str(error or "等待安全中断点")
            message["updatedAt"] = _now_iso()
            self._write(state, workspace_root=workspace_root, session_id=session_id)

    def apply_steer(
        self,
        *,
        workspace_root: Path,
        session_id: str,
        message_id: str,
        trace_id: str,
        segment_id: str,
    ) -> Dict[str, Any]:
        with self._lock:
            state = self._load(workspace_root=workspace_root, session_id=session_id)
            message = self._require_message(state, message_id)
            if str(message.get("status") or "") == "sent":
                return deepcopy(message)
            if str(message.get("status") or "") != "steering":
                raise FollowupMailboxError(
                    "invalid_followup_transition",
                    "Steer message is no longer waiting for application.",
                    details={"messageId": message_id, "status": message.get("status")},
                )
            message.update(
                {
                    "status": "sent",
                    "statusDetail": "已作为当前执行的新片段发送",
                    "dispatchTraceId": str(trace_id or "").strip(),
                    "segmentId": str(segment_id or "").strip(),
                    "updatedAt": _now_iso(),
                    "steerClaimToken": "",
                }
            )
            self._append_event(state, "SteerApplied", message, trace_id=trace_id, segment_id=segment_id)
            self._append_event(
                state,
                "ContinuationStarted",
                message,
                trace_id=trace_id,
                segment_id=segment_id,
            )
            self._write(state, workspace_root=workspace_root, session_id=session_id)
            return deepcopy(message)

    def claim_next_queued(
        self,
        *,
        workspace_root: Path,
        session_id: str,
        previous_trace_id: str,
        next_trace_id: str,
    ) -> Dict[str, Any] | None:
        with self._lock:
            state = self._load(workspace_root=workspace_root, session_id=session_id)
            if bool(state.get("paused")):
                return None
            if any(
                str(item.get("mode") or "") == "queued"
                and str(item.get("status") or "") == "dispatching"
                for item in self._ordered_messages(state)
            ):
                return None
            for message in self._ordered_messages(state):
                if str(message.get("mode") or "") != "queued" or str(message.get("status") or "") != "pending":
                    continue
                self._claim_queued_message(
                    state,
                    message,
                    previous_trace_id=previous_trace_id,
                    next_trace_id=next_trace_id,
                )
                self._write(state, workspace_root=workspace_root, session_id=session_id)
                return deepcopy(message)
            return None

    def claim_queued_by_id(
        self,
        *,
        workspace_root: Path,
        session_id: str,
        message_id: str,
        previous_trace_id: str,
        next_trace_id: str,
        expected_trace_id: str = "",
    ) -> Dict[str, Any]:
        """Atomically claim one persisted message when resuming a stopped queue."""

        with self._lock:
            state = self._load(workspace_root=workspace_root, session_id=session_id)
            if bool(state.get("paused")):
                raise FollowupMailboxError(
                    "followup_mailbox_paused",
                    "The follow-up mailbox is paused.",
                    details={"pauseReason": str(state.get("pauseReason") or "")},
                )
            message = self._require_message(state, message_id)
            if str(message.get("mode") or "") != "queued":
                raise FollowupMailboxError(
                    "invalid_followup_transition",
                    "Only a queued follow-up can start a resumed turn.",
                    details={"messageId": message_id, "mode": message.get("mode")},
                )
            expected = str(expected_trace_id or "").strip()
            previous = str(previous_trace_id or "").strip()
            if expected and expected != previous:
                raise FollowupMailboxError(
                    "stale_trace",
                    "The latest execution changed before the queued follow-up was resumed.",
                    details={"expectedTraceId": expected, "latestTraceId": previous},
                )
            status = str(message.get("status") or "")
            dispatch_trace = str(message.get("dispatchTraceId") or "").strip()
            if status == "dispatching" and dispatch_trace == str(next_trace_id or "").strip():
                return deepcopy(message)
            if any(
                str(item.get("messageId") or "") != str(message_id or "").strip()
                and str(item.get("mode") or "") == "queued"
                and str(item.get("status") or "") == "dispatching"
                for item in self._ordered_messages(state)
            ):
                raise FollowupMailboxError(
                    "followup_dispatch_in_progress",
                    "Another queued follow-up is already dispatching.",
                    details={"messageId": message_id},
                )
            if status != "pending":
                raise FollowupMailboxError(
                    "invalid_followup_transition",
                    "The follow-up is no longer pending dispatch.",
                    details={"messageId": message_id, "status": status},
                )
            self._claim_queued_message(
                state,
                message,
                previous_trace_id=previous,
                next_trace_id=next_trace_id,
            )
            self._write(state, workspace_root=workspace_root, session_id=session_id)
            return deepcopy(message)

    def mark_dispatch_sent(
        self,
        *,
        workspace_root: Path,
        session_id: str,
        message_id: str,
        trace_id: str,
    ) -> Dict[str, Any]:
        return self._finish_dispatch(
            workspace_root=workspace_root,
            session_id=session_id,
            message_id=message_id,
            trace_id=trace_id,
            status="sent",
            detail="已发送",
        )

    def mark_dispatch_failed(
        self,
        *,
        workspace_root: Path,
        session_id: str,
        message_id: str,
        trace_id: str,
        error: str,
        retryable: bool = False,
    ) -> Dict[str, Any]:
        return self._finish_dispatch(
            workspace_root=workspace_root,
            session_id=session_id,
            message_id=message_id,
            trace_id=trace_id,
            status="pending" if retryable else "failed",
            detail="等待恢复发送" if retryable else "发送失败",
            error=error,
        )

    def pause(
        self,
        *,
        workspace_root: Path,
        session_id: str,
        reason: str,
    ) -> Dict[str, Any]:
        with self._lock:
            state = self._load(workspace_root=workspace_root, session_id=session_id)
            state.update(
                {
                    "paused": True,
                    "pauseReason": str(reason or "paused"),
                    "updatedAt": _now_iso(),
                }
            )
            self._write(state, workspace_root=workspace_root, session_id=session_id)
            return deepcopy(state)

    def resume(self, *, workspace_root: Path, session_id: str) -> Dict[str, Any]:
        with self._lock:
            state = self._load(workspace_root=workspace_root, session_id=session_id)
            state.update({"paused": False, "pauseReason": "", "updatedAt": _now_iso()})
            # A dispatching item without a live trace must not be sent twice on
            # refresh.  Keep it dispatching; an explicit retry action can move
            # a failed item back to pending after the caller verifies history.
            self._write(state, workspace_root=workspace_root, session_id=session_id)
            return deepcopy(state)

    def events_for_trace(
        self,
        *,
        workspace_root: Path,
        session_id: str,
        trace_id: str,
        event_types: Iterable[str] | None = None,
    ) -> list[Dict[str, Any]]:
        normalized_trace = str(trace_id or "").strip()
        allowed = {str(item) for item in event_types} if event_types is not None else None
        with self._lock:
            state = self._load(workspace_root=workspace_root, session_id=session_id)
            matches = []
            for event in state.get("events") if isinstance(state.get("events"), list) else []:
                if not isinstance(event, dict):
                    continue
                if allowed is not None and str(event.get("_type") or "") not in allowed:
                    continue
                event_trace = str(event.get("traceId") or event.get("activeTraceId") or "").strip()
                if event_trace == normalized_trace:
                    matches.append(deepcopy(event))
            return matches

    def _finish_dispatch(
        self,
        *,
        workspace_root: Path,
        session_id: str,
        message_id: str,
        trace_id: str,
        status: str,
        detail: str,
        error: str = "",
    ) -> Dict[str, Any]:
        with self._lock:
            state = self._load(workspace_root=workspace_root, session_id=session_id)
            message = self._require_message(state, message_id)
            if str(message.get("status") or "") == "sent" and status == "sent":
                return deepcopy(message)
            if str(message.get("status") or "") != "dispatching":
                raise FollowupMailboxError(
                    "invalid_followup_transition",
                    "Follow-up is not dispatching.",
                    details={"messageId": message_id, "status": message.get("status")},
                )
            message.update(
                {
                    "status": status,
                    "statusDetail": detail,
                    "dispatchTraceId": str(trace_id or message.get("dispatchTraceId") or "").strip(),
                    "updatedAt": _now_iso(),
                    "error": str(error or ""),
                    "dispatchToken": "",
                }
            )
            self._append_event(state, "FollowupUpdated", message, trace_id=trace_id)
            self._write(state, workspace_root=workspace_root, session_id=session_id)
            return deepcopy(message)

    def _claim_queued_message(
        self,
        state: Dict[str, Any],
        message: Dict[str, Any],
        *,
        previous_trace_id: str,
        next_trace_id: str,
    ) -> None:
        message.update(
            {
                "status": "dispatching",
                "statusDetail": "正在启动下一轮",
                "dispatchTraceId": str(next_trace_id or "").strip(),
                "previousTraceId": str(previous_trace_id or "").strip(),
                "updatedAt": _now_iso(),
                "dispatchToken": uuid4().hex,
            }
        )
        self._append_event(
            state,
            "ContinuationStarted",
            message,
            trace_id=next_trace_id,
            previous_trace_id=previous_trace_id,
        )

    @staticmethod
    def _normalize_session_id(session_id: str) -> str:
        return str(session_id or "default").strip() or "default"

    @staticmethod
    def _normalize_mode(mode: str) -> str:
        normalized = str(mode or "queued").strip().lower()
        if normalized not in FOLLOWUP_MODES:
            raise FollowupMailboxError("invalid_followup_mode", "mode must be queued or steer.")
        return normalized

    @staticmethod
    def _validate_expected_trace(
        *,
        active_trace_id: str,
        expected_trace_id: str,
        require_active: bool,
    ) -> None:
        active = str(active_trace_id or "").strip()
        expected = str(expected_trace_id or "").strip()
        if require_active and not active:
            raise FollowupMailboxError("no_active_execution", "There is no active execution to steer.")
        if expected and active != expected:
            raise FollowupMailboxError(
                "stale_trace",
                "The active execution changed before the steer request was applied.",
                details={"expectedTraceId": expected, "activeTraceId": active},
            )

    @staticmethod
    def _ordered_messages(state: Dict[str, Any]) -> list[Dict[str, Any]]:
        messages = [item for item in state.get("messages", []) if isinstance(item, dict)]
        return sorted(messages, key=lambda item: (int(item.get("sequence") or 0), str(item.get("createdAt") or "")))

    @staticmethod
    def _find_message(state: Dict[str, Any], message_id: str) -> Dict[str, Any] | None:
        normalized = str(message_id or "").strip()
        for message in state.get("messages") if isinstance(state.get("messages"), list) else []:
            if isinstance(message, dict) and str(message.get("messageId") or "") == normalized:
                return message
        return None

    def _require_message(self, state: Dict[str, Any], message_id: str) -> Dict[str, Any]:
        message = self._find_message(state, message_id)
        if message is None:
            raise FollowupMailboxError(
                "followup_not_found",
                "Follow-up message was not found.",
                details={"messageId": str(message_id or "")},
            )
        return message

    @staticmethod
    def _next_message_sequence(state: Dict[str, Any]) -> int:
        return max(
            [int(item.get("sequence") or 0) for item in state.get("messages", []) if isinstance(item, dict)] or [0]
        ) + 1

    def _append_event(
        self,
        state: Dict[str, Any],
        event_type: str,
        message: Dict[str, Any],
        *,
        trace_id: str = "",
        segment_id: str = "",
        previous_trace_id: str = "",
    ) -> Dict[str, Any]:
        now = _now_iso()
        sequence = int(state.get("eventSequence") or 0) + 1
        state["eventSequence"] = sequence
        event = {
            "_type": str(event_type or "FollowupUpdated"),
            "_version": FOLLOWUP_EVENT_VERSION,
            "eventId": uuid4().hex,
            "sequence": sequence,
            "messageId": str(message.get("messageId") or ""),
            "sessionId": str(message.get("sessionId") or state.get("sessionId") or "default"),
            "activeTraceId": str(message.get("activeTraceId") or state.get("activeTraceId") or ""),
            "traceId": str(trace_id or message.get("dispatchTraceId") or ""),
            "previousTraceId": str(previous_trace_id or message.get("previousTraceId") or ""),
            "segmentId": str(segment_id or message.get("segmentId") or ""),
            "content": str(message.get("content") or ""),
            "mode": str(message.get("mode") or "queued"),
            "status": str(message.get("status") or "pending"),
            "createdAt": str(message.get("createdAt") or now),
            "updatedAt": now,
        }
        events = state.setdefault("events", [])
        events.append(event)
        if len(events) > _MAX_EVENTS:
            del events[:-_MAX_EVENTS]
        return event

    def _load(self, *, workspace_root: Path, session_id: str) -> Dict[str, Any]:
        workspace = Path(workspace_root).resolve()
        normalized_session = self._normalize_session_id(session_id)
        path = _mailbox_path(workspace, normalized_session)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict) or str(payload.get("sessionId") or normalized_session) != normalized_session:
            payload = {}
        state = {
            "_type": "FollowupMailbox",
            "_version": FOLLOWUP_MAILBOX_VERSION,
            "revision": int(payload.get("revision") or 0),
            "workspaceRoot": workspace.as_posix(),
            "sessionId": normalized_session,
            "activeTraceId": str(payload.get("activeTraceId") or ""),
            "paused": bool(payload.get("paused")),
            "pauseReason": str(payload.get("pauseReason") or ""),
            "messageSequence": int(payload.get("messageSequence") or 0),
            "eventSequence": int(payload.get("eventSequence") or 0),
            "messages": [
                dict(item)
                for item in (payload.get("messages") if isinstance(payload.get("messages"), list) else [])
                if isinstance(item, dict)
                and str(item.get("mode") or "") in FOLLOWUP_MODES
                and str(item.get("status") or "") in FOLLOWUP_STATUSES
            ],
            "events": [
                dict(item)
                for item in (payload.get("events") if isinstance(payload.get("events"), list) else [])
                if isinstance(item, dict)
            ][-_MAX_EVENTS:],
            "createdAt": str(payload.get("createdAt") or _now_iso()),
            "updatedAt": str(payload.get("updatedAt") or _now_iso()),
        }
        return state

    def _write(self, state: Dict[str, Any], *, workspace_root: Path, session_id: str) -> None:
        state["revision"] = int(state.get("revision") or 0) + 1
        state["updatedAt"] = _now_iso()
        path = _mailbox_path(Path(workspace_root).resolve(), self._normalize_session_id(session_id))
        _atomic_write_json(path, state)


def _mailbox_path(workspace_root: Path, session_id: str) -> Path:
    digest = hashlib.sha256(str(session_id or "default").encode("utf-8")).hexdigest()[:24]
    return Path(workspace_root).resolve() / ".storydex" / ".agent" / "followups" / f"{digest}.json"


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_SERVICE = FollowupMailboxService()


def get_followup_mailbox_service() -> FollowupMailboxService:
    return _SERVICE
