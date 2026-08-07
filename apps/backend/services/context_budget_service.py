"""P1-7 context cache, JIT selection and model-token budgeting.

The default feature flags preserve the legacy assembled prompt. Shadow token
accounting is always deterministic; truncation/omission happens only when the
real-budget or JIT flags are enabled. Every removed block remains represented
by explicit metadata so both the model renderer and ContextTrace can see it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from services.context_trace_service import estimate_tokens


DEFAULT_CONTEXT_WINDOW = 256_000
DEFAULT_OUTPUT_RESERVE_TOKENS = 8_192
_CACHE_MAX_ITEMS = 32
_JIT_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,}|[\u4e00-\u9fff]{2,}")
_JIT_STOPWORDS = frozenset(
    {
        "请", "请你", "帮我", "继续", "续写", "写", "生成", "创作", "修改", "更新",
        "检查", "阅读", "读取", "分析", "说明", "项目", "故事", "章节", "文件", "当前",
        "这个", "那个", "相关", "内容", "please", "continue", "write", "generate", "read",
        "project", "story", "chapter", "file", "check", "review", "the", "and",
    }
)
_ESSENTIAL_JIT_BLOCKS = frozenset(
    {
        "runtime_presets",
        "recent_segments",
        "project_organization_inventory",
        "related_passages",
    }
)
_TOKEN_PRIORITY = {
    "related_passages": 0,
    "runtime_presets": 1,
    "recent_segments": 2,
    "project_organization_inventory": 3,
}


def stable_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def context_cache_key(
    *,
    workspace_root: Path,
    catalog_snapshot: Any,
    policy_fingerprint: str,
    prompt: str,
    active_file: str,
    intent_primary: str,
    turn_plan: Mapping[str, Any] | None,
    block_config: Mapping[str, Any],
) -> str:
    entries = getattr(catalog_snapshot, "entries", {}) if catalog_snapshot is not None else {}
    source_revisions = (
        sorted(
            (str(path), str(getattr(entry, "revision", "")))
            for path, entry in entries.items()
        )
        if isinstance(entries, Mapping)
        else []
    )
    return stable_digest(
        {
            "workspace": Path(workspace_root).resolve().as_posix(),
            "catalogGeneration": int(getattr(catalog_snapshot, "generation", 0) or 0),
            "catalogRevision": str(getattr(catalog_snapshot, "catalog_revision", "") or ""),
            "sourceRevisions": source_revisions,
            "policy": str(policy_fingerprint or ""),
            "prompt": stable_digest(str(prompt or "")),
            "activeFile": str(active_file or "").replace("\\", "/"),
            "intent": str(intent_primary or ""),
            "turnPlan": turn_plan or {},
            "blockConfig": dict(block_config),
        }
    )


class ContextAssemblyCache:
    def __init__(self, max_items: int = _CACHE_MAX_ITEMS) -> None:
        self.max_items = max(1, int(max_items))
        self._items: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: str) -> Dict[str, Any] | None:
        with self._lock:
            value = self._items.get(str(key))
            if value is None:
                return None
            self._items.move_to_end(str(key))
            return copy.deepcopy(value)

    def put(self, key: str, value: Mapping[str, Any]) -> None:
        with self._lock:
            self._items[str(key)] = copy.deepcopy(dict(value))
            self._items.move_to_end(str(key))
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


def _jit_terms(prompt: str) -> list[str]:
    terms: list[str] = []
    raw = str(prompt or "")
    for match in _JIT_TERM_RE.findall(raw):
        term = str(match).strip()
        if not term or term.casefold() in _JIT_STOPWORDS:
            continue
        # Keep deterministic topic fragments for Chinese prompts.  A long
        # contiguous CJK run should not hide a useful trailing noun when a
        # context block contains only that noun.
        pieces = re.split(
            r"(?:请你|帮我|请|继续|续写|写|生成|创作|修改|更新|检查|阅读|读取|分析|"
            r"相关|内容|项目|故事|章节|文件|关于|围绕|引发|导致|其中|的|了|在|是|和|与|及|把|将|对)",
            term,
        )
        for piece in pieces:
            normalized = str(piece or "").strip()
            if len(normalized) >= 2 and normalized.casefold() not in _JIT_STOPWORDS:
                terms.append(normalized)
    return list(dict.fromkeys(terms))[:24]


def _truncate_to_tokens(content: str, max_tokens: int) -> str:
    text = str(content or "").strip()
    if estimate_tokens(text) <= max_tokens:
        return text
    marker = "\n... [truncated: token_budget]"
    if max_tokens <= estimate_tokens(marker):
        return ""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[:middle].rstrip() + marker
        if estimate_tokens(candidate) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + marker if low else ""


def apply_context_controls(
    blocks: Sequence[Mapping[str, Any]],
    *,
    prompt: str,
    real_budget_enabled: bool,
    jit_enabled: bool,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    output_reserve_tokens: int = DEFAULT_OUTPUT_RESERVE_TOKENS,
    base_input_tokens: int = 0,
) -> tuple[list[Dict[str, Any]], Dict[str, Any], list[str]]:
    """Return controlled blocks, accounting and explicit diagnostic notes."""

    window = max(1_024, int(context_window or DEFAULT_CONTEXT_WINDOW))
    reserve = max(0, min(window - 1, int(output_reserve_tokens or 0)))
    available = max(0, window - reserve - max(0, int(base_input_tokens or 0)))
    logical_tokens = sum(estimate_tokens(str(block.get("content") or "")) for block in blocks)
    terms = _jit_terms(prompt)
    controlled = [copy.deepcopy(dict(block)) for block in blocks]
    notes: list[str] = []
    for block in controlled:
        block["logicalTokenCount"] = estimate_tokens(str(block.get("content") or ""))
        block["tokenCount"] = block["logicalTokenCount"]
        block["omitted"] = False
        block["truncated"] = bool(block.get("truncated"))
        block["dropReason"] = str(block.get("dropReason") or "")
        if not jit_enabled:
            continue
        block_id = str(block.get("id") or "")
        haystack = (str(block.get("content") or "") + " " + " ".join(str(item) for item in block.get("sourcePaths", []))).casefold()
        relevant = block_id in _ESSENTIAL_JIT_BLOCKS or any(term.casefold() in haystack for term in terms)
        if relevant:
            continue
        block["content"] = ""
        block["tokenCount"] = 0
        block["omitted"] = True
        block["dropReason"] = "jit_not_relevant"
        notes.append(f"{block_id}_omitted_by_jit")

    if real_budget_enabled:
        remaining = available
        order = sorted(
            range(len(controlled)),
            key=lambda index: (_TOKEN_PRIORITY.get(str(controlled[index].get("id") or ""), 10), index),
        )
        for index in order:
            block = controlled[index]
            content = str(block.get("content") or "").strip()
            if not content or block.get("omitted"):
                continue
            requested = estimate_tokens(content)
            if requested <= remaining:
                remaining -= requested
                block["tokenCount"] = requested
                continue
            truncated = _truncate_to_tokens(content, remaining)
            if truncated:
                block["content"] = truncated
                block["tokenCount"] = estimate_tokens(truncated)
                block["truncated"] = True
                block["dropReason"] = "token_budget_truncated"
                remaining = max(0, remaining - int(block["tokenCount"]))
                notes.append(f"{block.get('id')}_truncated_by_token_budget")
            else:
                block["content"] = ""
                block["tokenCount"] = 0
                block["omitted"] = True
                block["dropReason"] = "token_budget_omitted"
                notes.append(f"{block.get('id')}_omitted_by_token_budget")

    transmitted_tokens = sum(int(block.get("tokenCount") or 0) for block in controlled)
    cached_tokens = sum(max(0, int(block.get("cachedTokenCount") or 0)) for block in controlled)
    omitted = [str(block.get("id") or "") for block in controlled if block.get("omitted")]
    truncated = [str(block.get("id") or "") for block in controlled if block.get("truncated")]
    accounting = {
        "_type": "ContextTokenAccounting",
        "_version": 1,
        "mode": "enforced" if real_budget_enabled else "shadow",
        "contextWindow": window,
        "baseInputTokens": max(0, int(base_input_tokens or 0)),
        "outputReserveTokens": reserve,
        "availableContextTokens": available,
        "logicalContextTokens": logical_tokens,
        "transmittedContextTokens": transmitted_tokens,
        "cachedContextTokens": cached_tokens,
        "omittedBlocks": omitted,
        "truncatedBlocks": truncated,
        "jitEnabled": bool(jit_enabled),
        "jitTerms": terms,
    }
    return controlled, accounting, notes


def request_token_accounting(
    *,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    output_reserve_tokens: int = DEFAULT_OUTPUT_RESERVE_TOKENS,
) -> Dict[str, Any]:
    def _serialized(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    system_tokens = 0
    history_tokens = 0
    turn_contract_tokens = 0
    for message in messages:
        item = message if isinstance(message, Mapping) else {}
        content = _serialized(item.get("content") or "")
        tokens = estimate_tokens(content)
        if str(item.get("role") or "") == "system":
            system_tokens += tokens
            marker = content.find("Storydex turn contract:")
            if marker >= 0:
                turn_contract_tokens += estimate_tokens(content[marker:])
        else:
            history_tokens += tokens + estimate_tokens(_serialized(item.get("tool_calls") or []))
    tool_schema_tokens = estimate_tokens(_serialized(list(tools)))
    reserve = max(0, int(output_reserve_tokens or 0))
    logical = system_tokens + history_tokens + tool_schema_tokens
    return {
        "_type": "RequestTokenAccounting",
        "_version": 1,
        "systemTokens": system_tokens,
        "historyTokens": history_tokens,
        "toolSchemaTokens": tool_schema_tokens,
        "turnContractTokens": turn_contract_tokens,
        "outputReserveTokens": reserve,
        "logicalInputTokens": logical,
        "transmittedInputTokens": logical,
        "cachedInputTokens": 0,
        "reservedTotalTokens": logical + reserve,
    }


_CACHE = ContextAssemblyCache()


def get_context_assembly_cache() -> ContextAssemblyCache:
    return _CACHE


def reset_context_assembly_cache() -> None:
    _CACHE.clear()
