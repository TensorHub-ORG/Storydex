from __future__ import annotations

import json
from types import SimpleNamespace

from services.coomi_agent_service import (
    _CoomiEventTranslator,
    _compaction_checkpoint_context,
    _render_context_assembly_blocks,
)
from services.context_budget_service import (
    apply_context_controls,
    context_cache_key,
    request_token_accounting,
    reset_context_assembly_cache,
)
from services.context_policy import ContextPolicy
from services.context_trace_service import (
    build_context_trace,
    capture_provider_request,
    merge_llm_metrics,
)
from services.storydex_context_assembler_service import StorydexContextAssemblerService
from api.routes_agent import _extract_trace_metrics


def _block(block_id: str, content: str, *, paths: list[str] | None = None) -> dict:
    return {
        "id": block_id,
        "title": block_id,
        "kind": block_id,
        "sourcePaths": paths or [],
        "content": content,
    }


def test_budget_shadow_is_content_equivalent_and_enforced_result_is_deterministic() -> None:
    blocks = [
        _block("related_passages", "相关证据 " * 40),
        _block("runtime_presets", "预设约束 " * 30),
        _block("recent_segments", "最近正文 " * 30),
        _block("project_organization_inventory", "项目目录 " * 30),
        _block("other", "低优先级内容 " * 160),
    ]

    shadow, shadow_accounting, _ = apply_context_controls(
        blocks,
        prompt="检查相关证据",
        real_budget_enabled=False,
        jit_enabled=False,
        context_window=4096,
        output_reserve_tokens=512,
    )
    assert [item["content"] for item in shadow] == [item["content"] for item in blocks]
    assert shadow_accounting["mode"] == "shadow"
    assert shadow_accounting["logicalContextTokens"] == shadow_accounting["transmittedContextTokens"]

    enforced_a, accounting_a, notes_a = apply_context_controls(
        blocks,
        prompt="检查相关证据",
        real_budget_enabled=True,
        jit_enabled=False,
        context_window=1024,
        output_reserve_tokens=800,
    )
    enforced_b, accounting_b, notes_b = apply_context_controls(
        blocks,
        prompt="检查相关证据",
        real_budget_enabled=True,
        jit_enabled=False,
        context_window=1024,
        output_reserve_tokens=800,
    )
    assert enforced_a == enforced_b
    assert accounting_a == accounting_b
    assert notes_a == notes_b
    assert accounting_a["mode"] == "enforced"
    assert accounting_a["omittedBlocks"] or accounting_a["truncatedBlocks"]
    # Priority is stable: a low-priority block cannot consume budget before
    # the related/preset/recent blocks.
    assert enforced_a[0]["tokenCount"] > 0
    assert enforced_a[-1]["omitted"] or enforced_a[-1]["truncated"]


def test_jit_marks_unrelated_blocks_and_renderer_exposes_status() -> None:
    blocks = [
        _block("related_passages", "星核密钥的冲突证据"),
        _block("other", "完全无关的天气描写"),
    ]
    controlled, accounting, _ = apply_context_controls(
        blocks,
        prompt="请检查星核密钥",
        real_budget_enabled=False,
        jit_enabled=True,
    )
    assert controlled[0]["omitted"] is False
    assert controlled[1]["omitted"] is True
    assert controlled[1]["dropReason"] == "jit_not_relevant"
    rendered = _render_context_assembly_blocks({"promptBlocks": controlled})
    assert "[omitted: jit_not_relevant]" in rendered
    assert accounting["jitEnabled"] is True


