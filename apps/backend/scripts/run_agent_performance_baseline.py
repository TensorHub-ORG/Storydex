from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict
from unittest.mock import patch
from uuid import uuid4


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.context_policy import ContextPolicy  # noqa: E402
from services.content_catalog_service import get_content_catalog_service  # noqa: E402
from services.retrieval_service import RetrievalService  # noqa: E402
from services.story_project_service import StoryProjectService  # noqa: E402
from services.storydex_orchestration_service import StorydexOrchestrationService  # noqa: E402


class _PathOperationCounter:
    def __init__(self) -> None:
        self.stat_count = 0
        self.directory_scan_count = 0
        self.read_count = 0
        self._stack = ExitStack()

    def __enter__(self) -> "_PathOperationCounter":
        self._patch("stat", "stat_count")
        self._patch("iterdir", "directory_scan_count")
        self._patch("glob", "directory_scan_count")
        self._patch("rglob", "directory_scan_count")
        self._patch("read_bytes", "read_count")
        self._patch("read_text", "read_count")
        self._stack.__enter__()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._stack.__exit__(*exc_info)

    def _patch(self, method_name: str, counter_name: str) -> None:
        original = getattr(Path, method_name)

        def counted(path: Path, *args: Any, **kwargs: Any) -> Any:
            setattr(self, counter_name, int(getattr(self, counter_name)) + 1)
            return original(path, *args, **kwargs)

        self._stack.enter_context(patch.object(Path, method_name, counted))


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


def _measure(operation: Callable[[], Any]) -> tuple[Any, float, _PathOperationCounter]:
    counter = _PathOperationCounter()
    started = time.perf_counter()
    with counter:
        result = operation()
    return result, (time.perf_counter() - started) * 1000, counter


def _build_fixture(root: Path, *, chapter_count: int) -> None:
    service = StoryProjectService()
    service.ensure_project_structure(root)
    for number in range(1, chapter_count + 1):
        chapter = root / "chapters" / f"第{number:04d}章 基线"
        chapter.mkdir(parents=True, exist_ok=True)
        (chapter / "001.md").write_text(
            f"第{number}章\n\n这是用于固定性能基线的正文。\n",
            encoding="utf-8",
        )


def _add_retrieval_files(root: Path, *, total_file_count: int, existing_count: int) -> None:
    remaining = max(0, total_file_count - existing_count)
    fixture_root = root / ".storydex" / "scripts" / "performance-baseline"
    fixture_root.mkdir(parents=True, exist_ok=True)
    for index in range(remaining):
        (fixture_root / f"source-{index:05d}.md").write_text(
            f"性能基线来源 {index}\n\n唯一内容标记 baseline-{index:05d}。\n",
            encoding="utf-8",
        )


def run_baseline(
    workspace_root: Path,
    *,
    chapter_count: int,
    file_count: int,
    iterations: int,
) -> Dict[str, Any]:
    root = Path(workspace_root).resolve()
    _build_fixture(root, chapter_count=chapter_count)
    project_service = StoryProjectService()
    orchestration = StorydexOrchestrationService(story_project_service=project_service)
    policy = ContextPolicy(
        story_structured_memory=False,
        passive_fts=False,
        wiki_context=False,
        coomi_memory=False,
        active_retrieval_tools=False,
    )

    def build_contract() -> Dict[str, Any]:
        return orchestration.build_turn_contract(
            root,
            prompt="请概括当前项目状态，不修改文件。",
            context_policy=policy,
            trace_id="performance-baseline",
            session_id="performance-baseline",
        )

    warmup, warmup_ms, warmup_counter = _measure(build_contract)
    samples: list[Dict[str, Any]] = []
    durations: list[float] = []
    for _ in range(max(1, iterations)):
        contract, elapsed_ms, counter = _measure(build_contract)
        durations.append(elapsed_ms)
        samples.append(
            {
                "durationMs": round(elapsed_ms, 3),
                "pathStatCount": counter.stat_count,
                "directoryScanCount": counter.directory_scan_count,
                "readCount": counter.read_count,
                "trace": contract.get("performanceTrace") or {},
            }
        )

    _add_retrieval_files(root, total_file_count=file_count, existing_count=chapter_count)
    retrieval = RetrievalService(root)
    _initial_count, initial_index_ms, initial_counter = _measure(retrieval.build_index)
    catalog_snapshot = get_content_catalog_service(root).snapshot()
    warm_update_count, warm_refresh_ms, warm_refresh_counter = _measure(
        lambda: retrieval.refresh_from_catalog(catalog_snapshot)
    )
    index_status, status_ms, status_counter = _measure(
        lambda: retrieval.index_status(check_stale=True)
    )
    query_result, query_ms, query_counter = _measure(
        lambda: retrieval.search_detailed("baseline-00001", top_k=1)
    )

    return {
        "_type": "AgentPerformanceBaseline",
        "_version": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "fixture": {
            "chapterCount": chapter_count,
            "targetFileCount": file_count,
            "iterations": max(1, iterations),
        },
        "turnContract": {
            "warmupMs": round(warmup_ms, 3),
            "warmupPathStatCount": warmup_counter.stat_count,
            "warmupTrace": warmup.get("performanceTrace") or {},
            "medianMs": round(statistics.median(durations), 3),
            "p95Ms": round(_percentile(durations, 0.95), 3),
            "samples": samples,
        },
        "retrieval": {
            "initialBuildMs": round(initial_index_ms, 3),
            "initialPathStatCount": initial_counter.stat_count,
            "initialDirectoryScanCount": initial_counter.directory_scan_count,
            "warmRefreshMs": round(warm_refresh_ms, 3),
            "warmUpdatedFileCount": int(warm_update_count or 0),
            "warmPathStatCount": warm_refresh_counter.stat_count,
            "warmDirectoryScanCount": warm_refresh_counter.directory_scan_count,
            "staleCheckMs": round(status_ms, 3),
            "staleCheckPathStatCount": status_counter.stat_count,
            "staleCheckDirectoryScanCount": status_counter.directory_scan_count,
            "status": index_status,
            "warmQueryMs": round(query_ms, 3),
            "warmQueryPathStatCount": query_counter.stat_count,
            "warmQueryDirectoryScanCount": query_counter.directory_scan_count,
            "warmQueryReadCount": query_counter.read_count,
            "warmQueryResultState": query_result.get("resultState"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build repeatable Storydex Agent performance baselines.")
    parser.add_argument("--workspace-root", default="")
    parser.add_argument("--chapters", type=int, default=300)
    parser.add_argument("--files", type=int, default=2000)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if str(args.workspace_root or "").strip():
        workspace_root = Path(args.workspace_root).resolve()
    else:
        temporary = tempfile.TemporaryDirectory(prefix="storydex-agent-performance-")
        workspace_root = Path(temporary.name).resolve()

    try:
        report = run_baseline(
            workspace_root,
            chapter_count=max(1, int(args.chapters)),
            file_count=max(1, int(args.files)),
            iterations=max(1, int(args.iterations)),
        )
        output_path = (
            Path(args.output).resolve()
            if str(args.output or "").strip()
            else REPOSITORY_ROOT
            / "output"
            / "agent-performance-baseline"
            / uuid4().hex[:10]
            / "baseline-report.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"status": "passed", "report": output_path.as_posix()}))
        return 0
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
