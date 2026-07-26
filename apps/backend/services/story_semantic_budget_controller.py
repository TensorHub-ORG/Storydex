from __future__ import annotations

import json
import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Protocol

from services.story_word_count_service import count_story_text_words


SEMANTIC_BUDGET_STRATEGY = "semantic_budget_v1"
SEMANTIC_BUDGET_RESULT_VERSION = 1
_CONTENT_WRAPPER_RE = re.compile(
    r"</?(?:content|summary|details|background|refine)(?:\s[^>]*)?>",
    re.IGNORECASE,
)
_LENGTH_META_RE = re.compile(
    r"Storydex|TurnContract|程序计数|非空白|字数|目标.{0,8}字|补写|可接受.{0,8}字",
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(r"全文略|待续|TODO|TBD|此处省略", re.IGNORECASE)
_SCENE_HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s*)?(?:场景|scene)\s*[一二三四五六七八九十\d]+", re.IGNORECASE)
_DIALOGUE_SPAN_RE = re.compile(r"“[^”]*”|‘[^’]*’|\"[^\"]*\"|'[^']*'", re.DOTALL)
_EXPLICIT_CONTENT_RE = re.compile(
    r"阴茎|阴道|龟头|射精|精液|小穴|肉棒|鸡巴|奶子|口交|性交|性爱|性器官|"
    r"插入.{0,8}(?:阴道|小穴)|(?:阴道|小穴).{0,8}插入",
    re.IGNORECASE,
)


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
            plan = parse_scene_plan(raw_plan, scene_count)
        except ValueError as first_error:
            emit("REPAIRING_PLAN", {"reason": str(first_error)[:240]})
            try:
                repaired_plan = await complete(
                    messages=planning_repair_messages(raw_plan, scene_count),
                    purpose="semantic_budget_plan_repair",
                    metadata={"sceneCount": scene_count, "targetWordCount": target},
                )
                plan = parse_scene_plan(repaired_plan, scene_count)
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
            original_issues = contextual_quality_issues(
                original,
                source_context=continuity,
                user_task=request.user_task,
            )
            observed_gain = round(original_count / max(1, model_reference), 4)
            observed_gains.append(observed_gain)
            tolerance = request.final_tolerance_ratio if final_scene else request.internal_tolerance_ratio
            length_revision_needed = not (
                int(round(desired * (1.0 - tolerance)))
                <= original_count
                <= int(round(desired * (1.0 + tolerance)))
            )
            revision_needed = length_revision_needed or bool(original_issues)
            revisions_used = sum(bool(item.get("revisionTriggered")) for item in scenes)
            future_floor = sum(
                max(180, int(round(value * 0.70))) for value in initial_budgets[index + 1 :]
            )
            chapter_upper_bound_at_risk = (
                written + original_count + future_floor > acceptance_maximum
            )
            chapter_internal_upper_bound_at_risk = (
                written + original_count + future_floor > internal_upper_bound
            )
            revision_available = revisions_used < revision_maximum and (
                bool(original_issues)
                or final_scene
                or revisions_used < max(0, revision_maximum - 1)
                or chapter_internal_upper_bound_at_risk
            )
            revision_triggered = revision_needed and revision_available
            accepted = original
            accepted_count = original_count
            accepted_issues = list(original_issues)
            revision_count = 0
            revision_issues: list[str] = []
            revision_accepted = False
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
                "lengthRevisionNeeded": length_revision_needed,
                "chapterUpperBoundAtRisk": chapter_upper_bound_at_risk,
                "chapterInternalUpperBoundAtRisk": chapter_internal_upper_bound_at_risk,
                "revisionNeeded": revision_needed,
                "revisionTriggered": revision_triggered,
                "revisionSkippedReason": (
                    "quality_revision_limit"
                    if original_issues and revision_needed and not revision_available
                    else "chapter_revision_limit"
                    if revision_needed and not revision_available
                    else ""
                ),
                "revisionWordCount": 0,
                "revisionMechanicalIssues": [],
                "revisionAccepted": False,
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
                        "desiredWordCount": desired,
                        "originalWordCount": original_count,
                        "qualityIssues": original_issues,
                    },
                )
                try:
                    raw_revision = await complete(
                        messages=revision_messages(
                            source_context=continuity,
                            scene=scene,
                            original=original,
                            actual=original_count,
                            desired=desired,
                            final_scene=final_scene,
                            quality_issues=original_issues,
                        ),
                        purpose="semantic_budget_revision",
                        metadata={
                            "scene": index + 1,
                            "desiredWordCount": desired,
                            "originalWordCount": original_count,
                        },
                    )
                except Exception as exc:
                    record["revisionError"] = safe_provider_error(
                        exc,
                        stage="revision",
                        scene=index + 1,
                    )
                    chapter_count_with_original = written + original_count
                    fallback_scene_minimum = max(180, int(round(desired * 0.60)))
                    fallback_scene_maximum = int(round(desired * 1.50))
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
                    revision_issues = contextual_quality_issues(
                        revision,
                        source_context=continuity,
                        user_task=request.user_task,
                    )
                    closer = abs(revision_count - desired) < abs(original_count - desired)
                    if revision and not revision_issues and (closer or bool(original_issues)):
                        accepted = revision
                        accepted_count = revision_count
                        accepted_issues = []
                        revision_accepted = True

            record.update(
                {
                    "revisionWordCount": revision_count,
                    "revisionMechanicalIssues": revision_issues,
                    "revisionAccepted": revision_accepted,
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
        return 3
    if target <= 3800:
        return 4
    return 5


def automatic_scene_revision_limit(target: int) -> int:
    return 3 if automatic_scene_count(target) == 3 else 2


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


def parse_scene_plan(text: str, expected_count: int) -> list[Dict[str, Any]]:
    payload = parse_json_object(text)
    raw_scenes = payload.get("scenes")
    if not isinstance(raw_scenes, list) or len(raw_scenes) != expected_count:
        actual = len(raw_scenes) if isinstance(raw_scenes, list) else 0
        raise ValueError(f"planning response must contain exactly {expected_count} scenes, got {actual}")
    scenes: list[Dict[str, Any]] = []
    fingerprints: set[str] = set()
    for index, raw_scene in enumerate(raw_scenes, start=1):
        if not isinstance(raw_scene, dict):
            raise ValueError(f"scene {index} is not an object")
        purpose = str(raw_scene.get("purpose") or "").strip()
        development = str(raw_scene.get("development") or "").strip()
        exit_hook = str(raw_scene.get("exitHook") or "").strip()
        if not purpose or not development or not exit_hook:
            raise ValueError(f"scene {index} lacks purpose, development, or exitHook")
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
                "purpose": purpose[:600],
                "development": development[:1000],
                "exitHook": exit_hook[:600],
                "weight": round(max(0.8, min(1.2, weight)), 3),
            }
        )
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
    gain = max(1.0, min(1.70, float(statistics.median(observed_gains[-3:]))))
    reference = int(round(desired / gain))
    lower = max(220, int(round(desired * 0.60)))
    return max(lower, min(desired, reference)), round(gain, 4)


