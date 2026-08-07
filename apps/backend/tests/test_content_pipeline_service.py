from __future__ import annotations

import os
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from watchdog.events import DirModifiedEvent, FileDeletedEvent, FileModifiedEvent, FileMovedEvent

from services import content_pipeline_service
from services.content_catalog_service import (
    get_content_catalog_service,
    reset_content_catalog_services,
)
from services.content_pipeline_service import ContentPipelineService, WorkspaceEventHandler
from services.retrieval_service import (
    INDEX_OK,
    get_retrieval_service,
    reset_retrieval_cache,
)


@pytest.fixture(autouse=True)
def _reset_content_services() -> None:
    reset_retrieval_cache()
    reset_content_catalog_services()
    yield
    reset_retrieval_cache()
    reset_content_catalog_services()


def _write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting for the content pipeline")


def test_workspace_event_handler_emits_files_and_both_move_paths(tmp_path: Path) -> None:
    observed: list[list[Path]] = []
    handler = WorkspaceEventHandler(lambda paths: observed.append(list(paths)))
    source = tmp_path / "chapters" / "old.md"
    target = tmp_path / "chapters" / "new.md"

    handler.on_modified(FileModifiedEvent(str(source)))
    handler.on_modified(DirModifiedEvent(str(source.parent)))
    handler.on_deleted(FileDeletedEvent(str(source)))
    handler.on_moved(FileMovedEvent(str(source), str(target)))

    assert observed == [[source], [source], [source, target]]


def test_catalog_projection_updates_only_changed_file_and_exactly_handles_rename_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _write(tmp_path, "chapters/first.md", "alphaunique\n")
    _write(tmp_path, "chapters/second.md", "stableunique\n")
    pipeline = ContentPipelineService()
    pipeline.process_workspace(tmp_path)
    retrieval = get_retrieval_service(tmp_path)
    catalog = get_content_catalog_service(tmp_path)
    indexed_paths: list[str] = []
    original_index_document = retrieval._index_document

    def tracked_index(*args, **kwargs):
        indexed_paths.append(str(args[2]))
        return original_index_document(*args, **kwargs)

    monkeypatch.setattr(retrieval, "_index_document", tracked_index)
    first.write_text("bravounique\n", encoding="utf-8")
    catalog.notify_external_changes([first])
    pipeline.process_workspace(tmp_path)

    assert indexed_paths == ["chapters/first.md"]
    assert retrieval.search_detailed("bravounique")["resultState"] == "hits"
    assert retrieval.search_detailed("alphaunique")["resultState"] == "no_hits"
    assert retrieval.search_detailed("stableunique")["resultState"] == "hits"

    renamed = first.with_name("renamed.md")
    first.rename(renamed)
    catalog.notify_external_changes([first, renamed])
    pipeline.process_workspace(tmp_path)
    renamed_hit = retrieval.search_detailed("bravounique")["hits"][0]
    assert renamed_hit["path"] == "chapters/renamed.md"

    renamed.unlink()
    catalog.notify_external_changes([renamed])
    pipeline.process_workspace(tmp_path)
    assert retrieval.search_detailed("bravounique")["resultState"] == "no_hits"


def test_reconciliation_recovers_missed_same_size_same_mtime_change(tmp_path: Path) -> None:
    source = _write(tmp_path, "chapters/001.md", "alpha\n")
    pipeline = ContentPipelineService()
    pipeline.process_workspace(tmp_path)
    retrieval = get_retrieval_service(tmp_path)
    before = source.stat()

    source.write_text("bravo\n", encoding="utf-8")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))

    assert retrieval.search_detailed("bravo")["resultState"] == "no_hits"
    result = pipeline.reconcile_workspace(tmp_path)

    assert result["reconciliationChangedCount"] == 1
    assert retrieval.search_detailed("bravo")["resultState"] == "hits"
    assert get_content_catalog_service(tmp_path).background_metrics == {
        "catalogEventLagMs": pytest.approx(0.0, abs=1000.0),
        "reconciliationScanCount": 1,
        "reconciliationChangedCount": 1,
    }


def test_incremental_index_failure_keeps_last_good_chunks_and_returns_explicit_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path, "chapters/001.md", "stable-pipeline-token\n")
    pipeline = ContentPipelineService()
    pipeline.process_workspace(tmp_path)
    retrieval = get_retrieval_service(tmp_path)
    source.write_text("explode-pipeline-token\n", encoding="utf-8")
    get_content_catalog_service(tmp_path).notify_external_changes([source])
    original_tokenized = retrieval._tokenized

    monkeypatch.setattr(
        retrieval,
        "_tokenized",
        lambda text: (_ for _ in ()).throw(RuntimeError("forced-incremental-failure")),
    )
    with pytest.raises(RuntimeError, match="forced-incremental-failure"):
        pipeline.process_workspace(tmp_path)

    failed = retrieval.search_detailed("explode-pipeline-token")
    assert failed["resultState"] == "unavailable"
    assert failed["status"] != INDEX_OK
    assert retrieval.search("stable-pipeline-token")

    monkeypatch.setattr(retrieval, "_tokenized", original_tokenized)
    pipeline.process_workspace(tmp_path)
    assert retrieval.search_detailed("explode-pipeline-token")["resultState"] == "hits"


