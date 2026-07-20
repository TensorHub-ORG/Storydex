from __future__ import annotations

import re
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence, Set, Tuple

from core.bounded_text_io import read_text_preview as read_bounded_text_preview
from services.context_trace_service import (
    build_context_trace,
    create_context_source,
    finalize_context_source,
)
from services.context_policy import ContextPolicy
from services.entity_registry import EntityRegistry
from services.fact_memory_store import FactMemoryStore
from services.relationship_memory_store import RelationshipMemoryStore
from services.story_project_service import StoryProjectService, get_story_project_service


_HEADER_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_VARIABLE_SNAPSHOT_PATH = ".storydex/memory/current-state/\u5168\u90e8\u53d8\u91cf.json"
_WIKI_GRAPH_PATH = ".storydex/wiki/knowledge_graph.json"


@dataclass(frozen=True)
class StorydexContextAssemblerService:
    story_project_service: StoryProjectService

    def assemble(
        self,
        workspace_root: Path,
        *,
        prompt: str = "",
        active_file: str = "",
        turn_plan: Dict[str, Any] | None = None,
        policy: ContextPolicy | None = None,
    ) -> Dict[str, Any]:
        assemble_started = time.perf_counter()
        root = Path(workspace_root).resolve()
        effective_policy = policy if isinstance(policy, ContextPolicy) else ContextPolicy()
        self.story_project_service.ensure_project_structure(root)
        generation_context = self.story_project_service.build_generation_context(
            root,
            active_file=active_file,
            prompt=prompt,
        )
        active_entities = self._infer_active_entities(root, prompt=prompt, active_file=active_file)
        blocks: List[Dict[str, Any]] = []
        sources: List[Dict[str, Any]] = []
        notes: List[str] = []
        max_total_chars = 10000

        source_started = time.perf_counter()
        preset_paths = self._runtime_preset_paths(root) if effective_policy.base_story_context else []
        preset_runtime_context = (
            self._build_preset_runtime_context(
                generation_context,
                prompt=prompt,
                active_entities=active_entities,
            )
            if effective_policy.base_story_context
            else {}
        )
        preset_compile_errors: List[str] = []
        preset_block = (
            self.story_project_service._build_preset_context(  # noqa: SLF001 - existing Storydex context builder
                root,
                max_files=5,
                max_chars_per_file=720,
                total_chars=2200,
                runtime_context=preset_runtime_context,
                compile_errors=preset_compile_errors,
            )
            if effective_policy.base_story_context
            else ""
        )
        for error in preset_compile_errors:
            # 编译失败必须浮出：notes 随 TurnContract 发给前端展示。
            notes.append(f"preset_compile_failed: {error}")
        # 外部导入预设可远超默认 2200 字预算；按实际长度放开该块，
        # 并把全局预算按溢出扩容，保证其他上下文块的预算不被挤占。
        preset_block_budget = max(2200, len(preset_block))
        max_total_chars += max(0, preset_block_budget - 2200)
        sources.append(
            self._source(
                "runtime_presets",
                preset_paths,
                candidate=preset_block,
                policy="active_or_compiled_safe_only",
                elapsed_ms=(time.perf_counter() - source_started) * 1000,
            )
        )
        self._append_policy_block(
            blocks,
            enabled=effective_policy.base_story_context,
            block_id="runtime_presets",
            title="Active or compiled project presets",
            kind="preset",
            content=preset_block,
            source_paths=preset_paths or self._extract_context_paths(preset_block),
            max_chars=preset_block_budget,
            max_total_chars=max_total_chars,
            notes=notes,
            trace_source=sources[-1],
        )

        # 最近正文片段紧随预设：续写时上文是第一上下文，
        # 不能被后续硬约束/记忆块把预算挤占殆尽。
        source_started = time.perf_counter()
        recent_segments = (
            self._recent_segments(root, generation_context=generation_context, active_file=active_file)
            if effective_policy.base_story_context
            else []
        )
        recent_segment_paths = [str(item.get("relativePath") or "") for item in recent_segments if str(item.get("relativePath") or "")]
        recent_segment_block = self._render_recent_segments(recent_segments)
        sources.append(
            self._source(
                "recent_segments",
                recent_segment_paths,
                candidate=recent_segment_block,
                policy="compact_recent_only",
                elapsed_ms=(time.perf_counter() - source_started) * 1000,
            )
        )
        self._append_policy_block(
            blocks,
            enabled=effective_policy.base_story_context,
            block_id="recent_segments",
            title="Recent story segments",
            kind="segment",
            content=recent_segment_block,
            source_paths=recent_segment_paths,
            max_chars=1400,
            max_total_chars=max_total_chars,
            notes=notes,
            trace_source=sources[-1],
        )

        # 滚动章节摘要：介于"紧邻上文"与"实体记忆"之间的中程剧情脉络。
        source_started = time.perf_counter()
        rolling_summaries, rolling_paths = (
            self._render_rolling_summaries(root)
            if effective_policy.story_structured_memory
            else ("", [])
        )
        sources.append(
            self._source(
                "rolling_summaries",
                rolling_paths,
                candidate=rolling_summaries,
                policy="latest_chapters_only",
                elapsed_ms=(time.perf_counter() - source_started) * 1000,
            )
        )
        self._append_policy_block(
            blocks,
            enabled=effective_policy.story_structured_memory,
            block_id="rolling_summaries",
            title="Rolling chapter summaries",
            kind="summary",
            content=rolling_summaries,
            source_paths=rolling_paths,
            max_chars=800,
            max_total_chars=max_total_chars,
            notes=notes,
            trace_source=sources[-1],
        )

        source_started = time.perf_counter()
        character_block = (
            self.story_project_service._build_character_hard_constraints_context(  # noqa: SLF001
                root,
                max_files=6,
                max_chars_per_file=520,
                total_chars=1600,
                prompt=prompt,
                active_file=active_file,
            )
            if effective_policy.base_story_context
            else ""
        )
        character_paths = self._extract_context_paths(character_block)
        sources.append(
            self._source(
                "active_characters",
                character_paths,
                candidate=character_block,
                policy="recent_or_relevant_only",
                elapsed_ms=(time.perf_counter() - source_started) * 1000,
            )
        )
        self._append_policy_block(
            blocks,
            enabled=effective_policy.base_story_context,
            block_id="active_characters",
            title="Relevant character hard constraints",
            kind="character",
            content=character_block,
            source_paths=character_paths,
            max_chars=1600,
            max_total_chars=max_total_chars,
            notes=notes,
            trace_source=sources[-1],
        )

        source_started = time.perf_counter()
        worldbook_block = (
            self.story_project_service._build_worldbook_hard_constraints_context(  # noqa: SLF001
                root,
                max_files=4,
                max_chars_per_file=420,
                total_chars=1200,
                prompt=prompt,
                active_file=active_file,
            )
            if effective_policy.base_story_context
            else ""
        )
        worldbook_paths = self._extract_context_paths(worldbook_block)
        sources.append(
            self._source(
                "worldbook",
                worldbook_paths,
                candidate=worldbook_block,
                policy="relevant_only",
                elapsed_ms=(time.perf_counter() - source_started) * 1000,
            )
        )
        self._append_policy_block(
            blocks,
            enabled=effective_policy.base_story_context,
            block_id="worldbook",
            title="Relevant worldbook hard constraints",
            kind="worldbook",
            content=worldbook_block,
            source_paths=worldbook_paths,
            max_chars=1200,
            max_total_chars=max_total_chars,
            notes=notes,
            trace_source=sources[-1],
        )

        source_started = time.perf_counter()
        fact_block = (
            FactMemoryStore(root).project_context(
                prompt=prompt,
                active_file=active_file,
                active_entities=active_entities,
                max_facts=6,
                max_chars=1000,
            )
            if effective_policy.story_structured_memory
            else ""
        )
        fact_count = self._count_context_rows(fact_block)
        sources.append(
            self._source(
                "facts",
                [".storydex/memory/current/facts.json"],
                candidate=fact_block,
                count=fact_count,
                policy="relevant_only",
                elapsed_ms=(time.perf_counter() - source_started) * 1000,
            )
        )
        self._append_policy_block(
            blocks,
            enabled=effective_policy.story_structured_memory,
            block_id="facts",
            title="Relevant fact memory",
            kind="fact",
            content=fact_block,
            source_paths=[".storydex/memory/current/facts.json"] if fact_count else [],
            max_chars=1000,
            max_total_chars=max_total_chars,
            notes=notes,
            trace_source=sources[-1],
        )

        source_started = time.perf_counter()
        relationship_block = (
            RelationshipMemoryStore(root).project_context(
                prompt=prompt,
                active_file=active_file,
                active_entities=active_entities,
                max_edges=6,
                max_chars=1000,
            )
            if effective_policy.story_structured_memory
            else ""
        )
        relationship_count = self._count_context_rows(relationship_block)
        sources.append(
            self._source(
                "relationships",
                [".storydex/memory/current/relationship_graph.json"],
                candidate=relationship_block,
                count=relationship_count,
                policy="neighborhood_only",
                elapsed_ms=(time.perf_counter() - source_started) * 1000,
            )
        )
        self._append_policy_block(
            blocks,
            enabled=effective_policy.story_structured_memory,
            block_id="relationships",
            title="Relevant relationship neighborhood",
            kind="relationship",
            content=relationship_block,
            source_paths=[".storydex/memory/current/relationship_graph.json"] if relationship_count else [],
            max_chars=1000,
            max_total_chars=max_total_chars,
            notes=notes,
            trace_source=sources[-1],
        )

        source_started = time.perf_counter()
        item_block = (
            self._render_item_memory(root, prompt=prompt, active_file=active_file)
            if effective_policy.story_structured_memory
            else ""
        )
        item_count = self._count_context_rows(item_block)
        sources.append(
            self._source(
                "items",
                [".storydex/memory/current/items.json"],
                candidate=item_block,
                count=item_count,
                policy="compact_relevant_only",
                elapsed_ms=(time.perf_counter() - source_started) * 1000,
            )
        )
        self._append_policy_block(
            blocks,
            enabled=effective_policy.story_structured_memory,
            block_id="items",
            title="Relevant item memory",
            kind="item",
            content=item_block,
            source_paths=[".storydex/memory/current/items.json"] if item_count else [],
            max_chars=900,
            max_total_chars=max_total_chars,
            notes=notes,
            trace_source=sources[-1],
        )

        source_started = time.perf_counter()
        related_passages, related_paths = (
            self._render_related_passages(
                root,
                prompt=prompt,
                active_entities=active_entities,
                # rolling_summaries 块已注入的摘要不再重复召回。
                exclude_paths={*recent_segment_paths, *rolling_paths, str(active_file or "").replace("\\", "/")},
            )
            if effective_policy.passive_fts
            else ("", [])
        )
        sources.append(
            self._source(
                "related_passages",
                related_paths,
                candidate=related_passages,
                policy="fts5_bm25_top_hits",
                elapsed_ms=(time.perf_counter() - source_started) * 1000,
            )
        )
        self._append_policy_block(
            blocks,
            enabled=effective_policy.passive_fts,
            block_id="related_passages",
            title="Related project passages (retrieval)",
            kind="retrieval",
            content=related_passages,
            source_paths=related_paths,
            max_chars=1000,
            max_total_chars=max_total_chars,
            notes=notes,
            trace_source=sources[-1],
        )

        source_started = time.perf_counter()
        wiki_reference, wiki_entry_count = (
            self._render_wiki_reference(root, active_entities=active_entities)
            if effective_policy.wiki_context
            else ("", 0)
        )
        wiki_paths = [_WIKI_GRAPH_PATH] if wiki_entry_count else []
        sources.append(
            self._source(
                "wiki_reference",
                wiki_paths,
                candidate=wiki_reference,
                count=wiki_entry_count,
                policy="entity_matched_reference_only",
                elapsed_ms=(time.perf_counter() - source_started) * 1000,
            )
        )
        self._append_policy_block(
            blocks,
            enabled=effective_policy.wiki_context,
            block_id="wiki_reference",
            title="WIKI reference entries (non-canonical)",
            kind="wiki",
            content=wiki_reference,
            source_paths=wiki_paths,
            max_chars=800,
            max_total_chars=max_total_chars,
            notes=notes,
            trace_source=sources[-1],
        )

        source_started = time.perf_counter()
        scripts = (
            self.story_project_service.list_relevant_scripts(
                root,
                prompt=prompt,
                active_file=active_file,
                limit=3,
                include_content=True,
                max_chars=700,
            )
            if effective_policy.base_story_context
            else []
        )
        script_paths = [str(item.get("relativePath") or "") for item in scripts if str(item.get("relativePath") or "")]
        script_block = self._render_story_scripts(scripts)
        sources.append(
            self._source(
                "story_scripts",
                script_paths,
                candidate=script_block,
                policy="relevant_only",
                elapsed_ms=(time.perf_counter() - source_started) * 1000,
            )
        )
        self._append_policy_block(
            blocks,
            enabled=effective_policy.base_story_context,
            block_id="story_scripts",
            title="Relevant story scripts",
            kind="script",
            content=script_block,
            source_paths=script_paths,
            max_chars=1000,
            max_total_chars=max_total_chars,
            notes=notes,
            trace_source=sources[-1],
        )

        source_started = time.perf_counter()
        variable_block = (
            self._render_variable_snapshot(generation_context)
            if effective_policy.story_structured_memory
            else ""
        )
        variable_count = 1 if variable_block else 0
        sources.append(
            self._source(
                "variable_snapshot",
                [_VARIABLE_SNAPSHOT_PATH],
                candidate=variable_block,
                count=variable_count,
                policy="compact_preview_only",
                elapsed_ms=(time.perf_counter() - source_started) * 1000,
            )
        )
        self._append_policy_block(
            blocks,
            enabled=effective_policy.story_structured_memory,
            block_id="variable_snapshot",
            title="Current variable snapshot preview",
            kind="variable",
            content=variable_block,
            source_paths=[_VARIABLE_SNAPSHOT_PATH] if variable_block else [],
            max_chars=900,
            max_total_chars=max_total_chars,
            notes=notes,
            trace_source=sources[-1],
        )

        total_chars = sum(int(block.get("charCount") or 0) for block in blocks)
        context_trace = build_context_trace(
            sources,
            blocks,
            assemble_ms=(time.perf_counter() - assemble_started) * 1000,
            context_policy=effective_policy.to_dict(),
        )
        return {
            "_type": "ContextAssembly",
            "_version": 1,
            "status": "assembled",
            "policy": {
                "activePresetsOnly": True,
                "compiledSafePresetsAllowed": True,
                "recentActiveCharactersOnly": True,
                "avoidFullMemoryDump": True,
                "variableSnapshotPreviewOnly": True,
                "sources": effective_policy.to_dict(),
                "fingerprint": effective_policy.fingerprint,
            },
            "budget": {
                "maxTotalChars": max_total_chars,
                "totalChars": total_chars,
                "blockCount": len(blocks),
            },
            "activeEntities": list(active_entities),
            "sources": sources,
            "promptBlocks": blocks,
            "contextTrace": context_trace,
            "notes": notes,
            "turnPlanRef": {
                "fragmentCount": int((turn_plan or {}).get("fragmentCount") or 0),
                "fragmentWordCount": int((turn_plan or {}).get("fragmentWordCount") or 0),
                "nextSegmentPath": str((turn_plan or {}).get("nextSegmentPath") or generation_context.get("nextSegmentPath") or ""),
            },
        }

    def _runtime_preset_paths(self, root: Path) -> List[str]:
        paths: List[str] = []
        for path in self.story_project_service._runtime_preset_files(root, max_files=5):  # noqa: SLF001
            try:
                paths.append(path.relative_to(root).as_posix())
            except ValueError:
                continue
        return paths

    @staticmethod
    def _build_preset_runtime_context(
        generation_context: Dict[str, Any],
        *,
        prompt: str,
        active_entities: Sequence[str],
    ) -> Dict[str, Any]:
        settings = generation_context.get("projectSettings") if isinstance(generation_context.get("projectSettings"), dict) else {}
        persona_name = str(settings.get("personaName") or settings.get("userName") or "user").strip()
        active_names = [str(item).strip() for item in active_entities if str(item).strip()]
        context: Dict[str, Any] = {
            "prompt": str(prompt or ""),
            "lastUserMessage": str(prompt or ""),
            "user": persona_name,
            "personaName": persona_name,
            # Storydex 目前只有常规生成一种触发类型；ST 的 injection_trigger
            # 按这个值判定是否生效。
            "generationType": "normal",
        }
        if active_names:
            context.update(
                {
                    "char": active_names[0],
                    "character": active_names[0],
                    "characterName": active_names[0],
                    "activeEntities": active_names,
                }
            )
        return context

    def _render_related_passages(
        self,
        root: Path,
        *,
        prompt: str,
        active_entities: Sequence[str],
        exclude_paths: Set[str],
    ) -> Tuple[str, List[str]]:
        """FTS5/BM25 检索与本轮请求相关的项目段落（中文 bigram 索引）。

        查询只取高置信度信号：活跃实体 + prompt 中被引号括起的专有名词。
        prompt 全文不进查询——续写指令里的功能词（"继续写这一段"等）切成
        bigram 后会把召回带偏向字面重叠多的无关文档。没有可用检索词时
        跳过本块。受 CONTEXT_PIPELINE_FTS5 Flag 控制，索引失败时静默降级
        为空块，不影响本轮生成。
        """
        query = " ".join(self._related_passage_query_terms(prompt, active_entities)).strip()
        if not query:
            return "", []
        try:
            from core.feature_flags import get_flags

            if not get_flags().get_bool("CONTEXT_PIPELINE_FTS5"):
                return "", []
            from services.retrieval_service import get_retrieval_service

            service = get_retrieval_service(root)
            service.watch_files()
            hits = service.search(query, top_k=8)
        except Exception:
            return "", []

        normalized_excludes = {str(path).replace("\\", "/") for path in exclude_paths if str(path).strip()}
        selected: List[Tuple[str, str]] = []
        for path, _score, snippet in hits:
            normalized = str(path).replace("\\", "/")
            if normalized in normalized_excludes:
                continue
            if not self._is_retrievable_content_path(normalized):
                continue
            if not snippet.strip():
                continue
            selected.append((normalized, snippet.strip()))
            if len(selected) >= 3:
                break
        if not selected:
            return "", []
        lines = [
            "[Related Project Passages]",
            "Retrieval hits for this turn only; treat as reference excerpts, not full documents.",
        ]
        for path, snippet in selected:
            lines.extend(["", f"### {path}", snippet])
        return "\n".join(lines).strip(), [path for path, _snippet in selected]

    _RETRIEVABLE_CONTENT_PREFIXES = (
        "chapters/",
        ".storydex/characters/",
        ".storydex/worldbook/",
        ".storydex/memory/chapters/",
        ".storydex/memory/summaries/",
    )

    _QUOTED_TERM_RE = re.compile(r"[「『《“‘]([^「」『』《》“”‘’]{2,24})[」』》”’]")

    @classmethod
    def _related_passage_query_terms(cls, prompt: str, active_entities: Sequence[str]) -> List[str]:
        """被动检索的查询词：活跃实体正名 + prompt 中引号/书名号括起的短语。"""
        terms = [str(item).strip() for item in active_entities if str(item).strip()]
        for match in cls._QUOTED_TERM_RE.finditer(str(prompt or "")):
            value = match.group(1).strip()
            if value:
                terms.append(value)
        return list(dict.fromkeys(terms))

    @staticmethod
    def _render_wiki_reference(root: Path, *, active_entities: Sequence[str]) -> Tuple[str, int]:
        """活跃实体命中的 WIKI 蒸馏条目，作为参考层注入（非硬约束）。

        只读已生成的 knowledge_graph.json；不存在或损坏时静默跳过，
        被动路径绝不触发 WIKI 构建。WIKI 含模型推断内容，因此：
        needsReview 条目排除、confidence 随行标注、块头声明权威事实
        以正文/角色文件/变量记忆为准。
        """
        entities = [str(item).strip() for item in active_entities if str(item).strip()]
        if not entities:
            return "", 0
        wiki_path = root / _WIKI_GRAPH_PATH
        if not wiki_path.exists():
            return "", 0
        try:
            payload = json.loads(wiki_path.read_text(encoding="utf-8"))
        except Exception:
            return "", 0
        raw_entries = payload.get("entries") if isinstance(payload, dict) else None
        entries = [entry for entry in raw_entries if isinstance(entry, dict)] if isinstance(raw_entries, list) else []
        if not entries:
            return "", 0

        selected: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        for entity in entities:
            for entry in entries:
                if bool(entry.get("needsReview")):
                    continue
                if str(entry.get("category") or "") == "overview":
                    continue
                entry_id = str(entry.get("id") or "")
                if not entry_id or entry_id in seen_ids:
                    continue
                title = str(entry.get("title") or "").strip()
                if not title or (entity not in title and title not in entity):
                    continue
                selected.append(entry)
                seen_ids.add(entry_id)
                break  # 每个实体最多取一条，防单实体多条目霸占预算
            if len(selected) >= 4:
                break
        if not selected:
            return "", 0

        lines = [
            "[WIKI Reference Entries]",
            "Distilled WIKI entries for active entities. Reference only and may include model inference; "
            "canonical facts live in chapters, character files, and variable memory.",
        ]
        for entry in selected:
            title = str(entry.get("title") or "").strip()
            category = str(entry.get("categoryLabel") or entry.get("category") or "").strip()
            summary = " ".join(str(entry.get("summary") or "").split())
            piece = f"- {title}"
            annotations = [item for item in (category, StorydexContextAssemblerService._format_confidence(entry.get("confidence"))) if item]
            if annotations:
                piece += f" ({', '.join(annotations)})"
            if summary:
                piece += f": {summary}"
            lines.append(piece)
        return "\n".join(lines), len(selected)

    @staticmethod
    def _format_confidence(value: Any) -> str:
        try:
            return f"confidence={float(value):.2f}"
        except (TypeError, ValueError):
            return ""

    @classmethod
    def _is_retrievable_content_path(cls, normalized_path: str) -> bool:
        """只召回创作内容；框架骨架（README/模板/配置/预设/WIKI 生成物）排除。"""
        lowered = normalized_path.casefold()
        if lowered.endswith("/readme.md") or lowered == "readme.md":
            return False
        return any(normalized_path.startswith(prefix) for prefix in cls._RETRIEVABLE_CONTENT_PREFIXES)

    @staticmethod
    def _render_rolling_summaries(root: Path) -> Tuple[str, List[str]]:
        """最近章节的滚动摘要（memory/summaries/rolling/），按 mtime 取最新两章。"""
        summaries_dir = root / ".storydex" / "memory" / "summaries" / "rolling"
        if not summaries_dir.is_dir():
            return "", []
        candidates: List[Tuple[float, Path]] = []
        for path in summaries_dir.glob("*.md"):
            if not path.is_file():
                continue
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
        candidates.sort(key=lambda item: item[0], reverse=True)
        lines = [
            "[Rolling Chapter Summaries]",
            "Mid-range plot context from the most recent chapters; compact summaries only.",
        ]
        paths: List[str] = []
        for _mtime, path in candidates[:2]:
            try:
                raw = read_bounded_text_preview(path, max_chars=600)
            except Exception:
                continue
            body = " ".join(
                " ".join(line.strip() for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#")).split()
            )[:400]
            if not body:
                continue
            relative = path.relative_to(root).as_posix()
            paths.append(relative)
            lines.extend(["", f"### {relative}", body])
        if not paths:
            return "", []
        return "\n".join(lines).strip(), paths

    def _recent_segments(
        self,
        root: Path,
        *,
        generation_context: Dict[str, Any],
        active_file: str,
    ) -> List[Dict[str, Any]]:
        focus = generation_context.get("focusChapter") if isinstance(generation_context.get("focusChapter"), dict) else {}
        focus_relative = str(focus.get("relativePath") or "").strip()
        recent = self.story_project_service.list_recent_segments(
            root,
            chapter_relative_path=focus_relative,
            limit=3,
            include_content=True,
            max_chars=700,
        )
        if recent:
            return recent
        return self.story_project_service.list_recent_segments(
            root,
            chapter_relative_path="",
            limit=3,
            include_content=True,
            max_chars=700,
        )

    def _infer_active_entities(self, root: Path, *, prompt: str, active_file: str) -> Sequence[str]:
        text = str(prompt or "")
        active_path = self._safe_workspace_file(root, active_file)
        if active_path is not None and active_path.exists() and active_path.is_file():
            try:
                text += "\n" + read_bounded_text_preview(active_path, max_chars=3000)
            except Exception:
                pass
        if not text.strip():
            return ()
        registry = EntityRegistry(root)
        fallback_names = self._fallback_entity_names(root)
        return registry.resolve_mentions(text, fallback_names=fallback_names)[:12]

    def _fallback_entity_names(self, root: Path) -> Sequence[str]:
        names: List[str] = []
        for record in EntityRegistry(root).load_records():
            names.append(record.canonical_name)
            names.extend(record.aliases)
        facts = FactMemoryStore(root).load_facts_payload().get("facts", [])
        if isinstance(facts, list):
            for item in facts:
                if isinstance(item, dict):
                    names.append(str(item.get("subject") or ""))
        edges = RelationshipMemoryStore(root).load_graph().get("edges", [])
        if isinstance(edges, list):
            for item in edges:
                if isinstance(item, dict):
                    names.append(str(item.get("source") or ""))
                    names.append(str(item.get("target") or ""))
        names.extend(self._character_file_names(root))
        return tuple(dict.fromkeys(name.strip() for name in names if name and name.strip()))

    def _character_file_names(self, root: Path) -> Sequence[str]:
        character_root = root / ".storydex" / "characters"
        if not character_root.exists() or not character_root.is_dir():
            return ()
        names: List[str] = []
        for path in character_root.rglob("*"):
            if not path.is_file() or path.name.lower() == "readme.md":
                continue
            if path.suffix.lower() not in {".md", ".txt", ".json"}:
                continue
            names.append(re.sub(r"^\d{1,3}[_-]", "", path.stem).strip())
        return tuple(names)

    @staticmethod
    def _safe_workspace_file(root: Path, active_file: str) -> Path | None:
        normalized = str(active_file or "").strip().replace("\\", "/").lstrip("/")
        if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
            return None
        candidate = (root / normalized).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    @staticmethod
    def _render_recent_segments(segments: Sequence[Dict[str, Any]]) -> str:
        if not segments:
            return ""
        lines = [
            "[Recent Story Segments]",
            "Compact recent narrative context only; do not treat this as a full chapter dump.",
        ]
        for item in segments:
            relative_path = str(item.get("relativePath") or "").strip()
            content = str(item.get("content") or item.get("snippet") or "").strip()
            if not relative_path or not content:
                continue
            lines.extend(["", f"### {relative_path}", content])
        return "\n".join(lines).strip() if len(lines) > 2 else ""

    @staticmethod
    def _render_story_scripts(scripts: Sequence[Dict[str, Any]]) -> str:
        if not scripts:
            return ""
        lines = [
            "[Relevant Story Scripts]",
            "Use only compact script excerpts that match the current turn.",
        ]
        for item in scripts:
            relative_path = str(item.get("relativePath") or "").strip()
            content = str(item.get("content") or item.get("snippet") or "").strip()
            if not relative_path or not content:
                continue
            lines.extend(["", f"### {relative_path}", content])
        return "\n".join(lines).strip() if len(lines) > 2 else ""

    @staticmethod
    def _render_variable_snapshot(generation_context: Dict[str, Any]) -> str:
        preview = str(generation_context.get("currentStatePreview") or "").strip()
        if not preview or preview in {"{}", "[]", "null"}:
            return ""
        return "\n".join(
            [
                "[Current Variable Snapshot Preview]",
                "This is a compact reference only. Variable thinking remains Markdown-first; fixed JSON path/value output is not required.",
                preview,
            ]
        ).strip()

    @staticmethod
    def _render_item_memory(root: Path, *, prompt: str, active_file: str) -> str:
        items_path = root / ".storydex" / "memory" / "current" / "items.json"
        if not items_path.exists():
            return ""
        try:
            payload = json.loads(items_path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        items = payload.get("items") if isinstance(payload, dict) and isinstance(payload.get("items"), list) else []
        normalized_items = [item for item in items if isinstance(item, dict) and str(item.get("name") or "").strip()]
        if not normalized_items:
            return ""

        text = str(prompt or "")
        active_path = StorydexContextAssemblerService._safe_workspace_file(root, active_file)
        if active_path is not None and active_path.exists() and active_path.is_file():
            try:
                text += "\n" + read_bounded_text_preview(active_path, max_chars=3000)
            except Exception:
                pass
        lowered = text.lower()

        def score(item: Dict[str, Any]) -> int:
            values = [
                item.get("name"),
                item.get("summary"),
                item.get("owner"),
                item.get("location"),
                item.get("state"),
                *(item.get("aliases") if isinstance(item.get("aliases"), list) else []),
                *(item.get("tags") if isinstance(item.get("tags"), list) else []),
            ]
            haystack = " ".join(str(value or "") for value in values)
            score_value = 0
            name = str(item.get("name") or "").strip()
            if name and name in text:
                score_value += 20
            for token in re.findall(r"[A-Za-z0-9_]{3,}|[\u4e00-\u9fff]{2,}", haystack):
                if token and token.lower() in lowered:
                    score_value += 3
            if item.get("latestSegment"):
                score_value += 1
            return score_value

        ranked = sorted(
            normalized_items,
            key=lambda item: (-score(item), str(item.get("updatedAt") or ""), str(item.get("name") or "")),
        )
        selected = [item for item in ranked if score(item) > 0][:6] or ranked[:6]
        lines = [
            "[Project Item Context]",
            "Compact item/object memory only. Missing details remain unknown; do not invent ownership, effects, or location.",
        ]
        for item in selected:
            name = str(item.get("name") or "").strip()
            pieces = [
                f"- {name}",
                str(item.get("kind") or "item"),
                f"status={item.get('status') or 'active'}",
            ]
            for key, label in (("owner", "owner"), ("location", "location"), ("state", "state")):
                value = str(item.get(key) or "").strip()
                if value:
                    pieces.append(f"{label}={value}")
            summary = str(item.get("summary") or "").strip()
            if summary:
                pieces.append(f"summary={summary}")
            latest = str(item.get("latestSegment") or "").strip()
            if latest:
                pieces.append(f"source={latest}")
            lines.append(" | ".join(pieces))
        return StorydexContextAssemblerService._truncate("\n".join(lines), max_chars=900)

    @staticmethod
    def _source(
        kind: str,
        paths: Sequence[str],
        *,
        candidate: str = "",
        count: int | None = None,
        policy: str = "",
        elapsed_ms: float = 0.0,
    ) -> Dict[str, Any]:
        return create_context_source(
            kind,
            paths,
            candidate=candidate,
            count=count,
            policy=policy,
            elapsed_ms=elapsed_ms,
        )

    @staticmethod
    def _append_policy_block(
        blocks: List[Dict[str, Any]],
        *,
        enabled: bool,
        block_id: str,
        title: str,
        kind: str,
        content: str,
        source_paths: Sequence[str],
        max_chars: int,
        max_total_chars: int,
        notes: List[str],
        trace_source: Dict[str, Any] | None = None,
    ) -> None:
        if not enabled:
            finalize_context_source(
                trace_source,
                content="",
                included=False,
                drop_reason="disabled_by_policy",
            )
            return
        StorydexContextAssemblerService._append_block(
            blocks,
            block_id=block_id,
            title=title,
            kind=kind,
            content=content,
            source_paths=source_paths,
            max_chars=max_chars,
            max_total_chars=max_total_chars,
            notes=notes,
            trace_source=trace_source,
        )

    @staticmethod
    def _append_block(
        blocks: List[Dict[str, Any]],
        *,
        block_id: str,
        title: str,
        kind: str,
        content: str,
        source_paths: Sequence[str],
        max_chars: int,
        max_total_chars: int,
        notes: List[str],
        trace_source: Dict[str, Any] | None = None,
    ) -> None:
        text = str(content or "").strip()
        if not text:
            finalize_context_source(
                trace_source,
                content="",
                included=False,
                drop_reason="empty",
            )
            return
        used = sum(int(block.get("charCount") or 0) for block in blocks)
        remaining = max_total_chars - used
        if remaining < 160:
            notes.append(f"context_budget_exhausted_before_{block_id}")
            finalize_context_source(
                trace_source,
                content="",
                included=False,
                drop_reason="budget_exhausted",
            )
            return
        limit = min(max(160, int(max_chars or 160)), remaining)
        truncated = StorydexContextAssemblerService._truncate(text, max_chars=limit)
        was_truncated = len(truncated) < len(text)
        blocks.append(
            {
                "id": block_id,
                "kind": kind,
                "title": title,
                "sourcePaths": [str(path).strip().replace("\\", "/") for path in source_paths if str(path).strip()][:12],
                "charCount": len(truncated),
                "content": truncated,
            }
        )
        finalize_context_source(
            trace_source,
            content=truncated,
            included=True,
            truncated=was_truncated,
            drop_reason="truncated_to_budget" if was_truncated else "",
        )
        if was_truncated:
            notes.append(f"{block_id}_truncated_to_budget")

    @staticmethod
    def _extract_context_paths(content: str) -> List[str]:
        paths: List[str] = []
        for match in _HEADER_RE.finditer(str(content or "")):
            value = match.group(1).strip()
            if value and (value.startswith(".storydex/") or value.startswith("chapters/")):
                paths.append(value)
        return list(dict.fromkeys(paths))

    @staticmethod
    def _count_context_rows(content: str) -> int:
        return len(re.findall(r"(?:^|\s)-\s+", str(content or "")))

    @staticmethod
    def _truncate(value: str, *, max_chars: int) -> str:
        text = str(value or "").strip()
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 18)].rstrip() + "\n... [truncated]"


_SERVICE = StorydexContextAssemblerService(story_project_service=get_story_project_service())


def get_storydex_context_assembler_service() -> StorydexContextAssemblerService:
    return _SERVICE
