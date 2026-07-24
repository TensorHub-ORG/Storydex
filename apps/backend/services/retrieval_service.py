"""WP-4.2 · SQLite FTS5 倒排索引（07 §5.5）。

把 ``.storydex/`` 与 ``chapters/`` 的所有 ``.md/.txt/.json/.yaml`` 内容入
SQLite FTS5 表 ``docs(content, path)``，提供 ``search(query, top_k, filters)``
返回 ``(path, score, snippet)`` 列表。代替旧 ``index_service`` 的 rglob
全扫 + 词频打分。

* DB 路径：``.storydex/.cache/retrieval.fts5.db``（manifest 已声明 create_on_init=False）。
* ``build_index(project_root)``：全量扫一次入库；幂等（删旧表重建）。
* ``watch_files(...)``：增量更新只扫 mtime > last_indexed_at 的文件。
* 受 ``CONTEXT_PIPELINE_FTS5`` Flag 控制；Off 时调用方走旧 index_service。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from core.bounded_text_io import read_text_limited, read_text_preview
from services.storydex_retrieval import _build_snippet, tokenize

logger = logging.getLogger(__name__)

INDEXABLE_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml"}
# v2：入库内容改为中文 bigram/trigram token 流（FTS5 unicode61 对连续中文
# 只切出整段 token，原始内容直接入库时中文检索基本失效）。schema 不兼容，
# 换文件名让旧库自然废弃。
DEFAULT_INDEX_REL = ".storydex/.cache/retrieval.fts5.v2.db"
FTS5_INDEX_CHAR_LIMIT = 120_000
MAX_QUERY_TOKENS = 24
RECALL_CANDIDATE_LIMIT = 30

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(tokens, path UNINDEXED, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS doc_meta (
    path TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    indexed_at REAL NOT NULL,
    size INTEGER
);
"""


