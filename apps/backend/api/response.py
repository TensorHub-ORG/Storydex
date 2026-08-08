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
    bridge_start_ms: float = Field(alias="bridgeStartMs", default=0.0)
    component_init_ms: float = Field(alias="componentInitMs", default=0.0)
    provider_config_ms: float = Field(alias="providerConfigMs", default=0.0)
    session_init_ms: float = Field(alias="sessionInitMs", default=0.0)
    project_instructions_ms: float = Field(alias="projectInstructionsMs", default=0.0)
    memory_init_ms: float = Field(alias="memoryInitMs", default=0.0)
    security_init_ms: float = Field(alias="securityInitMs", default=0.0)
    mcp_init_ms: float = Field(alias="mcpInitMs", default=0.0)
    hooks_init_ms: float = Field(alias="hooksInitMs", default=0.0)
    tools_init_ms: float = Field(alias="toolsInitMs", default=0.0)
    provider_init_ms: float = Field(alias="providerInitMs", default=0.0)
    model_rounds: int = Field(alias="modelRounds", default=0)
    duplicate_tool_calls_same_revision: int = Field(
        alias="duplicateToolCallsSameRevision",
        default=0,
    )
    logical_input_tokens: int = Field(alias="logicalInputTokens", default=0)
    transmitted_input_tokens: int = Field(alias="transmittedInputTokens", default=0)
    cached_input_tokens: int = Field(alias="cachedInputTokens", default=0)
    lifecycle: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True, protected_namespaces=())


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
