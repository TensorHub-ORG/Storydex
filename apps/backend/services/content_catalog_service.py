from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Iterable, Mapping, Sequence

from services.performance_trace_service import record_counter, record_duration, record_value
from services.source_contract import SourceRevision, describe_utf8_source, source_revision_id


CATALOG_SCHEMA_VERSION = 1
CATALOG_TEXT_SUFFIXES = frozenset({".md", ".txt", ".json", ".yaml", ".yml"})
CATALOG_ROOTS = (
    "chapters",
    ".storydex",
)
CATALOG_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".cache",
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
        "__pycache__",
    }
)
CATALOG_EXCLUDED_RELATIVE_PREFIXES = (
    ".storydex/wiki",
    ".storydex/memory/backups",
)

logger = logging.getLogger(__name__)


class ContentCatalogRefreshError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogEntry:
    source: SourceRevision
    kind: str
    freshness: str = "fresh"
    derived_state: str = "source"

    @property
    def path(self) -> str:
        return self.source.path

    @property
    def revision(self) -> str:
        return self.source.revision

    @property
    def size_bytes(self) -> int:
        return self.source.size_bytes

    @property
    def mtime_ns(self) -> int:
        return self.source.mtime_ns

    @property
    def total_chars(self) -> int:
        return self.source.total_chars

    @property
    def total_lines(self) -> int:
        return self.source.total_lines

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.source.to_dict(),
            "kind": self.kind,
            "freshness": self.freshness,
            "derivedState": self.derived_state,
        }


@dataclass(frozen=True)
class ContentCatalogSnapshot:
    workspace_root: Path
    generation: int
    catalog_revision: str
    published_at: str
    entries: Mapping[str, CatalogEntry]
    directories: Mapping[str, int]
    dirty_file_count: int = 0

    def get(self, relative_path: str) -> CatalogEntry | None:
        return self.entries.get(str(relative_path or "").replace("\\", "/").strip("/"))

    def files(
        self,
        *,
        prefix: str = "",
        suffixes: Sequence[str] | None = None,
    ) -> tuple[CatalogEntry, ...]:
        normalized_prefix = str(prefix or "").replace("\\", "/").strip("/")
        if normalized_prefix:
            normalized_prefix += "/"
        normalized_suffixes = {str(value).lower() for value in (suffixes or ()) if str(value)}
        return tuple(
            entry
            for path, entry in self.entries.items()
            if (not normalized_prefix or path.startswith(normalized_prefix))
            and (not normalized_suffixes or Path(path).suffix.lower() in normalized_suffixes)
        )

    def to_trace(self) -> Dict[str, Any]:
        return {
            "_type": "ContentCatalogSnapshot",
            "_version": CATALOG_SCHEMA_VERSION,
            "generation": self.generation,
            "catalogRevision": self.catalog_revision,
            "publishedAt": self.published_at,
            "fileCount": len(self.entries),
            "directoryCount": len(self.directories),
            "dirtyFileCount": self.dirty_file_count,
        }


