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
from typing import Any, AsyncIterator, Callable, Dict, List, Literal, Optional, Sequence
from uuid import uuid4

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from api.response import ApiEnvelope, ApiTrace, success_response
from core.config import FEATURE_FLAG_DEFAULTS
from core.exceptions import GitServiceError, StorydexError
from core.feature_flags import FeatureFlags
from services.agent_intent_routing import (
    ROUTING_MODE_FLAG,
    build_route_hints,
    likely_candidate_paths,
    normalize_routing_mode,
)
from services.agent_git_autocommit_service import AgentGitSnapshot, get_agent_git_autocommit_service
from services.coomi_agent_service import (
    CoomiStoryGenerationAdapter,
    StorydexCoomiSessionRestoreError,
    get_storydex_coomi_agent_service,
)
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
from services.agent_lifecycle_trace import build_agent_lifecycle_trace
from services.project_service import get_project_service
from services.story_project_service import (
    DEFAULT_CHAPTER_WORD_COUNT_TARGET,
    get_story_project_service,
)
from services.story_bounded_generation_service import BoundedStoryGeneration
from services.story_call_accounting import (
    STORY_INITIAL_GENERATION_PURPOSE,
    STORY_LENGTH_REVISION_PURPOSE,
    STORY_SECOND_DRAFT_PURPOSE,
    StoryCallAccounting,
)
from services.story_chapter_action_service import parse_chapter_range, validate_chapter_plan
from services.story_generation_pipeline import get_story_generation_pipeline
from services.story_length_calibration_service import (
    INITIAL_ATTEMPT_KIND,
    PRECISION_REVISION_ATTEMPT_KIND,
    get_story_length_calibration_service,
)
from services.story_length_tier_calibration_service import (
    get_story_length_tier_calibration_service,
)
from services.story_length_precision_controller import (
    get_story_length_precision_controller,
)
from services.story_word_count_service import (
    STORY_OVER_BUDGET_KEEP_MESSAGE,
    STORY_UNDER_BUDGET_KEEP_MESSAGE,
    STORY_WORD_COUNT_RULE,
    WORD_COUNT_POLICY_VERSION,
    chapter_normal_band,
    chapter_precision_band,
    migrate_chapter_word_count_target,
    normalize_chapter_length_tier,
)
from services.story_semantic_budget_context import read_scene_constraint_context
from services.story_semantic_budget_controller import (
    SEMANTIC_BUDGET_STRATEGY,
    SemanticBudgetController,
    SemanticBudgetRequest,
    SemanticBudgetResult,
    automatic_scene_revision_limit,
)
from services.story_wiki_service import get_story_wiki_service
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
story_length_calibration_service = get_story_length_calibration_service()
story_length_tier_calibration_service = get_story_length_tier_calibration_service()
execution_coordinator = get_execution_coordinator()
followup_mailbox_service = get_followup_mailbox_service()

_PHASE_HEARTBEAT_SECONDS = 0.6
_COMMIT_MESSAGE_TIMEOUT_SECONDS = 2.0
# The Rust bridge starts a bounded one-shot provider process for intent JSON.
# Cold DeepSeek-compatible requests take roughly 10-14 seconds in the packaged
# Windows path. The stream emits heartbeats throughout, so keep a hard deadline
# above the service-level 20-second bound instead of forcing every real turn
# into the fail-closed read-only fallback.
_INTENT_STAGE_TIMEOUT_SECONDS = 22.0
# Legacy contracts only. Current chapter-scoped word-count contracts run the
# bounded path, which resolves length before the single write instead of using a
# write-then-append correction continuation.
_STORY_GENERATION_MAX_CORRECTIONS = 1
BOUNDED_STORY_GENERATION_STRATEGY = "bounded_story_generation"
# The bounded path needs both a chapter-scoped policy and the current word-count
# policy version; version alone cannot select it, since other gates still decide
# whether this turn may bypass the legacy Agent tool loop.
BOUNDED_WORD_COUNT_POLICY_VERSION = WORD_COUNT_POLICY_VERSION
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
        from services.storydex_intent_service import safe_fallback_intent_frame

        return safe_fallback_intent_frame(reason="intent_stage_deadline_exceeded")


class AgentChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=12000)
    active_file: str = Field(default="", alias="activeFile")
    workspace_root: str = Field(default="", alias="workspaceRoot")
    reasoning_effort: Literal["auto", "low", "medium", "high", "xhigh", "max"] = Field(
        default="high",
        alias="reasoningEffort",
    )
    story_generation: Dict[str, Any] = Field(default_factory=dict, alias="storyGeneration")
    confirm_no_snapshot: bool = Field(default=False, alias="confirmNoSnapshot")
    replace_latest_trace_id: str = Field(default="", alias="replaceLatestTraceId")
    source_followup_message_id: str = Field(default="", alias="sourceFollowupMessageId")
    source_followup_expected_trace_id: str = Field(default="", alias="sourceFollowupExpectedTraceId")
    timeout_ms: int = Field(default=0, alias="timeoutMs", ge=0, le=600000)

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


class AgentReasoningWireFieldData(BaseModel):
    path: str = ""
    value: Any = None


class AgentReasoningLevelCapabilityData(BaseModel):
    effort: Literal["auto", "low", "medium", "high", "xhigh", "max"] = "auto"
    control: Literal["auto", "native", "prompt"] = "auto"
    wire_fields: List[AgentReasoningWireFieldData] = Field(default_factory=list, alias="wireFields")
    route_sensitive: bool = Field(default=False, alias="routeSensitive")

    model_config = ConfigDict(populate_by_name=True)


class AgentReasoningCapabilityData(BaseModel):
    support: Literal["supported", "unsupported", "unknown"] = "unknown"
    levels: List[AgentReasoningLevelCapabilityData] = Field(default_factory=list)
    source: Literal["model_config", "provider_config", "model_rule", "unknown"] = "unknown"
    prompt_fallback: bool = Field(default=False, alias="promptFallback")
    route_sensitive: bool = Field(default=False, alias="routeSensitive")
    fallback_reason: str = Field(default="", alias="fallbackReason")

    model_config = ConfigDict(populate_by_name=True)


class AgentReasoningRequestPlanData(BaseModel):
    requested: Literal["auto", "low", "medium", "high", "xhigh", "max"] = "auto"
    control: Literal["auto", "native", "prompt"] = "auto"
    sent: bool = False
    prompt_applied: bool = Field(default=False, alias="promptApplied")
    wire_fields: List[AgentReasoningWireFieldData] = Field(default_factory=list, alias="wireFields")
    support: Literal["supported", "unsupported", "unknown"] = "unknown"
    source: Literal["model_config", "provider_config", "model_rule", "unknown"] = "unknown"
    route_sensitive: bool = Field(default=False, alias="routeSensitive")
    fallback_reason: str = Field(default="", alias="fallbackReason")

    model_config = ConfigDict(populate_by_name=True)


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
    reasoning_capability: AgentReasoningCapabilityData = Field(
        default_factory=AgentReasoningCapabilityData,
        alias="reasoningCapability",
    )
    reasoning_request_plan: AgentReasoningRequestPlanData = Field(
        default_factory=AgentReasoningRequestPlanData,
        alias="reasoningRequestPlan",
    )
    models: List[Dict[str, Any]] = Field(default_factory=list)
    provider_capabilities: Dict[str, Any] = Field(default_factory=dict, alias="providerCapabilities")

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
    provider_type: str = Field(default="openai_compatible", alias="providerType")

    model_config = ConfigDict(populate_by_name=True)


class AgentCoomiModelListData(BaseModel):
    endpoint: str = ""
    models: List[str] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


class AgentPermissionModeRequest(BaseModel):
    permission_mode: str = Field(alias="permissionMode")

    model_config = ConfigDict(populate_by_name=True)


