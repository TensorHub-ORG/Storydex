from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Query

from api.response import ApiEnvelope, ApiTrace, success_response
from core.exceptions import StorydexError
from services.coomi_agent_service import get_storydex_coomi_agent_service
from services.execution_coordinator import get_execution_coordinator
from services.project_service import get_project_service
from services.story_wiki_service import get_story_wiki_service

router = APIRouter(tags=["story-wiki"])
project_service = get_project_service()
story_wiki_service = get_story_wiki_service()
execution_coordinator = get_execution_coordinator()
logger = logging.getLogger(__name__)

_WIKI_AGENT_JOB_LOCK = Lock()
_WIKI_AGENT_JOBS: Dict[str, Dict[str, Any]] = {}
_WIKI_AGENT_ACTIVE_BY_WORKSPACE: Dict[str, str] = {}
_WIKI_AGENT_TASKS: set[asyncio.Task[Any]] = set()


def _build_trace(*, started: float, trace_id: str) -> ApiTrace:
    return ApiTrace(
        traceId=trace_id,
        durationMs=int((perf_counter() - started) * 1000),
        toolCalls=1,
    )


@router.get("/story/wiki", response_model=ApiEnvelope)
def read_story_wiki() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = story_wiki_service.read_or_build(project_service.workspace_root)
    return success_response(
        data=data,
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=[{"action": "read_story_wiki", "ok": True}],
    )


@router.get("/story/wiki/graph", response_model=ApiEnvelope)
def query_story_wiki_graph(
    q: str = Query(default=""),
    category: str = Query(default=""),
    entry_id: str = Query(default="", alias="entryId"),
    node_id: str = Query(default="", alias="nodeId"),
    depth: int = Query(default=1, ge=1, le=2),
    limit: int = Query(default=60, ge=1, le=120),
) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = story_wiki_service.query_graph(
        project_service.workspace_root,
        q=q,
        category=category,
        entry_id=entry_id,
        node_id=node_id,
        depth=depth,
        limit=limit,
    )
    return success_response(
        data=data,
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=[{"action": "query_story_wiki_graph", "ok": True, "mode": data.get("mode")}],
    )


@router.post("/story/wiki/rebuild", response_model=ApiEnvelope)
def rebuild_story_wiki() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = story_wiki_service.rebuild(project_service.workspace_root)
    return success_response(
        data=data,
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=[{"action": "rebuild_story_wiki", "ok": True}],
    )


@router.post("/story/wiki/sync", response_model=ApiEnvelope)
def sync_story_wiki() -> ApiEnvelope:
    """本地确定性增量同步：保存/写作后自动跟进文件变更，不触发 Agent，保证快。"""
    started = perf_counter()
    trace_id = str(uuid4())
    data = story_wiki_service.sync_local_incremental(project_service.workspace_root)
    return success_response(
        data=data,
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=[{"action": "sync_story_wiki", "ok": True}],
    )


@router.post("/story/wiki/agent/generate", response_model=ApiEnvelope)
async def agent_generate_story_wiki() -> ApiEnvelope:
    return await _submit_agent_wiki_workflow("generate_wiki")


@router.post("/story/wiki/agent/update", response_model=ApiEnvelope)
async def agent_update_story_wiki() -> ApiEnvelope:
    return await _submit_agent_wiki_workflow("update_wiki")


@router.post("/story/wiki/agent/review", response_model=ApiEnvelope)
async def agent_review_story_wiki() -> ApiEnvelope:
    return await _submit_agent_wiki_workflow("review_wiki")


@router.post("/story/wiki/agent/refresh-graph", response_model=ApiEnvelope)
async def agent_refresh_story_wiki_graph() -> ApiEnvelope:
    return await _submit_agent_wiki_workflow("refresh_wiki_graph")


@router.post("/story/wiki/agent/repair", response_model=ApiEnvelope)
async def agent_repair_story_wiki() -> ApiEnvelope:
    return await _submit_agent_wiki_workflow("repair_wiki")


