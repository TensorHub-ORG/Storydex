from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.project_service import get_project_service
from services.file_history_service import get_file_history_service
from services.hooks_service import get_hooks_service
from services.media_reader import MediaReader
from services.story_project_service import get_story_project_service
from services.story_word_count_service import count_story_text_words
from storage.file_adapter import FileAdapter
from core.bounded_text_io import MAX_FULL_READ_BYTES


_CHAPTER_NUMBER_RE = re.compile(r"第\s*([0-9零〇一二两三四五六七八九十百千万]+)\s*章", re.IGNORECASE)
_NATURAL_PART_RE = re.compile(r"(\d+)")
_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000}


def _natural_story_name_key(name: str) -> tuple:
    text = str(name or "").casefold()
    chapter_number = _extract_chapter_number(text)
    natural_parts = tuple(
        _natural_part_key(part)
        for part in _NATURAL_PART_RE.split(text)
        if part != ""
    )
    if chapter_number is not None:
        return (0, chapter_number, natural_parts)
    return (1, natural_parts)


def _natural_part_key(part: str) -> tuple[int, int | str]:
    return (0, int(part)) if part.isdigit() else (1, part)


def _extract_chapter_number(text: str) -> int | None:
    match = _CHAPTER_NUMBER_RE.search(text)
    if not match:
        return None
    raw = match.group(1)
    if raw.isdigit():
        return int(raw)
    parsed = _parse_chinese_number(raw)
    return parsed if parsed > 0 else None


def _parse_chinese_number(value: str) -> int:
    total = 0
    section = 0
    number = 0
    for char in value:
        if char in _CHINESE_DIGITS:
            number = _CHINESE_DIGITS[char]
            continue
        if char in _CHINESE_UNITS:
            unit = _CHINESE_UNITS[char]
            section += (number or 1) * unit
            number = 0
            continue
        if char == "万":
            section = (section + number) * 10000
            total += section
            section = 0
            number = 0
    return total + section + number


