from __future__ import annotations

import json
import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Protocol

from services.story_prose_quality import (
    clean_generated_text,
    contextual_quality_issues,
    mechanical_issues,
    normalized_character_ngrams,
)
from services.story_word_count_service import count_story_text_words


SEMANTIC_BUDGET_STRATEGY = "semantic_budget_v1"
SEMANTIC_BUDGET_RESULT_VERSION = 1
_MODEL_REFERENCE_GAIN_MAX = 4.0
_MODEL_REFERENCE_MIN_CHARS = 180
_MODEL_REFERENCE_MIN_RATIO = 0.25
_REVISION_SCENE_FLOOR_RATIO = 0.60
_REVISION_COMPRESSION_FLOOR_RATIO = 0.30
_REVISION_VERIFIED_SCENE_FLOOR_RATIO = 0.40
_REVISION_VERIFIED_COMPRESSION_FLOOR_RATIO = 0.20
_FINAL_SCENE_CAPACITY_SAFE_FLOOR_RATIO = 0.50
_REVISION_PLAN_NGRAM_SIZE = 2
_REVISION_PLAN_MIN_ANCHORS = 8
_REVISION_DEVELOPMENT_MIN_RETENTION = 0.45
_REVISION_EXIT_HOOK_MIN_RETENTION = 0.60
_REVISION_DEVELOPMENT_PARAPHRASE_MIN_RETENTION = 0.25
_REVISION_EXIT_HOOK_PARAPHRASE_MIN_RETENTION = 0.20
_REVISION_DEVELOPMENT_MIN_CHARACTER_RETENTION = 0.60
_REVISION_EXIT_HOOK_MIN_CHARACTER_RETENTION = 0.50
_REVISION_EXIT_TAIL_RATIO = 0.40
_SEVERE_EARLY_UNDERSHOOT_RATIO = 0.70
_PLAN_DEVELOPMENT_MAX_CHARS = 300
_CONTEXT_REPETITION_NGRAM_SIZE = 24
_CONTEXT_REPETITION_SHARED_LIMIT = 8
_PLAN_ITEM_SEPARATOR_RE = re.compile(
    r"[、；;]|同时|与此同时|以及|外加|又(?:有|出现)|还(?:有|出现)"
)
_PLAN_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,39}$")
_SHORT_TERM_GOAL_STATES = {"completed", "partial", "delayed", "failed", "unchanged"}
_HAZARD_OUTCOMES = {
    "escaped",
    "avoided",
    "endured",
    "temporarily_resolved",
    "resolved",
    "unresolved",
}
_HAZARD_ROLES = {"none", "foreshadow", "pressure", "aftermath"}
_CLUE_ROLES = {"none", "seed", "reveal", "carry"}
_ABILITY_PURPOSES = {
    "escape",
    "temporary_relief",
    "environmental_aid",
    "controlled_attempt",
}
_ELEMENTAL_REFERENCE_PATTERNS = tuple(
    re.compile(
        rf"{element}(?:灵气|属性|系|入|归|行|润|沉|护|修|愈|驱|灼|挡|防|增强|强化)"
    )
    for element in "木火土金水"
)
_ELEMENTAL_CONTROL_DETAIL_RE = re.compile(
    r"调动|引导|凝聚|归位|分别|各自|滋润|稳固|修复|愈合|驱散|灼|"
    r"增强|强化|防御|抵挡|压制"
)
_LIMITED_ABILITY_VICTORY_RE = re.compile(
    r"(?:灵气|五行).{0,50}(?:击退|击败|重创|斩杀|击杀|震退)|"
    r"(?:击退|击败|重创|斩杀|击杀|震退).{0,50}(?:灵气|五行)"
)
_ABILITY_PLAN_MASTERY_RE = re.compile(
    r"按时辰|比例|重新分配|逐一|依次|轮流|切换|分别|各自控制|主导|"
    r"精确|精准|自创|自悟|熟练"
)
_UNREFERENCED_ABILITY_USE_RE = re.compile(
    r"(?P<verb_before>调动|运转|引导|操控|控制|凝聚|维持|平衡).{0,20}(?:灵气|五行)|"
    r"(?:灵气|五行).{0,20}(?P<verb_after>调动|运转|引导|操控|控制|凝聚|维持)"
)
_UNREFERENCED_ABILITY_STABILIZATION_RE = re.compile(
    r"(?P<stabilize_verb>稳住|压制住?).{0,12}(?:灵气|五行)"
)
_NON_ABILITY_STABILIZATION_TARGET_RE = re.compile(
    r"(?:稳住|压制住?).{0,8}(?:呼吸|身形|心神|气血|脚步|伤势)"
)
_NEGATED_ABILITY_USE_RE = re.compile(
    r"(?:不能|无法|无力|未能|没能|没有(?:能力|余力)?|难以|不敢|拒绝|停止|放弃|"
    r"失去(?:能力|控制)|不受控制|失控|自行|自发).{0,8}$"
)
_CLUE_UTILITY_RE = re.compile(
    r"可用作|作为.{0,8}媒介|用来.{0,8}(?:稳定|修炼|提升|恢复|强化)|"
    r"帮助.{0,8}(?:稳定|修炼|提升|恢复|强化)|安抚.{0,8}灵气|直接.{0,8}(?:补充|恢复)"
)
_CLUE_OBJECT_PATTERN = r"(?:线索|灵石|矿石|石头|碎片|结晶|药草|物体|遗物|玉简)"
_CLUE_BENEFIT_PATTERN = (
    r"(?:安抚|稳定|稳住|补充|恢复|提升|强化|维持.{0,8}(?:平衡|清明)|"
    r"帮助.{0,8}(?:脱身|行动|恢复)|"
    r"(?:刺痛|疼痛|伤势|不适|反噬).{0,8}(?:被)?(?:压下|减轻|缓解|消退|平复))"
)
_CLUE_BODY_UTILITY_RE = re.compile(
    rf"{_CLUE_OBJECT_PATTERN}(?:本身|自身|直接|立刻|顿时|随即|便|就|竟然|悄然|"
    rf"可以|能够|能)*{_CLUE_BENEFIT_PATTERN}|"
    rf"{_CLUE_OBJECT_PATTERN}.{{0,30}}(?:石中|其中|其内|物中|内部|所含|蕴含|散发|"
    rf"释放|传来|涌出|流出|冷香|香味|气息).{{0,16}}{_CLUE_BENEFIT_PATTERN}|"
    rf"(?:借助|依靠|靠着|利用|使用).{{0,12}}{_CLUE_OBJECT_PATTERN}.{{0,16}}"
    rf"{_CLUE_BENEFIT_PATTERN}|"
    rf"{_CLUE_BENEFIT_PATTERN}.{{0,24}}(?:来自|源于|依靠|借助|得益于).{{0,12}}"
    rf"{_CLUE_OBJECT_PATTERN}",
    re.DOTALL,
)


def _ability_use_is_negated(source: str, verb_start: int, verb: str) -> bool:
    prefix = source[max(0, verb_start - 20) : verb_start]
    prefix = re.split(
        r"[。！？!?；;，,\n]|但|却|仍然?|然而|可是|不过",
        prefix,
    )[-1]
    return bool(_NEGATED_ABILITY_USE_RE.search(prefix + verb))


def _uses_unreferenced_ability(text: str) -> bool:
    source = str(text or "")
    for match in _UNREFERENCED_ABILITY_USE_RE.finditer(source):
        verb_group = "verb_before" if match.group("verb_before") else "verb_after"
        if not _ability_use_is_negated(
            source,
            match.start(verb_group),
            str(match.group(verb_group)),
        ):
            return True
    for match in _UNREFERENCED_ABILITY_STABILIZATION_RE.finditer(source):
        if _NON_ABILITY_STABILIZATION_TARGET_RE.search(match.group(0)):
            continue
        if not _ability_use_is_negated(
            source,
            match.start("stabilize_verb"),
            str(match.group("stabilize_verb")),
        ):
            return True
    return False


