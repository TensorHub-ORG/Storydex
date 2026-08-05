from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from services import coomi_agent_service as coomi
from services.storydex_tool_types import ToolResult


def test_rust_session_binding_round_trip_and_delete(monkeypatch, tmp_path) -> None:
    sessions = tmp_path / "runtime-home" / "sessions"
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_SESSIONS", sessions)
    workspace = tmp_path / "story"
    workspace.mkdir()
    sessions.mkdir(parents=True)
    session_path = sessions / "runtime-1.json"
    session_path.write_text('{"messages": []}', encoding="utf-8")

    binding_path = coomi._write_coomi_session_binding(
        workspace_root=workspace,
        storydex_session_id="story-1",
        runtime_session_id="runtime-1",
    )
    binding = coomi._read_coomi_session_binding(
        workspace_root=workspace,
        storydex_session_id="story-1",
    )
    assert binding["runtime"] == "storydex-coomi-rs"
    assert binding["runtimeSessionId"] == "runtime-1"
    assert coomi._validated_session_path(binding) == session_path.resolve()

    coomi._delete_coomi_session_binding(
        workspace_root=workspace,
        storydex_session_id="story-1",
        delete_history=True,
    )
    assert not binding_path.exists()
    assert not session_path.exists()


def test_binding_rejects_session_path_outside_runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_SESSIONS", tmp_path / "sessions")
    with pytest.raises(ValueError, match="outside"):
        coomi._validated_session_path({"sessionPath": str(tmp_path / "escape.json")})


def test_translator_reports_turn_usage_instead_of_runtime_cumulative_usage() -> None:
    translator = coomi._CoomiEventTranslator(
        session_id="story-session",
        usage_baseline={
            "input_tokens": 4000,
            "cached_input_tokens": 500,
            "output_tokens": 300,
        },
    )

    usage_event = translator.translate({
        "type": "turn_completed",
        "data": {
            "usage": {
                "input_tokens": 9861,
                "cached_input_tokens": 500,
                "output_tokens": 549,
            }
        },
    })
    completed_event = translator.translate({
        "type": "completed",
        "data": {"usage": {"input_tokens": 9861, "output_tokens": 549}},
    })

    assert usage_event is not None
    assert usage_event[1]["usage"]["prompt_tokens"] == 5861
    assert usage_event[1]["usage"]["completion_tokens"] == 249
    assert usage_event[1]["usage"]["total_tokens"] == 6110
    assert completed_event is not None
    assert completed_event[1]["total_tokens"] == 6110
    assert completed_event[1]["prompt_tokens"] == 5861
    assert completed_event[1]["completion_tokens"] == 249


def test_session_snapshot_restore_and_rollback(monkeypatch, tmp_path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_SESSIONS", sessions)
    workspace = tmp_path / "story"
    workspace.mkdir()
    session_path = sessions / "runtime.json"
    session = {
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "one"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "two"},
        ]
    }
    session_path.write_text(json.dumps(session), encoding="utf-8")
    coomi._write_coomi_session_binding(
        workspace_root=workspace,
        storydex_session_id="story",
        runtime_session_id="runtime",
    )
    service = coomi.StorydexCoomiAgentService()
    snapshot = service.snapshot_session_history("story", workspace_root=workspace)
    assert snapshot["available"] is True
    assert service.rollback_last_turn("story", workspace_root=workspace)["rolledBack"] is True
    assert [item["content"] for item in json.loads(session_path.read_text(encoding="utf-8"))["messages"]] == ["first", "one"]
    assert service.restore_session_history(snapshot) is True
    assert json.loads(session_path.read_text(encoding="utf-8"))["messages"][-1]["content"] == "two"


def test_permission_modes_and_approval_resolution() -> None:
    async def exercise() -> None:
        service = coomi.StorydexCoomiAgentService()
        assert service.set_permission_mode("ask")["permissionMode"] == "ask_approval"
        assert service.cycle_permission_mode()["permissionMode"] == "approve_for_me"
        future = asyncio.get_running_loop().create_future()
        service._approval_waiters["approval-1"] = future
        result = service.resolve_approval("approval-1", "allow", response={"note": "ok"})
        assert result["resolved"] is True
        await asyncio.sleep(0)
        assert await future == {"decision": "allow", "response": {"note": "ok"}}

    asyncio.run(exercise())


def test_empty_exception_keeps_error_type_visible() -> None:
    assert coomi._coomi_error_message(NotImplementedError()) == (
        "Coomi execution failed (NotImplementedError)."
    )


