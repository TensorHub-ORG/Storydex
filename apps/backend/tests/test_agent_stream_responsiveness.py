from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from api import routes_agent
from services.agent_git_autocommit_service import AgentGitSnapshot
from services.execution_coordinator import ExecutionCoordinator


class _ConnectedRequest:
    headers = {}

    async def is_disconnected(self) -> bool:
        return False


def _packet(chunk: str) -> dict:
    data_line = next(line for line in chunk.splitlines() if line.startswith("data:"))
    return json.loads(data_line.removeprefix("data:").strip())


def test_stream_sends_acceptance_and_heartbeats_before_slow_intent_finishes(monkeypatch, tmp_path):
    class SlowIntentService:
        completed = False

        async def classify_intent(self, **kwargs):
            # Simulate cold Coomi/OpenAI imports that block synchronously before
            # the provider reaches its first await.
            time.sleep(0.06)
            self.completed = True
            return {"primary": "general", "confidence": "medium", "signals": [], "method": "llm"}

    class FastGitService:
        def begin_turn(self, workspace_root):
            return AgentGitSnapshot(workspace_root=workspace_root, available=False)

        def finish_turn(self, snapshot, **kwargs):
            return {"_type": "GitAutoCommit", "status": "info", "created": False}

    class OrchestrationService:
        def build_turn_contract(self, workspace_root, **kwargs):
            return {"contextAssembly": {"budget": {"blockCount": 0}}, "turnPlan": {}}

    async def fake_runtime(**kwargs):
        yield 'event: done\ndata: {"type":"done"}\n\n'

    intent_service = SlowIntentService()
    monkeypatch.setattr(routes_agent, "storydex_intent_service", intent_service)
    monkeypatch.setattr(routes_agent, "agent_git_autocommit_service", FastGitService())
    monkeypatch.setattr(routes_agent, "storydex_orchestration_service", OrchestrationService())
    monkeypatch.setattr(routes_agent, "_resolve_agent_workspace_root", lambda payload: tmp_path)
    monkeypatch.setattr(routes_agent, "_stream_coomi_sse", fake_runtime)
    monkeypatch.setattr(routes_agent, "_PHASE_HEARTBEAT_SECONDS", 0.01)

    payload = routes_agent.AgentChatRequest(
        prompt="帮我处理一下",
        activeFile="chapters/001.md",
        workspaceRoot=str(tmp_path),
    )

    async def collect():
        started = time.perf_counter()
        stream = routes_agent._stream_agent_chat_request_sse(
            payload=payload,
            request=_ConnectedRequest(),
            trace_id="trace-fast-first-packet",
            session_id="session-1",
            cancellation_token=routes_agent._CancellationToken(),
        )
        first = await stream.__anext__()
        first_elapsed = time.perf_counter() - started
        first_completed_state = intent_service.completed
        remaining = [chunk async for chunk in stream]
        return first, first_elapsed, first_completed_state, remaining

    first, first_elapsed, first_completed_state, remaining = asyncio.run(collect())

    assert _packet(first)["_type"] == "RunAccepted"
    assert first_elapsed < 0.05
    assert first_completed_state is False
    packets = [_packet(chunk) for chunk in remaining]
    intent_packets = [packet for packet in packets if packet.get("phase") == "intent_classification"]
    heartbeat_packets = [packet for packet in intent_packets if packet.get("heartbeat") is True]
    assert heartbeat_packets
    assert heartbeat_packets[0]["elapsedMs"] < 50
    assert intent_packets[-1]["status"] == "success"
    assert intent_service.completed is True


