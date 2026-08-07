from __future__ import annotations

import functools
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, TypeVar, cast


_F = TypeVar("_F", bound=Callable[..., Any])


@dataclass
class TurnPerformanceRecorder:
    """Low-overhead counters for one TurnContract build."""

    directory_scan_count: int = 0
    stat_count: int = 0
    read_count: int = 0
    chapter_snapshot_build_count: int = 0
    timings_ms: Dict[str, float] = field(default_factory=dict)
    values: Dict[str, Any] = field(default_factory=dict)

    def increment(self, name: str, count: int = 1) -> None:
        amount = max(0, int(count or 0))
        if name == "directoryScanCount":
            self.directory_scan_count += amount
        elif name == "statCount":
            self.stat_count += amount
        elif name == "readCount":
            self.read_count += amount
        elif name == "chapterSnapshotBuildCount":
            self.chapter_snapshot_build_count += amount
        else:
            self.values[name] = int(self.values.get(name) or 0) + amount

    def add_duration(self, name: str, elapsed_ms: float) -> None:
        self.timings_ms[name] = self.timings_ms.get(name, 0.0) + max(
            0.0, float(elapsed_ms or 0.0)
        )

    def set_value(self, name: str, value: Any) -> None:
        self.values[name] = value

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "_type": "TurnPerformanceTrace",
            "_version": 1,
            "contractBuildMs": round(float(self.timings_ms.get("contractBuildMs") or 0.0), 3),
            "directoryScanCount": self.directory_scan_count,
            "statCount": self.stat_count,
            "readCount": self.read_count,
            "chapterSnapshotBuildCount": self.chapter_snapshot_build_count,
            "wikiRefreshMs": round(float(self.timings_ms.get("wikiRefreshMs") or 0.0), 3),
            "ftsRefreshMs": round(float(self.timings_ms.get("ftsRefreshMs") or 0.0), 3),
        }
        for name, elapsed_ms in self.timings_ms.items():
            if name not in payload:
                payload[name] = round(float(elapsed_ms or 0.0), 3)
        payload.update(self.values)
        return payload


_ACTIVE_RECORDER: ContextVar[TurnPerformanceRecorder | None] = ContextVar(
    "storydex_turn_performance_recorder",
    default=None,
)


def activate_turn_performance(recorder: TurnPerformanceRecorder) -> Token[TurnPerformanceRecorder | None]:
    return _ACTIVE_RECORDER.set(recorder)


def deactivate_turn_performance(token: Token[TurnPerformanceRecorder | None]) -> None:
    _ACTIVE_RECORDER.reset(token)


def record_counter(name: str, count: int = 1) -> None:
    recorder = _ACTIVE_RECORDER.get()
    if recorder is not None:
        recorder.increment(name, count)


def record_duration(name: str, elapsed_ms: float) -> None:
    recorder = _ACTIVE_RECORDER.get()
    if recorder is not None:
        recorder.add_duration(name, elapsed_ms)


def record_value(name: str, value: Any) -> None:
    recorder = _ACTIVE_RECORDER.get()
    if recorder is not None:
        recorder.set_value(name, value)


def trace_turn_contract(function: _F) -> _F:
    """Attach one isolated performance record to a TurnContract result."""

    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        recorder = TurnPerformanceRecorder()
        token = activate_turn_performance(recorder)
        started = time.perf_counter()
        try:
            result = function(*args, **kwargs)
        finally:
            recorder.add_duration("contractBuildMs", (time.perf_counter() - started) * 1000)
            deactivate_turn_performance(token)

        if not isinstance(result, dict):
            return result
        performance = recorder.to_dict()
        result["performanceTrace"] = performance
        context_assembly = result.get("contextAssembly")
        if isinstance(context_assembly, dict):
            context_trace = context_assembly.get("contextTrace")
            if isinstance(context_trace, dict):
                context_trace["performance"] = dict(performance)
                totals = context_trace.get("totals")
                if isinstance(totals, dict):
                    totals.update(
                        {
                            key: performance[key]
                            for key in (
                                "contractBuildMs",
                                "directoryScanCount",
                                "statCount",
                                "readCount",
                                "chapterSnapshotBuildCount",
                                "wikiRefreshMs",
                                "ftsRefreshMs",
                            )
                        }
                    )
        return result

    return cast(_F, wrapped)
