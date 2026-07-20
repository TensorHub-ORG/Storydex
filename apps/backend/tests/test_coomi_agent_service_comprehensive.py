from __future__ import annotations

import asyncio
import json
import os
import sys
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

import services.coomi_agent_service as coomi
from services.llm_replay import get_llm_metrics, reset_llm_metrics


@contextmanager
def _noop_home():
    yield


def _event(name: str, **attrs):
    return type(name, (), attrs)()


def _install_provider(monkeypatch, provider) -> None:
    services = types.ModuleType("coomi.services")
    services.get_llm_provider = lambda: provider
    monkeypatch.setitem(sys.modules, "coomi", types.ModuleType("coomi"))
    monkeypatch.setitem(sys.modules, "coomi.services", services)


def test_binding_validation_write_restore_and_delete(monkeypatch, tmp_path):
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_SESSIONS", tmp_path / "sessions")
    history = coomi.STORYDEX_COOMI_SESSIONS / "session.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
    session = types.SimpleNamespace(id="coomi-1", history_path=history)
    path = coomi._write_coomi_session_binding(
        workspace_root=tmp_path / "project", storydex_session_id="story-1", session=session
    )
    assert path.is_file()
    payload = coomi._read_coomi_session_binding(workspace_root=tmp_path / "project", storydex_session_id="story-1")
    assert payload["coomiSessionId"] == "coomi-1"

    path.write_text("[]", encoding="utf-8")
    assert coomi._read_coomi_session_binding(workspace_root=tmp_path / "project", storydex_session_id="story-1") == {}
    path.write_text("{broken", encoding="utf-8")
    assert coomi._read_coomi_session_binding(workspace_root=tmp_path / "project", storydex_session_id="story-1") == {}
    path.write_text(json.dumps({"workspaceRoot": "wrong", "storydexSessionId": "story-1"}), encoding="utf-8")
    assert coomi._read_coomi_session_binding(workspace_root=tmp_path / "project", storydex_session_id="story-1") == {}
    path.write_text(
        json.dumps({"workspaceRoot": str((tmp_path / "project").resolve()), "storydexSessionId": "wrong"}),
        encoding="utf-8",
    )
    assert coomi._read_coomi_session_binding(workspace_root=tmp_path / "project", storydex_session_id="story-1") == {}

    coomi._write_coomi_session_binding(
        workspace_root=tmp_path / "project", storydex_session_id="story-1", session=session
    )
    loaded = types.SimpleNamespace(id="coomi-1")
    history_module = types.ModuleType("coomi.services.session_history")
    history_module.load_session_from_jsonl = lambda value: loaded
    monkeypatch.setitem(sys.modules, "coomi.services.session_history", history_module)
    manager = types.SimpleNamespace(register_session=lambda value: setattr(manager, "registered", value))
    assert coomi._restore_bound_coomi_session(
        manager=manager, workspace_root=tmp_path / "project", storydex_session_id="story-1"
    ) is loaded
    assert manager.registered is loaded

    history_module.load_session_from_jsonl = lambda value: types.SimpleNamespace(id="wrong")
    assert coomi._restore_bound_coomi_session(
        manager=manager, workspace_root=tmp_path / "project", storydex_session_id="story-1"
    ) is None
    history.unlink()
    assert coomi._restore_bound_coomi_session(
        manager=manager, workspace_root=tmp_path / "project", storydex_session_id="story-1"
    ) is None

    history.write_text("history", encoding="utf-8")
    coomi._write_coomi_session_binding(
        workspace_root=tmp_path / "project", storydex_session_id="story-1", session=session
    )
    coomi._delete_coomi_session_binding(
        workspace_root=tmp_path / "project", storydex_session_id="story-1", delete_history=True
    )
    assert not history.exists()
    assert not path.exists()
    coomi._delete_coomi_session_binding(
        workspace_root=tmp_path / "project", storydex_session_id="story-1", delete_history=False
    )


def test_task_plan_and_commit_message_provider_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(coomi, "_storydex_coomi_home", _noop_home)
    monkeypatch.setattr(coomi.StorydexCoomiAgentService, "_ensure_coomi_installed", staticmethod(lambda: None))

    class Provider:
        model = "fake"

        async def chat(self, messages, options):
            if "commit" in messages[0]["content"].lower():
                return types.SimpleNamespace(content="Commit message: update chapter")
            return types.SimpleNamespace(content='{"tasks":[{"title":"Read project","description":"Inspect files"}]}')

    _install_provider(monkeypatch, Provider())
    service = coomi.StorydexCoomiAgentService()
    reset_llm_metrics("t")
    reset_llm_metrics("commit-trace")
    tasks = asyncio.run(
        service.create_task_plan(
            prompt="work", trace_id="t", session_id="s", workspace_root=tmp_path, active_file="chapter.md"
        )
    )
    assert tasks and tasks[0]["title"] == "Read project"
    message = asyncio.run(
        service.generate_commit_message(
            workspace_root=tmp_path,
            changed_files=["chapter.md"],
            diff_summary="+ text",
            prompt="continue",
            trace_id="commit-trace",
        )
    )
    assert message == "update chapter"
    assert get_llm_metrics("t")["llmCalls"][0]["purpose"] == "plan"
    assert get_llm_metrics("commit-trace")["llmCalls"][0]["purpose"] == "commit"

    class BrokenProvider:
        async def chat(self, messages, options):
            raise RuntimeError("offline")

    _install_provider(monkeypatch, BrokenProvider())
    assert asyncio.run(
        service.create_task_plan(prompt="work", trace_id="t", session_id="s", workspace_root=tmp_path)
    ) == []
    with pytest.raises(coomi.StorydexCoomiUnavailable):
        asyncio.run(service.generate_commit_message(workspace_root=tmp_path, changed_files=[]))

    class EmptyProvider:
        async def chat(self, messages, options):
            return types.SimpleNamespace(content="   ")

    _install_provider(monkeypatch, EmptyProvider())
    with pytest.raises(coomi.StorydexCoomiUnavailable):
        asyncio.run(service.generate_commit_message(workspace_root=tmp_path, changed_files=[]))


