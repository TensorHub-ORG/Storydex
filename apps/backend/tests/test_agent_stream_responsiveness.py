from __future__ import annotations

import asyncio
import json
import threading
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
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.completed = False

        async def classify_intent(self, **kwargs):
            # Simulate cold Coomi/OpenAI imports that block synchronously before
            # the provider reaches its first await.
            self.started.set()
            if not self.release.wait(timeout=2.0):
                raise TimeoutError("test did not release the slow intent service")
            self.completed = True
            return {"primary": "general", "confidence": "medium", "signals": [], "method": "llm"}

    class FastGitService:
        def begin_turn(self, workspace_root):
            return AgentGitSnapshot(workspace_root=workspace_root, available=False)

        def finish_turn(self, snapshot, **kwargs):
            return {"_type": "GitAutoCommit", "status": "info", "created": False}

    class OrchestrationService:
        def __init__(self):
            self.contract_kwargs = {}

        def build_turn_contract(self, workspace_root, **kwargs):
            self.contract_kwargs = kwargs
            return {"contextAssembly": {"budget": {"blockCount": 0}}, "turnPlan": {}}

    class ActiveRuntime:
        def get_status(self, **kwargs):
            return {"providerId": "chy", "model": "deepseek-v4-flash"}

        def cancel_execution(self, **kwargs):
            return False

    async def fake_runtime(**kwargs):
        yield 'event: done\ndata: {"type":"done"}\n\n'

    intent_service = SlowIntentService()
    orchestration_service = OrchestrationService()
    monkeypatch.setattr(routes_agent, "storydex_intent_service", intent_service)
    monkeypatch.setattr(routes_agent, "agent_git_autocommit_service", FastGitService())
    monkeypatch.setattr(routes_agent, "storydex_orchestration_service", orchestration_service)
    monkeypatch.setattr(routes_agent, "get_storydex_coomi_agent_service", lambda: ActiveRuntime())
    monkeypatch.setattr(routes_agent, "_resolve_agent_workspace_root", lambda payload: tmp_path)
    monkeypatch.setattr(routes_agent, "_stream_coomi_sse", fake_runtime)
    monkeypatch.setattr(routes_agent, "_PHASE_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(routes_agent.followup_mailbox_service, "set_active_trace", lambda **kwargs: None)
    monkeypatch.setattr(routes_agent.followup_mailbox_service, "clear_active_trace", lambda **kwargs: None)

    trace_id = "trace-fast-first-packet"
    session_id = "session-1"
    coordinator = ExecutionCoordinator()
    execution_handle = coordinator.begin(tmp_path, session_id, trace_id)

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
            trace_id=trace_id,
            session_id=session_id,
            cancellation_token=routes_agent._CancellationToken(),
            execution_handle=execution_handle,
            resolved_workspace_root=tmp_path,
        )
        first = await stream.__anext__()
        first_elapsed = time.perf_counter() - started
        first_started_state = intent_service.started.is_set()
        first_completed_state = intent_service.completed
        heartbeat_observation = None
        remaining = []
        try:
            async for chunk in stream:
                remaining.append(chunk)
                packet = _packet(chunk)
                if (
                    heartbeat_observation is None
                    and packet.get("phase") == "intent_classification"
                    and packet.get("heartbeat") is True
                ):
                    heartbeat_observation = {
                        "intentCompleted": intent_service.completed,
                    }
                    intent_service.release.set()
        finally:
            intent_service.release.set()
        return (
            first,
            first_elapsed,
            first_started_state,
            first_completed_state,
            heartbeat_observation,
            remaining,
        )

    try:
        (
            first,
            first_elapsed,
            first_started_state,
            first_completed_state,
            heartbeat_observation,
            remaining,
        ) = asyncio.run(collect())
    finally:
        intent_service.release.set()
        if coordinator.active_handle(session_id=session_id, workspace_root=tmp_path) is execution_handle:
            execution_handle.reject_preflight("test_cleanup")

    assert _packet(first)["_type"] == "RunAccepted"
    # This is a deadlock watchdog, not a shared-runner microbenchmark. The
    # ordering assertion below proves acceptance precedes the slow intent work.
    assert first_elapsed < 1.0
    assert first_started_state is False
    assert first_completed_state is False
    packets = [_packet(chunk) for chunk in remaining]
    intent_packets = [packet for packet in packets if packet.get("phase") == "intent_classification"]
    heartbeat_packets = [packet for packet in intent_packets if packet.get("heartbeat") is True]
    assert heartbeat_packets
    assert heartbeat_observation == {"intentCompleted": False}
    assert intent_packets[-1]["status"] == "success"
    assert intent_service.completed is True
    assert orchestration_service.contract_kwargs["provider"] == "chy"
    assert orchestration_service.contract_kwargs["model"] == "deepseek-v4-flash"
    story_generation = orchestration_service.contract_kwargs["story_generation"]
    assert story_generation["preciseWordCountEnabled"] is False
    assert "chapterWordCountTarget" not in story_generation


