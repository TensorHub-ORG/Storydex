from __future__ import annotations

from time import perf_counter
from typing import Any, Dict
from uuid import uuid4

from fastapi import APIRouter, Query

from api.response import ApiEnvelope, ApiTrace, success_response
from core.exceptions import StorydexError
from services.project_service import get_project_service
from services.story_knowledge_relation_service import (
    KnowledgeRelationError,
    get_story_knowledge_relation_service,
)
from services.story_wiki_service import get_story_wiki_service

router = APIRouter(tags=["story-wiki"])
project_service = get_project_service()
story_wiki_service = get_story_wiki_service()
story_knowledge_relation_service = get_story_knowledge_relation_service()


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
    offset: int = Query(default=0, ge=0),
    include_review: bool = Query(default=False, alias="includeReview"),
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
        offset=offset,
        include_review=include_review,
    )
    return success_response(
        data=data,
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=[{"action": "query_story_wiki_graph", "ok": True, "mode": data.get("mode")}],
    )


@router.get("/story/wiki/relations/review", response_model=ApiEnvelope)
def read_relation_review_queue(
    status: str = Query(default="review_required"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = story_knowledge_relation_service.list_review_relations(
        project_service.workspace_root,
        status=status,
        offset=offset,
        limit=limit,
    )
    return success_response(
        data=data,
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=[
            {
                "action": "read_story_wiki_relation_review",
                "status": status,
                "returned": len(data.get("relations") or []),
            }
        ],
    )


@router.post("/story/wiki/relations/{candidate_id}/confirm", response_model=ApiEnvelope)
def confirm_relation_candidate(candidate_id: str, payload: Dict[str, Any]) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    try:
        data = story_knowledge_relation_service.confirm_candidate(
            project_service.workspace_root,
            candidate_id,
            expected_fingerprint=str(payload.get("expectedFingerprint") or ""),
            subject_id=str(payload.get("subjectId") or ""),
            predicate=str(payload.get("predicate") or ""),
            object_id=str(payload.get("objectId") or ""),
            target_source_path=str(payload.get("targetSourcePath") or ""),
            trace_id=trace_id,
        )
    except KnowledgeRelationError as exc:
        raise _relation_storydex_error(exc) from exc
    return success_response(
        data=data,
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=[{"action": "confirm_story_wiki_relation", "candidateId": candidate_id, "ok": True}],
    )


@router.post("/story/wiki/relations/{candidate_id}/reject", response_model=ApiEnvelope)
def reject_relation_candidate(candidate_id: str, payload: Dict[str, Any]) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    try:
        data = story_knowledge_relation_service.reject_candidate(
            project_service.workspace_root,
            candidate_id,
            expected_fingerprint=str(payload.get("expectedFingerprint") or ""),
            reason=str(payload.get("reason") or ""),
            note=str(payload.get("note") or ""),
        )
    except KnowledgeRelationError as exc:
        raise _relation_storydex_error(exc) from exc
    return success_response(
        data=data,
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=[
            {
                "action": "reject_story_wiki_relation",
                "candidateId": candidate_id,
                "reason": str(payload.get("reason") or ""),
                "ok": True,
            }
        ],
    )


def _relation_storydex_error(exc: KnowledgeRelationError) -> StorydexError:
    return StorydexError(
        str(exc),
        code=exc.code,
        status_code=exc.status_code,
        details=exc.details,
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
