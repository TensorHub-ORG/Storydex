from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from core.bounded_text_io import read_text_preview as read_bounded_text_preview
from services.entity_registry import EntityRegistry


_DIMENSION_PRIORITY = {
    "hostility": 90,
    "rivalry": 88,
    "family": 84,
    "professional": 80,
    "alliance": 76,
    "trust": 72,
    "loyalty": 70,
    "intimacy": 66,
}


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _safe_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _truncate(value: str, *, max_chars: int) -> str:
    text = _clean_text(value)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 16)].rstrip() + "... [truncated]"


@dataclass(frozen=True)
class RelationshipEdge:
    source: str
    target: str
    dimension: str
    current_level: int = 0
    last_updated_in: str = ""
    last_updated_at: str = ""
    latest_delta: str = ""
    latest_magnitude: str = ""
    detail: str = ""
    evidence: str = ""
    history_count: int = 0

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> Optional["RelationshipEdge"]:
        source = _clean_text(payload.get("source"))
        target = _clean_text(payload.get("target"))
        if not source or not target or source == target:
            return None

        history = payload.get("history") if isinstance(payload.get("history"), list) else []
        latest = next((item for item in reversed(history) if isinstance(item, dict)), {})
        return cls(
            source=source,
            target=target,
            dimension=_clean_text(payload.get("dimension")).lower() or "intimacy",
            current_level=max(-10, min(10, _safe_int(payload.get("current_level")))),
            last_updated_in=_clean_text(payload.get("last_updated_in")),
            last_updated_at=_clean_text(payload.get("last_updated_at")),
            latest_delta=_clean_text(latest.get("delta")),
            latest_magnitude=_clean_text(latest.get("magnitude")),
            detail=_clean_text(latest.get("detail") or payload.get("detail")),
            evidence=_clean_text(latest.get("evidence") or payload.get("evidence")),
            history_count=len(history),
        )

    def touches_any(self, entities: Iterable[str]) -> bool:
        entity_set = {str(item) for item in entities if str(item)}
        return self.source in entity_set or self.target in entity_set

    def other_entities(self, entities: Iterable[str]) -> Tuple[str, ...]:
        entity_set = {str(item) for item in entities if str(item)}
        others: List[str] = []
        if self.source in entity_set and self.target not in entity_set:
            others.append(self.target)
        if self.target in entity_set and self.source not in entity_set:
            others.append(self.source)
        return tuple(others)

    def context_line(self, *, evidence_chars: int = 120) -> str:
        last = self.last_updated_in or self.last_updated_at or "unknown"
        parts = [
            f"- {self.source} -> {self.target}",
            self.dimension,
            f"level {self.current_level}",
            f"last={last}",
        ]
        suffix = " | ".join(parts)
        evidence = _truncate(self.evidence or self.detail, max_chars=evidence_chars)
        if evidence:
            suffix += f" | evidence={evidence}"
        return suffix


@dataclass(frozen=True)
class RelationshipNeighborhood:
    active_entities: Tuple[str, ...]
    edges: List[RelationshipEdge]
    depth: int = 1