def clean_generated_text(text: str) -> str:
    value = str(text or "").strip()
    fenced = re.fullmatch(r"```(?:markdown|text)?\s*(.*?)\s*```", value, re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced.group(1).strip()
    value = re.sub(r"^(?:正文|续写正文|修改后正文)\s*[：:]\s*", "", value, count=1)
    return value.strip()


def mechanical_issues(text: str) -> list[str]:
    value = str(text or "")
    issues: list[str] = []
    if not value.strip():
        issues.append("empty")
    if _LENGTH_META_RE.search(value):
        issues.append("length_meta_language")
    if _PLACEHOLDER_RE.search(value):
        issues.append("placeholder")
    if _CONTENT_WRAPPER_RE.search(value):
        issues.append("content_wrapper")
    if _SCENE_HEADING_RE.search(value):
        issues.append("scene_heading")
    if not re.search(r"[。！？!?…][\"'”’」』）》】〕〉》)}\]]*$", value.rstrip()):
        issues.append("incomplete_ending")
    paragraphs = [re.sub(r"\s+", "", item) for item in re.split(r"\n\s*\n", value) if item.strip()]
    if len(paragraphs) != len(set(paragraphs)):
        issues.append("duplicate_paragraph")
    normalized = re.sub(r"\s+", "", value)
    if len(normalized) >= 24:
        ngrams: Dict[str, int] = {}
        for offset in range(len(normalized) - 23):
            item = normalized[offset : offset + 24]
            ngrams[item] = ngrams.get(item, 0) + 1
        if sum(count - 1 for count in ngrams.values() if count > 1) > 3:
            issues.append("repeated_ngram")
    return issues


def contextual_quality_issues(
    text: str,
    *,
    source_context: str,
    user_task: str = "",
) -> list[str]:
    issues = mechanical_issues(text)
    if _narrates_in_second_person(text) and not _narrates_in_second_person(source_context):
        issues.append("narrative_perspective_shift")
    if _EXPLICIT_CONTENT_RE.search(text) and not _EXPLICIT_CONTENT_RE.search(
        f"{user_task}\n{source_context}"
    ):
        issues.append("unexpected_explicit_content")
    return issues


def _narrates_in_second_person(text: str) -> bool:
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", str(text or "")) if item.strip()]
    if not paragraphs:
        return False
    second_person_starts = sum(item.startswith(("你", "您")) for item in paragraphs)
    unquoted = _DIALOGUE_SPAN_RE.sub("", str(text or ""))
    second_person_mentions = unquoted.count("你") + unquoted.count("您")
    return (
        second_person_starts >= 2
        and second_person_starts / len(paragraphs) >= 0.08
        and second_person_mentions >= 4
    )


def planning_messages(request: SemanticBudgetRequest, scene_count: int) -> list[Dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是中文长篇小说的场景规划器。只返回合法 JSON，不输出 markdown、正文或字数计算过程。"
                "每个场景必须推进不同的新因果步骤，不能换一种说法重复已经完成的发现、能力建立或冲突。"
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
                "JSON 格式："
                '{"scenes":[{"title":"短标题","purpose":"叙事目的",'
                '"development":"必须发生的新动作、冲突和人物反应",'
                '"exitHook":"如何衔接或收束","weight":1.0}]}\n\n'
                f"低优先级风格参考（与任务或前文冲突时忽略）：\n"
                f"{request.constraint_context[:6000] or '保持前文风格'}\n\n"
                f"前文结尾：\n{request.source_context[-6000:]}"
            ),
        },
    ]