def test_context_cache_key_changes_for_catalog_generation_and_revision() -> None:
    first = SimpleNamespace(
        generation=1,
        catalog_revision="sha256:catalog-a",
        entries={
            "chapters/b.md": SimpleNamespace(revision="sha256:b"),
            "chapters/a.md": SimpleNamespace(revision="sha256:a"),
        },
    )
    same_values_different_order = SimpleNamespace(
        generation=1,
        catalog_revision="sha256:catalog-a",
        entries={
            "chapters/a.md": SimpleNamespace(revision="sha256:a"),
            "chapters/b.md": SimpleNamespace(revision="sha256:b"),
        },
    )
    changed_generation = SimpleNamespace(
        generation=2,
        catalog_revision="sha256:catalog-b",
        entries=first.entries,
    )
    kwargs = {
        "workspace_root": SimpleNamespace(resolve=lambda: SimpleNamespace(as_posix=lambda: "workspace")),
        "policy_fingerprint": "policy",
        "prompt": "prompt",
        "active_file": "chapters/a.md",
        "intent_primary": "story",
        "turn_plan": {"target": "chapters/a.md"},
        "block_config": {"jit": False},
    }
    # Use a real Path-like root so the key assertion exercises canonicalization.
    from pathlib import Path

    kwargs["workspace_root"] = Path("workspace")
    assert context_cache_key(catalog_snapshot=first, **kwargs) == context_cache_key(
        catalog_snapshot=same_values_different_order,
        **kwargs,
    )
    assert context_cache_key(catalog_snapshot=first, **kwargs) != context_cache_key(
        catalog_snapshot=changed_generation,
        **kwargs,
    )


def test_request_accounting_covers_system_history_tools_turn_contract_and_reserve() -> None:
    messages = [
        {
            "role": "system",
            "content": "system preface\nStorydex turn contract:\n目标文件 chapters/001.md",
        },
        {"role": "user", "content": "请检查证据"},
        {
            "role": "assistant",
            "tool_calls": [{"id": "call-1", "function": {"name": "Read"}}],
            "content": "",
        },
    ]
    tools = [{"type": "function", "function": {"name": "Read", "parameters": {}}}]
    accounting = request_token_accounting(messages=messages, tools=tools, output_reserve_tokens=123)
    assert accounting["systemTokens"] > 0
    assert accounting["historyTokens"] > 0
    assert accounting["toolSchemaTokens"] > 0
    assert accounting["turnContractTokens"] > 0
    assert accounting["outputReserveTokens"] == 123
    assert accounting["logicalInputTokens"] == (
        accounting["systemTokens"] + accounting["historyTokens"] + accounting["toolSchemaTokens"]
    )
    assert accounting["reservedTotalTokens"] == accounting["logicalInputTokens"] + 123


def test_provider_request_capture_and_merge_exposes_shadow_accounting() -> None:
    block = _block("related_passages", "可核验证据")
    source = {
        "kind": "related_passages",
        "included": True,
        "chars": len(block["content"]),
        "estTokens": 3,
    }
    assembly = {
        "promptBlocks": [block],
        "contextTrace": build_context_trace([source], [block], assemble_ms=1),
    }
    request = capture_provider_request(
        assembly,
        request_index=0,
        purpose="chat",
        method="chat",
        messages=[
            {"role": "system", "content": f"header\n{block['content']}"},
            {"role": "user", "content": "继续"},
        ],
        tools=[{"type": "function", "function": {"name": "Read"}}],
        kwargs={},
        request_hash="hash-a",
    )
    assert request["tokenAccounting"]["systemTokens"] > 0
    merged = merge_llm_metrics(
        assembly["contextTrace"],
        {"llmCalls": [], "providerRequests": [request]},
    )
    totals = merged["totals"]
    assert totals["shadowRequestCount"] == 1
    assert totals["shadowLogicalInputTokens"] == request["tokenAccounting"]["logicalInputTokens"]
    assert totals["shadowTransmittedInputTokens"] == request["tokenAccounting"]["transmittedInputTokens"]
    assert totals["shadowTokenDelta"] == 0


def test_lru_assembler_hits_same_catalog_and_invalidates_on_generation(tmp_path) -> None:
    reset_context_assembly_cache()
    (tmp_path / ".storydex" / "config").mkdir(parents=True)
    (tmp_path / ".storydex" / "config" / "feature-flags.json").write_text(
        json.dumps({"CONTEXT_LRU_ENABLED": True}),
        encoding="utf-8",
    )
    service = SimpleNamespace(
        build_generation_context=lambda *_args, **_kwargs: {"chapterStates": []},
    )
    assembler = StorydexContextAssemblerService(service)
    policy = ContextPolicy(
        base_story_context=False,
        story_structured_memory=False,
        passive_fts=False,
        wiki_context=False,
        coomi_memory=False,
        active_retrieval_tools=False,
    )
    snapshot_a = SimpleNamespace(
        generation=1,
        catalog_revision="sha256:a",
        entries={},
    )
    first = assembler.assemble(tmp_path, prompt="hello", policy=policy, catalog_snapshot=snapshot_a)
    second = assembler.assemble(tmp_path, prompt="hello", policy=policy, catalog_snapshot=snapshot_a)
    assert first["budget"]["cacheStatus"] == "miss"
    assert second["budget"]["cacheStatus"] == "hit"
    assert second["contextTrace"]["cache"]["status"] == "hit"
    snapshot_b = SimpleNamespace(
        generation=2,
        catalog_revision="sha256:b",
        entries={},
    )
    third = assembler.assemble(tmp_path, prompt="hello", policy=policy, catalog_snapshot=snapshot_b)
    assert third["budget"]["cacheStatus"] == "miss"


