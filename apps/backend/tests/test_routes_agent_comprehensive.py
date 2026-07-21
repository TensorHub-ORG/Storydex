from __future__ import annotations

import asyncio
import json
import threading
import types
from pathlib import Path

import pytest

from api import routes_agent as routes
from core.exceptions import GitServiceError
from services.agent_git_autocommit_service import AgentGitSnapshot
from services.execution_coordinator import ExecutionCoordinator


class FakeRequest:
    def __init__(self, headers=None, disconnected=False):
        self.headers = headers or {}
        self._disconnected = disconnected

    async def is_disconnected(self):
        return self._disconnected


def _decode_sse(chunk: str):
    event = ""
    data = {}
    for line in chunk.splitlines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
    return event, data


def test_request_workspace_story_options_lock_git_and_sse_helpers(monkeypatch, tmp_path):
    request = FakeRequest({"x-trace-id": "trace", "x-session-id": "session"})
    assert routes._resolve_agent_trace_id(request) == "trace"
    assert routes._resolve_agent_session_id(request) == "session"
    assert routes._resolve_agent_trace_id(FakeRequest(), "fallback") == "fallback"
    assert routes._resolve_agent_session_id(FakeRequest()) == "default"

    opened = []
    fake_project = types.SimpleNamespace(workspace_root=tmp_path / "old", open_project=lambda value: opened.append(value))
    monkeypatch.setattr(routes, "project_service", fake_project)
    target = tmp_path / "new"
    target.mkdir()
    payload = routes.AgentChatRequest(prompt="x", workspaceRoot=str(target))
    assert routes._resolve_agent_workspace_root(payload) == target.resolve()
    assert opened == [target.resolve().as_posix()]
    fallback = routes._resolve_agent_workspace_root(routes.AgentChatRequest(prompt="x", workspaceRoot=str(tmp_path / "missing")))
    assert fallback == fake_project.workspace_root

    assert routes._normalize_story_generation_options(None) == {
        "fragmentCount": 1, "fragmentWordCount": 2000, "chapterTemplateId": ""
    }
    normalized = routes._normalize_story_generation_options(
        {"segmentCount": 99, "segmentWords": "bad", "chapter_template": "serial"}
    )
    assert normalized == {"fragmentCount": 20, "fragmentWordCount": 2000, "chapterTemplateId": "serial"}
    assert routes._apply_turn_contract_story_generation_defaults(normalized, {"turnPlan": {}}) is normalized
    applied = routes._apply_turn_contract_story_generation_defaults(normalized, {"turnPlan": {"selectedChapterTemplate": "book"}})
    assert applied["chapterTemplateId"] == "book" and normalized["chapterTemplateId"] == "serial"
    assert routes._bounded_int("bad", default=3, minimum=1, maximum=4) == 3

    routes._release_agent_generation_slot()
    assert routes._try_acquire_agent_generation_slot() is True
    assert routes._try_acquire_agent_generation_slot() is False
    routes._release_agent_generation_slot()
    routes._release_agent_generation_slot()

    monkeypatch.setattr(routes, "story_project_service", types.SimpleNamespace(read_project_settings=lambda root: {"agentCommitPromptEnabled": False}))
    assert routes._agent_commit_prompt_enabled(tmp_path) is False
    monkeypatch.setattr(routes, "story_project_service", types.SimpleNamespace(read_project_settings=lambda root: (_ for _ in ()).throw(OSError())))
    assert routes._agent_commit_prompt_enabled(tmp_path) is True
    assert routes._git_event_name({"_type": "GitCommitPrompt"}) == "GitCommitPrompt"
    assert routes._git_event_name({"_type": "bad"}) == "GitAutoCommit"
    error = routes._agent_busy_error(trace_id="t", session_id="s")
    assert error.code == "agent_busy" and error.status_code == 409
    assert _decode_sse(routes._encode_sse("Test", {"x": "中文"})) == ("Test", {"x": "中文"})


def test_phase_status_detail_and_text_sanitization_helpers():
    phase_cases = {
        "ToolDone": "tool", "TextChunk": "model", "GitCommitResult": "version_control",
        "TaskStarted": "planning", "TurnContract": "orchestration", "RunAccepted": "runtime",
        "AgentCompleted": "agent", "Other": "runtime",
    }
    for event, expected in phase_cases.items():
        assert routes._phase_for_event(event) == expected

    status_cases = [
        ("AgentError", {}, "error"), ("ToolDone", {"is_error": True}, "error"),
        ("TaskStarted", {}, "running"), ("TaskCompleted", {}, "success"),
        ("TaskFailed", {}, "error"), ("TaskSkipped", {}, "warning"),
        ("TaskPlanCreated", {}, "success"), ("GitAutoCommit", {"created": True}, "success"),
        ("TurnContract", {"status": "needs_user_input"}, "warning"), ("RunAccepted", {}, "running"),
        ("AgentCompleted", {}, "success"), ("AgentCancelled", {}, "warning"), ("Other", {}, "info"),
    ]
    for event, payload, expected in status_cases:
        assert routes._status_for_event(event, payload) == expected

    detail_cases = [
        ("TaskStarted", {"title": "task"}, "task"), ("ToolDone", {"tool_name": "Read"}, "Read"),
        ("TextChunk", {"content": "hello"}, "hello"),
        ("GitCommitResult", {"commit": {"subject": "commit"}}, "commit"),
        ("GitCommitPrompt", {"message": "confirm"}, "confirm"),
        ("AgentError", {"message": "bad"}, "bad"), ("RunAccepted", {"label": "accepted"}, "accepted"),
        ("TurnContract", {"turnPlan": {"requiresChapterTemplateSelection": True}}, "全新故事需要先选择章节目录模板"),
        ("TurnContract", {"intentFrame": {"primary": "wiki_work"}}, "wiki_work"),
        ("Other", {}, "Other"),
    ]
    for event, payload, expected in detail_cases:
        assert routes._detail_for_event(event, payload) == expected

    packet = routes._turn_phase_packet(
        trace_id="t", session_id="s", phase="intent", label="working", status="running", phase_started=0.0, heartbeat=True
    )
    assert packet["heartbeat"] is True and packet["elapsedMs"] >= 0
    assert routes._strip_visible_tool_text("plain") == "plain"
    assert routes._strip_visible_tool_text("before<read>secret</read>after") == "beforeafter"
    assert routes._strip_visible_tool_text("<path>a</path>") == ""
    assert routes._strip_visible_tool_text("DSML tool_calls invoke parameter") == ""
    assert routes._strip_visible_tool_text("visible\n<||DSML tool_calls invoke>\n") == "visible\n"
    assert routes._strip_textual_tool_blocks("") == ""
    assert routes._looks_like_tool_xml_fragment("<path>a</path>") is True


