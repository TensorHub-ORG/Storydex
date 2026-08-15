from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable


READ_ONLY = "read_only"
SCOPED_WRITE = "scoped_write"
WORKSPACE_WRITE = "workspace_write"
FULL_ACCESS = "full_access"

CAPABILITY_MODES = frozenset({READ_ONLY, SCOPED_WRITE, WORKSPACE_WRITE, FULL_ACCESS})


def _dict_value(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _normalized_roots(values: Iterable[Any]) -> tuple[str, ...]:
    roots: list[str] = []
    for value in values:
        normalized = str(value or "").strip().replace("\\", "/")
        if normalized and normalized not in roots:
            roots.append(normalized)
    return tuple(roots)


def compile_capability_mode(
    *,
    can_write: bool,
    allowed_write_roots: Iterable[Any] = (),
    knowledge_write_mode: str = "",
) -> str:
    """Compile semantic/workflow metadata into one explicit turn capability."""

    knowledge_mode = str(knowledge_write_mode or "").strip().lower()
    roots = _normalized_roots(allowed_write_roots)
    if knowledge_mode == "candidate_extraction" or not bool(can_write):
        return READ_ONLY
    if knowledge_mode == "explicit_binding" or roots:
        return SCOPED_WRITE
    return WORKSPACE_WRITE


@dataclass(frozen=True)
class AgentCapabilityPolicy:
    mode: str
    writes_allowed: bool
    core_writes_allowed: bool
    allowed_write_roots: tuple[str, ...]
    source: str

    def to_execution_dict(self) -> Dict[str, Any]:
        return {
            "capabilityMode": self.mode,
            "writesAllowed": self.writes_allowed,
            "coreWritesAllowed": self.core_writes_allowed,
            "allowedWriteRoots": list(self.allowed_write_roots),
            "capabilitySource": self.source,
        }


def resolve_capability_policy(
    turn_contract: Dict[str, Any] | None,
    *,
    plan_mode: bool = False,
) -> AgentCapabilityPolicy:
    """Resolve a fail-closed runtime policy from a TurnContract.

    New contracts carry ``executionPolicy.capabilityMode``. Legacy contracts
    are compiled from semantic fields so ``canWrite=False`` and
    ``directFileWrites=False`` no longer expose mutating core tools. Scoped
    write without a root is invalid and degrades to read-only.
    """

    contract = _dict_value(turn_contract)
    execution = _dict_value(contract.get("executionPolicy"))
    intent = _dict_value(contract.get("intentFrame"))
    knowledge = _dict_value(contract.get("knowledgeWritePolicy"))
    knowledge_mode = str(knowledge.get("mode") or "").strip().lower()
    raw_roots = execution.get("allowedWriteRoots")
    roots = _normalized_roots(raw_roots if isinstance(raw_roots, list) else ())

    if plan_mode:
        return AgentCapabilityPolicy(READ_ONLY, False, False, (), "plan_mode")
    if knowledge_mode == "candidate_extraction":
        return AgentCapabilityPolicy(READ_ONLY, False, False, (), "candidate_extraction")

    # The compiled capability is authoritative only when it agrees with the
    # semantic contract. A stale or hand-crafted write capability must never
    # override an explicit read-only intent.
    if intent.get("canWrite") is False:
        return AgentCapabilityPolicy(READ_ONLY, False, False, (), "intent_read_only")

    requested_mode = str(execution.get("capabilityMode") or "").strip().lower()
    source = "turn_contract"
    if requested_mode not in CAPABILITY_MODES:
        can_write = bool(intent.get("canWrite"))
        direct_file_writes = execution.get("directFileWrites")
        if (
            isinstance(direct_file_writes, bool)
            and not direct_file_writes
            and knowledge_mode != "explicit_binding"
        ):
            can_write = False
        requested_mode = compile_capability_mode(
            can_write=can_write,
            allowed_write_roots=roots,
            knowledge_write_mode=knowledge_mode,
        )
        source = "legacy_contract_compiler"

    if requested_mode == READ_ONLY:
        return AgentCapabilityPolicy(READ_ONLY, False, False, (), source)

    if requested_mode == SCOPED_WRITE:
        if not roots:
            return AgentCapabilityPolicy(
                READ_ONLY,
                False,
                False,
                (),
                "invalid_scoped_write_without_roots",
            )
        direct_file_writes = execution.get("directFileWrites")
        core_writes_allowed = (
            bool(direct_file_writes)
            if isinstance(direct_file_writes, bool)
            else knowledge_mode != "explicit_binding"
        )
        return AgentCapabilityPolicy(
            SCOPED_WRITE,
            True,
            core_writes_allowed,
            roots,
            source,
        )

    direct_file_writes = execution.get("directFileWrites")
    core_writes_allowed = (
        bool(direct_file_writes) if isinstance(direct_file_writes, bool) else True
    )
    return AgentCapabilityPolicy(
        requested_mode,
        True,
        core_writes_allowed,
        (),
        source,
    )
