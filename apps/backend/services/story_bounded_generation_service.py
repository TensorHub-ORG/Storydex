"""Live prose path for one bounded story turn (plan §5.1–§5.2, §6.1, §7.3).

Once a TurnContract exists the prose phase is closed. Plan §4 forbids an
open-ended Agent thinking/tool round after the contract is known, so this
module — not the Agent loop — runs current chapter-scoped word-count story
turns: one draft call, at most one bounded length revision or independent
second draft, one write.

Two details carry most of the weight.

**Prompt discipline (§6.1).** The draft prompt states exactly one length
instruction: normally one character reference, or an explicitly enabled
paragraph quota. It never mixes the two, exposes an acceptance interval, or
asks for a character countdown. Those instructions reliably produce text
*about* word counts instead of prose, and the authoritative count belongs to
the program anyway. The wide band exists for measurement, not for the prompt.

**Splitting is local.** The model writes continuous prose and the program cuts
it on paragraph boundaries to match the planned fragment count. Asking the model
to emit N separately budgeted fragments would reintroduce the per-fragment quota
that chapter-level counting replaced.

The Provider adapter is injected, so this module stays testable without a
network and the call contract stays enforceable in one place.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

from services.story_call_accounting import (
    STORY_INITIAL_GENERATION_PURPOSE,
    STORY_LENGTH_REVISION_PURPOSE,
    STORY_SECOND_DRAFT_PURPOSE,
    StoryCallAccounting,
)
from services.story_elastic_manuscript_service import (
    ELASTIC_DRAFT_TOOL_NAME,
    ELASTIC_LENGTH_CONTROL_STRATEGY,
    ELASTIC_REPAIR_TOOL_NAME,
    FALLBACK_REPAIR_FAILED,
    apply_elastic_repair_pack,
    build_elastic_draft_messages,
    build_elastic_repair_messages,
    elastic_draft_tool_schema,
    elastic_repair_tool_schema,
    generated_overhead_ratio,
    literal_fact_anchors,
    repair_completion_cap,
    select_elastic_draft,
)
from services.story_length_patch_service import LENGTH_PATCH_TOOL_NAME
from services.story_length_precision_controller import (
    BOUNDED_REDRAFT_TOOL_NAME,
    FEEDBACK_BOUNDED_REDRAFT_STRATEGY,
    LOCAL_PATCH_STRATEGY,
    revision_strategy,
)
from services.story_preset_length_policy_service import strip_quantitative_length_directives
from services.story_prose_quality import (
    contextual_quality_issues,
    extract_story_prose,
)
from services.story_word_count_service import (
    ASYMMETRIC_SELECTION_STRATEGY,
    STORY_WORD_COUNT_RULE,
    chapter_length_tier_prompt,
    count_story_text_words,
    normalize_chapter_length_tier,
)

# §6.1 verbatim. The reference value is the only length fact the model receives.
DRAFT_REFERENCE_INSTRUCTION = (
    "本章参考长度约为 {reference} 个 Storydex 非空白字符。以人物动机、情节因果、节奏和"
    "自然收束为先；接近参考长度时压缩重复信息，不注水、不复刻前文收尾、不为字数突然"
    "赶进度。只输出正文。"
)

DRAFT_PARAGRAPH_QUOTA_INSTRUCTION = (
    "本章请写成 {minimum}-{maximum} 个自然段，以 {quota} 段为参考；自然段之间空一行。"
    "这是本轮唯一的篇幅指令：不要估算或追逐任何字数、字符数；每段的长度和密度服从项目"
    "上下文中的文风要求。以人物动机、情节因果、节奏和自然收束为先，不注水、不复刻前文"
    "收尾、不为篇幅突然赶进度。只输出正文。"
)

_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？…”」』])")


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def turn_plan_of(turn_contract: Dict[str, Any]) -> Dict[str, Any]:
    return _dict(_dict(turn_contract).get("turnPlan"))


def word_count_policy_of(turn_contract: Dict[str, Any]) -> Dict[str, Any]:
    return _dict(turn_plan_of(turn_contract).get("wordCountPolicy"))


def precision_of(turn_contract: Dict[str, Any]) -> Dict[str, Any]:
    return _dict(word_count_policy_of(turn_contract).get("precision"))


def _context_section(turn_contract: Dict[str, Any]) -> str:
    """Render the context the orchestrator already assembled for this turn.

    The blocks are reused rather than re-read: §14 item 3 asks the second call to
    avoid duplicate context reads, and the same argument applies to the first.
    """

    assembly = _dict(_dict(turn_contract).get("contextAssembly"))
    blocks = assembly.get("promptBlocks") if isinstance(assembly.get("promptBlocks"), list) else []
    rendered: List[str] = []
    for item in blocks:
        block = _dict(item)
        content = str(block.get("content") or "").strip()
        if not content:
            continue
        title = str(block.get("title") or block.get("id") or "").strip()
        rendered.append(f"## {title}\n{content}" if title else content)
    return "\n\n".join(rendered)


def build_draft_messages(
    *,
    prompt: str,
    turn_contract: Dict[str, Any],
) -> List[Dict[str, str]]:
    """Build the single draft prompt (§6.1).

    Everything the turn already decided — chapter action, authoritative paths,
    template — is stated as settled fact. The model is writing prose into a
    target that was validated before the call, not choosing where to write.
    """

    turn_plan = turn_plan_of(turn_contract)
    policy = word_count_policy_of(turn_contract)
    tier_mode = str(policy.get("mode") or "").strip().lower() == "tier"
    reference = int(
        policy.get("modelReferenceWordCount")
        or policy.get("target")
        or turn_plan.get("chapterWordCountTarget")
        or 0
    )
    paragraph_quota = max(0, int(policy.get("paragraphQuota") or 0))
    fragment_count = max(1, int(turn_plan.get("fragmentCount") or 1))
    template = _dict(turn_plan.get("selectedChapterTemplateDetail"))

    system_lines = [
        "你是中文长篇小说的执笔作者。本轮的目标章节、写入路径和章节动作已由程序判定完成，"
        "你只需要写正文。",
    ]
    if tier_mode:
        # The semantic tier belongs after the requested plot objective and before
        # preset/context prose rules, so it is added to user_lines below.
        pass
    elif paragraph_quota > 0:
        paragraph_minimum = max(
            1,
            int(policy.get("paragraphQuotaMinimum") or paragraph_quota),
        )
        paragraph_maximum = max(
            paragraph_minimum,
            int(policy.get("paragraphQuotaMaximum") or paragraph_quota),
        )
        paragraph_quota = min(
            paragraph_maximum,
            max(paragraph_minimum, paragraph_quota),
        )
        system_lines.append(
            DRAFT_PARAGRAPH_QUOTA_INSTRUCTION.format(
                minimum=paragraph_minimum,
                maximum=paragraph_maximum,
                quota=paragraph_quota,
            )
        )
    else:
        system_lines.extend(
            [
                DRAFT_REFERENCE_INSTRUCTION.format(reference=reference),
                f"计数规则（由程序执行，你不需要自己计算）：{STORY_WORD_COUNT_RULE}。",
            ]
        )
    system_lines.append(
        "不要输出章节标题、章节编号、小标题、字数说明、创作说明或工具调用；"
        "不要复述已经发生过的情节。"
    )
    if fragment_count > 1:
        # The split is programmatic, so the model is asked for continuous prose
        # with clear paragraph breaks rather than N budgeted sections.
        system_lines.append(
            "请输出连续正文，自然段之间空一行；程序会按段落把正文切分成本章的多个片段文件。"
        )

    context = _context_section(turn_contract)
    author_request = str(prompt or "").strip()
    if tier_mode:
        author_request, _removed_length_directives = strip_quantitative_length_directives(
            author_request
        )
        if not author_request:
            author_request = "按本轮项目上下文完成章节推进。"
    user_lines = [f"作者请求：{author_request}"]
    if tier_mode:
        user_lines.append(chapter_length_tier_prompt(policy.get("tier")))
    chapter_path = str(turn_plan.get("authoritativeChapterPath") or "").strip()
    if chapter_path:
        user_lines.append(f"本轮目标章节目录（已判定，不需要你决定）：{chapter_path}")
    if template.get("name") or template.get("id"):
        user_lines.append(
            f"章节模板：{str(template.get('name') or template.get('id'))}"
        )
    summary = str(turn_plan.get("chapterActionReason") or "").strip()
    if summary:
        user_lines.append(f"章节动作理由：{summary}")
    if context:
        user_lines.append("以下是本轮可用的项目上下文：\n" + context)

    return [
        {"role": "system", "content": "\n".join(system_lines)},
        {"role": "user", "content": "\n\n".join(user_lines)},
    ]


def _sentences(paragraph: str) -> List[str]:
    parts = [item.strip() for item in _SENTENCE_BOUNDARY.split(paragraph) if item.strip()]
    return parts or [paragraph.strip()]


def _paragraphs(text: str) -> List[str]:
    return [item.strip() for item in str(text or "").split("\n\n") if item.strip()]


def _split_publishable_prose(
    prose: str,
    *,
    fragment_count: int,
) -> List[Dict[str, str]]:
    """Cut already-extracted prose into the planned number of fragments.

    Fragment count is a structural gate: a short count fails the turn, so the
    split has to produce exactly what was planned. Paragraph boundaries are
    preferred, and sentences are used only when the draft has fewer paragraphs
    than the chapter template needs.
    """

    count = max(1, int(fragment_count))
    paragraphs = _paragraphs(prose)
    if not paragraphs:
        return []
    if count == 1:
        return [{"text": "\n\n".join(paragraphs)}]

    units = list(paragraphs)
    if len(units) < count:
        # Not enough paragraphs: fall back to sentences so every planned fragment
        # can receive text. An empty fragment would fail the structure gate and
        # throw away an otherwise usable draft.
        units = []
        for paragraph in paragraphs:
            units.extend(_sentences(paragraph))
    if len(units) < count:
        # Still too few pieces to fill the plan; the caller's structure gate will
        # reject this draft rather than silently write fewer fragments.
        return [{"text": item} for item in units]

    total = sum(len(item) for item in units)
    per_fragment = total / count
    groups: List[List[str]] = [[] for _ in range(count)]
    index = 0
    accumulated = 0
    for position, unit in enumerate(units):
        remaining_units = len(units) - position
        remaining_slots = count - index
        groups[index].append(unit)
        accumulated += len(unit)
        # Advance when this fragment has its share, but never leave a later
        # fragment without enough units to be non-empty.
        if index < count - 1 and (
            accumulated >= per_fragment * (index + 1) or remaining_units <= remaining_slots
        ):
            index += 1
    return [{"text": "\n\n".join(group)} for group in groups if group]


def split_draft_into_fragments(text: str, *, fragment_count: int) -> List[Dict[str, str]]:
    """Extract one Provider response and split only its publishable prose."""

    extraction = extract_story_prose(text)
    return _split_publishable_prose(
        extraction.prose,
        fragment_count=fragment_count,
    )


def draft_payload(
    text: str,
    *,
    turn_contract: Dict[str, Any],
    prompt: str = "",
    active_file: str = "",
) -> Dict[str, Any]:
    """Shape one draft into an increment payload for the constrained writer."""

    turn_plan = turn_plan_of(turn_contract)
    targets = turn_plan.get("fragmentTargets") if isinstance(turn_plan.get("fragmentTargets"), list) else []
    fragment_count = len(targets) or max(1, int(turn_plan.get("fragmentCount") or 1))
    extraction = extract_story_prose(text)
    fragments = _split_publishable_prose(
        extraction.prose,
        fragment_count=fragment_count,
    )
    extraction_audit = extraction.as_dict()
    extraction_audit.pop("prose", None)
    return {
        "prompt": str(prompt or ""),
        "activeFile": str(active_file or ""),
        "fragments": fragments,
        "proseExtraction": extraction_audit,
    }


class BoundedStoryGeneration:
    """Run one bounded turn against a live Provider adapter."""

    def __init__(
        self,
        *,
        adapter: Any,
        pipeline: Any,
        controller: Any,
        revision_adapter: Any = None,
        event_sink: Callable[[str, Dict[str, Any]], None] | None = None,
        commit_state: Dict[str, Any] | None = None,
        elastic_enabled: bool = False,
    ) -> None:
        self.adapter = adapter
        # The revision call gets its own adapter so it can be constructed with
        # zero transport retries (§9); sharing the draft adapter would let one
        # precise correction become three or four requests.
        self.revision_adapter = revision_adapter if revision_adapter is not None else adapter
        self.pipeline = pipeline
        self.controller = controller
        self.event_sink = event_sink
        self.elastic_enabled = bool(elastic_enabled)
        self.commit_state = commit_state if isinstance(commit_state, dict) else {}
        self.commit_state.setdefault("started", False)
        self.commit_state.setdefault("finished", False)
        self.accounting = StoryCallAccounting()
        self.draft_duration_ms = 0
        self.second_draft_duration_ms = 0
        self.revision_outcome: Dict[str, Any] = {}
        self.revision_strategy = ""
        self.draft_selection: Dict[str, Any] = {}

    def _emit(self, name: str, payload: Dict[str, Any]) -> None:
        if self.event_sink is not None:
            self.event_sink(name, dict(payload))

    def _mark_commit_started(self) -> None:
        self.commit_state["started"] = True
        self.commit_state["finished"] = False
        self._emit(
            "StoryCommitStarted",
            {"_type": "StoryCommitStarted", "_version": 1},
        )

    def _mark_commit_finished(self) -> None:
        self.commit_state["finished"] = True
        self._emit(
            "StoryCommitFinished",
            {"_type": "StoryCommitFinished", "_version": 1},
        )

    async def run(
        self,
        workspace_root: Path,
        *,
        trace_id: str,
        turn_contract: Dict[str, Any],
        prompt: str = "",
        active_file: str = "",
    ) -> Dict[str, Any]:
        policy = word_count_policy_of(turn_contract)
        tier_mode = str(policy.get("mode") or "").strip().lower() == "tier"
        chapter_length_tier = normalize_chapter_length_tier(policy.get("tier"))
        asymmetric_enabled = (
            False
            if tier_mode
            else bool(_dict(policy.get("asymmetric")).get("enabled"))
        )
        precision_enabled = (
            not tier_mode
            and
            bool(precision_of(turn_contract).get("enabled"))
            and not asymmetric_enabled
        )
        elastic_active = self.elastic_enabled and not asymmetric_enabled and not tier_mode
        target = max(
            1,
            int(
                policy.get("target")
                or turn_plan_of(turn_contract).get("chapterWordCountTarget")
                or (
                    int(policy.get("preferredMinimum") or 1)
                    + int(policy.get("preferredMaximum") or 1)
                )
                // 2
            )
            or 1,
        )
        messages = (
            build_elastic_draft_messages(
                prompt=prompt,
                context=_context_section(turn_contract),
                target=target,
                chapter_path=str(
                    turn_plan_of(turn_contract).get("authoritativeChapterPath") or ""
                ),
            )
            if elastic_active
            else build_draft_messages(prompt=prompt, turn_contract=turn_contract)
        )

        async def generate_draft() -> Dict[str, Any]:
            started = time.perf_counter()
            if elastic_active:
                raw_draft = await self.adapter.complete_tool_call(
                    messages=messages,
                    tool=elastic_draft_tool_schema(),
                    purpose=STORY_INITIAL_GENERATION_PURPOSE,
                    tool_name=ELASTIC_DRAFT_TOOL_NAME,
                    metadata={
                        "target": target,
                        "trace": str(trace_id or ""),
                        "strategy": ELASTIC_LENGTH_CONTROL_STRATEGY,
                    },
                )
                selected = select_elastic_draft(
                    raw_draft,
                    target=target,
                    precise=precision_enabled,
                    source_context=_context_section(turn_contract),
                    user_task=prompt,
                )
                raw_object = _dict(raw_draft)
                selected["endingHook"] = str(raw_object.get("endingHook") or "")
                selected["generatedOverheadRatio"] = generated_overhead_ratio(
                    getattr(self.adapter, "last_completion_tokens", None),
                    int(policy.get("naturalBaselineCompletionTokens") or 0) or None,
                )
                selected["precisionAchieved"] = (
                    bool(selected.get("precisionAchieved"))
                    if precision_enabled
                    else None
                )
                self.draft_selection = dict(selected)
                text = str(selected.get("text") or "")
            else:
                text = await self.adapter.complete(
                    messages=messages,
                    purpose=STORY_INITIAL_GENERATION_PURPOSE,
                    metadata=(
                        {
                            "tier": chapter_length_tier,
                            "trace": str(trace_id or ""),
                            "promptVersion": str(policy.get("promptVersion") or ""),
                        }
                        if tier_mode
                        else {"target": target, "trace": str(trace_id or "")}
                    ),
                )
            self.draft_duration_ms = int((time.perf_counter() - started) * 1000)
            # Transport retries belong to the call they happened inside. The
            # pipeline already recorded one attempt for this logical call, so
            # only the retries are added here.
            for _ in range(int(getattr(self.adapter, "provider_retries", 0) or 0)):
                self.accounting.record_transport_retry(STORY_INITIAL_GENERATION_PURPOSE)
                self.accounting.record_provider_attempt(STORY_INITIAL_GENERATION_PURPOSE)
            payload = draft_payload(
                str(text or ""),
                turn_contract=turn_contract,
                prompt=prompt,
                active_file=active_file,
            )
            if elastic_active:
                payload["lengthControl"] = {
                    "lengthControlStrategy": ELASTIC_LENGTH_CONTROL_STRATEGY,
                    "canonicalWordCount": int(
                        self.draft_selection.get("canonicalWordCount") or 0
                    ),
                    "normalBandPassed": bool(
                        self.draft_selection.get("normalBandPassed")
                    ),
                    "precisionAchieved": self.draft_selection.get(
                        "precisionAchieved"
                    ),
                    "selectedEditIds": [
                        str(item)
                        for item in list(
                            self.draft_selection.get("selectedEditIds") or []
                        )
                    ],
                    "rejectedEditIds": [
                        str(item)
                        for item in list(
                            self.draft_selection.get("rejectedEditIds") or []
                        )
                    ],
                    "rejectedEditReasonCounts": {
                        str(key): int(value)
                        for key, value in _dict(
                            self.draft_selection.get("rejectedEditReasonCounts")
                        ).items()
                    },
                    "evaluatedCombinationCount": int(
                        self.draft_selection.get("evaluatedCombinationCount") or 0
                    ),
                    "lengthFallbackReason": str(
                        self.draft_selection.get("lengthFallbackReason") or ""
                    ),
                    "generatedOverheadRatio": self.draft_selection.get(
                        "generatedOverheadRatio"
                    ),
                    "endingHook": str(
                        self.draft_selection.get("endingHook") or ""
                    ),
                }
            if asymmetric_enabled or tier_mode:
                candidate_text = "\n\n".join(
                    str(_dict(item).get("text") or "")
                    for item in list(payload.get("fragments") or [])
                )
                quality_issues = contextual_quality_issues(
                    candidate_text,
                    source_context=_context_section(turn_contract),
                    user_task=prompt,
                )
                payload["qualityPassed"] = not quality_issues
                payload["qualityIssues"] = quality_issues
            return payload

        async def generate_second_draft() -> Dict[str, Any]:
            started = time.perf_counter()
            text = await self.revision_adapter.complete(
                messages=messages,
                purpose=STORY_SECOND_DRAFT_PURPOSE,
                metadata={
                    "target": target,
                    "trace": str(trace_id or ""),
                    "strategy": ASYMMETRIC_SELECTION_STRATEGY,
                },
            )
            self.second_draft_duration_ms = int(
                (time.perf_counter() - started) * 1000
            )
            for _ in range(
                int(getattr(self.revision_adapter, "provider_retries", 0) or 0)
            ):
                self.accounting.record_transport_retry(STORY_SECOND_DRAFT_PURPOSE)
                self.accounting.record_provider_attempt(STORY_SECOND_DRAFT_PURPOSE)
            payload = draft_payload(
                str(text or ""),
                turn_contract=turn_contract,
                prompt=prompt,
                active_file=active_file,
            )
            candidate_text = "\n\n".join(
                str(_dict(item).get("text") or "")
                for item in list(payload.get("fragments") or [])
            )
            quality_issues = contextual_quality_issues(
                candidate_text,
                source_context=_context_section(turn_contract),
                user_task=prompt,
            )
            payload["qualityPassed"] = not quality_issues
            payload["qualityIssues"] = quality_issues
            return payload

        async def call_provider(
            *,
            messages: List[Dict[str, str]],
            tool: Dict[str, Any],
            max_completion_tokens: int = 0,
        ) -> Any:
            selected_tool_name = str(tool.get("name") or LENGTH_PATCH_TOOL_NAME)
            strategy = (
                FEEDBACK_BOUNDED_REDRAFT_STRATEGY
                if selected_tool_name == BOUNDED_REDRAFT_TOOL_NAME
                else LOCAL_PATCH_STRATEGY
            )
            return await self.revision_adapter.complete_tool_call(
                messages=messages,
                tool=tool,
                purpose=STORY_LENGTH_REVISION_PURPOSE,
                tool_name=selected_tool_name,
                max_completion_tokens=max_completion_tokens,
                metadata={
                    "target": target,
                    "trace": str(trace_id or ""),
                    "strategy": strategy,
                },
            )

        async def revise_elastic(request: Dict[str, Any]) -> Dict[str, Any]:
            selected_strategy = ELASTIC_LENGTH_CONTROL_STRATEGY
            self.revision_strategy = selected_strategy
            self._emit(
                "StoryLengthRevisionStarted",
                {
                    "_type": "StoryLengthRevisionStarted",
                    "_version": 1,
                    "strategy": selected_strategy,
                    "direction": str(request.get("direction") or ""),
                    "initialWordCount": int(request.get("draftWordCount") or 0),
                    "maximumRevisionCalls": 1,
                },
            )
            source_payload = _dict(request.get("draftPayload"))
            source_fragments = list(source_payload.get("fragments") or [])
            source_text = "\n\n".join(
                str(_dict(item).get("text") or _dict(item).get("content") or "")
                for item in source_fragments
            )
            length_control = _dict(source_payload.get("lengthControl"))
            source_context = _context_section(turn_contract)
            protected_facts = literal_fact_anchors(source_context, source_text)
            status = _dict(request.get("wordCountStatus"))
            current_count = int(request.get("draftGeneratedWordCount") or 0)
            precision_low = int(status.get("precisionMinimum") or target)
            precision_high = int(status.get("precisionMaximum") or target)
            gap = (
                precision_low - current_count
                if current_count < precision_low
                else current_count - precision_high
                if current_count > precision_high
                else 0
            )
            completion_cap = repair_completion_cap(gap)
            repair_messages = build_elastic_repair_messages(
                manuscript=source_text,
                target=target,
                current_count=current_count,
                ending_hook=str(length_control.get("endingHook") or ""),
                protected_fact_anchors=protected_facts,
            )
            started = time.perf_counter()
            try:
                raw_repair = await asyncio.wait_for(
                    self.revision_adapter.complete_tool_call(
                        messages=repair_messages,
                        tool=elastic_repair_tool_schema(),
                        purpose=STORY_LENGTH_REVISION_PURPOSE,
                        tool_name=ELASTIC_REPAIR_TOOL_NAME,
                        max_completion_tokens=completion_cap,
                        metadata={
                            "target": target,
                            "trace": str(trace_id or ""),
                            "strategy": selected_strategy,
                            "currentWordCount": current_count,
                            "precisionBand": [precision_low, precision_high],
                        },
                    ),
                    timeout=60.0,
                )
                repair = apply_elastic_repair_pack(
                    source_text,
                    raw_repair,
                    target=target,
                    ending_hook=str(length_control.get("endingHook") or ""),
                    source_context=source_context,
                    user_task=prompt,
                    protected_fact_anchors=protected_facts,
                )
            except Exception as exc:  # noqa: BLE001 - the complete first draft remains writable
                rejection_reason = str(getattr(exc, "reason", "") or type(exc).__name__)
                repair = {
                    "text": source_text,
                    "accepted": False,
                    "qualityPassed": False,
                    "selectedEditIds": [],
                    "rejectionReasons": [rejection_reason],
                    "lengthFallbackReason": FALLBACK_REPAIR_FAILED,
                    "candidateWordCount": 0,
                }

            accepted = bool(repair.get("accepted"))
            rejection_reasons = [
                str(item)
                for item in list(repair.get("rejectionReasons") or [])
                if str(item)
            ]
            if not accepted and not rejection_reasons:
                rejection_reasons = [
                    str(repair.get("lengthFallbackReason") or FALLBACK_REPAIR_FAILED)
                ]
            first_edit_ids = [
                str(item) for item in list(length_control.get("selectedEditIds") or [])
            ]
            repair_edit_ids = [
                str(item) for item in list(repair.get("selectedEditIds") or [])
            ]
            next_length_control = {
                **length_control,
                "lengthControlStrategy": ELASTIC_LENGTH_CONTROL_STRATEGY,
                "normalBandPassed": (
                    bool(repair.get("normalBandPassed"))
                    if accepted
                    else bool(length_control.get("normalBandPassed"))
                ),
                "precisionAchieved": (
                    bool(repair.get("precisionAchieved"))
                    if accepted
                    else bool(length_control.get("precisionAchieved"))
                ),
                "selectedEditIds": sorted(
                    set(first_edit_ids + (repair_edit_ids if accepted else []))
                ),
                "lengthFallbackReason": str(
                    repair.get("lengthFallbackReason") or FALLBACK_REPAIR_FAILED
                ),
            }
            if accepted:
                outcome = draft_payload(
                    str(repair.get("text") or source_text),
                    turn_contract=turn_contract,
                    prompt=prompt,
                    active_file=active_file,
                )
                outcome["qualityPassed"] = True
            else:
                outcome = {
                    "fragments": [],
                    "qualityPassed": False,
                    "qualityIssues": rejection_reasons,
                }
            outcome["strategy"] = selected_strategy
            outcome["providerCallMade"] = True
            outcome["lengthControl"] = next_length_control
            outcome["repairAccepted"] = accepted
            outcome["candidateWordCount"] = int(
                repair.get("candidateWordCount") or 0
            )
            outcome["budget"] = {
                "maxCompletionTokens": completion_cap,
                "deadlineSeconds": 60,
                "capApplied": bool(
                    getattr(self.revision_adapter, "last_cap_applied", False)
                ),
            }
            outcome["completionTokens"] = getattr(
                self.revision_adapter,
                "last_completion_tokens",
                None,
            )
            self.revision_outcome = dict(outcome)
            self._emit(
                "StoryLengthRevisionResult",
                {
                    "_type": "StoryLengthRevisionResult",
                    "_version": 1,
                    "strategy": selected_strategy,
                    "candidateWordCount": int(
                        repair.get("candidateWordCount") or 0
                    ),
                    "accepted": accepted,
                    "outcome": "candidate" if accepted else "rejected",
                    "rejectionReasons": (
                        []
                        if accepted
                        else rejection_reasons
                    ),
                    "providerDurationMs": int(
                        (time.perf_counter() - started) * 1000
                    ),
                    "completionTokens": outcome["completionTokens"],
                    "capApplied": outcome["budget"]["capApplied"],
                    "budget": dict(outcome["budget"]),
                },
            )
            return outcome

        async def revise(request: Dict[str, Any]) -> Dict[str, Any]:
            if elastic_active:
                return await revise_elastic(request)
            selected_strategy = revision_strategy(request)
            self.revision_strategy = selected_strategy
            self._emit(
                "StoryLengthRevisionStarted",
                {
                    "_type": "StoryLengthRevisionStarted",
                    "_version": 1,
                    "strategy": selected_strategy,
                    "direction": str(request.get("direction") or ""),
                    "initialWordCount": int(request.get("draftWordCount") or 0),
                    "maximumRevisionCalls": 1,
                },
            )
            started = time.perf_counter()
            budget_policy: Dict[str, Any] = {}
            budget_policy_resolver = getattr(
                self.revision_adapter,
                "revision_budget_policy",
                None,
            )
            if callable(budget_policy_resolver):
                try:
                    resolved_policy = budget_policy_resolver()
                    if inspect.isawaitable(resolved_policy):
                        resolved_policy = await resolved_policy
                    if isinstance(resolved_policy, dict):
                        budget_policy = dict(resolved_policy)
                except Exception:  # noqa: BLE001 - Provider call reports its own failure
                    budget_policy = {}
            outcome = await self.controller.revise(
                request,
                call_provider=call_provider,
                chapter_context=_context_section(turn_contract),
                user_task=prompt,
                draft_completion_tokens=int(
                    getattr(self.adapter, "last_completion_tokens", 0) or 0
                ),
                draft_duration_ms=self.draft_duration_ms,
                budget_policy=budget_policy,
            )
            revision_completion_tokens = getattr(
                self.revision_adapter,
                "last_completion_tokens",
                None,
            )
            cap_applied = bool(
                getattr(self.revision_adapter, "last_cap_applied", False)
            )
            outcome["budget"] = {
                **dict(_dict(outcome.get("budget"))),
                "capApplied": cap_applied,
            }
            outcome["completionTokens"] = revision_completion_tokens
            outcome_strategy = str(outcome.get("strategy") or selected_strategy)
            outcome["strategy"] = outcome_strategy
            self.revision_strategy = outcome_strategy
            self.revision_outcome = dict(outcome)
            candidate_count = count_story_text_words(
                "\n\n".join(
                    str(_dict(item).get("text") or "")
                    for item in list(outcome.get("fragments") or [])
                )
            )
            paragraph_diagnostics: Dict[str, Any] = {}
            if outcome.get("redraftParagraphCount") is not None:
                paragraph_diagnostics["redraftParagraphCount"] = int(
                    outcome.get("redraftParagraphCount") or 0
                )
            suggested_paragraph_range = outcome.get(
                "suggestedRedraftParagraphRange"
            )
            if isinstance(suggested_paragraph_range, list):
                paragraph_diagnostics["suggestedRedraftParagraphRange"] = [
                    int(item) for item in suggested_paragraph_range
                ]
            if "redraftParagraphRangeAdhered" in outcome:
                paragraph_diagnostics["redraftParagraphRangeAdhered"] = bool(
                    outcome.get("redraftParagraphRangeAdhered")
                )
            self._emit(
                "StoryLengthRevisionResult",
                {
                    "_type": "StoryLengthRevisionResult",
                    "_version": 1,
                    "strategy": outcome_strategy,
                    "candidateWordCount": candidate_count,
                    "accepted": bool(outcome.get("qualityPassed")) and candidate_count > 0,
                    "outcome": "candidate" if outcome.get("fragments") else "unavailable",
                    "rejectionReasons": [
                        str(item) for item in list(outcome.get("qualityIssues") or [])
                    ],
                    "providerDurationMs": int((time.perf_counter() - started) * 1000),
                    "completionTokens": revision_completion_tokens,
                    "capApplied": cap_applied,
                    "budget": dict(_dict(outcome.get("budget"))),
                    **paragraph_diagnostics,
                },
            )
            return outcome

        result = await self.pipeline.run(
            workspace_root,
            trace_id=trace_id,
            turn_contract=turn_contract,
            generate_draft=generate_draft,
            generate_second_draft=(
                generate_second_draft if asymmetric_enabled else None
            ),
            revise=revise if precision_enabled else None,
            accounting=self.accounting,
            on_commit_started=self._mark_commit_started,
            on_commit_finished=self._mark_commit_finished,
        )
        selection = _dict(result.get("selection"))
        draft_status = _dict(selection.get("draftStatus"))
        self._emit(
            "StoryDraftMeasured",
            {
                "_type": "StoryDraftMeasured",
                "_version": 1,
                "initialWordCount": int(result.get("draftWordCount") or 0),
                "retainedWordCount": int(result.get("retainedWordCount") or 0),
                "generatedWordCount": int(
                    result.get("draftGeneratedWordCount") or 0
                ),
                "completionTokens": getattr(
                    self.adapter,
                    "last_completion_tokens",
                    None,
                ),
                "providerDurationMs": self.draft_duration_ms,
                "capApplied": bool(
                    getattr(self.adapter, "last_cap_applied", False)
                ),
                **(
                    {
                        "wordCountScope": str(policy.get("scope") or ""),
                        "actualWordCount": int(
                            result.get("draftWordCount") or 0
                        ),
                        "resultingWordCount": int(
                            result.get("resultingWordCount") or 0
                        ),
                        "chapterLengthTier": chapter_length_tier,
                        "tierHit": bool(draft_status.get("tierHit")),
                        "tierDeviation": str(
                            draft_status.get("tierDeviation") or ""
                        ),
                        "machineQualityPassed": bool(
                            selection.get("draftQualityPassed")
                        ),
                    }
                    if tier_mode
                    else {
                        "target": int(result.get("target") or target),
                        "normalBand": [
                            int(policy.get("normalMinimum") or 0),
                            int(policy.get("normalMaximum") or 0),
                        ],
                        "precisionBand": [
                            int(precision_of(turn_contract).get("minimum") or 0),
                            int(precision_of(turn_contract).get("maximum") or 0),
                        ],
                        "normalBandPassed": bool(
                            draft_status.get("normalBandPassed")
                        ),
                        "precisionBandPassed": bool(
                            draft_status.get("precisionBandPassed")
                        ),
                    }
                ),
                "calibrationStatus": str(
                    _dict(policy.get("calibration")).get(
                        "status" if tier_mode else "strength"
                    )
                    or ""
                ),
            },
        )
        call_accounting = _dict(result.get("callAccounting"))
        second_draft_called = int(call_accounting.get("secondDraftCalls") or 0) > 0
        second_draft_completion_tokens = (
            getattr(self.revision_adapter, "last_completion_tokens", None)
            if second_draft_called
            else None
        )
        if second_draft_called:
            second_status = _dict(selection.get("secondDraftStatus"))
            self._emit(
                "StorySecondDraftMeasured",
                {
                    "_type": "StorySecondDraftMeasured",
                    "_version": 1,
                    "secondDraftWordCount": int(
                        result.get("secondDraftWordCount")
                        or selection.get("secondDraftWordCount")
                        or 0
                    ),
                    "generatedWordCount": int(
                        result.get("secondDraftGeneratedWordCount")
                        or selection.get("secondDraftGeneratedWordCount")
                        or 0
                    ),
                    "completionTokens": second_draft_completion_tokens,
                    "providerDurationMs": self.second_draft_duration_ms,
                    "capApplied": bool(
                        getattr(self.revision_adapter, "last_cap_applied", False)
                    ),
                    "selected": str(selection.get("source") or "") == "second_draft",
                    "qualityPassed": bool(
                        selection.get("secondDraftQualityPassed")
                    ),
                    "hardMinimumPassed": bool(
                        second_status.get("hardMinimumPassed")
                    ),
                    "aboveSoftMaximum": bool(
                        second_status.get("aboveSoftMaximum")
                    ),
                    "runtimeSafetyExceeded": bool(
                        second_status.get("runtimeSafetyExceeded")
                    ),
                    "runtimeSafetyMaximum": int(
                        second_status.get("runtimeSafetyMaximum") or 0
                    ),
                    "asymmetricLengthLoss": selection.get("asymmetricLengthLoss"),
                },
            )
        result["revisionOutcome"] = dict(self.revision_outcome)
        result["revisionStrategy"] = self.revision_strategy
        result["draftDurationMs"] = self.draft_duration_ms
        result["secondDraftDurationMs"] = (
            self.second_draft_duration_ms if second_draft_called else None
        )
        result["draftCompletionTokens"] = getattr(
            self.adapter,
            "last_completion_tokens",
            None,
        )
        result["secondDraftCompletionTokens"] = second_draft_completion_tokens
        result["revisionCompletionTokens"] = (
            None
            if asymmetric_enabled
            else getattr(
                self.revision_adapter,
                "last_completion_tokens",
                None,
            )
        )
        result["revisionCapApplied"] = bool(
            getattr(self.revision_adapter, "last_cap_applied", False)
        )
        return result
