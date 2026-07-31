"""W2-4: the precision revision is a local patch, never a chapter rewrite.

Each test here pins one constraint from plan §7.2/§7.4. They matter because the
failure mode being prevented is silent: a patch that "worked" but rewrote the
draft produces a chapter of the right length whose voice no longer matches the
book, and nothing downstream would notice.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from services.story_length_patch_service import (
    COMPRESSION_MINIMUM_NGRAM_RETENTION,
    DIRECTION_COMPRESS,
    DIRECTION_EXPAND,
    LENGTH_PATCH_SCHEMA_VERSION,
    LENGTH_PATCH_TOOL_NAME,
    MAXIMUM_OPERATIONS,
    LengthPatchRejected,
    annotate_draft_paragraphs,
    apply_length_patch,
    length_patch_tool_schema,
    parse_length_patch,
    patch_quality_issues,
    revise_draft_with_patch,
)


def _draft(*paragraphs: str) -> Dict[str, Any]:
    return {"fragments": [{"text": "\n\n".join(paragraphs)}]}


def _paragraphs(count: int, *, prefix: str = "段") -> List[str]:
    # 每段都以句号收尾，避免机械门禁把测试数据判成 incomplete_ending。
    return [f"{prefix}{index}的正文内容写在这里，交代一件具体的事。" for index in range(1, count + 1)]


# --------------------------------------------------------------------------
# Paragraph IDs and the tool schema
# --------------------------------------------------------------------------


def test_draft_paragraphs_get_stable_ids_that_never_reach_the_prose() -> None:
    annotated = annotate_draft_paragraphs([{"text": "第一段。\n\n第二段。"}])

    fragment = annotated["fragments"][0]
    assert fragment["fragmentOrder"] == 1
    assert [item["id"] for item in fragment["paragraphs"]] == ["f1-p01", "f1-p02"]
    # ID 只用于给操作定位，不能出现在任何一段正文里。
    assert all("f1-p" not in item["text"] for item in fragment["paragraphs"])


def test_expand_and_compress_each_allow_exactly_one_operation_type() -> None:
    expand = length_patch_tool_schema(DIRECTION_EXPAND)
    compress = length_patch_tool_schema(
        DIRECTION_COMPRESS,
        draft_payload=_draft(*_paragraphs(5)),
    )

    assert expand["name"] == LENGTH_PATCH_TOOL_NAME
    operation_schema = expand["parameters"]["properties"]["operations"]
    assert operation_schema["maxItems"] == MAXIMUM_OPERATIONS
    assert operation_schema["items"]["properties"]["op"]["enum"] == ["insert_after"]
    assert "anchorParagraphId" in operation_schema["items"]["required"]

    compress_items = compress["parameters"]["properties"]["operations"]["items"]
    assert compress_items["properties"]["op"]["enum"] == ["delete_paragraphs"]
    assert compress_items["required"] == ["op", "paragraphIds"]
    paragraph_ids = compress_items["properties"]["paragraphIds"]
    assert paragraph_ids["maxItems"] == 8
    assert paragraph_ids["items"]["enum"] == ["f1-p02", "f1-p03", "f1-p04"]
    # 压缩只回传 ID；不能再返回正文或跨段范围，避免整章级输出撞 cap。
    assert "startParagraphId" not in operation_schema["items"]["properties"]
    assert "anchorParagraphId" not in compress_items["properties"]
    assert "text" not in compress_items["properties"]
    assert "startParagraphId" not in compress_items["properties"]
    assert "endParagraphId" not in compress_items["properties"]
    assert "replace_range" not in json.dumps(compress, ensure_ascii=False)


# --------------------------------------------------------------------------
# Envelope validation (§7.4 items 1-3)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("not json at all", "patch_not_json"),
        ({"version": 99, "direction": "expand", "operations": []}, "patch_version_mismatch"),
        (
            {"version": 1, "direction": "compress", "operations": [{"op": "replace_range"}]},
            "patch_direction_mismatch",
        ),
        ({"version": 1, "direction": "expand", "operations": []}, "patch_without_operations"),
    ],
)
def test_a_malformed_envelope_is_rejected(payload: Any, reason: str) -> None:
    with pytest.raises(LengthPatchRejected) as error:
        parse_length_patch(payload, direction=DIRECTION_EXPAND)
    assert error.value.reason == reason


def test_more_than_three_operations_is_rejected() -> None:
    operations = [
        {"op": "insert_after", "fragmentOrder": 1, "anchorParagraphId": "f1-p01", "text": "新增。"}
        for _ in range(MAXIMUM_OPERATIONS + 1)
    ]
    with pytest.raises(LengthPatchRejected) as error:
        parse_length_patch(
            {"version": 1, "direction": "expand", "operations": operations},
            direction=DIRECTION_EXPAND,
        )
    assert error.value.reason == "patch_too_many_operations"


def test_an_expand_patch_may_not_replace_paragraphs() -> None:
    # 方向与 op 必须一一对应，否则扩写调用可以改写原段落。
    with pytest.raises(LengthPatchRejected) as error:
        parse_length_patch(
            {
                "version": 1,
                "direction": "expand",
                "operations": [
                    {
                        "op": "replace_range",
                        "fragmentOrder": 1,
                        "startParagraphId": "f1-p01",
                        "endParagraphId": "f1-p02",
                        "text": "改写。",
                    }
                ],
            },
            direction=DIRECTION_EXPAND,
        )
    assert error.value.reason.startswith("operation_not_allowed")


def test_a_patch_arriving_as_a_json_string_is_accepted() -> None:
    # Provider 常把工具参数序列化成字符串再返回，这不该被当成非法补丁。
    patch = parse_length_patch(
        json.dumps(
            {
                "version": LENGTH_PATCH_SCHEMA_VERSION,
                "direction": "expand",
                "operations": [
                    {
                        "op": "insert_after",
                        "fragmentOrder": 1,
                        "anchorParagraphId": "f1-p01",
                        "text": "新增段落。",
                    }
                ],
            }
        ),
        direction=DIRECTION_EXPAND,
    )
    assert patch["operations"][0]["anchorParagraphId"] == "f1-p01"


# --------------------------------------------------------------------------
# Applying the patch
# --------------------------------------------------------------------------


def test_expansion_inserts_after_the_anchor_and_keeps_every_original_paragraph() -> None:
    fragments = [{"text": "开场。\n\n中段。\n\n收尾。"}]
    patch = {
        "version": 1,
        "direction": "expand",
        "operations": [
            {
                "op": "insert_after",
                "fragmentOrder": 1,
                "anchorParagraphId": "f1-p02",
                "text": "补写的一段。",
            }
        ],
    }

    result = apply_length_patch(fragments, parse_length_patch(patch, direction=DIRECTION_EXPAND))

    assert result[0]["text"].split("\n\n") == ["开场。", "中段。", "补写的一段。", "收尾。"]


def test_unknown_paragraph_id_is_rejected_before_anything_is_applied() -> None:
    fragments = [{"text": "开场。\n\n收尾。"}]
    patch = {
        "version": 1,
        "direction": "expand",
        "operations": [
            {
                "op": "insert_after",
                "fragmentOrder": 1,
                "anchorParagraphId": "f1-p09",
                "text": "补写。",
            }
        ],
    }
    with pytest.raises(LengthPatchRejected) as error:
        apply_length_patch(fragments, parse_length_patch(patch, direction=DIRECTION_EXPAND))
    assert error.value.reason == "unknown_paragraph_id:f1-p09"


def test_multiple_inserts_do_not_shift_each_others_anchors() -> None:
    # 多个操作若按顺序正向应用，后一个锚点会被前一个插入挪位，落在错误位置。
    fragments = [{"text": "一。\n\n二。\n\n三。"}]
    patch = {
        "version": 1,
        "direction": "expand",
        "operations": [
            {"op": "insert_after", "fragmentOrder": 1, "anchorParagraphId": "f1-p01", "text": "A。"},
            {"op": "insert_after", "fragmentOrder": 1, "anchorParagraphId": "f1-p03", "text": "B。"},
        ],
    }

    result = apply_length_patch(fragments, parse_length_patch(patch, direction=DIRECTION_EXPAND))

    assert result[0]["text"].split("\n\n") == ["一。", "A。", "二。", "三。", "B。"]


def test_compression_deletes_selected_paragraphs_without_returning_prose() -> None:
    fragments = [{"text": "\n\n".join(_paragraphs(5))}]
    patch = {
        "version": 1,
        "direction": "compress",
        "operations": [
            {
                "op": "delete_paragraphs",
                "paragraphIds": ["f1-p02", "f1-p03"],
            }
        ],
    }

    result = apply_length_patch(fragments, parse_length_patch(patch, direction=DIRECTION_COMPRESS))

    paragraphs = result[0]["text"].split("\n\n")
    assert paragraphs == [_paragraphs(5)[0], _paragraphs(5)[3], _paragraphs(5)[4]]


def test_compression_may_not_touch_the_first_or_last_paragraph() -> None:
    fragments = [{"text": "\n\n".join(_paragraphs(4))}]
    for paragraph_id in ("f1-p01", "f1-p04"):
        patch = {
            "version": 1,
            "direction": "compress",
            "operations": [
                {
                    "op": "delete_paragraphs",
                    "paragraphIds": [paragraph_id],
                }
            ],
        }
        with pytest.raises(LengthPatchRejected) as error:
            apply_length_patch(
                fragments, parse_length_patch(patch, direction=DIRECTION_COMPRESS)
            )
        # 开场与收尾承担进入本章和交棒下一章的作用，长度补丁不得改动。
        assert error.value.reason == "protected_boundary_paragraph"


def test_duplicate_compression_ids_are_rejected() -> None:
    patch = {
        "version": 1,
        "direction": "compress",
        "operations": [
            {
                "op": "delete_paragraphs",
                "paragraphIds": ["f1-p02", "f1-p03"],
            },
            {
                "op": "delete_paragraphs",
                "paragraphIds": ["f1-p03", "f1-p04"],
            },
        ],
    }
    with pytest.raises(LengthPatchRejected) as error:
        parse_length_patch(patch, direction=DIRECTION_COMPRESS)
    assert error.value.reason == "duplicate_paragraph_id:f1-p03"


def test_the_legacy_replace_range_operation_is_rejected() -> None:
    patch = {
        "version": 1,
        "direction": "compress",
        "operations": [
            {
                "op": "replace_range",
                "fragmentOrder": 1,
                "startParagraphId": "f1-p02",
                "endParagraphId": "f1-p04",
                "text": "精简。",
            }
        ],
    }
    with pytest.raises(LengthPatchRejected) as error:
        parse_length_patch(patch, direction=DIRECTION_COMPRESS)
    assert error.value.reason == "operation_not_allowed:replace_range"


def test_unknown_delete_id_rejects_the_whole_patch_before_mutation() -> None:
    original = "\n\n".join(_paragraphs(5))
    fragments = [{"text": original}]
    patch = parse_length_patch(
        {
            "version": 1,
            "direction": "compress",
            "operations": [
                {
                    "op": "delete_paragraphs",
                    "paragraphIds": ["f1-p02", "f1-p99"],
                }
            ],
        },
        direction=DIRECTION_COMPRESS,
    )

    with pytest.raises(LengthPatchRejected) as error:
        apply_length_patch(fragments, patch)

    assert error.value.reason == "unknown_paragraph_id:f1-p99"
    assert fragments == [{"text": original}]


def test_compression_rejects_more_than_eight_deleted_paragraphs() -> None:
    paragraph_ids = [f"f1-p{index:02d}" for index in range(2, 11)]
    with pytest.raises(LengthPatchRejected) as error:
        parse_length_patch(
            {
                "version": 1,
                "direction": "compress",
                "operations": [
                    {
                        "op": "delete_paragraphs",
                        "paragraphIds": paragraph_ids,
                    }
                ],
            },
            direction=DIRECTION_COMPRESS,
        )
    assert error.value.reason == "patch_deletes_too_many_paragraphs"


def test_a_fragment_order_beyond_the_draft_is_rejected() -> None:
    fragments = [{"text": "只有一个片段。"}]
    patch = {
        "version": 1,
        "direction": "expand",
        "operations": [
            {"op": "insert_after", "fragmentOrder": 3, "anchorParagraphId": "f3-p01", "text": "新增。"}
        ],
    }
    with pytest.raises(LengthPatchRejected) as error:
        apply_length_patch(fragments, parse_length_patch(patch, direction=DIRECTION_EXPAND))
    assert error.value.reason == "operation_fragment_order_out_of_range"


# --------------------------------------------------------------------------
# Quality gates (§7.4 items 5-9)
# --------------------------------------------------------------------------


def test_a_clean_expansion_passes_every_gate() -> None:
    draft = _draft(*_paragraphs(4))
    candidate = apply_length_patch(
        list(draft["fragments"]),
        parse_length_patch(
            {
                "version": 1,
                "direction": "expand",
                "operations": [
                    {
                        "op": "insert_after",
                        "fragmentOrder": 1,
                        "anchorParagraphId": "f1-p02",
                        "text": "他停下脚步，把刚才那句话又想了一遍，然后决定先回码头。",
                    }
                ],
            },
            direction=DIRECTION_EXPAND,
        ),
    )

    assert (
        patch_quality_issues(
            draft_fragments=draft["fragments"],
            candidate_fragments=candidate,
            direction=DIRECTION_EXPAND,
        )
        == []
    )


def test_an_expansion_that_rewrote_original_paragraphs_is_caught() -> None:
    # 即使长度合适，改写原段落也意味着首稿的语感已经被替换掉了。
    draft = _draft(*_paragraphs(3))
    candidate = [{"text": "\n\n".join(_paragraphs(3, prefix="改写") + ["补写的一段。"])}]

    issues = patch_quality_issues(
        draft_fragments=draft["fragments"],
        candidate_fragments=candidate,
        direction=DIRECTION_EXPAND,
    )
    assert "expansion_rewrote_original_paragraphs" in issues


def test_an_expansion_that_shrank_the_chapter_is_caught() -> None:
    draft = _draft(*_paragraphs(4))
    candidate = [{"text": "\n\n".join(_paragraphs(2))}]

    issues = patch_quality_issues(
        draft_fragments=draft["fragments"],
        candidate_fragments=candidate,
        direction=DIRECTION_EXPAND,
    )
    assert "expansion_shrank_the_chapter" in issues


def test_an_expansion_that_doubles_the_draft_is_out_of_budget() -> None:
    # 第二次调用必须是"缺口规模"的调用，不能是"再生成一章"的调用。
    draft = _draft(*_paragraphs(3))
    candidate = [{"text": "\n\n".join(_paragraphs(3) + _paragraphs(5, prefix="巨量"))}]

    issues = patch_quality_issues(
        draft_fragments=draft["fragments"],
        candidate_fragments=candidate,
        direction=DIRECTION_EXPAND,
    )
    assert "expansion_beyond_budget" in issues


def test_compression_that_paraphrased_the_chapter_is_caught() -> None:
    draft = _draft(*_paragraphs(6))
    candidate = [{"text": "完全换过说法的一段话，和原文用词几乎没有交集。\n\n又一段全新表述。"}]

    issues = patch_quality_issues(
        draft_fragments=draft["fragments"],
        candidate_fragments=candidate,
        direction=DIRECTION_COMPRESS,
    )
    assert "compression_rewrote_the_chapter" in issues


def test_compression_that_only_trims_keeps_enough_of_the_original() -> None:
    draft = _draft(*_paragraphs(10))
    candidate = apply_length_patch(
        list(draft["fragments"]),
        parse_length_patch(
            {
                "version": 1,
                "direction": "compress",
                "operations": [
                    {
                        "op": "delete_paragraphs",
                        "paragraphIds": ["f1-p05"],
                    }
                ],
            },
            direction=DIRECTION_COMPRESS,
        ),
    )

    issues = patch_quality_issues(
        draft_fragments=draft["fragments"],
        candidate_fragments=candidate,
        direction=DIRECTION_COMPRESS,
    )
    assert "compression_rewrote_the_chapter" not in issues
    assert issues == []


def test_compression_cannot_delete_a_fact_anchor_from_chapter_context() -> None:
    fact = "档案明确写着：沈岚在凌晨三点交出了铜钥匙。"
    draft = _draft(
        "开场时，林舟推开档案室的门。",
        "他先核对了两遍已经确认的门牌号码。",
        fact,
        "窗外的雨声盖住了走廊里的脚步。",
        "离开前，他把下一步调查写进了便笺。",
        "收尾时，林舟锁好门走进雨里。",
    )
    candidate = apply_length_patch(
        list(draft["fragments"]),
        parse_length_patch(
            {
                "version": 1,
                "direction": "compress",
                "operations": [
                    {
                        "op": "delete_paragraphs",
                        "paragraphIds": ["f1-p03"],
                    }
                ],
            },
            direction=DIRECTION_COMPRESS,
        ),
    )

    issues = patch_quality_issues(
        draft_fragments=draft["fragments"],
        candidate_fragments=candidate,
        direction=DIRECTION_COMPRESS,
        source_context=f"## 已确认事实\n- {fact}",
    )

    assert "compression_removed_context_anchor" in issues


def test_a_candidate_that_worsens_repetition_is_caught() -> None:
    repeated = "他又把同样一句话重复了一遍又一遍地说给自己听着。"
    draft = _draft(*_paragraphs(3))
    candidate = [{"text": "\n\n".join(_paragraphs(3) + [repeated, repeated])}]

    issues = patch_quality_issues(
        draft_fragments=draft["fragments"],
        candidate_fragments=candidate,
        direction=DIRECTION_EXPAND,
    )
    assert "repetition_worse_than_draft" in issues


def test_a_truncated_candidate_is_caught_by_the_mechanical_gate() -> None:
    draft = _draft(*_paragraphs(3))
    candidate = [{"text": "\n\n".join(_paragraphs(3) + ["他伸手去拿那封信，却在"])}]

    issues = patch_quality_issues(
        draft_fragments=draft["fragments"],
        candidate_fragments=candidate,
        direction=DIRECTION_EXPAND,
    )
    assert "incomplete_ending" in issues


def test_a_candidate_that_talks_about_word_counts_is_caught() -> None:
    # 模型把程序的计数指令当成了写作内容，这类文本不能进正文。
    draft = _draft(*_paragraphs(3))
    candidate = [{"text": "\n\n".join(_paragraphs(3) + ["为了达到目标字数，这里再补写一些内容。"])}]

    issues = patch_quality_issues(
        draft_fragments=draft["fragments"],
        candidate_fragments=candidate,
        direction=DIRECTION_EXPAND,
    )
    assert "length_meta_language" in issues


# --------------------------------------------------------------------------
# The candidate payload handed back to the pipeline
# --------------------------------------------------------------------------


def test_a_valid_patch_produces_a_committable_candidate() -> None:
    draft = _draft(*_paragraphs(4))
    candidate = revise_draft_with_patch(
        draft_payload=draft,
        patch={
            "version": 1,
            "direction": "expand",
            "operations": [
                {
                    "op": "insert_after",
                    "fragmentOrder": 1,
                    "anchorParagraphId": "f1-p02",
                    "text": "他把门推开，外面的风比想象中更冷一些。",
                }
            ],
        },
        direction=DIRECTION_EXPAND,
    )

    assert candidate["qualityPassed"] is True
    assert candidate["qualityIssues"] == []
    assert candidate["patchOperationCount"] == 1
    assert candidate["patchDirection"] == DIRECTION_EXPAND
    assert "他把门推开" in candidate["fragments"][0]["text"]


def test_a_rejected_patch_reports_instead_of_raising_so_the_draft_survives() -> None:
    # 补丁被拒是预期结果而不是异常：本轮仍有一份结构完整的首稿可以提交。
    draft = _draft(*_paragraphs(3))
    candidate = revise_draft_with_patch(
        draft_payload=draft,
        patch={"version": 1, "direction": "expand", "operations": "not a list"},
        direction=DIRECTION_EXPAND,
    )

    assert candidate["qualityPassed"] is False
    assert candidate["fragments"] == []
    assert candidate["rejectedReason"] == "patch_without_operations"


def test_the_compression_retention_threshold_is_the_documented_one() -> None:
    # 门槛写死在计划里（§7.4 第 9 条），实现时不得凭感觉放宽。
    assert COMPRESSION_MINIMUM_NGRAM_RETENTION == 0.85