def test_trace_audit_history_tasks_and_ledger_helpers(monkeypatch, tmp_path):
    events = [
        routes._event_to_trace_event("TaskPlanCreated", {"tasks": [{"title": "Inspect"}]}, 1),
        routes._event_to_trace_event("TaskStarted", {"taskId": "t-task-1", "order": 1, "title": "Inspect", "status": "running"}, 2),
        routes._event_to_trace_event("ToolDone", {"tool_name": "Read", "tool_call_id": "c", "duration_ms": 5, "result_preview": "ok"}, 3),
        routes._event_to_trace_event("TurnContract", {
            "status": "ready", "intentFrame": {"primary": "story_generation"},
            "turnPlan": {"fragmentCount": 2, "fragmentWordCount": 500},
            "skillRegistry": {"skillCount": 3}, "toolRegistry": {"toolCount": 4},
            "contextAssembly": {
                "budget": {"blockCount": 5, "totalChars": 100},
                "contextTrace": {
                    "sources": [
                        {
                            "kind": "recent_segments",
                            "included": True,
                            "truncated": False,
                            "chars": 100,
                            "estTokens": 20,
                        }
                    ],
                    "duplicates": [],
                    "llmCalls": [],
                    "totals": {"contextChars": 100, "estContextTokens": 20},
                },
            },
        }, 4),
        routes._event_to_trace_event("GitCommitResult", {
            "created": True, "target": "workspace", "workspaceRoot": tmp_path.as_posix(),
            "commitHash": "abcdef", "shortHash": "abc", "changedFiles": ["a\\b.md"],
            "changedFileCount": 1, "added": 2, "removed": 1, "traceId": "t", "sessionId": "s",
        }, 5),
        routes._event_to_trace_event("AgentCompleted", {"total_tokens": 42}, 6),
        routes._event_to_trace_event("TaskCompleted", {"taskId": "t-task-1", "order": 1, "status": "success"}, 7),
    ]
    metrics = routes._extract_trace_metrics(events, "t", 10)
    assert metrics["toolCalls"] == 1 and metrics["completionTokens"] == 42
    audit = routes._build_audit(events)
    assert {item["action"] for item in audit} == {"coomi_tool_call", "storydex_turn_contract", "agent_git_commit"}
    tasks = routes._extract_task_plan(events, "t")
    assert tasks[0]["status"] == "completed"
    ledger = routes._extract_change_ledger(events, trace_id="t", session_id="s")
    assert ledger["changedFiles"] == ["a/b.md"] and ledger["diffSource"] == "commit"
    empty_ledger = routes._extract_change_ledger([], trace_id="t")
    assert empty_ledger["changedFileCount"] == 0

    data = {"reply": "hello", "llmModel": "m", "llmProvider": "p", "assistant": {"runtime": "coomi"}}
    history = routes._build_history_record(
        trace_id="t", prompt="p", data=data, trace=metrics, audit=audit, events=events,
        workspace_root=tmp_path, status="failed", error_message="bad"
    )
    assert history["errorCode"] == "coomi_agent_error"
    assert history["tasks"] and history["changeLedger"]["commitHash"] == "abcdef"

    fake_service = types.SimpleNamespace(get_status=lambda **kwargs: {"model": "m", "providerId": "p"})
    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: fake_service)
    routes.reset_llm_metrics("t")
    log_rows = []
    execution_log = types.SimpleNamespace(write=lambda event, payload, **kwargs: log_rows.append((event, payload, kwargs)))
    built = routes._build_chat_payload(
        trace_id="t",
        prompt="p",
        reply="r",
        events=events,
        started=0.0,
        workspace_root=tmp_path,
        session_id="s",
        execution_log_session=execution_log,
    )
    assert built["data"]["reply"] == "r" and built["record"]["workspaceRoot"] == tmp_path.as_posix()
    assert built["record"]["contextTrace"]["sources"][0]["kind"] == "recent_segments"
    assert log_rows[0][0] == "context_trace_summary" and log_rows[0][1]["sourceCount"] == 1

    assert routes._turn_contract_needs_user_input({"status": "needs_user_input"}) is True
    contract = {"requiredQuestions": [None, {}, {"message": "choose"}]}
    assert routes._turn_contract_user_input_message(contract) == "choose"
    assert routes._turn_contract_user_input_message({})
    assert routes._turn_contract_waiting_packet(contract)["status"] == "needs_user_input"


def test_task_planner_normalization_tracker_and_event_helpers(monkeypatch, tmp_path):
    class Planner:
        async def create_task_plan(self, **kwargs):
            return [
                "分析需求", {"title": "Inspect files", "description": "read"},
                {"name": "Commit Git changes", "status": "success"}, None,
            ]

    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: Planner())
    planned = asyncio.run(routes._create_agent_task_plan(
        prompt="p", trace_id="t", session_id="s", workspace_root=tmp_path,
        active_file="", story_generation={}, turn_contract={}
    ))
    assert len(planned) == 2 and planned[1]["status"] == "completed"

    class BrokenPlanner:
        async def create_task_plan(self, **kwargs):
            raise RuntimeError("bad")

    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: BrokenPlanner())
    assert asyncio.run(routes._create_agent_task_plan(
        prompt="p", trace_id="t", session_id="s", workspace_root=tmp_path,
        active_file="", story_generation={}, turn_contract={}
    )) == []
    assert routes._normalize_task_plan(None, trace_id="t") == []
    assert routes._is_generic_route_task_title("执行本轮请求") is True
    assert routes._normalize_task_status("error") == "failed"
    assert routes._normalize_task_status("weird") == "pending"
    assert routes._is_version_task({"title": "Git commit"}) is True

    tracker = routes._TaskRunTracker(planned, trace_id="t", session_id="s")
    assert tracker.plan_created_payload()["tasks"]
    assert tracker.complete_current() == []
    assert tracker.fail_current() == []
    assert tracker.start_next()[0][0] == "TaskStarted"
    assert tracker.advance_after_runtime_event("Other") == []
    assert tracker.advance_after_runtime_event("ToolDone")
    assert tracker.start_next() == []  # version task is held until Git stage
    version_events = tracker.start_version_task()
    assert version_events and version_events[-1][0] == "TaskStarted"
    assert tracker.finish_version_task(failed=False)[0][0] == "TaskCompleted"
    assert tracker.finish_version_task(failed=True, message="bad")[0][0] == "TaskFailed"
    assert tracker.skip_pending("stop") == []

    no_version = routes._TaskRunTracker([{"title": "One"}, {"title": "Two"}], trace_id="x", session_id="s")
    assert no_version.start_next()
    assert no_version.complete_through_execution()
    assert no_version.start_version_task() == []
    assert no_version.finish_version_task(failed=False) == []
    skip_tracker = routes._TaskRunTracker([{"title": "One"}, {"title": "Two"}], trace_id="x", session_id="s")
    assert len(skip_tracker.skip_remaining_execution("stop")) == 2
    assert len(routes._yield_task_events([("TaskStarted", {"x": 1})])) == 1
    collected = []
    routes._append_task_events(collected, [("TaskStarted", {"title": "x"})])
    assert collected[0]["event"] == "TaskStarted"


