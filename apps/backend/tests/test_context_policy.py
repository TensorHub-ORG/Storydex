from __future__ import annotations

import asyncio
import types
from dataclasses import FrozenInstanceError

import pytest

from services.context_policy import ContextPolicy, context_policy_from_turn_contract
from services.global_config_service import GlobalConfigService
from services.story_project_service import get_story_project_service
from services.storydex_context_assembler_service import StorydexContextAssemblerService
from services.storydex_orchestration_service import StorydexOrchestrationService


def test_context_policy_defaults_are_frozen_and_serializable():
    policy = ContextPolicy()
    assert all(policy.to_dict().values())
    assert ContextPolicy.from_agent_settings({}) == policy
    assert ContextPolicy.from_agent_settings(
        {"coomiMemoryEnabled": False, "wikiContextEnabled": False}
    ) == policy.with_overrides(coomi_memory=False, wiki_context=False)
    assert context_policy_from_turn_contract(
        {"contextPolicy": {"sources": policy.to_dict()}}
    ) == policy
    assert len(policy.fingerprint) == 64
    with pytest.raises(FrozenInstanceError):
        policy.wiki_context = False  # type: ignore[misc]
    with pytest.raises(TypeError):
        policy.with_overrides(wiki_context="false")  # type: ignore[arg-type]


def test_agent_settings_default_true_without_persisting_and_round_trip(tmp_path):
    service = GlobalConfigService()
    service.settings = types.SimpleNamespace(global_root=str(tmp_path))
    assert service.read_agent_settings() == {
        "coomiMemoryEnabled": True,
        "wikiContextEnabled": True,
        "updatedAt": "",
    }
    assert not service.agent_settings_path().exists()

    updated = service.write_agent_settings(
        {"coomiMemoryEnabled": False, "wikiContextEnabled": True}
    )
    assert updated["coomiMemoryEnabled"] is False
    assert updated["wikiContextEnabled"] is True
    assert updated["updatedAt"]
    assert service.read_agent_settings() == updated


def test_assembler_skips_disabled_source_renderers_and_traces_policy(tmp_path, monkeypatch):
    service = get_story_project_service()
    service.ensure_project_structure(tmp_path)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("disabled renderer was called")

    monkeypatch.setattr(StorydexContextAssemblerService, "_render_related_passages", unexpected)
    monkeypatch.setattr(
        StorydexContextAssemblerService,
        "_render_wiki_reference",
        staticmethod(unexpected),
    )
    policy = ContextPolicy(
        base_story_context=False,
        story_structured_memory=False,
        passive_fts=False,
        wiki_context=False,
    )
    assembly = StorydexContextAssemblerService(service).assemble(
        tmp_path,
        prompt="核对上下文来源",
        policy=policy,
    )

    assert assembly["promptBlocks"] == []
    assert assembly["policy"]["sources"] == policy.to_dict()
    assert assembly["contextTrace"]["contextPolicy"] == policy.to_dict()
    expected_disabled = {
        "runtime_presets",
        "recent_segments",
        "rolling_summaries",
        "active_characters",
        "worldbook",
        "facts",
        "relationships",
        "items",
        "related_passages",
        "wiki_reference",
        "story_scripts",
        "variable_snapshot",
    }
    by_kind = {source["kind"]: source for source in assembly["contextTrace"]["sources"]}
    assert set(by_kind) == expected_disabled
    assert all(not by_kind[kind]["included"] for kind in expected_disabled)
    assert all(by_kind[kind]["dropReason"] == "disabled_by_policy" for kind in expected_disabled)


def test_product_agent_settings_generate_next_turn_policy(tmp_path):
    project_service = get_story_project_service()
    fake_global = types.SimpleNamespace(
        read_agent_settings=lambda: {
            "coomiMemoryEnabled": False,
            "wikiContextEnabled": False,
        }
    )
    orchestration = StorydexOrchestrationService(
        project_service,
        global_config_service=fake_global,
    )
    contract = orchestration.build_turn_contract(tmp_path, prompt="只读核对")
    sources = contract["contextPolicy"]["sources"]
    assert sources["coomi_memory"] is False
    assert sources["wiki_context"] is False
    wiki = next(
        source
        for source in contract["contextAssembly"]["contextTrace"]["sources"]
        if source["kind"] == "wiki_reference"
    )
    assert wiki["dropReason"] == "disabled_by_policy"