def test_agent_error_includes_safe_exception_chain_and_origin() -> None:
    try:
        try:
            raise NotImplementedError
        except NotImplementedError as cause:
            raise coomi.CoomiBridgeError("event loop does not support subprocesses") from cause
    except coomi.CoomiBridgeError as error:
        packet = coomi._agent_error(
            "trace-1",
            error,
            stage="bridge_start",
            session_id="session-1",
            provider_id="relay",
            model="model-a",
        )

    details = packet["details"]
    assert details["stage"] == "bridge_start"
    assert details["runtimeVersion"] == coomi.STORYDEX_COOMI_RUNTIME_VERSION
    assert details["providerId"] == "relay"
    assert details["model"] == "model-a"
    assert [item["type"] for item in details["exceptionChain"]] == [
        "CoomiBridgeError",
        "NotImplementedError",
    ]
    assert details["origin"]["file"].endswith("test_coomi_agent_service_comprehensive.py")
    assert details["traceback"]


def test_agent_error_redacts_provider_credentials() -> None:
    error = coomi.CoomiBridgeError(
        "request failed Authorization: Bearer private-token api_key=sk-verysecret123"
    )
    packet = coomi._agent_error("trace-1", error)
    serialized = json.dumps(packet, ensure_ascii=False)
    assert "private-token" not in serialized
    assert "sk-verysecret123" not in serialized
    assert serialized.count("[REDACTED]") >= 2


@pytest.mark.asyncio
async def test_storydex_tool_callback_resolves_bridge_request(monkeypatch, tmp_path) -> None:
    class Bridge:
        resolved = None

        async def resolve(self, request_id, value):
            self.resolved = (request_id, value)

    class Registry:
        def dispatch(self, name, arguments):
            assert name == "StorydexWikiQuery"
            assert arguments == {"query": "hero"}
            return ToolResult(success=True, output="wiki-result")

    bridge = Bridge()
    await coomi.StorydexCoomiAgentService()._dispatch_tool_request(
        bridge,
        Registry(),
        {
            "requestId": "request-1",
            "call": {"name": "StorydexWikiQuery", "arguments": {"query": "hero"}},
        },
    )
    assert bridge.resolved == (
        "request-1",
        {"success": True, "output": "wiki-result"},
    )


@pytest.mark.asyncio
async def test_user_input_options_reach_bridge_as_complete_answers() -> None:
    class Bridge:
        resolved = None

        async def resolve(self, request_id, value):
            self.resolved = (request_id, value)

    service = coomi.StorydexCoomiAgentService()
    bridge = Bridge()
    events, forwarding = service._prepare_interaction(
        bridge=bridge,
        packet_type="user_input_request",
        data={
            "requestId": "request-questions",
            "request": {
                "questions": [
                    {"id": "protagonist", "header": "主角", "question": "主角路线？", "options": []},
                    {"id": "tone", "header": "风格", "question": "故事风格？", "options": []},
                    {"id": "length", "header": "篇幅", "question": "篇幅规模？", "options": []},
                ]
            },
        },
        trace_id="trace-questions",
        session_id="session-questions",
    )

    selected = ["废材逆袭", "史诗宏大", "长篇"]
    for (_event_name, event), answer in zip(events, selected):
        service.resolve_approval(
            event["approvalId"],
            "answer",
            response={"option": answer, "label": answer, "other_text": None},
        )
    await forwarding

    assert bridge.resolved == (
        "request-questions",
        {
            "answers": {
                "protagonist": "废材逆袭",
                "tone": "史诗宏大",
                "length": "长篇",
            }
        },
    )


def test_user_input_answer_prefers_free_text_and_never_uses_control_decision() -> None:
    assert coomi._user_input_answer(
        {"option": "其他", "label": "其他", "other_text": "自定义设定"},
        "answer",
    ) == "自定义设定"
    assert coomi._user_input_answer({}, "answer") == ""


