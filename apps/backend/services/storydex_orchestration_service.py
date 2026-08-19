from __future__ import annotations

import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List

from core.config import FEATURE_FLAG_DEFAULTS
from core.feature_flags import FeatureFlags
from services.agent_capability_policy import compile_capability_mode
from services.content_catalog_service import get_content_catalog_service
from services.context_policy import ContextPolicy
from services.global_config_service import GlobalConfigService, get_global_config_service
from services.performance_trace_service import record_counter, trace_turn_contract
from services.story_chapter_action_service import (
    CHAPTER_ACTION_CONTINUE_CHAPTER,
    CHAPTER_ACTION_CONTINUE_FRAGMENT,
    chapter_number_from_path,
    validate_chapter_plan,
)
from services.storydex_context_assembler_service import StorydexContextAssemblerService, get_storydex_context_assembler_service
from services.storydex_intent_service import (
    _COMPLEXITY_LEVELS,
    _OPERATION_TYPES,
    _heuristic_complexity,
    _heuristic_operation_type,
    heuristic_intent_frame,
    intent_frame_allows_project_writes,
    is_explicit_knowledge_binding_request,
    is_valid_intent_frame,
)
from services.story_project_service import (
    DEFAULT_CHAPTER_WORD_COUNT_TARGET,
    DEFAULT_CHAPTER_TEMPLATE_ID,
    SINGLE_FILE_CONTENT_MODE,
    StoryProjectService,
    StoryProjectServiceError,
    get_story_project_service,
)
from services.story_length_calibration_service import (
    StoryLengthCalibrationService,
    get_story_length_calibration_service,
)
from services.story_length_tier_calibration_service import (
    StoryLengthTierCalibrationService,
    get_story_length_tier_calibration_service,
)
from services.story_preset_length_policy_service import (
    DEFAULT_PARAGRAPH_DENSITY_BAND,
    classify_paragraph_density,
)
from services.story_word_count_service import (
    DEFAULT_CHAPTER_LENGTH_TIER,
    STORY_WORD_COUNT_RULE,
    WORD_COUNT_POLICY_VERSION,
    asymmetric_policy_payload,
    migrate_chapter_word_count_target,
    normalize_chapter_length_tier,
    precision_policy_payload,
)
from services.story_semantic_budget_controller import (
    SEMANTIC_BUDGET_STRATEGY,
    automatic_scene_count,
    automatic_scene_revision_limit,
)


