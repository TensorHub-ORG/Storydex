from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List
from uuid import uuid4

from fastapi import APIRouter, Query

from api.response import ApiEnvelope, ApiTrace, success_response
from services.coomi_agent_service import get_storydex_coomi_agent_service
from services.project_service import get_project_service
from services.story_wiki_service import get_story_wiki_service

router = APIRouter(tags=["story-wiki"])
project_service = get_project_service()
story_wiki_service = get_story_wiki_service()


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
    return await _run_agent_wiki_workflow("generate_wiki")


@router.post("/story/wiki/agent/update", response_model=ApiEnvelope)
async def agent_update_story_wiki() -> ApiEnvelope:
    return await _run_agent_wiki_workflow("update_wiki")


@router.post("/story/wiki/agent/review", response_model=ApiEnvelope)
async def agent_review_story_wiki() -> ApiEnvelope:
    return await _run_agent_wiki_workflow("review_wiki")


@router.post("/story/wiki/agent/refresh-graph", response_model=ApiEnvelope)
async def agent_refresh_story_wiki_graph() -> ApiEnvelope:
    return await _run_agent_wiki_workflow("refresh_wiki_graph")


@router.post("/story/wiki/agent/repair", response_model=ApiEnvelope)
async def agent_repair_story_wiki() -> ApiEnvelope:
    return await _run_agent_wiki_workflow("repair_wiki")


async def _run_agent_wiki_workflow(workflow: str) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = await story_wiki_service.run_agent_workflow(
        project_service.workspace_root,
        workflow=workflow,
        agent_runner=_run_coomi_wiki_agent,
    )
    return success_response(
        data=data,
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=[
            {
                "action": "run_story_wiki_agent_workflow",
                "workflow": workflow,
                "status": data.get("status"),
                "agentAttempted": data.get("agentAttempted"),
                "fallbackUsed": data.get("fallbackUsed"),
            }
        ],
    )


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
