from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.content_catalog_service import (
    ContentCatalogRefreshError,
    ContentCatalogService,
    get_content_catalog_service,
    reset_content_catalog_services,
)
from services.retrieval_service import RetrievalService
from services.source_contract import build_source_span, describe_utf8_source, source_revision_id
from services.story_project_service import StoryProjectService
from storage.file_adapter import FileAdapter


@pytest.fixture(autouse=True)
def _reset_catalog_registry() -> None:
    reset_content_catalog_services()
    yield
    reset_content_catalog_services()


def _write_source(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_source_revision_and_span_match_retrieval_contract() -> None:
    text = "第一行\nsecond line\n尾行"
    raw = text.encode("utf-8")
    expected_revision = source_revision_id(raw)
    source, decoded = describe_utf8_source(
        path="chapters/第一章.md",
        raw=raw,
        mtime_ns=123,
    )
    span = build_source_span(
        decoded,
        revision=source.revision,
        start_char=4,
        end_char=len(text),
    )

    assert source.revision == expected_revision
    assert source.size_bytes == len(raw)
    assert source.total_chars == len(text)
    assert source.total_lines == 3
    assert span.start_byte == len(text[:4].encode("utf-8"))
    assert span.end_byte == len(raw)
    assert span.start_line == 2
    assert span.end_line == 3
    assert span.to_dict()["endExclusive"] is True
    assert RetrievalService._source_revision(raw) == expected_revision

    chunks = RetrievalService._chunks(text, expected_revision)
    assert chunks[0]["revision"] == expected_revision
    assert chunks[0]["startByte"] == 0
    assert chunks[0]["endByte"] == len(raw)


def test_catalog_publishes_immutable_warm_snapshot_without_rescanning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_source(tmp_path, "chapters/第1章/001.md", "正文\n第二行\n")
    service = ContentCatalogService(tmp_path)

    first = service.snapshot()

    assert first.generation == 1
    assert first.catalog_revision.startswith("sha256:")
    assert first.get("chapters/第1章/001.md").kind == "chapter"
    assert first.get("chapters/第1章/001.md").freshness == "fresh"
    assert "chapters/第1章" in first.directories
    with pytest.raises(TypeError):
        first.entries["other"] = first.get("chapters/第1章/001.md")

    monkeypatch.setattr(
        service,
        "_scan_roots",
        lambda: (_ for _ in ()).throw(AssertionError("warm snapshot rescanned")),
    )
    second = service.snapshot()

    assert second is first


def test_dirty_refresh_detects_same_size_same_mtime_content_replacement(tmp_path: Path) -> None:
    path = _write_source(tmp_path, "chapters/第1章/001.md", "alpha")
    service = ContentCatalogService(tmp_path)
    first = service.snapshot()
    before = path.stat()

    path.write_text("bravo", encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    service.notify_external_changes([path])
    second = service.refresh_dirty()

    assert second.generation == first.generation + 1
    assert second.get("chapters/第1章/001.md").revision != first.get(
        "chapters/第1章/001.md"
    ).revision
    assert second.get("chapters/第1章/001.md").mtime_ns == before.st_mtime_ns
    assert service.dirty_file_count == 0


def test_failed_dirty_refresh_keeps_previous_snapshot_and_dirty_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_source(tmp_path, "chapters/第1章/001.md", "before")
    service = ContentCatalogService(tmp_path)
    first = service.snapshot()
    path.write_text("after!", encoding="utf-8")
    service.mark_dirty([path])
    original = service._read_catalog_entry

    monkeypatch.setattr(
        service,
        "_read_catalog_entry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("forced catalog failure")),
    )
    with pytest.raises(ContentCatalogRefreshError, match="forced catalog failure"):
        service.refresh_dirty()

    assert service.peek_snapshot().generation == first.generation
    assert service.peek_snapshot().get("chapters/第1章/001.md").revision == first.get(
        "chapters/第1章/001.md"
    ).revision
    assert service.dirty_file_count == 1

    monkeypatch.setattr(service, "_read_catalog_entry", original)
    recovered = service.refresh_dirty()
    assert recovered.generation == first.generation + 1
    assert service.dirty_file_count == 0


def test_dirty_refresh_reads_only_the_affected_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_path = _write_source(tmp_path, "chapters/Chapter 1/001.md", "first")
    _write_source(tmp_path, "chapters/Chapter 2/001.md", "second")
    service = ContentCatalogService(tmp_path)
    service.snapshot()
    original = service._read_catalog_entry
    reads: list[str] = []

    def tracked(path: Path, relative: str):
        reads.append(relative)
        return original(path, relative)

    monkeypatch.setattr(service, "_read_catalog_entry", tracked)
    first_path.write_text("changed", encoding="utf-8")
    service.mark_dirty([first_path])
    service.refresh_dirty()

    assert reads == ["chapters/Chapter 1/001.md"]


def test_dirty_new_file_publishes_missing_parent_directories(tmp_path: Path) -> None:
    (tmp_path / "chapters").mkdir()
    service = ContentCatalogService(tmp_path)
    service.snapshot()
    path = _write_source(tmp_path, "chapters/New Chapter/001.md", "new chapter")

    service.mark_dirty([path])
    refreshed = service.refresh_dirty()
    states = StoryProjectService().list_chapter_states(
        tmp_path,
        catalog_snapshot=refreshed,
    )

    assert "chapters/New Chapter" in refreshed.directories
    assert [state.relative_path for state in states] == ["chapters/New Chapter"]


def test_file_adapter_write_rename_and_delete_feed_catalog_dirty_queue(tmp_path: Path) -> None:
    _write_source(tmp_path, "chapters/第1章/001.md", "before")
    catalog = get_content_catalog_service(tmp_path)
    first = catalog.snapshot()
    adapter = FileAdapter(tmp_path)

    adapter.write_text("chapters/第1章/001.md", "after")
    assert catalog.dirty_file_count == 1
    second = catalog.refresh_dirty()
    assert second.get("chapters/第1章/001.md").revision != first.get(
        "chapters/第1章/001.md"
    ).revision

    adapter.rename_path("chapters/第1章/001.md", "chapters/第1章/002.md")
    assert catalog.dirty_file_count == 2
    renamed = catalog.refresh_dirty()
    assert renamed.get("chapters/第1章/001.md") is None
    assert renamed.get("chapters/第1章/002.md") is not None

    adapter.delete_path("chapters/第1章/002.md")
    deleted = catalog.refresh_dirty()
    assert deleted.get("chapters/第1章/002.md") is None


def test_catalog_dirty_queue_ignores_paths_outside_declared_roots(tmp_path: Path) -> None:
    _write_source(tmp_path, "chapters/001.md", "chapter")
    service = ContentCatalogService(tmp_path)
    initial = service.snapshot()
    _write_source(tmp_path, "README.md", "project readme")
    _write_source(tmp_path, ".storydex/memory/file-history/ignored.md", "history")

    dirty_count = service.mark_dirty(
        ["README.md", ".storydex/memory/file-history/ignored.md"]
    )

    assert dirty_count == 0
    assert service.refresh_dirty() is initial
    assert initial.get("README.md") is None
    assert initial.get(".storydex/memory/file-history/ignored.md") is None


def test_catalog_dirty_parent_targets_unified_storydex_root(tmp_path: Path) -> None:
    _write_source(tmp_path, ".storydex/memory/current/items.json", "{}")
    service = ContentCatalogService(tmp_path)
    service.snapshot()

    service.mark_dirty([".storydex"])

    assert service.dirty_file_count == 1


def test_catalog_registry_isolated_by_canonical_workspace(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_source(left, "chapters/001.md", "left")
    _write_source(right, "chapters/001.md", "right")

    left_service = get_content_catalog_service(left)
    right_service = get_content_catalog_service(right)

    assert left_service is get_content_catalog_service(left / ".")
    assert left_service is not right_service
    assert left_service.snapshot().get("chapters/001.md").revision != right_service.snapshot().get(
        "chapters/001.md"
    ).revision


def test_catalog_backed_story_views_match_filesystem_views(tmp_path: Path) -> None:
    project = StoryProjectService()
    project.ensure_project_structure(tmp_path)
    _write_source(tmp_path, "chapters/Chapter 1/001.md", "alpha\n")
    _write_source(tmp_path, "chapters/Chapter 2/001.md", "bravo\n")
    _write_source(tmp_path, ".storydex/scripts/continuity.md", "alpha continuity\n")

    filesystem_states = project.list_chapter_states(tmp_path)
    filesystem_recent = project.list_recent_segments(
        tmp_path,
        limit=2,
        include_content=True,
    )
    filesystem_scripts = project.list_relevant_scripts(
        tmp_path,
        prompt="alpha continuity",
        limit=2,
        include_content=True,
    )
    filesystem_next = project.compute_next_segment_path(tmp_path)

    catalog = ContentCatalogService(tmp_path).snapshot()
    catalog_states = project.list_chapter_states(
        tmp_path,
        catalog_snapshot=catalog,
    )
    catalog_recent = project.list_recent_segments(
        tmp_path,
        limit=2,
        include_content=True,
        chapter_states=catalog_states,
        catalog_snapshot=catalog,
    )
    catalog_scripts = project.list_relevant_scripts(
        tmp_path,
        prompt="alpha continuity",
        limit=2,
        include_content=True,
        chapter_states=catalog_states,
        catalog_snapshot=catalog,
    )
    catalog_next = project.compute_next_segment_path(
        tmp_path,
        chapter_states=catalog_states,
        catalog_snapshot=catalog,
    )

    assert [item.__dict__ for item in catalog_states] == [
        item.__dict__ for item in filesystem_states
    ]
    assert catalog_recent == filesystem_recent
    assert catalog_scripts == filesystem_scripts
    assert catalog_next == filesystem_next