def test_collect_coomi_run_filters_tools_permissions_errors_and_disconnect(monkeypatch, tmp_path):
    class Service:
        def __init__(self):
            self.decisions = []

        async def stream_events(self, **kwargs):
            yield "TextChunk", {"content": "<read>secret</read>"}
            yield "TextChunk", {"content": "hello"}
            yield "PermissionRequest", {"approvalId": "a"}
            yield "AgentError", {"message": "bad"}
            yield "AgentCompleted", {"total_tokens": 1}

        def resolve_approval(self, approval_id, decision):
            self.decisions.append((approval_id, decision))

    service = Service()
    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: service)
    result = asyncio.run(routes._collect_coomi_run(
        prompt="p", trace_id="t", session_id="s", active_file="", workspace_root=tmp_path,
        story_generation={}, turn_contract={}, cancellation_token=routes._CancellationToken()
    ))
    assert result[0] == "hello" and result[2] is True and result[3] == "bad"
    assert service.decisions == [("a", "deny")]

    disconnected = FakeRequest(disconnected=True)
    token = routes._CancellationToken()
    result = asyncio.run(routes._collect_coomi_run(
        prompt="p", trace_id="t", session_id="s", active_file="", workspace_root=tmp_path,
        story_generation={}, turn_contract={}, cancellation_token=token, request=disconnected
    ))
    assert token.is_cancelled() is True and result[1] == []


def test_stream_coomi_sse_success_needs_input_and_runtime_error(monkeypatch, tmp_path):
    class Git:
        def finish_turn(self, snapshot, **kwargs):
            return {
                "_type": "GitCommitPrompt", "status": "pending", "created": False,
                "changedFiles": ["chapter.md"], "changedFileCount": 1, "message": "confirm",
            }

    monkeypatch.setattr(routes, "agent_git_autocommit_service", Git())
    monkeypatch.setattr(routes, "_agent_commit_prompt_enabled", lambda root: True)
    monkeypatch.setattr(routes, "trace_history_service", types.SimpleNamespace(upsert_record=lambda record, session: None))
    monkeypatch.setattr(routes, "_release_agent_generation_slot", lambda: None)
    monkeypatch.setattr(routes, "_create_agent_task_plan", lambda **kwargs: asyncio.sleep(0, result=[
        {"title": "Inspect"}, {"title": "Execute"}, {"title": "Git commit"}
    ]))
    monkeypatch.setattr(routes, "_build_chat_payload", lambda **kwargs: {"record": {"traceId": kwargs["trace_id"]}})

    class Service:
        async def stream_events(self, **kwargs):
            yield "AgentStarted", {}
            yield "TextChunk", {"content": "<read>hidden</read>"}
            yield "TextChunk", {"content": "visible"}
            yield "ToolDone", {"tool_name": "Read"}
            yield "AgentCompleted", {"total_tokens": 2}

    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: Service())
    snapshot = AgentGitSnapshot(workspace_root=tmp_path, available=True)

    async def collect(contract):
        return [_decode_sse(item) async for item in routes._stream_coomi_sse(
            prompt="p", trace_id="t", session_id="s", active_file="", workspace_root=tmp_path,
            story_generation={}, turn_contract=contract, git_snapshot=snapshot,
            request=FakeRequest(), cancellation_token=routes._CancellationToken()
        )]

    success = asyncio.run(collect({"status": "ready"}))
    names = [name for name, _ in success]
    assert "TextChunk" in names and "ToolDone" in names and names[-1] == "done"
    assert not any(data.get("content") == "" for name, data in success if name == "TextChunk")

    waiting = asyncio.run(collect({"status": "needs_user_input", "requiredQuestions": [{"message": "choose"}]}))
    assert any(name == "AgentCompleted" and data.get("status") == "needs_user_input" for name, data in waiting)

    class BrokenService:
        async def stream_events(self, **kwargs):
            raise RuntimeError("runtime broke")
            yield

    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: BrokenService())
    failed = asyncio.run(collect({"status": "ready"}))
    assert any(name == "AgentError" for name, _ in failed)


def test_stream_disconnect_finishes_cancelled_execution_in_background(monkeypatch, tmp_path):
    trace_records = []
    git_calls = []
    model_calls = []
    coordinator = ExecutionCoordinator()
    monkeypatch.setattr(routes, "execution_coordinator", coordinator)
    monkeypatch.setattr(routes, "_create_agent_task_plan", lambda **kwargs: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(
        routes,
        "_build_chat_payload",
        lambda **kwargs: {"record": {"traceId": kwargs["trace_id"], "events": kwargs["events"]}},
    )
    monkeypatch.setattr(
        routes,
        "_persist_execution_trace",
        lambda workspace, record, session: trace_records.append((workspace, dict(record), session)) or record,
    )

    class Git:
        def finish_turn(self, snapshot, **kwargs):
            git_calls.append(kwargs)
            return {"_type": "GitAutoCommit", "status": "info", "created": False}

    monkeypatch.setattr(routes, "agent_git_autocommit_service", Git())

    class Coomi:
        def cancel_execution(self, **kwargs):
            return False

        async def stream_events(self, **kwargs):
            model_calls.append(kwargs)
            yield "AgentCompleted", {"total_tokens": 0}

    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: Coomi())

    class DisconnectingRequest:
        checks = 0

        async def is_disconnected(self):
            self.checks += 1
            return self.checks >= 1

    request = DisconnectingRequest()
    snapshot = AgentGitSnapshot(workspace_root=tmp_path, available=True)

    async def collect_and_wait():
        packets = [
            _decode_sse(item)
            async for item in routes._stream_coomi_sse(
                prompt="cancel me",
                trace_id="trace-disconnect",
                session_id="session-disconnect",
                active_file="",
                workspace_root=tmp_path,
                story_generation={},
                turn_contract={},
                git_snapshot=snapshot,
                request=request,
                cancellation_token=routes._CancellationToken(),
            )
        ]
        for _ in range(100):
            if trace_records and not list((tmp_path / ".storydex" / ".agent" / "execution-intents").glob("*.json")):
                break
            await asyncio.sleep(0.01)
        return packets

    packets = asyncio.run(collect_and_wait())
    assert packets and packets[0][0] == "TurnPhase"
    assert len(git_calls) == 1
    assert model_calls == []
    assert trace_records and trace_records[0][1]["status"] == "cancelled"
    intent_root = tmp_path / ".storydex" / ".agent" / "execution-intents"
    assert not list(intent_root.glob("*.json"))
    assert coordinator.try_reserve() is True
    coordinator.release_reservation()


