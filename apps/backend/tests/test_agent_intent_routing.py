import asyncio

from services.agent_intent_routing import (
    DIRECT,
    HYBRID,
    LEGACY,
    WORKFLOW,
    build_route_hints,
    normalize_routing_mode,
    should_invoke_intent_model,
)
from services.storydex_intent_service import StorydexIntentService


def test_route_hints_are_small_advisory_location_signals() -> None:
    hints = build_route_hints(
        prompt="修改沈月角色卡的核心动机，只改这个字段，不要动其他内容",
        active_file="",
        routing_mode=HYBRID,
    )
    assert hints["advisoryOnly"] is True
    assert hints["routingMode"] == HYBRID
    assert "沈月" in hints["namedEntities"]
    assert "核心动机" in hints["requestedFields"]
    assert "character" in hints["documentKinds"]
    assert "write" in hints["operationSignals"]
    assert "scope_exclusion" in hints["operationSignals"]
    assert hints["namedEntities"] == ["沈月"]
    assert hints["requestedFields"] == ["核心动机"]


def test_route_hints_strip_read_and_delete_verbs_from_entity_names() -> None:
    summary = build_route_hints(
        prompt="总结沈月角色卡",
        routing_mode=HYBRID,
    )
    deletion = build_route_hints(
        prompt="删除角色卡",
        routing_mode=HYBRID,
    )

    assert summary["namedEntities"] == ["沈月"]
    assert deletion["namedEntities"] == []
    assert "delete" in deletion["operationSignals"]


def test_route_hints_do_not_turn_strict_read_only_negations_into_write_signals() -> None:
    hints = build_route_hints(
        prompt=(
            "这是一次只读测试。只能调用 read_file 读取 "
            "chapters/lifecycle-baseline.md；不要使用其他工具，"
            "不要修改或写入任何项目文件。"
        ),
        routing_mode=HYBRID,
    )

    assert hints["operationSignals"] == ["read", "no_write"]


def test_route_hints_keep_local_no_write_constraints_scoped() -> None:
    hints = build_route_hints(
        prompt="修复 Agent 路由缺陷，不要修改 Provider 配置",
        routing_mode=HYBRID,
    )

    assert "write" in hints["operationSignals"]
    assert "scope_exclusion" in hints["operationSignals"]
    assert "no_write" not in hints["operationSignals"]


def test_route_hints_ignore_negated_delete_as_positive_delete() -> None:
    hints = build_route_hints(
        prompt="只读检查 chapters/one.md，不要删除任何文件",
        routing_mode=HYBRID,
    )

    assert "delete" not in hints["operationSignals"]
    assert "write" not in hints["operationSignals"]


def test_routing_mode_aliases_are_explicit_and_reversible() -> None:
    assert normalize_routing_mode("b") == DIRECT
    assert normalize_routing_mode("c_hybrid") == HYBRID
    assert normalize_routing_mode("d") == WORKFLOW
    assert normalize_routing_mode("unknown") == LEGACY


def test_b_c_d_model_invocation_matrix() -> None:
    frame = {
        "primary": "character_work",
        "confidence": "medium",
        "operationType": "modify_existing",
    }
    hints = build_route_hints(
        prompt="修改沈月角色卡核心动机",
        routing_mode=HYBRID,
    )
    common = {
        "prompt": "修改沈月角色卡核心动机",
        "heuristic_frame": frame,
        "route_hints": hints,
        "previous_turn": None,
        "has_custom_intents": False,
        "explicit_workflow": False,
        "workflow_confirmation": False,
    }
    assert should_invoke_intent_model(LEGACY, **common) is True
    assert should_invoke_intent_model(DIRECT, **common) is False
    assert should_invoke_intent_model(HYBRID, **common) is False
    assert should_invoke_intent_model(WORKFLOW, **common) is False
    assert (
        should_invoke_intent_model(
            WORKFLOW,
            **{**common, "explicit_workflow": True},
        )
        is True
    )


