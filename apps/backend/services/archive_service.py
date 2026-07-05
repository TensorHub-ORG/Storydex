from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


ARCHIVE_SYSTEM_PROMPT = """你是一个创作项目卷宗归档助手。请根据以下内容全文和当前状态，生成结构化归档。

请严格按照以下JSON格式输出：
{
  "plot_summary": "200-500字的内容概要",
  "entity_arcs": {
    "实体名": {
      "start_state": "本卷开始时的状态",
      "end_state": "本卷结束时的状态",
      "key_events": ["关键事件1", "关键事件2"]
    }
  },
  "resolved_conflicts": [
    {"description": "冲突/问题描述", "resolution": "解决方式", "resolved_in": "位置标记"}
  ],
  "active_suspense": [
    {"description": "悬念/伏笔描述", "status": "planted/partially_resolved", "expected_payoff": "短期/中期/长期"}
  ],
  "timeline": [
    {"marker": "时间标记", "events": "事件描述"}
  ],
  "locations_visited": ["地点1", "地点2"],
  "items_introduced": ["物品1", "物品2"]
}

注意：
- plot_summary 要完整概括本卷核心内容走向
- entity_arcs 要体现实体在本卷的成长/变化（可以是角色、组织、概念等）
- resolved_conflicts 只包含本卷内已解决的冲突/问题
- active_suspense 包含本卷埋设但未回收的悬念/伏笔
- timeline 按时间顺序排列关键节点"""


@dataclass
class ArchiveResult:
    arc_range: str = ""
    plot_summary: str = ""
    entity_arcs: Dict[str, Any] = field(default_factory=dict)
    resolved_conflicts: List[Dict[str, Any]] = field(default_factory=list)
    active_suspense: List[Dict[str, Any]] = field(default_factory=list)
    timeline: List[Dict[str, Any]] = field(default_factory=list)
    locations_visited: List[str] = field(default_factory=list)
    items_introduced: List[str] = field(default_factory=list)
    success: bool = False
    error: str = ""
    archive_path: str = ""


