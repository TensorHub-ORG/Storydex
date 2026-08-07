from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from functools import lru_cache
from typing import Any, Dict, Iterable, Sequence


_TOKEN_ENCODING = "cl100k_base"
_PARAGRAPH_BREAK_RE = re.compile(r"(?:\r?\n\s*){2,}")
_WHITESPACE_RE = re.compile(r"\s+")
MAX_CONTEXT_SOURCE_PATHS = 32


@lru_cache(maxsize=1)
def _token_encoder() -> Any:
    import tiktoken

    return tiktoken.get_encoding(_TOKEN_ENCODING)


def estimate_tokens(value: Any) -> int:
    text = str(value or "")
    if not text:
        return 0
    return len(_token_encoder().encode(text, disallowed_special=()))


def normalized_content_hash(value: Any) -> str:
    normalized = _normalize_for_hash(value)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def create_context_source(
    kind: str,
    paths: Sequence[str],
    *,
    candidate: Any = "",
    count: int | None = None,
    policy: str = "",
    elapsed_ms: float = 0.0,
    max_paths: int = MAX_CONTEXT_SOURCE_PATHS,
) -> Dict[str, Any]:
    clean_paths = [str(path).strip().replace("\\", "/") for path in paths if str(path).strip()]
    candidate_text = str(candidate or "").strip()
    path_limit = max(0, int(max_paths))
    return {
        "kind": str(kind or "unknown"),
        "count": len(clean_paths) if count is None else max(0, int(count or 0)),
        "paths": clean_paths[:path_limit],
        "candidateChars": len(candidate_text),
        "candidateEstTokens": estimate_tokens(candidate_text),
        "chars": 0,
        "estTokens": 0,
        "included": False,
        "truncated": False,
        "dropReason": "empty" if not candidate_text else "not_included",
        "messageIndex": -1,
        "startEstToken": 0,
        "endEstToken": 0,
        "elapsedMs": round(max(0.0, float(elapsed_ms or 0.0)), 3),
        "policy": str(policy or ""),
        "contentHash": "",
    }


def finalize_context_source(
    source: Dict[str, Any] | None,
    *,
    content: Any = "",
    included: bool,
    truncated: bool = False,
    drop_reason: str = "",
) -> None:
    if not isinstance(source, dict):
        return
    text = str(content or "").strip()
    source.update(
        {
            "chars": len(text),
            "estTokens": estimate_tokens(text),
            "included": bool(included),
            "truncated": bool(truncated),
            "dropReason": str(drop_reason or ""),
            "contentHash": normalized_content_hash(text),
        }
    )