class StoryGenerationAdapter(Protocol):
    async def complete(
        self,
        *,
        messages: list[Dict[str, str]],
        purpose: str,
        metadata: Dict[str, Any],
    ) -> str:
        ...


@dataclass(frozen=True)
class SemanticBudgetRequest:
    product_target_word_count: int
    user_task: str
    source_context: str
    constraint_context: str = ""
    scene_count: int = 0
    maximum_scene_revisions: int = 2
    internal_tolerance_ratio: float = 0.20
    final_tolerance_ratio: float = 0.15


@dataclass(frozen=True)
class SemanticBudgetResult:
    status: str
    strategy: str
    target_word_count: int
    generated_word_count: int
    acceptance_minimum: int
    acceptance_maximum: int
    within_acceptance: bool
    text: str
    plan: list[Dict[str, Any]]
    scenes: list[Dict[str, Any]]
    events: list[Dict[str, Any]]
    provider_calls: int
    revision_attempts: int
    revision_acceptances: int
    duration_ms: int
    mechanical_issues: list[str] = field(default_factory=list)
    error: Dict[str, Any] = field(default_factory=dict)
    plan_contract: Dict[str, Any] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SemanticBudgetController:
    def __init__(
        self,
        *,
        counter: Callable[[str], int] = count_story_text_words,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._counter = counter
        self._clock = clock

    async def generate(
        self,
        request: SemanticBudgetRequest,
        adapter: StoryGenerationAdapter,
        *,
        event_sink: Callable[[str, Dict[str, Any]], None] | None = None,
    ) -> SemanticBudgetResult:
        self._validate_request(request)
        started = self._clock()
        target = int(request.product_target_word_count)
        scene_count = request.scene_count or automatic_scene_count(target)
        acceptance_minimum = int(round(target * 0.70))
        acceptance_maximum = int(round(target * 1.30))
        internal_upper_bound = min(
            acceptance_maximum,
            int(round(target * (1.0 + request.internal_tolerance_ratio))),
        )
        events: list[Dict[str, Any]] = []
        scenes: list[Dict[str, Any]] = []
        plan: list[Dict[str, Any]] = []
        plan_contract: Dict[str, Any] = {}
        generated_scenes: list[str] = []
        observed_gains: list[float] = []
        provider_calls = 0

        def emit(state: str, payload: Dict[str, Any] | None = None) -> None:
            event = {
                "_type": "SemanticBudgetProgress",
                "_version": 1,
                "strategy": SEMANTIC_BUDGET_STRATEGY,
                "state": state,
                **dict(payload or {}),
            }
            events.append(event)
            if event_sink is not None:
                event_sink("SemanticBudgetProgress", dict(event))

        async def complete(
            *,
            messages: list[Dict[str, str]],
            purpose: str,
            metadata: Dict[str, Any],
        ) -> str:
            nonlocal provider_calls
            provider_calls += 1
            return await adapter.complete(
                messages=messages,
                purpose=purpose,
                metadata=metadata,
            )

        def finish(
            status: str,
            *,
            error: Dict[str, Any] | None = None,
            final_text: str | None = None,
        ) -> SemanticBudgetResult:
            text = final_text if final_text is not None else "\n\n".join(generated_scenes).strip()
            generated_count = self._counter(text)
            issues = contextual_quality_issues(
                text,
                source_context=request.source_context,
                user_task=request.user_task,
            )
            within_acceptance = acceptance_minimum <= generated_count <= acceptance_maximum
            emit(
                "COMPLETED" if status == "completed" else "FAILED",
                {
                    "status": status,
                    "generatedWordCount": generated_count,
                    "withinAcceptance": within_acceptance,
                },
            )
            return SemanticBudgetResult(
                status=status,
                strategy=SEMANTIC_BUDGET_STRATEGY,
                target_word_count=target,
                generated_word_count=generated_count,
                acceptance_minimum=acceptance_minimum,
                acceptance_maximum=acceptance_maximum,
                within_acceptance=within_acceptance,
                text=text,
                plan=plan,
                scenes=scenes,
                events=events,
                provider_calls=provider_calls,
                revision_attempts=sum(bool(item.get("revisionTriggered")) for item in scenes),
                revision_acceptances=sum(bool(item.get("revisionAccepted")) for item in scenes),
                duration_ms=max(0, int(round((self._clock() - started) * 1000))),
                mechanical_issues=issues,
                error=dict(error or {}),
                plan_contract=plan_contract,
            )

        emit("PLANNING", {"sceneCount": scene_count, "targetWordCount": target})
        try:
            raw_plan = await complete(
                messages=planning_messages(request, scene_count),
                purpose="semantic_budget_plan",
                metadata={"sceneCount": scene_count, "targetWordCount": target},
            )
        except Exception as exc:
            return finish("failed_provider", error=safe_provider_error(exc, stage="planning"))

        try:
            plan_contract, plan = parse_story_plan(raw_plan, scene_count)
        except ValueError as first_error:
            emit("REPAIRING_PLAN", {"reason": str(first_error)[:240]})
            try:
                repaired_plan = await complete(
                    messages=planning_repair_messages(
                        raw_plan,
                        scene_count,
                        validation_error=str(first_error),
                    ),
                    purpose="semantic_budget_plan_repair",
                    metadata={
                        "sceneCount": scene_count,
                        "targetWordCount": target,
                        "validationReason": str(first_error)[:240],
                    },
                )
                plan_contract, plan = parse_story_plan(repaired_plan, scene_count)
            except Exception as exc:
                error = (
                    safe_provider_error(exc, stage="planning_repair")
                    if not isinstance(exc, ValueError)
                    else {"type": type(exc).__name__, "stage": "planning_repair", "reason": str(exc)[:240]}
                )
                return finish("failed_plan", error=error)

        initial_budgets = initial_scene_budgets(target, plan)
        revision_maximum = int(request.maximum_scene_revisions)
        for index, scene in enumerate(plan):
            written = self._counter("\n\n".join(generated_scenes))
            desired = dynamic_scene_budget(
                target=target,
                written=written,
                initial=initial_budgets,
                index=index,
            )
            model_reference, reference_gain = within_run_model_reference(desired, observed_gains)
            final_scene = index == len(plan) - 1
            emit(
                "GENERATING_SCENE",
                {
                    "scene": index + 1,
                    "sceneCount": len(plan),
                    "desiredWordCount": desired,
                    "modelReferenceWordCount": model_reference,
                    "writtenWordCount": written,
                },
            )
            continuity = (request.source_context + "\n\n" + "\n\n".join(generated_scenes))[-8000:]
            try:
                raw_original = await complete(
                    messages=generation_messages(
                        request=request,
                        source_context=continuity,
                        plan_contract=plan_contract,
                        plan=plan,
                        scene=scene,
                        scene_index=index,
                        desired=desired,
                        model_reference=model_reference,
                        written=written,
                    ),
                    purpose="semantic_budget_scene",
                    metadata={
                        "scene": index + 1,
                        "sceneCount": len(plan),
                        "desiredWordCount": desired,
                        "modelReferenceWordCount": model_reference,
                    },
                )
            except Exception as exc:
                return finish(
                    "failed_provider",
                    error=safe_provider_error(exc, stage="scene", scene=index + 1),
                )

            original = clean_generated_text(raw_original)
            original_count = self._counter(original)
            # Validate the provider payload before extracting publishable prose.
            # Otherwise a forbidden wrapper such as <content>...</content> is
            # silently removed and the candidate is incorrectly treated as clean.
            original_issues = list(
                dict.fromkeys(
                    contextual_quality_issues(
                        raw_original,
                        source_context=continuity,
                        user_task=request.user_task,
                    )
                    + scene_context_quality_issues(original, continuity)
                    + scene_plan_quality_issues(original, scene)
                    + scene_contract_quality_issues(
                        original,
                        scene,
                        plan_contract,
                    )
                )
            )
            observed_gain = round(original_count / max(1, model_reference), 4)
            observed_gains.append(observed_gain)
            chapter_count_with_original = written + original_count
            future_floor = sum(
                max(180, int(round(value * 0.70))) for value in initial_budgets[index + 1 :]
            )
            chapter_upper_bound_at_risk = (
                chapter_count_with_original + future_floor > acceptance_maximum
            )
            chapter_internal_upper_bound_at_risk = (
                chapter_count_with_original + future_floor > internal_upper_bound
            )
            final_scene_minimum = max(180, int(round(desired * 0.60)))
            final_scene_capacity_safe_minimum = max(
                180,
                int(round(desired * _FINAL_SCENE_CAPACITY_SAFE_FLOOR_RATIO)),
            )
            if final_scene:
                length_revision_needed = not (
                    acceptance_minimum
                    <= chapter_count_with_original
                    <= acceptance_maximum
                ) or original_count < final_scene_capacity_safe_minimum
            else:
                tolerance = request.internal_tolerance_ratio
                length_revision_needed = not (
                    int(round(desired * (1.0 - tolerance)))
                    <= original_count
                    <= int(round(desired * (1.0 + tolerance)))
                )

            revision_desired = desired
            if not final_scene and chapter_internal_upper_bound_at_risk:
                revision_scene_floor = max(
                    180,
                    int(round(desired * _REVISION_SCENE_FLOOR_RATIO)),
                )
                revision_desired = max(
                    revision_scene_floor,
                    min(
                        original_count,
                        internal_upper_bound - written - future_floor,
                    ),
                )
            elif final_scene and chapter_count_with_original > acceptance_maximum:
                revision_desired = max(
                    final_scene_minimum,
                    acceptance_maximum - written,
                )
            elif final_scene and chapter_count_with_original < acceptance_minimum:
                revision_desired = max(desired, acceptance_minimum - written)

            revision_needed = length_revision_needed or bool(original_issues)
            revisions_used = sum(bool(item.get("revisionTriggered")) for item in scenes)
            revision_budget_available = revisions_used < revision_maximum
            proactive_revision_budget_available = revisions_used < max(
                0,
                revision_maximum - 1,
            )
            severe_early_undershoot = (
                not final_scene
                and original_count
                < int(round(desired * _SEVERE_EARLY_UNDERSHOOT_RATIO))
                and proactive_revision_budget_available
            )
            revision_has_priority = (
                bool(original_issues)
                or final_scene
                or chapter_internal_upper_bound_at_risk
                or severe_early_undershoot
            )
            revision_available = revision_budget_available and revision_has_priority
            revision_triggered = revision_needed and revision_available
            revision_skipped_reason = ""
            if revision_needed and not revision_available:
                if not revision_budget_available:
                    revision_skipped_reason = (
                        "quality_revision_limit"
                        if original_issues
                        else "chapter_revision_limit"
                    )
                elif length_revision_needed and not revision_has_priority:
                    revision_skipped_reason = "chapter_capacity_safe"
                else:
                    revision_skipped_reason = "chapter_revision_limit"
            accepted = original
            accepted_count = original_count
            accepted_issues = list(original_issues)
            revision_count = 0
            revision_contextual_issues: list[str] = []
            revision_gate_issues: list[str] = []
            revision_issues: list[str] = []
            revision_accepted = False
            revision_rejected_reason = ""
            revision_improves_chapter_fit = False
            record: Dict[str, Any] = {
                "order": index + 1,
                "initialBudget": initial_budgets[index],
                "dynamicBudget": desired,
                "modelReferenceWordCount": model_reference,
                "referenceGain": reference_gain,
                "writtenBefore": written,
                "originalWordCount": original_count,
                "originalGain": observed_gain,
                "originalMechanicalIssues": original_issues,
                "chapterCountWithOriginal": chapter_count_with_original,
                "lengthRevisionNeeded": length_revision_needed,
                "chapterUpperBoundAtRisk": chapter_upper_bound_at_risk,
                "chapterInternalUpperBoundAtRisk": chapter_internal_upper_bound_at_risk,
                "severeEarlyUndershoot": severe_early_undershoot,
                "revisionNeeded": revision_needed,
                "revisionDesiredWordCount": revision_desired,
                "revisionTriggered": revision_triggered,
                "revisionSkippedReason": revision_skipped_reason,
                "revisionWordCount": 0,
                "revisionMechanicalIssues": [],
                "revisionPreservationIssues": [],
                "revisionQualityIssues": [],
                "revisionAccepted": False,
                "revisionRejectedReason": "",
                "revisionImprovesChapterFit": False,
                "revisionFallbackAccepted": False,
                "acceptedWordCount": original_count,
                "acceptedDeviation": original_count - desired,
                "mechanicalIssues": original_issues,
            }
            scenes.append(record)

            if revision_triggered:
                emit(
                    "REVISING_SCENE",
                    {
                        "scene": index + 1,
                        "desiredWordCount": revision_desired,
                        "originalWordCount": original_count,
                        "qualityIssues": original_issues,
                    },
                )
                try:
                    raw_revision = await complete(
                        messages=revision_messages(
                            source_context=continuity,
                            plan_contract=plan_contract,
                            scene=scene,
                            original=original,
                            actual=original_count,
                            desired=revision_desired,
                            final_scene=final_scene,
                            quality_issues=original_issues,
                        ),
                        purpose="semantic_budget_revision",
                        metadata={
                            "scene": index + 1,
                            "desiredWordCount": revision_desired,
                            "dynamicDesiredWordCount": desired,
                            "originalWordCount": original_count,
                        },
                    )
                except Exception as exc:
                    record["revisionError"] = safe_provider_error(
                        exc,
                        stage="revision",
                        scene=index + 1,
                    )
                    fallback_scene_minimum = max(
                        180,
                        int(round(revision_desired * 0.60)),
                    )
                    fallback_scene_maximum = int(round(revision_desired * 1.50))
                    if not (
                        final_scene
                        and not original_issues
                        and fallback_scene_minimum
                        <= original_count
                        <= fallback_scene_maximum
                        and acceptance_minimum
                        <= chapter_count_with_original
                        <= acceptance_maximum
                    ):
                        return finish("failed_provider", error=dict(record["revisionError"]))
                    record["revisionFallbackAccepted"] = True
                    emit(
                        "REVISION_FALLBACK",
                        {
                            "scene": index + 1,
                            "generatedWordCount": chapter_count_with_original,
                            "reason": "clean_original_within_product_range",
                            "providerError": dict(record["revisionError"]),
                        },
                    )
                else:
                    revision = clean_generated_text(raw_revision)
                    revision_count = self._counter(revision)
                    revision_contextual_issues = list(
                        dict.fromkeys(
                            contextual_quality_issues(
                                raw_revision,
                                source_context=continuity,
                                user_task=request.user_task,
                            )
                            + scene_context_quality_issues(
                                revision,
                                continuity,
                            )
                            + scene_contract_quality_issues(
                                revision,
                                scene,
                                plan_contract,
                            )
                        )
                    )
                    revision_gate_issues = revision_quality_gate_issues(
                        original=original,
                        revision=revision,
                        original_count=original_count,
                        revision_count=revision_count,
                        desired=revision_desired,
                        scene=scene,
                    )
                    revision_issues = list(
                        dict.fromkeys(revision_contextual_issues + revision_gate_issues)
                    )
                    closer = abs(revision_count - revision_desired) < abs(
                        original_count - revision_desired
                    )
                    if final_scene:
                        original_fit_distance = _distance_to_interval(
                            chapter_count_with_original,
                            acceptance_minimum,
                            acceptance_maximum,
                        )
                        revision_fit_distance = _distance_to_interval(
                            written + revision_count,
                            acceptance_minimum,
                            acceptance_maximum,
                        )
                    else:
                        original_fit_distance = max(
                            0,
                            chapter_count_with_original
                            + future_floor
                            - internal_upper_bound,
                        )
                        revision_fit_distance = max(
                            0,
                            written
                            + revision_count
                            + future_floor
                            - internal_upper_bound,
                        )
                    revision_improves_chapter_fit = (
                        revision_fit_distance < original_fit_distance
                    )
                    if revision and not revision_issues and (
                        closer
                        or bool(original_issues)
                        or revision_improves_chapter_fit
                    ):
                        accepted = revision
                        accepted_count = revision_count
                        accepted_issues = []
                        revision_accepted = True
                    elif revision:
                        revision_rejected_reason = (
                            "quality_gate" if revision_issues else "not_closer_to_target"
                        )

            record.update(
                {
                    "revisionWordCount": revision_count,
                    "revisionMechanicalIssues": revision_contextual_issues,
                    "revisionPreservationIssues": revision_gate_issues,
                    "revisionQualityIssues": revision_issues,
                    "revisionAccepted": revision_accepted,
                    "revisionRejectedReason": revision_rejected_reason,
                    "revisionImprovesChapterFit": revision_improves_chapter_fit,
                    "acceptedWordCount": accepted_count,
                    "acceptedDeviation": accepted_count - desired,
                    "mechanicalIssues": accepted_issues,
                }
            )
            emit("VERIFYING_SCENE", {"scene": index + 1, **dict(record)})
            if accepted_issues:
                return finish(
                    "failed_quality",
                    error={
                        "type": "SceneQualityError",
                        "stage": "scene_verification",
                        "scene": index + 1,
                        "issues": accepted_issues,
                    },
                )
            generated_scenes.append(accepted)

        emit("ASSEMBLING", {"sceneCount": len(generated_scenes)})
        final_text = "\n\n".join(generated_scenes).strip()
        final_count = self._counter(final_text)
        final_issues = contextual_quality_issues(
            final_text,
            source_context=request.source_context,
            user_task=request.user_task,
        )
        if final_issues:
            return finish(
                "failed_quality",
                error={"type": "ChapterQualityError", "stage": "assembly", "issues": final_issues},
                final_text=final_text,
            )
        if not acceptance_minimum <= final_count <= acceptance_maximum:
            return finish(
                "failed_length",
                error={
                    "type": "ChapterLengthError",
                    "stage": "assembly",
                    "generatedWordCount": final_count,
                },
                final_text=final_text,
            )
        return finish("completed", final_text=final_text)

    @staticmethod
    def _validate_request(request: SemanticBudgetRequest) -> None:
        target = int(request.product_target_word_count)
        if target < 900 or target > 20000:
            raise ValueError("product_target_word_count must be between 900 and 20000")
        if request.scene_count and not 2 <= int(request.scene_count) <= 8:
            raise ValueError("scene_count must be 0 or between 2 and 8")
        if not 0 <= int(request.maximum_scene_revisions) <= 8:
            raise ValueError("maximum_scene_revisions must be between 0 and 8")
        if not str(request.user_task or "").strip():
            raise ValueError("user_task must not be empty")


def automatic_scene_count(target: int) -> int:
    if target <= 1800:
        return 2
    if target <= 6000:
        return 4
    return 5


def automatic_scene_revision_limit(target: int) -> int:
    return 2


def parse_json_object(text: str) -> Dict[str, Any]:
    stripped = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else stripped
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("planning response is not a JSON object")
        try:
            value = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError("planning response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("planning response is not a JSON object")
    return value


def _required_plan_text(value: Any, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"planning response lacks {field_name}")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} exceeds {maximum} characters")
    return normalized


def _plan_id(value: Any, field_name: str) -> str:
    normalized = _required_plan_text(value, field_name, 40)
    if not _PLAN_ID_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a stable ASCII id")
    return normalized


def _atomic_plan_item(value: Any, field_name: str, maximum: int) -> str:
    normalized = _required_plan_text(value, field_name, maximum)
    if _PLAN_ITEM_SEPARATOR_RE.search(normalized):
        raise ValueError(f"{field_name} must describe one atomic item")
    return normalized


def _optional_plan_object(
    payload: Dict[str, Any],
    field_name: str,
) -> Dict[str, Any] | None:
    if field_name not in payload:
        raise ValueError(f"planning response lacks {field_name}")
    value = payload[field_name]
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object or null")
    return value


def _nullable_plan_ref(
    raw_scene: Dict[str, Any],
    field_name: str,
    scene_index: int,
) -> str | None:
    if field_name not in raw_scene:
        raise ValueError(f"scene {scene_index} lacks {field_name}")
    value = raw_scene[field_name]
    if value is None:
        return None
    return _plan_id(value, f"scene {scene_index} {field_name}")


def parse_story_plan(
    text: str,
    expected_count: int,
) -> tuple[Dict[str, Any], list[Dict[str, Any]]]:
    payload = parse_json_object(text)

    contract_errors: list[str] = []
    goal_outcome: Dict[str, Any] = {}
    try:
        raw_goal_outcome = payload.get("shortTermGoalOutcome")
        if not isinstance(raw_goal_outcome, dict):
            raise ValueError("planning response lacks shortTermGoalOutcome object")
        goal_state = _required_plan_text(
            raw_goal_outcome.get("state"),
            "shortTermGoalOutcome.state",
            20,
        )
        if goal_state not in _SHORT_TERM_GOAL_STATES:
            raise ValueError("shortTermGoalOutcome.state is invalid")
        goal_outcome = {
            "state": goal_state,
            "description": _required_plan_text(
                raw_goal_outcome.get("description"),
                "shortTermGoalOutcome.description",
                200,
            ),
        }
    except ValueError as exc:
        contract_errors.append(str(exc))

    hazard: Dict[str, Any] | None = None
    try:
        raw_hazard = _optional_plan_object(payload, "primaryHazard")
        if raw_hazard is not None:
            hazard_outcome = _required_plan_text(
                raw_hazard.get("outcome"),
                "primaryHazard.outcome",
                30,
            )
            if hazard_outcome not in _HAZARD_OUTCOMES:
                raise ValueError("primaryHazard.outcome is invalid")
            hazard = {
                "id": _plan_id(raw_hazard.get("id"), "primaryHazard.id"),
                "description": _atomic_plan_item(
                    raw_hazard.get("description"),
                    "primaryHazard.description",
                    160,
                ),
                "outcome": hazard_outcome,
            }
    except ValueError as exc:
        contract_errors.append(str(exc))

    clue: Dict[str, Any] | None = None
    try:
        raw_clue = _optional_plan_object(payload, "persistentClue")
        if raw_clue is not None:
            clue_description = _atomic_plan_item(
                raw_clue.get("description"),
                "persistentClue.description",
                160,
            )
            clue_function = _required_plan_text(
                raw_clue.get("function"),
                "persistentClue.function",
                30,
            )
            if (
                clue_function != "evidence_only"
                or _CLUE_UTILITY_RE.search(clue_description)
            ):
                raise ValueError("persistentClue must remain evidence only")
            clue = {
                "id": _plan_id(raw_clue.get("id"), "persistentClue.id"),
                "description": clue_description,
                "sourceRef": _required_plan_text(
                    raw_clue.get("sourceRef"),
                    "persistentClue.sourceRef",
                    40,
                ),
                "function": clue_function,
            }
            valid_clue_sources = {"character-choice", "existing-thread"}
            if hazard is not None:
                valid_clue_sources.add(hazard["id"])
            if clue["sourceRef"] not in valid_clue_sources:
                raise ValueError(
                    "persistentClue.sourceRef does not reference its cause"
                )
    except ValueError as exc:
        contract_errors.append(str(exc))

    ability: Dict[str, Any] | None = None
    try:
        raw_ability = _optional_plan_object(payload, "abilityLimitAndCost")
        if raw_ability is not None:
            ability_purpose = _required_plan_text(
                raw_ability.get("purpose"),
                "abilityLimitAndCost.purpose",
                30,
            )
            if ability_purpose not in _ABILITY_PURPOSES:
                raise ValueError("abilityLimitAndCost.purpose is invalid")
            ability_action = _atomic_plan_item(
                raw_ability.get("action"),
                "abilityLimitAndCost.action",
                120,
            )
            if _ABILITY_PLAN_MASTERY_RE.search(ability_action):
                raise ValueError(
                    "abilityLimitAndCost.action claims unestablished mastery"
                )
            ability = {
                "id": _plan_id(
                    raw_ability.get("id"),
                    "abilityLimitAndCost.id",
                ),
                "purpose": ability_purpose,
                "action": ability_action,
                "limit": _required_plan_text(
                    raw_ability.get("limit"),
                    "abilityLimitAndCost.limit",
                    160,
                ),
                "cost": _required_plan_text(
                    raw_ability.get("cost"),
                    "abilityLimitAndCost.cost",
                    160,
                ),
            }
    except ValueError as exc:
        contract_errors.append(str(exc))

    raw_scenes_for_contract = payload.get("scenes")
    if isinstance(raw_scenes_for_contract, list):
        for index, raw_scene in enumerate(raw_scenes_for_contract, start=1):
            if not isinstance(raw_scene, dict):
                continue
            development = str(raw_scene.get("development") or "")
            ability_ref = raw_scene.get("abilityRef")
            if ability_ref is not None and _ABILITY_PLAN_MASTERY_RE.search(development):
                contract_errors.append(
                    f"scene {index} ability development claims unestablished mastery"
                )
            if (
                "abilityRef" in raw_scene
                and ability_ref is None
                and _uses_unreferenced_ability(development)
            ):
                contract_errors.append(f"scene {index} uses ability without abilityRef")

    if contract_errors:
        raise ValueError("; ".join(dict.fromkeys(contract_errors)))

    raw_scenes = payload.get("scenes")
    if not isinstance(raw_scenes, list) or len(raw_scenes) != expected_count:
        actual = len(raw_scenes) if isinstance(raw_scenes, list) else 0
        raise ValueError(f"planning response must contain exactly {expected_count} scenes, got {actual}")
    scenes: list[Dict[str, Any]] = []
    fingerprints: set[str] = set()
    referenced_hazard = False
    referenced_clue = False
    referenced_ability = False
    for index, raw_scene in enumerate(raw_scenes, start=1):
        if not isinstance(raw_scene, dict):
            raise ValueError(f"scene {index} is not an object")
        purpose = _required_plan_text(
            raw_scene.get("purpose"),
            f"scene {index} purpose",
            200,
        )
        development = _required_plan_text(
            raw_scene.get("development"),
            f"scene {index} development",
            _PLAN_DEVELOPMENT_MAX_CHARS,
        )
        exit_hook = _required_plan_text(
            raw_scene.get("exitHook"),
            f"scene {index} exitHook",
            220,
        )

        hazard_ref = _nullable_plan_ref(raw_scene, "hazardRef", index)
        hazard_role = _required_plan_text(
            raw_scene.get("hazardRole"),
            f"scene {index} hazardRole",
            20,
        )
        if hazard_role not in _HAZARD_ROLES:
            raise ValueError(f"scene {index} hazardRole is invalid")
        if hazard_ref is None:
            if hazard_role != "none":
                raise ValueError(f"scene {index} hazardRole requires hazardRef")
        elif hazard is None or hazard_ref != hazard["id"] or hazard_role == "none":
            raise ValueError(f"scene {index} hazardRef does not reference primaryHazard")
        else:
            referenced_hazard = True

        clue_ref = _nullable_plan_ref(raw_scene, "clueRef", index)
        clue_role = _required_plan_text(
            raw_scene.get("clueRole"),
            f"scene {index} clueRole",
            20,
        )
        if clue_role not in _CLUE_ROLES:
            raise ValueError(f"scene {index} clueRole is invalid")
        if clue_ref is None:
            if clue_role != "none":
                raise ValueError(f"scene {index} clueRole requires clueRef")
        elif clue is None or clue_ref != clue["id"] or clue_role == "none":
            raise ValueError(f"scene {index} clueRef does not reference persistentClue")
        else:
            referenced_clue = True

        ability_ref = _nullable_plan_ref(raw_scene, "abilityRef", index)
        if ability_ref is None:
            if _uses_unreferenced_ability(development):
                raise ValueError(f"scene {index} uses ability without abilityRef")
        else:
            if ability is None or ability_ref != ability["id"]:
                raise ValueError(
                    f"scene {index} abilityRef does not reference abilityLimitAndCost"
                )
            if _ABILITY_PLAN_MASTERY_RE.search(development):
                raise ValueError(
                    f"scene {index} ability development claims unestablished mastery"
                )
            referenced_ability = True

        fingerprint = re.sub(r"\W+", "", purpose + development, flags=re.UNICODE).lower()
        if fingerprint in fingerprints:
            raise ValueError(f"scene {index} duplicates an earlier causal step")
        fingerprints.add(fingerprint)
        try:
            weight = float(raw_scene.get("weight") or 1.0)
        except (TypeError, ValueError):
            weight = 1.0
        scenes.append(
            {
                "order": index,
                "title": str(raw_scene.get("title") or f"Scene {index}").strip()[:80],
                "purpose": purpose,
                "development": development,
                "exitHook": exit_hook,
                "hazardRef": hazard_ref,
                "hazardRole": hazard_role,
                "clueRef": clue_ref,
                "clueRole": clue_role,
                "abilityRef": ability_ref,
                "weight": round(max(0.8, min(1.2, weight)), 3),
            }
        )

    if hazard is not None and not referenced_hazard:
        raise ValueError("primaryHazard is never referenced by a scene")
    if clue is not None and not referenced_clue:
        raise ValueError("persistentClue is never referenced by a scene")
    if ability is not None and not referenced_ability:
        raise ValueError("abilityLimitAndCost is never referenced by a scene")
    final_scene = scenes[-1]
    if final_scene["hazardRole"] == "foreshadow":
        raise ValueError("final scene cannot introduce a new hazard")
    if final_scene["hazardRole"] == "pressure" and not any(
        scene["hazardRef"] for scene in scenes[:-1]
    ):
        raise ValueError("final scene cannot introduce its hazard")
    if final_scene["clueRole"] == "seed":
        raise ValueError("final scene cannot seed an unexplained clue")

    plan_contract = {
        "shortTermGoalOutcome": goal_outcome,
        "primaryHazard": hazard,
        "persistentClue": clue,
        "abilityLimitAndCost": ability,
    }
    return plan_contract, scenes


def parse_scene_plan(text: str, expected_count: int) -> list[Dict[str, Any]]:
    _plan_contract, scenes = parse_story_plan(text, expected_count)
    return scenes


def initial_scene_budgets(target: int, scenes: list[Dict[str, Any]]) -> list[int]:
    weights = [float(scene.get("weight") or 1.0) for scene in scenes]
    total_weight = sum(weights) or float(len(scenes))
    budgets = [int(round(target * weight / total_weight)) for weight in weights]
    average = target / max(1, len(scenes))
    lower = max(220, int(round(average * 0.80)))
    upper = max(lower, int(round(average * 1.25)))
    budgets = [max(lower, min(upper, value)) for value in budgets]
    delta = target - sum(budgets)
    while delta:
        direction = 1 if delta > 0 else -1
        candidates = [
            index
            for index, value in enumerate(budgets)
            if (direction > 0 and value < upper) or (direction < 0 and value > lower)
        ]
        if not candidates:
            budgets[-1] += delta
            break
        candidates.sort(key=lambda index: weights[index], reverse=direction > 0)
        for index in candidates:
            budgets[index] += direction
            delta -= direction
            if not delta:
                break
    return budgets


def _distance_to_interval(value: int, minimum: int, maximum: int) -> int:
    if value < minimum:
        return minimum - value
    if value > maximum:
        return value - maximum
    return 0


def dynamic_scene_budget(*, target: int, written: int, initial: list[int], index: int) -> int:
    base = initial[index]
    remaining_target = target - written
    remaining_base = sum(initial[index:])
    proposed = remaining_target if index == len(initial) - 1 else int(
        round(remaining_target * base / max(1, remaining_base))
    )
    lower = max(220, int(round(base * 0.75)))
    upper = max(lower, int(round(base * 1.30)))
    if index < len(initial) - 1:
        future_floor = sum(max(180, int(round(value * 0.70))) for value in initial[index + 1 :])
        if remaining_target > future_floor:
            upper = min(upper, max(lower, remaining_target - future_floor))
    return max(lower, min(upper, proposed))


def within_run_model_reference(desired: int, observed_gains: list[float]) -> tuple[int, float]:
    if not observed_gains:
        return desired, 1.0
    gain = max(
        1.0,
        min(_MODEL_REFERENCE_GAIN_MAX, float(statistics.median(observed_gains[-3:]))),
    )
    reference = int(round(desired / gain))
    lower = max(
        _MODEL_REFERENCE_MIN_CHARS,
        int(round(desired * _MODEL_REFERENCE_MIN_RATIO)),
    )
    return max(lower, min(desired, reference)), round(gain, 4)


def revision_quality_gate_issues(
    *,
    original: str,
    revision: str,
    original_count: int,
    revision_count: int,
    desired: int,
    scene: Dict[str, Any],
) -> list[str]:
    del original
    issues, coverage_verified = _plan_coverage_issues(
        revision,
        scene,
        issue_prefix="revision",
    )
    scene_floor_ratio = (
        _REVISION_VERIFIED_SCENE_FLOOR_RATIO
        if coverage_verified
        else _REVISION_SCENE_FLOOR_RATIO
    )
    compression_floor_ratio = (
        _REVISION_VERIFIED_COMPRESSION_FLOOR_RATIO
        if coverage_verified
        else _REVISION_COMPRESSION_FLOOR_RATIO
    )
    scene_floor = max(
        _MODEL_REFERENCE_MIN_CHARS,
        int(round(desired * scene_floor_ratio)),
    )
    if revision_count < scene_floor:
        issues.append("revision_below_scene_floor")
    if revision_count < int(round(original_count * compression_floor_ratio)):
        issues.append("revision_extreme_compression")
    return issues


def scene_plan_quality_issues(text: str, scene: Dict[str, Any]) -> list[str]:
    issues, _coverage_verified = _plan_coverage_issues(
        text,
        scene,
        issue_prefix="scene",
    )
    return issues


def scene_contract_quality_issues(
    text: str,
    scene: Dict[str, Any],
    plan_contract: Dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    clue = plan_contract.get("persistentClue")
    if (
        scene.get("clueRef")
        and isinstance(clue, dict)
        and clue.get("function") == "evidence_only"
        and _CLUE_BODY_UTILITY_RE.search(str(text or ""))
    ):
        issues.append("scene_clue_utility_expansion")

    ability = plan_contract.get("abilityLimitAndCost")
    if (
        isinstance(ability, dict)
        and not scene.get("abilityRef")
        and _uses_unreferenced_ability(str(text or ""))
    ):
        issues.append("scene_unreferenced_ability_use")
    if not scene.get("abilityRef") or not isinstance(ability, dict):
        return issues
    ability_expanded = any(
        sum(
            bool(pattern.search(segment))
            for pattern in _ELEMENTAL_REFERENCE_PATTERNS
        )
        >= 3
        and bool(_ELEMENTAL_CONTROL_DETAIL_RE.search(segment))
        for segment in re.split(r"[。！？!?；\n]+", str(text or ""))
    ) or bool(_LIMITED_ABILITY_VICTORY_RE.search(str(text or "")))
    if ability_expanded:
        issues.append("scene_ability_scope_expansion")
    return issues


def scene_context_quality_issues(
    text: str,
    source_context: str,
) -> list[str]:
    candidate_ngrams = _normalized_character_ngrams(
        text,
        size=_CONTEXT_REPETITION_NGRAM_SIZE,
    )
    if not candidate_ngrams:
        return []
    context_ngrams = _normalized_character_ngrams(
        source_context,
        size=_CONTEXT_REPETITION_NGRAM_SIZE,
    )
    if (
        len(candidate_ngrams & context_ngrams)
        > _CONTEXT_REPETITION_SHARED_LIMIT
    ):
        return ["scene_context_repetition"]
    return []


def _plan_coverage_issues(
    text: str,
    scene: Dict[str, Any],
    *,
    issue_prefix: str,
) -> tuple[list[str], bool]:
    issues: list[str] = []
    text_ngrams = _normalized_character_ngrams(text)
    text_characters = _normalized_character_ngrams(text, size=1)
    normalized_text = re.sub(
        r"[\W_]+",
        "",
        str(text or "").lower(),
        flags=re.UNICODE,
    )
    exit_tail_size = max(1, int(round(len(normalized_text) * _REVISION_EXIT_TAIL_RATIO)))
    exit_tail_ngrams = _normalized_character_ngrams(normalized_text[-exit_tail_size:])
    exit_tail_characters = _normalized_character_ngrams(
        normalized_text[-exit_tail_size:],
        size=1,
    )
    coverage_verified = True
    for (
        field,
        issue,
        minimum_retention,
        paraphrase_minimum_retention,
        minimum_character_retention,
    ) in (
        (
            "development",
            f"{issue_prefix}_development_loss",
            _REVISION_DEVELOPMENT_MIN_RETENTION,
            _REVISION_DEVELOPMENT_PARAPHRASE_MIN_RETENTION,
            _REVISION_DEVELOPMENT_MIN_CHARACTER_RETENTION,
        ),
        (
            "exitHook",
            f"{issue_prefix}_exit_hook_loss",
            _REVISION_EXIT_HOOK_MIN_RETENTION,
            _REVISION_EXIT_HOOK_PARAPHRASE_MIN_RETENTION,
            _REVISION_EXIT_HOOK_MIN_CHARACTER_RETENTION,
        ),
    ):
        plan_ngrams = _normalized_character_ngrams(str(scene.get(field) or ""))
        plan_characters = _normalized_character_ngrams(
            str(scene.get(field) or ""),
            size=1,
        )
        if len(plan_ngrams) < _REVISION_PLAN_MIN_ANCHORS:
            coverage_verified = False
            continue
        candidate_ngrams = exit_tail_ngrams if field == "exitHook" else text_ngrams
        candidate_characters = (
            exit_tail_characters if field == "exitHook" else text_characters
        )
        retained = len(plan_ngrams & candidate_ngrams) / len(plan_ngrams)
        character_retained = len(plan_characters & candidate_characters) / max(
            1,
            len(plan_characters),
        )
        paraphrase_retained = (
            retained >= paraphrase_minimum_retention
            and character_retained >= minimum_character_retention
        )
        if retained < minimum_retention and not paraphrase_retained:
            issues.append(issue)
            coverage_verified = False
    return issues, coverage_verified


def _normalized_character_ngrams(text: str, *, size: int = _REVISION_PLAN_NGRAM_SIZE) -> set[str]:
    return normalized_character_ngrams(text, size=size)


def _planning_contract_example(scene_count: int) -> Dict[str, Any]:
    normalized_count = max(1, int(scene_count))
    if normalized_count == 1:
        return {
            "shortTermGoalOutcome": {
                "state": "partial",
                "description": "短期目标只完成一部分，人物承担直接后果。",
            },
            "primaryHazard": None,
            "persistentClue": None,
            "abilityLimitAndCost": None,
            "scenes": [
                {
                    "title": "单一因果步骤",
                    "purpose": "推进并收束当前短期目标",
                    "development": "人物完成一个行动并承担直接后果。",
                    "exitHook": "人物执行明确的离场动作。",
                    "hazardRef": None,
                    "hazardRole": "none",
                    "clueRef": None,
                    "clueRole": "none",
                    "abilityRef": None,
                    "weight": 1.0,
                }
            ],
        }

    scenes: list[Dict[str, Any]] = []
    for index in range(normalized_count):
        if index == 0:
            purpose = "建立唯一危险并延续短期目标"
            development = "人物察觉同一个危险逼近，因而调整原有行动。"
            exit_hook = "危险的直接压力迫使人物继续应对。"
            hazard_role = "foreshadow"
            clue_role = "seed"
            ability_ref = None
        elif index == normalized_count - 1:
            purpose = "处理既有危险的后果并完成离场"
            development = "人物从同一个危险中脱离，短期目标因此延后。"
            exit_hook = "人物带着唯一线索离开并承担既定代价。"
            hazard_role = "aftermath"
            clue_role = "reveal"
            ability_ref = "ability-main" if normalized_count == 2 else None
        else:
            purpose = f"推进同一因果链的第 {index + 1} 步"
            development = f"人物针对同一个危险完成第 {index + 1} 个有限行动并承担后果。"
            exit_hook = f"行动结果直接引出第 {index + 2} 场。"
            hazard_role = "pressure" if index == 1 else "aftermath"
            clue_role = "carry"
            ability_ref = "ability-main" if index == 1 else None
        scenes.append(
            {
                "title": f"因果步骤 {index + 1}",
                "purpose": purpose,
                "development": development,
                "exitHook": exit_hook,
                "hazardRef": "hazard-main",
                "hazardRole": hazard_role,
                "clueRef": "clue-main",
                "clueRole": clue_role,
                "abilityRef": ability_ref,
                "weight": 1.0,
            }
        )

    return {
        "shortTermGoalOutcome": {
            "state": "delayed",
            "description": "人物因危险延后短期目标并承担直接后果。",
        },
        "primaryHazard": {
            "id": "hazard-main",
            "description": "一个直接施压的危险",
            "outcome": "escaped",
        },
        "persistentClue": {
            "id": "clue-main",
            "description": "该危险留下的一个可观察痕迹",
            "sourceRef": "hazard-main",
            "function": "evidence_only",
        },
        "abilityLimitAndCost": {
            "id": "ability-main",
            "purpose": "escape",
            "action": "完成一次用于逃生的有限动作",
            "limit": "动作不能击败危险或永久解决问题",
            "cost": "人物受伤或短时失去继续行动的能力",
        },
        "scenes": scenes,
    }


def _planning_contract_instruction(scene_count: int) -> str:
    example = json.dumps(
        _planning_contract_example(scene_count),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"必须返回恰好 {scene_count} 个 scenes，并使用下面的章级契约。"
        "shortTermGoalOutcome 必须含 state 和 description；state 只能是 "
        "completed、partial、delayed、failed、unchanged。"
        "primaryHazard、persistentClue、abilityLimitAndCost 在任务确实不需要时写 JSON null；"
        "需要时每类只能声明一个对象，description/action 只能写一个原子事项，"
        "不得用顿号、分号或“同时/以及”捆绑多个事项。"
        "primaryHazard.outcome 只能是 escaped、avoided、endured、temporarily_resolved、"
        "resolved、unresolved。persistentClue.sourceRef 只能引用 primaryHazard.id、"
        "character-choice 或 existing-thread，function 必须是 evidence_only；"
        "线索只能提供可观察证据，不得直接稳定灵气、提升能力或充当修炼媒介。"
        "abilityLimitAndCost.purpose 只能是 "
        "escape、temporary_relief、environmental_aid、controlled_attempt。"
        "能力 action 和场景 development 不得声称按时辰/比例重新分配、逐一或依次轮流"
        "切换控制、自创或熟练运用多种能力。"
        "每场都必须填写 hazardRef/hazardRole、clueRef/clueRole、abilityRef；"
        "没有引用时 ref 写 null 且 role 写 none。hazardRole 只能是 "
        "none、foreshadow、pressure、aftermath；clueRole 只能是 "
        "none、seed、reveal、carry。所有非空 ref 必须引用对应章级对象的 id。"
        "每个已声明的非空章级对象都必须至少被一场引用。"
        "当 abilityRef 为 null 时，该场只能写能力代价、失控或后果，不得主动调动、"
        "运转、引导、操控、凝聚、维持、稳住或压制该能力。"
        "最后一场不得用 foreshadow 引入危险或用 seed 新开线索。"
        "每场 development 只写一到两个短句、一个原子因果步骤，不超过 180 个中文字符。\n"
        f"JSON 格式：{example}"
    )


def planning_messages(request: SemanticBudgetRequest, scene_count: int) -> list[Dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是中文长篇小说的场景规划器。只返回合法 JSON，不输出 markdown、正文或字数计算过程。"
                "每个场景必须推进不同的新因果步骤，不能换一种说法重复已经完成的发现、能力建立或冲突。"
                "各场景应推进同一条主要因果链，场景切分不代表引入新设定。"
                "必须延续人物前文正在执行的短期目标；若危险打断目标，应明确写出失败、放弃、"
                "延后或部分完成及其后果，不得无声遗忘。新增线索应优先关联前文已有伏笔，"
                "不要另起一个无关谜团。"
                "任务包含危险时，同一场只保留一个主要危险源，不叠加互不依赖的陷阱与敌人。"
                "任务要求线索时，线索必须是核心危险或人物选择自然留下的可观察证据；"
                "不得为交付线索另开密室、新地点，或引入文字载体直接解释机制。"
                "最后一场只收束已经发生的主因果链、短期目标与后果，不为凑篇幅新增外部冲突或支线。"
                "除续写任务明确要求外，不新增境界突破、术法、强敌、宝物或解释性遗物；"
                "任务只要求一个线索时，全章只保留一个持久线索，不得再用玉简、幻象或第二件宝物直接给出答案。"
                "不得让人物凭空掌握前文未建立的能力，或正面击败明显强于自己的敌人；"
                "初次运用能力必须有限、有代价，优先用于逃生、借助环境或暂时化解危险。"
                "新增篇幅用于动作、感官、选择、失败与后果，不靠堆叠新名词或支线。"
                "前文与续写任务高于外部风格参考；保持前文的叙事人称、主角称谓和题材边界，"
                "不得从风格参考自行引入任务与前文未建立的新题材、人物关系或事件类型。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"续写任务：{request.user_task}\n\n"
                f"章节产品目标：约 {request.product_target_word_count} 个 Storydex 字符。"
                f"请规划恰好 {scene_count} 个顺序场景，只给 0.8-1.2 的相对叙事权重。\n"
                f"{_planning_contract_instruction(scene_count)}\n"
                "exitHook 必须是正文末尾实际发生的动作，不得写成规划说明。\n\n"
                f"低优先级风格参考（与任务或前文冲突时忽略）：\n"
                f"{request.constraint_context[:6000] or '保持前文风格'}\n\n"
                f"前文结尾：\n{request.source_context[-6000:]}"
            ),
        },
    ]