def test_hybrid_keeps_vague_or_semantically_ambiguous_turns_on_model_path() -> None:
    vague_write = "\u5199\u4e0b\u4e00\u7ae0"
    frame = {
        "primary": "general",
        "confidence": "low",
        "operationType": "inquiry",
        "canWrite": False,
    }
    hints = build_route_hints(prompt=vague_write, routing_mode=HYBRID)
    assert should_invoke_intent_model(
        HYBRID,
        prompt=vague_write,
        heuristic_frame=frame,
        route_hints=hints,
        previous_turn=None,
        has_custom_intents=False,
        explicit_workflow=False,
        workflow_confirmation=False,
    ) is True

    ambiguous = "\u8bfb\u53d6\u89d2\u8272\u5361\uff0c\u7136\u540e\u51b3\u5b9a\u662f\u5426\u4fee\u6539"
    ambiguous_hints = build_route_hints(prompt=ambiguous, routing_mode=HYBRID)
    assert should_invoke_intent_model(
        HYBRID,
        prompt=ambiguous,
        heuristic_frame={
            "primary": "character_work",
            "confidence": "medium",
            "operationType": "modify_existing",
            "canWrite": True,
        },
        route_hints=ambiguous_hints,
        previous_turn=None,
        has_custom_intents=False,
        explicit_workflow=False,
        workflow_confirmation=False,
    ) is True


def test_local_read_routing_cannot_expose_write_capability(monkeypatch, tmp_path) -> None:
    async def fail_if_called(**_kwargs):
        raise AssertionError("clear read must not invoke intent provider")

    service = StorydexIntentService()
    monkeypatch.setattr(service, "_llm_intent_frame", fail_if_called)
    frame = asyncio.run(
        service.classify_intent(
            prompt="\u67e5\u770b\u6c88\u6708\u89d2\u8272\u5361",
            workspace_root=tmp_path,
            session_id="read-character",
            routing_mode=HYBRID,
        )
    )
    assert frame["operationType"] == "inquiry"
    assert frame["effect"] == "respond_only"
    assert frame["canWrite"] is False
    assert "deterministic_read_only" in frame["signals"]


def test_direct_and_hybrid_clear_edit_skip_intent_provider(monkeypatch, tmp_path) -> None:
    async def fail_if_called(**_kwargs):
        raise AssertionError("intent provider must be skipped")

    for mode in (DIRECT, HYBRID, WORKFLOW):
        service = StorydexIntentService()
        monkeypatch.setattr(service, "_llm_intent_frame", fail_if_called)
        frame = asyncio.run(
            service.classify_intent(
                prompt=(
                    "请修改沈月角色卡的“核心动机”为“查明月蚀真相并保护幸存者”。"
                    "先读取目标文件，只修改这个字段；不要创建并行角色卡，不要修改其他文件。"
                ),
                workspace_root=tmp_path,
                session_id=f"session-{mode}",
                routing_mode=mode,
            )
        )
        assert frame["primary"] == "character_work"
        assert frame["operationType"] == "modify_existing"
        assert frame["canWrite"] is True
        assert "scoped_no_write_exclusion" in frame["signals"]
        assert frame["intentModelInvoked"] is False
        assert frame["method"] == f"deterministic_{mode}"


def test_hybrid_and_workflow_keep_structured_model_for_specialized_binding(
    monkeypatch,
    tmp_path,
) -> None:
    calls = []

    async def classify(**kwargs):
        calls.append(kwargs["prompt"])
        return {
            "primary": "wiki_work",
            "confidence": "high",
            "signals": [],
            "method": "llm",
            "operationType": "modify_existing",
            "decision": "decided",
            "effect": "execute",
            "artifact": "wiki",
            "targetScope": "named_asset",
            "targetValue": "",
            "explicitConstraints": [],
            "ambiguities": [],
            "evidence": ["绑定"],
            "canWrite": True,
            "complexity": "simple",
        }

    for mode in (HYBRID, WORKFLOW):
        service = StorydexIntentService()
        monkeypatch.setattr(service, "_llm_intent_frame", classify)
        frame = asyncio.run(
            service.classify_intent(
                prompt="把潮汐兽绑定到夜港星，关系是栖息于",
                workspace_root=tmp_path,
                session_id=f"binding-{mode}",
                routing_mode=mode,
            )
        )
        assert frame["intentModelInvoked"] is True
        assert frame["knowledgeWriteMode"] == "explicit_binding"
    assert len(calls) == 2


def test_direct_mode_preserves_explicit_no_write_constraint(monkeypatch, tmp_path) -> None:
    service = StorydexIntentService()

    async def fail_if_called(**_kwargs):
        raise AssertionError("intent provider must be skipped")

    monkeypatch.setattr(service, "_llm_intent_frame", fail_if_called)
    frame = asyncio.run(
        service.classify_intent(
            prompt="只分析沈月角色卡，不要修改或写入任何项目文件",
            workspace_root=tmp_path,
            session_id="read-only",
            routing_mode=DIRECT,
        )
    )
    assert frame["canWrite"] is False
    assert frame["effect"] == "respond_only"
    assert "no_project_write" in frame["explicitConstraints"]
