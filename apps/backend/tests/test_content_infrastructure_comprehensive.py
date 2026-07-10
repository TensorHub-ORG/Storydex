from __future__ import annotations

import json
import struct
import subprocess
import types
from pathlib import Path

import pytest

from services import character_models, file_history_service, help_guide_service, hooks_service, index_service, media_reader, memory_reader, narrative_models


class Flags:
    def __init__(self, **values):
        self.values = values

    def get_bool(self, name):
        return bool(self.values.get(name, False))


def test_character_narrative_models_and_legacy_split():
    assert character_models.to_camel("current_location") == "currentLocation"
    split = character_models.split_legacy_character({
        "id": "alice", "name": "Alice", "role": "hero", "current_location": "city",
        "known_secrets": ["x"], "custom": 3, "schema_version": "1",
    })
    assert split["card"]["id"] == "alice" and split["card"]["custom"] == 3
    assert split["state"]["current_location"] == "city"
    fallback = character_models.split_legacy_character({"name": "Bob"})
    assert fallback["card"]["id"] == "Bob"
    assert character_models.StableRelationship(targetId="b").target_id == "b"
    assert character_models.ProseVoice(tone="calm").tone == "calm"
    assert character_models.RecentChange(chapterId="1").chapter_id == "1"
    assert narrative_models.Thread(id="t", title="Main").pending_beats == []
    assert narrative_models.Foreshadowing(id="f", title="Hint", introduced_in="1").current_status == "active"
    assert narrative_models.Unresolved(id="u", title="Question", introduced_in="1", resolution_condition="answer").priority == "normal"