def planning_repair_messages(raw_plan: str, scene_count: int) -> list[Dict[str, str]]:
    return [
        {
            "role": "system",
            "content": "只修复 JSON 结构，不新增正文、解释或 markdown。",
        },
        {
            "role": "user",
            "content": (
                f"把下面内容修复为恰好包含 {scene_count} 个 scenes 的合法 JSON。"
                "每项必须有 title、purpose、development、exitHook、weight：\n"
                f"{str(raw_plan or '')[:6000]}"
            ),
        },
    ]


def generation_messages(
    *,
    request: SemanticBudgetRequest,
    source_context: str,
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
        "化解当前危险并留下自然线索，不总结全文。"
        if final_scene
        else "推进当前场景并自然衔接下一场，不提前收束全章。"
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
                f"当前场景计划：{json.dumps(scene, ensure_ascii=False)}\n"
                f"全章场景路线：{json.dumps(plan, ensure_ascii=False)}\n\n"
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
    scene: Dict[str, Any],
    original: str,
    actual: int,
    desired: int,
    final_scene: bool,
    quality_issues: list[str],
) -> list[Dict[str, str]]:
    lower = int(round(desired * 0.90))
    upper = int(round(desired * 1.10))
    direction = "压缩" if actual > desired else "扩写"
    ending = "保留自然收束和后续线索" if final_scene else "保留与下一场的自然衔接"
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
                f"必须修复的问题：{', '.join(quality_issues) or '无'}\n\n"
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
