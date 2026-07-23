"""Storydex 意图识别服务的回归测试。

覆盖：
1. LLM 结构化分类正常路径（JSON 解析、标签校验、置信度归一）。
2. LLM 异常 / 超时 / 非法输出时回退关键词启发式。
3. 确定性短路（slash 命令、空输入）。
4. orchestration 注入 intent_frame 与无效注入的兜底。
5. 项目语义接地：意图目录合并 skill registry、assetTargets/matchedSkills
   富化、自定义 intent 标签、会话上一轮记忆。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from contextlib import contextmanager

from services.llm_replay import get_llm_metrics, llm_trace, reset_llm_metrics
from services.storydex_intent_service import (
    StorydexIntentService,
    _BoundedIntentProvider,
    _extract_json_object,
    _intent_messages,
    _parse_intent_frame,
    build_intent_catalog,
    heuristic_intent_frame,
    is_advisory_request,
    is_valid_intent_frame,
)
from services.story_project_service import get_story_project_service
from services.storydex_orchestration_service import get_storydex_orchestration_service


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


def _install_fake_provider(monkeypatch, provider) -> None:
    fake_services = types.ModuleType("coomi.services")
    fake_services.get_llm_provider = lambda: provider
    monkeypatch.setitem(sys.modules, "coomi", types.ModuleType("coomi"))
    monkeypatch.setitem(sys.modules, "coomi.services", fake_services)

    @contextmanager
    def fake_home():
        yield

    import services.coomi_agent_service as coomi_agent_service

    monkeypatch.setattr(coomi_agent_service, "_storydex_coomi_home", fake_home)


# ─────────────────── 1. LLM 正常路径 ───────────────────


def test_classify_intent_uses_llm_structured_output(monkeypatch):
    class FakeProvider:
        async def chat(self, messages, options):
            assert options is None
            assert messages[0]["role"] == "system"
            assert "worldbook_work" in messages[0]["content"]
            return _FakeResponse('{"primary": "worldbook_work", "confidence": "high", "reason": "设计世界观条目"}')

    _install_fake_provider(monkeypatch, FakeProvider())
    service = StorydexIntentService()
    reset_llm_metrics("intent-test")
    with llm_trace("intent-test"):
        frame = asyncio.run(service.classify_intent(prompt="帮我完善一下大陆的魔法体系设定", active_file=""))
    assert frame["primary"] == "worldbook_work"
    assert frame["confidence"] == "high"
    assert frame["method"] == "llm"
    assert frame["signals"] == ["llm_classifier"]
    assert get_llm_metrics("intent-test")["llmCalls"][0]["purpose"] == "intent"


def test_intent_metadata_provider_uses_short_low_reasoning_chat_request():
    captured = {}

    class Completions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(
                            content='{"primary":"general","secondary":"","confidence":"high","reason":"advisory"}',
                            reasoning_content="",
                        )
                    )
                ]
            )

    provider = types.SimpleNamespace(
        config=types.SimpleNamespace(type="openai_compatible"),
        model="gpt-5-mini",
        client=types.SimpleNamespace(chat=types.SimpleNamespace(completions=Completions())),
    )
    response = asyncio.run(
        _BoundedIntentProvider(provider).chat(
            [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "review"}]
        )
    )

    assert captured["response_format"] == {"type": "json_object"}
    assert captured["max_completion_tokens"] == 160
    assert captured["reasoning_effort"] == "low"
    assert "temperature" not in captured
    assert getattr(response, "content")


def test_intent_metadata_provider_uses_strict_openai_responses_schema():
    captured = {}

    class Responses:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(
                output_text='{"primary":"general","secondary":"","confidence":"high","reason":"advisory"}'
            )

    class Provider:
        config = types.SimpleNamespace(type="openai_responses")
        model = "gpt-5-mini"
        client = types.SimpleNamespace(responses=Responses())

        @staticmethod
        def _build_params(messages, tools, *, stream=False):
            assert tools is None
            assert stream is False
            return {"model": "gpt-5-mini", "instructions": messages[0]["content"], "input": messages[1:]}

    response = asyncio.run(
        _BoundedIntentProvider(Provider()).chat(
            [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "review"}]
        )
    )

    response_format = captured["text"]["format"]
    assert response_format["type"] == "json_schema"
    assert response_format["name"] == "storydex_intent"
    assert response_format["strict"] is True
    assert response_format["schema"]["additionalProperties"] is False
    assert captured["max_output_tokens"] == 160
    assert captured["reasoning"] == {"effort": "low"}
    assert captured["store"] is False
    assert "temperature" not in captured
    assert getattr(response, "content")


def test_advisory_rule_short_circuits_catalog_history_and_llm_under_100ms(monkeypatch):
    service = StorydexIntentService()
    monkeypatch.setattr(service, "_catalog", lambda root: (_ for _ in ()).throw(AssertionError("catalog must not load")))

    async def forbidden_llm(**kwargs):
        raise AssertionError("advisory requests must not call the intent model")

    monkeypatch.setattr(service, "_llm_intent_frame", forbidden_llm)
    started = time.perf_counter()
    frame = asyncio.run(
        service.classify_intent(
            prompt="这段写得怎么样？",
            active_file="chapters/001.md",
            session_id="session-advisory",
        )
    )
    elapsed = time.perf_counter() - started

    assert frame["primary"] == "general"
    assert frame["method"] == "advisory_fast"
    assert elapsed < 0.1
    assert is_advisory_request("这段写得怎么样？") is True
    assert is_advisory_request("请修改这段文字并保存") is False


def test_parse_intent_frame_accepts_fenced_json_and_normalizes_confidence():
    labels = set(build_intent_catalog())
    frame = _parse_intent_frame(
        '```json\n{"primary": "story_generation", "confidence": "certain"}\n```',
        valid_labels=labels,
    )
    assert frame is not None
    assert frame["primary"] == "story_generation"
    assert frame["confidence"] == "medium"


def test_parse_intent_frame_rejects_unknown_label_and_bad_json():
    labels = set(build_intent_catalog())
    assert _parse_intent_frame('{"primary": "hack_the_planet", "confidence": "high"}', valid_labels=labels) is None
    assert _parse_intent_frame("not json at all", valid_labels=labels) is None
    assert _parse_intent_frame("", valid_labels=labels) is None


# ─────────────────── 2. 兜底路径 ───────────────────


def test_classify_intent_uses_fast_heuristic_for_clear_signal(monkeypatch):
    calls = 0

    class BrokenProvider:
        async def chat(self, messages, options):
            nonlocal calls
            calls += 1
            raise RuntimeError("provider offline")

    _install_fake_provider(monkeypatch, BrokenProvider())
    service = StorydexIntentService()
    frame = asyncio.run(service.classify_intent(prompt="帮我整理知识图谱", active_file=""))
    assert frame["primary"] == "wiki_work"
    assert frame["method"] == "heuristic_fast"
    assert calls == 0


def test_classify_intent_falls_back_on_timeout(monkeypatch):
    class SlowProvider:
        async def chat(self, messages, options):
            await asyncio.sleep(0.2)
            return _FakeResponse('{"primary": "wiki_work", "confidence": "high"}')

    _install_fake_provider(monkeypatch, SlowProvider())
    service = StorydexIntentService(llm_timeout_seconds=0.01)
    frame = asyncio.run(service.classify_intent(prompt="帮我处理一下这个", active_file="chapters/001.md"))
    assert frame["primary"] == "story_generation"
    assert frame["method"] == "heuristic_fallback"


def test_classify_intent_falls_back_on_invalid_llm_output(monkeypatch):
    class NoisyProvider:
        async def chat(self, messages, options):
            return _FakeResponse("好的，我认为这是一个角色设计请求。")

    _install_fake_provider(monkeypatch, NoisyProvider())
    service = StorydexIntentService()
    frame = asyncio.run(service.classify_intent(prompt="帮我处理一下这个", active_file=""))
    assert frame["primary"] == "general"
    assert frame["method"] == "heuristic_fallback"


# ─────────────────── 3. 确定性短路 ───────────────────


def test_classify_intent_short_circuits_slash_commands_and_empty_prompt():
    service = StorydexIntentService()
    frame = asyncio.run(service.classify_intent(prompt="/plan 下一章", active_file=""))
    assert frame["method"] == "deterministic"
    empty = asyncio.run(service.classify_intent(prompt="   ", active_file="chapters/第一章/001.md"))
    assert empty["method"] == "deterministic"
    assert empty["primary"] == "story_generation"


def test_heuristic_frame_covers_worldbook_and_script_intents():
    assert heuristic_intent_frame(prompt="更新世界书里的王国设定", active_file="")["primary"] == "worldbook_work"
    assert heuristic_intent_frame(prompt="设计一份剧本大纲", active_file="")["primary"] == "script_work"


# ─────────────────── 4. orchestration 注入 ───────────────────


def test_build_turn_contract_uses_injected_intent_frame(tmp_path):
    orchestration = get_storydex_orchestration_service()
    contract = orchestration.build_turn_contract(
        tmp_path,
        prompt="随便聊聊",
        intent_frame={
            "primary": "character_work",
            "confidence": "high",
            "signals": ["llm_classifier"],
            "method": "llm",
        },
    )
    intent = contract["intentFrame"]
    assert intent["primary"] == "character_work"
    assert intent["confidence"] == "high"
    assert intent["existingChapterCount"] == 0


def test_build_turn_contract_ignores_invalid_injected_frame(tmp_path):
    orchestration = get_storydex_orchestration_service()
    contract = orchestration.build_turn_contract(
        tmp_path,
        prompt="续写一段剧情",
        intent_frame={"primary": "not_a_label"},
    )
    intent = contract["intentFrame"]
    assert intent["primary"] == "story_generation"
    assert "story_keywords" in intent["signals"]


def test_build_turn_contract_without_frame_keeps_heuristic_behavior(tmp_path):
    orchestration = get_storydex_orchestration_service()
    contract = orchestration.build_turn_contract(tmp_path, prompt="继续写一段剧情")
    assert contract["intentFrame"]["primary"] == "story_generation"


# ─────────────────── 5. 项目语义接地 ───────────────────


def test_intent_catalog_merges_default_skill_registry(tmp_path):
    catalog = build_intent_catalog(workspace_root=tmp_path)
    character = catalog["character_work"]
    assert ".storydex/characters/" in character["assetTargets"]
    assert "设计角色" in character["skills"]
    assert "角色更新" in character["skills"]
    wiki = catalog["wiki_work"]
    assert ".storydex/wiki/" in wiki["assetTargets"]
    assert "WIKI整理" in wiki["skills"]


def test_classify_intent_enriches_frame_with_asset_targets_and_skills(monkeypatch, tmp_path):
    class FakeProvider:
        async def chat(self, messages, options):
            assert ".storydex/characters/" in messages[0]["content"]
            return _FakeResponse('{"primary": "character_work", "confidence": "high", "reason": "角色设定"}')

    _install_fake_provider(monkeypatch, FakeProvider())
    service = StorydexIntentService()
    frame = asyncio.run(
        service.classify_intent(prompt="设计一个反派角色", active_file="", workspace_root=tmp_path)
    )
    assert frame["primary"] == "character_work"
    assert frame["assetTargets"] == [".storydex/characters/"]
    assert "设计角色" in frame["matchedSkills"]


def test_classify_intent_accepts_custom_registry_intent(monkeypatch, tmp_path):
    registry_path = get_story_project_service().agent_root(tmp_path) / "skills" / "registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "skills": [
                    {"id": "poetry", "name": "写诗", "intent": "poetry_work", "assetTargets": [".storydex/poetry/"]}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class FakeProvider:
        async def chat(self, messages, options):
            assert "poetry_work" in messages[0]["content"]
            return _FakeResponse('{"primary": "poetry_work", "confidence": "high", "reason": "写诗请求"}')

    _install_fake_provider(monkeypatch, FakeProvider())
    service = StorydexIntentService()
    frame = asyncio.run(service.classify_intent(prompt="给男主写一首出场诗", active_file="", workspace_root=tmp_path))
    assert frame["primary"] == "poetry_work"
    assert frame["assetTargets"] == [".storydex/poetry/"]
    assert frame["matchedSkills"] == ["写诗"]

    orchestration = get_storydex_orchestration_service()
    contract = orchestration.build_turn_contract(tmp_path, prompt="给男主写一首出场诗", intent_frame=frame)
    assert contract["intentFrame"]["primary"] == "poetry_work"


def test_classify_intent_passes_previous_turn_context(monkeypatch):
    calls = 0

    class FakeProvider:
        async def chat(self, messages, options):
            nonlocal calls
            calls += 1
            return _FakeResponse('{"primary": "general", "confidence": "medium"}')

    _install_fake_provider(monkeypatch, FakeProvider())
    service = StorydexIntentService()
    asyncio.run(service.classify_intent(prompt="设计一个新角色", session_id="s1"))
    frame = asyncio.run(service.classify_intent(prompt="继续", session_id="s1"))
    assert frame["primary"] == "character_work"
    assert frame["method"] == "deterministic_context"
    assert calls == 0


def test_persisted_assistant_action_survives_service_restart(monkeypatch, tmp_path):
    class PersistedTraceHistory:
        def list_records(self, *, session_id, limit):
            assert session_id == "restored-session"
            return [
                {
                    "prompt": "这是一个什么故事",
                    "reply": "变量更新已经完成，是否需要我继续执行变量整理？",
                    "workspaceRoot": str(tmp_path),
                    "audit": [
                        {
                            "action": "storydex_turn_contract",
                            "intent": "story_generation",
                        }
                    ],
                }
            ]

    import services.trace_history_service as trace_history_module

    monkeypatch.setattr(trace_history_module, "get_trace_history_service", lambda: PersistedTraceHistory())

    restarted_service = StorydexIntentService()
    frame = asyncio.run(
        restarted_service.classify_intent(
            prompt="执行",
            active_file="chapters/第三章/001.md",
            workspace_root=tmp_path,
            session_id="restored-session",
        )
    )

    assert frame["primary"] == "general"
    assert frame["method"] == "deterministic_context"
    assert "persistent_previous_turn" in frame["signals"]


def test_in_memory_previous_turn_is_isolated_by_workspace(tmp_path):
    service = StorydexIntentService()
    first_workspace = tmp_path / "one"
    second_workspace = tmp_path / "two"
    first_workspace.mkdir()
    second_workspace.mkdir()

    asyncio.run(
        service.classify_intent(
            prompt="设计一个新角色",
            workspace_root=first_workspace,
            session_id="same-id",
        )
    )
    frame = asyncio.run(
        service.classify_intent(
            prompt="继续",
            workspace_root=second_workspace,
            session_id="same-id",
        )
    )

    assert frame["primary"] != "character_work"


def test_catalog_tolerates_registry_failures_and_invalid_entries(tmp_path):
    class BrokenProjects:
        def read_agent_skill_registry(self, root):
            raise OSError("registry unavailable")

    fallback = build_intent_catalog(workspace_root=tmp_path, story_project_service=BrokenProjects())
    assert set(fallback) >= {"general", "story_generation"}

    class InvalidProjects:
        def read_agent_skill_registry(self, root):
            return {
                "skills": [
                    None,
                    "bad",
                    {},
                    {"name": "bad slug", "intent": "INVALID-LABEL"},
                    {"id": "minimal", "intent": "custom_work", "assetTargets": "not-a-list"},
                    {"name": "minimal", "intent": "custom_work", "assetTargets": ["", "assets/", "assets/"]},
                ]
            }

    catalog = build_intent_catalog(workspace_root=tmp_path, story_project_service=InvalidProjects())
    assert catalog["custom_work"]["skills"] == ["minimal"]
    assert catalog["custom_work"]["assetTargets"] == ["assets/"]


def test_heuristics_validation_json_and_message_helpers_cover_edge_cases():
    assert heuristic_intent_frame(prompt="设计角色关系", active_file="")["primary"] == "character_work"
    assert heuristic_intent_frame(prompt="整理项目目录", active_file="")["primary"] == "project_organization"
    assert heuristic_intent_frame(prompt="普通问题", active_file="chapters/001.md")["primary"] == "story_generation"
    assert heuristic_intent_frame(prompt="普通问题", active_file="notes/chapter.md")["primary"] == "general"

    assert is_valid_intent_frame(None) is False
    assert is_valid_intent_frame({"primary": "BAD", "method": "llm"}) is False
    assert is_valid_intent_frame({"primary": "general", "method": ""}) is False
    assert is_valid_intent_frame({"primary": "general", "method": "llm"}) is True

    assert _extract_json_object('prefix {"primary":"general"} suffix') == {"primary": "general"}
    assert _extract_json_object("prefix {broken} suffix") is None
    assert _extract_json_object("no braces") is None
    frame = _parse_intent_frame(
        '{"primary":"general","secondary":"wiki_work","confidence":"low","reason":"ok"}',
        valid_labels=set(build_intent_catalog()),
    )
    assert frame["secondary"] == "wiki_work"
    same = _parse_intent_frame(
        '{"primary":"general","secondary":"general"}',
        valid_labels=set(build_intent_catalog()),
    )
    assert "secondary" not in same

    messages = _intent_messages(
        prompt="x" * 3000,
        active_file="chapters/1.md",
        catalog={"general": {"description": "chat", "assetTargets": [], "skills": [], "examples": []}},
        previous_turn={"intent": "general"},
    )
    request = json.loads(messages[1]["content"])
    assert len(request["prompt"]) == 2000
    assert request["activeFileIsChapter"] is True


def test_service_catalog_fallback_clear_session_and_memory_bound(monkeypatch, tmp_path):
    import services.storydex_intent_service as intent_module

    service = StorydexIntentService(story_project_service=object())
    monkeypatch.setattr(intent_module, "build_intent_catalog", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("bad")) if kwargs else {"general": {"assetTargets": [], "skills": []}})
    assert set(service._catalog(tmp_path)) == {"general"}

    service = StorydexIntentService()
    one = tmp_path / "one"
    two = tmp_path / "two"
    service._remember(session_key=f"{one.resolve()}::same", prompt="one", primary="general")
    service._remember(session_key=f"{two.resolve()}::same", prompt="two", primary="general")
    service.clear_session(session_id="same", workspace_root=one)
    assert f"{one.resolve()}::same" not in service._session_turns
    assert f"{two.resolve()}::same" in service._session_turns
    service.clear_session(session_id="same")
    assert not service._session_turns
    service.clear_session(session_id="")

    monkeypatch.setattr(intent_module, "_MAX_SESSION_MEMORY", 2)
    service._remember(session_key="a", prompt="a", primary="general")
    service._remember(session_key="b", prompt="b", primary="general")
    service._remember(session_key="c", prompt="c", primary="general")
    assert list(service._session_turns) == ["b", "c"]
    service._remember(session_key="", prompt="x", primary="general")


def test_persisted_turn_handles_errors_workspace_filter_and_event_intent(monkeypatch, tmp_path):
    import services.trace_history_service as trace_history_module

    class BrokenHistory:
        def list_records(self, **kwargs):
            raise OSError("broken")

    monkeypatch.setattr(trace_history_module, "get_trace_history_service", lambda: BrokenHistory())
    assert StorydexIntentService._load_persisted_turn(session_id="s", workspace_root=tmp_path) is None

    other = tmp_path / "other"
    other.mkdir()

    class EventHistory:
        def list_records(self, **kwargs):
            return [
                "bad",
                {"workspaceRoot": str(other), "prompt": "wrong workspace", "reply": "ignored"},
                {"workspaceRoot": "\x00", "prompt": "bad path", "reply": "ignored"},
                {"workspaceRoot": str(tmp_path), "prompt": "", "reply": "", "audit": [None]},
                {
                    "workspaceRoot": str(tmp_path),
                    "prompt": "continue",
                    "reply": "done",
                    "audit": [None, {"action": "other"}],
                    "events": [
                        None,
                        {"event": "Other"},
                        {"event": "TurnContract", "data": "bad"},
                        {"event": "TurnContract", "data": {"intentFrame": "bad"}},
                        {"event": "TurnContract", "data": {"intentFrame": {"primary": "wiki_work"}}},
                    ],
                },
            ]

    monkeypatch.setattr(trace_history_module, "get_trace_history_service", lambda: EventHistory())
    restored = StorydexIntentService._load_persisted_turn(session_id="s", workspace_root=tmp_path)
    assert restored["intent"] == "wiki_work"
    assert restored["pendingAction"] == ""


def test_provider_without_content_falls_back_and_follow_up_keeps_pending_fields(monkeypatch):
    class ContentlessProvider:
        async def chat(self, messages, options):
            return object()

    _install_fake_provider(monkeypatch, ContentlessProvider())
    service = StorydexIntentService()
    frame = asyncio.run(service.classify_intent(prompt="请帮我处理一下"))
    assert frame["method"] == "heuristic_fallback"

    service._remember(
        session_key="default::s",
        prompt="previous",
        primary="character_work",
        previous_turn={"assistantReply": "please continue", "pendingAction": "update character"},
    )
    remembered = service._session_turns["default::s"]
    assert remembered["assistantReply"] == "please continue"
    assert remembered["pendingAction"] == "update character"
