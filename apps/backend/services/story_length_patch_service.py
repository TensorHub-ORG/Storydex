"""Local application and validation of one precision length patch (plan §7.2–§7.4).

The precision path spends at most one extra prose call, and that call must be a
*gap-sized* call rather than "write the chapter again". Regenerating 3000
characters costs nearly as much as the draft and rewrites vocabulary, syntax and
narrative distance that were already fine; a patch touches only what the gap
needs and leaves the rest of the draft byte-identical.

So the second call returns operations, not prose:

* ``expand`` may only ``insert_after`` an existing paragraph. Original
  paragraphs are never rewritten, which is what keeps the draft's voice.
* ``compress`` may only ``delete_paragraphs`` by stable ID. It returns no prose,
  and the first and last paragraph are protected so the chapter keeps its
  opening and its exit hook.

Paragraph IDs are assigned here, sent to the model, and dropped again on the way
back — they exist to anchor operations, never to reach the chapter file.

Validation is all-or-nothing on purpose. A partially applied patch is a chapter
no one wrote: half the model's plan against half the draft's structure. When any
check fails the caller keeps the draft, which is the quality insurance the whole
mechanism depends on.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

from services.story_prose_quality import (
    clean_generated_text,
    contextual_quality_issues,
    duplicate_paragraph_count,
    normalized_character_ngrams,
    repeated_ngram_count,
)
from services.story_word_count_service import count_story_text_words

LENGTH_PATCH_TOOL_NAME = "StorydexSubmitLengthPatch"
LENGTH_PATCH_SCHEMA_VERSION = 1

DIRECTION_EXPAND = "expand"
DIRECTION_COMPRESS = "compress"
_DIRECTIONS = (DIRECTION_EXPAND, DIRECTION_COMPRESS)

OP_INSERT_AFTER = "insert_after"
OP_DELETE_PARAGRAPHS = "delete_paragraphs"
# Each direction gets exactly one legal op. Allowing compress to insert (or
# expand to replace) would let a "length patch" quietly become a rewrite.
_OPS_BY_DIRECTION = {
    DIRECTION_EXPAND: OP_INSERT_AFTER,
    DIRECTION_COMPRESS: OP_DELETE_PARAGRAPHS,
}

MAXIMUM_OPERATIONS = 3
MAXIMUM_DELETED_PARAGRAPHS = 8
# An expansion that more than doubles the draft is not filling a gap.
_MAXIMUM_EXPANSION_RATIO = 1.0
# Compression that keeps under 85% of the draft's 2-grams has stopped trimming
# and started paraphrasing (plan §7.4 item 9).
COMPRESSION_MINIMUM_NGRAM_RETENTION = 0.85

# Plan §7.4 splits the quality checks in two. Item 6 (wrappers, placeholders,
# word-count asides, truncated sentences) is absolute: those are defects however
# the draft looked. Item 7's two repetition checks are comparative — they ask
# whether the patch made the chapter *worse*. Judging repetition absolutely on
# the candidate would make a draft that already repeats itself permanently
# unrevisable, which is the opposite of what a length patch is for.
_COMPARATIVE_ISSUES = frozenset({"repeated_ngram", "duplicate_paragraph"})


class LengthPatchRejected(RuntimeError):
    """Raised when a patch fails validation and the draft must be kept."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = str(reason)


def _split_paragraphs(text: str) -> List[str]:
    parts: List[str] = []
    for block in str(text or "").split("\n\n"):
        if block.strip():
            parts.append(block.strip())
    return parts


def paragraph_id(fragment_order: int, paragraph_index: int) -> str:
    return f"f{int(fragment_order)}-p{int(paragraph_index):02d}"


def annotate_draft_paragraphs(fragments: List[Any]) -> Dict[str, Any]:
    """Give every draft paragraph a stable ID for the revision prompt.

    The IDs are what let the second call address "after this paragraph" without
    resending the chapter as one opaque blob, and they are stripped before the
    candidate is ever counted or written.
    """

    annotated: List[Dict[str, Any]] = []
    for index, item in enumerate(fragments, start=1):
        text = (
            str(item.get("text") or item.get("content") or "")
            if isinstance(item, dict)
            else str(item or "")
        )
        paragraphs = _split_paragraphs(text)
        annotated.append(
            {
                "fragmentOrder": index,
                "paragraphs": [
                    {"id": paragraph_id(index, position), "text": paragraph}
                    for position, paragraph in enumerate(paragraphs, start=1)
                ],
            }
        )
    return {
        "_type": "StoryLengthPatchDraft",
        "_version": LENGTH_PATCH_SCHEMA_VERSION,
        "fragments": annotated,
    }