def test_stream_events_success_cancel_error_and_plan_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr(coomi, "_storydex_coomi_home", _noop_home)
    monkeypatch.setattr(coomi.StorydexCoomiAgentService, "_ensure_coomi_installed", staticmethod(lambda: None))
    monkeypatch.setattr(coomi.StorydexCoomiAgentService, "get_status", lambda self, **kwargs: {"model": "fake"})

    class Session:
        token_usage = types.SimpleNamespace(total_tokens=12, input_tokens=4)
        last_prompt_tokens = 4

    class Agent:
        context_window_size = 100
        cancel_token = types.SimpleNamespace(cancel=lambda: None)

        async def run_stream(self, session, prompt):
            yield _event("TextChunk", content="hello")
            yield _event("UsageUpdate", usage={"prompt_tokens": 5, "total_tokens": 7})
            yield _event("CompressionEvent", before=8, after=3)

    service = coomi.StorydexCoomiAgentService()

    async def runtime(**kwargs):
        return Agent(), Session()

    monkeypatch.setattr(service, "_get_or_create_runtime", runtime)

    async def collect(prompt="hello", token=None):
        return [item async for item in service.stream_events(
            prompt=prompt,
            trace_id="t",
            session_id="s",
            workspace_root=tmp_path,
            cancellation_token=token,
        )]

    events = asyncio.run(collect())
    assert [name for name, _ in events] == ["AgentStarted", "TextChunk", "UsageUpdate", "CompressionEvent", "AgentCompleted"]
    assert events[2][1]["usedTokens"] == 5
    assert events[3][1]["compressionStatus"] == "compressed"

    class CancelAgent(Agent):
        async def run_stream(self, session, prompt):
            yield _event("TextChunk", content="late")

    async def cancel_runtime(**kwargs):
        return CancelAgent(), Session()

    monkeypatch.setattr(service, "_get_or_create_runtime", cancel_runtime)
    cancelled = asyncio.run(collect(token=types.SimpleNamespace(is_cancelled=lambda: True)))
    assert cancelled[-1][0] == "AgentCancelled"

    class ErrorAgent(Agent):
        async def run_stream(self, session, prompt):
            if False:
                yield None
            raise RuntimeError("provider broke")

    async def error_runtime(**kwargs):
        return ErrorAgent(), Session()

    monkeypatch.setattr(service, "_get_or_create_runtime", error_runtime)
    failed = asyncio.run(collect())
    assert failed[-1][0] == "AgentError"

    async def fake_plan(**kwargs):
        yield "TextChunk", {"content": "plan"}

    async def fake_loop(**kwargs):
        yield "TextChunk", {"content": "loop"}

    monkeypatch.setattr(service, "_stream_plan_command", fake_plan)
    monkeypatch.setattr(service, "_stream_loop_command", fake_loop)
    assert asyncio.run(collect("/plan")) == [("TextChunk", {"content": "plan"})]
    assert asyncio.run(collect("/loop task")) == [("TextChunk", {"content": "loop"})]


def test_plan_command_and_empty_loop(monkeypatch, tmp_path):
    service = coomi.StorydexCoomiAgentService()
    agent = types.SimpleNamespace(plan_mode=False, set_plan_mode=lambda value: setattr(agent, "plan_mode", value))
    session = types.SimpleNamespace(system_prompt="")

    async def runtime(**kwargs):
        return agent, session

    async def prompt(**kwargs):
        return "system"

    monkeypatch.setattr(service, "_get_or_create_runtime", runtime)
    monkeypatch.setattr(coomi, "_build_coomi_system_prompt", prompt)
    monkeypatch.setattr(coomi, "_sync_coomi_runtime_workspace", lambda **kwargs: None)
    monkeypatch.setattr(service, "get_status", lambda **kwargs: {})

    async def collect_plan(command):
        return [item async for item in service._stream_plan_command(
            command=command, prompt=f"/{command}", trace_id="t", session_id="s", workspace_root=tmp_path
        )]

    enabled = asyncio.run(collect_plan("plan"))
    disabled = asyncio.run(collect_plan("exit_plan"))
    assert enabled[-1][1]["planMode"] is True
    assert disabled[-1][1]["planMode"] is False

    async def collect_loop():
        return [item async for item in service._stream_loop_command(
            command_body="", prompt="/loop", trace_id="t", session_id="s", workspace_root=tmp_path,
            cancellation_token=None, started=0.0
        )]

    loop_events = asyncio.run(collect_loop())
    assert loop_events[1][0] == "TextChunk"
    assert loop_events[-1][0] == "AgentCompleted"


def test_approval_context_and_service_resolution():
    async def exercise():
        service = coomi.StorydexCoomiAgentService()
        queue = asyncio.Queue()
        context = coomi._StorydexApprovalContext(service=service, event_queue=queue, trace_id="t", session_id="s")
        task = asyncio.create_task(context._handle_ask_questions([
            {"header": "Permission", "question": "Allow?", "options": [{"label": "Allow", "value": "allow"}, {"label": "Deny", "value": "deny"}]},
            {"header": "Name", "question": "Value?", "options": []},
        ]))
        first = await queue.get()
        second = await queue.get()
        first_id = first[1]["approvalId"]
        second_id = second[1]["approvalId"]
        assert service.resolve_approval("missing", "allow")["accepted"] is False
        assert service.resolve_approval(first_id, "allow")["accepted"] is True
        assert service.resolve_approval(second_id, "answer", response={"other_text": "Alice"})["accepted"] is True
        answers = await task
        assert answers[0]["option"] == "allow"
        assert answers[1]["other_text"] == "Alice"

        cancel_context = coomi._StorydexApprovalContext(service=service, event_queue=queue, trace_id="t2", session_id="s")
        cancel_task = asyncio.create_task(cancel_context._handle_ask_questions([{"question": "Cancel", "options": []}]))
        pending = await queue.get()
        service.resolve_approval(pending[1]["approvalId"], "cancel")
        assert await cancel_task == {"__cancelled__": True}

    asyncio.run(exercise())


def test_event_translator_all_events_and_tool_ids():
    translator = coomi._CoomiEventTranslator(session_id="s")
    cases = [
        (_event("TextChunk", content="x"), "TextChunk"),
        (_event("ReasoningChunk", content="r"), "ReasoningChunk"),
        (_event("ToolStart", tool_name="Read", tool_call_id="", arguments={"path": "a"}), "ToolStart"),
        (_event("ToolRunning", tool_name="Read", tool_call_id=""), "ToolRunning"),
        (_event("ToolDone", tool_name="Read", tool_call_id="", elapsed=0.2, is_error=False, result_preview="ok"), "ToolDone"),
        (_event("UsageUpdate", usage={"total": 1}), "UsageUpdate"),
        (_event("CompressionEvent", before=10, after=4), "CompressionEvent"),
        (_event("LoopProgress", current_step=2, total_steps=4, step_description="go"), "TurnPhase"),
        (_event("AgentCancelled"), "AgentCancelled"),
        (_event("AgentError", message="bad", is_fatal=True), "AgentError"),
    ]
    for event, expected in cases:
        translated = translator.translate(event)
        assert translated[0] == expected
    assert translator.translate(_event("Unknown")) is None
    explicit = translator.translate(_event("ToolStart", tool_name="Write", tool_call_id="id", arguments={}))
    assert explicit[1]["tool_call_id"] == "id"
    done = translator.translate(_event("ToolDone", tool_name="Write", tool_call_id="id", elapsed=0, is_error=False))
    assert done[1]["tool_call_id"] == "id"
    orphan = translator.translate(_event("ToolDone", tool_name="None", tool_call_id="", elapsed=0, is_error=False))
    assert orphan[1]["tool_call_id"].startswith("coomi-")


