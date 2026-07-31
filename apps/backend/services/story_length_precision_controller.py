"""The optional second prose call: one bounded length revision.

This is the controller the pipeline reaches for when precision is on and the
draft landed outside the ±10% band (plan §7.1). Everything about it is shaped by
one rule: the second call must be a *gap-sized* call, not a second chapter.

That rule produces the three constraints implemented here:

* **Scope** — a recoverable gap uses ``StorydexSubmitLengthPatch``; a draft
  beyond that corridor uses one feedback-calibrated whole-chapter redraft.
  Both are structured tool calls and both are validated locally.
* **Context** (§7.3) — the prompt carries the draft with paragraph IDs, the
  program's own measurement and the direction to move. It does not replay the
  Agent conversation, tool logs, memory or project files: the model is deciding
  where to add or trim, not re-deriving the chapter.
* **Budget** (§9) — a patch is capped by its small operation envelope, while a
  redraft is capped from the precision-band maximum plus JSON overhead. Both get
  zero transport retries.

The model is never asked to count characters. It is told which direction to move
and roughly how much; the authoritative count stays with the program, which is
why a miss here degrades to "keep the draft" instead of a retry loop.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from typing import Any, Awaitable, Callable, Dict, List

from services.coomi_agent_service import StorydexToolCallRejected
from services.story_length_patch_service import (
    LENGTH_PATCH_TOOL_NAME,
    MAXIMUM_DELETED_PARAGRAPHS,
    MAXIMUM_OPERATIONS,
    annotate_draft_paragraphs,
    length_patch_tool_schema,
    maximum_local_compression_characters,
    revise_draft_with_patch,
)
from services.story_prose_quality import (
    contextual_quality_issues,
    normalized_character_ngrams,
)
from services.story_word_count_service import (
    STORY_WORD_COUNT_RULE,
    chapter_precision_band,
    count_story_text_words,
)

REVISION_UNAVAILABLE_NO_TOOL_SUPPORT = "provider_without_tool_support"
REVISION_UNAVAILABLE_TIMEOUT = "revision_deadline_exceeded"
REVISION_UNAVAILABLE_TRANSPORT = "revision_transport_error"
REVISION_UNAVAILABLE_EMPTY = "revision_returned_nothing"
REVISION_INVALID_PATCH = "tool_arguments_invalid_patch"
REVISION_INVALID_REDRAFT = "tool_arguments_invalid_redraft"
REDRAFT_OUTSIDE_PRECISION_BAND = "redraft_outside_precision_band"

LOCAL_PATCH_STRATEGY = "structured_patch_v1"
FEEDBACK_BOUNDED_REDRAFT_STRATEGY = "feedback_bounded_redraft"
BOUNDED_REDRAFT_TOOL_NAME = "StorydexSubmitBoundedRedraft"
BOUNDED_REDRAFT_SCHEMA_VERSION = 1

# Plan §9. These are internal budget bounds, deliberately not user settings:
# they are derived from the draft that just ran, so a slow Provider gets a
# proportionally longer revision window without anyone tuning a number.
_REVISION_TOKEN_RATIO = 0.50
_REVISION_TOKEN_MINIMUM = 512
_REVISION_TOKEN_MAXIMUM = 2048
_REVISION_GAP_TOKEN_RATIO = 1.50
_REVISION_JSON_OVERHEAD_TOKENS = 256
_REVISION_DEADLINE_RATIO = 0.60
_REVISION_DEADLINE_MINIMUM_SECONDS = 20
_REVISION_DEADLINE_MAXIMUM_SECONDS = 60
_REDRAFT_TOKEN_MINIMUM = 1024
_REDRAFT_TOKEN_MAXIMUM = 32768
_REDRAFT_BODY_TOKEN_RATIO = 1.50
_REDRAFT_JSON_OVERHEAD_TOKENS = 384
_REDRAFT_JSON_PARAGRAPH_OVERHEAD_TOKENS = 16
_REDRAFT_PARAGRAPH_RADIUS = 2
_REDRAFT_MINIMUM_CONTINUITY_RETENTION = 0.30
_REDRAFT_CONTINUITY_RATIO = 0.60
_REDRAFT_MAXIMUM_CONTINUITY_RETENTION = 0.65
_REDRAFT_MINIMUM_ENDING_HOOK_RETENTION = 0.35
# A revision never retries transport (§9). One precise correction that retries
# is no longer a bounded second call.
REVISION_TRANSPORT_RETRIES = 0


def revision_budget(
    *,
    draft_completion_tokens: int = 0,
    draft_duration_ms: int = 0,
    budget_policy: Dict[str, Any] | None = None,
    required_character_delta: int = 0,
    maximum_patch_text_characters: int | None = None,
) -> Dict[str, Any]:
    """Return the token and deadline budget for one revision call (§9)."""

    character_delta = max(0, int(required_character_delta or 0))
    maximum_patch_text = (
        None
        if maximum_patch_text_characters is None
        else max(0, int(maximum_patch_text_characters))
    )
    if maximum_patch_text is not None:
        tokens = (
            int(round(maximum_patch_text * _REVISION_GAP_TOKEN_RATIO))
            + _REVISION_JSON_OVERHEAD_TOKENS
        )
    else:
        draft_relative_tokens = int(
            round(max(0, int(draft_completion_tokens)) * _REVISION_TOKEN_RATIO)
        )
        gap_relative_tokens = (
            int(round(character_delta * _REVISION_GAP_TOKEN_RATIO))
            + _REVISION_JSON_OVERHEAD_TOKENS
            if character_delta > 0
            else 0
        )
        tokens = max(draft_relative_tokens, gap_relative_tokens)
    completion_tokens = min(
        _REVISION_TOKEN_MAXIMUM, max(_REVISION_TOKEN_MINIMUM, tokens)
    )
    policy = budget_policy if isinstance(budget_policy, dict) else {}
    try:
        requested_ratio = float(policy.get("deadlineRatio") or _REVISION_DEADLINE_RATIO)
    except (TypeError, ValueError, OverflowError):
        requested_ratio = _REVISION_DEADLINE_RATIO
    deadline_ratio = min(2.0, max(_REVISION_DEADLINE_RATIO, requested_ratio))
    try:
        requested_minimum = int(
            policy.get("deadlineMinimumSeconds") or _REVISION_DEADLINE_MINIMUM_SECONDS
        )
    except (TypeError, ValueError, OverflowError):
        requested_minimum = _REVISION_DEADLINE_MINIMUM_SECONDS
    try:
        requested_maximum = int(
            policy.get("deadlineMaximumSeconds") or _REVISION_DEADLINE_MAXIMUM_SECONDS
        )
    except (TypeError, ValueError, OverflowError):
        requested_maximum = _REVISION_DEADLINE_MAXIMUM_SECONDS
    deadline_minimum = min(
        _REVISION_DEADLINE_MAXIMUM_SECONDS,
        max(_REVISION_DEADLINE_MINIMUM_SECONDS, requested_minimum),
    )
    deadline_maximum = min(
        _REVISION_DEADLINE_MAXIMUM_SECONDS,
        max(deadline_minimum, requested_maximum),
    )
    seconds = int(round(max(0, int(draft_duration_ms)) / 1000.0 * deadline_ratio))
    deadline_seconds = min(
        deadline_maximum,
        max(deadline_minimum, seconds),
    )
    result = {
        "maxCompletionTokens": completion_tokens,
        "requiredCharacterDelta": character_delta,
        "deadlineSeconds": deadline_seconds,
        "deadlinePolicy": str(policy.get("name") or "draft_relative_default"),
        "transportRetries": REVISION_TRANSPORT_RETRIES,
    }
    if maximum_patch_text is not None:
        result["maximumPatchTextCharacters"] = maximum_patch_text
    return result


def _fragment_paragraphs(draft_payload: Dict[str, Any]) -> List[str]:
    paragraphs: List[str] = []
    for item in list(draft_payload.get("fragments") or []):
        text = (
            str(item.get("text") or item.get("content") or "")
            if isinstance(item, dict)
            else str(item or "")
        )
        paragraphs.extend(
            paragraph.strip()
            for paragraph in re.split(r"\n\s*\n", text)
            if paragraph.strip()
        )
    return paragraphs


def redraft_calibration(
    *,
    draft_payload: Dict[str, Any],
    draft_word_count: int,
    target: int,
    retained_word_count: int = 0,
    draft_generated_word_count: int | None = None,
) -> Dict[str, Any]:
    """Derive one redraft target from Storydex's measured first draft."""

    measured_count = max(1, int(draft_word_count or 0) or 1)
    target_count = max(1, int(target or 0) or 1)
    retained_count = max(0, int(retained_word_count or 0))
    paragraphs = _fragment_paragraphs(draft_payload)
    paragraph_count = max(1, len(paragraphs))
    generated_count = (
        max(1, int(draft_generated_word_count))
        if draft_generated_word_count is not None
        else max(1, measured_count - retained_count)
    )
    average_density = generated_count / paragraph_count
    target_generated_count = max(1, target_count - retained_count)
    suggested_count = max(1, int(round(target_generated_count / average_density)))
    fragment_count = max(1, len(list(draft_payload.get("fragments") or [])))
    paragraph_minimum = max(
        fragment_count,
        suggested_count - _REDRAFT_PARAGRAPH_RADIUS,
    )
    paragraph_maximum = max(
        paragraph_minimum,
        suggested_count + _REDRAFT_PARAGRAPH_RADIUS,
    )
    minimum_paragraphs_to_merge_or_remove = max(
        0,
        paragraph_count - paragraph_maximum,
    )
    precision_minimum, precision_maximum = chapter_precision_band(target_count)
    return {
        "measuredDraftWordCount": measured_count,
        "targetWordCount": target_count,
        "precisionBand": [precision_minimum, precision_maximum],
        "targetToDraftRatio": round(target_count / measured_count, 4),
        "draftParagraphCount": paragraph_count,
        "averageParagraphWordCount": round(average_density, 2),
        "suggestedParagraphCount": suggested_count,
        "suggestedParagraphRange": [paragraph_minimum, paragraph_maximum],
        "minimumParagraphsToMergeOrRemove": minimum_paragraphs_to_merge_or_remove,
        "retainedWordCount": retained_count,
        "targetGeneratedWordCount": target_generated_count,
    }