class AgentPlanModeRequest(BaseModel):
    session_id: str = Field(default="default", alias="sessionId")
    active: bool

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
    explicit_tier = payload.get(
        "chapterLengthTier",
        payload.get("chapter_length_tier"),
    )
    legacy_target = payload.get(
        "chapterWordCountTarget",
        payload.get("chapter_word_count_target"),
    )
    if legacy_target is None:
        legacy_target = payload.get(
            "fragmentWordCount",
            payload.get(
                "fragment_word_count",
                payload.get(
                    "fragmentWordCountMax",
                    payload.get(
                        "fragment_word_count_max",
                        payload.get("segmentWords"),
                    ),
                ),
            ),
        )
    chapter_length_tier = (
        normalize_chapter_length_tier(explicit_tier)
        if explicit_tier is not None
        else migrate_chapter_word_count_target(legacy_target)
        if legacy_target is not None
        else None
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
    normalized = {
        "fragmentCount": fragment_count,
        "chapterTemplateId": chapter_template_id,
        # Retired switches are explicit false in the normalized request so no
        # downstream consumer can revive a numeric correction path.
        "preciseWordCountEnabled": False,
    }
    if chapter_length_tier is not None:
        normalized["chapterLengthTier"] = chapter_length_tier
    generation_strategy = str(
        payload.get("generationStrategy", payload.get("generation_strategy", ""))
        or ""
    ).strip().lower()
    if generation_strategy == SEMANTIC_BUDGET_STRATEGY:
        normalized["generationStrategy"] = SEMANTIC_BUDGET_STRATEGY
    return normalized


def _resolve_story_word_count_range(payload: Dict[str, Any]) -> tuple[int, int]:
    """Resolve the authoritative chapter target band from a request payload.

    The single chapter target has priority. Legacy min/max and single-value
    inputs remain accepted for older clients.
    """
    raw_target = payload.get("chapterWordCountTarget", payload.get("chapter_word_count_target"))
    if raw_target is not None:
        target = _bounded_int(
            raw_target,
            default=DEFAULT_CHAPTER_WORD_COUNT_TARGET,
            minimum=100,
            maximum=20000,
        )
        return target, target
    raw_min = payload.get("fragmentWordCountMin", payload.get("fragment_word_count_min"))
    raw_max = payload.get("fragmentWordCountMax", payload.get("fragment_word_count_max"))
    if raw_min is None and raw_max is None:
        legacy = payload.get(
            "fragmentWordCount", payload.get("fragment_word_count", payload.get("segmentWords"))
        )
        if legacy is None:
            return DEFAULT_CHAPTER_WORD_COUNT_TARGET, DEFAULT_CHAPTER_WORD_COUNT_TARGET
        value = _bounded_int(
            legacy,
            default=DEFAULT_CHAPTER_WORD_COUNT_TARGET,
            minimum=100,
            maximum=20000,
        )
        return value, value
    min_value = _bounded_int(
        raw_min,
        default=DEFAULT_CHAPTER_WORD_COUNT_TARGET,
        minimum=100,
        maximum=20000,
    )
    max_value = _bounded_int(
        raw_max,
        default=DEFAULT_CHAPTER_WORD_COUNT_TARGET,
        minimum=100,
        maximum=20000,
    )
    if min_value > max_value:
        min_value, max_value = max_value, min_value
    return min_value, max_value


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
    policy = (
        turn_plan.get("wordCountPolicy")
        if isinstance(turn_plan.get("wordCountPolicy"), dict)
        else {}
    )
    if str(policy.get("mode") or "").strip().lower() == "tier":
        next_story_generation["chapterLengthTier"] = normalize_chapter_length_tier(
            turn_plan.get("chapterLengthTier", policy.get("tier"))
        )
        next_story_generation["preciseWordCountEnabled"] = False
        for legacy_key in (
            "fragmentWordCount",
            "fragmentWordCountMin",
            "fragmentWordCountMax",
            "chapterWordCountTarget",
        ):
            next_story_generation.pop(legacy_key, None)
        next_story_generation["chapterTemplateId"] = selected_template
        next_story_generation["chapterTemplate"] = selected_template
        return next_story_generation
    raw_min = turn_plan.get("fragmentWordCountMin")
    raw_max = turn_plan.get("fragmentWordCountMax")
    if raw_min is None and raw_max is None:
        legacy = _bounded_int(
            turn_plan.get("fragmentWordCount"),
            default=DEFAULT_CHAPTER_WORD_COUNT_TARGET,
            minimum=100,
            maximum=20000,
        )
        min_value, max_value = legacy, legacy
    else:
        min_value = _bounded_int(
            raw_min,
            default=DEFAULT_CHAPTER_WORD_COUNT_TARGET,
            minimum=100,
            maximum=20000,
        )
        max_value = _bounded_int(
            raw_max,
            default=DEFAULT_CHAPTER_WORD_COUNT_TARGET,
            minimum=100,
            maximum=20000,
        )
        if min_value > max_value:
            min_value, max_value = max_value, min_value
    next_story_generation["fragmentWordCountMin"] = min_value
    next_story_generation["fragmentWordCountMax"] = max_value
    next_story_generation["fragmentWordCount"] = max_value
    next_story_generation["chapterWordCountTarget"] = _bounded_int(
        turn_plan.get("chapterWordCountTarget", int(round((min_value + max_value) / 2))),
        default=DEFAULT_CHAPTER_WORD_COUNT_TARGET,
        minimum=100,
        maximum=20000,
    )
    next_story_generation["chapterTemplateId"] = selected_template
    next_story_generation["chapterTemplate"] = selected_template
    return next_story_generation


def _build_turn_contract_with_active_model(
    workspace_root: Path,
    *,
    prompt: str,
    active_file: str,
    story_generation: Dict[str, Any],
    intent_frame: Dict[str, Any],
    route_hints: Dict[str, Any] | None,
    routing_metadata: Dict[str, Any] | None,
    context_policy: ContextPolicy | None,
    trace_id: str = "",
    session_id: str = "",
) -> Dict[str, Any]:
    provider = ""
    model = ""
    try:
        status = _coomi_status_for_execution(workspace_root)
        provider = str(status.get("providerId") or "") if isinstance(status, dict) else ""
        model = str(status.get("model") or "") if isinstance(status, dict) else ""
    except Exception as exc:
        _LOGGER.warning("Unable to resolve active Coomi model for length calibration: %s", exc)
    return storydex_orchestration_service.build_turn_contract(
        workspace_root,
        prompt=prompt,
        active_file=active_file,
        story_generation=story_generation,
        intent_frame=intent_frame,
        route_hints=route_hints,
        routing_metadata=routing_metadata,
        context_policy=context_policy,
        provider=provider,
        model=model,
        trace_id=trace_id,
        session_id=session_id,
    )


def _coomi_status_for_execution(workspace_root: Path) -> Dict[str, Any]:
    service = get_storydex_coomi_agent_service()
    cached_status = getattr(service, "get_status_for_execution", None)
    if callable(cached_status):
        return cached_status(workspace_root=workspace_root)
    return service.get_status(workspace_root=workspace_root)


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


def _exception_message(error: BaseException, fallback: str = "Coomi execution failed.") -> str:
    detail = str(error).strip()
    return detail or f"{fallback.rstrip('.')} ({type(error).__name__})."


def _reconcile_story_knowledge_projection(workspace_root: Path) -> Dict[str, Any]:
    """Reconcile the deterministic projection after a turn changed project files."""
    try:
        payload = get_story_wiki_service().sync_local_incremental(Path(workspace_root).resolve())
    except Exception as exc:
        return {
            "_type": "KnowledgeProjectionError",
            "_version": 1,
            "ok": False,
            "status": "error",
            "knowledgeRevision": 0,
            "builtFromRevision": 0,
            "lastSuccessfulRevision": 0,
            "changedSourcePaths": [],
            "diagnostics": [],
            "errorMessage": str(exc) or exc.__class__.__name__,
        }
    diagnostics = [
        dict(item)
        for item in payload.get("diagnostics", [])
        if isinstance(item, dict)
    ]
    status = str(payload.get("status") or "ready")
    ok = status == "ready"
    error_message = ""
    if not ok:
        error_message = next(
            (str(item.get("message") or "") for item in diagnostics if item.get("message")),
            f"Knowledge projection finished with status {status}.",
        )
    return {
        "_type": "KnowledgeProjectionUpdated" if ok else "KnowledgeProjectionError",
        "_version": 1,
        "ok": ok,
        "status": status,
        "schemaVersion": int(payload.get("schemaVersion") or 0),
        "knowledgeRevision": int(payload.get("knowledgeRevision") or 0),
        "builtFromRevision": int(payload.get("builtFromRevision") or 0),
        "lastSuccessfulRevision": int(payload.get("lastSuccessfulRevision") or 0),
        "sourceSetChecksum": str(payload.get("sourceSetChecksum") or ""),
        "graphChecksum": str(payload.get("graphChecksum") or ""),
        "changedSourcePaths": [
            str(path)
            for path in payload.get("changedSourcePaths", [])
            if str(path).strip()
        ],
        "sourceStats": dict(payload.get("sourceStats") or {}),
        "diagnostics": diagnostics,
        "errorMessage": error_message,
    }


_READ_ONLY_AGENT_TOOLS = frozenset(
    {
        "ask_user",
        "ask_user_question",
        "get_loop",
        "glob",
        "grep",
        "grep_files",
        "list_dir",
        "list_skills",
        "memory_list",
        "memory_read",
        "memory_search",
        "read",
        "read_file",
        "read_skill",
        "request_user_input",
        "search",
        "storydexhelpguidesearch",
        "storydexprojectsearch",
        "storydexruntimepresetstatus",
        "storydexversionstatus",
        "storydexwikiquery",
        "storydexwordcount",
        "todo",
        "todo_write",
        "todowrite",
        "update_plan",
        "view_image",
        "wait_agent",
        "web_fetch",
        "web_search",
        "webfetch",
        "websearch",
    }
)


def _has_project_mutation_attempt(events: Sequence[Dict[str, Any]]) -> bool:
    """Return whether the runtime attempted an operation that may change project files."""
    for item in events:
        if not isinstance(item, dict):
            continue
        event_name = str(item.get("event") or "")
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        if event_name == "StoryGenerationValidation" and bool(data.get("writeToolApplied")):
            return True
        if event_name == "StoryCommitStarted":
            return True
        if (
            event_name == "SemanticBudgetProgress"
            and str(data.get("state") or "").strip().upper() == "APPLYING"
        ):
            return True
        if event_name not in {"ToolStart", "ToolDone"}:
            continue
        tool_name = str(data.get("tool_name") or data.get("toolName") or "").strip().lower()
        if tool_name and tool_name not in _READ_ONLY_AGENT_TOOLS:
            return True
    return False


def _turn_requires_knowledge_projection(
    snapshot: AgentGitSnapshot,
    events: Sequence[Dict[str, Any]],
) -> bool:
    mutation_attempted = _has_project_mutation_attempt(events)
    if not mutation_attempted:
        return False
    detector = getattr(agent_git_autocommit_service, "changed_paths_since_turn", None)
    if callable(detector):
        try:
            changed_paths = detector(snapshot)
        except Exception:
            _LOGGER.exception("Unable to compare Agent turn changes before knowledge projection")
        else:
            if changed_paths is not None:
                return bool(changed_paths)
    return True


def _skipped_knowledge_projection() -> Dict[str, Any]:
    return {
        "_type": "KnowledgeProjectionSkipped",
        "_version": 1,
        "ok": True,
        "status": "skipped",
        "reason": "no_project_changes",
        "changedSourcePaths": [],
        "diagnostics": [],
        "errorMessage": "",
    }


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


_INTENT_OPERATION_LABELS = {
    "create_new": "生成新内容",
    "modify_existing": "修改现有文件",
    "inquiry": "理解性问询",
    "greeting": "问候",
    "other": "其他",
}
_INTENT_COMPLEXITY_LABELS = {"simple": "简单", "complex": "复杂"}


def _intent_phase_detail(intent_frame: Dict[str, Any]) -> str:
    """把意图帧渲染成可读中文串，实时推送到瀑布流面板。"""
    frame = intent_frame if isinstance(intent_frame, dict) else {}
    primary = str(frame.get("primary") or "general")
    operation_type = str(frame.get("operationType") or "").strip().lower()
    complexity = str(frame.get("complexity") or "").strip().lower()
    method = str(frame.get("method") or "unknown")
    reason = str(frame.get("reason") or "").strip()
    parts = [f"意图：{primary}"]
    if operation_type:
        parts.append(f"操作：{_INTENT_OPERATION_LABELS.get(operation_type, operation_type)}")
    if complexity:
        parts.append(f"复杂度：{_INTENT_COMPLEXITY_LABELS.get(complexity, complexity)}")
    parts.append(f"来源：{method}")
    detail = " · ".join(parts)
    if reason:
        detail += f"（{reason[:60]}）"
    return detail


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _phase_for_event(event_name: str) -> str:
    if event_name.startswith("Tool"):
        return "tool"
    if event_name in {"TextChunk", "ReasoningChunk", "ConnectionRetry", "ModelCompleted"}:
        return "model"
    if event_name in {"GitAutoCommit", "GitCommitPrompt", "GitCommitResult"}:
        return "version_control"
    if event_name in {"KnowledgeProjectionUpdated", "KnowledgeProjectionError"}:
        return "knowledge_projection"
    if event_name.startswith("Task"):
        return "planning"
    if event_name in {"TurnContract", "StoryGenerationValidation"}:
        return "orchestration"
    if event_name.startswith("SemanticBudget"):
        return "model"
    if event_name in {
        "RunAccepted",
        "UsageUpdate",
        "CompressionEvent",
        "TurnPhase",
        "ReasoningPlan",
        "RuntimeMetrics",
    }:
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
        if bool(payload.get("passed")) and str(payload.get("status") or "").lower() == "warning":
            return "warning"
        return "success" if bool(payload.get("passed")) else "error"
    if event_name == "SemanticBudgetProviderAttempt":
        if str(payload.get("outcome") or "") == "success":
            return "success"
        return "warning" if bool(payload.get("retryScheduled")) else "error"
    if event_name in {"SemanticBudgetProgress", "SemanticBudgetResult"}:
        state = str(payload.get("state") or "").upper()
        status = str(payload.get("status") or "").lower()
        if state == "FAILED" or status.startswith("failed"):
            return "error"
        if state == "COMPLETED" or status == "completed":
            return "success"
        return "running"
    if event_name in {"KnowledgeProjectionUpdated", "KnowledgeProjectionError"}:
        return "success" if bool(payload.get("ok")) else "error"
    if event_name == "RunAccepted":
        return "running"
    if event_name == "ReasoningPlan":
        return "info"
    if event_name == "ModelCompleted":
        return "success"
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
    if event_name == "ReasoningPlan":
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
        requested = str(plan.get("requested") or "auto")
        fields = plan.get("wireFields") if isinstance(plan.get("wireFields"), list) else []
        fallback = str(plan.get("fallbackReason") or plan.get("fallback_reason") or "").strip()
        detail = f"reasoning={requested}; wireFields={len(fields)}"
        return f"{detail}; fallback={fallback}" if fallback else detail
    if event_name == "ModelCompleted":
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        reasoning = usage.get("reasoning_tokens")
        model = str(payload.get("responseModel") or "")
        evidence = "nativeReasoning=true" if payload.get("nativeReasoning") else "nativeReasoning=false"
        tokens = f"reasoningTokens={int(reasoning or 0)}" if reasoning is not None else ""
        return "; ".join(value for value in (model, evidence, tokens) if value)
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
    if event_name == "SemanticBudgetProviderAttempt":
        purpose = str(payload.get("purpose") or "provider")
        attempt = int(payload.get("attempt") or 1)
        if payload.get("retryScheduled"):
            return f"{purpose} 第 {attempt} 次请求失败，按上游要求有界重试"
        return f"{purpose} 第 {attempt} 次请求{('完成' if payload.get('outcome') == 'success' else '失败')}"
    if event_name in {"SemanticBudgetProgress", "SemanticBudgetResult"}:
        labels = {
            "PLANNING": "正在规划章节场景",
            "REPAIRING_PLAN": "正在修复场景计划结构",
            "GENERATING_SCENE": "正在生成当前场景",
            "REVISING_SCENE": "正在局部修订当前场景",
            "VERIFYING_SCENE": "正在校验当前场景",
            "ASSEMBLING": "正在组装章节正文",
            "APPLYING": "正在一次性写入章节正文",
            "COMPLETED": "语义预算生成完成",
            "FAILED": "语义预算生成失败",
        }
        state = str(payload.get("state") or "").upper()
        return labels.get(state, str(payload.get("status") or event_name))
    if event_name == "SemanticBudgetFallback":
        return f"语义预算策略未启用，回退普通 Agent：{payload.get('reason') or 'unknown'}"
    if event_name in {"KnowledgeProjectionUpdated", "KnowledgeProjectionError"}:
        changed_count = len(payload.get("changedSourcePaths") or [])
        if payload.get("ok"):
            return f"知识图谱已对齐 revision {payload.get('builtFromRevision') or 0}，处理 {changed_count} 个源文件变更"
        return str(payload.get("errorMessage") or "知识图谱投影更新失败")
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


def _rollback_reply_chunks(reply_chunks: List[str], character_count: int) -> None:
    remaining = max(0, int(character_count or 0))
    if remaining <= 0:
        return
    text = "".join(reply_chunks)
    reply_chunks.clear()
    if remaining < len(text):
        reply_chunks.append(text[:-remaining])


def _rollback_trace_text_events(events: List[Dict[str, Any]], character_count: int) -> None:
    remaining = max(0, int(character_count or 0))
    if remaining <= 0:
        return
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if str(event.get("event") or "") != "TextChunk":
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        content = str(data.get("content") or "")
        if len(content) <= remaining:
            remaining -= len(content)
            events.pop(index)
            if remaining <= 0:
                break
            continue
        kept = content[:-remaining]
        data["content"] = kept
        event["data"] = data
        event["detail"] = kept[:240]
        remaining = 0
        break
    for index, event in enumerate(events, start=1):
        event["index"] = index


def _extract_trace_metrics(
    events: List[Dict[str, Any]],
    trace_id: str,
    duration_ms: int,
    llm_metrics: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    tool_calls = len([item for item in events if item.get("event") == "ToolDone"])
    tool_arguments = {
        str(data.get("tool_call_id") or ""): data.get("arguments") or {}
        for item in events
        if item.get("event") == "ToolStart"
        for data in [item.get("data") if isinstance(item.get("data"), dict) else {}]
        if str(data.get("tool_call_id") or "")
    }
    observed_tool_signatures: set[str] = set()
    duplicate_tool_calls_same_revision = 0
    evidence_observations = 0
    evidence_invalidations = 0
    evidence_byte_intervals: dict[tuple[str, str], list[tuple[int, int]]] = {}
    evidence_covered_paths: set[str] = set()
    evidence_missing_paths: set[str] = set()
    for item in events:
        if item.get("event") != "ToolDone":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        ledger = data.get("evidenceLedger") if isinstance(data.get("evidenceLedger"), dict) else {}
        evidence_observations += int(ledger.get("observationCount") or 0)
        for observation in ledger.get("observations", []):
            if not isinstance(observation, dict):
                continue
            evidence_invalidations += len(observation.get("invalidated") or [])
            evidence_path = str(observation.get("path") or "")
            evidence_revision = str(observation.get("revision") or "")
            for span in observation.get("spans", []):
                if not isinstance(span, dict):
                    continue
                try:
                    start_byte = max(0, int(span.get("startByte") or 0))
                    end_byte = max(start_byte, int(span.get("endByte") or 0))
                except (TypeError, ValueError):
                    continue
                if end_byte > start_byte:
                    evidence_byte_intervals.setdefault(
                        (evidence_path, evidence_revision),
                        [],
                    ).append((start_byte, end_byte))
        coverage = ledger.get("coverage") if isinstance(ledger.get("coverage"), dict) else {}
        evidence_covered_paths.update(str(path) for path in coverage.get("coveredPaths", []) if str(path))
        evidence_missing_paths.update(str(path) for path in coverage.get("missingPaths", []) if str(path))
        revision = str(data.get("source_revision") or "").strip()
        if not revision:
            continue
        call_id = str(data.get("tool_call_id") or "")
        signature = json.dumps(
            {
                "tool": str(data.get("tool_name") or ""),
                "arguments": data.get("arguments") or tool_arguments.get(call_id, {}),
                "path": str(data.get("source_path") or ""),
                "revision": revision,
                "span": data.get("source_span") or {},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if signature in observed_tool_signatures:
            duplicate_tool_calls_same_revision += 1
        observed_tool_signatures.add(signature)

    evidence_unique_bytes = 0
    for intervals in evidence_byte_intervals.values():
        merged_intervals: list[list[int]] = []
        for start_byte, end_byte in sorted(intervals):
            if not merged_intervals or start_byte > merged_intervals[-1][1]:
                merged_intervals.append([start_byte, end_byte])
            else:
                merged_intervals[-1][1] = max(merged_intervals[-1][1], end_byte)
        evidence_unique_bytes += sum(end_byte - start_byte for start_byte, end_byte in merged_intervals)

    model_usages: List[Dict[str, Any]] = []
    for item in events:
        if item.get("event") != "ModelCompleted":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        model_usages.append(usage)
    input_tokens_by_round = [
        max(0, int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0))
        for usage in model_usages
    ]
    cached_tokens_by_round = [
        max(0, int(usage.get("cached_input_tokens") or usage.get("cache_read_input_tokens") or 0))
        for usage in model_usages
    ]
    runtime_metrics: Dict[str, Any] = {}
    for item in events:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        if item.get("event") == "ModelCompleted":
            candidate = (
                data.get("runtimeMetrics")
                if isinstance(data.get("runtimeMetrics"), dict)
                else {}
            )
        elif item.get("event") == "RuntimeMetrics":
            candidate = data
        else:
            continue
        for key, value in candidate.items():
            if str(key).endswith("Ms"):
                runtime_metrics[str(key)] = round(max(0.0, float(value or 0.0)), 3)
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
    event_usage: Dict[str, int] | None = None
    for item in reversed(events):
        if item.get("event") != "UsageUpdate":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        try:
            prompt_tokens = max(
                0,
                int(
                    usage.get("prompt_tokens")
                    or usage.get("promptTokens")
                    or usage.get("input_tokens")
                    or 0
                ),
            )
            completion_tokens = max(
                0,
                int(
                    usage.get("completion_tokens")
                    or usage.get("completionTokens")
                    or usage.get("output_tokens")
                    or 0
                ),
            )
        except (TypeError, ValueError):
            continue
        if prompt_tokens + completion_tokens <= 0:
            continue
        event_usage = {
            "promptTokens": prompt_tokens,
            "completionTokens": completion_tokens,
        }
        break
    if event_usage is not None:
        prompt_tokens = event_usage["promptTokens"]
        completion_tokens = event_usage["completionTokens"]
    elif usage_calls:
        prompt_tokens = int(observed.get("promptTokens") or 0)
        completion_tokens = int(observed.get("completionTokens") or 0)
    else:
        prompt_tokens = 0
        completion_tokens = total_tokens
    lifecycle = build_agent_lifecycle_trace(events)
    return {
        "traceId": trace_id,
        "durationMs": duration_ms,
        "toolCalls": tool_calls,
        "llmCalls": observed_calls or len(model_usages) or (1 if total_tokens else 0),
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "estimatedCost": 0.0,
        "modelRounds": len(model_usages),
        "duplicateToolCallsSameRevision": duplicate_tool_calls_same_revision,
        # The largest request is the turn's logical active context; transmitted
        # input is the sum actually sent across all model/tool rounds.
        "logicalInputTokens": max(input_tokens_by_round, default=0),
        "transmittedInputTokens": sum(input_tokens_by_round),
        "cachedInputTokens": sum(cached_tokens_by_round),
        "evidenceObservations": evidence_observations,
        "evidenceInvalidations": evidence_invalidations,
        "uniqueEvidenceBytes": evidence_unique_bytes,
        "evidenceCoverage": {
            "coveredPathCount": len(evidence_covered_paths),
            "missingPathCount": len(evidence_missing_paths),
            "coveredPaths": sorted(evidence_covered_paths),
            "missingPaths": sorted(evidence_missing_paths),
        },
        "lifecycle": lifecycle,
        **runtime_metrics,
    }


def _merge_runtime_trace_metrics(
    context_trace: Dict[str, Any],
    trace: Dict[str, Any],
) -> Dict[str, Any]:
    runtime_keys = (
        "bridgeStartMs",
        "componentInitMs",
        "providerConfigMs",
        "sessionInitMs",
        "projectInstructionsMs",
        "memoryInitMs",
        "securityInitMs",
        "mcpInitMs",
        "hooksInitMs",
        "toolsInitMs",
        "providerInitMs",
        "modelRounds",
        "toolCalls",
        "duplicateToolCallsSameRevision",
        "logicalInputTokens",
        "transmittedInputTokens",
        "cachedInputTokens",
        "evidenceObservations",
        "evidenceInvalidations",
        "uniqueEvidenceBytes",
    )
    totals = context_trace.get("totals") if isinstance(context_trace.get("totals"), dict) else {}
    totals.update({key: trace.get(key, 0) for key in runtime_keys})
    context_trace["totals"] = totals
    performance = (
        context_trace.get("performance")
        if isinstance(context_trace.get("performance"), dict)
        else {"_type": "TurnPerformanceTrace", "_version": 1}
    )
    performance.update({key: trace.get(key, 0) for key in runtime_keys})
    context_trace["performance"] = performance
    context_trace["evidenceCoverage"] = trace.get("evidenceCoverage", {})
    return context_trace


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
                    "fragmentWordCountMin": int(turn_plan.get("fragmentWordCountMin") or 0),
                    "fragmentWordCountMax": int(turn_plan.get("fragmentWordCountMax") or 0),
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
                    "targetWordCountMin": int(data.get("targetWordCountMin") or 0),
                    "targetWordCountMax": int(data.get("targetWordCountMax") or 0),
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
    status_data = _coomi_status_for_execution(workspace_root)
    duration_ms = int((time.perf_counter() - started) * 1000)
    llm_metrics = get_llm_metrics(trace_id)
    trace = _extract_trace_metrics(events, trace_id, duration_ms, llm_metrics)
    context_trace = _merge_runtime_trace_metrics(
        merge_llm_metrics(_extract_context_trace(events), llm_metrics),
        trace,
    )
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
        }
        for index, item in enumerate(fragments)
        if isinstance(item, dict) and str(item.get("status") or "") != "passed"
    ]
    correction = {
        "correctionAttempt": correction_attempt,
        "maximumCorrectionAttempts": _STORY_GENERATION_MAX_CORRECTIONS,
        "algorithm": str(validation.get("algorithm") or "storydex_visible_characters_v1"),
        "countingRule": str(validation.get("countingRule") or STORY_WORD_COUNT_RULE),
        "chapterContentMode": str(validation.get("chapterContentMode") or ""),
        "structurePassed": bool(validation.get("structurePassed")),
        "writeToolApplied": bool(validation.get("writeToolApplied")),
        "expansionDirections": ["当前冲突的直接后果", "角色的下一步决定", "必要的场景收束"],
        "failures": failures,
    }
    return (
        "Storydex 检测到本轮正文偏短，将进行一次定向补写。\n"
        "在不改变既定剧情计划、不新增无关支线、不重复已有信息的前提下，"
        "补充与本轮核心事件直接相关的动作后果、角色下一步决定和必要的场景收束。\n"
        "不要写摘要、留言或任何正文之外的内容——它们不计入字数。\n"
        "请保持当前 TurnContract 约定的章节路径、文件数量和写入模式，"
        "完成扩写后再次调用 StorydexApplyStoryIncrement。\n"
        "禁止使用普通 Write/Edit 工具写 chapters/ 正文。\n"
        f"STORYDEX_OBJECTIVE_VALIDATION={json.dumps(correction, ensure_ascii=False, separators=(',', ':'))}"
    )


def _rebuild_story_generation_contract_for_correction(
    workspace_root: Path,
    turn_contract: Dict[str, Any],
    validation: Dict[str, Any],
) -> Dict[str, Any]:
    """Refresh append baselines before a correction reuses the turn contract.

    A successful append advances the file on disk. Reusing the original
    ``baselineWordCount`` would make the duplicate-append guard reject every
    correction attempt. Only append targets in chapters reported by the failed
    validation are refreshed; the guard itself remains unchanged.
    """

    next_contract = copy.deepcopy(turn_contract) if isinstance(turn_contract, dict) else {}
    turn_plan = next_contract.get("turnPlan") if isinstance(next_contract.get("turnPlan"), dict) else {}
    targets = turn_plan.get("fragmentTargets") if isinstance(turn_plan.get("fragmentTargets"), list) else []
    if not targets:
        return next_contract

    failed_chapters: set[str] = set()
    validation_fragments = validation.get("fragments") if isinstance(validation.get("fragments"), list) else []
    for item in validation_fragments:
        if not isinstance(item, dict) or str(item.get("status") or "") == "passed":
            continue
        relative_path = str(item.get("path") or "").strip().replace("\\", "/").lstrip("/")
        if relative_path:
            failed_chapters.add(Path(relative_path).parent.as_posix())

    root = Path(workspace_root).resolve()
    for raw_target in targets:
        if not isinstance(raw_target, dict) or str(raw_target.get("writeMode") or "replace") != "append":
            continue
        relative_path = str(raw_target.get("path") or "").strip().replace("\\", "/").lstrip("/")
        if not relative_path:
            continue
        chapter_path = Path(relative_path).parent.as_posix()
        if failed_chapters and chapter_path not in failed_chapters:
            continue
        candidate = (root / relative_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        raw_target["baselineWordCount"] = (
            story_project_service.count_story_file_words(candidate) if candidate.is_file() else 0
        )
    return next_contract


def _has_successful_story_generation_write(
    events: List[Dict[str, Any]],
    *,
    start_index: int = 0,
) -> bool:
    for item in events[max(0, int(start_index)) :]:
        if item.get("event") != "ToolDone":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        tool_name = str(data.get("tool_name") or data.get("toolName") or "").strip().lower()
        if tool_name == "storydexapplystoryincrement" and not bool(data.get("is_error")):
            return True
    return False


def _story_generation_needs_length_correction(validation: Dict[str, Any]) -> bool:
    # 只有章级偏短触发补写。超上限已降级为告警，不再阻断补写判定：
    # 多章场景下另一章偏长，不该让偏短的这一章失去唯一一次补写机会。
    return (
        str(validation.get("wordCountScope") or "").strip().lower() == "chapter"
        and bool(validation.get("belowBudget"))
    )


def _supports_correction_continuation(turn_contract: Dict[str, Any] | None) -> bool:
    """Whether this contract may still use the append-based correction round.

    Only legacy contracts may. A current chapter-scoped word-count contract
    belongs to the bounded path, where a short draft is handled *before* the
    single write; letting it also run the Agent continuation would restore the
    double write this work removed: the first draft already on disk, with a
    correction appended after it.

    Reaching this function with a current bounded-capable contract means the
    bounded gate declined the turn for some other reason (feature flag off, no
    authoritative chapter path). Such a turn keeps its one draft: writing more
    prose is not a safe recovery when the reason the bounded path declined is
    unknown here.
    """

    contract = turn_contract if isinstance(turn_contract, dict) else {}
    plan = contract.get("turnPlan") if isinstance(contract.get("turnPlan"), dict) else {}
    policy = plan.get("wordCountPolicy") if isinstance(plan.get("wordCountPolicy"), dict) else {}
    return int(policy.get("version") or 0) < BOUNDED_WORD_COUNT_POLICY_VERSION


def _recorded_story_call_accounting(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the last ledger the bounded path emitted, if this turn used it.

    The bounded path increments its counters at the moment it decides to spend a
    call, so its ledger states what happened. Later events win over earlier ones
    only because a turn emits at most one of these; taking the last is simply the
    safe reading if that ever changes.
    """

    recorded: Dict[str, Any] = {}
    for item in events:
        if str(item.get("event") or "") != "StoryCallAccounting":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        if "logicalStoryCalls" in data:
            recorded = dict(data)
    return recorded


def story_call_accounting_payload(
    events: List[Dict[str, Any]],
    *,
    turn_contract: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Report the three call numbers for one finished story turn.

    An audit needs to tell one prose call from three, and the HTTP request count
    cannot answer that: a single logical call that retried twice on a 429 is
    still one call against the budget.

    The bounded path counts its own calls as it makes them and emits the ledger
    as a ``StoryCallAccounting`` event, so that event is authoritative whenever it
    is present. Only legacy Agent-loop turns fall back to deriving the numbers
    from trace signals:

    * each ``AgentStarted`` opens one logical prose call (the segment loop emits
      one per draft and one per correction continuation);
    * each ``ConnectionRetry`` is a transport retry inside the call it belongs to;
    * Provider attempts are logical calls plus those retries.

    Derivation is a reconstruction, not a measurement: it cannot see a call the
    loop made without announcing it. That is why the bounded path records instead
    of inferring, and why this function prefers its ledger.

    ``contractViolations`` is what makes an unexpected second prose call visible
    instead of merely expensive.
    """

    plan = (
        turn_contract.get("turnPlan")
        if isinstance(turn_contract, dict) and isinstance(turn_contract.get("turnPlan"), dict)
        else {}
    )
    policy = plan.get("wordCountPolicy") if isinstance(plan.get("wordCountPolicy"), dict) else {}
    precision = policy.get("precision") if isinstance(policy.get("precision"), dict) else {}
    precision_enabled = bool(precision.get("enabled"))
    asymmetric = policy.get("asymmetric") if isinstance(policy.get("asymmetric"), dict) else {}
    asymmetric_enabled = bool(asymmetric.get("enabled"))

    recorded = _recorded_story_call_accounting(events)
    if recorded:
        return {
            "_type": "StoryCallAccounting",
            "_version": 1,
            "preciseWordCountEnabled": bool(
                recorded.get("preciseWordCountEnabled", precision_enabled)
            ),
            "asymmetricLengthEnabled": bool(
                recorded.get("asymmetricLengthEnabled", asymmetric_enabled)
            ),
            "source": "recorded",
            "logicalStoryCalls": int(recorded.get("logicalStoryCalls") or 0),
            "providerAttempts": int(recorded.get("providerAttempts") or 0),
            "transportRetries": int(recorded.get("transportRetries") or 0),
            "initialGenerationCalls": int(recorded.get("initialGenerationCalls") or 0),
            "lengthRevisionCalls": int(recorded.get("lengthRevisionCalls") or 0),
            "secondDraftCalls": int(recorded.get("secondDraftCalls") or 0),
            "nonProseCalls": dict(recorded.get("nonProseCalls") or {}),
            "contractViolations": [
                str(item) for item in list(recorded.get("contractViolations") or [])
            ],
        }

    accounting = StoryCallAccounting()
    seen_initial = False
    for item in events:
        name = str(item.get("event") or "")
        if name == "AgentStarted":
            purpose = (
                STORY_INITIAL_GENERATION_PURPOSE
                if not seen_initial
                else STORY_SECOND_DRAFT_PURPOSE
                if asymmetric_enabled
                else STORY_LENGTH_REVISION_PURPOSE
            )
            seen_initial = True
            accounting.record_logical_call(purpose)
            accounting.record_provider_attempt(purpose)
        elif name == "ConnectionRetry" and seen_initial:
            purpose = (
                STORY_SECOND_DRAFT_PURPOSE
                if accounting.second_draft_calls
                else
                STORY_LENGTH_REVISION_PURPOSE
                if accounting.length_revision_calls
                else STORY_INITIAL_GENERATION_PURPOSE
            )
            accounting.record_transport_retry(purpose)
            accounting.record_provider_attempt(purpose)

    return {
        "_type": "StoryCallAccounting",
        "_version": 1,
        "preciseWordCountEnabled": precision_enabled,
        "asymmetricLengthEnabled": asymmetric_enabled,
        **accounting.payload(),
        "contractViolations": accounting.contract_violations(
            precision_enabled=precision_enabled,
            asymmetric_enabled=asymmetric_enabled,
        ),
    }


def _semantic_budget_gate(
    workspace_root: Path,
    turn_contract: Dict[str, Any],
) -> Dict[str, Any]:
    intent = turn_contract.get("intentFrame") if isinstance(turn_contract.get("intentFrame"), dict) else {}
    turn_plan = turn_contract.get("turnPlan") if isinstance(turn_contract.get("turnPlan"), dict) else {}
    control = (
        turn_plan.get("generationControl")
        if isinstance(turn_plan.get("generationControl"), dict)
        else {}
    )
    requested = str(control.get("strategy") or "").strip().lower() == SEMANTIC_BUDGET_STRATEGY
    if not requested:
        return {"requested": False, "enabled": False, "reason": "not_requested"}
    if str(intent.get("primary") or "").strip().lower() != "story_generation":
        return {"requested": True, "enabled": False, "reason": "not_story_generation"}
    if str(turn_plan.get("operationType") or "").strip().lower() != "create_new":
        return {"requested": True, "enabled": False, "reason": "operation_not_create_new"}
    flags = FeatureFlags(Path(workspace_root).resolve(), FEATURE_FLAG_DEFAULTS)
    if not flags.get_bool("SEMANTIC_BUDGET_GENERATION_ENABLED"):
        return {"requested": True, "enabled": False, "reason": "feature_flag_disabled"}
    targets = turn_plan.get("fragmentTargets") if isinstance(turn_plan.get("fragmentTargets"), list) else []
    if int(turn_plan.get("fragmentCount") or 0) != 1 or len(targets) != 1:
        return {"requested": True, "enabled": False, "reason": "single_target_required"}
    target = int(control.get("productTargetWordCount") or turn_plan.get("chapterWordCountTarget") or 0)
    if target < 900:
        return {"requested": True, "enabled": False, "reason": "target_below_900"}
    return {"requested": True, "enabled": True, "reason": "enabled"}


def _semantic_budget_source_context(
    workspace_root: Path,
    *,
    active_file: str,
    turn_contract: Dict[str, Any],
    limit: int = 8000,
) -> str:
    root = Path(workspace_root).resolve()
    relative = str(active_file or "").strip().replace("\\", "/").lstrip("/")
    if relative:
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            candidate = root / "__invalid_active_file__"
        if candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8-sig")[-limit:]
            except OSError:
                pass
    assembly = (
        turn_contract.get("contextAssembly")
        if isinstance(turn_contract.get("contextAssembly"), dict)
        else {}
    )
    blocks = assembly.get("promptBlocks") if isinstance(assembly.get("promptBlocks"), list) else []
    chunks: list[str] = []
    for block in blocks:
        if not isinstance(block, dict) or str(block.get("id") or "") == "runtime_presets":
            continue
        content = str(block.get("content") or "").strip()
        if content:
            chunks.append(content)
    return "\n\n".join(chunks)[-limit:]


def _semantic_budget_result_packet(
    result: SemanticBudgetResult,
    adapter: CoomiStoryGenerationAdapter,
) -> Dict[str, Any]:
    return {
        "_type": "SemanticBudgetResult",
        "_version": 1,
        "state": "COMPLETED" if result.completed else "FAILED",
        "status": result.status,
        "strategy": result.strategy,
        "targetWordCount": result.target_word_count,
        "generatedWordCount": result.generated_word_count,
        "acceptanceMinimum": result.acceptance_minimum,
        "acceptanceMaximum": result.acceptance_maximum,
        "withinAcceptance": result.within_acceptance,
        "sceneCount": len(result.scenes),
        "scenes": [dict(item) for item in result.scenes],
        "providerCalls": result.provider_calls,
        "providerAttempts": adapter.provider_attempts,
        "providerRetries": adapter.provider_retries,
        "revisionAttempts": result.revision_attempts,
        "revisionAcceptances": result.revision_acceptances,
        "durationMs": result.duration_ms,
        "mechanicalIssues": list(result.mechanical_issues),
        "error": dict(result.error),
    }


async def _execute_semantic_budget_generation(
    *,
    prompt: str,
    trace_id: str,
    active_file: str,
    workspace_root: Path,
    turn_contract: Dict[str, Any],
    event_sink: Callable[[str, Dict[str, Any]], None],
) -> Dict[str, Any]:
    turn_plan = turn_contract.get("turnPlan") if isinstance(turn_contract.get("turnPlan"), dict) else {}
    control = (
        turn_plan.get("generationControl")
        if isinstance(turn_plan.get("generationControl"), dict)
        else {}
    )
    constraint_context, constraint_audit = read_scene_constraint_context(workspace_root)
    product_target_word_count = int(
        control.get("productTargetWordCount") or turn_plan.get("chapterWordCountTarget") or 0
    )
    maximum_scene_revisions = control.get("maximumSceneRevisions")
    request = SemanticBudgetRequest(
        product_target_word_count=product_target_word_count,
        user_task=prompt,
        source_context=_semantic_budget_source_context(
            workspace_root,
            active_file=active_file,
            turn_contract=turn_contract,
        ),
        constraint_context=constraint_context,
        scene_count=int(control.get("sceneCount") or 0),
        maximum_scene_revisions=(
            int(maximum_scene_revisions)
            if maximum_scene_revisions is not None
            else automatic_scene_revision_limit(product_target_word_count)
        ),
        internal_tolerance_ratio=float(control.get("internalToleranceRatio") or 0.20),
        final_tolerance_ratio=float(control.get("finalToleranceRatio") or 0.15),
    )
    adapter = CoomiStoryGenerationAdapter(
        trace_id=trace_id,
        reasoning_effort=str(turn_contract.get("reasoningEffort") or "auto"),
        maximum_transport_retries=1,
        event_sink=event_sink,
    )
    deferred_completion: Dict[str, Any] | None = None

    def controller_event_sink(name: str, packet: Dict[str, Any]) -> None:
        nonlocal deferred_completion
        normalized = dict(packet)
        if (
            name == "SemanticBudgetProgress"
            and str(normalized.get("state") or "").upper() == "COMPLETED"
        ):
            deferred_completion = normalized
            return
        event_sink(name, normalized)

    result = await SemanticBudgetController().generate(
        request,
        adapter,
        event_sink=controller_event_sink,
    )
    result_packet = _semantic_budget_result_packet(result, adapter)
    result_packet["constraintModules"] = constraint_audit

    def emit_apply_failure(apply_error: Dict[str, Any]) -> Dict[str, Any]:
        failure_packet = {
            **result_packet,
            "state": "FAILED",
            "status": "failed_apply",
            "generationStatus": result.status,
            "error": dict(apply_error),
        }
        event_sink(
            "SemanticBudgetProgress",
            {
                "_type": "SemanticBudgetProgress",
                "_version": 1,
                "strategy": SEMANTIC_BUDGET_STRATEGY,
                "state": "FAILED",
                "status": "failed_apply",
                "errorType": str(apply_error.get("type") or "StoryGenerationApplyFailed"),
            },
        )
        event_sink("SemanticBudgetResult", failure_packet)
        return failure_packet

    if not result.completed:
        event_sink("SemanticBudgetResult", result_packet)
        return {
            "ok": False,
            "result": result,
            "resultPacket": result_packet,
            "applyResult": {},
            "validation": {},
        }

    targets = turn_plan.get("fragmentTargets") if isinstance(turn_plan.get("fragmentTargets"), list) else []
    target_spec = targets[0] if len(targets) == 1 and isinstance(targets[0], dict) else {}
    target_path = str(target_spec.get("path") or "").strip()
    if not target_path:
        apply_error = {"type": "MissingTargetPath"}
        failure_packet = emit_apply_failure(apply_error)
        return {
            "ok": False,
            "result": result,
            "resultPacket": failure_packet,
            "applyResult": {},
            "validation": {},
            "applyError": apply_error,
        }
    event_sink(
        "SemanticBudgetProgress",
        {
            "_type": "SemanticBudgetProgress",
            "_version": 1,
            "strategy": SEMANTIC_BUDGET_STRATEGY,
            "state": "APPLYING",
            "targetPath": target_path,
        },
    )
    payload = {
        "activeFile": active_file,
        "prompt": prompt,
        "applyVariables": False,
        "applyWiki": False,
        "fragments": [{"path": target_path, "text": result.text}],
    }
    try:
        apply_result = await asyncio.to_thread(
            story_project_service.apply_story_generation_increment,
            workspace_root,
            payload,
            generation_contract=turn_contract,
        )
    except Exception as exc:
        apply_error = {
            "type": "StoryGenerationApplyError",
            "causeType": type(exc).__name__,
        }
        failure_packet = emit_apply_failure(apply_error)
        return {
            "ok": False,
            "result": result,
            "resultPacket": failure_packet,
            "applyResult": {},
            "validation": {},
            "targetPath": target_path,
            "applyError": apply_error,
        }
    apply_result = dict(apply_result) if isinstance(apply_result, dict) else {}
    if not bool(apply_result.get("ok")):
        apply_error = {"type": "StoryGenerationApplyRejected"}
        failure_packet = emit_apply_failure(apply_error)
        return {
            "ok": False,
            "result": result,
            "resultPacket": failure_packet,
            "applyResult": apply_result,
            "validation": {},
            "targetPath": target_path,
            "applyError": apply_error,
        }
    try:
        validation = await asyncio.to_thread(
            story_project_service.validate_story_generation_turn,
            workspace_root,
            turn_contract,
        )
    except Exception as exc:
        apply_error = {
            "type": "StoryGenerationPostApplyValidationError",
            "causeType": type(exc).__name__,
        }
        failure_packet = emit_apply_failure(apply_error)
        return {
            "ok": False,
            "result": result,
            "resultPacket": failure_packet,
            "applyResult": apply_result,
            "validation": {},
            "targetPath": target_path,
            "applyError": apply_error,
        }
    validation = dict(validation) if isinstance(validation, dict) else {}
    if not bool(validation.get("passed")):
        apply_error = {"type": "StoryGenerationPostApplyValidationFailed"}
        failure_packet = emit_apply_failure(apply_error)
        return {
            "ok": False,
            "result": result,
            "resultPacket": failure_packet,
            "applyResult": apply_result,
            "validation": validation,
            "targetPath": target_path,
            "applyError": apply_error,
        }

    completion_packet = {
        "_type": "SemanticBudgetProgress",
        "_version": 1,
        "strategy": SEMANTIC_BUDGET_STRATEGY,
        **dict(deferred_completion or {}),
        "state": "COMPLETED",
        "status": result.status,
        "generatedWordCount": result.generated_word_count,
        "withinAcceptance": result.within_acceptance,
    }
    event_sink("SemanticBudgetProgress", completion_packet)
    event_sink("SemanticBudgetResult", result_packet)
    return {
        "ok": True,
        "result": result,
        "resultPacket": result_packet,
        "applyResult": apply_result,
        "validation": validation,
        "targetPath": target_path,
        "applyError": {},
    }


def _semantic_budget_failure_message(outcome: Dict[str, Any]) -> str:
    apply_error = outcome.get("applyError") if isinstance(outcome.get("applyError"), dict) else {}
    apply_error_type = str(apply_error.get("type") or "")
    apply_labels = {
        "MissingTargetPath": "语义场景正文缺少合法目标路径，项目未写入。",
        "StoryGenerationApplyError": "语义场景正文写入时发生错误，Execution 已停止并保留审计。",
        "StoryGenerationApplyRejected": "项目服务拒绝语义场景正文写入，Execution 已停止。",
        "StoryGenerationPostApplyValidationError": "语义场景正文写入后验收发生错误，Execution 以失败收尾。",
        "StoryGenerationPostApplyValidationFailed": "语义场景正文已写入，但未通过落盘后项目验收。",
    }
    if apply_error_type in apply_labels:
        return apply_labels[apply_error_type]
    result = outcome.get("result")
    status = str(getattr(result, "status", "") or "")
    labels = {
        "failed_provider": "语义场景生成的 Provider 请求失败，项目未写入。",
        "failed_plan": "语义场景计划无效，项目未写入。",
        "failed_quality": "候选正文未通过机械质量门禁，项目未写入。",
        "failed_length": "候选正文未进入章节字数放行区间，项目未写入。",
    }
    return labels.get(status, "语义场景候选未能安全写入项目。")


def _bounded_story_generation_gate(
    workspace_root: Path,
    turn_contract: Dict[str, Any],
) -> Dict[str, Any]:
    """Decide whether this turn runs the bounded prose path (plan §5.1, §8.1).

    The gate is narrow on purpose. ``wordCountPolicy.version`` alone cannot
    select it, so the turn must additionally use the current chapter contract
    or the tier contract's candidate scope, with authoritative fragment paths
    the planner validated. Anything else keeps the Agent tool loop untouched.
    """

    intent = turn_contract.get("intentFrame") if isinstance(turn_contract.get("intentFrame"), dict) else {}
    turn_plan = turn_contract.get("turnPlan") if isinstance(turn_contract.get("turnPlan"), dict) else {}
    policy = turn_plan.get("wordCountPolicy") if isinstance(turn_plan.get("wordCountPolicy"), dict) else {}
    tier_mode = str(policy.get("mode") or "").strip().lower() == "tier"
    control = (
        turn_plan.get("generationControl")
        if isinstance(turn_plan.get("generationControl"), dict)
        else {}
    )
    if str(intent.get("primary") or "").strip().lower() != "story_generation":
        return {"enabled": False, "reason": "not_story_generation"}
    if str(control.get("strategy") or "").strip().lower() == SEMANTIC_BUDGET_STRATEGY:
        return {"enabled": False, "reason": "semantic_budget_requested"}
    if int(policy.get("version") or 0) < BOUNDED_WORD_COUNT_POLICY_VERSION:
        return {"enabled": False, "reason": "legacy_word_count_policy"}
    word_count_scope = str(policy.get("scope") or "").strip().lower()
    if not (
        word_count_scope == "chapter"
        or (tier_mode and word_count_scope == "candidate")
    ):
        return {"enabled": False, "reason": "not_chapter_scoped"}
    if str(turn_plan.get("operationType") or "").strip().lower() != "create_new":
        return {"enabled": False, "reason": "operation_not_create_new"}

    def close_plan(
        reason: str,
        issues: List[str],
        *,
        validation: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return {
            "enabled": False,
            "terminal": True,
            "reason": reason,
            "error": {
                "type": "ChapterPlanValidationFailed",
                "issues": list(dict.fromkeys(str(item) for item in issues if str(item))),
                "validation": dict(validation or {}),
            },
        }

    targets = (
        turn_plan.get("fragmentTargets")
        if isinstance(turn_plan.get("fragmentTargets"), list)
        else []
    )
    if not targets:
        return close_plan("no_fragment_targets", ["missing_fragment_paths"])
    # The chapter decision is a pre-prose hard gate: without an authoritative
    # path the draft could land in the wrong chapter, and its length would then
    # describe something other than the planned one.
    if not str(turn_plan.get("authoritativeChapterPath") or "").strip():
        return close_plan(
            "no_authoritative_chapter_path",
            ["missing_authoritative_chapter_path"],
        )

    action = str(turn_plan.get("chapterAction") or "").strip()
    target_chapter_number = int(turn_plan.get("targetChapterNumber") or 0)
    authoritative_chapter_path = str(
        turn_plan.get("authoritativeChapterPath") or ""
    ).strip()
    authoritative_fragment_paths = [
        str(item or "").strip().replace("\\", "/").lstrip("/")
        for item in list(turn_plan.get("authoritativeFragmentPaths") or [])
    ]
    target_paths = [
        str(item.get("path") or "").strip().replace("\\", "/").lstrip("/")
        for item in targets
        if isinstance(item, dict)
    ]
    published_validation = (
        turn_plan.get("chapterPlanValidation")
        if isinstance(turn_plan.get("chapterPlanValidation"), dict)
        else {}
    )
    if not published_validation:
        return close_plan(
            "chapter_plan_validation_missing",
            ["missing_published_chapter_plan_validation"],
        )
    if not bool(published_validation.get("passed")):
        published_issues = (
            published_validation.get("issues")
            if isinstance(published_validation.get("issues"), list)
            else []
        )
        return close_plan(
            "chapter_plan_validation_failed",
            [str(item) for item in published_issues]
            or ["published_chapter_plan_validation_failed"],
            validation=published_validation,
        )

    published_matches = bool(
        str(published_validation.get("action") or "") == action
        and int(published_validation.get("targetChapterNumber") or 0)
        == target_chapter_number
        and str(published_validation.get("authoritativeChapterPath") or "").strip()
        == authoritative_chapter_path
        and list(published_validation.get("authoritativeFragmentPaths") or [])
        == authoritative_fragment_paths
        and target_paths == authoritative_fragment_paths
    )
    if not published_matches:
        return close_plan(
            "chapter_plan_contract_mismatch",
            ["published_chapter_plan_mismatch"],
            validation=published_validation,
        )

    chapter_states = story_project_service.list_chapter_states(workspace_root)
    runtime_validation = validate_chapter_plan(
        workspace_root,
        action=action,
        target_chapter_number=target_chapter_number,
        authoritative_chapter_path=authoritative_chapter_path,
        fragment_paths=authoritative_fragment_paths,
        chapter_numbers=tuple(state.chapter_number for state in chapter_states),
    )
    if not bool(runtime_validation.get("passed")):
        return close_plan(
            "chapter_plan_runtime_validation_failed",
            [str(item) for item in list(runtime_validation.get("issues") or [])],
            validation=runtime_validation,
        )

    runtime_retained = (
        story_project_service.count_chapter_story_words(
            workspace_root,
            authoritative_chapter_path,
        )
        if action in {"continue_current_chapter", "continue_current_fragment"}
        else 0
    )
    published_retained = max(0, int(policy.get("retainedWordCount") or 0))
    target_word_count = max(0, int(policy.get("target") or 0))
    runtime_remaining = max(0, target_word_count - runtime_retained)
    published_remaining = max(0, int(policy.get("remainingWordCount") or 0))
    state_changed = (
        published_retained != runtime_retained
        if tier_mode
        else published_retained != runtime_retained
        or published_remaining != runtime_remaining
    )
    if state_changed:
        state_validation = {
            **runtime_validation,
            "retainedWordCount": runtime_retained,
        }
        if not tier_mode:
            state_validation["remainingWordCount"] = runtime_remaining
        return close_plan(
            "chapter_word_count_state_changed",
            ["chapter_word_count_state_changed"],
            validation=state_validation,
        )
    if not tier_mode and target_word_count > 0 and runtime_remaining <= 0:
        return {
            "enabled": False,
            "terminal": True,
            "reason": "chapter_target_already_reached",
            "error": {
                "type": "ChapterWordCountTargetReached",
                "issues": ["no_remaining_chapter_budget"],
                "retainedWordCount": runtime_retained,
                "remainingWordCount": runtime_remaining,
                "targetWordCount": target_word_count,
            },
        }
    flags = FeatureFlags(Path(workspace_root).resolve(), FEATURE_FLAG_DEFAULTS)
    if not flags.get_bool("BOUNDED_STORY_GENERATION_ENABLED"):
        return {"enabled": False, "reason": "feature_flag_disabled"}
    precision = policy.get("precision") if isinstance(policy.get("precision"), dict) else {}
    return {
        "enabled": True,
        "reason": "enabled",
        "chapterLengthTier": (
            normalize_chapter_length_tier(policy.get("tier"))
            if tier_mode
            else ""
        ),
        "precisionEnabled": bool(precision.get("enabled")) and not tier_mode,
    }


def _safe_exception_diagnostic(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        return ""
    message = re.sub(
        r"(?i)(authorization|api[_-]?key|access[_-]?token|bearer)[\"']?\s*[:=]?\s*[\"']?[^\s,;\"'}]+",
        r"\1=[redacted]",
        message,
    )
    message = re.sub(r"(?i)\bsk-[a-z0-9_-]{12,}\b", "[redacted-api-key]", message)
    return message[:2000]


async def _execute_bounded_story_generation(
    *,
    prompt: str,
    trace_id: str,
    active_file: str,
    workspace_root: Path,
    turn_contract: Dict[str, Any],
    event_sink: Callable[[str, Dict[str, Any]], None],
    commit_state: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Run one bounded turn and validate the result (plan §5.2, §7.3, §8.1).

    Both adapters use zero transport retries. One user generation may spend one
    draft plus one bounded revision or one independent second draft, but a
    transport retry must not turn that contract into a hidden third Provider
    request.
    """

    draft_adapter = CoomiStoryGenerationAdapter(
        trace_id=trace_id,
        reasoning_effort=str(turn_contract.get("reasoningEffort") or "auto"),
        maximum_transport_retries=0,
        event_sink=event_sink,
        attempt_event_name="StoryProviderAttempt",
    )
    revision_adapter = CoomiStoryGenerationAdapter(
        trace_id=trace_id,
        reasoning_effort=str(turn_contract.get("reasoningEffort") or "auto"),
        maximum_transport_retries=0,
        event_sink=event_sink,
        attempt_event_name="StoryProviderAttempt",
    )
    elastic_enabled = FeatureFlags(
        Path(workspace_root).resolve(),
        FEATURE_FLAG_DEFAULTS,
    ).get_bool("ELASTIC_STORY_MANUSCRIPT_ENABLED")
    runner = BoundedStoryGeneration(
        adapter=draft_adapter,
        pipeline=get_story_generation_pipeline(),
        controller=get_story_length_precision_controller(),
        revision_adapter=revision_adapter,
        event_sink=event_sink,
        commit_state=commit_state,
        elastic_enabled=elastic_enabled,
    )
    try:
        result = await runner.run(
            workspace_root,
            trace_id=trace_id,
            turn_contract=turn_contract,
            prompt=prompt,
            active_file=active_file,
        )
    except Exception as exc:  # noqa: BLE001 - a failed draft ends the turn cleanly
        rejection_reason = str(getattr(exc, "reason", "") or "").strip()
        rejection_issues = [
            str(item)
            for item in list(getattr(exc, "issues", ()) or ())
            if str(item)
        ]
        diagnostic_message = _safe_exception_diagnostic(exc)
        retryable = isinstance(exc, (TimeoutError, ConnectionError, OSError)) or any(
            marker in diagnostic_message.casefold()
            for marker in (
                "timeout", "timed out", "connection", "temporarily unavailable",
                "http 429", "http 500", "http 502", "http 503", "http 504",
            )
        )
        return {
            "ok": False,
            "result": {},
            "callAccounting": runner.accounting.payload(),
            "error": {
                "type": "StoryDraftGenerationFailed",
                "causeType": type(exc).__name__,
                **({"message": diagnostic_message} if diagnostic_message else {}),
                "retryable": retryable,
                **({"reason": rejection_reason} if rejection_reason else {}),
                **({"issues": rejection_issues} if rejection_issues else {}),
            },
        }

    if not bool(result.get("committed")):
        return {
            "ok": False,
            "result": result,
            "callAccounting": dict(result.get("callAccounting") or {}),
            "error": {"type": "StoryGenerationApplyRejected"},
        }

    validation = await asyncio.to_thread(
        story_project_service.validate_story_generation_turn,
        workspace_root,
        turn_contract,
    )
    validation = dict(validation) if isinstance(validation, dict) else {}
    return {
        "ok": bool(validation.get("passed")),
        "result": result,
        "validation": validation,
        "callAccounting": dict(result.get("callAccounting") or {}),
        "error": (
            {} if bool(validation.get("passed")) else {"type": "StoryGenerationValidationFailed"}
        ),
    }


def _bounded_story_validation_packet(
    *,
    validation: Dict[str, Any],
    result: Dict[str, Any],
    trace_id: str,
    session_id: str,
    provider: str,
    model: str,
) -> Dict[str, Any]:
    """Extend StoryGenerationValidation with the bounded path's own facts (§10)."""

    selection = result.get("selection") if isinstance(result.get("selection"), dict) else {}
    tier_mode = bool(
        result.get("chapterLengthTier") or selection.get("chapterLengthTier")
    )
    if not validation and isinstance(selection.get("draftValidation"), dict):
        validation = dict(selection["draftValidation"])
    revision = (
        result.get("revisionOutcome") if isinstance(result.get("revisionOutcome"), dict) else {}
    )
    accounting = (
        result.get("callAccounting")
        if isinstance(result.get("callAccounting"), dict)
        else {}
    )
    selected_source = str(selection.get("source") or "")
    selected_status_key = (
        "secondDraftStatus" if selected_source == "second_draft" else "draftStatus"
    )
    selected_status = (
        selection.get(selected_status_key)
        if isinstance(selection.get(selected_status_key), dict)
        else {}
    )
    asymmetric_enabled = bool(
        result.get("asymmetricLengthEnabled", validation.get("asymmetricLengthEnabled"))
    )
    hard_minimum_passed = (
        validation.get("hardMinimumPassed")
        if "hardMinimumPassed" in validation
        else selected_status.get("hardMinimumPassed")
    )
    above_soft_maximum = (
        validation.get("aboveSoftMaximum")
        if "aboveSoftMaximum" in validation
        else selected_status.get("aboveSoftMaximum")
    )
    runtime_safety_exceeded = (
        validation.get("runtimeSafetyExceeded")
        if "runtimeSafetyExceeded" in validation
        else selected_status.get("runtimeSafetyExceeded")
    )
    runtime_safety_maximum = (
        validation.get("runtimeSafetyMaximum")
        if "runtimeSafetyMaximum" in validation
        else selected_status.get("runtimeSafetyMaximum")
    )
    precision_enabled = bool(result.get("precisionEnabled"))
    precision_achieved = (
        result.get("precisionAchieved")
        if "precisionAchieved" in result
        else selection.get("precisionAchieved")
    )
    normal_band_passed = (
        result.get("normalBandPassed")
        if "normalBandPassed" in result
        else selection.get("normalBandPassed")
    )
    overhead_ratio = (
        result.get("generatedOverheadRatio")
        if "generatedOverheadRatio" in result
        else selection.get("generatedOverheadRatio")
    )
    passed = bool(validation.get("passed"))
    if tier_mode:
        passed = bool(
            passed
            and selection.get("draftQualityPassed") is True
            and result.get("committed")
        )
    message = str(validation.get("message") or "")
    if passed and bool(validation.get("overBudget")):
        message = STORY_OVER_BUDGET_KEEP_MESSAGE
    elif passed and bool(validation.get("belowBudget")):
        message = STORY_UNDER_BUDGET_KEEP_MESSAGE
    packet = {
        **validation,
        "passed": passed,
        "status": (
            "warning"
            if passed
            and (
                bool(validation.get("belowBudget"))
                or bool(validation.get("overBudget"))
                or (tier_mode and not bool(selection.get("tierHit")))
                or (precision_enabled and not bool(precision_achieved))
            )
            else "success"
            if passed
            else "error"
        ),
        "message": message,
        # The write happened through the pipeline's single apply, not through an
        # Agent tool call, so the tool-based signal is reported as satisfied here
        # rather than being inferred from a ToolDone event that never arrives.
        "writeToolApplied": bool(result.get("committed")),
        "traceId": trace_id,
        "sessionId": session_id,
        "strategy": BOUNDED_STORY_GENERATION_STRATEGY,
        "chapterLengthTier": str(
            result.get("chapterLengthTier")
            or selection.get("chapterLengthTier")
            or validation.get("chapterLengthTier")
            or ""
        ),
        "tierHit": (
            bool(selection.get("tierHit")) if tier_mode else None
        ),
        "tierDeviation": (
            str(selection.get("tierDeviation") or "") if tier_mode else ""
        ),
        "machineQualityPassed": (
            bool(selection.get("draftQualityPassed")) if tier_mode else None
        ),
        "preciseWordCountEnabled": precision_enabled,
        "asymmetricLengthEnabled": asymmetric_enabled,
        "initialWordCount": int(result.get("draftWordCount") or 0),
        "finalWordCount": int(selection.get("finalWordCount") or 0),
        "revisionApplied": str(selection.get("source") or "") == "revision",
        "revisionOutcomeReason": str(selection.get("reason") or ""),
        "hardMinimumPassed": (
            bool(hard_minimum_passed) if hard_minimum_passed is not None else None
        ),
        "aboveSoftMaximum": (
            bool(above_soft_maximum) if above_soft_maximum is not None else None
        ),
        "runtimeSafetyExceeded": (
            bool(runtime_safety_exceeded)
            if runtime_safety_exceeded is not None
            else None
        ),
        "runtimeSafetyMaximum": (
            int(runtime_safety_maximum) if runtime_safety_maximum is not None else None
        ),
        "secondDraftWordCount": int(
            result.get("secondDraftWordCount")
            or selection.get("secondDraftWordCount")
            or 0
        ),
        "secondDraftApplied": selected_source == "second_draft",
        "asymmetricLengthLoss": selection.get("asymmetricLengthLoss"),
        "secondDraftCalls": int(accounting.get("secondDraftCalls") or 0),
        "lengthControlStrategy": str(
            result.get("lengthControlStrategy")
            or selection.get("lengthControlStrategy")
            or ""
        ),
        "canonicalWordCount": int(
            result.get("canonicalWordCount")
            or selection.get("canonicalWordCount")
            or result.get("draftWordCount")
            or 0
        ),
        "normalBandPassed": (
            bool(normal_band_passed) if normal_band_passed is not None else None
        ),
        "precisionAchieved": (
            bool(precision_achieved) if precision_enabled else None
        ),
        "selectedEditIds": [
            str(item)
            for item in list(
                result.get("selectedEditIds")
                or selection.get("selectedEditIds")
                or []
            )
        ],
        "rejectedEditIds": [
            str(item)
            for item in list(
                result.get("rejectedEditIds")
                or selection.get("rejectedEditIds")
                or []
            )
        ],
        "rejectedEditReasonCounts": {
            str(key): int(value)
            for key, value in dict(
                result.get("rejectedEditReasonCounts")
                or selection.get("rejectedEditReasonCounts")
                or {}
            ).items()
        },
        "evaluatedCombinationCount": int(
            result.get("evaluatedCombinationCount")
            or selection.get("evaluatedCombinationCount")
            or 0
        ),
        "lengthFallbackReason": str(
            result.get("lengthFallbackReason")
            or selection.get("lengthFallbackReason")
            or ""
        ),
        "generatedOverheadRatio": (
            float(overhead_ratio)
            if isinstance(overhead_ratio, (int, float))
            else None
        ),
        "revisionRejectionReasons": [
            str(item) for item in list(revision.get("qualityIssues") or [])
        ],
        "attemptKind": (
            PRECISION_REVISION_ATTEMPT_KIND
            if str(selection.get("source") or "") == "revision"
            else INITIAL_ATTEMPT_KIND
        ),
        "callAccounting": dict(result.get("callAccounting") or {}),
        "contractViolations": [str(item) for item in list(result.get("contractViolations") or [])],
        "providerCalls": int(
            dict(result.get("callAccounting") or {}).get("logicalStoryCalls") or 0
        ),
    }
    if tier_mode:
        # Tier SSE exposes the selected semantic tier, objective result and hit
        # status. Numeric bands remain internal observability data rather than a
        # user-facing promise.
        for key in (
            "chapterWordCountTarget",
            "targetWordCount",
            "targetWordCountMin",
            "targetWordCountMax",
            "acceptWordCountMin",
            "acceptWordCountMax",
            "preferredMinimum",
            "preferredMaximum",
            "softMaximum",
            "precisionMinimum",
            "precisionMaximum",
        ):
            packet.pop(key, None)
        packet["actualWordCount"] = int(result.get("draftWordCount") or 0)
        packet["initialWordCount"] = int(result.get("draftWordCount") or 0)
        packet["finalWordCount"] = (
            int(selection.get("finalWordCount") or 0)
            if bool(result.get("committed"))
            else 0
        )
        packet["revisionApplied"] = False
        packet["secondDraftApplied"] = False
        packet["normalBandPassed"] = bool(selection.get("tierHit"))
        packet["precisionAchieved"] = None
    return packet


def _bounded_story_failure_message(outcome: Dict[str, Any]) -> str:
    error = outcome.get("error") if isinstance(outcome.get("error"), dict) else {}
    labels = {
        "StoryDraftGenerationFailed": "正文草稿的 Provider 请求失败，项目未写入。",
        "StoryGenerationApplyRejected": "项目服务拒绝本轮正文写入，暂存候选已保留。",
        "StoryGenerationValidationFailed": "正文已写入，但未通过 Storydex 客观校验。",
    }
    return labels.get(
        str(error.get("type") or ""),
        "有界正文路径未能安全完成本轮写入。",
    )


def _should_create_task_checklist(intent_frame: Dict[str, Any]) -> bool:
    """仅复杂任务才创建任务清单。

    门控依据意图帧的 ``complexity``：``complex`` 才建清单。缺失时保守地不建，
    避免给问候/简单问询/单步编辑套上多余的清单流程。纯问候/理解性问询即便被
    误标为复杂，也不建清单。
    """
    frame = intent_frame if isinstance(intent_frame, dict) else {}
    operation_type = str(frame.get("operationType") or "").strip().lower()
    if operation_type in {"greeting", "inquiry"}:
        return False
    return str(frame.get("complexity") or "").strip().lower() == "complex"


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
    # 任务清单按"复杂度"门控，而非意图类别：只有复杂任务（多步骤，需读文件、
    # 重构、再更新变量/WIKI、清理等）才建清单。简单任务/闲聊不建清单。
    if not _should_create_task_checklist(intent_frame):
        return []
    del prompt, session_id, workspace_root, active_file
    candidates: Any = None
    for container in (turn_contract, story_generation):
        if not isinstance(container, dict):
            continue
        if isinstance(container.get("tasks"), list):
            candidates = container.get("tasks")
            break
        plan = container.get("plan") if isinstance(container.get("plan"), dict) else {}
        if isinstance(plan.get("tasks"), list):
            candidates = plan.get("tasks")
            break
        if isinstance(plan.get("steps"), list):
            candidates = plan.get("steps")
            break
    return _normalize_task_plan(candidates, trace_id=trace_id)


def _normalize_task_plan(value: Any, *, trace_id: str) -> List[Dict[str, Any]]:
    raw_tasks = value if isinstance(value, list) else []
    now = _now_iso()
    tasks: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_tasks[:10]):
        record = item if isinstance(item, dict) else {"title": str(item or "")}
        title = str(
            record.get("title")
            or record.get("name")
            or record.get("task")
            or record.get("step")
            or ""
        ).strip()
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
    model_attempt_reply_baseline = 0
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
        if event_name == "TurnPhase" and str(packet.get("phase") or "") == "model":
            model_attempt_reply_baseline = len("".join(reply_chunks))
        if event_name == "ConnectionRetry":
            reset_characters = max(
                0, len("".join(reply_chunks)) - model_attempt_reply_baseline
            )
            packet["resetTextCharacters"] = reset_characters
            _rollback_reply_chunks(reply_chunks, reset_characters)
            _rollback_trace_text_events(events, reset_characters)
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
    knowledge_projection: Dict[str, Any] = {}
    completed_after_commit_cancellation = False

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
            # 清单门控改为按复杂度：只有复杂任务才规划任务清单，简单任务/闲聊/问询不建清单。
            should_plan = _should_create_task_checklist(intent_frame)
            if not should_plan:
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
            if not should_plan or planning_task is None:
                yield _encode_sse(
                    "TurnPhase",
                    _turn_phase_packet(
                        trace_id=trace_id,
                        session_id=session_id,
                        phase="task_planning",
                        label="执行步骤规划完成",
                        status="success",
                        phase_started=planning_started,
                        detail=("无需生成执行步骤" if not should_plan else f"已生成 {len(task_plan)} 个执行步骤"),
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

            semantic_gate = (
                _semantic_budget_gate(workspace_root, turn_contract)
                if should_run_coomi
                else {"requested": False, "enabled": False, "reason": "turn_not_runnable"}
            )
            if semantic_gate.get("requested") and not semantic_gate.get("enabled"):
                fallback_packet = {
                    "_type": "SemanticBudgetFallback",
                    "_version": 1,
                    "strategy": SEMANTIC_BUDGET_STRATEGY,
                    "status": "warning",
                    "reason": str(semantic_gate.get("reason") or "not_enabled"),
                    "degradedFallbackReason": str(semantic_gate.get("reason") or "not_enabled"),
                }
                events.append(
                    _event_to_trace_event(
                        "SemanticBudgetFallback",
                        fallback_packet,
                        len(events) + 1,
                    )
                )
                yield _encode_sse("SemanticBudgetFallback", fallback_packet)

            if semantic_gate.get("enabled") and not execution_handle.is_cancelled:
                if replacement is not None and not replacement.accepted:
                    await asyncio.to_thread(replacement.accept)
                if tracker is not None:
                    for task_event_name, task_payload in tracker.start_next():
                        events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                        yield _encode_sse(task_event_name, task_payload)
                semantic_started = time.perf_counter()
                yield _encode_sse(
                    "TurnPhase",
                    _turn_phase_packet(
                        trace_id=trace_id,
                        session_id=session_id,
                        phase="model_execution",
                        label="正在执行语义场景预算生成",
                        status="running",
                        phase_started=semantic_started,
                    ),
                )
                provider_status = _coomi_status_for_execution(workspace_root)
                started_packet = {
                    "_type": "AgentStarted",
                    "_version": 1,
                    "session_id": session_id,
                    "mode": "semantic_budget",
                    "query": prompt,
                    "llmModel": str(provider_status.get("model") or ""),
                    "llmProvider": str(provider_status.get("providerId") or ""),
                    "coomiStatus": provider_status,
                }
                events.append(_event_to_trace_event("AgentStarted", started_packet, len(events) + 1))
                yield _encode_sse("AgentStarted", started_packet)

                semantic_events: asyncio.Queue[tuple[str, Dict[str, Any]]] = asyncio.Queue()
                semantic_apply_started = False

                def semantic_event_sink(name: str, packet: Dict[str, Any]) -> None:
                    nonlocal semantic_apply_started
                    if (
                        name == "SemanticBudgetProgress"
                        and str(packet.get("state") or "").upper() == "APPLYING"
                    ):
                        semantic_apply_started = True
                    semantic_events.put_nowait((name, dict(packet)))

                semantic_task = asyncio.create_task(
                    _execute_semantic_budget_generation(
                        prompt=prompt,
                        trace_id=trace_id,
                        active_file=active_file,
                        workspace_root=workspace_root,
                        turn_contract=turn_contract,
                        event_sink=semantic_event_sink,
                    ),
                    name=f"storydex-semantic-budget-{trace_id}",
                )
                semantic_outcome: Dict[str, Any] = {}
                semantic_cancelled = False
                try:
                    while not semantic_task.done() or not semantic_events.empty():
                        if execution_handle.is_cancelled or cancellation_token.is_cancelled():
                            semantic_cancelled = True
                            if not semantic_apply_started:
                                semantic_task.cancel()
                                break
                            # asyncio.to_thread cannot stop an in-flight project write.
                            # Let that single atomic apply finish before Git/Trace finalization.
                        try:
                            semantic_event_name, semantic_packet = await asyncio.wait_for(
                                semantic_events.get(),
                                timeout=_PHASE_HEARTBEAT_SECONDS,
                            )
                        except asyncio.TimeoutError:
                            yield _encode_sse(
                                "TurnPhase",
                                _turn_phase_packet(
                                    trace_id=trace_id,
                                    session_id=session_id,
                                    phase="model_execution",
                                    label="正在执行语义场景预算生成",
                                    status="running",
                                    phase_started=semantic_started,
                                    heartbeat=True,
                                ),
                            )
                            continue
                        events.append(
                            _event_to_trace_event(
                                semantic_event_name,
                                semantic_packet,
                                len(events) + 1,
                            )
                        )
                        yield _encode_sse(semantic_event_name, semantic_packet)
                    if semantic_cancelled:
                        await asyncio.gather(semantic_task, return_exceptions=True)
                    else:
                        semantic_outcome = await semantic_task
                finally:
                    if not semantic_task.done():
                        semantic_task.cancel()
                        await asyncio.gather(semantic_task, return_exceptions=True)

                if semantic_cancelled:
                    terminal_event = (
                        "AgentCancelled",
                        {
                            "_type": "AgentCancelled",
                            "_version": 1,
                            "session_id": session_id,
                            "reason": execution_handle.cancel_reason or "cancelled",
                        },
                    )
                    completed = False
                elif bool(semantic_outcome.get("ok")):
                    validation_packet = dict(semantic_outcome.get("validation") or {})
                    validation_packet.update(
                        {
                            "passed": True,
                            "status": "success",
                            "writeToolApplied": True,
                            "strategy": SEMANTIC_BUDGET_STRATEGY,
                            "traceId": trace_id,
                            "sessionId": session_id,
                            "providerCalls": int(
                                (semantic_outcome.get("resultPacket") or {}).get("providerCalls") or 0
                            ),
                        }
                    )
                    events.append(
                        _event_to_trace_event(
                            "StoryGenerationValidation",
                            validation_packet,
                            len(events) + 1,
                        )
                    )
                    yield _encode_sse("StoryGenerationValidation", validation_packet)
                    story_length_calibration_service.record_generation_result(
                        workspace_root,
                        turn_contract=turn_contract,
                        validation=validation_packet,
                        provider=str(provider_status.get("providerId") or ""),
                        model=str(provider_status.get("model") or ""),
                    )
                    target_path = str(semantic_outcome.get("targetPath") or "")
                    reply = f"正文已生成并写入 {target_path}。"
                    reply_packet = {
                        "_type": "TextChunk",
                        "_version": 1,
                        "content": reply,
                    }
                    reply_chunks.append(reply)
                    events.append(_event_to_trace_event("TextChunk", reply_packet, len(events) + 1))
                    yield _encode_sse("TextChunk", reply_packet)
                    completed = True
                    terminal_event = (
                        "AgentCompleted",
                        {
                            "_type": "AgentCompleted",
                            "_version": 1,
                            "session_id": session_id,
                            "route": "semantic_budget",
                            "status": "completed",
                            "duration_ms": int((time.perf_counter() - semantic_started) * 1000),
                        },
                    )
                else:
                    error_message = _semantic_budget_failure_message(semantic_outcome)
                    error_packet = {
                        "_type": "AgentError",
                        "_version": 1,
                        "error_type": "SemanticBudgetGenerationFailed",
                        "message": error_message,
                        "details": {
                            "runtime": "semantic_budget",
                            "result": dict(semantic_outcome.get("resultPacket") or {}),
                            "applyError": dict(semantic_outcome.get("applyError") or {}),
                        },
                    }
                    terminal_event = ("AgentError", error_packet)
                    completed = False
                should_run_coomi = False

            bounded_gate = (
                _bounded_story_generation_gate(workspace_root, turn_contract)
                if should_run_coomi
                else {"enabled": False, "reason": "turn_not_runnable"}
            )
            if bounded_gate.get("terminal") and should_run_coomi:
                gate_error = (
                    bounded_gate.get("error")
                    if isinstance(bounded_gate.get("error"), dict)
                    else {}
                )
                error_type = str(gate_error.get("type") or "ChapterPlanValidationFailed")
                warning_message = (
                    "本章已达到目标字数。请明确要求超写，或续写下一章。"
                    if error_type == "ChapterWordCountTargetReached"
                    else "章节写入计划已失效，Storydex 已在生成正文前停止执行。"
                )
                followup_mailbox_service.pause(
                    workspace_root=workspace_root,
                    session_id=session_id,
                    reason="chapter_plan_validation_failed",
                )
                warning_packet = {
                    "_type": "AgentWarning",
                    "_version": 1,
                    "warning_type": error_type,
                    "error_type": error_type,
                    "status": "warning",
                    "message": warning_message,
                    "details": {
                        "runtime": BOUNDED_STORY_GENERATION_STRATEGY,
                        "reason": str(bounded_gate.get("reason") or ""),
                        "issues": list(gate_error.get("issues") or []),
                        "validation": dict(gate_error.get("validation") or {}),
                        "providerCalls": 0,
                    },
                }
                events.append(
                    _event_to_trace_event("AgentWarning", warning_packet, len(events) + 1)
                )
                yield _encode_sse("AgentWarning", warning_packet)
                reply_chunks.append(warning_message)
                completed = True
                terminal_event = (
                    "AgentCompleted",
                    {
                        "_type": "AgentCompleted",
                        "_version": 1,
                        "session_id": session_id,
                        "route": BOUNDED_STORY_GENERATION_STRATEGY,
                        "status": "warning",
                    },
                )
                should_run_coomi = False
            if bounded_gate.get("enabled") and not execution_handle.is_cancelled:
                if replacement is not None and not replacement.accepted:
                    await asyncio.to_thread(replacement.accept)
                if tracker is not None:
                    for task_event_name, task_payload in tracker.start_next():
                        events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                        yield _encode_sse(task_event_name, task_payload)
                bounded_started = time.perf_counter()
                yield _encode_sse(
                    "TurnPhase",
                    _turn_phase_packet(
                        trace_id=trace_id,
                        session_id=session_id,
                        phase="model_execution",
                        label="正在生成本章正文",
                        status="running",
                        phase_started=bounded_started,
                    ),
                )
                provider_status = _coomi_status_for_execution(workspace_root)
                bounded_provider = str(provider_status.get("providerId") or "")
                bounded_model = str(provider_status.get("model") or "")
                started_packet = {
                    "_type": "AgentStarted",
                    "_version": 1,
                    "session_id": session_id,
                    "mode": BOUNDED_STORY_GENERATION_STRATEGY,
                    "query": prompt,
                    "llmModel": bounded_model,
                    "llmProvider": bounded_provider,
                    "chapterLengthTier": str(
                        bounded_gate.get("chapterLengthTier") or ""
                    ),
                    "preciseWordCountEnabled": bool(bounded_gate.get("precisionEnabled")),
                    "coomiStatus": provider_status,
                }
                events.append(_event_to_trace_event("AgentStarted", started_packet, len(events) + 1))
                yield _encode_sse("AgentStarted", started_packet)

                bounded_events: asyncio.Queue[tuple[str, Dict[str, Any]]] = asyncio.Queue()
                bounded_commit_state: Dict[str, Any] = {
                    "started": False,
                    "finished": False,
                }

                def bounded_event_sink(name: str, packet: Dict[str, Any]) -> None:
                    bounded_events.put_nowait((name, dict(packet)))

                bounded_task = asyncio.create_task(
                    _execute_bounded_story_generation(
                        prompt=prompt,
                        trace_id=trace_id,
                        active_file=active_file,
                        workspace_root=workspace_root,
                        turn_contract=turn_contract,
                        event_sink=bounded_event_sink,
                        commit_state=bounded_commit_state,
                    ),
                    name=f"storydex-bounded-story-{trace_id}",
                )
                bounded_outcome: Dict[str, Any] = {}
                bounded_cancelled = False
                bounded_cancel_deferred = False
                try:
                    while not bounded_task.done() or not bounded_events.empty():
                        if execution_handle.is_cancelled or cancellation_token.is_cancelled():
                            if bool(bounded_commit_state.get("started")):
                                # Once the atomic commit begins, its result owns
                                # the terminal status. Cancelling the coroutine
                                # here could report "cancelled" after the files
                                # were already replaced.
                                bounded_cancel_deferred = True
                            else:
                                bounded_cancelled = True
                                bounded_task.cancel()
                                break
                        try:
                            bounded_event_name, bounded_packet = await asyncio.wait_for(
                                bounded_events.get(),
                                timeout=_PHASE_HEARTBEAT_SECONDS,
                            )
                        except asyncio.TimeoutError:
                            yield _encode_sse(
                                "TurnPhase",
                                _turn_phase_packet(
                                    trace_id=trace_id,
                                    session_id=session_id,
                                    phase="model_execution",
                                    label="正在生成本章正文",
                                    status="running",
                                    phase_started=bounded_started,
                                    heartbeat=True,
                                ),
                            )
                            continue
                        events.append(
                            _event_to_trace_event(
                                bounded_event_name,
                                bounded_packet,
                                len(events) + 1,
                            )
                        )
                        yield _encode_sse(bounded_event_name, bounded_packet)
                    if bounded_cancelled:
                        await asyncio.gather(bounded_task, return_exceptions=True)
                    else:
                        bounded_outcome = await bounded_task
                        if bounded_cancel_deferred:
                            completed_after_commit_cancellation = True
                finally:
                    if not bounded_task.done():
                        bounded_task.cancel()
                        await asyncio.gather(bounded_task, return_exceptions=True)

                bounded_result = (
                    bounded_outcome.get("result")
                    if isinstance(bounded_outcome.get("result"), dict)
                    else {}
                )
                if bounded_cancelled:
                    terminal_event = (
                        "AgentCancelled",
                        {
                            "_type": "AgentCancelled",
                            "_version": 1,
                            "session_id": session_id,
                            "reason": execution_handle.cancel_reason or "cancelled",
                        },
                    )
                    completed = False
                else:
                    validation_packet = _bounded_story_validation_packet(
                        validation=dict(bounded_outcome.get("validation") or {}),
                        result=bounded_result,
                        trace_id=trace_id,
                        session_id=session_id,
                        provider=bounded_provider,
                        model=bounded_model,
                    )
                    if bool(validation_packet.get("applicable")):
                        events.append(
                            _event_to_trace_event(
                                "StoryGenerationValidation",
                                validation_packet,
                                len(events) + 1,
                            )
                        )
                        yield _encode_sse("StoryGenerationValidation", validation_packet)
                        turn_policy = dict(
                            (turn_contract.get("turnPlan") or {}).get(
                                "wordCountPolicy"
                            )
                            or {}
                        )
                        if str(turn_policy.get("mode") or "") == "tier":
                            accounting = dict(
                                bounded_result.get("callAccounting") or {}
                            )
                            story_length_tier_calibration_service.record_sample(
                                workspace_root,
                                provider=bounded_provider,
                                model=bounded_model,
                                tier=turn_policy.get("tier"),
                                prompt_version=str(
                                    turn_policy.get("promptVersion") or ""
                                ),
                                actual_word_count=int(
                                    bounded_result.get("draftWordCount")
                                    or validation_packet.get("actualWordCount")
                                    or 0
                                ),
                                tier_hit=bool(validation_packet.get("tierHit")),
                                structure_passed=bool(
                                    validation_packet.get("structurePassed")
                                ),
                                machine_quality_passed=bool(
                                    validation_packet.get(
                                        "machineQualityPassed"
                                    )
                                ),
                                word_count_scope=str(
                                    validation_packet.get("wordCountScope")
                                    or ""
                                ),
                                attempt_kind=INITIAL_ATTEMPT_KIND,
                                logical_prose_calls=int(
                                    accounting.get("logicalStoryCalls") or 0
                                ),
                                completion_tokens=(
                                    int(bounded_result["draftCompletionTokens"])
                                    if bounded_result.get("draftCompletionTokens")
                                    is not None
                                    else None
                                ),
                                duration_ms=(
                                    int(bounded_result["draftDurationMs"])
                                    if bounded_result.get("draftDurationMs")
                                    is not None
                                    else None
                                ),
                                trace_id=trace_id,
                            )
                        else:
                            story_length_calibration_service.record_paragraph_generation_result(
                                workspace_root,
                                turn_contract=turn_contract,
                                validation=validation_packet,
                                provider=bounded_provider,
                                model=bounded_model,
                            )
                            story_length_calibration_service.record_generation_result(
                                workspace_root,
                                turn_contract=turn_contract,
                                validation={
                                    **validation_packet,
                                    "attemptKind": INITIAL_ATTEMPT_KIND,
                                    "generatedWordCount": int(
                                        bounded_result.get("draftWordCount")
                                        or validation_packet.get("generatedWordCount")
                                        or 0
                                    ),
                                },
                                provider=bounded_provider,
                                model=bounded_model,
                            )
                    accounting_packet = {
                        "_type": "StoryCallAccounting",
                        "_version": 1,
                        "traceId": trace_id,
                        "sessionId": session_id,
                        "chapterLengthTier": str(
                            bounded_result.get("chapterLengthTier") or ""
                        ),
                        "preciseWordCountEnabled": bool(bounded_result.get("precisionEnabled")),
                        "asymmetricLengthEnabled": bool(
                            bounded_result.get("asymmetricLengthEnabled")
                        ),
                        **dict(bounded_outcome.get("callAccounting") or {}),
                        "contractViolations": [
                            str(item)
                            for item in list(bounded_result.get("contractViolations") or [])
                        ],
                    }
                    events.append(
                        _event_to_trace_event(
                            "StoryCallAccounting",
                            accounting_packet,
                            len(events) + 1,
                        )
                    )
                    yield _encode_sse("StoryCallAccounting", accounting_packet)

                    if bool(bounded_outcome.get("ok")):
                        selection = (
                            bounded_result.get("selection")
                            if isinstance(bounded_result.get("selection"), dict)
                            else {}
                        )
                        chapter_path = str(
                            (turn_contract.get("turnPlan") or {}).get("authoritativeChapterPath")
                            or ""
                        )
                        final_word_count = int(selection.get("finalWordCount") or 0)
                        turn_plan = (
                            turn_contract.get("turnPlan")
                            if isinstance(turn_contract.get("turnPlan"), dict)
                            else {}
                        )
                        word_count_policy = (
                            turn_plan.get("wordCountPolicy")
                            if isinstance(turn_plan.get("wordCountPolicy"), dict)
                            else {}
                        )
                        tier_mode = (
                            str(word_count_policy.get("mode") or "") == "tier"
                        )
                        count_summary = (
                            f"本次续写 {final_word_count} 字"
                            if tier_mode
                            else f"字数 {final_word_count}"
                        )
                        reply = (
                            f"章节已写入 {chapter_path}，{count_summary}"
                            if chapter_path
                            else f"章节已写入，{count_summary}"
                        )
                        if tier_mode:
                            tier_labels = {
                                "short": "短档",
                                "medium": "中档",
                                "long": "长档",
                            }
                            tier_label = tier_labels.get(
                                str(validation_packet.get("chapterLengthTier") or ""),
                                "所选档位",
                            )
                            reply += (
                                f"，{tier_label}已命中"
                                if bool(validation_packet.get("tierHit"))
                                else f"，{tier_label}未命中，正文按原稿保留"
                            )
                        elif bool(validation_packet.get("preciseWordCountEnabled")) and not bool(
                            validation_packet.get("precisionAchieved")
                        ):
                            product_target = max(
                                1,
                                int(
                                    word_count_policy.get("target")
                                    or turn_plan.get("chapterWordCountTarget")
                                    or 1
                                ),
                            )
                            precision_low, precision_high = chapter_precision_band(
                                product_target
                            )
                            reply += (
                                f"，未达到精确范围 {precision_low}-{precision_high}"
                            )
                        elif validation_packet.get("normalBandPassed") is False:
                            product_target = max(
                                1,
                                int(
                                    word_count_policy.get("target")
                                    or turn_plan.get("chapterWordCountTarget")
                                    or 1
                                ),
                            )
                            normal_low, normal_high = chapter_normal_band(product_target)
                            reply += f"，未达到正常范围 {normal_low}-{normal_high}"
                        reply += "。"
                        if not tier_mode and bool(validation_packet.get("overBudget")):
                            reply = f"{reply}{STORY_OVER_BUDGET_KEEP_MESSAGE}"
                        reply_packet = {
                            "_type": "TextChunk",
                            "_version": 1,
                            "content": reply,
                        }
                        reply_chunks.append(reply)
                        events.append(
                            _event_to_trace_event("TextChunk", reply_packet, len(events) + 1)
                        )
                        yield _encode_sse("TextChunk", reply_packet)
                        completed = True
                        terminal_event = (
                            "AgentCompleted",
                            {
                                "_type": "AgentCompleted",
                                "_version": 1,
                                "session_id": session_id,
                                "route": BOUNDED_STORY_GENERATION_STRATEGY,
                                "status": "completed",
                                "duration_ms": int(
                                    (time.perf_counter() - bounded_started) * 1000
                                ),
                                "message": (
                                    STORY_OVER_BUDGET_KEEP_MESSAGE
                                    if not tier_mode
                                    and bool(validation_packet.get("overBudget"))
                                    else reply
                                ),
                                "finalWordCount": final_word_count,
                                "wordCountScope": str(
                                    validation_packet.get("wordCountScope") or ""
                                ),
                                "actualWordCount": int(
                                    validation_packet.get("actualWordCount") or 0
                                ),
                                "generatedWordCount": int(
                                    validation_packet.get("generatedWordCount") or 0
                                ),
                                "retainedWordCount": int(
                                    validation_packet.get("retainedWordCount") or 0
                                ),
                                "resultingWordCount": int(
                                    validation_packet.get("resultingWordCount") or 0
                                ),
                                "chapterLengthTier": str(
                                    validation_packet.get("chapterLengthTier") or ""
                                ),
                                "tierHit": validation_packet.get("tierHit"),
                                "tierDeviation": str(
                                    validation_packet.get("tierDeviation") or ""
                                ),
                                "preciseWordCountEnabled": bool(
                                    validation_packet.get("preciseWordCountEnabled")
                                ),
                                "lengthControlStrategy": str(
                                    validation_packet.get("lengthControlStrategy") or ""
                                ),
                                "canonicalWordCount": int(
                                    validation_packet.get("canonicalWordCount") or 0
                                ),
                                "normalBandPassed": validation_packet.get(
                                    "normalBandPassed"
                                ),
                                "precisionAchieved": validation_packet.get(
                                    "precisionAchieved"
                                ),
                                "selectedEditIds": [
                                    str(item)
                                    for item in list(
                                        validation_packet.get("selectedEditIds") or []
                                    )
                                ],
                                "lengthFallbackReason": str(
                                    validation_packet.get("lengthFallbackReason") or ""
                                ),
                                "generatedOverheadRatio": validation_packet.get(
                                    "generatedOverheadRatio"
                                ),
                                **(
                                    {
                                        "overBudget": True,
                                        "message": STORY_OVER_BUDGET_KEEP_MESSAGE,
                                    }
                                    if not tier_mode
                                    and bool(validation_packet.get("overBudget"))
                                    else {}
                                ),
                            },
                        )
                    else:
                        error_message = _bounded_story_failure_message(bounded_outcome)
                        followup_mailbox_service.pause(
                            workspace_root=workspace_root,
                            session_id=session_id,
                            reason="story_generation_validation_failed",
                        )
                        error_packet = {
                            "_type": "AgentError",
                            "_version": 1,
                            "error_type": "BoundedStoryGenerationFailed",
                            "message": error_message,
                            "details": {
                                "runtime": BOUNDED_STORY_GENERATION_STRATEGY,
                                "error": dict(bounded_outcome.get("error") or {}),
                                "validation": validation_packet,
                                # Staged candidates survive a failed write so the
                                # chapter can be recovered without a new call.
                                "stagedCandidates": dict(
                                    bounded_result.get("stagedCandidates") or {}
                                ),
                            },
                        }
                        terminal_event = ("AgentError", error_packet)
                        completed = False
                should_run_coomi = False

            if should_run_coomi and not execution_handle.is_cancelled:
                try:
                    get_storydex_coomi_agent_service().validate_session_for_execution(
                        session_id=session_id,
                        workspace_root=workspace_root,
                    )
                except StorydexCoomiSessionRestoreError as exc:
                    error_message = _exception_message(exc)
                    followup_mailbox_service.pause(
                        workspace_root=workspace_root,
                        session_id=session_id,
                        reason="execution_error",
                    )
                    terminal_event = (
                        "AgentError",
                        {
                            "_type": "AgentError",
                            "_version": 1,
                            "error_type": type(exc).__name__,
                            "message": error_message,
                            "details": {
                                "runtime": "storydex-coomi-rs",
                                "stage": "session_restore",
                                "traceId": trace_id,
                                "sessionId": session_id,
                            },
                        },
                    )
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
                model_attempt_reply_baseline = len("".join(reply_chunks))
                segment_prompt = prompt
                segment_index = 0
                story_correction_attempts = 0
                story_generation_provider = ""
                story_generation_model = ""
                while not execution_handle.is_cancelled and not cancellation_token.is_cancelled():
                    segment_id = f"{trace_id}-segment-{segment_index + 1}"
                    segment_event_start = len(events)
                    pending_steer: Dict[str, Any] | None = None
                    segment_completed = False
                    segment_visible_text_characters = 0
                    segment_tool_result_seen = False
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
                            if event_name == "AgentStarted":
                                story_generation_provider = str(
                                    packet.get("llmProvider") or packet.get("llm_provider") or ""
                                )
                                story_generation_model = str(
                                    packet.get("llmModel") or packet.get("llm_model") or ""
                                )
                            if event_name == "TextChunk":
                                packet["content"] = _strip_visible_tool_text(str(packet.get("content") or ""))
                                if not packet["content"]:
                                    continue
                            if (
                                event_name == "TurnPhase"
                                and str(packet.get("phase") or "") == "model"
                            ):
                                model_attempt_reply_baseline = len("".join(reply_chunks))
                            if event_name == "ConnectionRetry":
                                reset_characters = max(
                                    0,
                                    len("".join(reply_chunks))
                                    - model_attempt_reply_baseline,
                                )
                                packet["resetTextCharacters"] = reset_characters
                                _rollback_reply_chunks(reply_chunks, reset_characters)
                                _rollback_trace_text_events(events, reset_characters)
                            if event_name == "TextChunk":
                                visible_content = str(packet.get("content") or "")
                                reply_chunks.append(visible_content)
                                segment_visible_text_characters += len(visible_content)
                            elif event_name == "ToolDone":
                                segment_tool_result_seen = True
                            elif event_name == "AgentCompleted":
                                if (
                                    segment_visible_text_characters == 0
                                    and not segment_tool_result_seen
                                ):
                                    error_message = (
                                        "Model completed without visible text or tool results; "
                                        "the provider likely exhausted its output-token budget during reasoning."
                                    )
                                    segment_completed = False
                                    terminal_event = None
                                    followup_mailbox_service.pause(
                                        workspace_root=workspace_root,
                                        session_id=session_id,
                                        reason="empty_model_result",
                                    )
                                    error_packet = {
                                        "_type": "AgentError",
                                        "_version": 1,
                                        "error_type": "EmptyModelResult",
                                        "message": error_message,
                                        "details": {
                                            "runtime": "storydex-coomi-rs",
                                            "traceId": trace_id,
                                            "sessionId": session_id,
                                        },
                                    }
                                    terminal_event = ("AgentError", error_packet)
                                    continue
                                segment_completed = True
                                terminal_event = (event_name, packet)
                                continue
                            elif event_name == "AgentCancelled":
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
                                terminal_event = (event_name, packet)
                                continue
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
                            write_tool_applied = _has_successful_story_generation_write(
                                events,
                                start_index=segment_event_start,
                            )
                            correction_applied = story_correction_attempts > 0 and write_tool_applied
                            validation_passed = bool(
                                write_tool_applied
                                and validation_packet.get("passed")
                                and not validation_packet.get("belowBudget")
                            )
                            validation_message = str(validation_packet.get("message") or "")
                            if not write_tool_applied:
                                validation_message = (
                                    "本轮没有成功调用 StorydexApplyStoryIncrement，"
                                    "不能把磁盘上的既有正文误判为本轮生成结果。"
                                )
                            elif validation_passed and bool(validation_packet.get("overBudget")):
                                validation_message = STORY_OVER_BUDGET_KEEP_MESSAGE
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
                                "correctionApplied": correction_applied,
                            }
                            if (
                                validation_passed
                                and bool(validation_packet.get("overBudget"))
                                and terminal_event is not None
                                and terminal_event[0] == "AgentCompleted"
                            ):
                                terminal_event = (
                                    terminal_event[0],
                                    {
                                        **terminal_event[1],
                                        "overBudget": True,
                                        "message": STORY_OVER_BUDGET_KEEP_MESSAGE,
                                    },
                                )
                            events.append(
                                _event_to_trace_event(
                                    "StoryGenerationValidation",
                                    validation_packet,
                                    len(events) + 1,
                                )
                            )
                            yield _encode_sse("StoryGenerationValidation", validation_packet)
                            # 段落形态样本与字数是否达标无关；只在 passed 时采样会让
                            # 一个偏掉的密度档永远无法自我纠正。
                            story_length_calibration_service.record_paragraph_generation_result(
                                workspace_root,
                                turn_contract=turn_contract,
                                validation=validation_packet,
                                provider=story_generation_provider,
                                model=story_generation_model,
                            )
                            if validation_passed:
                                story_length_calibration_service.record_generation_result(
                                    workspace_root,
                                    turn_contract=turn_contract,
                                    validation=validation_packet,
                                    provider=story_generation_provider,
                                    model=story_generation_model,
                                )
                            if not bool(validation_packet.get("passed")):
                                if (
                                    _supports_correction_continuation(turn_contract)
                                    and story_correction_attempts < _STORY_GENERATION_MAX_CORRECTIONS
                                    and _story_generation_needs_length_correction(validation_packet)
                                ):
                                    story_correction_attempts += 1
                                    turn_contract = _rebuild_story_generation_contract_for_correction(
                                        workspace_root,
                                        turn_contract,
                                        validation_packet,
                                    )
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

                                if story_correction_attempts:
                                    error_message = (
                                        "正文完成一次定向补写后仍未形成可验收的本轮写入，"
                                        "Storydex 已停止自动修订。"
                                    )
                                elif not _supports_correction_continuation(turn_contract):
                                    # 本轮合同属于有界正文路径，长度修订只发生在写入之前；
                                    # 说明具体原因，避免让作者以为还有一次补写没被用掉。
                                    error_message = (
                                        "正文未通过 Storydex 客观校验；本轮不再追加补写正文，"
                                        "长度修订只在写入前进行。"
                                    )
                                else:
                                    error_message = (
                                        "正文未通过 Storydex 客观校验；仅章级偏短结果可触发一次自动补写。"
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
                                terminal_event = ("AgentError", error_packet)
                                break
                    completed = segment_completed
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
            error_message = _exception_message(exc)
            _LOGGER.exception(
                "Coomi execution failed trace=%s session=%s",
                trace_id,
                session_id,
            )
            packet = {
                "_type": "AgentError",
                "_version": 1,
                "error_type": type(exc).__name__,
                "message": error_message,
                "details": {
                    "runtime": "coomi",
                    "traceId": trace_id,
                    "sessionId": session_id,
                },
            }
            terminal_event = ("AgentError", packet)
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

        requires_projection = await asyncio.to_thread(
            _turn_requires_knowledge_projection,
            git_snapshot,
            events,
        )
        if requires_projection:
            knowledge_projection = await asyncio.to_thread(
                _reconcile_story_knowledge_projection,
                workspace_root,
            )
            projection_event_name = str(
                knowledge_projection.get("_type")
                or ("KnowledgeProjectionUpdated" if knowledge_projection.get("ok") else "KnowledgeProjectionError")
            )
            events.append(
                _event_to_trace_event(
                    projection_event_name,
                    knowledge_projection,
                    len(events) + 1,
                )
            )
            yield _encode_sse(projection_event_name, knowledge_projection)
            if not bool(knowledge_projection.get("ok")):
                projection_error = str(
                    knowledge_projection.get("errorMessage")
                    or "Knowledge projection reconciliation failed."
                )
                if not error_message:
                    error_message = projection_error
                completed = False
        else:
            knowledge_projection = _skipped_knowledge_projection()

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
                event_name, packet = terminal_event or (
                    "AgentError",
                    {
                        "_type": "AgentError",
                        "_version": 1,
                        "error_type": "ExecutionFailed",
                        "message": terminal_error or "Coomi execution failed.",
                        "details": {"runtime": "coomi"},
                    },
                )
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
                record["knowledgeProjection"] = copy.deepcopy(knowledge_projection)
                changed_paths = knowledge_projection.get("changedSourcePaths")
                partial_failed = bool(
                    status == "failed"
                    and knowledge_projection.get("ok")
                    and isinstance(changed_paths, list)
                    and changed_paths
                )
                record["partialFailed"] = partial_failed
                record["executionOutcome"] = "partial_failed" if partial_failed else status
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
            cancelled=(
                execution_handle.is_cancelled or cancellation_token.is_cancelled()
            )
            and not completed_after_commit_cancellation,
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
        error_message = _exception_message(exc)
        _LOGGER.exception(
            "Coomi finalization failed trace=%s session=%s",
            trace_id,
            session_id,
        )
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
            "message": error_message,
            "details": {
                "runtime": "execution_coordinator",
                "traceId": trace_id,
                "sessionId": session_id,
            },
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
    timeout_ms: int = 0,
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
            error_message = _exception_message(exc)
            _LOGGER.exception(
                "Coomi worker crashed trace=%s session=%s",
                trace_id,
                session_id,
            )
            await queue.put(
                _encode_sse(
                    "AgentError",
                    {
                        "_type": "AgentError",
                        "_version": 1,
                        "error_type": type(exc).__name__,
                        "message": error_message,
                        "details": {
                            "runtime": "execution_worker",
                            "traceId": trace_id,
                            "sessionId": session_id,
                        },
                    },
                )
            )
            await queue.put(_encode_sse("done", {"type": "done"}))
        finally:
            queue.put_nowait(None)

    worker = _retain_background_execution_task(
        asyncio.create_task(pump(), name=f"storydex-execution-{trace_id}")
    )
    deadline = (
        asyncio.get_running_loop().time() + (max(0, int(timeout_ms)) / 1000.0)
        if int(timeout_ms or 0) > 0
        else None
    )

    def cancel_for_timeout() -> None:
        nonlocal deadline
        if deadline is None:
            return
        deadline = None
        handle.cancel("timeout")
        followup_mailbox_service.pause(
            workspace_root=workspace_root,
            session_id=session_id,
            reason="timeout",
        )

    try:
        while True:
            wait_seconds = _PHASE_HEARTBEAT_SECONDS
            if deadline is not None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    cancel_for_timeout()
                    continue
                wait_seconds = min(wait_seconds, remaining)
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                    cancel_for_timeout()
                    continue
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
        delete_usage=True,
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
def agent_coomi_status(
    request: Request,
    session_id_query: Optional[str] = Query(default=None, alias="sessionId"),
) -> ApiEnvelope:
    del request
    started = time.perf_counter()
    trace_id = str(uuid4())
    coomi_service = get_storydex_coomi_agent_service()
    if str(session_id_query or "").strip():
        status = coomi_service.get_status(
            workspace_root=project_service.workspace_root,
            session_id=str(session_id_query).strip(),
        )
    else:
        status = coomi_service.get_status(workspace_root=project_service.workspace_root)
    data = AgentCoomiStatusData(**status)
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=ApiTrace(traceId=trace_id, durationMs=int((time.perf_counter() - started) * 1000), toolCalls=0),
        audit=[{"action": "read_coomi_status", "toolCount": data.tool_count}],
    )


@router.post("/agent/coomi/plan-mode", response_model=ApiEnvelope)
def agent_set_coomi_plan_mode(
    payload: AgentPlanModeRequest, request: Request
) -> ApiEnvelope:
    del request
    started = time.perf_counter()
    trace_id = str(uuid4())
    result = get_storydex_coomi_agent_service().set_plan_mode(
        session_id=payload.session_id,
        workspace_root=project_service.workspace_root,
        active=payload.active,
    )
    return success_response(
        data=result,
        trace=ApiTrace(
            traceId=trace_id,
            durationMs=int((time.perf_counter() - started) * 1000),
            toolCalls=0,
        ),
        audit=[
            {
                "action": "set_coomi_plan_mode",
                "sessionId": result.get("sessionId"),
                "planMode": result.get("planMode"),
            }
        ],
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
            provider_type=payload.provider_type,
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

        routing_flags = FeatureFlags(workspace_root, FEATURE_FLAG_DEFAULTS)
        routing_mode = normalize_routing_mode(
            routing_flags.get_str(ROUTING_MODE_FLAG, fallback="legacy")
        )
        route_hints = build_route_hints(
            prompt=payload.prompt,
            active_file=payload.active_file,
            routing_mode=routing_mode,
        )
        resolved_candidates = likely_candidate_paths(workspace_root, route_hints)
        if resolved_candidates:
            route_hints["candidatePaths"] = resolved_candidates

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
                    routing_mode=routing_mode,
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
        routing_metadata = {
            "_type": "IntentRoutingTrace",
            "_version": 1,
            "mode": routing_mode,
            "classifierMethod": str(intent_frame.get("method") or "unknown"),
            "intentModelInvoked": bool(intent_frame.get("intentModelInvoked")),
            "routeHintsSource": str(route_hints.get("source") or "deterministic"),
        }
        yield _encode_sse(
            "TurnPhase",
            _turn_phase_packet(
                trace_id=trace_id,
                session_id=session_id,
                phase="intent_classification",
                label="执行意图识别完成",
                status="success",
                phase_started=intent_started,
                detail=_intent_phase_detail(intent_frame),
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
                _build_turn_contract_with_active_model,
                workspace_root,
                prompt=payload.prompt,
                active_file=payload.active_file,
                story_generation=story_generation,
                intent_frame=intent_frame,
                route_hints=route_hints,
                routing_metadata=routing_metadata,
                context_policy=context_policy_override,
                trace_id=trace_id,
                session_id=session_id,
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
        turn_contract = {
            **(await contract_task),
            "reasoningEffort": payload.reasoning_effort,
        }
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
            timeout_ms=payload.timeout_ms,
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

    workspace_root = _resolve_agent_workspace_root(payload)
    original_range_prompt = payload.prompt
    try:
        requested_chapter_range = parse_chapter_range(payload.prompt)
    except ValueError as error:
        _release_agent_generation_slot()
        yield _encode_sse(
            "AgentError",
            {
                "_type": "AgentError",
                "_version": 1,
                "error_type": "ChapterRangeTooLarge",
                "message": f"章节范围过大：{error}",
                "details": {"maximumChapters": 20},
                "traceId": trace_id,
            },
        )
        yield _encode_sse("done", {"type": "done"})
        return

    existing_chapters = {
        state.chapter_number
        for state in story_project_service.list_chapter_states(workspace_root)
    } if requested_chapter_range else set()
    pending_range_targets = [
        number for number in requested_chapter_range if number not in existing_chapters
    ]
    if requested_chapter_range and not pending_range_targets:
        _release_agent_generation_slot()
        yield _encode_sse(
            "ChapterRangeCompleted",
            {
                "_type": "ChapterRangeCompleted",
                "_version": 1,
                "traceId": trace_id,
                "chapters": list(requested_chapter_range),
                "skippedExisting": list(requested_chapter_range),
            },
        )
        yield _encode_sse("done", {"type": "done"})
        return

    def range_prompt(chapter_number: int) -> str:
        return (
            f"请只完成第{chapter_number}章，并确认正文已写入项目后结束本轮。"
            f"这是多章节顺序任务的一部分；不要生成其他章节。\n\n原始要求：{original_range_prompt}"
        )

    current_range_target = pending_range_targets.pop(0) if pending_range_targets else 0
    current_payload = (
        payload.model_copy(update={"prompt": range_prompt(current_range_target)})
        if current_range_target
        else payload
    )
    current_trace_id = trace_id
    current_token = cancellation_token
    source_message: Dict[str, Any] | None = initial_source_message
    current_replacement = replacement
    final_done = _encode_sse("done", {"type": "done"})

    while True:
        saw_error = False
        saw_cancel = False
        cancel_reason = ""
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
                cancel_reason = str(packet.get("reason") or "").strip()
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

        if saw_cancel and cancel_reason == "steer":
            followup_mailbox_service.requeue_steering(
                workspace_root=workspace_root,
                session_id=session_id,
                trace_id=current_trace_id,
            )
            followup_mailbox_service.pause(
                workspace_root=workspace_root,
                session_id=session_id,
                reason="steer_requires_resume",
            )

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

        if current_range_target:
            persisted_chapters = {
                chapter.chapter_number
                for chapter in story_project_service.list_chapter_states(workspace_root)
            }
            if current_range_target not in persisted_chapters:
                followup_mailbox_service.pause(
                    workspace_root=workspace_root,
                    session_id=session_id,
                    reason="chapter_range_not_persisted",
                )
                yield _encode_sse(
                    "AgentError",
                    {
                        "_type": "AgentError",
                        "_version": 1,
                        "error_type": "ChapterRangeProgressNotPersisted",
                        "message": f"第{current_range_target}章未在项目中落盘，已暂停后续章节。",
                        "details": {
                            "targetChapterNumber": current_range_target,
                            "remainingChapters": list(pending_range_targets),
                        },
                        "traceId": current_trace_id,
                    },
                )
                break
            if pending_range_targets:
                if not _try_acquire_agent_generation_slot():
                    followup_mailbox_service.pause(
                        workspace_root=workspace_root,
                        session_id=session_id,
                        reason="chapter_range_continuation_busy",
                    )
                    yield _encode_sse(
                        "AgentError",
                        {
                            "_type": "AgentError",
                            "_version": 1,
                            "error_type": "ChapterRangeContinuationBusy",
                            "message": "章节范围任务被另一项执行占用，请重新发送原范围；已写入章节会自动跳过。",
                            "details": {"remainingChapters": list(pending_range_targets)},
                            "traceId": current_trace_id,
                        },
                    )
                    break
                next_trace_id = str(uuid4())
                next_target = pending_range_targets.pop(0)
                yield _encode_sse(
                    "ContinuationStarted",
                    {
                        "_type": "ContinuationStarted",
                        "_version": 1,
                        "traceId": next_trace_id,
                        "previousTraceId": current_trace_id,
                        "continuationMode": "chapter_range",
                        "targetChapterNumber": next_target,
                        "remainingChapters": list(pending_range_targets),
                    },
                )
                current_payload = AgentChatRequest(
                    prompt=range_prompt(next_target),
                    activeFile=current_payload.active_file,
                    workspaceRoot=workspace_root.as_posix(),
                    reasoningEffort=current_payload.reasoning_effort,
                    storyGeneration=dict(current_payload.story_generation),
                )
                current_trace_id = next_trace_id
                current_token = _CancellationToken()
                source_message = None
                current_replacement = None
                current_range_target = next_target
                continue
            yield _encode_sse(
                "ChapterRangeCompleted",
                {
                    "_type": "ChapterRangeCompleted",
                    "_version": 1,
                    "traceId": current_trace_id,
                    "chapters": list(requested_chapter_range),
                    "skippedExisting": sorted(existing_chapters.intersection(requested_chapter_range)),
                },
            )
            current_range_target = 0

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
            reasoningEffort=current_payload.reasoning_effort,
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