class ContentCatalogService:
    """Publishes immutable, per-workspace source snapshots.

    The first snapshot performs a full content verification. Later queries return
    the published object directly; writes and watcher callbacks only enqueue dirty
    paths until ``refresh_dirty`` publishes a complete replacement snapshot.
    """

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self._state_lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._snapshot: ContentCatalogSnapshot | None = None
        self._dirty: Dict[str, int] = {}
        self._dirty_enqueued_at: Dict[str, float] = {}
        self._next_dirty_revision = 0
        self._last_event_lag_ms = 0.0
        self._reconciliation_scan_count = 0
        self._reconciliation_changed_count = 0

    def snapshot(self) -> ContentCatalogSnapshot:
        with self._state_lock:
            current = self._snapshot
        if current is None:
            current = self.refresh_all()
        with self._state_lock:
            dirty_count = len(self._dirty)
        if current.dirty_file_count == dirty_count:
            return current
        return self._with_dirty_count(current, dirty_count)

    def peek_snapshot(self) -> ContentCatalogSnapshot | None:
        with self._state_lock:
            current = self._snapshot
            dirty_count = len(self._dirty)
        return self._with_dirty_count(current, dirty_count) if current is not None else None

    def mark_dirty(
        self,
        paths: Iterable[str | Path],
        *,
        source: str = "workspace_write",
        notify_background: bool = True,
    ) -> int:
        normalized = {
            target
            for path in paths
            for target in self._catalog_dirty_targets(path)
        }
        if not normalized:
            return self.dirty_file_count
        with self._state_lock:
            enqueued_at = time.monotonic()
            for relative in sorted(normalized):
                self._next_dirty_revision += 1
                self._dirty[relative] = self._next_dirty_revision
                self._dirty_enqueued_at[relative] = enqueued_at
            dirty_count = len(self._dirty)
        if notify_background:
            self._notify_background(source=source)
        return dirty_count

    def notify_external_changes(self, paths: Iterable[str | Path]) -> int:
        candidates: list[str | Path] = []
        with self._state_lock:
            current = self._snapshot
            already_dirty = set(self._dirty)
        for raw_path in paths:
            relative = self._normalize_relative_path(raw_path)
            targets = self._catalog_dirty_targets(relative)
            if not targets:
                continue
            for target in targets:
                if current is None or target in already_dirty:
                    candidates.append(target)
                    continue
                entry = current.entries.get(target)
                if entry is None:
                    candidates.append(target)
                    continue
                # Watchdog also reports the replace event emitted by our own
                # atomic publisher. If the published revision already equals
                # the on-disk revision, the event carries no new catalog data.
                try:
                    observed = self._read_catalog_entry(
                        self.workspace_root / target,
                        target,
                    )
                except Exception:
                    observed = None
                if observed is None or observed.revision != entry.revision:
                    candidates.append(target)
        return self.mark_dirty(candidates, source="external_watcher")

    @property
    def dirty_file_count(self) -> int:
        with self._state_lock:
            return len(self._dirty)

    @property
    def background_metrics(self) -> Dict[str, Any]:
        with self._state_lock:
            return {
                "catalogEventLagMs": round(self._last_event_lag_ms, 3),
                "reconciliationScanCount": self._reconciliation_scan_count,
                "reconciliationChangedCount": self._reconciliation_changed_count,
            }

    def refresh_all(self) -> ContentCatalogSnapshot:
        started = time.perf_counter()
        with self._refresh_lock:
            with self._state_lock:
                pending = dict(self._dirty)
            try:
                entries, directories = self._scan_roots()
            except Exception as exc:
                raise ContentCatalogRefreshError(f"content catalog full refresh failed: {exc}") from exc
            with self._state_lock:
                generation = (self._snapshot.generation if self._snapshot is not None else 0) + 1
                for relative, dirty_revision in pending.items():
                    if self._dirty.get(relative) == dirty_revision:
                        self._dirty.pop(relative, None)
                        enqueued_at = self._dirty_enqueued_at.pop(relative, None)
                        if enqueued_at is not None:
                            self._last_event_lag_ms = max(
                                self._last_event_lag_ms,
                                (time.monotonic() - enqueued_at) * 1000,
                            )
                snapshot = self._publish(
                    generation=generation,
                    entries=entries,
                    directories=directories,
                    dirty_file_count=len(self._dirty),
                )
                self._snapshot = snapshot
            self._record_snapshot(snapshot, (time.perf_counter() - started) * 1000)
            return snapshot

    def refresh_dirty(self) -> ContentCatalogSnapshot:
        started = time.perf_counter()
        with self._state_lock:
            if self._snapshot is None:
                return self.refresh_all()
        with self._refresh_lock:
            with self._state_lock:
                current = self._snapshot
                pending = dict(self._dirty)
            if current is None:
                raise ContentCatalogRefreshError("content catalog snapshot disappeared during refresh")
            if not pending:
                self._record_snapshot(current, 0.0)
                return current

            entries = dict(current.entries)
            directories = dict(current.directories)
            try:
                for relative in sorted(pending):
                    self._replace_dirty_path(
                        relative,
                        entries=entries,
                        directories=directories,
                    )
            except Exception as exc:
                raise ContentCatalogRefreshError(
                    f"content catalog dirty refresh failed for {relative}: {exc}"
                ) from exc

            with self._state_lock:
                generation = current.generation + 1
                entries_unchanged = entries == dict(current.entries)
                directories_unchanged = directories == dict(current.directories)
                for relative, dirty_revision in pending.items():
                    if self._dirty.get(relative) == dirty_revision:
                        self._dirty.pop(relative, None)
                        enqueued_at = self._dirty_enqueued_at.pop(relative, None)
                        if enqueued_at is not None:
                            self._last_event_lag_ms = max(
                                self._last_event_lag_ms,
                                (time.monotonic() - enqueued_at) * 1000,
                            )
                if entries_unchanged and directories_unchanged:
                    snapshot = self._with_dirty_count(current, len(self._dirty))
                else:
                    snapshot = self._publish(
                        generation=generation,
                        entries=entries,
                        directories=directories,
                        dirty_file_count=len(self._dirty),
                    )
                self._snapshot = snapshot
            self._record_snapshot(snapshot, (time.perf_counter() - started) * 1000)
            return snapshot

    def reconcile(self, *, notify_background: bool = True) -> int:
        """Discover missed filesystem events without publishing on the query path."""
        with self._state_lock:
            current = self._snapshot
        if current is None:
            self.refresh_all()
            with self._state_lock:
                self._reconciliation_scan_count += 1
            record_counter("reconciliationScanCount")
            record_value("reconciliationChangedCount", 0)
            return 0

        entries, directories = self._scan_roots()
        changed = {
            path
            for path in set(current.entries) | set(entries)
            if current.entries.get(path) != entries.get(path)
        }
        changed.update(set(current.directories) ^ set(directories))
        with self._state_lock:
            self._reconciliation_scan_count += 1
            self._reconciliation_changed_count += len(changed)
        record_counter("reconciliationScanCount")
        record_value("reconciliationChangedCount", len(changed))
        if changed:
            self.mark_dirty(
                changed,
                source="reconciliation",
                notify_background=notify_background,
            )
        return len(changed)

    @staticmethod
    def changed_paths(
        previous: ContentCatalogSnapshot | None,
        current: ContentCatalogSnapshot,
    ) -> tuple[str, ...]:
        """Return source paths whose published catalog entry changed.

        The diff is calculated from immutable snapshots, so consumers can pass
        one exact change set to multiple projections without rescanning the
        workspace or relying on a watcher event payload.
        """
        if previous is None:
            return tuple(sorted(current.entries))
        changed = {
            path
            for path in set(previous.entries) | set(current.entries)
            if previous.entries.get(path) != current.entries.get(path)
        }
        return tuple(sorted(changed))

    def _scan_roots(self) -> tuple[Dict[str, CatalogEntry], Dict[str, int]]:
        entries: Dict[str, CatalogEntry] = {}
        directories: Dict[str, int] = {}
        for relative_root in CATALOG_ROOTS:
            root = self.workspace_root / relative_root
            if not root.exists():
                continue
            self._scan_tree(root, entries=entries, directories=directories)
        return entries, directories

    def _scan_tree(
        self,
        root: Path,
        *,
        entries: Dict[str, CatalogEntry],
        directories: Dict[str, int],
    ) -> None:
        try:
            relative_root = root.relative_to(self.workspace_root).as_posix()
        except ValueError:
            return
        if self._is_excluded_relative(relative_root):
            return
        if root.is_file():
            self._add_file(root, entries)
            return
        for directory, child_directories, file_names in os.walk(root):
            record_counter("directoryScanCount")
            directory_path = Path(directory)
            relative_directory = directory_path.relative_to(self.workspace_root).as_posix()
            child_directories[:] = [
                name
                for name in child_directories
                if name not in CATALOG_EXCLUDED_DIRECTORY_NAMES
                and not self._is_excluded_relative(
                    f"{relative_directory}/{name}" if relative_directory else name
                )
            ]
            record_counter("statCount")
            directories[relative_directory] = int(directory_path.stat().st_mtime_ns)
            for file_name in file_names:
                path = directory_path / file_name
                if path.suffix.lower() not in CATALOG_TEXT_SUFFIXES:
                    continue
                if self._is_excluded_relative(path.relative_to(self.workspace_root).as_posix()):
                    continue
                self._add_file(path, entries)

    def _add_file(self, path: Path, entries: Dict[str, CatalogEntry]) -> None:
        relative = path.relative_to(self.workspace_root).as_posix()
        entries[relative] = self._read_catalog_entry(path, relative)

    def _read_catalog_entry(self, path: Path, relative: str) -> CatalogEntry:
        record_counter("statCount")
        before = path.stat()
        record_counter("readCount")
        raw = path.read_bytes()
        record_counter("statCount")
        after = path.stat()
        before_signature = (int(before.st_size), int(before.st_mtime_ns))
        after_signature = (int(after.st_size), int(after.st_mtime_ns))
        if before_signature != after_signature or len(raw) != after_signature[0]:
            raise ContentCatalogRefreshError(f"source changed while cataloging: {relative}")
        try:
            source, _text = describe_utf8_source(
                path=relative,
                raw=raw,
                mtime_ns=after_signature[1],
            )
            freshness = "fresh"
        except UnicodeDecodeError:
            revision = source_revision_id(raw)
            source = SourceRevision(
                path=relative,
                revision=revision,
                size_bytes=len(raw),
                mtime_ns=after_signature[1],
                total_chars=0,
                total_lines=0,
                content_hash=revision,
            )
            freshness = "unreadable"
        return CatalogEntry(
            source=source,
            kind=self._kind_for_path(relative),
            freshness=freshness,
        )

    def _replace_dirty_path(
        self,
        relative: str,
        *,
        entries: Dict[str, CatalogEntry],
        directories: Dict[str, int],
    ) -> None:
        if self._is_excluded_relative(relative):
            return
        prefix = relative.rstrip("/") + "/"
        for existing in [path for path in entries if path == relative or path.startswith(prefix)]:
            entries.pop(existing, None)
        for existing in [path for path in directories if path == relative or path.startswith(prefix)]:
            directories.pop(existing, None)

        path = self.workspace_root / relative
        if not path.exists():
            self._add_parent_directories(path.parent, directories)
            return
        if path.is_dir():
            self._scan_tree(path, entries=entries, directories=directories)
            self._add_parent_directories(path.parent, directories)
            return
        if path.suffix.lower() in CATALOG_TEXT_SUFFIXES:
            self._add_file(path, entries)
            self._add_parent_directories(path.parent, directories)

    def _add_parent_directories(
        self,
        directory: Path,
        directories: Dict[str, int],
    ) -> None:
        current = directory
        while current != self.workspace_root:
            try:
                relative = current.relative_to(self.workspace_root).as_posix()
            except ValueError:
                return
            if not any(
                relative == root or relative.startswith(root + "/")
                for root in CATALOG_ROOTS
            ):
                return
            record_counter("statCount")
            if current.is_dir():
                record_counter("statCount")
                directories[relative] = int(current.stat().st_mtime_ns)
            else:
                directories.pop(relative, None)
            current = current.parent

    def _catalog_dirty_targets(self, value: str | Path) -> tuple[str, ...]:
        normalized = self._normalize_relative_path(value)
        if not normalized:
            return ()
        if self._is_excluded_relative(normalized):
            return ()
        parts = Path(normalized).parts
        if any(part in CATALOG_EXCLUDED_DIRECTORY_NAMES for part in parts):
            return ()
        if any(
            normalized == root or normalized.startswith(root + "/")
            for root in CATALOG_ROOTS
        ):
            return (normalized,)
        return tuple(
            root
            for root in CATALOG_ROOTS
            if root.startswith(normalized + "/")
        )

    @staticmethod
    def _is_excluded_relative(relative: str) -> bool:
        normalized = str(relative or "").replace("\\", "/").strip("/").lower()
        return any(
            normalized == prefix or normalized.startswith(prefix + "/")
            for prefix in CATALOG_EXCLUDED_RELATIVE_PREFIXES
        )

    def _normalize_relative_path(self, value: str | Path) -> str:
        path = Path(value)
        if path.is_absolute():
            try:
                path = path.resolve().relative_to(self.workspace_root)
            except ValueError:
                return ""
        normalized = path.as_posix().strip("/")
        if not normalized or any(part in {"", ".", ".."} for part in Path(normalized).parts):
            return ""
        return normalized

    def _notify_background(self, *, source: str) -> None:
        try:
            from services.content_pipeline_service import notify_content_workspace_dirty

            notify_content_workspace_dirty(self.workspace_root)
        except ImportError:
            logger.debug("Content pipeline is not available for %s", self.workspace_root)
        except Exception as exc:
            logger.warning(
                "Unable to notify content pipeline for %s source=%s: %s",
                self.workspace_root,
                source,
                exc,
            )

    @staticmethod
    def _kind_for_path(relative: str) -> str:
        normalized = str(relative).replace("\\", "/")
        mappings = (
            ("chapters/", "chapter"),
            (".storydex/characters/", "character"),
            (".storydex/worldbook/", "worldbook"),
            (".storydex/memory/", "memory"),
            (".storydex/wiki/", "wiki"),
            (".storydex/scripts/", "script"),
            (".storydex/presets/", "preset"),
            (".storydex/skills/", "skill"),
        )
        return next((kind for prefix, kind in mappings if normalized.startswith(prefix)), "project")

    def _publish(
        self,
        *,
        generation: int,
        entries: Dict[str, CatalogEntry],
        directories: Dict[str, int],
        dirty_file_count: int,
    ) -> ContentCatalogSnapshot:
        ordered_entries = dict(sorted(entries.items()))
        ordered_directories = dict(sorted(directories.items()))
        digest_payload = "\n".join(
            [*(f"F\0{path}\0{entry.revision}" for path, entry in ordered_entries.items()),
             *(f"D\0{path}" for path in ordered_directories)]
        )
        catalog_revision = f"sha256:{hashlib.sha256(digest_payload.encode('utf-8')).hexdigest()}"
        return ContentCatalogSnapshot(
            workspace_root=self.workspace_root,
            generation=max(1, int(generation)),
            catalog_revision=catalog_revision,
            published_at=datetime.now(timezone.utc).isoformat(),
            entries=MappingProxyType(ordered_entries),
            directories=MappingProxyType(ordered_directories),
            dirty_file_count=max(0, int(dirty_file_count)),
        )

    @staticmethod
    def _with_dirty_count(
        snapshot: ContentCatalogSnapshot,
        dirty_file_count: int,
    ) -> ContentCatalogSnapshot:
        return ContentCatalogSnapshot(
            workspace_root=snapshot.workspace_root,
            generation=snapshot.generation,
            catalog_revision=snapshot.catalog_revision,
            published_at=snapshot.published_at,
            entries=snapshot.entries,
            directories=snapshot.directories,
            dirty_file_count=max(0, int(dirty_file_count)),
        )

    @staticmethod
    def _record_snapshot(snapshot: ContentCatalogSnapshot, elapsed_ms: float) -> None:
        record_duration("catalogRefreshMs", elapsed_ms)
        record_value("catalogRevision", snapshot.catalog_revision)
        record_value("catalogGeneration", snapshot.generation)
        record_value("dirtyFileCount", snapshot.dirty_file_count)


_CATALOGS: Dict[Path, ContentCatalogService] = {}
_CATALOGS_LOCK = threading.Lock()


def get_content_catalog_service(workspace_root: Path) -> ContentCatalogService:
    root = Path(workspace_root).resolve()
    with _CATALOGS_LOCK:
        service = _CATALOGS.get(root)
        if service is None:
            service = ContentCatalogService(root)
            _CATALOGS[root] = service
    try:
        from services.content_pipeline_service import register_content_workspace

        register_content_workspace(root)
    except ImportError:
        logger.debug("Content pipeline is not available for %s", root)
    return service


def reset_content_catalog_services() -> None:
    with _CATALOGS_LOCK:
        _CATALOGS.clear()