@pytest.mark.asyncio
async def test_stream_events_preserves_storydex_event_contract(monkeypatch, tmp_path) -> None:
    started_payload = {}

    class Bridge:
        async def events(self):
            yield {"type": "session_bound", "data": {"runtimeSessionId": "runtime-1"}}
            yield {"type": "text_delta", "data": {"text": "hello"}}
            yield {"type": "completed", "data": {"usage": {"input_tokens": 2, "output_tokens": 3}}}

        async def close(self):
            return None

        async def cancel(self, *, steer=False):
            return None

    async def start(payload):
        started_payload.update(payload)
        return Bridge()

    monkeypatch.setattr(coomi.LiveBridgeProcess, "start", start)
    monkeypatch.setattr(
        coomi.StorydexCoomiAgentService,
        "get_status",
        lambda self, **_kwargs: {"model": "model", "providerId": "provider"},
    )
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_SESSIONS", tmp_path / "sessions")
    events = [
        event
        async for event in coomi.StorydexCoomiAgentService().stream_events(
            prompt="hello",
            trace_id="trace",
            session_id="story-session",
            workspace_root=tmp_path,
            turn_contract={"executionPolicy": {"directFileWrites": False}},
        )
    ]
    assert [name for name, _payload in events] == ["AgentStarted", "TextChunk", "AgentCompleted"]
    binding = coomi._read_coomi_session_binding(
        workspace_root=tmp_path,
        storydex_session_id="story-session",
    )
    assert binding["runtimeSessionId"] == "runtime-1"
    assert started_payload["permissionMode"] == "full_access"
    assert started_payload["writesAllowed"] is True
    assert any(tool["name"] == "StorydexSyncWiki" for tool in started_payload["toolSpecs"])
    assert "This turn is read-only" not in started_payload["systemPrompt"]


@pytest.mark.asyncio
async def test_stream_forwards_provider_retry_as_connection_retry(monkeypatch, tmp_path) -> None:
    class Bridge:
        async def events(self):
            yield {"type": "session_bound", "data": {"runtimeSessionId": "runtime-1"}}
            yield {"type": "text_delta", "data": {"text": "partial"}}
            yield {
                "type": "provider_retry",
                "data": {"attempt": 1, "maxAttempts": 3, "resetTextCharacters": 7},
            }
            yield {"type": "text_delta", "data": {"text": "ok"}}
            yield {"type": "completed", "data": {"usage": {"input_tokens": 1, "output_tokens": 1}}}

        async def close(self):
            return None

        async def cancel(self, *, steer=False):
            return None

    async def start(payload):
        return Bridge()

    monkeypatch.setattr(coomi.LiveBridgeProcess, "start", start)
    monkeypatch.setattr(
        coomi.StorydexCoomiAgentService,
        "get_status",
        lambda self, **_kwargs: {"model": "model", "providerId": "provider"},
    )
    events = [
        event
        async for event in coomi.StorydexCoomiAgentService().stream_events(
            prompt="hello",
            trace_id="trace",
            session_id="story-session",
            workspace_root=tmp_path,
            turn_contract={"executionPolicy": {"directFileWrites": False}},
        )
    ]
    names = [name for name, _payload in events]
    assert names == ["AgentStarted", "TextChunk", "ConnectionRetry", "TextChunk", "AgentCompleted"]
    retry = events[2][1]
    assert retry["_type"] == "ConnectionRetry"
    assert retry["attempt"] == 1
    assert retry["maxAttempts"] == 3
    assert retry["resetTextCharacters"] == 7
    assert retry["providerResetTextCharacters"] == 7
    assert "当前上游提供商服务不稳定" in retry["message"]


@pytest.mark.asyncio
async def test_stream_rejects_agent_attempt_to_enter_plan_mode(monkeypatch, tmp_path) -> None:
    class Bridge:
        async def events(self):
            yield {
                "type": "plan_mode_changed",
                "data": {"active": True, "source": "agent"},
            }
            yield {
                "type": "completed",
                "data": {"usage": {"input_tokens": 1, "output_tokens": 1}},
            }

        async def close(self):
            return None

        async def cancel(self, *, steer=False):
            return None

    async def start(_payload):
        return Bridge()

    monkeypatch.setattr(coomi.LiveBridgeProcess, "start", start)
    monkeypatch.setattr(
        coomi.StorydexCoomiAgentService,
        "get_status",
        lambda self, **_kwargs: {"model": "model", "providerId": "provider"},
    )
    service = coomi.StorydexCoomiAgentService()
    events = [
        event
        async for event in service.stream_events(
            prompt="continue",
            trace_id="trace",
            session_id="story-session",
            workspace_root=tmp_path,
        )
    ]

    assert [name for name, _payload in events] == [
        "AgentStarted",
        "AgentWarning",
        "AgentCompleted",
    ]
    assert events[1][1]["warning_type"] == "AgentPlanModeEntryRejected"
    runtime_key = service._runtime_key(
        session_id="story-session",
        workspace_root=tmp_path,
    )
    assert service._plan_modes.get(runtime_key, False) is False