def test_event_translator_deduplicates_announced_and_executing_tool_starts():
    translator = coomi._CoomiEventTranslator(session_id="s")
    events = [
        _event("ToolStart", tool_name="StorydexProjectSearch", arguments={}),
        _event("ToolStart", tool_name="StorydexProjectSearch", arguments={}),
        _event("UsageUpdate", usage={"total_tokens": 10}),
        _event("ToolStart", tool_name="StorydexProjectSearch", arguments={"query": "first"}),
        _event("ToolStart", tool_name="StorydexProjectSearch", arguments={"query": "second"}),
        _event("ToolRunning", tool_name="StorydexProjectSearch"),
        _event("ToolRunning", tool_name="StorydexProjectSearch"),
        _event("ToolDone", tool_name="StorydexProjectSearch", result_preview="first"),
        _event("ToolDone", tool_name="StorydexProjectSearch", result_preview="second"),
    ]

    translated = [item for event in events if (item := translator.translate(event)) is not None]
    starts = [payload for name, payload in translated if name == "ToolStart"]
    running = [payload for name, payload in translated if name == "ToolRunning"]
    done = [payload for name, payload in translated if name == "ToolDone"]

    start_ids = [payload["tool_call_id"] for payload in starts]
    assert len(start_ids) == 2
    assert len(set(start_ids)) == 2
    assert [payload["tool_call_id"] for payload in running] == start_ids
    assert [payload["tool_call_id"] for payload in done] == start_ids
    assert translator.active_by_tool == {}


def test_config_status_models_sessions_and_permission_modes(monkeypatch, tmp_path):
    config = tmp_path / "providers.json"
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_CONFIG", config)
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_HOME", tmp_path)
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(coomi, "_storydex_coomi_home", _noop_home)
    monkeypatch.setattr(coomi.StorydexCoomiAgentService, "_ensure_coomi_installed", staticmethod(lambda: None))
    service = coomi.StorydexCoomiAgentService()

    coomi._ensure_storydex_coomi_config()
    assert service.read_config()["parsed"]["providers"] == {}
    with pytest.raises(ValueError):
        service.write_config("")
    with pytest.raises(ValueError):
        service.write_config("[]")
    updated = service.write_config('{"active":"fake","providers":{}}')
    assert updated["parsed"]["active"] == "fake"

    class Response:
        status_code = 200

        def json(self):
            return {"data": [{"id": "m1"}, {"name": "m2"}, "m1", None]}

    result = service.list_models(base_url="https://example.test/v1/chat/completions", api_key="key", http_get=lambda *a, **k: Response())
    assert result["models"] == ["m1", "m2"]
    with pytest.raises(ValueError):
        service.list_models(base_url="https://example.test/v1", api_key="")
    with pytest.raises(ValueError):
        service.list_models(base_url="https://example.test/v1", api_key="key", http_get=lambda *a, **k: (_ for _ in ()).throw(OSError()))
    bad_status = types.SimpleNamespace(status_code=500, json=lambda: {})
    with pytest.raises(ValueError):
        service.list_models(base_url="https://example.test/v1", api_key="key", http_get=lambda *a, **k: bad_status)
    bad_json = types.SimpleNamespace(status_code=200, json=lambda: (_ for _ in ()).throw(ValueError()))
    with pytest.raises(ValueError):
        service.list_models(base_url="https://example.test/v1", api_key="key", http_get=lambda *a, **k: bad_json)

    root1, root2 = tmp_path / "one", tmp_path / "two"
    key1 = service._runtime_key(session_id="s", workspace_root=root1)
    key2 = service._runtime_key(session_id="s", workspace_root=root2)
    for cache in (service._sessions, service._agents, service._permissions):
        cache[key1] = object()
        cache[key2] = object()
    service.clear_session("s", workspace_root=root1)
    assert key1 not in service._sessions and key2 in service._sessions
    service.clear_session("s")
    assert not service._sessions and not service._agents and not service._permissions

    security = types.ModuleType("coomi.security")
    security.PermissionMode = types.SimpleNamespace(ASK_APPROVAL="ask", APPROVE_FOR_ME="approve", FULL_ACCESS="full")
    monkeypatch.setitem(sys.modules, "coomi.security", security)
    permission = types.SimpleNamespace(set_mode=lambda mode: setattr(permission, "mode", mode))
    service._permissions["p"] = permission
    assert service.set_permission_mode("ask")["permissionMode"] == "ask_approval"
    assert permission.mode == "ask"
    assert service.cycle_permission_mode()["permissionMode"] == "approve_for_me"


def test_url_model_json_commit_and_planner_helpers(tmp_path):
    assert coomi._coomi_api_base_url("") == ""
    assert coomi._coomi_api_base_url("custom") == "custom"
    assert coomi._coomi_api_base_url("https://x.test/v1/chat/completions?x=1") == "https://x.test/v1"
    assert coomi._coomi_models_endpoint("https://x.test/v1/responses") == "https://x.test/v1/models"
    assert coomi._coomi_models_endpoint("https://x.test/v1/models") == "https://x.test/v1/models"
    with pytest.raises(ValueError):
        coomi._coomi_models_endpoint("")
    with pytest.raises(ValueError):
        coomi._coomi_models_endpoint("bad")

    assert coomi._extract_model_ids({"models": [{"model": "x"}, {"id": "x"}, {"name": "y"}]}) == ["x", "y"]
    assert coomi._extract_model_ids({"model": "solo"}) == ["solo"]
    assert coomi._extract_model_ids("bad") == []
    assert coomi._parse_commit_message_content("\n- `hello`\n") == "hello"
    assert coomi._parse_commit_message_content("") == ""
    assert len(coomi._commit_message_messages(changed_files=[str(i) for i in range(100)], diff_summary="", prompt="")) == 2

    assert coomi._extract_json_payload('```json\n{"tasks":[]}\n```') == {"tasks": []}
    assert coomi._extract_json_payload('prefix [{"title":"x"}] suffix') == {"title": "x"}
    assert coomi._extract_json_payload("broken") is None
    tasks = coomi._normalize_planner_tasks(
        [None, {"title": "Use route story_generation", "description": "generic"}, {"title": "Inspect", "description": "Details", "status": "done"}],
        trace_id="t",
    )
    assert tasks[-1]["title"] == "Inspect"
    assert tasks[-1]["status"] == "pending"
    assert coomi._normalize_planner_tasks({}, trace_id="t") == []
    assert coomi._parse_task_plan_content('{"tasks":[{"title":"One"}]}', trace_id="t")
    assert coomi._parse_task_plan_content("bad", trace_id="t") == []


