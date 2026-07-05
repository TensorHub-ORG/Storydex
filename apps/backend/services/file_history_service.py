from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

from services.project_service import get_project_service


class FileHistoryService:
    def __init__(self) -> None:
        self.project_service = get_project_service()

    def backup_before_operations(self, operations: List[Dict[str, Any]], *, trace_id: str = "") -> List[Dict[str, Any]]:
        # WP-3.4 · ASYNC_FILE_BACKUP_ENABLED：preview 阶段不再 backup，
        # 由 backup_at_commit 在 commit 时统一触发。Flag Off 保留旧行为。
        from core.feature_flags import get_flags
        if get_flags().get_bool("ASYNC_FILE_BACKUP_ENABLED"):
            return []
        return self._do_backup(operations, trace_id=trace_id)

    def backup_at_commit(self, operations: List[Dict[str, Any]], *, trace_id: str = "") -> List[Dict[str, Any]]:
        """WP-3.4 · 在 commit 阶段执行 backup（与旧 backup_before_operations 行为一致）。

        始终执行，不受 Flag 控制——commit 阶段确保数据安全。
        """
        return self._do_backup(operations, trace_id=trace_id)

    def _do_backup(self, operations: List[Dict[str, Any]], *, trace_id: str = "") -> List[Dict[str, Any]]:
        workspace_root = self.project_service.workspace_root
        history_root = self.project_service.storydex_root / "file-history"
        history_root.mkdir(parents=True, exist_ok=True)

        backups: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            for relative_path in self._source_paths_for_operation(operation):
                normalized = self._normalize_relative_path(relative_path)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                path = (workspace_root / normalized).resolve()
                if workspace_root not in path.parents and path != workspace_root:
                    continue
                if not path.exists() or path.is_dir():
                    continue
                backups.append(self._write_backup(history_root=history_root, path=path, relative_path=normalized, trace_id=trace_id))
        return backups

    def _write_backup(self, *, history_root: Path, path: Path, relative_path: str, trace_id: str) -> Dict[str, Any]:
        data = path.read_bytes()
        backup_id = uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        safe_name = relative_path.replace("/", "__").replace("\\", "__")
        content_path = history_root / f"{backup_id}-{safe_name}.bak"
        meta_path = history_root / f"{backup_id}-{safe_name}.json"
        content_path.write_bytes(data)

        metadata = {
            "backupId": backup_id,
            "relativePath": relative_path,
            "backupPath": content_path.relative_to(self.project_service.workspace_root).as_posix(),
            "createdAt": created_at,
            "traceId": trace_id,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mtimeMs": int(path.stat().st_mtime * 1000),
        }
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return metadata

    @staticmethod
    def _source_paths_for_operation(operation: Dict[str, Any]) -> List[str]:
        op = str(operation.get("op") or "").strip().lower()
        if op == "rename":
            return [str(operation.get("fromRelativePath") or "")]
        return [str(operation.get("relativePath") or "")]

    @staticmethod
    def _normalize_relative_path(value: str) -> str:
        normalized = str(value or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized.lstrip("/")


_file_history_service = FileHistoryService()


def get_file_history_service() -> FileHistoryService:
    return _file_history_service
