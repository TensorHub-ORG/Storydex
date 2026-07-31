"""Deterministic assembly for the elastic story manuscript protocol.

The first Provider call always submits a complete canonical chapter. Optional
edits are treated as independent, disposable hints: one malformed edit never
invalidates the canonical prose or another edit. This module owns every text
position and selection decision so routes and UI code never infer manuscript
semantics from model-provided counts.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from itertools import combinations
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from services.story_prose_quality import (
    contextual_quality_issues,
    duplicate_paragraph_count,
    repeated_ngram_count,
)
from services.story_word_count_service import (
    STORY_WORD_COUNT_RULE,
    chapter_normal_band,
    chapter_precision_band,
    count_story_text_words,
)


ELASTIC_DRAFT_TOOL_NAME = "StorydexSubmitElasticDraftV1"
ELASTIC_DRAFT_SCHEMA_VERSION = 1
ELASTIC_REPAIR_TOOL_NAME = "StorydexSubmitElasticRepairV1"
ELASTIC_REPAIR_SCHEMA_VERSION = 1
ELASTIC_LENGTH_CONTROL_STRATEGY = "elastic_manuscript_v1"
MAXIMUM_COMPACT_REPLACEMENTS = 3
MAXIMUM_EXPANSION_MODULES = 3
MAXIMUM_OPTIONAL_EDITS = 6
MAXIMUM_EDIT_COMBINATIONS = 64
MAXIMUM_REPAIR_OPERATIONS = 3
WIDE_RECOVERY_MINIMUM_IMPROVEMENT = 0.50

FALLBACK_CANONICAL_IN_BAND = "canonical_in_band"
FALLBACK_CANDIDATE_IN_BAND = "candidate_in_band"
FALLBACK_CANDIDATE_RECOVERED = "candidate_recovered"
FALLBACK_NO_VALID_EDITS = "no_valid_edits"
FALLBACK_INSUFFICIENT_IMPROVEMENT = "insufficient_improvement"
FALLBACK_REPAIR_FAILED = "repair_failed"
FALLBACK_REPAIR_QUALITY_REJECTED = "repair_quality_rejected"
FALLBACK_REPAIR_OUTSIDE_BAND = "repair_outside_band"

_PARAGRAPH_SEPARATOR_RE = re.compile(r"\n\s*\n")


class ElasticManuscriptRejected(ValueError):
    def __init__(self, reason: str, *, issues: Sequence[str] = ()) -> None:
        super().__init__(reason)
        self.reason = str(reason)
        self.issues = list(dict.fromkeys(str(item) for item in issues if str(item)))


def repair_completion_cap(character_gap: int) -> int:
    """Return a capacity ceiling sized from the measured local repair gap."""

    gap = max(0, abs(int(character_gap)))
    return max(512, min(8192, gap * 2 + 384))


def generated_overhead_ratio(
    completion_tokens: int | None,
    baseline_completion_tokens: int | None,
) -> float | None:
    """Compare true Provider usage when a real natural-draft baseline exists."""

    if completion_tokens is None or baseline_completion_tokens is None:
        return None
    baseline = int(baseline_completion_tokens)
    if baseline <= 0:
        return None
    return round(int(completion_tokens) / baseline, 4)


def literal_fact_anchors(source_context: str, manuscript: str) -> List[str]:
    """Return context lines that can be protected by exact manuscript matching."""

    text = str(manuscript or "")
    anchors: List[str] = []
    for raw_line in str(source_context or "").splitlines():
        anchor = re.sub(
            r"^\s*(?:#{1,6}\s*|[-*+]\s+|\d+[.)]\s+)",
            "",
            raw_line,
        ).strip()
        if 4 <= len(anchor) <= 200 and text.count(anchor) == 1:
            anchors.append(anchor)
    return list(dict.fromkeys(anchors))


def build_elastic_draft_messages(
    *,
    prompt: str,
    context: str,
    target: int,
    chapter_path: str = "",
) -> List[Dict[str, str]]:
    """Build the one-call manuscript request without paragraph length quotas."""

    normal_low, normal_high = chapter_normal_band(target)
    system = (
        "你是中文长篇小说的执笔作者。必须调用 StorydexSubmitElasticDraftV1，"
        "canonicalText 必须是一篇连续、完整、可独立发布且自然收尾的章节正文。"
        "正文质量、因果、人物状态、视角、节奏和结尾完整性优先于长度。"
        f"本章参考长度约为 {max(1, int(target))} 个 Storydex 非空白字符；"
        f"正常放行范围为 {normal_low}-{normal_high}，这是程序验收带，不是逐段配额。"
        f"计数规则由程序执行：{STORY_WORD_COUNT_RULE}。"
        "不要自行逐字精确计数，不要把正文拆成场景或段落长度容器，"
        "不要输出标题、摘要、思考过程、字数说明或元话语。"
        "最多提供 3 个紧凑替换和 3 个扩展模块；它们必须是可丢弃的局部自然编辑，"
        "使用 canonicalText 中唯一可匹配的原文字面锚点。"
        f"如果 canonicalText 可能低于 {normal_low}，请提供 1 至 3 个 expansionModules，"
        "每个模块展开既有感知、动作、心理或环境压力，供程序在不重写整章的情况下补足自然篇幅。"
        "紧凑替换必须更短并保持事实、状态、因果和语气不变；扩展模块只能展开已有事实，"
        "不得引入新决定、位置、人物状态或后文依赖。开头、最后两段和 endingHook 不得编辑。"
    )
    user_parts = [f"作者请求：{str(prompt or '').strip()}"]
    if chapter_path:
        user_parts.append(f"已确定写入章节：{chapter_path}")
    if str(context or "").strip():
        user_parts.append("可用项目上下文：\n" + str(context).strip())
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def build_elastic_repair_messages(
    *,
    manuscript: str,
    target: int,
    current_count: int,
    ending_hook: str,
    protected_fact_anchors: Sequence[str] = (),
) -> List[Dict[str, str]]:
    """Build the single precise-mode repair request around measured facts only."""

    manuscript_text = str(manuscript or "")
    paragraphs = [
        manuscript_text[start:end]
        for start, end in _paragraph_spans(manuscript_text)
    ]
    protected_opening = paragraphs[0] if paragraphs else manuscript_text
    protected_ending = "\n\n".join(paragraphs[-2:]) if paragraphs else manuscript_text
    precision_low, precision_high = chapter_precision_band(target)
    if current_count < precision_low:
        gap_text = f"需要局部增加约 {precision_low - current_count} 个字符"
        operation_rule = (
            "扩写时只能使用 1 至 3 个 insert_after；anchor 必须位于可编辑的中部正文，"
            "每段扩写只能展开既有感知、动作、心理或环境压力。"
        )
    elif current_count > precision_high:
        gap_text = f"需要局部减少约 {current_count - precision_high} 个字符"
        operation_rule = (
            "缩短时只能使用 1 至 3 个 replace_paragraph_range；"
            "sourceStart 和 sourceEnd 都必须位于可编辑的中部正文，replacementText 必须更短。"
        )
    else:
        gap_text = "正文已经进入精确范围，不应进行修补"
        operation_rule = "正文已在精确范围内，不应提交任何修补操作。"
    facts = [str(item) for item in protected_fact_anchors if str(item).strip()]
    system = (
        "你只能调用 StorydexSubmitElasticRepairV1 返回一个局部 repair pack。"
        "只允许 replace_paragraph_range 和 insert_after，最多 3 个互不重叠的操作。"
        "禁止全文重述、全文压缩、全文续写、删除自然段、改变开头、最后两段、"
        "endingHook、既有事实、人物状态、因果、视角和后文依赖。"
        "所有锚点必须是下方正文中唯一可匹配的原文字面，不要计算数字字符偏移。"
        "全部操作的总作用域必须小于正文的一半。"
        + operation_rule
    )
    user = (
        f"程序实测正文长度：{int(current_count)}。\n"
        f"精确范围：{precision_low}-{precision_high}。\n"
        f"局部修补方向：{gap_text}。\n"
        f"受保护 endingHook：{str(ending_hook or '')}\n"
        f"受保护事实锚点：{json.dumps(facts, ensure_ascii=False)}\n\n"
        f"禁止触碰的开头段落原文：{protected_opening}\n\n"
        f"禁止触碰的末尾两段原文：{protected_ending}\n\n"
        "待局部修补的完整正文：\n"
        + manuscript_text
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def elastic_draft_tool_schema() -> Dict[str, Any]:
    compact_item = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "id",
            "sourceStart",
            "sourceEnd",
            "replacementText",
            "preservedFactIds",
            "stateDelta",
        ],
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "sourceStart": {"type": "string", "minLength": 1},
            "sourceEnd": {"type": "string", "minLength": 1},
            "replacementText": {"type": "string", "minLength": 1},
            "preservedFactIds": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
            },
            "stateDelta": {"type": "string", "enum": ["same"]},
        },
    }
    expansion_item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "anchor", "position", "text", "stateDelta"],
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "anchor": {"type": "string", "minLength": 1},
            "position": {"type": "string", "enum": ["after"]},
            "text": {"type": "string", "minLength": 1},
            "stateDelta": {"type": "string", "enum": ["none"]},
        },
    }
    return {
        "name": ELASTIC_DRAFT_TOOL_NAME,
        "description": (
            "Submit one complete, publishable chapter and optional local semantic edits. "
            "The canonical chapter must stand on its own when every optional edit is ignored."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "version",
                "canonicalText",
                "compactReplacements",
                "expansionModules",
                "endingHook",
            ],
            "properties": {
                "version": {"type": "integer", "enum": [ELASTIC_DRAFT_SCHEMA_VERSION]},
                "canonicalText": {
                    "type": "string",
                    "description": "完整、可发布的整章正文；即使忽略所有 optional edits 也必须自然收尾。",
                },
                "compactReplacements": {
                    "type": "array",
                    "maxItems": MAXIMUM_COMPACT_REPLACEMENTS,
                    "items": compact_item,
                },
                "expansionModules": {
                    "type": "array",
                    "description": "当 canonicalText 可能明显偏短时，提供 1 至 3 个可丢弃的局部扩写模块。",
                    "maxItems": MAXIMUM_EXPANSION_MODULES,
                    "items": expansion_item,
                },
                "endingHook": {"type": "string", "minLength": 1},
            },
        },
    }


def elastic_repair_tool_schema() -> Dict[str, Any]:
    operation = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "op"],
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "op": {
                "type": "string",
                "enum": ["replace_paragraph_range", "insert_after"],
            },
            "sourceStart": {"type": "string", "minLength": 1},
            "sourceEnd": {"type": "string", "minLength": 1},
            "replacementText": {"type": "string", "minLength": 1},
            "anchor": {"type": "string", "minLength": 1},
            "text": {"type": "string", "minLength": 1},
        },
    }
    return {
        "name": ELASTIC_REPAIR_TOOL_NAME,
        "description": (
            "Submit one atomic local repair pack for an existing complete chapter. "
            "Never restate, compress, continue, or rewrite the whole chapter."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["version", "operations"],
            "properties": {
                "version": {"type": "integer", "enum": [ELASTIC_REPAIR_SCHEMA_VERSION]},
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAXIMUM_REPAIR_OPERATIONS,
                    "items": operation,
                },
            },
        },
    }


def band_distance(count: int, band: Tuple[int, int]) -> int:
    low, high = int(band[0]), int(band[1])
    actual = max(0, int(count))
    if actual < low:
        return low - actual
    if actual > high:
        return actual - high
    return 0


def _as_object(value: Any, *, reason: str) -> Dict[str, Any]:
    data = value
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except ValueError as exc:
            raise ElasticManuscriptRejected(reason) from exc
    if not isinstance(data, dict):
        raise ElasticManuscriptRejected(reason)
    return data


def _unique_span(text: str, start_anchor: str, end_anchor: str) -> Tuple[int, int]:
    start_value = str(start_anchor or "")
    end_value = str(end_anchor or "")
    if not start_value or not end_value:
        raise ElasticManuscriptRejected("anchor_missing")
    if text.count(start_value) != 1 or text.count(end_value) != 1:
        raise ElasticManuscriptRejected("anchor_not_unique")
    start = text.index(start_value)
    end_start = text.index(end_value)
    end = end_start + len(end_value)
    if end_start < start or end <= start:
        raise ElasticManuscriptRejected("anchor_order_invalid")
    return start, end


def _unique_anchor(text: str, anchor: str) -> Tuple[int, int]:
    value = str(anchor or "")
    if not value:
        raise ElasticManuscriptRejected("anchor_missing")
    if text.count(value) != 1:
        raise ElasticManuscriptRejected("anchor_not_unique")
    start = text.index(value)
    return start, start + len(value)


def _paragraph_spans(text: str) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    cursor = 0
    for match in _PARAGRAPH_SEPARATOR_RE.finditer(text):
        if text[cursor : match.start()].strip():
            left = cursor
            while left < match.start() and text[left].isspace():
                left += 1
            right = match.start()
            while right > left and text[right - 1].isspace():
                right -= 1
            spans.append((left, right))
        cursor = match.end()
    if text[cursor:].strip():
        left = cursor
        while left < len(text) and text[left].isspace():
            left += 1
        right = len(text)
        while right > left and text[right - 1].isspace():
            right -= 1
        spans.append((left, right))
    return spans


def _protected_spans(text: str) -> List[Tuple[int, int]]:
    paragraphs = _paragraph_spans(text)
    if not paragraphs:
        return [(0, len(text))]
    protected = [paragraphs[0], *paragraphs[-2:]]
    return sorted(set(protected))


def _overlaps(left: Tuple[int, int], right: Tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _touches_protected(span: Tuple[int, int], protected: Sequence[Tuple[int, int]]) -> bool:
    return any(_overlaps(span, item) for item in protected)


def _quality_regressed(candidate: str, canonical: str, *, source_context: str, user_task: str) -> bool:
    issues = contextual_quality_issues(
        candidate,
        source_context="\n\n".join(
            item for item in (str(source_context or "").strip(), canonical) if item
        ),
        user_task=user_task,
    )
    if issues:
        return True
    if repeated_ngram_count(candidate) > repeated_ngram_count(canonical):
        return True
    return duplicate_paragraph_count(candidate) > duplicate_paragraph_count(canonical)


def _normalize_optional_edits(
    data: Dict[str, Any],
    canonical: str,
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    protected = _protected_spans(canonical)
    edits: List[Dict[str, Any]] = []
    rejected: List[str] = []
    rejected_reasons: List[str] = []
    seen_ids: set[str] = set()

    raw_compact = data.get("compactReplacements")
    compact = raw_compact if isinstance(raw_compact, list) else []
    for index, raw in enumerate(compact):
        item = raw if isinstance(raw, dict) else {}
        edit_id = str(item.get("id") or f"compact-{index + 1}").strip()
        try:
            if index >= MAXIMUM_COMPACT_REPLACEMENTS:
                raise ElasticManuscriptRejected("too_many_compact_replacements")
            if not edit_id or edit_id in seen_ids:
                raise ElasticManuscriptRejected("edit_id_invalid")
            if str(item.get("stateDelta") or "") != "same":
                raise ElasticManuscriptRejected("compact_state_delta_invalid")
            replacement = str(item.get("replacementText") or "")
            if not replacement.strip():
                raise ElasticManuscriptRejected("compact_replacement_empty")
            span = _unique_span(canonical, item.get("sourceStart"), item.get("sourceEnd"))
            if _touches_protected(span, protected):
                raise ElasticManuscriptRejected("edit_touches_protected_region")
            source = canonical[span[0] : span[1]]
            if count_story_text_words(replacement) >= count_story_text_words(source):
                raise ElasticManuscriptRejected("compact_replacement_not_shorter")
            edits.append(
                {
                    "id": edit_id,
                    "kind": "replace",
                    "start": span[0],
                    "end": span[1],
                    "text": replacement,
                    "changedCharacters": abs(len(source) - len(replacement)),
                    "preservedFactIds": [
                        str(value)
                        for value in list(item.get("preservedFactIds") or [])
                        if str(value).strip()
                    ],
                }
            )
            seen_ids.add(edit_id)
        except (ElasticManuscriptRejected, TypeError) as exc:
            rejected.append(edit_id or f"compact-{index + 1}")
            rejected_reasons.append(str(exc) or type(exc).__name__)

    raw_expansion = data.get("expansionModules")
    expansion = raw_expansion if isinstance(raw_expansion, list) else []
    for index, raw in enumerate(expansion):
        item = raw if isinstance(raw, dict) else {}
        edit_id = str(item.get("id") or f"expansion-{index + 1}").strip()
        try:
            if index >= MAXIMUM_EXPANSION_MODULES:
                raise ElasticManuscriptRejected("too_many_expansion_modules")
            if not edit_id or edit_id in seen_ids:
                raise ElasticManuscriptRejected("edit_id_invalid")
            if str(item.get("position") or "") != "after":
                raise ElasticManuscriptRejected("expansion_position_invalid")
            if str(item.get("stateDelta") or "") != "none":
                raise ElasticManuscriptRejected("expansion_state_delta_invalid")
            module_text = str(item.get("text") or "")
            if not module_text.strip():
                raise ElasticManuscriptRejected("expansion_text_empty")
            span = _unique_anchor(canonical, item.get("anchor"))
            if _touches_protected(span, protected):
                raise ElasticManuscriptRejected("edit_touches_protected_region")
            edits.append(
                {
                    "id": edit_id,
                    "kind": "insert",
                    "start": span[1],
                    "end": span[1],
                    "text": module_text,
                    "changedCharacters": len(module_text),
                    "preservedFactIds": [],
                }
            )
            seen_ids.add(edit_id)
        except (ElasticManuscriptRejected, TypeError) as exc:
            rejected.append(edit_id or f"expansion-{index + 1}")
            rejected_reasons.append(str(exc) or type(exc).__name__)
    return edits, rejected, rejected_reasons


def _edits_conflict(edits: Sequence[Dict[str, Any]]) -> bool:
    ordered = sorted(edits, key=lambda item: (int(item["start"]), int(item["end"])))
    for left, right in combinations(ordered, 2):
        left_span = (int(left["start"]), int(left["end"]))
        right_span = (int(right["start"]), int(right["end"]))
        if left["kind"] == "insert" and right["kind"] == "insert":
            if left_span[0] == right_span[0]:
                return True
            continue
        if left["kind"] == "insert":
            if right_span[0] <= left_span[0] <= right_span[1]:
                return True
            continue
        if right["kind"] == "insert":
            if left_span[0] <= right_span[0] <= left_span[1]:
                return True
            continue
        if _overlaps(left_span, right_span):
            return True
    return False


def _apply_edits(canonical: str, edits: Sequence[Dict[str, Any]]) -> str:
    text = canonical
    for edit in sorted(
        edits,
        key=lambda item: (int(item["start"]), int(item["end"]), str(item["id"])),
        reverse=True,
    ):
        start, end = int(edit["start"]), int(edit["end"])
        text = text[:start] + str(edit["text"]) + text[end:]
    return text


def _all_edit_combinations(edits: Sequence[Dict[str, Any]]) -> Iterable[Tuple[Dict[str, Any], ...]]:
    bounded = list(edits[:MAXIMUM_OPTIONAL_EDITS])
    for size in range(len(bounded) + 1):
        yield from combinations(bounded, size)


def _result_payload(
    *,
    text: str,
    canonical: str,
    target: int,
    selected: Sequence[Dict[str, Any]],
    rejected_ids: Sequence[str],
    rejected_reasons: Sequence[str],
    reason: str,
    evaluated_count: int,
) -> Dict[str, Any]:
    final_count = count_story_text_words(text)
    canonical_count = count_story_text_words(canonical)
    normal_low, normal_high = chapter_normal_band(target)
    precision_low, precision_high = chapter_precision_band(target)
    return {
        "text": text,
        "canonicalText": canonical,
        "canonicalWordCount": canonical_count,
        "finalWordCount": final_count,
        "normalBandPassed": normal_low <= final_count <= normal_high,
        "precisionAchieved": precision_low <= final_count <= precision_high,
        "selectedEditIds": sorted(str(item["id"]) for item in selected),
        "rejectedEditIds": sorted(set(str(item) for item in rejected_ids)),
        "rejectedEditReasonCounts": dict(
            sorted(Counter(str(item) for item in rejected_reasons if str(item)).items())
        ),
        "lengthFallbackReason": reason,
        "lengthControlStrategy": ELASTIC_LENGTH_CONTROL_STRATEGY,
        "evaluatedCombinationCount": evaluated_count,
    }


def select_elastic_draft(
    payload: Any,
    *,
    target: int,
    precise: bool,
    source_context: str = "",
    user_task: str = "",
    protected_fact_anchors: Sequence[str] = (),
) -> Dict[str, Any]:
    """Validate optional edits and deterministically select one manuscript."""

    data = _as_object(payload, reason="elastic_draft_not_object")
    if int(data.get("version") or 0) != ELASTIC_DRAFT_SCHEMA_VERSION:
        raise ElasticManuscriptRejected("elastic_draft_version_mismatch")
    canonical = str(data.get("canonicalText") or "").strip()
    if not canonical:
        raise ElasticManuscriptRejected("canonical_text_empty")
    canonical_issues = contextual_quality_issues(
        canonical,
        source_context=str(source_context or ""),
        user_task=user_task,
    )
    if canonical_issues:
        raise ElasticManuscriptRejected(
            "canonical_quality_rejected",
            issues=canonical_issues,
        )

    ending_hook = str(data.get("endingHook") or "").strip()
    hook_valid = bool(ending_hook) and canonical.rstrip().endswith(ending_hook)
    fact_anchors = list(protected_fact_anchors) or literal_fact_anchors(
        source_context,
        canonical,
    )
    edits, rejected_ids, rejected_reasons = _normalize_optional_edits(data, canonical)

    selected_band = chapter_precision_band(target) if precise else chapter_normal_band(target)
    canonical_count = count_story_text_words(canonical)
    if band_distance(canonical_count, selected_band) == 0:
        return _result_payload(
            text=canonical,
            canonical=canonical,
            target=target,
            selected=[],
            rejected_ids=rejected_ids,
            rejected_reasons=rejected_reasons,
            reason=FALLBACK_CANONICAL_IN_BAND,
            evaluated_count=1,
        )

    candidates: List[Dict[str, Any]] = []
    evaluated_count = 0
    for combination in _all_edit_combinations(edits):
        evaluated_count += 1
        if not combination or _edits_conflict(combination):
            continue
        candidate = _apply_edits(canonical, combination)
        if any(str(anchor) and str(anchor) not in candidate for anchor in fact_anchors):
            continue
        if hook_valid and not candidate.rstrip().endswith(ending_hook):
            continue
        if _quality_regressed(
            candidate,
            canonical,
            source_context=source_context,
            user_task=user_task,
        ):
            continue
        count = count_story_text_words(candidate)
        candidates.append(
            {
                "text": candidate,
                "count": count,
                "distance": band_distance(count, selected_band),
                "edits": combination,
                "editCount": len(combination),
                "changedCharacters": sum(
                    int(item["changedCharacters"]) for item in combination
                ),
                "ids": tuple(sorted(str(item["id"]) for item in combination)),
            }
        )

    in_band = [item for item in candidates if int(item["distance"]) == 0]
    ordering = lambda item: (
        int(item["editCount"]),
        int(item["changedCharacters"]),
        abs(int(item["count"]) - max(1, int(target))),
        item["ids"],
    )
    if in_band:
        winner = min(in_band, key=ordering)
        return _result_payload(
            text=str(winner["text"]),
            canonical=canonical,
            target=target,
            selected=list(winner["edits"]),
            rejected_ids=rejected_ids,
            rejected_reasons=rejected_reasons,
            reason=FALLBACK_CANDIDATE_IN_BAND,
            evaluated_count=evaluated_count,
        )

    original_distance = band_distance(canonical_count, selected_band)
    recovered = [
        item
        for item in candidates
        if original_distance > 0
        and int(item["distance"])
        <= original_distance * (1.0 - WIDE_RECOVERY_MINIMUM_IMPROVEMENT)
    ]
    if recovered:
        winner = min(recovered, key=ordering)
        return _result_payload(
            text=str(winner["text"]),
            canonical=canonical,
            target=target,
            selected=list(winner["edits"]),
            rejected_ids=rejected_ids,
            rejected_reasons=rejected_reasons,
            reason=FALLBACK_CANDIDATE_RECOVERED,
            evaluated_count=evaluated_count,
        )

    return _result_payload(
        text=canonical,
        canonical=canonical,
        target=target,
        selected=[],
        rejected_ids=rejected_ids,
        rejected_reasons=rejected_reasons,
        reason=(
            FALLBACK_INSUFFICIENT_IMPROVEMENT
            if edits
            else FALLBACK_NO_VALID_EDITS
        ),
        evaluated_count=evaluated_count,
    )


def _normalize_repair_operations(
    canonical: str,
    payload: Any,
) -> List[Dict[str, Any]]:
    data = _as_object(payload, reason="repair_not_object")
    if int(data.get("version") or 0) != ELASTIC_REPAIR_SCHEMA_VERSION:
        raise ElasticManuscriptRejected("repair_version_mismatch")
    raw_operations = data.get("operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        raise ElasticManuscriptRejected("repair_without_operations")
    if len(raw_operations) > MAXIMUM_REPAIR_OPERATIONS:
        raise ElasticManuscriptRejected("repair_too_many_operations")
    protected = _protected_spans(canonical)
    operations: List[Dict[str, Any]] = []
    ids: set[str] = set()
    for raw in raw_operations:
        if not isinstance(raw, dict):
            raise ElasticManuscriptRejected("repair_operation_not_object")
        operation_id = str(raw.get("id") or "").strip()
        if not operation_id or operation_id in ids:
            raise ElasticManuscriptRejected("repair_operation_id_invalid")
        op = str(raw.get("op") or "").strip()
        if op == "replace_paragraph_range":
            span = _unique_span(canonical, raw.get("sourceStart"), raw.get("sourceEnd"))
            replacement = str(raw.get("replacementText") or "")
            if not replacement.strip():
                raise ElasticManuscriptRejected("repair_replacement_empty")
            operation_text = replacement
        elif op == "insert_after":
            anchor_span = _unique_anchor(canonical, raw.get("anchor"))
            span = (anchor_span[1], anchor_span[1])
            operation_text = str(raw.get("text") or "")
            if not operation_text.strip():
                raise ElasticManuscriptRejected("repair_insert_empty")
        else:
            raise ElasticManuscriptRejected("repair_operation_not_allowed")
        protected_span = span if span[0] != span[1] else (max(0, span[0] - 1), span[1] + 1)
        if _touches_protected(protected_span, protected):
            raise ElasticManuscriptRejected("repair_touches_protected_region")
        operations.append(
            {
                "id": operation_id,
                "kind": "replace" if op == "replace_paragraph_range" else "insert",
                "start": span[0],
                "end": span[1],
                "text": operation_text,
                "changedCharacters": abs((span[1] - span[0]) - len(operation_text)),
            }
        )
        ids.add(operation_id)
    if _edits_conflict(operations):
        raise ElasticManuscriptRejected("repair_operations_overlap")
    affected = sum(max(1, int(item["end"]) - int(item["start"])) for item in operations)
    if affected >= max(1, int(len(canonical) * 0.50)):
        raise ElasticManuscriptRejected("repair_scope_too_large")
    return operations


def apply_elastic_repair_pack(
    canonical: str,
    payload: Any,
    *,
    target: int,
    ending_hook: str,
    source_context: str = "",
    user_task: str = "",
    protected_fact_anchors: Sequence[str] = (),
) -> Dict[str, Any]:
    """Apply one atomic repair pack or return the untouched complete chapter."""

    base = str(canonical or "").strip()
    base_count = count_story_text_words(base)
    common = {
        "canonicalText": base,
        "canonicalWordCount": base_count,
        "lengthControlStrategy": ELASTIC_LENGTH_CONTROL_STRATEGY,
    }
    try:
        operations = _normalize_repair_operations(base, payload)
        candidate = _apply_edits(base, operations)
    except (ElasticManuscriptRejected, TypeError) as exc:
        rejection_reason = (
            str(exc.reason)
            if isinstance(exc, ElasticManuscriptRejected)
            else type(exc).__name__
        )
        return {
            **common,
            "text": base,
            "finalWordCount": base_count,
            "accepted": False,
            "qualityPassed": False,
            "selectedEditIds": [],
            "rejectionReasons": [rejection_reason],
            "lengthFallbackReason": FALLBACK_REPAIR_FAILED,
        }

    quality_passed = (
        (not ending_hook or candidate.rstrip().endswith(str(ending_hook)))
        and all(str(anchor) in candidate for anchor in protected_fact_anchors if str(anchor))
        and not _quality_regressed(
            candidate,
            base,
            source_context=source_context,
            user_task=user_task,
        )
    )
    candidate_count = count_story_text_words(candidate)
    precision_band = chapter_precision_band(target)
    normal_band = chapter_normal_band(target)
    precision_achieved = band_distance(candidate_count, precision_band) == 0
    improved_enough = (
        band_distance(base_count, precision_band) > 0
        and band_distance(candidate_count, precision_band)
        <= band_distance(base_count, precision_band)
        * (1.0 - WIDE_RECOVERY_MINIMUM_IMPROVEMENT)
    )
    normal_passed = band_distance(candidate_count, normal_band) == 0
    accepted = quality_passed and (
        precision_achieved or (normal_passed and improved_enough)
    )
    if not quality_passed:
        reason = FALLBACK_REPAIR_QUALITY_REJECTED
    elif not accepted:
        reason = FALLBACK_REPAIR_OUTSIDE_BAND
    else:
        reason = "repair_in_band" if precision_achieved else "repair_normal_recovery"
    final_text = candidate if accepted else base
    final_count = candidate_count if accepted else base_count
    return {
        **common,
        "text": final_text,
        "candidateText": candidate,
        "candidateWordCount": candidate_count,
        "finalWordCount": final_count,
        "accepted": accepted,
        "qualityPassed": quality_passed,
        "normalBandPassed": band_distance(final_count, normal_band) == 0,
        "precisionAchieved": band_distance(final_count, precision_band) == 0,
        "selectedEditIds": (
            sorted(str(item["id"]) for item in operations) if accepted else []
        ),
        "lengthFallbackReason": reason,
    }
