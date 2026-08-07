"""Persistent, provenance-only evidence ledger for Storydex Agent turns.

P1-5 deliberately records observations before introducing any read-result
reuse. The ledger stores source metadata (path, revision and spans), hashes
and turn provenance, never the tool response body. A revision change
invalidates only the affected path; unrelated evidence in the same session is
kept intact.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence
from uuid import uuid4

from services.source_contract import normalize_source_path, validate_source_revision


LEDGER_SCHEMA_VERSION = 1
LEDGER_RELATIVE_ROOT = ".storydex/.agent/runtime/evidence-ledger"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_key(value: str) -> str:
    return str(value or "default").strip() or "default"


def _hash_text(value: Any) -> str:
    return "sha256:" + hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _value(item: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in item and item[name] is not None:
            return item[name]
    return None


def _int_value(item: Mapping[str, Any], *names: str, default: int = 0) -> int:
    value = _value(item, *names)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def normalize_evidence_span(span: Mapping[str, Any] | None) -> Dict[str, Any] | None:
    """Normalize source-contract spans while accepting snake/camel aliases."""

    if not isinstance(span, Mapping):
        return None
    char_start = _value(span, "startChar", "start_char")
    char_end = _value(span, "endChar", "end_char")
    byte_start = _value(span, "startByte", "start_byte")
    byte_end = _value(span, "endByte", "end_byte")
    line_start = _value(span, "startLine", "start_line")
    line_end = _value(span, "endLine", "end_line")
    if char_start is None and byte_start is None and line_start is None:
        return None
    start_char = _int_value(span, "startChar", "start_char") if char_start is not None else None
    end_char = _int_value(span, "endChar", "end_char", default=start_char or 0) if char_end is not None else None
    start_byte = _int_value(span, "startByte", "start_byte") if byte_start is not None else None
    end_byte = _int_value(span, "endByte", "end_byte", default=start_byte or 0) if byte_end is not None else None
    start_line = _int_value(span, "startLine", "start_line", default=1) if line_start is not None else None
    end_line = _int_value(span, "endLine", "end_line", default=start_line or 1) if line_end is not None else None
    if start_char is not None and end_char is not None and end_char < start_char:
        return None
    if start_byte is not None and end_byte is not None and end_byte < start_byte:
        return None
    if start_line is not None and end_line is not None and end_line < start_line:
        return None
    result: Dict[str, Any] = {"endExclusive": True}
    if start_char is not None:
        result.update({"startChar": start_char, "endChar": end_char})
    if start_byte is not None:
        result.update({"startByte": start_byte, "endByte": end_byte})
    if start_line is not None:
        result.update({"startLine": max(1, start_line), "endLine": max(1, end_line or start_line)})
    revision = str(_value(span, "revision") or "").strip()
    if revision:
        try:
            result["revision"] = validate_source_revision(revision)
        except ValueError:
            return None
    return result


def _span_start(span: Mapping[str, Any]) -> int:
    return _int_value(span, "startChar", "startByte", "startLine")


def _span_end(span: Mapping[str, Any]) -> int:
    return _int_value(span, "endChar", "endByte", "endLine", default=_span_start(span))


def _spans_touch(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    coordinate_pairs = [("startChar", "endChar"), ("startByte", "endByte"), ("startLine", "endLine")]
    # Prefer the finest available coordinate. A shared line number must not
    # merge two disjoint character spans on that line.
    if "startChar" in left and "endChar" in left and "startChar" in right and "endChar" in right:
        coordinate_pairs = coordinate_pairs[:1]
    elif "startByte" in left and "endByte" in left and "startByte" in right and "endByte" in right:
        coordinate_pairs = coordinate_pairs[1:2]
    for start_name, end_name in coordinate_pairs:
        if start_name not in left or end_name not in left or start_name not in right:
            continue
        if _int_value(right, start_name) <= _int_value(left, end_name) + 1:
            return True
    return False


def merge_evidence_spans(spans: Iterable[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    """Merge overlapping or adjacent spans without crossing coordinate gaps."""

    normalized = [item for item in (normalize_evidence_span(span) for span in spans) if item]
    normalized.sort(key=lambda item: (_span_start(item), _span_end(item)))
    merged: list[Dict[str, Any]] = []
    for current in normalized:
        if not merged or not _spans_touch(merged[-1], current):
            merged.append(dict(current))
            continue
        target = merged[-1]
        for start_name, end_name in (("startChar", "endChar"), ("startByte", "endByte"), ("startLine", "endLine")):
            if start_name in target and start_name in current:
                target[start_name] = min(_int_value(target, start_name), _int_value(current, start_name))
            if end_name in target and end_name in current:
                target[end_name] = max(_int_value(target, end_name), _int_value(current, end_name))
        if current.get("revision") and not target.get("revision"):
            target["revision"] = current["revision"]
        target["endExclusive"] = True
    return merged


class EvidenceLedgerService:
    """A small atomic JSON ledger scoped to one canonical workspace/session."""

    def __init__(self, workspace_root: Path, session_id: str = "default") -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.session_id = _session_key(session_id)
        digest = hashlib.sha256(self.session_id.encode("utf-8")).hexdigest()[:24]
        self.path = self.workspace_root / LEDGER_RELATIVE_ROOT / f"{digest}.json"
        self._lock = threading.RLock()

    def _empty(self) -> Dict[str, Any]:
        return {
            "_type": "EvidenceLedger",
            "_version": LEDGER_SCHEMA_VERSION,
            "workspaceRoot": self.workspace_root.as_posix(),
            "sessionId": self.session_id,
            "updatedAt": _now(),
            "entries": [],
            "coverageGoals": [],
            "invalidations": [],
        }

    def _read(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            # A corrupt ledger must never break the Agent turn. Start a new
            # observable ledger and retain the corrupt file for diagnosis.
            return self._empty()
        if not isinstance(payload, dict):
            return self._empty()
        payload.setdefault("entries", [])
        payload.setdefault("coverageGoals", [])
        payload.setdefault("invalidations", [])
        return payload

    def _write(self, payload: Dict[str, Any]) -> None:
        payload["updatedAt"] = _now()
        _atomic_write(self.path, payload)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            payload = self._read()
            active = [
                dict(item)
                for item in payload.get("entries", [])
                if isinstance(item, dict) and item.get("active", True)
            ]
            goals = [dict(item) for item in payload.get("coverageGoals", []) if isinstance(item, dict)]
            return {
                "_type": "EvidenceLedgerSnapshot",
                "_version": LEDGER_SCHEMA_VERSION,
                "workspaceRoot": self.workspace_root.as_posix(),
                "sessionId": self.session_id,
                "path": self.path.as_posix(),
                "entries": active,
                "coverageGoals": goals,
                "invalidations": [
                    dict(item) for item in payload.get("invalidations", []) if isinstance(item, dict)
                ],
            }

    def record(
        self,
        *,
        path: str,
        revision: str,
        span: Mapping[str, Any],
        source_tool: str,
        result_hash: str = "",
        turn_id: str = "",
        coverage_goal: str = "",
        total: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        try:
            normalized_path = normalize_source_path(path)
            normalized_revision = validate_source_revision(revision)
        except ValueError:
            return {"recorded": False, "reason": "invalid_source_metadata"}
        normalized_span = normalize_evidence_span(span)
        if normalized_span is None:
            return {"recorded": False, "reason": "invalid_source_span", "path": normalized_path}
        normalized_span["revision"] = normalized_revision
        digest = str(result_hash or "").strip() or _hash_text(json.dumps(normalized_span, sort_keys=True))
        with self._lock:
            payload = self._read()
            invalidated: list[Dict[str, Any]] = []
            active_entries = [
                item
                for item in payload.get("entries", [])
                if isinstance(item, dict) and item.get("active", True)
            ]
            for item in active_entries:
                if str(item.get("path") or "") != normalized_path:
                    continue
                old_revision = str(item.get("revision") or "")
                if old_revision and old_revision != normalized_revision:
                    item["active"] = False
                    item["invalidatedAt"] = _now()
                    item["invalidatedByRevision"] = normalized_revision
                    invalidated.append({"path": normalized_path, "revision": old_revision})
            target = next(
                (
                    item
                    for item in active_entries
                    if item.get("active", True)
                    and str(item.get("path") or "") == normalized_path
                    and str(item.get("revision") or "") == normalized_revision
                ),
                None,
            )
            if target is None:
                target = {
                    "path": normalized_path,
                    "revision": normalized_revision,
                    "spans": [],
                    "sourceTools": [],
                    "resultHashes": [],
                    "firstObservedTurn": str(turn_id or ""),
                    "lastObservedTurn": str(turn_id or ""),
                    "coverageGoal": str(coverage_goal or ""),
                    "observedCount": 0,
                    "active": True,
                }
                payload.setdefault("entries", []).append(target)
            before_spans = list(target.get("spans") or [])
            merged = merge_evidence_spans([*before_spans, normalized_span])
            target["spans"] = merged
            target["sourceTools"] = sorted(
                set(str(item) for item in target.get("sourceTools", []) if str(item))
                | {str(source_tool or "unknown")}
            )
            hashes = set(str(item) for item in target.get("resultHashes", []) if str(item))
            hashes.add(digest)
            target["resultHashes"] = sorted(hashes)[-32:]
            target["firstObservedTurn"] = str(target.get("firstObservedTurn") or turn_id or "")
            target["lastObservedTurn"] = str(turn_id or target.get("lastObservedTurn") or "")
            target["coverageGoal"] = str(coverage_goal or target.get("coverageGoal") or "")
            target["observedCount"] = max(0, int(target.get("observedCount") or 0)) + 1
            if isinstance(total, Mapping):
                target["total"] = {
                    key: max(0, int(value or 0))
                    for key, value in total.items()
                    if key in {"chars", "bytes", "lines", "totalChars", "totalBytes", "totalLines"}
                    and value is not None
                }
            if invalidated:
                payload.setdefault("invalidations", []).extend(
                    {**item, "newRevision": normalized_revision, "at": _now()} for item in invalidated
                )
            self._write(payload)
            coverage = self.coverage_gate([normalized_path], revisions={normalized_path: normalized_revision})
            return {
                "recorded": True,
                "path": normalized_path,
                "revision": normalized_revision,
                "newSpans": max(0, len(merged) - len(before_spans)),
                "spanCount": len(merged),
                "spans": [dict(item) for item in merged],
                "invalidated": invalidated,
                "coverage": coverage,
            }

    def record_coverage_goal(
        self,
        *,
        paths: Sequence[str],
        revisions: Mapping[str, str] | None = None,
        goal: str,
        turn_id: str = "",
    ) -> Dict[str, Any]:
        normalized_paths = []
        for raw in paths:
            try:
                normalized_paths.append(normalize_source_path(str(raw)))
            except ValueError:
                continue
        normalized_paths = list(dict.fromkeys(normalized_paths))
        if not normalized_paths:
            return {"recorded": False, "reason": "no_candidate_paths"}
        with self._lock:
            payload = self._read()
            goals = payload.setdefault("coverageGoals", [])
            target = next(
                (
                    item
                    for item in goals
                    if isinstance(item, dict) and str(item.get("goal") or "") == str(goal or "")
                ),
                None,
            )
            if target is None:
                target = {
                    "goal": str(goal or "candidate_coverage"),
                    "paths": [],
                    "revisions": {},
                    "firstObservedTurn": turn_id,
                }
                goals.append(target)
            target["paths"] = sorted(set(target.get("paths", [])) | set(normalized_paths))
            revision_map = dict(target.get("revisions") or {})
            for path, revision in (revisions or {}).items():
                try:
                    revision_map[normalize_source_path(path)] = validate_source_revision(revision)
                except ValueError:
                    continue
            target["revisions"] = revision_map
            target["lastObservedTurn"] = str(turn_id or target.get("lastObservedTurn") or "")
            self._write(payload)
            return {"recorded": True, "goal": target["goal"], **self.coverage_gate(target["paths"], revisions=revision_map)}

    def coverage_gate(
        self,
        paths: Sequence[str],
        *,
        revisions: Mapping[str, str] | None = None,
    ) -> Dict[str, Any]:
        normalized_paths: list[str] = []
        for raw in paths:
            try:
                normalized_paths.append(normalize_source_path(str(raw)))
            except ValueError:
                continue
        active = self.snapshot().get("entries") or []
        by_path = {str(item.get("path")): item for item in active if isinstance(item, dict)}
        covered: list[str] = []
        missing: list[str] = []
        revision_mismatch: list[str] = []
        for path in dict.fromkeys(normalized_paths):
            item = by_path.get(path)
            expected = str((revisions or {}).get(path) or "")
            if item is None:
                missing.append(path)
                continue
            if expected and str(item.get("revision") or "") != expected:
                revision_mismatch.append(path)
                continue
            covered.append(path)
        return {
            "complete": bool(normalized_paths) and not missing and not revision_mismatch,
            "requestedPathCount": len(dict.fromkeys(normalized_paths)),
            "coveredPathCount": len(covered),
            "coveredPaths": covered,
            "missingPaths": missing,
            "revisionMismatchPaths": revision_mismatch,
        }

    def record_tool_result(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        raw_output: str,
        turn_id: str = "",
    ) -> Dict[str, Any]:
        payload = _decode_tool_output(raw_output)
        if not isinstance(payload, dict):
            return {"recorded": False, "reason": "non_json_tool_output"}
        records: list[Dict[str, Any]] = []
        result_hash = _hash_text(raw_output)

        def collect(item: Mapping[str, Any], *, source_tool: str) -> None:
            path = _value(item, "path", "relativePath", "relative_path", "sourcePath")
            revision = _value(item, "revision", "sourceRevision", "source_revision")
            span = _value(item, "snippetSpan", "sourceSpan", "source_span", "span")
            if path and revision and isinstance(span, Mapping):
                records.append(
                    self.record(
                        path=str(path),
                        revision=str(revision),
                        span=span,
                        source_tool=source_tool,
                        result_hash=result_hash,
                        turn_id=turn_id,
                        coverage_goal=str(_value(payload, "query") or ""),
                        total={
                            "totalChars": _value(item, "totalChars"),
                            "totalBytes": _value(item, "totalBytes"),
                            "totalLines": _value(item, "totalLines"),
                        },
                    )
                )

        collect(payload, source_tool=tool_name)
        nested = _value(payload, "results", "hits", "entries")
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, Mapping):
                    collect(item, source_tool=tool_name)
        candidate_paths = _value(payload, "candidatePaths", "candidate_paths")
        if isinstance(candidate_paths, list) and candidate_paths:
            revisions = {
                str(item.get("path")): str(item.get("revision"))
                for item in nested or []
                if isinstance(item, Mapping) and item.get("path") and item.get("revision")
            }
            goal = str(_value(payload, "query") or f"{tool_name}:candidate_coverage")
            coverage = self.record_coverage_goal(
                paths=[str(item) for item in candidate_paths],
                revisions=revisions,
                goal=goal,
                turn_id=turn_id,
            )
        else:
            argument_path = _value(arguments or {}, "path", "relativePath", "relative_path")
            coverage = self.coverage_gate([str(argument_path)] if argument_path else [])
        return {
            "recorded": any(bool(item.get("recorded")) for item in records),
            "observations": [item for item in records if isinstance(item, dict)],
            "observationCount": len(records),
            "coverage": coverage,
        }


def _decode_tool_output(raw_output: str) -> Any:
    text = str(raw_output or "").strip()
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        for prefix in ("success: ", "error: "):
            if text.startswith(prefix):
                try:
                    return json.loads(text[len(prefix) :])
                except (TypeError, ValueError, json.JSONDecodeError):
                    return None
    return None


_SERVICES: dict[tuple[str, str], EvidenceLedgerService] = {}
_SERVICES_LOCK = threading.Lock()


def get_evidence_ledger_service(
    workspace_root: Path,
    session_id: str = "default",
) -> EvidenceLedgerService:
    key = (Path(workspace_root).resolve().as_posix(), _session_key(session_id))
    with _SERVICES_LOCK:
        service = _SERVICES.get(key)
        if service is None:
            service = EvidenceLedgerService(Path(workspace_root), session_id)
            _SERVICES[key] = service
        return service


def reset_evidence_ledger_services() -> None:
    with _SERVICES_LOCK:
        _SERVICES.clear()
