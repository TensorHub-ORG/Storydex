from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from services.context_policy import ContextPolicy
from services.global_config_service import GlobalConfigService, get_global_config_service
from services.storydex_context_assembler_service import StorydexContextAssemblerService, get_storydex_context_assembler_service
from services.storydex_intent_service import heuristic_intent_frame, is_valid_intent_frame
from services.story_project_service import StoryProjectService, get_story_project_service


DEFAULT_CHAPTER_TEMPLATE_ID = "default_chapter_directory"


@dataclass(frozen=True)
class StorydexOrchestrationService:
    story_project_service: StoryProjectService
    context_assembler: StorydexContextAssemblerService | None = None
    global_config_service: GlobalConfigService | None = None

    def build_turn_contract(
        self,
        workspace_root: Path,
        *,
        prompt: str,
        active_file: str = "",
        story_generation: Dict[str, Any] | None = None,
        intent_frame: Dict[str, Any] | None = None,
        context_policy: ContextPolicy | None = None,
    ) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        effective_context_policy = self._context_policy(context_policy)
        self.story_project_service.ensure_project_structure(root)
        story_generation = story_generation if isinstance(story_generation, dict) else {}
        settings = self.story_project_service.read_project_settings(root)
        chapters = self.story_project_service.list_chapter_states(root)
        intent = self._intent_frame(
            prompt=prompt,
            active_file=active_file,
            chapter_count=len(chapters),
            intent_frame=intent_frame,
        )
        chapter_templates = self._list_chapter_templates(root)
        requested_template = self._selected_chapter_template(story_generation)
        selected_template = self._resolve_template(
            chapter_templates,
            requested_template or DEFAULT_CHAPTER_TEMPLATE_ID,
        )
        if not requested_template and selected_template is None and chapter_templates:
            selected_template = chapter_templates[0]
        is_new_story = intent["primary"] == "story_generation" and len(chapters) == 0
        invalid_template = bool(requested_template and selected_template is None)
        requires_template = is_new_story and invalid_template

        fragment_count = self._bounded_int(story_generation.get("fragmentCount"), default=1, minimum=1, maximum=20)
        fragment_word_count = self._bounded_int(
            story_generation.get("fragmentWordCount"),
            default=2000,
            minimum=100,
            maximum=20000,
        )
        next_segment_path = ""
        if intent["primary"] == "story_generation" and not requires_template:
            if is_new_story and selected_template:
                next_segment_path = self.story_project_service.initial_segment_path_from_chapter_template(selected_template)
            else:
                next_segment_path = self.story_project_service.compute_next_segment_path(
                    root,
                    active_file=active_file,
                    prompt=prompt,
                )

        turn_plan = {
            "fragmentCount": fragment_count,
            "fragmentWordCount": fragment_word_count,
            "isNewStory": is_new_story,
            "requiresChapterTemplateSelection": requires_template,
            "selectedChapterTemplate": str(selected_template.get("id") or "") if selected_template else "",
            "selectedChapterTemplateDetail": self._template_detail(selected_template),
            "invalidChapterTemplate": requested_template if invalid_template else "",
            "availableChapterTemplates": chapter_templates,
            "nextSegmentPath": next_segment_path,
            "chapterCount": len(chapters),
            "activeFile": active_file,
            "storyFormatSource": "existing_project" if chapters else "selected_chapter_template" if selected_template else "chapter_template",
        }
        context_assembly = (self.context_assembler or StorydexContextAssemblerService(self.story_project_service)).assemble(
            root,
            prompt=prompt,
            active_file=active_file,
            turn_plan=turn_plan,
            policy=effective_context_policy,
        )
        skill_registry = self._skill_registry(root)

        return {
            "_type": "TurnContract",
            "_version": 1,
            "status": "needs_user_input" if requires_template else "ready",
            "intentFrame": intent,
            "executionPolicy": {
                "coomiRole": "general_agent_runtime",
                "storydexRole": "fiction_orchestration",
                "directFileWrites": True,
                "pendingWriteApproval": False,
                "localGitAutoCommit": True,
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
        frame["existingChapterCount"] = chapter_count
        return frame

    def _list_chapter_templates(self, workspace_root: Path) -> List[Dict[str, Any]]:
        return self.story_project_service.list_chapter_templates(workspace_root)

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
            "initialChapterDirectory": str(first_initial.get("directory") or ""),
            "initialChapterFirstSegment": str(first_initial.get("firstSegment") or ""),
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
)


def get_storydex_orchestration_service() -> StorydexOrchestrationService:
    return _SERVICE