def test_python_compaction_events_keep_checkpoint_fields(tmp_path) -> None:
    translator = _CoomiEventTranslator(
        session_id="session-a",
        trace_id="turn-a",
        workspace_root=tmp_path,
    )
    started = translator.translate(
        {
            "type": "compaction_started",
            "data": {
                "automatic": True,
                "checkpointValid": True,
                "checkpointHash": "sha256:checkpoint",
                "toolCallCount": 3,
                "evidenceRevisionCount": 2,
            },
        }
    )
    completed = translator.translate(
        {
            "type": "compaction_completed",
            "data": {"automatic": True, "beforeTokens": 1000, "afterTokens": 400},
        }
    )
    assert started is not None and completed is not None
    assert started[0] == completed[0] == "CompressionEvent"
    assert started[1]["compact_status"] == "checkpoint_ready"
    assert completed[1]["compact_status"] == "completed"
    assert completed[1]["checkpointValid"] is True
    assert completed[1]["checkpointHash"] == "sha256:checkpoint"
    assert completed[1]["toolCallCount"] == 3
    assert completed[1]["evidenceRevisionCount"] == 2


def test_compaction_checkpoint_context_carries_permission_target_and_evidence(tmp_path) -> None:
    from services.evidence_ledger_service import EvidenceLedgerService
    from services.source_contract import source_revision_id

    ledger = EvidenceLedgerService(tmp_path, "session-a")
    revision = source_revision_id("evidence".encode("utf-8"))
    ledger.record(
        path="chapters/001.md",
        revision=revision,
        span={"startChar": 0, "endChar": 8, "startByte": 0, "endByte": 8, "startLine": 1, "endLine": 1},
        source_tool="read_file",
        turn_id="turn-a",
    )
    checkpoint = _compaction_checkpoint_context(
        workspace_root=tmp_path,
        session_id="session-a",
        permission_mode="plan_mode",
        prompt="继续",
        active_file="chapters/001.md",
        turn_contract={
            "intentFrame": {"assetTargets": ["chapters/002.md"]},
            "turnPlan": {
                "authoritativeChapterPath": "chapters/001.md",
                "nextSegmentPath": "chapters/001/segments/003.md",
                "fragmentTargets": [{"path": "chapters/003.md"}],
            },
        },
    )
    assert checkpoint["permissionMode"] == "plan_mode"
    assert checkpoint["target"] == "chapters/001.md"
    assert "chapters/002.md" in checkpoint["targets"]
    assert checkpoint["evidenceRevisions"][0]["revision"] == revision
    assert checkpoint["evidenceRevisions"][0]["spans"][0]["startChar"] == 0


def test_trace_unique_evidence_bytes_merges_overlapping_spans() -> None:
    events = [
        {
            "event": "ToolDone",
            "data": {
                "evidenceLedger": {
                    "observationCount": 1,
                    "observations": [
                        {
                            "path": "chapters/001.md",
                            "revision": "sha256:r1",
                            "spans": [{"startByte": 0, "endByte": 100}],
                        }
                    ],
                }
            },
        },
        {
            "event": "ToolDone",
            "data": {
                "evidenceLedger": {
                    "observationCount": 1,
                    "observations": [
                        {
                            "path": "chapters/001.md",
                            "revision": "sha256:r1",
                            "spans": [{"startByte": 50, "endByte": 150}],
                        }
                    ],
                }
            },
        },
    ]
    metrics = _extract_trace_metrics(events, "trace", 0, {})
    assert metrics["evidenceObservations"] == 2
    assert metrics["uniqueEvidenceBytes"] == 150