def bounded_redraft_tool_schema(calibration: Dict[str, Any]) -> Dict[str, Any]:
    paragraph_range = list(calibration.get("suggestedParagraphRange") or [1, 1])
    minimum = max(1, int(paragraph_range[0] if paragraph_range else 1))
    maximum = max(
        minimum,
        int(paragraph_range[1] if len(paragraph_range) > 1 else minimum),
    )
    return {
        "name": BOUNDED_REDRAFT_TOOL_NAME,
        "description": "Submit one feedback-calibrated whole-chapter redraft.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "version": {
                    "type": "integer",
                    "enum": [BOUNDED_REDRAFT_SCHEMA_VERSION],
                },
                "strategy": {
                    "type": "string",
                    "enum": [FEEDBACK_BOUNDED_REDRAFT_STRATEGY],
                },
                "paragraphs": {
                    "type": "array",
                    "minItems": minimum,
                    "maxItems": maximum,
                    "description": (
                        "One natural paragraph per item; merge or omit source "
                        "paragraphs and do not preserve one output item per source paragraph."
                    ),
                    "items": {"type": "string", "minLength": 1},
                },
            },
            "required": ["version", "strategy", "paragraphs"],
        },
    }


def build_bounded_redraft_messages(
    *,
    draft_payload: Dict[str, Any],
    calibration: Dict[str, Any],
    direction: str,
    chapter_context: str = "",
) -> list[Dict[str, str]]:
    paragraph_minimum, paragraph_maximum = list(
        calibration["suggestedParagraphRange"]
    )
    draft_paragraph_count = max(
        1,
        int(calibration.get("draftParagraphCount") or 1),
    )
    minimum_paragraphs_to_merge_or_remove = max(
        0,
        int(calibration.get("minimumParagraphsToMergeOrRemove") or 0),
    )
    if str(direction or "").strip().lower() == "compress":
        paragraph_instruction = (
            f"首稿共 {draft_paragraph_count} 段，最终必须只提交 "
            f"{paragraph_minimum}-{paragraph_maximum} 段；"
            f"至少合并或删除 {minimum_paragraphs_to_merge_or_remove} 个首稿段落边界，"
            "不要逐段缩写后仍保留大多数原段落。"
        )
    else:
        paragraph_instruction = (
            f"首稿共 {draft_paragraph_count} 段，最终必须提交 "
            f"{paragraph_minimum}-{paragraph_maximum} 段；"
            "新增内容应合并到完整叙事段落中，不要用碎段填充。"
        )
    system = (
        "你是中文长篇小说的修订编辑。你只能通过调用 "
        f"{BOUNDED_REDRAFT_TOOL_NAME} 提交一次受约束的整章重写，不要直接输出正文。\n"
        "必须保留首稿的剧情节点、已确认事实、人物决定、叙事视角和结尾钩子；"
        "优先压缩重复描写、同义复述和冗余过渡，不得机械截断、填充或谈论字数。\n"
        f"{paragraph_instruction}\n"
        "不要逐段追逐硬字符数。\n"
        f"计数规则（由程序执行，你不需要自己计算）：{STORY_WORD_COUNT_RULE}。"
    )
    user_payload: Dict[str, Any] = {
        "strategy": FEEDBACK_BOUNDED_REDRAFT_STRATEGY,
        "direction": str(direction or "").strip().lower(),
        **dict(calibration),
        "draft": annotate_draft_paragraphs(
            list(draft_payload.get("fragments") or [])
        ),
        "preservationRequirements": [
            "plot_nodes",
            "facts",
            "character_decisions",
            "narrative_perspective",
            "ending_hook",
        ],
    }
    if chapter_context.strip():
        user_payload["chapterAnchors"] = chapter_context.strip()
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False, indent=2),
        },
    ]


