from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, AsyncIterator, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from api.response import ApiEnvelope, ApiTrace, success_response
from core.exceptions import GitServiceError, StorydexError
from services.agent_git_autocommit_service import AgentGitSnapshot, get_agent_git_autocommit_service
from services.coomi_agent_service import get_storydex_coomi_agent_service
from services.git_service import get_git_service
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

_AGENT_GENERATION_LOCK = Lock()
_INTENT_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="storydex-intent")
_PHASE_HEARTBEAT_SECONDS = 0.6
_COMMIT_MESSAGE_TIMEOUT_SECONDS = 2.0


class _CancellationToken:
    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled


async def _classify_intent_without_blocking_event_loop(**kwargs: Any) -> Dict[str, Any]:
    """Run intent classification on a worker loop.

    Coomi/OpenAI provider construction performs synchronous imports and client
    initialization before its first await.  Keeping that work on the request
    loop can suppress phase heartbeats for several seconds on a cold start.
    Intent classification is read-only, so isolating it in a worker preserves
    cancellation of the response stream while keeping progress events timely.
    """

    def classify() -> Dict[str, Any]:
        return asyncio.run(storydex_intent_service.classify_intent(**kwargs))

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_INTENT_EXECUTOR, classify)


class AgentChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=12000)
    active_file: str = Field(default="", alias="activeFile")
    workspace_root: str = Field(default="", alias="workspaceRoot")
    story_generation: Dict[str, Any] = Field(default_factory=dict, alias="storyGeneration")

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


