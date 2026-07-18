from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List


_PROMPT_BLOCK_RE = re.compile(r"```(?:prompt|text)\s*\n(?P<body>.*?)```", re.IGNORECASE | re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"\[[^\]\r\n]{1,40}\]")


class PromptRepositoryService:
    DIRECTORY_NAME = "prompts"

    def read_repository(self, *, query: str = "", category: str = "") -> Dict[str, Any]:
        root = self._resolve_root()
        all_items = self._read_items(root)
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
                }
            )
        return items

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
