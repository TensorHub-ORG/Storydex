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

from core.bounded_text_io import read_text_limited

logger = logging.getLogger(__name__)

INDEXABLE_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml"}
DEFAULT_INDEX_REL = ".storydex/.cache/retrieval.fts5.db"
FTS5_INDEX_CHAR_LIMIT = 120_000

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(content, path UNINDEXED, tokenize='unicode61');
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
                conn.execute("INSERT INTO docs(content, path) VALUES(?, ?)", (text, rel))
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
            for p in self._candidate_files():
                rel = str(p.relative_to(self.project_root)).replace("\\", "/")
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
                conn.execute("INSERT INTO docs(content, path) VALUES(?, ?)", (text, rel))
                conn.execute(
                    "INSERT OR REPLACE INTO doc_meta(path, mtime, indexed_at, size) VALUES(?, ?, ?, ?)",
                    (rel, mtime, now, size),
                )
                updated += 1
            return updated

    # ─────────────────── 检索 ───────────────────

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

    def search(self, query: str, *, top_k: int = 20, path_prefix: Optional[str] = None) -> List[Tuple[str, float, str]]:
        """返回 (path, score, snippet)。score 越小越相关（FTS5 bm25）。"""
        if not query or not query.strip():
            return []
        with self._lock, self._connect() as conn:
            sql = (
                "SELECT path, bm25(docs) AS score, snippet(docs, 0, '[', ']', '...', 16) AS snip "
                "FROM docs WHERE docs MATCH ?"
            )
            params: List = [self._sanitize_query(query)]
            if path_prefix:
                sql += " AND path LIKE ?"
                params.append(f"{path_prefix}%")
            sql += " ORDER BY score LIMIT ?"
            params.append(int(top_k))
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                logger.warning("FTS5 query failed: %s", exc)
                return []
        results: List[Tuple[str, float, str]] = []
        for row in rows:
            path = str(row["path"] or "")
            if self._is_runtime_path(tuple(Path(path).parts)):
                continue
            results.append((path, float(row["score"] or 0.0), str(row["snip"] or "")))
        return results

    @staticmethod
    def _sanitize_query(query: str) -> str:
        # FTS5 把空格视作 AND；引号包起单 token 避免特殊符号触发语法错
        terms = [t for t in query.replace('"', " ").split() if t]
        return " ".join(f'"{t}"' for t in terms) if terms else "\"\""

    @staticmethod
    def _is_runtime_path(relative_parts: Tuple[str, ...]) -> bool:
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
