from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any


SMALL_FILE_BYTES = 2 * 1024 * 1024
LARGE_FILE_BYTES = 20 * 1024 * 1024
INITIAL_CHUNK_BYTES = 256 * 1024
DEFAULT_WINDOW_LINES = 400
MAX_WINDOW_LINES = 2000
MAX_INDEX_CACHE_ENTRIES = 8


@dataclass(frozen=True)
class LineIndex:
    size: int
    mtime_ns: int
    offsets: tuple[int, ...]


class LargeFileService:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="storydex-file-index")
        self._lock = Lock()
        self._indexes: OrderedDict[str, LineIndex] = OrderedDict()
        self._pending: dict[str, Future[LineIndex]] = {}

    @staticmethod
    def mode_for_size(size: int) -> str:
        if size < SMALL_FILE_BYTES:
            return "full"
        if size <= LARGE_FILE_BYTES:
            return "progressive"
        return "large-readonly"

    def read_window(self, path: Path, *, start_line: int = 0, line_count: int = DEFAULT_WINDOW_LINES) -> dict[str, Any]:
        target = Path(path)
        stat = target.stat()
        start = max(0, int(start_line))
        count = max(1, min(MAX_WINDOW_LINES, int(line_count)))
        index = self._get_index(target, stat.st_size, stat.st_mtime_ns)
        self._schedule_index(target, stat.st_size, stat.st_mtime_ns)

        if index is not None:
            total_lines = max(1, len(index.offsets) - 1)
            start = min(start, max(0, total_lines - 1))
            end_line = min(total_lines, start + count)
            start_byte = index.offsets[start]
            end_byte = index.offsets[end_line]
            with target.open("rb") as handle:
                handle.seek(start_byte)
                payload = handle.read(max(0, end_byte - start_byte))
            content = payload.decode("utf-8-sig" if start_byte == 0 else "utf-8", errors="replace")
            return self._response(
                target=target,
                stat=stat,
                content=content,
                start_line=start,
                loaded_lines=max(0, end_line - start),
                total_lines=total_lines,
                exact=True,
            )

        selected: list[str] = []
        observed = 0
        consumed_bytes = 0
        has_more = False
        with target.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            for raw_line in handle:
                current = observed
                observed += 1
                if current < start:
                    continue
                encoded_size = len(raw_line.encode("utf-8"))
                if selected and consumed_bytes + encoded_size > INITIAL_CHUNK_BYTES:
                    has_more = True
                    break
                if len(selected) >= count:
                    has_more = True
                    break
                selected.append(raw_line.rstrip("\r\n"))
                consumed_bytes += encoded_size

        content = "\n".join(selected)
        if selected and has_more:
            content += "\n"
        average = max(1, consumed_bytes // max(1, len(selected)))
        estimated_lines = max(observed, stat.st_size // average)
        return self._response(
            target=target,
            stat=stat,
            content=content,
            start_line=start,
            loaded_lines=len(selected),
            total_lines=estimated_lines,
            exact=not has_more and observed >= estimated_lines,
        )

    def _response(
        self,
        *,
        target: Path,
        stat: Any,
        content: str,
        start_line: int,
        loaded_lines: int,
        total_lines: int,
        exact: bool,
    ) -> dict[str, Any]:
        return {
            "content": content,
            "size": int(stat.st_size),
            "mtimeMs": int(stat.st_mtime * 1000),
            "startLine": start_line,
            "loadedLines": loaded_lines,
            "lineCount": max(loaded_lines, int(total_lines)),
            "lineCountExact": bool(exact),
            "hasPrevious": start_line > 0,
            "hasNext": start_line + loaded_lines < max(loaded_lines, int(total_lines)),
            "mode": self.mode_for_size(int(stat.st_size)),
            "readOnly": int(stat.st_size) > LARGE_FILE_BYTES,
            "initialChunkBytes": INITIAL_CHUNK_BYTES,
        }

    def _get_index(self, path: Path, size: int, mtime_ns: int) -> LineIndex | None:
        key = str(path.resolve())
        with self._lock:
            pending = self._pending.get(key)
            if pending is not None and pending.done():
                try:
                    self._store_index(key, pending.result())
                finally:
                    self._pending.pop(key, None)
            index = self._indexes.get(key)
            if index is None or index.size != size or index.mtime_ns != mtime_ns:
                self._indexes.pop(key, None)
                return None
            self._indexes.move_to_end(key)
            return index

    def _schedule_index(self, path: Path, size: int, mtime_ns: int) -> None:
        key = str(path.resolve())
        with self._lock:
            if key in self._pending or key in self._indexes:
                return
            self._pending[key] = self._executor.submit(self._build_index, path, size, mtime_ns)

    def _store_index(self, key: str, index: LineIndex) -> None:
        self._indexes[key] = index
        self._indexes.move_to_end(key)
        while len(self._indexes) > MAX_INDEX_CACHE_ENTRIES:
            self._indexes.popitem(last=False)

    @staticmethod
    def _build_index(path: Path, size: int, mtime_ns: int) -> LineIndex:
        offsets = [0]
        position = 0
        with Path(path).open("rb") as handle:
            for line in handle:
                position += len(line)
                offsets.append(position)
        if offsets[-1] != size:
            offsets.append(size)
        if len(offsets) == 1:
            offsets.append(0)
        return LineIndex(size=size, mtime_ns=mtime_ns, offsets=tuple(offsets))


_large_file_service = LargeFileService()


def get_large_file_service() -> LargeFileService:
    return _large_file_service