@router.get("/story/wiki/agent/jobs/{job_id}", response_model=ApiEnvelope)
def read_agent_wiki_job(job_id: str) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    job = _get_wiki_agent_job(job_id)
    if job is None:
        raise StorydexError(
            "WIKI Agent 任务不存在或后端已重启，请重新发起。",
            code="wiki_agent_job_not_found",
            status_code=404,
            details={"jobId": str(job_id or "")},
        )
    return success_response(
        data=job,
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=[
            {
                "action": "read_story_wiki_agent_job",
                "jobId": job["jobId"],
                "workflow": job["workflow"],
                "status": job["status"],
            }
        ],
    )


async def _submit_agent_wiki_workflow(workflow: str) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    workspace_root = project_service.workspace_root.resolve()
    workspace_key = workspace_root.as_posix()
    active_job = _active_wiki_agent_job(workspace_key)
    if active_job is not None:
        raise StorydexError(
            "当前项目已有 WIKI Agent 任务正在运行，请等待完成后再试。",
            code="wiki_agent_job_running",
            status_code=409,
            details={"jobId": active_job["jobId"], "workflow": active_job["workflow"]},
        )
    if not execution_coordinator.try_reserve():
        raise StorydexError(
            "Agent 正忙，请等待当前执行完成后再生成 WIKI。",
            code="agent_busy",
            status_code=409,
            details={"workflow": workflow, "runtime": "coomi"},
        )

    job = _create_wiki_agent_job(workspace_key, workflow)
    if job is None:
        execution_coordinator.release_reservation()
        active_job = _active_wiki_agent_job(workspace_key)
        raise StorydexError(
            "当前项目已有 WIKI Agent 任务正在运行，请等待完成后再试。",
            code="wiki_agent_job_running",
            status_code=409,
            details={
                "jobId": str((active_job or {}).get("jobId") or ""),
                "workflow": str((active_job or {}).get("workflow") or ""),
            },
        )

    try:
        task = asyncio.create_task(
            _execute_wiki_agent_job(
                job_id=job["jobId"],
                workflow=workflow,
                workspace_root=workspace_root,
            ),
            name=f"storydex-wiki-agent-{job['jobId']}",
        )
    except Exception:
        _fail_wiki_agent_job(job["jobId"], "无法启动 WIKI Agent 后台任务。")
        execution_coordinator.release_reservation()
        raise
    _WIKI_AGENT_TASKS.add(task)
    task.add_done_callback(_WIKI_AGENT_TASKS.discard)

    return success_response(
        data=_public_wiki_agent_job(job),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=[
            {
                "action": "submit_story_wiki_agent_job",
                "jobId": job["jobId"],
                "workflow": workflow,
                "status": "running",
            }
        ],
    )


async def _execute_wiki_agent_job(*, job_id: str, workflow: str, workspace_root: Path) -> None:
    try:
        data = await story_wiki_service.run_agent_workflow(
            workspace_root,
            workflow=workflow,
            agent_runner=_run_coomi_wiki_agent,
        )
        _complete_wiki_agent_job(job_id, data)
    except asyncio.CancelledError:
        _fail_wiki_agent_job(job_id, "WIKI Agent 任务因后端停止而中断。")
        raise
    except Exception as exc:
        logger.exception("WIKI Agent job %s failed", job_id)
        _fail_wiki_agent_job(job_id, str(exc) or exc.__class__.__name__)
    finally:
        execution_coordinator.release_reservation()


def _create_wiki_agent_job(workspace_key: str, workflow: str) -> Optional[Dict[str, Any]]:
    with _WIKI_AGENT_JOB_LOCK:
        active_id = _WIKI_AGENT_ACTIVE_BY_WORKSPACE.get(workspace_key)
        active = _WIKI_AGENT_JOBS.get(active_id or "")
        if active and active.get("status") == "running":
            return None
        job_id = str(uuid4())
        now = _now_iso()
        job = {
            "jobId": job_id,
            "workspaceKey": workspace_key,
            "workflow": workflow,
            "status": "running",
            "createdAt": now,
            "updatedAt": now,
            "result": None,
            "errorMessage": "",
        }
        _WIKI_AGENT_JOBS[job_id] = job
        _WIKI_AGENT_ACTIVE_BY_WORKSPACE[workspace_key] = job_id
        return dict(job)


