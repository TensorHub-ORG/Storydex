from pathlib import Path

from services.large_file_service import LARGE_FILE_BYTES, SMALL_FILE_BYTES, LargeFileService


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