def maximum_local_compression_characters(draft_payload: Dict[str, Any]) -> int:
    """Return the hard upper bound removable by one ID-only compression patch."""

    removable: List[int] = []
    for item in list((draft_payload or {}).get("fragments") or []):
        text = (
            str(item.get("text") or item.get("content") or "")
            if isinstance(item, dict)
            else str(item or "")
        )
        paragraphs = _split_paragraphs(text)
        removable.extend(
            count_story_text_words(paragraph) for paragraph in paragraphs[1:-1]
        )
    return sum(sorted(removable, reverse=True)[:MAXIMUM_DELETED_PARAGRAPHS])


def length_patch_tool_schema(
    direction: str,
    *,
    draft_payload: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Describe the one tool the revision call is allowed to return.

    A Provider that cannot honour a tool schema must be reported as unable to
    revise; the plan forbids falling back to free-text chapter rewriting, which
    would reintroduce exactly the cost and drift the patch avoids.
    """

    normalized = _normalize_direction(direction)
    operation = _OPS_BY_DIRECTION[normalized]
    properties: Dict[str, Any] = {"op": {"type": "string", "enum": [operation]}}
    if normalized == DIRECTION_EXPAND:
        properties["fragmentOrder"] = {"type": "integer", "minimum": 1}
        properties["text"] = {"type": "string", "minLength": 1}
        properties["anchorParagraphId"] = {"type": "string", "minLength": 1}
        required = ["op", "fragmentOrder", "text", "anchorParagraphId"]
    else:
        valid_ids: List[str] = []
        annotated = annotate_draft_paragraphs(
            list((draft_payload or {}).get("fragments") or [])
        )
        for fragment in annotated["fragments"]:
            paragraphs = list(fragment.get("paragraphs") or [])
            valid_ids.extend(
                str(item.get("id") or "") for item in paragraphs[1:-1]
            )
        item_schema: Dict[str, Any] = {"type": "string", "minLength": 1}
        if valid_ids:
            item_schema["enum"] = valid_ids
        properties["paragraphIds"] = {
            "type": "array",
            "minItems": 1,
            "maxItems": MAXIMUM_DELETED_PARAGRAPHS,
            "uniqueItems": True,
            "items": item_schema,
        }
        required = ["op", "paragraphIds"]
    return {
        "name": LENGTH_PATCH_TOOL_NAME,
        "description": (
            "提交一次局部长度补丁。只允许调整正文长度，不得改动路径、标题、章节编号、"
            "摘要、WIKI 或变量。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["version", "direction", "operations"],
            "properties": {
                "version": {"type": "integer", "enum": [LENGTH_PATCH_SCHEMA_VERSION]},
                "direction": {"type": "string", "enum": [normalized]},
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAXIMUM_OPERATIONS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": required,
                        "properties": properties,
                    },
                },
            },
        },
    }


def _normalize_direction(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in _DIRECTIONS:
        raise LengthPatchRejected(f"unsupported_direction:{normalized or 'empty'}")
    return normalized


def parse_length_patch(payload: Any, *, direction: str) -> Dict[str, Any]:
    """Validate the patch envelope before any text is applied (§7.4 items 1–3)."""

    expected_direction = _normalize_direction(direction)
    data = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except ValueError as exc:
            raise LengthPatchRejected("patch_not_json") from exc
    if not isinstance(data, dict):
        raise LengthPatchRejected("patch_not_object")
    if int(data.get("version") or 0) != LENGTH_PATCH_SCHEMA_VERSION:
        raise LengthPatchRejected("patch_version_mismatch")
    if _normalize_direction(data.get("direction")) != expected_direction:
        raise LengthPatchRejected("patch_direction_mismatch")
    operations = data.get("operations")
    if not isinstance(operations, list) or not operations:
        raise LengthPatchRejected("patch_without_operations")
    if len(operations) > MAXIMUM_OPERATIONS:
        raise LengthPatchRejected("patch_too_many_operations")
    allowed_op = _OPS_BY_DIRECTION[expected_direction]
    normalized_operations: List[Dict[str, Any]] = []
    deleted_ids: set[str] = set()
    for item in operations:
        if not isinstance(item, dict):
            raise LengthPatchRejected("operation_not_object")
        if str(item.get("op") or "").strip() != allowed_op:
            raise LengthPatchRejected(f"operation_not_allowed:{item.get('op')}")
        if expected_direction == DIRECTION_COMPRESS:
            unexpected = sorted(set(item) - {"op", "paragraphIds"})
            if unexpected:
                raise LengthPatchRejected(
                    f"operation_unexpected_field:{unexpected[0]}"
                )
            paragraph_ids = item.get("paragraphIds")
            if not isinstance(paragraph_ids, list) or not paragraph_ids:
                raise LengthPatchRejected("operation_without_paragraph_ids")
            normalized_ids = [str(value or "").strip() for value in paragraph_ids]
            if any(not value for value in normalized_ids):
                raise LengthPatchRejected("operation_invalid_paragraph_id")
            for wanted in normalized_ids:
                if wanted in deleted_ids:
                    raise LengthPatchRejected(f"duplicate_paragraph_id:{wanted}")
                deleted_ids.add(wanted)
            if len(deleted_ids) > MAXIMUM_DELETED_PARAGRAPHS:
                raise LengthPatchRejected("patch_deletes_too_many_paragraphs")
            normalized_operations.append(
                {"op": allowed_op, "paragraphIds": normalized_ids}
            )
            continue
        text = clean_generated_text(str(item.get("text") or ""))
        if not text:
            raise LengthPatchRejected("operation_without_text")
        try:
            fragment_order = int(item.get("fragmentOrder"))
        except (TypeError, ValueError) as exc:
            raise LengthPatchRejected("operation_without_fragment_order") from exc
        if fragment_order < 1:
            raise LengthPatchRejected("operation_fragment_order_out_of_range")
        normalized_operations.append(
            {
                "op": allowed_op,
                "fragmentOrder": fragment_order,
                "text": text,
                "anchorParagraphId": str(item.get("anchorParagraphId") or "").strip(),
                "startParagraphId": str(item.get("startParagraphId") or "").strip(),
                "endParagraphId": str(item.get("endParagraphId") or "").strip(),
            }
        )
    return {
        "version": LENGTH_PATCH_SCHEMA_VERSION,
        "direction": expected_direction,
        "operations": normalized_operations,
    }


def _paragraph_index(paragraphs: List[str], fragment_order: int, wanted: str) -> int:
    for position in range(1, len(paragraphs) + 1):
        if paragraph_id(fragment_order, position) == wanted:
            return position
    raise LengthPatchRejected(f"unknown_paragraph_id:{wanted or 'empty'}")


def _apply_paragraph_deletions(
    fragment_texts: List[str],
    operations: List[Dict[str, Any]],
) -> List[str]:
    paragraphs_by_fragment = [_split_paragraphs(text) for text in fragment_texts]
    locations: Dict[str, Tuple[int, int]] = {}
    for fragment_index, paragraphs in enumerate(paragraphs_by_fragment, start=1):
        for paragraph_index in range(1, len(paragraphs) + 1):
            locations[paragraph_id(fragment_index, paragraph_index)] = (
                fragment_index - 1,
                paragraph_index - 1,
            )

    selected: Dict[int, set[int]] = {}
    seen: set[str] = set()
    for operation in operations:
        for wanted in list(operation.get("paragraphIds") or []):
            paragraph_key = str(wanted or "").strip()
            if paragraph_key in seen:
                raise LengthPatchRejected(f"duplicate_paragraph_id:{paragraph_key}")
            seen.add(paragraph_key)
            location = locations.get(paragraph_key)
            if location is None:
                raise LengthPatchRejected(f"unknown_paragraph_id:{paragraph_key or 'empty'}")
            fragment_index, paragraph_index = location
            paragraphs = paragraphs_by_fragment[fragment_index]
            if paragraph_index == 0 or paragraph_index == len(paragraphs) - 1:
                raise LengthPatchRejected("protected_boundary_paragraph")
            selected.setdefault(fragment_index, set()).add(paragraph_index)
    if len(seen) > MAXIMUM_DELETED_PARAGRAPHS:
        raise LengthPatchRejected("patch_deletes_too_many_paragraphs")

    result = list(fragment_texts)
    for fragment_index, indexes in selected.items():
        paragraphs = list(paragraphs_by_fragment[fragment_index])
        for paragraph_index in sorted(indexes, reverse=True):
            del paragraphs[paragraph_index]
        result[fragment_index] = "\n\n".join(paragraphs)
    return result


def apply_length_patch(fragments: List[Any], patch: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Apply a validated patch in memory and return the candidate fragments.

    Ranges are resolved against the *original* paragraph list and applied from
    the end backwards, so one operation never shifts the anchors of another.
    """

    direction = _normalize_direction(patch.get("direction"))
    operations = list(patch.get("operations") or [])
    fragment_texts: List[str] = []
    for item in fragments:
        fragment_texts.append(
            str(item.get("text") or item.get("content") or "")
            if isinstance(item, dict)
            else str(item or "")
        )

    if direction == DIRECTION_COMPRESS:
        result_texts = _apply_paragraph_deletions(fragment_texts, operations)
        candidate: List[Dict[str, Any]] = []
        for index, text in enumerate(result_texts):
            source = fragments[index] if index < len(fragments) else {}
            entry = dict(source) if isinstance(source, dict) else {}
            entry["text"] = text
            entry.pop("content", None)
            candidate.append(entry)
        return candidate

    by_fragment: Dict[int, List[Dict[str, Any]]] = {}
    for operation in operations:
        order = int(operation["fragmentOrder"])
        if order > len(fragment_texts):
            raise LengthPatchRejected("operation_fragment_order_out_of_range")
        by_fragment.setdefault(order, []).append(operation)

    result_texts = list(fragment_texts)
    for order, fragment_operations in by_fragment.items():
        paragraphs = _split_paragraphs(fragment_texts[order - 1])
        if not paragraphs:
            raise LengthPatchRejected("empty_fragment_cannot_be_patched")
        resolved: List[Tuple[int, int, str]] = []
        for operation in fragment_operations:
            if direction == DIRECTION_EXPAND:
                anchor = _paragraph_index(
                    paragraphs, order, operation["anchorParagraphId"]
                )
                resolved.append((anchor, anchor, operation["text"]))
                continue
            raise LengthPatchRejected(f"operation_not_allowed:{operation.get('op')}")

        resolved.sort(key=lambda entry: entry[0])
        for previous, current in zip(resolved, resolved[1:]):
            if current[0] <= previous[1]:
                raise LengthPatchRejected("overlapping_ranges")

        updated = list(paragraphs)
        for start, end, text in sorted(resolved, key=lambda entry: entry[0], reverse=True):
            updated.insert(start, text)
        result_texts[order - 1] = "\n\n".join(updated)

    candidate: List[Dict[str, Any]] = []
    for index, text in enumerate(result_texts):
        source = fragments[index] if index < len(fragments) else {}
        entry = dict(source) if isinstance(source, dict) else {}
        entry["text"] = text
        entry.pop("content", None)
        candidate.append(entry)
    return candidate


def patch_quality_issues(
    *,
    draft_fragments: List[Any],
    candidate_fragments: List[Any],
    direction: str,
    source_context: str = "",
    user_task: str = "",
) -> List[str]:
    """Report why a candidate is not safe to commit (§7.4 items 5–9).

    Mechanical defects are checked on the candidate, and the comparative checks
    ask whether the patch made the chapter *worse* than the draft rather than
    whether it is perfect: a draft with pre-existing repetition should not be
    unfixable, but a patch that adds repetition is not an improvement.
    """

    normalized_direction = _normalize_direction(direction)
    draft_text = "\n\n".join(
        str(item.get("text") or item.get("content") or "") if isinstance(item, dict) else str(item or "")
        for item in draft_fragments
    )
    candidate_text = "\n\n".join(
        str(item.get("text") or item.get("content") or "") if isinstance(item, dict) else str(item or "")
        for item in candidate_fragments
    )

    # §7.4 item 6 is absolute: a wrapper tag, placeholder, word-count aside or
    # unfinished sentence is a defect no matter what the draft looked like.
    # Items 7's two repetition checks are comparative, because a draft that
    # already repeats itself must still be patchable — judging the candidate
    # absolutely there would make an imperfect draft permanently unrevisable.
    quality_context = "\n\n".join(
        item for item in (str(source_context or "").strip(), draft_text) if item
    )
    issues = [
        item
        for item in contextual_quality_issues(
            candidate_text,
            source_context=quality_context,
            user_task=user_task,
        )
        if item not in _COMPARATIVE_ISSUES
    ]
    if len(candidate_fragments) != len(draft_fragments):
        issues.append("fragment_count_changed")

    draft_count = count_story_text_words(draft_text)
    candidate_count = count_story_text_words(candidate_text)
    if repeated_ngram_count(candidate_text) > repeated_ngram_count(draft_text):
        issues.append("repetition_worse_than_draft")
    if duplicate_paragraph_count(candidate_text) > duplicate_paragraph_count(draft_text):
        issues.append("duplicate_paragraphs_worse_than_draft")

    if normalized_direction == DIRECTION_EXPAND:
        if candidate_count < draft_count:
            issues.append("expansion_shrank_the_chapter")
        if draft_count and candidate_count > draft_count * (1.0 + _MAXIMUM_EXPANSION_RATIO):
            issues.append("expansion_beyond_budget")
        # Expansion inserts only, so every original paragraph must survive.
        draft_paragraphs = {
            item for item in _split_paragraphs(draft_text) if item
        }
        candidate_paragraphs = set(_split_paragraphs(candidate_text))
        if not draft_paragraphs.issubset(candidate_paragraphs):
            issues.append("expansion_rewrote_original_paragraphs")
    else:
        if candidate_count > draft_count:
            issues.append("compression_grew_the_chapter")
        context_anchors: List[str] = []
        for raw_line in str(source_context or "").splitlines():
            anchor = re.sub(
                r"^\s*(?:#{1,6}\s*|[-*+]\s+|\d+[.)]\s+)",
                "",
                raw_line,
            ).strip()
            if 4 <= len(anchor) <= 200 and anchor in draft_text:
                context_anchors.append(anchor)
        if any(anchor not in candidate_text for anchor in context_anchors):
            issues.append("compression_removed_context_anchor")
        draft_ngrams = normalized_character_ngrams(draft_text)
        candidate_ngrams = normalized_character_ngrams(candidate_text)
        if draft_ngrams:
            retained = len(draft_ngrams & candidate_ngrams) / len(draft_ngrams)
            if retained < COMPRESSION_MINIMUM_NGRAM_RETENTION:
                issues.append("compression_rewrote_the_chapter")
    return issues


def revise_draft_with_patch(
    *,
    draft_payload: Dict[str, Any],
    patch: Any,
    direction: str,
    source_context: str = "",
    user_task: str = "",
) -> Dict[str, Any]:
    """Turn one raw patch into a committable candidate payload.

    Returns the increment payload with ``qualityPassed`` so the pipeline's
    selection rules stay the single place that decides draft versus revision.
    Rejection is reported rather than raised: a refused patch is an expected
    outcome that keeps the draft, not an error that should fail the turn.
    """

    fragments = list(draft_payload.get("fragments") or [])
    try:
        normalized_patch = parse_length_patch(patch, direction=direction)
        candidate_fragments = apply_length_patch(fragments, normalized_patch)
    except LengthPatchRejected as exc:
        return {
            "fragments": [],
            "qualityPassed": False,
            "rejectedReason": exc.reason,
            "qualityIssues": [exc.reason],
        }

    issues = patch_quality_issues(
        draft_fragments=fragments,
        candidate_fragments=candidate_fragments,
        direction=direction,
        source_context=source_context,
        user_task=user_task,
    )
    candidate = dict(draft_payload)
    candidate["fragments"] = candidate_fragments
    candidate["qualityPassed"] = not issues
    candidate["qualityIssues"] = issues
    candidate["patchDirection"] = _normalize_direction(direction)
    candidate["patchOperationCount"] = len(normalized_patch["operations"])
    return candidate