def bounded_redraft_budget(
    *,
    calibration: Dict[str, Any],
    draft_completion_tokens: int = 0,
    draft_duration_ms: int = 0,
    budget_policy: Dict[str, Any] | None = None,
    required_character_delta: int = 0,
) -> Dict[str, Any]:
    base = revision_budget(
        draft_completion_tokens=draft_completion_tokens,
        draft_duration_ms=draft_duration_ms,
        budget_policy=budget_policy,
        required_character_delta=required_character_delta,
    )
    precision_maximum = int(list(calibration["precisionBand"])[1])
    retained_count = max(0, int(calibration.get("retainedWordCount") or 0))
    maximum_body_characters = max(1, precision_maximum - retained_count)
    paragraph_maximum = int(list(calibration["suggestedParagraphRange"])[1])
    tokens = (
        int(math.ceil(maximum_body_characters * _REDRAFT_BODY_TOKEN_RATIO))
        + _REDRAFT_JSON_OVERHEAD_TOKENS
        + paragraph_maximum * _REDRAFT_JSON_PARAGRAPH_OVERHEAD_TOKENS
    )
    base.update(
        {
            "strategy": FEEDBACK_BOUNDED_REDRAFT_STRATEGY,
            "maxCompletionTokens": min(
                _REDRAFT_TOKEN_MAXIMUM,
                max(_REDRAFT_TOKEN_MINIMUM, tokens),
            ),
            "maximumBodyCharacters": maximum_body_characters,
            "paragraphRange": list(calibration["suggestedParagraphRange"]),
        }
    )
    return base


