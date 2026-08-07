from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import feature_flags
from services import retrieval_service
from services.context_policy import ContextPolicy
from services.retrieval_service import (
    INDEX_ERROR,
    INDEX_OK,
    INDEX_STALE,
    RetrievalService,
    get_retrieval_service,
    reset_retrieval_cache,
)
from services.story_project_service import get_story_project_service
from services.storydex_agent_tools import StorydexProjectSearchTool
from services.storydex_context_assembler_service import StorydexContextAssemblerService


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
    stale = service.search_detailed("newuniquey82")
    assert stale["status"] == INDEX_STALE
    assert stale["resultState"] == "unavailable"
    assert service.watch_files() == 1
    assert service.search_detailed("newuniquey82")["resultState"] == "hits"
    assert service.search_detailed("olduniquex91")["resultState"] == "no_hits"

    renamed = chapter.with_name("renamed.md")
    chapter.rename(renamed)
    _write_exact(renamed, "renameduniquez73\n")
    assert service.watch_files() == 2
    renamed_result = service.search_detailed("renameduniquez73")
    assert renamed_result["hits"][0]["path"] == "chapters/renamed.md"

    renamed.unlink()
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


def test_project_search_distinguishes_complete_no_hits_from_index_error(tmp_path) -> None:
    _write_exact(tmp_path / "chapters" / "001.md", "known-search-token\n")
    reset_retrieval_cache()
    tool = StorydexProjectSearchTool(workspace_root=tmp_path)

    no_hit = tool.run({"query": "missinguniquex99"})
    no_hit_payload = json.loads(no_hit.output)
    assert no_hit.success is True
    assert no_hit_payload["status"] == INDEX_OK
    assert no_hit_payload["resultState"] == "no_hits"

    service = get_retrieval_service(tmp_path)
    conn = sqlite3.connect(service.db_path)
    try:
        conn.execute("DROP TABLE chunks")
        conn.commit()
    finally:
        conn.close()

    failed = tool.run({"query": "known-search-token"})
    failed_payload = json.loads(failed.output)
    assert failed.success is False
    assert failed_payload["ok"] is False
    assert failed_payload["status"] == INDEX_ERROR
    assert failed_payload["resultState"] == "unavailable"
    assert failed_payload["results"] == []


def test_project_search_refresh_exception_fails_closed_even_if_old_state_says_ok(
    tmp_path,
    monkeypatch,
) -> None:
    fake_service = SimpleNamespace(
        watch_files=lambda: (_ for _ in ()).throw(RuntimeError("refresh-crashed")),
        index_status=lambda **_kwargs: {"status": INDEX_OK, "generation": "sha256:old"},
        search_detailed=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not query after a failed refresh")
        ),
    )
    monkeypatch.setattr(retrieval_service, "get_retrieval_service", lambda _root: fake_service)

    result = StorydexProjectSearchTool(workspace_root=tmp_path).run({"query": "anything"})
    payload = json.loads(result.output)

    assert result.success is False
    assert payload["status"] == INDEX_ERROR
    assert payload["resultState"] == "unavailable"
    assert payload["error"] == "refresh-crashed"


def test_passive_retrieval_error_is_visible_in_block_notes_and_context_trace(tmp_path, monkeypatch) -> None:
    fake_service = SimpleNamespace(
        watch_files=lambda: (_ for _ in ()).throw(RuntimeError("forced-passive-index-error")),
        index_status=lambda **_kwargs: {
            "status": INDEX_ERROR,
            "schemaVersion": 3,
            "generation": "sha256:last-good",
            "coverage": {"documentCount": 2, "chunkCount": 7},
            "lastError": "forced-passive-index-error",
        },
    )
    monkeypatch.setattr(feature_flags, "get_flags", lambda: SimpleNamespace(get_bool=lambda _name: True))
    monkeypatch.setattr(retrieval_service, "get_retrieval_service", lambda _root: fake_service)
    policy = ContextPolicy(
        base_story_context=False,
        story_structured_memory=False,
        passive_fts=True,
        wiki_context=False,
        coomi_memory=False,
        active_retrieval_tools=False,
    )
    assembler = StorydexContextAssemblerService(get_story_project_service())

    assembly = assembler.assemble(tmp_path, prompt="核对『暮色钥印』", policy=policy)

    source = next(
        item
        for item in assembly["contextTrace"]["sources"]
        if item["kind"] == "related_passages"
    )
    block = next(item for item in assembly["promptBlocks"] if item["id"] == "related_passages")
    assert source["retrieval"]["status"] == INDEX_ERROR
    assert source["retrieval"]["generation"] == "sha256:last-good"
    assert "do not interpret" in block["content"]
    assert any("related_passages_index_error" in note for note in assembly["notes"])


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