def test_memory_migration_dry_run_copy_idempotence_and_reader(monkeypatch, tmp_path):
    storydex = tmp_path / ".storydex"
    old_snap = storydex / "memory/chapters/001.variables.json"
    old_snap.parent.mkdir(parents=True)
    old_snap.write_text("{}", encoding="utf-8")
    old_state = storydex / "memory/current-state/全部变量.json"
    old_state.parent.mkdir(parents=True)
    old_state.write_text('{"chapter":1}', encoding="utf-8")
    old_summary = storydex / "memory/concision/001.md"
    old_summary.parent.mkdir(parents=True)
    old_summary.write_text("summary", encoding="utf-8")
    chars = storydex / "characters"
    chars.mkdir(parents=True)
    (chars / "alice.json").write_text(json.dumps({"id": "alice", "name": "Alice", "current_location": "city"}), encoding="utf-8")
    (chars / "bad.json").write_text("{broken", encoding="utf-8")
    (chars / "list.json").write_text("[]", encoding="utf-8")

    dry = memory_reader.migrate_workspace(tmp_path, dry_run=True)
    assert dry["migrated"] and not (storydex / "memory/current/story_state.json").exists()
    report = memory_reader.migrate_workspace(tmp_path)
    assert report["migrated"]
    assert (storydex / "memory/raw/snapshots/001.variables.json").exists()
    assert (chars / "cards/alice.json").exists() and (chars / "states/alice.json").exists()
    assert json.loads((storydex / "config/schema-versions.json").read_text(encoding="utf-8"))["memory_layer"] == "v2"
    again = memory_reader.migrate_workspace(tmp_path)
    assert again["skipped"]

    monkeypatch.setattr(memory_reader, "get_flags", lambda: Flags(MEMORY_LAYER_V2=True))
    reader = memory_reader.MemoryReader(tmp_path)
    assert reader.read_story_state()["chapter"] == 1
    assert reader.list_rolling_summaries()[0].name == "001.md"
    assert reader.read_character_card("alice")["name"] == "Alice"

    (storydex / "memory/current/story_state.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(memory_reader, "get_flags", lambda: Flags(MEMORY_LAYER_V2=False))
    assert reader.read_story_state()["chapter"] == 1
    assert reader.read_character_card("alice")["name"] == "Alice"
    assert reader.read_character_card("missing") is None

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(memory_reader, "get_flags", lambda: Flags(MEMORY_LAYER_V2=True))
    assert memory_reader.MemoryReader(empty).read_story_state() == {}
    assert memory_reader.MemoryReader(empty).list_rolling_summaries() == []


def test_file_history_backups_flag_paths_duplicates_and_escape(monkeypatch, tmp_path):
    storydex_root = tmp_path / ".storydex"
    service = file_history_service.FileHistoryService()
    service.project_service = types.SimpleNamespace(workspace_root=tmp_path.resolve(), storydex_root=storydex_root)
    file = tmp_path / "chapters/a.md"
    file.parent.mkdir(parents=True)
    file.write_text("content", encoding="utf-8")
    outside = tmp_path.parent / "outside-storydex-test.md"
    outside.write_text("outside", encoding="utf-8")

    import core.feature_flags as flags_module
    monkeypatch.setattr(flags_module, "get_flags", lambda: Flags(ASYNC_FILE_BACKUP_ENABLED=True))
    assert service.backup_before_operations([{"op": "write", "relativePath": "chapters/a.md"}]) == []
    backups = service.backup_at_commit([
        None,
        {"op": "write", "relativePath": "./chapters/a.md"},
        {"op": "write", "relativePath": "chapters/a.md"},
        {"op": "rename", "fromRelativePath": "chapters/a.md"},
        {"op": "write", "relativePath": "../outside-storydex-test.md"},
        {"op": "write", "relativePath": "chapters"},
    ], trace_id="t")
    assert len(backups) == 1
    assert backups[0]["sha256"] and backups[0]["traceId"] == "t"
    assert (tmp_path / backups[0]["backupPath"]).is_file()
    monkeypatch.setattr(flags_module, "get_flags", lambda: Flags(ASYNC_FILE_BACKUP_ENABLED=False))
    assert service.backup_before_operations([{"relativePath": "chapters/a.md"}])
    assert service._source_paths_for_operation({"op": "rename", "fromRelativePath": "x"}) == ["x"]
    assert service._normalize_relative_path("././a\\b") == "a/b"
    outside.unlink()


def test_help_guide_read_search_snippets_fallback_and_mtime(monkeypatch, tmp_path):
    root = tmp_path / "guide"
    root.mkdir()
    (root / "01.md").write_text("# Getting Started\n\nOpen a project and use Coomi.", encoding="utf-8")
    (root / "02.md").write_text("No heading but Coomi appears twice. Coomi.", encoding="utf-8")
    (root / "folder").mkdir()
    monkeypatch.setenv("STORYDEX_HELP_GUIDE_ROOT", str(root))
    service = help_guide_service.HelpGuideService()
    guide = service.read_guide()
    assert len(guide["items"]) == 2 and "Getting Started" in guide["content"]
    assert service.search("", max_results=1)["items"][0]["preview"]
    results = service.search("Coomi")
    assert results["items"][0]["score"] >= 1 and results["items"][0]["snippets"]
    assert service.search("missing")["items"] == []
    assert service._extract_title("text", "fallback") == "fallback"
    assert service._build_combined_content([]).startswith("# 使用指南")
    class BadPath:
        def stat(self):
            raise OSError("missing")

    assert service._mtime_iso(BadPath()) == ""
    monkeypatch.delenv("STORYDEX_HELP_GUIDE_ROOT")
    monkeypatch.setattr(help_guide_service, "__file__", str(tmp_path / "isolated/service.py"))
    assert service._resolve_guide_root() is None


def test_hooks_sync_async_spawn_timeout_config_and_logs(monkeypatch, tmp_path):
    service = hooks_service.HooksService()
    service.project_service = types.SimpleNamespace(workspace_root=tmp_path, storydex_root=tmp_path / ".storydex")
    hooks_path = service.project_service.storydex_root / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(json.dumps({
        "event": [None, {}, {"command": "ok", "name": "success", "sync": True}, {"command": "fail", "name": "failure", "sync": True}, {"command": "slow", "name": "timeout", "sync": True}, {"command": "async", "name": "background"}]
    }), encoding="utf-8")
    import core.feature_flags as flags_module
    monkeypatch.setattr(flags_module, "get_flags", lambda: Flags(ASYNC_HOOKS_ENABLED=True))

    def fake_run(command, **kwargs):
        if command == "slow":
            raise subprocess.TimeoutExpired(command, 1, output="partial", stderr="late")
        return types.SimpleNamespace(returncode=0 if command == "ok" else 2, stdout="out", stderr="err")

    monkeypatch.setattr(hooks_service.subprocess, "run", fake_run)
    monkeypatch.setattr(hooks_service.subprocess, "Popen", lambda *args, **kwargs: object())
    results = service.run("event", {"x": 1}, timeout_seconds=1)
    assert [item["status"] for item in results] == ["ok", "error", "timeout", "fire_and_forget"]
    assert (service.project_service.storydex_root / "logs/hooks.jsonl").is_file()
    assert service.run("missing", {}) == []
    service._append_hook_log([])
    hooks_path.write_text("[]", encoding="utf-8")
    assert service._load_hooks() == {}
    hooks_path.write_text("{broken", encoding="utf-8")
    assert service._load_hooks() == {}
    hooks_path.unlink()
    assert service._load_hooks() == {}

    hooks_path.write_text(json.dumps({"event": [{"command": "async"}]}), encoding="utf-8")
    monkeypatch.setattr(hooks_service.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("spawn")))
    assert service.run("event", {})[0]["status"] == "spawn_error"