def _parse_bounded_redraft(raw: Any) -> List[str]:
    payload = raw
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError("redraft_not_json") from exc
    if not isinstance(payload, dict):
        raise ValueError("redraft_not_object")
    if int(payload.get("version") or 0) != BOUNDED_REDRAFT_SCHEMA_VERSION:
        raise ValueError("redraft_version_mismatch")
    if str(payload.get("strategy") or "").strip() != FEEDBACK_BOUNDED_REDRAFT_STRATEGY:
        raise ValueError("redraft_strategy_mismatch")
    paragraphs = payload.get("paragraphs")
    if not isinstance(paragraphs, list) or not paragraphs:
        raise ValueError("redraft_without_paragraphs")
    cleaned = [str(item or "").strip() for item in paragraphs]
    if any(not item for item in cleaned):
        raise ValueError("redraft_empty_paragraph")
    if any(re.search(r"\n\s*\n", item) for item in cleaned):
        raise ValueError("redraft_paragraph_contains_break")
    return cleaned


def _distribute_paragraphs(
    paragraphs: List[str],
    *,
    fragment_count: int,
) -> List[List[str]]:
    count = max(1, int(fragment_count or 0) or 1)
    if len(paragraphs) < count:
        return []
    groups: List[List[str]] = []
    start = 0
    for index in range(count):
        remaining_paragraphs = len(paragraphs) - start
        remaining_fragments = count - index
        take = int(math.ceil(remaining_paragraphs / remaining_fragments))
        groups.append(paragraphs[start : start + take])
        start += take
    return groups


