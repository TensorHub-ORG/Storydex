"""Measure the Python Stable and Rust Refactor Agent replay paths.

The runner uses deterministic provider replays and isolated workspaces. It is
intended to measure local runtime work, not live provider latency.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

import httpx


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPOSITORY_ROOT / "apps" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.run_agent_stream_differential import AgentdProcess  # noqa: E402
from scripts.run_agent_stream_replay_contract import (  # noqa: E402
    _prepare_contract_workspace,
)
from scripts.run_graph_live_acceptance import (  # noqa: E402
    AcceptanceError,
    BackendProcess,
    free_port,
    load_isolated_provider,
    provider_config_path,
)
from services.agent_stream_contract import (  # noqa: E402
    load_fixture,
    validate_chat_stream_events,
)


READ_ONLY_FIXTURE = (
    BACKEND_ROOT
    / "contracts"
    / "fixtures"
    / "agent-chat-stream-read-only-v1"
)
CANCEL_FIXTURE = (
    BACKEND_ROOT
    / "contracts"
    / "fixtures"
    / "agent-chat-stream-cancel-v1"
)
TERMINAL_EVENTS = frozenset({"AgentCompleted", "AgentError", "AgentCancelled"})
MILESTONE_EVENTS = {
    "TurnContract": "turnContractMs",
    "AgentStarted": "agentStartedMs",
    "RuntimeMetrics": "runtimeMetricsMs",
    "ToolStart": "toolStartMs",
}
GATE_DECISION_EVIDENCE = (
    "output/rust-migration-decision-live/"
    "eb2805f48eed-20260818T014444-m0-performance-gate/decision-report.json"
)
USER_VISIBLE_P95_FACTOR = 1.10
LOCAL_MEDIAN_FACTOR = 0.80
LOCAL_P95_FACTOR = 1.00
IDLE_RSS_FACTOR = 0.80
COMPONENT_INVESTIGATION_FACTOR = 5.00


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        max(0, int(round((len(ordered) - 1) * percentile))),
    )
    return ordered[index]


def _round_ms(value: float) -> float:
    return round(max(0.0, float(value)), 3)


def _summarize_samples(samples: list[Mapping[str, Any]]) -> dict[str, Any]:
    metric_names = sorted(
        {
            str(key)
            for sample in samples
            for key, value in sample.items()
            if str(key).endswith("Ms")
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        }
    )
    metrics: dict[str, Any] = {}
    for name in metric_names:
        values = [
            float(sample[name])
            for sample in samples
            if isinstance(sample.get(name), (int, float))
            and not isinstance(sample.get(name), bool)
        ]
        metrics[name] = {
            "sampleCount": len(values),
            "medianMs": _round_ms(statistics.median(values)),
            "p95Ms": _round_ms(_percentile(values, 0.95)),
            "minMs": _round_ms(min(values)),
            "maxMs": _round_ms(max(values)),
        }
    return {
        "sampleCount": len(samples),
        "metrics": metrics,
        "samples": [dict(sample) for sample in samples],
    }


def _comparison(
    python_summary: Mapping[str, Any], rust_summary: Mapping[str, Any]
) -> dict[str, Any]:
    python_metrics = python_summary.get("metrics")
    python_metrics = python_metrics if isinstance(python_metrics, Mapping) else {}
    rust_metrics = rust_summary.get("metrics")
    rust_metrics = rust_metrics if isinstance(rust_metrics, Mapping) else {}
    result: dict[str, Any] = {}
    for name in sorted(set(python_metrics) & set(rust_metrics)):
        python_metric = python_metrics.get(name)
        rust_metric = rust_metrics.get(name)
        if not isinstance(python_metric, Mapping) or not isinstance(
            rust_metric, Mapping
        ):
            continue
        python_median = float(python_metric.get("medianMs") or 0.0)
        rust_median = float(rust_metric.get("medianMs") or 0.0)
        python_p95 = float(python_metric.get("p95Ms") or 0.0)
        rust_p95 = float(rust_metric.get("p95Ms") or 0.0)
        result[str(name)] = {
            "pythonMedianMs": _round_ms(python_median),
            "rustMedianMs": _round_ms(rust_median),
            "medianRatio": (
                round(rust_median / python_median, 4) if python_median else None
            ),
            "pythonP95Ms": _round_ms(python_p95),
            "rustP95Ms": _round_ms(rust_p95),
            "p95Ratio": round(rust_p95 / python_p95, 4) if python_p95 else None,
        }
    return result


def _metric(
    report: Mapping[str, Any],
    implementation: str,
    scenario: str,
    metric: str,
) -> Mapping[str, Any]:
    implementations = report.get("implementations")
    implementations = (
        implementations if isinstance(implementations, Mapping) else {}
    )
    implementation_value = implementations.get(implementation)
    implementation_value = (
        implementation_value if isinstance(implementation_value, Mapping) else {}
    )
    scenario_value = implementation_value.get(scenario)
    scenario_value = scenario_value if isinstance(scenario_value, Mapping) else {}
    metrics = scenario_value.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    value = metrics.get(metric)
    return value if isinstance(value, Mapping) else {}


def _relative_gate_check(
    report: Mapping[str, Any],
    *,
    scenario: str,
    metric: str,
    statistic: str,
    factor: float,
    category: str,
) -> dict[str, Any]:
    python_metric = _metric(report, "pythonStable", scenario, metric)
    rust_metric = _metric(report, "rustRefactor", scenario, metric)
    baseline = float(python_metric.get(statistic) or 0.0)
    actual = float(rust_metric.get(statistic) or 0.0)
    sample_count = min(
        int(python_metric.get("sampleCount") or 0),
        int(rust_metric.get("sampleCount") or 0),
    )
    limit = baseline * factor
    passed = bool(sample_count >= 20 and baseline > 0 and actual <= limit)
    return {
        "category": category,
        "scenario": scenario,
        "metric": metric,
        "statistic": statistic,
        "factor": factor,
        "baselineMs": _round_ms(baseline),
        "limitMs": _round_ms(limit),
        "actualMs": _round_ms(actual),
        "sampleCount": sample_count,
        "status": "passed" if passed else "failed",
    }


def _evaluate_performance_gate(report: Mapping[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for scenario, metric in (
        ("readOnly", "startupToHealthMs"),
        ("readOnly", "firstEventMs"),
        ("cancellation", "firstEventMs"),
        ("cancellation", "stopRequestToHttpAcceptedMs"),
        ("cancellation", "stopRequestToTerminalMs"),
    ):
        checks.append(
            _relative_gate_check(
                report,
                scenario=scenario,
                metric=metric,
                statistic="p95Ms",
                factor=USER_VISIBLE_P95_FACTOR,
                category="user_visible_p95_non_regression",
            )
        )
    for scenario, metrics in (
        (
            "readOnly",
            (
                "turnContractMs",
                "agentStartedMs",
                "runtimeMetricsMs",
                "toolStartMs",
                "terminalMs",
            ),
        ),
        (
            "cancellation",
            (
                "turnContractMs",
                "agentStartedMs",
                "runtimeMetricsMs",
                "terminalMs",
            ),
        ),
    ):
        for metric in metrics:
            checks.extend(
                (
                    _relative_gate_check(
                        report,
                        scenario=scenario,
                        metric=metric,
                        statistic="medianMs",
                        factor=LOCAL_MEDIAN_FACTOR,
                        category="local_median_improvement",
                    ),
                    _relative_gate_check(
                        report,
                        scenario=scenario,
                        metric=metric,
                        statistic="p95Ms",
                        factor=LOCAL_P95_FACTOR,
                        category="local_p95_non_regression",
                    ),
                )
            )
    implementations = report.get("implementations")
    implementations = (
        implementations if isinstance(implementations, Mapping) else {}
    )
    python_value = implementations.get("pythonStable")
    python_value = python_value if isinstance(python_value, Mapping) else {}
    rust_value = implementations.get("rustRefactor")
    rust_value = rust_value if isinstance(rust_value, Mapping) else {}
    python_idle = python_value.get("idleProcessTree")
    python_idle = python_idle if isinstance(python_idle, Mapping) else {}
    rust_idle = rust_value.get("idleProcessTree")
    rust_idle = rust_idle if isinstance(rust_idle, Mapping) else {}
    baseline_rss = int(python_idle.get("totalRssBytes") or 0)
    actual_rss = int(rust_idle.get("totalRssBytes") or 0)
    rss_limit = baseline_rss * IDLE_RSS_FACTOR
    rss_passed = bool(
        int(python_idle.get("idleSeconds") or 0) >= 60
        and int(rust_idle.get("idleSeconds") or 0) >= 60
        and baseline_rss > 0
        and actual_rss <= rss_limit
    )
    checks.append(
        {
            "category": "idle_process_tree_rss",
            "metric": "totalRssBytes",
            "factor": IDLE_RSS_FACTOR,
            "baselineBytes": baseline_rss,
            "limitBytes": int(rss_limit),
            "actualBytes": actual_rss,
            "idleSeconds": min(
                int(python_idle.get("idleSeconds") or 0),
                int(rust_idle.get("idleSeconds") or 0),
            ),
            "status": "passed" if rss_passed else "failed",
        }
    )
    investigations: list[dict[str, Any]] = []
    component_metrics = (
        "componentInitMs",
        "hooksInitMs",
        "mcpInitMs",
        "memoryInitMs",
        "projectInstructionsMs",
        "providerConfigMs",
        "providerInitMs",
        "securityInitMs",
        "sessionInitMs",
        "toolsInitMs",
    )
    for scenario in ("readOnly", "cancellation"):
        for metric in component_metrics:
            python_metric = _metric(report, "pythonStable", scenario, metric)
            rust_metric = _metric(report, "rustRefactor", scenario, metric)
            baseline = float(python_metric.get("p95Ms") or 0.0)
            actual = float(rust_metric.get("p95Ms") or 0.0)
            ratio = actual / baseline if baseline else 0.0
            if baseline and ratio > COMPONENT_INVESTIGATION_FACTOR:
                investigations.append(
                    {
                        "scenario": scenario,
                        "metric": metric,
                        "pythonP95Ms": _round_ms(baseline),
                        "rustP95Ms": _round_ms(actual),
                        "p95Ratio": round(ratio, 4),
                        "status": "investigate_before_beta",
                    }
                )
    failed = [check for check in checks if check.get("status") != "passed"]
    return {
        "status": "failed" if failed else "passed",
        "profile": "end_to_end_relative_gate",
        "selectedBy": "Storydex Stable HTTP/SSE + OPENCODE/deepseek-v4-flash",
        "decisionEvidence": GATE_DECISION_EVIDENCE,
        "thresholds": {
            "userVisibleP95Factor": USER_VISIBLE_P95_FACTOR,
            "localMedianFactor": LOCAL_MEDIAN_FACTOR,
            "localP95Factor": LOCAL_P95_FACTOR,
            "idleProcessTreeRssFactor": IDLE_RSS_FACTOR,
            "componentInvestigationFactor": COMPONENT_INVESTIGATION_FACTOR,
            "liveLlmLatencyExcluded": True,
            "replayTotalLatencyGate": False,
        },
        "checks": checks,
        "failedChecks": failed,
        "diagnosticInvestigations": investigations,
    }


def _apply_performance_gate(report: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(report)
    gate = _evaluate_performance_gate(result)
    result["decisionGate"] = gate
    if gate["status"] != "passed":
        result["status"] = "failed"
    return result


def _numeric_runtime_metrics(payload: Mapping[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    for key, value in payload.items():
        if (
            str(key).endswith("Ms")
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            result[str(key)] = _round_ms(float(value))
    return result


def _event_matches(
    name: str,
    payload: Mapping[str, Any],
    expected_name: str,
    expected_fields: Mapping[str, Any],
) -> bool:
    return name == expected_name and all(
        payload.get(str(key)) == value for key, value in expected_fields.items()
    )


def _safe_event_tail(
    events: list[tuple[str, Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    tail: list[dict[str, Any]] = []
    for name, payload in events[-12:]:
        item: dict[str, Any] = {"event": name}
        for field in (
            "phase",
            "status",
            "code",
            "error_type",
            "reason",
            "message",
        ):
            value = payload.get(field)
            if value not in (None, ""):
                item[field] = str(value)[:500]
        details = payload.get("details")
        if isinstance(details, Mapping):
            item["details"] = {
                str(key): value
                for key, value in details.items()
                if str(key)
                in {"stage", "providerHttpStatus", "statusCode", "httpStatus"}
            }
        tail.append(item)
    return tail


def _measure_turn(
    *,
    base_url: str,
    token: str,
    workspace: Path,
    fixture: Mapping[str, Any],
    provider_id: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    request = fixture.get("request")
    if not isinstance(request, Mapping):
        raise AcceptanceError("performance fixture request must be an object")
    payload = dict(request)
    payload["workspaceRoot"] = str(workspace.resolve())
    trace_id = str(uuid.uuid4())
    session_id = f"agent-performance-{uuid.uuid4().hex}"
    headers = {"x-trace-id": trace_id, "x-session-id": session_id}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    interaction = fixture.get("interaction")
    interaction = interaction if isinstance(interaction, Mapping) else {}
    action = str(interaction.get("action") or "").strip()
    after_event = str(interaction.get("afterEvent") or "").strip()
    after_fields = interaction.get("afterEventFields")
    after_fields = after_fields if isinstance(after_fields, Mapping) else {}
    timeout = httpx.Timeout(
        connect=min(30.0, max(1.0, timeout_seconds)),
        read=None,
        write=30.0,
        pool=30.0,
    )
    events: list[tuple[str, dict[str, Any]]] = []
    timings: dict[str, Any] = {}
    triggered = False
    terminal_event = ""
    stop_started: float | None = None
    response_headers: Mapping[str, str] = {}
    request_started = time.perf_counter()
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        with client.stream(
            "POST",
            base_url.rstrip("/") + "/api/v1/agent/chat/stream",
            headers=headers,
            json=payload,
        ) as response:
            response_headers = dict(response.headers)
            timings["requestToResponseHeadersMs"] = _round_ms(
                (time.perf_counter() - request_started) * 1000
            )
            if response.status_code != 200:
                body = response.read().decode("utf-8", errors="replace")[:1000]
                raise AcceptanceError(
                    f"performance chat stream returned HTTP {response.status_code}: {body}"
                )
            current_event = ""
            data_lines: list[str] = []
            for raw_line in response.iter_lines():
                line = str(raw_line)
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                if line or not current_event:
                    continue
                raw_data = "\n".join(data_lines)
                try:
                    decoded = json.loads(raw_data) if raw_data else {}
                except json.JSONDecodeError as exc:
                    raise AcceptanceError(
                        f"performance SSE event {current_event} has invalid JSON"
                    ) from exc
                if not isinstance(decoded, dict):
                    raise AcceptanceError(
                        f"performance SSE event {current_event} data is not an object"
                    )
                name = current_event
                event_payload = decoded
                current_event = ""
                data_lines = []
                elapsed_ms = _round_ms((time.perf_counter() - request_started) * 1000)
                events.append((name, event_payload))
                timings.setdefault("firstEventMs", elapsed_ms)
                milestone = MILESTONE_EVENTS.get(name)
                if milestone:
                    timings.setdefault(milestone, elapsed_ms)
                if name == "RuntimeMetrics":
                    for key, value in _numeric_runtime_metrics(event_payload).items():
                        timings.setdefault(key, value)
                if name in TERMINAL_EVENTS:
                    terminal_event = name
                    timings.setdefault("terminalMs", elapsed_ms)
                    if stop_started is not None:
                        timings["stopRequestToTerminalMs"] = _round_ms(
                            (time.perf_counter() - stop_started) * 1000
                        )
                if (
                    action == "stop"
                    and not triggered
                    and _event_matches(
                        name,
                        event_payload,
                        after_event,
                        after_fields,
                    )
                ):
                    triggered = True
                    stop_started = time.perf_counter()
                    stop_response = client.post(
                        base_url.rstrip("/") + "/api/v1/agent/executions/stop",
                        headers={
                            "Authorization": f"Bearer {token}"
                        }
                        if token
                        else {},
                        json={
                            "sessionId": session_id,
                            "expectedTraceId": trace_id,
                            "workspaceRoot": str(workspace.resolve()),
                        },
                        timeout=30.0,
                    )
                    timings["stopRequestToHttpAcceptedMs"] = _round_ms(
                        (time.perf_counter() - stop_started) * 1000
                    )
                    try:
                        stop_payload = stop_response.json()
                    except ValueError as exc:
                        raise AcceptanceError(
                            "performance stop response is not valid JSON"
                        ) from exc
                    stop_data = (
                        stop_payload.get("data")
                        if isinstance(stop_payload, Mapping)
                        and isinstance(stop_payload.get("data"), Mapping)
                        else {}
                    )
                    if (
                        stop_response.status_code != 200
                        or not isinstance(stop_payload, Mapping)
                        or stop_payload.get("ok") is not True
                        or stop_data.get("accepted") is not True
                    ):
                        raise AcceptanceError(
                            "performance stop request did not accept the active trace"
                        )
    timings["totalMs"] = _round_ms((time.perf_counter() - request_started) * 1000)
    if action and not triggered:
        raise AcceptanceError(
            f"performance interaction did not observe trigger event {after_event!r}; "
            f"tail={_safe_event_tail(events)!r}"
        )
    try:
        observation = validate_chat_stream_events(
            events,
            status_code=200,
            headers=response_headers,
            trace_id=trace_id,
            session_id=session_id,
            fixture=fixture,
            expected_provider=provider_id,
            expected_model=model,
            require_turn_contract=True,
        )
    except Exception as exc:
        raise AcceptanceError(
            f"{exc}; tail={_safe_event_tail(events)!r}"
        ) from exc
    timings.update(
        {
            "terminalEvent": terminal_event,
            "eventCount": int(observation.get("eventCount") or 0),
        }
    )
    return timings


@contextmanager
def _temporary_environment(updates: Mapping[str, str]) -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in updates}
    try:
        os.environ.update({name: str(value) for name, value in updates.items()})
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def _runtime(
    *,
    implementation: str,
    fixture_dir: Path,
    fixture: Mapping[str, Any],
    source_config: Path,
    provider_id: str,
    model: str,
    binary: Path,
    bridge: Path,
) -> Iterator[dict[str, Any]]:
    with tempfile.TemporaryDirectory(
        prefix=f"storydex-agent-performance-{implementation}-"
    ) as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        coomi_home = root / "coomi-home"
        _prepare_contract_workspace(workspace, fixture)
        load_isolated_provider(
            source_config,
            coomi_home,
            provider_id,
            model,
        )
        replay_path = fixture_dir / "provider-replay.json"
        if implementation == "pythonStable":
            process: BackendProcess | AgentdProcess = BackendProcess(
                workspace=workspace,
                coomi_home=coomi_home,
                log_path=root / "backend.log",
                port=free_port(),
                repository_root=REPOSITORY_ROOT,
            )
        else:
            process = AgentdProcess(
                binary=binary,
                bridge=bridge,
                coomi_home=coomi_home,
                refactor_root=root,
                replay_fixture=replay_path,
                log_path=root / "agentd.log",
            )
        routing_mode = str(fixture.get("intentRoutingMode") or "hybrid").strip()
        environment = {
            "STORYDEX_COOMI_BRIDGE": str(bridge),
            "STORYDEX_AGENT_PROVIDER_REPLAY_FIXTURE": str(replay_path),
            "AGENT_INTENT_ROUTING_MODE": routing_mode,
        }
        started = time.perf_counter()
        try:
            with _temporary_environment(environment):
                process.start()
            headers = (
                {"Authorization": f"Bearer {process.token}"}
                if isinstance(process, AgentdProcess)
                else {}
            )
            with httpx.Client(timeout=15.0, trust_env=False) as client:
                health = client.get(
                    process.base_url.rstrip("/") + "/api/v1/sys/health",
                    headers=headers,
                )
            if health.status_code != 200:
                raise AcceptanceError(
                    f"{implementation} health returned HTTP {health.status_code}"
                )
            startup_ms = _round_ms((time.perf_counter() - started) * 1000)
            raw_process = process.process
            if raw_process is None:
                raise AcceptanceError(f"{implementation} process handle is missing")
            yield {
                "baseUrl": process.base_url,
                "token": process.token if isinstance(process, AgentdProcess) else "",
                "workspace": workspace,
                "pid": int(raw_process.pid),
                "startupToHealthMs": startup_ms,
            }
        finally:
            process.stop()


def _run_sample(
    *,
    implementation: str,
    fixture_dir: Path,
    source_config: Path,
    provider_id: str,
    model: str,
    binary: Path,
    bridge: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    fixture = load_fixture(fixture_dir / "scenario.json")
    with _runtime(
        implementation=implementation,
        fixture_dir=fixture_dir,
        fixture=fixture,
        source_config=source_config,
        provider_id=provider_id,
        model=model,
        binary=binary,
        bridge=bridge,
    ) as runtime:
        turn = _measure_turn(
            base_url=str(runtime["baseUrl"]),
            token=str(runtime["token"]),
            workspace=Path(runtime["workspace"]),
            fixture=fixture,
            provider_id=provider_id,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        return {"startupToHealthMs": runtime["startupToHealthMs"], **turn}


def _windows_process_tree_rss(root_pid: int) -> dict[str, Any]:
    script = f"""
