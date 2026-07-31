from __future__ import annotations

import pytest

from services.story_elastic_manuscript_service import (
    ELASTIC_DRAFT_TOOL_NAME,
    ElasticManuscriptRejected,
    apply_elastic_repair_pack,
    build_elastic_draft_messages,
    build_elastic_repair_messages,
    elastic_draft_tool_schema,
    select_elastic_draft,
)


TARGET = 100


def _unique_chars(start: int, count: int) -> str:
    return "".join(chr(0x4E00 + start + offset) for offset in range(count))


def _paragraphs(*lengths: int) -> tuple[str, list[str]]:
    parts: list[str] = []
    offset = 0
    for length in lengths:
        body = _unique_chars(offset, max(1, length - 1)) + "。"
        parts.append(body)
        offset += max(1, length - 1)
    return "\n\n".join(parts), parts


def _draft(
    canonical_text: str,
    *,
    compact: list[dict[str, object]] | None = None,
    expansion: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    paragraphs = [item for item in canonical_text.split("\n\n") if item]
    return {
        "version": 1,
        "canonicalText": canonical_text,
        "compactReplacements": compact or [],
        "expansionModules": expansion or [],
        "endingHook": paragraphs[-1][-8:],
    }


def _replacement(
    edit_id: str,
    source: str,
    *,
    replacement_length: int,
) -> dict[str, object]:
    return {
        "id": edit_id,
        "sourceStart": source[:4],
        "sourceEnd": source[-4:],
        "replacementText": _unique_chars(1800 + replacement_length, replacement_length),
        "preservedFactIds": [],
        "stateDelta": "same",
    }


def _expansion(edit_id: str, anchor: str, length: int) -> dict[str, object]:
    return {
        "id": edit_id,
        "anchor": anchor,
        "position": "after",
        "text": _unique_chars(2100 + length, length),
        "stateDelta": "none",
    }


def test_draft_schema_requires_a_complete_canonical_and_caps_optional_edits() -> None:
    schema = elastic_draft_tool_schema()
    parameters = schema["parameters"]
    properties = parameters["properties"]

    assert schema["name"] == ELASTIC_DRAFT_TOOL_NAME
    assert parameters["required"] == [
        "version",
        "canonicalText",
        "compactReplacements",
        "expansionModules",
        "endingHook",
    ]
    assert properties["canonicalText"]["type"] == "string"
    assert properties["compactReplacements"]["maxItems"] == 3
    assert properties["expansionModules"]["maxItems"] == 3
    assert (
        properties["compactReplacements"]["items"]["properties"]["stateDelta"]["enum"]
        == ["same"]
    )
    assert (
        properties["expansionModules"]["items"]["properties"]["stateDelta"]["enum"]
        == ["none"]
    )


def test_large_target_draft_contract_names_normal_band_and_expansion_reserve() -> None:
    messages = build_elastic_draft_messages(
        prompt="写下一章",
        context="前文",
        target=5000,
        chapter_path="chapters/第3章/001.md",
    )
    system = messages[0]["content"]
    properties = elastic_draft_tool_schema()["parameters"]["properties"]

    assert "正常放行范围为 4250-6500" in system
    assert "不要自行逐字精确计数" in system
    assert "可能低于 4250" in system
    assert "提供 1 至 3 个 expansionModules" in system
    assert "完整、可发布的整章正文" in properties["canonicalText"]["description"]
    assert "明显偏短" in properties["expansionModules"]["description"]


def test_repair_contract_names_exact_protected_paragraphs_and_middle_only_scope() -> None:
    manuscript, parts = _paragraphs(20, 20, 20, 20, 20)
    messages = build_elastic_repair_messages(
        manuscript=manuscript,
        target=80,
        current_count=100,
        ending_hook=parts[-1][-8:],
    )
    system = messages[0]["content"]
    user = messages[1]["content"]

    assert "缩短时只能使用 1 至 3 个 replace_paragraph_range" in system
    assert "sourceStart 和 sourceEnd 都必须位于可编辑的中部正文" in system
    assert "全部操作的总作用域必须小于正文的一半" in system
    assert f"禁止触碰的开头段落原文：{parts[0]}" in user
    assert f"禁止触碰的末尾两段原文：{parts[-2]}\n\n{parts[-1]}" in user


def test_canonical_quality_rejection_exposes_structured_issue_codes() -> None:
    with pytest.raises(ElasticManuscriptRejected) as error:
        select_elastic_draft(
            _draft("这一段正文没有句末标点"),
            target=TARGET,
            precise=False,
        )

    assert error.value.reason == "canonical_quality_rejected"
    assert error.value.issues == ["incomplete_ending"]


def test_canonical_in_band_wins_even_when_optional_edits_exist() -> None:
    canonical, parts = _paragraphs(20, 20, 20, 20, 20)
    middle = parts[1]
    invalid_opening = parts[0]
    result = select_elastic_draft(
        _draft(
            canonical,
            compact=[
                _replacement("middle", middle[3:15], replacement_length=4),
                _replacement("opening", invalid_opening[2:14], replacement_length=4),
            ],
        ),
        target=TARGET,
        precise=False,
    )

    assert result["text"] == canonical
    assert result["canonicalWordCount"] == 100
    assert result["selectedEditIds"] == []
    assert result["normalBandPassed"] is True
    assert result["lengthFallbackReason"] == "canonical_in_band"
    assert "opening" in result["rejectedEditIds"]
    assert result["rejectedEditReasonCounts"] == {
        "edit_touches_protected_region": 1
    }


def test_selection_enumerates_all_64_combinations_and_uses_the_fixed_tie_break() -> None:
    canonical, parts = _paragraphs(20, 30, 30, 30, 20, 20)
    # Canonical is 150 chars. Both one-edit compact candidates enter the normal
    # 70-130 band. edit-a changes fewer characters (22 rather than 30), so it
    # wins before distance-to-center is considered.
    edit_a_source = parts[1][1:29]
    edit_b_source = parts[2][1:29]
    compact = [
        _replacement("edit-a", edit_a_source, replacement_length=6),
        _replacement("edit-b", edit_b_source, replacement_length=1),
        _replacement("edit-c", parts[3][2:12], replacement_length=8),
    ]
    expansion = [
        _expansion("expand-a", parts[1][5:9], 1),
        _expansion("expand-b", parts[2][7:11], 2),
        _expansion("expand-c", parts[3][9:13], 3),
    ]

    result = select_elastic_draft(
        _draft(canonical, compact=compact, expansion=expansion),
        target=TARGET,
        precise=False,
    )

    assert result["evaluatedCombinationCount"] == 64
    assert result["selectedEditIds"] == ["edit-a"]
    assert result["normalBandPassed"] is True
    assert result["lengthFallbackReason"] == "candidate_in_band"


def test_outside_band_candidate_must_remove_at_least_half_the_band_distance() -> None:
    canonical, parts = _paragraphs(20, 30, 30, 30, 20, 20)
    # 150 is 20 chars above the normal high bound. This replacement removes
    # only 6 chars, leaving a distance of 14: 30% improvement is rejected.
    source = parts[1][3:15]
    result = select_elastic_draft(
        _draft(
            canonical,
            compact=[_replacement("small-improvement", source, replacement_length=6)],
        ),
        target=TARGET,
        precise=False,
    )

    assert result["text"] == canonical
    assert result["selectedEditIds"] == []
    assert result["normalBandPassed"] is False
    assert result["lengthFallbackReason"] == "insufficient_improvement"


def test_outside_band_candidate_is_adopted_at_the_exact_fifty_percent_threshold() -> None:
    canonical, parts = _paragraphs(20, 30, 30, 30, 20, 20)
    source = parts[1][2:18]
    result = select_elastic_draft(
        _draft(
            canonical,
            compact=[_replacement("half-recovery", source, replacement_length=6)],
        ),
        target=TARGET,
        precise=False,
    )

    assert result["finalWordCount"] == 140
    assert result["normalBandPassed"] is False
    assert result["selectedEditIds"] == ["half-recovery"]
    assert result["lengthFallbackReason"] == "candidate_recovered"


def test_inexact_declared_hook_does_not_discard_an_expansion_that_preserves_the_ending() -> None:
    canonical, parts = _paragraphs(10, 10, 10, 10, 10)
    payload = _draft(
        canonical,
        expansion=[_expansion("middle-expansion", parts[1][3:7], 25)],
    )
    payload["endingHook"] = "模型概括的结尾钩子"

    result = select_elastic_draft(
        payload,
        target=TARGET,
        precise=False,
    )

    assert result["selectedEditIds"] == ["middle-expansion"]
    assert result["finalWordCount"] == 75
    # The expansion is still retained even though 75 is below the current
    # target-mode hard minimum (0.85 * 100). This test protects ending
    # preservation, not the superseded 0.70T band.
    assert result["normalBandPassed"] is False
    assert result["text"].endswith("\n\n".join(parts[-2:]))


def test_invalid_repair_pack_keeps_the_complete_first_call_text() -> None:
    canonical, parts = _paragraphs(20, 20, 20, 20, 20)
    result = apply_elastic_repair_pack(
        canonical,
        {
            "version": 1,
            "operations": [
                {
                    "id": "touches-ending",
                    "op": "replace_paragraph_range",
                    "sourceStart": parts[-1][:4],
                    "sourceEnd": parts[-1][-4:],
                    "replacementText": "保留结尾。",
                }
            ],
        },
        target=120,
        ending_hook=parts[-1][-8:],
    )

    assert result["text"] == canonical
    assert result["accepted"] is False
    assert result["qualityPassed"] is False
    assert result["lengthFallbackReason"] == "repair_failed"


def test_invalid_repair_pack_reports_the_structured_operation_type_rejection_reason() -> None:
    canonical, parts = _paragraphs(20, 20, 20, 20, 20)
    result = apply_elastic_repair_pack(
        canonical,
        {
            "version": 1,
            "operations": [
                {
                    "id": "invalid-operation",
                    "op": "rewrite_whole_chapter",
                }
            ],
        },
        target=TARGET,
        ending_hook=parts[-1][-8:],
    )

    assert result["text"] == canonical
    assert result["accepted"] is False
    assert result["rejectionReasons"] == ["repair_operation_not_allowed"]


def test_repair_ignores_unused_metadata_fields_and_applies_the_valid_local_operation() -> None:
    canonical, parts = _paragraphs(20, 20, 20, 20, 20, 20)
    source = parts[2][2:16]
    result = apply_elastic_repair_pack(
        canonical,
        {
            "version": 1,
            "operations": [
                {
                    "id": "local-compact",
                    "op": "replace_paragraph_range",
                    "sourceStart": source[:4],
                    "sourceEnd": source[-4:],
                    "replacementText": _unique_chars(2600, 4),
                    "rationale": "unused model metadata",
                }
            ],
        },
        target=TARGET,
        ending_hook=parts[-1][-8:],
    )

    assert result["accepted"] is True
    assert result["selectedEditIds"] == ["local-compact"]
    assert result["finalWordCount"] == 110


def test_candidate_that_removes_a_protected_fact_anchor_is_discarded() -> None:
    canonical, parts = _paragraphs(20, 30, 30, 30, 20, 20)
    protected_fact = parts[1][5:11]
    source = parts[1][2:18]
    result = select_elastic_draft(
        _draft(
            canonical,
            compact=[_replacement("drops-fact", source, replacement_length=1)],
        ),
        target=TARGET,
        precise=False,
        protected_fact_anchors=[protected_fact],
    )

    assert result["text"] == canonical
    assert result["selectedEditIds"] == []
    assert result["normalBandPassed"] is False


def test_worse_repair_candidate_is_not_adopted_just_because_the_call_was_spent() -> None:
    canonical, parts = _paragraphs(20, 20, 20, 20, 20, 20)
    result = apply_elastic_repair_pack(
        canonical,
        {
            "version": 1,
            "operations": [
                {
                    "id": "moves-away",
                    "op": "insert_after",
                    "anchor": parts[2][5:9],
                    "text": _unique_chars(2400, 10),
                }
            ],
        },
        target=TARGET,
        ending_hook=parts[-1][-8:],
    )

    assert result["candidateWordCount"] == 130
    assert result["accepted"] is False
    assert result["text"] == canonical
    assert result["lengthFallbackReason"] == "repair_outside_band"
