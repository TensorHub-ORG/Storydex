from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ApiError(BaseModel):
    code: str
    message: str
    details: Optional[Dict[str, Any]] = None


class ApiTrace(BaseModel):
    trace_id: str = Field(alias="traceId")
    duration_ms: int = Field(alias="durationMs", default=0)
    tool_calls: int = Field(alias="toolCalls", default=0)
    llm_calls: int = Field(alias="llmCalls", default=0)
    prompt_tokens: int = Field(alias="promptTokens", default=0)
    completion_tokens: int = Field(alias="completionTokens", default=0)
    estimated_cost: float = Field(alias="estimatedCost", default=0.0)
    cache_read_input_tokens: int = Field(alias="cacheReadInputTokens", default=0)
    cache_creation_input_tokens: int = Field(alias="cacheCreationInputTokens", default=0)
    cache_hit_ratio: float = Field(alias="cacheHitRatio", default=0.0)
    cache_savings: float = Field(alias="cacheSavings", default=0.0)

    model_config = ConfigDict(populate_by_name=True)


class ApiEnvelope(BaseModel):
    ok: bool
    data: Optional[Any] = None
    error: Optional[ApiError] = None
    trace: Optional[ApiTrace] = None
    audit: List[Dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


def success_response(
    *,
    data: Any,
    trace: Optional[ApiTrace] = None,
    audit: Optional[List[Dict[str, Any]]] = None,
) -> ApiEnvelope:
    return ApiEnvelope(ok=True, data=data, trace=trace, audit=audit or [])


def error_response(
    *,
    code: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    trace: Optional[ApiTrace] = None,
    audit: Optional[List[Dict[str, Any]]] = None,
) -> ApiEnvelope:
    return ApiEnvelope(
        ok=False,
        data=None,
        error=ApiError(code=code, message=message, details=details),
        trace=trace,
        audit=audit or [],
    )