class RetrievalService:
    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.db_path = self.project_root / DEFAULT_INDEX_REL
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._init_schema()

    # ─────────────────── schema ───────────────────

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ─────────────────── 索引构建 ───────────────────

    def _candidate_files(self) -> Iterable[Path]:
        roots = [self.project_root / "chapters", self.project_root / ".storydex"]
        for root in roots:
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                if p.suffix.lower() not in INDEXABLE_SUFFIXES:
                    continue
                # 跳过 .agent 内部运行时数据（jobs.db 等）
                rel_parts = p.relative_to(self.project_root).parts
                if self._is_runtime_path(rel_parts):
                    continue
                yield p

    def build_index(self) -> int:
        """全量重建索引；返回入库文件数。"""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM docs")
            conn.execute("DELETE FROM doc_meta")
            count = 0
            now = time.time()
            for p in self._candidate_files():
                indexed = self._read_indexable_text(p)
                if indexed is None:
                    continue
                text, size, mtime = indexed
                rel = str(p.relative_to(self.project_root)).replace("\\", "/")
                conn.execute("INSERT INTO docs(tokens, path) VALUES(?, ?)", (self._tokenized(text), rel))
                conn.execute(
                    "INSERT OR REPLACE INTO doc_meta(path, mtime, indexed_at, size) VALUES(?, ?, ?, ?)",
                    (rel, mtime, now, size),
                )
                count += 1
            return count

    def watch_files(self) -> int:
        """增量：只对 mtime 比上次 indexed_at 新的文件重新入库。返回处理数。"""
        with self._lock, self._connect() as conn:
            existing = {
                row["path"]: float(row["indexed_at"])
                for row in conn.execute("SELECT path, indexed_at FROM doc_meta").fetchall()
            }
            updated = 0
            now = time.time()
            seen_paths: set[str] = set()
            for p in self._candidate_files():
                rel = str(p.relative_to(self.project_root)).replace("\\", "/")
                seen_paths.add(rel)
                try:
                    stat = p.stat()
                except OSError:
                    continue
                mtime = stat.st_mtime
                if rel in existing and mtime <= existing[rel]:
                    continue
                indexed = self._read_indexable_text(p, stat=stat)
                if indexed is None:
                    continue
                text, size, _mtime = indexed
                conn.execute("DELETE FROM docs WHERE path=?", (rel,))
                conn.execute("INSERT INTO docs(tokens, path) VALUES(?, ?)", (self._tokenized(text), rel))
                conn.execute(
                    "INSERT OR REPLACE INTO doc_meta(path, mtime, indexed_at, size) VALUES(?, ?, ?, ?)",
                    (rel, mtime, now, size),
                )
                updated += 1
            for rel in set(existing) - seen_paths:
                conn.execute("DELETE FROM docs WHERE path=?", (rel,))
                conn.execute("DELETE FROM doc_meta WHERE path=?", (rel,))
                updated += 1
            return updated

    # ─────────────────── 检索 ───────────────────

    @staticmethod
    def _tokenized(text: str) -> str:
        return " ".join(tokenize(text))

    @staticmethod
    def _read_indexable_text(path: Path, *, stat: Any | None = None) -> tuple[str, int, float] | None:
        try:
            file_stat = stat if stat is not None else path.stat()
            read = read_text_limited(path, FTS5_INDEX_CHAR_LIMIT, preserve_tail=True)
        except OSError:
            return None
        size = int(getattr(file_stat, "st_size", 0) or read.total_chars)
        mtime = float(getattr(file_stat, "st_mtime", 0.0) or 0.0)
        return read.text, size, mtime

    def _ranked_matches(
        self,
        query: str,
        *,
        limit: int,
        path_prefix: Optional[str] = None,
    ) -> Tuple[List[str], List[Tuple[str, float]]]:
        if not query or not query.strip():
            return [], []
        query_tokens = tokenize(query)[:MAX_QUERY_TOKENS]
        if not query_tokens:
            return [], []
        result_limit = int(limit)
        if result_limit <= 0:
            return query_tokens, []
        match_expr = " OR ".join(f'"{token}"' for token in query_tokens)
        with self._lock, self._connect() as conn:
            sql = "SELECT path, bm25(docs) AS score FROM docs WHERE docs MATCH ?"
            params: List = [match_expr]
            if path_prefix:
                sql += " AND path LIKE ?"
                params.append(f"{path_prefix}%")
            sql += " ORDER BY score LIMIT ?"
            params.append(result_limit)
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                logger.warning("FTS5 query failed: %s", exc)
                return query_tokens, []
        matches: List[Tuple[str, float]] = []
        for row in rows:
            path = str(row["path"] or "")
            if self._is_runtime_path(tuple(Path(path).parts)):
                continue
            matches.append((path, float(row["score"] or 0.0)))
        return query_tokens, matches

    def _materialize_hits(
        self,
        matches: Iterable[Tuple[str, float]],
        query_tokens: List[str],
    ) -> List[Tuple[str, float, str]]:
        results: List[Tuple[str, float, str]] = []
        for path, score in matches:
            # tokens 列是 bigram 流，FTS5 自带 snippet 不可读；从原文生成摘录。
            snippet = ""
            try:
                snippet = _build_snippet(read_text_preview(self.project_root / path, max_chars=4000), query_tokens)
            except Exception:
                snippet = ""
            results.append((path, score, snippet))
        return results

    def search(self, query: str, *, top_k: int = 20, path_prefix: Optional[str] = None) -> List[Tuple[str, float, str]]:
        """返回 (path, score, snippet)。score 越小越相关（FTS5 bm25）。"""
        query_tokens, matches = self._ranked_matches(query, limit=top_k, path_prefix=path_prefix)
        return self._materialize_hits(matches, query_tokens)

    def search_with_candidates(
        self,
        query: str,
        *,
        top_k: int = 20,
        candidate_limit: int = RECALL_CANDIDATE_LIMIT,
        path_prefix: Optional[str] = None,
    ) -> Tuple[List[Tuple[str, float, str]], List[str]]:
        """返回可见摘录和更宽的候选路径，候选路径不额外读取文件内容。"""
        visible_limit = max(0, int(top_k))
        bounded_candidate_limit = max(0, min(int(candidate_limit), RECALL_CANDIDATE_LIMIT))
        ranked_limit = max(visible_limit, bounded_candidate_limit)
        query_tokens, matches = self._ranked_matches(query, limit=ranked_limit, path_prefix=path_prefix)
        visible_matches = matches[:visible_limit]
        return self._materialize_hits(visible_matches, query_tokens), [path for path, _score in matches]

    @staticmethod
    def _is_runtime_path(relative_parts: Tuple[str, ...]) -> bool:
        """运行时数据与生成物不入检索：Agent 查 WIKI 走 StorydexWikiQuery。"""
        if len(relative_parts) < 2 or relative_parts[0] != ".storydex":
            return False
        return relative_parts[1] in {
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
            "wiki",
        }


_PROBE: Dict[str, RetrievalService] = {}
_PROBE_LOCK = Lock()


def get_retrieval_service(project_root: Path) -> RetrievalService:
    key = str(Path(project_root).resolve())
    with _PROBE_LOCK:
        if key not in _PROBE:
            _PROBE[key] = RetrievalService(Path(project_root))
        return _PROBE[key]


def reset_retrieval_cache() -> None:
    with _PROBE_LOCK:
        _PROBE.clear()


_ENCODING_SELFTEST = "RetrievalService 编码自检：FTS5 / 倒排"
assert "�" not in _ENCODING_SELFTEST