def test_config_validation_models_and_status(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    config = home / "config" / "providers.json"
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_HOME", home)
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_CONFIG", config)
    monkeypatch.setattr(coomi, "_ensure_storydex_coomi_config", lambda: config)
    monkeypatch.setattr(coomi.StorydexCoomiAgentService, "_ensure_coomi_installed", staticmethod(lambda: None))
    config.parent.mkdir(parents=True)
    config.write_text('{"version":1,"active":"","providers":{}}\n', encoding="utf-8")
    service = coomi.StorydexCoomiAgentService()
    updated = service.write_config(
        json.dumps(
            {
                "version": 1,
                "active": "relay",
                "providers": {
                    "relay": {
                        "type": "openai_compatible",
                        "display": "Relay",
                        "api_key": "secret",
                        "base_url": "https://example.test/v1",
                        "model": "model-a",
                    }
                },
            }
        )
    )
    assert updated["parsed"]["active"] == "relay"
    status = service.get_status(workspace_root=tmp_path)
    assert status["runtime"] == "storydex-coomi-rs"
    assert status["providerId"] == "relay"
    assert status["model"] == "model-a"


def test_status_tracks_plan_mode_and_persistent_context_per_storydex_session(
    monkeypatch, tmp_path
) -> None:
    sessions = tmp_path / "coomi-home" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_SESSIONS", sessions)
    monkeypatch.setattr(
        coomi.StorydexCoomiAgentService,
        "_ensure_coomi_installed",
        staticmethod(lambda: None),
    )
    monkeypatch.setattr(coomi, "_read_providers_config_payload", lambda: {})
    runtime_id = "11111111-1111-4111-8111-111111111111"
    (sessions / f"{runtime_id}.json").write_text(
        json.dumps(
            {
                "usage": {"input_tokens": 1_234_000, "output_tokens": 567_000},
                "context": {"estimated_active_tokens": 42_000},
            }
        ),
        encoding="utf-8",
    )
    coomi._write_coomi_session_binding(
        workspace_root=tmp_path,
        storydex_session_id="session-a",
        runtime_session_id=runtime_id,
    )
    service = coomi.StorydexCoomiAgentService()
    service.set_plan_mode(session_id="session-a", workspace_root=tmp_path, active=True)

    session_a = service.get_status(workspace_root=tmp_path, session_id="session-a")
    session_b = service.get_status(workspace_root=tmp_path, session_id="session-b")

    assert session_a["planMode"] is True
    assert session_a["permissionMode"] == "plan_mode"
    assert session_a["usedTokens"] == 42_000
    assert session_a["cumulativeTokens"] == 1_801_000
    assert session_b["planMode"] is False
    assert session_b["cumulativeTokens"] == 0


def test_persistent_context_deduplicates_events_and_accumulates_new_runtime(
    monkeypatch, tmp_path
) -> None:
    sessions = tmp_path / "coomi-home" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_SESSIONS", sessions)
    service = coomi.StorydexCoomiAgentService()
    snapshot = coomi._context_snapshot_from_bridge({})

    coomi._write_coomi_session_binding(
        workspace_root=tmp_path,
        storydex_session_id="session-a",
        runtime_session_id="runtime-1",
    )
    first = service._merge_persistent_context(
        workspace_root=tmp_path,
        session_id="session-a",
        snapshot=snapshot,
        runtime_total=100,
    )
    duplicate = service._merge_persistent_context(
        workspace_root=tmp_path,
        session_id="session-a",
        snapshot=snapshot,
        runtime_total=100,
    )
    increased = service._merge_persistent_context(
        workspace_root=tmp_path,
        session_id="session-a",
        snapshot=snapshot,
        runtime_total=150,
    )

    coomi._write_coomi_session_binding(
        workspace_root=tmp_path,
        storydex_session_id="session-a",
        runtime_session_id="runtime-2",
    )
    next_runtime = service._merge_persistent_context(
        workspace_root=tmp_path,
        session_id="session-a",
        snapshot=snapshot,
        runtime_total=25,
    )

    assert first["cumulativeTokens"] == 100
    assert duplicate["cumulativeTokens"] == 100
    assert increased["cumulativeTokens"] == 150
    assert next_runtime["cumulativeTokens"] == 175


def test_clear_session_preserves_usage_until_session_is_deleted(
    monkeypatch, tmp_path
) -> None:
    sessions = tmp_path / "coomi-home" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_SESSIONS", sessions)
    service = coomi.StorydexCoomiAgentService()
    coomi._write_coomi_usage_ledger(
        workspace_root=tmp_path,
        storydex_session_id="session-a",
        value={
            "cumulativeTokens": 123,
            "runtimeSessionId": "runtime-1",
            "runtimeTotalTokens": 123,
        },
    )
    ledger_path = coomi._coomi_usage_ledger_path(tmp_path, "session-a")

    service.clear_session("session-a", workspace_root=tmp_path, delete_history=True)
    assert ledger_path.is_file()

    service.clear_session(
        "session-a",
        workspace_root=tmp_path,
        delete_history=True,
        delete_usage=True,
    )
    assert not ledger_path.exists()


def test_persistent_context_tolerates_malformed_usage_ledger(tmp_path) -> None:
    ledger_path = coomi._coomi_usage_ledger_path(tmp_path, "session-a")
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        json.dumps(
            {
                "cumulativeTokens": "not-a-number",
                "runtimeSessionId": "runtime-1",
                "runtimeTotalTokens": {"invalid": True},
            }
        ),
        encoding="utf-8",
    )

    context = coomi.StorydexCoomiAgentService()._merge_persistent_context(
        workspace_root=tmp_path,
        session_id="session-a",
        snapshot=coomi._context_snapshot_from_bridge({}),
    )

    assert context["cumulativeTokens"] == 0


