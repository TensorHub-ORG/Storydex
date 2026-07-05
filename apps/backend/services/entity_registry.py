from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


@dataclass(frozen=True)
class EntityRecord:
    canonical_name: str
    aliases: Tuple[str, ...] = ()
    kind: str = ""
    status: str = "active"

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> Optional["EntityRecord"]:
        canonical = _clean_text(payload.get("canonical_name") or payload.get("canonicalName") or payload.get("name"))
        if not canonical:
            return None
        aliases_payload = payload.get("aliases") if isinstance(payload.get("aliases"), list) else []
        aliases = tuple(
            dict.fromkeys(
                alias
                for alias in (_clean_text(item) for item in aliases_payload)
                if alias and alias != canonical
            )
        )
        return cls(
            canonical_name=canonical,
            aliases=aliases,
            kind=_clean_text(payload.get("kind")),
            status=_clean_text(payload.get("status")) or "active",
        )

    def names(self) -> Tuple[str, ...]:
        return (self.canonical_name, *self.aliases)


class EntityRegistry:
    """Read-only canonical entity/alias lookup for `.storydex/memory/current/entities.json`."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.entities_path = self.workspace_root / ".storydex" / "memory" / "current" / "entities.json"

    def load_records(self) -> List[EntityRecord]:
        if not self.entities_path.exists():
            return []
        try:
            payload = json.loads(self.entities_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(payload, dict) or not isinstance(payload.get("entities"), list):
            return []
        records = [EntityRecord.from_payload(item) for item in payload.get("entities", []) if isinstance(item, dict)]
        return [record for record in records if record is not None and record.status != "archived"]

    def canonicalize_many(self, names: Sequence[str]) -> Tuple[str, ...]:
        alias_map = self._alias_map()
        resolved: List[str] = []
        for name in names:
            cleaned = _clean_text(name)
            if not cleaned:
                continue
            resolved.append(alias_map.get(cleaned, cleaned))
        return tuple(dict.fromkeys(resolved))

    def resolve_mentions(self, text: str, *, fallback_names: Sequence[str] = ()) -> Tuple[str, ...]:
        haystack = str(text or "")
        if not haystack.strip():
            return ()

        candidates: List[Tuple[str, str]] = []
        for record in self.load_records():
            for name in record.names():
                candidates.append((name, record.canonical_name))
        fallback_set = {_clean_text(item) for item in fallback_names if _clean_text(item)}
        known_canonicals = {canonical for _name, canonical in candidates}
        for name in fallback_set:
            if name not in known_canonicals:
                candidates.append((name, name))

        hits: List[Tuple[int, int, str]] = []
        for mention, canonical in candidates:
            index = haystack.find(mention)
            if index >= 0:
                hits.append((index, -len(mention), canonical))
        hits.sort()
        return tuple(dict.fromkeys(canonical for _index, _length, canonical in hits))

    def _alias_map(self) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for record in self.load_records():
            mapping[record.canonical_name] = record.canonical_name
            for alias in record.aliases:
                mapping[alias] = record.canonical_name
        return mapping
