"""Run a low-risk, repeatable Agent lifecycle baseline against an isolated provider.

The script copies only the selected provider entry into a temporary Coomi home,
starts the normal HTTP/SSE backend route, and writes a redacted report.  It is
intended for phase-A/E diagnostics, not as a production health check.
"""

from __future__ import annotations

import argparse
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
    write_json,
)


MARKER = "STORYDEX_LIFECYCLE_BASELINE_OK"


def prepare_fixture(workspace: Path) -> dict[str, Any]:
    prepare_workspace(workspace)
    source = workspace / "chapters" / "lifecycle-baseline.md"
    source.write_text(
        "# Lifecycle baseline\n\n"
        f"固定验收标记：{MARKER}\n"
        "本文件只用于只读 Agent 生命周期测量。\n",
        encoding="utf-8",
    )
    return {
        "workspaceFiles": ["chapters/lifecycle-baseline.md"],
        "prompt": "固定只读任务：读取该文件并返回标记，不修改任何项目文件。",
        "marker": MARKER,
    }


def provider_config_observation(source: Path, provider_id: str) -> dict[str, Any]:
    document = json.loads(source.read_text(encoding="utf-8-sig"))
    providers = document.get("providers") if isinstance(document, dict) else {}
    provider = providers.get(provider_id) if isinstance(providers, Mapping) else None
    if not isinstance(provider, Mapping):
        raise AcceptanceError(f"provider {provider_id} is missing from {source}")
    observed: dict[str, Any] = {
        "providerId": provider_id,
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
        elif str(key).lower() in {"max_output_tokens", "max_tokens", "supports_reasoning_effort", "reasoning_prompt_fallback"}:
            observed[key] = value
        elif str(key).lower() in {"stream", "stream_options", "tool_protocol"}:
            observed[key] = value
    return observed


def output_limit_observation(provider: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve the configured capability limit and the OpenAI wire default.

    The two values intentionally remain separate: the former is used by the
    engine's context accounting, while the latter is the value placed in the
    OpenAI-compatible request when ``ModelRequest.max_output_tokens`` is None.
    """

    config_source = (REPOSITORY_ROOT / "vendor" / "coomi-rs" / "services" / "src" / "config.rs").read_text(
        encoding="utf-8"
    )
    provider_source = (REPOSITORY_ROOT / "vendor" / "coomi-rs" / "services" / "src" / "provider.rs").read_text(
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
    return {
        "configuredMaxOutputTokens": configured,
        "resolvedCapabilityMaxOutputTokens": capability,
        "wireDefaultMaxTokens": wire_default,
        "requestOverride": None,
        "mismatch": capability != wire_default,
        "source": {
            "capability": "vendor/coomi-rs/services/src/config.rs",
            "wire": "vendor/coomi-rs/services/src/provider.rs",
        },
    }


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


def public_turn(result: Mapping[str, Any], fixture: Mapping[str, Any]) -> dict[str, Any]:
    lifecycle = result.get("lifecycle") if isinstance(result.get("lifecycle"), Mapping) else {}
    protocol = result.get("protocol") if isinstance(result.get("protocol"), Mapping) else {}
    tool_calls = result.get("toolCalls") if isinstance(result.get("toolCalls"), list) else []
    reply_preview = str(result.get("replyPreview") or "")
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
        "toolNames": sorted(
            {
                str(item.get("toolName") or "")
                for item in tool_calls
                if isinstance(item, Mapping) and str(item.get("toolName") or "")
            }
        ),
        "visibleReplyChars": len(reply_preview),
        "markerObserved": MARKER in reply_preview,
        "usage": dict(result.get("usage") or {}) if isinstance(result.get("usage"), Mapping) else {},
        "errors": list(result.get("errors") or []),
        "fixtureMarker": str(fixture.get("marker") or ""),
    }


def validate_baseline_turn(turn: Mapping[str, Any]) -> None:
    """Reject a baseline that does not follow its deliberately tiny contract."""

    tool_names = [str(name) for name in (turn.get("toolNames") or []) if str(name)]
    tool_count = int(turn.get("toolCallCount") or 0)
    if tool_names != ["read_file"] or tool_count != 1:
        raise AcceptanceError(
            "baseline task used an unexpected tool sequence: "
            f"names={tool_names!r}, count={tool_count}"
        )
    lifecycle = turn.get("lifecycle") if isinstance(turn.get("lifecycle"), Mapping) else {}
    tools = lifecycle.get("tools") if isinstance(lifecycle.get("tools"), list) else []
    if any(bool(item.get("error")) for item in tools if isinstance(item, Mapping)):
        raise AcceptanceError("baseline task reported a tool error")
    if not bool(turn.get("markerObserved")):
        raise AcceptanceError("baseline task did not return the fixed acceptance marker")


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
    bridge = REPOSITORY_ROOT / "vendor" / "coomi-rs" / "target" / "debug" / "storydex-coomi-bridge.exe"
    if not bridge.is_file():
        raise AcceptanceError(f"debug bridge is missing: {bridge}")
    try:
        with tempfile.TemporaryDirectory(prefix="storydex-agent-lifecycle-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            coomi_home = root / "coomi-home"
            fixture = prepare_fixture(workspace)
            provider = load_isolated_provider(
                source_config,
                coomi_home,
                args.provider_id,
                args.model,
            )
            config_observation = provider_config_observation(source_config, args.provider_id)
            config_observation["outputLimit"] = output_limit_observation(
                json.loads(source_config.read_text(encoding="utf-8-sig"))["providers"][args.provider_id]
            )
            os.environ["STORYDEX_COOMI_BRIDGE"] = str(bridge)
            port = free_port()
            backend = BackendProcess(
                workspace=workspace,
                coomi_home=coomi_home,
                log_path=output_root / "backend.log",
                port=port,
            )
            client = httpx.Client()
            try:
                backend.start()
                prompt = (
                    "这是一次只读生命周期基线测试。只能调用一次 read_file，读取 "
                    "chapters/lifecycle-baseline.md；不要使用其他工具，不要修改任何文件。"
                    f"读取后只返回固定标记 {MARKER}。"
                )
                result = run_turn(
                    client,
                    base_url=backend.base_url,
                    workspace=workspace,
                    session_id="lifecycle-baseline-" + uuid.uuid4().hex[:10],
                    prompt=prompt,
                    reasoning_effort=args.reasoning_effort,
                    label="lifecycle-baseline",
                    expected_provider=args.provider_id,
                    expected_model=args.model,
                    timeout_seconds=args.turn_timeout,
                )
            finally:
                client.close()
                backend.stop()

            turn = public_turn(result, fixture)
            validate_baseline_turn(turn)
            report = {
                "_type": "AgentLifecycleBaseline",
                "_version": 1,
                "status": "passed",
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
            write_json(report_path, redact(report))
            return report
    finally:
        if original_bridge is None:
            os.environ.pop("STORYDEX_COOMI_BRIDGE", None)
        else:
            os.environ["STORYDEX_COOMI_BRIDGE"] = original_bridge


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-id", default="OPENCODE")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--reasoning-effort", default="low", choices=("auto", "low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--config", default="")
    parser.add_argument("--turn-timeout", type=int, default=300)
    parser.add_argument("--output-dir", default="")
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
