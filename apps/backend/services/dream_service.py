from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


DREAM_CONSOLIDATE_PROMPT = """你是一个创作项目记忆整合助手。请检查以下记忆库，执行以下操作：

1. 合并相似的冲突/悬念条目（描述相近的合并为一条）
2. 删除矛盾的事实（保留最新版本）
3. 标记已过时的条目（状态为 resolved 的冲突、已回收的悬念）
4. 检查实体状态是否一致（位置/状态是否与最新内容匹配）

请输出整合后的记忆库，格式与输入相同。只输出 JSON，不要其他内容。"""


@dataclass
class DreamResult:
    phase: str = ""
    items_processed: int = 0
    items_merged: int = 0
    items_pruned: int = 0
    success: bool = False
    error: str = ""
    duration_ms: int = 0


class DreamService:
    def __init__(
        self,
        *,
        llm_chat_fn: Optional[Callable] = None,
        min_session_count: int = 5,
        min_interval_hours: int = 24,
        stale_days: int = 30,
    ) -> None:
        self._llm_chat_fn = llm_chat_fn
        self.min_session_count = min_session_count
        self.min_interval_hours = min_interval_hours
        self.stale_days = stale_days
        self._last_dream_timestamp: float = 0.0

    @staticmethod
    def _tscale() -> float:
        return 1.0

    def should_run_dream(
        self,
        *,
        session_count: int = 0,
        last_dream_ts: float = 0.0,
    ) -> bool:
        if session_count < self.min_session_count:
            return False
        interval_hours = (time.time() - (last_dream_ts or self._last_dream_timestamp)) / 3600
        if interval_hours < self.min_interval_hours:
            return False
        return True

    def run_dream(self, workspace_root: Path) -> DreamResult:
        start_time = time.time()
        storydex_root = workspace_root / ".storydex"

        try:
            orient_result = self._orient(storydex_root)
            gather_result = self._gather(storydex_root, orient_result)
            consolidate_result = self._consolidate(storydex_root, gather_result)
            prune_result = self._prune(storydex_root, consolidate_result)

            duration_ms = int((time.time() - start_time) * 1000)
            self._last_dream_timestamp = time.time()
            self._write_dream_timestamp(storydex_root)

            return DreamResult(
                phase="complete",
                items_processed=orient_result.get("_total_items", 0),
                items_merged=consolidate_result.get("_merged", 0),
                items_pruned=prune_result.get("_pruned", 0),
                success=True,
                duration_ms=duration_ms,
            )
        except Exception as exc:
            duration_ms = int((time.time() - start_time) * 1000)
            return DreamResult(
                phase="error",
                success=False,
                error=str(exc),
                duration_ms=duration_ms,
            )

    def _orient(self, storydex_root: Path) -> Dict[str, Any]:
        current_dir = storydex_root / "memory" / "current"
        panorama: Dict[str, Any] = {"_total_items": 0}

        for state_file in ["story_state.json", "character_state.json", "thread_state.json"]:
            path = current_dir / state_file
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        panorama[state_file] = data
                        panorama["_total_items"] += sum(
                            len(v) for v in data.values() if isinstance(v, (list, dict))
                        )
                except Exception:
                    pass

        summaries_dir = storydex_root / "memory" / "summaries" / "rolling"
        if summaries_dir.exists():
            summaries = list(summaries_dir.glob("*.md"))
            panorama["_rolling_summary_count"] = len(summaries)

        archives_dir = storydex_root / "archives"
        if archives_dir.exists():
            archives = list(archives_dir.glob("arc-*.json"))
            panorama["_archive_count"] = len(archives)

        return panorama

    def _gather(self, storydex_root: Path, orient_result: Dict[str, Any]) -> Dict[str, Any]:
        gather_result = dict(orient_result)
        gather_result["_stale_items"] = []

        story_state = orient_result.get("story_state.json", {})
        if isinstance(story_state, dict):
            for conflict in story_state.get("active_conflicts", []):
                if isinstance(conflict, dict) and conflict.get("status") in ("resolved",):
                    gather_result["_stale_items"].append(
                        {"type": "conflict", "id": conflict.get("id", ""), "reason": "resolved"}
                    )
            for foreshadow in story_state.get("active_suspense", story_state.get("active_foreshadowing", [])):
                if isinstance(foreshadow, dict) and foreshadow.get("status") in ("resolved",):
                    gather_result["_stale_items"].append(
                        {"type": "suspense", "id": foreshadow.get("id", ""), "reason": "resolved"}
                    )

        return gather_result

    def _consolidate(self, storydex_root: Path, gather_result: Dict[str, Any]) -> Dict[str, Any]:
        consolidate_result = dict(gather_result)
        consolidate_result["_merged"] = 0

        if not self._llm_chat_fn:
            return consolidate_result

        story_state = gather_result.get("story_state.json", {})
        if not isinstance(story_state, dict) or not story_state:
            return consolidate_result

        try:
            state_json = json.dumps(story_state, ensure_ascii=False)
            messages = [
                {"role": "system", "content": DREAM_CONSOLIDATE_PROMPT},
                {"role": "user", "content": f"请整合以下记忆库：\n{state_json[:int(6000 * self._tscale())]}"},
            ]
            result = self._llm_chat_fn(
                messages=messages,
                purpose="dream_consolidate",
                max_tokens=3000,
                temperature=0.1,
            )
            content = self._extract_content(result)
            if content:
                parsed = self._parse_json_response(content)
                if isinstance(parsed, dict):
                    original_conflicts = len(story_state.get("active_conflicts", []))
                    original_suspense = len(story_state.get("active_suspense", story_state.get("active_foreshadowing", [])))
                    new_conflicts = len(parsed.get("active_conflicts", []))
                    new_suspense = len(parsed.get("active_suspense", parsed.get("active_foreshadowing", [])))
                    merged = (original_conflicts - new_conflicts) + (original_suspense - new_suspense)
                    consolidate_result["_merged"] = max(0, merged)

                    current_dir = storydex_root / "memory" / "current"
                    story_state_path = current_dir / "story_state.json"
                    if story_state_path.exists():
                        parsed["updatedAt"] = datetime.now(timezone.utc).isoformat()
                        parsed.setdefault("version", story_state.get("version", 2))
                        story_state_path.write_text(
                            json.dumps(parsed, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
        except Exception:
            pass

        return consolidate_result

    def _prune(self, storydex_root: Path, consolidate_result: Dict[str, Any]) -> Dict[str, Any]:
        prune_result = dict(consolidate_result)
        prune_result["_pruned"] = 0

        now = time.time()
        stale_cutoff = now - (self.stale_days * 86400)

        cache_dir = storydex_root / ".cache" / "retrieval"
        if cache_dir.exists():
            for cache_file in cache_dir.glob("bm25_*.json"):
                try:
                    if cache_file.stat().st_mtime < stale_cutoff:
                        cache_file.unlink()
                        prune_result["_pruned"] += 1
                except Exception:
                    pass

        stale_items = consolidate_result.get("_stale_items", [])
        if stale_items:
            current_dir = storydex_root / "memory" / "current"
            story_state_path = current_dir / "story_state.json"
            if story_state_path.exists():
                try:
                    story_state = json.loads(story_state_path.read_text(encoding="utf-8"))
                    if isinstance(story_state, dict):
                        stale_ids = {item.get("id") for item in stale_items if item.get("id")}
                        conflicts = story_state.get("active_conflicts", [])
                        if isinstance(conflicts, list):
                            original_len = len(conflicts)
                            story_state["active_conflicts"] = [
                                c for c in conflicts
                                if not isinstance(c, dict) or c.get("id") not in stale_ids
                            ]
                            prune_result["_pruned"] += original_len - len(story_state["active_conflicts"])

                        suspense = story_state.get("active_suspense", story_state.get("active_foreshadowing", []))
                        if isinstance(suspense, list):
                            original_len = len(suspense)
                            story_state["active_suspense"] = [
                                f for f in suspense
                                if not isinstance(f, dict) or f.get("id") not in stale_ids
                            ]
                            prune_result["_pruned"] += original_len - len(story_state["active_suspense"])

                        story_state["updatedAt"] = datetime.now(timezone.utc).isoformat()
                        story_state_path.write_text(
                            json.dumps(story_state, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                except Exception:
                    pass

        return prune_result

    def _write_dream_timestamp(self, storydex_root: Path) -> None:
        dream_meta_path = storydex_root / "memory" / ".dream_meta.json"
        dream_meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta = {"last_dream_at": datetime.now(timezone.utc).isoformat(), "last_dream_ts": time.time()}
        dream_meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

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
