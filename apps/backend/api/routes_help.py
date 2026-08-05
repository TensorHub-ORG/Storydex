from __future__ import annotations

from time import perf_counter
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.response import ApiEnvelope, ApiTrace, success_response
from services.help_guide_service import get_help_guide_service
from services.prompt_repository_service import get_prompt_repository_service

router = APIRouter(tags=["help"])


class CustomPromptCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    prompt_text: str = Field(alias="promptText", min_length=1, max_length=12000)


class CustomPromptUpdateRequest(BaseModel):
    prompt_text: str = Field(alias="promptText", min_length=1, max_length=12000)


def _trace(started: float, trace_id: str) -> ApiTrace:
    return ApiTrace(traceId=trace_id, durationMs=int((perf_counter() - started) * 1000))


@router.get("/help/guide", response_model=ApiEnvelope)
def read_help_guide() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = get_help_guide_service().read_guide()
    return success_response(
        data=data,
        trace=_trace(started, trace_id),
        audit=[{"action": "read_help_guide", "count": len(data.get("items") or [])}],
    )


@router.get("/help/guide/search", response_model=ApiEnvelope)
def search_help_guide(
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=6, ge=1, le=20),
) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = get_help_guide_service().search(q, max_results=limit)
    return success_response(
        data=data,
        trace=_trace(started, trace_id),
        audit=[{"action": "search_help_guide", "query": q, "count": len(data.get("items") or [])}],
    )


@router.get("/help/prompts", response_model=ApiEnvelope)
def read_prompt_repository(
    q: str = Query(default="", max_length=200),
    category: str = Query(default="", max_length=80),
) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = get_prompt_repository_service().read_repository(query=q, category=category)
    return success_response(
        data=data,
        trace=_trace(started, trace_id),
        audit=[
            {
                "action": "read_prompt_repository",
                "query": q,
                "category": category,
                "count": len(data.get("items") or []),
            }
        ],
    )


@router.post("/help/prompts/custom", response_model=ApiEnvelope)
def create_custom_prompt(payload: CustomPromptCreateRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    try:
        item = get_prompt_repository_service().create_custom_prompt(
            title=payload.title,
            prompt_text=payload.prompt_text,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return success_response(
        data={"item": item},
        trace=_trace(started, trace_id),
        audit=[{"action": "create_custom_prompt", "promptId": item.get("id")}],
    )


@router.put("/help/prompts/custom/{prompt_id}", response_model=ApiEnvelope)
def update_custom_prompt(prompt_id: str, payload: CustomPromptUpdateRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    try:
        item = get_prompt_repository_service().update_custom_prompt(
            prompt_id,
            prompt_text=payload.prompt_text,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return success_response(
        data={"item": item},
        trace=_trace(started, trace_id),
        audit=[{"action": "update_custom_prompt", "promptId": item.get("id")}],
    )
