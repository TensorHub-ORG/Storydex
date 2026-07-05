from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


EXTRACTION_SYSTEM_PROMPT = """你是一个创作项目记忆提取助手。请从以下内容中提取关键信息。

请严格按照以下JSON格式输出：
{
  "entities": [
    {"name": "实体名", "type": "角色/组织/地点/物品/概念", "state": "当前状态", "actions": ["行为1", "行为2"], "relations": [{"target": "另一实体", "relation": "关系描述"}]}
  ],
  "conflicts": [
    {"description": "冲突/问题描述", "status": "active/escalated/resolved", "involved": ["实体1", "实体2"]}
  ],
  "suspense": [
    {"description": "悬念/伏笔描述", "status": "planted/partially_resolved/resolved", "expected_payoff": "短期/中期/长期"}
  ],
  "timeline": [
    {"marker": "时间标记", "event": "事件描述"}
  ],
  "locations": [
    {"name": "地点名", "description": "简述"}
  ]
}

注意：
- 只提取本段内容中明确出现或暗示的信息
- 实体类型可以是：角色、组织、地点、物品、概念等，根据实际内容判断
- 实体状态用1-2个词概括
- 冲突状态：active(进行中), escalated(升级), resolved(已解决)
- 悬念状态：planted(埋设), partially_resolved(部分回收), resolved(已回收)
- 时间标记可以是叙事时间（"第1天"、"第一夜"）或逻辑顺序（"事件A后"）"""

ROLLING_SUMMARY_PROMPT = """你是一个创作内容摘要生成助手。请为以下内容生成简洁的结构化摘要。

要求：
1. 150-300字
2. 包含：主要事件/变化、关键实体行为、冲突/问题变化、新悬念/伏笔
3. 保留关键名称和术语
4. 使用客观描述

直接输出摘要文本，不要加标题或格式标记。"""


@dataclass
class ExtractionResult:
    entities: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    suspense: List[Dict[str, Any]] = field(default_factory=list)
    timeline: List[Dict[str, Any]] = field(default_factory=list)
    locations: List[Dict[str, Any]] = field(default_factory=list)
    rolling_summary: str = ""
    success: bool = False
    error: str = ""