def planning_repair_messages(
    raw_plan: str,
    scene_count: int,
    *,
    validation_error: str = "",
) -> list[Dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "这是唯一一次计划修复。只返回完整合法 JSON，不输出正文、解释或 markdown。"
                "按校验原因删除多余危险、线索或能力用途，不新增原计划没有的设定名词。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"校验失败原因：{str(validation_error or '计划契约不合法')[:500]}\n"
                f"{_planning_contract_instruction(scene_count)}\n\n"
                "待修复计划：\n"
                f"{str(raw_plan or '')[:6000]}"
            ),
        },
    ]


def generation_messages(
    *,
    request: SemanticBudgetRequest,
    source_context: str,
    plan_contract: Dict[str, Any],
    plan: list[Dict[str, Any]],
    scene: Dict[str, Any],
    scene_index: int,
    desired: int,
    model_reference: int,
    written: int,
) -> list[Dict[str, str]]:
    lower = int(round(model_reference * 0.88))
    upper = int(round(model_reference * 1.12))
    final_scene = scene_index == len(plan) - 1
    position_rule = (
        "收束当前主因果链与短期目标；已有线索时只处理其后果，"
        "不添加第二条线索或无关新冲突，不总结全文。"
        if final_scene
        else "推进当前场景并自然衔接下一场，不提前收束全章。"
    )
    ability = plan_contract.get("abilityLimitAndCost")
    if isinstance(ability, dict) and scene.get("abilityRef"):
        ability_rule = (
            "能力 action 是正文可展示的能力上限，只能忠实改写这一个动作；"
            "若 action 是维持整体平衡，不得逐一给多种属性分配经脉、功效或攻击用途，"
            "也不得写成击退或击败危险。\n"
        )
    elif isinstance(ability, dict):
        ability_rule = (
            "当前场景 abilityRef 为 null，只能写能力代价、失控或后果；"
            "不得让人物主动调动、运转、引导、操控、凝聚、维持、稳住或压制该能力。\n"
        )
    else:
        ability_rule = ""
    clue = plan_contract.get("persistentClue")
    clue_rule = (
        "线索 function 是 evidence_only：只能观察、记录、携带并推断来源，"
        "不得直接稳定或恢复人物、强化能力、帮助脱身，也不得给出机制答案。\n"
        if isinstance(clue, dict)
        else ""
    )
    return [
        {
            "role": "system",
            "content": (
                "只输出当前场景可直接拼接的中文小说正文，不输出标题、计划、markdown、XML/HTML 标签、"
                "总结、留言、字数说明或创作解释。不要复述上下文或重复已完成的核心事件。"
                "严格延续紧邻上下文的叙事人称、主角称谓和题材边界；总任务与前文高于风格参考，"
                "不得从风格参考自行加入未建立的新题材、人物关系或事件类型。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"总任务：{request.user_task}\n"
                f"场景位置：第 {scene_index + 1}/{len(plan)} 场。{position_rule}\n"
                f"章级唯一约束：{json.dumps(plan_contract, ensure_ascii=False)}\n"
                "不得添加章级约束之外的危险、持久线索或能力用途。\n"
                f"{ability_rule}"
                f"{clue_rule}"
                f"当前场景计划：{json.dumps(scene, ensure_ascii=False)}\n"
                f"全章场景路线：{json.dumps(plan, ensure_ascii=False)}\n\n"
                "必须从紧邻上下文的最后动作之后开始，不得复述或近义改写其最后一段。\n"
                f"外部程序已写 {written} 个字符；本场参考约 {model_reference} 个非空白字符，"
                f"柔性范围 {lower}-{upper}。字数服务于节奏，不得重复、解释或填充。\n\n"
                f"低优先级风格参考（与总任务或前文冲突时忽略）：\n"
                f"{request.constraint_context[:6000] or '保持前文风格'}\n\n"
                f"紧邻上下文：\n{source_context}"
            ),
        },
    ]