def test_task_planning_phase_is_emitted_before_planner_completes(monkeypatch, tmp_path):
    planner_completed = False
    coordinator = ExecutionCoordinator()
    monkeypatch.setattr(routes_agent, "execution_coordinator", coordinator)

    async def slow_plan(**kwargs):
        nonlocal planner_completed
        await asyncio.sleep(0.05)
        planner_completed = True
        return []

    class FastGitService:
        def finish_turn(self, snapshot, **kwargs):
            return {
                "_type": "GitAutoCommit",
                "status": "info",
                "created": False,
                "message": "no changes",
            }

    monkeypatch.setattr(routes_agent, "_create_agent_task_plan", slow_plan)
    monkeypatch.setattr(routes_agent, "agent_git_autocommit_service", FastGitService())

    async def read_first():
        stream = routes_agent._stream_coomi_sse(
            prompt="test",
            trace_id="trace-plan",
            session_id="session-plan",
            active_file="",
            workspace_root=tmp_path,
            story_generation={},
            turn_contract={},
            git_snapshot=AgentGitSnapshot(workspace_root=tmp_path, available=False),
            request=_ConnectedRequest(),
            cancellation_token=routes_agent._CancellationToken(),
        )
        first = await stream.__anext__()
        completed_at_first = planner_completed
        await stream.aclose()
        return first, completed_at_first

    first, completed_at_first = asyncio.run(read_first())
    packet = _packet(first)
    assert packet["phase"] == "task_planning"
    assert packet["status"] == "running"
    assert completed_at_first is False
    assert coordinator.try_reserve() is True
    coordinator.release_reservation()
    intent_files = list((Path(tmp_path) / ".storydex" / ".agent" / "execution-intents").glob("*.json"))
    assert intent_files
    assert json.loads(intent_files[0].read_text(encoding="utf-8"))["state"] == "finalization_failed"


def test_cold_intent_workers_are_serialized_to_avoid_provider_import_races(monkeypatch):
    class BlockingIntentService:
        active = 0
        max_active = 0

        async def classify_intent(self, **kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.03)
                return {"primary": "general"}
            finally:
                self.active -= 1

    service = BlockingIntentService()
    monkeypatch.setattr(routes_agent, "storydex_intent_service", service)

    async def run_both():
        return await asyncio.gather(
            routes_agent._classify_intent_without_blocking_event_loop(prompt="one"),
            routes_agent._classify_intent_without_blocking_event_loop(prompt="two"),
        )

    results = asyncio.run(run_both())
    assert [item["primary"] for item in results] == ["general", "general"]
    assert service.max_active == 1


def test_slow_task_planning_emits_heartbeat_and_success(monkeypatch, tmp_path):
    coordinator = ExecutionCoordinator()
    monkeypatch.setattr(routes_agent, "execution_coordinator", coordinator)
    monkeypatch.setattr(routes_agent, "_PHASE_HEARTBEAT_SECONDS", 0.01)

    async def slow_plan(**kwargs):
        await asyncio.sleep(0.04)
        return []

    class Git:
        def finish_turn(self, snapshot, **kwargs):
            return {"_type": "GitAutoCommit", "status": "info", "created": False}

    class Service:
        def cancel_execution(self, **kwargs):
            return False

        async def stream_events(self, **kwargs):
            raise AssertionError("needs-user-input turn must not call the model")
            yield

    monkeypatch.setattr(routes_agent, "_create_agent_task_plan", slow_plan)
    monkeypatch.setattr(routes_agent, "agent_git_autocommit_service", Git())
    monkeypatch.setattr(routes_agent, "get_storydex_coomi_agent_service", lambda: Service())
    monkeypatch.setattr(
        routes_agent,
        "_build_chat_payload",
        lambda **kwargs: {"record": {"traceId": kwargs["trace_id"]}},
    )
    monkeypatch.setattr(routes_agent, "_persist_execution_trace", lambda *args: args[1])

    async def collect():
        return [
            _packet(chunk)
            async for chunk in routes_agent._stream_coomi_sse(
                prompt="plan",
                trace_id="trace-slow-plan",
                session_id="session-slow-plan",
                active_file="",
                workspace_root=tmp_path,
                story_generation={},
                turn_contract={"status": "needs_user_input", "requiredQuestions": [{"message": "choose"}]},
                git_snapshot=AgentGitSnapshot(workspace_root=tmp_path, available=True),
                request=_ConnectedRequest(),
                cancellation_token=routes_agent._CancellationToken(),
            )
        ]

    packets = asyncio.run(collect())
    planning = [item for item in packets if item.get("phase") == "task_planning"]
    assert any(item.get("heartbeat") is True for item in planning)
    assert planning[-1]["status"] == "success"