def _redraft_fragments(
    draft_payload: Dict[str, Any],
    paragraphs: List[str],
) -> List[Dict[str, Any]]:
    source_fragments = list(draft_payload.get("fragments") or [])
    groups = _distribute_paragraphs(
        paragraphs,
        fragment_count=len(source_fragments),
    )
    if not groups or len(groups) != len(source_fragments):
        return []
    result: List[Dict[str, Any]] = []
    for source, group in zip(source_fragments, groups):
        entry = dict(source) if isinstance(source, dict) else {}
        entry["text"] = "\n\n".join(group)
        entry.pop("content", None)
        result.append(entry)
    return result


def _context_anchors(source_context: str, draft_text: str) -> List[str]:
    anchors: List[str] = []
    for raw_line in str(source_context or "").splitlines():
        anchor = re.sub(
            r"^\s*(?:#{1,6}\s*|[-*+]\s+|\d+[.)]\s+)",
            "",
            raw_line,
        ).strip()
        if 4 <= len(anchor) <= 200 and anchor in draft_text:
            anchors.append(anchor)
    return anchors


def bounded_redraft_quality_issues(
    *,
    draft_payload: Dict[str, Any],
    candidate_fragments: List[Any],
    target: int,
    retained_word_count: int = 0,
    source_context: str = "",
    user_task: str = "",
) -> List[str]:
    draft_paragraphs = _fragment_paragraphs(draft_payload)
    candidate_paragraphs = _fragment_paragraphs({"fragments": candidate_fragments})
    draft_text = "\n\n".join(draft_paragraphs)
    candidate_text = "\n\n".join(candidate_paragraphs)
    quality_context = "\n\n".join(
        item for item in (str(source_context or "").strip(), draft_text) if item
    )
    issues = contextual_quality_issues(
        candidate_text,
        source_context=quality_context,
        user_task=user_task,
    )
    if len(candidate_fragments) != len(list(draft_payload.get("fragments") or [])):
        issues.append("fragment_count_changed")
    candidate_generated_count = count_story_text_words(candidate_text)
    final_count = max(0, int(retained_word_count or 0)) + candidate_generated_count
    precision_minimum, precision_maximum = chapter_precision_band(target)
    if not precision_minimum <= final_count <= precision_maximum:
        issues.append(REDRAFT_OUTSIDE_PRECISION_BAND)
    for anchor in _context_anchors(source_context, draft_text):
        if anchor not in candidate_text:
            issues.append("redraft_removed_context_anchor")
            break
    draft_ngrams = normalized_character_ngrams(draft_text)
    candidate_ngrams = normalized_character_ngrams(candidate_text)
    if draft_ngrams:
        retention = len(draft_ngrams & candidate_ngrams) / len(draft_ngrams)
        expected_retention = min(
            _REDRAFT_MAXIMUM_CONTINUITY_RETENTION,
            max(
                _REDRAFT_MINIMUM_CONTINUITY_RETENTION,
                (candidate_generated_count / max(1, count_story_text_words(draft_text)))
                * _REDRAFT_CONTINUITY_RATIO,
            ),
        )
        if retention < expected_retention:
            issues.append("redraft_continuity_too_low")
    if draft_paragraphs and candidate_paragraphs:
        hook_ngrams = normalized_character_ngrams(draft_paragraphs[-1])
        candidate_hook_ngrams = normalized_character_ngrams(candidate_paragraphs[-1])
        if hook_ngrams:
            hook_retention = len(hook_ngrams & candidate_hook_ngrams) / len(hook_ngrams)
            if hook_retention < _REDRAFT_MINIMUM_ENDING_HOOK_RETENTION:
                issues.append("redraft_ending_hook_changed")
    return list(dict.fromkeys(issues))


