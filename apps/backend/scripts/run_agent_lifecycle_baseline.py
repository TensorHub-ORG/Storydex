"""Run a low-risk, repeatable Agent lifecycle baseline against an isolated provider.

The script copies only the selected provider entry into a temporary Coomi home,
starts the normal HTTP/SSE backend route, and writes a redacted report.  It is
intended for phase-A/E diagnostics, not as a production health check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPOSITORY_ROOT / "apps" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.run_graph_live_acceptance import (  # noqa: E402
    AcceptanceError,
    BackendProcess,
    free_port,
    load_isolated_provider,
    now_iso,
    prepare_workspace,
    provider_config_path,
    redact,
    run_turn,
    started_tool_names,
    TurnFailure,
    write_json,
)


MARKER = "STORYDEX_LIFECYCLE_BASELINE_OK"
MULTI_MARKERS = (
    "STORYDEX_MULTI_ALPHA_41A7",
    "STORYDEX_MULTI_BRAVO_82C3",
    "STORYDEX_MULTI_CHARLIE_D5E9",
)
CHARACTER_PRESERVE_MARKER = "STORYDEX_CHARACTER_PRESERVE_7F2A"
CHARACTER_NEW_MOTIVATION = "查明月蚀真相并保护幸存者"
CHAPTER_MIDDLE_MARKER = "STORYDEX_CHAPTER_MIDDLE_6C4E"


def prepare_fixture(workspace: Path, scenario: str) -> dict[str, Any]:
    prepare_workspace(workspace)
    if scenario == "chapter-middle-read":
        relative = "chapters/lifecycle-middle.md"
        source = workspace / relative
        prefix = "\n".join(
            f"前段占位 {index:02d}：" + ("甲" * 90) for index in range(24)
        )
        suffix = "\n".join(
            f"后段占位 {index:02d}：" + ("乙" * 90) for index in range(24)
        )
        source.write_text(
            "# Lifecycle middle-span evidence\n\n"
            f"{prefix}\n\n"
            "## 中段锚点\n"
            "月蚀航标只在赤潮退去后的第三夜点亮。\n"
            f"固定验收标记：{CHAPTER_MIDDLE_MARKER}\n\n"
            f"{suffix}\n",
            encoding="utf-8",
        )
        return {
            "scenario": scenario,
            "workspaceFiles": [relative],
            "prompt": "只读查找章节中的“中段锚点”，返回其附近的固定验收标记。",
            "markers": [CHAPTER_MIDDLE_MARKER],
            "expectedToolNames": ["read_file"],
        }
    if scenario == "character-field-edit":
        relative = ".storydex/characters/Shenyue.md"
        source = workspace / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "# 沈月\n\n"
            "## 身份\n夜港档案员\n\n"
            "## 核心动机\n守住家族留下的旧档案\n\n"
            "## 行为边界\n不会主动伤害无辜者\n\n"
            f"## 验收保留标记\n{CHARACTER_PRESERVE_MARKER}\n",
            encoding="utf-8",
        )
        return {
            "scenario": scenario,
            "workspaceFiles": [relative],
            "prompt": "修改沈月角色卡的核心动机，只修改目标字段并保留其他内容。",
            "markers": [],
            "expectedToolNames": ["read_file", "edit_file"],
            "toolValidation": "contains_in_order",
            "expectedFileState": {
                "path": relative,
                "contains": [CHARACTER_NEW_MOTIVATION, CHARACTER_PRESERVE_MARKER],
                "notContains": ["守住家族留下的旧档案"],
            },
        }
    if scenario == "strict-multi-read":
        files = []
        for name, marker in zip(("alpha", "bravo", "charlie"), MULTI_MARKERS):
            relative = f"chapters/lifecycle-{name}.md"
            source = workspace / relative
            source.write_text(
                f"# {name.title()} lifecycle evidence\n\n固定验收标记：{marker}\n",
                encoding="utf-8",
            )
            files.append(relative)
        return {
            "scenario": scenario,
            "workspaceFiles": files,
            "prompt": "固定只读任务：依次读取三个文件并返回各自标记，不修改任何项目文件。",
            "markers": list(MULTI_MARKERS),
            "expectedToolNames": ["read_file", "read_file", "read_file"],
        }
    source = workspace / "chapters" / "lifecycle-baseline.md"
    source.write_text(
        "# Lifecycle baseline\n\n"
        f"固定验收标记：{MARKER}\n"
        "本文件只用于只读 Agent 生命周期测量。\n",
        encoding="utf-8",
    )
    return {
        "scenario": scenario,
        "workspaceFiles": ["chapters/lifecycle-baseline.md"],
        "prompt": "固定只读任务：读取该文件并返回标记，不修改任何项目文件。",
        "markers": [MARKER],
        "expectedToolNames": ["read_file"],
    }


def fixture_prompt(fixture: Mapping[str, Any]) -> str:
    if fixture.get("scenario") == "chapter-middle-read":
        return (
            "这是一次严格只读的章节中段证据测试。只能调用 read_file，读取 "
            "chapters/lifecycle-middle.md；不要使用其他工具，不要修改任何文件。"
            "请定位“中段锚点”附近的内容，读取后只返回固定标记 "
            f"{CHAPTER_MIDDLE_MARKER}。"
        )
    if fixture.get("scenario") == "character-field-edit":
        return (
            "请修改沈月角色卡的“核心动机”为“"
            f"{CHARACTER_NEW_MOTIVATION}"
            "”。先读取目标文件作为证据，只修改这个字段，保留身份、行为边界和验收标记；"
            "不要创建并行角色卡，不要修改其他文件。"
        )
    if fixture.get("scenario") == "strict-multi-read":
        paths = "、".join(str(item) for item in fixture.get("workspaceFiles") or [])
        markers = "、".join(str(item) for item in fixture.get("markers") or [])
        return (
            "这是一次严格只读多工具生命周期基线测试。只能调用 read_file，并严格按照给定顺序，"
            f"对每个文件恰好调用一次：{paths}。不要使用其他工具，不要重复读取，不要修改任何文件。"
            f"全部读取完成后，只按给定顺序返回三个固定标记：{markers}。"
        )
    return (
        "这是一次只读生命周期基线测试。只能调用一次 read_file，读取 "
        "chapters/lifecycle-baseline.md；不要使用其他工具，不要修改任何文件。"
        f"读取后只返回固定标记 {MARKER}。"
    )


def provider_config_observation(
    provider: Mapping[str, Any], provider_id: str, source_format: str
) -> dict[str, Any]:
    observed: dict[str, Any] = {
        "providerId": provider_id,
        "sourceFormat": str(source_format or "unknown"),
        "type": str(provider.get("type") or ""),
        "model": str(provider.get("model") or ""),
        "baseUrl": str(provider.get("base_url") or provider.get("baseUrl") or ""),
        "toolProtocol": str(provider.get("tool_protocol") or provider.get("toolProtocol") or ""),
        "configuredFields": sorted(
            str(key)
            for key in provider
            if str(key).lower()
            in {
                "max_output_tokens",
                "max_tokens",
                "reasoning_effort",
                "reasoning_effort_map",
                "reasoning_profiles",
                "supports_reasoning_effort",
                "supports_parallel_tool_calls",
                "reasoning_prompt_fallback",
                "stream",
                "stream_options",
                "tool_protocol",
            }
        ),
    }
    for key in observed["configuredFields"]:
        value = provider.get(key)
        if str(key).lower() in {"reasoning_effort_map", "reasoning_profiles"} and isinstance(value, Mapping):
            observed[key] = sorted(str(item) for item in value)
        elif str(key).lower() in {
            "max_output_tokens",
            "max_tokens",
            "supports_reasoning_effort",
            "supports_parallel_tool_calls",
            "reasoning_prompt_fallback",
        }:
            observed[key] = value
        elif str(key).lower() in {"stream", "stream_options", "tool_protocol"}:
            observed[key] = value
    return observed


def output_limit_observation(
    provider: Mapping[str, Any], lifecycle: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare the resolved capability limit with observed wire values."""

    config_source = (REPOSITORY_ROOT / "apps" / "desktop" / "agent-runtime" / "services" / "src" / "config.rs").read_text(
        encoding="utf-8"
    )
    provider_source = (REPOSITORY_ROOT / "apps" / "desktop" / "agent-runtime" / "services" / "src" / "provider.rs").read_text(
        encoding="utf-8"
    )
    capability_match = re.search(r"max_output_tokens\.unwrap_or\((\d[\d_]*)\)", config_source)
    wire_match = re.search(r"PROVIDER_DEFAULT_MAX_OUTPUT_TOKENS: u64 = (\d[\d_]*)", provider_source)
    if capability_match is None or wire_match is None:
        raise AcceptanceError("unable to resolve local output-token defaults")
    configured = provider.get("max_output_tokens")
    capability_default = int(capability_match.group(1).replace("_", ""))
    wire_default = int(wire_match.group(1).replace("_", ""))
    capability = int(configured) if configured is not None else capability_default
    rounds = lifecycle.get("rounds") if isinstance(lifecycle.get("rounds"), list) else []
    observed_values = sorted(
        {
            int(item.get("wireMaxOutputTokens") or 0)
            for item in rounds
            if isinstance(item, Mapping) and int(item.get("wireMaxOutputTokens") or 0) > 0
        }
    )
    return {
        "configuredMaxOutputTokens": configured,
        "resolvedCapabilityMaxOutputTokens": capability,
        "wireDefaultMaxTokens": wire_default,
        "wireObserved": bool(observed_values),
        "observedWireMaxOutputTokens": observed_values,
        "mismatch": bool(observed_values) and observed_values != [capability],
        "source": {
            "capability": "apps/desktop/agent-runtime/services/src/config.rs",
            "wire": "apps/desktop/agent-runtime/services/src/provider.rs",
        },
    }