class ArchiveService:
    def __init__(
        self,
        *,
        llm_chat_fn: Optional[Callable] = None,
        chapters_per_arc: int = 10,
    ) -> None:
        self._llm_chat_fn = llm_chat_fn
        self.chapters_per_arc = chapters_per_arc

    @staticmethod
    def _tscale() -> float:
        return 1.0

    def should_archive(self, committed_chapter_count: int) -> bool:
        return (
            committed_chapter_count > 0
            and committed_chapter_count % self.chapters_per_arc == 0
        )

    def get_arc_range(self, committed_chapter_count: int) -> str:
        end = committed_chapter_count
        start = end - self.chapters_per_arc + 1
        return f"{start:03d}-{end:03d}"

    def create_archive(
        self,
        *,
        chapter_texts: Dict[str, str],
        story_state: Dict[str, Any],
        character_state: Dict[str, Any],
        arc_range: str,
        workspace_root: Path,
    ) -> ArchiveResult:
        if not chapter_texts or not self._llm_chat_fn:
            return ArchiveResult(arc_range=arc_range, error="No chapters or LLM callable")

        try:
            combined_text = self._combine_chapters(chapter_texts)
            state_context = self._build_state_context(story_state, character_state)

            messages = [
                {"role": "system", "content": ARCHIVE_SYSTEM_PROMPT},
                {"role": "user", "content": f"内容全文：\n{combined_text[:int(12000 * self._tscale())]}\n\n当前状态：\n{state_context[:int(4000 * self._tscale())]}"},
            ]
            result = self._llm_chat_fn(
                messages=messages,
                purpose="archive",
                max_tokens=4000,
                temperature=0.2,
            )
            content = self._extract_content(result)
            if not content:
                return ArchiveResult(arc_range=arc_range, error="LLM returned empty archive")

            parsed = self._parse_json_response(content)
            if not parsed:
                return ArchiveResult(arc_range=arc_range, error="Failed to parse archive JSON")

            archive_data = self._build_archive_data(
                parsed=parsed,
                arc_range=arc_range,
                chapter_texts=chapter_texts,
                story_state=story_state,
            )

            archive_path = self._write_archive(
                archive_data=archive_data,
                arc_range=arc_range,
                workspace_root=workspace_root,
            )

            return ArchiveResult(
                arc_range=arc_range,
                plot_summary=parsed.get("plot_summary", ""),
                entity_arcs=parsed.get("entity_arcs", parsed.get("character_arcs", {})),
                resolved_conflicts=parsed.get("resolved_conflicts", []),
                active_suspense=parsed.get("active_suspense", parsed.get("active_foreshadowing", [])),
                timeline=parsed.get("timeline", []),
                locations_visited=parsed.get("locations_visited", []),
                items_introduced=parsed.get("items_introduced", []),
                success=True,
                archive_path=archive_path,
            )
        except Exception as exc:
            return ArchiveResult(arc_range=arc_range, error=str(exc))

    def _combine_chapters(self, chapter_texts: Dict[str, str]) -> str:
        scale = self._tscale()
        sections: List[str] = []
        for ch_id, text in sorted(chapter_texts.items()):
            sections.append(f"=== {ch_id} ===\n{text[:int(3000 * scale)]}")
        return "\n\n".join(sections)

    def _build_state_context(self, story_state: Dict[str, Any], character_state: Dict[str, Any]) -> str:
        scale = self._tscale()
        parts: List[str] = []
        if story_state:
            parts.append(f"故事状态: {json.dumps(story_state, ensure_ascii=False)[:int(2000 * scale)]}")
        if character_state:
            parts.append(f"角色状态: {json.dumps(character_state, ensure_ascii=False)[:int(2000 * scale)]}")
        return "\n".join(parts)

    def _build_archive_data(
        self,
        *,
        parsed: Dict[str, Any],
        arc_range: str,
        chapter_texts: Dict[str, str],
        story_state: Dict[str, Any],
    ) -> Dict[str, Any]:
        now_iso = datetime.now(timezone.utc).isoformat()
        chapter_titles = {ch_id: ch_id for ch_id in chapter_texts}
        return {
            "version": 1,
            "arc_range": arc_range,
            "generated_at": now_iso,
            "section_titles": chapter_titles,
            "plot_summary": parsed.get("plot_summary", ""),
            "entity_arcs": parsed.get("entity_arcs", parsed.get("character_arcs", {})),
            "resolved_conflicts": parsed.get("resolved_conflicts", []),
            "active_suspense": parsed.get("active_suspense", parsed.get("active_foreshadowing", [])),
            "timeline": parsed.get("timeline", []),
            "locations_visited": parsed.get("locations_visited", []),
            "items_introduced": parsed.get("items_introduced", []),
        }

    def _write_archive(
        self,
        *,
        archive_data: Dict[str, Any],
        arc_range: str,
        workspace_root: Path,
    ) -> str:
        archives_dir = workspace_root / ".storydex" / "archives"
        archives_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archives_dir / f"arc-{arc_range}.json"
        archive_path.write_text(
            json.dumps(archive_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return str(archive_path.relative_to(workspace_root).as_posix())

    def list_archives(self, workspace_root: Path) -> List[Dict[str, Any]]:
        archives_dir = workspace_root / ".storydex" / "archives"
        if not archives_dir.exists():
            return []
        archives: List[Dict[str, Any]] = []
        for path in sorted(archives_dir.glob("arc-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                data["_path"] = str(path.relative_to(workspace_root).as_posix())
                archives.append(data)
            except Exception:
                continue
        return archives

    @staticmethod
    def _extract_content(result: Any) -> str:
        if hasattr(result, "content"):
            return str(result.content or "")
        if isinstance(result, dict):
            return str(result.get("content") or result.get("text") or "")
        if isinstance(result, str):
            return result
        return ""

    @staticmethod
    def _parse_json_response(content: str) -> Optional[Dict[str, Any]]:
        text = content.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        return None
