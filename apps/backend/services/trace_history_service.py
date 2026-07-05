from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

from services.project_service import get_project_service


class TraceHistoryService:
    _SESSION_MARKER_NAME = "_session.json"

    def __init__(self) -> None:
        self.project_service = get_project_service()
        self._lock = Lock()
        # WP-3.2 · ASYNC_TRACE_ENABLED 时异步落盘队列在 _enqueue_async_drain 里使用
        self._async_handler_registered = False

    def get_session_root(self, session_id: str) -> Path:
        normalized = self._normalize_session_id(session_id)
        return self._session_root(normalized, create=True)

    def get_session_root_for_storydex_root(self, storydex_root: Path, session_id: str) -> Path:
        normalized = self._normalize_session_id(session_id)
        return self._session_root_at_storydex_root(storydex_root, normalized, create=True)

    def list_sessions(self) -> List[str]:
        return [item["sessionId"] for item in self.list_session_summaries()]

    def list_session_summaries(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._migrate_legacy_traces_locked()
            summaries: List[Dict[str, Any]] = []
            for session_id in self._collect_session_names():
                records = self._collect_session_records(session_id)
                if records:
                    summaries.append(self._build_session_summary(session_id, records))
                    continue
                marker = self._read_session_marker(session_id)
                if marker:
                    summaries.append(marker)

        summaries.sort(
            key=lambda item: (
                self._timestamp_value(str(item.get("updatedAt") or "")),
                self._timestamp_value(str(item.get("createdAt") or "")),
                str(item.get("sessionId") or ""),
            ),
            reverse=True,
        )
        return summaries

    def list_records(self, session_id: str = "default", limit: int = 40) -> List[Dict[str, Any]]:
        max_items = max(1, min(int(limit), 200))
        with self._lock:
            self._migrate_legacy_traces_locked()
            items = self._collect_session_records(session_id)

        items.sort(
            key=lambda item: (
                self._timestamp_value(str(item.get("updatedAt") or "")),
                self._timestamp_value(str(item.get("createdAt") or "")),
                str(item.get("traceId") or ""),
            ),
            reverse=True,
        )
        return items[:max_items]

    def read_record(self, trace_id: str, session_id: str = "default") -> Optional[Dict[str, Any]]:
        normalized_trace_id = str(trace_id or "").strip()
        if not normalized_trace_id:
            return None

        with self._lock:
            self._migrate_legacy_traces_locked()
            path = self._find_trace_file(normalized_trace_id, session_id)
            if path is None:
                return None
            return self._read_json(path)

    def clear_records(self, session_id: str = "default") -> int:
        normalized = self._normalize_session_id(session_id)
        removed = 0
        with self._lock:
            self._migrate_legacy_traces_locked()
            session_root = self._session_root(normalized)
            if session_root.exists():
                for path in list(session_root.glob("*.json")):
                    if path.name in {"log.json", self._SESSION_MARKER_NAME}:
                        continue
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        continue

            legacy_root = self._legacy_session_root(normalized)
            if legacy_root.exists():
                for path in list(legacy_root.glob("*.json")):
                    if path.name in {"log.json", self._SESSION_MARKER_NAME}:
                        continue
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        continue
        return removed

    def delete_session(self, session_id: str = "default") -> Dict[str, Any]:
        normalized = self._normalize_session_id(session_id)
        removed = 0
        with self._lock:
            self._migrate_legacy_traces_locked()

            for session_root in (self._session_root(normalized), self._legacy_session_root(normalized)):
                if not session_root.exists():
                    continue
                try:
                    shutil.rmtree(session_root)
                except OSError:
                    for path in list(session_root.glob("*.json")):
                        try:
                            path.unlink()
                            removed += 1
                        except OSError:
                            continue
                else:
                    removed += 1
        return {"deleted": True, "sessionId": normalized, "removedCount": removed}

    def mark_session_cleared(self, session_id: str = "default") -> Dict[str, Any]:
        normalized = self._normalize_session_id(session_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        marker = {
            "sessionId": normalized,
            "firstPrompt": "",
            "createdAt": now_iso,
            "updatedAt": now_iso,
            "traceCount": 0,
            "clearedAt": now_iso,
        }
        with self._lock:
            self._session_marker_path(normalized).write_text(
                json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return marker

    def was_session_cleared_after(self, session_id: str, started_at: str) -> bool:
        started_timestamp = self._timestamp_value(str(started_at or ""))
        if started_timestamp <= 0:
            return False
        with self._lock:
            marker = self._read_json(self._session_marker_path(session_id))
        if not marker:
            return False
        cleared_timestamp = self._timestamp_value(
            str(marker.get("clearedAt") or marker.get("updatedAt") or marker.get("createdAt") or "")
        )
        return cleared_timestamp >= started_timestamp

    def upsert_record(self, record: Dict[str, Any], session_id: str = "default") -> Dict[str, Any]:
        trace_id = str(record.get("traceId") or "").strip()
        if not trace_id:
            return {}

        # WP-3.2 · ASYNC_TRACE_ENABLED：把落盘推到 JobQueue worker，主路径不阻塞 lock。
        # 主路径仍然返回 merged record（dict 内存合并），仅 IO 异步化。
        from core.feature_flags import get_flags
        if get_flags().get_bool("ASYNC_TRACE_ENABLED"):
            return self._upsert_record_async(record=record, session_id=session_id, trace_id=trace_id)

        return self._upsert_record_sync(record=record, session_id=session_id, trace_id=trace_id)

    def _upsert_record_sync(self, *, record: Dict[str, Any], session_id: str, trace_id: str) -> Dict[str, Any]:
        now_iso = datetime.now(timezone.utc).isoformat()
        normalized_session_id = self._normalize_session_id(session_id)
        with self._lock:
            self._migrate_legacy_traces_locked()
            existing_path = self._find_trace_file(trace_id, session_id)
            existing_record = self._read_json(existing_path) if existing_path is not None else {}

            merged = dict(existing_record)
            merged.update(record)
            merged["traceId"] = trace_id
            merged["sessionId"] = normalized_session_id
            merged["createdAt"] = str(record.get("createdAt") or existing_record.get("createdAt") or now_iso)
            merged["updatedAt"] = str(record.get("updatedAt") or now_iso)

            target_path = existing_path or self._build_trace_path(
                trace_id=trace_id,
                created_at=str(merged["createdAt"]),
                session_id=session_id,
            )
            target_path.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return merged

    def _upsert_record_sync_at_storydex_root(
        self,
        *,
        storydex_root: Path,
        record: Dict[str, Any],
        session_id: str,
        trace_id: str,
    ) -> Dict[str, Any]:
        now_iso = datetime.now(timezone.utc).isoformat()
        normalized_session_id = self._normalize_session_id(session_id)
        with self._lock:
            for traces_root in self._trace_roots_at_storydex_root(storydex_root):
                self._migrate_trace_root_locked(traces_root, Path(storydex_root).resolve())
            existing_path = self._find_trace_file_at_storydex_root(
                storydex_root=storydex_root,
                trace_id=trace_id,
                session_id=session_id,
            )
            existing_record = self._read_json(existing_path) if existing_path is not None else {}

            merged = dict(existing_record)
            merged.update(record)
            merged["traceId"] = trace_id
            merged["sessionId"] = normalized_session_id
            merged["createdAt"] = str(record.get("createdAt") or existing_record.get("createdAt") or now_iso)
            merged["updatedAt"] = str(record.get("updatedAt") or now_iso)

            target_path = existing_path or self._build_trace_path_at_storydex_root(
                storydex_root=storydex_root,
                trace_id=trace_id,
                created_at=str(merged["createdAt"]),
                session_id=session_id,
            )
            target_path.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return merged

    def _upsert_record_async(self, *, record: Dict[str, Any], session_id: str, trace_id: str) -> Dict[str, Any]:
        """WP-3.2 异步落盘：推 JobQueue；handler 仍走 _upsert_record_sync。"""
        from services.job_queue import get_default_queue

        now_iso = datetime.now(timezone.utc).isoformat()
        normalized_session_id = self._normalize_session_id(session_id)
        merged = dict(record)
        merged["traceId"] = trace_id
        merged["sessionId"] = normalized_session_id
        merged["createdAt"] = str(record.get("createdAt") or now_iso)
        merged["updatedAt"] = str(record.get("updatedAt") or now_iso)

        queue = get_default_queue()
        if not self._async_handler_registered:
            queue.register_handler("trace_upsert", self._async_trace_handler)
            self._async_handler_registered = True
        queue.enqueue(
            kind="trace_upsert",
            payload={
                "record": merged,
                "session_id": session_id,
                "trace_id": trace_id,
                "storydex_root": self.project_service.storydex_root.as_posix(),
            },
            dedup_key=f"trace::{session_id}::{trace_id}",
        )
        return merged

    def _async_trace_handler(self, payload: Dict[str, Any]) -> None:
        """JobQueue handler：在 worker 上下文里同步落盘。"""
        record = payload.get("record") or {}
        session_id = str(payload.get("session_id") or "default")
        trace_id = str(payload.get("trace_id") or record.get("traceId") or "")
        if not trace_id:
            return
        storydex_root = str(payload.get("storydex_root") or "").strip()
        if storydex_root:
            self._upsert_record_sync_at_storydex_root(
                storydex_root=Path(storydex_root),
                record=record,
                session_id=session_id,
                trace_id=trace_id,
            )
            return
        self._upsert_record_sync(record=record, session_id=session_id, trace_id=trace_id)

    def _primary_sessions_root(self) -> Path:
        root = self.project_service.storydex_root / ".agent" / "sessions"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _session_root(self, normalized_session_id: str, *, create: bool = False) -> Path:
        return self._resolve_session_root(
            self._primary_sessions_root(),
            normalized_session_id,
            create=create,
        )

    def _session_root_at_storydex_root(
        self,
        storydex_root: Path,
        normalized_session_id: str,
        *,
        create: bool = False,
    ) -> Path:
        base = Path(storydex_root).resolve() / ".agent" / "sessions"
        return self._resolve_session_root(base, normalized_session_id, create=create)

    @classmethod
    def _resolve_session_root(cls, base: Path, normalized_session_id: str, *, create: bool = False) -> Path:
        base = Path(base).resolve()
        if create:
            base.mkdir(parents=True, exist_ok=True)
        if cls._session_id_needs_safe_directory(normalized_session_id):
            path = base / cls._safe_session_directory_name(normalized_session_id)
        else:
            path = (base / normalized_session_id).resolve()
            try:
                path.relative_to(base)
            except ValueError:
                path = base / cls._safe_session_directory_name(normalized_session_id)
        if create:
            path.mkdir(parents=True, exist_ok=True)
            readme = path / "README.md"
            if not readme.exists():
                readme.write_text("# 会话记录\n\n存放该 Agent 会话的记录和状态。\n", encoding="utf-8")
        return path

    @staticmethod
    def _session_id_needs_safe_directory(session_id: str) -> bool:
        normalized = str(session_id or "").strip()
        if normalized in {".", ".."}:
            return True
        path = Path(normalized)
        return path.is_absolute() or any(part == ".." for part in path.parts)

    @staticmethod
    def _safe_session_directory_name(session_id: str) -> str:
        digest = sha256(str(session_id or "default").encode("utf-8")).hexdigest()[:16]
        return f"_session_{digest}"

    def _primary_traces_root(self) -> Path:
        return self.project_service.storydex_root / "trace"

    def _legacy_traces_root(self) -> Path:
        return self.project_service.storydex_root / "traces"

    def _legacy_session_root(self, normalized_session_id: str) -> Path:
        return self._resolve_session_root(
            self.project_service.storydex_root / "sessions",
            normalized_session_id,
            create=False,
        )

    @staticmethod
    def _trace_roots_at_storydex_root(storydex_root: Path) -> List[Path]:
        root = Path(storydex_root).resolve()
        return [root / "trace", root / "traces"]

    def _migrate_legacy_traces_locked(self) -> None:
        for traces_root in self._trace_roots_at_storydex_root(self.project_service.storydex_root):
            self._migrate_trace_root_locked(traces_root, self.project_service.storydex_root)

    def _migrate_trace_root_locked(self, traces_root: Path, storydex_root: Path) -> None:
        if not traces_root.exists() or not traces_root.is_dir():
            return
        for path in sorted(traces_root.rglob("*.json")):
            if not path.is_file():
                continue
            payload = self._read_json(path)
            trace_id = str(payload.get("traceId") or path.stem).strip()
            if not trace_id:
                continue
            session_id = self._record_session_id(payload)
            created_at = str(payload.get("createdAt") or payload.get("updatedAt") or "")
            target_path = self._build_trace_path_at_storydex_root(
                storydex_root=storydex_root,
                trace_id=trace_id,
                created_at=created_at,
                session_id=session_id,
            )
            existing = self._read_json(target_path)
            if not existing or self._should_replace_record(existing, payload):
                target_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            try:
                path.unlink()
            except OSError:
                continue
        try:
            shutil.rmtree(traces_root)
        except OSError:
            pass

    def _date_dir(self, date_str: str = "") -> Path:
        if not date_str:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        date_dir = self._primary_traces_root() / date_str
        date_dir.mkdir(parents=True, exist_ok=True)
        readme = date_dir / "README.md"
        if not readme.exists():
            readme.write_text("# Trace 日期分组\n\n存放本日期的 Agent 执行审计记录。\n", encoding="utf-8")
        return date_dir

    @staticmethod
    def _safe_date(created_at: str) -> str:
        try:
            parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now(timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _normalize_session_id(session_id: str) -> str:
        return str(session_id or "default").strip() or "default"

    def _collect_session_names(self) -> List[str]:
        names: List[str] = []
        seen: set[str] = set()
        root = self._primary_sessions_root()
        if root.exists():
            for entry in root.iterdir():
                if not entry.is_dir():
                    continue
                if entry.name in seen:
                    continue
                seen.add(entry.name)
                names.append(entry.name)
        return names

    def _collect_session_records(self, session_id: str) -> List[Dict[str, Any]]:
        records_by_trace_id: Dict[str, Dict[str, Any]] = {}
        normalized = self._normalize_session_id(session_id)

        for legacy_root in (self._session_root(normalized), self._legacy_session_root(normalized)):
            if legacy_root.exists():
                for path in legacy_root.glob("*.json"):
                    if path.name in {"log.json", self._SESSION_MARKER_NAME}:
                        continue
                    payload = self._read_json(path)
                    if not payload:
                        continue
                    trace_id = str(payload.get("traceId") or path.stem).strip()
                    if not trace_id:
                        continue
                    previous = records_by_trace_id.get(trace_id)
                    if previous is None or self._should_replace_record(previous, payload):
                        records_by_trace_id[trace_id] = payload

        return list(records_by_trace_id.values())

    def _build_session_summary(self, session_id: str, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        sorted_by_created = sorted(
            records,
            key=lambda item: (
                self._timestamp_value(str(item.get("createdAt") or item.get("updatedAt") or "")),
                str(item.get("traceId") or ""),
            ),
        )
        sorted_by_updated = sorted(
            records,
            key=lambda item: (
                self._timestamp_value(str(item.get("updatedAt") or item.get("createdAt") or "")),
                str(item.get("traceId") or ""),
            ),
            reverse=True,
        )

        first_prompt = ""
        for item in sorted_by_created:
            prompt = str(item.get("prompt") or "").strip()
            if prompt:
                first_prompt = prompt
                break

        first_record = sorted_by_created[0] if sorted_by_created else {}
        latest_record = sorted_by_updated[0] if sorted_by_updated else {}
        created_at = str(first_record.get("createdAt") or first_record.get("updatedAt") or "")
        updated_at = str(latest_record.get("updatedAt") or latest_record.get("createdAt") or created_at)

        return {
            "sessionId": session_id,
            "firstPrompt": first_prompt,
            "createdAt": created_at,
            "updatedAt": updated_at,
            "traceCount": len(records),
        }

    def _find_trace_file(self, trace_id: str, session_id: str) -> Optional[Path]:
        normalized = self._normalize_session_id(session_id)
        for session_root in (self._session_root(normalized), self._legacy_session_root(normalized)):
            if session_root.exists():
                exact = session_root / f"{trace_id}.json"
                if exact.exists():
                    return exact
                for candidate in session_root.glob(f"*_{trace_id}.json"):
                    return candidate

        for traces_root in (self._primary_traces_root(), self._legacy_traces_root()):
            if not traces_root.exists():
                continue
            for date_dir in sorted(traces_root.iterdir(), reverse=True):
                if not date_dir.is_dir():
                    continue
                exact = date_dir / f"{trace_id}.json"
                if exact.exists() and self._record_belongs_to_session(self._read_json(exact), normalized):
                    return exact
                for candidate in date_dir.glob(f"*_{trace_id}.json"):
                    if self._record_belongs_to_session(self._read_json(candidate), normalized):
                        return candidate

        return None

    def _session_marker_path(self, session_id: str) -> Path:
        return self.get_session_root(session_id) / self._SESSION_MARKER_NAME

    def _read_session_marker(self, session_id: str) -> Dict[str, Any]:
        marker = self._read_json(self._session_marker_path(session_id))
        if not marker:
            return {}
        normalized = self._normalize_session_id(session_id)
        return {
            "sessionId": normalized,
            "firstPrompt": str(marker.get("firstPrompt") or ""),
            "createdAt": str(marker.get("createdAt") or marker.get("updatedAt") or ""),
            "updatedAt": str(marker.get("updatedAt") or marker.get("createdAt") or ""),
            "traceCount": 0,
        }

    def _find_trace_file_at_storydex_root(self, *, storydex_root: Path, trace_id: str, session_id: str) -> Optional[Path]:
        normalized = self._normalize_session_id(session_id)
        for session_root in (
            self._session_root_at_storydex_root(storydex_root, normalized, create=False),
            self._resolve_session_root(Path(storydex_root).resolve() / "sessions", normalized, create=False),
        ):
            if not session_root.exists():
                continue
            exact = session_root / f"{trace_id}.json"
            if exact.exists():
                return exact

            for candidate in session_root.glob(f"*_{trace_id}.json"):
                return candidate

        for traces_root in self._trace_roots_at_storydex_root(storydex_root):
            if not traces_root.exists():
                continue
            for date_dir in sorted(traces_root.iterdir(), reverse=True):
                if not date_dir.is_dir():
                    continue
                exact = date_dir / f"{trace_id}.json"
                if exact.exists() and self._record_belongs_to_session(self._read_json(exact), normalized):
                    return exact
                for candidate in date_dir.glob(f"*_{trace_id}.json"):
                    if self._record_belongs_to_session(self._read_json(candidate), normalized):
                        return candidate
        return None

    def _build_trace_path(self, *, trace_id: str, created_at: str, session_id: str) -> Path:
        timestamp = self._safe_timestamp(created_at)
        normalized = self._normalize_session_id(session_id)
        return self._session_root(normalized, create=True) / f"{timestamp}_{trace_id}.json"

    def _build_trace_path_at_storydex_root(
        self,
        *,
        storydex_root: Path,
        trace_id: str,
        created_at: str,
        session_id: str,
    ) -> Path:
        timestamp = self._safe_timestamp(created_at)
        normalized = self._normalize_session_id(session_id)
        target_dir = self._session_root_at_storydex_root(storydex_root, normalized, create=True)
        return target_dir / f"{timestamp}_{trace_id}.json"

    @staticmethod
    def _safe_timestamp(created_at: str) -> str:
        try:
            parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now(timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    @staticmethod
    def _read_json(path: Optional[Path]) -> Dict[str, Any]:
        if path is None or not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _record_session_id(cls, payload: Dict[str, Any]) -> str:
        return cls._normalize_session_id(str(payload.get("sessionId") or payload.get("session_id") or "default"))

    @classmethod
    def _record_belongs_to_session(cls, payload: Dict[str, Any], normalized_session_id: str) -> bool:
        return cls._record_session_id(payload) == normalized_session_id

    @staticmethod
    def _timestamp_value(value: str) -> float:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, AttributeError):
            return 0.0

    def _should_replace_record(self, previous: Dict[str, Any], incoming: Dict[str, Any]) -> bool:
        previous_updated_at = str(previous.get("updatedAt") or previous.get("createdAt") or "")
        incoming_updated_at = str(incoming.get("updatedAt") or incoming.get("createdAt") or "")
        if self._timestamp_value(incoming_updated_at) != self._timestamp_value(previous_updated_at):
            return self._timestamp_value(incoming_updated_at) > self._timestamp_value(previous_updated_at)
        return bool(incoming.get("prompt")) and not bool(previous.get("prompt"))


_trace_history_service = TraceHistoryService()


def get_trace_history_service() -> TraceHistoryService:
    return _trace_history_service