def parallel_tool_calls_observation(
    provider: Mapping[str, Any], lifecycle: Mapping[str, Any]
) -> dict[str, Any]:
    rounds = lifecycle.get("rounds") if isinstance(lifecycle.get("rounds"), list) else []
    observed = sorted(
        {
            bool(item.get("wireParallelToolCalls"))
            for item in rounds
            if isinstance(item, Mapping) and isinstance(item.get("wireParallelToolCalls"), bool)
        }
    )
    configured = bool(provider.get("supports_parallel_tool_calls"))
    return {
        "configured": configured,
        "wireObserved": bool(observed),
        "observedWireValues": observed,
        "mismatch": bool(observed) and observed != [configured],
    }


def enable_parallel_tool_calls_in_isolated_config(
    coomi_home: Path, provider_id: str
) -> dict[str, Any]:
    config_path = coomi_home / "config" / "providers.json"
    document = json.loads(config_path.read_text(encoding="utf-8-sig"))
    providers = document.get("providers") if isinstance(document, dict) else None
    provider = providers.get(provider_id) if isinstance(providers, dict) else None
    if not isinstance(provider, dict):
        raise AcceptanceError(f"isolated provider {provider_id} is missing")
    provider["supports_parallel_tool_calls"] = True
    write_json(config_path, document)
    return provider


