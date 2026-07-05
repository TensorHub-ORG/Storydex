"""WP-3.1 · 影子路径 Job Queue（SQLite 持久化，07 §5.4）。

供 P3 / P5 影子任务（trace 落盘 / hooks / file backup / stage2 / summary）
使用的轻量 job 队列。所有 job 持久化到 ``.storydex/jobs.db``，
进程崩溃后启动时通过 ``process_pending`` 恢复未完成任务。

核心设计
--------

* SQLite 单文件 + WAL 模式；进程内有 worker 协程消费。
* 每个 job 有 ``kind / payload / status / retry_count / max_retries / last_error``；
  超过 ``max_retries`` 的任务会持久化到 ``.storydex/failures/`` 留档。
* ``dedup_key`` 可选：相同 dedup_key 的 pending job 不重复入队。
* ``register_handler(kind, handler)`` 注册 kind → handler；handler 是
  ``Callable[[dict], Awaitable[None]]`` 或同步 ``Callable[[dict], None]``。
* 状态：``pending`` → ``running`` → ``done`` / ``failed``。

不引入新 Flag —— 队列本身是基础设施；是否使用由各 P3 WP 自己的 Flag 决定。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

JobHandler = Callable[[Dict[str, Any]], Union[Awaitable[None], None]]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    dedup_key TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    enqueued_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    project_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_kind ON jobs(status, kind);
CREATE INDEX IF NOT EXISTS idx_jobs_dedup ON jobs(dedup_key, status) WHERE dedup_key IS NOT NULL;
"""


@dataclass
class JobRecord:
    id: str
    kind: str
    payload: Dict[str, Any]
    dedup_key: Optional[str] = None
    status: str = "pending"
    retry_count: int = 0
    max_retries: int = 3
    last_error: str = ""
    enqueued_at: float = 0.0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    project_id: Optional[str] = None


