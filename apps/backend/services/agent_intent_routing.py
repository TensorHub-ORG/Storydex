from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable


LEGACY = "legacy"
DIRECT = "direct"
HYBRID = "hybrid"
WORKFLOW = "workflow"
ROUTING_MODES = frozenset({LEGACY, DIRECT, HYBRID, WORKFLOW})
ROUTING_MODE_FLAG = "AGENT_INTENT_ROUTING_MODE"

_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s\"'`]+|(?:\.{0,2}[\\/])?[^\s，。！？；;\"'`]+[\\/][^\s，。！？；;\"'`]+|[^\s，。！？；;\"'`]+\.(?:md|txt|json|jsonl|yaml|yml|toml))",
    re.IGNORECASE,
)
_ENTITY_PATTERNS = (
    re.compile(
        r"(?P<name>[\u4e00-\u9fffA-Za-z0-9_-]{1,24})"
        r"(?:的)?(?:角色卡|人物卡|世界书|设定条目)"
    ),
)
_ENTITY_ACTION_PREFIX_RE = re.compile(
    r"^(?:(?:请|先|再|然后|不要|不得|禁止|请勿|帮我|替我)\s*)*"
    r"(?:修改|更新|读取|查看|检查|分析|总结|概括|完善|调整|重写|整理|创建|新增|删除|移除|保留)?\s*"
)
_ENTITY_STOPWORDS = {
    "目标",
    "这个",
    "并行",
    "其他",
    "其它",
    "其余",
    "文件",
    "内容",
    "字段",
    "核心动机",
}
_TARGET_FIELD_RE = re.compile(
    r"(?:修改|更新|调整|重写|改写)[^，,。！？!?；;\n]{0,48}?"
    r"[\"“](?P<field>[^\"”]{1,24})[\"”](?:\s*(?:为|成|改为|改成))?",
    re.IGNORECASE,
)
_FIELD_NAMES = (
    "核心动机",
    "动机",
    "性格",
    "背景",
    "目标",
    "关系",
    "能力",
    "外貌",
    "弱点",
    "秘密",
    "边界",
    "规则",
    "禁忌",
    "历史",
    "地理",
)
_READ_SIGNAL_RE = re.compile(r"读取|查看|检查|分析|总结|概括|read|inspect|review|summari[sz]e", re.I)
_WRITE_SIGNAL_RE = re.compile(
    r"修改|更新|调整|重写|改写|创建|新增|生成|删除|整理|同步|实现|修复|写入|保存|"
    r"edit|update|adjust|rewrite|create|add|generate|delete|organize|sync|implement|fix|save",
    re.I,
)
_NO_WRITE_RE = re.compile(
    r"不要|不得|禁止|无需|不用|不必|只读|不修改|不写入|do\s+not|don't|never|read[- ]?only",
    re.I,
)
_SCOPED_NO_WRITE_RE = re.compile(
    r"(?:不要|不得|禁止|请勿|别|切勿)[^，,。！？!?；;\n]{0,16}"
    r"(?:其他|其它|其余|别的|非目标|无关)(?:项目)?(?:文件|字段|内容|角色卡)?|"
    r"(?:其他|其它|其余|别的|非目标|无关)(?:项目)?(?:文件|字段|内容|角色卡)?"
    r"[^，,。！？!?；;\n]{0,12}(?:不要|不得|禁止|请勿|别|切勿)",
    re.IGNORECASE,
)
_AMBIGUOUS_FOLLOWUP_RE = re.compile(
    r"^\s*(继续|执行|处理一下|弄一下|按刚才的来|就这样|确认|可以|好的?|go ahead|continue|do it)\s*[。.!！]?$",
    re.I,
)