def test_permission_helpers_cover_modes_paths_and_shell(tmp_path):
    levels = types.SimpleNamespace(AUTO="auto", DENY="deny", ASK="ask")
    permissions = types.SimpleNamespace(
        _storydex_workspace_root=tmp_path,
        _storydex_mode="full_access",
        _storydex_plan_mode=False,
        _bash_safety=types.SimpleNamespace(check_command=lambda command: types.SimpleNamespace(risk_level="low")),
    )
    assert coomi._storydex_check_permission(levels, permissions, None, "AskUserQuestion", {}) == "auto"
    assert coomi._storydex_check_permission(levels, permissions, None, "Write", {"path": "../escape"}) == "deny"
    assert coomi._storydex_check_permission(levels, permissions, None, "Read", {"path": "file.md"}) == "auto"
    permissions._storydex_mode = "ask_approval"
    assert coomi._storydex_check_permission(levels, permissions, None, "Read", {}) == "ask"
    permissions._storydex_mode = "approve_for_me"
    assert coomi._storydex_check_permission(levels, permissions, None, "Read", {"path": ".ssh/key"}) == "ask"
    assert coomi._storydex_check_permission(levels, permissions, None, "Read", {"path": "chapter.md"}) == "auto"
    assert coomi._storydex_check_permission(levels, permissions, None, "Write", {"path": "chapter.md"}) == "auto"
    assert coomi._storydex_check_permission(levels, permissions, None, "Bash", {"command": "echo hi"}) == "auto"
    assert coomi._storydex_check_permission(levels, permissions, None, "Bash", {"command": "cat .env"}) == "ask"
    assert coomi._storydex_check_permission(levels, permissions, None, "Unknown", {}) == "ask"

    permissions._storydex_plan_mode = True
    assert coomi._storydex_check_permission(levels, permissions, None, "Read", {"path": "chapter.md"}) == "auto"
    assert coomi._storydex_check_permission(levels, permissions, None, "Read", {"path": ".env"}) == "ask"
    assert coomi._storydex_check_permission(levels, permissions, None, "Write", {"path": ".storydex/.agent/plans/a.md"}) == "auto"
    assert coomi._storydex_check_permission(levels, permissions, None, "Write", {"path": "chapter.md"}) == "deny"
    assert coomi._storydex_check_permission(levels, permissions, None, "Bash", {}) == "deny"

    assert coomi._argument_paths({"path": " a ", "query": "q", "file": 2}) == ["a", "q"]
    assert coomi._is_sensitive_path(permissions, "") is False
    assert coomi._is_sensitive_path(permissions, ".aws/credentials") is True
    assert coomi._is_sensitive_path(permissions, "API-KEY.txt") is True
    assert coomi._command_mentions_sensitive_path("") is False
    assert coomi._command_mentions_sensitive_path("type .env") is True
    assert coomi._resolve_permission_path(tmp_path, "") is None
    assert coomi._resolve_permission_path(tmp_path, "../outside") is None
    assert coomi._resolve_permission_path(tmp_path, "inside.md") == (tmp_path / "inside.md").resolve()


def test_render_contract_context_templates_and_snapshots(monkeypatch, tmp_path):
    contract = {
        "status": "waiting_for_user",
        "intentFrame": {"primary": "story_generation", "confidence": "high", "assetTargets": ["chapters/"], "matchedSkills": ["write"]},
        "executionPolicy": {},
        "turnPlan": {
            "fragmentCount": 2,
            "fragmentWordCount": 800,
            "requiresChapterTemplateSelection": True,
            "invalidChapterTemplate": "bad",
            "availableChapterTemplates": [{"id": "serial", "name": "Serial"}, {"relativePath": "chapters"}],
            "nextSegmentPath": "chapters/1.md",
        },
        "contextPolicy": {"machineVariableOperations": "optional"},
        "skillRegistry": {"registryPath": "registry.json", "skills": [{"id": "a", "file": "a.md"}], "skillCount": 1},
        "contextAssembly": {
            "budget": {"blockCount": 1, "totalChars": 12},
            "sources": [{"kind": "recent", "count": 1}],
            "promptBlocks": [None, {"title": "Recent", "content": "text", "sourcePaths": ["a.md"]}, {"content": ""}],
        },
        "updatePolicy": {"autoUpdateVariables": True, "autoUpdateWiki": False},
    }
    rendered = coomi._render_turn_contract(contract)
    assert "waiting_for_user" in rendered
    assert "Serial (serial)" in rendered
    assert "Storydex assembled context blocks" in rendered
    contract["contextAssembly"]["contextTrace"] = {
        "sources": [{"kind": "recent", "chars": 4}],
        "duplicates": [],
        "llmCalls": [],
        "totals": {"estContextTokens": 1},
    }
    assert coomi._render_turn_contract(contract) == rendered, "Trace 元数据不得改变模型可见 Prompt"
    assert coomi._render_turn_contract(None) == ""

    selected = dict(contract)
    selected["turnPlan"] = {
        "selectedChapterTemplate": "serial",
        "selectedChapterTemplateDetail": {
            "id": "serial", "name": "Serial", "chapterMode": "directory",
            "chapterNamePattern": "Chapter {n}", "segmentNaming": "numeric",
            "initialChapterDirectory": "001", "initialChapterFirstSegment": "001.md",
        },
    }
    assert "selectedTemplateRules" in coomi._render_turn_contract(selected)
    assert coomi._chapter_template_labels("bad") == []
    assert coomi._chapter_template_detail_label({}, "fallback") == "fallback"
    assert coomi._skill_registry_summary({}) == ""
    assert coomi._context_assembly_summary({}) == ""
    assert coomi._render_context_assembly_blocks({}) == ""
    assert coomi._render_story_generation_options({"fragmentCount": 99, "fragmentWordCount": "bad"})
    assert coomi._bounded_int("bad", default=3, minimum=1, maximum=4) == 3

    monkeypatch.setattr(coomi, "_resolve_context_window", lambda: 100)
    session = types.SimpleNamespace(last_prompt_tokens=20, token_usage=types.SimpleNamespace(total_tokens=30, input_tokens=10))
    agent = types.SimpleNamespace(context_window_size=100)
    snapshot = coomi._context_snapshot(session=session, agent=agent)
    assert snapshot["usageRatio"] == 0.2
    payload = {"usage": {"promptTokens": 25, "totalTokens": 40}}
    coomi._attach_context_snapshot(payload, session=session, agent=agent, compressed=True)
    assert payload["usedTokens"] == 25
    assert payload["lastTotalTokens"] == 40
    assert payload["compressionStatus"] == "compressed"


