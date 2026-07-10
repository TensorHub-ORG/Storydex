from __future__ import annotations

import json
import os
import time
import types

from services.archive_service import ArchiveService
from services.compact_service import CompactService, _estimate_text_tokens
from services.dream_service import DreamService
from services.memory_extraction_service import ExtractionResult, MemoryExtractionService


def test_archive_success_errors_helpers_parsing_and_listing(tmp_path):
    assert ArchiveService(chapters_per_arc=10).should_archive(10) is True
    assert ArchiveService(chapters_per_arc=10).should_archive(9) is False
    assert ArchiveService(chapters_per_arc=10).get_arc_range(20) == "011-020"
    assert ArchiveService().create_archive(
        chapter_texts={}, story_state={}, character_state={}, arc_range="001-010", workspace_root=tmp_path
    ).success is False

    parsed = {
        "plot_summary": "summary", "character_arcs": {"Alice": {}},
        "resolved_conflicts": [{"description": "x"}], "active_foreshadowing": [{"description": "hint"}],
        "timeline": [], "locations_visited": ["city"], "items_introduced": ["key"],
    }

    def llm(**kwargs):
        assert kwargs["purpose"] == "archive"
        return types.SimpleNamespace(content="```json\n" + json.dumps(parsed) + "\n```")

    service = ArchiveService(llm_chat_fn=llm, chapters_per_arc=2)
    result = service.create_archive(
        chapter_texts={"002": "second", "001": "first"}, story_state={"chapter": 2},
        character_state={"Alice": {"state": "ok"}}, arc_range="001-002", workspace_root=tmp_path
    )
    assert result.success is True and result.entity_arcs == {"Alice": {}}
    assert result.active_suspense[0]["description"] == "hint"
    assert service.list_archives(tmp_path)[0]["_path"] == result.archive_path
    bad = tmp_path / ".storydex/archives/arc-bad.json"
    bad.write_text("{broken", encoding="utf-8")
    assert len(service.list_archives(tmp_path)) == 1
    assert service._combine_chapters({"b": "2", "a": "1"}).startswith("=== a ===")
    assert "故事状态" in service._build_state_context({"x": 1}, {"y": 2})
    assert service._build_state_context({}, {}) == ""
    assert ArchiveService._extract_content({"text": "x"}) == "x"
    assert ArchiveService._extract_content("x") == "x"
    assert ArchiveService._extract_content(object()) == ""
    assert ArchiveService._parse_json_response('prefix {"x":1} suffix') == {"x": 1}
    assert ArchiveService._parse_json_response("[]") is None
    assert ArchiveService._parse_json_response("broken") is None

    empty = ArchiveService(llm_chat_fn=lambda **kwargs: "").create_archive(
        chapter_texts={"1": "x"}, story_state={}, character_state={}, arc_range="1", workspace_root=tmp_path
    )
    assert "empty" in empty.error
    invalid = ArchiveService(llm_chat_fn=lambda **kwargs: "bad").create_archive(
        chapter_texts={"1": "x"}, story_state={}, character_state={}, arc_range="1", workspace_root=tmp_path
    )
    assert "parse" in invalid.error
    failed = ArchiveService(llm_chat_fn=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline"))).create_archive(
        chapter_texts={"1": "x"}, story_state={}, character_state={}, arc_range="1", workspace_root=tmp_path
    )
    assert failed.error == "offline"


def test_compact_success_all_content_types_errors_restore_and_token_estimation():
    assert CompactService().auto_compact("text", estimated_tokens=10).success is False
    assert CompactService(llm_chat_fn=lambda **kwargs: "").auto_compact("text", estimated_tokens=10).error

    accounting = types.SimpleNamespace(estimate_text_tokens=lambda text: 4)
    service = CompactService(llm_chat_fn=lambda **kwargs: types.SimpleNamespace(content=" summary "), token_accounting=accounting)
    result = service.auto_compact("long text", estimated_tokens=20)
    assert result.success is True and result.compacted_tokens == 4 and result.tokens_saved == 16
    assert CompactService(llm_chat_fn=lambda **kwargs: {"text": "dict summary"}).auto_compact("x", estimated_tokens=2).success is True
    assert CompactService(llm_chat_fn=lambda **kwargs: "plain").auto_compact("x", estimated_tokens=1).summary == "plain"
    failed = CompactService(llm_chat_fn=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("bad"))).auto_compact("x", estimated_tokens=5)
    assert failed.success is False and failed.error == "bad"
    assert service.should_compact({"status": "compact_needed"}) is True
    bundle = CompactService.build_restored_bundle(
        compact_summary="summary", current_content="current", entity_cards=["card1", "card2"], setting_entries=["setting"]
    )
    assert "[compact_summary]" in bundle and "[entity_card_2]" in bundle and "[setting_1]" in bundle
    assert CompactService.build_restored_bundle(compact_summary="") == ""
    assert _estimate_text_tokens("") == 1 and _estimate_text_tokens("x" * 30) == 10


