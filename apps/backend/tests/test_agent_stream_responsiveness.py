from __future__ import annotations

import asyncio
import json
import time

from api import routes_agent
from services.agent_git_autocommit_service import AgentGitSnapshot


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