def test_preflight_disconnect_finishes_cancelled_execution_in_background(monkeypatch, tmp_path):
    trace_records = []
    git_calls = []
    model_calls = []
    intent_started = threading.Event()
    release_intent = threading.Event()
    coordinator = ExecutionCoordinator()
    monkeypatch.setattr(routes, "execution_coordinator", coordinator)
    monkeypatch.setattr(routes, "_PHASE_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(routes, "_create_agent_execution_log_session", lambda **kwargs: None)
    monkeypatch.setattr(
        routes,
        "_build_chat_payload",
        lambda **kwargs: {
            "data": {"route": "coomi", "reply": "", "events": kwargs["events"], "assistant": {}},
            "trace": {"traceId": kwargs["trace_id"], "durationMs": 1, "toolCalls": 0},
            "audit": [],
            "record": {"traceId": kwargs["trace_id"], "events": kwargs["events"]},
        },
    )
    monkeypatch.setattr(
        routes,
        "_persist_execution_trace",
        lambda workspace, record, session: trace_records.append(dict(record)) or record,
    )

    class Git:
        def begin_turn(self, root):
            git_calls.append(("begin", root))
            return AgentGitSnapshot(workspace_root=root, available=True)

        def finish_turn(self, snapshot, **kwargs):
            git_calls.append(("finish", snapshot))
            return {"_type": "GitAutoCommit", "status": "info", "created": False}

    monkeypatch.setattr(routes, "agent_git_autocommit_service", Git())

    class Intent:
        async def classify_intent(self, **kwargs):
            intent_started.set()
            while not release_intent.is_set():
                await asyncio.sleep(0.001)
            return {"primary": "general"}

    monkeypatch.setattr(routes, "storydex_intent_service", Intent())

    class Coomi:
        def stream_events(self, **kwargs):
            model_calls.append(kwargs)
            raise AssertionError("runtime must not start after preflight disconnect")

        def get_status(self, **kwargs):
            return {"model": "test", "providerId": "test"}

    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: Coomi())

    class DisconnectingRequest:
        checks = 0

        async def is_disconnected(self):
            self.checks += 1
            return self.checks >= 1

    payload = routes.AgentChatRequest(prompt="disconnect during intent", workspaceRoot=str(tmp_path))

    async def run():
        stream = routes._stream_agent_chat_request_sse(
            payload=payload,
            request=DisconnectingRequest(),
            trace_id="trace-preflight-disconnect",
            session_id="session-preflight-disconnect",
            cancellation_token=routes._CancellationToken(),
        )
        first = await stream.__anext__()
        await stream.__anext__()
        disconnect = asyncio.create_task(stream.__anext__())
        for _ in range(100):
            if intent_started.is_set():
                break
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.03)
        release_intent.set()
        try:
            await disconnect
        except StopAsyncIteration:
            pass
        await stream.aclose()
        for _ in range(200):
            if trace_records and not list((tmp_path / ".storydex" / ".agent" / "execution-intents").glob("*.json")):
                break
            await asyncio.sleep(0.01)
        return first

    first = asyncio.run(run())
    assert _decode_sse(first)[0] == "RunAccepted"
    assert model_calls == []
    assert [name for name, _ in git_calls] == ["begin", "finish"]
    assert len(trace_records) == 1
    assert trace_records[0]["status"] == "cancelled"
    terminal_events = [event for event in trace_records[0]["events"] if event.get("event") == "AgentCancelled"]
    assert len(terminal_events) == 1
    assert not list((tmp_path / ".storydex" / ".agent" / "execution-intents").glob("*.json"))
    assert coordinator.try_reserve() is True
    coordinator.release_reservation()


def test_run_accepted_close_finishes_cancelled_execution_without_leaks(monkeypatch, tmp_path):
    trace_records = []
    git_calls = []
    coordinator = ExecutionCoordinator()
    monkeypatch.setattr(routes, "execution_coordinator", coordinator)
    monkeypatch.setattr(routes, "_create_agent_execution_log_session", lambda **kwargs: None)
    monkeypatch.setattr(
        routes,
        "_build_chat_payload",
        lambda **kwargs: {
            "data": {"route": "coomi", "reply": "", "events": kwargs["events"], "assistant": {}},
            "trace": {"traceId": kwargs["trace_id"], "durationMs": 1, "toolCalls": 0},
            "audit": [],
            "record": {"traceId": kwargs["trace_id"], "events": kwargs["events"]},
        },
    )
    monkeypatch.setattr(
        routes,
        "_persist_execution_trace",
        lambda workspace, record, session: trace_records.append(dict(record)) or record,
    )

    class Git:
        def begin_turn(self, root):
            git_calls.append(("begin", root))
            return AgentGitSnapshot(workspace_root=root, available=True)

        def finish_turn(self, snapshot, **kwargs):
            git_calls.append(("finish", snapshot))
            return {"_type": "GitAutoCommit", "status": "info", "created": False}

    monkeypatch.setattr(routes, "agent_git_autocommit_service", Git())
    monkeypatch.setattr(
        routes,
        "storydex_intent_service",
        types.SimpleNamespace(classify_intent=lambda **kwargs: asyncio.sleep(0, result={"primary": "general"})),
    )
    monkeypatch.setattr(
        routes,
        "get_storydex_coomi_agent_service",
        lambda: types.SimpleNamespace(get_status=lambda **kwargs: {"model": "test", "providerId": "test"}),
    )
    payload = routes.AgentChatRequest(prompt="close after accepted", workspaceRoot=str(tmp_path))

    async def run():
        stream = routes._stream_agent_chat_request_sse(
            payload=payload,
            request=FakeRequest(),
            trace_id="trace-accepted-close",
            session_id="session-accepted-close",
            cancellation_token=routes._CancellationToken(),
        )
        first = await stream.__anext__()
        await stream.aclose()
        for _ in range(200):
            if trace_records and not list((tmp_path / ".storydex" / ".agent" / "execution-intents").glob("*.json")):
                break
            await asyncio.sleep(0.01)
        return first

    first = asyncio.run(run())
    assert _decode_sse(first)[0] == "RunAccepted"
    assert [name for name, _ in git_calls] == ["begin", "finish"]
    assert len(trace_records) == 1
    assert trace_records[0]["status"] == "cancelled"
    assert sum(event.get("event") == "AgentCancelled" for event in trace_records[0]["events"]) == 1
    assert not list((tmp_path / ".storydex" / ".agent" / "execution-intents").glob("*.json"))
    assert coordinator.try_reserve() is True
    coordinator.release_reservation()


