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
    entity_id: str = ""
    aliases: Tuple[str, ...] = ()
    kind: str = ""
    status: str = "active"
    source_paths: Tuple[str, ...] = ()

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
        source_paths_payload = (
            payload.get("source_paths")
            if isinstance(payload.get("source_paths"), list)
            else payload.get("sourcePaths")
            if isinstance(payload.get("sourcePaths"), list)
            else []
        )
        return cls(
            canonical_name=canonical,
            entity_id=_clean_text(
                payload.get("entity_id")
                or payload.get("entityId")
                or payload.get("stable_id")
                or payload.get("stableId")
                or payload.get("id")
            ),
            aliases=aliases,
            kind=_clean_text(payload.get("kind")),
            status=_clean_text(payload.get("status")) or "active",
            source_paths=tuple(
                dict.fromkeys(
                    path
                    for path in (_clean_text(item).replace("\\", "/") for item in source_paths_payload)
                    if path
                )
            ),
        )

    def names(self) -> Tuple[str, ...]:
        return (self.canonical_name, *self.aliases)


class EntityRegistry:
    """Read-only canonical entity/alias lookup for `.storydex/memory/current/entities.json`."""

    def __init__(self, workspace_root: Path, *, records: Optional[Sequence[EntityRecord]] = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.entities_path = self.workspace_root / ".storydex" / "memory" / "current" / "entities.json"
        # 实例内记忆。registry 是只读视图，且所有调用方都「创建即用完丢弃」，
        # 单次操作内 entities.json 不会被改写，所以不需要失效逻辑。
        # 没有它时，一次 WIKI 重建会把同一个文件反复读盘解析数百次。
        self._records: Optional[List[EntityRecord]] = list(records) if records is not None else None
        self._aliases: Optional[Dict[str, str]] = None
        self._name_candidates: Dict[bool, Dict[str, List[EntityRecord]]] = {}

    def load_records(self) -> List[EntityRecord]:
        if self._records is None:
            self._records = self._read_records()
        # 返回副本，保持「每次调用拿到独立列表」的原有语义。
        return list(self._records)

    def _read_records(self) -> List[EntityRecord]:
        if not self.entities_path.exists():
            return []
        try:
            payload = json.loads(self.entities_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return []
        if not isinstance(payload, dict) or not isinstance(payload.get("entities"), list):
            return []
        records = [EntityRecord.from_payload(item) for item in payload.get("entities", []) if isinstance(item, dict)]
        return [record for record in records if record is not None and record.status != "archived"]

    def canonicalize_many(self, names: Sequence[str]) -> Tuple[str, ...]:
        alias_map = self._alias_map()
        known_names = self._lookup_candidates()
        resolved: List[str] = []
        for name in names:
            cleaned = _clean_text(name)
            if not cleaned:
                continue
            if cleaned in alias_map:
                resolved.append(alias_map[cleaned])
            elif cleaned not in known_names:
                resolved.append(cleaned)
        return tuple(dict.fromkeys(resolved))

    def resolve_mentions(self, text: str, *, fallback_names: Sequence[str] = ()) -> Tuple[str, ...]:
        return tuple(dict.fromkeys(
            record.canonical_name if record is not None else mention
            for mention, record in self._mention_matches(text, fallback_names=fallback_names)
        ))

    def resolve_mention_ids(self, text: str, *, ignore_case: bool = False) -> Tuple[str, ...]:
        """保留实体身份：同名角色的独有别名只算到对应 ID，不回退为同名字符串。"""
        return tuple(dict.fromkeys(
            record.entity_id
            for _mention, record in self._mention_matches(text, ignore_case=ignore_case)
            if record is not None and record.entity_id
        ))

    def resolve_name(self, name: str) -> Optional[EntityRecord]:
        candidates = self._lookup_candidates().get(_clean_text(name), [])
        return candidates[0] if len(candidates) == 1 else None

    def ambiguous_names(self) -> Tuple[str, ...]:
        identities: Dict[str, set[tuple]] = {}
        for record in self.load_records():
            for name in record.names():
                identities.setdefault(name, set()).add(self._record_identity(record))
        return tuple(name for name, matches in identities.items() if len(matches) > 1)

    @staticmethod
    def _record_identity(record: EntityRecord) -> tuple:
        return (record.entity_id, record.kind, record.canonical_name) if record.entity_id else (
            record.kind, record.canonical_name, record.source_paths,
        )

    def _lookup_candidates(self, *, ignore_case: bool = False) -> Dict[str, List[EntityRecord]]:
        if ignore_case not in self._name_candidates:
            canonical: Dict[str, Dict[tuple, EntityRecord]] = {}
            aliases: Dict[str, Dict[tuple, EntityRecord]] = {}
            for record in self.load_records():
                identity = self._record_identity(record)
                name = record.canonical_name.casefold() if ignore_case else record.canonical_name
                canonical.setdefault(name, {})[identity] = record
                for alias in record.aliases:
                    aliases.setdefault(alias.casefold() if ignore_case else alias, {})[identity] = record
            # 正式姓名优先；重名和共享别名保持多候选，禁止按文件顺序覆盖。
            self._name_candidates[ignore_case] = {
                name: list(canonical.get(name, aliases.get(name, {})).values())
                for name in canonical.keys() | aliases.keys()
            }
        return self._name_candidates[ignore_case]

    def _mention_matches(
        self, text: str, *, fallback_names: Sequence[str] = (), ignore_case: bool = False,
    ) -> List[Tuple[str, Optional[EntityRecord]]]:
        haystack = _clean_text(text)
        if ignore_case:
            haystack = haystack.casefold()
        candidates = self._lookup_candidates(ignore_case=ignore_case)
        fallbacks = {_clean_text(name) for name in fallback_names if _clean_text(name)}
        names = set(candidates) | ({name.casefold() for name in fallbacks} if ignore_case else fallbacks)
        if not haystack or not names:
            return []
        patterns = []
        for name in sorted(names, key=lambda value: (-len(value), value)):
            escaped = re.escape(name)
            patterns.append(
                rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
                if re.fullmatch(r"[A-Za-z0-9_ -]+", name) else escaped
            )
        hits: List[Tuple[str, Optional[EntityRecord]]] = []
        for match in re.finditer("|".join(patterns), haystack):
            name = match.group(0)
            matches = candidates.get(name, [])
            record = matches[0] if len(matches) == 1 else None
            if record is not None or name not in candidates:
                hits.append((name, record))
        return hits

    def _alias_map(self) -> Dict[str, str]:
        if self._aliases is None:
            self._aliases = {
                name: matches[0].canonical_name
                for name, matches in self._lookup_candidates().items()
                if len(matches) == 1
            }
        return dict(self._aliases)
