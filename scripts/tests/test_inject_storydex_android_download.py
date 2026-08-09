from pathlib import Path

from scripts.inject_storydex_android_download import (
    END_MARKER,
    START_MARKER,
    inject_overlay,
)


def test_inject_overlay_is_atomic_and_idempotent(tmp_path: Path) -> None:
    index = tmp_path / "index.html"
    index.write_text("<html><body><div id=\"app\"></div></body></html>", encoding="utf-8")

    assert inject_overlay(index, "/assets/android-v0.1.0.js", "0.1.0") is True
    first = index.read_text(encoding="utf-8")
    assert first.count(START_MARKER) == 1
    assert first.count(END_MARKER) == 1
    assert '<script defer src="/assets/android-v0.1.0.js"></script>' in first
    assert (tmp_path / "index.html.pre-android-v0.1.0.bak").exists()

    assert inject_overlay(index, "/assets/android-v0.1.0.js", "0.1.0") is False
    assert index.read_text(encoding="utf-8") == first


def test_inject_overlay_replaces_an_older_version(tmp_path: Path) -> None:
    index = tmp_path / "index.html"
    index.write_text(
        "<html><body>\n"
        f"{START_MARKER}\n<script defer src=\"/assets/android-v0.0.9.js\"></script>\n{END_MARKER}\n"
        "</body></html>",
        encoding="utf-8",
    )

    assert inject_overlay(index, "/assets/android-v0.1.0.js", "0.1.0") is True
    updated = index.read_text(encoding="utf-8")
    assert "android-v0.0.9.js" not in updated
    assert updated.count(START_MARKER) == 1
    assert "/assets/android-v0.1.0.js" in updated