def test_snapshot_failure_requires_confirmation_before_execution(monkeypatch, tmp_path):
    coordinator = ExecutionCoordinator()
    trace_records = []
    git_calls = []
    model_calls = []
    monkeypatch.setattr(routes, "execution_coordinator", coordinator)
    monkeypatch.setattr(routes, "_resolve_agent_workspace_root", lambda payload: tmp_path)
    monkeypatch.setattr(routes, "_create_agent_execution_log_session", lambda **kwargs: None)
    monkeypatch.setattr(routes, "_create_agent_task_plan", lambda **kwargs: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(
        routes,
        "storydex_intent_service",
        types.SimpleNamespace(classify_intent=lambda **kwargs: asyncio.sleep(0, result={"primary": "general"})),
    )
    monkeypatch.setattr(
        routes,
        "storydex_orchestration_service",
        types.SimpleNamespace(build_turn_contract=lambda *args, **kwargs: {"status": "ready", "turnPlan": {}}),
    )
    monkeypatch.setattr(
        routes,
        "_build_chat_payload",
        lambda **kwargs: {
            "data": {"route": "coomi", "reply": "done", "events": [], "assistant": {}},
            "trace": {"traceId": kwargs["trace_id"], "durationMs": 1, "toolCalls": 0},
            "audit": [],
            "record": {"traceId": kwargs["trace_id"], "status": kwargs["status"]},
        },
    )
    monkeypatch.setattr(
        routes,
        "_persist_execution_trace",
        lambda workspace, record, session: trace_records.append(dict(record)) or record,
    )

    class Git:
        def begin_turn(self, root):
            return AgentGitSnapshot(workspace_root=root, available=False, error_message="git unavailable")

        def finish_turn(self, snapshot, **kwargs):
            git_calls.append(kwargs)
            return {"_type": "GitAutoCommit", "status": "warning", "created": False}

    monkeypatch.setattr(routes, "agent_git_autocommit_service", Git())

    class Coomi:
        def cancel_execution(self, **kwargs):
            return False

        async def stream_events(self, **kwargs):
            model_calls.append(kwargs)
            yield "AgentCompleted", {"total_tokens": 0}

    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: Coomi())

    payload = routes.AgentChatRequest(prompt="run", workspaceRoot=str(tmp_path))

    async def collect(request_payload):
        return [
            _decode_sse(item)
            async for item in routes._stream_agent_chat_request_sse(
                payload=request_payload,
                request=FakeRequest(),
                trace_id="trace-snapshot" if not request_payload.confirm_no_snapshot else "trace-confirmed",
                session_id="session-snapshot",
                cancellation_token=routes._CancellationToken(),
            )
        ]

    first = asyncio.run(collect(payload))
    error_packets = [data for name, data in first if name == "AgentError"]
    assert error_packets and error_packets[0]["code"] == "SNAPSHOT_FAILED"
    assert error_packets[0]["details"]["confirmNoSnapshotRequired"] is True
    assert model_calls == []
    assert coordinator.try_reserve() is True
    coordinator.release_reservation()

    confirmed = asyncio.run(collect(routes.AgentChatRequest(prompt="run", workspaceRoot=str(tmp_path), confirmNoSnapshot=True)))
    assert any(name == "TurnPhase" and data.get("noRestorePoint") is True for name, data in confirmed)
    assert any(name == "AgentCompleted" for name, _ in confirmed)
    assert trace_records and trace_records[-1]["noRestorePoint"] is True
    assert len(model_calls) == 1
    assert coordinator.try_reserve() is True
    coordinator.release_reservation()


def test_changed_file_diff_record_helpers(monkeypatch, tmp_path):
    inside = tmp_path / "inside.md"
    inside.write_text("text", encoding="utf-8")
    candidates = routes._normalize_changed_file_candidates(
        ["File written to inside.md (12 bytes)", str(inside), "../escape", "a.json extra", "{bad}", "inside.md"],
        workspace_root=tmp_path,
    )
    assert candidates == ["inside.md", "a.json"]
    assert routes._merge_changed_file_lists(["a\\b.md", ""], ["a/b.md", "c.md"]) == ["a/b.md", "c.md"]

    fake_git = types.SimpleNamespace(
        build_file_snapshot_diff=lambda root, paths, status: {
            "files": [{"relativePath": path, "added": 2, "removed": 0} for path in paths]
        }
    )
    monkeypatch.setattr(routes, "git_service", fake_git)
    data = routes._include_missing_agent_snapshot_diffs({"files": []}, tmp_path, ["inside.md", "missing.md", "../bad"])
    assert data["totals"] == {"files": 1, "added": 2, "removed": 0}
    assert routes._include_missing_agent_snapshot_diffs(data, tmp_path, ["inside.md"]) is data

    records = {
        ("trace", "other"): {"traceId": "trace", "sessionId": "other", "workspaceRoot": str(tmp_path)},
    }
    history = types.SimpleNamespace(
        read_record=lambda trace, session: records.get((trace, session)),
        list_session_summaries=lambda: [{"sessionId": ""}, {"sessionId": "default"}, {"sessionId": "other"}],
    )
    monkeypatch.setattr(routes, "trace_history_service", history)
    record, session = routes._read_agent_run_record("trace", "default")
    assert session == "other" and record is not None
    assert routes._read_agent_run_record("missing", "default") == (None, "default")
    monkeypatch.setattr(routes, "project_service", types.SimpleNamespace(workspace_root=tmp_path / "fallback"))
    assert routes._record_workspace_root(record) == tmp_path.resolve()
    assert routes._record_workspace_root({}) == tmp_path / "fallback"

    extracted = routes._record_change_ledger(
        {"changeLedger": {"changedFiles": ["a\\b.md"], "commitHash": "hash", "diffSource": "bad"}},
        trace_id="t", session_id="s",
    )
    assert extracted["changedFiles"] == ["a/b.md"] and extracted["diffSource"] == ""