def test_stream_completes_when_active_model_status_is_unavailable(monkeypatch, tmp_path):
    class FastIntentService:
        async def classify_intent(self, **kwargs):
            return {"primary": "general", "confidence": "medium", "signals": [], "method": "llm"}

    class FastGitService:
        def begin_turn(self, workspace_root):
            return AgentGitSnapshot(workspace_root=workspace_root, available=False)

        def finish_turn(self, snapshot, **kwargs):
            return {"_type": "GitAutoCommit", "status": "info", "created": False}

    class OrchestrationService:
        def build_turn_contract(self, workspace_root, **kwargs):
            assert kwargs["provider"] == ""
            assert kwargs["model"] == ""
            return {
                "contextAssembly": {"budget": {"blockCount": 0}},
                "turnPlan": {
                    "wordCountPolicy": {
                        "calibration": {
                            "status": "fallback",
                            "reason": "model_identity_unavailable",
                        }
                    }
                },
            }

    class UnavailableRuntime:
        def get_status(self, **kwargs):
            raise RuntimeError("status unavailable")

        def cancel_execution(self, **kwargs):
            return False

    captured_contract = {}

    async def fake_runtime(**kwargs):
        captured_contract.update(kwargs["turn_contract"])
        yield 'event: done\ndata: {"type":"done"}\n\n'
        kwargs["execution_handle"].reject_preflight("status_unavailable_test_complete")

    monkeypatch.setattr(routes_agent, "storydex_intent_service", FastIntentService())
    monkeypatch.setattr(routes_agent, "agent_git_autocommit_service", FastGitService())
    monkeypatch.setattr(routes_agent, "storydex_orchestration_service", OrchestrationService())
    monkeypatch.setattr(routes_agent, "get_storydex_coomi_agent_service", lambda: UnavailableRuntime())
    monkeypatch.setattr(routes_agent, "_resolve_agent_workspace_root", lambda payload: tmp_path)
    monkeypatch.setattr(routes_agent, "_stream_coomi_sse", fake_runtime)

    payload = routes_agent.AgentChatRequest(
        prompt="continue chapter",
        activeFile="chapters/001.md",
        workspaceRoot=str(tmp_path),
        confirmNoSnapshot=True,
    )

    async def collect():
        stream = routes_agent._stream_agent_chat_request_sse(
            payload=payload,
            request=_ConnectedRequest(),
            trace_id="trace-status-unavailable",
            session_id="session-status-unavailable",
            cancellation_token=routes_agent._CancellationToken(),
        )
        return [chunk async for chunk in stream]

    chunks = asyncio.run(collect())
    assert captured_contract["turnPlan"]["wordCountPolicy"]["calibration"] == {
        "status": "fallback",
        "reason": "model_identity_unavailable",
    }
    assert _packet(chunks[-1]) == {"type": "done"}


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
            turn_contract={
                "intentFrame": {
                    "primary": "project_organization",
                    "operationType": "modify_existing",
                    "complexity": "complex",
                }
            },
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


def test_cold_intent_workers_are_isolated_and_do_not_queue_behind_each_other(monkeypatch):
    workers_started = threading.Barrier(2)

    class BlockingIntentService:
        active = 0
        max_active = 0

        async def classify_intent(self, **kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                workers_started.wait(timeout=1.0)
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
    assert service.max_active == 2


def test_slow_task_planning_runs_in_background_without_blocking_agent_start(monkeypatch, tmp_path):
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
            yield "AgentStarted", {"_type": "AgentStarted"}
            await asyncio.sleep(0.06)
            yield "TextChunk", {"content": "reply"}
            yield "AgentCompleted", {"total_tokens": 1}

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
                turn_contract={
                    "status": "ready",
                    "intentFrame": {
                        "primary": "content_generation",
                        "operationType": "modify_existing",
                        "complexity": "complex",
                    },
                },
                git_snapshot=AgentGitSnapshot(workspace_root=tmp_path, available=True),
                request=_ConnectedRequest(),
                cancellation_token=routes_agent._CancellationToken(),
            )
        ]

    packets = asyncio.run(collect())
    planning = [item for item in packets if item.get("phase") == "task_planning"]
    assert any(item.get("heartbeat") is True for item in planning)
    assert planning[-1]["status"] == "success"
    agent_started_index = next(index for index, item in enumerate(packets) if item.get("_type") == "AgentStarted")
    planning_success_index = next(
        index
        for index, item in enumerate(packets)
        if item.get("phase") == "task_planning" and item.get("status") == "success"
    )
    assert agent_started_index < planning_success_index


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