class MemoryExtractionService:
    def __init__(
        self,
        *,
        llm_chat_fn: Optional[Callable] = None,
    ) -> None:
        self._llm_chat_fn = llm_chat_fn

    @staticmethod
    def _tscale() -> float:
        return 1.0

    def extract_memories(
        self,
        chapter_text: str,
        *,
        chapter_id: str = "",
        segment_id: str = "",
    ) -> ExtractionResult:
        if not chapter_text or not self._llm_chat_fn:
            return ExtractionResult(error="No chapter text or LLM callable")

        try:
            extraction = self._call_extraction_llm(chapter_text)
            summary = self._call_summary_llm(chapter_text)
            extraction.rolling_summary = summary
            extraction.success = True
            return extraction
        except Exception as exc:
            return ExtractionResult(error=str(exc))

    def _call_extraction_llm(self, chapter_text: str) -> ExtractionResult:
        messages = [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": f"请从以下内容中提取关键信息：\n\n{chapter_text[:int(6000 * self._tscale())]}"},
        ]
        result = self._llm_chat_fn(
            messages=messages,
            purpose="memory_extraction",
            max_tokens=1500,
            temperature=0.2,
        )
        content = self._extract_content(result)
        if not content:
            return ExtractionResult(error="LLM returned empty extraction")

        parsed = self._parse_json_response(content)
        if not parsed:
            return ExtractionResult(error="Failed to parse extraction JSON")

        return ExtractionResult(
            entities=parsed.get("entities", []),
            conflicts=parsed.get("conflicts", []),
            suspense=parsed.get("suspense", []),
            timeline=parsed.get("timeline", []),
            locations=parsed.get("locations", []),
        )

    def _call_summary_llm(self, chapter_text: str) -> str:
        messages = [
            {"role": "system", "content": ROLLING_SUMMARY_PROMPT},
            {"role": "user", "content": f"请为以下内容生成摘要：\n\n{chapter_text[:int(6000 * self._tscale())]}"},
        ]
        result = self._llm_chat_fn(
            messages=messages,
            purpose="rolling_summary",
            max_tokens=800,
            temperature=0.3,
        )
        return self._extract_content(result).strip()

    def write_extraction_to_state_files(
        self,
        extraction: ExtractionResult,
        *,
        workspace_root: Path,
        chapter_id: str = "",
        segment_id: str = "",
    ) -> List[str]:
        if not extraction.success:
            return []
        written: List[str] = []
        storydex_root = workspace_root / ".storydex"
        current_dir = storydex_root / "memory" / "current"
        current_dir.mkdir(parents=True, exist_ok=True)
        now_iso = datetime.now(timezone.utc).isoformat()

        if extraction.entities:
            entity_state_path = current_dir / "character_state.json"
            existing: Dict[str, Any] = {}
            if entity_state_path.exists():
                try:
                    existing = json.loads(entity_state_path.read_text(encoding="utf-8"))
                    if not isinstance(existing, dict):
                        existing = {}
                except Exception:
                    existing = {}
            entities = existing.get("characters", existing.get("entities", {}))
            if not isinstance(entities, dict):
                entities = {}
            for ent_data in extraction.entities:
                name = str(ent_data.get("name") or "").strip()
                if not name:
                    continue
                entry = entities.get(name, {})
                if not isinstance(entry, dict):
                    entry = {}
                if ent_data.get("location"):
                    entry["current_location"] = str(ent_data["location"]).strip()
                elif ent_data.get("state"):
                    entry["current_state"] = str(ent_data["state"]).strip()
                if ent_data.get("emotion"):
                    entry["emotional_state"] = str(ent_data["emotion"]).strip()
                if ent_data.get("type"):
                    entry["entity_type"] = str(ent_data["type"]).strip()
                if ent_data.get("actions"):
                    entry["recent_actions"] = [str(a).strip() for a in ent_data["actions"] if str(a).strip()][-5:]
                if ent_data.get("relations"):
                    entry["relations"] = ent_data["relations"]
                elif ent_data.get("relationships"):
                    entry["relations"] = ent_data["relationships"]
                entry["last_updated_in_segment"] = segment_id
                entities[name] = entry
            payload = {"version": 2, "updatedAt": now_iso, "entities": entities}
            entity_state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written.append(str(entity_state_path.relative_to(workspace_root).as_posix()))

        if extraction.conflicts or extraction.suspense or extraction.timeline or extraction.locations:
            story_state_path = current_dir / "story_state.json"
            existing_story: Dict[str, Any] = {}
            if story_state_path.exists():
                try:
                    existing_story = json.loads(story_state_path.read_text(encoding="utf-8"))
                    if not isinstance(existing_story, dict):
                        existing_story = {}
                except Exception:
                    existing_story = {}
            existing_story.setdefault("version", 2)
            existing_story["updatedAt"] = now_iso
            if chapter_id:
                existing_story["current_chapter"] = chapter_id
            if segment_id:
                existing_story["current_segment"] = segment_id

            if extraction.conflicts:
                conflicts = existing_story.get("active_conflicts", [])
                if not isinstance(conflicts, list):
                    conflicts = []
                for c in extraction.conflicts:
                    desc = str(c.get("description") or "").strip()
                    if not desc:
                        continue
                    merged = False
                    for existing_c in conflicts:
                        if not isinstance(existing_c, dict):
                            continue
                        ed = str(existing_c.get("description") or "").strip()
                        if ed and (ed in desc or desc in ed):
                            existing_c.update({k: v for k, v in c.items() if v})
                            merged = True
                            break
                    if not merged:
                        new_item = dict(c)
                        if "id" not in new_item:
                            new_item["id"] = f"conflict_{len(conflicts) + 1:03d}"
                        conflicts.append(new_item)
                existing_story["active_conflicts"] = conflicts

            if extraction.suspense:
                suspense = existing_story.get("active_suspense", existing_story.get("active_foreshadowing", []))
                if not isinstance(suspense, list):
                    suspense = []
                for f in extraction.suspense:
                    desc = str(f.get("description") or "").strip()
                    if not desc:
                        continue
                    merged = False
                    for existing_f in suspense:
                        if not isinstance(existing_f, dict):
                            continue
                        ed = str(existing_f.get("description") or "").strip()
                        if ed and (ed in desc or desc in ed):
                            existing_f.update({k: v for k, v in f.items() if v})
                            merged = True
                            break
                    if not merged:
                        new_item = dict(f)
                        if "id" not in new_item:
                            new_item["id"] = f"suspense_{len(suspense) + 1:03d}"
                        suspense.append(new_item)
                existing_story["active_suspense"] = suspense

            if extraction.timeline:
                timeline = existing_story.get("timeline", {})
                if not isinstance(timeline, dict):
                    timeline = {}
                for t in extraction.timeline:
                    marker = str(t.get("marker") or t.get("day") or "").strip()
                    event = str(t.get("event") or "").strip()
                    if marker and event:
                        timeline[marker] = event
                existing_story["timeline"] = timeline

            if extraction.locations:
                locations = existing_story.get("locations", {})
                if not isinstance(locations, dict):
                    locations = {}
                for loc in extraction.locations:
                    name = str(loc.get("name") or "").strip()
                    desc = str(loc.get("description") or "").strip()
                    if name:
                        locations[name] = {"description": desc, "last_mentioned_in": segment_id}
                existing_story["locations"] = locations

            story_state_path.write_text(json.dumps(existing_story, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written.append(str(story_state_path.relative_to(workspace_root).as_posix()))

        return written

    def write_rolling_summary(
        self,
        summary_text: str,
        *,
        workspace_root: Path,
        chapter_id: str = "",
    ) -> Optional[str]:
        if not summary_text:
            return None
        summaries_dir = workspace_root / ".storydex" / "memory" / "summaries" / "rolling"
        summaries_dir.mkdir(parents=True, exist_ok=True)
        ch_key = chapter_id.replace(" ", "_").replace("/", "_") if chapter_id else "unknown"
        summary_path = summaries_dir / f"{ch_key}.md"
        header = f"# {chapter_id} - Rolling Summary\n\n"
        summary_path.write_text(header + summary_text + "\n", encoding="utf-8")
        return str(summary_path.relative_to(workspace_root).as_posix())

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