def test_session_coomi_config_permission_approval_and_history_endpoints(monkeypatch, tmp_path):
    calls = []

    class CoomiService:
        def clear_session(self, *args, **kwargs):
            calls.append(("clear", args, kwargs))

        def get_status(self, **kwargs):
            return {"runtime": "coomi", "installed": True, "toolCount": 2, "contextWindow": 100}

        def read_config(self):
            return {"configPath": "config.json", "content": "{}", "parsed": {}, "updatedAt": "now"}

        def write_config(self, content):
            if content == "bad":
                raise ValueError("bad config")
            return {"configPath": "config.json", "content": content, "parsed": json.loads(content), "updatedAt": "now"}

        def list_models(self, **kwargs):
            if kwargs["base_url"] == "bad":
                raise ValueError("offline")
            return {"endpoint": "https://x.test/models", "models": ["m1"]}

        def set_permission_mode(self, mode):
            return {"permissionMode": mode, "permissionLabel": mode}

        def cycle_permission_mode(self):
            return {"permissionMode": "full_access", "permissionLabel": "Full"}

        def resolve_approval(self, approval_id, decision, response=None):
            return {"accepted": True, "approvalId": approval_id, "decision": decision, "response": response}

    service = CoomiService()
    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: service)
    monkeypatch.setattr(routes, "project_service", types.SimpleNamespace(workspace_root=tmp_path))
    intent = types.SimpleNamespace(clear_session=lambda **kwargs: calls.append(("intent", kwargs)))
    monkeypatch.setattr(routes, "storydex_intent_service", intent)
    history = types.SimpleNamespace(
        list_session_summaries=lambda: [{"sessionId": "s", "firstPrompt": "p", "traceCount": 1}],
        delete_session=lambda session: {"sessionId": session, "removedCount": 2},
        list_records=lambda **kwargs: [{"traceId": "t"}],
    )
    monkeypatch.setattr(routes, "trace_history_service", history)

    assert routes.agent_sessions(FakeRequest()).data["items"][0]["sessionId"] == "s"
    assert routes.agent_delete_session("s", FakeRequest()).data["removedCount"] == 2
    assert routes.agent_delete_session_by_body(routes.AgentSessionDeleteRequest(sessionId="s2"), FakeRequest()).data["sessionId"] == "s2"
    assert routes.agent_coomi_status(FakeRequest()).data["installed"] is True
    assert routes.agent_read_coomi_config(FakeRequest()).data["configPath"] == "config.json"
    assert routes.agent_update_coomi_config(routes.AgentCoomiConfigUpdateRequest(content="{}"), FakeRequest()).data["parsed"] == {}
    with pytest.raises(routes.StorydexError) as config_error:
        routes.agent_update_coomi_config(routes.AgentCoomiConfigUpdateRequest(content="bad"), FakeRequest())
    assert config_error.value.code == "coomi_config_invalid"
    assert routes.agent_list_coomi_models(routes.AgentCoomiModelListRequest(baseUrl="https://x.test/v1", apiKey="k"), FakeRequest()).data["models"] == ["m1"]
    with pytest.raises(routes.StorydexError) as model_error:
        routes.agent_list_coomi_models(routes.AgentCoomiModelListRequest(baseUrl="bad", apiKey="k"), FakeRequest())
    assert model_error.value.code == "coomi_models_unavailable"
    assert routes.agent_set_coomi_permission(routes.AgentPermissionModeRequest(permissionMode="ask_approval"), FakeRequest()).data["permissionMode"] == "ask_approval"
    assert routes.agent_cycle_coomi_permission(FakeRequest()).data["permissionMode"] == "full_access"
    assert routes.agent_resolve_coomi_approval(routes.AgentApprovalRequest(approvalId="a", decision="allow"), FakeRequest()).data["accepted"] is True
    assert routes.agent_history(FakeRequest(), limit=10, session_id_query="s").data["items"][0]["traceId"] == "t"


def test_commit_decision_all_modes_and_validation(monkeypatch, tmp_path):
    record = {"prompt": "continue chapter", "workspaceRoot": str(tmp_path), "changeLedger": {"changedFiles": ["a.md"], "added": 1}}
    monkeypatch.setattr(routes, "_read_agent_run_record", lambda trace, session: (record, "s"))
    monkeypatch.setattr(routes, "_record_workspace_root", lambda value: tmp_path)
    monkeypatch.setattr(routes, "_append_git_commit_decision_record", lambda **kwargs: None)
    monkeypatch.setattr(routes, "_build_commit_message_diff_summary", lambda *args, **kwargs: "diff")

    class Git:
        def acknowledge_skip(self, root, **kwargs):
            return {"status": "info", "reason": "user_skipped", "changedFiles": kwargs["changed_files"], "changedFileCount": 1}

        def current_changes_payload(self, root, **kwargs):
            return {"status": "info", "reason": "pending", "changedFiles": ["a.md"], "changedFileCount": 1}

        def commit_current_changes(self, root, *, message):
            return {"status": "success", "created": True, "reason": "committed", "changedFiles": ["a.md"], "changedFileCount": 1, "message": message}

        def _commit_message_for_prompt(self, prompt):
            return "fallback"

    git = Git()
    monkeypatch.setattr(routes, "agent_git_autocommit_service", git)

    class Coomi:
        async def generate_commit_message(self, **kwargs):
            return "generated"

    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: Coomi())
    request = FakeRequest()

    with pytest.raises(routes.StorydexError) as invalid:
        asyncio.run(routes.agent_run_commit_decision("t", routes.AgentCommitDecisionRequest(mode="bad"), request))
    assert invalid.value.code == "invalid_agent_commit_decision"
    skipped = asyncio.run(routes.agent_run_commit_decision("t", routes.AgentCommitDecisionRequest(mode="skip"), request))
    assert skipped.data["reason"] == "user_skipped"
    with pytest.raises(routes.StorydexError) as missing:
        asyncio.run(routes.agent_run_commit_decision("t", routes.AgentCommitDecisionRequest(mode="manual"), request))
    assert missing.value.code == "commit_message_required"
    manual = asyncio.run(routes.agent_run_commit_decision("t", routes.AgentCommitDecisionRequest(mode="manual", message="manual"), request))
    assert manual.data["message"] == "manual"
    auto = asyncio.run(routes.agent_run_commit_decision("t", routes.AgentCommitDecisionRequest(mode="auto"), request))
    assert auto.data["generatedMessage"] is True and auto.data["commitMessageStrategy"] == "llm"

    git.current_changes_payload = lambda *args, **kwargs: {"status": "error", "changedFiles": []}
    error = asyncio.run(routes.agent_run_commit_decision("t", routes.AgentCommitDecisionRequest(mode="auto"), request))
    assert error.data["status"] == "error"


