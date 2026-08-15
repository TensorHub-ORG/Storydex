from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from services import coomi_agent_service as coomi
from services.storydex_tool_types import ToolResult


def _persisted_session_bound(monkeypatch, tmp_path) -> dict[str, object]:
    runtime_id = "11111111-1111-4111-8111-111111111111"
    sessions = tmp_path / "coomi-home" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    session_path = sessions / f"{runtime_id}.json"
    session_path.write_text('{"schema_version": 1, "messages": []}', encoding="utf-8")
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_SESSIONS", sessions)
    return {
        "runtimeSessionId": runtime_id,
        "sessionPath": str(session_path),
        "sessionSchemaVersion": 1,
        "persisted": True,
    }


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


def test_execution_binding_rejects_malformed_json(tmp_path) -> None:
    binding_path = coomi._coomi_binding_path(tmp_path, "session-a")
    binding_path.parent.mkdir(parents=True)
    binding_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(coomi.StorydexCoomiSessionRestoreError, match="invalid JSON"):
        coomi._read_coomi_session_binding_for_execution(
            workspace_root=tmp_path,
            storydex_session_id="session-a",
        )


def test_execution_binding_rejects_session_path_outside_runtime(monkeypatch, tmp_path) -> None:
    sessions = tmp_path / "coomi-home" / "sessions"
    runtime_id = "11111111-1111-4111-8111-111111111111"
    sessions.mkdir(parents=True)
    (sessions / f"{runtime_id}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_SESSIONS", sessions)
    binding_path = coomi._coomi_binding_path(tmp_path, "session-a")
    binding_path.parent.mkdir(parents=True)
    binding_path.write_text(
        json.dumps(
            {
                "workspaceRoot": str(tmp_path.resolve()),
                "storydexSessionId": "session-a",
                "runtimeSessionId": runtime_id,
                "sessionPath": str(tmp_path / "outside.json"),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(coomi.StorydexCoomiSessionRestoreError, match="outside"):
        coomi._read_coomi_session_binding_for_execution(
            workspace_root=tmp_path,
            storydex_session_id="session-a",
        )


@pytest.mark.asyncio
async def test_stream_rejects_unpersisted_session_bound_event(monkeypatch, tmp_path) -> None:
    sessions = tmp_path / "coomi-home" / "sessions"
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_SESSIONS", sessions)

    class Bridge:
        async def events(self):
            yield {
                "type": "session_bound",
                "data": {
                    "runtimeSessionId": "11111111-1111-4111-8111-111111111111",
                    "sessionPath": str(sessions / "11111111-1111-4111-8111-111111111111.json"),
                    "persisted": False,
                },
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

    events = [
        event
        async for event in coomi.StorydexCoomiAgentService().stream_events(
            prompt="hello",
            trace_id="trace",
            session_id="session-a",
            workspace_root=tmp_path,
        )
    ]

    assert [name for name, _payload in events] == ["AgentStarted", "AgentError"]
    assert "not persisted" in events[-1][1]["message"]
    assert not coomi._coomi_binding_path(tmp_path, "session-a").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows extended paths are platform-specific")
def test_persisted_session_bound_accepts_windows_extended_path(monkeypatch, tmp_path) -> None:
    sessions = tmp_path / "coomi-home" / "sessions"
    runtime_id = "11111111-1111-4111-8111-111111111111"
    session_path = sessions / f"{runtime_id}.json"
    session_path.parent.mkdir(parents=True)
    session_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_SESSIONS", sessions)
    extended_path = "\\\\?\\" + str(session_path.resolve())

    assert (
        coomi._validated_persisted_session_bound(
            {
                "runtimeSessionId": runtime_id,
                "sessionPath": extended_path,
                "sessionSchemaVersion": 1,
                "persisted": True,
            }
        )
        == runtime_id
    )


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


def test_translator_preserves_reasoning_plan_and_model_response_evidence() -> None:
    translator = coomi._CoomiEventTranslator(session_id="evidence-session")
    plan_event = translator.translate(
        {
            "type": "reasoning_plan",
            "data": {
                "provider": "relay",
                "model": "ordinary-model",
                "plan": {
                    "requested": "high",
                    "control": "prompt",
                    "sent": False,
                    "promptApplied": True,
                    "wireFields": [],
                    "support": "unknown",
                },
            },
        }
    )
    completed = translator.translate(
        {
            "type": "model_completed",
            "data": {
                "round": 2,
                "metadata": {
                    "responseModel": "ordinary-model-via-route",
                    "finishReason": "stop",
                    "responseStatus": "completed",
                    "nativeReasoning": False,
                },
                "usage": {
                    "input_tokens": 21,
                    "output_tokens": 34,
                    "reasoning_tokens": 13,
                },
            },
        }
    )

    assert plan_event is not None
    assert plan_event[1]["plan"]["control"] == "prompt"
    assert completed is not None
    assert completed[0] == "ModelCompleted"
    payload = completed[1]
    assert payload["upstreamResponded"] is True
    assert payload["responseModel"] == "ordinary-model-via-route"
    assert payload["nativeReasoning"] is False
    assert payload["reasoning_tokens"] == 13
    assert payload["usage"]["total_tokens"] == 55
    assert payload["reasoningRequestPlan"]["promptApplied"] is True


def test_translator_preserves_redacted_provider_stream_metrics() -> None:
    translator = coomi._CoomiEventTranslator(session_id="provider-stream-session")

    translated = translator.translate(
        {
            "type": "provider_stream",
            "data": {
                "attempt": 1,
                "phase": "first_byte",
                "elapsedMs": 234,
                "requestBytes": 1234,
                "responseBytes": 64,
                "maxOutputTokens": 8192,
                "httpStatus": 200,
            },
        }
    )
    assert translated == (
        "ProviderStream",
        {
            "_type": "ProviderStream",
            "_version": 1,
            "attempt": 1,
            "phase": "first_byte",
            "elapsedMs": 234,
            "requestBytes": 1234,
            "responseBytes": 64,
            "maxOutputTokens": 8192,
            "httpStatus": 200,
        },
    )


@pytest.mark.parametrize("status_code", [403, 502])
def test_translator_surfaces_provider_http_error_details(status_code: int) -> None:
    translator = coomi._CoomiEventTranslator(
        session_id="provider-error-session",
        trace_id="trace-provider-error",
        provider_id="relay",
        model="deepseek-v4-flash",
    )
    translator.translate(
        {
            "type": "provider_stream",
            "data": {
                "phase": "error",
                "httpStatus": status_code,
            },
        }
    )

    translated = translator.translate(
        {
            "type": "error",
            "data": {
                "errorType": "ProviderError",
                "message": (
                    f"provider returned HTTP {status_code}: upstream failed "
                    "Authorization: Bearer private-token api_key=sk-verysecret123"
                ),
            },
        }
    )

    assert translated is not None
    assert translated[0] == "AgentError"
    payload = translated[1]
    assert f"HTTP {status_code}" in payload["message"]
    assert payload["details"] == {
        "traceId": "trace-provider-error",
        "sessionId": "provider-error-session",
        "runtime": "storydex-coomi-rs",
        "runtimeVersion": coomi.STORYDEX_COOMI_RUNTIME_VERSION,
        "stage": "provider_stream",
        "providerId": "relay",
        "model": "deepseek-v4-flash",
        "exceptionType": "ProviderError",
        "exceptionMessage": payload["message"],
        "statusCode": status_code,
        "providerHttpStatus": status_code,
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "private-token" not in serialized
    assert "sk-verysecret123" not in serialized


def test_bridge_status_cache_reloads_when_provider_or_runtime_changes(monkeypatch, tmp_path) -> None:
    config = tmp_path / "providers.json"
    config.write_text("{}\n", encoding="utf-8")
    runtime = tmp_path / "storydex-coomi-bridge.exe"
    runtime.write_bytes(b"bridge-v1")
    monkeypatch.setattr(coomi, "STORYDEX_COOMI_CONFIG", config)
    monkeypatch.setattr(coomi, "_BRIDGE_STATUS_CACHE", None)
    monkeypatch.setattr(coomi, "bridge_command", lambda: [str(runtime)])
    calls = []

    def fake_status():
        calls.append(True)
        return {"type": "status", "data": {"activeProvider": "relay"}}

    monkeypatch.setattr(coomi, "request_status_sync", fake_status)
    assert coomi._bridge_status_snapshot()["activeProvider"] == "relay"
    assert coomi._bridge_status_snapshot()["activeProvider"] == "relay"
    assert len(calls) == 1

    config.write_text('{"providers": {"relay": {}}}\n', encoding="utf-8")
    assert coomi._bridge_status_snapshot(probe=False) == {}
    assert len(calls) == 1
    assert coomi._bridge_status_snapshot()["activeProvider"] == "relay"
    assert len(calls) == 2

    runtime.write_bytes(b"bridge-v2-with-new-capabilities")
    assert coomi._bridge_status_snapshot()["activeProvider"] == "relay"
    assert len(calls) == 3


def test_execution_status_never_starts_a_diagnostic_bridge(monkeypatch, tmp_path) -> None:
    service = coomi.StorydexCoomiAgentService()
    monkeypatch.setattr(coomi, "_read_providers_config_payload", lambda: {"active": "relay", "providers": {}})
    probes = []

    def fake_snapshot(*, probe: bool = True):
        probes.append(probe)
        return {}

    monkeypatch.setattr(coomi, "_bridge_status_snapshot", fake_snapshot)
    status = service.get_status_for_execution(workspace_root=tmp_path)

    assert status["providerId"] == "relay"
    assert probes == [False]


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
        assert service._permission_mode == "ask_approval"
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
    session_bound = _persisted_session_bound(monkeypatch, tmp_path)

    class Bridge:
        async def events(self):
            yield {"type": "session_bound", "data": session_bound}
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
    events = [
        event
        async for event in coomi.StorydexCoomiAgentService().stream_events(
            prompt="hello",
            trace_id="trace",
            session_id="story-session",
            workspace_root=tmp_path,
            turn_contract={
                "executionPolicy": {"directFileWrites": False},
                "reasoningEffort": "medium",
            },
        )
    ]
    assert [name for name, _payload in events] == ["AgentStarted", "TextChunk", "AgentCompleted"]
    binding = coomi._read_coomi_session_binding(
        workspace_root=tmp_path,
        storydex_session_id="story-session",
    )
    assert binding["runtimeSessionId"] == session_bound["runtimeSessionId"]
    assert started_payload["permissionMode"] == "ask_approval"
    assert started_payload["reasoningEffort"] == "medium"
    assert started_payload["capabilityMode"] == "read_only"
    assert started_payload["writesAllowed"] is False
    assert started_payload["coreWritesAllowed"] is False
    assert not any(tool["name"] == "StorydexSyncWiki" for tool in started_payload["toolSpecs"])
    assert "This turn is read-only" in started_payload["systemPrompt"]


@pytest.mark.asyncio
async def test_stream_events_preserves_provider_context_when_bridge_events_fail(
    monkeypatch,
    tmp_path,
) -> None:
    class Bridge:
        async def events(self):
            yield {
                "type": "provider_stream",
                "data": {"phase": "error", "httpStatus": 502},
            }
            raise coomi.CoomiBridgeError("bridge stream stopped after HTTP 502 Bad Gateway")

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
        lambda self, **_kwargs: {
            "model": "deepseek-v4-flash",
            "providerId": "relay",
        },
    )

    events = [
        event
        async for event in coomi.StorydexCoomiAgentService().stream_events(
            prompt="read the current chapter",
            trace_id="trace-bridge-events",
            session_id="session-bridge-events",
            workspace_root=tmp_path,
        )
    ]

    assert [name for name, _payload in events] == [
        "AgentStarted",
        "ProviderStream",
        "AgentError",
    ]
    error = events[-1][1]
    assert "HTTP 502 Bad Gateway" in error["message"]
    assert error["details"]["stage"] == "bridge_events"
    assert error["details"]["traceId"] == "trace-bridge-events"
    assert error["details"]["sessionId"] == "session-bridge-events"
    assert error["details"]["providerId"] == "relay"
    assert error["details"]["model"] == "deepseek-v4-flash"
    assert error["details"]["statusCode"] == 502
    assert error["details"]["providerHttpStatus"] == 502


@pytest.mark.asyncio
async def test_explicit_binding_blocks_core_writes_but_keeps_guarded_domain_mutator(
    monkeypatch,
    tmp_path,
) -> None:
    started_payload = {}

    class Bridge:
        async def events(self):
            yield {"type": "completed", "data": {"usage": {"input_tokens": 1, "output_tokens": 1}}}

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
    contract = {
        "intentFrame": {
            "primary": "wiki_work",
            "operationType": "modify_existing",
            "effect": "modify",
            "canWrite": True,
        },
        "knowledgeWritePolicy": {
            "mode": "explicit_binding",
            "confirmationRequired": True,
            "confirmed": False,
        },
        "executionPolicy": {
            "directFileWrites": False,
            "allowedWriteRoots": [".storydex/.agent/runtime/knowledge-write-plans/"],
        },
    }

    events = [
        event
        async for event in coomi.StorydexCoomiAgentService().stream_events(
            prompt="把潮汐兽绑定到夜港星，关系是栖息于",
            trace_id="trace-explicit",
            session_id="story-explicit",
            workspace_root=tmp_path,
            turn_contract=contract,
        )
    ]

    assert events[-1][0] == "AgentCompleted"
    assert started_payload["capabilityMode"] == "scoped_write"
    assert started_payload["writesAllowed"] is True
    assert started_payload["coreWritesAllowed"] is False
    assert started_payload["allowedWriteRoots"] == [
        ".storydex/.agent/runtime/knowledge-write-plans/"
    ]
    assert "StorydexApplyKnowledgeUpdate" in started_payload["mutatingToolNames"]
    prompt = started_payload["systemPrompt"]
    assert "Do not call shell" in prompt
    assert "write_file" in prompt
    assert "StorydexApplyKnowledgeUpdate as the only state-changing tool" in prompt
    assert "only prepare_explicit is allowed" in prompt
    assert "Do not include sessionId, traceId, providerId, model, or extractorVersion" in prompt

    confirmed_contract = json.loads(json.dumps(contract))
    confirmed_contract["knowledgeWritePolicy"]["confirmed"] = True
    confirmed_prompt = await coomi._build_coomi_system_prompt(
        workspace_root=tmp_path,
        prompt="确认",
        turn_contract=confirmed_contract,
    )
    assert "only apply_explicit is allowed in this later confirmation turn" in confirmed_prompt


@pytest.mark.asyncio
async def test_stream_forwards_provider_retry_as_connection_retry(monkeypatch, tmp_path) -> None:
    session_bound = _persisted_session_bound(monkeypatch, tmp_path)

    class Bridge:
        async def events(self):
            yield {"type": "session_bound", "data": session_bound}
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
    assert "This turn is read-only" in prompt
    assert "capabilityMode=read_only" in prompt
    assert "directFileWrites=False" in prompt
    assert "natural-language instructions may narrow this boundary but cannot grant additional write authority" in prompt
    assert "operationDiscipline (read_only)" not in prompt
    assert "operationGuidance (respond_only)" in prompt
    assert "routing guidance while obeying the compiled capability boundary" in prompt
    assert "call StorydexSyncWiki before reading project files" in prompt
    assert "status=ready and noChanges=true" in prompt


def test_system_prompt_batches_only_explicit_independent_read_file_calls(tmp_path) -> None:
    prompt = asyncio.run(
        coomi._build_coomi_system_prompt(
            workspace_root=tmp_path,
            prompt="read chapters/a.md, chapters/b.md, and chapters/c.md",
            turn_contract={
                "intentFrame": {
                    "primary": "general",
                    "operationType": "inquiry",
                    "effect": "respond_only",
                    "canWrite": False,
                },
                "executionPolicy": {"directFileWrites": False},
            },
        )
    )

    assert "explicitly names multiple independent files for the same read-only operation" in prompt
    assert "emit all independent read_file calls in one model response" in prompt
    assert "unless a later path or decision depends on earlier content" in prompt