def classify_wait_stages(lifecycle: Mapping[str, Any]) -> dict[str, Any]:
    phase_totals = lifecycle.get("phaseTotalsMs") if isinstance(lifecycle.get("phaseTotalsMs"), Mapping) else {}
    candidates: list[dict[str, Any]] = []
    labels = {
        "intent_classification": "intent_classification",
        "context_assembly": "context_assembly",
        "workspace_snapshot": "workspace_snapshot",
        "task_planning": "task_planning",
    }
    for key, label in labels.items():
        value = int(phase_totals.get(key) or 0)
        if value:
            candidates.append({"stage": label, "durationMs": value})
    model_ms = int(lifecycle.get("modelGenerationMs") or 0)
    if model_ms:
        candidates.append(
            {
                "stage": "model_generation",
                "durationMs": model_ms,
                "providerWaitMs": int(lifecycle.get("providerWaitMs") or 0),
            }
        )
    tool_ms = int(lifecycle.get("toolExecutionMs") or 0)
    if tool_ms:
        candidates.append({"stage": "tool_execution", "durationMs": tool_ms})
    retry_ms = int(lifecycle.get("retryRecoveryMs") or 0)
    if retry_ms:
        candidates.append({"stage": "provider_retry_recovery", "durationMs": retry_ms})
    candidates.sort(key=lambda item: int(item.get("durationMs") or 0), reverse=True)
    return {
        "primaryWaitStage": str(candidates[0]["stage"]) if candidates else "unknown",
        "ranking": candidates,
    }