def test_misc_permission_approval_path_model_and_environment_helpers(monkeypatch, tmp_path):
    assert coomi._parse_slash_command("hello") == {"name": "", "body": ""}
    assert coomi._parse_slash_command("/LOOP task") == {"name": "loop", "body": "task"}
    assert coomi._normalize_permission_mode("ask") == "ask_approval"
    assert coomi._normalize_permission_mode("unknown") == "full_access"
    assert coomi._permission_label("approve") == "自动批准"
    assert coomi._approval_answer("cancel") == {"__cancelled__": True}
    assert coomi._approval_answer("deny")["option"] == "deny"
    assert coomi._approval_answer("custom")["label"] == "custom"
    assert coomi._is_permission_question({"options": "bad"}) is False
    assert coomi._is_permission_question({"options": [{"value": "allow"}, {"option": "deny"}]}) is True
    assert len(coomi._approval_options(None, is_permission=True)) == 2
    assert len(coomi._approval_options(None, is_permission=False)) == 1
    assert coomi._approval_options([None, {}, {"label": "Yes", "is_recommended": True}])[0]["value"] == "Yes"

    assert coomi._safe_loop_spec_path(tmp_path, "") is None
    assert coomi._safe_loop_spec_path(tmp_path, "../outside.md") is None
    spec_path = tmp_path / "spec.md"
    spec_path.write_text("spec", encoding="utf-8")
    assert coomi._safe_loop_spec_path(tmp_path, "spec.md") == spec_path.resolve()

    assert coomi._model_display(types.SimpleNamespace(get_model_display_name=lambda: "Display")) == "Display"
    assert coomi._model_display(types.SimpleNamespace(get_model_display_name=lambda: (_ for _ in ()).throw(RuntimeError()), model="fallback")) == "fallback"
    assert coomi._is_cancelled(types.SimpleNamespace(is_cancelled=lambda: True)) is True
    assert coomi._is_cancelled(types.SimpleNamespace(is_cancelled=lambda: (_ for _ in ()).throw(RuntimeError()))) is False
    assert coomi._is_cancelled(None) is False
    assert coomi._agent_started(session_id="s", prompt="p", status={"model": "m", "providerId": "x"}, mode="coomi")[1]["llmModel"] == "m"

    monkeypatch.setattr(coomi, "STORYDEX_COOMI_HOME", tmp_path)
    monkeypatch.setattr(coomi, "_ensure_storydex_coomi_config", lambda: None)
    monkeypatch.setattr(coomi, "_install_coomi_endpoint_compat", lambda: None)
    monkeypatch.setattr(coomi, "_install_coomi_home_redirects", lambda: False)
    old_home = os.environ.pop("HOME", None)
    old_profile = os.environ.get("USERPROFILE")
    try:
        with coomi._storydex_coomi_home():
            assert os.environ["HOME"] == str(tmp_path)
            assert os.environ["USERPROFILE"] == str(tmp_path)
        assert "HOME" not in os.environ
        assert os.environ.get("USERPROFILE") == old_profile
    finally:
        if old_home is not None:
            os.environ["HOME"] = old_home
        elif "HOME" in os.environ:
            os.environ.pop("HOME")


def test_runtime_creation_new_cached_and_restored(monkeypatch, tmp_path):
    service = coomi.StorydexCoomiAgentService()
    monkeypatch.setattr(coomi, "_build_coomi_system_prompt", lambda **kwargs: asyncio.sleep(0, result="system"))
    monkeypatch.setattr(coomi, "_resolve_context_window", lambda: 4096)
    monkeypatch.setattr(coomi, "_create_storydex_tool_registry", lambda root, policy=None: "registry")
    monkeypatch.setattr(coomi, "_write_coomi_session_binding", lambda **kwargs: None)
    monkeypatch.setattr(coomi, "_sync_coomi_runtime_workspace", lambda **kwargs: setattr(kwargs["session"], "synced", True))

    class Provider:
        model = "fake-model"

    services = types.ModuleType("coomi.services")
    services.get_llm_provider = lambda: Provider()
    monkeypatch.setitem(sys.modules, "coomi.services", services)

    class SessionManager:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def create_session(self, **kwargs):
            return types.SimpleNamespace(**kwargs)

    session_module = types.ModuleType("coomi.engine.session")
    session_module.SessionManager = SessionManager
    monkeypatch.setitem(sys.modules, "coomi.engine.session", session_module)

    class AgentLoop:
        def __init__(self, provider, registry, **kwargs):
            self.provider = provider
            self.registry = registry
            self.plan_mode = False
            for key, value in kwargs.items():
                setattr(self, key, value)

    loop_module = types.ModuleType("coomi.engine.loop")
    loop_module.AgentLoop = AgentLoop
    monkeypatch.setitem(sys.modules, "coomi.engine.loop", loop_module)

    security = types.ModuleType("coomi.security")
    security.PermissionLevel = types.SimpleNamespace()
    security.PermissionMode = types.SimpleNamespace()
    security.PermissionSystem = object
    monkeypatch.setitem(sys.modules, "coomi.security", security)

    permission = types.SimpleNamespace()
    monkeypatch.setattr(service, "_create_permission_system", lambda *args: permission)
    monkeypatch.setattr(coomi, "_restore_bound_coomi_session", lambda **kwargs: None)

    async def create():
        return await service._get_or_create_runtime(
            session_id="s", workspace_root=tmp_path, prompt="hello", app_context="ctx"
        )

    agent, session = asyncio.run(create())
    assert session.model == "fake-model"
    assert session.synced is True
    assert agent.context_window_size == 4096
    assert service._permissions[service._runtime_key(session_id="s", workspace_root=tmp_path)] is permission

    agent.plan_mode = True
    cached_agent, cached_session = asyncio.run(create())
    assert cached_agent is agent and cached_session is session
    assert session.system_prompt == "system"

    service.clear_session("s", workspace_root=tmp_path)
    restored = types.SimpleNamespace(id="restored")
    monkeypatch.setattr(coomi, "_restore_bound_coomi_session", lambda **kwargs: restored)
    restored_agent, restored_session = asyncio.run(create())
    assert restored_session is restored
    assert restored.current_model == "fake-model"


