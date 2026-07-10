"""针对运行时修复的回归测试。

覆盖：
1. 上下文窗口从 providers.json 读取（provider 级 / 顶层 / 回退默认）。
2. Write/Edit 工具的工作区硬边界（越界 DENY，任何权限模式下生效）。
3. 预设 sidecar 编译失败浮出（compile_errors 收集 + 组装 notes）。
4. FTS5 中文 bigram 检索与上下文组装的 related_passages 块。
5. Workspace 绑定工具替代进程级 chdir。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import services.coomi_agent_service as coomi_agent_service
from services.coomi_agent_service import (
    DEFAULT_CONTEXT_WINDOW,
    _coomi_binding_path,
    _delete_coomi_session_binding,
    _restore_bound_coomi_session,
    _resolve_context_window,
    _storydex_check_permission,
    _write_coomi_session_binding,
    _write_paths_escape_workspace,
)
from services.retrieval_service import get_retrieval_service, reset_retrieval_cache
from services.story_project_service import get_story_project_service
from services.storydex_context_assembler_service import StorydexContextAssemblerService


class _FakeLevel:
    AUTO = "auto"
    ASK = "ask"
    DENY = "deny"


def _fake_permissions(workspace_root: Path, mode: str = "full_access") -> SimpleNamespace:
    return SimpleNamespace(
        _storydex_workspace_root=Path(workspace_root).resolve(),
        _storydex_mode=mode,
        _storydex_plan_mode=False,
    )


def _write_providers_config(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ─────────────────── 1. 上下文窗口 ───────────────────


def test_resolve_context_window_prefers_active_provider(tmp_path, monkeypatch):
    config_path = tmp_path / "providers.json"
    _write_providers_config(
        config_path,
        {
            "active": "local",
            "contextWindow": 64000,
            "providers": {"local": {"type": "generic", "model": "m", "context_window": 32000}},
        },
    )
    monkeypatch.setattr(coomi_agent_service, "STORYDEX_COOMI_CONFIG", config_path)
    assert _resolve_context_window() == 32000


def test_resolve_context_window_falls_back_to_top_level_then_default(tmp_path, monkeypatch):
    config_path = tmp_path / "providers.json"
    _write_providers_config(
        config_path,
        {"active": "local", "contextWindow": 64000, "providers": {"local": {"type": "generic", "model": "m"}}},
    )
    monkeypatch.setattr(coomi_agent_service, "STORYDEX_COOMI_CONFIG", config_path)
    assert _resolve_context_window() == 64000

    _write_providers_config(config_path, {"active": "", "providers": {}})
    assert _resolve_context_window() == DEFAULT_CONTEXT_WINDOW


def test_resolve_context_window_clamps_invalid_values(tmp_path, monkeypatch):
    config_path = tmp_path / "providers.json"
    _write_providers_config(
        config_path,
        {"active": "local", "providers": {"local": {"context_window": 1}}},
    )
    monkeypatch.setattr(coomi_agent_service, "STORYDEX_COOMI_CONFIG", config_path)
    assert _resolve_context_window() == coomi_agent_service.MIN_CONTEXT_WINDOW


# ─────────────────── 2. 写路径硬边界 ───────────────────


def test_write_tool_outside_workspace_is_denied_even_in_full_access(tmp_path):
    permissions = _fake_permissions(tmp_path, mode="full_access")
    outside = tmp_path.parent / "outside.md"
    decision = _storydex_check_permission(
        _FakeLevel, permissions, None, "Write", {"file_path": str(outside)}
    )
    assert decision == _FakeLevel.DENY


def test_write_tool_relative_escape_is_denied(tmp_path):
    permissions = _fake_permissions(tmp_path, mode="approve_for_me")
    assert _write_paths_escape_workspace(permissions, {"file_path": "../escape.md"})
    decision = _storydex_check_permission(
        _FakeLevel, permissions, None, "Edit", {"file_path": "../escape.md", "old_string": "a", "new_string": "b"}
    )
    assert decision == _FakeLevel.DENY


def test_write_tool_inside_workspace_stays_allowed(tmp_path):
    permissions = _fake_permissions(tmp_path, mode="full_access")
    decision = _storydex_check_permission(
        _FakeLevel, permissions, None, "Write", {"file_path": "chapters/001.md", "content": "x"}
    )
    assert decision == _FakeLevel.AUTO


def test_default_permission_mode_is_full_access():
    service = coomi_agent_service.StorydexCoomiAgentService()
    assert service._permission_mode == "full_access"


def test_coomi_session_binding_restores_jsonl_messages(monkeypatch, tmp_path):
    pytest.importorskip("coomi")
    from coomi.engine.session import SessionManager
    from coomi.services.session_history import append_message
    from coomi.types import Message

    history_root = tmp_path / "coomi-history"
    monkeypatch.setattr(coomi_agent_service, "STORYDEX_COOMI_SESSIONS", history_root)
    workspace = tmp_path / "story"
    workspace.mkdir()
    manager = SessionManager(history_dir=history_root, persist_history=True)
    session = manager.create_session(system_prompt="system", cwd=str(workspace), model="fake")
    user_message = Message(role="user", content="是否需要执行变量整理")
    assistant_message = Message(role="assistant", content="需要，请确认后执行。")
    session.messages.extend([user_message, assistant_message])
    append_message(session, user_message)
    append_message(session, assistant_message)
    _write_coomi_session_binding(
        workspace_root=workspace,
        storydex_session_id="storydex-session",
        session=session,
    )

    restored_manager = SessionManager(history_dir=history_root, persist_history=True)
    restored = _restore_bound_coomi_session(
        manager=restored_manager,
        workspace_root=workspace,
        storydex_session_id="storydex-session",
    )

    assert restored is not None
    assert [message.content for message in restored.messages] == ["是否需要执行变量整理", "需要，请确认后执行。"]
    assert restored_manager.get_session(restored.id) is restored


def test_coomi_session_binding_is_project_isolated(tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    assert _coomi_binding_path(first, "same-session") != _coomi_binding_path(second, "same-session")


def test_corrupt_coomi_binding_safely_falls_back(monkeypatch, tmp_path):
    pytest.importorskip("coomi")
    from coomi.engine.session import SessionManager

    history_root = tmp_path / "coomi-history"
    monkeypatch.setattr(coomi_agent_service, "STORYDEX_COOMI_SESSIONS", history_root)
    workspace = tmp_path / "story"
    workspace.mkdir()
    binding_path = _coomi_binding_path(workspace, "broken-session")
    binding_path.parent.mkdir(parents=True, exist_ok=True)
    binding_path.write_text("{broken", encoding="utf-8")

    restored = _restore_bound_coomi_session(
        manager=SessionManager(history_dir=history_root, persist_history=True),
        workspace_root=workspace,
        storydex_session_id="broken-session",
    )

    assert restored is None


def test_delete_coomi_binding_removes_bound_history(monkeypatch, tmp_path):
    pytest.importorskip("coomi")
    from coomi.engine.session import SessionManager

    history_root = tmp_path / "coomi-history"
    monkeypatch.setattr(coomi_agent_service, "STORYDEX_COOMI_SESSIONS", history_root)
    workspace = tmp_path / "story"
    workspace.mkdir()
    session = SessionManager(history_dir=history_root, persist_history=True).create_session(
        system_prompt="system",
        cwd=str(workspace),
        model="fake",
    )
    binding_path = _write_coomi_session_binding(
        workspace_root=workspace,
        storydex_session_id="delete-session",
        session=session,
    )
    history_path = Path(session.history_path)

    _delete_coomi_session_binding(
        workspace_root=workspace,
        storydex_session_id="delete-session",
        delete_history=True,
    )

    assert not binding_path.exists()
    assert not history_path.exists()


# ─────────────────── 3. 预设编译失败浮出 ───────────────────


def _prepare_broken_preset_project(tmp_path: Path, monkeypatch) -> None:
    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)
    active_dir = tmp_path / ".storydex" / "presets" / "active"
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "main.md").write_text("# 主预设\n\n保持克制的叙事节奏。\n", encoding="utf-8")
    (active_dir / "main.preset.json").write_text(
        json.dumps({"version": 2, "meta": {"name": "主预设"}, "modules": []}, ensure_ascii=False),
        encoding="utf-8",
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("boom: macro expansion failed")

    import services.preset_compiler as preset_compiler

    monkeypatch.setattr(preset_compiler, "compile_preset", _boom)


def test_preset_compile_failure_is_collected_not_swallowed(tmp_path, monkeypatch):
    _prepare_broken_preset_project(tmp_path, monkeypatch)
    service = get_story_project_service()
    errors: list[str] = []
    entries = service._collect_preset_entries(  # noqa: SLF001
        tmp_path,
        max_files=5,
        max_chars_per_file=720,
        runtime_context={"prompt": "写一段"},
        compile_errors=errors,
    )
    assert errors, "编译异常必须被收集而不是静默吞掉"
    assert "boom" in errors[0]
    # 回退路径仍提供原文预览，预设不至于完全消失。
    assert entries


def test_assembler_surfaces_preset_compile_failure_note(tmp_path, monkeypatch):
    _prepare_broken_preset_project(tmp_path, monkeypatch)
    assembly = StorydexContextAssemblerService(get_story_project_service()).assemble(
        tmp_path, prompt="继续写第一章", active_file=""
    )
    assert any(str(note).startswith("preset_compile_failed:") for note in assembly.get("notes", []))


# ─────────────────── 4. FTS5 检索与上下文组装 ───────────────────


def test_retrieval_service_supports_chinese_bigram_search(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir(parents=True)
    (chapters / "001.md").write_text("沈青抵达云桥，在藏经阁外驻足。\n", encoding="utf-8")
    (chapters / "002.md").write_text("阿离在荒村夜宿，遇见了旧识。\n", encoding="utf-8")
    reset_retrieval_cache()
    service = get_retrieval_service(tmp_path)
    assert service.build_index() == 2

    hits = service.search("云桥 藏经阁", top_k=5)
    assert hits, "中文查询必须能命中 bigram 索引"
    assert hits[0][0] == "chapters/001.md"
    assert hits[0][2], "命中结果应带可读摘录"


def test_assembler_orders_recent_segments_before_memory_blocks(tmp_path):
    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)
    chapters = tmp_path / "chapters" / "第1章"
    chapters.mkdir(parents=True, exist_ok=True)
    (chapters / "001.md").write_text("沈青抵达云桥。\n", encoding="utf-8")
    reset_retrieval_cache()

    assembly = StorydexContextAssemblerService(service).assemble(
        tmp_path, prompt="继续写沈青在云桥的剧情", active_file="chapters/第1章/001.md"
    )
    block_ids = [str(block.get("id")) for block in assembly.get("promptBlocks", [])]
    assert "recent_segments" in block_ids
    for memory_block in ("facts", "relationships", "items"):
        if memory_block in block_ids:
            assert block_ids.index("recent_segments") < block_ids.index(memory_block)
    if "active_characters" in block_ids:
        assert block_ids.index("recent_segments") < block_ids.index("active_characters")


def test_related_passage_query_excludes_prompt_instruction_words():
    terms = StorydexContextAssemblerService._related_passage_query_terms(
        "继续写下一段剧情，节奏放慢一些", ["林拾烟", "云桥"]
    )
    assert terms == ["林拾烟", "云桥"], "prompt 指令词不得进入检索查询"


def test_related_passage_query_extracts_quoted_terms():
    terms = StorydexContextAssemblerService._related_passage_query_terms(
        "写「青萝」得到《山河图》的场景", []
    )
    assert terms == ["青萝", "山河图"]


def test_related_passages_skipped_without_entities_or_quotes(tmp_path):
    service = StorydexContextAssemblerService(get_story_project_service())
    content, paths = service._render_related_passages(  # noqa: SLF001
        tmp_path, prompt="继续写下一段", active_entities=(), exclude_paths=set()
    )
    assert content == "" and paths == [], "无高置信度检索词时必须跳过被动检索块"


# ─────────────────── 7. WIKI 参考块 ───────────────────


def _write_wiki_graph(tmp_path: Path, entries: list[dict]) -> None:
    wiki_dir = tmp_path / ".storydex" / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "knowledge_graph.json").write_text(
        json.dumps({"entries": entries, "graph": {"nodes": [], "edges": []}}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_wiki_reference_block_matches_active_entities(tmp_path):
    _write_wiki_graph(
        tmp_path,
        [
            {"id": "overview:project", "title": "项目总览", "category": "overview", "summary": "总览"},
            {
                "id": "character:shenqing",
                "title": "沈青",
                "category": "characters",
                "categoryLabel": "角色",
                "summary": "云桥出身的刀客，left眼有旧伤。",
                "confidence": 0.9,
                "needsReview": False,
            },
            {
                "id": "character:ali",
                "title": "阿离",
                "category": "characters",
                "summary": "身世未明。",
                "needsReview": True,
            },
        ],
    )
    content, count = StorydexContextAssemblerService._render_wiki_reference(
        tmp_path, active_entities=["沈青", "阿离"]
    )
    assert count == 1, "needsReview 与 overview 条目必须排除"
    assert "沈青" in content and "confidence=0.90" in content
    assert "阿离" not in content
    assert "canonical facts" in content, "块头必须声明 WIKI 为参考层而非权威事实"


def test_wiki_reference_block_silent_without_graph(tmp_path):
    content, count = StorydexContextAssemblerService._render_wiki_reference(
        tmp_path, active_entities=["沈青"]
    )
    assert content == "" and count == 0, "无 WIKI 文件时被动路径必须静默跳过且不触发构建"


# ─────────────────── 8. 滚动章节摘要层 ───────────────────


def test_apply_increment_writes_rolling_chapter_summary(tmp_path):
    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)
    result = service.apply_story_generation_increment(
        tmp_path,
        {
            "segmentPath": "chapters/第1章/001.md",
            "segmentText": "沈青抵达云桥。",
            "chapterSummary": "沈青抵达云桥，在藏经阁外与守阁人对峙，埋下身世悬念。",
        },
    )
    summary_path = result.get("chapterSummaryPath")
    assert summary_path, "增量返回必须带 chapterSummaryPath"
    assert summary_path in result.get("writtenPaths", [])
    summary_file = tmp_path / summary_path
    assert summary_file.exists()
    text = summary_file.read_text(encoding="utf-8")
    assert "第1章" in text and "云桥" in text


def test_chapter_key_inference_from_segment_path():
    service = get_story_project_service()
    assert service._chapter_key_for_segment("chapters/第1章/001.md") == "第1章"  # noqa: SLF001
    assert service._chapter_key_for_segment("chapters/001.md") == "001"  # noqa: SLF001
    assert service._chapter_key_for_segment("") == ""  # noqa: SLF001


def test_rolling_summaries_block_reads_latest_chapters(tmp_path):
    import os

    rolling = tmp_path / ".storydex" / "memory" / "summaries" / "rolling"
    rolling.mkdir(parents=True)
    old = rolling / "第1章.md"
    old.write_text("# 第1章 - Rolling Summary\n\n沈青初到云桥。\n", encoding="utf-8")
    newer = rolling / "第2章.md"
    newer.write_text("# 第2章 - Rolling Summary\n\n阿离现身，旧案重提。\n", encoding="utf-8")
    older_time = old.stat().st_mtime - 100
    os.utime(old, (older_time, older_time))

    content, paths = StorydexContextAssemblerService._render_rolling_summaries(tmp_path)
    assert paths[0].endswith("第2章.md"), "必须按 mtime 优先返回最新章节摘要"
    assert "旧案重提" in content and "沈青初到云桥" in content
    assert "Rolling Summary" not in content.replace("[Rolling Chapter Summaries]", ""), "Markdown 标题行必须剥离"


def test_assembler_places_rolling_summaries_after_recent_segments(tmp_path):
    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)
    chapters = tmp_path / "chapters" / "第1章"
    chapters.mkdir(parents=True, exist_ok=True)
    (chapters / "001.md").write_text("沈青抵达云桥。\n", encoding="utf-8")
    rolling = tmp_path / ".storydex" / "memory" / "summaries" / "rolling"
    rolling.mkdir(parents=True, exist_ok=True)
    (rolling / "第1章.md").write_text("# 第1章\n\n沈青初到云桥，身世成谜。\n", encoding="utf-8")
    reset_retrieval_cache()

    assembly = StorydexContextAssemblerService(service).assemble(
        tmp_path, prompt="继续写", active_file="chapters/第1章/001.md"
    )
    block_ids = [str(block.get("id")) for block in assembly.get("promptBlocks", [])]
    assert "rolling_summaries" in block_ids
    assert block_ids.index("recent_segments") < block_ids.index("rolling_summaries")
    if "active_characters" in block_ids:
        assert block_ids.index("rolling_summaries") < block_ids.index("active_characters")


# ─────────────────── 5. Workspace 绑定工具 ───────────────────


def test_workspace_bound_write_tool_resolves_relative_paths(tmp_path):
    pytest.importorskip("coomi")
    from services.storydex_coomi_runtime_tools import StorydexWriteTool

    tool = StorydexWriteTool(workspace_root=tmp_path)
    result = tool.run({"file_path": "chapters/new.md", "content": "hello"})
    assert result.success
    assert (tmp_path / "chapters" / "new.md").read_text(encoding="utf-8") == "hello"


def test_workspace_bound_bash_tool_runs_in_workspace(tmp_path):
    pytest.importorskip("coomi")
    from services.storydex_coomi_runtime_tools import StorydexBashTool

    tool = StorydexBashTool(workspace_root=tmp_path)
    command = "cd" if os.name == "nt" else "pwd"
    result = tool.run({"command": command})
    assert result.success
    assert str(tmp_path.resolve()).lower() in result.output.strip().lower()


def test_registry_overrides_replace_default_tools(tmp_path):
    pytest.importorskip("coomi")
    from services.coomi_agent_service import _create_storydex_tool_registry
    from services.storydex_coomi_runtime_tools import StorydexBashTool, StorydexWriteTool

    registry = _create_storydex_tool_registry(tmp_path)
    assert isinstance(registry.get("Bash"), StorydexBashTool)
    assert isinstance(registry.get("Write"), StorydexWriteTool)


# ─────────────────── 6. Agent 主动检索工具 ───────────────────


def test_project_search_tool_returns_ranked_hits(tmp_path):
    pytest.importorskip("coomi")
    from services.storydex_agent_tools import StorydexProjectSearchTool

    chapters = tmp_path / "chapters"
    chapters.mkdir(parents=True)
    (chapters / "001.md").write_text("沈青抵达云桥，在藏经阁外驻足良久。\n", encoding="utf-8")
    (chapters / "002.md").write_text("阿离在荒村夜宿，遇见了旧识。\n", encoding="utf-8")
    reset_retrieval_cache()

    tool = StorydexProjectSearchTool(workspace_root=tmp_path)
    result = tool.run({"query": "云桥 藏经阁"})
    assert result.success
    payload = json.loads(result.output)
    assert payload["ok"] and payload["resultCount"] >= 1
    assert payload["results"][0]["path"] == "chapters/001.md"
    assert payload["results"][0]["snippet"], "命中必须带可读摘录"


def test_project_search_tool_requires_query(tmp_path):
    pytest.importorskip("coomi")
    from services.storydex_agent_tools import StorydexProjectSearchTool

    tool = StorydexProjectSearchTool(workspace_root=tmp_path)
    result = tool.run({"query": "  "})
    assert not result.success


def test_wiki_query_tool_requires_selector(tmp_path):
    pytest.importorskip("coomi")
    from services.storydex_agent_tools import StorydexWikiQueryTool

    tool = StorydexWikiQueryTool(workspace_root=tmp_path)
    result = tool.run({})
    assert not result.success


def test_wiki_query_tool_returns_compact_entries_with_caveat(tmp_path):
    pytest.importorskip("coomi")
    from services.storydex_agent_tools import StorydexWikiQueryTool

    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)
    chapters = tmp_path / "chapters" / "第1章"
    chapters.mkdir(parents=True, exist_ok=True)
    (chapters / "001.md").write_text("沈青抵达云桥，与阿离重逢。\n", encoding="utf-8")

    tool = StorydexWikiQueryTool(workspace_root=tmp_path)
    result = tool.run({"query": "沈青"})
    assert result.success
    payload = json.loads(result.output)
    assert payload["ok"]
    assert "caveat" in payload and "inference" in payload["caveat"]
    for entry in payload.get("entries", []):
        assert set(entry) <= {
            "id", "title", "category", "summary", "details", "confidence", "needsReview", "sourcePaths",
        }


def test_registry_includes_retrieval_tools(tmp_path):
    pytest.importorskip("coomi")
    from services.coomi_agent_service import _create_storydex_tool_registry
    from services.storydex_agent_tools import StorydexProjectSearchTool, StorydexWikiQueryTool

    registry = _create_storydex_tool_registry(tmp_path)
    assert isinstance(registry.get("StorydexProjectSearch"), StorydexProjectSearchTool)
    assert isinstance(registry.get("StorydexWikiQuery"), StorydexWikiQueryTool)
