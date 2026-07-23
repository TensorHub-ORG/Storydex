from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import copy
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from api.response import ApiEnvelope, ApiTrace, success_response
from core.exceptions import GitServiceError, StorydexError
from services.agent_git_autocommit_service import AgentGitSnapshot, get_agent_git_autocommit_service
from services.coomi_agent_service import get_storydex_coomi_agent_service
from services.context_policy import ContextPolicy
from services.context_trace_service import merge_llm_metrics, summarize_context_trace
from services.execution_log_service import ExecutionLogSession, create_execution_log_session
from services.followup_mailbox_service import FollowupMailboxError, get_followup_mailbox_service
from services.execution_coordinator import (
    ExecutionFinalizationContext,
    ExecutionHandle,
    ExecutionObservation,
    SnapshotConfirmationRequired,
    get_execution_coordinator,
)
from services.git_service import get_git_service
from services.llm_replay import get_llm_metrics, llm_trace, reset_llm_metrics
from services.project_service import get_project_service
from services.story_project_service import get_story_project_service
from services.storydex_intent_service import get_storydex_intent_service
from services.storydex_orchestration_service import get_storydex_orchestration_service
from services.trace_history_service import get_trace_history_service


router = APIRouter(tags=["agent"])

trace_history_service = get_trace_history_service()
project_service = get_project_service()
agent_git_autocommit_service = get_agent_git_autocommit_service()
storydex_orchestration_service = get_storydex_orchestration_service()
storydex_intent_service = get_storydex_intent_service()
git_service = get_git_service()
story_project_service = get_story_project_service()
execution_coordinator = get_execution_coordinator()
followup_mailbox_service = get_followup_mailbox_service()

_PHASE_HEARTBEAT_SECONDS = 0.6
_COMMIT_MESSAGE_TIMEOUT_SECONDS = 2.0
# Keep headroom below the externally observable two-second acceptance bound;
# asyncio timeout wake-up and SSE serialization still consume a few milliseconds.
_INTENT_STAGE_TIMEOUT_SECONDS = 1.8
_PLANNER_TIMEOUT_SECONDS = 3.0
_STORY_GENERATION_MAX_CORRECTIONS = 2
_LOGGER = logging.getLogger(__name__)
_BACKGROUND_EXECUTION_TASKS: set[asyncio.Task[Any]] = set()


def _retain_background_execution_task(task: asyncio.Task[Any]) -> asyncio.Task[Any]:
    _BACKGROUND_EXECUTION_TASKS.add(task)

    def release(completed: asyncio.Task[Any]) -> None:
        _BACKGROUND_EXECUTION_TASKS.discard(completed)
        if completed.cancelled():
            return
        try:
            completed.result()
        except Exception:
            _LOGGER.exception("Background execution task failed: %s", completed.get_name())

    task.add_done_callback(release)
    return task


class _CancellationToken:
    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled


async def _classify_intent_without_blocking_event_loop(**kwargs: Any) -> Dict[str, Any]:
    result: concurrent.futures.Future[Dict[str, Any]] = concurrent.futures.Future()
    context = contextvars.copy_context()

    def classify_on_isolated_loop() -> Dict[str, Any]:
        return context.run(
            lambda: asyncio.run(storydex_intent_service.classify_intent(**kwargs))
        )

    def run() -> None:
        try:
            value = classify_on_isolated_loop()
        except BaseException as exc:
            if not result.done():
                result.set_exception(exc)
        else:
            if not result.done():
                result.set_result(value)

    # A timed-out provider import/request must not occupy a shared executor
    # worker or delay the next intent classification.  The isolated daemon
    # thread may finish cleanup in the background without holding any queue.
    threading.Thread(
        target=run,
        name=f"storydex-intent-{uuid4().hex[:8]}",
        daemon=True,
    ).start()
    try:
        return await asyncio.wait_for(
            asyncio.wrap_future(result),
            timeout=_INTENT_STAGE_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, TimeoutError):
        from services.storydex_intent_service import heuristic_intent_frame

        frame = heuristic_intent_frame(
            prompt=str(kwargs.get("prompt") or ""),
            active_file=str(kwargs.get("active_file") or ""),
        )
        frame["method"] = "heuristic_deadline_fallback"
        frame.setdefault("assetTargets", [])
        frame.setdefault("matchedSkills", [])
        return frame


class AgentChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=12000)
    active_file: str = Field(default="", alias="activeFile")
    workspace_root: str = Field(default="", alias="workspaceRoot")
    story_generation: Dict[str, Any] = Field(default_factory=dict, alias="storyGeneration")
    confirm_no_snapshot: bool = Field(default=False, alias="confirmNoSnapshot")
    replace_latest_trace_id: str = Field(default="", alias="replaceLatestTraceId")
    source_followup_message_id: str = Field(default="", alias="sourceFollowupMessageId")
    source_followup_expected_trace_id: str = Field(default="", alias="sourceFollowupExpectedTraceId")

    model_config = ConfigDict(populate_by_name=True)


class AgentTraceEvent(BaseModel):
    index: int = 0
    event: str = ""
    phase: str = ""
    status: str = "info"
    detail: str = ""
    timestamp: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)


class AgentChatData(BaseModel):
    route: str = "coomi"
    reply: str = ""
    llm_model: str = Field(default="", alias="llmModel")
    llm_provider: str = Field(default="", alias="llmProvider")
    events: List[AgentTraceEvent] = Field(default_factory=list)
    assistant: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)


class AgentHistoryData(BaseModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)


class AgentSessionSummary(BaseModel):
    session_id: str = Field(alias="sessionId")
    first_prompt: str = Field(default="", alias="firstPrompt")
    created_at: str = Field(default="", alias="createdAt")
    updated_at: str = Field(default="", alias="updatedAt")
    trace_count: int = Field(default=0, alias="traceCount")

    model_config = ConfigDict(populate_by_name=True)


class AgentSessionsData(BaseModel):
    items: List[AgentSessionSummary] = Field(default_factory=list)


class AgentCoomiStatusData(BaseModel):
    runtime: str = "coomi"
    installed: bool = False
    home: str = ""
    config_path: str = Field(default="", alias="configPath")
    sessions_path: str = Field(default="", alias="sessionsPath")
    provider_id: str = Field(default="", alias="providerId")
    provider_type: str = Field(default="", alias="providerType")
    model: str = ""
    display: str = ""
    permission_mode: str = Field(default="", alias="permissionMode")
    permission_label: str = Field(default="", alias="permissionLabel")
    plan_mode: bool = Field(default=False, alias="planMode")
    tool_count: int = Field(default=0, alias="toolCount")
    context_window: int = Field(default=0, alias="contextWindow")
    used_tokens: int = Field(default=0, alias="usedTokens")
    usage_ratio: float = Field(default=0.0, alias="usageRatio")
    cumulative_tokens: int = Field(default=0, alias="cumulativeTokens")
    compact_threshold: int = Field(default=0, alias="compactThreshold")
    warning_threshold: int = Field(default=0, alias="warningThreshold")
    compression_status: str = Field(default="", alias="compressionStatus")

    model_config = ConfigDict(populate_by_name=True)


class AgentCoomiConfigData(BaseModel):
    config_path: str = Field(alias="configPath")
    content: str = ""
    parsed: Dict[str, Any] = Field(default_factory=dict)
    updated_at: str = Field(default="", alias="updatedAt")

    model_config = ConfigDict(populate_by_name=True)


class AgentCoomiConfigUpdateRequest(BaseModel):
    content: str


class AgentCoomiModelListRequest(BaseModel):
    base_url: str = Field(default="", alias="baseUrl")
    api_key: str = Field(default="", alias="apiKey")

    model_config = ConfigDict(populate_by_name=True)


class AgentCoomiModelListData(BaseModel):
    endpoint: str = ""
    models: List[str] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


class AgentPermissionModeRequest(BaseModel):
    permission_mode: str = Field(alias="permissionMode")

    model_config = ConfigDict(populate_by_name=True)


class AgentSessionDeleteRequest(BaseModel):
    session_id: str = Field(alias="sessionId")

    model_config = ConfigDict(populate_by_name=True)


class AgentExecutionRollbackRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_trace_id: str = Field(default="", alias="expectedTraceId")

    model_config = ConfigDict(populate_by_name=True)


class AgentFollowupRequest(BaseModel):
    message_id: str = Field(alias="messageId", min_length=1, max_length=160)
    session_id: str = Field(alias="sessionId")
    active_trace_id: str = Field(default="", alias="activeTraceId")
    expected_trace_id: str = Field(default="", alias="expectedTraceId")
    workspace_root: str = Field(default="", alias="workspaceRoot")
    content: str = Field(min_length=1, max_length=12000)
    mode: str = "queued"

    model_config = ConfigDict(populate_by_name=True)


class AgentFollowupUpdateRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_trace_id: str = Field(default="", alias="expectedTraceId")
    workspace_root: str = Field(default="", alias="workspaceRoot")
    content: Optional[str] = Field(default=None, max_length=12000)
    mode: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class AgentFollowupActionRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_trace_id: str = Field(default="", alias="expectedTraceId")
    workspace_root: str = Field(default="", alias="workspaceRoot")

    model_config = ConfigDict(populate_by_name=True)


class AgentExecutionStopRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_trace_id: str = Field(default="", alias="expectedTraceId")
    workspace_root: str = Field(default="", alias="workspaceRoot")

    model_config = ConfigDict(populate_by_name=True)


class AgentApprovalRequest(BaseModel):
    approval_id: str = Field(alias="approvalId")
    decision: str
    response: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)


class AgentCommitDecisionRequest(BaseModel):
    mode: str
    message: str = ""
    session_id: str = Field(default="", alias="sessionId")

    model_config = ConfigDict(populate_by_name=True)


class _LatestExecutionReplacement:
    """Reversible dialogue-only replacement transaction for the latest turn."""

    def __init__(
        self,
        *,
        session_id: str,
        expected_trace_id: str,
        replacement_trace_id: str,
        workspace_root: Path,
        replacement_prompt: str,
    ) -> None:
        self.session_id = str(session_id or "default").strip() or "default"
        self.expected_trace_id = str(expected_trace_id or "").strip()
        self.replacement_trace_id = str(replacement_trace_id or "").strip()
        self.workspace_root = Path(workspace_root).resolve()
        self.replacement_prompt = str(replacement_prompt or "")
        self.original_record: Dict[str, Any] | None = None
        self.session_snapshot: Dict[str, Any] | None = None
        self.prepared = False
        self.accepted = False
        self.restored = False

    def prepare(self) -> None:
        records = trace_history_service.list_records(session_id=self.session_id, limit=1)
        latest = records[0] if records else None
        latest_trace_id = str(latest.get("traceId") or "").strip() if isinstance(latest, dict) else ""
        if not isinstance(latest, dict) or not latest_trace_id:
            raise StorydexError(
                "There is no completed execution to replace.",
                code="replacement_target_missing",
                status_code=409,
            )
        if self.expected_trace_id and latest_trace_id != self.expected_trace_id:
            raise StorydexError(
                "The latest execution changed before replacement was confirmed.",
                code="stale_trace",
                status_code=409,
                details={"expectedTraceId": self.expected_trace_id, "latestTraceId": latest_trace_id},
            )
        if str(latest.get("status") or "").strip() == "running":
            raise StorydexError(
                "A running execution cannot be edited.",
                code="replacement_target_running",
                status_code=409,
            )

        record_workspace = str(latest.get("workspaceRoot") or "").strip()
        if record_workspace:
            try:
                if Path(record_workspace).resolve() != self.workspace_root:
                    raise StorydexError(
                        "The replacement target belongs to another workspace.",
                        code="replacement_workspace_mismatch",
                        status_code=409,
                    )
            except OSError as exc:
                raise StorydexError(
                    "The replacement target workspace is unavailable.",
                    code="replacement_workspace_mismatch",
                    status_code=409,
                ) from exc

        self.original_record = copy.deepcopy(latest)
        coomi_service = get_storydex_coomi_agent_service()
        self.session_snapshot = coomi_service.snapshot_session_history(
            self.session_id,
            workspace_root=self.workspace_root,
        )
        pending_record = copy.deepcopy(latest)
        pending_record.update(
            {
                "status": "superseded",
                "superseded": True,
                "supersededByTraceId": self.replacement_trace_id,
                "replacement": {
                    "status": "pending",
                    "replacementTraceId": self.replacement_trace_id,
                    "expectedTraceId": latest_trace_id,
                    "replacementPrompt": self.replacement_prompt,
                    "preparedAt": _now_iso(),
                    "dialogueOnly": True,
                    "fileChangesReverted": False,
                },
                "updatedAt": _now_iso(),
            }
        )
        _persist_execution_trace(self.workspace_root, pending_record, self.session_id)
        try:
            rollback = coomi_service.rollback_last_turn(
                self.session_id,
                workspace_root=self.workspace_root,
            )
            if bool((self.session_snapshot or {}).get("available")) and not bool(rollback.get("rolledBack")):
                raise StorydexError(
                    "Unable to withdraw the latest Coomi turn for replacement.",
                    code="replacement_context_unavailable",
                    status_code=409,
                )
        except Exception:
            self.restore(reason="prepare_failed")
            raise
        storydex_intent_service.clear_session(session_id=self.session_id, workspace_root=self.workspace_root)
        self.prepared = True

    def accept(self) -> None:
        if not self.prepared or self.accepted or self.original_record is None:
            return
        superseded_record = copy.deepcopy(self.original_record)
        superseded_record.update(
            {
                "status": "superseded",
                "superseded": True,
                "supersededByTraceId": self.replacement_trace_id,
                "replacement": {
                    "status": "accepted",
                    "replacementTraceId": self.replacement_trace_id,
                    "expectedTraceId": str(self.original_record.get("traceId") or ""),
                    "acceptedAt": _now_iso(),
                    "dialogueOnly": True,
                    "fileChangesReverted": False,
                },
                "updatedAt": _now_iso(),
            }
        )
        _persist_execution_trace(self.workspace_root, superseded_record, self.session_id)
        self.accepted = True

    def restore(self, *, reason: str) -> None:
        if self.restored or self.accepted or self.original_record is None:
            return
        try:
            if self.session_snapshot is not None:
                get_storydex_coomi_agent_service().restore_session_history(self.session_snapshot)
        finally:
            restored_record = copy.deepcopy(self.original_record)
            restored_record.update(
                {
                    "superseded": False,
                    "supersededByTraceId": "",
                    "replacement": {
                        "status": "restored",
                        "replacementTraceId": self.replacement_trace_id,
                        "restoredAt": _now_iso(),
                        "reason": str(reason or "replacement_failed"),
                        "dialogueOnly": True,
                        "fileChangesReverted": False,
                    },
                    "updatedAt": _now_iso(),
                }
            )
            _persist_execution_trace(self.workspace_root, restored_record, self.session_id)
            storydex_intent_service.clear_session(session_id=self.session_id, workspace_root=self.workspace_root)
            self.restored = True


def _resolve_agent_trace_id(request: Request, fallback_trace_id: str = "") -> str:
    return request.headers.get("x-trace-id") or fallback_trace_id or str(uuid4())


def _resolve_agent_session_id(request: Request) -> str:
    return request.headers.get("x-session-id") or "default"


def _resolve_agent_workspace_root(payload: AgentChatRequest) -> Path:
    raw_root = str(payload.workspace_root or "").strip()
    if raw_root:
        candidate = Path(raw_root).expanduser()
        if candidate.exists() and candidate.is_dir():
            resolved = candidate.resolve()
            if resolved != project_service.workspace_root:
                project_service.open_project(resolved.as_posix())
            return resolved
    return project_service.workspace_root


def _resolve_followup_workspace_root(*, session_id: str, workspace_root: str = "") -> Path:
    active = execution_coordinator.active_handle(session_id=str(session_id or "").strip())
    if active is not None:
        return active.workspace_root
    raw_root = str(workspace_root or "").strip()
    if raw_root:
        candidate = Path(raw_root).expanduser()
        if candidate.exists() and candidate.is_dir():
            return candidate.resolve()
    return project_service.workspace_root


def _raise_followup_error(exc: FollowupMailboxError) -> None:
    status_code = 404 if exc.code == "followup_not_found" else 409 if exc.code in {
        "message_id_conflict",
        "followup_not_editable",
        "invalid_followup_transition",
        "followup_dispatch_in_progress",
        "followup_mailbox_paused",
        "stale_trace",
        "no_active_execution",
    } else 400
    raise StorydexError(
        str(exc),
        code=exc.code,
        status_code=status_code,
        details=exc.details,
    ) from exc


def _latest_session_trace_id(session_id: str) -> str:
    records = trace_history_service.list_records(session_id=str(session_id or "default"), limit=1)
    latest = records[0] if records else None
    return str(latest.get("traceId") or "").strip() if isinstance(latest, dict) else ""


def _claim_initial_followup_dispatch(
    *,
    payload: AgentChatRequest,
    workspace_root: Path,
    session_id: str,
    trace_id: str,
) -> tuple[AgentChatRequest, Dict[str, Any] | None]:
    message_id = str(payload.source_followup_message_id or "").strip()
    if not message_id:
        return payload, None
    if payload.replace_latest_trace_id:
        raise StorydexError(
            "A replacement request cannot also dispatch a queued follow-up.",
            code="invalid_followup_transition",
            status_code=409,
        )
    if payload.confirm_no_snapshot:
        state = followup_mailbox_service.list_mailbox(
            workspace_root=workspace_root,
            session_id=session_id,
        )
        if str(state.get("pauseReason") or "") == "snapshot_confirmation":
            followup_mailbox_service.resume(
                workspace_root=workspace_root,
                session_id=session_id,
            )
    previous_trace_id = _latest_session_trace_id(session_id)
    try:
        message = followup_mailbox_service.claim_queued_by_id(
            workspace_root=workspace_root,
            session_id=session_id,
            message_id=message_id,
            previous_trace_id=previous_trace_id,
            next_trace_id=trace_id,
            expected_trace_id=payload.source_followup_expected_trace_id,
        )
    except FollowupMailboxError as exc:
        _raise_followup_error(exc)
    authoritative_payload = payload.model_copy(
        update={
            "prompt": str(message.get("content") or ""),
            "source_followup_message_id": message_id,
        }
    )
    return authoritative_payload, message