def test_memory_disabled_passes_none_and_records_zero_memory_source(tmp_path, monkeypatch):
    from services.coomi_agent_service import _build_coomi_memory, _build_coomi_system_prompt

    policy = ContextPolicy(coomi_memory=False)
    assert _build_coomi_memory(tmp_path, policy) == (None, None)

    contract = {
        "contextPolicy": {"sources": policy.to_dict()},
        "contextAssembly": {"promptBlocks": [], "sources": [], "budget": {}},
    }
    prompt = asyncio.run(
        _build_coomi_system_prompt(
            workspace_root=tmp_path,
            prompt="hello",
            turn_contract=contract,
        )
    )
    assert "Persistent Memories" not in prompt
    assert "Rust runtime tools" in prompt


def test_active_retrieval_policy_removes_only_storydex_retrieval_tools(tmp_path):
    from services.coomi_agent_service import _create_storydex_tool_registry

    registry = _create_storydex_tool_registry(
        tmp_path,
        ContextPolicy(active_retrieval_tools=False),
    )
    names = {tool.name for tool in registry.list_tools()}
    assert "StorydexProjectSearch" not in names
    assert "StorydexWikiQuery" not in names
    assert "StorydexSyncWiki" in names
    assert "StorydexApplyStoryIncrement" in names


def test_intent_metadata_does_not_activate_plan_mode_tool_filtering(tmp_path):
    from services.coomi_agent_service import _create_storydex_tool_registry
    from services.storydex_tool_types import ToolAccess

    registry = _create_storydex_tool_registry(
        tmp_path,
        turn_contract={
            "intentFrame": {
                "primary": "story_generation",
                "operationType": "inquiry",
                "decision": "decided",
                "effect": "respond_only",
                "canWrite": False,
            },
            "executionPolicy": {
                "directFileWrites": False,
                "allowedWriteRoots": [],
            },
        },
    )
    names = {tool.name for tool in registry.list_tools()}

    assert names
    assert any(tool.access != ToolAccess.READ_ONLY for tool in registry.list_tools())
    assert "StorydexSyncWiki" in names
    assert "StorydexApplyStoryIncrement" in names

    plan_registry = _create_storydex_tool_registry(
        tmp_path,
        turn_contract={
            "executionPolicy": {
                "directFileWrites": True,
                "allowedWriteRoots": [],
            },
        },
        plan_mode=True,
    )
    assert all(tool.access == ToolAccess.READ_ONLY for tool in plan_registry.list_tools())


@pytest.mark.parametrize(
    ("primary", "allowed_root", "expected_domain_tool", "forbidden_domain_tool"),
    [
        ("character_work", ".storydex/characters/", None, "StorydexApplyStoryIncrement"),
        ("worldbook_work", ".storydex/worldbook/", None, "StorydexSyncWiki"),
        ("story_generation", "chapters/", "StorydexApplyStoryIncrement", "StorydexSyncWiki"),
        ("wiki_work", ".storydex/wiki/", "StorydexSyncWiki", "StorydexApplyStoryIncrement"),
    ],
)
def test_scoped_write_turn_registry_exposes_only_relevant_mutators(
    tmp_path,
    primary,
    allowed_root,
    expected_domain_tool,
    forbidden_domain_tool,
):
    from services.coomi_agent_service import _create_storydex_tool_registry

    registry = _create_storydex_tool_registry(
        tmp_path,
        turn_contract={
            "intentFrame": {
                "primary": primary,
                "operationType": "create_new" if primary != "wiki_work" else "modify_existing",
                "decision": "decided",
                "effect": "create" if primary != "wiki_work" else "execute",
                "canWrite": True,
            },
            "executionPolicy": {
                "directFileWrites": True,
                "allowedWriteRoots": [allowed_root],
            },
        },
    )
    names = {tool.name for tool in registry.list_tools()}

    assert "StorydexProjectSearch" in names
    if expected_domain_tool:
        assert expected_domain_tool in names
    assert forbidden_domain_tool not in names