$rootId = {int(root_pid)}
$rows = @(Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId)
$ids = [System.Collections.Generic.HashSet[int]]::new()
[void]$ids.Add($rootId)
do {{
  $changed = $false
  foreach ($row in $rows) {{
    if ($ids.Contains([int]$row.ParentProcessId) -and $ids.Add([int]$row.ProcessId)) {{
      $changed = $true
    }}
  }}
}} while ($changed)
$items = @()
foreach ($id in $ids) {{
  $process = Get-Process -Id $id -ErrorAction SilentlyContinue
  if ($null -ne $process) {{
    $items += [pscustomobject]@{{
      pid = [int]$process.Id
      name = [string]$process.ProcessName
      rssBytes = [int64]$process.WorkingSet64
    }}
  }}
}}
[pscustomobject]@{{
  processCount = $items.Count
  totalRssBytes = [int64](($items | Measure-Object -Property rssBytes -Sum).Sum)
  processes = $items
}} | ConvertTo-Json -Compress -Depth 4
"""
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    result = json.loads(completed.stdout)
    if not isinstance(result, dict):
        raise AcceptanceError("Windows process tree RSS result is not an object")
    return result


def _linux_process_tree_rss(root_pid: int) -> dict[str, Any]:
    rows: dict[int, int] = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text(encoding="utf-8").split()
            rows[int(fields[0])] = int(fields[3])
        except (OSError, ValueError, IndexError):
            continue
    ids = {int(root_pid)}
    changed = True
    while changed:
        changed = False
        for pid, parent in rows.items():
            if parent in ids and pid not in ids:
                ids.add(pid)
                changed = True
    processes: list[dict[str, Any]] = []
    for pid in sorted(ids):
        try:
            status = (Path("/proc") / str(pid) / "status").read_text(
                encoding="utf-8"
            )
        except OSError:
            continue
        name = ""
        rss_bytes = 0
        for line in status.splitlines():
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("VmRSS:"):
                rss_bytes = int(line.split()[1]) * 1024
        processes.append({"pid": pid, "name": name, "rssBytes": rss_bytes})
    return {
        "processCount": len(processes),
        "totalRssBytes": sum(int(item["rssBytes"]) for item in processes),
        "processes": processes,
    }


def _process_tree_rss(root_pid: int) -> dict[str, Any]:
    if os.name == "nt":
        return _windows_process_tree_rss(root_pid)
    if sys.platform.startswith("linux"):
        return _linux_process_tree_rss(root_pid)
    raise AcceptanceError(f"process tree RSS is unsupported on {sys.platform}")


def _measure_idle_rss(
    *,
    implementation: str,
    idle_seconds: int,
    source_config: Path,
    provider_id: str,
    model: str,
    binary: Path,
    bridge: Path,
) -> dict[str, Any]:
    fixture = load_fixture(READ_ONLY_FIXTURE / "scenario.json")
    with _runtime(
        implementation=implementation,
        fixture_dir=READ_ONLY_FIXTURE,
        fixture=fixture,
        source_config=source_config,
        provider_id=provider_id,
        model=model,
        binary=binary,
        bridge=bridge,
    ) as runtime:
        time.sleep(max(0, int(idle_seconds)))
        snapshot = _process_tree_rss(int(runtime["pid"]))
        return {
            "idleSeconds": max(0, int(idle_seconds)),
            "startupToHealthMs": runtime["startupToHealthMs"],
            **snapshot,
        }


def _cpu_name() -> str:
    fallback = str(platform.processor() or platform.machine()).strip()
    if os.name != "nt":
        return fallback
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name)",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return completed.stdout.strip() or fallback
    except (OSError, subprocess.CalledProcessError):
        return fallback


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout.strip()


def _binary_profile(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    content = resolved.read_bytes()
    lowered = [part.casefold() for part in resolved.parts]
    build_type = "debug" if "debug" in lowered else "release"
    return {
        "path": resolved.as_posix(),
        "buildType": build_type,
        "sizeBytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def run_performance(args: argparse.Namespace) -> dict[str, Any]:
    suffix = ".exe" if os.name == "nt" else ""
    target = (
        REPOSITORY_ROOT / "apps" / "desktop" / "agent-runtime" / "target" / "debug"
    )
    binary = (
        Path(args.agentd).resolve()
        if str(args.agentd or "").strip()
        else target / f"storydex-agentd{suffix}"
    )
    bridge = (
        Path(args.bridge).resolve()
        if str(args.bridge or "").strip()
        else target / f"storydex-coomi-bridge{suffix}"
    )
    source_config = (
        Path(args.config).resolve()
        if str(args.config or "").strip()
        else provider_config_path().resolve()
    )
    for path, label in (
        (binary, "storydex-agentd"),
        (bridge, "storydex-coomi-bridge"),
        (source_config, "provider config"),
    ):
        if not path.is_file():
            raise AcceptanceError(f"{label} is missing: {path}")
    warmups = max(0, int(args.warmups))
    sample_count = max(1, int(args.samples))
    implementations: dict[str, Any] = {}
    for implementation in ("pythonStable", "rustRefactor"):
        scenario_results: dict[str, Any] = {}
        for scenario, fixture_dir in (
            ("readOnly", READ_ONLY_FIXTURE),
            ("cancellation", CANCEL_FIXTURE),
        ):
            for _ in range(warmups):
                _run_sample(
                    implementation=implementation,
                    fixture_dir=fixture_dir,
                    source_config=source_config,
                    provider_id=str(args.provider_id),
                    model=str(args.model),
                    binary=binary,
                    bridge=bridge,
                    timeout_seconds=float(args.timeout),
                )
            samples = [
                _run_sample(
                    implementation=implementation,
                    fixture_dir=fixture_dir,
                    source_config=source_config,
                    provider_id=str(args.provider_id),
                    model=str(args.model),
                    binary=binary,
                    bridge=bridge,
                    timeout_seconds=float(args.timeout),
                )
                for _ in range(sample_count)
            ]
            scenario_results[scenario] = _summarize_samples(samples)
        scenario_results["idleProcessTree"] = _measure_idle_rss(
            implementation=implementation,
            idle_seconds=max(0, int(args.idle_seconds)),
            source_config=source_config,
            provider_id=str(args.provider_id),
            model=str(args.model),
            binary=binary,
            bridge=bridge,
        )
        implementations[implementation] = scenario_results
    report = {
        "_type": "AgentRefactorPerformanceWindow",
        "_version": 1,
        "status": "passed",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "head": _git_head(),
        "provider": {
            "providerId": str(args.provider_id),
            "model": str(args.model),
            "mode": "replay",
            "networkLatencyIncluded": False,
        },
        "environment": {
            "os": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "cpu": _cpu_name(),
            "logicalCpuCount": os.cpu_count(),
            "pythonVersion": platform.python_version(),
            "pythonExecutable": Path(sys.executable).resolve().as_posix(),
            "agentd": _binary_profile(binary),
            "bridge": _binary_profile(bridge),
        },
        "methodology": {
            "warmupSamplesDiscarded": warmups,
            "formalSamplesPerImplementationAndScenario": sample_count,
            "sampleIsolation": "fresh process, workspace, Coomi home, and session",
            "clock": "time.perf_counter",
            "statistics": "median and nearest-rank p95 over formal samples",
            "idleSeconds": max(0, int(args.idle_seconds)),
            "totalLatencyUse": "deterministic local replay comparison only",
        },
        "fixtures": {
            "readOnly": READ_ONLY_FIXTURE.relative_to(REPOSITORY_ROOT).as_posix(),
            "cancellation": CANCEL_FIXTURE.relative_to(REPOSITORY_ROOT).as_posix(),
        },
        "implementations": implementations,
        "comparisons": {
            scenario: _comparison(
                implementations["pythonStable"][scenario],
                implementations["rustRefactor"][scenario],
            )
            for scenario in ("readOnly", "cancellation")
        },
    }
    return _apply_performance_gate(report)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-id", default="OPENCODE")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--config", default="")
    parser.add_argument("--bridge", default="")
    parser.add_argument("--agentd", default="")
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--idle-seconds", type=int, default=60)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--evaluate-existing", default="")
    parser.add_argument("--output", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path = (
        Path(args.output).resolve()
        if str(args.output or "").strip()
        else REPOSITORY_ROOT
        / "output"
        / "agent-runtime-contract"
        / "m0-performance-current"
        / "performance-report.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if str(args.evaluate_existing or "").strip():
            existing_path = Path(args.evaluate_existing).resolve()
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
            if not isinstance(existing, Mapping) or existing.get("status") != "passed":
                raise AcceptanceError(
                    "--evaluate-existing requires a passed performance report"
                )
            report = _apply_performance_gate(existing)
        else:
            report = run_performance(args)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {"status": report.get("status"), "report": output_path.as_posix()},
                ensure_ascii=False,
            )
        )
        return 0 if report.get("status") == "passed" else 1
    except Exception as exc:
        failed = {
            "_type": "AgentRefactorPerformanceWindow",
            "_version": 1,
            "status": "failed",
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }
        output_path.write_text(
            json.dumps(failed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "status": "failed",
                    "report": output_path.as_posix(),
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