def normalize_routing_mode(value: Any, *, fallback: str = LEGACY) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "b": DIRECT,
        "b_direct": DIRECT,
        "c": HYBRID,
        "c_hybrid": HYBRID,
        "d": WORKFLOW,
        "d_workflow": WORKFLOW,
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in ROUTING_MODES else fallback


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip().replace("\\", "/")
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def build_route_hints(
    *,
    prompt: str,
    active_file: str = "",
    routing_mode: str = LEGACY,
) -> Dict[str, Any]:
    """Build small, advisory-only project-location hints.

    These hints never grant write authority. They help the main model choose a
    first read/search action without injecting file previews or pretending the
    router has already established the requested fact.
    """

    text = str(prompt or "")
    explicit_paths = _dedupe(match.group(0).strip("`'\"") for match in _PATH_RE.finditer(text))
    normalized_active = str(active_file or "").strip().replace("\\", "/")
    entities: list[str] = []
    for pattern in _ENTITY_PATTERNS:
        for match in pattern.finditer(text):
            name = str(match.group("name") or "").strip()
            name = _ENTITY_ACTION_PREFIX_RE.sub("", name).strip()
            if name and name not in _ENTITY_STOPWORDS and name not in entities:
                entities.append(name)

    requested_fields = _dedupe(
        match.group("field") for match in _TARGET_FIELD_RE.finditer(text)
    )
    if not requested_fields:
        first_clause = re.split(r"[，,。！？!?；;\n]", text, maxsplit=1)[0]
        requested_fields = [
            field
            for field in _FIELD_NAMES
            if field in first_clause
            and not any(
                field != longer and field in longer and longer in first_clause
                for longer in _FIELD_NAMES
            )
        ]
    document_kinds: list[str] = []
    lower = text.lower()
    path_evidence = " ".join([normalized_active, *explicit_paths]).lower()
    for kind, signals in (
        ("character", ("角色", "人物", ".storydex/characters/")),
        ("worldbook", ("世界书", "世界观", "设定", ".storydex/worldbook/")),
        ("chapter", ("章节", "正文", "chapter", "chapters/")),
        ("wiki", ("wiki", "知识图谱", ".storydex/wiki/")),
        ("script", ("剧本", "大纲", "script", ".storydex/scripts/")),
    ):
        if any(signal.lower() in lower or signal.lower() in path_evidence for signal in signals):
            document_kinds.append(kind)

    operation_signals: list[str] = []
    if _READ_SIGNAL_RE.search(text):
        operation_signals.append("read")
    if _WRITE_SIGNAL_RE.search(text):
        operation_signals.append("write")
    if _NO_WRITE_RE.search(text):
        operation_signals.append(
            "scope_exclusion" if _SCOPED_NO_WRITE_RE.search(text) else "no_write"
        )
    if "删除" in text or re.search(r"\bdelete|\bremove", text, re.I):
        operation_signals.append("delete")

    candidate_paths = _dedupe([normalized_active, *explicit_paths])
    confidence = (
        "high"
        if explicit_paths or (operation_signals and (document_kinds or entities))
        else "medium"
        if normalized_active or document_kinds or entities or operation_signals
        else "low"
    )
    return {
        "_type": "RouteHints",
        "_version": 1,
        "source": "deterministic",
        "routingMode": normalize_routing_mode(routing_mode),
        "confidence": confidence,
        "explicitPaths": explicit_paths[:8],
        "activeFile": normalized_active,
        "namedEntities": entities[:8],
        "requestedFields": requested_fields[:8],
        "documentKinds": document_kinds[:6],
        "operationSignals": _dedupe(operation_signals),
        "candidatePaths": candidate_paths[:8],
        "advisoryOnly": True,
    }


def should_invoke_intent_model(
    mode: str,
    *,
    prompt: str,
    heuristic_frame: Dict[str, Any],
    route_hints: Dict[str, Any],
    previous_turn: Dict[str, Any] | None,
    has_custom_intents: bool,
    explicit_workflow: bool,
    workflow_confirmation: bool,
) -> bool:
    normalized_mode = normalize_routing_mode(mode)
    if normalized_mode == LEGACY:
        return True
    if normalized_mode == DIRECT:
        return False

    specialized = bool(explicit_workflow or workflow_confirmation or has_custom_intents)
    if normalized_mode == WORKFLOW:
        return specialized
    if specialized:
        return True

    text = str(prompt or "").strip()
    if previous_turn and _AMBIGUOUS_FOLLOWUP_RE.match(text):
        return True
    operation_signals = set(route_hints.get("operationSignals") or [])
    requested_fields = route_hints.get("requestedFields") or []
    candidates = (
        route_hints.get("explicitPaths")
        or route_hints.get("candidatePaths")
        or route_hints.get("namedEntities")
        or route_hints.get("documentKinds")
    )

    # A broad no-write instruction is already a hard, deterministic boundary.
    # A scoped exclusion ("change this field, not other files") is deliberately
    # different: it is a clear mutation and may skip the extra classifier call.
    if "no_write" in operation_signals and "scope_exclusion" not in operation_signals:
        return False

    # A read/write mixture without a named field is usually a discussion or an
    # ambiguous workflow (for example, "read it and decide what to change").
    # Keep the semantic model in that path instead of granting a write boundary
    # from keyword order alone.
    if {"read", "write"}.issubset(operation_signals) and not requested_fields:
        return True

    operation_type = str(heuristic_frame.get("operationType") or "").strip().lower()
    can_write = bool(
        heuristic_frame.get(
            "canWrite",
            operation_type in {"create_new", "modify_existing"},
        )
    )
    confidence = str(heuristic_frame.get("confidence") or "low").strip().lower()

    # Greetings and explicit reads are safe to route locally.  The caller also
    # applies the read-only semantic correction before using this decision so a
    # heuristic domain label can never accidentally expose write tools.
    if operation_type == "greeting" and not ({"write", "delete"} & operation_signals):
        return False
    if (
        "read" in operation_signals
        and "write" not in operation_signals
        and not can_write
        and candidates
        and confidence in {"high", "medium"}
    ):
        return False

    # Skip only when a mutation has both a concrete target and a deterministic
    # operation.  This covers the common Storydex field edit while retaining
    # the model for vague requests such as "write the next chapter" or
    # "what is this character like?".
    if (
        "write" in operation_signals
        and can_write
        and candidates
        and operation_type in {"create_new", "modify_existing"}
        and confidence in {"high", "medium"}
        and "delete" not in operation_signals
    ):
        return False

    # Low-confidence domain guesses must not become permission decisions.
    return True


def likely_candidate_paths(
    workspace_root: Path,
    route_hints: Dict[str, Any],
) -> list[str]:
    """Resolve only already-named candidates; never scan file contents here."""

    root = Path(workspace_root).resolve()
    candidates: list[str] = []
    for raw in route_hints.get("candidatePaths", []):
        normalized = str(raw or "").strip().replace("\\", "/").lstrip("/")
        if not normalized:
            continue
        try:
            path = (root / normalized).resolve()
            path.relative_to(root)
        except (OSError, ValueError):
            continue
        if path.exists() and normalized not in candidates:
            candidates.append(normalized)
    return candidates
