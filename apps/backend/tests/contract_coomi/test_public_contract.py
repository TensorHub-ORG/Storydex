from __future__ import annotations

import inspect
import asyncio
from importlib.metadata import version

from services.coomi_agent_service import _CoomiEventTranslator
from services.coomi_version_service import read_expected_coomi_version


def test_usage_provenance_public_contract_or_pinned_legacy_baseline():
    from coomi.services import LLMUsage
    from coomi.services.llm import UsageStreamAccumulator, usage_from_response

    assert version("coomi-agent") == read_expected_coomi_version()

    missing = LLMUsage(
        source="missing",
        protocol="openai_chat",
        requested_model="contract-model",
        estimated_input_tokens=12,
        estimator="contract-estimator",
    )
    payload = missing.to_dict()
    assert payload["_type"] == "LLMUsage"
    assert payload["_version"] == 1
    assert payload["source"] == "missing"
    assert payload["prompt_tokens"] is None
    assert payload["estimated_input_tokens"] == 12
    assert UsageStreamAccumulator is not None
    assert usage_from_response is not None


def test_agent_and_loop_public_signatures():
    from coomi.engine.loop import AgentLoop
    from coomi.engine.loop_runner import LoopRunner

    agent_params = inspect.signature(AgentLoop).parameters
    assert {"llm", "tool_registry", "permission_system", "project_path"} <= set(agent_params)
    assert inspect.isasyncgenfunction(AgentLoop.run_stream)

    start_params = inspect.signature(LoopRunner.start_loop).parameters
    assert {"cwd", "memory_manager", "memory_recall"} <= set(start_params)
    assert inspect.isasyncgenfunction(LoopRunner.start_loop)


def test_session_lifecycle_and_run_stream_is_iterable(tmp_path):
    from coomi.engine.loop import AgentLoop
    from coomi.engine.session import SessionManager
    from coomi.tools.registry import ToolRegistry

    class Provider:
        model = "contract-model"

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        manager = SessionManager(history_dir=tmp_path, persist_history=False)
        session = manager.create_session(system_prompt="contract", cwd=str(tmp_path), model="contract-model")
        agent = AgentLoop(Provider(), ToolRegistry(), project_path=str(tmp_path))
        stream = agent.run_stream(session, "hello")
        assert stream.__aiter__() is stream
        assert session.system_prompt == "contract"
        assert session.id
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_translated_public_event_shapes():
    from coomi.ui.events import (
        AgentCancelled,
        AgentError,
        CompressionEvent,
        ReasoningChunk,
        TextChunk,
        ToolDone,
        ToolRunning,
        ToolStart,
        UsageUpdate,
    )

    translator = _CoomiEventTranslator(session_id="contract-session")
    cases = [
        (TextChunk(content="text"), "TextChunk", "content"),
        (ReasoningChunk(content="reason"), "ReasoningChunk", "content"),
        (ToolStart(tool_name="Read", arguments={}), "ToolStart", "tool_name"),
        (ToolRunning(tool_name="Read"), "ToolRunning", "progress"),
        (ToolDone(tool_name="Read", result_preview="ok"), "ToolDone", "result_preview"),
        (UsageUpdate(usage={"total_tokens": 1}), "UsageUpdate", "usage"),
        (CompressionEvent(before=2, after=1), "CompressionEvent", "summary"),
        (AgentError(message="bad"), "AgentError", "message"),
        (AgentCancelled(), "AgentCancelled", "reason"),
    ]
    for event, expected_type, expected_field in cases:
        translated = translator.translate(event)
        assert translated is not None
        name, payload = translated
        assert name == expected_type
        assert payload["_type"] == expected_type
        assert expected_field in payload


def test_permissions_memory_cancel_and_config_public_contract(monkeypatch, tmp_path):
    from coomi.engine.loop import AgentLoop
    from coomi.engine.loop_runner import LoopRunner
    from coomi.security import PermissionLevel, PermissionMode, PermissionSystem
    from coomi.services.llm.config import ConfigManager
    from coomi.services.memory import MemoryManager, MemoryRecall
    from coomi.tools.registry import ToolRegistry

    permissions = PermissionSystem()
    permissions.set_mode(PermissionMode.FULL_ACCESS)
    assert PermissionLevel.AUTO.value == "auto"
    assert permissions is not None

    manager = MemoryManager(project_path=str(tmp_path))
    provider = object()
    recall = MemoryRecall(provider, manager)
    assert manager is not None and recall is not None

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        runner = LoopRunner(provider, ToolRegistry(), permission_system=permissions)
        stream = runner.start_loop(cwd=str(tmp_path), memory_manager=None, memory_recall=None)
        assert stream.__aiter__() is stream

        agent = AgentLoop(provider, ToolRegistry(), permission_system=permissions, project_path=str(tmp_path))
        agent.cancel_token.cancel()
        assert agent.cancel_token.is_cancelled is True
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    config = ConfigManager()
    assert config.data["version"] == 1
