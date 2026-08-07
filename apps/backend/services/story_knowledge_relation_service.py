from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from uuid import uuid4


ENTITY_SCHEMA_VERSION = 2
FACT_SCHEMA_VERSION = 2
REVIEW_SCHEMA_VERSION = 1
KNOWLEDGE_PLAN_SCHEMA_VERSION = 1
KNOWLEDGE_PLAN_TTL_HOURS = 24

REVIEW_STATUSES = {"confirmed", "review_required", "rejected", "superseded"}
KNOWLEDGE_STATUSES = {"planned", "observed", "inferred"}
REJECT_REASONS = {"incorrect", "ambiguous", "duplicate", "not_canon", "other"}

ENTITY_SOURCE_PATH = Path(".storydex/memory/current/entities.json")
FACT_SOURCE_PATH = Path(".storydex/memory/current/facts.json")
REVIEW_SOURCE_PATH = Path(".storydex/memory/review/relations.json")
PLAN_ROOT_PATH = Path(".storydex/.agent/runtime/knowledge-write-plans")

FORMAL_RELATION_PREFIXES = (
    ".storydex/worldbook/",
    ".storydex/scripts/",
    ".storydex/characters/",
)
FORMAL_RELATION_HEADING = "关联对象"

_WIKILINK_RE = re.compile(r"\[\[\s*(?P<target>[^\]|\n]+?)(?:\|(?P<label>[^\]\n]+?))?\s*\]\]")
_RELATION_LINE_RE = re.compile(
    r"^\s*[-*+]\s*(?P<predicate>[^:：\n]{1,100}?)\s*[:：]\s*(?P<targets>.+?)\s*$"
)
_HEADING_RE = re.compile(r"^\s*(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<body>[\s\S]*?)\n---\s*(?:\n|\Z)")
_NEGATION_RE = re.compile(
    r"(?:并不|不是|并非|没有|不存在|从未|未曾|不属于|不位于|不生活|不栖息|否认|never|not\s+|does\s+not)",
    re.IGNORECASE,
)
_HYPOTHETICAL_RE = re.compile(
    r"(?:如果|假如|若是|倘若|可能会|也许会|或许会|设想|假设|if\s+|would\s+|could\s+)",
    re.IGNORECASE,
)
_RUMOR_RE = re.compile(r"(?:传闻|据说|听说|相传|有人说|rumou?r|reportedly|allegedly)", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalize_relative_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/").lstrip("/")
    if not raw:
        return ""
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _stable_hash(*values: Any, length: int = 24) -> str:
    encoded = "\x1f".join(str(value or "") for value in values).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def _parse_iso(value: Any) -> Optional[datetime]:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class KnowledgeRelationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "knowledge_relation_error",
        status_code: int = 400,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = dict(details or {})


@dataclass(frozen=True)
class ResolvedEntity:
    entity_id: str
    canonical_name: str
    aliases: tuple[str, ...]
    kind: str
    status: str
    source_paths: tuple[str, ...]
    created_at: str
    updated_at: str

    def to_payload(self) -> Dict[str, Any]:
        return {
            "entityId": self.entity_id,
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "kind": self.kind,
            "status": self.status,
            "sourcePaths": list(self.source_paths),
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


@dataclass(frozen=True)
class MarkdownRelation:
    predicate: str
    target: str
    display_target: str
    quote: str
    line_start: int
    line_end: int


class StoryKnowledgeRelationService:
    """Authoritative relation domain service.

    Explicit user bindings are prepared and then committed deterministically.
    Model-extracted relations are stored in a review ledger and cannot become
    confirmed facts until the user confirms one candidate through this service.
    """

    def __init__(self) -> None:
        self._lock = RLock()

    # ------------------------------------------------------------------
    # Paths and bounded workspace access
    # ------------------------------------------------------------------
    @staticmethod
    def entities_path(workspace_root: Path) -> Path:
        return Path(workspace_root).resolve() / ENTITY_SOURCE_PATH

    @staticmethod
    def facts_path(workspace_root: Path) -> Path:
        return Path(workspace_root).resolve() / FACT_SOURCE_PATH

    @staticmethod
    def review_path(workspace_root: Path) -> Path:
        return Path(workspace_root).resolve() / REVIEW_SOURCE_PATH

    @staticmethod
    def plans_root(workspace_root: Path) -> Path:
        return Path(workspace_root).resolve() / PLAN_ROOT_PATH

    def _workspace_file(self, workspace_root: Path, relative_path: Any) -> Path:
        root = Path(workspace_root).resolve()
        normalized = _normalize_relative_path(relative_path)
        if not normalized:
            raise KnowledgeRelationError(
                "项目相对路径无效。",
                code="knowledge_path_invalid",
                details={"path": str(relative_path or "")},
            )
        candidate = (root / normalized).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise KnowledgeRelationError(
                "目标路径越出项目工作区。",
                code="knowledge_path_outside_workspace",
                details={"path": normalized},
            ) from exc
        return candidate

    @staticmethod
    def _is_formal_relation_path(relative_path: str) -> bool:
        normalized = _normalize_relative_path(relative_path)
        return bool(
            normalized.lower().endswith(".md")
            and any(normalized.startswith(prefix) for prefix in FORMAL_RELATION_PREFIXES)
        )

    @staticmethod
    def is_relation_sidecar_path(relative_path: Any) -> bool:
        return _normalize_relative_path(relative_path).lower().endswith(".relations.md")

    # ------------------------------------------------------------------
    # Entity registry v2 and resolution
    # ------------------------------------------------------------------
    def load_entities(self, workspace_root: Path) -> Dict[str, Any]:
        path = self.entities_path(workspace_root)
        payload = self._read_json(path, default={})
        raw_entities = payload.get("entities") if isinstance(payload.get("entities"), list) else []
        now = _now_iso()
        normalized: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(raw_entities):
            if not isinstance(raw, dict):
                continue
            canonical = _clean_text(raw.get("canonical_name") or raw.get("canonicalName") or raw.get("name"))
            if not canonical:
                continue
            aliases = self._unique_texts(raw.get("aliases"))
            source_paths = self._unique_paths(raw.get("sourcePaths") or raw.get("source_paths"))
            kind = _clean_text(raw.get("kind")) or self._kind_from_paths(source_paths) or "setting"
            entity_id = _clean_text(
                raw.get("entityId")
                or raw.get("entity_id")
                or raw.get("stableId")
                or raw.get("stable_id")
                or raw.get("id")
            )
            if not entity_id:
                entity_id = self._new_entity_id(kind=kind, canonical_name=canonical, source_paths=source_paths)
            if entity_id in seen_ids:
                entity_id = f"{entity_id}-{_stable_hash(index, canonical, source_paths, length=8)}"
            seen_ids.add(entity_id)
            normalized.append(
                ResolvedEntity(
                    entity_id=entity_id,
                    canonical_name=canonical,
                    aliases=tuple(alias for alias in aliases if alias != canonical),
                    kind=kind,
                    status=_clean_text(raw.get("status")) or "active",
                    source_paths=tuple(source_paths),
                    created_at=str(raw.get("createdAt") or raw.get("created_at") or now),
                    updated_at=str(raw.get("updatedAt") or raw.get("updated_at") or now),
                ).to_payload()
            )
        return {
            **{key: value for key, value in payload.items() if key != "entities"},
            "version": ENTITY_SCHEMA_VERSION,
            "schemaVersion": ENTITY_SCHEMA_VERSION,
            "entities": normalized,
            "updatedAt": str(payload.get("updatedAt") or now),
        }

    def resolve_entity(
        self,
        workspace_root: Path,
        reference: Any = None,
        *,
        entity_id: Any = "",
        source_path: Any = "",
        allow_discovery: bool = True,
    ) -> Dict[str, Any]:
        payload = self.load_entities(workspace_root)
        entities = [dict(item) for item in payload["entities"] if isinstance(item, dict)]
        requested_id = _clean_text(entity_id)
        requested_path = _normalize_relative_path(source_path)
        requested_name = ""
        if isinstance(reference, dict):
            requested_id = requested_id or _clean_text(
                reference.get("entityId") or reference.get("entity_id") or reference.get("id")
            )
            requested_path = requested_path or _normalize_relative_path(
                reference.get("sourcePath") or reference.get("source_path") or reference.get("path")
            )
            requested_name = _clean_text(
                reference.get("canonical_name")
                or reference.get("canonicalName")
                or reference.get("name")
                or reference.get("label")
                or reference.get("value")
            )
        else:
            requested_name = _clean_text(reference)

        # Models sometimes copy a human-readable endpoint name into the
        # optional ``*Id`` field.  Keep real IDs strict, but treat that exact
        # redundant spelling as an omitted ID so the accompanying source path
        # or canonical name can still resolve the entity deterministically.
        # An arbitrary/non-matching ID continues to fail closed.
        supplied_entity_id = requested_id
        if requested_id:
            matches = [item for item in entities if _clean_text(item.get("entityId")) == requested_id]
            if matches:
                return self._unique_entity_match(matches, requested_id, "entityId")
            human_references = {requested_name}
            if requested_path:
                human_references.add(Path(requested_path).stem.replace(".relations", ""))
            if requested_id not in {value for value in human_references if value}:
                return self._unique_entity_match([], requested_id, "entityId")
            requested_id = ""

        if requested_path:
            matches = [
                item
                for item in entities
                if requested_path in self._unique_paths(item.get("sourcePaths") or item.get("source_paths"))
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise self._ambiguous_entity_error(requested_path, matches, "sourcePath")

        if requested_name:
            canonical_matches = [
                item
                for item in entities
                if _clean_text(item.get("canonical_name") or item.get("canonicalName") or item.get("name"))
                == requested_name
            ]
            if len(canonical_matches) == 1:
                return canonical_matches[0]
            if len(canonical_matches) > 1:
                raise self._ambiguous_entity_error(requested_name, canonical_matches, "canonical_name")

            alias_matches = [
                item for item in entities if requested_name in self._unique_texts(item.get("aliases"))
            ]
            if len(alias_matches) == 1:
                return alias_matches[0]
            if len(alias_matches) > 1:
                raise self._ambiguous_entity_error(requested_name, alias_matches, "alias")

        if allow_discovery:
            discovered = self._discover_entity(
                workspace_root,
                name=requested_name,
                source_path=requested_path,
            )
            if discovered is not None:
                return discovered

        raise KnowledgeRelationError(
            f"无法唯一解析实体：{requested_id or requested_path or requested_name or '<empty>'}",
            code="knowledge_entity_not_found",
            status_code=404,
            details={
                "entityId": supplied_entity_id,
                "sourcePath": requested_path,
                "name": requested_name,
            },
        )

    def ensure_entity(
        self,
        workspace_root: Path,
        reference: Any,
        *,
        source_path: Any = "",
        kind: Any = "",
        entity_id: Any = "",
        _allow_discovery: bool = True,
    ) -> Dict[str, Any]:
        try:
            return self.resolve_entity(
                workspace_root,
                reference,
                entity_id=entity_id,
                source_path=source_path,
                # Discovery delegates back to ensure_entity once it finds a
                # unique source file.  Disable that second discovery pass so
                # the first materialization terminates instead of recursing.
                allow_discovery=_allow_discovery,
            )
        except KnowledgeRelationError as exc:
            if exc.code not in {"knowledge_entity_not_found"}:
                raise
        canonical = _clean_text(
            reference.get("canonical_name")
            or reference.get("canonicalName")
            or reference.get("name")
            or reference.get("label")
            if isinstance(reference, dict)
            else reference
        )
        relative_path = _normalize_relative_path(source_path)
        if not relative_path and isinstance(reference, dict):
            relative_path = _normalize_relative_path(
                reference.get("sourcePath")
                or reference.get("source_path")
                or reference.get("path")
            )
        if not canonical and relative_path:
            canonical = self._title_from_source(self._workspace_file(workspace_root, relative_path))
        if not canonical:
            raise KnowledgeRelationError(
                "新实体缺少可用名称。",
                code="knowledge_entity_name_required",
            )
        effective_kind = _clean_text(kind) or self._kind_from_paths([relative_path]) or "setting"
        now = _now_iso()
        record = ResolvedEntity(
            entity_id=_clean_text(entity_id)
            or self._new_entity_id(
                kind=effective_kind,
                canonical_name=canonical,
                source_paths=[relative_path] if relative_path else [],
            ),
            canonical_name=canonical,
            aliases=(),
            kind=effective_kind,
            status="active",
            source_paths=(relative_path,) if relative_path else (),
            created_at=now,
            updated_at=now,
        ).to_payload()
        payload = self.load_entities(workspace_root)
        payload["entities"].append(record)
        payload["updatedAt"] = now
        self._atomic_write_text(self.entities_path(workspace_root), _json_text(payload))
        return record

    def _discover_entity(
        self,
        workspace_root: Path,
        *,
        name: str,
        source_path: str,
    ) -> Optional[Dict[str, Any]]:
        root = Path(workspace_root).resolve()
        candidates: List[tuple[str, str, str]] = []
        if source_path:
            path = self._workspace_file(root, source_path)
            if path.is_file() and self._is_entity_source_path(source_path):
                candidates.append((source_path, self._title_from_source(path), self._kind_from_paths([source_path])))
        if name:
            for prefix in (".storydex/worldbook", ".storydex/scripts", ".storydex/characters"):
                base = root / prefix
                if not base.is_dir():
                    continue
                for path in base.rglob("*"):
                    if not path.is_file() or path.suffix.lower() not in {".md", ".json", ".txt"}:
                        continue
                    relative = path.relative_to(root).as_posix()
                    if self.is_relation_sidecar_path(relative):
                        continue
                    title = self._title_from_source(path)
                    if title == name or path.stem == name:
                        candidates.append((relative, title or path.stem, self._kind_from_paths([relative])))
        unique = list(dict.fromkeys(candidates))
        if not unique:
            return None
        if len(unique) > 1:
            raise KnowledgeRelationError(
                f"实体“{name or source_path}”对应多个项目文件，不能猜测合并。",
                code="knowledge_entity_ambiguous",
                status_code=409,
                details={"candidates": [item[0] for item in unique]},
            )
        relative, title, kind = unique[0]
        return self.ensure_entity(
            workspace_root,
            title or name,
            source_path=relative,
            kind=kind,
            _allow_discovery=False,
        )

    # ------------------------------------------------------------------
    # Markdown relation parser and deterministic writer
    # ------------------------------------------------------------------
    def parse_markdown_relations(self, content: str) -> List[Dict[str, Any]]:
        lines = str(content or "").splitlines()
        in_section = False
        relations: List[Dict[str, Any]] = []
        for index, raw_line in enumerate(lines, start=1):
            heading = _HEADING_RE.match(raw_line)
            if heading:
                level = len(heading.group("marks"))
                title = re.sub(r"[*_`]", "", heading.group("title")).strip()
                if level == 2 and title == FORMAL_RELATION_HEADING:
                    in_section = True
                    continue
                if in_section and level <= 2:
                    break
                continue
            if not in_section:
                continue
            line_match = _RELATION_LINE_RE.match(raw_line)
            if not line_match:
                continue
            predicate = _clean_text(line_match.group("predicate"))
            targets = list(_WIKILINK_RE.finditer(line_match.group("targets")))
            for target_match in targets:
                target = _clean_text(target_match.group("target"))
                display = _clean_text(target_match.group("label")) or target
                if not predicate or not target:
                    continue
                relations.append(
                    MarkdownRelation(
                        predicate=predicate,
                        target=target,
                        display_target=display,
                        quote=raw_line.strip(),
                        line_start=index,
                        line_end=index,
                    ).__dict__
                )
        return relations

    def parse_markdown_file(self, workspace_root: Path, relative_path: Any) -> List[Dict[str, Any]]:
        normalized = _normalize_relative_path(relative_path)
        path = self._workspace_file(workspace_root, normalized)
        if not path.is_file():
            return []
        return self.parse_markdown_relations(path.read_text(encoding="utf-8-sig", errors="strict"))

    def render_markdown_relation(
        self,
        content: str,
        *,
        predicate: str,
        target: str,
        display_target: str = "",
        entity_id: str = "",
    ) -> tuple[str, str, bool]:
        normalized_predicate = _clean_text(predicate)
        normalized_target = _clean_text(target)
        if not normalized_predicate or not normalized_target:
            raise KnowledgeRelationError(
                "关系谓词和目标实体不能为空。",
                code="knowledge_relation_fields_required",
            )
        link = f"[[{normalized_target}|{_clean_text(display_target)}]]" if _clean_text(display_target) and _clean_text(display_target) != normalized_target else f"[[{normalized_target}]]"
        relation_line = f"- {normalized_predicate}：{link}"
        existing = self.parse_markdown_relations(content)
        if any(
            _clean_text(item.get("predicate")) == normalized_predicate
            and _clean_text(item.get("target")) == normalized_target
            for item in existing
        ):
            return str(content or ""), relation_line, False

        original = str(content or "")
        prefix = ""
        if entity_id and not _FRONTMATTER_RE.match(original):
            prefix = f"---\nentityId: {entity_id}\n---\n\n"
        lines = original.splitlines()
        heading_index = -1
        section_end = len(lines)
        for index, raw_line in enumerate(lines):
            match = _HEADING_RE.match(raw_line)
            if not match:
                continue
            level = len(match.group("marks"))
            title = re.sub(r"[*_`]", "", match.group("title")).strip()
            if heading_index < 0 and level == 2 and title == FORMAL_RELATION_HEADING:
                heading_index = index
                continue
            if heading_index >= 0 and level <= 2:
                section_end = index
                break

        if heading_index < 0:
            base = original.rstrip()
            separator = "\n\n" if base else ""
            next_content = f"{prefix}{base}{separator}## {FORMAL_RELATION_HEADING}\n\n{relation_line}\n"
            return next_content, relation_line, True

        insert_index = section_end
        while insert_index > heading_index + 1 and not lines[insert_index - 1].strip():
            insert_index -= 1
        lines.insert(insert_index, relation_line)
        next_content = "\n".join(lines).rstrip() + "\n"
        if prefix:
            next_content = prefix + next_content
        return next_content, relation_line, True

    # ------------------------------------------------------------------
    # Canonical relation DTO, facts v2 and review ledger
    # ------------------------------------------------------------------
    def relation_dto(
        self,
        *,
        subject: Mapping[str, Any],
        predicate: Any,
        obj: Mapping[str, Any],
        review_status: str,
        knowledge_status: str,
        source_refs: Sequence[Mapping[str, Any]],
        provenance: Optional[Mapping[str, Any]] = None,
        trace_id: Any = "",
        confidence: Any = "confirmed",
        relation_id: Any = "",
        created_at: Any = "",
        updated_at: Any = "",
    ) -> Dict[str, Any]:
        subject_id = _clean_text(subject.get("entityId") or subject.get("entity_id") or subject.get("id"))
        object_id = _clean_text(obj.get("entityId") or obj.get("entity_id") or obj.get("id"))
        subject_name = _clean_text(subject.get("canonical_name") or subject.get("canonicalName") or subject.get("name"))
        object_name = _clean_text(obj.get("canonical_name") or obj.get("canonicalName") or obj.get("name"))
        normalized_predicate = _clean_text(predicate)
        normalized_review = review_status if review_status in REVIEW_STATUSES else "review_required"
        normalized_knowledge = knowledge_status if knowledge_status in KNOWLEDGE_STATUSES else "observed"
        if not subject_id or not object_id or not normalized_predicate:
            raise KnowledgeRelationError(
                "标准关系缺少稳定端点或谓词。",
                code="knowledge_relation_invalid",
            )
        if subject_id == object_id:
            raise KnowledgeRelationError(
                "关系不能是自环。",
                code="knowledge_relation_self_loop",
            )
        normalized_refs = self._normalize_source_refs(source_refs)
        relation_key = f"{subject_id}|{normalized_predicate}|{object_id}"
        provenance_payload = {
            "origin": "explicit_user_binding" if normalized_review == "confirmed" else "agent_extraction",
            "extractorVersion": "storydex-knowledge-relations-v1",
            **dict(provenance or {}),
        }
        fingerprint_payload = {
            "relationKey": relation_key,
            "reviewStatus": normalized_review,
            "knowledgeStatus": normalized_knowledge,
            "sourceRefs": normalized_refs,
            # Provider/model/trace are audit metadata, not evidence identity.
            # Keeping them out prevents unchanged rejected evidence from being
            # resubmitted merely because the runtime or extractor changed.
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        now = _now_iso()
        return {
            "id": _clean_text(relation_id) or f"fact:{_stable_hash(relation_key, length=24)}",
            "subjectId": subject_id,
            "subject": subject_name,
            "predicate": normalized_predicate,
            "objectId": object_id,
            "object": object_name,
            "reviewStatus": normalized_review,
            "knowledgeStatus": normalized_knowledge,
            "confidence": confidence,
            "confidenceScore": self._confidence_score(confidence),
            "sourceRefs": normalized_refs,
            "provenance": provenance_payload,
            "traceId": _clean_text(trace_id),
            "relationKey": relation_key,
            "fingerprint": fingerprint,
            "createdAt": str(created_at or now),
            "updatedAt": str(updated_at or now),
        }

    def load_facts(self, workspace_root: Path) -> Dict[str, Any]:
        path = self.facts_path(workspace_root)
        payload = self._read_json(path, default={})
        raw_facts = payload.get("facts") if isinstance(payload.get("facts"), list) else []
        normalized: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_facts:
            if not isinstance(raw, dict):
                continue
            fact = self._normalize_fact_record(workspace_root, raw)
            if fact is None or fact["relationKey"] in seen:
                continue
            seen.add(fact["relationKey"])
            normalized.append(fact)
        normalized.sort(key=lambda item: item["relationKey"])
        return {
            **{key: value for key, value in payload.items() if key != "facts"},
            "version": FACT_SCHEMA_VERSION,
            "schemaVersion": FACT_SCHEMA_VERSION,
            "facts": normalized,
            "updatedAt": str(payload.get("updatedAt") or _now_iso()),
        }

    def load_review_ledger(self, workspace_root: Path) -> Dict[str, Any]:
        path = self.review_path(workspace_root)
        payload = self._read_json(path, default={})
        raw_relations = payload.get("relations") if isinstance(payload.get("relations"), list) else []
        relations = [dict(item) for item in raw_relations if isinstance(item, dict)]
        relations.sort(key=lambda item: (str(item.get("createdAt") or ""), str(item.get("id") or "")))
        return {
            **{key: value for key, value in payload.items() if key != "relations"},
            "version": REVIEW_SCHEMA_VERSION,
            "schemaVersion": REVIEW_SCHEMA_VERSION,
            "relations": relations,
            "updatedAt": str(payload.get("updatedAt") or _now_iso()),
        }

    def list_review_relations(
        self,
        workspace_root: Path,
        *,
        status: str = "review_required",
        offset: int = 0,
        limit: int = 100,
    ) -> Dict[str, Any]:
        ledger = self.load_review_ledger(workspace_root)
        normalized_status = str(status or "").strip()
        items = [
            dict(item)
            for item in ledger["relations"]
            if not normalized_status or str(item.get("reviewStatus") or "") == normalized_status
        ]
        start = max(0, int(offset or 0))
        size = max(1, min(500, int(limit or 100)))
        page = items[start : start + size]
        return {
            "relations": page,
            "total": len(items),
            "offset": start,
            "limit": size,
            "hasMore": start + len(page) < len(items),
            "nextOffset": start + len(page) if start + len(page) < len(items) else None,
        }

    # ------------------------------------------------------------------
    # Explicit two-turn binding workflow
    # ------------------------------------------------------------------
    def prepare_explicit(
        self,
        workspace_root: Path,
        relations: Sequence[Mapping[str, Any]],
        *,
        session_id: Any = "",
        trace_id: Any = "",
        provider_id: Any = "",
        model: Any = "",
    ) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        if not isinstance(relations, Sequence) or isinstance(relations, (str, bytes)) or not relations:
            raise KnowledgeRelationError(
                "prepare_explicit 至少需要一条关系。",
                code="knowledge_relations_required",
            )
        prepared: List[Dict[str, Any]] = []
        fingerprints: set[str] = set()
        for index, raw_relation in enumerate(relations):
            if not isinstance(raw_relation, Mapping):
                raise KnowledgeRelationError(
                    f"第 {index + 1} 条关系格式无效。",
                    code="knowledge_relation_invalid",
                )
            subject = self.resolve_entity(
                root,
                raw_relation.get("subject") or raw_relation.get("subjectName"),
                entity_id=raw_relation.get("subjectId"),
                source_path=raw_relation.get("subjectSourcePath"),
            )
            object_reference = raw_relation.get("object") or raw_relation.get("objectName") or raw_relation.get("target")
            object_source_path = raw_relation.get("objectSourcePath")
            legacy_object_path = False
            # ``targetSourcePath`` historically appeared in model-generated
            # payloads as the object's source file.  If it clearly names the
            # resolved object and no dedicated objectSourcePath is present,
            # consume it as an entity hint and let the formal destination
            # default to the subject file.  An explicit formalPath or a path
            # that does not identify the object keeps the documented meaning.
            if not _clean_text(object_source_path):
                candidate_object_path = _normalize_relative_path(raw_relation.get("targetSourcePath"))
                candidate_object_name = _clean_text(object_reference)
                if candidate_object_path and candidate_object_name:
                    candidate_file = self._workspace_file(root, candidate_object_path)
                    candidate_title = self._title_from_source(candidate_file) if candidate_file.is_file() else ""
                    candidate_stem = Path(candidate_object_path).stem.replace(".relations", "")
                    if candidate_title == candidate_object_name or candidate_stem == candidate_object_name:
                        object_source_path = candidate_object_path
                        legacy_object_path = True
            obj = self.resolve_entity(
                root,
                object_reference,
                entity_id=raw_relation.get("objectId") or raw_relation.get("targetId"),
                source_path=object_source_path,
            )
            predicate = _clean_text(raw_relation.get("predicate") or raw_relation.get("relation"))
            if not predicate:
                raise KnowledgeRelationError(
                    f"第 {index + 1} 条关系缺少谓词。",
                    code="knowledge_relation_predicate_required",
                )
            requested_formal_path = raw_relation.get("formalPath")
            if not _clean_text(requested_formal_path) and not legacy_object_path:
                requested_formal_path = raw_relation.get("targetSourcePath")
            target_path, sidecar = self._resolve_formal_target_path(
                root,
                subject,
                requested_path=requested_formal_path,
            )
            relation_key = f"{subject['entityId']}|{predicate}|{obj['entityId']}"
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "relationKey": relation_key,
                        "targetSourcePath": target_path,
                        "knowledgeStatus": str(raw_relation.get("knowledgeStatus") or "planned"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if fingerprint in fingerprints:
                continue
            fingerprints.add(fingerprint)
            prepared.append(
                {
                    "subjectId": subject["entityId"],
                    "subject": subject["canonical_name"],
                    "predicate": predicate,
                    "objectId": obj["entityId"],
                    "object": obj["canonical_name"],
                    "knowledgeStatus": str(raw_relation.get("knowledgeStatus") or "planned")
                    if str(raw_relation.get("knowledgeStatus") or "planned") in KNOWLEDGE_STATUSES
                    else "planned",
                    "targetSourcePath": target_path,
                    "usesSidecar": sidecar,
                    "relationKey": relation_key,
                    "fingerprint": fingerprint,
                }
            )
        if not prepared:
            raise KnowledgeRelationError(
                "没有可准备的唯一关系。",
                code="knowledge_relations_empty_after_dedup",
            )
        plan_id = f"krp_{uuid4().hex}"
        created = _now()
        plan = {
            "schemaVersion": KNOWLEDGE_PLAN_SCHEMA_VERSION,
            "planId": plan_id,
            "workspaceKey": hashlib.sha256(root.as_posix().encode("utf-8")).hexdigest(),
            "sessionId": _clean_text(session_id),
            "traceId": _clean_text(trace_id),
            "createdAt": created.isoformat(),
            "expiresAt": (created + timedelta(hours=KNOWLEDGE_PLAN_TTL_HOURS)).isoformat(),
            "providerId": _clean_text(provider_id),
            "model": _clean_text(model),
            "relations": prepared,
        }
        plan["fingerprint"] = self._plan_fingerprint(plan)
        path = self.plans_root(root) / f"{plan_id}.json"
        self._atomic_write_text(path, _json_text(plan))
        return {
            "ok": True,
            "operation": "prepare_explicit",
            "planId": plan_id,
            "expiresAt": plan["expiresAt"],
            "fingerprint": plan["fingerprint"],
            "relationCount": len(prepared),
            "relations": [dict(item) for item in prepared],
            "targetPaths": list(dict.fromkeys(item["targetSourcePath"] for item in prepared)),
            "writtenPaths": [path.relative_to(root).as_posix()],
            "confirmationRequired": True,
        }

    def apply_explicit(
        self,
        workspace_root: Path,
        plan_id: str,
        *,
        session_id: Any = "",
        trace_id: Any = "",
        expected_fingerprint: Any = "",
        _relation_metadata: Optional[Mapping[str, Mapping[str, Any]]] = None,
        _additional_writes: Optional[Mapping[Path, str]] = None,
    ) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        normalized_plan_id = str(plan_id or "").strip()
        if not re.fullmatch(r"krp_[0-9a-f]{32}", normalized_plan_id):
            raise KnowledgeRelationError("planId 无效。", code="knowledge_plan_invalid")
        plan_path = self.plans_root(root) / f"{normalized_plan_id}.json"
        plan = self._read_json(plan_path, default={})
        if not plan:
            raise KnowledgeRelationError(
                "知识写入计划不存在或已清理。",
                code="knowledge_plan_not_found",
                status_code=404,
            )
        self._validate_plan(root, plan, session_id=session_id, trace_id=trace_id, expected_fingerprint=expected_fingerprint)
        relations = plan.get("relations") if isinstance(plan.get("relations"), list) else []
        if not relations:
            raise KnowledgeRelationError("知识写入计划没有关系。", code="knowledge_plan_empty")

        with self._lock:
            entities_payload = self.load_entities(root)
            facts_payload = self.load_facts(root)
            facts_by_key = {
                str(item.get("relationKey") or ""): dict(item)
                for item in facts_payload["facts"]
                if isinstance(item, dict) and str(item.get("relationKey") or "")
            }
            entity_by_id = {
                str(item.get("entityId") or ""): dict(item)
                for item in entities_payload["entities"]
                if isinstance(item, dict)
            }
            target_contents: Dict[Path, str] = {}
            changed_paths: List[str] = []
            fact_ids: List[str] = []
            graph_added: List[Dict[str, Any]] = []
            now = _now_iso()
            relation_metadata = {
                str(key): dict(value)
                for key, value in (_relation_metadata or {}).items()
                if isinstance(value, Mapping)
            }

            for raw in relations:
                if not isinstance(raw, dict):
                    continue
                subject = entity_by_id.get(str(raw.get("subjectId") or ""))
                obj = entity_by_id.get(str(raw.get("objectId") or ""))
                if not subject or not obj:
                    raise KnowledgeRelationError(
                        "计划中的实体已变化，请重新准备并确认。",
                        code="knowledge_plan_stale_entity",
                        status_code=409,
                    )
                relative_path = _normalize_relative_path(raw.get("targetSourcePath"))
                path = self._workspace_file(root, relative_path)
                current = target_contents.get(path)
                if current is None:
                    current = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
                rendered, relation_line, changed = self.render_markdown_relation(
                    current,
                    predicate=str(raw.get("predicate") or ""),
                    target=str(obj.get("canonical_name") or ""),
                    entity_id=str(subject.get("entityId") or "") if bool(raw.get("usesSidecar")) else "",
                )
                target_contents[path] = rendered
                if changed:
                    changed_paths.append(relative_path)
                source_ref = {
                    "path": relative_path,
                    "quote": relation_line,
                    "role": "formal_relation",
                }
                metadata = relation_metadata.get(str(raw.get("relationKey") or ""), {})
                inherited_refs = (
                    metadata.get("sourceRefs")
                    if isinstance(metadata.get("sourceRefs"), list)
                    else []
                )
                source_refs = self._normalize_source_refs([*inherited_refs, source_ref])
                inherited_provenance = (
                    dict(metadata.get("provenance"))
                    if isinstance(metadata.get("provenance"), Mapping)
                    else {}
                )
                dto = self.relation_dto(
                    subject=subject,
                    predicate=raw.get("predicate"),
                    obj=obj,
                    review_status="confirmed",
                    knowledge_status=str(raw.get("knowledgeStatus") or "planned"),
                    source_refs=source_refs,
                    provenance={
                        "origin": "explicit_user_binding",
                        "extractorVersion": "storydex-explicit-binding-v1",
                        "providerId": str(plan.get("providerId") or ""),
                        "model": str(plan.get("model") or ""),
                        **inherited_provenance,
                    },
                    trace_id=metadata.get("traceId") or trace_id or plan.get("traceId"),
                    confidence=metadata.get("confidence", "confirmed"),
                    relation_id=(
                        metadata.get("relationId")
                        or (facts_by_key.get(str(raw.get("relationKey") or "")) or {}).get("id")
                    ),
                    created_at=(facts_by_key.get(str(raw.get("relationKey") or "")) or {}).get("createdAt"),
                    updated_at=now,
                )
                dto.update(
                    {
                        "established_in": relative_path,
                        "evidence": relation_line,
                        **(
                            dict(metadata.get("extraFields"))
                            if isinstance(metadata.get("extraFields"), Mapping)
                            else {}
                        ),
                    }
                )
                facts_by_key[dto["relationKey"]] = dto
                fact_ids.append(dto["id"])
                graph_added.append(self.graph_edge_from_relation(dto))

            facts_payload["facts"] = sorted(facts_by_key.values(), key=lambda item: str(item.get("relationKey") or ""))
            facts_payload["updatedAt"] = now
            entities_payload["updatedAt"] = now
            writes: Dict[Path, str] = {
                **target_contents,
                self.entities_path(root): _json_text(entities_payload),
                self.facts_path(root): _json_text(facts_payload),
                **{
                    Path(path): str(content)
                    for path, content in (_additional_writes or {}).items()
                },
            }
            self._validate_relation_invariants(entities_payload, facts_payload)
            before_checksums = self._checksums(writes.keys())
            self._transactional_replace(writes)
            try:
                wiki = self._rebuild_projection(root)
                if not isinstance(wiki, dict) or str(wiki.get("status") or "").strip() != "ready":
                    raise KnowledgeRelationError(
                        "知识关系已写入，但 WIKI 投影未进入 ready 状态。",
                        code="knowledge_projection_not_ready",
                        status_code=409,
                        details={"status": str(wiki.get("status") or "") if isinstance(wiki, dict) else "invalid"},
                    )
                wiki_graph = wiki.get("graph") if isinstance(wiki.get("graph"), dict) else {}
                published_edge_ids = {
                    str(edge.get("id") or "")
                    for edge in wiki_graph.get("edges", [])
                    if isinstance(edge, dict) and str(edge.get("id") or "")
                }
                missing_fact_ids = sorted(
                    fact_id
                    for fact_id in set(fact_ids)
                    if fact_id not in published_edge_ids
                )
                if missing_fact_ids:
                    raise KnowledgeRelationError(
                        "WIKI 投影未发布本次确认的全部关系。",
                        code="knowledge_projection_relation_missing",
                        status_code=409,
                        details={"missingFactIds": missing_fact_ids},
                    )
            except Exception:
                self._restore_checksums_snapshot(before_checksums)
                try:
                    self._rebuild_projection(root)
                except Exception:
                    pass
                raise

            plan["appliedAt"] = now
            plan["appliedTraceId"] = _clean_text(trace_id)
            plan["status"] = "applied"
            self._atomic_write_text(plan_path, _json_text(plan))
            revision = int(wiki.get("knowledgeRevision") or wiki.get("builtFromRevision") or 0) if isinstance(wiki, dict) else 0
            return {
                "ok": True,
                "operation": "apply_explicit",
                "planId": normalized_plan_id,
                "relationCount": len(graph_added),
                "factIds": list(dict.fromkeys(fact_ids)),
                "writtenPaths": list(
                    dict.fromkeys(
                        [*changed_paths, ENTITY_SOURCE_PATH.as_posix(), FACT_SOURCE_PATH.as_posix()]
                        + [
                            Path(path).resolve().relative_to(root).as_posix()
                            for path in (_additional_writes or {})
                        ]
                    )
                ),
                "revision": revision,
                "graphDiff": {"added": graph_added, "removed": [], "unchanged": []},
                "wiki": self._wiki_summary(wiki),
            }

    # ------------------------------------------------------------------
    # Candidate extraction, confirmation and rejection
    # ------------------------------------------------------------------
    def submit_candidates(
        self,
        workspace_root: Path,
        candidates: Sequence[Mapping[str, Any]],
        *,
        trace_id: Any = "",
        provider_id: Any = "",
        model: Any = "",
        extractor_version: Any = "",
    ) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        ledger = self.load_review_ledger(root)
        existing = [dict(item) for item in ledger["relations"]]
        by_fingerprint = {
            str(item.get("fingerprint") or ""): item
            for item in existing
            if str(item.get("fingerprint") or "")
        }
        active_by_key = {
            str(item.get("relationKey") or ""): item
            for item in existing
            if str(item.get("reviewStatus") or "") == "review_required"
        }
        confirmed_relation_keys = {
            str(item.get("relationKey") or "")
            for item in self.load_facts(root).get("facts", [])
            if isinstance(item, dict)
            and str(item.get("reviewStatus") or "confirmed") == "confirmed"
            and str(item.get("relationKey") or "")
        }
        accepted: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        for index, raw in enumerate(candidates if isinstance(candidates, Sequence) else []):
            if not isinstance(raw, Mapping):
                skipped.append({"index": index, "reason": "invalid_candidate"})
                continue
            try:
                subject = self.resolve_entity(
                    root,
                    raw.get("subject") or raw.get("subjectName"),
                    entity_id=raw.get("subjectId"),
                    source_path=raw.get("subjectSourcePath"),
                    # Candidate submission is intentionally ledger-only.  It
                    # may resolve an entity already registered by the local
                    # index, but it must never discover/materialize a new
                    # canonical entity as a side effect of model output.
                    allow_discovery=False,
                )
                obj = self.resolve_entity(
                    root,
                    raw.get("object") or raw.get("objectName") or raw.get("target"),
                    entity_id=raw.get("objectId") or raw.get("targetId"),
                    source_path=raw.get("objectSourcePath"),
                    allow_discovery=False,
                )
                source_refs = self._validate_candidate_source_refs(
                    root,
                    raw.get("sourceRefs"),
                    subject=subject,
                    obj=obj,
                )
                quote_text = "\n".join(str(item.get("quote") or "") for item in source_refs)
                if _NEGATION_RE.search(quote_text):
                    skipped.append({"index": index, "reason": "negated_evidence"})
                    continue
                if _HYPOTHETICAL_RE.search(quote_text):
                    skipped.append({"index": index, "reason": "hypothetical_evidence"})
                    continue
                knowledge_status = str(raw.get("knowledgeStatus") or "observed")
                if knowledge_status not in KNOWLEDGE_STATUSES:
                    knowledge_status = "observed"
                if _RUMOR_RE.search(quote_text):
                    knowledge_status = "inferred"
                dto = self.relation_dto(
                    subject=subject,
                    predicate=raw.get("predicate") or raw.get("relation"),
                    obj=obj,
                    review_status="review_required",
                    knowledge_status=knowledge_status,
                    source_refs=source_refs,
                    provenance={
                        **(dict(raw.get("provenance")) if isinstance(raw.get("provenance"), Mapping) else {}),
                        # Runtime metadata is authoritative; candidate payloads
                        # cannot impersonate another provider/model/origin.
                        "origin": "agent_extraction",
                        "extractorVersion": _clean_text(extractor_version) or "storydex-agent-relations-v1",
                        "providerId": _clean_text(provider_id),
                        "model": _clean_text(model),
                    },
                    trace_id=trace_id or raw.get("traceId"),
                    confidence=raw.get("confidence", 0.7),
                    relation_id=f"candidate:{_stable_hash(index, subject['entityId'], raw.get('predicate'), obj['entityId'], source_refs, length=24)}",
                )
                dto["evidenceHash"] = hashlib.sha256(quote_text.encode("utf-8")).hexdigest()
                dto["reviewReason"] = "agent_extracted"
                dto["publishedFactId"] = ""
                dto["subjectSourcePaths"] = self._unique_paths(subject.get("sourcePaths"))
                dto["objectSourcePaths"] = self._unique_paths(obj.get("sourcePaths"))
                try:
                    suggested_path, uses_sidecar = self._resolve_formal_target_path(root, subject)
                except KnowledgeRelationError:
                    suggested_path, uses_sidecar = "", False
                dto["targetSourcePath"] = suggested_path
                dto["usesSidecar"] = uses_sidecar
            except KnowledgeRelationError as exc:
                skipped.append({"index": index, "reason": exc.code, "message": str(exc)})
                continue

            prior_same = by_fingerprint.get(dto["fingerprint"])
            if prior_same is not None:
                skipped.append(
                    {
                        "index": index,
                        "reason": "unchanged_candidate",
                        "candidateId": prior_same.get("id"),
                        "reviewStatus": prior_same.get("reviewStatus"),
                    }
                )
                continue
            if dto["relationKey"] in confirmed_relation_keys:
                skipped.append(
                    {
                        "index": index,
                        "reason": "already_confirmed",
                        "relationKey": dto["relationKey"],
                    }
                )
                continue
            previous_active = active_by_key.get(dto["relationKey"])
            if previous_active is not None:
                previous_active["reviewStatus"] = "superseded"
                previous_active["supersededAt"] = _now_iso()
                previous_active["supersededBy"] = dto["id"]
            existing.append(dto)
            by_fingerprint[dto["fingerprint"]] = dto
            active_by_key[dto["relationKey"]] = dto
            accepted.append(dto)

        if accepted or any(item.get("reviewStatus") == "superseded" for item in existing):
            ledger["relations"] = existing
            ledger["updatedAt"] = _now_iso()
            self._atomic_write_text(self.review_path(root), _json_text(ledger))
        return {
            "ok": True,
            "operation": "submit_candidates",
            "submittedCount": len(candidates) if isinstance(candidates, Sequence) else 0,
            "acceptedCount": len(accepted),
            "skippedCount": len(skipped),
            "candidates": accepted,
            "skipped": skipped,
            "writtenPaths": [REVIEW_SOURCE_PATH.as_posix()] if accepted else [],
        }

    def confirm_candidate(
        self,
        workspace_root: Path,
        candidate_id: str,
        *,
        expected_fingerprint: str,
        subject_id: str = "",
        predicate: str = "",
        object_id: str = "",
        target_source_path: str = "",
        trace_id: str = "",
    ) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        ledger = self.load_review_ledger(root)
        candidate = self._ledger_candidate(ledger, candidate_id)
        self._require_current_candidate(candidate, expected_fingerprint)
        subject = self.resolve_entity(
            root,
            candidate.get("subject"),
            entity_id=subject_id or candidate.get("subjectId"),
        )
        obj = self.resolve_entity(
            root,
            candidate.get("object"),
            entity_id=object_id or candidate.get("objectId"),
        )
        # A reviewer may correct either endpoint.  Re-check the original
        # verbatim evidence against the corrected pair before publishing;
        # otherwise a candidate could retain evidence that only supported the
        # old endpoint.
        candidate_refs = candidate.get("sourceRefs") if isinstance(candidate.get("sourceRefs"), list) else []
        if candidate_refs:
            self._validate_candidate_source_refs(
                root,
                candidate_refs,
                subject=subject,
                obj=obj,
            )
        effective_predicate = _clean_text(predicate) or _clean_text(candidate.get("predicate"))
        formal_path, sidecar = self._resolve_formal_target_path(
            root,
            subject,
            requested_path=target_source_path,
        )
        prepared = self.prepare_explicit(
            root,
            [
                {
                    "subjectId": subject["entityId"],
                    "predicate": effective_predicate,
                    "objectId": obj["entityId"],
                    "targetSourcePath": formal_path,
                    "knowledgeStatus": candidate.get("knowledgeStatus") or "observed",
                }
            ],
            session_id="review-api",
            trace_id=f"candidate:{candidate_id}",
            provider_id=str((candidate.get("provenance") or {}).get("providerId") or "")
            if isinstance(candidate.get("provenance"), dict)
            else "",
            model=str((candidate.get("provenance") or {}).get("model") or "")
            if isinstance(candidate.get("provenance"), dict)
            else "",
        )
        prepared_relation = dict((prepared.get("relations") or [{}])[0])
        relation_key = str(prepared_relation.get("relationKey") or "")
        existing_fact = next(
            (
                fact
                for fact in self.load_facts(root).get("facts", [])
                if isinstance(fact, dict) and str(fact.get("relationKey") or "") == relation_key
            ),
            {},
        )
        published_fact_id = str(existing_fact.get("id") or f"fact:{_stable_hash(relation_key, length=24)}")
        confirmed_at = _now_iso()
        candidate.update(
            {
                "subjectId": subject["entityId"],
                "subject": subject["canonical_name"],
                "predicate": effective_predicate,
                "objectId": obj["entityId"],
                "object": obj["canonical_name"],
                "relationKey": relation_key,
                "reviewStatus": "confirmed",
                "confirmedAt": confirmed_at,
                "confirmedTraceId": trace_id,
                "targetSourcePath": formal_path,
                "usesSidecar": sidecar,
                "publishedFactId": published_fact_id,
            }
        )
        ledger["updatedAt"] = confirmed_at
        candidate_provenance = (
            dict(candidate.get("provenance"))
            if isinstance(candidate.get("provenance"), Mapping)
            else {}
        )
        applied = self.apply_explicit(
            root,
            prepared["planId"],
            session_id="review-api",
            trace_id=trace_id or f"confirm:{uuid4()}",
            expected_fingerprint=prepared["fingerprint"],
            _relation_metadata={
                relation_key: {
                    "sourceRefs": candidate.get("sourceRefs") or [],
                    "provenance": {
                        **candidate_provenance,
                        "origin": "agent_extraction_confirmed",
                        "confirmedBy": "user_review",
                    },
                    "traceId": candidate.get("traceId"),
                    "confidence": candidate.get("confidence", 0.7),
                    "relationId": published_fact_id,
                    "extraFields": {
                        "confirmationTraceId": trace_id,
                        "reviewCandidateId": candidate_id,
                    },
                }
            },
            _additional_writes={self.review_path(root): _json_text(ledger)},
        )
        return {"ok": True, "candidate": dict(candidate), "apply": applied}

    def reject_candidate(
        self,
        workspace_root: Path,
        candidate_id: str,
        *,
        expected_fingerprint: str,
        reason: str,
        note: str = "",
    ) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        normalized_reason = str(reason or "").strip().lower()
        if normalized_reason not in REJECT_REASONS:
            raise KnowledgeRelationError(
                "驳回原因无效。",
                code="knowledge_reject_reason_invalid",
                details={"allowed": sorted(REJECT_REASONS)},
            )
        ledger = self.load_review_ledger(root)
        candidate = self._ledger_candidate(ledger, candidate_id)
        self._require_current_candidate(candidate, expected_fingerprint)
        candidate.update(
            {
                "reviewStatus": "rejected",
                "rejectedAt": _now_iso(),
                "rejectionReason": normalized_reason,
                "rejectionNote": _clean_text(note),
            }
        )
        ledger["updatedAt"] = _now_iso()
        self._atomic_write_text(self.review_path(root), _json_text(ledger))
        return {"ok": True, "candidate": dict(candidate)}

    # ------------------------------------------------------------------
    # Migration and deterministic scanning
    # ------------------------------------------------------------------
    def migrate_v1(self, workspace_root: Path) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        with self._lock:
            entities_path = self.entities_path(root)
            facts_path = self.facts_path(root)
            raw_entities = self._read_json(entities_path, default={})
            raw_facts = self._read_json(facts_path, default={})
            already_v2 = (
                int(raw_entities.get("version") or raw_entities.get("schemaVersion") or 0) >= 2
                and int(raw_facts.get("version") or raw_facts.get("schemaVersion") or 0) >= 2
            )
            if already_v2:
                return {"ok": True, "migrated": False, "reason": "already_v2", "backupPath": ""}
            if not entities_path.exists() and not facts_path.exists():
                return {"ok": True, "migrated": False, "reason": "no_legacy_data", "backupPath": ""}
            backup_root = self._backup_legacy_sources(root)
            entities_payload = self.load_entities(root)
            ledger = self.load_review_ledger(root)
            facts: List[Dict[str, Any]] = []
            review_items = [dict(item) for item in ledger["relations"]]
            for raw in raw_facts.get("facts", []) if isinstance(raw_facts.get("facts"), list) else []:
                if not isinstance(raw, dict):
                    continue
                normalized = self._normalize_fact_record(root, raw)
                if normalized is None:
                    unresolved = self._legacy_unresolved_candidate(raw)
                    if unresolved is not None:
                        review_items.append(unresolved)
                    continue
                confidence = str(raw.get("confidence") or "canon").strip().lower()
                source_refs = normalized.get("sourceRefs") if isinstance(normalized.get("sourceRefs"), list) else []
                evidence_valid = bool(source_refs) and all(self._source_ref_exists(root, ref) for ref in source_refs)
                if confidence in {"canon", "confirmed", ""} and evidence_valid:
                    normalized["reviewStatus"] = "confirmed"
                    facts.append(normalized)
                else:
                    candidate = dict(normalized)
                    candidate["id"] = f"candidate:{_stable_hash(normalized['fingerprint'], length=24)}"
                    candidate["reviewStatus"] = "review_required"
                    candidate["reviewReason"] = "v1_migration_insufficient_evidence"
                    review_items.append(candidate)
            facts_payload = {
                "version": FACT_SCHEMA_VERSION,
                "schemaVersion": FACT_SCHEMA_VERSION,
                "updatedAt": _now_iso(),
                "facts": self._dedupe_relations(facts),
            }
            ledger["relations"] = self._dedupe_review_items(review_items)
            ledger["updatedAt"] = _now_iso()
            self._validate_relation_invariants(entities_payload, facts_payload)
            self._transactional_replace(
                {
                    entities_path: _json_text(entities_payload),
                    facts_path: _json_text(facts_payload),
                    self.review_path(root): _json_text(ledger),
                }
            )
            return {
                "ok": True,
                "migrated": True,
                "backupPath": backup_root.relative_to(root).as_posix(),
                "entityCount": len(entities_payload["entities"]),
                "factCount": len(facts_payload["facts"]),
                "reviewCount": len(ledger["relations"]),
            }

    def scan_formal_markdown_relations(self, workspace_root: Path) -> List[Dict[str, Any]]:
        root = Path(workspace_root).resolve()
        entities = self.load_entities(root)
        entity_by_path: Dict[str, List[Dict[str, Any]]] = {}
        for entity in entities["entities"]:
            if not isinstance(entity, dict):
                continue
            for source_path in self._unique_paths(entity.get("sourcePaths")):
                entity_by_path.setdefault(source_path, []).append(entity)
        results: List[Dict[str, Any]] = []
        for prefix in FORMAL_RELATION_PREFIXES:
            base = root / prefix.rstrip("/")
            if not base.is_dir():
                continue
            for path in base.rglob("*.md"):
                relative = path.relative_to(root).as_posix()
                owner_candidates = entity_by_path.get(relative, [])
                if self.is_relation_sidecar_path(relative):
                    frontmatter_id = self._frontmatter_entity_id(path.read_text(encoding="utf-8-sig"))
                    owner_candidates = [
                        item for item in entities["entities"] if str(item.get("entityId") or "") == frontmatter_id
                    ]
                if len(owner_candidates) != 1:
                    continue
                subject = owner_candidates[0]
                for parsed in self.parse_markdown_file(root, relative):
                    try:
                        obj = self.resolve_entity(root, parsed.get("target"))
                        dto = self.relation_dto(
                            subject=subject,
                            predicate=parsed.get("predicate"),
                            obj=obj,
                            review_status="confirmed",
                            knowledge_status="planned",
                            source_refs=[
                                {
                                    "path": relative,
                                    "quote": parsed.get("quote"),
                                    "lineStart": parsed.get("line_start"),
                                    "lineEnd": parsed.get("line_end"),
                                    "role": "formal_relation",
                                }
                            ],
                            provenance={"origin": "explicit_markdown", "extractorVersion": "storydex-markdown-relations-v1"},
                            confidence="confirmed",
                        )
                    except KnowledgeRelationError:
                        continue
                    results.append(dto)
        return self._dedupe_relations(results)

    # ------------------------------------------------------------------
    # Graph adapter
    # ------------------------------------------------------------------
    @staticmethod
    def graph_edge_from_relation(relation: Mapping[str, Any]) -> Dict[str, Any]:
        review_status = str(relation.get("reviewStatus") or "review_required")
        source_refs = relation.get("sourceRefs") if isinstance(relation.get("sourceRefs"), list) else []
        first_ref = source_refs[0] if source_refs and isinstance(source_refs[0], dict) else {}
        return {
            "id": str(relation.get("id") or ""),
            "source": str(relation.get("subjectId") or relation.get("source") or ""),
            "target": str(relation.get("objectId") or relation.get("target") or ""),
            "label": str(relation.get("predicate") or relation.get("label") or ""),
            "predicate": str(relation.get("predicate") or relation.get("label") or ""),
            "type": "fact",
            "reviewStatus": review_status,
            "knowledgeStatus": str(relation.get("knowledgeStatus") or "observed"),
            "confidence": relation.get("confidence"),
            "sourceRefs": source_refs,
            "provenance": dict(relation.get("provenance") or {}) if isinstance(relation.get("provenance"), Mapping) else {},
            "traceId": str(relation.get("traceId") or ""),
            "fingerprint": str(relation.get("fingerprint") or ""),
            "evidence": str(first_ref.get("quote") or relation.get("evidence") or ""),
            "sourcePath": str(first_ref.get("path") or relation.get("established_in") or ""),
            "needsReview": review_status == "review_required",
            "weight": 1,
        }

    # ------------------------------------------------------------------
    # Internal normalization and validation helpers
    # ------------------------------------------------------------------
    def _normalize_fact_record(self, workspace_root: Path, raw: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        subject_name = _clean_text(raw.get("subject"))
        object_name = _clean_text(raw.get("object"))
        predicate = _clean_text(raw.get("predicate"))
        if not subject_name or not object_name or not predicate:
            return None
        try:
            subject = self.resolve_entity(
                workspace_root,
                subject_name,
                entity_id=raw.get("subjectId") or raw.get("subject_id"),
                allow_discovery=False,
            )
            obj = self.resolve_entity(
                workspace_root,
                object_name,
                entity_id=raw.get("objectId") or raw.get("object_id"),
                allow_discovery=False,
            )
        except KnowledgeRelationError:
            return None
        raw_refs = raw.get("sourceRefs") if isinstance(raw.get("sourceRefs"), list) else []
        if not raw_refs:
            legacy_path = _normalize_relative_path(raw.get("established_in") or raw.get("establishedIn"))
            legacy_quote = _clean_text(raw.get("evidence"))
            if legacy_path or legacy_quote:
                raw_refs = [{"path": legacy_path, "quote": legacy_quote, "role": "legacy_evidence"}]
        review_status = str(raw.get("reviewStatus") or "").strip()
        if review_status not in REVIEW_STATUSES:
            confidence = str(raw.get("confidence") or "canon").strip().lower()
            review_status = "confirmed" if confidence in {"canon", "confirmed", ""} else "review_required"
        knowledge_status = str(raw.get("knowledgeStatus") or "observed").strip()
        if knowledge_status not in KNOWLEDGE_STATUSES:
            knowledge_status = "observed"
        dto = self.relation_dto(
            subject=subject,
            predicate=predicate,
            obj=obj,
            review_status=review_status,
            knowledge_status=knowledge_status,
            source_refs=raw_refs,
            provenance=raw.get("provenance") if isinstance(raw.get("provenance"), Mapping) else {"origin": "legacy_fact", "extractorVersion": "storydex-facts-v1"},
            trace_id=raw.get("traceId"),
            confidence=raw.get("confidence", "confirmed"),
            relation_id=raw.get("id"),
            created_at=raw.get("createdAt") or raw.get("created_at"),
            updated_at=raw.get("updatedAt") or raw.get("updated_at"),
        )
        dto["established_in"] = str(raw.get("established_in") or raw.get("establishedIn") or (dto["sourceRefs"][0].get("path") if dto["sourceRefs"] else ""))
        dto["evidence"] = str(raw.get("evidence") or (dto["sourceRefs"][0].get("quote") if dto["sourceRefs"] else ""))
        return dto

    def _normalize_source_refs(self, refs: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for raw in refs if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)) else []:
            if not isinstance(raw, Mapping):
                continue
            path = _normalize_relative_path(raw.get("path") or raw.get("sourcePath"))
            quote = str(raw.get("quote") or raw.get("evidence") or "").strip()
            if not path and not quote:
                continue
            item: Dict[str, Any] = {
                "path": path,
                "quote": quote,
                "role": _clean_text(raw.get("role")) or "evidence",
            }
            for source_key, target_key in (("lineStart", "lineStart"), ("line_start", "lineStart"), ("lineEnd", "lineEnd"), ("line_end", "lineEnd")):
                if source_key in raw:
                    try:
                        item[target_key] = max(1, int(raw[source_key]))
                    except (TypeError, ValueError):
                        pass
            if item not in normalized:
                normalized.append(item)
        return normalized

    def _validate_candidate_source_refs(
        self,
        workspace_root: Path,
        refs: Any,
        *,
        subject: Mapping[str, Any],
        obj: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        normalized = self._normalize_source_refs(refs if isinstance(refs, list) else [])
        if not normalized:
            raise KnowledgeRelationError(
                "候选关系必须包含逐字 sourceRefs。",
                code="knowledge_candidate_source_refs_required",
            )
        subject_names = {
            _clean_text(subject.get("canonical_name")),
            *self._unique_texts(subject.get("aliases")),
        }
        object_names = {
            _clean_text(obj.get("canonical_name")),
            *self._unique_texts(obj.get("aliases")),
        }
        for ref in normalized:
            path = self._workspace_file(workspace_root, ref.get("path"))
            if not path.is_file():
                raise KnowledgeRelationError(
                    "候选证据文件不存在。",
                    code="knowledge_candidate_source_missing",
                    details={"path": ref.get("path")},
                )
            content = path.read_text(encoding="utf-8-sig", errors="strict")
            quote = str(ref.get("quote") or "")
            if not quote or quote not in content:
                raise KnowledgeRelationError(
                    "候选逐字证据在来源文件中不存在。",
                    code="knowledge_candidate_quote_not_found",
                    details={"path": ref.get("path"), "quote": quote[:200]},
                )
            if not any(name and name in quote for name in subject_names) or not any(
                name and name in quote for name in object_names
            ):
                raise KnowledgeRelationError(
                    "候选证据不能同时锚定主体和客体。",
                    code="knowledge_candidate_endpoints_unanchored",
                    details={"path": ref.get("path")},
                )
        return normalized

    def _resolve_formal_target_path(
        self,
        workspace_root: Path,
        subject: Mapping[str, Any],
        *,
        requested_path: Any = "",
    ) -> tuple[str, bool]:
        root = Path(workspace_root).resolve()
        requested = _normalize_relative_path(requested_path)
        if requested:
            if not self._is_formal_relation_path(requested):
                raise KnowledgeRelationError(
                    "正式关系只能写入 worldbook/scripts/characters 下的 Markdown 文件。",
                    code="knowledge_formal_target_invalid",
                    details={"path": requested},
                )
            self._workspace_file(root, requested)
            return requested, self.is_relation_sidecar_path(requested)
        source_paths = self._unique_paths(subject.get("sourcePaths") or subject.get("source_paths"))
        for source_path in source_paths:
            suffix = Path(source_path).suffix.lower()
            if suffix in {".md", ".txt"} and self._is_formal_relation_path(
                str(Path(source_path).with_suffix(".md")).replace("\\", "/")
                if suffix == ".txt"
                else source_path
            ):
                target = source_path if suffix == ".md" else str(Path(source_path).with_suffix(".md")).replace("\\", "/")
                return target, False
            if suffix == ".json" and source_path.startswith(".storydex/characters/"):
                sidecar = str(Path(source_path).with_suffix(".relations.md")).replace("\\", "/")
                return sidecar, True
        raise KnowledgeRelationError(
            f"实体“{subject.get('canonical_name') or subject.get('entityId')}”没有可写入的正式 Markdown 来源。",
            code="knowledge_formal_target_missing",
            status_code=409,
            details={"sourcePaths": source_paths},
        )

    def _validate_plan(
        self,
        workspace_root: Path,
        plan: Mapping[str, Any],
        *,
        session_id: Any,
        trace_id: Any,
        expected_fingerprint: Any,
    ) -> None:
        root = Path(workspace_root).resolve()
        if str(plan.get("workspaceKey") or "") != hashlib.sha256(root.as_posix().encode("utf-8")).hexdigest():
            raise KnowledgeRelationError("计划不属于当前项目。", code="knowledge_plan_workspace_mismatch", status_code=409)
        expires_at = _parse_iso(plan.get("expiresAt"))
        if expires_at is None or expires_at <= _now():
            raise KnowledgeRelationError("知识写入计划已过期，请重新准备。", code="knowledge_plan_expired", status_code=409)
        if str(plan.get("status") or "") == "applied":
            raise KnowledgeRelationError("知识写入计划已经应用。", code="knowledge_plan_already_applied", status_code=409)
        plan_session = _clean_text(plan.get("sessionId"))
        if plan_session and _clean_text(session_id) and plan_session != _clean_text(session_id):
            raise KnowledgeRelationError("计划不属于当前会话。", code="knowledge_plan_session_mismatch", status_code=409)
        plan_trace = _clean_text(plan.get("traceId"))
        if plan_trace and _clean_text(trace_id) and plan_trace == _clean_text(trace_id):
            raise KnowledgeRelationError(
                "不能在创建计划的同一 trace 中应用；必须等待用户下一轮确认。",
                code="knowledge_plan_same_trace",
                status_code=409,
            )
        current_fingerprint = self._plan_fingerprint(plan)
        if str(plan.get("fingerprint") or "") != current_fingerprint:
            raise KnowledgeRelationError("计划内容已变化，请重新准备。", code="knowledge_plan_stale", status_code=409)
        if _clean_text(expected_fingerprint) and _clean_text(expected_fingerprint) != current_fingerprint:
            raise KnowledgeRelationError("计划 fingerprint 已变化。", code="knowledge_plan_stale", status_code=409)

    @staticmethod
    def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
        payload = {
            "workspaceKey": plan.get("workspaceKey"),
            "sessionId": plan.get("sessionId"),
            "traceId": plan.get("traceId"),
            "expiresAt": plan.get("expiresAt"),
            "relations": plan.get("relations"),
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _validate_relation_invariants(
        self,
        entities_payload: Mapping[str, Any],
        facts_payload: Mapping[str, Any],
    ) -> None:
        entities = entities_payload.get("entities") if isinstance(entities_payload.get("entities"), list) else []
        entity_ids = [str(item.get("entityId") or "") for item in entities if isinstance(item, dict)]
        if any(not item for item in entity_ids) or len(entity_ids) != len(set(entity_ids)):
            raise KnowledgeRelationError("实体 ID 不完整或重复。", code="knowledge_invariant_entity_ids")
        known = set(entity_ids)
        facts = facts_payload.get("facts") if isinstance(facts_payload.get("facts"), list) else []
        fact_ids: set[str] = set()
        relation_keys: set[str] = set()
        for fact in facts:
            if not isinstance(fact, dict):
                raise KnowledgeRelationError("facts 包含非法记录。", code="knowledge_invariant_fact_invalid")
            fact_id = str(fact.get("id") or "")
            relation_key = str(fact.get("relationKey") or "")
            subject_id = str(fact.get("subjectId") or "")
            object_id = str(fact.get("objectId") or "")
            if not fact_id or fact_id in fact_ids or not relation_key or relation_key in relation_keys:
                raise KnowledgeRelationError("事实 ID 或 relationKey 重复。", code="knowledge_invariant_duplicate_fact")
            if subject_id not in known or object_id not in known or subject_id == object_id:
                raise KnowledgeRelationError("事实端点无效。", code="knowledge_invariant_fact_endpoint")
            fact_ids.add(fact_id)
            relation_keys.add(relation_key)

    def _transactional_replace(self, writes: Mapping[Path, str]) -> None:
        if not writes:
            return
        originals: Dict[Path, Optional[bytes]] = {}
        temporary_paths: Dict[Path, Path] = {}
        committed: List[Path] = []
        try:
            for raw_path, content in writes.items():
                path = Path(raw_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                originals[path] = path.read_bytes() if path.exists() else None
                temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
                temporary.write_text(str(content), encoding="utf-8")
                temporary_paths[path] = temporary
            for path, temporary in temporary_paths.items():
                os.replace(temporary, path)
                committed.append(path)
        except Exception:
            for temporary in temporary_paths.values():
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            for path in reversed(committed):
                original = originals.get(path)
                try:
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        restore = path.with_name(f".{path.name}.{uuid4().hex}.restore")
                        restore.write_bytes(original)
                        os.replace(restore, path)
                except Exception:
                    if original is not None:
                        path.write_bytes(original)
            raise

    def _checksums(self, paths: Iterable[Path]) -> Dict[Path, Optional[bytes]]:
        return {Path(path): Path(path).read_bytes() if Path(path).exists() else None for path in paths}

    @staticmethod
    def _restore_checksums_snapshot(snapshot: Mapping[Path, Optional[bytes]]) -> None:
        for raw_path, content in snapshot.items():
            path = Path(raw_path)
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(str(content), encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _read_json(path: Path, *, default: Dict[str, Any]) -> Dict[str, Any]:
        if not path.is_file():
            return dict(default)
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return dict(default)
        return dict(payload) if isinstance(payload, dict) else dict(default)

    @staticmethod
    def _unique_texts(value: Any) -> List[str]:
        raw = value if isinstance(value, list) else []
        return list(dict.fromkeys(text for text in (_clean_text(item) for item in raw) if text))

    @staticmethod
    def _unique_paths(value: Any) -> List[str]:
        raw = value if isinstance(value, list) else []
        return list(dict.fromkeys(path for path in (_normalize_relative_path(item) for item in raw) if path))

    @staticmethod
    def _kind_from_paths(paths: Sequence[str]) -> str:
        for path in paths:
            normalized = _normalize_relative_path(path)
            if normalized.startswith(".storydex/characters/"):
                return "character"
            if normalized.startswith(".storydex/scripts/"):
                return "plot"
            if normalized.startswith(".storydex/worldbook/"):
                return "setting"
        return ""

    @staticmethod
    def _new_entity_id(*, kind: str, canonical_name: str, source_paths: Sequence[str]) -> str:
        prefix = {
            "character": "character",
            "location": "location",
            "place": "location",
            "faction": "faction",
            "organization": "faction",
            "item": "item",
            "event": "event",
            "plot": "event",
            "world": "world",
            "setting": "setting",
        }.get(str(kind or "").lower(), "entity")
        identity = next((path for path in source_paths if path), canonical_name)
        return f"{prefix}:{_stable_hash(identity, canonical_name, length=24)}"

    @staticmethod
    def _is_entity_source_path(relative_path: str) -> bool:
        normalized = _normalize_relative_path(relative_path)
        return any(normalized.startswith(prefix) for prefix in FORMAL_RELATION_PREFIXES) and not normalized.endswith(".relations.md")

    @staticmethod
    def _title_from_source(path: Path) -> str:
        if not path.is_file():
            return path.stem.replace(".relations", "")
        try:
            if path.suffix.lower() == ".json":
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(payload, dict):
                    name = _clean_text(payload.get("name") or payload.get("displayName") or payload.get("title"))
                    if name:
                        return name
            else:
                text = path.read_text(encoding="utf-8-sig")
                match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
                if match:
                    return re.sub(r"[*_`]", "", match.group(1)).strip()
        except Exception:
            pass
        return path.stem.replace(".relations", "")

    @staticmethod
    def _unique_entity_match(matches: Sequence[Dict[str, Any]], value: str, field: str) -> Dict[str, Any]:
        if len(matches) == 1:
            return dict(matches[0])
        if len(matches) > 1:
            raise KnowledgeRelationError(
                f"实体 {field}“{value}”不唯一。",
                code="knowledge_entity_ambiguous",
                status_code=409,
                details={"field": field, "value": value},
            )
        raise KnowledgeRelationError(
            f"未找到实体 {field}“{value}”。",
            code="knowledge_entity_not_found",
            status_code=404,
            details={"field": field, "value": value},
        )

    @staticmethod
    def _ambiguous_entity_error(value: str, matches: Sequence[Mapping[str, Any]], field: str) -> KnowledgeRelationError:
        return KnowledgeRelationError(
            f"实体 {field}“{value}”存在歧义，不能猜测合并。",
            code="knowledge_entity_ambiguous",
            status_code=409,
            details={
                "field": field,
                "value": value,
                "candidates": [str(item.get("entityId") or "") for item in matches],
            },
        )

    @staticmethod
    def _confidence_score(value: Any) -> float:
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value)))
        normalized = str(value or "").strip().lower()
        return {"canon": 1.0, "confirmed": 1.0, "high": 0.9, "medium": 0.7, "low": 0.4}.get(normalized, 0.7)

    @staticmethod
    def _frontmatter_entity_id(content: str) -> str:
        match = _FRONTMATTER_RE.match(str(content or ""))
        if not match:
            return ""
        for line in match.group("body").splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() in {"entityId", "entity_id"}:
                return value.strip().strip("'\"")
        return ""

    def _source_ref_exists(self, workspace_root: Path, ref: Mapping[str, Any]) -> bool:
        try:
            path = self._workspace_file(workspace_root, ref.get("path"))
        except KnowledgeRelationError:
            return False
        if not path.is_file():
            return False
        quote = str(ref.get("quote") or "")
        try:
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            return False
        return bool(quote and quote in content)

    def _ledger_candidate(self, ledger: Mapping[str, Any], candidate_id: str) -> Dict[str, Any]:
        relations = ledger.get("relations") if isinstance(ledger.get("relations"), list) else []
        candidate = next(
            (item for item in relations if isinstance(item, dict) and str(item.get("id") or "") == str(candidate_id or "")),
            None,
        )
        if candidate is None:
            raise KnowledgeRelationError(
                "候选关系不存在。",
                code="knowledge_candidate_not_found",
                status_code=404,
                details={"candidateId": candidate_id},
            )
        return candidate

    @staticmethod
    def _require_current_candidate(candidate: Mapping[str, Any], expected_fingerprint: str) -> None:
        if str(candidate.get("reviewStatus") or "") != "review_required":
            raise KnowledgeRelationError(
                "候选关系已处理或已被替代。",
                code="knowledge_candidate_not_reviewable",
                status_code=409,
            )
        if not expected_fingerprint or str(candidate.get("fingerprint") or "") != str(expected_fingerprint):
            raise KnowledgeRelationError(
                "候选关系已变化，请刷新后重试。",
                code="knowledge_candidate_stale",
                status_code=409,
                details={"currentFingerprint": str(candidate.get("fingerprint") or "")},
            )

    def _backup_legacy_sources(self, workspace_root: Path) -> Path:
        root = Path(workspace_root).resolve()
        stamp = _now().strftime("%Y%m%dT%H%M%S%fZ")
        backup_root = root / ".storydex" / "memory" / "backups" / f"knowledge-relations-{stamp}"
        backup_root.mkdir(parents=True, exist_ok=False)
        for relative in (ENTITY_SOURCE_PATH, FACT_SOURCE_PATH, Path(".storydex/memory/current/relationship_graph.json")):
            source = root / relative
            if source.is_file():
                destination = backup_root / relative.name
                shutil.copy2(source, destination)
        wiki_root = root / ".storydex" / "wiki"
        if wiki_root.is_dir():
            shutil.copytree(wiki_root, backup_root / "wiki")
        return backup_root

    def _legacy_unresolved_candidate(self, raw: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        subject = _clean_text(raw.get("subject"))
        predicate = _clean_text(raw.get("predicate"))
        obj = _clean_text(raw.get("object"))
        if not subject or not predicate or not obj:
            return None
        legacy_path = _normalize_relative_path(raw.get("established_in") or raw.get("establishedIn"))
        legacy_quote = str(raw.get("evidence") or "").strip()
        source_refs = self._normalize_source_refs(
            [{"path": legacy_path, "quote": legacy_quote, "role": "legacy_evidence"}]
        )
        relation_key = f"unresolved:{subject}|{predicate}|{obj}"
        evidence_hash = hashlib.sha256(legacy_quote.encode("utf-8")).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps(
                {"relationKey": relation_key, "sourceRefs": source_refs},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        now = _now_iso()
        return {
            "id": f"candidate:{_stable_hash(fingerprint, length=24)}",
            "subjectId": "",
            "subject": subject,
            "predicate": predicate,
            "objectId": "",
            "object": obj,
            "reviewStatus": "review_required",
            "knowledgeStatus": "observed",
            "confidence": raw.get("confidence", "unknown"),
            "confidenceScore": self._confidence_score(raw.get("confidence")),
            "sourceRefs": source_refs,
            "provenance": {
                "origin": "v1_migration",
                "extractorVersion": "storydex-knowledge-relations-v1-migration",
            },
            "traceId": _clean_text(raw.get("traceId")),
            "relationKey": relation_key,
            "fingerprint": fingerprint,
            "evidenceHash": evidence_hash,
            "reviewReason": "v1_migration_unresolved_endpoint",
            "publishedFactId": "",
            "createdAt": str(raw.get("createdAt") or raw.get("created_at") or now),
            "updatedAt": str(raw.get("updatedAt") or raw.get("updated_at") or now),
        }

    @staticmethod
    def _dedupe_relations(relations: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        by_key: Dict[str, Dict[str, Any]] = {}
        for relation in relations:
            key = str(relation.get("relationKey") or "")
            if key:
                by_key[key] = dict(relation)
        return sorted(by_key.values(), key=lambda item: str(item.get("relationKey") or ""))

    @staticmethod
    def _dedupe_review_items(relations: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        by_fingerprint: Dict[str, Dict[str, Any]] = {}
        for relation in relations:
            fingerprint = str(relation.get("fingerprint") or "")
            if fingerprint:
                by_fingerprint[fingerprint] = dict(relation)
        return sorted(by_fingerprint.values(), key=lambda item: (str(item.get("createdAt") or ""), str(item.get("id") or "")))

    @staticmethod
    def _wiki_summary(payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
        return {
            "knowledgeRevision": int(payload.get("knowledgeRevision") or 0),
            "entryCount": len(payload.get("entries") or []),
            "nodeCount": len(graph.get("nodes") or []),
            "edgeCount": len(graph.get("edges") or []),
        }

    @staticmethod
    def _rebuild_projection(workspace_root: Path) -> Dict[str, Any]:
        from services.story_wiki_service import get_story_wiki_service

        return get_story_wiki_service().rebuild(Path(workspace_root).resolve())


_SERVICE: Optional[StoryKnowledgeRelationService] = None


def get_story_knowledge_relation_service() -> StoryKnowledgeRelationService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = StoryKnowledgeRelationService()
    return _SERVICE


# Compatibility aliases for tests and callers that use a shorter service name.
KnowledgeRelationService = StoryKnowledgeRelationService