def revise_draft_with_bounded_redraft(
    *,
    draft_payload: Dict[str, Any],
    raw: Any,
    calibration: Dict[str, Any],
    target: int,
    source_context: str = "",
    user_task: str = "",
) -> Dict[str, Any]:
    try:
        paragraphs = _parse_bounded_redraft(raw)
    except (TypeError, ValueError) as exc:
        reason = str(exc) or REVISION_INVALID_REDRAFT
        return {
            **_rejected(REVISION_INVALID_REDRAFT),
            "redraftRejectionReason": reason,
            "strategy": FEEDBACK_BOUNDED_REDRAFT_STRATEGY,
        }
    suggested_paragraph_range = list(calibration["suggestedParagraphRange"])
    paragraph_minimum, paragraph_maximum = suggested_paragraph_range
    paragraph_range_adhered = (
        paragraph_minimum <= len(paragraphs) <= paragraph_maximum
    )
    fragments = _redraft_fragments(draft_payload, paragraphs)
    if not fragments:
        return {
            **_rejected(REVISION_INVALID_REDRAFT),
            "redraftRejectionReason": "redraft_fragment_shape_invalid",
            "strategy": FEEDBACK_BOUNDED_REDRAFT_STRATEGY,
            "redraftParagraphCount": len(paragraphs),
            "suggestedRedraftParagraphRange": suggested_paragraph_range,
            "redraftParagraphRangeAdhered": paragraph_range_adhered,
        }
    issues = bounded_redraft_quality_issues(
        draft_payload=draft_payload,
        candidate_fragments=fragments,
        target=target,
        retained_word_count=int(calibration.get("retainedWordCount") or 0),
        source_context=source_context,
        user_task=user_task,
    )
    candidate = dict(draft_payload)
    candidate["fragments"] = fragments
    candidate["qualityPassed"] = not issues
    candidate["qualityIssues"] = issues
    candidate["strategy"] = FEEDBACK_BOUNDED_REDRAFT_STRATEGY
    candidate["redraftParagraphCount"] = len(paragraphs)
    candidate["suggestedRedraftParagraphRange"] = suggested_paragraph_range
    candidate["redraftParagraphRangeAdhered"] = paragraph_range_adhered
    candidate["redraftWordCount"] = int(calibration.get("retainedWordCount") or 0) + sum(
        count_story_text_words(paragraph) for paragraph in paragraphs
    )
    return candidate


def maximum_local_expansion_characters() -> int:
    """Return the largest prose gap the capped insert protocol can serialize."""

    usable_tokens = max(
        0,
        _REVISION_TOKEN_MAXIMUM - _REVISION_JSON_OVERHEAD_TOKENS,
    )
    return int(math.floor(usable_tokens / _REVISION_GAP_TOKEN_RATIO))


def revision_strategy(request: Dict[str, Any]) -> str:
    """Select the one permitted second-call protocol for this measured draft."""

    direction = str(request.get("direction") or "").strip().lower()
    draft_payload = (
        request.get("draftPayload")
        if isinstance(request.get("draftPayload"), dict)
        else {}
    )
    if direction not in {"expand", "compress"} or not draft_payload.get("fragments"):
        return LOCAL_PATCH_STRATEGY
    target = max(1, int(request.get("target") or 0) or 1)
    precision_minimum, precision_maximum = chapter_precision_band(target)
    draft_word_count = int(request.get("draftWordCount") or 0)
    if direction == "expand":
        required_character_delta = max(0, precision_minimum - draft_word_count)
        if required_character_delta > maximum_local_expansion_characters():
            return FEEDBACK_BOUNDED_REDRAFT_STRATEGY
        return LOCAL_PATCH_STRATEGY
    required_character_delta = max(0, draft_word_count - precision_maximum)
    maximum_local_compression = maximum_local_compression_characters(draft_payload)
    if required_character_delta > maximum_local_compression:
        return FEEDBACK_BOUNDED_REDRAFT_STRATEGY
    return LOCAL_PATCH_STRATEGY


def _expansion_guidance(direction: str) -> str:
    if direction == "expand":
        return (
            "在不改变既定剧情计划、不新增无关支线、不重复已有信息的前提下，"
            "补充与本轮核心事件直接相关的动作后果、角色下一步决定和必要的场景收束。"
        )
    return (
        "在保留全部既有事实、因果和角色决定的前提下，"
        "删去重复核验、冗余环境描写和同义复述。"
    )