@dataclass(frozen=True)
class StorydexOrchestrationService:
    story_project_service: StoryProjectService
    context_assembler: StorydexContextAssemblerService | None = None
    global_config_service: GlobalConfigService | None = None
    length_calibration_service: StoryLengthCalibrationService | None = None
    length_tier_calibration_service: StoryLengthTierCalibrationService | None = None

    @trace_turn_contract
    def build_turn_contract(
        self,
        workspace_root: Path,
        *,
        prompt: str,
        active_file: str = "",
        story_generation: Dict[str, Any] | None = None,
        intent_frame: Dict[str, Any] | None = None,
        route_hints: Dict[str, Any] | None = None,
        routing_metadata: Dict[str, Any] | None = None,
        context_policy: ContextPolicy | None = None,
        provider: str = "",
        model: str = "",
        trace_id: str = "",
        session_id: str = "",
    ) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        effective_context_policy = self._context_policy(context_policy)
        self.story_project_service.ensure_project_structure(root)
        story_generation = story_generation if isinstance(story_generation, dict) else {}
        settings = self.story_project_service.read_project_settings(
            root,
            ensure_structure=False,
        )
        catalog_service = get_content_catalog_service(root)
        catalog_snapshot = catalog_service.peek_snapshot()
        self._mark_changed_active_source(
            root,
            active_file=active_file,
            catalog_service=catalog_service,
            catalog_snapshot=catalog_snapshot,
        )
        chapter_path_mapping: Dict[str, str] = {}
        if catalog_snapshot is None or catalog_service.dirty_file_count:
            chapter_path_mapping = self.story_project_service._normalize_chapter_directories(  # noqa: SLF001
                root
            )
        content_catalog = (
            catalog_service.refresh_all()
            if chapter_path_mapping
            else catalog_service.refresh_dirty()
        )
        chapters = self.story_project_service.list_chapter_states(
            root,
            catalog_snapshot=content_catalog,
        )
        intent = self._intent_frame(
            prompt=prompt,
            active_file=active_file,
            chapter_count=len(chapters),
            intent_frame=intent_frame,
        )
        chapter_templates = self._list_chapter_templates(
            root,
            ensure_structure=False,
        )
        requested_template = self._selected_chapter_template(story_generation) or str(
            settings.get("storyChapterTemplateId") or DEFAULT_CHAPTER_TEMPLATE_ID
        ).strip()
        selected_template = self._resolve_template(
            chapter_templates,
            requested_template or DEFAULT_CHAPTER_TEMPLATE_ID,
        )
        if not requested_template and selected_template is None and chapter_templates:
            selected_template = chapter_templates[0]
        # Only an explicit, write-authorised create_new decision may plan prose
        # targets. Domain mentions and read-only inquiries never reach this path.
        operation_type = str(intent.get("operationType") or "").strip().lower()
        can_write = intent_frame_allows_project_writes(intent)
        is_story_creation = bool(
            intent["primary"] == "story_generation"
            and operation_type == "create_new"
            and can_write
        )
        bounded_modify_existing = bool(
            intent["primary"] == "story_generation"
            and operation_type == "modify_existing"
            and can_write
            and not self._explicit_generic_file_tool_request(prompt)
        )
        is_new_story = is_story_creation and len(chapters) == 0
        invalid_template = bool(requested_template and selected_template is None)
        requires_template = is_story_creation and invalid_template

        requested_fragment_count = self._positive_int(story_generation.get("fragmentCount"), default=1)
        flags = FeatureFlags(root, FEATURE_FLAG_DEFAULTS)
        tier_mode_enabled = flags.get_bool("STORY_LENGTH_TIER_ENABLED")
        explicit_tier = story_generation.get(
            "chapterLengthTier",
            story_generation.get("chapter_length_tier"),
        )
        legacy_tier_source = story_generation.get(
            "chapterWordCountTarget",
            story_generation.get(
                "chapter_word_count_target",
                story_generation.get(
                    "fragmentWordCount",
                    story_generation.get("fragment_word_count"),
                ),
            ),
        )
        chapter_length_tier = (
            normalize_chapter_length_tier(explicit_tier)
            if explicit_tier is not None
            else migrate_chapter_word_count_target(legacy_tier_source)
            if legacy_tier_source is not None
            else normalize_chapter_length_tier(
                settings.get("chapterLengthTier") or DEFAULT_CHAPTER_LENGTH_TIER
            )
        )
        fragment_word_count_min, fragment_word_count_max = self._resolve_story_word_count_range(story_generation)
        chapter_word_count_target = int(round((fragment_word_count_min + fragment_word_count_max) / 2))
        word_count_mode = (
            "range"
            if self._uses_legacy_story_word_count_range(story_generation)
            else "target"
        )
        asymmetric_enabled = (
            word_count_mode == "target"
            and flags.get_bool("ASYMMETRIC_STORY_LENGTH_ENABLED")
        )
        # The precision band is published either way so observability can report
        # how far a normal draft landed from it, but only `enabled` may authorise a
        # second prose call. `reason` records which input decided that, which is
        # what makes an unexpected extra call traceable afterwards.
        precision_enabled, precision_reason = self._resolve_precise_word_count(
            story_generation,
            settings=settings,
            word_count_mode=word_count_mode,
        )
        if asymmetric_enabled:
            precision_enabled = False
            precision_reason = "asymmetric_story_length_enabled"
        precision_policy = precision_policy_payload(
            chapter_word_count_target,
            enabled=precision_enabled,
            reason=precision_reason,
        )
        asymmetric_policy = asymmetric_policy_payload(
            chapter_word_count_target,
            enabled=asymmetric_enabled,
        )
        length_guidance = (
            self.length_calibration_service or get_story_length_calibration_service()
        ).resolve_generation_guidance(
            root,
            product_target_word_count=chapter_word_count_target,
            provider=provider,
            model=model,
        )
        if word_count_mode == "target":
            model_reference_word_count = int(length_guidance["modelReferenceWordCount"])
            accept_min = int(length_guidance["acceptanceMinimum"])
            accept_max = int(length_guidance["acceptanceMaximum"])
            calibration = dict(length_guidance["calibration"])
            allocation_min = model_reference_word_count
            allocation_max = model_reference_word_count
        else:
            model_reference_word_count = chapter_word_count_target
            accept_min, accept_max = self.story_project_service._story_word_count_acceptance_band(  # noqa: SLF001
                fragment_word_count_min,
                fragment_word_count_max,
            )
            calibration = {
                **dict(length_guidance["calibration"]),
                "status": "fallback",
                "reason": "legacy_range_not_calibrated",
                "sourceTargetGrade": None,
                "sampleCount": 0,
                "medianRatio": None,
                "appliedRatio": 1.0,
            }
            allocation_min = fragment_word_count_min
            allocation_max = fragment_word_count_max
        # 段数配额只在 target 模式下生效：legacy range 模式没有单一章级目标可换算。
        # 每段字数归预设文风所有，段数归本轮契约所有，两者正交（章字数 = 段数 x 每段字数）。
        paragraph_quota_enabled = (
            word_count_mode == "target"
            and not asymmetric_enabled
            and flags.get_bool("PARAGRAPH_QUOTA_GENERATION_ENABLED")
        )
        paragraph_density = (
            classify_paragraph_density(root)
            if paragraph_quota_enabled
            else {"band": DEFAULT_PARAGRAPH_DENSITY_BAND, "reason": "paragraph_quota_disabled"}
        )
        paragraph_quota = (
            self.length_calibration_service or get_story_length_calibration_service()
        ).resolve_paragraph_quota(
            root,
            product_target_word_count=chapter_word_count_target,
            provider=provider,
            model=model,
            density_band=str(paragraph_density.get("band") or DEFAULT_PARAGRAPH_DENSITY_BAND),
        )
        # 保留旧字段以向后兼容；在新 contract 中它表示章级目标而非每片段上限。
        fragment_word_count = chapter_word_count_target
        chapter_content_mode = str((selected_template or {}).get("contentMode") or "multi_fragment")
        fragment_count = 1 if chapter_content_mode == SINGLE_FILE_CONTENT_MODE else requested_fragment_count
        next_segment_path = ""
        fragment_targets: List[Dict[str, Any]] = []
        # The chapter action is resolved once and shared with the planner so the
        # published plan and the paths on disk cannot disagree about which chapter
        # this turn writes into.
        chapter_action: Dict[str, Any] = {}
        chapter_plan_validation: Dict[str, Any] = {}
        authoritative_chapter_path = ""
        authoritative_fragment_paths: List[str] = []
        retained_word_count = 0
        remaining_word_count = chapter_word_count_target
        if is_story_creation and not requires_template:
            chapter_action = self.story_project_service.resolve_turn_chapter_action(
                root,
                prompt=prompt,
                active_file=active_file,
                content_mode=chapter_content_mode,
                is_new_story=is_new_story,
                chapter_states=chapters,
            )
            fragment_targets = self.story_project_service.plan_story_generation_targets(
                root,
                template=selected_template or self.story_project_service.default_chapter_directory_template(),
                fragment_count=fragment_count,
                active_file=active_file,
                prompt=prompt,
                is_new_story=is_new_story,
                chapter_action=chapter_action,
                chapter_states=chapters,
                catalog_snapshot=content_catalog,
            )
            next_segment_path = str(fragment_targets[0].get("path") or "") if fragment_targets else ""
            # The authoritative paths are read back off the planned targets rather
            # than rebuilt, so the published contract names exactly the files the
            # planner chose.
            authoritative_fragment_paths = [
                str(target.get("path") or "") for target in fragment_targets if target.get("path")
            ]
            authoritative_chapter_path = (
                PurePosixPath(authoritative_fragment_paths[0]).parent.as_posix()
                if authoritative_fragment_paths
                else ""
            )
            # Validate the plan here, while the turn is still cheap to abandon. A
            # wrong target directory cannot be repaired by a later word-count
            # call, so the execution path must be able to stop before spending
            # the prose call rather than after writing prose to the wrong place.
            chapter_plan_validation = validate_chapter_plan(
                root,
                action=str(chapter_action.get("action") or ""),
                target_chapter_number=int(chapter_action.get("targetChapterNumber") or 0),
                authoritative_chapter_path=authoritative_chapter_path,
                fragment_paths=authoritative_fragment_paths,
                chapter_numbers=tuple(state.chapter_number for state in chapters),
            )

            if str(chapter_action.get("action") or "") in {
                CHAPTER_ACTION_CONTINUE_CHAPTER,
                CHAPTER_ACTION_CONTINUE_FRAGMENT,
            }:
                retained_word_count = self.story_project_service.count_chapter_story_words(
                    root,
                    authoritative_chapter_path,
                )
            remaining_word_count = max(
                0,
                chapter_word_count_target - retained_word_count,
            )

            if word_count_mode == "target":
                if remaining_word_count > 0:
                    remaining_guidance = (
                        self.length_calibration_service
                        or get_story_length_calibration_service()
                    ).resolve_generation_guidance(
                        root,
                        product_target_word_count=remaining_word_count,
                        provider=provider,
                        model=model,
                    )
                    model_reference_word_count = int(
                        remaining_guidance["modelReferenceWordCount"]
                    )
                    calibration = dict(remaining_guidance["calibration"])
                    allocation_min = model_reference_word_count
                    allocation_max = model_reference_word_count
                    paragraph_quota = (
                        self.length_calibration_service
                        or get_story_length_calibration_service()
                    ).resolve_paragraph_quota(
                        root,
                        product_target_word_count=remaining_word_count,
                        provider=provider,
                        model=model,
                        density_band=str(
                            paragraph_density.get("band")
                            or DEFAULT_PARAGRAPH_DENSITY_BAND
                        ),
                    )
                else:
                    model_reference_word_count = 0
                    allocation_min = 0
                    allocation_max = 0
                    calibration = {
                        **calibration,
                        "status": "not_applicable",
                        "reason": "no_remaining_budget",
                        "sourceTargetGrade": None,
                        "sampleCount": 0,
                        "medianRatio": None,
                        "appliedRatio": 1.0,
                    }
                    paragraph_quota = {
                        **paragraph_quota,
                        "paragraphQuota": 0,
                        "paragraphQuotaMinimum": 0,
                        "paragraphQuotaMaximum": 0,
                    }

            assumed_written = 0
            for index, target in enumerate(fragment_targets):
                reference = (
                    0
                    if word_count_mode == "target" and remaining_word_count <= 0
                    else self.story_project_service.allocate_story_fragment_reference_word_count(
                        allocation_min,
                        allocation_max,
                        written_word_count=assumed_written,
                        remaining_fragment_count=len(fragment_targets) - index,
                        total_fragment_count=len(fragment_targets),
                    )
                )
                target["referenceWordCount"] = reference
                target["referenceWordCountIsHardLimit"] = False
                assumed_written += reference
        elif bounded_modify_existing:
            try:
                fragment_targets = self.story_project_service.plan_modify_existing_targets(
                    root,
                    active_file=active_file,
                    fragment_count=requested_fragment_count,
                )
            except StoryProjectServiceError as exc:
                fragment_targets = []
                chapter_plan_validation = {
                    "_type": "ModifyExistingPlanValidation",
                    "_version": 1,
                    "passed": False,
                    "action": "modify_existing",
                    "targetChapterNumber": chapter_number_from_path(active_file),
                    "authoritativeChapterPath": "",
                    "authoritativeFragmentPaths": [],
                    "issues": [str(exc)],
                }
            else:
                authoritative_fragment_paths = [
                    str(target.get("path") or "")
                    for target in fragment_targets
                    if target.get("path")
                ]
                authoritative_chapter_path = (
                    PurePosixPath(authoritative_fragment_paths[0]).parent.as_posix()
                    if authoritative_fragment_paths
                    else ""
                )
                target_chapter_number = chapter_number_from_path(active_file)
                chapter_action = {
                    "action": "modify_existing",
                    "targetChapterNumber": target_chapter_number,
                    "reason": "active_existing_file",
                    "isNewChapter": False,
                }
                chapter_plan_validation = {
                    "_type": "ModifyExistingPlanValidation",
                    "_version": 1,
                    "passed": True,
                    "action": "modify_existing",
                    "targetChapterNumber": target_chapter_number,
                    "authoritativeChapterPath": authoritative_chapter_path,
                    "authoritativeFragmentPaths": list(authoritative_fragment_paths),
                    "issues": [],
                }
            fragment_count = len(fragment_targets) or requested_fragment_count
            next_segment_path = (
                str(fragment_targets[0].get("path") or "") if fragment_targets else ""
            )
            if authoritative_fragment_paths:
                authoritative_chapter_path = (
                    PurePosixPath(authoritative_fragment_paths[0]).parent.as_posix()
                )

        paragraph_calibration = dict(paragraph_quota.get("calibration") or {})
        # 全局冷启动密度会因 Provider/模型/预设差异产生很大偏差。实验开关只允许
        # 已有同桶观测的段数配额进入正文 prompt；样本不足时继续使用字符软参考，
        # 同时照常积累段落观测，达到校准下限后再自动切换。
        paragraph_quota_active = paragraph_quota_enabled and (
            str(paragraph_calibration.get("status") or "").strip().lower()
            == "applied"
        )
        tier_word_count_policy: Dict[str, Any] = {}
        if tier_mode_enabled:
            tier_word_count_policy = dict(
                (
                    self.length_tier_calibration_service
                    or get_story_length_tier_calibration_service()
                ).resolve_policy(
                    root,
                    tier=chapter_length_tier,
                    provider=provider,
                    model=model,
                )
            )
            tier_word_count_policy.update(
                {
                    "retainedWordCount": retained_word_count,
                    "precision": {
                        "enabled": False,
                        "maximumRevisionCalls": 0,
                        "reason": "tier_mode",
                    },
                    "asymmetric": {
                        "enabled": False,
                        "maximumSecondDrafts": 0,
                        "reason": "tier_mode",
                    },
                    "paragraphQuota": 0,
                }
            )
            for target in fragment_targets:
                target.pop("referenceWordCount", None)
                target.pop("referenceWordCountIsHardLimit", None)

        turn_plan = {
            "requestedFragmentCount": requested_fragment_count,
            "fragmentCount": fragment_count,
            "fragmentWordCount": fragment_word_count,
            "fragmentWordCountMin": fragment_word_count_min,
            "fragmentWordCountMax": fragment_word_count_max,
            "chapterWordCountTarget": chapter_word_count_target,
            "wordCountPolicy": {
                "version": WORD_COUNT_POLICY_VERSION,
                "algorithm": "storydex_visible_characters_v1",
                "countingRule": STORY_WORD_COUNT_RULE,
                "mode": word_count_mode,
                "scope": "chapter",
                "target": chapter_word_count_target,
                "retainedWordCount": retained_word_count,
                "remainingWordCount": remaining_word_count,
                "modelReferenceWordCount": model_reference_word_count,
                "minimum": fragment_word_count_min,
                "maximum": fragment_word_count_max,
                # The acceptance keys stay for existing consumers. They describe
                # the same interval as the normal band and must not drift from it.
                "acceptanceMinimum": accept_min,
                "acceptanceMaximum": accept_max,
                "normalMinimum": accept_min,
                "normalMaximum": accept_max,
                "precision": precision_policy,
                "asymmetric": asymmetric_policy,
                "overBudgetAction": "warn_and_keep",
                "calibration": calibration,
                # paragraphQuota 为 0 表示本轮不下发段数配额，提示词回退到字符软参考。
                "paragraphQuota": (
                    int(paragraph_quota["paragraphQuota"])
                    if paragraph_quota_active
                    else 0
                ),
                "paragraphQuotaMinimum": int(paragraph_quota["paragraphQuotaMinimum"]),
                "paragraphQuotaMaximum": int(paragraph_quota["paragraphQuotaMaximum"]),
                "charsPerParagraph": float(paragraph_quota["charsPerParagraph"]),
                "paragraphDensityBand": str(paragraph_quota["densityBand"]),
                "paragraphDensityReason": str(paragraph_density.get("reason") or ""),
                "paragraphCalibration": paragraph_calibration,
            },
            "operationType": operation_type or "other",
            "complexity": str(intent.get("complexity") or "simple"),
            "isNewStory": is_new_story,
            "requiresChapterTemplateSelection": requires_template,
            "selectedChapterTemplate": str(selected_template.get("id") or "") if selected_template else "",
            "selectedChapterTemplateDetail": self._template_detail(selected_template),
            "chapterContentMode": chapter_content_mode,
            "fragmentTargets": fragment_targets,
            "boundedStoryGeneration": bounded_modify_existing or is_story_creation,
            # The chapter decision travels with the contract so the execution
            # path gates on the same target the planner chose, and an audit can
            # answer "which chapter was this turn for" without re-parsing prompt
            # text.
            "chapterAction": str(chapter_action.get("action") or ""),
            "targetChapterNumber": int(chapter_action.get("targetChapterNumber") or 0),
            "chapterActionReason": str(chapter_action.get("reason") or ""),
            "authoritativeChapterPath": authoritative_chapter_path,
            "authoritativeFragmentPaths": list(authoritative_fragment_paths),
            "chapterPlanValidation": dict(chapter_plan_validation),
            "invalidChapterTemplate": requested_template if invalid_template else "",
            "availableChapterTemplates": chapter_templates,
            "nextSegmentPath": next_segment_path,
            "chapterCount": len(chapters),
            "activeFile": active_file,
            "storyFormatSource": "existing_project" if chapters else "selected_chapter_template" if selected_template else "chapter_template",
        }
        if tier_mode_enabled:
            for legacy_key in (
                "fragmentWordCount",
                "fragmentWordCountMin",
                "fragmentWordCountMax",
                "chapterWordCountTarget",
            ):
                turn_plan.pop(legacy_key, None)
            turn_plan["chapterLengthTier"] = chapter_length_tier
            turn_plan["wordCountPolicy"] = tier_word_count_policy
        requested_generation_strategy = str(story_generation.get("generationStrategy") or "").strip().lower()
        if (
            not tier_mode_enabled
            and is_story_creation
            and requested_generation_strategy == SEMANTIC_BUDGET_STRATEGY
        ):
            semantic_scene_count = automatic_scene_count(chapter_word_count_target)
            turn_plan["generationControl"] = {
                "strategy": SEMANTIC_BUDGET_STRATEGY,
                "productTargetWordCount": chapter_word_count_target,
                "sceneCount": semantic_scene_count,
                "internalToleranceRatio": 0.20,
                "finalToleranceRatio": 0.15,
                "maximumSceneRevisions": automatic_scene_revision_limit(chapter_word_count_target),
                "applyMode": "single_commit",
                "rolloutMode": "gated_direct",
            }
        context_assembly = (self.context_assembler or StorydexContextAssemblerService(self.story_project_service)).assemble(
            root,
            prompt=prompt,
            active_file=active_file,
            intent_primary=str(intent.get("primary") or ""),
            turn_plan=turn_plan,
            policy=effective_context_policy,
            chapter_states=chapters,
            catalog_snapshot=content_catalog,
        )
        skill_registry = self._skill_registry(root)
        allowed_write_roots = (
            [str(item) for item in intent.get("assetTargets", []) if str(item).strip()]
            if can_write
            else []
        )
        knowledge_write_mode = str(intent.get("knowledgeWriteMode") or "").strip()
        if not knowledge_write_mode and is_explicit_knowledge_binding_request(prompt):
            knowledge_write_mode = "explicit_binding"
        knowledge_confirmed = bool(intent.get("knowledgeConfirmed"))
        knowledge_write_policy = {
            "mode": knowledge_write_mode or "standard",
            "confirmationRequired": knowledge_write_mode == "explicit_binding",
            "confirmed": knowledge_confirmed if knowledge_write_mode == "explicit_binding" else True,
        }
        direct_file_writes = can_write
        if knowledge_write_mode == "explicit_binding":
            # Native/generic file tools are confined to the ephemeral plan
            # directory.  The domain tool owns all formal Markdown/facts/WIKI
            # writes after the later confirmation turn.
            allowed_write_roots = (
                [".storydex/.agent/runtime/knowledge-write-plans/"]
                if can_write
                else []
            )
            direct_file_writes = False
        capability_mode = compile_capability_mode(
            can_write=can_write,
            allowed_write_roots=allowed_write_roots,
            knowledge_write_mode=knowledge_write_mode,
        )

        return {
            "_type": "TurnContract",
            "_version": 1,
            "traceId": str(trace_id or ""),
            "sessionId": str(session_id or ""),
            "providerId": str(provider or ""),
            "model": str(model or ""),
            "routeHints": dict(route_hints) if isinstance(route_hints, dict) else {},
            "intentRouting": (
                dict(routing_metadata) if isinstance(routing_metadata, dict) else {}
            ),
            "contentCatalog": content_catalog.to_trace(),
            "status": "needs_user_input" if requires_template else "ready",
            "intentFrame": intent,
            "knowledgeWritePolicy": knowledge_write_policy,
            "executionPolicy": {
                "coomiRole": "general_agent_runtime",
                "storydexRole": "fiction_orchestration",
                "capabilityMode": capability_mode,
                "directFileWrites": direct_file_writes,
                "pendingWriteApproval": False,
                "localGitAutoCommit": can_write,
                "allowedWriteRoots": allowed_write_roots,
                "remotePush": False,
                "highRiskChangeRequiresNotice": True,
            },
            "turnPlan": turn_plan,
            "assetTargets": {
                "chapterRoot": "chapters/",
                "characterRoot": ".storydex/characters/",
                "variableThoughtRoot": ".storydex/memory/chapters/",
                "factMemoryPath": ".storydex/memory/current/facts.json",
                "relationshipGraphPath": ".storydex/memory/current/relationship_graph.json",
                "wikiRoot": ".storydex/wiki/",
            },
            "contextPolicy": {
                "activePresetsOnly": True,
                "compiledSafePresetsAllowed": True,
                "recentActiveCharactersOnly": True,
                "avoidFullMemoryDump": True,
                "variableThinkingFormat": "markdown_first",
                "machineVariableOperations": "optional",
                "sources": effective_context_policy.to_dict(),
                "fingerprint": effective_context_policy.fingerprint,
            },
            "skillRegistry": skill_registry,
            "toolRegistry": self._tool_registry(effective_context_policy),
            "contextAssembly": context_assembly,
            "updatePolicy": {
                "autoUpdateVariables": bool(settings.get("autoUpdateVariables", False)),
                "autoUpdateWiki": bool(settings.get("autoUpdateWiki", False)),
                "autoUpdateVariablesNote": str(settings.get("autoUpdateVariablesNote") or ""),
            },
            "requiredQuestions": self._required_questions(
                requires_template=requires_template,
                templates=chapter_templates,
                invalid_template=requested_template if invalid_template else "",
            ),
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }

    def _context_policy(self, override: ContextPolicy | None) -> ContextPolicy:
        if override is not None:
            if not isinstance(override, ContextPolicy):
                raise TypeError("context_policy must be a ContextPolicy")
            return override
        global_config = self.global_config_service or get_global_config_service()
        return ContextPolicy.from_agent_settings(global_config.read_agent_settings())

    @staticmethod
    def _explicit_generic_file_tool_request(prompt: str) -> bool:
        """Keep explicit generic-tool contracts on the historical Agent bridge."""

        normalized = str(prompt or "").strip().lower()
        return any(
            marker in normalized
            for marker in (
                "write_file",
                "edit_file",
                "read_file",
                "tool call",
                "tool_call",
                "只调用",
                "不要调用其他工具",
            )
        )

    def _intent_frame(
        self,
        *,
        prompt: str,
        active_file: str,
        chapter_count: int,
        intent_frame: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        frame = intent_frame if is_valid_intent_frame(intent_frame) else None
        if frame is None:
            frame = heuristic_intent_frame(prompt=prompt, active_file=active_file)
        frame = dict(frame)
        frame["primary"] = str(frame.get("primary") or "general")
        frame["confidence"] = str(frame.get("confidence") or "low")
        frame["signals"] = list(frame.get("signals") if isinstance(frame.get("signals"), list) else [])
        # operationType / complexity 透传给下游门控与前端。旧注入帧（无这两字段）
        # 用启发式兜底，保证契约始终携带这两个维度。
        operation_type = str(frame.get("operationType") or "").strip().lower()
        if operation_type not in _OPERATION_TYPES:
            operation_type = _heuristic_operation_type(prompt, primary=frame["primary"])
        frame["operationType"] = operation_type
        frame["canWrite"] = intent_frame_allows_project_writes(frame)
        if not frame["canWrite"]:
            frame["assetTargets"] = []
            frame["matchedSkills"] = []
        complexity = str(frame.get("complexity") or "").strip().lower()
        if complexity not in _COMPLEXITY_LEVELS:
            complexity = _heuristic_complexity(prompt)
        frame["complexity"] = complexity
        frame["existingChapterCount"] = chapter_count
        return frame

    def _list_chapter_templates(
        self,
        workspace_root: Path,
        *,
        ensure_structure: bool = True,
    ) -> List[Dict[str, Any]]:
        return self.story_project_service.list_chapter_templates(
            workspace_root,
            ensure_structure=ensure_structure,
        )

    @staticmethod
    def _mark_changed_active_source(
        workspace_root: Path,
        *,
        active_file: str,
        catalog_service: Any,
        catalog_snapshot: Any,
    ) -> None:
        if catalog_snapshot is None:
            return
        normalized = str(active_file or "").strip().replace("\\", "/").strip("/")
        parts = Path(normalized).parts
        if (
            len(parts) < 2
            or parts[0] != "chapters"
            or any(part in {"", ".", ".."} for part in parts)
        ):
            return
        root = Path(workspace_root).resolve()
        candidate = (root / normalized).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return
        entry = catalog_snapshot.get(normalized)
        record_counter("statCount")
        try:
            source_stat = candidate.stat()
            is_file = stat.S_ISREG(source_stat.st_mode)
        except OSError:
            source_stat = None
            is_file = False
        if entry is None and is_file:
            catalog_service.mark_dirty(
                [Path(normalized).parent.as_posix()],
                source="active_file_probe",
            )
            return
        if entry is not None and (
            not is_file
            or source_stat is None
            or int(source_stat.st_size) != entry.size_bytes
            or int(source_stat.st_mtime_ns) != entry.mtime_ns
        ):
            catalog_service.mark_dirty(
                [normalized],
                source="active_file_probe",
            )

    @staticmethod
    def _selected_chapter_template(story_generation: Dict[str, Any]) -> str:
        for key in ("chapterTemplate", "chapterTemplateId", "chapter_template", "chapter_template_id"):
            value = str(story_generation.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _resolve_template(templates: List[Dict[str, Any]], template_id: str) -> Dict[str, Any] | None:
        normalized = str(template_id or "").strip()
        if not normalized:
            return None
        return next((item for item in templates if str(item.get("id") or "") == normalized), None)

    @staticmethod
    def _template_detail(template: Dict[str, Any] | None) -> Dict[str, Any]:
        if not template:
            return {}
        initial_chapters = template.get("initialChapters") if isinstance(template.get("initialChapters"), list) else []
        first_initial = initial_chapters[0] if initial_chapters and isinstance(initial_chapters[0], dict) else {}
        return {
            "id": str(template.get("id") or ""),
            "name": str(template.get("name") or ""),
            "relativePath": str(template.get("relativePath") or ""),
            "description": str(template.get("description") or ""),
            "chapterMode": str(template.get("chapterMode") or "directory"),
            "chapterNamePattern": str(template.get("chapterNamePattern") or ""),
            "segmentNaming": str(template.get("segmentNaming") or "001.md"),
            "contentMode": str(template.get("contentMode") or "multi_fragment"),
            "initialChapterDirectory": str(first_initial.get("directory") or ""),
            "initialChapterFirstSegment": str(first_initial.get("firstSegment") or ""),
            "rules": [str(item) for item in template.get("rules", []) if str(item).strip()]
            if isinstance(template.get("rules"), list)
            else [],
        }

    @staticmethod
    def _required_questions(
        *,
        requires_template: bool,
        templates: List[Dict[str, Any]],
        invalid_template: str = "",
    ) -> List[Dict[str, Any]]:
        if not requires_template:
            return []
        message = "已选择的章节目录模板不可用，请重新选择。"
        if invalid_template:
            message = f"已选择的章节目录模板 `{invalid_template}` 不存在或已失效，请重新选择。"
        return [
            {
                "type": "chapter_template_selection",
                "message": message,
                "options": [
                    {
                        "id": str(item.get("id") or ""),
                        "name": str(item.get("name") or ""),
                        "relativePath": str(item.get("relativePath") or ""),
                    }
                    for item in templates
                ],
            }
        ]

    @staticmethod
    def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _positive_int(value: Any, *, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(1, parsed)

    def _resolve_story_word_count_range(self, story_generation: Dict[str, Any]) -> tuple[int, int]:
        """Resolve the authoritative chapter target band from turn options.

        The new single target has priority. Old min/max and single-value fields
        remain accepted as compatibility input.
        """
        raw_target = story_generation.get(
            "chapterWordCountTarget",
            story_generation.get("chapter_word_count_target"),
        )
        if raw_target is not None:
            target = self._bounded_int(
                raw_target,
                default=DEFAULT_CHAPTER_WORD_COUNT_TARGET,
                minimum=100,
                maximum=20000,
            )
            return target, target
        raw_min = story_generation.get("fragmentWordCountMin")
        raw_max = story_generation.get("fragmentWordCountMax")
        if raw_min is None and raw_max is None:
            legacy = self._bounded_int(
                story_generation.get("fragmentWordCount"),
                default=DEFAULT_CHAPTER_WORD_COUNT_TARGET,
                minimum=100,
                maximum=20000,
            )
            return legacy, legacy
        min_value = self._bounded_int(
            raw_min,
            default=DEFAULT_CHAPTER_WORD_COUNT_TARGET,
            minimum=100,
            maximum=20000,
        )
        max_value = self._bounded_int(
            raw_max,
            default=DEFAULT_CHAPTER_WORD_COUNT_TARGET,
            minimum=100,
            maximum=20000,
        )
        if min_value > max_value:
            min_value, max_value = max_value, min_value
        return min_value, max_value

    def _resolve_precise_word_count(
        self,
        story_generation: Dict[str, Any],
        *,
        settings: Dict[str, Any],
        word_count_mode: str,
    ) -> tuple[bool, str]:
        """Decide whether this turn may spend the optional precision call.

        An explicit request value wins over the stored project setting, so a
        single run can opt in or out without editing the project. A legacy
        min/max range has no single centre to revise towards, so it forces the
        switch off no matter what was asked for.
        """

        if word_count_mode != "target":
            return False, "legacy_range_mode"
        requested = story_generation.get(
            "preciseWordCountEnabled",
            story_generation.get("precise_word_count_enabled"),
        )
        if requested is not None:
            enabled = self.story_project_service._normalize_bool(  # noqa: SLF001
                requested,
                default=False,
            )
            return enabled, "request" if enabled else "request_disabled"
        enabled = self.story_project_service._normalize_bool(  # noqa: SLF001
            settings.get("preciseWordCountEnabled"),
            default=False,
        )
        return enabled, "project_setting" if enabled else "disabled"

    @staticmethod
    def _uses_legacy_story_word_count_range(story_generation: Dict[str, Any]) -> bool:
        if story_generation.get("chapterWordCountTarget") is not None or story_generation.get(
            "chapter_word_count_target"
        ) is not None:
            return False
        return any(
            story_generation.get(key) is not None
            for key in (
                "fragmentWordCountMin",
                "fragment_word_count_min",
                "fragmentWordCountMax",
                "fragment_word_count_max",
            )
        )

    def _skill_registry(self, workspace_root: Path) -> Dict[str, Any]:
        payload = self.story_project_service.read_agent_skill_registry(workspace_root)
        skills = payload.get("skills") if isinstance(payload.get("skills"), list) else []
        compact_skills: List[Dict[str, Any]] = []
        for item in skills:
            if not isinstance(item, dict):
                continue
            compact_skills.append(
                {
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("name") or ""),
                    "file": str(item.get("file") or ""),
                    "intent": str(item.get("intent") or ""),
                    "outputPolicy": str(item.get("outputPolicy") or ""),
                }
            )
        return {
            "registryPath": ".storydex/.agent/skills/registry.json",
            "skillCount": len(compact_skills),
            "skills": compact_skills,
            "policy": payload.get("policy") if isinstance(payload.get("policy"), dict) else {},
        }

    @staticmethod
    def _tool_registry(policy: ContextPolicy) -> Dict[str, Any]:
        tools = [
            {
                "name": "StorydexRuntimePresetStatus",
                "access": "read_only",
                "purpose": "inspect runtime preset eligibility",
            },
            {
                "name": "StorydexVersionStatus",
                "access": "read_only",
                "purpose": "inspect local novel-project Git status",
            },
            {
                "name": "StorydexHelpGuideSearch",
                "access": "read_only",
                "purpose": "search bundled Storydex usage guides before answering operation questions",
            },
            {
                "name": "StorydexProjectSearch",
                "access": "read_only",
                "purpose": "relevance-ranked full-text search over chapters and project assets to verify earlier plot details",
            },
            {
                "name": "StorydexWikiQuery",
                "access": "read_only",
                "purpose": "query WIKI knowledge graph for entity facts, relationships, and foreshadowing with evidence",
            },
            {
                "name": "StorydexSyncWiki",
                "access": "write",
                "purpose": "sync WIKI and knowledge graph from project files",
            },
            {
                "name": "StorydexWordCount",
                "access": "read_only",
                "purpose": "read the same objective non-whitespace character count shown by the Storydex editor",
            },
            {
                "name": "StorydexApplyStoryIncrement",
                "access": "write",
                "purpose": "apply story fragments and post-generation increments",
            },
        ]
        if not policy.active_retrieval_tools:
            tools = [
                tool
                for tool in tools
                if tool["name"] not in {"StorydexProjectSearch", "StorydexWikiQuery"}
            ]
        return {
            "runtime": "coomi",
            "scope": "storydex_domain_tools",
            "toolCount": len(tools),
            "tools": tools,
        }


_SERVICE = StorydexOrchestrationService(
    story_project_service=get_story_project_service(),
    context_assembler=get_storydex_context_assembler_service(),
    length_calibration_service=get_story_length_calibration_service(),
    length_tier_calibration_service=get_story_length_tier_calibration_service(),
)


def get_storydex_orchestration_service() -> StorydexOrchestrationService:
    return _SERVICE
