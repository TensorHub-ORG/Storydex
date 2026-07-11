from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from services.project_service import get_project_service
from services.story_project_service import get_story_project_service

MAX_DIAGNOSTIC_FILE_BYTES = 512 * 1024


class DiagnosticsService:
    def __init__(self) -> None:
        self.project_service = get_project_service()
        self.story_project_service = get_story_project_service()

    def diagnose_paths(self, relative_paths: List[str]) -> List[Dict[str, Any]]:
        diagnostics: List[Dict[str, Any]] = []
        story_diagnostics = self.story_project_service.collect_story_diagnostics(self.project_service.workspace_root)
        for relative_path in relative_paths:
            normalized = self._normalize_relative_path(relative_path)
            if not normalized:
                continue
            path = (self.project_service.workspace_root / normalized).resolve()
            if self.project_service.workspace_root not in path.parents and path != self.project_service.workspace_root:
                continue
            diagnostics.extend(self._diagnose_path(path=path, relative_path=normalized))
            diagnostics.extend(self._diagnose_story_path(story_diagnostics=story_diagnostics, relative_path=normalized))
        return diagnostics

    def diagnose_workspace_operations(self, operations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        diagnostics: List[Dict[str, Any]] = []
        virtual_text: Dict[str, str] = {}

        for operation in operations:
            if not isinstance(operation, dict):
                continue
            op = str(operation.get("op") or "").strip().lower()
            relative_path = self._normalize_relative_path(operation.get("relativePath"))
            if not relative_path:
                continue
            if not self._is_safe_workspace_path(relative_path):
                continue

            before = virtual_text.get(relative_path)
            if before is None:
                before = self._read_optional_text(relative_path)

            after: str | None = None
            if op == "write":
                content = operation.get("content")
                after = content if isinstance(content, str) else None
            elif op == "append":
                content = operation.get("content")
                after = self._append_text(before, content) if isinstance(content, str) else None
            elif op == "edit":
                try:
                    after = self._apply_text_edit(
                        before,
                        old_string=str(operation.get("oldString") or ""),
                        new_string=str(operation.get("newString") or ""),
                        replace_all=bool(operation.get("replaceAll", False)),
                    )
                except ValueError:
                    after = None
            elif op == "multi_edit":
                edits = operation.get("edits")
                if isinstance(edits, list) and edits:
                    after = before
                    try:
                        for edit in edits:
                            if not isinstance(edit, dict):
                                raise ValueError("invalid edit")
                            after = self._apply_text_edit(
                                after,
                                old_string=str(edit.get("oldString") or ""),
                                new_string=str(edit.get("newString") or ""),
                                replace_all=bool(edit.get("replaceAll", False)),
                            )
                    except ValueError:
                        after = None
            elif op == "delete":
                virtual_text.pop(relative_path, None)
                continue

            if after is None:
                continue
            virtual_text[relative_path] = after
            diagnostics.extend(self._diagnose_text(content=after, relative_path=relative_path))

        return diagnostics

    def _diagnose_path(self, *, path: Path, relative_path: str) -> List[Dict[str, Any]]:
        if not path.exists() or path.is_dir():
            return []
        suffix = path.suffix.lower()
        if suffix in {".py", ".json", ".ipynb"} and self._is_too_large_for_deep_diagnostics(path):
            return [self._large_file_diagnostic(path=path, relative_path=relative_path)]
        if suffix == ".py":
            return self._diagnose_python(path=path, relative_path=relative_path)
        if suffix == ".json" or suffix == ".ipynb":
            diagnostics = self._diagnose_json(path=path, relative_path=relative_path)
            if not any(item.get("severity") == "error" for item in diagnostics):
                legacy = self._legacy_memory_diagnostic(path=path, relative_path=relative_path)
                if legacy:
                    diagnostics.append(legacy)
                if relative_path.replace("\\", "/") == ".storydex/memory/catalog.json":
                    diagnostics.extend(self._memory_catalog_diagnostics(path))
            return diagnostics
        return []

    @staticmethod
    def _legacy_memory_diagnostic(*, path: Path, relative_path: str) -> Dict[str, Any] | None:
        normalized = relative_path.replace("\\", "/")
        if not normalized.startswith(".storydex/memory/"):
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        version = payload.get("schemaVersion", payload.get("schema_version"))
        if version is not None or normalized.endswith("catalog.json"):
            return None
        return {
            "code": "story.memory.legacy_schema",
            "source": "story.memory",
            "severity": "info",
            "relativePath": normalized,
            "line": 1,
            "column": 1,
            "message": "该记忆文件使用旧格式，可在安全备份和冲突预览后迁移。",
            "evidence": "缺少 schemaVersion 字段",
            "fixes": [],
        }

    @staticmethod
    def _memory_catalog_diagnostics(path: Path) -> List[Dict[str, Any]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return []
        modules = payload.get("modules") if isinstance(payload, dict) and isinstance(payload.get("modules"), list) else []
        diagnostics: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_paths: set[str] = set()
        now = datetime.now(timezone.utc)
        for module in modules:
            if not isinstance(module, dict):
                continue
            module_id = str(module.get("id") or "").strip()
            module_path = str(module.get("path") or "").strip()
            if not module_id or not module_path or not module.get("purpose") or not module.get("schemaVersion"):
                diagnostics.append(DiagnosticsService._catalog_item("story.memory.module_incomplete", "warning", f"记忆模块 {module_id or module_path or '<unknown>'} 缺少用途、路径或schemaVersion。"))
            if module_id in seen_ids or module_path in seen_paths:
                diagnostics.append(DiagnosticsService._catalog_item("story.memory.module_duplicate", "warning", f"记忆模块 {module_id or module_path} 与现有模块重复，建议合并或归档。"))
            seen_ids.add(module_id)
            seen_paths.add(module_path)
            if not module.get("consumers"):
                diagnostics.append(DiagnosticsService._catalog_item("story.memory.module_unused", "warning", f"记忆模块 {module_id or module_path} 没有声明消费者。"))
            timestamp = str(module.get("lastReadAt") or module.get("updatedAt") or "").replace("Z", "+00:00")
            try:
                age = now - datetime.fromisoformat(timestamp)
            except (TypeError, ValueError):
                age = None
            if age is not None and age.days >= 90 and not module.get("lastReadAt"):
                diagnostics.append(DiagnosticsService._catalog_item("story.memory.module_stale", "info", f"记忆模块 {module_id or module_path} 长期没有读取记录，可考虑归档，但不会自动删除。"))
        return diagnostics

    @staticmethod
    def _catalog_item(code: str, severity: str, message: str) -> Dict[str, Any]:
        return {"code": code, "source": "story.memory", "severity": severity, "relativePath": ".storydex/memory/catalog.json", "line": 1, "column": 1, "message": message, "evidence": "memory catalog governance", "fixes": []}

    @staticmethod
    def _is_too_large_for_deep_diagnostics(path: Path) -> bool:
        try:
            return path.stat().st_size > MAX_DIAGNOSTIC_FILE_BYTES
        except OSError:
            return False

    @staticmethod
    def _large_file_diagnostic(*, path: Path, relative_path: str) -> Dict[str, Any]:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return {
            "source": "diagnostics.size_limit",
            "severity": "warning",
            "relativePath": relative_path,
            "line": 0,
            "column": 0,
            "message": (
                f"Skipped deep diagnostics for large file ({size} bytes); "
                f"limit is {MAX_DIAGNOSTIC_FILE_BYTES} bytes."
            ),
        }

    def _diagnose_python(self, *, path: Path, relative_path: str) -> List[Dict[str, Any]]:
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            return [
                {
                    "source": "python.ast",
                    "severity": "error",
                    "relativePath": relative_path,
                    "line": 0,
                    "column": 0,
                    "message": str(exc),
                }
            ]
        diagnostics = self._diagnose_python_content(content=content, relative_path=relative_path)
        if self._has_utf8_bom(path):
            diagnostics.insert(0, self._utf8_bom_diagnostic(relative_path))
        return diagnostics

    def _diagnose_python_content(self, *, content: str, relative_path: str) -> List[Dict[str, Any]]:
        try:
            ast.parse(content, filename=relative_path)
        except SyntaxError as exc:
            return [
                {
                    "source": "python.ast",
                    "severity": "error",
                    "relativePath": relative_path,
                    "line": int(exc.lineno or 0),
                    "column": int(exc.offset or 0),
                    "message": exc.msg,
                }
            ]
        return []

    def _diagnose_json(self, *, path: Path, relative_path: str) -> List[Dict[str, Any]]:
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            return [
                {
                    "code": "json.encoding_error",
                    "source": "json",
                    "severity": "error",
                    "relativePath": relative_path,
                    "line": 0,
                    "column": 0,
                    "message": str(exc),
                }
            ]
        diagnostics = self._diagnose_json_content(content=content, relative_path=relative_path)
        if self._has_utf8_bom(path):
            diagnostics.insert(0, self._utf8_bom_diagnostic(relative_path))
        return diagnostics

    @staticmethod
    def _has_utf8_bom(path: Path) -> bool:
        try:
            with path.open("rb") as handle:
                return handle.read(3) == b"\xef\xbb\xbf"
        except OSError:
            return False

    @staticmethod
    def _utf8_bom_diagnostic(relative_path: str) -> Dict[str, Any]:
        return {
            "code": "text.utf8_bom",
            "source": "encoding",
            "severity": "info",
            "relativePath": relative_path,
            "line": 1,
            "column": 1,
            "message": "检测到 UTF-8 BOM，已兼容读取；可移除 BOM 以统一项目编码。",
            "evidence": "文件以 EF BB BF 开头",
            "fixes": [{"id": "remove_utf8_bom", "label": "移除 BOM"}],
        }

    def apply_fix(self, *, relative_path: str, fix_id: str) -> Dict[str, Any]:
        normalized = self._normalize_relative_path(relative_path)
        if fix_id != "remove_utf8_bom" or not normalized or not self._is_safe_workspace_path(normalized):
            raise ValueError("Unsupported diagnostic fix.")
        path = (self.project_service.workspace_root / normalized).resolve()
        payload = path.read_bytes()
        changed = payload.startswith(b"\xef\xbb\xbf")
        if changed:
            temporary = path.with_name(f".{path.name}.storydex-bom-fix")
            temporary.write_bytes(payload[3:])
            temporary.replace(path)
        return {"relativePath": normalized, "fixId": fix_id, "changed": changed}

    def _diagnose_json_content(self, *, content: str, relative_path: str) -> List[Dict[str, Any]]:
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            return [
                {
                    "code": "json.parse_error",
                    "source": "json",
                    "severity": "error",
                    "relativePath": relative_path,
                    "line": int(exc.lineno),
                    "column": int(exc.colno),
                    "message": exc.msg,
                    "evidence": f"line {exc.lineno}, column {exc.colno}",
                    "fixes": [],
                }
            ]
        return []

    def _diagnose_text(self, *, content: str, relative_path: str) -> List[Dict[str, Any]]:
        suffix = Path(relative_path).suffix.lower()
        if suffix == ".py":
            return self._diagnose_python_content(content=content, relative_path=relative_path)
        if suffix == ".json" or suffix == ".ipynb":
            return self._diagnose_json_content(content=content, relative_path=relative_path)
        return []

    def _is_safe_workspace_path(self, relative_path: str) -> bool:
        root = self.project_service.workspace_root.resolve()
        path = (root / relative_path).resolve()
        return path == root or root in path.parents

    def _read_optional_text(self, relative_path: str) -> str:
        path = self.project_service.workspace_root / relative_path
        try:
            return path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return ""

    @staticmethod
    def _append_text(before: str, addition: str) -> str:
        if not before:
            return addition
        if not addition:
            return before
        if before.endswith("\n") or addition.startswith("\n"):
            return before + addition
        return before + "\n" + addition

    @staticmethod
    def _apply_text_edit(content: str, *, old_string: str, new_string: str, replace_all: bool) -> str:
        if not old_string or old_string == new_string:
            raise ValueError("invalid edit")
        count = content.count(old_string)
        if count == 0:
            raise ValueError("old string not found")
        if count > 1 and not replace_all:
            raise ValueError("old string is not unique")
        return content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)

    @staticmethod
    def _diagnose_story_path(*, story_diagnostics: Dict[str, List[Dict[str, Any]]], relative_path: str) -> List[Dict[str, Any]]:
        items = story_diagnostics.get(relative_path)
        if not isinstance(items, list):
            return []
        normalized_items: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            normalized_items.append(
                {
                    "code": str(item.get("code") or "story.memory"),
                    "source": str(item.get("source") or "story.memory"),
                    "severity": str(item.get("severity") or "warning"),
                    "relativePath": str(item.get("relativePath") or relative_path),
                    "line": int(item.get("line") or 0),
                    "column": int(item.get("column") or 0),
                    "message": str(item.get("message") or "").strip(),
                    "evidence": str(item.get("evidence") or "").strip(),
                    "fixes": item.get("fixes") if isinstance(item.get("fixes"), list) else [],
                }
            )
        return normalized_items

    @staticmethod
    def _normalize_relative_path(value: str) -> str:
        normalized = str(value or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized.lstrip("/")


_diagnostics_service = DiagnosticsService()


def get_diagnostics_service() -> DiagnosticsService:
    return _diagnostics_service