def _normalize_story_generation_options(value: Dict[str, Any] | None) -> Dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    fragment_count = _bounded_int(
        payload.get("fragmentCount", payload.get("fragment_count", payload.get("segmentCount"))),
        default=1,
        minimum=1,
        maximum=20,
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
    next_story_generation["chapterTemplateId"] = selected_template
    next_story_generation["chapterTemplate"] = selected_template
    return next_story_generation


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _try_acquire_agent_generation_slot() -> bool:
    return _AGENT_GENERATION_LOCK.acquire(blocking=False)


def _release_agent_generation_slot() -> None:
    try:
        _AGENT_GENERATION_LOCK.release()
    except RuntimeError:
        pass


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
    if event_name in {"TextChunk", "ReasoningChunk"}:
        return "model"
    if event_name in {"GitAutoCommit", "GitCommitPrompt", "GitCommitResult"}:
        return "version_control"
    if event_name.startswith("Task"):
        return "planning"
    if event_name == "TurnContract":
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
    if event_name == "RunAccepted":
        return "running"
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


def _extract_trace_metrics(events: List[Dict[str, Any]], trace_id: str, duration_ms: int) -> Dict[str, Any]:
    tool_calls = len([item for item in events if item.get("event") == "ToolDone"])
    total_tokens = 0
    for item in reversed(events):
        if item.get("event") != "AgentCompleted":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        total_tokens = int(data.get("total_tokens") or data.get("totalTokens") or 0)
        break
    return {
        "traceId": trace_id,
        "durationMs": duration_ms,
        "toolCalls": tool_calls,
        "llmCalls": 1,
        "promptTokens": 0,
        "completionTokens": total_tokens,
        "estimatedCost": 0.0,
    }


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
    return audit


def _build_chat_payload(
    *,
    trace_id: str,
    prompt: str,
    reply: str,
    events: List[Dict[str, Any]],
    started: float,
    workspace_root: Path,
    status: str = "completed",
    error_message: str = "",
) -> Dict[str, Any]:
    status_data = get_storydex_coomi_agent_service().get_status(workspace_root=workspace_root)
    duration_ms = int((time.perf_counter() - started) * 1000)
    trace = _extract_trace_metrics(events, trace_id, duration_ms)
    audit = _build_audit(events)
    data = AgentChatData(
        route="coomi",
        reply=reply,
        llmModel=str(status_data.get("model") or ""),
        llmProvider=str(status_data.get("providerId") or ""),
        events=[AgentTraceEvent(**event) for event in events],
        assistant={"runtime": "coomi", "status": status_data},
    ).model_dump(by_alias=True)
    return {
        "data": data,
        "trace": trace,
        "audit": audit,
        "record": _build_history_record(
            trace_id=trace_id,
            prompt=prompt,
            data=data,
            trace=trace,
            audit=audit,
            events=events,
            workspace_root=workspace_root,
            status=status,
            error_message=error_message,
        ),
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
        "workspaceRoot": workspace_root.as_posix(),
        "errorMessage": error_message,
        "errorCode": "coomi_agent_error" if error_message else None,
    }


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
    planner = getattr(get_storydex_coomi_agent_service(), "create_task_plan", None)
    if callable(planner):
        try:
            tasks = await planner(
                prompt=prompt,
                trace_id=trace_id,
                session_id=session_id,
                workspace_root=workspace_root,
                active_file=active_file,
                story_generation=story_generation,
                turn_contract=turn_contract,
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
) -> AsyncIterator[str]:
    started = time.perf_counter()
    reply_chunks: List[str] = []
    events: List[Dict[str, Any]] = []
    completed = False
    error_message = ""
    tracker: _TaskRunTracker | None = None
    git_finished = False
    runtime_tasks_finalized = False

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
            planning_task = asyncio.create_task(
                _create_agent_task_plan(
                    prompt=prompt,
                    trace_id=trace_id,
                    session_id=session_id,
                    workspace_root=workspace_root,
                    active_file=active_file,
                    story_generation=story_generation,
                    turn_contract=turn_contract,
                )
            )
            while not planning_task.done():
                done, _ = await asyncio.wait({planning_task}, timeout=_PHASE_HEARTBEAT_SECONDS)
                if done:
                    break
                if await request.is_disconnected():
                    cancellation_token.cancel()
                    planning_task.cancel()
                    return
                yield _encode_sse(
                    "TurnPhase",
                    _turn_phase_packet(
                        trace_id=trace_id,
                        session_id=session_id,
                        phase="task_planning",
                        label="正在规划执行步骤",
                        status="running",
                        phase_started=planning_started,
                        heartbeat=True,
                    ),
                )
            task_plan = await planning_task
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

            should_run_coomi = True
            if turn_contract:
                events.append(_event_to_trace_event("TurnContract", turn_contract, len(events) + 1))
                yield _encode_sse("TurnContract", turn_contract)
                for task_event_name, task_payload in tracker.complete_current():
                    events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                    yield _encode_sse(task_event_name, task_payload)
                if _turn_contract_needs_user_input(turn_contract):
                    for task_event_name, task_payload in tracker.skip_remaining_execution(reason="needs_user_input"):
                        events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                        yield _encode_sse(task_event_name, task_payload)
                    runtime_tasks_finalized = True
                    packet = _turn_contract_waiting_packet(turn_contract)
                    events.append(_event_to_trace_event("AgentCompleted", packet, len(events) + 1))
                    completed = True
                    reply_chunks.append(str(packet.get("message") or ""))
                    yield _encode_sse("AgentCompleted", packet)
                    should_run_coomi = False

            if should_run_coomi:
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
                runtime_events = get_storydex_coomi_agent_service().stream_events(
                    prompt=prompt,
                    trace_id=trace_id,
                    session_id=session_id,
                    workspace_root=workspace_root,
                    active_file=active_file,
                    story_generation=story_generation,
                    turn_contract=turn_contract,
                    cancellation_token=cancellation_token,
                ).__aiter__()
                model_output_started = False
                while True:
                    next_event = asyncio.create_task(runtime_events.__anext__())
                    while not next_event.done():
                        done, _ = await asyncio.wait({next_event}, timeout=_PHASE_HEARTBEAT_SECONDS)
                        if done:
                            break
                        if await request.is_disconnected():
                            cancellation_token.cancel()
                            next_event.cancel()
                            return
                        yield _encode_sse(
                            "TurnPhase",
                            _turn_phase_packet(
                                trace_id=trace_id,
                                session_id=session_id,
                                phase="model_execution",
                                label="正在等待模型输出",
                                status="running",
                                phase_started=model_started,
                                heartbeat=True,
                            ),
                        )
                    try:
                        event_name, payload = await next_event
                    except StopAsyncIteration:
                        break
                    if await request.is_disconnected():
                        cancellation_token.cancel()
                        break
                    if not model_output_started and event_name not in {"AgentStarted", "UsageUpdate"}:
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
                    events.append(_event_to_trace_event(event_name, packet, len(events) + 1))
                    if event_name == "TextChunk":
                        reply_chunks.append(str(packet.get("content") or ""))
                    elif event_name == "AgentCompleted":
                        completed = True
                    elif event_name == "AgentError":
                        error_message = str(packet.get("message") or "Coomi Agent error")
                    yield _encode_sse(event_name, packet)
                    for task_event_name, task_payload in tracker.advance_after_runtime_event(event_name):
                        events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                        yield _encode_sse(task_event_name, task_payload)

            if error_message:
                for task_event_name, task_payload in tracker.fail_current(error_message):
                    events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                    yield _encode_sse(task_event_name, task_payload)
                for task_event_name, task_payload in tracker.skip_remaining_execution(reason="execution_failed"):
                    events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                    yield _encode_sse(task_event_name, task_payload)
            elif completed and not runtime_tasks_finalized:
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

        if tracker is not None:
            for task_event_name, task_payload in tracker.start_version_task():
                events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                yield _encode_sse(task_event_name, task_payload)
        git_payload = finish_git_turn()
        git_payload["traceId"] = trace_id
        git_payload["sessionId"] = session_id
        git_event_name = _git_event_name(git_payload)
        events.append(_event_to_trace_event(git_event_name, git_payload, len(events) + 1))
        yield _encode_sse(git_event_name, git_payload)
        git_failed = str(git_payload.get("status") or "") == "error"
        if tracker is not None:
            for task_event_name, task_payload in tracker.finish_version_task(
                failed=git_failed,
                message=str(git_payload.get("message") or ""),
            ):
                events.append(_event_to_trace_event(task_event_name, task_payload, len(events) + 1))
                yield _encode_sse(task_event_name, task_payload)
        if git_failed and not error_message:
            error_message = str(git_payload.get("message") or "Local Git auto commit failed.")
        status = "failed" if git_failed else "completed" if completed and not error_message else "cancelled" if cancellation_token.is_cancelled() else "failed"
        payload_data = _build_chat_payload(
            trace_id=trace_id,
            prompt=prompt,
            reply="".join(reply_chunks),
            events=events,
            started=started,
            workspace_root=workspace_root,
            status=status,
            error_message=error_message,
        )
        trace_history_service.upsert_record(payload_data["record"], session_id)
        yield _encode_sse("done", {"type": "done"})
    finally:
        if not git_finished:
            try:
                finish_git_turn()
            except Exception:
                pass
        _release_agent_generation_slot()


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
    started = time.perf_counter()
    cancellation_token = _CancellationToken()
    workspace_root = _resolve_agent_workspace_root(payload)
    git_snapshot = agent_git_autocommit_service.begin_turn(workspace_root)
    git_finished = False
    try:
        story_generation = _normalize_story_generation_options(payload.story_generation)
        intent_frame = await storydex_intent_service.classify_intent(
            prompt=payload.prompt,
            active_file=payload.active_file,
            workspace_root=workspace_root,
            session_id=session_id,
        )
        turn_contract = storydex_orchestration_service.build_turn_contract(
            workspace_root,
            prompt=payload.prompt,
            active_file=payload.active_file,
            story_generation=story_generation,
            intent_frame=intent_frame,
        )
        story_generation = _apply_turn_contract_story_generation_defaults(story_generation, turn_contract)
        task_plan = await _create_agent_task_plan(
            prompt=payload.prompt,
            trace_id=trace_id,
            session_id=session_id,
            workspace_root=workspace_root,
            active_file=payload.active_file,
            story_generation=story_generation,
            turn_contract=turn_contract,
        )
        tracker = _TaskRunTracker(task_plan, trace_id=trace_id, session_id=session_id)
        events: List[Dict[str, Any]] = [
            _event_to_trace_event("TaskPlanCreated", tracker.plan_created_payload(), 1)
        ]
        _append_task_events(events, tracker.start_next())
        if turn_contract:
            events.append(_event_to_trace_event("TurnContract", turn_contract, len(events) + 1))
            _append_task_events(events, tracker.complete_current())
            if _turn_contract_needs_user_input(turn_contract):
                _append_task_events(events, tracker.skip_remaining_execution(reason="needs_user_input"))
                packet = _turn_contract_waiting_packet(turn_contract)
                events.append(_event_to_trace_event("AgentCompleted", packet, len(events) + 1))
                reply = _turn_contract_user_input_message(turn_contract)
                completed = True
                error_message = ""
            else:
                _append_task_events(events, tracker.start_next())
                reply, coomi_events, completed, error_message = await _collect_coomi_run(
                    prompt=payload.prompt,
                    trace_id=trace_id,
                    session_id=session_id,
                    active_file=payload.active_file,
                    workspace_root=workspace_root,
                    story_generation=story_generation,
                    turn_contract=turn_contract,
                    cancellation_token=cancellation_token,
                )
                for event in coomi_events:
                    events.append({**event, "index": len(events) + 1})
                    if event.get("event") in {"ToolDone", "StageOutput"}:
                        _append_task_events(events, tracker.advance_after_runtime_event(str(event.get("event") or "")))
                if error_message:
                    _append_task_events(events, tracker.fail_current(error_message))
                    _append_task_events(events, tracker.skip_remaining_execution(reason="execution_failed"))
                elif completed:
                    _append_task_events(events, tracker.complete_through_execution())
        else:
            _append_task_events(events, tracker.complete_current())
            _append_task_events(events, tracker.start_next())
            reply, coomi_events, completed, error_message = await _collect_coomi_run(
                prompt=payload.prompt,
                trace_id=trace_id,
                session_id=session_id,
                active_file=payload.active_file,
                workspace_root=workspace_root,
                story_generation=story_generation,
                turn_contract=turn_contract,
                cancellation_token=cancellation_token,
            )
            for event in coomi_events:
                events.append({**event, "index": len(events) + 1})
                if event.get("event") in {"ToolDone", "StageOutput"}:
                    _append_task_events(events, tracker.advance_after_runtime_event(str(event.get("event") or "")))
            if error_message:
                _append_task_events(events, tracker.fail_current(error_message))
                _append_task_events(events, tracker.skip_remaining_execution(reason="execution_failed"))
            elif completed:
                _append_task_events(events, tracker.complete_through_execution())
        _append_task_events(events, tracker.start_version_task())
        git_payload = agent_git_autocommit_service.finish_turn(
            git_snapshot,
            prompt=payload.prompt,
            commit_prompt_enabled=_agent_commit_prompt_enabled(workspace_root),
        )
        git_payload["traceId"] = trace_id
        git_payload["sessionId"] = session_id
        git_finished = True
        events.append(_event_to_trace_event(_git_event_name(git_payload), git_payload, len(events) + 1))
        git_failed = str(git_payload.get("status") or "") == "error"
        _append_task_events(events, tracker.finish_version_task(failed=git_failed, message=str(git_payload.get("message") or "")))
        if git_failed and not error_message:
            error_message = str(git_payload.get("message") or "Local Git auto commit failed.")
        payload_data = _build_chat_payload(
            trace_id=trace_id,
            prompt=payload.prompt,
            reply=reply,
            events=events,
            started=started,
            workspace_root=workspace_root,
            status="failed" if git_failed else "completed" if completed and not error_message else "failed",
            error_message=error_message,
        )
        trace_history_service.upsert_record(payload_data["record"], session_id)
        return success_response(
            data=payload_data["data"],
            trace=ApiTrace(**payload_data["trace"]),
            audit=payload_data["audit"],
        )
    finally:
        if not git_finished:
            agent_git_autocommit_service.finish_turn(
                git_snapshot,
                prompt=payload.prompt,
                commit_prompt_enabled=_agent_commit_prompt_enabled(workspace_root),
            )
        _release_agent_generation_slot()


async def _stream_agent_chat_request_sse(
    *,
    payload: AgentChatRequest,
    request: Request,
    trace_id: str,
    session_id: str,
    cancellation_token: _CancellationToken,
) -> AsyncIterator[str]:
    request_started = time.perf_counter()
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
    }
    yield _encode_sse("RunAccepted", accepted)

    workspace_root: Path | None = None
    git_snapshot: AgentGitSnapshot | None = None
    delegated = False
    git_task: asyncio.Task[AgentGitSnapshot] | None = None
    intent_task: asyncio.Task[Dict[str, Any]] | None = None
    try:
        workspace_root = _resolve_agent_workspace_root(payload)
        story_generation = _normalize_story_generation_options(payload.story_generation)
        git_task = asyncio.create_task(asyncio.to_thread(agent_git_autocommit_service.begin_turn, workspace_root))

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
                cancellation_token.cancel()
                intent_task.cancel()
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
            )
        )
        while not contract_task.done():
            done, _ = await asyncio.wait({contract_task}, timeout=_PHASE_HEARTBEAT_SECONDS)
            if done:
                break
            if await request.is_disconnected():
                cancellation_token.cancel()
                contract_task.cancel()
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
                cancellation_token.cancel()
                git_task.cancel()
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
        ):
            yield chunk
    except Exception as exc:
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
        if not delegated:
            for task in (intent_task, git_task):
                if task is not None and not task.done():
                    task.cancel()
            if git_snapshot is not None and workspace_root is not None:
                try:
                    agent_git_autocommit_service.finish_turn(
                        git_snapshot,
                        prompt=payload.prompt,
                        commit_prompt_enabled=_agent_commit_prompt_enabled(workspace_root),
                    )
                except Exception:
                    pass
            _release_agent_generation_slot()


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
    cancellation_token = _CancellationToken()
    return StreamingResponse(
        _stream_agent_chat_request_sse(
            payload=payload,
            request=request,
            trace_id=trace_id,
            session_id=session_id,
            cancellation_token=cancellation_token,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
