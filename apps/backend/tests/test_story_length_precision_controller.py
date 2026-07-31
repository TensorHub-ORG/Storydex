"""W2-4: the optional second prose call is bounded and fails safe.

The tests are organised around the three things that make this call acceptable:
it only runs when the draft actually missed the band, it chooses a local patch
or feedback-calibrated redraft from the measured gap, it never costs more than
one request, and every failure keeps the draft instead of retrying.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

import pytest

from services.coomi_agent_service import StorydexToolCallRejected
from services.story_length_patch_service import LENGTH_PATCH_TOOL_NAME
from services.story_length_precision_controller import (
    REVISION_TRANSPORT_RETRIES,
    REVISION_UNAVAILABLE_EMPTY,
    REVISION_UNAVAILABLE_NO_TOOL_SUPPORT,
    REVISION_UNAVAILABLE_TIMEOUT,
    REVISION_UNAVAILABLE_TRANSPORT,
    StoryLengthPrecisionController,
    build_revision_messages,
    revision_budget,
)

_PARAGRAPH = "他推开门，走进那间空置多年的旧屋，灰尘在光里浮动。"


def _draft(paragraph_count: int = 4) -> Dict[str, Any]:
    paragraphs = [f"{_PARAGRAPH}第{index}段。" for index in range(1, paragraph_count + 1)]
    return {"fragments": [{"text": "\n\n".join(paragraphs)}]}


def _unique_paragraph(size: int, *, offset: int) -> str:
    return "".join(chr(0x4E00 + offset + index) for index in range(size - 1)) + "。"


def _paragraph_with_prefix(size: int, prefix: str, *, offset: int) -> str:
    suffix_size = size - len(prefix) - 1
    assert suffix_size >= 0
    suffix = "".join(chr(0x4E00 + offset + index) for index in range(suffix_size))
    return prefix + suffix + "。"


def _bounded_redraft_case(
    *,
    candidate_word_count: int = 3000,
    unrelated_candidate: bool = False,
) -> tuple[Dict[str, Any], list[str], list[str]]:
    paragraph_sizes = [1300, *([134] * 6), *([133] * 12), 1300]
    draft_paragraphs: list[str] = []
    offset = 0
    for size in paragraph_sizes:
        draft_paragraphs.append(_unique_paragraph(size, offset=offset))
        offset += size
    if unrelated_candidate:
        base, remainder = divmod(candidate_word_count, 12)
        candidate_sizes = [base + (1 if index < remainder else 0) for index in range(12)]
        candidate_paragraphs = []
        offset = 10000
        for size in candidate_sizes:
            candidate_paragraphs.append(_unique_paragraph(size, offset=offset))
            offset += size
    else:
        interior_count = candidate_word_count - len(draft_paragraphs[0]) - len(
            draft_paragraphs[-1]
        )
        base, remainder = divmod(interior_count, 10)
        candidate_paragraphs = [draft_paragraphs[0]]
        for index, paragraph in enumerate(draft_paragraphs[1:11]):
            size = base + (1 if index < remainder else 0)
            candidate_paragraphs.append(paragraph[: size - 1] + "。")
        candidate_paragraphs.append(draft_paragraphs[-1])
    draft_payload = {
        "prompt": "继续当前章",
        "fragments": [
            {
                "path": "chapters/第1章/001.md",
                "text": "\n\n".join(draft_paragraphs),
            }
        ],
    }
    return draft_payload, draft_paragraphs, candidate_paragraphs


def _run_bounded_redraft(
    draft_payload: Dict[str, Any],
    candidate_paragraphs: list[str],
    *,
    chapter_context: str = "",
    user_task: str = "继续当前章",
) -> Dict[str, Any]:
    async def call_provider(**_kwargs: Any) -> Dict[str, Any]:
        return {
            "version": 1,
            "strategy": "feedback_bounded_redraft",
            "paragraphs": candidate_paragraphs,
        }

    return asyncio.run(
        StoryLengthPrecisionController().revise(
            {
                "draftPayload": draft_payload,
                "draftWordCount": 5000,
                "draftGeneratedWordCount": 5000,
                "retainedWordCount": 0,
                "target": 3000,
                "direction": "compress",
            },
            call_provider=call_provider,
            chapter_context=chapter_context,
            user_task=user_task,
            draft_completion_tokens=9000,
        )
    )


def _request(direction: str = "expand", *, draft_word_count: int = 2300) -> Dict[str, Any]:
    return {
        "draftPayload": _draft(),
        "draftWordCount": draft_word_count,
        "target": 3000,
        "direction": direction,
    }


def _expand_patch(anchor: str = "f1-p02", text: str = "新增的一段正文，交代动作后果。") -> str:
    return json.dumps(
        {
            "version": 1,
            "direction": "expand",
            "operations": [
                {
                    "op": "insert_after",
                    "fragmentOrder": 1,
                    "anchorParagraphId": anchor,
                    "text": text,
                }
            ],
        },
        ensure_ascii=False,
    )


def _multi_expand_patch(*texts: str) -> str:
    return json.dumps(
        {
            "version": 1,
            "direction": "expand",
            "operations": [
                {
                    "op": "insert_after",
                    "fragmentOrder": 1,
                    "anchorParagraphId": f"f1-p{index + 1:02d}",
                    "text": text,
                }
                for index, text in enumerate(texts, start=1)
            ],
        },
        ensure_ascii=False,
    )


# --------------------------------------------------------------------------
# Budget (plan §9): derived from the draft, never a user setting.
# --------------------------------------------------------------------------


def test_revision_budget_is_half_the_draft_within_bounds() -> None:
    budget = revision_budget(draft_completion_tokens=3000, draft_duration_ms=60_000)
    assert budget["maxCompletionTokens"] == 1500
    assert budget["deadlineSeconds"] == 36
    # 修订绝不重试传输：一次精确修订重试两次就变成三四个请求，正是本设计要避免的成本。
    assert budget["transportRetries"] == REVISION_TRANSPORT_RETRIES == 0


def test_revision_budget_clamps_both_ends() -> None:
    # 极小首稿不会把修订预算压到无法完成一次补丁。
    tiny = revision_budget(draft_completion_tokens=10, draft_duration_ms=1_000)
    assert tiny["maxCompletionTokens"] == 512
    assert tiny["deadlineSeconds"] == 20

    # 极慢首稿也不会让修订无限等待。
    huge = revision_budget(draft_completion_tokens=100_000, draft_duration_ms=600_000)
    assert huge["maxCompletionTokens"] == 2048
    assert huge["deadlineSeconds"] == 60


def test_revision_budget_uses_a_bounded_provider_capability_policy() -> None:
    budget = revision_budget(
        draft_completion_tokens=1763,
        draft_duration_ms=29_206,
        budget_policy={
            "name": "openai_compatible_non_streaming",
            "deadlineRatio": 1.25,
            "deadlineMinimumSeconds": 30,
            "deadlineMaximumSeconds": 60,
        },
    )

    assert budget["maxCompletionTokens"] == 882
    assert budget["deadlineSeconds"] == 37
    assert budget["deadlinePolicy"] == "openai_compatible_non_streaming"
    assert budget["transportRetries"] == 0


def test_revision_budget_reserves_json_overhead_for_the_measured_length_gap() -> None:
    budget = revision_budget(
        draft_completion_tokens=800,
        draft_duration_ms=10_000,
        required_character_delta=1000,
    )

    assert budget["maxCompletionTokens"] == 1756
    assert budget["requiredCharacterDelta"] == 1000

    bounded = revision_budget(
        draft_completion_tokens=4000,
        required_character_delta=5000,
    )
    assert bounded["maxCompletionTokens"] == 2048


def test_compression_budget_is_derived_from_the_zero_text_patch_protocol() -> None:
    budget = revision_budget(
        draft_completion_tokens=10_000,
        required_character_delta=3_000,
        maximum_patch_text_characters=0,
    )

    assert budget["maxCompletionTokens"] == 512
    assert budget["maximumPatchTextCharacters"] == 0
    assert budget["requiredCharacterDelta"] == 3_000


# --------------------------------------------------------------------------
# Prompt (plan §7.3): draft + measurement + direction, nothing else.
# --------------------------------------------------------------------------


def test_revision_prompt_carries_paragraph_ids_and_direction() -> None:
    messages = build_revision_messages(
        draft_payload=_draft(),
        draft_word_count=1200,
        target=3000,
        direction="expand",
    )
    assert [item["role"] for item in messages] == ["system", "user"]
    assert LENGTH_PATCH_TOOL_NAME in messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    assert payload["direction"] == "expand"
    ids = [item["id"] for item in payload["draft"]["fragments"][0]["paragraphs"]]
    assert ids == ["f1-p01", "f1-p02", "f1-p03", "f1-p04"]
    # 缺口以方向和量级表达，模型不负责验算最终字符数。
    assert "增加约 1500 字" in payload["instruction"]
    assert "动作后果" in payload["expansionDirections"]


def test_revision_prompt_does_not_replay_agent_context() -> None:
    messages = build_revision_messages(
        draft_payload=_draft(),
        draft_word_count=3600,
        target=3000,
        direction="compress",
    )
    combined = "\n".join(item["content"] for item in messages)
    # §7.3：第二次调用不重复注入完整 Agent 对话、工具日志、记忆和项目文件。
    for leaked in ("ToolDone", "TurnContract", "AgentStarted", "memory", "wiki"):
        assert leaked not in combined
    payload = json.loads(messages[1]["content"])
    assert payload["direction"] == "compress"
    assert "精简约 300 字" in payload["instruction"]
    assert "删去重复核验" in payload["expansionDirections"]
    assert "不得重写整章" in combined
    assert "只提交冗余段落 ID" in combined
    assert payload["patchConstraints"] == {
        "maximumOperations": 3,
        "maximumDeletedParagraphs": 8,
        "maximumReplacementCharacters": 0,
    }


# --------------------------------------------------------------------------
# One call, and it either lands or keeps the draft.
# --------------------------------------------------------------------------


def test_a_valid_patch_becomes_a_candidate_in_one_call() -> None:
    controller = StoryLengthPrecisionController()
    calls: list[Dict[str, Any]] = []

    async def call_provider(**kwargs: Any) -> str:
        calls.append(kwargs)
        return _expand_patch()

    candidate = asyncio.run(
        controller.revise(_request(), call_provider=call_provider)
    )

    assert len(calls) == 1
    assert calls[0]["tool"]["name"] == LENGTH_PATCH_TOOL_NAME
    assert calls[0]["max_completion_tokens"] == 856
    assert candidate["qualityPassed"] is True
    assert candidate["patchDirection"] == "expand"
    assert candidate["patchOperationCount"] == 1
    # 原段落全部保留，只多出插入的一段。
    paragraphs = candidate["fragments"][0]["text"].split("\n\n")
    assert len(paragraphs) == 5
    assert "新增的一段正文" in paragraphs[2]


def test_compression_tool_enumerates_only_current_interior_paragraph_ids() -> None:
    controller = StoryLengthPrecisionController()
    calls: list[Dict[str, Any]] = []

    async def call_provider(**kwargs: Any) -> str:
        calls.append(kwargs)
        return json.dumps(
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
            ensure_ascii=False,
        )

    request = {
        "draftPayload": _draft(10),
        "draftWordCount": 3500,
        "target": 3000,
        "direction": "compress",
    }
    candidate = asyncio.run(
        controller.revise(request, call_provider=call_provider)
    )

    paragraph_ids = calls[0]["tool"]["parameters"]["properties"]["operations"][
        "items"
    ]["properties"]["paragraphIds"]["items"]["enum"]
    assert paragraph_ids == [f"f1-p{index:02d}" for index in range(2, 10)]
    assert candidate["patchDirection"] == "compress"
    assert candidate["patchOperationCount"] == 1


def test_compression_outside_the_local_delete_corridor_uses_feedback_bounded_redraft() -> None:
    controller = StoryLengthPrecisionController()
    calls: list[Dict[str, Any]] = []

    paragraph_count = 20
    paragraph_size = 250
    paragraph_sizes = [1300, *([134] * 6), *([133] * 12), 1300]
    draft_payload = {
        "fragments": [
            {
                "text": "\n\n".join(
                    "甲" * (size - 1) + "。" for size in paragraph_sizes
                )
            }
        ]
    }

    async def call_provider(**kwargs: Any) -> None:
        calls.append(kwargs)
        return None

    candidate = asyncio.run(
        controller.revise(
            {
                "draftPayload": draft_payload,
                "draftWordCount": 5000,
                "target": 3000,
                "direction": "compress",
            },
            call_provider=call_provider,
            draft_completion_tokens=9000,
        )
    )

    assert len(calls) == 1
    assert calls[0]["tool"]["name"] == "StorydexSubmitBoundedRedraft"
    feedback = json.loads(calls[0]["messages"][1]["content"])
    assert feedback["strategy"] == "feedback_bounded_redraft"
    assert feedback["measuredDraftWordCount"] == 5000
    assert feedback["targetWordCount"] == 3000
    assert feedback["precisionBand"] == [2700, 3300]
    assert feedback["targetToDraftRatio"] == 0.6
    assert feedback["draftParagraphCount"] == paragraph_count
    assert feedback["averageParagraphWordCount"] == paragraph_size
    assert feedback["suggestedParagraphRange"] == [10, 14]
    assert feedback["minimumParagraphsToMergeOrRemove"] == 6
    assert "至少合并或删除 6 个首稿段落边界" in calls[0]["messages"][0]["content"]
    assert calls[0]["tool"]["parameters"]["properties"]["paragraphs"]["minItems"] == 10
    assert calls[0]["tool"]["parameters"]["properties"]["paragraphs"]["maxItems"] == 14
    assert "do not preserve one output item per source paragraph" in (
        calls[0]["tool"]["parameters"]["properties"]["paragraphs"]["description"]
    )
    assert calls[0]["max_completion_tokens"] == 5558
    assert candidate["strategy"] == "feedback_bounded_redraft"
    assert candidate["budget"]["maximumBodyCharacters"] == 3300
    assert candidate["budget"]["paragraphRange"] == [10, 14]
    assert candidate["rejectedReason"] == REVISION_UNAVAILABLE_EMPTY


def test_expansion_outside_the_local_patch_budget_uses_feedback_bounded_redraft() -> None:
    controller = StoryLengthPrecisionController()
    calls: list[Dict[str, Any]] = []
    draft_payload = {
        "fragments": [
            {
                "text": "\n\n".join(
                    _unique_paragraph(100, offset=index * 100)
                    for index in range(12)
                )
            }
        ]
    }

    async def call_provider(**kwargs: Any) -> None:
        calls.append(kwargs)
        return None

    candidate = asyncio.run(
        controller.revise(
            {
                "draftPayload": draft_payload,
                "draftWordCount": 1200,
                "draftGeneratedWordCount": 1200,
                "retainedWordCount": 0,
                "target": 3000,
                "direction": "expand",
            },
            call_provider=call_provider,
            draft_completion_tokens=1800,
        )
    )

    assert len(calls) == 1
    assert calls[0]["tool"]["name"] == "StorydexSubmitBoundedRedraft"
    feedback = json.loads(calls[0]["messages"][1]["content"])
    assert feedback["direction"] == "expand"
    assert feedback["targetToDraftRatio"] == 2.5
    assert feedback["draftParagraphCount"] == 12
    assert feedback["averageParagraphWordCount"] == 100
    assert feedback["suggestedParagraphRange"] == [28, 32]
    assert calls[0]["max_completion_tokens"] == 5846
    assert candidate["strategy"] == "feedback_bounded_redraft"


def test_feedback_bounded_redraft_returns_one_precision_candidate() -> None:
    controller = StoryLengthPrecisionController()
    paragraph_sizes = [1300, *([134] * 6), *([133] * 12), 1300]
    draft_paragraphs: list[str] = []
    offset = 0
    for size in paragraph_sizes:
        draft_paragraphs.append(_unique_paragraph(size, offset=offset))
        offset += size
    candidate_paragraphs = [
        draft_paragraphs[0],
        *(paragraph[:39] + "。" for paragraph in draft_paragraphs[1:11]),
        draft_paragraphs[-1],
    ]
    draft_payload = {
        "prompt": "继续当前章",
        "fragments": [{"path": "chapters/第1章/001.md", "text": "\n\n".join(draft_paragraphs)}],
    }

    async def call_provider(**_kwargs: Any) -> Dict[str, Any]:
        return {
            "version": 1,
            "strategy": "feedback_bounded_redraft",
            "paragraphs": candidate_paragraphs,
        }

    candidate = asyncio.run(
        controller.revise(
            {
                "draftPayload": draft_payload,
                "draftWordCount": 5000,
                "draftGeneratedWordCount": 5000,
                "retainedWordCount": 0,
                "target": 3000,
                "direction": "compress",
            },
            call_provider=call_provider,
            draft_completion_tokens=9000,
        )
    )

    assert candidate["strategy"] == "feedback_bounded_redraft"
    assert candidate["qualityPassed"] is True
    assert candidate["qualityIssues"] == []
    assert candidate["redraftWordCount"] == 3000
    assert candidate["redraftParagraphCount"] == 12
    assert candidate["prompt"] == draft_payload["prompt"]
    assert candidate["fragments"][0]["path"] == draft_payload["fragments"][0]["path"]
    assert candidate["fragments"][0]["text"].split("\n\n") == candidate_paragraphs


def test_feedback_bounded_redraft_treats_the_suggested_paragraph_range_as_advisory() -> None:
    draft_payload, _, candidate_paragraphs = _bounded_redraft_case()
    paragraph = candidate_paragraphs[1]
    quarter = len(paragraph) // 4
    split_paragraphs = [
        paragraph[:quarter],
        paragraph[quarter : quarter * 2],
        paragraph[quarter * 2 : quarter * 3],
        paragraph[quarter * 3 :],
    ]
    candidate_paragraphs = [
        candidate_paragraphs[0],
        *split_paragraphs,
        *candidate_paragraphs[2:],
    ]

    candidate = _run_bounded_redraft(draft_payload, candidate_paragraphs)

    assert candidate["qualityPassed"] is True
    assert candidate["redraftWordCount"] == 3000
    assert candidate["redraftParagraphCount"] == 15
    assert candidate["suggestedRedraftParagraphRange"] == [10, 14]
    assert candidate["redraftParagraphRangeAdhered"] is False


def test_feedback_bounded_redraft_preserves_multi_fragment_shape() -> None:
    draft_payload, draft_paragraphs, candidate_paragraphs = _bounded_redraft_case()
    draft_payload["fragments"] = [
        {
            "path": "chapters/第1章/001.md",
            "text": "\n\n".join(draft_paragraphs[:10]),
        },
        {
            "path": "chapters/第1章/002.md",
            "text": "\n\n".join(draft_paragraphs[10:]),
        },
    ]

    candidate = _run_bounded_redraft(draft_payload, candidate_paragraphs)

    assert candidate["qualityPassed"] is True
    assert [item["path"] for item in candidate["fragments"]] == [
        "chapters/第1章/001.md",
        "chapters/第1章/002.md",
    ]
    assert [
        len(item["text"].split("\n\n")) for item in candidate["fragments"]
    ] == [6, 6]


def test_feedback_bounded_redraft_rejects_embedded_paragraph_breaks() -> None:
    controller = StoryLengthPrecisionController()
    draft_payload, _, candidate_paragraphs = _bounded_redraft_case()
    smuggled = [
        candidate_paragraphs[0] + "\n\n" + candidate_paragraphs[1],
        *candidate_paragraphs[2:],
    ]

    async def call_provider(**_kwargs: Any) -> Dict[str, Any]:
        return {
            "version": 1,
            "strategy": "feedback_bounded_redraft",
            "paragraphs": smuggled,
        }

    candidate = asyncio.run(
        controller.revise(
            {
                "draftPayload": draft_payload,
                "draftWordCount": 5000,
                "draftGeneratedWordCount": 5000,
                "retainedWordCount": 0,
                "target": 3000,
                "direction": "compress",
            },
            call_provider=call_provider,
            draft_completion_tokens=9000,
        )
    )

    assert candidate["qualityPassed"] is False
    assert candidate["rejectedReason"] == "tool_arguments_invalid_redraft"
    assert candidate["redraftRejectionReason"] == "redraft_paragraph_contains_break"
    assert candidate["fragments"] == []


def test_feedback_bounded_redraft_rejects_a_locally_measured_length_miss() -> None:
    draft_payload, _, candidate_paragraphs = _bounded_redraft_case(
        candidate_word_count=3400
    )

    candidate = _run_bounded_redraft(draft_payload, candidate_paragraphs)

    assert candidate["redraftWordCount"] == 3400
    assert candidate["qualityPassed"] is False
    assert "redraft_outside_precision_band" in candidate["qualityIssues"]


def test_feedback_bounded_redraft_rejects_continuity_and_ending_hook_drift() -> None:
    draft_payload, _, candidate_paragraphs = _bounded_redraft_case(
        unrelated_candidate=True
    )

    candidate = _run_bounded_redraft(draft_payload, candidate_paragraphs)

    assert candidate["redraftWordCount"] == 3000
    assert candidate["qualityPassed"] is False
    assert "redraft_continuity_too_low" in candidate["qualityIssues"]
    assert "redraft_ending_hook_changed" in candidate["qualityIssues"]


def test_feedback_bounded_redraft_rejects_contextual_quality_regressions() -> None:
    draft_payload, _, candidate_paragraphs = _bounded_redraft_case()
    candidate_paragraphs[1] = _paragraph_with_prefix(
        len(candidate_paragraphs[1]),
        "你推开门，你看见桌上的信，你没有立刻触碰。",
        offset=14000,
    )
    candidate_paragraphs[2] = _paragraph_with_prefix(
        len(candidate_paragraphs[2]),
        "你回过头，你决定先封锁现场再追查。",
        offset=14100,
    )
    candidate_paragraphs[3] = _paragraph_with_prefix(
        len(candidate_paragraphs[3]),
        "他突然露出阴茎并强行插入她的阴道。",
        offset=14200,
    )
    candidate_paragraphs[4] = _paragraph_with_prefix(
        len(candidate_paragraphs[4]),
        "为了达到目标字数，这里继续补写正文。",
        offset=14300,
    )

    candidate = _run_bounded_redraft(
        draft_payload,
        candidate_paragraphs,
        chapter_context="林舟沿着走廊前行，他始终没有回头。",
        user_task="继续调查现场",
    )

    assert candidate["qualityPassed"] is False
    assert "narrative_perspective_shift" in candidate["qualityIssues"]
    assert "unexpected_explicit_content" in candidate["qualityIssues"]
    assert "length_meta_language" in candidate["qualityIssues"]


def test_feedback_bounded_redraft_rejects_a_removed_context_fact_anchor() -> None:
    fact = "沈岚在凌晨三点交出了铜钥匙。"
    draft_payload, draft_paragraphs, candidate_paragraphs = _bounded_redraft_case()
    draft_paragraphs[5] = _paragraph_with_prefix(
        len(draft_paragraphs[5]),
        fact,
        offset=14500,
    )
    draft_payload["fragments"][0]["text"] = "\n\n".join(draft_paragraphs)

    candidate = _run_bounded_redraft(
        draft_payload,
        candidate_paragraphs,
        chapter_context=f"## 已确认事实\n- {fact}",
    )

    assert candidate["qualityPassed"] is False
    assert "redraft_removed_context_anchor" in candidate["qualityIssues"]


def test_a_patch_cannot_shift_a_third_person_chapter_into_second_person() -> None:
    controller = StoryLengthPrecisionController()

    async def call_provider(**_kwargs: Any) -> str:
        return _multi_expand_patch(
            "你推开窗，冷风让你后退半步，你仍盯着巷口。",
            "你听见脚步逼近，便让你自己贴紧墙面等候。",
        )

    candidate = asyncio.run(
        controller.revise(
            _request(),
            call_provider=call_provider,
            chapter_context="林舟沿着走廊前行，他始终没有回头。",
            user_task="继续当前章",
        )
    )

    assert candidate["qualityPassed"] is False
    assert "narrative_perspective_shift" in candidate["qualityIssues"]


def test_a_patch_cannot_introduce_unrequested_explicit_content() -> None:
    controller = StoryLengthPrecisionController()

    async def call_provider(**_kwargs: Any) -> str:
        return _expand_patch(text="他突然露出阴茎并强行插入她的阴道。")

    candidate = asyncio.run(
        controller.revise(
            _request(),
            call_provider=call_provider,
            chapter_context="两人隔着桌子核对失踪者留下的账本。",
            user_task="补足调查现场的动作后果",
        )
    )

    assert candidate["qualityPassed"] is False
    assert "unexpected_explicit_content" in candidate["qualityIssues"]


def test_a_timeout_keeps_the_draft_without_retrying() -> None:
    controller = StoryLengthPrecisionController()
    attempts = 0

    async def call_provider(**_kwargs: Any) -> str:
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(5)
        return _expand_patch()

    async def scenario() -> Dict[str, Any]:
        # 用极短 deadline 复现超时，不真的等 20 秒。
        controller_budget = revision_budget()
        assert controller_budget["deadlineSeconds"] == 20
        return await asyncio.wait_for(
            controller.revise(_request(), call_provider=call_provider),
            timeout=30,
        )

    # 直接用受控的极短超时验证行为，避免测试挂 20 秒。
    async def fast_scenario() -> Dict[str, Any]:
        original = asyncio.wait_for

        async def patched(awaitable: Any, timeout: Any) -> Any:  # noqa: ANN401
            return await original(awaitable, 0.05)

        asyncio.wait_for = patched  # type: ignore[assignment]
        try:
            return await controller.revise(_request(), call_provider=call_provider)
        finally:
            asyncio.wait_for = original  # type: ignore[assignment]

    del scenario
    candidate = asyncio.run(fast_scenario())

    assert candidate["qualityPassed"] is False
    assert candidate["rejectedReason"] == REVISION_UNAVAILABLE_TIMEOUT
    assert candidate["fragments"] == []
    # §8.2：修订超时不重试，首稿仍是完整一章。
    assert attempts == 1


def test_a_provider_without_tool_support_is_unavailable_not_downgraded() -> None:
    controller = StoryLengthPrecisionController()

    async def call_provider(**_kwargs: Any) -> str:
        raise NotImplementedError("provider cannot honour tool schemas")

    candidate = asyncio.run(
        controller.revise(_request(), call_provider=call_provider)
    )

    # §7.2：不支持结构化工具就判定修订不可用，不降级成自由文本整章重写。
    assert candidate["rejectedReason"] == REVISION_UNAVAILABLE_NO_TOOL_SUPPORT
    assert candidate["qualityPassed"] is False
    assert candidate["fragments"] == []


def test_a_transport_error_keeps_the_draft() -> None:
    controller = StoryLengthPrecisionController()
    attempts = 0

    async def call_provider(**_kwargs: Any) -> str:
        nonlocal attempts
        attempts += 1
        raise ConnectionResetError("connection dropped")

    candidate = asyncio.run(
        controller.revise(_request(), call_provider=call_provider)
    )

    assert candidate["rejectedReason"] == REVISION_UNAVAILABLE_TRANSPORT
    assert candidate["errorType"] == "ConnectionResetError"
    assert attempts == 1


def test_an_empty_response_keeps_the_draft() -> None:
    controller = StoryLengthPrecisionController()

    async def call_provider(**_kwargs: Any) -> str:
        return ""

    candidate = asyncio.run(
        controller.revise(_request(), call_provider=call_provider)
    )
    assert candidate["rejectedReason"] == REVISION_UNAVAILABLE_EMPTY


def test_a_typed_tool_call_rejection_is_preserved_without_retrying() -> None:
    controller = StoryLengthPrecisionController()
    attempts = 0

    async def call_provider(**_kwargs: Any) -> str:
        nonlocal attempts
        attempts += 1
        raise StorydexToolCallRejected("tool_arguments_truncated")

    candidate = asyncio.run(
        controller.revise(_request(), call_provider=call_provider)
    )

    assert attempts == 1
    assert candidate["rejectedReason"] == "tool_arguments_truncated"
    assert candidate["qualityIssues"] == ["tool_arguments_truncated"]
    assert candidate["fragments"] == []


def test_an_illegal_patch_is_reported_not_applied() -> None:
    controller = StoryLengthPrecisionController()

    async def call_provider(**_kwargs: Any) -> str:
        # 扩写方向返回压缩操作：越权补丁必须整体拒绝。
        return json.dumps(
            {
                "version": 1,
                "direction": "expand",
                "operations": [
                    {
                        "op": "replace_range",
                        "fragmentOrder": 1,
                        "startParagraphId": "f1-p02",
                        "endParagraphId": "f1-p03",
                        "text": "改写后的正文。",
                    }
                ],
            },
            ensure_ascii=False,
        )

    candidate = asyncio.run(
        controller.revise(_request(), call_provider=call_provider)
    )
    assert candidate["qualityPassed"] is False
    assert candidate["rejectedReason"] == "tool_arguments_invalid_patch"
    assert candidate["patchRejectionReason"].startswith("operation_not_allowed")
    assert candidate["qualityIssues"][0] == "tool_arguments_invalid_patch"
    assert candidate["fragments"] == []


@pytest.mark.parametrize("direction", ["", "rewrite", "shorten_a_lot"])
def test_an_unknown_direction_never_calls_the_provider(direction: str) -> None:
    controller = StoryLengthPrecisionController()
    called = False

    async def call_provider(**_kwargs: Any) -> str:
        nonlocal called
        called = True
        return _expand_patch()

    candidate = asyncio.run(
        controller.revise(
            {**_request(), "direction": direction},
            call_provider=call_provider,
        )
    )

    assert called is False
    assert candidate["qualityPassed"] is False
