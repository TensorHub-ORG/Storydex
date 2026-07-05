from __future__ import annotations

import re
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence

from core.bounded_text_io import read_text_preview as read_bounded_text_preview
from services.entity_registry import EntityRegistry
from services.fact_memory_store import FactMemoryStore
from services.relationship_memory_store import RelationshipMemoryStore
from services.story_project_service import StoryProjectService, get_story_project_service


_HEADER_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_VARIABLE_SNAPSHOT_PATH = ".storydex/memory/current-state/\u5168\u90e8\u53d8\u91cf.json"


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
    ) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
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
        max_total_chars = 7600

        preset_paths = self._runtime_preset_paths(root)
        preset_block = self.story_project_service._build_preset_context(  # noqa: SLF001 - existing Storydex context builder
            root,
            max_files=5,
            max_chars_per_file=720,
            total_chars=2200,
        )
        sources.append(self._source("runtime_presets", preset_paths, policy="active_or_compiled_safe_only"))
        self._append_block(
            blocks,
            block_id="runtime_presets",
            title="Active or compiled-safe project presets",
            kind="preset",
            content=preset_block,
            source_paths=preset_paths or self._extract_context_paths(preset_block),
            max_chars=2200,
            max_total_chars=max_total_chars,
            notes=notes,
        )

        character_block = self.story_project_service._build_character_hard_constraints_context(  # noqa: SLF001
            root,
            max_files=6,
            max_chars_per_file=520,
            total_chars=1600,
            prompt=prompt,
            active_file=active_file,
        )
        character_paths = self._extract_context_paths(character_block)
        sources.append(self._source("active_characters", character_paths, policy="recent_or_relevant_only"))
        self._append_block(
            blocks,
            block_id="active_characters",
            title="Relevant character hard constraints",
            kind="character",
            content=character_block,
            source_paths=character_paths,
            max_chars=1600,
            max_total_chars=max_total_chars,
            notes=notes,
        )

        worldbook_block = self.story_project_service._build_worldbook_hard_constraints_context(  # noqa: SLF001
            root,
            max_files=4,
            max_chars_per_file=420,
            total_chars=1200,
            prompt=prompt,
            active_file=active_file,
        )
        worldbook_paths = self._extract_context_paths(worldbook_block)
        sources.append(self._source("worldbook", worldbook_paths, policy="relevant_only"))
        self._append_block(
            blocks,
            block_id="worldbook",
            title="Relevant worldbook hard constraints",
            kind="worldbook",
            content=worldbook_block,
            source_paths=worldbook_paths,
            max_chars=1200,
            max_total_chars=max_total_chars,
            notes=notes,
        )

        fact_block = FactMemoryStore(root).project_context(
            prompt=prompt,
            active_file=active_file,
            active_entities=active_entities,
            max_facts=6,
            max_chars=1000,
        )
        fact_count = self._count_context_rows(fact_block)
        sources.append(self._source("facts", [".storydex/memory/current/facts.json"], count=fact_count, policy="relevant_only"))
        self._append_block(
            blocks,
            block_id="facts",
            title="Relevant fact memory",
            kind="fact",
            content=fact_block,
            source_paths=[".storydex/memory/current/facts.json"] if fact_count else [],
            max_chars=1000,
            max_total_chars=max_total_chars,
            notes=notes,
        )

        relationship_block = RelationshipMemoryStore(root).project_context(
            prompt=prompt,
            active_file=active_file,
            active_entities=active_entities,
            max_edges=6,
            max_chars=1000,
        )
        relationship_count = self._count_context_rows(relationship_block)
        sources.append(
            self._source(
                "relationships",
                [".storydex/memory/current/relationship_graph.json"],
                count=relationship_count,
                policy="neighborhood_only",
            )
        )
        self._append_block(
            blocks,
            block_id="relationships",
            title="Relevant relationship neighborhood",
            kind="relationship",
            content=relationship_block,
            source_paths=[".storydex/memory/current/relationship_graph.json"] if relationship_count else [],
            max_chars=1000,
            max_total_chars=max_total_chars,
            notes=notes,
        )

        item_block = self._render_item_memory(root, prompt=prompt, active_file=active_file)
        item_count = self._count_context_rows(item_block)
        sources.append(self._source("items", [".storydex/memory/current/items.json"], count=item_count, policy="compact_relevant_only"))
        self._append_block(
            blocks,
            block_id="items",
            title="Relevant item memory",
            kind="item",
            content=item_block,
            source_paths=[".storydex/memory/current/items.json"] if item_count else [],
            max_chars=900,
            max_total_chars=max_total_chars,
            notes=notes,
        )

        recent_segments = self._recent_segments(root, generation_context=generation_context, active_file=active_file)
        recent_segment_paths = [str(item.get("relativePath") or "") for item in recent_segments if str(item.get("relativePath") or "")]
        sources.append(self._source("recent_segments", recent_segment_paths, policy="compact_recent_only"))
        self._append_block(
            blocks,
            block_id="recent_segments",
            title="Recent story segments",
            kind="segment",
            content=self._render_recent_segments(recent_segments),
            source_paths=recent_segment_paths,
            max_chars=1400,
            max_total_chars=max_total_chars,
            notes=notes,
        )

        scripts = self.story_project_service.list_relevant_scripts(
            root,
            prompt=prompt,
            active_file=active_file,
            limit=3,
            include_content=True,
            max_chars=700,
        )
        script_paths = [str(item.get("relativePath") or "") for item in scripts if str(item.get("relativePath") or "")]
        sources.append(self._source("story_scripts", script_paths, policy="relevant_only"))
        self._append_block(
            blocks,
            block_id="story_scripts",
            title="Relevant story scripts",
            kind="script",
            content=self._render_story_scripts(scripts),
            source_paths=script_paths,
            max_chars=1000,
            max_total_chars=max_total_chars,
            notes=notes,
        )

        variable_block = self._render_variable_snapshot(generation_context)
        variable_count = 1 if variable_block else 0
        sources.append(self._source("variable_snapshot", [_VARIABLE_SNAPSHOT_PATH], count=variable_count, policy="compact_preview_only"))
        self._append_block(
            blocks,
            block_id="variable_snapshot",
            title="Current variable snapshot preview",
            kind="variable",
            content=variable_block,
            source_paths=[_VARIABLE_SNAPSHOT_PATH] if variable_block else [],
            max_chars=900,
            max_total_chars=max_total_chars,
            notes=notes,
        )

        total_chars = sum(int(block.get("charCount") or 0) for block in blocks)
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
            },
            "budget": {
                "maxTotalChars": max_total_chars,
                "totalChars": total_chars,
                "blockCount": len(blocks),
            },
            "activeEntities": list(active_entities),
            "sources": sources,
            "promptBlocks": blocks,
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
    def _source(kind: str, paths: Sequence[str], *, count: int | None = None, policy: str = "") -> Dict[str, Any]:
        clean_paths = [str(path).strip().replace("\\", "/") for path in paths if str(path).strip()]
        return {
            "kind": kind,
            "count": len(clean_paths) if count is None else max(0, int(count or 0)),
            "paths": clean_paths[:12],
            "policy": policy,
        }

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
    ) -> None:
        text = str(content or "").strip()
        if not text:
            return
        used = sum(int(block.get("charCount") or 0) for block in blocks)
        remaining = max_total_chars - used
        if remaining < 160:
            notes.append(f"context_budget_exhausted_before_{block_id}")
            return
        limit = min(max(160, int(max_chars or 160)), remaining)
        truncated = StorydexContextAssemblerService._truncate(text, max_chars=limit)
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
        if len(truncated) < len(text):
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
