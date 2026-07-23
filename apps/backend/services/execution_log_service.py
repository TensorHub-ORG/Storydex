from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterator, Optional


_CURRENT_EXECUTION_LOG_SESSION: ContextVar["ExecutionLogSession | None"] = ContextVar(
    "storydex_execution_log_session",
    default=None,
)


@dataclass
class ExecutionLogSession:
    path: Path
    trace_id: str
    session_id: str
    request_kind: str
    workspace_root: str
    storydex_root: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    _sequence: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def bind(self, **metadata: Any) -> None:
        for key, value in metadata.items():
            if value in (None, ""):
                continue
            self.metadata[str(key)] = sanitize_execution_log_payload(value)

    def write(
        self,
        event: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        category: str = "runtime",
        level: str = "info",
        trace: Optional[Dict[str, Any]] = None,
    ) -> None:
        entry = {
            "sequence": 0,
            "timestamp": datetime.now().astimezone().isoformat(),
            "traceId": self.trace_id,
            "sessionId": self.session_id,
            "requestKind": self.request_kind,
            "category": str(category or "runtime"),
            "level": str(level or "info"),
            "event": str(event or "unknown"),
            "workspaceRoot": self.workspace_root,
            "storydexRoot": self.storydex_root,
            "metadata": sanitize_execution_log_payload(dict(self.metadata)),
            "payload": sanitize_execution_log_payload(dict(payload or {})),
        }
        if isinstance(trace, dict) and trace:
            entry["trace"] = sanitize_execution_log_payload(trace)

        with self._lock:
            self._sequence += 1
            entry["sequence"] = self._sequence
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def sanitize_execution_log_payload(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated:depth]"

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        if len(value) <= 120000:
            return value
        return value[:119997] + "..."

    if isinstance(value, dict):
        sanitized: Dict[str, Any] = {}
        max_items = 512
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                sanitized["__truncated_items__"] = len(value) - max_items
                break
            sanitized[str(key)] = sanitize_execution_log_payload(item, depth=depth + 1)
        return sanitized

    if isinstance(value, (list, tuple, set)):
        items = list(value)
        max_items = 512
        result = [
            sanitize_execution_log_payload(item, depth=depth + 1)
            for item in items[:max_items]
        ]
        if len(items) > max_items:
            result.append(f"...[{len(items) - max_items} more item(s)]")
        return result

    text = str(value)
    if len(text) <= 120000:
        return text
    return text[:119997] + "..."


def create_execution_log_session(
    *,
    trace_id: str,
    session_id: str,
    request_kind: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> ExecutionLogSession:
    from services.project_service import get_project_service

    project_service = get_project_service()
    log_dir = project_service.agent_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = _build_unique_log_path(log_dir)
    return ExecutionLogSession(
        path=log_path,
        trace_id=str(trace_id or "").strip(),
        session_id=str(session_id or "default").strip() or "default",
        request_kind=str(request_kind or "agent_run").strip() or "agent_run",
        workspace_root=project_service.workspace_root.as_posix(),
        storydex_root=project_service.storydex_root.as_posix(),
        metadata=sanitize_execution_log_payload(dict(metadata or {})),
    )


def get_current_execution_log_session() -> ExecutionLogSession | None:
    return _CURRENT_EXECUTION_LOG_SESSION.get()


@contextmanager
def use_execution_log_session(session: ExecutionLogSession | None) -> Iterator[None]:
    token: Token[ExecutionLogSession | None] = _CURRENT_EXECUTION_LOG_SESSION.set(session)
    try:
        yield
    finally:
        _CURRENT_EXECUTION_LOG_SESSION.reset(token)


def _build_unique_log_path(log_dir: Path) -> Path:
    timestamp = datetime.now().astimezone().strftime("%Y-%m%d-%H-%M-%S")
    candidate = log_dir / f"{timestamp}.jsonl"
    if not candidate.exists():
        return candidate

    for index in range(2, 1000):
        candidate = log_dir / f"{timestamp}-{index:02d}.jsonl"
        if not candidate.exists():
            return candidate
    return log_dir / f"{timestamp}-overflow.jsonl"