def _create_agent_execution_log_session(
    *,
    trace_id: str,
    session_id: str,
) -> ExecutionLogSession | None:
    try:
        return create_execution_log_session(
            trace_id=trace_id,
            session_id=session_id,
            request_kind="agent_chat",
            metadata={"runtime": "coomi"},
        )
    except OSError as exc:
        _LOGGER.warning("Unable to create context Trace execution log for %s: %s", trace_id, exc)
        return None


def _normalize_story_generation_options(value: Dict[str, Any] | None) -> Dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    fragment_count = _positive_int(
        payload.get("fragmentCount", payload.get("fragment_count", payload.get("segmentCount"))),
        default=1,
    )
    fragment_word_count = _bounded_int(
        payload.get("fragmentWordCount", payload.get("fragment_word_count", payload.get("segmentWords"))),
        default=2000,
        minimum=100,
        maximum=20000,
    )
    chapter_template_id = str(
        payload.get(
            "chapterTemplateId",
            payload.get(
                "chapter_template_id",
                payload.get("chapterTemplate", payload.get("chapter_template", "")),
            ),
        )
        or ""
    ).strip()
    return {
        "fragmentCount": fragment_count,
        "fragmentWordCount": fragment_word_count,
        "chapterTemplateId": chapter_template_id,
    }


def _apply_turn_contract_story_generation_defaults(
    story_generation: Dict[str, Any],
    turn_contract: Dict[str, Any],
) -> Dict[str, Any]:
    turn_plan = turn_contract.get("turnPlan") if isinstance(turn_contract.get("turnPlan"), dict) else {}
    selected_template = str(turn_plan.get("selectedChapterTemplate") or "").strip()
    if not selected_template:
        return story_generation

    next_story_generation = dict(story_generation)
    next_story_generation["fragmentCount"] = _positive_int(turn_plan.get("fragmentCount"), default=1)
    next_story_generation["fragmentWordCount"] = _bounded_int(
        turn_plan.get("fragmentWordCount"),
        default=2000,
        minimum=100,
        maximum=20000,
    )
    next_story_generation["chapterTemplateId"] = selected_template
    next_story_generation["chapterTemplate"] = selected_template
    return next_story_generation


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, parsed)


def _try_acquire_agent_generation_slot() -> bool:
    return execution_coordinator.try_reserve()


def _release_agent_generation_slot() -> None:
    execution_coordinator.release_reservation()


def _agent_commit_prompt_enabled(workspace_root: Path) -> bool:
    try:
        settings = story_project_service.read_project_settings(workspace_root)
    except Exception:
        return True
    return bool(settings.get("agentCommitPromptEnabled", True))


def _git_event_name(payload: Dict[str, Any]) -> str:
    event_name = str(payload.get("_type") or "GitAutoCommit").strip()
    if event_name in {"GitAutoCommit", "GitCommitPrompt", "GitCommitResult"}:
        return event_name
    return "GitAutoCommit"


def _agent_busy_error(*, trace_id: str, session_id: str) -> StorydexError:
    return StorydexError(
        "Coomi Agent is already running. Wait for the current generation to finish before starting another.",
        code="agent_busy",
        status_code=409,
        details={"traceId": trace_id, "sessionId": session_id, "runtime": "coomi"},
    )