def test_planner_commit_and_model_helpers() -> None:
    tasks = coomi._parse_task_plan_content(
        '{"tasks":[{"title":"Inspect","status":"in_progress"}]}',
        trace_id="trace",
    )
    assert tasks[0]["title"] == "Inspect"
    assert tasks[0]["traceId"] == "trace"
    assert tasks[0]["order"] == 1
    filtered = coomi._parse_task_plan_content(
        'prefix ```json\n{"tasks":["分析需求",{"name":"Update chapter","notes":"Preserve continuity"}]}\n``` suffix',
        trace_id="trace",
    )
    assert [task["title"] for task in filtered] == ["Update chapter"]
    assert filtered[0]["detail"] == "Preserve continuity"
    assert coomi._parse_commit_message_content("- `agent: update story`") == "agent: update story"
    assert coomi._extract_model_ids({"data": [{"id": "a"}, {"name": "b"}, {"id": "a"}]}) == ["a", "b"]
    assert coomi._models_endpoint("https://example.test/v1/chat/completions", "openai") == "https://example.test/v1/models"


def test_translator_hides_reasoning_and_surfaces_errors() -> None:
    translator = coomi._CoomiEventTranslator(session_id="s")
    assert translator.translate({"type": "reasoning_delta", "data": {"text": "hidden"}}) is None
    event = translator.translate(
        {"type": "error", "data": {"errorType": "ProviderError", "message": "failed"}}
    )
    assert event is not None
    assert event[0] == "AgentError"
    assert event[1]["details"]["runtime"] == "storydex-coomi-rs"
    plan = translator.translate(
        {
            "type": "plan_mode_changed",
            "data": {"active": False, "permissionMode": "full_access", "source": "agent"},
        }
    )
    assert plan is not None
    assert plan[0] == "PlanModeChanged"
    assert plan[1]["planMode"] is False


def test_system_prompt_assigns_core_and_domain_tool_ownership(tmp_path) -> None:
    prompt = asyncio.run(
        coomi._build_coomi_system_prompt(
            workspace_root=tmp_path,
            prompt="write",
            turn_contract={
                "intentFrame": {
                    "primary": "general",
                    "operationType": "inquiry",
                    "effect": "respond_only",
                    "canWrite": False,
                    "method": "safe_fallback",
                },
                "executionPolicy": {"directFileWrites": False},
            },
        )
    )
    assert "Rust runtime tools" in prompt
    assert "Storydex domain tools" in prompt
    assert "Plan mode is inactive" in prompt
    assert "This turn is read-only" not in prompt
    assert "directFileWrites" not in prompt
    assert "only the user's /plan command activates read-only Plan mode" in prompt
    assert "operationDiscipline (read_only)" not in prompt
    assert "operationGuidance (respond_only)" in prompt
    assert "routing guidance, not a permission boundary" in prompt