class RelationshipMemoryStore:
    """Read-only query interface for `.storydex/memory/current/relationship_graph.json`."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.graph_path = self.workspace_root / ".storydex" / "memory" / "current" / "relationship_graph.json"

    def load_graph(self) -> Dict[str, Any]:
        if not self.graph_path.exists():
            return {"version": 1, "edges": []}
        try:
            payload = json.loads(self.graph_path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "edges": []}
        if not isinstance(payload, dict):
            return {"version": 1, "edges": []}
        if not isinstance(payload.get("edges"), list):
            payload["edges"] = []
        return payload

    def neighborhood(
        self,
        active_entities: Sequence[str],
        *,
        depth: int = 1,
        max_edges: int = 12,
        dimensions: Sequence[str] = (),
    ) -> RelationshipNeighborhood:
        registry = EntityRegistry(self.workspace_root)
        active = registry.canonicalize_many(active_entities)
        if not active:
            return RelationshipNeighborhood(active_entities=(), edges=[], depth=max(1, int(depth or 1)))

        allowed_dimensions = {_clean_text(item).lower() for item in dimensions if _clean_text(item)}
        all_edges = [
            edge
            for edge in (
                self._edge_from_payload(item, registry=registry) for item in self.load_graph().get("edges", [])
            )
            if edge is not None and (not allowed_dimensions or edge.dimension in allowed_dimensions)
        ]
        if not all_edges:
            return RelationshipNeighborhood(active_entities=active, edges=[], depth=max(1, int(depth or 1)))

        selected: Dict[Tuple[str, str, str], Tuple[int, RelationshipEdge]] = {}
        frontier = set(active)
        visited_entities = set(active)
        max_depth = max(1, min(2, int(depth or 1)))
        for hop in range(1, max_depth + 1):
            next_frontier: set[str] = set()
            for edge in all_edges:
                if not edge.touches_any(frontier):
                    continue
                key = (edge.source, edge.target, edge.dimension)
                current = selected.get(key)
                if current is None or hop < current[0] or (
                    hop == current[0]
                    and self._edge_score(edge, hop=hop) > self._edge_score(current[1], hop=current[0])
                ):
                    selected[key] = (hop, edge)
                next_frontier.update(item for item in edge.other_entities(frontier) if item not in visited_entities)
            visited_entities.update(next_frontier)
            frontier = next_frontier
            if not frontier:
                break

        ranked = sorted(
            selected.values(),
            key=lambda item: (
                -self._edge_score(item[1], hop=item[0]),
                item[0],
                item[1].source,
                item[1].target,
                item[1].dimension,
            ),
        )
        limit = max(0, int(max_edges or 0))
        edges = [edge for _, edge in (ranked[:limit] if limit else [])]
        return RelationshipNeighborhood(active_entities=active, edges=edges, depth=max_depth)

    def project_context(
        self,
        *,
        prompt: str,
        active_file: str,
        active_entities: Sequence[str],
        max_edges: int = 8,
        max_chars: int = 1200,
    ) -> str:
        active = EntityRegistry(self.workspace_root).canonicalize_many(active_entities)
        if not active:
            active = self._infer_active_entities(prompt=prompt, active_file=active_file)
        if not active:
            return ""

        neighborhood = self.neighborhood(active, depth=1, max_edges=max_edges)
        if not neighborhood.edges:
            return ""

        lines = [
            "[Project Relationship Context]",
            f"active_entities: {', '.join(neighborhood.active_entities)}",
            "Only the listed relationship facts are active chapter material for this turn.",
            "Unlisted relationship facts remain background truth, not chapter material.",
        ]
        lines.extend(edge.context_line() for edge in neighborhood.edges)
        return _truncate("\n".join(lines), max_chars=max_chars)

    def _infer_active_entities(self, *, prompt: str, active_file: str) -> Tuple[str, ...]:
        names = self._entity_names()
        if not names:
            return ()

        text = str(prompt or "")
        active_path = self._safe_workspace_file(active_file)
        if active_path is not None and active_path.exists() and active_path.is_file():
            try:
                text += "\n" + read_bounded_text_preview(active_path, max_chars=4000)
            except Exception:
                pass
        if not text.strip():
            return ()

        return EntityRegistry(self.workspace_root).resolve_mentions(text, fallback_names=names)

    def _entity_names(self) -> Tuple[str, ...]:
        names: List[str] = []
        for item in self.load_graph().get("edges", []):
            if not isinstance(item, dict):
                continue
            for key in ("source", "target"):
                name = _clean_text(item.get(key))
                if name:
                    names.append(name)
        return tuple(dict.fromkeys(names))

    def _edge_from_payload(self, payload: Any, *, registry: EntityRegistry) -> Optional[RelationshipEdge]:
        if not isinstance(payload, dict):
            return None

        normalized = dict(payload)
        source = registry.canonicalize_many([_clean_text(normalized.get("source"))])
        target = registry.canonicalize_many([_clean_text(normalized.get("target"))])
        if source:
            normalized["source"] = source[0]
        if target:
            normalized["target"] = target[0]
        return RelationshipEdge.from_payload(normalized)

    def _safe_workspace_file(self, active_file: str) -> Optional[Path]:
        normalized = str(active_file or "").strip().replace("\\", "/").lstrip("/")
        if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
            return None
        candidate = (self.workspace_root / normalized).resolve()
        if candidate == self.workspace_root or self.workspace_root not in candidate.parents:
            return None
        return candidate

    @staticmethod
    def _edge_score(edge: RelationshipEdge, *, hop: int) -> int:
        recency_score = _safe_int(edge.last_updated_in, fallback=0)
        return (
            max(0, 3 - int(hop)) * 100
            + _DIMENSION_PRIORITY.get(edge.dimension, 40)
            + abs(edge.current_level) * 10
            + min(20, max(0, recency_score))
            + (8 if edge.evidence else 0)
            + min(6, edge.history_count)
        )
