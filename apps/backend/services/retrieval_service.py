"""Versioned SQLite FTS5 retrieval with full-file chunk coverage."""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from services.performance_trace_service import record_counter
from services.source_contract import source_line_count, source_revision_id
from services.storydex_retrieval import tokenize

logger = logging.getLogger(__name__)

INDEXABLE_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml"}
INDEX_SCHEMA_VERSION = 3
DEFAULT_INDEX_REL = ".storydex/.cache/retrieval.fts5.v3.db"
LEGACY_INDEX_REL = ".storydex/.cache/retrieval.fts5.v2.db"

# Kept as a compatibility import for callers that referenced the v2 limit. V3
# never uses this value to truncate indexed source text.
FTS5_INDEX_CHAR_LIMIT = 120_000
CHUNK_TARGET_CHARS = 3_200
CHUNK_MIN_CHARS = 2_000
CHUNK_OVERLAP_CHARS = 400
SNIPPET_MAX_CHARS = 800
MAX_QUERY_TOKENS = 24
RECALL_CANDIDATE_LIMIT = 30
MAX_CHUNK_QUERY_ROWS = 2_000

INDEX_OK = "ok"
INDEX_BUILDING = "index_building"
INDEX_STALE = "index_stale"
INDEX_ERROR = "index_error"

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    tokens,
    content UNINDEXED,
    path UNINDEXED,
    chunk_id UNINDEXED,
    start_char UNINDEXED,
    end_char UNINDEXED,
    start_byte UNINDEXED,
    end_byte UNINDEXED,
    start_line UNINDEXED,
    end_line UNINDEXED,
    revision UNINDEXED,
    tokenize='unicode61'
);
CREATE TABLE IF NOT EXISTS documents (
    path TEXT PRIMARY KEY,
    revision TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    total_chars INTEGER NOT NULL,
    total_lines INTEGER NOT NULL,
    indexed_at REAL NOT NULL,
    chunk_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS index_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    schema_version INTEGER NOT NULL,
    state TEXT NOT NULL,
    generation TEXT NOT NULL,
    last_error TEXT NOT NULL,
    updated_at REAL NOT NULL
);
INSERT OR IGNORE INTO index_state(
    singleton, schema_version, state, generation, last_error, updated_at
) VALUES(1, 3, 'ok', '', '', 0);
"""


class RetrievalIndexError(RuntimeError):
    """The published retrieval index cannot prove a complete query result."""


class RetrievalIndexStaleError(RetrievalIndexError):
    """A source changed while a stable revision was being indexed."""


@contextmanager
def _immediate_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


class RetrievalService:
    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.db_path = self.project_root / DEFAULT_INDEX_REL
        self.legacy_db_path = self.project_root / LEGACY_INDEX_REL
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._initialize_database(self.db_path)

    # -------------------- database lifecycle --------------------

    @staticmethod
    def _initialize_database(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with RetrievalService._connect_path(path) as conn:
            conn.executescript(_SCHEMA)

    @staticmethod
    @contextmanager
    def _connect_path(path: Path) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._connect_path(self.db_path) as conn:
            yield conn

    @staticmethod
    def _write_state(
        conn: sqlite3.Connection,
        state: str,
        *,
        generation: str | None = None,
        error: str = "",
    ) -> None:
        if generation is None:
            conn.execute(
                "UPDATE index_state SET state=?, last_error=?, updated_at=? WHERE singleton=1",
                (state, str(error or "")[:1000], time.time()),
            )
            return
        conn.execute(
            """
            UPDATE index_state
            SET schema_version=?, state=?, generation=?, last_error=?, updated_at=?
            WHERE singleton=1
            """,
            (
                INDEX_SCHEMA_VERSION,
                state,
                generation,
                str(error or "")[:1000],
                time.time(),
            ),
        )

    def _write_current_state(self, state: str, *, error: str = "") -> None:
        try:
            with self._connect() as conn:
                self._write_state(conn, state, error=error)
        except Exception as exc:
            logger.error("Failed to persist retrieval index state %s: %s", state, exc)

    # -------------------- source discovery and chunking --------------------

    def _candidate_files(self) -> Iterable[Path]:
        roots = [self.project_root / "chapters", self.project_root / ".storydex"]
        candidates: List[Path] = []
        for root in roots:
            if not root.exists():
                continue
            record_counter("directoryScanCount")
            for path in root.rglob("*"):
                record_counter("statCount")
                if not path.is_file() or path.suffix.lower() not in INDEXABLE_SUFFIXES:
                    continue
                relative_parts = path.relative_to(self.project_root).parts
                if self._is_runtime_path(relative_parts):
                    continue
                candidates.append(path)
        return sorted(candidates, key=lambda item: item.as_posix())

    @staticmethod
    def _tokenized(text: str) -> str:
        return " ".join(tokenize(text))

    @staticmethod
    def _source_revision(raw: bytes) -> str:
        return source_revision_id(raw)

    @staticmethod
    def _read_source(path: Path, *, stat: os.stat_result | None = None) -> Dict[str, Any]:
        try:
            before = stat if stat is not None else path.stat()
            raw = path.read_bytes()
            after = path.stat()
        except OSError as exc:
            raise RetrievalIndexError(f"failed to read retrieval source {path}: {exc}") from exc
        before_signature = (int(before.st_size), int(before.st_mtime_ns))
        after_signature = (int(after.st_size), int(after.st_mtime_ns))
        if before_signature != after_signature or len(raw) != after_signature[0]:
            raise RetrievalIndexStaleError(f"retrieval source changed while indexing: {path}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RetrievalIndexError(f"retrieval source is not valid UTF-8: {path}") from exc
        return {
            "text": text,
            "revision": RetrievalService._source_revision(raw),
            "sizeBytes": len(raw),
            "mtimeNs": after_signature[1],
            "totalChars": len(text),
            "totalLines": source_line_count(text),
        }

    @staticmethod
    def _chunk_boundaries(text: str) -> List[Tuple[int, int]]:
        total = len(text)
        if total <= 0:
            return []
        boundaries: List[Tuple[int, int]] = []
        start = 0
        while start < total:
            desired_end = min(total, start + CHUNK_TARGET_CHARS)
            end = desired_end
            if desired_end < total:
                boundary_floor = min(desired_end, start + CHUNK_MIN_CHARS)
                window = text[boundary_floor:desired_end]
                paragraph = window.rfind("\n\n")
                line = window.rfind("\n")
                if paragraph >= 0:
                    end = boundary_floor + paragraph + 2
                elif line >= 0:
                    end = boundary_floor + line + 1
            if end <= start:
                end = min(total, start + CHUNK_TARGET_CHARS)
            boundaries.append((start, end))
            if end >= total:
                break
            start = max(start + 1, end - CHUNK_OVERLAP_CHARS)
        return boundaries

    @staticmethod
    def _position_metrics(text: str, positions: Sequence[int]) -> Dict[int, Tuple[int, int]]:
        wanted = set(int(position) for position in positions)
        metrics: Dict[int, Tuple[int, int]] = {}
        byte_offset = 0
        line = 1
        if 0 in wanted:
            metrics[0] = (0, 1)
        for index, character in enumerate(text):
            if index in wanted and index not in metrics:
                metrics[index] = (byte_offset, line)
            byte_offset += len(character.encode("utf-8"))
            if character == "\n":
                line += 1
            next_index = index + 1
            if next_index in wanted:
                metrics[next_index] = (byte_offset, line)
        return metrics

    @classmethod
    def _chunks(cls, text: str, revision: str) -> List[Dict[str, Any]]:
        boundaries = cls._chunk_boundaries(text)
        positions: set[int] = set()
        for start, end in boundaries:
            positions.update((start, end, max(start, end - 1)))
        metrics = cls._position_metrics(text, tuple(positions))
        chunks: List[Dict[str, Any]] = []
        for chunk_id, (start, end) in enumerate(boundaries):
            start_byte, start_line = metrics[start]
            end_byte, _line_after_end = metrics[end]
            _last_byte, end_line = metrics[max(start, end - 1)]
            chunks.append(
                {
                    "chunkId": chunk_id,
                    "startChar": start,
                    "endChar": end,
                    "startByte": start_byte,
                    "endByte": end_byte,
                    "startLine": start_line,
                    "endLine": end_line,
                    "revision": revision,
                    "content": text[start:end],
                }
            )
        return chunks

    # -------------------- index publication --------------------

    def _index_document(
        self,
        conn: sqlite3.Connection,
        path: Path,
        relative_path: str,
        *,
        indexed_at: float,
        stat: os.stat_result | None = None,
    ) -> int:
        source = self._read_source(path, stat=stat)
        chunks = self._chunks(str(source["text"]), str(source["revision"]))
        conn.execute("DELETE FROM chunks WHERE path=?", (relative_path,))
        conn.execute("DELETE FROM documents WHERE path=?", (relative_path,))
        for chunk in chunks:
            conn.execute(
                """
                INSERT INTO chunks(
                    tokens, content, path, chunk_id, start_char, end_char,
                    start_byte, end_byte, start_line, end_line, revision
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._tokenized(str(chunk["content"])),
                    chunk["content"],
                    relative_path,
                    chunk["chunkId"],
                    chunk["startChar"],
                    chunk["endChar"],
                    chunk["startByte"],
                    chunk["endByte"],
                    chunk["startLine"],
                    chunk["endLine"],
                    chunk["revision"],
                ),
            )
        conn.execute(
            """
            INSERT INTO documents(
                path, revision, size_bytes, mtime_ns, total_chars,
                total_lines, indexed_at, chunk_count
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                relative_path,
                source["revision"],
                source["sizeBytes"],
                source["mtimeNs"],
                source["totalChars"],
                source["totalLines"],
                indexed_at,
                len(chunks),
            ),
        )
        return len(chunks)

    @staticmethod
    def _generation(conn: sqlite3.Connection) -> str:
        rows = conn.execute("SELECT path, revision FROM documents ORDER BY path").fetchall()
        payload = "\n".join(f"{row['path']}\0{row['revision']}" for row in rows)
        return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _validate_database(
        conn: sqlite3.Connection,
        *,
        paths: Sequence[str] | None = None,
        integrity_check: bool = False,
    ) -> None:
        if integrity_check:
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0] or "")
            if integrity.lower() != "ok":
                raise RetrievalIndexError(
                    f"retrieval database integrity check failed: {integrity}"
                )
        if paths is None:
            document_rows = conn.execute(
                "SELECT path, total_chars, chunk_count FROM documents ORDER BY path"
            ).fetchall()
        else:
            unique_paths = list(dict.fromkeys(str(path) for path in paths if str(path)))
            if not unique_paths:
                document_rows = []
            else:
                placeholders = ",".join("?" for _path in unique_paths)
                document_rows = conn.execute(
                    f"SELECT path, total_chars, chunk_count FROM documents "  # noqa: S608 - placeholders only
                    f"WHERE path IN ({placeholders}) ORDER BY path",
                    unique_paths,
                ).fetchall()
        total_chunks = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        declared_chunks = int(
            conn.execute("SELECT COALESCE(SUM(chunk_count), 0) FROM documents").fetchone()[0]
        )
        if total_chunks != declared_chunks:
            raise RetrievalIndexError(
                f"retrieval chunk count mismatch: table={total_chunks}, documents={declared_chunks}"
            )
        for document in document_rows:
            total_chars = int(document["total_chars"] or 0)
            rows = conn.execute(
                "SELECT start_char, end_char FROM chunks WHERE path=? ORDER BY start_char, chunk_id",
                (document["path"],),
            ).fetchall()
            if total_chars == 0:
                if rows:
                    raise RetrievalIndexError(f"empty retrieval source has chunks: {document['path']}")
                continue
            cursor = 0
            for row in rows:
                start = int(row["start_char"])
                end = int(row["end_char"])
                if start > cursor or end <= start:
                    raise RetrievalIndexError(
                        f"retrieval chunk coverage gap for {document['path']}: {cursor} -> {start}..{end}"
                    )
                cursor = max(cursor, end)
            if cursor != total_chars:
                raise RetrievalIndexError(
                    f"retrieval chunk coverage incomplete for {document['path']}: {cursor}/{total_chars}"
                )

    def build_index(self) -> int:
        """Build a complete temporary v3 database and atomically publish it."""
        temporary_path = self.db_path.with_name(f".{self.db_path.name}.{uuid.uuid4().hex}.tmp")
        with self._lock:
            self._write_current_state(INDEX_BUILDING)
            try:
                self._initialize_database(temporary_path)
                with self._connect_path(temporary_path) as conn:
                    self._write_state(conn, INDEX_BUILDING)
                    count = 0
                    indexed_at = time.time()
                    with _immediate_transaction(conn):
                        for path in self._candidate_files():
                            relative = path.relative_to(self.project_root).as_posix()
                            self._index_document(
                                conn,
                                path,
                                relative,
                                indexed_at=indexed_at,
                            )
                            count += 1
                        self._validate_database(conn, integrity_check=True)
                        self._write_state(
                            conn,
                            INDEX_OK,
                            generation=self._generation(conn),
                        )
                os.replace(temporary_path, self.db_path)
                return count
            except BaseException as exc:
                self._write_current_state(INDEX_ERROR, error=str(exc))
                raise
            finally:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Failed to remove temporary retrieval database %s", temporary_path)

    def watch_files(self) -> int:
        """Atomically refresh changed files and delete chunks for removed paths."""
        with self._lock:
            try:
                with self._connect() as conn:
                    existing = {
                        str(row["path"]): (int(row["mtime_ns"]), int(row["size_bytes"]))
                        for row in conn.execute(
                            "SELECT path, mtime_ns, size_bytes FROM documents"
                        ).fetchall()
                    }
                    pending: List[Tuple[Path, str, os.stat_result]] = []
                    seen_paths: set[str] = set()
                    for path in self._candidate_files():
                        relative = path.relative_to(self.project_root).as_posix()
                        seen_paths.add(relative)
                        try:
                            record_counter("statCount")
                            stat = path.stat()
                        except OSError as exc:
                            raise RetrievalIndexError(
                                f"failed to stat retrieval source {path}: {exc}"
                            ) from exc
                        signature = (int(stat.st_mtime_ns), int(stat.st_size))
                        if existing.get(relative) != signature:
                            pending.append((path, relative, stat))
                    removed_paths = sorted(set(existing) - seen_paths)
                    watcher_changes = [relative for _path, relative, _stat in pending]
                    watcher_changes.extend(removed_paths)
                    if watcher_changes:
                        from services.content_catalog_service import get_content_catalog_service

                        get_content_catalog_service(self.project_root).notify_external_changes(
                            watcher_changes
                        )
                    state_row = conn.execute(
                        "SELECT state FROM index_state WHERE singleton=1"
                    ).fetchone()
                    current_state = str(state_row["state"] or INDEX_ERROR) if state_row else INDEX_ERROR
                    if not pending and not removed_paths and current_state == INDEX_OK:
                        return 0
                    self._write_state(conn, INDEX_BUILDING)
                    updated = 0
                    indexed_at = time.time()
                    with _immediate_transaction(conn):
                        for path, relative, stat in pending:
                            self._index_document(
                                conn,
                                path,
                                relative,
                                indexed_at=indexed_at,
                                stat=stat,
                            )
                            updated += 1
                        for relative in removed_paths:
                            conn.execute("DELETE FROM chunks WHERE path=?", (relative,))
                            conn.execute("DELETE FROM documents WHERE path=?", (relative,))
                            updated += 1
                        changed_paths = [relative for _path, relative, _stat in pending]
                        self._validate_database(
                            conn,
                            paths=changed_paths if pending or removed_paths else None,
                            integrity_check=not pending and not removed_paths,
                        )
                        self._write_state(
                            conn,
                            INDEX_OK,
                            generation=self._generation(conn),
                        )
                    return updated
            except BaseException as exc:
                self._write_current_state(INDEX_ERROR, error=str(exc))
                raise

    # -------------------- status and search --------------------

    def index_status(self, *, check_stale: bool = True) -> Dict[str, Any]:
        try:
            with self._lock, self._connect() as conn:
                state = conn.execute(
                    "SELECT schema_version, state, generation, last_error, updated_at FROM index_state WHERE singleton=1"
                ).fetchone()
                if state is None:
                    raise RetrievalIndexError("retrieval index state is missing")
                coverage = conn.execute(
                    """
                    SELECT COUNT(*) AS document_count,
                           COALESCE(SUM(chunk_count), 0) AS chunk_count,
                           COALESCE(SUM(size_bytes), 0) AS total_bytes,
                           COALESCE(SUM(total_chars), 0) AS total_chars
                    FROM documents
                    """
                ).fetchone()
                persisted_state = str(state["state"] or INDEX_ERROR)
                result = {
                    "status": persisted_state,
                    "schemaVersion": int(state["schema_version"] or 0),
                    "generation": str(state["generation"] or ""),
                    "lastError": str(state["last_error"] or ""),
                    "updatedAt": float(state["updated_at"] or 0.0),
                    "database": self.db_path.relative_to(self.project_root).as_posix(),
                    "legacyDatabasePresent": self.legacy_db_path.is_file(),
                    "coverage": {
                        "documentCount": int(coverage["document_count"] or 0),
                        "chunkCount": int(coverage["chunk_count"] or 0),
                        "totalBytes": int(coverage["total_bytes"] or 0),
                        "totalChars": int(coverage["total_chars"] or 0),
                    },
                }
                if persisted_state == INDEX_OK and check_stale:
                    stale_paths = self._stale_paths(conn)
                    if stale_paths:
                        result["status"] = INDEX_STALE
                        result["stalePaths"] = stale_paths[:32]
                return result
        except Exception as exc:
            return {
                "status": INDEX_ERROR,
                "schemaVersion": INDEX_SCHEMA_VERSION,
                "generation": "",
                "lastError": str(exc),
                "updatedAt": 0.0,
                "database": self.db_path.relative_to(self.project_root).as_posix(),
                "legacyDatabasePresent": self.legacy_db_path.is_file(),
                "coverage": {
                    "documentCount": 0,
                    "chunkCount": 0,
                    "totalBytes": 0,
                    "totalChars": 0,
                },
            }

    def _stale_paths(self, conn: sqlite3.Connection) -> List[str]:
        existing = {
            str(row["path"]): (int(row["mtime_ns"]), int(row["size_bytes"]))
            for row in conn.execute("SELECT path, mtime_ns, size_bytes FROM documents").fetchall()
        }
        stale: List[str] = []
        seen: set[str] = set()
        for path in self._candidate_files():
            relative = path.relative_to(self.project_root).as_posix()
            seen.add(relative)
            try:
                record_counter("statCount")
                stat = path.stat()
            except OSError:
                stale.append(relative)
                continue
            if existing.get(relative) != (int(stat.st_mtime_ns), int(stat.st_size)):
                stale.append(relative)
        stale.extend(sorted(set(existing) - seen))
        return list(dict.fromkeys(stale))

    def search_detailed(
        self,
        query: str,
        *,
        top_k: int = 20,
        candidate_limit: int = RECALL_CANDIDATE_LIMIT,
        path_prefix: Optional[str] = None,
    ) -> Dict[str, Any]:
        index = self.index_status(check_stale=True)
        status = str(index.get("status") or INDEX_ERROR)
        if status != INDEX_OK:
            return {
                "status": status,
                "resultState": "unavailable",
                "query": str(query or ""),
                "hits": [],
                "candidatePaths": [],
                "index": index,
                "error": str(index.get("lastError") or status),
            }
        try:
            query_tokens, rows = self._ranked_chunk_rows(
                query,
                path_prefix=path_prefix,
                requested_paths=max(int(top_k), int(candidate_limit), 1),
            )
            hits, candidate_paths = self._materialize_ranked_rows(
                rows,
                query_tokens,
                top_k=max(0, int(top_k)),
                candidate_limit=max(0, min(int(candidate_limit), RECALL_CANDIDATE_LIMIT)),
            )
        except Exception as exc:
            self._write_current_state(INDEX_ERROR, error=str(exc))
            index = self.index_status(check_stale=False)
            return {
                "status": INDEX_ERROR,
                "resultState": "unavailable",
                "query": str(query or ""),
                "hits": [],
                "candidatePaths": [],
                "index": index,
                "error": str(exc),
            }
        return {
            "status": INDEX_OK,
            "resultState": "hits" if hits else "no_hits",
            "query": str(query or ""),
            "hits": hits,
            "candidatePaths": candidate_paths,
            "index": index,
            "error": "",
        }

    def _ranked_chunk_rows(
        self,
        query: str,
        *,
        path_prefix: Optional[str],
        requested_paths: int,
    ) -> Tuple[List[str], List[sqlite3.Row]]:
        if not query or not str(query).strip():
            return [], []
        query_tokens = tokenize(query)[:MAX_QUERY_TOKENS]
        if not query_tokens:
            return [], []
        match_expr = " OR ".join(f'"{token}"' for token in query_tokens)
        row_limit = max(1, min(MAX_CHUNK_QUERY_ROWS, requested_paths))
        sql = """
            WITH ranked AS (
                SELECT path, chunk_id, content, start_char, end_char,
                       start_byte, end_byte, start_line, end_line, revision,
                       bm25(chunks) AS score
                FROM chunks
                WHERE chunks MATCH ?
        """
        params: List[Any] = [match_expr]
        if path_prefix:
            sql += " AND path LIKE ?"
            params.append(f"{path_prefix}%")
        sql += """
            ), best_per_path AS (
                SELECT *,
                       ROW_NUMBER() OVER(
                           PARTITION BY path ORDER BY score, chunk_id
                       ) AS path_rank
                FROM ranked
            )
            SELECT path, chunk_id, content, start_char, end_char,
                   start_byte, end_byte, start_line, end_line, revision, score
            FROM best_per_path
            WHERE path_rank = 1
            ORDER BY score, path
            LIMIT ?
        """
        params.append(row_limit)
        with self._lock, self._connect() as conn:
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                raise RetrievalIndexError(f"FTS5 query failed: {exc}") from exc
        return query_tokens, rows

    @classmethod
    def _materialize_ranked_rows(
        cls,
        rows: Sequence[sqlite3.Row],
        query_tokens: List[str],
        *,
        top_k: int,
        candidate_limit: int,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        best_by_path: Dict[str, sqlite3.Row] = {}
        for row in rows:
            path = str(row["path"] or "")
            if not path or cls._is_runtime_path(tuple(Path(path).parts)):
                continue
            best_by_path.setdefault(path, row)
        ranked = list(best_by_path.items())
        candidate_paths = [path for path, _row in ranked[:candidate_limit]]
        hits = [
            cls._materialize_hit(path, row, query_tokens)
            for path, row in ranked[:top_k]
        ]
        return hits, candidate_paths

    @classmethod
    def _materialize_hit(
        cls,
        path: str,
        row: sqlite3.Row,
        query_tokens: List[str],
    ) -> Dict[str, Any]:
        content = str(row["content"] or "")
        local_start, local_end, snippet = cls._snippet_with_offsets(content, query_tokens)
        chunk_start_char = int(row["start_char"])
        chunk_start_byte = int(row["start_byte"])
        chunk_start_line = int(row["start_line"])
        snippet_start_char = chunk_start_char + local_start
        snippet_end_char = chunk_start_char + local_end
        snippet_start_byte = chunk_start_byte + len(content[:local_start].encode("utf-8"))
        snippet_end_byte = chunk_start_byte + len(content[:local_end].encode("utf-8"))
        snippet_start_line = chunk_start_line + content[:local_start].count("\n")
        snippet_end_line = snippet_start_line + snippet[:-1].count("\n") if snippet else snippet_start_line
        return {
            "path": path,
            "score": float(row["score"] or 0.0),
            "revision": str(row["revision"] or ""),
            "chunkId": int(row["chunk_id"]),
            "span": {
                "startChar": chunk_start_char,
                "endChar": int(row["end_char"]),
                "startByte": chunk_start_byte,
                "endByte": int(row["end_byte"]),
                "startLine": chunk_start_line,
                "endLine": int(row["end_line"]),
                "revision": str(row["revision"] or ""),
                "endExclusive": True,
            },
            "snippet": snippet,
            "snippetSpan": {
                "startChar": snippet_start_char,
                "endChar": snippet_end_char,
                "startByte": snippet_start_byte,
                "endByte": snippet_end_byte,
                "startLine": snippet_start_line,
                "endLine": snippet_end_line,
                "revision": str(row["revision"] or ""),
                "endExclusive": True,
            },
        }

    @staticmethod
    def _snippet_with_offsets(content: str, query_tokens: List[str]) -> Tuple[int, int, str]:
        candidates: List[Tuple[Tuple[int, int, int, int], int, int]] = []
        cursor = 0
        for raw_line in content.splitlines(keepends=True):
            visible_end = len(raw_line.rstrip("\r\n"))
            leading = len(raw_line[:visible_end]) - len(raw_line[:visible_end].lstrip())
            trailing_text = raw_line[leading:visible_end].rstrip()
            start = cursor + leading
            end = start + len(trailing_text)
            selected = content[start:end]
            lowered = selected.lower()
            matched = [
                token
                for token in query_tokens
                if str(token).strip() and str(token).lower() in lowered
            ]
            if matched:
                score = (
                    len(set(matched)),
                    sum(len(token) for token in set(matched)),
                    sum(lowered.count(token.lower()) for token in set(matched)),
                    -start,
                )
                candidates.append((score, start, end))
            cursor += len(raw_line)
        if candidates:
            _score, start, end = max(candidates, key=lambda item: item[0])
        else:
            start, end = 0, min(len(content), SNIPPET_MAX_CHARS)
        if end - start > SNIPPET_MAX_CHARS:
            selected = content[start:end]
            lowered = selected.lower()
            positions = [
                lowered.find(token.lower())
                for token in query_tokens
                if lowered.find(token.lower()) >= 0
            ]
            anchor = min(positions) if positions else 0
            local = max(
                0,
                min(
                    anchor - SNIPPET_MAX_CHARS // 3,
                    len(selected) - SNIPPET_MAX_CHARS,
                ),
            )
            start += local
            end = start + SNIPPET_MAX_CHARS
        return start, end, content[start:end]

    # -------------------- compatibility list APIs --------------------

    def _legacy_search_detailed(
        self,
        query: str,
        *,
        top_k: int,
        candidate_limit: int,
        path_prefix: Optional[str],
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        query_tokens, rows = self._ranked_chunk_rows(
            query,
            path_prefix=path_prefix,
            requested_paths=max(top_k, candidate_limit, 1),
        )
        return self._materialize_ranked_rows(
            rows,
            query_tokens,
            top_k=max(0, int(top_k)),
            candidate_limit=max(0, min(int(candidate_limit), RECALL_CANDIDATE_LIMIT)),
        )

    def search(
        self,
        query: str,
        *,
        top_k: int = 20,
        path_prefix: Optional[str] = None,
    ) -> List[Tuple[str, float, str]]:
        """Compatibility API. Structured callers must use search_detailed()."""
        hits, _candidates = self._legacy_search_detailed(
            query,
            top_k=top_k,
            candidate_limit=top_k,
            path_prefix=path_prefix,
        )
        return [
            (str(hit["path"]), float(hit["score"]), str(hit["snippet"]))
            for hit in hits
        ]

    def search_with_candidates(
        self,
        query: str,
        *,
        top_k: int = 20,
        candidate_limit: int = RECALL_CANDIDATE_LIMIT,
        path_prefix: Optional[str] = None,
    ) -> Tuple[List[Tuple[str, float, str]], List[str]]:
        """Compatibility API for non-Agent consumers."""
        hits, candidates = self._legacy_search_detailed(
            query,
            top_k=top_k,
            candidate_limit=candidate_limit,
            path_prefix=path_prefix,
        )
        return (
            [
                (str(hit["path"]), float(hit["score"]), str(hit["snippet"]))
                for hit in hits
            ],
            candidates,
        )

    @staticmethod
    def _is_runtime_path(relative_parts: Tuple[str, ...]) -> bool:
        """Runtime data and generated projections are excluded from retrieval."""
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
