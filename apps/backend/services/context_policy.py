from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Dict


@dataclass(frozen=True)
class ContextPolicy:
    """Immutable source policy for one Storydex execution.

    Product defaults deliberately preserve the pre-T2 runtime. Evaluation code
    may pass a replacement value for one execution without touching persisted
    user configuration.
    """

    base_story_context: bool = True
    story_structured_memory: bool = True
    passive_fts: bool = True
    wiki_context: bool = True
    coomi_memory: bool = True
    active_retrieval_tools: bool = True

    @classmethod
    def from_agent_settings(cls, value: Dict[str, Any] | None) -> "ContextPolicy":
        settings = value if isinstance(value, dict) else {}
        return cls(
            coomi_memory=_strict_bool(settings.get("coomiMemoryEnabled"), True),
            wiki_context=_strict_bool(settings.get("wikiContextEnabled"), True),
        )

    @classmethod
    def from_dict(cls, value: Dict[str, Any] | None) -> "ContextPolicy":
        payload = value if isinstance(value, dict) else {}
        defaults = cls()
        return cls(
            base_story_context=_strict_bool(payload.get("base_story_context"), defaults.base_story_context),
            story_structured_memory=_strict_bool(
                payload.get("story_structured_memory"),
                defaults.story_structured_memory,
            ),
            passive_fts=_strict_bool(payload.get("passive_fts"), defaults.passive_fts),
            wiki_context=_strict_bool(payload.get("wiki_context"), defaults.wiki_context),
            coomi_memory=_strict_bool(payload.get("coomi_memory"), defaults.coomi_memory),
            active_retrieval_tools=_strict_bool(
                payload.get("active_retrieval_tools"),
                defaults.active_retrieval_tools,
            ),
        )

    def with_overrides(self, **values: bool) -> "ContextPolicy":
        unknown = set(values) - set(self.to_dict())
        if unknown:
            raise ValueError(f"Unknown ContextPolicy field(s): {sorted(unknown)}")
        if any(not isinstance(value, bool) for value in values.values()):
            raise TypeError("ContextPolicy overrides must be booleans")
        return replace(self, **values)

    def to_dict(self) -> Dict[str, bool]:
        return {
            "base_story_context": self.base_story_context,
            "story_structured_memory": self.story_structured_memory,
            "passive_fts": self.passive_fts,
            "wiki_context": self.wiki_context,
            "coomi_memory": self.coomi_memory,
            "active_retrieval_tools": self.active_retrieval_tools,
        }

    @property
    def fingerprint(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def context_policy_from_turn_contract(value: Dict[str, Any] | None) -> ContextPolicy:
    contract = value if isinstance(value, dict) else {}
    policy = contract.get("contextPolicy") if isinstance(contract.get("contextPolicy"), dict) else {}
    sources = policy.get("sources") if isinstance(policy.get("sources"), dict) else {}
    return ContextPolicy.from_dict(sources)


def _strict_bool(value: Any, fallback: bool) -> bool:
    return value if isinstance(value, bool) else fallback