def test_run_diff_empty_summary_commit_summary_and_record_append(monkeypatch, tmp_path):
    summary_git = types.SimpleNamespace(read_summary=lambda root: {"available": True, "gitInstalled": True, "initialized": True, "branch": "develop"})
    monkeypatch.setattr(routes, "git_service", summary_git)
    empty = routes._empty_agent_run_diff_payload(tmp_path, message="none", trace_id="t", session_id="s")
    assert empty["branch"] == "develop"
    summary_git.read_summary = lambda root: (_ for _ in ()).throw(OSError())
    assert routes._empty_agent_run_diff_payload(tmp_path, message="none", trace_id="t", session_id="s")["available"] is False

    diff = {
        "totals": {"files": 1, "added": 2, "removed": 1},
        "files": [None, {"relativePath": "a.md", "status": "M", "added": 2, "removed": 1, "hunks": [
            None, {"lines": [None, {"kind": "context", "content": "same"}, {"kind": "added", "content": "new"}, {"kind": "removed", "content": "old"}]}
        ]}],
    }
    summary_git.read_diff = lambda *args, **kwargs: diff
    text = routes._build_commit_message_diff_summary(tmp_path, ["a.md"])
    assert "+ new" in text and "- old" in text
    assert len(routes._build_commit_message_diff_summary(tmp_path, ["a.md"], max_chars=10)) == 10
    summary_git.read_diff = lambda *args, **kwargs: (_ for _ in ()).throw(GitServiceError("bad"))
    assert routes._build_commit_message_diff_summary(tmp_path, ["a.md"]) == ""

    record = {"traceId": "t", "events": []}
    saved = []
    monkeypatch.setattr(routes, "_read_agent_run_record", lambda trace, session: (record, "s"))
    monkeypatch.setattr(routes, "trace_history_service", types.SimpleNamespace(upsert_record=lambda value, session: saved.append((value, session))))
    routes._append_git_commit_decision_record(trace_id="t", session_id="s", payload={"_type": "GitCommitResult", "created": True})
    assert saved[0][0]["status"] == "committed"
    monkeypatch.setattr(routes, "_read_agent_run_record", lambda trace, session: (None, session))
    routes._append_git_commit_decision_record(trace_id="t", session_id="s", payload={})


def test_agent_clear_conversation_and_chat_paths(monkeypatch, tmp_path):
    calls = []
    reset_calls = []
    service = types.SimpleNamespace(clear_session=lambda *args, **kwargs: calls.append(("clear", args, kwargs)))
    monkeypatch.setattr(routes, "get_storydex_coomi_agent_service", lambda: service)
    monkeypatch.setattr(routes, "project_service", types.SimpleNamespace(workspace_root=tmp_path))
    monkeypatch.setattr(routes, "storydex_intent_service", types.SimpleNamespace(
        clear_session=lambda **kwargs: calls.append(("intent_clear", kwargs)),
        classify_intent=lambda **kwargs: asyncio.sleep(0, result={"primary": "general", "method": "fake"}),
    ))
    history = types.SimpleNamespace(
        clear_records=lambda session: 3,
        mark_session_cleared=lambda session: calls.append(("marked", session)),
        upsert_record=lambda record, session: calls.append(("record", session)),
    )
    monkeypatch.setattr(routes, "trace_history_service", history)
    monkeypatch.setattr(routes, "reset_llm_metrics", lambda trace_id=None: reset_calls.append(("reset", trace_id)))

    cleared = routes.agent_clear_conversation(FakeRequest({"x-session-id": "header"}), session_id_query="query")
    assert cleared.data["historyClearedCount"] == 3 and cleared.data["sessionId"] == "query"

    monkeypatch.setattr(routes, "_try_acquire_agent_generation_slot", lambda: False)
    with pytest.raises(routes.StorydexError) as busy:
        asyncio.run(routes.agent_chat(routes.AgentChatRequest(prompt="p"), FakeRequest()))
    assert busy.value.code == "agent_busy"

    monkeypatch.setattr(routes, "_try_acquire_agent_generation_slot", lambda: True)
    monkeypatch.setattr(routes, "_release_agent_generation_slot", lambda: calls.append(("released",)))
    monkeypatch.setattr(routes, "_resolve_agent_workspace_root", lambda payload: tmp_path)
    snapshot = AgentGitSnapshot(workspace_root=tmp_path, available=True)

    class Git:
        def begin_turn(self, root):
            calls.append(("begin", root))
            return snapshot

        def finish_turn(self, snap, **kwargs):
            calls.append(("finish", kwargs))
            return {"_type": "GitAutoCommit", "status": "info", "created": False, "reason": "no_changes", "message": "none"}

    monkeypatch.setattr(routes, "agent_git_autocommit_service", Git())
    monkeypatch.setattr(routes, "_agent_commit_prompt_enabled", lambda root: True)
    monkeypatch.setattr(routes, "_create_agent_task_plan", lambda **kwargs: asyncio.sleep(0, result=[{"title": "Inspect"}, {"title": "Git commit"}]))
    def build_chat_payload(**kwargs):
        reset_calls.append(("build", kwargs["trace_id"]))
        return {
            "data": {"route": "coomi", "reply": kwargs["reply"], "events": [], "assistant": {}},
            "trace": {"traceId": kwargs["trace_id"], "durationMs": 1, "toolCalls": 0},
            "audit": [], "record": {"traceId": kwargs["trace_id"]},
        }

    monkeypatch.setattr(routes, "_build_chat_payload", build_chat_payload)

    orchestration = types.SimpleNamespace(build_turn_contract=lambda *args, **kwargs: {
        "status": "needs_user_input", "requiredQuestions": [{"message": "choose"}], "turnPlan": {}
    })
    monkeypatch.setattr(routes, "storydex_orchestration_service", orchestration)
    reset_calls.clear()
    response = asyncio.run(routes.agent_chat(routes.AgentChatRequest(prompt="p"), FakeRequest(), session_id_query="s"))
    assert response.data["reply"] == "choose"
    assert [item[0] for item in reset_calls] == ["reset", "build", "reset"]
    assert len({item[1] for item in reset_calls}) == 1

    orchestration.build_turn_contract = lambda *args, **kwargs: {"status": "ready", "turnPlan": {}}

    async def stream_events(**kwargs):
        yield "TextChunk", {"content": "reply"}
        yield "ToolDone", {"tool_name": "Read"}
        yield "AgentCompleted", {"total_tokens": 1}

    service.stream_events = stream_events
    reset_calls.clear()
    response = asyncio.run(routes.agent_chat(routes.AgentChatRequest(prompt="p"), FakeRequest(), session_id_query="s"))
    assert response.data["reply"] == "reply"
    assert [item[0] for item in reset_calls] == ["reset", "build", "reset"]
    assert len({item[1] for item in reset_calls}) == 1

    broken_intent = types.SimpleNamespace(classify_intent=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("intent failed")))
    monkeypatch.setattr(routes, "storydex_intent_service", broken_intent)
    reset_calls.clear()
    with pytest.raises(RuntimeError):
        asyncio.run(routes.agent_chat(routes.AgentChatRequest(prompt="p"), FakeRequest(), session_id_query="s"))
    assert [item[0] for item in reset_calls] == ["reset", "reset"]
    assert len({item[1] for item in reset_calls}) == 1
    assert routes.execution_coordinator.try_reserve() is True
    routes.execution_coordinator.release_reservation()