def _active_wiki_agent_job(workspace_key: str) -> Optional[Dict[str, Any]]:
    with _WIKI_AGENT_JOB_LOCK:
        job_id = _WIKI_AGENT_ACTIVE_BY_WORKSPACE.get(workspace_key)
        job = _WIKI_AGENT_JOBS.get(job_id or "")
        if not job or job.get("status") != "running":
            return None
        return _public_wiki_agent_job(job)


def _get_wiki_agent_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _WIKI_AGENT_JOB_LOCK:
        job = _WIKI_AGENT_JOBS.get(str(job_id or "").strip())
        return _public_wiki_agent_job(job) if job else None


def _complete_wiki_agent_job(job_id: str, result: Dict[str, Any]) -> None:
    _finish_wiki_agent_job(job_id, status="completed", result=result, error_message="")


def _fail_wiki_agent_job(job_id: str, error_message: str) -> None:
    _finish_wiki_agent_job(job_id, status="failed", result=None, error_message=error_message)


def _finish_wiki_agent_job(
    job_id: str,
    *,
    status: str,
    result: Optional[Dict[str, Any]],
    error_message: str,
) -> None:
    with _WIKI_AGENT_JOB_LOCK:
        job = _WIKI_AGENT_JOBS.get(job_id)
        if not job:
            return
        job.update(
            {
                "status": status,
                "updatedAt": _now_iso(),
                "result": dict(result) if isinstance(result, dict) else None,
                "errorMessage": str(error_message or ""),
            }
        )
        workspace_key = str(job.get("workspaceKey") or "")
        if _WIKI_AGENT_ACTIVE_BY_WORKSPACE.get(workspace_key) == job_id:
            _WIKI_AGENT_ACTIVE_BY_WORKSPACE.pop(workspace_key, None)


def _public_wiki_agent_job(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "jobId": str(job.get("jobId") or ""),
        "workflow": str(job.get("workflow") or ""),
        "status": str(job.get("status") or "running"),
        "createdAt": str(job.get("createdAt") or ""),
        "updatedAt": str(job.get("updatedAt") or ""),
        "result": dict(job["result"]) if isinstance(job.get("result"), dict) else None,
        "errorMessage": str(job.get("errorMessage") or ""),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reset_wiki_agent_jobs_for_tests() -> None:
    for task in list(_WIKI_AGENT_TASKS):
        if not task.done():
            task.cancel()
    _WIKI_AGENT_TASKS.clear()
    with _WIKI_AGENT_JOB_LOCK:
        _WIKI_AGENT_JOBS.clear()
        _WIKI_AGENT_ACTIVE_BY_WORKSPACE.clear()


async def _run_coomi_wiki_agent(
    *,
    prompt: str,
    trace_id: str,
    session_id: str,
    workspace_root: Path,
) -> Dict[str, Any]:
    reply_chunks: List[str] = []
    events: List[Dict[str, Any]] = []
    completed = False
    error_message = ""
    service = get_storydex_coomi_agent_service()
    async for event_name, payload in service.stream_events(
        prompt=prompt,
        trace_id=trace_id,
        session_id=session_id,
        workspace_root=workspace_root,
        active_file="",
        cancellation_token=None,
    ):
        packet = dict(payload)
        events.append({"event": event_name, "payload": packet})
        if event_name == "TextChunk":
            reply_chunks.append(str(packet.get("content") or ""))
        elif event_name == "PermissionRequest":
            approval_id = str(packet.get("approvalId") or packet.get("approval_id") or "")
            if approval_id:
                tool_name = str(packet.get("toolName") or packet.get("tool_name") or packet.get("tool") or "").lower()
                decision = "approve" if any(key in tool_name for key in ("read", "list", "search", "grep")) else "deny"
                service.resolve_approval(approval_id, decision)
        elif event_name == "AgentCompleted":
            completed = True
        elif event_name == "AgentError":
            error_message = str(packet.get("message") or "Coomi Agent error")
    return {
        "attempted": True,
        "completed": completed and not error_message,
        "errorMessage": error_message,
        "reply": "".join(reply_chunks),
        "events": events[-80:],
        "traceId": trace_id,
    }
