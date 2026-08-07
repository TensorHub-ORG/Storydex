from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterable

from watchdog.events import FileSystemEvent, FileSystemEventHandler, FileSystemMovedEvent
from watchdog.observers import Observer


logger = logging.getLogger(__name__)

DEFAULT_EVENT_DEBOUNCE_SECONDS = 0.2
DEFAULT_RECONCILIATION_INTERVAL_SECONDS = 300.0
MAX_RETRY_DELAY_SECONDS = 60.0
MAX_SYNC_CONVERGENCE_PASSES = 3


class WorkspaceEventHandler(FileSystemEventHandler):
    """Translate native workspace events into exact catalog dirty paths."""

    def __init__(self, callback: Callable[[Iterable[Path]], None]) -> None:
        super().__init__()
        self._callback = callback

    def on_created(self, event: FileSystemEvent) -> None:
        self._emit(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._emit(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._emit(event.src_path)

    def on_moved(self, event: FileSystemMovedEvent) -> None:
        self._emit(event.src_path, event.dest_path)

    def _emit(self, *values: str) -> None:
        paths = [Path(value) for value in values if str(value or "").strip()]
        if paths:
            self._callback(paths)


class ContentPipelineService:
    """Own filesystem events, catalog publication, and FTS projection refresh."""

    def __init__(
        self,
        *,
        event_debounce_seconds: float = DEFAULT_EVENT_DEBOUNCE_SECONDS,
        reconciliation_interval_seconds: float = DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
        observer_factory: Callable[[], Any] = Observer,
    ) -> None:
        self.event_debounce_seconds = max(0.0, float(event_debounce_seconds))
        self.reconciliation_interval_seconds = max(
            0.05,
            float(reconciliation_interval_seconds),
        )
        self._observer_factory = observer_factory
        self._condition = threading.Condition(threading.RLock())
        self._observer: Any | None = None
        self._worker: threading.Thread | None = None
        self._roots: set[Path] = set()
        self._watches: Dict[Path, Any] = {}
        self._pending: Dict[Path, float] = {}
        self._failures: Dict[Path, int] = {}
        self._watcher_errors: Dict[Path, str] = {}
        # A workspace has one catalog and one projection bundle.  Watcher
        # callbacks and synchronous API calls must not publish those two
        # artifacts concurrently (Windows can reject the second os.replace
        # with WinError 32).  Keep one re-entrant lock per workspace so the
        # pipeline worker and explicit sync share the same serialization.
        self._workspace_locks: Dict[Path, threading.RLock] = {}
        self._processing_roots: set[Path] = set()
        self._deferred_paths: Dict[Path, set[Path]] = {}
        self._stopping = False
        self._next_reconciliation = time.monotonic() + self.reconciliation_interval_seconds

    @property
    def running(self) -> bool:
        with self._condition:
            observer_alive = True
            observer = self._observer
            is_alive = getattr(observer, "is_alive", None)
            if callable(is_alive):
                try:
                    observer_alive = bool(is_alive())
                except Exception:
                    observer_alive = False
            return (
                self._worker is not None
                and self._worker.is_alive()
                and observer_alive
                and not self._stopping
            )

    def start(self) -> None:
        with self._condition:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stopping = False
            self._observer = self._observer_factory()
            self._observer.start()
            self._worker = threading.Thread(
                target=self._run,
                name="storydex-content-pipeline",
                daemon=True,
            )
            self._worker.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            worker = self._worker
            observer = self._observer
        if worker is not None:
            worker.join(timeout=max(0.0, float(timeout)))
        if observer is not None:
            observer.stop()
            observer.join(timeout=max(0.0, float(timeout)))
        with self._condition:
            self._worker = None
            self._observer = None
            self._watches.clear()
            self._pending.clear()
            self._roots.clear()
            self._processing_roots.clear()
            self._deferred_paths.clear()
            self._workspace_locks.clear()

    def register_workspace(
        self,
        workspace_root: Path,
        *,
        schedule: bool = True,
    ) -> None:
        root = Path(workspace_root).resolve()
        with self._condition:
            if self._stopping or self._observer is None:
                return
            if root not in self._roots:
                self._roots.add(root)
                try:
                    handler = WorkspaceEventHandler(
                        lambda paths, watched_root=root: self._handle_paths(watched_root, paths)
                    )
                    self._watches[root] = self._observer.schedule(
                        handler,
                        str(root),
                        recursive=True,
                    )
                    self._watcher_errors.pop(root, None)
                except Exception as exc:
                    self._watcher_errors[root] = str(exc)
                    logger.error("Unable to watch content workspace %s: %s", root, exc)
            # ``get_content_catalog_service`` is called from inside a
            # publication.  Scheduling a second worker run at that point
            # races the current publisher; the outer operation will drain
            # deferred events when it finishes.
            if schedule and root not in self._processing_roots:
                self._schedule_unlocked(root, delay=0.0)

    def notify_dirty(self, workspace_root: Path) -> None:
        root = Path(workspace_root).resolve()
        with self._condition:
            if self._stopping or self._observer is None:
                return
            if root not in self._roots:
                self.register_workspace(root)
                return
            self._schedule_unlocked(root, delay=self.event_debounce_seconds)

    def bootstrap_workspace(self, workspace_root: Path) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        from services.story_wiki_service import get_story_wiki_service

        # Source migration must finish before the watcher owns this root;
        # otherwise its own writes can race the synchronous first publication.
        get_story_wiki_service().prepare_catalog_sources(root, reconcile_entities=True)
        self.register_workspace(root, schedule=False)
        with self._condition:
            self._pending.pop(root, None)
        return self.process_workspace(root)

    def process_workspace(self, workspace_root: Path) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        with self._workspace_processing(root):
            return self._process_workspace_unlocked(root)

    def _process_workspace_unlocked(self, root: Path) -> Dict[str, Any]:
        from services.content_catalog_service import get_content_catalog_service
        from services.retrieval_service import get_retrieval_service
        from services.story_wiki_service import get_story_wiki_service

        retrieval_service = get_retrieval_service(root)
        catalog_service = get_content_catalog_service(root)
        previous_snapshot = catalog_service.peek_snapshot()
        wiki_service = get_story_wiki_service()
        preparation = wiki_service.prepare_catalog_sources(root)
        prepared_paths = [
            str(path)
            for path in preparation.get("writtenPaths", [])
            if str(path).strip()
        ]
        if previous_snapshot is not None and prepared_paths:
            catalog_service.mark_dirty(
                prepared_paths,
                source="wiki_source_prepare",
                notify_background=False,
            )
        snapshot = catalog_service.refresh_dirty()
        changed_paths = catalog_service.changed_paths(previous_snapshot, snapshot)
        if previous_snapshot is None or self._changed_paths_need_entity_reconcile(changed_paths):
            entity_preparation = wiki_service.prepare_catalog_sources(
                root,
                reconcile_entities=True,
            )
            entity_paths = [
                str(path)
                for path in entity_preparation.get("writtenPaths", [])
                if str(path).strip()
            ]
            if entity_paths:
                catalog_service.mark_dirty(
                    entity_paths,
                    source="wiki_entity_reconcile",
                    notify_background=False,
                )
                snapshot = catalog_service.refresh_dirty()
                changed_paths = catalog_service.changed_paths(previous_snapshot, snapshot)
        updated = retrieval_service.refresh_from_catalog(snapshot)
        try:
            wiki_result = wiki_service.refresh_from_catalog(
                root,
                snapshot,
                changed_paths=changed_paths,
            )
        except Exception:
            # Catalog publication is immutable and may have already consumed
            # the watcher dirty entries.  Keep the exact projection change set
            # queued so a later background attempt cannot silently lose it.
            if changed_paths:
                catalog_service.mark_dirty(changed_paths, source="wiki_publish_retry")
            raise
        with self._condition:
            self._failures.pop(root, None)
        return {
            "workspaceRoot": root.as_posix(),
            "catalogGeneration": snapshot.generation,
            "catalogRevision": snapshot.catalog_revision,
            "catalogChangedPaths": list(changed_paths),
            "retrievalUpdatedFileCount": updated,
            "wikiStatus": str(wiki_result.get("status") or "ready"),
            "wikiKnowledgeRevision": wiki_result.get("knowledgeRevision"),
        }

    @staticmethod
    def _changed_paths_need_entity_reconcile(paths: Iterable[str]) -> bool:
        return any(
            (
                str(path).replace("\\", "/").startswith(".storydex/characters/")
                or str(path).replace("\\", "/").startswith("characters/")
            )
            for path in paths
        )

    def reconcile_workspace(self, workspace_root: Path) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        from services.content_catalog_service import get_content_catalog_service

        with self._workspace_processing(root):
            catalog_service = get_content_catalog_service(root)
            changed = catalog_service.reconcile(notify_background=False)
            result = self._process_workspace_unlocked(root)
            result["reconciliationChangedCount"] = changed
            return result

    def sync_workspace(
        self,
        workspace_root: Path,
        *,
        reconcile: bool = True,
    ) -> Dict[str, Any]:
        """Synchronously publish one settled workspace state.

        Native watcher events are delivered asynchronously, including events
        emitted by our own atomic projection/entity writes.  Keep the
        workspace lock held while draining those events and perform a bounded
        number of convergence passes.  This gives an explicit ``/sync`` call
        a deterministic ready/stale boundary without making query paths wait
        or retry indefinitely.
        """
        root = Path(workspace_root).resolve()
        from services.content_catalog_service import get_content_catalog_service

        with self._workspace_processing(root):
            catalog_service = get_content_catalog_service(root)
            result: Dict[str, Any] | None = None
            reconciliation_changed = 0
            for pass_index in range(MAX_SYNC_CONVERGENCE_PASSES):
                if reconcile:
                    reconciliation_changed += catalog_service.reconcile(notify_background=False)
                result = self._process_workspace_unlocked(root)
                self._drain_deferred_events(root)
                if catalog_service.dirty_file_count == 0 and not self._has_deferred_paths(root):
                    # Allow the native observer to deliver the final replace
                    # notifications while the processing flag still diverts
                    # them into the bounded drain above.
                    settle = min(0.25, max(0.05, self.event_debounce_seconds))
                    if settle:
                        time.sleep(settle)
                    self._drain_deferred_events(root)
                    if catalog_service.dirty_file_count == 0 and not self._has_deferred_paths(root):
                        break
                # A dirty queue left by a late event is intentional input for
                # the next pass; no unbounded retry is allowed here.
                if pass_index + 1 < MAX_SYNC_CONVERGENCE_PASSES:
                    continue
            if result is None:
                result = self._process_workspace_unlocked(root)
            result["reconciliationChangedCount"] = reconciliation_changed
            return result

    def health(self, workspace_root: Path) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        from services.content_catalog_service import get_content_catalog_service

        metrics = get_content_catalog_service(root).background_metrics
        return {**self.monitor_status(root), **metrics}

    def monitor_status(self, workspace_root: Path) -> Dict[str, Any]:
        root = Path(workspace_root)
        with self._condition:
            return {
                "enabled": True,
                "running": self.running,
                "registered": root in self._roots,
                "watcherError": self._watcher_errors.get(root, ""),
                "pending": root in self._pending,
                "failures": self._failures.get(root, 0),
            }

    def _handle_paths(self, root: Path, paths: Iterable[Path]) -> None:
        from services.content_catalog_service import get_content_catalog_service

        normalized_paths = {Path(path) for path in paths if str(path or "").strip()}
        if not normalized_paths:
            return
        with self._condition:
            if root in self._processing_roots:
                self._deferred_paths.setdefault(root, set()).update(normalized_paths)
                return

        # Ignore generated/runtime events that are intentionally outside the
        # catalog.  Scheduling the pipeline for every SQLite/log write would
        # create a self-sustaining refresh loop even though no source revision
        # changed.  ``mark_dirty`` also wakes a registered pipeline, while the
        # explicit call keeps custom/test pipeline instances responsive.
        dirty_count = get_content_catalog_service(root).notify_external_changes(normalized_paths)
        if dirty_count:
            self.notify_dirty(root)

    def _workspace_lock(self, root: Path) -> threading.RLock:
        with self._condition:
            lock = self._workspace_locks.get(root)
            if lock is None:
                lock = threading.RLock()
                self._workspace_locks[root] = lock
            return lock

    @contextmanager
    def _workspace_processing(self, root: Path):
        lock = self._workspace_lock(root)
        lock.acquire()
        with self._condition:
            self._processing_roots.add(root)
        try:
            yield
        finally:
            # Do this before clearing ``_processing_roots`` so a watcher event
            # arriving during the final drain cannot race a newly started
            # worker publication.
            self._drain_deferred_events(root)
            with self._condition:
                self._processing_roots.discard(root)
            lock.release()

    def _has_deferred_paths(self, root: Path) -> bool:
        with self._condition:
            return bool(self._deferred_paths.get(root))

    def _drain_deferred_events(self, root: Path) -> int:
        with self._condition:
            paths = self._deferred_paths.pop(root, set())
        if not paths:
            return 0
        from services.content_catalog_service import get_content_catalog_service

        dirty_count = get_content_catalog_service(root).notify_external_changes(paths)
        if dirty_count:
            with self._condition:
                if not self._stopping:
                    self._schedule_unlocked(root, delay=self.event_debounce_seconds)
        return dirty_count

    def _schedule_unlocked(self, root: Path, *, delay: float) -> None:
        due = time.monotonic() + max(0.0, float(delay))
        current = self._pending.get(root)
        if current is None or due > current:
            self._pending[root] = due
        self._condition.notify_all()

    def _run(self) -> None:
        while True:
            due_roots: list[Path] = []
            reconcile_roots: list[Path] = []
            with self._condition:
                if self._stopping:
                    return
                now = time.monotonic()
                due_roots = [root for root, due in self._pending.items() if due <= now]
                for root in due_roots:
                    self._pending.pop(root, None)
                if now >= self._next_reconciliation:
                    reconcile_roots = sorted(self._roots, key=lambda item: item.as_posix())
                    self._next_reconciliation = now + self.reconciliation_interval_seconds
                if not due_roots and not reconcile_roots:
                    deadlines = [self._next_reconciliation, *self._pending.values()]
                    timeout = max(0.01, min(deadlines) - now) if deadlines else 1.0
                    self._condition.wait(timeout=timeout)
                    continue

            reconciled = set(reconcile_roots)
            for root in reconcile_roots:
                self._execute(root, reconcile=True)
            for root in due_roots:
                if root not in reconciled:
                    self._execute(root, reconcile=False)

    def _execute(self, root: Path, *, reconcile: bool) -> None:
        try:
            if reconcile:
                self.reconcile_workspace(root)
            else:
                self.process_workspace(root)
        except Exception as exc:
            with self._condition:
                failures = self._failures.get(root, 0) + 1
                self._failures[root] = failures
                delay = min(MAX_RETRY_DELAY_SECONDS, 2.0 ** min(failures, 6))
                self._pending[root] = time.monotonic() + delay
                self._condition.notify_all()
            logger.error(
                "Content pipeline refresh failed for %s (retry in %.1fs): %s",
                root,
                delay,
                exc,
            )


_PIPELINE_LOCK = threading.Lock()
_PIPELINE: ContentPipelineService | None = None


def start_content_pipeline() -> ContentPipelineService:
    global _PIPELINE
    with _PIPELINE_LOCK:
        if _PIPELINE is None:
            _PIPELINE = ContentPipelineService()
            _PIPELINE.start()
        return _PIPELINE


def stop_content_pipeline() -> None:
    global _PIPELINE
    with _PIPELINE_LOCK:
        pipeline = _PIPELINE
        _PIPELINE = None
    if pipeline is not None:
        pipeline.stop()


def register_content_workspace(workspace_root: Path) -> None:
    with _PIPELINE_LOCK:
        pipeline = _PIPELINE
    if pipeline is not None:
        pipeline.register_workspace(workspace_root)


def notify_content_workspace_dirty(workspace_root: Path) -> None:
    with _PIPELINE_LOCK:
        pipeline = _PIPELINE
    if pipeline is not None:
        pipeline.notify_dirty(workspace_root)


def bootstrap_content_workspace(workspace_root: Path) -> Dict[str, Any]:
    return start_content_pipeline().bootstrap_workspace(workspace_root)


def sync_content_workspace(
    workspace_root: Path,
    *,
    reconcile: bool = True,
) -> Dict[str, Any]:
    pipeline = start_content_pipeline()
    return pipeline.sync_workspace(workspace_root, reconcile=reconcile)


def content_workspace_monitor_status(workspace_root: Path) -> Dict[str, Any]:
    with _PIPELINE_LOCK:
        pipeline = _PIPELINE
    if pipeline is None:
        return {
            "enabled": False,
            "running": False,
            "registered": False,
            "watcherError": "",
            "pending": False,
            "failures": 0,
        }
    return pipeline.monitor_status(workspace_root)
