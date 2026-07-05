from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Dict, List, Optional, Tuple

from core.exceptions import AtomicWriteError, InvalidWorkspacePathError


PROTECTED_STORYDEX_DIRECTORIES = frozenset(
    {
        ".storydex",
        ".storydex/characters",
        ".storydex/file-history",
        ".storydex/logs",
        ".storydex/memory",
        ".storydex/presets",
        ".storydex/regexs",
        ".storydex/scripts",
        ".storydex/sessions",
        ".storydex/templates",
        ".storydex/worldbook",
    }
)


class FileAdapter:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def resolve_path(self, relative_path: str) -> Path:
        candidate = (self.workspace_root / relative_path).resolve()
        if self.workspace_root not in candidate.parents and candidate != self.workspace_root:
            raise InvalidWorkspacePathError(f"Path is outside workspace: {relative_path}")
        return candidate

    @staticmethod
    def _normalize_relative_path(value: str) -> str:
        normalized = str(value or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.lstrip("/").rstrip("/")
        return normalized

    @classmethod
    def _is_protected_directory_path(cls, relative_path: str) -> bool:
        normalized = cls._normalize_relative_path(relative_path)
        if not normalized:
            return False
        return normalized in PROTECTED_STORYDEX_DIRECTORIES

    @classmethod
    def _is_forbidden_delete_target(cls, relative_path: str) -> bool:
        normalized = cls._normalize_relative_path(relative_path)
        if not normalized:
            return False
        if normalized in PROTECTED_STORYDEX_DIRECTORIES:
            return True
        # Prevent deleting a parent folder that would remove protected directories as descendants.
        return any(protected.startswith(normalized + "/") for protected in PROTECTED_STORYDEX_DIRECTORIES)

    def read_text(self, relative_path: str) -> str:
        path = self.resolve_path(relative_path)
        return path.read_text(encoding="utf-8")

    def file_metadata(self, relative_path: str) -> Dict[str, object]:
        path = self.resolve_path(relative_path)
        exists = path.exists()
        if not exists:
            return {
                "relativePath": self._normalize_relative_path(relative_path),
                "exists": False,
                "mtimeMs": None,
                "sha256": "",
                "size": 0,
            }
        if path.is_dir():
            return {
                "relativePath": self._normalize_relative_path(relative_path),
                "exists": True,
                "kind": "directory",
                "mtimeMs": int(path.stat().st_mtime * 1000),
                "sha256": "",
                "size": 0,
            }
        data = path.read_bytes()
        stat = path.stat()
        return {
            "relativePath": self._normalize_relative_path(relative_path),
            "exists": True,
            "kind": "file",
            "mtimeMs": int(stat.st_mtime * 1000),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": stat.st_size,
        }

    def write_text(self, relative_path: str, content: str) -> None:
        path = self.resolve_path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def write_bytes(self, relative_path: str, content: bytes) -> Dict[str, object]:
        normalized_relative_path = self._normalize_relative_path(relative_path)
        if not normalized_relative_path:
            raise AtomicWriteError("Workspace binary write operation received empty relativePath.")
        path = self.resolve_path(normalized_relative_path)
        if path.exists():
            raise AtomicWriteError(
                "Workspace binary write target already exists.",
                details={"relativePath": normalized_relative_path},
            )
        if path.parent.exists() and path.parent.is_file():
            raise AtomicWriteError(
                "Workspace binary write parent resolves to a file path.",
                details={"relativePath": normalized_relative_path},
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return self.file_metadata(normalized_relative_path)

    def import_file_bytes(self, target_directory: str, file_name: str, content: bytes) -> Dict[str, object]:
        normalized_directory = self._normalize_relative_path(target_directory)
        sanitized_name = self._sanitize_file_name(file_name)
        if not sanitized_name:
            raise AtomicWriteError("Workspace import file operation received empty file name.")

        directory_path = self.resolve_path(normalized_directory) if normalized_directory else self.workspace_root
        if directory_path.exists() and not directory_path.is_dir():
            raise AtomicWriteError(
                "Workspace import target is not a directory.",
                details={"targetDirectory": normalized_directory},
            )
        directory_path.mkdir(parents=True, exist_ok=True)
        target_relative_path = self._unique_child_relative_path(normalized_directory, sanitized_name)
        return self.write_bytes(target_relative_path, content)

    def create_file(self, relative_path: str, content: str = "") -> Dict[str, object]:
        normalized_relative_path = self._normalize_relative_path(relative_path)
        if not normalized_relative_path:
            raise AtomicWriteError("Workspace create file operation received empty relativePath.")
        path = self.resolve_path(normalized_relative_path)
        if path.exists():
            raise AtomicWriteError(
                "Workspace create file target already exists.",
                details={"relativePath": normalized_relative_path},
            )
        if path.parent.exists() and path.parent.is_file():
            raise AtomicWriteError(
                "Workspace create file parent resolves to a file path.",
                details={"relativePath": normalized_relative_path},
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return self.file_metadata(normalized_relative_path)

    @staticmethod
    def _sanitize_file_name(value: str) -> str:
        raw = str(value or "").strip().replace("\\", "/")
        leaf = next((part for part in reversed(raw.split("/")) if part), "")
        return "".join(char for char in leaf if char not in '<>:"|?*' and ord(char) >= 32).strip()

    def _unique_child_relative_path(self, parent_relative_path: str, file_name: str) -> str:
        normalized_parent = self._normalize_relative_path(parent_relative_path)
        leaf = self._sanitize_file_name(file_name)
        stem = Path(leaf).stem or leaf
        suffix = Path(leaf).suffix
        candidate_name = leaf
        attempt = 0
        while attempt < 500:
            candidate_relative_path = f"{normalized_parent}/{candidate_name}" if normalized_parent else candidate_name
            if not self.resolve_path(candidate_relative_path).exists():
                return candidate_relative_path
            attempt += 1
            candidate_name = f"{stem}-{attempt}{suffix}"
        timestamp = int(datetime.now(timezone.utc).timestamp())
        fallback_name = f"{stem}-{timestamp}{suffix}"
        return f"{normalized_parent}/{fallback_name}" if normalized_parent else fallback_name

    def create_directory(self, relative_path: str) -> Dict[str, object]:
        normalized_relative_path = self._normalize_relative_path(relative_path)
        if not normalized_relative_path:
            raise AtomicWriteError("Workspace create directory operation received empty relativePath.")
        path = self.resolve_path(normalized_relative_path)
        if path.exists():
            raise AtomicWriteError(
                "Workspace create directory target already exists.",
                details={"relativePath": normalized_relative_path},
            )
        if path.parent.exists() and path.parent.is_file():
            raise AtomicWriteError(
                "Workspace create directory parent resolves to a file path.",
                details={"relativePath": normalized_relative_path},
            )
        path.mkdir(parents=True, exist_ok=False)
        return self.file_metadata(normalized_relative_path)

    def rename_path(self, from_relative_path: str, to_relative_path: str) -> Dict[str, object]:
        normalized_from_relative_path = self._normalize_relative_path(from_relative_path)
        normalized_to_relative_path = self._normalize_relative_path(to_relative_path)
        if not normalized_from_relative_path or not normalized_to_relative_path:
            raise AtomicWriteError("Workspace rename operation requires both source and target paths.")
        if self._is_protected_directory_path(normalized_from_relative_path) or self._is_protected_directory_path(
            normalized_to_relative_path
        ):
            raise AtomicWriteError(
                "Renaming protected Storydex directories is forbidden.",
                details={
                    "fromRelativePath": normalized_from_relative_path,
                    "toRelativePath": normalized_to_relative_path,
                },
            )
        source_path = self.resolve_path(normalized_from_relative_path)
        target_path = self.resolve_path(normalized_to_relative_path)
        if not source_path.exists():
            raise AtomicWriteError(
                "Workspace rename source does not exist.",
                details={"fromRelativePath": normalized_from_relative_path},
            )
        if target_path.exists():
            raise AtomicWriteError(
                "Workspace rename target already exists.",
                details={"toRelativePath": normalized_to_relative_path},
            )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.rename(target_path)
        return self.file_metadata(normalized_to_relative_path)

    def delete_path(self, relative_path: str) -> None:
        normalized_relative_path = self._normalize_relative_path(relative_path)
        if not normalized_relative_path:
            raise AtomicWriteError("Workspace delete operation received empty relativePath.")
        if self._is_forbidden_delete_target(normalized_relative_path):
            raise AtomicWriteError(
                "Deleting protected Storydex directories is forbidden.",
                details={"relativePath": normalized_relative_path},
            )
        path = self.resolve_path(normalized_relative_path)
        if not path.exists():
            raise AtomicWriteError(
                "Workspace delete target does not exist.",
                details={"relativePath": normalized_relative_path},
            )
        if path.is_dir():
            shutil.rmtree(path)
            return
        path.unlink()

    def copy_path(self, from_relative_path: str, to_relative_path: str) -> Dict[str, object]:
        normalized_from_relative_path = self._normalize_relative_path(from_relative_path)
        normalized_to_relative_path = self._normalize_relative_path(to_relative_path)
        if not normalized_from_relative_path or not normalized_to_relative_path:
            raise AtomicWriteError("Workspace copy operation requires both source and target paths.")
        source_path = self.resolve_path(normalized_from_relative_path)
        target_path = self.resolve_path(normalized_to_relative_path)
        if not source_path.exists():
            raise AtomicWriteError(
                "Workspace copy source does not exist.",
                details={"fromRelativePath": normalized_from_relative_path},
            )
        if target_path.exists():
            raise AtomicWriteError(
                "Workspace copy target already exists.",
                details={"toRelativePath": normalized_to_relative_path},
            )
        try:
            target_path.relative_to(source_path)
            raise AtomicWriteError(
                "Workspace copy target cannot be nested inside its own source path.",
                details={
                    "fromRelativePath": normalized_from_relative_path,
                    "toRelativePath": normalized_to_relative_path,
                },
            )
        except ValueError:
            pass
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path.is_dir():
            shutil.copytree(source_path, target_path)
        else:
            shutil.copy2(source_path, target_path)
        return self.file_metadata(normalized_to_relative_path)

    def move_path(self, from_relative_path: str, to_relative_path: str) -> Dict[str, object]:
        return self.rename_path(from_relative_path, to_relative_path)

    def write_text_atomic(self, relative_path: str, content: str) -> None:
        self.write_many_atomic([{"relativePath": relative_path, "content": content}])

    def write_many_atomic(self, writes: List[Dict[str, str]]) -> None:
        if not writes:
            return

        resolved_paths: List[Path] = []
        content_map: Dict[Path, str] = {}
        original_bytes: Dict[Path, Optional[bytes]] = {}
        temp_paths: Dict[Path, Path] = {}
        replaced_paths: List[Path] = []

        try:
            for item in writes:
                relative_path = str(item.get("relativePath") or "").strip()
                if not relative_path:
                    raise AtomicWriteError("Atomic write received empty relativePath.")

                path = self.resolve_path(relative_path)
                resolved_paths.append(path)
                content_map[path] = str(item.get("content") or "")

            if len(set(resolved_paths)) != len(resolved_paths):
                raise AtomicWriteError("Atomic write payload contains duplicate target paths.")

            for path in resolved_paths:
                original_bytes[path] = path.read_bytes() if path.exists() else None
                temp_paths[path] = self._create_temp_file(path=path, content=content_map[path])

            for path in resolved_paths:
                os.replace(str(temp_paths[path]), str(path))
                replaced_paths.append(path)

        except Exception as exc:
            self._rollback_replaced_paths(replaced_paths=replaced_paths, original_bytes=original_bytes)
            raise AtomicWriteError(
                "Atomic write failed and rollback was applied.",
                details={
                    "targets": [path.as_posix() for path in resolved_paths],
                    "reason": str(exc),
                },
            ) from exc
        finally:
            for temp_path in temp_paths.values():
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except OSError:
                    pass

    def _create_temp_file(self, *, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, dir=str(path.parent)) as temp_file:
            temp_file.write(content)
            temp_path = Path(temp_file.name)
        return temp_path

    def _rollback_replaced_paths(
        self,
        *,
        replaced_paths: List[Path],
        original_bytes: Dict[Path, Optional[bytes]],
    ) -> None:
        for path in reversed(replaced_paths):
            original = original_bytes.get(path)
            try:
                if original is None:
                    if path.exists():
                        path.unlink()
                else:
                    path.write_bytes(original)
            except OSError:
                continue

    def apply_operations_atomic(self, operations: List[Dict[str, object]]) -> None:
        if not operations:
            return

        original_bytes: Dict[Path, Optional[bytes]] = {}
        touched_paths: List[Path] = []
        temp_paths: Dict[int, Path] = {}
        executed_paths: List[Path] = []

        try:
            normalized_operations: List[Tuple[int, str, Path, Optional[Path], str]] = []
            virtual_text: Dict[Path, str] = {}
            for index, item in enumerate(operations):
                op = str(item.get("op") or "write").strip().lower()
                if op == "write":
                    relative_path = str(item.get("relativePath") or "").strip()
                    if not relative_path:
                        raise AtomicWriteError("Workspace write operation received empty relativePath.")
                    normalized_relative_path = self._normalize_relative_path(relative_path)
                    if self._is_protected_directory_path(normalized_relative_path):
                        raise AtomicWriteError(
                            "Writing directly to protected Storydex directories is forbidden.",
                            details={"relativePath": normalized_relative_path},
                        )
                    path = self.resolve_path(relative_path)
                    if path.exists() and path.is_dir():
                        raise AtomicWriteError(
                            "Workspace write target resolves to a directory path.",
                            details={"relativePath": normalized_relative_path},
                        )
                    self._ensure_expected_file_state(path=path, operation=item)
                    content = str(item.get("content") or "")
                    virtual_text[path] = content
                    normalized_operations.append((index, op, path, None, content))
                    touched_paths.append(path)
                elif op == "append":
                    relative_path = str(item.get("relativePath") or "").strip()
                    if not relative_path:
                        raise AtomicWriteError("Workspace append operation received empty relativePath.")
                    normalized_relative_path = self._normalize_relative_path(relative_path)
                    if self._is_protected_directory_path(normalized_relative_path):
                        raise AtomicWriteError(
                            "Appending directly to protected Storydex directories is forbidden.",
                            details={"relativePath": normalized_relative_path},
                        )
                    path = self.resolve_path(relative_path)
                    if path.exists() and path.is_dir():
                        raise AtomicWriteError(
                            "Workspace append target resolves to a directory path.",
                            details={"relativePath": normalized_relative_path},
                        )
                    self._ensure_expected_file_state(path=path, operation=item)
                    before = virtual_text[path] if path in virtual_text else path.read_text(encoding="utf-8") if path.exists() else ""
                    after = self._append_text(before, str(item.get("content") or ""))
                    virtual_text[path] = after
                    normalized_operations.append((index, "write", path, None, after))
                    touched_paths.append(path)
                elif op == "edit":
                    relative_path = str(item.get("relativePath") or "").strip()
                    if not relative_path:
                        raise AtomicWriteError("Workspace edit operation received empty relativePath.")
                    path = self.resolve_path(relative_path)
                    self._ensure_editable_file(relative_path=relative_path, path=path)
                    self._ensure_expected_file_state(path=path, operation=item)
                    before = virtual_text[path] if path in virtual_text else path.read_text(encoding="utf-8")
                    after = self._apply_text_edit(
                        before,
                        old_string=str(item.get("oldString") or ""),
                        new_string=str(item.get("newString") or ""),
                        replace_all=bool(item.get("replaceAll", False)),
                    )
                    virtual_text[path] = after
                    normalized_operations.append((index, "write", path, None, after))
                    touched_paths.append(path)
                elif op == "multi_edit":
                    relative_path = str(item.get("relativePath") or "").strip()
                    if not relative_path:
                        raise AtomicWriteError("Workspace multi_edit operation received empty relativePath.")
                    path = self.resolve_path(relative_path)
                    self._ensure_editable_file(relative_path=relative_path, path=path)
                    self._ensure_expected_file_state(path=path, operation=item)
                    edits = item.get("edits")
                    if not isinstance(edits, list) or not edits:
                        raise AtomicWriteError(
                            "Workspace multi_edit operation requires a non-empty edits list.",
                            details={"relativePath": self._normalize_relative_path(relative_path)},
                        )
                    after = virtual_text[path] if path in virtual_text else path.read_text(encoding="utf-8")
                    for edit_index, edit in enumerate(edits, start=1):
                        if not isinstance(edit, dict):
                            raise AtomicWriteError(
                                "Workspace multi_edit entry must be an object.",
                                details={"relativePath": self._normalize_relative_path(relative_path), "editIndex": edit_index},
                            )
                        after = self._apply_text_edit(
                            after,
                            old_string=str(edit.get("oldString") or ""),
                            new_string=str(edit.get("newString") or ""),
                            replace_all=bool(edit.get("replaceAll", False)),
                            edit_index=edit_index,
                        )
                    virtual_text[path] = after
                    normalized_operations.append((index, "write", path, None, after))
                    touched_paths.append(path)
                elif op == "rename":
                    from_relative_path = str(item.get("fromRelativePath") or "").strip()
                    to_relative_path = str(item.get("toRelativePath") or "").strip()
                    if not from_relative_path or not to_relative_path:
                        raise AtomicWriteError("Workspace rename operation requires both fromRelativePath and toRelativePath.")
                    normalized_from_relative_path = self._normalize_relative_path(from_relative_path)
                    normalized_to_relative_path = self._normalize_relative_path(to_relative_path)
                    if self._is_protected_directory_path(normalized_from_relative_path) or self._is_protected_directory_path(
                        normalized_to_relative_path
                    ):
                        raise AtomicWriteError(
                            "Renaming protected Storydex directories is forbidden.",
                            details={
                                "fromRelativePath": normalized_from_relative_path,
                                "toRelativePath": normalized_to_relative_path,
                            },
                        )
                    from_path = self.resolve_path(from_relative_path)
                    to_path = self.resolve_path(to_relative_path)
                    self._ensure_expected_file_state(path=from_path, operation=item)
                    normalized_operations.append((index, op, from_path, to_path, ""))
                    touched_paths.extend([from_path, to_path])
                elif op == "delete":
                    relative_path = str(item.get("relativePath") or "").strip()
                    if not relative_path:
                        raise AtomicWriteError("Workspace delete operation received empty relativePath.")
                    normalized_relative_path = self._normalize_relative_path(relative_path)
                    if self._is_forbidden_delete_target(normalized_relative_path):
                        raise AtomicWriteError(
                            "Deleting protected Storydex directories is forbidden.",
                            details={"relativePath": normalized_relative_path},
                        )
                    path = self.resolve_path(relative_path)
                    if path.exists() and path.is_dir():
                        raise AtomicWriteError(
                            "Directory delete operation is not supported in atomic workspace mode.",
                            details={"relativePath": normalized_relative_path},
                        )
                    self._ensure_expected_file_state(path=path, operation=item)
                    virtual_text.pop(path, None)
                    normalized_operations.append((index, op, path, None, ""))
                    touched_paths.append(path)
                else:
                    raise AtomicWriteError(f"Unsupported workspace operation: {op}")

            for path in touched_paths:
                if path not in original_bytes:
                    original_bytes[path] = path.read_bytes() if path.exists() else None

            for index, op, path, _target, content in normalized_operations:
                if op == "write":
                    temp_paths[index] = self._create_temp_file(path=path, content=content)

            for index, op, path, target_path, _content in normalized_operations:
                if op == "write":
                    os.replace(str(temp_paths[index]), str(path))
                    executed_paths.append(path)
                    continue

                if op == "rename":
                    if not path.exists():
                        raise AtomicWriteError(
                            "Workspace rename source does not exist.",
                            details={"source": path.as_posix(), "target": target_path.as_posix() if target_path else ""},
                        )
                    if target_path is None:
                        raise AtomicWriteError("Workspace rename target is missing.")
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(str(path), str(target_path))
                    executed_paths.extend([path, target_path])
                    continue

                if op == "delete":
                    if path.exists():
                        path.unlink()
                        executed_paths.append(path)

        except Exception as exc:
            self._rollback_replaced_paths(replaced_paths=list(dict.fromkeys(executed_paths)), original_bytes=original_bytes)
            raise AtomicWriteError(
                "Atomic workspace operation failed and rollback was applied.",
                details={"reason": str(exc)},
            ) from exc
        finally:
            for temp_path in temp_paths.values():
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except OSError:
                    pass

    def _ensure_editable_file(self, *, relative_path: str, path: Path) -> None:
        normalized_relative_path = self._normalize_relative_path(relative_path)
        if self._is_protected_directory_path(normalized_relative_path):
            raise AtomicWriteError(
                "Editing protected Storydex directories is forbidden.",
                details={"relativePath": normalized_relative_path},
            )
        if not path.exists():
            raise AtomicWriteError(
                "Workspace edit target does not exist. Use a write operation to create a new file.",
                details={"relativePath": normalized_relative_path},
            )
        if path.is_dir():
            raise AtomicWriteError(
                "Workspace edit target resolves to a directory path.",
                details={"relativePath": normalized_relative_path},
            )

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
    def _apply_text_edit(
        content: str,
        *,
        old_string: str,
        new_string: str,
        replace_all: bool,
        edit_index: int = 1,
    ) -> str:
        if old_string == new_string:
            raise AtomicWriteError(
                "No changes to make: oldString and newString are exactly the same.",
                details={"editIndex": edit_index},
            )
        if old_string == "":
            raise AtomicWriteError(
                "Workspace edit operation requires a non-empty oldString.",
                details={"editIndex": edit_index},
            )

        count = content.count(old_string)
        if count == 0:
            raise AtomicWriteError(
                "Workspace edit oldString was not found in the target file.",
                details={"editIndex": edit_index},
            )
        if count > 1 and not replace_all:
            raise AtomicWriteError(
                "Workspace edit oldString is not unique. Set replaceAll=true or provide more surrounding context.",
                details={"editIndex": edit_index, "matchCount": count},
            )
        return content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)

    @staticmethod
    def _operation_value(operation: Dict[str, object], *names: str) -> object:
        for name in names:
            if name in operation:
                return operation.get(name)
        return None

    def _ensure_expected_file_state(self, *, path: Path, operation: Dict[str, object]) -> None:
        expected_exists = self._operation_value(operation, "expectedExists")
        expected_sha256 = str(self._operation_value(operation, "expectedSha256", "expectedHash") or "").strip()
        expected_mtime = self._operation_value(operation, "expectedMtimeMs", "expectedMtime")

        if expected_exists is not None and bool(expected_exists) != path.exists():
            raise AtomicWriteError(
                "Workspace file changed since preview: existence no longer matches.",
                details={"path": path.as_posix(), "expectedExists": bool(expected_exists), "exists": path.exists()},
            )

        if not path.exists():
            return
        if path.is_dir():
            return

        if expected_sha256:
            current_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            if current_sha256 != expected_sha256:
                raise AtomicWriteError(
                    "Workspace file changed since preview: content hash mismatch.",
                    details={"path": path.as_posix()},
                )

        if expected_mtime is not None:
            try:
                expected_mtime_ms = int(float(expected_mtime))
            except (TypeError, ValueError):
                expected_mtime_ms = -1
            current_mtime_ms = int(path.stat().st_mtime * 1000)
            # Windows and cloud-sync filesystems can round mtimes slightly.
            if expected_mtime_ms >= 0 and abs(current_mtime_ms - expected_mtime_ms) > 1000:
                raise AtomicWriteError(
                    "Workspace file changed since preview: mtime mismatch.",
                    details={"path": path.as_posix(), "expectedMtimeMs": expected_mtime_ms, "mtimeMs": current_mtime_ms},
                )