def build_revision_messages(
    *,
    draft_payload: Dict[str, Any],
    draft_word_count: int,
    target: int,
    direction: str,
    chapter_context: str = "",
) -> list[Dict[str, str]]:
    """Build the second call's prompt (§7.3): draft, measurement, direction.

    The measurement is stated as a direction and an approximate magnitude rather
    than an exact arithmetic instruction. Asking a model to hit a character count
    reliably produces text *about* word counts, which is why
    ``length_meta_language`` is a rejection reason in the quality gate.
    """

    normalized_direction = str(direction or "").strip().lower()
    minimum, maximum = chapter_precision_band(target)
    annotated = annotate_draft_paragraphs(list(draft_payload.get("fragments") or []))
    if normalized_direction == "expand":
        magnitude = max(0, minimum - int(draft_word_count))
        instruction = (
            f"这一章目前略短，需要在保持结构不变的前提下增加约 {magnitude} 字的正文。"
        )
    else:
        magnitude = max(0, int(draft_word_count) - maximum)
        instruction = (
            f"这一章目前略长，需要在保持结构不变的前提下精简约 {magnitude} 字的正文。"
        )

    system = (
        "你是中文长篇小说的修订编辑。你只能通过调用 "
        f"{LENGTH_PATCH_TOOL_NAME} 提交一次局部长度补丁，不要直接输出正文。\n"
        "补丁只调整长度：不得改动文件路径、章节标题、章节编号、摘要、WIKI 或变量，"
        "也不得改变已经写定的剧情走向。\n"
        f"计数规则（由程序执行，你不需要自己计算）：{STORY_WORD_COUNT_RULE}。"
    )
    if normalized_direction == "compress":
        system += (
            "不得重写整章；优先删除完整的冗余自然段，只提交冗余段落 ID，"
            "不得返回任何替换正文。"
        )
    user_payload = {
        "direction": normalized_direction,
        "instruction": instruction,
        "expansionDirections": _expansion_guidance(normalized_direction),
        "draft": annotated,
    }
    if normalized_direction == "compress":
        user_payload["patchConstraints"] = {
            "maximumOperations": MAXIMUM_OPERATIONS,
            "maximumDeletedParagraphs": MAXIMUM_DELETED_PARAGRAPHS,
            "maximumReplacementCharacters": 0,
        }
    if chapter_context.strip():
        user_payload["chapterAnchors"] = chapter_context.strip()
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False, indent=2),
        },
    ]


def _rejected(reason: str) -> Dict[str, Any]:
    return {
        "fragments": [],
        "qualityPassed": False,
        "rejectedReason": reason,
        "qualityIssues": [reason],
    }


