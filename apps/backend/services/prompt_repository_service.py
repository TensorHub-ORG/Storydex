from __future__ import annotations

import os
import re
import json
import tempfile
from uuid import uuid4
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List

from services.global_config_service import get_global_config_service


_PROMPT_BLOCK_RE = re.compile(r"```(?:prompt|text)\s*\n(?P<body>.*?)```", re.IGNORECASE | re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"\[[^\]\r\n]{1,40}\]")


class PromptRepositoryService:
    DIRECTORY_NAME = "prompts"
    CUSTOM_CATEGORY = "自定义"

    def __init__(self, *, custom_repository_path: Path | None = None) -> None:
        self._custom_repository_path = custom_repository_path
        self._custom_lock = Lock()

    def read_repository(self, *, query: str = "", category: str = "") -> Dict[str, Any]:
        root = self._resolve_root()
        all_items = [*self._read_items(root), *self._read_custom_items()]
        normalized_query = str(query or "").strip().lower()
        normalized_category = str(category or "").strip()

        items = [
            item
            for item in all_items
            if (not normalized_category or str(item.get("category") or "") == normalized_category)
            and (
                not normalized_query
                or normalized_query
                in "\n".join(
                    [
                        str(item.get("title") or ""),
                        str(item.get("summary") or ""),
                        str(item.get("category") or ""),
                        str(item.get("content") or ""),
                    ]
                ).lower()
            )
        ]

        category_counts: Dict[str, int] = {}
        for item in all_items:
            name = str(item.get("category") or "通用")
            category_counts[name] = category_counts.get(name, 0) + 1
        category_counts.setdefault(self.CUSTOM_CATEGORY, 0)

        categories = [
            {"id": name, "label": name, "count": count}
            for name, count in sorted(category_counts.items(), key=lambda entry: entry[0])
        ]
        return {
            "root": root.as_posix() if root else "",
            "query": str(query or "").strip(),
            "category": normalized_category,
            "categories": categories,
            "items": items,
        }

    def create_custom_prompt(self, *, title: str, prompt_text: str) -> Dict[str, Any]:
        normalized_title = self._validate_title(title)
        normalized_prompt = self._validate_prompt_text(prompt_text)
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "id": uuid4().hex,
            "title": normalized_title,
            "promptText": normalized_prompt,
            "createdAt": now,
            "updatedAt": now,
        }
        with self._custom_lock:
            payload = self._read_custom_payload()
            records = payload.get("items") if isinstance(payload.get("items"), list) else []
            records.append(record)
            self._write_custom_payload({"version": 1, "items": records})
        return self._custom_item(record)

    def update_custom_prompt(self, prompt_id: str, *, prompt_text: str) -> Dict[str, Any]:
        normalized_id = str(prompt_id or "").strip()
        normalized_prompt = self._validate_prompt_text(prompt_text)
        if not re.fullmatch(r"[a-f0-9]{32}", normalized_id):
            raise ValueError("Invalid custom prompt id.")
        with self._custom_lock:
            payload = self._read_custom_payload()
            records = payload.get("items") if isinstance(payload.get("items"), list) else []
            updated: Dict[str, Any] | None = None
            for record in records:
                if not isinstance(record, dict) or str(record.get("id") or "") != normalized_id:
                    continue
                # Titles are intentionally immutable after creation. Only the body
                # of a custom prompt may be edited by this operation.
                record["promptText"] = normalized_prompt
                record["updatedAt"] = datetime.now(timezone.utc).isoformat()
                updated = record
                break
            if updated is None:
                raise LookupError("Custom prompt does not exist.")
            self._write_custom_payload({"version": 1, "items": records})
        return self._custom_item(updated)

    def _resolve_root(self) -> Path | None:
        configured_raw = os.environ.get("STORYDEX_PROMPT_REPOSITORY_ROOT", "").strip()
        configured = Path(configured_raw).expanduser() if configured_raw else None
        if configured is not None and configured.exists() and configured.is_dir():
            return configured.resolve()

        current = Path(__file__).resolve()
        for parent in current.parents:
            candidate = parent / "docs" / self.DIRECTORY_NAME
            if candidate.exists() and candidate.is_dir():
                return candidate.resolve()
        return None

    def _read_items(self, root: Path | None) -> List[Dict[str, Any]]:
        if root is None:
            return []

        items: List[Dict[str, Any]] = []
        for path in sorted(root.rglob("*.md"), key=lambda item: item.relative_to(root).as_posix().lower()):
            if not path.is_file() or path.name.lower() == "readme.md":
                continue
            content = path.read_text(encoding="utf-8")
            relative = path.relative_to(root)
            prompt_text = self._extract_prompt_text(content)
            category = relative.parts[0] if len(relative.parts) > 1 else "通用"
            items.append(
                {
                    "id": relative.with_suffix("").as_posix(),
                    "title": self._extract_title(content, path.stem),
                    "summary": self._extract_summary(content),
                    "category": category,
                    "relativePath": relative.as_posix(),
                    "content": content,
                    "promptText": prompt_text,
                    "placeholders": self._extract_placeholders(prompt_text),
                    "updatedAt": self._mtime_iso(path),
                    "isCustom": False,
                }
            )
        return items

    def _read_custom_items(self) -> List[Dict[str, Any]]:
        payload = self._read_custom_payload()
        records = payload.get("items") if isinstance(payload.get("items"), list) else []
        items: List[Dict[str, Any]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                items.append(self._custom_item(record))
            except ValueError:
                continue
        return items

    def _custom_item(self, record: Dict[str, Any]) -> Dict[str, Any]:
        prompt_id = str(record.get("id") or "").strip()
        if not re.fullmatch(r"[a-f0-9]{32}", prompt_id):
            raise ValueError("Invalid custom prompt id.")
        title = self._validate_title(str(record.get("title") or ""))
        prompt_text = self._validate_prompt_text(str(record.get("promptText") or ""))
        return {
            "id": f"custom/{prompt_id}",
            "title": title,
            "summary": "用户自定义的可复用指令。",
            "category": self.CUSTOM_CATEGORY,
            "relativePath": "",
            "content": prompt_text,
            "promptText": prompt_text,
            "placeholders": self._extract_placeholders(prompt_text),
            "updatedAt": str(record.get("updatedAt") or ""),
            "isCustom": True,
        }

    def _custom_path(self) -> Path:
        if self._custom_repository_path is not None:
            return self._custom_repository_path.resolve()
        return get_global_config_service().root / "prompts" / "custom.json"

    def _read_custom_payload(self) -> Dict[str, Any]:
        path = self._custom_path()
        if not path.exists():
            return {"version": 1, "items": []}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "items": []}
        return payload if isinstance(payload, dict) else {"version": 1, "items": []}

    def _write_custom_payload(self, payload: Dict[str, Any]) -> None:
        path = self._custom_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @staticmethod
    def _validate_title(value: str) -> str:
        title = str(value or "").strip()
        if not title or len(title) > 120 or "\n" in title or "\r" in title:
            raise ValueError("Custom prompt title must contain 1 to 120 characters.")
        return title

    @staticmethod
    def _validate_prompt_text(value: str) -> str:
        prompt_text = str(value or "").strip()
        if not prompt_text or len(prompt_text) > 12000:
            raise ValueError("Custom prompt body must contain 1 to 12000 characters.")
        return prompt_text

    @staticmethod
    def _extract_title(content: str, fallback: str) -> str:
        for raw_line in str(content or "").splitlines():
            line = raw_line.strip()
            if line.startswith("# "):
                return line[2:].strip() or fallback
        return fallback

    @staticmethod
    def _extract_summary(content: str) -> str:
        for raw_line in str(content or "").splitlines():
            line = raw_line.strip()
            if line.startswith(">"):
                return line.lstrip(">").strip()
        return ""

    @staticmethod
    def _extract_prompt_text(content: str) -> str:
        match = _PROMPT_BLOCK_RE.search(str(content or ""))
        return (match.group("body") if match else str(content or "")).strip()

    @staticmethod
    def _extract_placeholders(prompt_text: str) -> List[str]:
        placeholders: List[str] = []
        for match in _PLACEHOLDER_RE.finditer(str(prompt_text or "")):
            value = match.group(0)
            if value not in placeholders:
                placeholders.append(value)
        return placeholders

    @staticmethod
    def _mtime_iso(path: Path) -> str:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            return ""


@lru_cache(maxsize=1)
def get_prompt_repository_service() -> PromptRepositoryService:
    return PromptRepositoryService()
