from pathlib import Path
from concurrent.futures import Future
from types import SimpleNamespace

from services.large_file_service import (
    LARGE_FILE_BYTES,
    MAX_INDEX_CACHE_ENTRIES,
    SMALL_FILE_BYTES,
    LargeFileService,
    LineIndex,
    get_large_file_service,
)


def test_large_file_modes_and_utf8_windows(tmp_path: Path):
    service = LargeFileService()
    assert service.mode_for_size(SMALL_FILE_BYTES - 1) == "full"
    assert service.mode_for_size(SMALL_FILE_BYTES) == "progressive"
    assert service.mode_for_size(LARGE_FILE_BYTES + 1) == "large-readonly"

    target = tmp_path / "中文.txt"
    target.write_text("\ufeff第一行\n第二行🙂\n第三行\n第四行\n", encoding="utf-8")
    first = service.read_window(target, start_line=0, line_count=2)
    assert first["content"].startswith("第一行")
    assert "第二行🙂" in first["content"]
    assert first["startLine"] == 0

    # Allow the asynchronous index to complete, then verify direct jumping.
    service._pending[str(target.resolve())].result(timeout=5)  # noqa: SLF001
    jumped = service.read_window(target, start_line=2, line_count=2)
    assert jumped["content"].startswith("第三行")
    assert "第四行" in jumped["content"]
    assert jumped["lineCountExact"] is True


def test_large_file_index_invalidates_after_file_change(tmp_path: Path):
    service = LargeFileService()
    target = tmp_path / "large.txt"
    target.write_text("a\nb\nc\n", encoding="utf-8")
    service.read_window(target, start_line=0, line_count=1)
    service._pending[str(target.resolve())].result(timeout=5)  # noqa: SLF001
    assert service.read_window(target, start_line=2, line_count=1)["content"].startswith("c")

    target.write_text("甲\n乙\n丙\n丁\n", encoding="utf-8")
    refreshed = service.read_window(target, start_line=0, line_count=2)
    assert "甲" in refreshed["content"]
    assert refreshed["mtimeMs"] == int(target.stat().st_mtime * 1000)


def test_large_file_bounds_empty_files_and_cache_eviction(tmp_path: Path):
    service = LargeFileService()
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    empty_index = service._build_index(empty, 0, empty.stat().st_mtime_ns)  # noqa: SLF001
    assert empty_index.offsets == (0, 0)

    response = service.read_window(empty, start_line=-20, line_count=0)
    assert response["startLine"] == 0
    assert response["loadedLines"] == 0
    assert response["lineCount"] == 0
    assert response["hasPrevious"] is False

    for index in range(MAX_INDEX_CACHE_ENTRIES + 2):
        key = f"cache-{index}"
        service._store_index(key, LineIndex(size=index, mtime_ns=index, offsets=(0, 0)))  # noqa: SLF001
    assert len(service._indexes) == MAX_INDEX_CACHE_ENTRIES  # noqa: SLF001
    assert "cache-0" not in service._indexes  # noqa: SLF001
    assert get_large_file_service() is get_large_file_service()


def test_large_file_chunk_limit_and_cached_index_clamping(tmp_path: Path):
    service = LargeFileService()
    target = tmp_path / "chunked.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    first = service.read_window(target, start_line=1, line_count=1)
    assert first["content"] == "two\n"
    assert first["hasPrevious"] is True
    assert first["hasNext"] is True

    key = str(target.resolve())
    service._pending[key].result(timeout=5)  # noqa: SLF001
    clamped = service.read_window(target, start_line=999, line_count=99999)
    assert clamped["startLine"] == 2
    assert clamped["content"].replace("\r\n", "\n") == "three\n"
    assert clamped["hasNext"] is False

    # A fresh schedule is ignored while a valid cached index is available.
    service._schedule_index(target, target.stat().st_size, target.stat().st_mtime_ns)  # noqa: SLF001
    assert key not in service._pending  # noqa: SLF001


def test_large_file_byte_budget_pending_and_stale_index_branches(tmp_path: Path):
    service = LargeFileService()
    target = tmp_path / "byte-budget.txt"
    target.write_text(("甲" * 100_000) + "\n" + ("乙" * 100_000) + "\n", encoding="utf-8")
    limited = service.read_window(target, start_line=0, line_count=20)
    assert limited["loadedLines"] == 1
    assert limited["hasNext"] is True

    key = str(target.resolve())
    service._pending[key].result(timeout=5)  # noqa: SLF001
    service.read_window(target, start_line=0, line_count=1)
    cached = service._indexes[key]  # noqa: SLF001
    assert service._get_index(target, cached.size, cached.mtime_ns + 1) is None  # noqa: SLF001

    unresolved: Future[LineIndex] = Future()
    service._pending[key] = unresolved  # noqa: SLF001
    assert service._get_index(target, target.stat().st_size, target.stat().st_mtime_ns) is None  # noqa: SLF001
    service._schedule_index(target, target.stat().st_size, target.stat().st_mtime_ns)  # noqa: SLF001
    assert service._pending[key] is unresolved  # noqa: SLF001
    unresolved.cancel()

    large_response = service._response(  # noqa: SLF001
        target=target,
        stat=SimpleNamespace(st_size=LARGE_FILE_BYTES + 1, st_mtime=1.25),
        content="preview",
        start_line=5,
        loaded_lines=1,
        total_lines=10,
        exact=False,
    )
    assert large_response["mode"] == "large-readonly"
    assert large_response["readOnly"] is True
    assert large_response["hasPrevious"] is True
