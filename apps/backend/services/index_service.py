from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from core.bounded_text_io import read_text_preview
from services.storydex_retrieval import bm25_search, hybrid_search, tokenize
from storage.workspace_io import WorkspaceIO

logger = logging.getLogger(__name__)

FALLBACK_SNIPPET_PREVIEW_CHARS = 4000


class IndexService:
    """Hybrid retrieval service for workspace files (BM25 + semantic)."""

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        workspace = WorkspaceIO()
        workspace_root = workspace.workspace_root
        candidate_files = list(self._iter_candidate_files(workspace_root))
        if not candidate_files:
            return []

        hybrid_results = hybrid_search(
            query=query,
            file_paths=candidate_files,
            workspace_root=workspace_root,
            limit=max(1, min(limit, 20)),
        )
        if hybrid_results:
            return hybrid_results

        if shutil.which("rg"):
            ripgrep_hits = self._search_with_ripgrep(
                workspace=workspace,
                keywords=tokenize(query),
                limit=limit,
            )
            if ripgrep_hits:
                return ripgrep_hits

        bm25_results = bm25_search(
            query=query,
            file_paths=candidate_files,
            workspace_root=workspace_root,
            limit=max(1, min(limit, 20)),
        )
        for r in bm25_results:
            r["relativePath"] = r.pop("doc_id", r.get("relativePath", ""))
            r["snippet"] = r.get("metadata", {}).get("snippet", "")
            if not r["snippet"]:
                fp = workspace_root / r["relativePath"]
                if fp.exists():
                    try:
                        content = read_text_preview(fp, max_chars=FALLBACK_SNIPPET_PREVIEW_CHARS)
                        r["snippet"] = self._build_snippet(content, tokenize(query))
                    except (UnicodeDecodeError, OSError):
                        pass
            r.pop("metadata", None)
            r["engine"] = "bm25"
        return bm25_results

    def _search_with_ripgrep(
        self,
        *,
        workspace: WorkspaceIO,
        keywords: List[str],
        limit: int,
    ) -> List[Dict[str, Any]]:
        search_roots = self._search_roots(workspace)
        if not search_roots:
            return []

        command = [
            "rg",
            "--json",
            "--line-number",
            "--hidden",
            "--smart-case",
            "--max-count",
            "3",
            "--glob",
            "!**/.git/**",
            "--glob",
            "!**/node_modules/**",
            "--glob",
            "!**/__pycache__/**",
        ]
        for keyword in keywords[:6]:
            command.extend(["-e", keyword])
        command.extend(search_roots)

        try:
            completed = subprocess.run(
                command,
                cwd=str(workspace.workspace_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError:
            return []

        if completed.returncode not in {0, 1}:
            return []

        aggregated: Dict[str, Dict[str, Any]] = {}
        for line in completed.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if payload.get("type") != "match":
                continue
            data = payload.get("data")
            if not isinstance(data, dict):
                continue
            path_payload = data.get("path")
            if not isinstance(path_payload, dict):
                continue
            relative_path = str(path_payload.get("text") or "").replace("\\", "/")
            if not relative_path:
                continue
            line_number = int(data.get("line_number") or 0)
            lines_payload = data.get("lines")
            line_text = ""
            if isinstance(lines_payload, dict):
                line_text = str(lines_payload.get("text") or "").strip()
            submatches = data.get("submatches")
            match_count = len(submatches) if isinstance(submatches, list) else 1

            record = aggregated.setdefault(
                relative_path,
                {
                    "relativePath": relative_path,
                    "score": 0,
                    "snippet": "",
                    "lineNumber": line_number,
                    "matchCount": 0,
                    "matches": [],
                    "engine": "ripgrep",
                },
            )
            if not record["snippet"] and line_text:
                record["snippet"] = self._clean_snippet(line_text)
            record["score"] += 4 + match_count
            record["matchCount"] += match_count
            if line_number and not record.get("lineNumber"):
                record["lineNumber"] = line_number
            if line_text and len(record["matches"]) < 3:
                record["matches"].append(
                    {
                        "lineNumber": line_number,
                        "text": self._clean_snippet(line_text, max_len=260),
                    }
                )

        results = list(aggregated.values())
        results.sort(
            key=lambda item: (
                -int(item.get("score") or 0),
                str(item.get("relativePath") or ""),
            )
        )
        return results[: max(1, min(limit, 20))]

    def _iter_candidate_files(self, workspace_root: Path):
        include_roots = [
            workspace_root / ".storydex",
            workspace_root / "chapters",
            workspace_root / "docs",
        ]
        include_suffixes = {".md", ".json", ".yaml", ".yml", ".txt"}
        exclude_dirs = {
            ".agent",
            "autopilot",
            "file-history",
            "logs",
            "projections",
            "rollback_backups",
            "sessions",
            "temp",
            "trace",
            "traces",
        }

        for root in include_roots:
            if not root.exists():
                continue
            for candidate in root.rglob("*"):
                if not candidate.is_file():
                    continue
                if candidate.suffix.lower() not in include_suffixes:
                    continue
                parts = candidate.relative_to(root).parts
                if any(part in exclude_dirs for part in parts):
                    continue
                yield candidate

    @staticmethod
    def _build_snippet(content: str, keywords: List[str]) -> str:
        lowered = content.lower()
        first_index = -1
        for token in keywords:
            first_index = lowered.find(token)
            if first_index >= 0:
                break

        if first_index < 0:
            snippet = content[:220]
        else:
            left = max(0, first_index - 80)
            right = min(len(content), first_index + 140)
            snippet = content[left:right]

        snippet = " ".join(snippet.split())
        if len(snippet) > 220:
            return snippet[:217] + "..."
        return snippet

    @staticmethod
    def _clean_snippet(text: str, max_len: int = 220) -> str:
        compact = " ".join(str(text or "").split())
        if len(compact) <= max_len:
            return compact
        return compact[: max_len - 3] + "..."

    @staticmethod
    def _search_roots(workspace: WorkspaceIO) -> List[str]:
        candidates = [
            workspace.storydex_root,
            workspace.workspace_root / "chapters",
            workspace.workspace_root / "docs",
        ]
        roots: List[str] = []
        for root in candidates:
            if root.exists():
                try:
                    roots.append(root.relative_to(workspace.workspace_root).as_posix())
                except ValueError:
                    roots.append(root.as_posix())
        if not roots:
            roots.append(".")
        return roots