class WorkspaceIO:
    def __init__(self) -> None:
        self.project_service = get_project_service()
        self.media_reader = MediaReader()
        self.story_project_service = get_story_project_service()

    @property
    def workspace_root(self) -> Path:
        return self.project_service.workspace_root

    @property
    def storydex_root(self) -> Path:
        return self.project_service.storydex_root

    @property
    def file_adapter(self) -> FileAdapter:
        return FileAdapter(self.workspace_root)

    def init_workspace_dirs(self) -> None:
        self.project_service.ensure_project_structure(self.workspace_root)

    def read_text(self, relative_path: str) -> str:
        return self.file_adapter.read_text(relative_path)

    def resolve_existing_relative_path(self, relative_path: str) -> str:
        normalized = FileAdapter._normalize_relative_path(relative_path)
        if not normalized:
            return ""

        adapter = self.file_adapter
        try:
            if adapter.resolve_path(normalized).exists():
                return normalized
        except Exception:
            return normalized

        parts = Path(normalized).parts
        if len(parts) < 2 or parts[0] != "chapters":
            return normalized

        rest = Path(*parts[2:]).as_posix() if len(parts) > 2 else ""
        chapter_hint = parts[1]
        try:
            chapter_states = self.story_project_service.list_chapter_states(self.workspace_root)
        except Exception:
            chapter_states = []

        chapter_number = 0
        extract_number = getattr(self.story_project_service, "_extract_chapter_number", None)
        if callable(extract_number):
            try:
                chapter_number = int(extract_number(chapter_hint) or 0)
            except Exception:
                chapter_number = 0

        ordered_chapters = []
        if chapter_number:
            ordered_chapters.extend(
                item for item in chapter_states if int(getattr(item, "chapter_number", 0) or 0) == chapter_number
            )
        ordered_chapters.extend(
            item
            for item in chapter_states
            if all(getattr(existing, "relative_path", "") != getattr(item, "relative_path", "") for existing in ordered_chapters)
        )

        for chapter in ordered_chapters:
            chapter_relative = str(getattr(chapter, "relative_path", "") or "").strip()
            if not chapter_relative:
                continue
            candidate = f"{chapter_relative}/{rest}" if rest else chapter_relative
            try:
                if adapter.resolve_path(candidate).exists():
                    return candidate
            except Exception:
                continue

        if rest:
            chapters_root = self.workspace_root / "chapters"
            try:
                matches = [
                    item.relative_to(self.workspace_root).as_posix()
                    for item in chapters_root.glob(f"*/{rest}")
                    if item.exists()
                ]
            except Exception:
                matches = []
            if len(matches) == 1:
                return matches[0]

        return normalized

    def read_document(self, relative_path: str, *, offset: Optional[int] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        path = self.file_adapter.resolve_path(relative_path)
        if path.is_dir():
            return self._build_directory_document(path=path, limit=limit)
        if offset is None and limit is None:
            special = self.media_reader.read_special_document(path, workspace_root=self.workspace_root)
            if special is not None:
                return special
        if limit is not None and self._should_stream_limited_document(path):
            try:
                return self._build_limited_file_document(path=path, offset=offset, limit=limit)
            except UnicodeDecodeError:
                return self._build_unsupported_document(path=path, message="Unable to display this file.")
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            return self._build_unsupported_document(path=path, message="此文件无法展示")
        return self._build_file_document(path=path, content=content, offset=offset, limit=limit)

    def write_text(self, relative_path: str, content: str) -> Dict[str, Any]:
        self.file_adapter.write_text(relative_path, content)
        return self.describe_path(relative_path)

    def create_file(self, relative_path: str, content: str = "") -> Dict[str, Any]:
        self.file_adapter.create_file(relative_path, content)
        return self.describe_path(relative_path)

    def create_directory(self, relative_path: str) -> Dict[str, Any]:
        return self.file_adapter.create_directory(relative_path)

    def rename_path(self, from_relative_path: str, to_relative_path: str) -> Dict[str, Any]:
        return self.file_adapter.rename_path(from_relative_path, to_relative_path)

    def delete_path(self, relative_path: str) -> Dict[str, Any]:
        metadata = self.file_adapter.file_metadata(relative_path)
        self.file_adapter.delete_path(relative_path)
        return metadata

    def copy_path(self, from_relative_path: str, to_relative_path: str) -> Dict[str, Any]:
        return self.file_adapter.copy_path(from_relative_path, to_relative_path)

    def move_path(self, from_relative_path: str, to_relative_path: str) -> Dict[str, Any]:
        return self.file_adapter.move_path(from_relative_path, to_relative_path)

    def describe_path(self, relative_path: str) -> Dict[str, Any]:
        path = self.file_adapter.resolve_path(relative_path)
        content = path.read_text(encoding="utf-8-sig")
        return self._build_file_document(path=path, content=content)

    def list_workspace_tree(self) -> Dict[str, Any]:
        roots: List[Dict[str, Any]] = []
        project_info = self.project_service.current_project()
        tree_meta = self.story_project_service.build_tree_meta(self.workspace_root)
        for root in self._iter_workspace_roots():
            if root.exists():
                roots.append(self._build_tree_node(root, tree_meta=tree_meta))

        return {
            "workspaceRoot": self.workspace_root.as_posix(),
            "storydexRoot": self.storydex_root.as_posix(),
            "projectName": project_info["projectName"],
            "hasStorydexConfig": project_info["hasStorydexConfig"],
            "requiresInitialization": project_info["requiresInitialization"],
            "missingDirectories": project_info["missingDirectories"],
            "openedAt": project_info["openedAt"],
            "defaultFile": self.find_default_file(),
            "roots": roots,
        }

    def write_story_and_snapshot_atomic(
        self,
        *,
        segment_relative_path: str,
        segment_content: str,
        snapshot_relative_path: str,
        snapshot_payload: Dict[str, Any],
        trace_id: str = "",
    ) -> None:
        snapshot_content = json.dumps(snapshot_payload, ensure_ascii=False, indent=2) + "\n"
        operations = [
            {"op": "write", "relativePath": segment_relative_path, "content": segment_content},
            {"op": "write", "relativePath": snapshot_relative_path, "content": snapshot_content},
        ]
        self._run_pre_write_ecosystem(operations=operations, trace_id=trace_id)
        self.file_adapter.write_many_atomic(
            [
                {"relativePath": segment_relative_path, "content": segment_content},
                {"relativePath": snapshot_relative_path, "content": snapshot_content},
            ]
        )
        if snapshot_relative_path:
            self.story_project_service.sync_current_state_from_snapshot_payload(
                self.workspace_root,
                snapshot_relative_path,
                snapshot_payload,
            )
        self._run_post_write_hooks(operations=operations, trace_id=trace_id)

    def apply_workspace_operations_atomic(self, operations: List[Dict[str, Any]], *, trace_id: str = "") -> None:
        self._run_pre_write_ecosystem(operations=operations, trace_id=trace_id)
        self.file_adapter.apply_operations_atomic(operations)
        self._run_post_write_hooks(operations=operations, trace_id=trace_id)

    def _run_pre_write_ecosystem(self, *, operations: List[Dict[str, Any]], trace_id: str) -> None:
        payload = self._build_hook_payload(operations=operations, trace_id=trace_id)
        pre_results = get_hooks_service().run("preWorkspaceWrite", payload)
        blocking = [item for item in pre_results if str(item.get("status") or "") not in {"ok"}]
        if blocking:
            from core.exceptions import AtomicWriteError

            raise AtomicWriteError(
                "Pre-write hook rejected workspace changes.",
                details={"hooks": blocking},
            )
        get_file_history_service().backup_before_operations(operations, trace_id=trace_id)

    def _run_post_write_hooks(self, *, operations: List[Dict[str, Any]], trace_id: str) -> None:
        payload = self._build_hook_payload(operations=operations, trace_id=trace_id)
        get_hooks_service().run("postWorkspaceWrite", payload)

    def _build_hook_payload(self, *, operations: List[Dict[str, Any]], trace_id: str) -> Dict[str, Any]:
        return {
            "traceId": trace_id,
            "workspaceRoot": self.workspace_root.as_posix(),
            "storydexRoot": self.storydex_root.as_posix(),
            "operations": [
                {
                    key: value
                    for key, value in dict(operation).items()
                    if key not in {"content", "oldString", "newString"}
                }
                for operation in operations
                if isinstance(operation, dict)
            ],
        }

    def preview_workspace_operations(self, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        preview_items: List[Dict[str, Any]] = []
        target_paths: List[str] = []
        virtual_text: Dict[str, str] = {}

        for index, operation in enumerate(operations, start=1):
            if not isinstance(operation, dict):
                continue
            operation.setdefault("id", self._build_operation_id(index=index, operation=operation))
            preview = self._build_operation_preview(index=index, operation=operation, virtual_text=virtual_text)
            if not preview:
                continue
            op = str(preview.get("op") or "").strip().lower()
            relative_path = str(preview.get("relativePath") or "")
            after_text_raw = str(preview.pop("afterTextRaw", ""))
            if op in {"write", "append", "edit", "multi_edit"} and relative_path:
                virtual_text[relative_path] = after_text_raw
            elif op == "delete" and relative_path:
                virtual_text.pop(relative_path, None)
            preview_items.append(preview)
            for candidate in (
                str(preview.get("relativePath") or ""),
                str(preview.get("fromRelativePath") or ""),
                str(preview.get("toRelativePath") or ""),
            ):
                if candidate and candidate not in target_paths:
                    target_paths.append(candidate)

        if not preview_items:
            return {}

        primary_item = preview_items[0]
        summary = self._build_preview_summary(preview_items)
        return {
            "relativePath": primary_item.get("relativePath", ""),
            "exists": bool(primary_item.get("existsBefore", False)),
            "changeType": primary_item.get("changeType", "update"),
            "lineCount": int(primary_item.get("lineCountAfter", 0) or 0),
            "contentPreview": str(primary_item.get("afterText") or primary_item.get("beforeText") or ""),
            "targetPaths": target_paths,
            "summary": summary,
            "totalOperations": len(preview_items),
            "items": preview_items,
        }

    def attach_operation_guards(self, operations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        guarded: List[Dict[str, Any]] = []
        for index, operation in enumerate(operations, start=1):
            if not isinstance(operation, dict):
                continue
            next_operation = dict(operation)
            next_operation.setdefault("id", self._build_operation_id(index=index, operation=next_operation))
            op = str(next_operation.get("op") or "").strip().lower()
            relative_path = ""
            if op == "rename":
                relative_path = str(next_operation.get("fromRelativePath") or "").strip()
            else:
                relative_path = str(next_operation.get("relativePath") or "").strip()

            if relative_path:
                try:
                    metadata = self.file_adapter.file_metadata(relative_path)
                except Exception:
                    metadata = {}
                if metadata:
                    next_operation.setdefault("expectedExists", bool(metadata.get("exists", False)))
                    if metadata.get("exists") and metadata.get("kind", "file") == "file":
                        next_operation.setdefault("expectedMtimeMs", metadata.get("mtimeMs"))
                        next_operation.setdefault("expectedSha256", metadata.get("sha256"))
            guarded.append(next_operation)
        return guarded

    def read_worldbook(self, name: str) -> str:
        path = self.storydex_root / "worldbook" / f"{name}.md"
        return self.read_text(path.relative_to(self.workspace_root).as_posix())

    def find_default_file(self) -> Optional[str]:
        for root in self._iter_workspace_roots():
            if not root.exists():
                continue
            for candidate in root.rglob("*"):
                if candidate.is_file() and self._is_default_file_candidate(candidate):
                    return candidate.relative_to(self.workspace_root).as_posix()
        return None

    def _iter_workspace_roots(self) -> List[Path]:
        return [
            child
            for child in self._sorted_children(self.workspace_root)
            if self._include_child(child)
        ]

    def _build_tree_node(self, path: Path, *, tree_meta: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
        relative_path = path.relative_to(self.workspace_root).as_posix() if path != self.workspace_root else None
        meta = tree_meta.get(relative_path, {}) if relative_path and isinstance(tree_meta, dict) else {}
        if path.is_dir():
            children = [
                self._build_tree_node(child, tree_meta=tree_meta)
                for child in self._sorted_children(path)
                if self._include_child(child)
            ]
            node = {
                "name": path.name,
                "relativePath": relative_path,
                "kind": "directory",
                "children": children,
            }
            if isinstance(meta.get("story"), dict):
                node["story"] = meta["story"]
            if isinstance(meta.get("diagnostics"), list):
                node["diagnostics"] = meta["diagnostics"]
            return node

        stat = path.stat()
        node = {
            "name": path.name,
            "relativePath": relative_path,
            "kind": "file",
            "extension": path.suffix.lower(),
            "size": stat.st_size,
            "updatedAt": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "children": [],
        }
        if isinstance(meta.get("story"), dict):
            node["story"] = meta["story"]
        if isinstance(meta.get("diagnostics"), list):
            node["diagnostics"] = meta["diagnostics"]
        return node

    @staticmethod
    def _sorted_children(path: Path) -> List[Path]:
        return sorted(
            list(path.iterdir()),
            key=lambda item: (item.is_file(), _natural_story_name_key(item.name)),
        )

    def _include_child(self, path: Path) -> bool:
        if path.is_dir():
            return path.name not in {"__pycache__", ".pytest_cache", "node_modules", ".git"}
        return self._include_file(path)

    @staticmethod
    def _include_file(path: Path) -> bool:
        hidden_names = {"Thumbs.db", ".DS_Store", "desktop.ini"}
        return path.name not in hidden_names

    @staticmethod
    def _is_default_file_candidate(path: Path) -> bool:
        preferred_names = {".env", ".gitignore", "README", "README.md"}
        preferred_suffixes = {
            ".md",
            ".json",
            ".txt",
            ".yaml",
            ".yml",
            ".py",
            ".ts",
            ".tsx",
            ".vue",
            ".js",
            ".css",
            ".html",
            ".bat",
        }
        return path.name in preferred_names or path.suffix.lower() in preferred_suffixes

    @staticmethod
    def _should_stream_limited_document(path: Path) -> bool:
        try:
            return path.stat().st_size > MAX_FULL_READ_BYTES
        except OSError:
            return False

    def _build_limited_file_document(
        self,
        *,
        path: Path,
        offset: Optional[int],
        limit: int,
    ) -> Dict[str, Any]:
        stat = path.stat()
        normalized_offset = max(0, int(offset)) if offset is not None else 0
        normalized_limit = max(1, int(limit))
        selected_lines, observed_lines, has_more = self._read_line_window(
            path,
            offset=normalized_offset,
            limit=normalized_limit,
        )
        selected_content = "\n".join(selected_lines)
        if selected_lines and has_more:
            selected_content += "\n"
        is_partial = normalized_offset > 0 or has_more
        return {
            "relativePath": path.relative_to(self.workspace_root).as_posix(),
            "content": selected_content,
            "fullContentSha256": "",
            "mtimeMs": int(stat.st_mtime * 1000),
            "size": stat.st_size,
            "wordCount": self._count_story_text_words(selected_content),
            "updatedAt": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "extension": path.suffix.lower(),
            "kind": "file",
            "lineCount": observed_lines,
            "lineCountExact": not has_more,
            "offset": normalized_offset if is_partial else None,
            "limit": normalized_limit if is_partial else None,
            "isPartialView": is_partial,
        }

    @classmethod
    def _read_line_window(cls, path: Path, *, offset: int, limit: int) -> tuple[list[str], int, bool]:
        selected: list[str] = []
        observed_lines = 0
        has_more = False
        with path.open("r", encoding="utf-8-sig") as handle:
            for raw_line in handle:
                line_index = observed_lines
                observed_lines += 1
                if line_index < offset:
                    continue
                if len(selected) < limit:
                    selected.append(cls._strip_line_ending(raw_line))
                    continue
                has_more = True
                break
        return selected, observed_lines, has_more

    @staticmethod
    def _strip_line_ending(line: str) -> str:
        return line.rstrip("\n").rstrip("\r")

    def _build_file_document(
        self,
        *,
        path: Path,
        content: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        stat = path.stat()
        lines = content.splitlines()
        normalized_offset = max(0, int(offset)) if offset is not None else 0
        normalized_limit = max(1, int(limit)) if limit is not None else None
        selected_lines = lines[normalized_offset:]
        if normalized_limit is not None:
            selected_lines = selected_lines[:normalized_limit]
        selected_content = "\n".join(selected_lines)
        if content.endswith("\n") and selected_lines and (normalized_limit is None or normalized_offset + len(selected_lines) < len(lines)):
            selected_content += "\n"
        is_partial = normalized_offset > 0 or (normalized_limit is not None and normalized_offset + len(selected_lines) < len(lines))
        return {
            "relativePath": path.relative_to(self.workspace_root).as_posix(),
            "content": selected_content if is_partial else content,
            "fullContentSha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "mtimeMs": int(stat.st_mtime * 1000),
            "size": stat.st_size,
            "wordCount": self._count_story_text_words(content),
            "updatedAt": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "extension": path.suffix.lower(),
            "kind": "file",
            "lineCount": len(lines),
            "lineCountExact": True,
            "offset": normalized_offset if is_partial else None,
            "limit": normalized_limit if is_partial else None,
            "isPartialView": is_partial,
        }

    def _build_directory_document(self, *, path: Path, limit: Optional[int] = None) -> Dict[str, Any]:
        stat = path.stat()
        normalized_limit = max(1, int(limit)) if limit is not None else 200
        children = [
            child
            for child in self._sorted_children(path)
            if self._include_child(child)
        ]
        visible_children = children[:normalized_limit]
        lines = [
            f"- {'dir' if child.is_dir() else 'file'} {child.name}"
            for child in visible_children
        ]
        if len(children) > len(visible_children):
            lines.append(f"... {len(children) - len(visible_children)} more item(s)")
        content = "\n".join(lines)
        if content:
            content += "\n"
        return {
            "relativePath": path.relative_to(self.workspace_root).as_posix(),
            "content": content,
            "fullContentSha256": "",
            "mtimeMs": int(stat.st_mtime * 1000),
            "size": 0,
            "wordCount": 0,
            "updatedAt": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "extension": "",
            "kind": "directory",
            "lineCount": len(lines),
            "offset": None,
            "limit": normalized_limit if len(children) > normalized_limit else None,
            "isPartialView": len(children) > normalized_limit,
            "childCount": len(children),
            "children": [
                {
                    "name": child.name,
                    "relativePath": child.relative_to(self.workspace_root).as_posix(),
                    "kind": "directory" if child.is_dir() else "file",
                }
                for child in visible_children
            ],
        }

    def _build_unsupported_document(self, *, path: Path, message: str) -> Dict[str, Any]:
        stat = path.stat()
        return {
            "relativePath": path.relative_to(self.workspace_root).as_posix(),
            "content": "",
            "fullContentSha256": "",
            "mtimeMs": int(stat.st_mtime * 1000),
            "size": stat.st_size,
            "wordCount": 0,
            "updatedAt": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "extension": path.suffix.lower(),
            "kind": "file",
            "lineCount": 0,
            "offset": None,
            "limit": None,
            "isPartialView": False,
            "media": {
                "previewUnsupported": True,
                "message": message,
            },
        }

    @staticmethod
    def _count_story_text_words(content: str) -> int:
        return count_story_text_words(content)

    def _build_operation_preview(
        self,
        *,
        index: int,
        operation: Dict[str, Any],
        virtual_text: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        op = str(operation.get("op") or "write").strip().lower()
        operation_id = str(operation.get("id") or self._build_operation_id(index=index, operation=operation))
        reason = str(operation.get("reason") or "").strip()
        virtual = virtual_text if isinstance(virtual_text, dict) else {}
        if op == "write":
            relative_path = str(operation.get("relativePath") or "").strip()
            if not relative_path:
                return {}
            path = self.file_adapter.resolve_path(relative_path)
            before_text = virtual[relative_path] if relative_path in virtual else self._read_optional_text(path)
            before_exists = path.exists()
            after_text = str(operation.get("content") or "")
            return self._compose_preview_item(
                index=index,
                op=op,
                relative_path=relative_path,
                before_exists=before_exists,
                after_exists=True,
                before_text=before_text,
                after_text=after_text,
                reason=reason,
                change_type="update" if before_exists else "create",
                operation_id=operation_id,
            )

        if op == "append":
            relative_path = str(operation.get("relativePath") or "").strip()
            if not relative_path:
                return {}
            path = self.file_adapter.resolve_path(relative_path)
            before_text = virtual[relative_path] if relative_path in virtual else self._read_optional_text(path)
            before_exists = path.exists()
            after_text = self._append_text(before_text, str(operation.get("content") or ""))
            return self._compose_preview_item(
                index=index,
                op=op,
                relative_path=relative_path,
                before_exists=before_exists,
                after_exists=True,
                before_text=before_text,
                after_text=after_text,
                reason=reason,
                change_type="append" if before_exists else "create",
                operation_id=operation_id,
            )

        if op == "edit":
            relative_path = str(operation.get("relativePath") or "").strip()
            if not relative_path:
                return {}
            path = self.file_adapter.resolve_path(relative_path)
            before_text = virtual[relative_path] if relative_path in virtual else self._read_optional_text(path)
            before_exists = path.exists()
            try:
                after_text = self._apply_preview_edit(
                    before_text,
                    old_string=str(operation.get("oldString") or ""),
                    new_string=str(operation.get("newString") or ""),
                    replace_all=bool(operation.get("replaceAll", False)),
                )
            except ValueError:
                return {}
            return self._compose_preview_item(
                index=index,
                op=op,
                relative_path=relative_path,
                before_exists=before_exists,
                after_exists=True,
                before_text=before_text,
                after_text=after_text,
                reason=reason,
                change_type="edit",
                operation_id=operation_id,
            )

        if op == "multi_edit":
            relative_path = str(operation.get("relativePath") or "").strip()
            edits = operation.get("edits")
            if not relative_path or not isinstance(edits, list) or not edits:
                return {}
            path = self.file_adapter.resolve_path(relative_path)
            before_text = virtual[relative_path] if relative_path in virtual else self._read_optional_text(path)
            before_exists = path.exists()
            after_text = before_text
            try:
                for edit in edits:
                    if not isinstance(edit, dict):
                        return {}
                    after_text = self._apply_preview_edit(
                        after_text,
                        old_string=str(edit.get("oldString") or ""),
                        new_string=str(edit.get("newString") or ""),
                        replace_all=bool(edit.get("replaceAll", False)),
                    )
            except ValueError:
                return {}
            return self._compose_preview_item(
                index=index,
                op=op,
                relative_path=relative_path,
                before_exists=before_exists,
                after_exists=True,
                before_text=before_text,
                after_text=after_text,
                reason=reason,
                change_type="multi_edit",
                operation_id=operation_id,
            )

        if op == "rename":
            from_relative_path = str(operation.get("fromRelativePath") or "").strip()
            to_relative_path = str(operation.get("toRelativePath") or "").strip()
            if not from_relative_path or not to_relative_path:
                return {}
            from_path = self.file_adapter.resolve_path(from_relative_path)
            before_text = self._read_optional_text(from_path)
            before_exists = from_path.exists()
            return self._compose_preview_item(
                index=index,
                op=op,
                relative_path=to_relative_path,
                before_exists=before_exists,
                after_exists=before_exists,
                before_text=before_text,
                after_text=before_text,
                reason=reason,
                change_type="rename",
                from_relative_path=from_relative_path,
                to_relative_path=to_relative_path,
                operation_id=operation_id,
            )

        if op == "delete":
            relative_path = str(operation.get("relativePath") or "").strip()
            if not relative_path:
                return {}
            path = self.file_adapter.resolve_path(relative_path)
            before_text = virtual[relative_path] if relative_path in virtual else self._read_optional_text(path)
            before_exists = path.exists()
            return self._compose_preview_item(
                index=index,
                op=op,
                relative_path=relative_path,
                before_exists=before_exists,
                after_exists=False,
                before_text=before_text,
                after_text="",
                reason=reason,
                change_type="delete",
                operation_id=operation_id,
            )

        return {}

    def _compose_preview_item(
        self,
        *,
        operation_id: str,
        index: int,
        op: str,
        relative_path: str,
        before_exists: bool,
        after_exists: bool,
        before_text: str,
        after_text: str,
        reason: str,
        change_type: str,
        from_relative_path: str = "",
        to_relative_path: str = "",
    ) -> Dict[str, Any]:
        capped_before_text, before_truncated = self._cap_preview_text(before_text)
        capped_after_text, after_truncated = self._cap_preview_text(after_text)
        diff_text, diff_truncated = self._build_diff_text(
            before_text=before_text,
            after_text=after_text,
            relative_path=relative_path,
            change_type=change_type,
            from_relative_path=from_relative_path,
            to_relative_path=to_relative_path,
        )
        added_lines, removed_lines = self._count_line_delta(before_text=before_text, after_text=after_text)
        return {
            "id": operation_id or f"{op}:{index:02d}:{relative_path}",
            "op": op,
            "relativePath": relative_path,
            "fromRelativePath": from_relative_path,
            "toRelativePath": to_relative_path,
            "reason": reason,
            "changeType": change_type,
            "existsBefore": before_exists,
            "existsAfter": after_exists,
            "lineCountBefore": len(before_text.splitlines()) if before_text else 0,
            "lineCountAfter": len(after_text.splitlines()) if after_text else 0,
            "beforeText": capped_before_text,
            "afterText": capped_after_text,
            "afterTextRaw": after_text,
            "diffText": diff_text,
            "truncated": before_truncated or after_truncated or diff_truncated,
            "stats": {
                "added": added_lines,
                "removed": removed_lines,
            },
        }

    @staticmethod
    def _build_operation_id(*, index: int, operation: Dict[str, Any]) -> str:
        op = str(operation.get("op") or "write").strip().lower() or "write"
        if op == "rename":
            target = str(operation.get("toRelativePath") or operation.get("fromRelativePath") or "").strip()
        else:
            target = str(operation.get("relativePath") or "").strip()
        return f"{op}:{index:02d}:{target}"

    @staticmethod
    def _read_optional_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
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
    def _apply_preview_edit(content: str, *, old_string: str, new_string: str, replace_all: bool) -> str:
        if not old_string or old_string == new_string:
            raise ValueError("invalid edit")
        count = content.count(old_string)
        if count == 0:
            raise ValueError("old string not found")
        if count > 1 and not replace_all:
            raise ValueError("old string is not unique")
        return content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)

    @staticmethod
    def _count_line_delta(*, before_text: str, after_text: str) -> tuple[int, int]:
        before_lines = before_text.splitlines()
        after_lines = after_text.splitlines()
        matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines)
        added = 0
        removed = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in {"replace", "delete"}:
                removed += max(0, i2 - i1)
            if tag in {"replace", "insert"}:
                added += max(0, j2 - j1)
        return added, removed

    @staticmethod
    def _cap_preview_text(content: str, *, max_chars: int = 10000) -> tuple[str, bool]:
        if len(content) <= max_chars:
            return content, False
        return content[: max_chars - 53] + "\n\n[Preview truncated by Storydex]", True

    @staticmethod
    def _build_diff_text(
        *,
        before_text: str,
        after_text: str,
        relative_path: str,
        change_type: str,
        from_relative_path: str = "",
        to_relative_path: str = "",
        max_lines: int = 240,
    ) -> tuple[str, bool]:
        if change_type == "rename":
            return (
                "\n".join(
                    [
                        f"rename: {from_relative_path}",
                        f"     -> {to_relative_path}",
                    ]
                ),
                False,
            )

        diff_lines = list(
            difflib.unified_diff(
                before_text.splitlines(),
                after_text.splitlines(),
                fromfile=relative_path if before_text else "/dev/null",
                tofile=relative_path if after_text else "/dev/null",
                lineterm="",
            )
        )
        if len(diff_lines) <= max_lines:
            return "\n".join(diff_lines), False
        truncated = diff_lines[:max_lines]
        truncated.append("... diff truncated by Storydex ...")
        return "\n".join(truncated), True

    @staticmethod
    def _build_preview_summary(preview_items: List[Dict[str, Any]]) -> str:
        parts: List[str] = []
        for item in preview_items:
            change_type = str(item.get("changeType") or "update")
            relative_path = str(item.get("relativePath") or "")
            if relative_path:
                parts.append(f"{change_type}:{relative_path}")
        return ", ".join(parts[:6])
