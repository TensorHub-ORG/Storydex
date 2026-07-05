"""WP-5.1 · L0–L4 目录迁移 + WP-5.4/5.5 Summary models + 兼容 reader（07 §5.6）。"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from core.feature_flags import get_flags


# ─────────────────── Summary models（WP-5.4 / 5.5）───────────────────


class RollingSummary(BaseModel):
    summary_type: str = "rolling"
    scope: Dict[str, Any] = Field(default_factory=dict)
    body: str
    new_foreshadowing: List[str] = Field(default_factory=list)
    new_unresolved: List[str] = Field(default_factory=list)
    superseded_summaries: List[str] = Field(default_factory=list)
    generated_at: str
    confidence: float = 1.0


class CanonicalSummary(BaseModel):
    summary_type: str = "canonical"
    scope: Dict[str, Any] = Field(default_factory=dict)
    body: str
    superseded_summaries: List[str] = Field(default_factory=list)
    generated_at: str
    confidence: float = 1.0
    source_summaries: List[str] = Field(default_factory=list)


# ─────────────────── 迁移 ───────────────────


def migrate_workspace(workspace_root: Path, *, dry_run: bool = False) -> Dict[str, Any]:
    """v1 → v2 layout；幂等，旧文件保留。返回 {migrated, skipped}。"""
    workspace_root = Path(workspace_root).resolve()
    storydex = workspace_root / ".storydex"
    report: Dict[str, List[str]] = {"migrated": [], "skipped": []}

    def _move(src: Path, dst: Path) -> None:
        if not src.exists():
            return
        if dst.exists():
            report["skipped"].append(str(src.relative_to(workspace_root)))
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dry_run:
            report["migrated"].append(f"{src.relative_to(workspace_root)} -> {dst.relative_to(workspace_root)}")
            return
        shutil.copy2(src, dst)
        report["migrated"].append(f"{src.relative_to(workspace_root)} -> {dst.relative_to(workspace_root)}")

    old_chapters = storydex / "memory" / "chapters"
    if old_chapters.exists():
        for snap in old_chapters.rglob("*.variables.json"):
            rel = snap.relative_to(old_chapters)
            _move(snap, storydex / "memory" / "raw" / "snapshots" / rel)

    _move(
        storydex / "memory" / "current-state" / "全部变量.json",
        storydex / "memory" / "current" / "story_state.json",
    )

    old_concision = storydex / "memory" / "concision"
    if old_concision.exists():
        for md in old_concision.rglob("*.md"):
            _move(md, storydex / "memory" / "summaries" / "rolling" / md.name)

    chars_dir = storydex / "characters"
    if chars_dir.exists():
        for cf in chars_dir.glob("*.json"):
            try:
                data = json.loads(cf.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            from services.character_models import split_legacy_character
            split = split_legacy_character(data)
            cid = str(data.get("id") or cf.stem)
            card_dst = chars_dir / "cards" / f"{cid}.json"
            state_dst = chars_dir / "states" / f"{cid}.json"
            if not dry_run:
                card_dst.parent.mkdir(parents=True, exist_ok=True)
                state_dst.parent.mkdir(parents=True, exist_ok=True)
                if not card_dst.exists():
                    card_dst.write_text(json.dumps(split["card"], ensure_ascii=False, indent=2), encoding="utf-8")
                    report["migrated"].append(f"{cf.name} -> cards/{cid}.json")
                if not state_dst.exists():
                    state_dst.write_text(json.dumps(split["state"], ensure_ascii=False, indent=2), encoding="utf-8")
                    report["migrated"].append(f"{cf.name} -> states/{cid}.json")
            else:
                report["migrated"].append(f"{cf.name} -> cards/states (dry-run)")

    if not dry_run:
        sv_path = storydex / "config" / "schema-versions.json"
        sv_path.parent.mkdir(parents=True, exist_ok=True)
        payload: Dict[str, Any] = {}
        try:
            if sv_path.exists():
                payload = json.loads(sv_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    payload = {}
        except Exception:
            payload = {}
        payload["memory_layer"] = "v2"
        payload["migrated_at"] = datetime.now(timezone.utc).isoformat()
        sv_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return report


# ─────────────────── 兼容 reader（双向）───────────────────


class MemoryReader:
    """v2 优先，MEMORY_LAYER_V2 Off 或 v2 缺失时 fallback v1。"""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()

    def read_story_state(self) -> Dict[str, Any]:
        v2 = self.workspace_root / ".storydex" / "memory" / "current" / "story_state.json"
        v1 = self.workspace_root / ".storydex" / "memory" / "current-state" / "全部变量.json"
        prefer = get_flags().get_bool("MEMORY_LAYER_V2")
        order = (v2, v1) if prefer else (v1, v2)
        for p in order:
            if p.exists():
                try:
                    payload = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(payload, dict) and payload:
                        return payload
                except Exception:
                    continue
        return {}

    def list_rolling_summaries(self) -> List[Path]:
        v2 = self.workspace_root / ".storydex" / "memory" / "summaries" / "rolling"
        v1 = self.workspace_root / ".storydex" / "memory" / "concision"
        prefer = get_flags().get_bool("MEMORY_LAYER_V2")
        roots = (v2, v1) if prefer else (v1, v2)
        for r in roots:
            if r.exists():
                files = sorted(r.glob("*.md"))
                if files:
                    return files
        return []

    def read_character_card(self, character_id: str) -> Optional[Dict[str, Any]]:
        v2 = self.workspace_root / ".storydex" / "characters" / "cards" / f"{character_id}.json"
        v1 = self.workspace_root / ".storydex" / "characters" / f"{character_id}.json"
        if get_flags().get_bool("MEMORY_LAYER_V2") and v2.exists():
            try:
                return json.loads(v2.read_text(encoding="utf-8"))
            except Exception:
                pass
        if v1.exists():
            try:
                legacy = json.loads(v1.read_text(encoding="utf-8"))
                if isinstance(legacy, dict):
                    from services.character_models import split_legacy_character
                    return split_legacy_character(legacy)["card"]
            except Exception:
                pass
        return None


_ENCODING_SELFTEST = "MemoryReader / migrate 编码自检"
assert "�" not in _ENCODING_SELFTEST