def test_nonempty_loop_success_cancel_and_error(monkeypatch, tmp_path):
    service = coomi.StorydexCoomiAgentService()
    monkeypatch.setattr(service, "get_status", lambda **kwargs: {})
    monkeypatch.setattr(coomi, "_create_storydex_tool_registry", lambda root, policy=None: "registry")
    monkeypatch.setattr(coomi, "_resolve_context_window", lambda: 100)
    monkeypatch.setattr(coomi, "_resolve_loop_spec", lambda root, body: (None, "spec"))
    monkeypatch.setattr(service, "_create_permission_system", lambda *args: types.SimpleNamespace())
    monkeypatch.setattr(coomi, "_sync_storydex_permission_context", lambda *args, **kwargs: None)

    provider = types.SimpleNamespace(model="fake", get_model_display_name=lambda: "Fake")
    services = types.ModuleType("coomi.services")
    services.get_llm_provider = lambda: provider
    monkeypatch.setitem(sys.modules, "coomi.services", services)
    memory = types.ModuleType("coomi.services.memory")
    memory.MemoryManager = lambda **kwargs: types.SimpleNamespace(**kwargs)
    memory.MemoryRecall = lambda provider, manager: types.SimpleNamespace(provider=provider, manager=manager)
    monkeypatch.setitem(sys.modules, "coomi.services.memory", memory)
    security = types.ModuleType("coomi.security")
    security.PermissionLevel = security.PermissionMode = security.PermissionSystem = object
    monkeypatch.setitem(sys.modules, "coomi.security", security)

    mode = {"value": "success"}

    class LoopRunner:
        def __init__(self, *args, **kwargs):
            self.cancel_token = types.SimpleNamespace(cancel=lambda: setattr(self, "cancelled", True))

        async def start_loop(self, **kwargs):
            if mode["value"] == "error":
                if False:
                    yield None
                raise RuntimeError("loop failed")
            yield _event("LoopProgress", current_step=1, total_steps=2, step_description="work")

    runner_module = types.ModuleType("coomi.engine.loop_runner")
    runner_module.LoopRunner = LoopRunner
    monkeypatch.setitem(sys.modules, "coomi.engine.loop_runner", runner_module)

    async def collect(token=None):
        return [item async for item in service._stream_loop_command(
            command_body="task", prompt="/loop task", trace_id="t", session_id="s",
            workspace_root=tmp_path, cancellation_token=token, started=0.0
        )]

    success = asyncio.run(collect())
    assert [item[0] for item in success][-2:] == ["TurnPhase", "AgentCompleted"]
    cancelled = asyncio.run(collect(types.SimpleNamespace(is_cancelled=lambda: True)))
    assert cancelled[-1][0] == "AgentCancelled"
    mode["value"] = "error"
    failed = asyncio.run(collect())
    assert failed[-1][0] == "AgentError"


def test_permission_system_runtime_sync_and_registry_helpers(monkeypatch, tmp_path):
    levels = types.SimpleNamespace(AUTO="auto", DENY="deny", ASK="ask")
    modes = types.SimpleNamespace(ASK_APPROVAL="ask", APPROVE_FOR_ME="approve", FULL_ACCESS="full")

    class PermissionSystem:
        def __init__(self):
            self._bash_safety = types.SimpleNamespace(check_command=lambda command: types.SimpleNamespace(risk_level="low"))
            self.check_permission = lambda tool, args: "original"

        def set_mode(self, mode):
            self.mode = mode

    permissions = coomi._create_storydex_permission_system(levels, modes, PermissionSystem, tmp_path, "approve")
    assert permissions.mode == "approve"
    assert permissions.check_permission("Read", {"path": "a.md"}) == "auto"
    assert permissions._storydex_workspace_root == tmp_path.resolve()

    service = coomi.StorydexCoomiAgentService()
    service._permission_mode = "ask_approval"
    created = service._create_permission_system(levels, modes, PermissionSystem, tmp_path)
    assert created.mode == "ask"
    assert created.check_permission("Read", {}) == "ask"

    updated_roots = []
    tool = types.SimpleNamespace(set_workspace_root=lambda root: updated_roots.append(root))
    registry = types.SimpleNamespace(list_tools=lambda: [tool, object()])
    executor_permission = types.SimpleNamespace(_storydex_mode="approve_for_me")
    executor = types.SimpleNamespace(tool_registry=registry, permission_system=executor_permission)
    agent = types.SimpleNamespace(tool_registry=registry, tool_executor=executor, plan_mode=True)
    session = types.SimpleNamespace()
    coomi._sync_coomi_runtime_workspace(agent=agent, session=session, workspace_root=tmp_path, app_context="ctx")
    assert session.cwd == tmp_path.resolve().as_posix()
    assert executor.project_path == tmp_path.resolve()
    assert executor_permission._storydex_plan_mode is True
    assert len(updated_roots) == 2
    coomi._sync_storydex_tools_workspace(None, tmp_path)