def _encode_sse(event_name: str, payload: Dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _turn_phase_packet(
    *,
    trace_id: str,
    session_id: str,
    phase: str,
    label: str,
    status: str,
    phase_started: float,
    detail: str = "",
    heartbeat: bool = False,
) -> Dict[str, Any]:
    elapsed_ms = max(0, int((time.perf_counter() - phase_started) * 1000))
    return {
        "_type": "TurnPhase",
        "_version": 1,
        "traceId": trace_id,
        "sessionId": session_id,
        "phase": phase,
        "label": label,
        "detail": detail or label,
        "status": status,
        "startedAt": (datetime.now(timezone.utc) - timedelta(milliseconds=elapsed_ms)).isoformat(),
        "elapsedMs": elapsed_ms,
        "heartbeat": heartbeat,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _phase_for_event(event_name: str) -> str:
    if event_name.startswith("Tool"):
        return "tool"
    if event_name in {"TextChunk", "ReasoningChunk", "ConnectionRetry"}:
        return "model"
    if event_name in {"GitAutoCommit", "GitCommitPrompt", "GitCommitResult"}:
        return "version_control"
    if event_name.startswith("Task"):
        return "planning"
    if event_name in {"TurnContract", "StoryGenerationValidation"}:
        return "orchestration"
    if event_name in {"RunAccepted", "UsageUpdate", "CompressionEvent", "TurnPhase"}:
        return "runtime"
    if event_name.startswith("Agent"):
        return "agent"
    return "runtime"


def _status_for_event(event_name: str, payload: Dict[str, Any]) -> str:
    if event_name == "AgentError" or bool(payload.get("is_error")):
        return "error"
    if event_name == "TaskStarted":
        return "running"
    if event_name == "TaskCompleted":
        return "success"
    if event_name == "TaskFailed":
        return "error"
    if event_name == "TaskSkipped":
        return "warning"
    if event_name in {"TaskPlanCreated", "TaskPlanUpdated"}:
        return "success"
    if event_name in {"GitAutoCommit", "GitCommitPrompt", "GitCommitResult"}:
        return str(payload.get("status") or ("success" if payload.get("created") else "info"))
    if event_name == "TurnContract":
        return "warning" if str(payload.get("status") or "") == "needs_user_input" else "info"
    if event_name == "StoryGenerationValidation":
        return "success" if bool(payload.get("passed")) else "error"
    if event_name == "RunAccepted":
        return "running"
    if event_name == "ConnectionRetry":
        return "warning"
    if event_name in {"AgentCompleted", "ToolDone"}:
        return "success"
    if event_name == "AgentCancelled":
        return "warning"
    return str(payload.get("status") or "info")


def _detail_for_event(event_name: str, payload: Dict[str, Any]) -> str:
    if event_name.startswith("Task"):
        return str(payload.get("title") or payload.get("detail") or event_name)
    if event_name.startswith("Tool"):
        return str(payload.get("tool_name") or event_name)
    if event_name in {"TextChunk", "ReasoningChunk"}:
        return str(payload.get("content") or "")[:240]
    if event_name == "ConnectionRetry":
        attempt = int(payload.get("attempt") or 1)
        max_attempts = int(payload.get("maxAttempts") or payload.get("max_attempts") or attempt)
        message = str(payload.get("message") or "Model connection interrupted; retrying.")
        return f"{message} ({attempt}/{max_attempts})"
    if event_name in {"GitAutoCommit", "GitCommitPrompt", "GitCommitResult"}:
        commit = payload.get("commit") if isinstance(payload.get("commit"), dict) else {}
        subject = str(commit.get("subject") or "").strip()
        if subject:
            return subject
        return str(payload.get("message") or payload.get("reason") or event_name)
    if event_name == "AgentError":
        return str(payload.get("message") or "Coomi Agent error")
    if event_name in {"RunAccepted", "TurnPhase"}:
        return str(payload.get("detail") or payload.get("label") or event_name)
    if event_name == "TurnContract":
        turn_plan = payload.get("turnPlan") if isinstance(payload.get("turnPlan"), dict) else {}
        intent = payload.get("intentFrame") if isinstance(payload.get("intentFrame"), dict) else {}
        if turn_plan.get("requiresChapterTemplateSelection"):
            return "全新故事需要先选择章节目录模板"
        return str(intent.get("primary") or "Storydex turn contract")
    if event_name == "StoryGenerationValidation":
        return str(payload.get("message") or "Storydex 正文客观验收")
    return event_name


_TEXT_TOOL_TAG_NAMES = (
    "read",
    "read_file",
    "readfile",
    "glob",
    "grep",
    "bash",
    "powershell",
    "web_search",
    "websearch",
    "web_fetch",
    "webfetch",
    "write",
    "edit",
    "todo",
    "todowrite",
    "todo_write",
    "ask_user",
    "ask_user_question",
    "askuserquestion",
    "enter_plan_mode",
    "enterplanmode",
    "exit_plan_mode",
    "exitplanmode",
)
_TEXT_TOOL_TAG_PATTERN = "|".join(_TEXT_TOOL_TAG_NAMES)
_TEXT_TOOL_BLOCK_RE = re.compile(
    rf"<\s*({_TEXT_TOOL_TAG_PATTERN})\b[^>]*>.*?</\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TEXT_TOOL_TAG_LINE_RE = re.compile(
    rf"^\s*</?\s*({_TEXT_TOOL_TAG_PATTERN})\b[^>]*>\s*$",
    re.IGNORECASE,
)
_TEXT_TOOL_PARAM_LINE_RE = re.compile(
    r"^\s*<\s*(path|pattern|file_path|command|query|url|prompt|offset|limit|content|old_string|new_string|todos)\b[^>]*>.*?</\s*\1\s*>\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _strip_visible_tool_text(content: str) -> str:
    text = str(content or "")
    text = _strip_textual_tool_blocks(text)
    if "DSML" not in text and "dsml" not in text:
        return text
    kept: list[str] = []
    for line in text.splitlines(keepends=True):
        compact = "".join(line.casefold().split())
        if "dsml" in compact and (
            "tool_calls" in compact
            or "tool_call" in compact
            or "invoke" in compact
            or "parameter" in compact
            or compact.startswith("<||dsml")
            or compact.startswith("&lt;||dsml")
        ):
            continue
        kept.append(line)
    cleaned = "".join(kept)
    compact_cleaned = "".join(cleaned.casefold().split())
    if "dsml" in compact_cleaned and (
        "tool_calls" in compact_cleaned
        or "tool_call" in compact_cleaned
        or "invoke" in compact_cleaned
        or "parameter" in compact_cleaned
    ):
        return ""
    return cleaned


def _strip_textual_tool_blocks(text: str) -> str:
    if not text:
        return ""
    cleaned = _TEXT_TOOL_BLOCK_RE.sub("", text)
    if cleaned == text and not _looks_like_tool_xml_fragment(text):
        return text
    kept: list[str] = []
    for line in cleaned.splitlines(keepends=True):
        if _TEXT_TOOL_TAG_LINE_RE.match(line) or _TEXT_TOOL_PARAM_LINE_RE.match(line):
            continue
        kept.append(line)
    return "".join(kept)


def _looks_like_tool_xml_fragment(text: str) -> bool:
    return bool(_TEXT_TOOL_TAG_LINE_RE.search(text) or _TEXT_TOOL_PARAM_LINE_RE.search(text))


def _event_to_trace_event(event_name: str, payload: Dict[str, Any], index: int) -> Dict[str, Any]:
    return {
        "index": index,
        "event": event_name,
        "phase": _phase_for_event(event_name),
        "status": _status_for_event(event_name, payload),
        "detail": _detail_for_event(event_name, payload),
        "timestamp": _now_iso(),
        "data": payload,
    }


def _extract_trace_metrics(
    events: List[Dict[str, Any]],
    trace_id: str,
    duration_ms: int,
    llm_metrics: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    tool_calls = len([item for item in events if item.get("event") == "ToolDone"])
    total_tokens = 0
    for item in reversed(events):
        if item.get("event") != "AgentCompleted":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        total_tokens = int(data.get("total_tokens") or data.get("totalTokens") or 0)
        break
    observed = llm_metrics if isinstance(llm_metrics, dict) else {}
    observed_calls = int(observed.get("calls") or 0)
    usage_calls = int(observed.get("usageCalls") or 0)
    return {
        "traceId": trace_id,
        "durationMs": duration_ms,
        "toolCalls": tool_calls,
        "llmCalls": observed_calls or (1 if total_tokens else 0),
        "promptTokens": int(observed.get("promptTokens") or 0) if usage_calls else 0,
        "completionTokens": int(observed.get("completionTokens") or 0) if usage_calls else total_tokens,
        "estimatedCost": 0.0,
    }


def _extract_context_trace(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    for item in reversed(events):
        if item.get("event") != "TurnContract":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        context_assembly = data.get("contextAssembly") if isinstance(data.get("contextAssembly"), dict) else {}
        context_trace = context_assembly.get("contextTrace")
        if isinstance(context_trace, dict):
            return context_trace
    return {}


def _build_audit(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    audit: List[Dict[str, Any]] = []
    for item in events:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        if item.get("event") == "ToolDone":
            audit.append(
                {
                    "action": "coomi_tool_call",
                    "toolName": str(data.get("tool_name") or ""),
                    "toolCallId": str(data.get("tool_call_id") or ""),
                    "isError": bool(data.get("is_error")),
                    "durationMs": int(data.get("duration_ms") or 0),
                    "resultPreview": str(data.get("result_preview") or "")[:2000],
                }
            )
        elif item.get("event") in {"GitAutoCommit", "GitCommitPrompt", "GitCommitResult"}:
            audit.append(
                {
                    "action": "agent_git_commit",
                    "event": str(item.get("event") or ""),
                    "created": bool(data.get("created")),
                    "reason": str(data.get("reason") or ""),
                    "target": str(data.get("target") or ""),
                    "workspaceRoot": str(data.get("workspaceRoot") or ""),
                    "commitHash": str(data.get("commitHash") or ""),
                    "changedFileCount": int(data.get("changedFileCount") or 0),
                    "added": int(data.get("added") or 0),
                    "removed": int(data.get("removed") or 0),
                }
            )
        elif item.get("event") == "TurnContract":
            intent = data.get("intentFrame") if isinstance(data.get("intentFrame"), dict) else {}
            turn_plan = data.get("turnPlan") if isinstance(data.get("turnPlan"), dict) else {}
            skill_registry = data.get("skillRegistry") if isinstance(data.get("skillRegistry"), dict) else {}
            tool_registry = data.get("toolRegistry") if isinstance(data.get("toolRegistry"), dict) else {}
            context_assembly = data.get("contextAssembly") if isinstance(data.get("contextAssembly"), dict) else {}
            context_budget = context_assembly.get("budget") if isinstance(context_assembly.get("budget"), dict) else {}
            audit.append(
                {
                    "action": "storydex_turn_contract",
                    "status": str(data.get("status") or ""),
                    "intent": str(intent.get("primary") or ""),
                    "requiresChapterTemplateSelection": bool(turn_plan.get("requiresChapterTemplateSelection")),
                    "fragmentCount": int(turn_plan.get("fragmentCount") or 0),
                    "fragmentWordCount": int(turn_plan.get("fragmentWordCount") or 0),
                    "skillCount": int(skill_registry.get("skillCount") or 0),
                    "toolCount": int(tool_registry.get("toolCount") or 0),
                    "contextBlockCount": int(context_budget.get("blockCount") or 0),
                    "contextTotalChars": int(context_budget.get("totalChars") or 0),
                }
            )
        elif item.get("event") == "StoryGenerationValidation":
            fragments = data.get("fragments") if isinstance(data.get("fragments"), list) else []
            audit.append(
                {
                    "action": "story_generation_validation",
                    "version": int(data.get("_version") or 1),
                    "passed": bool(data.get("passed")),
                    "algorithm": str(data.get("algorithm") or ""),
                    "exact": bool(data.get("exact")),
                    "fragmentCount": int(data.get("fragmentCount") or len(fragments)),
                    "targetWordCount": int(data.get("targetWordCount") or 0),
                    "chapterContentMode": str(data.get("chapterContentMode") or ""),
                    "structurePassed": bool(data.get("structurePassed")),
                    "writeToolApplied": bool(data.get("writeToolApplied")),
                    "correctionAttempt": int(data.get("correctionAttempt") or 0),
                    "fragments": fragments,
                }
            )
        elif item.get("event") in {
            "FollowupQueued",
            "FollowupUpdated",
            "SteerRequested",
            "SteerApplied",
            "ContinuationStarted",
        }:
            audit.append(
                {
                    "action": "agent_followup",
                    "event": str(item.get("event") or ""),
                    "version": int(data.get("_version") or 1),
                    "messageId": str(data.get("messageId") or ""),
                    "sessionId": str(data.get("sessionId") or ""),
                    "activeTraceId": str(data.get("activeTraceId") or ""),
                    "traceId": str(data.get("traceId") or ""),
                    "mode": str(data.get("mode") or "queued"),
                    "status": str(data.get("status") or "pending"),
                    "segmentId": str(data.get("segmentId") or ""),
                }
            )
    return audit


def _build_chat_payload(
    *,
    trace_id: str,
    prompt: str,
    reply: str,
    events: List[Dict[str, Any]],
    started: float,
    workspace_root: Path,
    session_id: str = "default",
    execution_log_session: ExecutionLogSession | None = None,
    status: str = "completed",
    error_message: str = "",
) -> Dict[str, Any]:
    status_data = get_storydex_coomi_agent_service().get_status(workspace_root=workspace_root)
    duration_ms = int((time.perf_counter() - started) * 1000)
    llm_metrics = get_llm_metrics(trace_id)
    trace = _extract_trace_metrics(events, trace_id, duration_ms, llm_metrics)
    context_trace = merge_llm_metrics(_extract_context_trace(events), llm_metrics)
    audit = _build_audit(events)
    data = AgentChatData(
        route="coomi",
        reply=reply,
        llmModel=str(status_data.get("model") or ""),
        llmProvider=str(status_data.get("providerId") or ""),
        events=[AgentTraceEvent(**event) for event in events],
        assistant={"runtime": "coomi", "status": status_data},
    ).model_dump(by_alias=True)
    record = _build_history_record(
        trace_id=trace_id,
        prompt=prompt,
        data=data,
        trace=trace,
        audit=audit,
        events=events,
        workspace_root=workspace_root,
        status=status,
        error_message=error_message,
        context_trace=copy.deepcopy(context_trace),
    )
    if execution_log_session is not None:
        try:
            execution_log_session.write(
                "context_trace_summary",
                summarize_context_trace(context_trace),
                category="observability",
            )
        except OSError as exc:
            _LOGGER.warning("Unable to write context Trace execution log for %s: %s", trace_id, exc)
    return {
        "data": data,
        "trace": trace,
        "audit": audit,
        "record": record,
    }


def _build_history_record(
    *,
    trace_id: str,
    prompt: str,
    data: Dict[str, Any],
    trace: Dict[str, Any],
    audit: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
    workspace_root: Path,
    status: str,
    error_message: str = "",
    context_trace: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    now = _now_iso()
    return {
        "traceId": trace_id,
        "prompt": prompt,
        "route": "coomi",
        "agentMode": "coomi",
        "status": status,
        "createdAt": now,
        "updatedAt": now,
        "lastAction": "chat",
        "reply": str(data.get("reply") or ""),
        "llmModel": str(data.get("llmModel") or ""),
        "llmProvider": str(data.get("llmProvider") or ""),
        "events": events,
        "tasks": _extract_task_plan(events, trace_id),
        "changeLedger": _extract_change_ledger(events, trace_id=trace_id, session_id=""),
        "trace": trace,
        "audit": audit,
        "assistant": data.get("assistant") if isinstance(data.get("assistant"), dict) else {},
        "contextTrace": context_trace if isinstance(context_trace, dict) else {},
        "workspaceRoot": workspace_root.as_posix(),
        "errorMessage": error_message,
        "errorCode": "coomi_agent_error" if error_message else None,
    }


def _persist_execution_trace(workspace_root: Path, record: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    """Persist a final trace against the execution workspace, atomically when available."""
    writer = getattr(trace_history_service, "upsert_record_atomic_at_storydex_root", None)
    if callable(writer):
        return writer(workspace_root / ".storydex", record, session_id)
    atomic_writer = getattr(trace_history_service, "upsert_record_atomic", None)
    if callable(atomic_writer):
        return atomic_writer(record, session_id)
    return trace_history_service.upsert_record(record, session_id)


def _extract_task_plan(events: List[Dict[str, Any]], trace_id: str) -> List[Dict[str, Any]]:
    tasks: List[Dict[str, Any]] = []
    for item in events:
        event_name = str(item.get("event") or "")
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        if event_name in {"TaskPlanCreated", "TaskPlanUpdated"}:
            tasks = _normalize_task_plan(data.get("tasks"), trace_id=trace_id)
            continue
        if event_name not in {"TaskStarted", "TaskCompleted", "TaskFailed", "TaskSkipped"}:
            continue
        task_id = str(data.get("taskId") or "").strip()
        if not task_id:
            continue
        order = int(data.get("order") or len(tasks) + 1)
        existing = next((task for task in tasks if str(task.get("taskId") or "") == task_id), None)
        task = {
            "taskId": task_id,
            "traceId": str(data.get("traceId") or trace_id),
            "order": order,
            "title": str(data.get("title") or (existing or {}).get("title") or f"Task {order}"),
            "detail": str(data.get("detail") or (existing or {}).get("detail") or ""),
            "status": _normalize_task_status(data.get("status")),
            "createdAt": str(data.get("createdAt") or (existing or {}).get("createdAt") or _now_iso()),
            "updatedAt": str(data.get("updatedAt") or _now_iso()),
        }
        tasks = [item for item in tasks if str(item.get("taskId") or "") != task_id]
        tasks.append(task)
    return sorted(tasks[:10], key=lambda task: int(task.get("order") or 0))


def _extract_change_ledger(
    events: List[Dict[str, Any]],
    *,
    trace_id: str,
    session_id: str = "",
) -> Dict[str, Any]:
    ledger = {
        "traceId": trace_id,
        "sessionId": session_id,
        "changedFiles": [],
        "changedFileCount": 0,
        "added": 0,
        "removed": 0,
        "diffSource": "",
        "commitHash": "",
        "shortHash": "",
        "updatedAt": "",
    }
    for item in events:
        if item.get("event") not in {"GitAutoCommit", "GitCommitPrompt", "GitCommitResult"}:
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        changed_files = [
            str(path).replace("\\", "/").strip()
            for path in (data.get("changedFiles") if isinstance(data.get("changedFiles"), list) else [])
            if str(path).strip()
        ]
        commit_hash = str(data.get("commitHash") or "").strip()
        diff_source = str(data.get("diffSource") or ("commit" if commit_hash else "working_tree" if changed_files else "")).strip()
        ledger = {
            "traceId": str(data.get("traceId") or trace_id),
            "sessionId": str(data.get("sessionId") or data.get("session_id") or session_id),
            "changedFiles": changed_files,
            "changedFileCount": int(data.get("changedFileCount") or len(changed_files)),
            "added": int(data.get("added") or 0),
            "removed": int(data.get("removed") or 0),
            "diffSource": diff_source if diff_source in {"working_tree", "commit"} else "",
            "commitHash": commit_hash,
            "shortHash": str(data.get("shortHash") or "").strip(),
            "updatedAt": str(data.get("updatedAt") or item.get("timestamp") or _now_iso()),
        }
    return ledger


def _turn_contract_needs_user_input(turn_contract: Dict[str, Any]) -> bool:
    return str((turn_contract or {}).get("status") or "").strip() == "needs_user_input"


def _turn_contract_user_input_message(turn_contract: Dict[str, Any]) -> str:
    questions = turn_contract.get("requiredQuestions") if isinstance(turn_contract.get("requiredQuestions"), list) else []
    for item in questions:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        if message:
            return message
    return "Storydex 需要补充信息后才能继续执行。"


def _turn_contract_waiting_packet(turn_contract: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "_type": "AgentCompleted",
        "_version": 1,
        "status": "needs_user_input",
        "message": _turn_contract_user_input_message(turn_contract),
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


def _story_generation_correction_prompt(
    validation: Dict[str, Any],
    *,
    correction_attempt: int,
) -> str:
    fragments = validation.get("fragments") if isinstance(validation.get("fragments"), list) else []
    failures = [
        {
            "order": int(item.get("order") or index + 1),
            "path": str(item.get("path") or ""),
            "exists": bool(item.get("exists")),
            "writeMode": str(item.get("writeMode") or "replace"),
            "baselineWordCount": int(item.get("baselineWordCount") or 0),
            "generatedWordCount": int(item.get("generatedWordCount") or 0),
            "targetWordCount": int(item.get("targetWordCount") or 0),
            "difference": int(item.get("difference") or 0),
        }
        for index, item in enumerate(fragments)
        if isinstance(item, dict) and str(item.get("status") or "") != "passed"
    ]
    correction = {
        "correctionAttempt": correction_attempt,
        "maximumCorrectionAttempts": _STORY_GENERATION_MAX_CORRECTIONS,
        "algorithm": str(validation.get("algorithm") or "storydex_visible_characters_v1"),
        "countingRule": str(validation.get("countingRule") or "count every non-whitespace Unicode character"),
        "exact": True,
        "chapterContentMode": str(validation.get("chapterContentMode") or ""),
        "structurePassed": bool(validation.get("structurePassed")),
        "writeToolApplied": bool(validation.get("writeToolApplied")),
        "failures": failures,
    }
    return (
        "Storydex 的落盘后客观验收未通过。不要自行估算字数，也不要宣布完成。"
        "请依据下方校验结果修订全部失败片段，并再次调用 StorydexApplyStoryIncrement。"
        "每个片段必须按 Storydex 内置规则（忽略所有空白后逐个 Unicode 字符计数）精确达到目标字数；"
        "章节路径、文件数量和写入模式必须完全遵守当前 TurnContract。"
        "禁止使用普通 Write/Edit 工具写 chapters/ 正文。\n"
        f"STORYDEX_OBJECTIVE_VALIDATION={json.dumps(correction, ensure_ascii=False, separators=(',', ':'))}"
    )


def _has_successful_story_generation_write(events: List[Dict[str, Any]]) -> bool:
    for item in events:
        if item.get("event") != "ToolDone":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        tool_name = str(data.get("tool_name") or data.get("toolName") or "").strip().lower()
        if tool_name == "storydexapplystoryincrement" and not bool(data.get("is_error")):
            return True
    return False


async def _create_agent_task_plan(
    *,
    prompt: str,
    trace_id: str,
    session_id: str,
    workspace_root: Path,
    active_file: str,
    story_generation: Dict[str, Any],
    turn_contract: Dict[str, Any],
) -> List[Dict[str, Any]]:
    intent_frame = turn_contract.get("intentFrame") if isinstance(turn_contract.get("intentFrame"), dict) else {}
    if intent_frame and str(intent_frame.get("primary") or "general").strip() == "general":
        return []
    planner = getattr(get_storydex_coomi_agent_service(), "create_task_plan", None)
    if callable(planner):
        try:
            tasks = await asyncio.wait_for(
                planner(
                    prompt=prompt,
                    trace_id=trace_id,
                    session_id=session_id,
                    workspace_root=workspace_root,
                    active_file=active_file,
                    story_generation=story_generation,
                    turn_contract=turn_contract,
                ),
                timeout=_PLANNER_TIMEOUT_SECONDS,
            )
            normalized = _normalize_task_plan(tasks, trace_id=trace_id)
            if normalized:
                return normalized
        except Exception:
            pass
    return []


def _normalize_task_plan(value: Any, *, trace_id: str) -> List[Dict[str, Any]]:
    raw_tasks = value if isinstance(value, list) else []
    now = _now_iso()
    tasks: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_tasks[:10]):
        record = item if isinstance(item, dict) else {"title": str(item or "")}
        title = str(record.get("title") or record.get("name") or record.get("task") or "").strip()
        if not title or _is_generic_route_task_title(title):
            continue
        tasks.append(
            {
                "taskId": str(record.get("taskId") or record.get("id") or f"{trace_id}-task-{len(tasks) + 1}"),
                "traceId": str(record.get("traceId") or trace_id),
                "order": len(tasks) + 1,
                "title": title[:80],
                "detail": str(record.get("detail") or record.get("description") or "").strip()[:240],
                "status": _normalize_task_status(record.get("status")),
                "createdAt": str(record.get("createdAt") or now),
                "updatedAt": str(record.get("updatedAt") or record.get("createdAt") or now),
            }
        )
    return tasks[:10]


def _is_generic_route_task_title(title: str) -> bool:
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


def _normalize_task_status(value: Any) -> str:
    normalized = str(value or "pending").strip().lower()
    if normalized in {"pending", "running", "completed", "failed", "skipped"}:
        return normalized
    if normalized == "success":
        return "completed"
    if normalized == "error":
        return "failed"
    return "pending"


def _is_version_task(task: Dict[str, Any]) -> bool:
    title = str(task.get("title") or "").casefold()
    detail = str(task.get("detail") or "").casefold()
    text = f"{title} {detail}"
    return any(token in text for token in ("git", "commit", "版本", "提交", "记录"))


class _TaskRunTracker:
    def __init__(self, tasks: List[Dict[str, Any]], *, trace_id: str, session_id: str) -> None:
        self.tasks = [dict(item) for item in _normalize_task_plan(tasks, trace_id=trace_id)]
        self.trace_id = trace_id
        self.session_id = session_id
        self.current_index = -1
        self.version_index = self._resolve_version_index()

    def plan_created_payload(self) -> Dict[str, Any]:
        return {
            "_type": "TaskPlanCreated",
            "_version": 1,
            "traceId": self.trace_id,
            "sessionId": self.session_id,
            "tasks": [dict(item) for item in self.tasks],
            "createdAt": _now_iso(),
            "updatedAt": _now_iso(),
        }

    def start_next(self, *, before_version: bool = True) -> List[tuple[str, Dict[str, Any]]]:
        next_index = self.current_index + 1
        if next_index >= len(self.tasks):
            return []
        if before_version and self.version_index >= 0 and next_index >= self.version_index:
            return []
        self.current_index = next_index
        return [("TaskStarted", self._task_event_payload(next_index, "TaskStarted", "running"))]

    def complete_current(self) -> List[tuple[str, Dict[str, Any]]]:
        if self.current_index < 0 or self.current_index >= len(self.tasks):
            return []
        if self.tasks[self.current_index].get("status") not in {"running", "pending"}:
            return []
        return [("TaskCompleted", self._task_event_payload(self.current_index, "TaskCompleted", "completed"))]

    def fail_current(self, message: str = "") -> List[tuple[str, Dict[str, Any]]]:
        if self.current_index < 0 or self.current_index >= len(self.tasks):
            return []
        return [("TaskFailed", self._task_event_payload(self.current_index, "TaskFailed", "failed", message=message))]

    def skip_remaining_execution(self, reason: str = "") -> List[tuple[str, Dict[str, Any]]]:
        events: List[tuple[str, Dict[str, Any]]] = []
        start_index = max(0, self.current_index + 1)
        for index in range(start_index, self._execution_limit()):
            if self.tasks[index].get("status") in {"completed", "failed", "skipped"}:
                continue
            events.append(("TaskSkipped", self._task_event_payload(index, "TaskSkipped", "skipped", message=reason)))
        self.current_index = max(self.current_index, self._execution_limit() - 1)
        return events

    def complete_through_execution(self) -> List[tuple[str, Dict[str, Any]]]:
        events: List[tuple[str, Dict[str, Any]]] = []
        execution_limit = self._execution_limit()
        while True:
            if self.current_index >= 0 and self.current_index < execution_limit:
                events.extend(self.complete_current())
            next_index = self.current_index + 1
            if next_index >= execution_limit:
                break
            events.extend(self.start_next(before_version=True))
        return events

    def advance_after_runtime_event(self, event_name: str) -> List[tuple[str, Dict[str, Any]]]:
        if event_name not in {"ToolDone", "StageOutput"}:
            return []
        events = self.complete_current()
        events.extend(self.start_next(before_version=True))
        return events

    def start_version_task(self) -> List[tuple[str, Dict[str, Any]]]:
        events: List[tuple[str, Dict[str, Any]]] = []
        if self.current_index < self._execution_limit() - 1:
            events.extend(self.complete_through_execution())
        if self.version_index < 0 or self.version_index >= len(self.tasks):
            return events
        if self.current_index != self.version_index:
            self.current_index = self.version_index
            events.append(("TaskStarted", self._task_event_payload(self.version_index, "TaskStarted", "running")))
        return events

    def finish_version_task(self, *, failed: bool, message: str = "") -> List[tuple[str, Dict[str, Any]]]:
        if self.version_index < 0 or self.version_index >= len(self.tasks):
            return []
        self.current_index = self.version_index
        if failed:
            return [("TaskFailed", self._task_event_payload(self.version_index, "TaskFailed", "failed", message=message))]
        return [("TaskCompleted", self._task_event_payload(self.version_index, "TaskCompleted", "completed"))]

    def skip_pending(self, reason: str = "") -> List[tuple[str, Dict[str, Any]]]:
        events: List[tuple[str, Dict[str, Any]]] = []
        for index, task in enumerate(self.tasks):
            if task.get("status") in {"completed", "failed", "skipped"}:
                continue
            events.append(("TaskSkipped", self._task_event_payload(index, "TaskSkipped", "skipped", message=reason)))
        return events

    def _resolve_version_index(self) -> int:
        for index, task in enumerate(self.tasks):
            if _is_version_task(task):
                return index
        return -1

    def _execution_limit(self) -> int:
        if self.version_index < 0:
            return len(self.tasks)
        return min(self.version_index, len(self.tasks))

    def _task_event_payload(self, index: int, event_name: str, status: str, *, message: str = "") -> Dict[str, Any]:
        task = self.tasks[index]
        updated_at = _now_iso()
        task["status"] = status
        task["updatedAt"] = updated_at
        return {
            "_type": event_name,
            "_version": 1,
            "traceId": self.trace_id,
            "sessionId": self.session_id,
            "taskId": str(task.get("taskId") or f"{self.trace_id}-task-{index + 1}"),
            "order": int(task.get("order") or index + 1),
            "title": str(task.get("title") or f"Task {index + 1}"),
            "detail": message or str(task.get("detail") or ""),
            "status": status,
            "createdAt": str(task.get("createdAt") or updated_at),
            "updatedAt": updated_at,
        }


def _append_task_events(events: List[Dict[str, Any]], task_events: List[tuple[str, Dict[str, Any]]]) -> None:
    for event_name, payload in task_events:
        events.append(_event_to_trace_event(event_name, payload, len(events) + 1))


def _yield_task_events(task_events: List[tuple[str, Dict[str, Any]]]) -> List[str]:
    return [_encode_sse(event_name, payload) for event_name, payload in task_events]


async def _collect_coomi_run(
    *,
    prompt: str,
    trace_id: str,
    session_id: str,
    active_file: str,
    workspace_root: Path,
    story_generation: Dict[str, Any],
    turn_contract: Dict[str, Any],
    cancellation_token: _CancellationToken,
    request: Request | None = None,
) -> tuple[str, List[Dict[str, Any]], bool, str]:
    reply_chunks: List[str] = []
    events: List[Dict[str, Any]] = []
    completed = False
    error_message = ""
    async for event_name, payload in get_storydex_coomi_agent_service().stream_events(
        prompt=prompt,
        trace_id=trace_id,
        session_id=session_id,
        workspace_root=workspace_root,
        active_file=active_file,
        story_generation=story_generation,
        turn_contract=turn_contract,
        cancellation_token=cancellation_token,
    ):
        if request is not None and await request.is_disconnected():
            cancellation_token.cancel()
            break
        if event_name == "ReasoningChunk":
            # Provider hidden reasoning is neither user-visible nor trace data.
            continue
        packet = dict(payload)
        if event_name == "TextChunk":
            packet["content"] = _strip_visible_tool_text(str(packet.get("content") or ""))
            if not packet["content"]:
                continue
        events.append(_event_to_trace_event(event_name, packet, len(events) + 1))
        if event_name == "TextChunk":
            reply_chunks.append(str(packet.get("content") or ""))
        elif event_name == "PermissionRequest":
            get_storydex_coomi_agent_service().resolve_approval(str(packet.get("approvalId") or packet.get("approval_id") or ""), "deny")
        elif event_name == "AgentCompleted":
            completed = True
        elif event_name == "AgentError":
            error_message = str(packet.get("message") or "Coomi Agent error")
    return "".join(reply_chunks), events, completed, error_message


async def _stream_coomi_sse_worker(
    *,
    prompt: str,
    trace_id: str,
    session_id: str,
    active_file: str,
    workspace_root: Path,
    story_generation: Dict[str, Any],
    turn_contract: Dict[str, Any],
    git_snapshot: AgentGitSnapshot,
    cancellation_token: _CancellationToken,
    execution_handle: ExecutionHandle,
    execution_log_session: ExecutionLogSession | None = None,
    replacement: _LatestExecutionReplacement | None = None,
) -> AsyncIterator[str]:
    started = time.perf_counter()
    reply_chunks: List[str] = []
    events: List[Dict[str, Any]] = []
    completed = False
    error_message = ""
    tracker: _TaskRunTracker | None = None
    git_finished = False
    runtime_tasks_finalized = False
    terminal_event: tuple[str, Dict[str, Any]] | None = None
    finalization_packets: List[str] = []
    planning_task: asyncio.Task[List[Dict[str, Any]]] | None = None
    planning_started = 0.0

    def finish_git_turn() -> Dict[str, Any]:
        nonlocal git_finished
        if git_finished:
            return {}
        git_finished = True
        return agent_git_autocommit_service.finish_turn(
            git_snapshot,
            prompt=prompt,
            commit_prompt_enabled=_agent_commit_prompt_enabled(workspace_root),
        )

    try:
        try:
            planning_started = time.perf_counter()
            yield _encode_sse(
                "TurnPhase",
                _turn_phase_packet(
                    trace_id=trace_id,
                    session_id=session_id,
                    phase="task_planning",
                    label="正在规划执行步骤",
                    status="running",
                    phase_started=planning_started,
                ),
            )
            intent_frame = turn_contract.get("intentFrame") if isinstance(turn_contract.get("intentFrame"), dict) else {}
            intent_primary = str(intent_frame.get("primary") or "general").strip()
            if intent_primary == "general":
                task_plan: List[Dict[str, Any]] = []
            else:
                planning_task = asyncio.create_task(
                    _create_agent_task_plan(
                        prompt=prompt,
                        trace_id=trace_id,
                        session_id=session_id,
                        workspace_root=workspace_root,
                        active_file=active_file,
                        story_generation=story_generation,
                        turn_contract=turn_contract,
                    ),
                    name=f"storydex-planner-{trace_id}",
                )
                await asyncio.sleep(0)
                task_plan = await planning_task if planning_task.done() else []
                if planning_task.done():
                    planning_task = None
            if intent_primary == "general" or planning_task is None:
                yield _encode_sse(
                    "TurnPhase",
                    _turn_phase_packet(
                        trace_id=trace_id,
                        session_id=session_id,
                        phase="task_planning",
                        label="执行步骤规划完成",
                        status="success",
                        phase_started=planning_started,
                        detail=("无需生成执行步骤" if intent_primary == "general" else f"已生成 {len(task_plan)} 个执行步骤"),
                    ),
                )
                tracker = _TaskRunTracker(task_plan, trace_id=trace_id, session_id=session_id)
                plan_payload = tracker.plan_created_payload()
                events.append(_event_to_trace_event("TaskPlanCreated", plan_payload, len(events) + 1))
                yield _encode_sse("TaskPlanCreated", plan_payload)
                for task_event_name, task_payload in tracker.start_next():
                    events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                    yield _encode_sse(task_event_name, task_payload)

            should_run_coomi = True
            if turn_contract:
                events.append(_event_to_trace_event("TurnContract", turn_contract, len(events) + 1))
                yield _encode_sse("TurnContract", turn_contract)
                if tracker is not None:
                    for task_event_name, task_payload in tracker.complete_current():
                        events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                        yield _encode_sse(task_event_name, task_payload)
                if _turn_contract_needs_user_input(turn_contract):
                    if replacement is not None and not replacement.accepted:
                        await asyncio.to_thread(replacement.accept)
                    followup_mailbox_service.pause(
                        workspace_root=workspace_root,
                        session_id=session_id,
                        reason="needs_user_input",
                    )
                    if tracker is not None:
                        for task_event_name, task_payload in tracker.skip_remaining_execution(reason="needs_user_input"):
                            events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                            yield _encode_sse(task_event_name, task_payload)
                    runtime_tasks_finalized = True
                    packet = _turn_contract_waiting_packet(turn_contract)
                    completed = True
                    reply_chunks.append(str(packet.get("message") or ""))
                    terminal_event = ("AgentCompleted", packet)
                    should_run_coomi = False

            if should_run_coomi and not execution_handle.is_cancelled:
                if tracker is not None:
                    for task_event_name, task_payload in tracker.start_next():
                        events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                        yield _encode_sse(task_event_name, task_payload)
                model_started = time.perf_counter()
                yield _encode_sse(
                    "TurnPhase",
                    _turn_phase_packet(
                        trace_id=trace_id,
                        session_id=session_id,
                        phase="model_execution",
                        label="正在启动模型执行",
                        status="running",
                        phase_started=model_started,
                    ),
                )
                model_output_started = False
                segment_prompt = prompt
                segment_index = 0
                story_correction_attempts = 0
                while not execution_handle.is_cancelled and not cancellation_token.is_cancelled():
                    segment_id = f"{trace_id}-segment-{segment_index + 1}"
                    pending_steer: Dict[str, Any] | None = None
                    segment_completed = False
                    segment_cancelled = False
                    runtime_events = get_storydex_coomi_agent_service().stream_events(
                        prompt=segment_prompt,
                        trace_id=trace_id,
                        session_id=session_id,
                        workspace_root=workspace_root,
                        active_file=active_file,
                        story_generation=story_generation,
                        turn_contract=turn_contract,
                        cancellation_token=cancellation_token,
                    ).__aiter__()
                    try:
                        while True:
                            next_event = asyncio.create_task(runtime_events.__anext__())
                            while not next_event.done():
                                waiters: set[asyncio.Task[Any]] = {next_event}
                                if planning_task is not None:
                                    waiters.add(planning_task)
                                done, _ = await asyncio.wait(
                                    waiters,
                                    timeout=_PHASE_HEARTBEAT_SECONDS,
                                    return_when=asyncio.FIRST_COMPLETED,
                                )
                                if planning_task is not None and planning_task in done:
                                    task_plan = await planning_task
                                    planning_task = None
                                    yield _encode_sse(
                                        "TurnPhase",
                                        _turn_phase_packet(
                                            trace_id=trace_id,
                                            session_id=session_id,
                                            phase="task_planning",
                                            label="执行步骤规划完成",
                                            status="success",
                                            phase_started=planning_started,
                                            detail=f"已生成 {len(task_plan)} 个执行步骤",
                                        ),
                                    )
                                    tracker = _TaskRunTracker(task_plan, trace_id=trace_id, session_id=session_id)
                                    plan_payload = tracker.plan_created_payload()
                                    events.append(_event_to_trace_event("TaskPlanCreated", plan_payload, len(events) + 1))
                                    yield _encode_sse("TaskPlanCreated", plan_payload)
                                    for task_event_name, task_payload in tracker.start_next():
                                        events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                                        yield _encode_sse(task_event_name, task_payload)
                                if next_event in done:
                                    break
                                if planning_task is not None:
                                    yield _encode_sse(
                                        "TurnPhase",
                                        _turn_phase_packet(
                                            trace_id=trace_id,
                                            session_id=session_id,
                                            phase="task_planning",
                                            label="正在后台规划执行步骤",
                                            status="running",
                                            phase_started=planning_started,
                                            heartbeat=True,
                                        ),
                                    )
                                if pending_steer is None and not execution_handle.is_cancelled:
                                    pending_steer = followup_mailbox_service.claim_steer(
                                        workspace_root=workspace_root,
                                        session_id=session_id,
                                        trace_id=trace_id,
                                    )
                                    if pending_steer is not None:
                                        requested = get_storydex_coomi_agent_service().request_steer(
                                            session_id=session_id,
                                            workspace_root=workspace_root,
                                        )
                                        if not requested:
                                            followup_mailbox_service.release_steer_claim(
                                                workspace_root=workspace_root,
                                                session_id=session_id,
                                                message_id=str(pending_steer.get("messageId") or ""),
                                            )
                                            pending_steer = None
                                yield _encode_sse(
                                    "TurnPhase",
                                    _turn_phase_packet(
                                        trace_id=trace_id,
                                        session_id=session_id,
                                        phase="model_execution",
                                        label=("等待安全中断点" if pending_steer is not None else "正在等待模型输出"),
                                        status="running",
                                        phase_started=model_started,
                                        heartbeat=True,
                                    ),
                                )
                            try:
                                event_name, payload = await next_event
                            except StopAsyncIteration:
                                break
                            if event_name == "ReasoningChunk":
                                # Never expose or persist provider chain-of-thought.
                                continue
                            if (
                                replacement is not None
                                and not replacement.accepted
                                and event_name
                                not in {
                                    "AgentStarted",
                                    "UsageUpdate",
                                    "ConnectionRetry",
                                    "AgentError",
                                    "AgentCancelled",
                                }
                            ):
                                # Runtime/provider setup is not considered accepted
                                # until Coomi produces a substantive event.  This
                                # lets an immediate startup failure restore the
                                # original dialogue and session snapshot.
                                await asyncio.to_thread(replacement.accept)
                            if not model_output_started and event_name not in {
                                "AgentStarted",
                                "UsageUpdate",
                                "ConnectionRetry",
                            }:
                                model_output_started = True
                                yield _encode_sse(
                                    "TurnPhase",
                                    _turn_phase_packet(
                                        trace_id=trace_id,
                                        session_id=session_id,
                                        phase="model_execution",
                                        label="模型已开始输出",
                                        status="success",
                                        phase_started=model_started,
                                    ),
                                )
                            packet = dict(payload)
                            if event_name == "TextChunk":
                                packet["content"] = _strip_visible_tool_text(str(packet.get("content") or ""))
                                if not packet["content"]:
                                    continue
                            if event_name == "TextChunk":
                                reply_chunks.append(str(packet.get("content") or ""))
                            elif event_name == "AgentCompleted":
                                segment_completed = True
                                terminal_event = (event_name, packet)
                                continue
                            elif event_name == "AgentCancelled":
                                if pending_steer is None and not execution_handle.is_cancelled:
                                    pending_steer = followup_mailbox_service.claim_steer(
                                        workspace_root=workspace_root,
                                        session_id=session_id,
                                        trace_id=trace_id,
                                    )
                                if pending_steer is not None and not cancellation_token.is_cancelled():
                                    segment_cancelled = True
                                    terminal_event = None
                                    break
                                execution_handle.cancel(str(packet.get("reason") or "coomi_cancelled"))
                                terminal_event = (event_name, packet)
                                continue
                            elif event_name == "PermissionRequest":
                                followup_mailbox_service.pause(
                                    workspace_root=workspace_root,
                                    session_id=session_id,
                                    reason="permission_request",
                                )
                            elif event_name == "AgentError":
                                error_message = str(packet.get("message") or "Coomi Agent error")
                                followup_mailbox_service.pause(
                                    workspace_root=workspace_root,
                                    session_id=session_id,
                                    reason="execution_error",
                                )
                            events.append(_event_to_trace_event(event_name, packet, len(events) + 1))
                            yield _encode_sse(event_name, packet)
                            if tracker is not None:
                                for task_event_name, task_payload in tracker.advance_after_runtime_event(event_name):
                                    events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                                    yield _encode_sse(task_event_name, task_payload)
                    finally:
                        close_runtime = getattr(runtime_events, "aclose", None)
                        if callable(close_runtime):
                            await close_runtime()

                    if pending_steer is None and not execution_handle.is_cancelled and not error_message:
                        # Covers the race where completion and SteerRequested are
                        # committed at the same time.
                        pending_steer = followup_mailbox_service.claim_steer(
                            workspace_root=workspace_root,
                            session_id=session_id,
                            trace_id=trace_id,
                        )
                    if pending_steer is not None and not execution_handle.is_cancelled and not error_message:
                        segment_index += 1
                        next_segment_id = f"{trace_id}-segment-{segment_index + 1}"
                        applied = followup_mailbox_service.apply_steer(
                            workspace_root=workspace_root,
                            session_id=session_id,
                            message_id=str(pending_steer.get("messageId") or ""),
                            trace_id=trace_id,
                            segment_id=next_segment_id,
                        )
                        steer_packet = {
                            "_type": "SteerApplied",
                            "_version": 1,
                            **applied,
                            "traceId": trace_id,
                            "segmentId": next_segment_id,
                            "previousSegmentId": segment_id,
                        }
                        events.append(_event_to_trace_event("SteerApplied", steer_packet, len(events) + 1))
                        yield _encode_sse("SteerApplied", steer_packet)
                        continuation_packet = {
                            "_type": "ContinuationStarted",
                            "_version": 1,
                            **applied,
                            "traceId": trace_id,
                            "segmentId": next_segment_id,
                            "previousSegmentId": segment_id,
                            "continuationMode": "steer",
                        }
                        events.append(_event_to_trace_event("ContinuationStarted", continuation_packet, len(events) + 1))
                        yield _encode_sse("ContinuationStarted", continuation_packet)
                        segment_prompt = str(applied.get("content") or "")
                        completed = False
                        terminal_event = None
                        continue
                    if pending_steer is not None:
                        followup_mailbox_service.release_steer_claim(
                            workspace_root=workspace_root,
                            session_id=session_id,
                            message_id=str(pending_steer.get("messageId") or ""),
                            error="当前执行已停止，信息仍保留在邮箱中",
                        )
                    if (
                        segment_completed
                        and not execution_handle.is_cancelled
                        and not cancellation_token.is_cancelled()
                        and not error_message
                    ):
                        validation_packet = story_project_service.validate_story_generation_turn(
                            workspace_root,
                            turn_contract,
                        )
                        if bool(validation_packet.get("applicable")):
                            write_tool_applied = _has_successful_story_generation_write(events)
                            validation_passed = bool(validation_packet.get("passed")) and write_tool_applied
                            validation_message = str(validation_packet.get("message") or "")
                            if not write_tool_applied:
                                validation_message = (
                                    "本轮没有成功调用 StorydexApplyStoryIncrement，"
                                    "不能把磁盘上的既有正文误判为本轮生成结果。"
                                )
                            validation_packet = {
                                **validation_packet,
                                "passed": validation_passed,
                                "status": "success" if validation_passed else "error",
                                "message": validation_message,
                                "writeToolApplied": write_tool_applied,
                                "traceId": trace_id,
                                "sessionId": session_id,
                                "segmentId": segment_id,
                                "correctionAttempt": story_correction_attempts,
                                "maximumCorrectionAttempts": _STORY_GENERATION_MAX_CORRECTIONS,
                            }
                            events.append(
                                _event_to_trace_event(
                                    "StoryGenerationValidation",
                                    validation_packet,
                                    len(events) + 1,
                                )
                            )
                            yield _encode_sse("StoryGenerationValidation", validation_packet)
                            if not bool(validation_packet.get("passed")):
                                if story_correction_attempts < _STORY_GENERATION_MAX_CORRECTIONS:
                                    story_correction_attempts += 1
                                    segment_index += 1
                                    next_segment_id = f"{trace_id}-segment-{segment_index + 1}"
                                    continuation_packet = {
                                        "_type": "ContinuationStarted",
                                        "_version": 1,
                                        "traceId": trace_id,
                                        "sessionId": session_id,
                                        "segmentId": next_segment_id,
                                        "previousSegmentId": segment_id,
                                        "continuationMode": "story_generation_correction",
                                        "correctionAttempt": story_correction_attempts,
                                        "maximumCorrectionAttempts": _STORY_GENERATION_MAX_CORRECTIONS,
                                        "validation": validation_packet,
                                    }
                                    events.append(
                                        _event_to_trace_event(
                                            "ContinuationStarted",
                                            continuation_packet,
                                            len(events) + 1,
                                        )
                                    )
                                    yield _encode_sse("ContinuationStarted", continuation_packet)
                                    segment_prompt = _story_generation_correction_prompt(
                                        validation_packet,
                                        correction_attempt=story_correction_attempts,
                                    )
                                    completed = False
                                    terminal_event = None
                                    continue

                                error_message = (
                                    "正文经过 "
                                    f"{_STORY_GENERATION_MAX_CORRECTIONS} 次自动修订后仍未通过 Storydex "
                                    "客观字数或章节结构校验。"
                                )
                                completed = False
                                terminal_event = None
                                followup_mailbox_service.pause(
                                    workspace_root=workspace_root,
                                    session_id=session_id,
                                    reason="story_generation_validation_failed",
                                )
                                error_packet = {
                                    "_type": "AgentError",
                                    "_version": 1,
                                    "error_type": "StoryGenerationValidationFailed",
                                    "message": error_message,
                                    "details": {
                                        "runtime": "storydex_validation",
                                        "validation": validation_packet,
                                    },
                                }
                                events.append(_event_to_trace_event("AgentError", error_packet, len(events) + 1))
                                yield _encode_sse("AgentError", error_packet)
                                break
                    completed = segment_completed
                    if segment_cancelled and not execution_handle.is_cancelled:
                        completed = False
                    break

            if error_message:
                if tracker is not None:
                    for task_event_name, task_payload in tracker.fail_current(error_message):
                        events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                        yield _encode_sse(task_event_name, task_payload)
                    for task_event_name, task_payload in tracker.skip_remaining_execution(reason="execution_failed"):
                        events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                        yield _encode_sse(task_event_name, task_payload)
            elif completed and not runtime_tasks_finalized:
                if tracker is not None:
                    for task_event_name, task_payload in tracker.complete_through_execution():
                        events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                        yield _encode_sse(task_event_name, task_payload)
        except Exception as exc:
            error_message = str(exc)
            packet = {
                "_type": "AgentError",
                "_version": 1,
                "error_type": type(exc).__name__,
                "message": str(exc),
                "details": {"runtime": "coomi"},
            }
            events.append(_event_to_trace_event("AgentError", packet, len(events) + 1))
            yield _encode_sse("AgentError", packet)
            if tracker is not None:
                for task_event_name, task_payload in tracker.fail_current(error_message):
                    events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                    yield _encode_sse(task_event_name, task_payload)
                for task_event_name, task_payload in tracker.skip_remaining_execution(reason="execution_failed"):
                    events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                    yield _encode_sse(task_event_name, task_payload)

        if planning_task is not None:
            if planning_task.done() and not planning_task.cancelled():
                try:
                    task_plan = planning_task.result()
                except Exception:
                    task_plan = []
                yield _encode_sse(
                    "TurnPhase",
                    _turn_phase_packet(
                        trace_id=trace_id,
                        session_id=session_id,
                        phase="task_planning",
                        label="执行步骤规划完成",
                        status="success",
                        phase_started=planning_started,
                        detail=f"已生成 {len(task_plan)} 个执行步骤（未阻塞模型启动）",
                    ),
                )
            else:
                planning_task.cancel()
                _retain_background_execution_task(planning_task)
                task_plan = []
                yield _encode_sse(
                    "TurnPhase",
                    _turn_phase_packet(
                        trace_id=trace_id,
                        session_id=session_id,
                        phase="task_planning",
                        label="执行已先于规划继续",
                        status="warning",
                        phase_started=planning_started,
                        detail="规划未阻塞正式 Agent 启动",
                    ),
                )
            planning_task = None
            if tracker is None:
                tracker = _TaskRunTracker(task_plan, trace_id=trace_id, session_id=session_id)
                plan_payload = tracker.plan_created_payload()
                events.append(_event_to_trace_event("TaskPlanCreated", plan_payload, len(events) + 1))
                yield _encode_sse("TaskPlanCreated", plan_payload)
                if error_message:
                    for task_event_name, task_payload in tracker.fail_current(error_message):
                        events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                        yield _encode_sse(task_event_name, task_payload)
                elif completed:
                    for task_event_name, task_payload in tracker.complete_through_execution():
                        events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                        yield _encode_sse(task_event_name, task_payload)

        mailbox_events = followup_mailbox_service.events_for_trace(
            workspace_root=workspace_root,
            session_id=session_id,
            trace_id=trace_id,
            event_types={
                "FollowupQueued",
                "FollowupUpdated",
                "SteerRequested",
                "SteerApplied",
                "ContinuationStarted",
            },
        )
        existing_mailbox_event_ids = {
            str((item.get("data") or {}).get("eventId") or "")
            for item in events
            if isinstance(item, dict) and isinstance(item.get("data"), dict)
        }
        for mailbox_event in mailbox_events:
            if str(mailbox_event.get("eventId") or "") in existing_mailbox_event_ids:
                continue
            event_name = str(mailbox_event.get("_type") or "FollowupUpdated")
            events.append(_event_to_trace_event(event_name, mailbox_event, len(events) + 1))

        if tracker is not None:
            for task_event_name, task_payload in tracker.start_version_task():
                events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                yield _encode_sse(task_event_name, task_payload)

        def on_git_payload(git_payload: Dict[str, Any]) -> None:
            git_payload["traceId"] = trace_id
            git_payload["sessionId"] = session_id
            git_event_name = _git_event_name(git_payload)
            if git_event_name == "GitCommitPrompt":
                followup_mailbox_service.pause(
                    workspace_root=workspace_root,
                    session_id=session_id,
                    reason="git_commit_prompt",
                )
            events.append(_event_to_trace_event(git_event_name, git_payload, len(events) + 1))
            finalization_packets.append(_encode_sse(git_event_name, git_payload))
            if tracker is not None:
                for task_event_name, task_payload in tracker.finish_version_task(
                    failed=str(git_payload.get("status") or "") == "error",
                    message=str(git_payload.get("message") or ""),
                ):
                    events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                    finalization_packets.append(_encode_sse(task_event_name, task_payload))

        def on_terminal(status: str, terminal_error: str) -> None:
            nonlocal terminal_event
            if status == "completed":
                event_name, packet = terminal_event or (
                    "AgentCompleted",
                    {
                        "_type": "AgentCompleted",
                        "_version": 1,
                        "session_id": session_id,
                        "route": "coomi",
                    },
                )
            elif status == "cancelled":
                event_name, packet = terminal_event or (
                    "AgentCancelled",
                    {
                        "_type": "AgentCancelled",
                        "_version": 1,
                        "session_id": session_id,
                        "reason": execution_handle.cancel_reason or "cancelled",
                    },
                )
            else:
                existing_error = next(
                    (item for item in reversed(events) if item.get("event") == "AgentError"),
                    None,
                )
                if existing_error is not None:
                    return
                event_name = "AgentError"
                packet = {
                    "_type": "AgentError",
                    "_version": 1,
                    "error_type": "ExecutionFailed",
                    "message": terminal_error or "Coomi execution failed.",
                    "details": {"runtime": "coomi"},
                }
            events.append(_event_to_trace_event(event_name, packet, len(events) + 1))
            finalization_packets.append(_encode_sse(event_name, packet))

        def build_payload(
            status: str,
            terminal_error: str,
            no_restore_point: bool,
            _timings: Dict[str, float],
        ) -> Dict[str, Any]:
            payload_data = _build_chat_payload(
                trace_id=trace_id,
                prompt=prompt,
                reply="".join(reply_chunks),
                events=events,
                started=started,
                workspace_root=workspace_root,
                session_id=session_id,
                execution_log_session=execution_log_session,
                status=status,
                error_message=terminal_error,
            )
            record = payload_data.get("record")
            if isinstance(record, dict):
                record["noRestorePoint"] = no_restore_point
            return payload_data

        def write_timing(payload: Dict[str, Any]) -> None:
            if execution_log_session is not None:
                execution_log_session.write(
                    "execution_coordinator_timing",
                    payload,
                    category="observability",
                )

        observation = ExecutionObservation(
            completed=completed,
            error_message=error_message,
            error_code="coomi_agent_error" if error_message else "",
            cancelled=execution_handle.is_cancelled or cancellation_token.is_cancelled(),
        )
        context = ExecutionFinalizationContext(
            finish_git=finish_git_turn,
            on_git_payload=on_git_payload,
            on_terminal=on_terminal,
            build_payload=build_payload,
            persist_trace=lambda record: _persist_execution_trace(
                workspace_root,
                record,
                session_id,
            ),
            write_timing=write_timing,
        )
        await execution_handle.finalize(observation, context)
        if replacement is not None and not replacement.accepted:
            await asyncio.to_thread(replacement.restore, reason="replacement_start_failed")
        reset_llm_metrics(trace_id)
        for packet in finalization_packets:
            yield packet
        yield _encode_sse("done", {"type": "done"})
    except Exception as exc:
        if replacement is not None and not replacement.accepted:
            try:
                await asyncio.to_thread(replacement.restore, reason="replacement_execution_failed")
            except Exception:
                _LOGGER.exception("Unable to restore replacement target %s", replacement.expected_trace_id)
        reset_llm_metrics(trace_id)
        packet = {
            "_type": "AgentError",
            "_version": 1,
            "error_type": type(exc).__name__,
            "message": str(exc),
            "details": {"runtime": "execution_coordinator"},
        }
        yield _encode_sse("AgentError", packet)
        yield _encode_sse("done", {"type": "done"})
    finally:
        if replacement is not None and not replacement.accepted and not replacement.restored:
            try:
                await asyncio.to_thread(replacement.restore, reason="replacement_worker_stopped")
            except Exception:
                _LOGGER.exception("Unable to restore replacement target %s", replacement.expected_trace_id)


async def _stream_coomi_sse(
    *,
    prompt: str,
    trace_id: str,
    session_id: str,
    active_file: str,
    workspace_root: Path,
    story_generation: Dict[str, Any],
    turn_contract: Dict[str, Any],
    git_snapshot: AgentGitSnapshot,
    request: Request,
    cancellation_token: _CancellationToken,
    execution_handle: ExecutionHandle | None = None,
    execution_log_session: ExecutionLogSession | None = None,
    replacement: _LatestExecutionReplacement | None = None,
) -> AsyncIterator[str]:
    """Transport-only wrapper around the independent execution worker."""
    handle = execution_handle or execution_coordinator.adopt_reservation_or_begin(
        workspace_root,
        session_id,
        trace_id,
    )
    if execution_handle is None:
        handle.register_snapshot(git_snapshot, confirm_no_snapshot=True)
    handle.bind_cancellation(lambda _reason: cancellation_token.cancel())
    coomi_service = get_storydex_coomi_agent_service()
    cancel_execution = getattr(coomi_service, "cancel_execution", None)
    if callable(cancel_execution):
        handle.bind_cancellation(
            lambda reason: cancel_execution(
                session_id=session_id,
                workspace_root=workspace_root,
                reason=reason,
            )
        )

    if await request.is_disconnected():
        handle.cancel("client_disconnected")
        followup_mailbox_service.pause(
            workspace_root=workspace_root,
            session_id=session_id,
            reason="client_disconnected",
        )

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for chunk in _stream_coomi_sse_worker(
                prompt=prompt,
                trace_id=trace_id,
                session_id=session_id,
                active_file=active_file,
                workspace_root=workspace_root,
                story_generation=story_generation,
                turn_contract=turn_contract,
                git_snapshot=git_snapshot,
                cancellation_token=cancellation_token,
                execution_handle=handle,
                execution_log_session=execution_log_session,
                replacement=replacement,
            ):
                await queue.put(chunk)
        except asyncio.CancelledError:
            handle.abandon("worker_cancelled")
            raise
        except Exception as exc:
            await queue.put(
                _encode_sse(
                    "AgentError",
                    {
                        "_type": "AgentError",
                        "_version": 1,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
            )
            await queue.put(_encode_sse("done", {"type": "done"}))
        finally:
            queue.put_nowait(None)

    worker = _retain_background_execution_task(
        asyncio.create_task(pump(), name=f"storydex-execution-{trace_id}")
    )
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=_PHASE_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                if await request.is_disconnected():
                    handle.cancel("client_disconnected")
                    followup_mailbox_service.pause(
                        workspace_root=workspace_root,
                        session_id=session_id,
                        reason="client_disconnected",
                    )
                    return
                continue
            if chunk is None:
                break
            yield chunk
            if await request.is_disconnected():
                handle.cancel("client_disconnected")
                followup_mailbox_service.pause(
                    workspace_root=workspace_root,
                    session_id=session_id,
                    reason="client_disconnected",
                )
                return
        await asyncio.shield(worker)
    finally:
        if not worker.done():
            handle.cancel("client_disconnected")
            followup_mailbox_service.pause(
                workspace_root=workspace_root,
                session_id=session_id,
                reason="client_disconnected",
            )


@router.get("/agent/sessions", response_model=ApiEnvelope)
def agent_sessions(request: Request) -> ApiEnvelope:
    del request
    started = time.perf_counter()
    trace_id = str(uuid4())
    items = [AgentSessionSummary(**item) for item in trace_history_service.list_session_summaries()]
    data = AgentSessionsData(items=items)
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=ApiTrace(traceId=trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=1),
        audit=[{"action": "read_agent_sessions", "runtime": "coomi", "count": len(items)}],
    )


@router.delete("/agent/sessions/{session_id}", response_model=ApiEnvelope)
def agent_delete_session(session_id: str, request: Request) -> ApiEnvelope:
    del request
    return _delete_agent_session(session_id)


@router.post("/agent/sessions/delete", response_model=ApiEnvelope)
def agent_delete_session_by_body(payload: AgentSessionDeleteRequest, request: Request) -> ApiEnvelope:
    del request
    return _delete_agent_session(payload.session_id)


def _delete_agent_session(session_id: str) -> ApiEnvelope:
    started = time.perf_counter()
    trace_id = str(uuid4())
    workspace_root = project_service.workspace_root
    get_storydex_coomi_agent_service().clear_session(
        session_id,
        workspace_root=workspace_root,
        delete_history=True,
    )
    storydex_intent_service.clear_session(session_id=session_id, workspace_root=workspace_root)
    result = trace_history_service.delete_session(session_id)
    return success_response(
        data={**result, "runtime": "coomi"},
        trace=ApiTrace(traceId=trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=1),
        audit=[
            {
                "action": "delete_agent_session",
                "runtime": "coomi",
                "sessionId": result.get("sessionId"),
                "removedCount": result.get("removedCount"),
            }
        ],
    )


@router.get("/agent/coomi/status", response_model=ApiEnvelope)
def agent_coomi_status(request: Request) -> ApiEnvelope:
    del request
    started = time.perf_counter()
    trace_id = str(uuid4())
    status = get_storydex_coomi_agent_service().get_status(workspace_root=project_service.workspace_root)
    data = AgentCoomiStatusData(**status)
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=ApiTrace(traceId=trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=0),
        audit=[{"action": "read_coomi_status", "toolCount": data.tool_count}],
    )


@router.get("/agent/coomi/config", response_model=ApiEnvelope)
def agent_read_coomi_config(request: Request) -> ApiEnvelope:
    del request
    started = time.perf_counter()
    trace_id = str(uuid4())
    payload = get_storydex_coomi_agent_service().read_config()
    data = AgentCoomiConfigData(**payload)
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=ApiTrace(traceId=trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=0),
        audit=[{"action": "read_coomi_config", "configPath": data.config_path}],
    )


@router.put("/agent/coomi/config", response_model=ApiEnvelope)
def agent_update_coomi_config(payload: AgentCoomiConfigUpdateRequest, request: Request) -> ApiEnvelope:
    del request
    started = time.perf_counter()
    trace_id = str(uuid4())
    try:
        updated = get_storydex_coomi_agent_service().write_config(payload.content)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise StorydexError(
            "Invalid Coomi providers config.",
            code="coomi_config_invalid",
            status_code=400,
            details={"message": str(exc)},
        ) from exc
    data = AgentCoomiConfigData(**updated)
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=ApiTrace(traceId=trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=0),
        audit=[{"action": "update_coomi_config", "configPath": data.config_path}],
    )


@router.post("/agent/coomi/models", response_model=ApiEnvelope)
def agent_list_coomi_models(payload: AgentCoomiModelListRequest, request: Request) -> ApiEnvelope:
    del request
    started = time.perf_counter()
    trace_id = str(uuid4())
    try:
        result = get_storydex_coomi_agent_service().list_models(
            base_url=payload.base_url,
            api_key=payload.api_key,
        )
    except ValueError as exc:
        raise StorydexError(
            "Unable to fetch Coomi model list.",
            code="coomi_models_unavailable",
            status_code=400,
            details={"message": str(exc)},
        ) from exc
    data = AgentCoomiModelListData(**result)
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=ApiTrace(traceId=trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=0),
        audit=[{"action": "fetch_coomi_models", "endpoint": data.endpoint, "modelCount": len(data.models)}],
    )


@router.post("/agent/coomi/permission", response_model=ApiEnvelope)
def agent_set_coomi_permission(payload: AgentPermissionModeRequest, request: Request) -> ApiEnvelope:
    del request
    started = time.perf_counter()
    trace_id = str(uuid4())
    result = get_storydex_coomi_agent_service().set_permission_mode(payload.permission_mode)
    return success_response(
        data=result,
        trace=ApiTrace(traceId=trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=0),
        audit=[{"action": "set_coomi_permission", "permissionMode": result.get("permissionMode")}],
    )


@router.post("/agent/coomi/permission/cycle", response_model=ApiEnvelope)
def agent_cycle_coomi_permission(request: Request) -> ApiEnvelope:
    del request
    started = time.perf_counter()
    trace_id = str(uuid4())
    result = get_storydex_coomi_agent_service().cycle_permission_mode()
    return success_response(
        data=result,
        trace=ApiTrace(traceId=trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=0),
        audit=[{"action": "cycle_coomi_permission", "permissionMode": result.get("permissionMode")}],
    )


@router.post("/agent/coomi/approval", response_model=ApiEnvelope)
def agent_resolve_coomi_approval(payload: AgentApprovalRequest, request: Request) -> ApiEnvelope:
    del request
    started = time.perf_counter()
    trace_id = str(uuid4())
    result = get_storydex_coomi_agent_service().resolve_approval(
        payload.approval_id,
        payload.decision,
        response=payload.response,
    )
    return success_response(
        data=result,
        trace=ApiTrace(traceId=trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=0),
        audit=[{"action": "resolve_coomi_approval", "approvalId": result.get("approvalId"), "decision": result.get("decision")}],
    )


@router.post("/agent/runs/{trace_id}/commit", response_model=ApiEnvelope)
async def agent_run_commit_decision(
    trace_id: str,
    payload: AgentCommitDecisionRequest,
    request: Request,
    session_id_query: Optional[str] = Query(default=None, alias="sessionId"),
) -> ApiEnvelope:
    started = time.perf_counter()
    api_trace_id = str(uuid4())
    session_id = str(payload.session_id or session_id_query or "").strip() or _resolve_agent_session_id(request)
    record, resolved_session_id = _read_agent_run_record(trace_id, session_id)
    workspace_root = _record_workspace_root(record) if record is not None else project_service.workspace_root
    mode = str(payload.mode or "").strip().lower()
    if mode not in {"auto", "manual", "skip"}:
        raise StorydexError(
            "Unsupported commit decision mode.",
            code="invalid_agent_commit_decision",
            status_code=422,
            details={"mode": payload.mode},
        )

    existing_ledger = _record_change_ledger(
        record or {},
        trace_id=trace_id,
        session_id=resolved_session_id,
    )
    if mode == "skip":
        current_payload = agent_git_autocommit_service.acknowledge_skip(
            workspace_root,
            changed_files=(
                existing_ledger.get("changedFiles")
                if isinstance(existing_ledger.get("changedFiles"), list)
                else []
            ),
            added=int(existing_ledger.get("added") or 0),
            removed=int(existing_ledger.get("removed") or 0),
        )
    else:
        current_payload = agent_git_autocommit_service.current_changes_payload(
            workspace_root,
            event_type="GitCommitResult",
            status="info",
            reason="pending_commit",
            message="检测到未提交修改。",
        )
    current_changed_files = [
        str(path).replace("\\", "/").strip()
        for path in (
            current_payload.get("changedFiles") if isinstance(current_payload.get("changedFiles"), list) else []
        )
        if str(path).strip()
    ]
    if not current_changed_files or str(current_payload.get("status") or "") == "error":
        result_payload = current_payload
    elif mode == "skip":
        result_payload = current_payload
    else:
        commit_message = str(payload.message or "").strip()
        generated_message = False
        if mode == "manual":
            if not commit_message:
                raise StorydexError(
                    "Commit message is required.",
                    code="commit_message_required",
                    status_code=422,
                )
        else:
            original_prompt = str((record or {}).get("prompt") or "")
            try:
                commit_message = await asyncio.wait_for(
                    get_storydex_coomi_agent_service().generate_commit_message(
                        workspace_root=workspace_root,
                        changed_files=current_changed_files,
                        diff_summary=_build_commit_message_diff_summary(workspace_root, current_changed_files),
                        prompt=original_prompt,
                        trace_id=trace_id,
                    ),
                    timeout=_COMMIT_MESSAGE_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                del exc
                commit_message = agent_git_autocommit_service._commit_message_for_prompt(original_prompt)
            else:
                generated_message = True
        result_payload = agent_git_autocommit_service.commit_current_changes(
            workspace_root,
            message=commit_message,
        )
        result_payload["generatedMessage"] = generated_message
        result_payload["commitMessageStrategy"] = "llm" if generated_message else "deterministic_fallback"

    result_payload["traceId"] = trace_id
    result_payload["sessionId"] = resolved_session_id
    _append_git_commit_decision_record(
        trace_id=trace_id,
        session_id=resolved_session_id,
        payload=result_payload,
    )
    audit = [
        {
            "action": "agent_git_commit_decision",
            "mode": mode,
            "traceId": trace_id,
            "sessionId": resolved_session_id,
            "workspaceRoot": workspace_root.as_posix(),
            "created": bool(result_payload.get("created")),
            "reason": str(result_payload.get("reason") or ""),
            "commitHash": str(result_payload.get("commitHash") or ""),
            "changedFileCount": int(result_payload.get("changedFileCount") or 0),
        }
    ]
    return success_response(
        data=result_payload,
        trace=ApiTrace(traceId=api_trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=1),
        audit=audit,
    )


@router.get("/agent/history", response_model=ApiEnvelope)
def agent_history(
    request: Request,
    limit: int = Query(default=40, ge=1, le=200),
    session_id_query: Optional[str] = Query(default=None, alias="sessionId"),
) -> ApiEnvelope:
    started = time.perf_counter()
    trace_id = str(uuid4())
    session_id = str(session_id_query or "").strip() or _resolve_agent_session_id(request)
    items = trace_history_service.list_records(session_id=session_id, limit=limit)
    data = AgentHistoryData(items=items)
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=ApiTrace(traceId=trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=1),
        audit=[{"action": "read_agent_history", "runtime": "coomi", "count": len(items)}],
    )


@router.get("/agent/followups", response_model=ApiEnvelope)
def agent_followups(
    request: Request,
    session_id_query: Optional[str] = Query(default=None, alias="sessionId"),
    workspace_root_query: Optional[str] = Query(default=None, alias="workspaceRoot"),
) -> ApiEnvelope:
    started = time.perf_counter()
    api_trace_id = str(uuid4())
    session_id = str(session_id_query or "").strip() or _resolve_agent_session_id(request)
    workspace_root = _resolve_followup_workspace_root(
        session_id=session_id,
        workspace_root=str(workspace_root_query or ""),
    )
    state = followup_mailbox_service.list_mailbox(
        workspace_root=workspace_root,
        session_id=session_id,
    )
    return success_response(
        data=state,
        trace=ApiTrace(traceId=api_trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=1),
        audit=[{"action": "read_agent_followups", "sessionId": session_id, "revision": state.get("revision")}],
    )


@router.post("/agent/followups", response_model=ApiEnvelope)
def agent_enqueue_followup(payload: AgentFollowupRequest, request: Request) -> ApiEnvelope:
    del request
    started = time.perf_counter()
    api_trace_id = str(uuid4())
    session_id = str(payload.session_id or "default").strip() or "default"
    workspace_root = _resolve_followup_workspace_root(
        session_id=session_id,
        workspace_root=payload.workspace_root,
    )
    try:
        message = followup_mailbox_service.enqueue(
            workspace_root=workspace_root,
            session_id=session_id,
            message_id=payload.message_id,
            content=payload.content,
            mode=payload.mode,
            expected_trace_id=payload.expected_trace_id or payload.active_trace_id,
        )
    except FollowupMailboxError as exc:
        _raise_followup_error(exc)
    steer_requested = False
    if str(message.get("mode") or "") == "steer":
        steer_requested = get_storydex_coomi_agent_service().request_steer(
            session_id=session_id,
            workspace_root=workspace_root,
        )
    return success_response(
        data={"message": message, "steerRequested": steer_requested},
        trace=ApiTrace(traceId=api_trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=1),
        audit=[
            {
                "action": "enqueue_agent_followup",
                "messageId": payload.message_id,
                "sessionId": session_id,
                "mode": message.get("mode"),
                "status": message.get("status"),
                "activeTraceId": message.get("activeTraceId"),
            }
        ],
    )


@router.patch("/agent/followups/{message_id}", response_model=ApiEnvelope)
def agent_update_followup(
    message_id: str,
    payload: AgentFollowupUpdateRequest,
    request: Request,
) -> ApiEnvelope:
    del request
    started = time.perf_counter()
    api_trace_id = str(uuid4())
    session_id = str(payload.session_id or "default").strip() or "default"
    workspace_root = _resolve_followup_workspace_root(
        session_id=session_id,
        workspace_root=payload.workspace_root,
    )
    try:
        message = followup_mailbox_service.update_message(
            workspace_root=workspace_root,
            session_id=session_id,
            message_id=message_id,
            content=payload.content,
            mode=payload.mode,
            expected_trace_id=payload.expected_trace_id,
        )
    except FollowupMailboxError as exc:
        _raise_followup_error(exc)
    steer_requested = False
    if str(message.get("mode") or "") == "steer":
        steer_requested = get_storydex_coomi_agent_service().request_steer(
            session_id=session_id,
            workspace_root=workspace_root,
        )
    return success_response(
        data={"message": message, "steerRequested": steer_requested},
        trace=ApiTrace(traceId=api_trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=1),
        audit=[{"action": "update_agent_followup", "messageId": message_id, "status": message.get("status")}],
    )


@router.delete("/agent/followups/{message_id}", response_model=ApiEnvelope)
def agent_delete_followup(
    message_id: str,
    request: Request,
    session_id_query: Optional[str] = Query(default=None, alias="sessionId"),
    workspace_root_query: Optional[str] = Query(default=None, alias="workspaceRoot"),
) -> ApiEnvelope:
    started = time.perf_counter()
    api_trace_id = str(uuid4())
    session_id = str(session_id_query or "").strip() or _resolve_agent_session_id(request)
    workspace_root = _resolve_followup_workspace_root(
        session_id=session_id,
        workspace_root=str(workspace_root_query or ""),
    )
    try:
        message = followup_mailbox_service.cancel_message(
            workspace_root=workspace_root,
            session_id=session_id,
            message_id=message_id,
        )
    except FollowupMailboxError as exc:
        _raise_followup_error(exc)
    return success_response(
        data={"message": message},
        trace=ApiTrace(traceId=api_trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=1),
        audit=[{"action": "delete_agent_followup", "messageId": message_id, "status": message.get("status")}],
    )


@router.post("/agent/followups/{message_id}/steer", response_model=ApiEnvelope)
def agent_steer_followup(
    message_id: str,
    payload: AgentFollowupActionRequest,
    request: Request,
) -> ApiEnvelope:
    del request
    started = time.perf_counter()
    api_trace_id = str(uuid4())
    session_id = str(payload.session_id or "default").strip() or "default"
    workspace_root = _resolve_followup_workspace_root(
        session_id=session_id,
        workspace_root=payload.workspace_root,
    )
    try:
        message = followup_mailbox_service.update_message(
            workspace_root=workspace_root,
            session_id=session_id,
            message_id=message_id,
            mode="steer",
            expected_trace_id=payload.expected_trace_id,
        )
    except FollowupMailboxError as exc:
        _raise_followup_error(exc)
    steer_requested = get_storydex_coomi_agent_service().request_steer(
        session_id=session_id,
        workspace_root=workspace_root,
    )
    return success_response(
        data={"message": message, "steerRequested": steer_requested},
        trace=ApiTrace(traceId=api_trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=1),
        audit=[{"action": "steer_agent_followup", "messageId": message_id, "activeTraceId": message.get("activeTraceId")}],
    )


@router.post("/agent/followups/resume", response_model=ApiEnvelope)
def agent_resume_followups(payload: AgentFollowupActionRequest, request: Request) -> ApiEnvelope:
    del request
    started = time.perf_counter()
    api_trace_id = str(uuid4())
    session_id = str(payload.session_id or "default").strip() or "default"
    workspace_root = _resolve_followup_workspace_root(
        session_id=session_id,
        workspace_root=payload.workspace_root,
    )
    state = followup_mailbox_service.resume(workspace_root=workspace_root, session_id=session_id)
    return success_response(
        data=state,
        trace=ApiTrace(traceId=api_trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=1),
        audit=[{"action": "resume_agent_followups", "sessionId": session_id}],
    )


@router.post("/agent/executions/stop", response_model=ApiEnvelope)
def agent_stop_execution(payload: AgentExecutionStopRequest, request: Request) -> ApiEnvelope:
    del request
    started = time.perf_counter()
    api_trace_id = str(uuid4())
    session_id = str(payload.session_id or "default").strip() or "default"
    workspace_root = _resolve_followup_workspace_root(
        session_id=session_id,
        workspace_root=payload.workspace_root,
    )
    result = execution_coordinator.cancel_active(
        session_id=session_id,
        expected_trace_id=payload.expected_trace_id,
        workspace_root=workspace_root,
        reason="manual_stop",
    )
    if str(result.get("reason") or "") == "stale_trace":
        raise StorydexError(
            "The active execution changed before the stop request was applied.",
            code="stale_trace",
            status_code=409,
            details=result,
        )
    state = followup_mailbox_service.pause(
        workspace_root=workspace_root,
        session_id=session_id,
        reason="manual_stop",
    )
    return success_response(
        data={**result, "mailboxPaused": bool(state.get("paused")), "pauseReason": state.get("pauseReason")},
        trace=ApiTrace(traceId=api_trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=1),
        audit=[{"action": "stop_agent_execution", "sessionId": session_id, "activeTraceId": result.get("activeTraceId")}],
    )


@router.post("/agent/executions/rollback-latest", response_model=ApiEnvelope)
def agent_rollback_latest_execution(
    payload: AgentExecutionRollbackRequest,
    request: Request,
) -> ApiEnvelope:
    del request
    started = time.perf_counter()
    api_trace_id = str(uuid4())
    session_id = str(payload.session_id or "default").strip() or "default"
    if not _try_acquire_agent_generation_slot():
        raise _agent_busy_error(trace_id=api_trace_id, session_id=session_id)

    removed_trace_id = ""
    prompt = ""
    rolled_back = False
    try:
        records = trace_history_service.list_records(session_id=session_id, limit=1)
        latest = records[0] if records else None
        if isinstance(latest, dict):
            trace_id = str(latest.get("traceId") or "").strip()
            prompt = str(latest.get("prompt") or "")
            if payload.expected_trace_id and trace_id != str(payload.expected_trace_id or "").strip():
                raise StorydexError(
                    "The latest execution changed before deletion was confirmed.",
                    code="stale_trace",
                    status_code=409,
                    details={"expectedTraceId": payload.expected_trace_id, "latestTraceId": trace_id},
                )
            if str(latest.get("status") or "") == "running":
                raise StorydexError(
                    "A running execution cannot be deleted.",
                    code="execution_running",
                    status_code=409,
                )
            if trace_id:
                record_workspace = str(latest.get("workspaceRoot") or "").strip()
                rollback_workspace = Path(record_workspace).resolve() if record_workspace else project_service.workspace_root
                rollback = get_storydex_coomi_agent_service().rollback_last_turn(
                    session_id,
                    workspace_root=rollback_workspace,
                )
                rolled_back = bool(rollback.get("rolledBack"))
                if rolled_back:
                    trace_history_service.delete_record(trace_id, session_id)
                    storydex_intent_service.clear_session(
                        session_id=session_id,
                        workspace_root=rollback_workspace,
                    )
                    removed_trace_id = trace_id

        data = {
            "rolledBack": rolled_back,
            "sessionId": session_id,
            "removedTraceId": removed_trace_id,
            "prompt": prompt,
        }
        return success_response(
            data=data,
            trace=ApiTrace(
                traceId=api_trace_id,
                durationMs=int((time.perf_counter() - started) * 1000),
                toolCalls=2 if rolled_back else 1,
            ),
            audit=[
                {
                    "action": "rollback_latest_execution",
                    "runtime": "coomi",
                    "sessionId": session_id,
                    "removedTraceId": removed_trace_id,
                    "rolledBack": rolled_back,
                }
            ],
        )
    finally:
        _release_agent_generation_slot()


@router.get("/agent/runs/{trace_id}/diff", response_model=ApiEnvelope)
def agent_run_diff(
    trace_id: str,
    request: Request,
    session_id_query: Optional[str] = Query(default=None, alias="sessionId"),
    changed_files_query: Optional[str] = Query(default=None, alias="changedFiles"),
    commit_hash_query: Optional[str] = Query(default=None, alias="commitHash"),
) -> ApiEnvelope:
    started = time.perf_counter()
    api_trace_id = str(uuid4())
    session_id = str(session_id_query or "").strip() or _resolve_agent_session_id(request)
    record, resolved_session_id = _read_agent_run_record(trace_id, session_id)
    if record is None:
        workspace_root = project_service.workspace_root
        fallback_changed_files = _normalize_changed_file_candidates(changed_files_query, workspace_root=workspace_root)
        fallback_commit_hash = str(commit_hash_query or "").strip()
        try:
            if fallback_commit_hash:
                data = git_service.read_commit_diff(
                    workspace_root,
                    commit_id=fallback_commit_hash,
                    paths=fallback_changed_files or None,
                )
            elif fallback_changed_files:
                data = git_service.read_diff(workspace_root, paths=fallback_changed_files)
            else:
                data = _empty_agent_run_diff_payload(
                    workspace_root,
                    message="本轮 Diff 数据不可用。",
                    trace_id=trace_id,
                    session_id=session_id,
                )
        except GitServiceError as exc:
            data = _empty_agent_run_diff_payload(
                workspace_root,
                message="本轮 Diff 数据不可用。",
                trace_id=trace_id,
                session_id=session_id,
            )
            data["error"] = {"code": exc.code, "message": exc.message, "details": exc.details}
        if not fallback_commit_hash and fallback_changed_files:
            data = _include_missing_agent_snapshot_diffs(data, workspace_root, fallback_changed_files)
        totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
        if fallback_changed_files or fallback_commit_hash:
            data.update(
                {
                    "traceId": trace_id,
                    "sessionId": session_id,
                    "changedFiles": fallback_changed_files,
                    "changedFileCount": len(fallback_changed_files) or int(totals.get("files") or 0),
                    "added": int(totals.get("added") or 0),
                    "removed": int(totals.get("removed") or 0),
                    "diffSource": "commit" if fallback_commit_hash else "working_tree",
                    "commitHash": fallback_commit_hash,
                    "shortHash": fallback_commit_hash[:12] if fallback_commit_hash else "",
                }
            )
        return success_response(
            data=data,
            trace=ApiTrace(traceId=api_trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=1),
            audit=[
                {
                    "action": "read_agent_run_diff",
                    "found": False,
                    "fallback": bool(fallback_changed_files or fallback_commit_hash),
                    "traceId": trace_id,
                    "sessionId": session_id,
                    "fileCount": int(totals.get("files") or 0),
                    "diffSource": data.get("diffSource"),
                }
            ],
        )

    workspace_root = _record_workspace_root(record)
    fallback_changed_files = _normalize_changed_file_candidates(changed_files_query, workspace_root=workspace_root)
    fallback_commit_hash = str(commit_hash_query or "").strip()
    ledger = _record_change_ledger(record, trace_id=trace_id, session_id=resolved_session_id)
    ledger_changed_files = ledger.get("changedFiles") if isinstance(ledger.get("changedFiles"), list) else []
    changed_files = _merge_changed_file_lists(ledger_changed_files, fallback_changed_files)
    commit_hash = str(ledger.get("commitHash") or fallback_commit_hash or "").strip()
    try:
        if commit_hash:
            data = git_service.read_commit_diff(
                workspace_root,
                commit_id=commit_hash,
                paths=changed_files or None,
            )
        elif changed_files:
            data = git_service.read_diff(workspace_root, paths=changed_files)
        else:
            data = _empty_agent_run_diff_payload(
                workspace_root,
                message="本轮没有可展示的文件修改。",
                trace_id=trace_id,
                session_id=resolved_session_id,
            )
    except GitServiceError as exc:
        data = _empty_agent_run_diff_payload(
            workspace_root,
            message="本轮 Diff 数据不可用。",
            trace_id=trace_id,
            session_id=resolved_session_id,
        )
        data["error"] = {"code": exc.code, "message": exc.message, "details": exc.details}

    if not commit_hash and changed_files:
        data = _include_missing_agent_snapshot_diffs(data, workspace_root, changed_files)
    totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
    data.update(
        {
            "traceId": trace_id,
            "sessionId": resolved_session_id,
            "changedFiles": changed_files,
            "changedFileCount": int(ledger.get("changedFileCount") or len(changed_files)),
            "added": int(ledger.get("added") or totals.get("added") or 0),
            "removed": int(ledger.get("removed") or totals.get("removed") or 0),
            "diffSource": str(ledger.get("diffSource") or ("commit" if commit_hash else "working_tree" if changed_files else "")),
            "commitHash": commit_hash,
            "shortHash": str(ledger.get("shortHash") or ""),
        }
    )
    audit = [
        {
            "action": "read_agent_run_diff",
            "found": True,
            "traceId": trace_id,
            "sessionId": resolved_session_id,
            "workspaceRoot": workspace_root.as_posix(),
            "fileCount": int(totals.get("files") or len(data.get("files") if isinstance(data.get("files"), list) else [])),
            "diffSource": data.get("diffSource"),
            "commitHash": commit_hash,
        }
    ]
    return success_response(
        data=data,
        trace=ApiTrace(traceId=api_trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=1),
        audit=audit,
    )


def _normalize_changed_file_candidates(value: Any, *, workspace_root: Path) -> List[str]:
    sources: List[str] = []
    if isinstance(value, list):
        sources.extend(str(item or "") for item in value)
    elif value is not None:
        sources.append(str(value or ""))

    root = Path(workspace_root).resolve()
    result: List[str] = []
    seen: set[str] = set()
    for source in sources:
        for raw_part in re.split(r"[\r\n]+", str(source or "")):
            text = raw_part.replace("\0", "").replace("\\", "/").strip().strip("\"'`")
            text = re.sub(
                r"^(?:File written to|Wrote file|Updated file|Created file|Modified file|Deleted file)\s+",
                "",
                text,
                flags=re.IGNORECASE,
            )
            text = re.sub(r"\s+\((?:\d+|[\d.]+)\s*(?:bytes|chars|characters|字节|字符).*?\)$", "", text, flags=re.IGNORECASE)
            text = text.rstrip("。；;，,").strip()
            file_path_match = re.match(r"^(.+\.(?:md|markdown|json|jsonl|txt|yml|yaml|csv|toml))(?:\s+.*)?$", text, flags=re.IGNORECASE)
            if file_path_match:
                text = file_path_match.group(1).strip()
            if not text or len(text) > 500 or any(token in text for token in ("\r", "\n", "{", "}")):
                continue
            try:
                candidate = Path(text)
                if candidate.is_absolute():
                    try:
                        normalized = candidate.resolve().relative_to(root).as_posix()
                    except (OSError, ValueError):
                        continue
                else:
                    if any(part == ".." for part in text.split("/")):
                        continue
                    normalized = text.lstrip("./").strip("/")
            except OSError:
                continue
            if not normalized or normalized == "." or any(part == ".." for part in normalized.split("/")):
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
    return result


def _merge_changed_file_lists(primary: Any, fallback: List[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for raw in [*(primary if isinstance(primary, list) else []), *fallback]:
        normalized = str(raw or "").replace("\\", "/").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _include_missing_agent_snapshot_diffs(
    data: Dict[str, Any],
    workspace_root: Path,
    changed_files: List[str],
) -> Dict[str, Any]:
    files = data.get("files") if isinstance(data.get("files"), list) else []
    existing_paths = {
        str(item.get("relativePath") or "").replace("\\", "/").strip()
        for item in files
        if isinstance(item, dict)
    }
    root = Path(workspace_root).resolve()
    missing_paths: List[str] = []
    for raw_path in changed_files:
        relative_path = str(raw_path or "").replace("\\", "/").strip().strip("/")
        if not relative_path or relative_path in existing_paths or any(part == ".." for part in relative_path.split("/")):
            continue
        candidate = (root / relative_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.is_file():
            missing_paths.append(relative_path)
    if not missing_paths:
        return data

    snapshot = git_service.build_file_snapshot_diff(root, paths=missing_paths, status="A")
    snapshot_files = [
        item for item in (snapshot.get("files") if isinstance(snapshot.get("files"), list) else [])
        if isinstance(item, dict)
    ]
    if not snapshot_files:
        return data
    merged_files = [*files, *snapshot_files]
    next_data = dict(data)
    next_data["files"] = merged_files
    next_data["totals"] = {
        "files": len(merged_files),
        "added": sum(int(item.get("added") or 0) for item in merged_files if isinstance(item, dict)),
        "removed": sum(int(item.get("removed") or 0) for item in merged_files if isinstance(item, dict)),
    }
    return next_data


def _read_agent_run_record(trace_id: str, session_id: str) -> tuple[Dict[str, Any] | None, str]:
    normalized_trace_id = str(trace_id or "").strip()
    normalized_session_id = str(session_id or "").strip() or "default"
    record = trace_history_service.read_record(normalized_trace_id, normalized_session_id)
    if record is not None:
        return record, str(record.get("sessionId") or normalized_session_id)
    for summary in trace_history_service.list_session_summaries():
        candidate_session = str(summary.get("sessionId") or "").strip()
        if not candidate_session or candidate_session == normalized_session_id:
            continue
        record = trace_history_service.read_record(normalized_trace_id, candidate_session)
        if record is not None:
            return record, str(record.get("sessionId") or candidate_session)
    return None, normalized_session_id


def _record_workspace_root(record: Dict[str, Any]) -> Path:
    raw_root = str(record.get("workspaceRoot") or "").strip()
    if raw_root:
        candidate = Path(raw_root).expanduser()
        if candidate.exists() and candidate.is_dir():
            return candidate.resolve()
    return project_service.workspace_root


def _record_change_ledger(record: Dict[str, Any], *, trace_id: str, session_id: str) -> Dict[str, Any]:
    ledger = record.get("changeLedger") if isinstance(record.get("changeLedger"), dict) else {}
    extracted = _extract_change_ledger(
        record.get("events") if isinstance(record.get("events"), list) else [],
        trace_id=trace_id,
        session_id=session_id,
    )
    if not ledger:
        return extracted
    changed_files = ledger.get("changedFiles") if isinstance(ledger.get("changedFiles"), list) else extracted.get("changedFiles")
    commit_hash = str(ledger.get("commitHash") or extracted.get("commitHash") or "").strip()
    diff_source = str(ledger.get("diffSource") or extracted.get("diffSource") or ("commit" if commit_hash else "")).strip()
    normalized_files = [
        str(path).replace("\\", "/").strip()
        for path in (changed_files if isinstance(changed_files, list) else [])
        if str(path).strip()
    ]
    return {
        "traceId": str(ledger.get("traceId") or extracted.get("traceId") or trace_id),
        "sessionId": str(ledger.get("sessionId") or extracted.get("sessionId") or session_id),
        "changedFiles": normalized_files,
        "changedFileCount": int(ledger.get("changedFileCount") or extracted.get("changedFileCount") or len(normalized_files)),
        "added": int(ledger.get("added") or extracted.get("added") or 0),
        "removed": int(ledger.get("removed") or extracted.get("removed") or 0),
        "diffSource": diff_source if diff_source in {"working_tree", "commit"} else "",
        "commitHash": commit_hash,
        "shortHash": str(ledger.get("shortHash") or extracted.get("shortHash") or "").strip(),
        "updatedAt": str(ledger.get("updatedAt") or extracted.get("updatedAt") or ""),
    }


def _empty_agent_run_diff_payload(
    workspace_root: Path,
    *,
    message: str,
    trace_id: str,
    session_id: str,
) -> Dict[str, Any]:
    root = Path(workspace_root).resolve()
    try:
        summary = git_service.read_summary(root)
        return {
            "available": bool(summary.get("available", True)),
            "gitInstalled": bool(summary.get("gitInstalled", True)),
            "initialized": bool(summary.get("initialized", False)),
            "branch": str(summary.get("branch") or ""),
            "files": [],
            "totals": {"files": 0, "added": 0, "removed": 0},
            "message": message,
            "traceId": trace_id,
            "sessionId": session_id,
        }
    except Exception:
        return {
            "available": False,
            "gitInstalled": False,
            "initialized": False,
            "branch": "",
            "files": [],
            "totals": {"files": 0, "added": 0, "removed": 0},
            "message": message,
            "traceId": trace_id,
            "sessionId": session_id,
        }


def _build_commit_message_diff_summary(workspace_root: Path, changed_files: List[str], *, max_chars: int = 9000) -> str:
    try:
        diff_payload = git_service.read_diff(workspace_root, paths=changed_files)
    except GitServiceError:
        return ""
    totals = diff_payload.get("totals") if isinstance(diff_payload.get("totals"), dict) else {}
    lines = [
        f"files={int(totals.get('files') or 0)} added={int(totals.get('added') or 0)} removed={int(totals.get('removed') or 0)}"
    ]
    files = diff_payload.get("files") if isinstance(diff_payload.get("files"), list) else []
    for item in files:
        if not isinstance(item, dict):
            continue
        relative_path = str(item.get("relativePath") or "").replace("\\", "/").strip()
        status = str(item.get("status") or "").strip()
        lines.append(
            f"{status or 'M'} {relative_path} +{int(item.get('added') or 0)} -{int(item.get('removed') or 0)}"
        )
        hunks = item.get("hunks") if isinstance(item.get("hunks"), list) else []
        for hunk in hunks[:2]:
            if not isinstance(hunk, dict):
                continue
            hunk_lines = hunk.get("lines") if isinstance(hunk.get("lines"), list) else []
            for line in hunk_lines[:24]:
                if not isinstance(line, dict):
                    continue
                kind = str(line.get("kind") or "").strip()
                if kind not in {"added", "removed"}:
                    continue
                prefix = "+" if kind == "added" else "-"
                content = str(line.get("content") or "").strip()
                if content:
                    lines.append(f"{prefix} {content[:220]}")
        text = "\n".join(lines)
        if len(text) >= max_chars:
            return text[:max_chars]
    return "\n".join(lines)[:max_chars]


def _append_git_commit_decision_record(
    *,
    trace_id: str,
    session_id: str,
    payload: Dict[str, Any],
) -> None:
    record, resolved_session_id = _read_agent_run_record(trace_id, session_id)
    if record is None:
        return
    events = list(record.get("events") if isinstance(record.get("events"), list) else [])
    event_name = _git_event_name(payload)
    events.append(_event_to_trace_event(event_name, payload, len(events) + 1))
    next_record = dict(record)
    next_record["events"] = events
    next_record["changeLedger"] = _extract_change_ledger(events, trace_id=trace_id, session_id=resolved_session_id)
    if event_name == "GitCommitResult" and bool(payload.get("created")):
        next_record["status"] = "committed"
    next_record["updatedAt"] = _now_iso()
    trace_history_service.upsert_record(next_record, resolved_session_id)


@router.post("/agent/clear-conversation", response_model=ApiEnvelope)
def agent_clear_conversation(
    request: Request,
    session_id_query: Optional[str] = Query(default=None, alias="sessionId"),
) -> ApiEnvelope:
    session_id = str(session_id_query or "").strip() or _resolve_agent_session_id(request)
    workspace_root = project_service.workspace_root
    get_storydex_coomi_agent_service().clear_session(
        session_id,
        workspace_root=workspace_root,
        delete_history=True,
    )
    storydex_intent_service.clear_session(session_id=session_id, workspace_root=workspace_root)
    cleared_history_count = trace_history_service.clear_records(session_id)
    trace_history_service.mark_session_cleared(session_id)
    return success_response(
        data={
            "cleared": True,
            "sessionId": session_id,
            "historyClearedCount": cleared_history_count,
            "runtime": "coomi",
        }
    )


@router.post("/agent/chat", response_model=ApiEnvelope)
async def agent_chat(
    payload: AgentChatRequest,
    request: Request,
    session_id_query: Optional[str] = Query(default=None, alias="sessionId"),
) -> ApiEnvelope:
    trace_id = _resolve_agent_trace_id(request)
    session_id = str(session_id_query or "").strip() or _resolve_agent_session_id(request)
    if not _try_acquire_agent_generation_slot():
        raise _agent_busy_error(trace_id=trace_id, session_id=session_id)
    execution_handle: ExecutionHandle | None = None
    replacement: _LatestExecutionReplacement | None = None
    try:
        workspace_root = _resolve_agent_workspace_root(payload)
        if payload.source_followup_message_id:
            raise StorydexError(
                "Queued follow-ups must be dispatched through the streaming endpoint.",
                code="followup_stream_required",
                status_code=400,
            )
        if payload.replace_latest_trace_id:
            replacement = _LatestExecutionReplacement(
                session_id=session_id,
                expected_trace_id=payload.replace_latest_trace_id,
                replacement_trace_id=trace_id,
                workspace_root=workspace_root,
                replacement_prompt=payload.prompt,
            )
            await asyncio.to_thread(replacement.prepare)
        execution_handle = execution_coordinator.adopt_reservation_or_begin(
            workspace_root,
            session_id,
            trace_id,
        )
        cancellation_token = _CancellationToken()
        async for _chunk in _stream_agent_chat_request_sse(
            payload=payload,
            request=request,
            trace_id=trace_id,
            session_id=session_id,
            cancellation_token=cancellation_token,
            execution_handle=execution_handle,
            resolved_workspace_root=workspace_root,
            raise_preflight_errors=True,
            replacement=replacement,
        ):
            pass
        result = await execution_handle.wait_finalized()
        if result is None:
            raise StorydexError(
                "Agent execution ended before finalization completed.",
                code="execution_unfinished",
                status_code=500,
                details={"traceId": trace_id, "sessionId": session_id},
            )
        payload_data = result.payload_data
        return success_response(
            data=payload_data["data"],
            trace=ApiTrace(**payload_data["trace"]),
            audit=payload_data["audit"],
        )
    finally:
        if execution_handle is None:
            if replacement is not None:
                replacement.restore(reason="execution_start_failed")
            _release_agent_generation_slot()


async def _finalize_cancelled_preflight_execution(
    *,
    payload: AgentChatRequest,
    trace_id: str,
    session_id: str,
    workspace_root: Path,
    request_started: float,
    accepted: Dict[str, Any],
    execution_handle: ExecutionHandle,
    execution_log_session: ExecutionLogSession | None,
    git_snapshot: AgentGitSnapshot | None,
    git_task: asyncio.Task[AgentGitSnapshot] | None,
    intent_task: asyncio.Task[Dict[str, Any]] | None,
    contract_task: asyncio.Task[Dict[str, Any]] | None,
) -> None:
    """Finish an accepted preflight cancellation independently of the SSE transport."""

    try:
        for task in (intent_task, contract_task):
            if task is None:
                continue
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
            except Exception:
                # Preparation failures do not override an already accepted cancellation.
                pass

        snapshot = git_snapshot
        snapshot_error = ""
        if snapshot is None and git_task is not None:
            try:
                candidate = await asyncio.shield(git_task)
                if isinstance(candidate, AgentGitSnapshot):
                    snapshot = candidate
            except asyncio.CancelledError:
                if not git_task.cancelled():
                    raise
                snapshot_error = "Git snapshot preparation was cancelled."
            except Exception as exc:
                snapshot_error = str(exc)
        if snapshot is None:
            snapshot = AgentGitSnapshot(
                workspace_root=workspace_root,
                available=False,
                error_message=snapshot_error or "Git snapshot preparation did not complete.",
            )

        execution_handle.register_snapshot(snapshot, confirm_no_snapshot=True)
        events: List[Dict[str, Any]] = [
            _event_to_trace_event("RunAccepted", dict(accepted), 1)
        ]

        def finish_git_turn() -> Dict[str, Any]:
            return agent_git_autocommit_service.finish_turn(
                snapshot,
                prompt=payload.prompt,
                commit_prompt_enabled=_agent_commit_prompt_enabled(workspace_root),
            )

        def on_git_payload(git_payload: Dict[str, Any]) -> None:
            git_payload["traceId"] = trace_id
            git_payload["sessionId"] = session_id
            event_name = _git_event_name(git_payload)
            events.append(_event_to_trace_event(event_name, git_payload, len(events) + 1))

        def on_terminal(status: str, _terminal_error: str) -> None:
            packet = {
                "_type": "AgentCancelled",
                "_version": 1,
                "traceId": trace_id,
                "sessionId": session_id,
                "session_id": session_id,
                "reason": execution_handle.cancel_reason or "client_disconnected",
            }
            events.append(_event_to_trace_event("AgentCancelled", packet, len(events) + 1))

        def build_payload(
            status: str,
            terminal_error: str,
            no_restore_point: bool,
            _timings: Dict[str, float],
        ) -> Dict[str, Any]:
            payload_data = _build_chat_payload(
                trace_id=trace_id,
                prompt=payload.prompt,
                reply="",
                events=events,
                started=request_started,
                workspace_root=workspace_root,
                session_id=session_id,
                execution_log_session=execution_log_session,
                status=status,
                error_message=terminal_error,
            )
            record = payload_data.get("record")
            if isinstance(record, dict):
                record["noRestorePoint"] = no_restore_point
            return payload_data

        def write_timing(timing_payload: Dict[str, Any]) -> None:
            if execution_log_session is not None:
                execution_log_session.write(
                    "execution_coordinator_timing",
                    timing_payload,
                    category="observability",
                )

        await execution_handle.finalize(
            ExecutionObservation(
                cancelled=True,
                error_code="client_disconnected",
            ),
            ExecutionFinalizationContext(
                finish_git=finish_git_turn,
                on_git_payload=on_git_payload,
                on_terminal=on_terminal,
                build_payload=build_payload,
                persist_trace=lambda record: _persist_execution_trace(
                    workspace_root,
                    record,
                    session_id,
                ),
                write_timing=write_timing,
            ),
        )
    except asyncio.CancelledError:
        execution_handle.abandon("preflight_finalizer_cancelled")
        raise
    except Exception:
        _LOGGER.exception("Preflight cancellation finalization failed for %s", trace_id)
        execution_handle.abandon("preflight_finalization_failed")
    finally:
        reset_llm_metrics(trace_id)


async def _stream_agent_chat_request_sse(
    *,
    payload: AgentChatRequest,
    request: Request,
    trace_id: str,
    session_id: str,
    cancellation_token: _CancellationToken,
    context_policy_override: ContextPolicy | None = None,
    execution_handle: ExecutionHandle | None = None,
    resolved_workspace_root: Path | None = None,
    raise_preflight_errors: bool = False,
    replacement: _LatestExecutionReplacement | None = None,
) -> AsyncIterator[str]:
    reset_llm_metrics(trace_id)
    request_started = time.perf_counter()
    workspace_root = resolved_workspace_root or _resolve_agent_workspace_root(payload)
    if execution_handle is None:
        execution_handle = execution_coordinator.adopt_reservation_or_begin(
            workspace_root,
            session_id,
            trace_id,
        )
    execution_handle.bind_cancellation(lambda _reason: cancellation_token.cancel())
    followup_mailbox_service.set_active_trace(
        workspace_root=workspace_root,
        session_id=session_id,
        trace_id=trace_id,
    )
    if payload.confirm_no_snapshot:
        mailbox_state = followup_mailbox_service.list_mailbox(
            workspace_root=workspace_root,
            session_id=session_id,
        )
        if str(mailbox_state.get("pauseReason") or "") == "snapshot_confirmation":
            followup_mailbox_service.resume(workspace_root=workspace_root, session_id=session_id)
    accepted = {
        "_type": "RunAccepted",
        "_version": 1,
        "traceId": trace_id,
        "sessionId": session_id,
        "phase": "accepted",
        "label": "请求已接收",
        "detail": "正在准备 Storydex 执行环境",
        "status": "running",
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "elapsedMs": 0,
        "noRestorePoint": bool(payload.confirm_no_snapshot),
    }

    git_snapshot: AgentGitSnapshot | None = None
    delegated = False
    preflight_rejected = False
    git_task: asyncio.Task[AgentGitSnapshot] | None = None
    intent_task: asyncio.Task[Dict[str, Any]] | None = None
    contract_task: asyncio.Task[Dict[str, Any]] | None = None
    execution_log_session: ExecutionLogSession | None = None
    try:
        git_task = asyncio.create_task(
            asyncio.to_thread(agent_git_autocommit_service.begin_turn, workspace_root)
        )
        yield _encode_sse("RunAccepted", accepted)

        execution_log_session = _create_agent_execution_log_session(
            trace_id=trace_id,
            session_id=session_id,
        )
        story_generation = _normalize_story_generation_options(payload.story_generation)

        intent_started = time.perf_counter()
        yield _encode_sse(
            "TurnPhase",
            _turn_phase_packet(
                trace_id=trace_id,
                session_id=session_id,
                phase="intent_classification",
                label="正在识别执行意图",
                status="running",
                phase_started=intent_started,
            ),
        )
        with llm_trace(trace_id):
            intent_task = asyncio.create_task(
                _classify_intent_without_blocking_event_loop(
                    prompt=payload.prompt,
                    active_file=payload.active_file,
                    workspace_root=workspace_root,
                    session_id=session_id,
                )
            )
        while not intent_task.done():
            done, _ = await asyncio.wait({intent_task}, timeout=_PHASE_HEARTBEAT_SECONDS)
            if done:
                break
            if await request.is_disconnected():
                execution_handle.cancel("client_disconnected")
                return
            yield _encode_sse(
                "TurnPhase",
                _turn_phase_packet(
                    trace_id=trace_id,
                    session_id=session_id,
                    phase="intent_classification",
                    label="正在识别执行意图",
                    status="running",
                    phase_started=intent_started,
                    heartbeat=True,
                ),
            )
        intent_frame = await intent_task
        yield _encode_sse(
            "TurnPhase",
            _turn_phase_packet(
                trace_id=trace_id,
                session_id=session_id,
                phase="intent_classification",
                label="执行意图识别完成",
                status="success",
                phase_started=intent_started,
                detail=(
                    f"{str(intent_frame.get('primary') or 'general')}"
                    f" · {str(intent_frame.get('method') or 'unknown')}"
                ),
            ),
        )

        context_started = time.perf_counter()
        yield _encode_sse(
            "TurnPhase",
            _turn_phase_packet(
                trace_id=trace_id,
                session_id=session_id,
                phase="context_assembly",
                label="正在组装项目上下文",
                status="running",
                phase_started=context_started,
            ),
        )
        contract_task = asyncio.create_task(
            asyncio.to_thread(
                storydex_orchestration_service.build_turn_contract,
                workspace_root,
                prompt=payload.prompt,
                active_file=payload.active_file,
                story_generation=story_generation,
                intent_frame=intent_frame,
                context_policy=context_policy_override,
            )
        )
        while not contract_task.done():
            done, _ = await asyncio.wait({contract_task}, timeout=_PHASE_HEARTBEAT_SECONDS)
            if done:
                break
            if await request.is_disconnected():
                execution_handle.cancel("client_disconnected")
                return
            yield _encode_sse(
                "TurnPhase",
                _turn_phase_packet(
                    trace_id=trace_id,
                    session_id=session_id,
                    phase="context_assembly",
                    label="正在组装项目上下文",
                    status="running",
                    phase_started=context_started,
                    heartbeat=True,
                ),
            )
        turn_contract = await contract_task
        story_generation = _apply_turn_contract_story_generation_defaults(story_generation, turn_contract)
        context_assembly = turn_contract.get("contextAssembly") if isinstance(turn_contract, dict) else {}
        budget = context_assembly.get("budget") if isinstance(context_assembly, dict) else {}
        yield _encode_sse(
            "TurnPhase",
            _turn_phase_packet(
                trace_id=trace_id,
                session_id=session_id,
                phase="context_assembly",
                label="项目上下文组装完成",
                status="success",
                phase_started=context_started,
                detail=f"已准备 {int((budget or {}).get('blockCount') or 0)} 个上下文块",
            ),
        )

        snapshot_started = time.perf_counter()
        while git_task is not None and not git_task.done():
            done, _ = await asyncio.wait({git_task}, timeout=_PHASE_HEARTBEAT_SECONDS)
            if done:
                break
            if await request.is_disconnected():
                execution_handle.cancel("client_disconnected")
                return
            yield _encode_sse(
                "TurnPhase",
                _turn_phase_packet(
                    trace_id=trace_id,
                    session_id=session_id,
                    phase="workspace_snapshot",
                    label="正在读取项目版本状态",
                    status="running",
                    phase_started=snapshot_started,
                    heartbeat=True,
                ),
            )
        if git_task is None:
            raise RuntimeError("Git snapshot task was not initialized.")
        git_snapshot = await git_task
        execution_handle.register_snapshot(
            git_snapshot,
            confirm_no_snapshot=payload.confirm_no_snapshot,
        )
        yield _encode_sse(
            "TurnPhase",
            {
                **_turn_phase_packet(
                    trace_id=trace_id,
                    session_id=session_id,
                    phase="workspace_snapshot",
                    label=(
                        "将在无恢复点状态下继续"
                        if execution_handle.no_restore_point
                        else "项目恢复点已就绪"
                    ),
                    status="warning" if execution_handle.no_restore_point else "success",
                    phase_started=snapshot_started,
                ),
                "noRestorePoint": execution_handle.no_restore_point,
            },
        )

        if replacement is not None:
            turn_contract = {
                **turn_contract,
                "replacement": {
                    "replacesTraceId": replacement.expected_trace_id,
                    "replacementTraceId": trace_id,
                    "dialogueOnly": True,
                    "fileChangesReverted": False,
                },
            }
        delegated = True
        async for chunk in _stream_coomi_sse(
            prompt=payload.prompt,
            trace_id=trace_id,
            session_id=session_id,
            active_file=payload.active_file,
            workspace_root=workspace_root,
            story_generation=story_generation,
            turn_contract=turn_contract,
            git_snapshot=git_snapshot,
            request=request,
            cancellation_token=cancellation_token,
            execution_handle=execution_handle,
            execution_log_session=execution_log_session,
            replacement=replacement,
        ):
            yield chunk
    except SnapshotConfirmationRequired as exc:
        preflight_rejected = True
        followup_mailbox_service.pause(
            workspace_root=workspace_root,
            session_id=session_id,
            reason="snapshot_confirmation",
        )
        execution_handle.reject_preflight(exc.code, str(exc))
        details = {
            **exc.details,
            "confirmNoSnapshotRequired": True,
        }
        if raise_preflight_errors:
            raise StorydexError(
                str(exc),
                code=exc.code,
                status_code=409,
                details=details,
            ) from exc
        packet = {
            "_type": "AgentError",
            "_version": 1,
            "traceId": trace_id,
            "sessionId": session_id,
            "error_type": exc.code,
            "code": exc.code,
            "message": str(exc),
            "details": details,
            "duration_ms": int((time.perf_counter() - request_started) * 1000),
        }
        yield _encode_sse("AgentError", packet)
        yield _encode_sse("done", {"type": "done"})
    except Exception as exc:
        preflight_rejected = True
        followup_mailbox_service.pause(
            workspace_root=workspace_root,
            session_id=session_id,
            reason="preflight_error",
        )
        execution_handle.reject_preflight(type(exc).__name__, str(exc))
        if raise_preflight_errors:
            raise
        packet = {
            "_type": "AgentError",
            "_version": 1,
            "traceId": trace_id,
            "sessionId": session_id,
            "error_type": type(exc).__name__,
            "message": str(exc),
            "duration_ms": int((time.perf_counter() - request_started) * 1000),
        }
        yield _encode_sse("AgentError", packet)
        yield _encode_sse("done", {"type": "done"})
    finally:
        followup_mailbox_service.clear_active_trace(
            workspace_root=workspace_root,
            session_id=session_id,
            expected_trace_id=trace_id,
        )
        if not delegated:
            if replacement is not None:
                replacement.restore(reason="preflight_not_accepted")
            if preflight_rejected:
                for task in (intent_task, contract_task, git_task):
                    if task is not None and not task.done():
                        task.cancel()
                if git_snapshot is not None:
                    try:
                        agent_git_autocommit_service.finish_turn(
                            git_snapshot,
                            prompt=payload.prompt,
                            commit_prompt_enabled=_agent_commit_prompt_enabled(workspace_root),
                        )
                    except Exception:
                        pass
                reset_llm_metrics(trace_id)
            else:
                execution_handle.cancel("client_disconnected")
                followup_mailbox_service.pause(
                    workspace_root=workspace_root,
                    session_id=session_id,
                    reason="client_disconnected",
                )
                _retain_background_execution_task(
                    asyncio.create_task(
                        _finalize_cancelled_preflight_execution(
                            payload=payload,
                            trace_id=trace_id,
                            session_id=session_id,
                            workspace_root=workspace_root,
                            request_started=request_started,
                            accepted=accepted,
                            execution_handle=execution_handle,
                            execution_log_session=execution_log_session,
                            git_snapshot=git_snapshot,
                            git_task=git_task,
                            intent_task=intent_task,
                            contract_task=contract_task,
                        ),
                        name=f"storydex-preflight-finalize-{trace_id}",
                    )
                )


def _decode_sse_packet(chunk: str) -> Dict[str, Any]:
    for line in str(chunk or "").splitlines():
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line.removeprefix("data:").strip())
        except (ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


async def _stream_agent_chat_with_followups_sse(
    *,
    payload: AgentChatRequest,
    request: Request,
    trace_id: str,
    session_id: str,
    cancellation_token: _CancellationToken,
    replacement: _LatestExecutionReplacement | None = None,
    initial_source_message: Dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    """Run one accepted request and drain durable FIFO follow-ups in-band."""

    current_payload = payload
    current_trace_id = trace_id
    current_token = cancellation_token
    source_message: Dict[str, Any] | None = initial_source_message
    current_replacement = replacement
    workspace_root = _resolve_agent_workspace_root(payload)
    final_done = _encode_sse("done", {"type": "done"})

    while True:
        saw_error = False
        saw_cancel = False
        source_marked_sent = source_message is None
        async for chunk in _stream_agent_chat_request_sse(
            payload=current_payload,
            request=request,
            trace_id=current_trace_id,
            session_id=session_id,
            cancellation_token=current_token,
            resolved_workspace_root=workspace_root,
            replacement=current_replacement,
        ):
            packet = _decode_sse_packet(chunk)
            event_name = str(packet.get("_type") or packet.get("type") or "")
            if event_name == "done":
                continue
            if event_name == "AgentError":
                saw_error = True
            elif event_name == "AgentCancelled":
                saw_cancel = True
            if source_message is not None and not source_marked_sent:
                if event_name in {"TaskPlanCreated", "TurnContract", "AgentStarted"} or (
                    event_name == "TurnPhase" and str(packet.get("phase") or "") == "task_planning"
                ):
                    try:
                        followup_mailbox_service.mark_dispatch_sent(
                            workspace_root=workspace_root,
                            session_id=session_id,
                            message_id=str(source_message.get("messageId") or ""),
                            trace_id=current_trace_id,
                        )
                        source_marked_sent = True
                    except FollowupMailboxError:
                        pass
            yield chunk

        if source_message is not None and not source_marked_sent:
            try:
                followup_mailbox_service.mark_dispatch_failed(
                    workspace_root=workspace_root,
                    session_id=session_id,
                    message_id=str(source_message.get("messageId") or ""),
                    trace_id=current_trace_id,
                    error="Continuation preprocessing failed before model execution.",
                    # No acceptance event means the queued turn never reached
                    # task planning/model execution.  Keep it pending so a
                    # snapshot confirmation, repaired config, or reconnect can
                    # retry the same idempotency key.
                    retryable=True,
                )
            except FollowupMailboxError:
                pass

        state = followup_mailbox_service.list_mailbox(
            workspace_root=workspace_root,
            session_id=session_id,
        )
        if saw_error and not bool(state.get("paused")):
            followup_mailbox_service.pause(
                workspace_root=workspace_root,
                session_id=session_id,
                reason="execution_error",
            )
        elif saw_cancel and not bool(state.get("paused")):
            followup_mailbox_service.pause(
                workspace_root=workspace_root,
                session_id=session_id,
                reason="execution_stopped",
            )

        state = followup_mailbox_service.list_mailbox(
            workspace_root=workspace_root,
            session_id=session_id,
        )
        if saw_error or saw_cancel or bool(state.get("paused")):
            break

        # Reserve before claiming so an unrelated user request cannot create an
        # agent_busy race between current finalization and FIFO continuation.
        if not _try_acquire_agent_generation_slot():
            break
        next_trace_id = str(uuid4())
        try:
            next_message = followup_mailbox_service.claim_next_queued(
                workspace_root=workspace_root,
                session_id=session_id,
                previous_trace_id=current_trace_id,
                next_trace_id=next_trace_id,
            )
        except Exception:
            _release_agent_generation_slot()
            raise
        if next_message is None:
            _release_agent_generation_slot()
            break

        continuation_packet = {
            "_type": "ContinuationStarted",
            "_version": 1,
            **next_message,
            "traceId": next_trace_id,
            "previousTraceId": current_trace_id,
            "continuationMode": "queued",
        }
        yield _encode_sse("ContinuationStarted", continuation_packet)
        current_payload = AgentChatRequest(
            prompt=str(next_message.get("content") or ""),
            activeFile=current_payload.active_file,
            workspaceRoot=workspace_root.as_posix(),
            storyGeneration=dict(current_payload.story_generation),
        )
        current_trace_id = next_trace_id
        current_token = _CancellationToken()
        source_message = next_message
        current_replacement = None

    yield final_done


@router.post("/agent/chat/stream")
async def agent_chat_stream(
    payload: AgentChatRequest,
    request: Request,
    session_id_query: Optional[str] = Query(default=None, alias="sessionId"),
) -> StreamingResponse:
    trace_id = _resolve_agent_trace_id(request)
    session_id = str(session_id_query or "").strip() or _resolve_agent_session_id(request)
    if not _try_acquire_agent_generation_slot():
        raise _agent_busy_error(trace_id=trace_id, session_id=session_id)
    workspace_root = _resolve_agent_workspace_root(payload)
    replacement: _LatestExecutionReplacement | None = None
    source_message: Dict[str, Any] | None = None
    try:
        payload, source_message = _claim_initial_followup_dispatch(
            payload=payload,
            workspace_root=workspace_root,
            session_id=session_id,
            trace_id=trace_id,
        )
        if payload.replace_latest_trace_id:
            replacement = _LatestExecutionReplacement(
                session_id=session_id,
                expected_trace_id=payload.replace_latest_trace_id,
                replacement_trace_id=trace_id,
                workspace_root=workspace_root,
                replacement_prompt=payload.prompt,
            )
            await asyncio.to_thread(replacement.prepare)
    except Exception:
        _release_agent_generation_slot()
        raise
    cancellation_token = _CancellationToken()
    return StreamingResponse(
        _stream_agent_chat_with_followups_sse(
            payload=payload,
            request=request,
            trace_id=trace_id,
            session_id=session_id,
            cancellation_token=cancellation_token,
            replacement=replacement,
            initial_source_message=source_message,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
