from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from core.bounded_text_io import read_text_preview as read_bounded_text_preview
from services.entity_registry import EntityRegistry


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _truncate(value: str, *, max_chars: int) -> str:
    text = _clean_text(value)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 16)].rstrip() + "... [truncated]"


@dataclass(frozen=True)
class ProjectFact:
    subject: str
    predicate: str
    object: str
    confidence: str = "canon"
    established_in: str = ""
    updated_at: str = ""
    evidence: str = ""

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> Optional["ProjectFact"]:
        subject = _clean_text(payload.get("subject"))
        predicate = _clean_text(payload.get("predicate"))
        obj = _clean_text(payload.get("object"))
        if not subject or not predicate or not obj:
            return None
        return cls(
            subject=subject,
            predicate=predicate,
            object=obj,
            confidence=_clean_text(payload.get("confidence")).lower() or "canon",
            established_in=_clean_text(payload.get("established_in") or payload.get("establishedIn")),
            updated_at=_clean_text(payload.get("updated_at") or payload.get("updatedAt")),
            evidence=_clean_text(payload.get("evidence")),
        )

    def context_line(self, *, evidence_chars: int = 120) -> str:
        parts = [
            f"- {self.subject}",
            self.predicate,
            self.object,
            f"confidence={self.confidence}",
        ]
        source = self.established_in or self.updated_at
        if source:
            parts.append(f"source={source}")
        suffix = " | ".join(parts)
        evidence = _truncate(self.evidence, max_chars=evidence_chars)
        if evidence:
            suffix += f" | evidence={evidence}"
        return suffix


class FactMemoryStore:
    """Read-only query interface for `.storydex/memory/current/facts.json`."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.facts_path = self.workspace_root / ".storydex" / "memory" / "current" / "facts.json"

    def load_facts_payload(self) -> Dict[str, Any]:
        if not self.facts_path.exists():
            return {"version": 1, "facts": []}
        try:
            payload = json.loads(self.facts_path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "facts": []}
        if not isinstance(payload, dict):
            return {"version": 1, "facts": []}
        if not isinstance(payload.get("facts"), list):
            payload["facts"] = []
        return payload

    def relevant_facts(
        self,
        active_entities: Sequence[str],
        *,
        max_facts: int = 8,
        include_tentative: bool = False,
    ) -> List[ProjectFact]:
        registry = EntityRegistry(self.workspace_root)
        active = registry.canonicalize_many(active_entities)
        if not active:
            return []

        active_set = set(active)
        facts = [
            fact
            for fact in (
                self._fact_from_payload(item, registry=registry) for item in self.load_facts_payload().get("facts", [])
            )
            if fact is not None
            and fact.subject in active_set
            and (include_tentative or fact.confidence in {"canon", "confirmed"})
        ]
        facts = self._deduplicate_facts(facts)
        ranked = sorted(
            facts,
            key=lambda fact: (
                -self._fact_score(fact),
                fact.subject,
                fact.predicate,
                fact.object,
            ),
        )
        limit = max(0, int(max_facts or 0))
        return ranked[:limit] if limit else []

    def project_context(
        self,
        *,
        prompt: str,
        active_file: str,
        active_entities: Sequence[str],
        max_facts: int = 8,
        max_chars: int = 1200,
    ) -> str:
        active = EntityRegistry(self.workspace_root).canonicalize_many(active_entities)
        if not active:
            active = self._infer_active_entities(prompt=prompt, active_file=active_file)
        if not active:
            return ""

        facts = self.relevant_facts(active, max_facts=max_facts)
        if not facts:
            return ""

        lines = [
            "[Project Fact Context]",
            f"active_entities: {', '.join(active)}",
            "Only the listed facts are hard constraints for this turn.",
            "Unlisted facts remain background truth, not active chapter material.",
        ]
        lines.extend(fact.context_line() for fact in facts)
        return _truncate("\n".join(lines), max_chars=max_chars)

    def _infer_active_entities(self, *, prompt: str, active_file: str) -> Tuple[str, ...]:
        names = self._subject_names()
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

    def _subject_names(self) -> Tuple[str, ...]:
        names: List[str] = []
        for item in self.load_facts_payload().get("facts", []):
            if not isinstance(item, dict):
                continue
            name = _clean_text(item.get("subject"))
            if name:
                    names.append(name)
        return tuple(dict.fromkeys(names))

    def _fact_from_payload(self, payload: Any, *, registry: EntityRegistry) -> Optional[ProjectFact]:
        if not isinstance(payload, dict):
            return None

        normalized = dict(payload)
        subject = registry.canonicalize_many([_clean_text(normalized.get("subject"))])
        if subject:
            normalized["subject"] = subject[0]
        return ProjectFact.from_payload(normalized)

    @classmethod
    def _deduplicate_facts(cls, facts: Sequence[ProjectFact]) -> List[ProjectFact]:
        selected: Dict[Tuple[str, str, str], ProjectFact] = {}
        for fact in facts:
            key = (fact.subject, fact.predicate, fact.object)
            current = selected.get(key)
            if current is None or cls._fact_score(fact) > cls._fact_score(current):
                selected[key] = fact
        return list(selected.values())

    def _safe_workspace_file(self, active_file: str) -> Optional[Path]:
        normalized = str(active_file or "").strip().replace("\\", "/").lstrip("/")
        if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
            return None
        candidate = (self.workspace_root / normalized).resolve()
        if candidate == self.workspace_root or self.workspace_root not in candidate.parents:
            return None
        return candidate

    @staticmethod
    def _fact_score(fact: ProjectFact) -> int:
        confidence_score = {"canon": 80, "confirmed": 76}.get(fact.confidence, 20)
        evidence_score = 12 if fact.evidence else 0
        source_score = 8 if fact.established_in or fact.updated_at else 0
        return confidence_score + evidence_score + source_score