def build_context_trace(
    sources: Sequence[Dict[str, Any]],
    blocks: Sequence[Dict[str, Any]],
    *,
    assemble_ms: float,
    context_policy: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    source_records = [source for source in sources if isinstance(source, dict)]
    context_chars = sum(int(source.get("chars") or 0) for source in source_records)
    context_tokens = sum(int(source.get("estTokens") or 0) for source in source_records)
    return {
        "_type": "ContextTrace",
        "_version": 5,
        "tokenEstimator": f"tiktoken:{_TOKEN_ENCODING}",
        "contextPolicy": dict(context_policy or {}),
        "usageContract": {
            "_type": "LLMUsage",
            "_version": 1,
            "reportedSources": ["provider_response", "provider_query"],
            "deprecatedAliases": {
                "providerUsageRequestCount": "providerReportedUsageRequestCount",
                "providerInputTokens": "providerReportedInputTokens",
                "providerOutputTokens": "providerReportedOutputTokens",
                "providerTotalTokens": "providerReportedTotalTokens",
            },
        },
        "sources": source_records,
        "duplicates": _detect_duplicates(blocks),
        "llmCalls": [],
        "providerRequests": [],
        "totals": {
            "contextChars": context_chars,
            "estContextTokens": context_tokens,
            "assembleMs": round(max(0.0, float(assemble_ms or 0.0)), 3),
            "contextRequestIndex": None,
            "contextRequestChars": 0,
            "contextRequestEstTokens": 0,
            "contextRequestHash": "",
            "providerRequestCount": 0,
            "providerRequestEstTokens": 0,
            "providerUsageRequestCount": 0,
            "providerUsageRequestEstTokens": 0,
            "providerInputTokens": None,
            "providerOutputTokens": None,
            "providerTotalTokens": None,
            "providerInputEstimateErrorTokens": None,
            "providerInputEstimateErrorPct": None,
            "providerReportedUsageRequestCount": 0,
            "providerReportedUsageRequestEstTokens": 0,
            "providerReportedInputTokens": None,
            "providerReportedOutputTokens": None,
            "providerReportedTotalTokens": None,
            "providerReportedCacheReadInputTokens": None,
            "providerReportedCacheCreationInputTokens": None,
            "providerReportedReasoningTokens": None,
            "providerReportedInputEstimateErrorTokens": None,
            "providerReportedInputEstimateErrorPct": None,
            "estimatedUsageRequestCount": 0,
            "missingUsageRequestCount": 0,
            "legacyUnknownUsageRequestCount": 0,
            "reportedUsageCoveragePct": None,
            "providerAttemptCount": 0,
            "providerRetryCount": 0,
            "providerFailedAttemptCount": 0,
            "contractBuildMs": 0.0,
            "directoryScanCount": 0,
            "statCount": 0,
            "readCount": 0,
            "chapterSnapshotBuildCount": 0,
            "wikiRefreshMs": 0.0,
            "ftsRefreshMs": 0.0,
            "bridgeStartMs": 0.0,
            "componentInitMs": 0.0,
            "providerConfigMs": 0.0,
            "sessionInitMs": 0.0,
            "projectInstructionsMs": 0.0,
            "memoryInitMs": 0.0,
            "securityInitMs": 0.0,
            "mcpInitMs": 0.0,
            "hooksInitMs": 0.0,
            "toolsInitMs": 0.0,
            "providerInitMs": 0.0,
            "modelRounds": 0,
            "toolCalls": 0,
            "duplicateToolCallsSameRevision": 0,
            "logicalInputTokens": 0,
            "transmittedInputTokens": 0,
            "cachedInputTokens": 0,
            "shadowLogicalInputTokens": 0,
            "shadowTransmittedInputTokens": 0,
            "shadowCachedInputTokens": 0,
            "shadowOutputReserveTokens": 0,
            "shadowRequestCount": 0,
            "shadowTokenDelta": 0,
            "logicalContextTokens": 0,
            "transmittedContextTokens": 0,
            "cachedContextTokens": 0,
            "contextOmittedBlockCount": 0,
            "contextTruncatedBlockCount": 0,
            "contextCacheHitCount": 0,
            "evidenceObservations": 0,
            "evidenceInvalidations": 0,
            "uniqueEvidenceBytes": 0,
        },
    }


def capture_coomi_memory_source(
    context_assembly: Dict[str, Any] | None,
    *,
    system_prompt: str,
    enabled: bool,
) -> None:
    """Attach Coomi persistent-memory measurements without storing its text."""

    assembly = context_assembly if isinstance(context_assembly, dict) else {}
    trace = assembly.get("contextTrace") if isinstance(assembly.get("contextTrace"), dict) else None
    if trace is None:
        return
    sources = trace.get("sources") if isinstance(trace.get("sources"), list) else []
    sources = [
        source
        for source in sources
        if not (isinstance(source, dict) and str(source.get("kind") or "") == "coomi_memory")
    ]
    content = _coomi_memory_block(system_prompt) if enabled else ""
    source = create_context_source(
        "coomi_memory",
        ["coomi://persistent-memory"] if content else [],
        candidate=content,
        count=1 if content else 0,
        policy="execution_context_policy",
    )
    finalize_context_source(
        source,
        content=content,
        included=bool(content),
        drop_reason="" if content else "empty" if enabled else "disabled_by_policy",
    )
    sources.append(source)
    trace["sources"] = sources
    totals = trace.get("totals") if isinstance(trace.get("totals"), dict) else {}
    totals.update(
        {
            "contextChars": sum(int(item.get("chars") or 0) for item in sources if isinstance(item, dict)),
            "estContextTokens": sum(
                int(item.get("estTokens") or 0) for item in sources if isinstance(item, dict)
            ),
        }
    )
    trace["totals"] = totals


def capture_provider_request(
    context_assembly: Dict[str, Any] | None,
    *,
    request_index: int,
    purpose: str,
    method: str,
    messages: Sequence[Dict[str, Any]],
    tools: Sequence[Dict[str, Any]] | None,
    kwargs: Dict[str, Any] | None,
    request_hash: str,
) -> Dict[str, Any]:
    """Measure one Provider request and locate assembled Storydex blocks once.

    Positions are estimated within each message's content. The message index keeps
    those offsets unambiguous without pretending that Provider usage can be split
    among individual blocks. The returned record is later paired with this exact
    call's Provider usage by ``llm_replay``.
    """

    normalized_request = {
        "messages": list(messages),
        "tools": list(tools or []),
        "kwargs": dict(kwargs or {}),
    }
    request_text = _stable_json(normalized_request)
    system_prompt = next(
        (
            _message_content_text(message)
            for message in messages
            if isinstance(message, dict) and str(message.get("role") or "") == "system"
        ),
        "",
    )
    request_record = {
        "index": max(0, int(request_index or 0)),
        "purpose": str(purpose or "unknown"),
        "method": str(method or "unknown"),
        "requestChars": len(request_text),
        "requestEstTokens": estimate_tokens(request_text),
        "requestHash": str(request_hash or ""),
        "systemPromptChars": len(system_prompt),
        "systemPromptEstTokens": estimate_tokens(system_prompt),
        "toolNames": _tool_names(tools or []),
        "toolsDigest": hashlib.sha256(_stable_json(list(tools or [])).encode("utf-8")).hexdigest(),
        "providerAttempts": [],
        "providerAttemptCount": 0,
        "providerRetryCount": 0,
        "usage": None,
        "usageSource": "",
        "protocol": "unknown",
        "requestId": "",
        "requestedModel": "",
        "reportedModel": "",
        "providerReportedInputTokens": None,
        "providerReportedOutputTokens": None,
        "providerReportedTotalTokens": None,
        "cacheReadInputTokens": None,
        "cacheCreationInputTokens": None,
        "reasoningTokens": None,
        "estimatedInputTokens": None,
        "estimator": "",
        "totalDerived": False,
        "usageSnapshotCount": 0,
        "providerDetails": {},
        "inputTokens": None,
        "outputTokens": None,
        "totalTokens": None,
        "estimateErrorTokens": None,
        "estimateErrorPct": None,
    }
    try:
        # Local import avoids a module cycle: context_budget_service uses the
        # token estimator defined in this module.
        from services.context_budget_service import (
            DEFAULT_OUTPUT_RESERVE_TOKENS,
            request_token_accounting,
        )

        raw_reserve = (kwargs or {}).get("outputReserveTokens")
        if raw_reserve is None:
            raw_reserve = (kwargs or {}).get("output_reserve_tokens")
        try:
            reserve = int(raw_reserve) if raw_reserve is not None else DEFAULT_OUTPUT_RESERVE_TOKENS
        except (TypeError, ValueError):
            reserve = DEFAULT_OUTPUT_RESERVE_TOKENS
        token_accounting = request_token_accounting(
            messages=messages,
            tools=tools or [],
            output_reserve_tokens=reserve,
        )
        request_record["tokenAccounting"] = token_accounting
        request_record.update(
            {
                "logicalInputTokens": int(token_accounting.get("logicalInputTokens") or 0),
                "transmittedInputTokens": int(token_accounting.get("transmittedInputTokens") or 0),
                "cachedInputTokens": int(token_accounting.get("cachedInputTokens") or 0),
                "outputReserveTokens": int(token_accounting.get("outputReserveTokens") or 0),
            }
        )
    except Exception:
        # Request capture is observability-only; preserve the Provider call if
        # an optional accounting dependency is unavailable.
        request_record["tokenAccounting"] = {
            "_type": "RequestTokenAccounting",
            "_version": 1,
            "logicalInputTokens": request_record["requestEstTokens"],
            "transmittedInputTokens": request_record["requestEstTokens"],
            "cachedInputTokens": 0,
            "outputReserveTokens": 0,
        }

    assembly = context_assembly if isinstance(context_assembly, dict) else {}
    trace = assembly.get("contextTrace") if isinstance(assembly.get("contextTrace"), dict) else None
    if trace is None:
        return request_record
    totals = trace.get("totals") if isinstance(trace.get("totals"), dict) else {}
    if str(totals.get("contextRequestHash") or ""):
        return request_record

    blocks = assembly.get("promptBlocks") if isinstance(assembly.get("promptBlocks"), list) else []
    sources = trace.get("sources") if isinstance(trace.get("sources"), list) else []
    source_by_kind = {
        str(source.get("kind") or ""): source
        for source in sources
        if isinstance(source, dict) and str(source.get("kind") or "")
    }
    message_texts = [_message_content_text(message) for message in messages]
    cursors = [0 for _ in message_texts]
    found = 0
    for raw_block in blocks:
        block = raw_block if isinstance(raw_block, dict) else {}
        block_kind = str(block.get("id") or "").strip()
        block_content = str(block.get("content") or "").strip()
        source = source_by_kind.get(block_kind)
        if source is None or not block_content:
            continue
        for message_index, message_text in enumerate(message_texts):
            char_index = message_text.find(block_content, cursors[message_index])
            if char_index < 0:
                continue
            start_token = estimate_tokens(message_text[:char_index])
            source.update(
                {
                    "messageIndex": message_index,
                    "startEstToken": start_token,
                    "endEstToken": start_token + estimate_tokens(block_content),
                }
            )
            cursors[message_index] = char_index + len(block_content)
            found += 1
            break

    memory_source = source_by_kind.get("coomi_memory")
    if isinstance(memory_source, dict) and bool(memory_source.get("included")):
        expected_chars = int(memory_source.get("chars") or 0)
        expected_hash = str(memory_source.get("contentHash") or "")
        for message_index, message_text in enumerate(message_texts):
            for marker in ("## Persistent Memories", "## Memory Index"):
                char_index = message_text.find(marker)
                if char_index < 0 or expected_chars <= 0:
                    continue
                candidate = message_text[char_index : char_index + expected_chars]
                if normalized_content_hash(candidate) != expected_hash:
                    continue
                start_token = estimate_tokens(message_text[:char_index])
                memory_source.update(
                    {
                        "messageIndex": message_index,
                        "startEstToken": start_token,
                        "endEstToken": start_token + estimate_tokens(candidate),
                    }
                )
                found += 1
                break
            if int(memory_source.get("messageIndex", -1)) >= 0:
                break

    included_count = sum(1 for source in source_by_kind.values() if bool(source.get("included")))
    if included_count and found == 0:
        return request_record

    totals.update(
        {
            "contextRequestIndex": request_record["index"],
            "contextRequestChars": request_record["requestChars"],
            "contextRequestEstTokens": request_record["requestEstTokens"],
            "contextRequestHash": request_record["requestHash"],
        }
    )
    trace["totals"] = totals
    return request_record


def merge_llm_metrics(context_trace: Dict[str, Any] | None, metrics: Dict[str, Any] | None) -> Dict[str, Any]:
    trace = context_trace if isinstance(context_trace, dict) else {}
    metric_values = metrics if isinstance(metrics, dict) else {}
    calls = metric_values.get("llmCalls") if isinstance(metric_values.get("llmCalls"), list) else []
    trace["llmCalls"] = [dict(call) for call in calls if isinstance(call, dict)]
    raw_requests = (
        metric_values.get("providerRequests")
        if isinstance(metric_values.get("providerRequests"), list)
        else []
    )
    provider_requests = [dict(request) for request in raw_requests if isinstance(request, dict)]
    trace["providerRequests"] = provider_requests
    totals = trace.get("totals") if isinstance(trace.get("totals"), dict) else {}
    reported_requests = [
        request
        for request in provider_requests
        if str(request.get("usageSource") or "") in {"provider_response", "provider_query"}
    ]
    estimated_requests = [
        request for request in provider_requests if request.get("estimatedInputTokens") is not None
    ]
    missing_requests = [
        request for request in provider_requests if str(request.get("usageSource") or "") == "missing"
    ]
    legacy_requests = [
        request
        for request in provider_requests
        if str(request.get("usageSource") or "") == "legacy_unknown"
    ]
    provider_attempt_count = sum(
        int(request.get("providerAttemptCount") or 0) for request in provider_requests
    )
    provider_retry_count = sum(
        int(request.get("providerRetryCount") or 0) for request in provider_requests
    )
    provider_failed_attempt_count = sum(
        sum(
            1
            for attempt in request.get("providerAttempts", [])
            if isinstance(attempt, dict) and str(attempt.get("outcome") or "") == "error"
        )
        for request in provider_requests
    )
    input_reported_requests = [
        request
        for request in reported_requests
        if request.get("providerReportedInputTokens") is not None
    ]
    reported_request_est_tokens = sum(
        int(request.get("requestEstTokens") or 0) for request in reported_requests
    )
    compared_est_tokens = sum(
        int(request.get("requestEstTokens") or 0) for request in input_reported_requests
    )
    compared_input_tokens = sum(
        int(request.get("providerReportedInputTokens") or 0)
        for request in input_reported_requests
    )
    estimate_error_tokens = (
        compared_est_tokens - compared_input_tokens if input_reported_requests else None
    )
    reported_input_tokens = _sum_optional(reported_requests, "providerReportedInputTokens")
    reported_output_tokens = _sum_optional(reported_requests, "providerReportedOutputTokens")
    reported_total_tokens = _sum_optional(reported_requests, "providerReportedTotalTokens")
    reported_cache_read = _sum_optional(reported_requests, "cacheReadInputTokens")
    reported_cache_creation = _sum_optional(
        reported_requests,
        "cacheCreationInputTokens",
    )
    reported_reasoning = _sum_optional(reported_requests, "reasoningTokens")
    accounting_records = [
        request.get("tokenAccounting")
        for request in provider_requests
        if isinstance(request.get("tokenAccounting"), dict)
    ]
    shadow_logical = sum(int(item.get("logicalInputTokens") or 0) for item in accounting_records)
    shadow_transmitted = sum(int(item.get("transmittedInputTokens") or 0) for item in accounting_records)
    shadow_cached = sum(int(item.get("cachedInputTokens") or 0) for item in accounting_records)
    shadow_reserve = sum(int(item.get("outputReserveTokens") or 0) for item in accounting_records)
    coverage_pct = (
        round((len(reported_requests) / len(provider_requests)) * 100, 4)
        if provider_requests
        else None
    )
    totals.update(
        {
            "providerRequestCount": len(provider_requests),
            "providerRequestEstTokens": sum(
                int(request.get("requestEstTokens") or 0) for request in provider_requests
            ),
            "providerUsageRequestCount": len(reported_requests),
            "providerUsageRequestEstTokens": reported_request_est_tokens,
            "providerInputTokens": reported_input_tokens,
            "providerOutputTokens": reported_output_tokens,
            "providerTotalTokens": reported_total_tokens,
            "providerInputEstimateErrorTokens": estimate_error_tokens,
            "providerInputEstimateErrorPct": (
                round((estimate_error_tokens / compared_input_tokens) * 100, 4)
                if compared_input_tokens
                else None
            ),
            "providerReportedUsageRequestCount": len(reported_requests),
            "providerReportedUsageRequestEstTokens": reported_request_est_tokens,
            "providerReportedInputTokens": reported_input_tokens,
            "providerReportedOutputTokens": reported_output_tokens,
            "providerReportedTotalTokens": reported_total_tokens,
            "providerReportedCacheReadInputTokens": reported_cache_read,
            "providerReportedCacheCreationInputTokens": reported_cache_creation,
            "providerReportedReasoningTokens": reported_reasoning,
            "providerReportedInputEstimateErrorTokens": estimate_error_tokens,
            "providerReportedInputEstimateErrorPct": (
                round((estimate_error_tokens / compared_input_tokens) * 100, 4)
                if compared_input_tokens
                else None
            ),
            "estimatedUsageRequestCount": len(estimated_requests),
            "missingUsageRequestCount": len(missing_requests),
            "legacyUnknownUsageRequestCount": len(legacy_requests),
            "reportedUsageCoveragePct": coverage_pct,
            "providerAttemptCount": provider_attempt_count,
            "providerRetryCount": provider_retry_count,
            "providerFailedAttemptCount": provider_failed_attempt_count,
            "shadowLogicalInputTokens": shadow_logical,
            "shadowTransmittedInputTokens": shadow_transmitted,
            "shadowCachedInputTokens": shadow_cached,
            "shadowOutputReserveTokens": shadow_reserve,
            "shadowRequestCount": len(accounting_records),
            "shadowTokenDelta": shadow_transmitted - shadow_logical,
        }
    )
    # Keep the long-standing aggregate names useful for direct callers.  The
    # route-level runtime merge may replace them with observed Provider usage;
    # the shadow-prefixed values remain the deterministic request accounting.
    if not int(totals.get("logicalInputTokens") or 0):
        totals["logicalInputTokens"] = shadow_logical
    if not int(totals.get("transmittedInputTokens") or 0):
        totals["transmittedInputTokens"] = shadow_transmitted
    if not int(totals.get("cachedInputTokens") or 0):
        totals["cachedInputTokens"] = shadow_cached
    trace["requestTokenAccounting"] = {
        "logicalInputTokens": shadow_logical,
        "transmittedInputTokens": shadow_transmitted,
        "cachedInputTokens": shadow_cached,
        "outputReserveTokens": shadow_reserve,
        "requestCount": len(accounting_records),
    }
    trace["totals"] = totals
    return trace


def summarize_context_trace(context_trace: Dict[str, Any] | None) -> Dict[str, Any]:
    trace = context_trace if isinstance(context_trace, dict) else {}
    sources = trace.get("sources") if isinstance(trace.get("sources"), list) else []
    duplicates = trace.get("duplicates") if isinstance(trace.get("duplicates"), list) else []
    calls = trace.get("llmCalls") if isinstance(trace.get("llmCalls"), list) else []
    provider_requests = (
        trace.get("providerRequests") if isinstance(trace.get("providerRequests"), list) else []
    )
    totals = trace.get("totals") if isinstance(trace.get("totals"), dict) else {}
    return {
        "estContextTokens": int(totals.get("estContextTokens") or 0),
        "sourceCount": len(sources),
        "truncatedCount": sum(1 for source in sources if isinstance(source, dict) and bool(source.get("truncated"))),
        "droppedCount": sum(1 for source in sources if isinstance(source, dict) and not bool(source.get("included"))),
        "duplicateCount": len(duplicates),
        "llmCallCount": sum(int(call.get("count") or 0) for call in calls if isinstance(call, dict)),
        "providerRequestCount": len(provider_requests),
        "providerUsageRequestCount": int(totals.get("providerUsageRequestCount") or 0),
        "providerTotalTokens": totals.get("providerTotalTokens"),
        "providerInputEstimateErrorPct": totals.get("providerInputEstimateErrorPct"),
        "providerReportedUsageRequestCount": int(
            totals.get("providerReportedUsageRequestCount") or 0
        ),
        "contractBuildMs": float(totals.get("contractBuildMs") or 0.0),
        "directoryScanCount": int(totals.get("directoryScanCount") or 0),
        "statCount": int(totals.get("statCount") or 0),
        "bridgeStartMs": float(totals.get("bridgeStartMs") or 0.0),
        "componentInitMs": float(totals.get("componentInitMs") or 0.0),
        "providerConfigMs": float(totals.get("providerConfigMs") or 0.0),
        "sessionInitMs": float(totals.get("sessionInitMs") or 0.0),
        "projectInstructionsMs": float(totals.get("projectInstructionsMs") or 0.0),
        "memoryInitMs": float(totals.get("memoryInitMs") or 0.0),
        "securityInitMs": float(totals.get("securityInitMs") or 0.0),
        "mcpInitMs": float(totals.get("mcpInitMs") or 0.0),
        "hooksInitMs": float(totals.get("hooksInitMs") or 0.0),
        "toolsInitMs": float(totals.get("toolsInitMs") or 0.0),
        "providerInitMs": float(totals.get("providerInitMs") or 0.0),
        "modelRounds": int(totals.get("modelRounds") or 0),
        "toolCalls": int(totals.get("toolCalls") or 0),
        "duplicateToolCallsSameRevision": int(
            totals.get("duplicateToolCallsSameRevision") or 0
        ),
        "logicalInputTokens": int(totals.get("logicalInputTokens") or 0),
        "transmittedInputTokens": int(totals.get("transmittedInputTokens") or 0),
        "cachedInputTokens": int(totals.get("cachedInputTokens") or 0),
        "shadowLogicalInputTokens": int(totals.get("shadowLogicalInputTokens") or 0),
        "shadowTransmittedInputTokens": int(totals.get("shadowTransmittedInputTokens") or 0),
        "shadowCachedInputTokens": int(totals.get("shadowCachedInputTokens") or 0),
        "shadowOutputReserveTokens": int(totals.get("shadowOutputReserveTokens") or 0),
        "shadowRequestCount": int(totals.get("shadowRequestCount") or 0),
        "shadowTokenDelta": int(totals.get("shadowTokenDelta") or 0),
        "logicalContextTokens": int(totals.get("logicalContextTokens") or 0),
        "transmittedContextTokens": int(totals.get("transmittedContextTokens") or 0),
        "cachedContextTokens": int(totals.get("cachedContextTokens") or 0),
        "contextOmittedBlockCount": int(totals.get("contextOmittedBlockCount") or 0),
        "contextTruncatedBlockCount": int(totals.get("contextTruncatedBlockCount") or 0),
        "contextCacheHitCount": int(totals.get("contextCacheHitCount") or 0),
        "contextBudgetMode": str(
            (trace.get("tokenAccounting") or {}).get("mode")
            if isinstance(trace.get("tokenAccounting"), dict)
            else ""
        ),
        "contextCacheStatus": str(
            (trace.get("cache") or {}).get("status")
            if isinstance(trace.get("cache"), dict)
            else ""
        ),
        "jitEnabled": bool(
            (trace.get("jit") or {}).get("enabled")
            if isinstance(trace.get("jit"), dict)
            else False
        ),
        "evidenceObservations": int(totals.get("evidenceObservations") or 0),
        "evidenceInvalidations": int(totals.get("evidenceInvalidations") or 0),
        "uniqueEvidenceBytes": int(totals.get("uniqueEvidenceBytes") or 0),
        "evidenceCoverage": trace.get("evidenceCoverage") if isinstance(trace.get("evidenceCoverage"), dict) else {},
        "providerReportedTotalTokens": totals.get("providerReportedTotalTokens"),
        "estimatedUsageRequestCount": int(totals.get("estimatedUsageRequestCount") or 0),
        "missingUsageRequestCount": int(totals.get("missingUsageRequestCount") or 0),
        "legacyUnknownUsageRequestCount": int(
            totals.get("legacyUnknownUsageRequestCount") or 0
        ),
        "reportedUsageCoveragePct": totals.get("reportedUsageCoveragePct"),
        "providerAttemptCount": int(totals.get("providerAttemptCount") or 0),
        "providerRetryCount": int(totals.get("providerRetryCount") or 0),
        "providerFailedAttemptCount": int(totals.get("providerFailedAttemptCount") or 0),
    }


def _coomi_memory_block(system_prompt: str) -> str:
    text = str(system_prompt or "")
    starts = [index for marker in ("## Persistent Memories", "## Memory Index") if (index := text.find(marker)) >= 0]
    if not starts:
        return ""
    start = min(starts)
    end = text.find("\n## ", start + 4)
    return text[start : end if end >= 0 else len(text)].strip()


def _tool_names(tools: Sequence[Dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for raw_tool in tools:
        tool = raw_tool if isinstance(raw_tool, dict) else {}
        function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        name = str(tool.get("name") or function.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _sum_optional(requests: Sequence[Dict[str, Any]], key: str) -> int | None:
    values = [int(request.get(key) or 0) for request in requests if request.get(key) is not None]
    return sum(values) if values else None


def _detect_duplicates(blocks: Sequence[Dict[str, Any]]) -> list[Dict[str, Any]]:
    occurrences: dict[str, Dict[str, Any]] = {}
    for raw_block in blocks:
        block = raw_block if isinstance(raw_block, dict) else {}
        kind = str(block.get("id") or block.get("kind") or "unknown").strip() or "unknown"
        for paragraph in _paragraphs(str(block.get("content") or "")):
            digest = hashlib.sha256(paragraph.encode("utf-8")).hexdigest()
            item = occurrences.setdefault(digest, {"hash": digest, "kinds": [], "chars": len(paragraph)})
            if kind not in item["kinds"]:
                item["kinds"].append(kind)
    return [item for item in occurrences.values() if len(item["kinds"]) > 1]


def _paragraphs(value: str) -> Iterable[str]:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not normalized:
        return ()
    return tuple(
        compact
        for paragraph in _PARAGRAPH_BREAK_RE.split(normalized)
        if (compact := _WHITESPACE_RE.sub("", paragraph))
    )


def _normalize_for_hash(value: Any) -> str:
    return _WHITESPACE_RE.sub("", unicodedata.normalize("NFKC", str(value or "")))


def _message_content_text(message: Dict[str, Any]) -> str:
    content = message.get("content") if isinstance(message, dict) else ""
    return content if isinstance(content, str) else _stable_json(content)


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
