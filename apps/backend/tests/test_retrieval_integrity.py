from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import feature_flags
from services import retrieval_service
from services.content_catalog_service import get_content_catalog_service
from services.content_pipeline_service import ContentPipelineService
from services.retrieval_service import (
    INDEX_ERROR,
    INDEX_OK,
    INDEX_STALE,
    RetrievalService,
    get_retrieval_service,
    reset_retrieval_cache,
)
from services.story_project_service import get_story_project_service


def _write_exact(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path.read_bytes().decode("utf-8")


def _assert_exact_snippet(path: Path, hit: dict) -> None:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    span = hit["snippetSpan"]
    assert text[span["startChar"] : span["endChar"]] == hit["snippet"]
    assert raw[span["startByte"] : span["endByte"]].decode("utf-8") == hit["snippet"]


def test_v3_indexes_long_file_head_middle_tail_with_exact_revision_and_spans(tmp_path) -> None:
    chapter = tmp_path / "chapters" / "long.md"
    markers = ("HEAD_UNIQUE_A17", "MIDDLE_UNIQUE_C83", "TAIL_UNIQUE_F29")
    text = _write_exact(
        chapter,
        f"{markers[0]}\r\n"
        + ("普通背景段落。\r\n" * 10_000)
        + f"{markers[1]}\r\n"
        + ("尾部背景段落。\r\n" * 10_000)
        + f"{markers[2]}\r\n",
    )
    raw = chapter.read_bytes()
    service = RetrievalService(tmp_path)

    assert service.build_index() == 1
    status = service.index_status()
    assert status["status"] == INDEX_OK
    assert status["schemaVersion"] == 3
    assert status["database"].endswith("retrieval.fts5.v3.db")
    assert status["coverage"]["documentCount"] == 1
    assert status["coverage"]["chunkCount"] > 3
    assert status["coverage"]["totalChars"] == len(text)
    assert status["coverage"]["totalBytes"] == len(raw)

    expected_revision = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    for marker in markers:
        result = service.search_detailed(marker)
        assert result["status"] == INDEX_OK
        assert result["resultState"] == "hits"
        hit = result["hits"][0]
        assert marker in hit["snippet"]
        assert hit["revision"] == expected_revision
        assert hit["span"]["startChar"] <= hit["snippetSpan"]["startChar"]
        assert hit["snippetSpan"]["endChar"] <= hit["span"]["endChar"]
        _assert_exact_snippet(chapter, hit)


def test_v3_chunk_overlap_recovers_utf8_phrase_across_long_line_boundary(tmp_path) -> None:
    chapter = tmp_path / "chapters" / "one-line.md"
    marker = "星核🙂密钥"
    _write_exact(chapter, ("界" * 3_197) + marker + ("界" * 5_000))
    service = RetrievalService(tmp_path)
    service.build_index()

    result = service.search_detailed("星核 密钥")

    assert result["status"] == INDEX_OK
    hit = result["hits"][0]
    assert marker in hit["snippet"]
    _assert_exact_snippet(chapter, hit)
    assert hit["snippetSpan"]["endByte"] > hit["snippetSpan"]["endChar"]


def test_v3_reports_stale_then_replaces_update_delete_and_rename_chunks(tmp_path) -> None:
    chapter = tmp_path / "chapters" / "old.md"
    _write_exact(chapter, "olduniquex91\n")
    service = RetrievalService(tmp_path)
    service.build_index()

    _write_exact(chapter, "newuniquey82\n")
    get_content_catalog_service(tmp_path).notify_external_changes([chapter])
    stale = service.search_detailed("newuniquey82")
    assert stale["status"] == INDEX_STALE
    assert stale["resultState"] == "unavailable"
    assert service.watch_files() == 1
    assert service.search_detailed("newuniquey82")["resultState"] == "hits"
    assert service.search_detailed("olduniquex91")["resultState"] == "no_hits"

    renamed = chapter.with_name("renamed.md")
    chapter.rename(renamed)
    _write_exact(renamed, "renameduniquez73\n")
    get_content_catalog_service(tmp_path).notify_external_changes([chapter, renamed])
    assert service.watch_files() == 2
    renamed_result = service.search_detailed("renameduniquez73")
    assert renamed_result["hits"][0]["path"] == "chapters/renamed.md"

    renamed.unlink()
    get_content_catalog_service(tmp_path).notify_external_changes([renamed])
    assert service.watch_files() == 1
    assert service.search_detailed("renameduniquez73")["resultState"] == "no_hits"
    assert service.index_status()["coverage"]["documentCount"] == 0


def test_failed_full_build_keeps_last_complete_index_and_exposes_error(tmp_path, monkeypatch) -> None:
    stable = tmp_path / "chapters" / "stable.md"
    _write_exact(stable, "stable-published-token\n")
    legacy = tmp_path / ".storydex" / ".cache" / "retrieval.fts5.v2.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"legacy-must-survive")
    service = RetrievalService(tmp_path)
    service.build_index()
    _write_exact(tmp_path / "chapters" / "new.md", "explode-during-build\n")
    original_tokenized = service._tokenized

    def fail_on_marker(text: str) -> str:
        if "explode-during-build" in text:
            raise RuntimeError("forced-v3-build-failure")
        return original_tokenized(text)

    monkeypatch.setattr(service, "_tokenized", fail_on_marker)
    with pytest.raises(RuntimeError, match="forced-v3-build-failure"):
        service.build_index()

    assert legacy.read_bytes() == b"legacy-must-survive"
    assert service.index_status(check_stale=False)["status"] == INDEX_ERROR
    assert service.search_detailed("stable-published-token")["status"] == INDEX_ERROR
    assert service.search("stable-published-token")
    assert not list(service.db_path.parent.glob(f".{service.db_path.name}.*.tmp"))

    monkeypatch.setattr(service, "_tokenized", original_tokenized)
    assert service.build_index() == 2
    assert service.search_detailed("explode-during-build")["resultState"] == "hits"










def test_chunk_ranking_returns_best_chunk_per_path_before_candidate_limit(tmp_path) -> None:
    _write_exact(
        tmp_path / "chapters" / "many.md",
        ("shared-ranking-token repeated context\n" * 4_000),
    )
    _write_exact(
        tmp_path / "chapters" / "other.md",
        "shared-ranking-token appears in another relevant file\n",
    )
    service = RetrievalService(tmp_path)
    service.build_index()

    result = service.search_detailed(
        "shared-ranking-token",
        top_k=2,
        candidate_limit=2,
    )

    assert result["status"] == INDEX_OK
    assert set(result["candidatePaths"]) == {"chapters/many.md", "chapters/other.md"}
    assert len(result["hits"]) == 2
    assert len({hit["path"] for hit in result["hits"]}) == 2