def test_event_arriving_during_publication_keeps_next_background_wakeup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path, "chapters/001.md", "firstraceunique\n")
    pipeline = ContentPipelineService()
    pipeline.process_workspace(tmp_path)
    retrieval = get_retrieval_service(tmp_path)
    catalog = get_content_catalog_service(tmp_path)
    source.write_text("secondraceunique\n", encoding="utf-8")
    catalog.notify_external_changes([source])
    original_refresh = retrieval.refresh_from_catalog

    def refresh_and_receive_next_event(snapshot):
        updated = original_refresh(snapshot)
        source.write_text("thirdraceunique\n", encoding="utf-8")
        catalog.notify_external_changes([source])
        with pipeline._condition:
            pipeline._schedule_unlocked(tmp_path.resolve(), delay=10.0)
        return updated

    monkeypatch.setattr(retrieval, "refresh_from_catalog", refresh_and_receive_next_event)
    pipeline.process_workspace(tmp_path)

    assert catalog.dirty_file_count == 1
    assert tmp_path.resolve() in pipeline._pending

    monkeypatch.setattr(retrieval, "refresh_from_catalog", original_refresh)
    pipeline.process_workspace(tmp_path)
    assert retrieval.search_detailed("thirdraceunique")["resultState"] == "hits"


def test_real_workspace_watcher_publishes_external_create(tmp_path: Path) -> None:
    (tmp_path / "chapters").mkdir()
    pipeline = ContentPipelineService(
        event_debounce_seconds=0.05,
        reconciliation_interval_seconds=60.0,
    )
    pipeline.start()
    try:
        pipeline.bootstrap_workspace(tmp_path)
        _write(tmp_path, "chapters/external.md", "native-watcher-token\n")

        def published() -> bool:
            result = get_retrieval_service(tmp_path).search_detailed("native-watcher-token")
            return result["status"] == INDEX_OK and result["resultState"] == "hits"

        _wait_until(published)
        assert pipeline.health(tmp_path)["watcherError"] == ""
    finally:
        pipeline.stop()


def test_warm_query_has_no_workspace_scan_stat_or_source_read(tmp_path: Path) -> None:
    _write(tmp_path, "chapters/001.md", "zero-io-query-token\n")
    ContentPipelineService().process_workspace(tmp_path)
    retrieval = get_retrieval_service(tmp_path)
    counts = {"scan": 0, "stat": 0, "read": 0}

    def track(method_name: str, counter_name: str, stack: ExitStack) -> None:
        original = getattr(Path, method_name)

        def counted(path: Path, *args, **kwargs):
            counts[counter_name] += 1
            return original(path, *args, **kwargs)

        stack.enter_context(patch.object(Path, method_name, counted))

    with ExitStack() as stack:
        for method in ("rglob", "glob", "iterdir"):
            track(method, "scan", stack)
        track("stat", "stat", stack)
        track("read_bytes", "read", stack)
        track("read_text", "read", stack)
        result = retrieval.search_detailed("zero-io-query-token")

    assert result["status"] == INDEX_OK
    assert result["resultState"] == "hits"
    assert counts == {"scan": 0, "stat": 0, "read": 0}


def test_production_query_is_stale_when_workspace_watcher_registration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenObserver:
        def start(self) -> None:
            pass

        def schedule(self, *_args, **_kwargs):
            raise OSError("native-watcher-unavailable")

        def stop(self) -> None:
            pass

        def join(self, **_kwargs) -> None:
            pass

    _write(tmp_path, "chapters/001.md", "watcher-health-token\n")
    pipeline = ContentPipelineService(observer_factory=BrokenObserver)
    monkeypatch.setattr(content_pipeline_service, "_PIPELINE", pipeline)
    pipeline.start()
    try:
        pipeline.bootstrap_workspace(tmp_path)
        result = get_retrieval_service(tmp_path).search_detailed("watcher-health-token")

        assert result["status"] != INDEX_OK
        assert result["resultState"] == "unavailable"
        assert result["index"]["contentMonitor"]["watcherError"] == (
            "native-watcher-unavailable"
        )
    finally:
        pipeline.stop()