def test_memory_extraction_llm_write_merge_invalid_existing_and_summary(tmp_path):
    payload = {
        "entities": [
            {"name": "Alice", "type": "角色", "location": "city", "emotion": "happy", "actions": ["walk", ""], "relations": [{"target": "Bob"}]},
            {"name": "Bob", "state": "hurt", "relationships": [{"target": "Alice"}]},
            {"name": ""},
        ],
        "conflicts": [{"description": "old conflict extended", "status": "active"}, {"description": "new conflict"}, {"description": ""}],
        "suspense": [{"description": "old hint", "status": "resolved"}, {"description": "new hint"}, {"description": ""}],
        "timeline": [{"marker": "day1", "event": "event"}, {"day": "day2", "event": "next"}, {"marker": ""}],
        "locations": [{"name": "city", "description": "large"}, {"name": ""}],
    }

    def llm(**kwargs):
        if kwargs["purpose"] == "memory_extraction":
            return {"content": "prefix " + json.dumps(payload) + " suffix"}
        return "rolling summary"

    service = MemoryExtractionService(llm_chat_fn=llm)
    assert service.extract_memories("").success is False
    extraction = service.extract_memories("chapter text", chapter_id="1", segment_id="001")
    assert extraction.success is True and extraction.rolling_summary == "rolling summary"

    current = tmp_path / ".storydex/memory/current"
    current.mkdir(parents=True)
    (current / "character_state.json").write_text(json.dumps({"characters": {"Alice": "bad"}}), encoding="utf-8")
    (current / "story_state.json").write_text(json.dumps({
        "active_conflicts": [None, {"id": "c1", "description": "old conflict"}],
        "active_foreshadowing": [None, {"id": "f1", "description": "old hint"}],
        "timeline": [], "locations": [],
    }), encoding="utf-8")
    written = service.write_extraction_to_state_files(
        extraction, workspace_root=tmp_path, chapter_id="Chapter 1", segment_id="001"
    )
    assert len(written) == 2
    chars = json.loads((current / "character_state.json").read_text(encoding="utf-8"))["entities"]
    assert chars["Alice"]["current_location"] == "city" and chars["Bob"]["current_state"] == "hurt"
    state = json.loads((current / "story_state.json").read_text(encoding="utf-8"))
    assert len(state["active_conflicts"]) == 3 and state["active_conflicts"][1]["status"] == "active"
    assert state["timeline"]["day2"] == "next" and state["locations"]["city"]["description"] == "large"
    assert service.write_extraction_to_state_files(ExtractionResult(), workspace_root=tmp_path) == []
    summary_path = service.write_rolling_summary("summary", workspace_root=tmp_path, chapter_id="Chapter / 1")
    assert summary_path.endswith("Chapter___1.md")
    assert service.write_rolling_summary("", workspace_root=tmp_path) is None

    assert MemoryExtractionService._extract_content(types.SimpleNamespace(content="x")) == "x"
    assert MemoryExtractionService._extract_content({"text": "x"}) == "x"
    assert MemoryExtractionService._extract_content(object()) == ""
    assert MemoryExtractionService._parse_json_response("```json\n{\"x\":1}\n```") == {"x": 1}
    assert MemoryExtractionService._parse_json_response("[]") is None
    assert MemoryExtractionService._parse_json_response("bad") is None
    assert MemoryExtractionService(llm_chat_fn=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline"))).extract_memories("x").error == "offline"


def test_dream_orient_gather_consolidate_prune_run_and_helpers(tmp_path):
    storydex = tmp_path / ".storydex"
    current = storydex / "memory/current"
    current.mkdir(parents=True)
    story_state_path = current / "story_state.json"
    story_state_path.write_text(json.dumps({
        "version": 2,
        "active_conflicts": [{"id": "old", "description": "old", "status": "resolved"}, {"id": "keep", "description": "keep", "status": "active"}],
        "active_suspense": [{"id": "hint", "description": "hint", "status": "resolved"}],
    }), encoding="utf-8")
    (current / "character_state.json").write_text(json.dumps({"entities": {"Alice": {}}}), encoding="utf-8")
    (current / "thread_state.json").write_text("{broken", encoding="utf-8")
    summaries = storydex / "memory/summaries/rolling"
    summaries.mkdir(parents=True)
    (summaries / "1.md").write_text("summary", encoding="utf-8")
    archives = storydex / "archives"
    archives.mkdir()
    (archives / "arc-1.json").write_text("{}", encoding="utf-8")
    cache = storydex / ".cache/retrieval"
    cache.mkdir(parents=True)
    stale = cache / "bm25_old.json"
    stale.write_text("{}", encoding="utf-8")
    os.utime(stale, (1, 1))

    def llm(**kwargs):
        if kwargs.get("purpose") == "dream_consolidate":
            return types.SimpleNamespace(content=json.dumps({
                "version": 2,
                "active_conflicts": [{"id": "keep", "description": "keep", "status": "active"}],
                "active_suspense": [],
            }))
        return types.SimpleNamespace(content="{}")

    service = DreamService(llm_chat_fn=llm, min_session_count=2, min_interval_hours=1, stale_days=1)
    assert service.should_run_dream(session_count=1) is False
    assert service.should_run_dream(session_count=2, last_dream_ts=time.time()) is False
    assert service.should_run_dream(session_count=2, last_dream_ts=1) is True
    orient = service._orient(storydex)
    assert orient["_total_items"] >= 3 and orient["_rolling_summary_count"] == 1 and orient["_archive_count"] == 1
    gather = service._gather(storydex, orient)
    assert "_stale_items" in gather
    consolidate = service._consolidate(storydex, gather)
    assert "_merged" in consolidate
    prune = service._prune(storydex, consolidate)
    assert prune["_pruned"] >= 1 and not stale.exists()
    result = service.run_dream(tmp_path)
    assert result.success is True and result.phase == "complete"
    assert (storydex / "memory/.dream_meta.json").is_file()

    broken = DreamService()
    broken._orient = lambda root: (_ for _ in ()).throw(RuntimeError("bad"))
    assert broken.run_dream(tmp_path).error == "bad"
    assert DreamService._extract_content({"text": "x"}) == "x"
    assert DreamService._extract_content("x") == "x"
    assert DreamService._extract_content(object()) == ""
    assert DreamService._parse_json_response('prefix {"x":1} suffix') == {"x": 1}
    assert DreamService._parse_json_response("bad") is None