def revision_messages(
    *,
    source_context: str,
    plan_contract: Dict[str, Any],
    scene: Dict[str, Any],
    original: str,
    actual: int,
    desired: int,
    final_scene: bool,
    quality_issues: list[str],
) -> list[Dict[str, str]]:
    far_from_target = actual < desired * 0.70 or actual > desired * 1.30
    lower_ratio = 0.70 if far_from_target else 0.90
    upper_ratio = 1.30 if far_from_target else 1.10
    lower = int(round(desired * lower_ratio))
    upper = int(round(desired * upper_ratio))
    direction = "压缩" if actual > desired else "扩写"
    ending = "保留自然收束和后续线索" if final_scene else "保留与下一场的自然衔接"
    ability = plan_contract.get("abilityLimitAndCost")
    if isinstance(ability, dict) and scene.get("abilityRef"):
        ability_rule = (
            "能力 action 是修订稿可展示的上限。删除逐一给多种属性分配经脉、功效或攻击用途的内容，"
            "只保留整体、短暂且有代价的尝试；不得击退或击败危险。\n\n"
        )
    elif isinstance(ability, dict):
        ability_rule = (
            "当前场景 abilityRef 为 null。删除人物主动调动、运转、引导、操控、凝聚、"
            "维持、稳住或压制该能力的内容，只保留能力代价、失控或后果。\n\n"
        )
    else:
        ability_rule = ""
    clue = plan_contract.get("persistentClue")
    clue_rule = (
        "线索只能作为 evidence_only。删除它直接稳定、恢复或强化人物及帮助脱身的效果，"
        "只保留观察、记录、携带和来源推断。\n\n"
        if isinstance(clue, dict)
        else ""
    )
    return [
        {
            "role": "system",
            "content": (
                "只输出修改后的当前场景正文，不输出标题、计划、总结、留言、字数说明、markdown 或任何标签。"
                "不得改变既定事件和人物动机。叙事人称、主角称谓和题材边界必须以紧邻前文为准；"
                "原稿若与前文冲突，必须纠正，不能保留错误视角或未建立的题材内容。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"当前场景实测 {actual} 个非空白字符，期望约 {desired}，柔性范围 {lower}-{upper}。"
                f"请做一次{direction}，{ending}。优先删重复或补动作因果，不机械截断、不引入新支线。\n\n"
                "必须逐项保留场景计划 development 中的因果动作与人物反应，以及 exitHook 的衔接动作；"
                "若字数与完整性冲突，优先保留这些必要节拍。\n\n"
                f"最后一段必须明确执行这个离场钩子，不得只暗示可能性或写成相反选择："
                f"{scene.get('exitHook') or ''}\n\n"
                f"必须修复的问题：{', '.join(quality_issues) or '无'}\n\n"
                "修订稿必须删除与紧邻上下文重复的句段，从上一动作之后直接推进。\n\n"
                f"章级唯一约束：{json.dumps(plan_contract, ensure_ascii=False)}\n"
                "修订不得添加章级约束之外的危险、持久线索或能力用途。\n\n"
                f"{ability_rule}"
                f"{clue_rule}"
                f"场景计划：{json.dumps(scene, ensure_ascii=False)}\n\n"
                f"紧邻前文：\n{source_context}\n\n"
                f"待修改场景：\n{original}"
            ),
        },
    ]


def safe_provider_error(exc: Exception, *, stage: str, scene: int | None = None) -> Dict[str, Any]:
    status = getattr(exc, "status_code", None)
    try:
        status_code = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_code = None
    payload: Dict[str, Any] = {
        "type": type(exc).__name__,
        "stage": stage,
        "statusCode": status_code,
        "retryable": bool(status_code == 429 or (status_code is not None and status_code >= 500)),
    }
    if scene is not None:
        payload["scene"] = scene
    return payload