def test_build_system_prompt_and_tool_registry(monkeypatch, tmp_path):
    provider = types.SimpleNamespace(model="fake", get_model_display_name=lambda: "Fake Model")
    services = types.ModuleType("coomi.services")
    services.get_llm_provider = lambda: provider
    monkeypatch.setitem(sys.modules, "coomi.services", services)

    session_module = types.ModuleType("coomi.engine.session")

    async def build_system_prompt(**kwargs):
        assert kwargs["cwd"] == tmp_path.as_posix()
        return "base"

    session_module.build_system_prompt = build_system_prompt
    monkeypatch.setitem(sys.modules, "coomi.engine.session", session_module)
    memory = types.ModuleType("coomi.services.memory")
    memory.MemoryManager = lambda **kwargs: types.SimpleNamespace(**kwargs)
    memory.MemoryRecall = lambda provider, manager: object()
    monkeypatch.setitem(sys.modules, "coomi.services.memory", memory)

    normal = asyncio.run(coomi._build_coomi_system_prompt(workspace_root=tmp_path, prompt="write"))
    plan = asyncio.run(coomi._build_coomi_system_prompt(workspace_root=tmp_path, prompt="plan", plan_mode=True))
    assert "Storydex Project Runtime" in normal
    assert "Plan Mode" in plan

    registered = []
    registry = types.SimpleNamespace(register=lambda tool: registered.append(tool))
    registry_module = types.ModuleType("coomi.tools.registry")
    registry_module.create_default_registry = lambda: registry
    monkeypatch.setitem(sys.modules, "coomi.tools.registry", registry_module)
    runtime_tools = types.ModuleType("services.storydex_coomi_runtime_tools")
    runtime_tools.create_workspace_bound_tool_overrides = lambda root: ["read", "write"]
    monkeypatch.setitem(sys.modules, "services.storydex_coomi_runtime_tools", runtime_tools)
    agent_tools = types.ModuleType("services.storydex_agent_tools")

    class DummyTool:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    for name in (
        "StorydexApplyStoryIncrementTool", "StorydexHelpGuideSearchTool", "StorydexProjectSearchTool",
        "StorydexRuntimePresetStatusTool", "StorydexSyncWikiTool", "StorydexVersionStatusTool", "StorydexWikiQueryTool",
    ):
        setattr(agent_tools, name, DummyTool)
    monkeypatch.setitem(sys.modules, "services.storydex_agent_tools", agent_tools)
    result = coomi._create_storydex_tool_registry(tmp_path)
    assert result is registry
    assert len(registered) == 9


def test_status_context_config_parsing_and_sync_chat(monkeypatch, tmp_path):
    original_resolve_context_window = coomi._resolve_context_window
    monkeypatch.setattr(coomi, "_storydex_coomi_home", _noop_home)
    monkeypatch.setattr(coomi.StorydexCoomiAgentService, "_ensure_coomi_installed", staticmethod(lambda: None))
    active = types.SimpleNamespace(id="p", type="openai", model="m", display="Model")
    config_module = types.ModuleType("coomi.services.llm.config")
    config_module.ConfigManager = lambda: types.SimpleNamespace(get_active=lambda: active)
    monkeypatch.setitem(sys.modules, "coomi.services.llm.config", config_module)
    monkeypatch.setattr(
        coomi,
        "_create_storydex_tool_registry",
        lambda root, policy=None: types.SimpleNamespace(list_tools=lambda: [1, 2]),
    )
    monkeypatch.setattr(coomi, "_resolve_context_window", lambda: 1000)
    service = coomi.StorydexCoomiAgentService()
    status = service.get_status(workspace_root=tmp_path)
    assert status["providerId"] == "p" and status["toolCount"] == 2
    assert status["usedTokens"] == 0

    service._sessions["s"] = types.SimpleNamespace(last_prompt_tokens=30, token_usage=types.SimpleNamespace(total_tokens=40, input_tokens=20))
    service._agents["s"] = types.SimpleNamespace(context_window_size=100, plan_mode=True)
    status = service.get_status(workspace_root=tmp_path)
    assert status["planMode"] is True and status["usedTokens"] == 30

    assert coomi._parse_context_window(None) is None
    assert coomi._parse_context_window(-1) is None
    assert coomi._parse_context_window(1) == coomi.MIN_CONTEXT_WINDOW
    monkeypatch.setattr(coomi, "_resolve_context_window", original_resolve_context_window)
    monkeypatch.setattr(coomi, "_read_providers_config_payload", lambda: {"active": "p", "providers": {"p": {"contextWindow": 12345}}})
    assert coomi._resolve_context_window() == 12345
    monkeypatch.setattr(coomi, "_read_providers_config_payload", lambda: {})
    assert coomi._resolve_context_window() == coomi.DEFAULT_CONTEXT_WINDOW

    class SyncProvider:
        def chat(self, messages, options):
            return types.SimpleNamespace(content="sync")

    class AwaitableProvider:
        def chat(self, messages, options):
            async def response():
                return types.SimpleNamespace(content="awaited")
            return response()

    assert asyncio.run(coomi._call_provider_chat(SyncProvider(), [], None)).content == "sync"
    assert asyncio.run(coomi._call_provider_chat(AwaitableProvider(), [], None)).content == "awaited"


def test_home_redirect_endpoint_compat_and_loop_spec(monkeypatch, tmp_path):
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_HOME", tmp_path / "home")
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_SESSIONS", tmp_path / "home/.coomi/sessions")
    monkeypatch.setattr(coomi, "_COOMI_HOME_REDIRECTS_INSTALLED", False)

    session_history = types.ModuleType("coomi.services.session_history")
    services = types.ModuleType("coomi.services")
    services.session_history = session_history
    monkeypatch.setitem(sys.modules, "coomi.services", services)
    monkeypatch.setitem(sys.modules, "coomi.services.session_history", session_history)

    class ConfigManager:
        def __init__(self):
            self.config_dir = Path("old")
            self.config_path = Path("old/providers.json")
            self.data = {}

        def _load(self):
            return {"loaded": True}

    config_module = types.ModuleType("coomi.services.llm.config")
    config_module.ConfigManager = ConfigManager
    monkeypatch.setitem(sys.modules, "coomi.services.llm.config", config_module)

    class MemoryManager:
        def _generate_project_hash(self, path):
            return "hash"

    memory_manager_module = types.ModuleType("coomi.services.memory.manager")
    memory_manager_module.MemoryManager = MemoryManager
    monkeypatch.setitem(sys.modules, "coomi.services.memory.manager", memory_manager_module)

    assert coomi._install_coomi_home_redirects() is True
    assert coomi._install_coomi_home_redirects() is True
    config = ConfigManager()
    assert config.config_path == tmp_path / "home/.coomi/config/providers.json"
    manager = MemoryManager()
    assert manager._get_global_memory_dir() == tmp_path / "home/.coomi/memory"
    assert "projects/hash/memory" in manager._get_project_memory_dir(tmp_path).as_posix()
    assert manager._get_project_memory_dir(None) == manager._get_global_memory_dir()
    assert session_history.default_sessions_dir() == coomi.STORYDEX_COOMI_SESSIONS

    monkeypatch.setattr(coomi, "_COOMI_ENDPOINT_COMPAT_INSTALLED", False)

    class ProviderConfig:
        @classmethod
        def from_dict(cls, provider_id, data):
            return data

    config_module.ProviderConfig = ProviderConfig
    coomi._install_coomi_endpoint_compat()
    converted = ProviderConfig.from_dict("p", {"base_url": "https://x.test/v1/chat/completions"})
    assert converted["base_url"] == "https://x.test/v1"
    coomi._install_coomi_endpoint_compat()

    types_module = types.ModuleType("coomi.types")

    class Spec:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    types_module.Spec = Spec
    monkeypatch.setitem(sys.modules, "coomi.types", types_module)
    spec_file = tmp_path / "spec.md"
    spec_file.write_text("spec", encoding="utf-8")
    path, spec = coomi._resolve_loop_spec(tmp_path, "spec.md")
    assert path == spec_file.as_posix() and spec is None
    path, spec = coomi._resolve_loop_spec(tmp_path, "- step one\n- step two")
    assert path is None and spec.steps == ["step one", "step two"]