class StoryLengthPrecisionController:
    """Run one length revision call and return a candidate, or keep the draft."""

    async def revise(
        self,
        request: Dict[str, Any],
        *,
        call_provider: Callable[..., Awaitable[Any]],
        chapter_context: str = "",
        user_task: str = "",
        draft_completion_tokens: int = 0,
        draft_duration_ms: int = 0,
        budget_policy: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Ask for one bounded revision and validate it locally.

        ``request`` is the pipeline's revision request (draft payload, measured
        count, target and direction). ``call_provider`` performs the single
        Provider request; transport lives outside this controller so the call
        contract can be tested without a network.

        Every failure path returns a rejected candidate rather than raising: the
        draft is still a complete chapter, and the plan's failure matrix (§8.2)
        says a revision timeout or invalid candidate keeps it without retrying.
        """

        direction = str(request.get("direction") or "").strip().lower()
        draft_payload = (
            request.get("draftPayload") if isinstance(request.get("draftPayload"), dict) else {}
        )
        if direction not in {"expand", "compress"} or not draft_payload.get("fragments"):
            return _rejected(REVISION_UNAVAILABLE_EMPTY)

        target = max(1, int(request.get("target") or 0) or 1)
        precision_minimum, precision_maximum = chapter_precision_band(target)
        draft_word_count = int(request.get("draftWordCount") or 0)
        required_character_delta = (
            max(0, precision_minimum - draft_word_count)
            if direction == "expand"
            else max(0, draft_word_count - precision_maximum)
        )
        strategy = revision_strategy(request)
        outside_local_patch_corridor = (
            strategy == FEEDBACK_BOUNDED_REDRAFT_STRATEGY
        )
        if outside_local_patch_corridor:
            calibration = redraft_calibration(
                draft_payload=draft_payload,
                draft_word_count=draft_word_count,
                target=target,
                retained_word_count=int(request.get("retainedWordCount") or 0),
                draft_generated_word_count=(
                    int(request["draftGeneratedWordCount"])
                    if request.get("draftGeneratedWordCount") is not None
                    else None
                ),
            )
            budget = bounded_redraft_budget(
                calibration=calibration,
                draft_completion_tokens=draft_completion_tokens,
                draft_duration_ms=draft_duration_ms,
                budget_policy=budget_policy,
                required_character_delta=required_character_delta,
            )
            messages = build_bounded_redraft_messages(
                draft_payload=draft_payload,
                calibration=calibration,
                direction=direction,
                chapter_context=chapter_context,
            )
            schema = bounded_redraft_tool_schema(calibration)
        else:
            budget = revision_budget(
                draft_completion_tokens=draft_completion_tokens,
                draft_duration_ms=draft_duration_ms,
                budget_policy=budget_policy,
                required_character_delta=required_character_delta,
                maximum_patch_text_characters=(
                    0 if direction == "compress" else None
                ),
            )
            messages = build_revision_messages(
                draft_payload=draft_payload,
                draft_word_count=int(request.get("draftWordCount") or 0),
                target=target,
                direction=direction,
                chapter_context=chapter_context,
            )
            schema = length_patch_tool_schema(
                direction,
                draft_payload=draft_payload,
            )

        try:
            raw = await asyncio.wait_for(
                call_provider(
                    messages=messages,
                    tool=schema,
                    max_completion_tokens=budget["maxCompletionTokens"],
                ),
                timeout=budget["deadlineSeconds"],
            )
        except asyncio.TimeoutError:
            return {
                **_rejected(REVISION_UNAVAILABLE_TIMEOUT),
                "budget": budget,
                "strategy": strategy,
            }
        except NotImplementedError:
            # A Provider without tool support is reported unavailable rather
            # than downgraded to free-text rewriting (§7.2).
            return {
                **_rejected(REVISION_UNAVAILABLE_NO_TOOL_SUPPORT),
                "budget": budget,
                "strategy": strategy,
            }
        except StorydexToolCallRejected as exc:
            return {
                **_rejected(exc.reason),
                "budget": budget,
                "strategy": strategy,
            }
        except Exception as exc:  # noqa: BLE001 - any transport failure keeps the draft
            return {
                **_rejected(REVISION_UNAVAILABLE_TRANSPORT),
                "budget": budget,
                "strategy": strategy,
                "errorType": type(exc).__name__,
            }

        if not raw:
            return {
                **_rejected(REVISION_UNAVAILABLE_EMPTY),
                "budget": budget,
                "strategy": strategy,
            }

        if outside_local_patch_corridor:
            candidate = revise_draft_with_bounded_redraft(
                draft_payload=draft_payload,
                raw=raw,
                calibration=calibration,
                target=target,
                source_context=chapter_context,
                user_task=user_task,
            )
            candidate["budget"] = budget
            candidate["strategy"] = strategy
            return candidate

        candidate = revise_draft_with_patch(
            draft_payload=draft_payload,
            patch=raw,
            direction=direction,
            source_context=chapter_context,
            user_task=user_task,
        )
        patch_rejection = str(candidate.get("rejectedReason") or "")
        if patch_rejection:
            candidate["patchRejectionReason"] = patch_rejection
            candidate["rejectedReason"] = REVISION_INVALID_PATCH
            candidate["qualityIssues"] = [
                REVISION_INVALID_PATCH,
                *[
                    str(item)
                    for item in list(candidate.get("qualityIssues") or [])
                    if str(item) != REVISION_INVALID_PATCH
                ],
            ]
        candidate["budget"] = budget
        candidate["strategy"] = strategy
        return candidate


_CONTROLLER: StoryLengthPrecisionController | None = None


def get_story_length_precision_controller() -> StoryLengthPrecisionController:
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = StoryLengthPrecisionController()
    return _CONTROLLER