class JobQueue:
    """SQLite 持久化的影子任务队列。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._handlers: Dict[str, JobHandler] = {}
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    # ─────────────────────── public api ───────────────────────

    def register_handler(self, kind: str, handler: JobHandler) -> None:
        with self._lock:
            self._handlers[kind] = handler

    def enqueue(
        self,
        *,
        kind: str,
        payload: Dict[str, Any],
        dedup_key: Optional[str] = None,
        max_retries: int = 3,
        project_id: Optional[str] = None,
    ) -> str:
        import uuid
        job_id = uuid.uuid4().hex
        with self._lock, self._connect() as conn:
            if dedup_key:
                row = conn.execute(
                    "SELECT id FROM jobs WHERE dedup_key=? AND status IN ('pending','running')",
                    (dedup_key,),
                ).fetchone()
                if row is not None:
                    return str(row["id"])
            conn.execute(
                """
                INSERT INTO jobs (id, kind, payload, dedup_key, status, retry_count, max_retries, enqueued_at, project_id)
                VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    job_id,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    dedup_key,
                    int(max_retries),
                    time.time(),
                    project_id,
                ),
            )
        return job_id

    def get(self, job_id: str) -> Optional[JobRecord]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row_to_record(row) if row else None

    def list_pending(self, kind: Optional[str] = None, limit: int = 100) -> List[JobRecord]:
        with self._lock, self._connect() as conn:
            if kind is None:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status='pending' ORDER BY enqueued_at LIMIT ?",
                    (int(limit),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status='pending' AND kind=? ORDER BY enqueued_at LIMIT ?",
                    (kind, int(limit)),
                ).fetchall()
        return [_row_to_record(r) for r in rows]

    def queue_depth(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE status='pending'").fetchone()
        return int(row["n"]) if row else 0

    async def process_pending(self) -> int:
        """启动时扫一次未完成 jobs。返回处理数量。"""
        processed = 0
        for job in self.list_pending(limit=1000):
            try:
                await self._run_job(job)
            except Exception as exc:
                logger.exception("job %s 处理失败: %s", job.id, exc)
            processed += 1
        return processed

    async def wait_for(
        self,
        *,
        kind: str,
        project_id: Optional[str] = None,
        timeout_ms: int = 5000,
    ) -> bool:
        """等待该 kind / project_id 下所有 pending job 完成。

        WP-3.5 stage2 barrier 用：超时返回 False，caller 标 stale_state_warning。
        """
        deadline = time.time() + max(0.0, timeout_ms / 1000.0)
        while time.time() < deadline:
            with self._lock, self._connect() as conn:
                if project_id is None:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n FROM jobs WHERE kind=? AND status IN ('pending','running')",
                        (kind,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n FROM jobs WHERE kind=? AND project_id=? AND status IN ('pending','running')",
                        (kind, project_id),
                    ).fetchone()
            if int(row["n"]) == 0:
                return True
            await asyncio.sleep(0.05)
        return False

    # ─────────────────────── internal ───────────────────────

    async def _run_job(self, job: JobRecord) -> None:
        with self._lock:
            handler = self._handlers.get(job.kind)
        if handler is None:
            self._mark(job.id, status="failed", last_error=f"no handler for kind={job.kind}")
            return

        self._mark(job.id, status="running", started_at=time.time())
        try:
            ret = handler(job.payload)
            if asyncio.iscoroutine(ret):
                await ret
            self._mark(job.id, status="done", finished_at=time.time())
        except Exception as exc:
            error_text = f"{exc.__class__.__name__}: {exc}\n{traceback.format_exc()[:1000]}"
            with self._lock, self._connect() as conn:
                row = conn.execute("SELECT retry_count, max_retries FROM jobs WHERE id=?", (job.id,)).fetchone()
                retry_count = int(row["retry_count"]) + 1 if row else 1
                max_retries = int(row["max_retries"]) if row else job.max_retries
                if retry_count >= max_retries:
                    conn.execute(
                        "UPDATE jobs SET status='failed', last_error=?, retry_count=?, finished_at=? WHERE id=?",
                        (error_text[:2000], retry_count, time.time(), job.id),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET status='pending', last_error=?, retry_count=? WHERE id=?",
                        (error_text[:2000], retry_count, job.id),
                    )

    def _mark(
        self,
        job_id: str,
        *,
        status: Optional[str] = None,
        last_error: Optional[str] = None,
        started_at: Optional[float] = None,
        finished_at: Optional[float] = None,
    ) -> None:
        sets: List[str] = []
        params: List[Any] = []
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if last_error is not None:
            sets.append("last_error=?")
            params.append(last_error[:2000])
        if started_at is not None:
            sets.append("started_at=?")
            params.append(started_at)
        if finished_at is not None:
            sets.append("finished_at=?")
            params.append(finished_at)
        if not sets:
            return
        params.append(job_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", params)


def _row_to_record(row: sqlite3.Row) -> JobRecord:
    payload = {}
    with contextlib.suppress(Exception):
        payload = json.loads(row["payload"]) if row["payload"] else {}
    return JobRecord(
        id=str(row["id"]),
        kind=str(row["kind"]),
        payload=payload if isinstance(payload, dict) else {},
        dedup_key=row["dedup_key"],
        status=str(row["status"]),
        retry_count=int(row["retry_count"]),
        max_retries=int(row["max_retries"]),
        last_error=row["last_error"] or "",
        enqueued_at=float(row["enqueued_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        project_id=row["project_id"],
    )


_DEFAULT_QUEUE: Optional[JobQueue] = None
_DEFAULT_QUEUE_LOCK = Lock()


def get_default_queue(db_path: Optional[Path] = None) -> JobQueue:
    """单例入口；默认路径来自 ``settings.workspace_root / .storydex/jobs.db``。"""
    global _DEFAULT_QUEUE
    with _DEFAULT_QUEUE_LOCK:
        if _DEFAULT_QUEUE is not None:
            return _DEFAULT_QUEUE
        if db_path is None:
            from core.config import get_settings
            settings = get_settings()
            db_path = Path(settings.workspace_root) / ".storydex" / "jobs.db"
        _DEFAULT_QUEUE = JobQueue(db_path)
    return _DEFAULT_QUEUE


def reset_default_queue() -> None:
    global _DEFAULT_QUEUE
    with _DEFAULT_QUEUE_LOCK:
        _DEFAULT_QUEUE = None


# 编码自检
_ENCODING_SELFTEST = "JobQueue 编码自检：影子任务 / 持久化 / 重试"
assert "�" not in _ENCODING_SELFTEST, "job_queue.py 含 replacement char"