def test_stream_request_wrapper_success_error_and_chat_stream(monkeypatch, tmp_path):
    reset_calls = []
    monkeypatch.setattr(routes, "reset_llm_metrics", lambda trace_id=None: reset_calls.append(("reset", trace_id)))
    monkeypatch.setattr(routes, "_release_agent_generation_slot", lambda: None)
    monkeypatch.setattr(routes, "_resolve_agent_workspace_root", lambda payload: tmp_path)
    monkeypatch.setattr(routes, "_normalize_story_generation_options", lambda value: {})
    snapshot = AgentGitSnapshot(workspace_root=tmp_path, available=True)
    git = types.SimpleNamespace(begin_turn=lambda root: snapshot, finish_turn=lambda *args, **kwargs: {})
    monkeypatch.setattr(routes, "agent_git_autocommit_service", git)
    intent = types.SimpleNamespace(classify_intent=lambda **kwargs: asyncio.sleep(0, result={"primary": "general", "method": "fake"}))
    monkeypatch.setattr(routes, "storydex_intent_service", intent)
    monkeypatch.setattr(routes, "storydex_orchestration_service", types.SimpleNamespace(build_turn_contract=lambda *args, **kwargs: {
        "status": "ready", "contextAssembly": {"budget": {"blockCount": 2}}, "turnPlan": {}
    }))

    async def delegated(**kwargs):
        yield routes._encode_sse("TextChunk", {"content": "hello"})
        yield routes._encode_sse("done", {"type": "done"})
        kwargs["execution_handle"].reject_preflight("test_delegate_complete")
        routes.reset_llm_metrics(kwargs["trace_id"])

    monkeypatch.setattr(routes, "_stream_coomi_sse", delegated)
    token = routes._CancellationToken()

    async def collect():
        return [_decode_sse(item) async for item in routes._stream_agent_chat_request_sse(
            payload=routes.AgentChatRequest(prompt="p"), request=FakeRequest(), trace_id="t", session_id="s", cancellation_token=token
        )]

    events = asyncio.run(collect())
    names = [name for name, _ in events]
    assert names[0] == "RunAccepted" and "TextChunk" in names and names[-1] == "done"
    assert reset_calls == [("reset", "t"), ("reset", "t")]

    reset_calls.clear()
    intent.classify_intent = lambda **kwargs: asyncio.sleep(0, result=(_ for _ in ()).throw(RuntimeError("bad")))
    failed = asyncio.run(collect())
    assert [name for name, _ in failed][-2:] == ["AgentError", "done"]
    assert reset_calls == [("reset", "t"), ("reset", "t")]

    monkeypatch.setattr(routes, "_try_acquire_agent_generation_slot", lambda: False)
    with pytest.raises(routes.StorydexError):
        asyncio.run(routes.agent_chat_stream(routes.AgentChatRequest(prompt="p"), FakeRequest()))
    monkeypatch.setattr(routes, "_try_acquire_agent_generation_slot", lambda: True)
    response = asyncio.run(routes.agent_chat_stream(routes.AgentChatRequest(prompt="p"), FakeRequest()))
    assert response.media_type == "text/event-stream"


def test_agent_run_diff_missing_present_commit_worktree_and_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(routes, "project_service", types.SimpleNamespace(workspace_root=tmp_path))
    monkeypatch.setattr(routes, "_include_missing_agent_snapshot_diffs", lambda data, root, files: data)

    class Git:
        def read_summary(self, root):
            return {"available": True, "gitInstalled": True, "initialized": True, "branch": "develop"}

        def read_diff(self, root, *, paths):
            return {"files": [{"relativePath": paths[0], "added": 3, "removed": 1}], "totals": {"files": 1, "added": 3, "removed": 1}}

        def read_commit_diff(self, root, *, commit_id, paths):
            return {"files": [{"relativePath": (paths or ["commit.md"])[0], "added": 2, "removed": 0}], "totals": {"files": 1, "added": 2, "removed": 0}}

    git = Git()
    monkeypatch.setattr(routes, "git_service", git)
    request = FakeRequest({"x-session-id": "s"})

    monkeypatch.setattr(routes, "_read_agent_run_record", lambda trace, session: (None, session))
    empty = routes.agent_run_diff("missing", request, session_id_query=None, changed_files_query=None, commit_hash_query=None)
    assert empty.data["files"] == [] and empty.data["message"]
    worktree = routes.agent_run_diff("missing", request, session_id_query=None, changed_files_query="a.md\nb.md", commit_hash_query=None)
    assert worktree.data["diffSource"] == "working_tree" and worktree.data["added"] == 3
    commit = routes.agent_run_diff("missing", request, session_id_query=None, changed_files_query="a.md", commit_hash_query="abcdef")
    assert commit.data["diffSource"] == "commit" and commit.data["commitHash"] == "abcdef"

    original_read_diff = git.read_diff
    git.read_diff = lambda *args, **kwargs: (_ for _ in ()).throw(GitServiceError("diff failed"))
    failed_fallback = routes.agent_run_diff("missing", request, session_id_query=None, changed_files_query="a.md", commit_hash_query=None)
    assert failed_fallback.data["error"]["code"] == "git_service_error"
    git.read_diff = original_read_diff

    record = {
        "workspaceRoot": str(tmp_path),
        "changeLedger": {
            "changedFiles": ["ledger.md"], "changedFileCount": 1, "added": 7, "removed": 2,
            "diffSource": "working_tree", "commitHash": "", "shortHash": "",
        },
    }
    monkeypatch.setattr(routes, "_read_agent_run_record", lambda trace, session: (record, "resolved"))
    present = routes.agent_run_diff("trace", request, session_id_query=None, changed_files_query="fallback.md", commit_hash_query=None)
    assert present.data["changedFiles"] == ["ledger.md", "fallback.md"]
    assert present.data["added"] == 7 and present.data["sessionId"] == "resolved"

    record["changeLedger"]["commitHash"] = "commit-hash"
    record["changeLedger"]["diffSource"] = "commit"
    committed = routes.agent_run_diff("trace", request, session_id_query=None, changed_files_query=None, commit_hash_query=None)
    assert committed.data["commitHash"] == "commit-hash" and committed.data["diffSource"] == "commit"

    record["changeLedger"] = {"changedFiles": []}
    no_files = routes.agent_run_diff("trace", request, session_id_query=None, changed_files_query=None, commit_hash_query=None)
    assert no_files.data["files"] == []

    record["changeLedger"] = {"changedFiles": ["ledger.md"]}
    git.read_diff = lambda *args, **kwargs: (_ for _ in ()).throw(GitServiceError("diff failed"))
    failed_present = routes.agent_run_diff("trace", request, session_id_query=None, changed_files_query=None, commit_hash_query=None)
    assert failed_present.data["error"]["message"] == "diff failed"