def test_media_reader_notebook_pdf_images_and_dimension_helpers(tmp_path):
    reader = media_reader.MediaReader()
    notebook = tmp_path / "test.ipynb"
    notebook.write_text(json.dumps({"cells": [None, {"cell_type": "markdown", "source": ["# Hi\n", "text"]}, {"cell_type": "code", "source": "print(1)"}]}), encoding="utf-8")
    doc = reader.read_special_document(notebook, workspace_root=tmp_path)
    assert doc["kind"] == "notebook" and doc["media"]["cellCount"] == 2
    pdf = tmp_path / "test.pdf"
    pdf.write_bytes(b"%PDF /Type /Page (This is readable PDF preview text) <Another readable text fragment>")
    pdf_doc = reader.read_special_document(pdf, workspace_root=tmp_path)
    assert pdf_doc["kind"] == "pdf" and pdf_doc["media"]["pageCountEstimate"] == 1
    assert "readable" in pdf_doc["content"]

    png = tmp_path / "image.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 20, 10))
    image = reader.read_special_document(png, workspace_root=tmp_path)
    assert image["media"]["width"] == 20 and image["media"]["height"] == 10
    gif_data = b"GIF89a" + struct.pack("<HH", 7, 8)
    assert media_reader._detect_image_dimensions(gif_data, ".gif")["height"] == 8
    webp = bytearray(b"RIFF" + b"\0" * 4 + b"WEBP" + b"VP8X" + b"\0" * 8 + (4).to_bytes(3, "little") + (5).to_bytes(3, "little"))
    assert media_reader._webp_dimensions(bytes(webp)) == {"width": 5, "height": 6}
    jpeg = b"\xff\xd8\xff\xc0\x00\x09\x08\x00\x0a\x00\x14\x03\x00"
    assert media_reader._jpeg_dimensions(jpeg) == {"width": 20, "height": 10}
    assert media_reader._jpeg_dimensions(b"\xff\xd8bad") is None
    assert media_reader._webp_dimensions(b"bad") is None
    assert media_reader._detect_image_dimensions(b"bad", ".bmp")["width"] is None
    assert reader.read_special_document(tmp_path / "plain.txt", workspace_root=tmp_path) is None


def test_index_search_candidates_hybrid_ripgrep_bm25_and_snippets(monkeypatch, tmp_path):
    for rel in (".storydex/memory/a.md", "chapters/1.md", "docs/info.json", ".storydex/logs/skip.md"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("dragon story text", encoding="utf-8")
    service = index_service.IndexService()
    candidates = list(service._iter_candidate_files(tmp_path))
    assert len(candidates) == 3
    assert "dragon" in service._build_snippet("x" * 100 + "dragon" + "y" * 200, ["dragon"])
    assert len(service._build_snippet("a" * 300, ["missing"])) == 220
    assert service._clean_snippet(" a\n b ") == "a b"
    assert service._clean_snippet("x" * 20, max_len=10).endswith("...")

    class Workspace:
        workspace_root = tmp_path
        storydex_root = tmp_path / ".storydex"

    monkeypatch.setattr(index_service, "WorkspaceIO", Workspace)
    monkeypatch.setattr(index_service, "hybrid_search", lambda **kwargs: [{"engine": "hybrid"}])
    assert service.search("dragon")[0]["engine"] == "hybrid"

    monkeypatch.setattr(index_service, "hybrid_search", lambda **kwargs: [])
    monkeypatch.setattr(index_service.shutil, "which", lambda name: "rg")
    payloads = "\n".join([
        "bad json",
        json.dumps({"type": "begin"}),
        json.dumps({"type": "match", "data": {"path": {"text": "chapters/1.md"}, "line_number": 2, "lines": {"text": "dragon here\n"}, "submatches": [{}, {}]}}),
    ])
    monkeypatch.setattr(index_service.subprocess, "run", lambda *args, **kwargs: types.SimpleNamespace(returncode=0, stdout=payloads))
    hits = service.search("dragon")
    assert hits[0]["engine"] == "ripgrep" and hits[0]["matchCount"] == 2

    monkeypatch.setattr(index_service.shutil, "which", lambda name: None)
    monkeypatch.setattr(index_service, "bm25_search", lambda **kwargs: [{"doc_id": "chapters/1.md", "metadata": {}}])
    bm25 = service.search("dragon")
    assert bm25[0]["engine"] == "bm25" and bm25[0]["snippet"]

    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    Workspace.workspace_root = empty_root
    Workspace.storydex_root = empty_root / ".storydex"
    assert service.search("dragon") == []
    assert service._search_roots(Workspace()) == ["."]
