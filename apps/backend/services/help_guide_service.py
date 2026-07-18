from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List


class HelpGuideService:
    GUIDE_DIRECTORY_NAME = "guide"

    def read_guide(self) -> Dict[str, Any]:
        root = self._resolve_guide_root()
        items = self._read_items(root)
        return {
            "root": root.as_posix() if root else "",
            "items": items,
            "content": self._build_combined_content(items),
        }

    def search(self, query: str, *, max_results: int = 6) -> Dict[str, Any]:
        normalized_query = str(query or "").strip()
        guide = self.read_guide()
        items = guide.get("items") if isinstance(guide.get("items"), list) else []
        if not normalized_query:
            return {
                "query": normalized_query,
                "items": [
                    self._compact_item(item)
                    for item in items[: max(1, min(int(max_results or 6), 20))]
                    if isinstance(item, dict)
                ],
            }

        terms = [term.lower() for term in normalized_query.split() if term.strip()]
        if not terms:
            terms = [normalized_query.lower()]

        matches: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            haystack = f"{item.get('title') or ''}\n{item.get('content') or ''}".lower()
            score = sum(haystack.count(term) for term in terms)
            if score <= 0:
                continue
            matches.append(
                {
                    **self._compact_item(item),
                    "score": score,
                    "snippets": self._snippets(str(item.get("content") or ""), terms),
                }
            )

        matches.sort(key=lambda item: (int(item.get("score") or 0), str(item.get("id") or "")), reverse=True)
        return {
            "query": normalized_query,
            "items": matches[: max(1, min(int(max_results or 6), 20))],
        }

    def _resolve_guide_root(self) -> Path | None:
        configured_raw = os.environ.get("STORYDEX_HELP_GUIDE_ROOT", "").strip()
        configured = Path(configured_raw).expanduser() if configured_raw else None
        if configured is not None and configured.exists() and configured.is_dir():
            return configured.resolve()

        current = Path(__file__).resolve()
        for parent in current.parents:
            candidate = parent / "docs" / self.GUIDE_DIRECTORY_NAME
            if candidate.exists() and candidate.is_dir():
                return candidate.resolve()
        return None

    def _read_items(self, root: Path | None) -> List[Dict[str, Any]]:
        if root is None:
            return []
        items: List[Dict[str, Any]] = []
        for path in sorted(root.glob("*.md"), key=lambda item: item.name.lower()):
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
            items.append(
                {
                    "id": path.stem,
                    "title": self._extract_title(content, path.stem),
                    "relativePath": path.name,
                    "content": content,
                    "updatedAt": self._mtime_iso(path),
                }
            )
        return items

    @staticmethod
    def _extract_title(content: str, fallback: str) -> str:
        for raw_line in str(content or "").splitlines():
            line = raw_line.strip()
            if line.startswith("#"):
                return line.lstrip("#").strip() or fallback
        return fallback

    @staticmethod
    def _mtime_iso(path: Path) -> str:
        try:
            from datetime import datetime, timezone

            return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            return ""

    @staticmethod
    def _build_combined_content(items: List[Dict[str, Any]]) -> str:
        if not items:
            return "# 使用指南\n\n暂未找到 Storydex 使用指南。"
        chunks = ["# 使用指南"]
        for item in items:
            title = str(item.get("title") or item.get("id") or "指南").strip()
            content = str(item.get("content") or "").strip()
            chunks.append(f"\n\n## {title}\n\n{content}")
        return "\n".join(chunks).strip() + "\n"

    @staticmethod
    def _compact_item(item: Dict[str, Any]) -> Dict[str, Any]:
        content = str(item.get("content") or "")
        return {
            "id": str(item.get("id") or ""),
            "title": str(item.get("title") or ""),
            "relativePath": str(item.get("relativePath") or ""),
            "preview": content[:240].strip(),
        }

    @staticmethod
    def _snippets(content: str, terms: List[str]) -> List[str]:
        lowered = content.lower()
        snippets: List[str] = []
        for term in terms:
            index = lowered.find(term)
            if index < 0:
                continue
            start = max(0, index - 80)
            end = min(len(content), index + len(term) + 160)
            snippet = content[start:end].strip()
            if start > 0:
                snippet = f"...{snippet}"
            if end < len(content):
                snippet = f"{snippet}..."
            if snippet and snippet not in snippets:
                snippets.append(snippet)
            if len(snippets) >= 3:
                break
        return snippets


@lru_cache(maxsize=1)
def get_help_guide_service() -> HelpGuideService:
    return HelpGuideService()