def test_slow_model_first_output_emits_heartbeat_and_success(monkeypatch, tmp_path):
    coordinator = ExecutionCoordinator()
    monkeypatch.setattr(routes_agent, "execution_coordinator", coordinator)
    monkeypatch.setattr(routes_agent, "_PHASE_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(routes_agent, "_create_agent_task_plan", lambda **kwargs: asyncio.sleep(0, result=[]))

    class Git:
        def finish_turn(self, snapshot, **kwargs):
            return {"_type": "GitAutoCommit", "status": "info", "created": False}

    class Service:
        def cancel_execution(self, **kwargs):
            return False

        async def stream_events(self, **kwargs):
            yield "AgentStarted", {}
            await asyncio.sleep(0.04)
            yield "TextChunk", {"content": "reply"}
            yield "AgentCompleted", {"total_tokens": 1}

    monkeypatch.setattr(routes_agent, "agent_git_autocommit_service", Git())
    monkeypatch.setattr(routes_agent, "get_storydex_coomi_agent_service", lambda: Service())
    monkeypatch.setattr(
        routes_agent,
        "_build_chat_payload",
        lambda **kwargs: {"record": {"traceId": kwargs["trace_id"]}},
    )
    monkeypatch.setattr(routes_agent, "_persist_execution_trace", lambda *args: args[1])

    async def collect():
        return [
            _packet(chunk)
            async for chunk in routes_agent._stream_coomi_sse(
                prompt="model",
                trace_id="trace-slow-model",
                session_id="session-slow-model",
                active_file="",
                workspace_root=tmp_path,
                story_generation={},
                turn_contract={},
                git_snapshot=AgentGitSnapshot(workspace_root=tmp_path, available=True),
                request=_ConnectedRequest(),
                cancellation_token=routes_agent._CancellationToken(),
            )
        ]

    packets = asyncio.run(collect())
    model = [item for item in packets if item.get("phase") == "model_execution"]
    assert any(item.get("heartbeat") is True for item in model)
    assert any(item.get("status") == "success" for item in model)


def test_slow_snapshot_emits_heartbeat_and_warning_success(monkeypatch, tmp_path):
    coordinator = ExecutionCoordinator()
    monkeypatch.setattr(routes_agent, "execution_coordinator", coordinator)
    monkeypatch.setattr(routes_agent, "_PHASE_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(routes_agent, "_resolve_agent_workspace_root", lambda payload: tmp_path)
    monkeypatch.setattr(routes_agent, "_create_agent_execution_log_session", lambda **kwargs: None)
    monkeypatch.setattr(routes_agent, "_create_agent_task_plan", lambda **kwargs: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(
        routes_agent,
        "storydex_intent_service",
        type("Intent", (), {"classify_intent": lambda self, **kwargs: asyncio.sleep(0, result={"primary": "general"})})(),
    )
    monkeypatch.setattr(
        routes_agent,
        "storydex_orchestration_service",
        type("Orchestration", (), {"build_turn_contract": lambda self, root, **kwargs: {"status": "needs_user_input", "requiredQuestions": [{"message": "choose"}]}})(),
    )

    class SlowGit:
        def begin_turn(self, root):
            time.sleep(0.04)
            return AgentGitSnapshot(workspace_root=root, available=False, error_message="unavailable")

        def finish_turn(self, snapshot, **kwargs):
            return {"_type": "GitAutoCommit", "status": "warning", "created": False}

    class Service:
        def cancel_execution(self, **kwargs):
            return False

    monkeypatch.setattr(routes_agent, "agent_git_autocommit_service", SlowGit())
    monkeypatch.setattr(routes_agent, "get_storydex_coomi_agent_service", lambda: Service())
    monkeypatch.setattr(
        routes_agent,
        "_build_chat_payload",
        lambda **kwargs: {"record": {"traceId": kwargs["trace_id"]}},
    )
    monkeypatch.setattr(routes_agent, "_persist_execution_trace", lambda *args: args[1])

    payload = routes_agent.AgentChatRequest(prompt="snapshot", workspaceRoot=str(tmp_path), confirmNoSnapshot=True)

    async def collect():
        return [
            _packet(chunk)
            async for chunk in routes_agent._stream_agent_chat_request_sse(
                payload=payload,
                request=_ConnectedRequest(),
                trace_id="trace-slow-snapshot",
                session_id="session-slow-snapshot",
                cancellation_token=routes_agent._CancellationToken(),
            )
        ]

    packets = asyncio.run(collect())
    snapshot = [item for item in packets if item.get("phase") == "workspace_snapshot"]
    assert any(item.get("heartbeat") is True for item in snapshot)
    assert snapshot[-1]["status"] == "warning"
    assert snapshot[-1]["noRestorePoint"] is True