def test_provider_config_file_and_redirected_home_fast_path(monkeypatch, tmp_path):
    config = tmp_path / "providers.json"
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_CONFIG", config)
    assert coomi._read_providers_config_payload() == {}
    config.write_text("[]", encoding="utf-8")
    assert coomi._read_providers_config_payload() == {}
    config.write_text("{broken", encoding="utf-8")
    assert coomi._read_providers_config_payload() == {}
    config.write_text('{"active":"p"}', encoding="utf-8")
    assert coomi._read_providers_config_payload()["active"] == "p"

    monkeypatch.setattr(coomi, "STORYDEX_COOMI_HOME", tmp_path / "home")
    monkeypatch.setattr(coomi, "_ensure_storydex_coomi_config", lambda: None)
    monkeypatch.setattr(coomi, "_install_coomi_endpoint_compat", lambda: None)
    monkeypatch.setattr(coomi, "_install_coomi_home_redirects", lambda: True)
    before = dict(os.environ)
    with coomi._storydex_coomi_home():
        pass
    assert dict(os.environ) == before


def test_cross_workspace_session_isolation_and_clear(monkeypatch, tmp_path):
    """Verify that the same Storydex sessionId in two different workspaces:

    - produces distinct runtime keys (runtime, intent, permissions)
    - does not create cross-workspace Coomi session bindings
    - clearing one workspace does not evict the other
    """
    import services.storydex_intent_service as intent_mod

    service = coomi.StorydexCoomiAgentService()
    monkeypatch.setattr(coomi, "_build_coomi_system_prompt", lambda **kwargs: asyncio.sleep(0, result="system"))
    monkeypatch.setattr(coomi, "_resolve_context_window", lambda: 4096)
    monkeypatch.setattr(coomi, "_create_storydex_tool_registry", lambda root, policy=None: "registry")
    monkeypatch.setattr(coomi, "_write_coomi_session_binding", lambda **kwargs: None)
    monkeypatch.setattr(coomi, "_sync_coomi_runtime_workspace", lambda **kwargs: setattr(kwargs["session"], "synced", True))
    monkeypatch.setattr(coomi, "_restore_bound_coomi_session", lambda **kwargs: None)

    class Provider:
        model = "fake-model"

    services = types.ModuleType("coomi.services")
    services.get_llm_provider = lambda: Provider()
    monkeypatch.setitem(sys.modules, "coomi.services", services)

    class SessionManager:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def create_session(self, **kwargs):
            return types.SimpleNamespace(**kwargs)

    monkeypatch.setitem(sys.modules, "coomi.engine.session", types.ModuleType("coomi.engine.session"))
    sys.modules["coomi.engine.session"].SessionManager = SessionManager

    class AgentLoop:
        def __init__(self, provider, registry, **kwargs):
            self.provider = provider
            self.registry = registry
            self.plan_mode = False
            for k, v in kwargs.items():
                setattr(self, k, v)

    monkeypatch.setitem(sys.modules, "coomi.engine.loop", types.ModuleType("coomi.engine.loop"))
    sys.modules["coomi.engine.loop"].AgentLoop = AgentLoop

    security = types.ModuleType("coomi.security")
    security.PermissionLevel = types.SimpleNamespace()
    security.PermissionMode = types.SimpleNamespace()
    security.PermissionSystem = object
    monkeypatch.setitem(sys.modules, "coomi.security", security)

    permission = types.SimpleNamespace()
    monkeypatch.setattr(service, "_create_permission_system", lambda *args, **kwargs: permission)

    ws_a = tmp_path / "workspace_a"
    ws_b = tmp_path / "workspace_b"
    ws_a.mkdir(); ws_b.mkdir()

    shared_session = "shared-session"

    async def create(ws):
        return await service._get_or_create_runtime(
            session_id=shared_session, workspace_root=ws, prompt="hello", app_context="ctx"
        )

    # Create runtime in both workspaces with the same sessionId
    agent_a, session_a = asyncio.run(create(ws_a))
    agent_b, session_b = asyncio.run(create(ws_b))
    assert agent_a is not agent_b, "different workspaces must create separate agents"
    assert session_a is not session_b, "different workspaces must create separate sessions"

    key_a = service._runtime_key(session_id=shared_session, workspace_root=ws_a)
    key_b = service._runtime_key(session_id=shared_session, workspace_root=ws_b)
    assert key_a != key_b, "runtime keys must differ across workspaces"
    assert service._sessions[key_a] is session_a
    assert service._sessions[key_b] is session_b

    # Permissions cached per key
    assert service._permissions[key_a] is permission

    # Same sessionId, different workspace: intent service also isolates
    k_a = intent_mod.StorydexIntentService._session_key(workspace_root=ws_a, session_id=shared_session)
    k_b = intent_mod.StorydexIntentService._session_key(workspace_root=ws_b, session_id=shared_session)
    assert k_a != k_b, "intent session keys must differ across workspaces"

    # Clear workspace A → workspace B must remain
    service.clear_session(shared_session, workspace_root=ws_a)
    assert key_a not in service._sessions
    assert key_a not in service._agents
    assert key_a not in service._permissions
    assert service._sessions.get(key_b) is session_b, "ws_b session must survive clearance of ws_a"
    assert service._agents.get(key_b) is agent_b
    assert service._permissions.get(key_b) is permission

    # Binding validation: cross-workspace binding file must be rejected
    binding_path = coomi._coomi_binding_path(ws_a, shared_session)
    binding_path.parent.mkdir(parents=True, exist_ok=True)
    cross_ws_binding = {
        "version": 1,
        "workspaceRoot": str(ws_b.resolve()),
        "storydexSessionId": shared_session,
        "coomiSessionId": "coomi-cross",
    }
    binding_path.write_text(json.dumps(cross_ws_binding), encoding="utf-8")
    result = coomi._read_coomi_session_binding(workspace_root=ws_a, storydex_session_id=shared_session)
    assert result == {}, "cross-workspace binding must be rejected"