def public_turn(
    result: Mapping[str, Any],
    fixture: Mapping[str, Any],
    *,
    workspace: Path | None = None,
) -> dict[str, Any]:
    lifecycle = result.get("lifecycle") if isinstance(result.get("lifecycle"), Mapping) else {}
    protocol = result.get("protocol") if isinstance(result.get("protocol"), Mapping) else {}
    tool_calls = result.get("toolCalls") if isinstance(result.get("toolCalls"), list) else []
    reply_preview = str(result.get("replyPreview") or "")
    started_calls: list[Mapping[str, Any]] = []
    seen_call_ids: set[str] = set()
    for item in tool_calls:
        if not isinstance(item, Mapping) or str(item.get("event") or "") not in {
            "ToolCall",
            "ToolStart",
            "ToolStarted",
        }:
            continue
        call_id = str(item.get("toolCallId") or "").strip()
        if call_id and call_id in seen_call_ids:
            continue
        if call_id:
            seen_call_ids.add(call_id)
        started_calls.append(item)
    fingerprints = [
        hashlib.sha256(
            json.dumps(
                {
                    "tool": str(item.get("toolName") or ""),
                    "arguments": item.get("arguments") if isinstance(item.get("arguments"), Mapping) else {},
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for item in started_calls
    ]
    markers = [str(item) for item in fixture.get("markers") or [] if str(item)]
    contract_observation: dict[str, Any] = {}
    for event in result.get("events", []):
        if not isinstance(event, Mapping) or event.get("event") != "TurnContract":
            continue
        contract_observation = (
            dict(event.get("turnContract"))
            if isinstance(event.get("turnContract"), Mapping)
            else {}
        )
        break
    file_checks: dict[str, Any] = {}
    expected_file = fixture.get("expectedFileState")
    if workspace is not None and isinstance(expected_file, Mapping):
        relative = str(expected_file.get("path") or "").strip()
        target = workspace / relative
        content = target.read_text(encoding="utf-8") if target.is_file() else ""
        file_checks = {
            "path": relative,
            "exists": target.is_file(),
            "contains": {
                str(value): str(value) in content
                for value in expected_file.get("contains", [])
                if str(value)
            },
            "notContains": {
                str(value): str(value) not in content
                for value in expected_file.get("notContains", [])
                if str(value)
            },
        }
    return {
        "traceId": str(result.get("traceId") or ""),
        "sessionId": str(result.get("sessionId") or ""),
        "elapsedMs": int(result.get("elapsedMs") or 0),
        "eventCount": int(result.get("eventCount") or 0),
        "eventNames": [
            str(item.get("event") or "")
            for item in result.get("events", [])
            if isinstance(item, Mapping)
        ],
        "lifecycle": dict(lifecycle),
        "protocol": dict(protocol),
        "toolCallCount": int(lifecycle.get("toolCalls") or 0),
        "toolSequence": started_tool_names(result),
        "uniqueToolInvocationCount": len(set(fingerprints)),
        "duplicateToolInvocationCount": len(fingerprints) - len(set(fingerprints)),
        "toolNames": sorted(
            {
                str(item.get("toolName") or "")
                for item in tool_calls
                if isinstance(item, Mapping) and str(item.get("toolName") or "")
            }
        ),
        "visibleReplyChars": len(reply_preview),
        "markersObserved": all(marker in reply_preview for marker in markers),
        "turnContractObservation": contract_observation,
        "fileChecks": file_checks,
        "usage": dict(result.get("usage") or {}) if isinstance(result.get("usage"), Mapping) else {},
        "errors": list(result.get("errors") or []),
    }


def validate_baseline_turn(turn: Mapping[str, Any], fixture: Mapping[str, Any] | None = None) -> None:
    """Reject a baseline that does not follow its deliberately tiny contract."""

    fixture = fixture or {
        "expectedToolNames": ["read_file"],
        "markers": [MARKER],
    }
    tool_names = [str(name) for name in (turn.get("toolSequence") or turn.get("toolNames") or []) if str(name)]
    tool_count = int(turn.get("toolCallCount") or 0)
    expected_tools = [str(name) for name in fixture.get("expectedToolNames") or []]
    validation_mode = str(fixture.get("toolValidation") or "exact")
    tool_sequence_ok = tool_names == expected_tools
    if validation_mode == "contains_in_order":
        position = 0
        for name in tool_names:
            if position < len(expected_tools) and name == expected_tools[position]:
                position += 1
        tool_sequence_ok = position == len(expected_tools)
    tool_count_ok = (
        tool_count >= len(expected_tools)
        if validation_mode == "contains_in_order"
        else tool_count == len(expected_tools)
    )
    if not tool_sequence_ok or not tool_count_ok:
        raise AcceptanceError(
            "baseline task used an unexpected tool sequence: "
            f"names={tool_names!r}, count={tool_count}"
        )
    lifecycle = turn.get("lifecycle") if isinstance(turn.get("lifecycle"), Mapping) else {}
    tools = lifecycle.get("tools") if isinstance(lifecycle.get("tools"), list) else []
    if any(bool(item.get("error")) for item in tools if isinstance(item, Mapping)):
        raise AcceptanceError("baseline task reported a tool error")
    if int(turn.get("duplicateToolInvocationCount") or 0):
        raise AcceptanceError("baseline task repeated an identical tool invocation")
    unique_count = int(turn.get("uniqueToolInvocationCount") or 0)
    if (
        validation_mode == "exact"
        and unique_count != len(expected_tools)
        or validation_mode == "contains_in_order"
        and unique_count < len(expected_tools)
    ):
        raise AcceptanceError("baseline task did not use the required unique tool invocations")
    markers_observed = turn.get("markersObserved")
    if markers_observed is None:
        markers_observed = turn.get("markerObserved")
    if not bool(markers_observed):
        raise AcceptanceError("baseline task did not return all fixed acceptance markers")
    file_checks = turn.get("fileChecks") if isinstance(turn.get("fileChecks"), Mapping) else {}
    if file_checks:
        if not bool(file_checks.get("exists")):
            raise AcceptanceError("baseline target file is missing after execution")
        if not all(bool(value) for value in (file_checks.get("contains") or {}).values()):
            raise AcceptanceError("baseline target file is missing expected content")
        if not all(bool(value) for value in (file_checks.get("notContains") or {}).values()):
            raise AcceptanceError("baseline target file still contains replaced content")


def run_baseline(args: argparse.Namespace) -> dict[str, Any]:
    started_at = now_iso()
    output_root = (
        Path(args.output_dir).resolve()
        if str(args.output_dir or "").strip()
        else REPOSITORY_ROOT / "output" / "agent-lifecycle-baseline" / uuid.uuid4().hex[:10]
    )
    output_root.mkdir(parents=True, exist_ok=True)
    report_path = output_root / "baseline-report.json"
    source_config = Path(args.config).resolve() if args.config else provider_config_path()
    original_bridge = os.environ.get("STORYDEX_COOMI_BRIDGE")
    original_routing_mode = os.environ.get("AGENT_INTENT_ROUTING_MODE")
    bridge = (
        Path(args.bridge).resolve()
        if str(args.bridge or "").strip()
        else REPOSITORY_ROOT
        / "apps"
        / "desktop"
        / "agent-runtime"
        / "target"
        / "debug"
        / "storydex-coomi-bridge.exe"
    )
    if not bridge.is_file():
        raise AcceptanceError(f"debug bridge is missing: {bridge}")
    try:
        with tempfile.TemporaryDirectory(prefix="storydex-agent-lifecycle-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            coomi_home = root / "coomi-home"
            fixture = prepare_fixture(workspace, args.scenario)
            provider = load_isolated_provider(
                source_config,
                coomi_home,
                args.provider_id,
                args.model,
            )
            isolated_provider = json.loads(
                (coomi_home / "config" / "providers.json").read_text(encoding="utf-8-sig")
            )["providers"][args.provider_id]
            if args.enable_parallel_tool_calls:
                isolated_provider = enable_parallel_tool_calls_in_isolated_config(
                    coomi_home, args.provider_id
                )
            config_observation = provider_config_observation(
                isolated_provider,
                args.provider_id,
                str(provider.get("sourceFormat") or ""),
            )
            if args.enable_parallel_tool_calls:
                config_observation["temporaryOverrides"] = {
                    "supports_parallel_tool_calls": True,
                }
            os.environ["STORYDEX_COOMI_BRIDGE"] = str(bridge)
            if str(args.intent_routing_mode or "").strip():
                os.environ["AGENT_INTENT_ROUTING_MODE"] = str(args.intent_routing_mode).strip()
            port = free_port()
            backend = BackendProcess(
                workspace=workspace,
                coomi_home=coomi_home,
                log_path=output_root / "backend.log",
                port=port,
                repository_root=(
                    Path(args.backend_repository_root).resolve()
                    if str(args.backend_repository_root or "").strip()
                    else REPOSITORY_ROOT
                ),
            )
            client = httpx.Client()
            try:
                backend.start()
                try:
                    result = run_turn(
                        client,
                        base_url=backend.base_url,
                        workspace=workspace,
                        session_id="lifecycle-baseline-" + uuid.uuid4().hex[:10],
                        prompt=fixture_prompt(fixture),
                        reasoning_effort=args.reasoning_effort,
                        label="lifecycle-baseline",
                        expected_provider=args.provider_id,
                        expected_model=args.model,
                        timeout_seconds=args.turn_timeout,
                    )
                except TurnFailure as exc:
                    failed_turn = public_turn(exc.result, fixture, workspace=workspace)
                    config_observation["outputLimit"] = output_limit_observation(
                        isolated_provider,
                        failed_turn["lifecycle"],
                    )
                    config_observation["parallelToolCalls"] = parallel_tool_calls_observation(
                        isolated_provider,
                        failed_turn["lifecycle"],
                    )
                    report = {
                        "_type": "AgentLifecycleBaseline",
                        "_version": 2,
                        "status": "failed",
                        "startedAt": started_at,
                        "finishedAt": now_iso(),
                        "provider": redact(provider),
                        "configObservation": redact(config_observation),
                        "fixture": fixture,
                        "turn": failed_turn,
                        "classification": {
                            **classify_wait_stages(failed_turn["lifecycle"]),
                            "evidence": {
                                "timeToFirstByteMs": failed_turn["lifecycle"].get(
                                    "timeToFirstByteMs", 0
                                ),
                                "timeToFirstVisibleOutputMs": failed_turn["lifecycle"].get(
                                    "timeToFirstVisibleOutputMs", 0
                                ),
                                "providerWaitMs": failed_turn["lifecycle"].get(
                                    "providerWaitMs", 0
                                ),
                                "modelGenerationMs": failed_turn["lifecycle"].get(
                                    "modelGenerationMs", 0
                                ),
                                "toolExecutionMs": failed_turn["lifecycle"].get(
                                    "toolExecutionMs", 0
                                ),
                                "retryCount": failed_turn["lifecycle"].get("retryCount", 0),
                            },
                        },
                        "error": str(exc),
                    }
                    write_json(report_path, redact(report))
                    raise AcceptanceError(f"{exc}; report={report_path.as_posix()}") from exc
            finally:
                client.close()
                backend.stop()

            turn = public_turn(result, fixture, workspace=workspace)
            validation_error: AcceptanceError | None = None
            try:
                validate_baseline_turn(turn, fixture)
            except AcceptanceError as exc:
                validation_error = exc
            config_observation["outputLimit"] = output_limit_observation(
                isolated_provider,
                turn["lifecycle"],
            )
            config_observation["parallelToolCalls"] = parallel_tool_calls_observation(
                isolated_provider,
                turn["lifecycle"],
            )
            report = {
                "_type": "AgentLifecycleBaseline",
                "_version": 2,
                "status": "failed" if validation_error is not None else "passed",
                "startedAt": started_at,
                "finishedAt": now_iso(),
                "provider": redact(provider),
                "configObservation": redact(config_observation),
                "fixture": fixture,
                "turn": turn,
                "classification": {
                    **classify_wait_stages(turn["lifecycle"]),
                    "evidence": {
                        "timeToFirstByteMs": turn["lifecycle"].get("timeToFirstByteMs", 0),
                        "timeToFirstVisibleOutputMs": turn["lifecycle"].get(
                            "timeToFirstVisibleOutputMs", 0
                        ),
                        "providerWaitMs": turn["lifecycle"].get("providerWaitMs", 0),
                        "modelGenerationMs": turn["lifecycle"].get("modelGenerationMs", 0),
                        "toolExecutionMs": turn["lifecycle"].get("toolExecutionMs", 0),
                        "retryCount": turn["lifecycle"].get("retryCount", 0),
                    },
                },
            }
            if validation_error is not None:
                report["error"] = f"baseline validation failed: {validation_error}"
            write_json(report_path, redact(report))
            if validation_error is not None:
                raise AcceptanceError(
                    f"{validation_error}; report={report_path.as_posix()}"
                ) from validation_error
            return report
    finally:
        if original_bridge is None:
            os.environ.pop("STORYDEX_COOMI_BRIDGE", None)
        else:
            os.environ["STORYDEX_COOMI_BRIDGE"] = original_bridge
        if original_routing_mode is None:
            os.environ.pop("AGENT_INTENT_ROUTING_MODE", None)
        else:
            os.environ["AGENT_INTENT_ROUTING_MODE"] = original_routing_mode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-id", default="OPENCODE")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--reasoning-effort", default="low", choices=("auto", "low", "medium", "high", "xhigh", "max"))
    parser.add_argument(
        "--config",
        default="",
        help="Storydex providers.json or OpenCode opencode.json; only the selected provider is copied",
    )
    parser.add_argument("--turn-timeout", type=int, default=300)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--bridge", default="")
    parser.add_argument("--backend-repository-root", default="")
    parser.add_argument(
        "--intent-routing-mode",
        default="",
        choices=("", "legacy", "direct", "hybrid", "workflow"),
    )
    parser.add_argument(
        "--enable-parallel-tool-calls",
        action="store_true",
        help="Enable parallel tool calls only in the temporary isolated provider copy",
    )
    parser.add_argument(
        "--scenario",
        default="strict-single-read",
        choices=(
            "strict-single-read",
            "strict-multi-read",
            "character-field-edit",
            "chapter-middle-read",
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = run_baseline(args)
        report_path = (
            Path(args.output_dir).resolve() / "baseline-report.json"
            if str(args.output_dir or "").strip()
            else REPOSITORY_ROOT / "output" / "agent-lifecycle-baseline"
        )
        # ``run_baseline`` writes the authoritative path; print it without
        # retaining temporary workspace paths in the process output.
        candidates = sorted(
            (Path(args.output_dir).resolve() if str(args.output_dir or "").strip() else REPOSITORY_ROOT / "output" / "agent-lifecycle-baseline").glob("**/baseline-report.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            report_path = candidates[0]
        print(json.dumps({"status": report.get("status"), "report": report_path.as_posix()}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
