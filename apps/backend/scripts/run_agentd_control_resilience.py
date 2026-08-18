"""Run isolated black-box checks for the Refactor Agent control plane."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping

import httpx

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPOSITORY_ROOT / "apps" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.run_agent_stream_differential import AgentdProcess  # noqa: E402
from scripts.run_graph_live_acceptance import (  # noqa: E402
    AcceptanceError,
    load_isolated_provider,
    provider_config_path,
)

SESSION_ID = "control-resilience"
APPROVAL_IDS = ("approval-concurrent-a", "approval-concurrent-b")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _runtime_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    suffix = ".exe" if os.name == "nt" else ""
    target = REPOSITORY_ROOT / "apps" / "desktop" / "agent-runtime" / "target" / "debug"
    agentd = Path(args.agentd).resolve() if args.agentd else target / f"storydex-agentd{suffix}"
    bridge = Path(args.bridge).resolve() if args.bridge else target / f"storydex-coomi-bridge{suffix}"
    if not agentd.is_file():
        raise AcceptanceError(f"debug storydex-agentd is missing: {agentd}")
    if not bridge.is_file():
        raise AcceptanceError(f"debug storydex-coomi-bridge is missing: {bridge}")
    return agentd, bridge


def _source_config(args: argparse.Namespace) -> Path:
    path = Path(args.config).resolve() if args.config else provider_config_path()
    if not path.is_file():
        raise AcceptanceError(f"Storydex provider config is missing: {path}")
    return path


def _prepare_home(
    source_config: Path, home: Path, provider_id: str, model: str
) -> None:
    load_isolated_provider(source_config, home, provider_id, model)


def _auth_headers(agentd: AgentdProcess) -> dict[str, str]:
    return {"Authorization": f"Bearer {agentd.token}"}


def _post(
    agentd: AgentdProcess, path: str, payload: Mapping[str, Any], timeout: float = 10.0
) -> httpx.Response:
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        return client.post(
            agentd.base_url + path,
            headers=_auth_headers(agentd),
            json=dict(payload),
        )


def _mailbox_contention(
    *,
    root: Path,
    workspace: Path,
    source_config: Path,
    agentd_binary: Path,
    bridge: Path,
    replay_fixture: Path,
    provider_id: str,
    model: str,
) -> dict[str, Any]:
    homes = [root / "mailbox-home-a", root / "mailbox-home-b"]
    for home in homes:
        _prepare_home(source_config, home, provider_id, model)
    agents = [
        AgentdProcess(
            binary=agentd_binary,
            bridge=bridge,
            coomi_home=home,
            refactor_root=root,
            replay_fixture=replay_fixture,
            log_path=root / f"mailbox-agentd-{index}.log",
        )
        for index, home in enumerate(homes, start=1)
    ]
    try:
        for agent in agents:
            agent.start()
        barrier = threading.Barrier(2)

        def enqueue(index: int) -> dict[str, Any]:
            barrier.wait(timeout=5.0)
            response = _post(
                agents[index],
                "/api/v1/agent/followups",
                {
                    "messageId": f"multiprocess-{index + 1}",
                    "sessionId": SESSION_ID,
                    "workspaceRoot": str(workspace),
                    "content": f"multiprocess-content-{index + 1}",
                    "mode": "queued",
                },
            )
            response.raise_for_status()
            return response.json()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(enqueue, range(2)))
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            response = client.get(
                agents[0].base_url + "/api/v1/agent/followups",
                headers=_auth_headers(agents[0]),
                params={"sessionId": SESSION_ID, "workspaceRoot": str(workspace)},
            )
        response.raise_for_status()
        mailbox = response.json().get("data") or {}
        messages = mailbox.get("messages") if isinstance(mailbox, Mapping) else []
        events = mailbox.get("events") if isinstance(mailbox, Mapping) else []
        message_ids = sorted(
            str(item.get("messageId") or "")
            for item in messages or []
            if isinstance(item, Mapping)
        )
        expected_ids = ["multiprocess-1", "multiprocess-2"]
        if message_ids != expected_ids:
            raise AcceptanceError(
                f"multi-process mailbox lost an update: {message_ids}"
            )
        if int(mailbox.get("revision") or 0) != 2 or len(events or []) != 2:
            raise AcceptanceError("multi-process mailbox revision/event sequence drifted")
        return {
            "status": "passed",
            "processCount": 2,
            "responses": len(results),
            "messageIds": message_ids,
            "revision": mailbox.get("revision"),
            "eventCount": len(events or []),
        }
    finally:
        for agent in reversed(agents):
            agent.stop()


def _fake_bridge(root: Path) -> Path:
    script = root / "concurrent-approval-bridge.py"
    script.write_text(
        """import json
import sys

request = sys.stdin.readline()
if not request:
    raise SystemExit(2)
for request_id, question in (
    (\"approval-concurrent-a\", \"First question?\"),
    (\"approval-concurrent-b\", \"Second question?\"),
):
    print(json.dumps({
        \"type\": \"user_input_request\",
        \"protocolVersion\": 1,
        \"data\": {
            \"requestId\": request_id,
            \"request\": {\"questions\": [{\"id\": request_id, \"question\": question}]},
        },
    }), flush=True)
resolved = set()
for line in sys.stdin:
    value = json.loads(line)
    if value.get(\"action\") == \"resolve\":
        resolved.add(value.get(\"requestId\"))
    if resolved == {\"approval-concurrent-a\", \"approval-concurrent-b\"}:
        print(json.dumps({\"type\": \"text\", \"data\": {\"text\": \"CONCURRENT_APPROVALS_RESOLVED\"}}), flush=True)
        print(json.dumps({\"type\": \"completed\", \"data\": {}}), flush=True)
        raise SystemExit(0)
raise SystemExit(3)
""",
        encoding="utf-8",
    )
    if os.name == "nt":
        wrapper = root / "concurrent-approval-bridge.cmd"
        wrapper.write_text(
            "@echo off\r\n" + subprocess.list2cmdline([sys.executable, str(script)]) + "\r\n",
            encoding="utf-8",
        )
    else:
        wrapper = root / "concurrent-approval-bridge.sh"
        wrapper.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))}\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
    return wrapper


def _stream_events(
    agentd: AgentdProcess,
    workspace: Path,
    events: list[dict[str, Any]],
    approvals_ready: threading.Event,
    errors: list[str],
) -> None:
    try:
        with httpx.Client(timeout=30.0, trust_env=False) as client:
            with client.stream(
                "POST",
                agentd.base_url + "/api/v1/agent/chat/stream",
                headers={
                    **_auth_headers(agentd),
                    "x-session-id": SESSION_ID,
                    "x-trace-id": "concurrent-approval-trace",
                },
                json={
                    "prompt": "Read-only approval contract. Ask one question before returning the fixed marker STORYDEX_AGENT_STREAM_APPROVAL_REPLY_4C8E.",
                    "workspaceRoot": str(workspace),
                    "reasoningEffort": "low",
                    "storyGeneration": {},
                    "confirmNoSnapshot": True,
                    "permissionMode": "ask_approval",
                    "capabilityMode": "read_only",
                },
            ) as response:
                response.raise_for_status()
                event_name = ""
                for line in response.iter_lines():
                    if line.startswith("event:"):
                        event_name = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        payload = json.loads(line.split(":", 1)[1].strip())
                        events.append({"event": event_name, "data": payload})
                        approval_ids = {
                            str(item["data"].get("approvalId") or "")
                            for item in events
                            if item["event"] == "PermissionRequest"
                        }
                        if approval_ids == set(APPROVAL_IDS):
                            approvals_ready.set()
    except Exception as exc:  # pragma: no cover - surfaced by the caller
        errors.append(str(exc))
        approvals_ready.set()


def _concurrent_approvals(
    *,
    root: Path,
    workspace: Path,
    source_config: Path,
    agentd_binary: Path,
    replay_fixture: Path,
    provider_id: str,
    model: str,
) -> dict[str, Any]:
    home = root / "approval-home"
    _prepare_home(source_config, home, provider_id, model)
    agentd = AgentdProcess(
        binary=agentd_binary,
        bridge=_fake_bridge(root),
        coomi_home=home,
        refactor_root=root,
        replay_fixture=replay_fixture,
        log_path=root / "approval-agentd.log",
    )
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    approvals_ready = threading.Event()
    try:
        agentd.start()
        stream = threading.Thread(
            target=_stream_events,
            args=(agentd, workspace, events, approvals_ready, errors),
            daemon=True,
        )
        stream.start()
        if not approvals_ready.wait(timeout=10.0):
            raise AcceptanceError("two concurrent approvals were not exposed")
        if errors:
            raise AcceptanceError(f"approval stream failed: {errors[0]}")
        barrier = threading.Barrier(2)

        def resolve(request_id: str) -> dict[str, Any]:
            barrier.wait(timeout=5.0)
            response = _post(
                agentd,
                "/api/v1/agent/coomi/approval",
                {
                    "approvalId": request_id,
                    "decision": "answer",
                    "response": {"answers": {request_id: "accepted"}},
                    "sessionId": SESSION_ID,
                    "expectedTraceId": "concurrent-approval-trace",
                    "workspaceRoot": str(workspace),
                },
            )
            response.raise_for_status()
            return response.json()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(resolve, APPROVAL_IDS))
        stream.join(timeout=10.0)
        if stream.is_alive():
            raise AcceptanceError("approval stream did not terminate")
        if errors:
            raise AcceptanceError(f"approval stream failed: {errors[0]}")
        accepted = [bool((item.get("data") or {}).get("accepted")) for item in responses]
        event_names = [str(item.get("event") or "") for item in events]
        if accepted != [True, True]:
            raise AcceptanceError(f"concurrent approval resolution failed: {accepted}")
        if event_names.count("PermissionRequest") != 2:
            raise AcceptanceError("approval request count drifted")
        if event_names[-2:] != ["AgentCompleted", "done"]:
            raise AcceptanceError(f"approval terminal sequence drifted: {event_names[-2:]}")
        return {
            "status": "passed",
            "pendingCount": 2,
            "acceptedCount": sum(accepted),
            "permissionRequestCount": event_names.count("PermissionRequest"),
            "terminalSequence": event_names[-2:],
        }
    finally:
        agentd.stop()


def _child_process_ids(parent_pid: int, process_name: str) -> list[int]:
    if os.name == "nt":
        command = (
            "$items = Get-CimInstance Win32_Process -Filter "
            f"\"ParentProcessId = {parent_pid}\"; "
            f"$items | Where-Object {{ $_.Name -eq '{process_name}' }} | "
            "ForEach-Object { $_.ProcessId }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    else:
        result = subprocess.run(
            ["ps", "-o", "pid=,comm=", "--ppid", str(parent_pid)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return [
            int(line.split()[0])
            for line in result.stdout.splitlines()
            if len(line.split()) >= 2
            and Path(line.split()[1]).name == process_name
            and line.split()[0].isdigit()
        ]
    return [int(value) for value in result.stdout.split() if value.isdigit()]


def _process_exists(process_id: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"if (Get-Process -Id {process_id} -ErrorAction SilentlyContinue) {{ 'present' }}",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return result.stdout.strip() == "present"
    result = subprocess.run(
        ["ps", "-p", str(process_id), "-o", "pid="],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return bool(result.stdout.strip())


def _process_crash_cancellation(
    *,
    root: Path,
    workspace: Path,
    source_config: Path,
    agentd_binary: Path,
    bridge: Path,
    replay_fixture: Path,
    provider_id: str,
    model: str,
) -> dict[str, Any]:
    home = root / "crash-home"
    _prepare_home(source_config, home, provider_id, model)
    agentd = AgentdProcess(
        binary=agentd_binary,
        bridge=bridge,
        coomi_home=home,
        refactor_root=root,
        replay_fixture=replay_fixture,
        log_path=root / "crash-agentd.log",
    )
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    permission_ready = threading.Event()
    try:
        agentd.start()
        stream = threading.Thread(
            target=_stream_events,
            args=(agentd, workspace, events, permission_ready, errors),
            daemon=True,
        )
        stream.start()
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if any(item.get("event") == "PermissionRequest" for item in events):
                break
            if errors:
                raise AcceptanceError(f"crash stream failed before approval: {errors[0]}")
            time.sleep(0.05)
        else:
            raise AcceptanceError("real bridge did not reach a pending approval")
        process = agentd.process
        if process is None:
            raise AcceptanceError("storydex-agentd process handle is unavailable")
        children = _child_process_ids(process.pid, bridge.name)
        if len(children) != 1:
            raise AcceptanceError(f"expected one bridge child, got {len(children)}")
        bridge_pid = children[0]
        process.kill()
        process.wait(timeout=10.0)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and _process_exists(bridge_pid):
            time.sleep(0.1)
        bridge_exited = not _process_exists(bridge_pid)
        if not bridge_exited:
            raise AcceptanceError("bridge survived the agentd control-channel crash")
        stream.join(timeout=3.0)
        return {
            "status": "passed",
            "permissionObserved": True,
            "agentdCrashed": process.returncode != 0,
            "bridgeExited": bridge_exited,
            "orphanBridgeCount": 0,
        }
    finally:
        agentd.stop()


def run(args: argparse.Namespace) -> dict[str, Any]:
    agentd_binary, bridge = _runtime_paths(args)
    source_config = _source_config(args)
    fixture_dir = (
        REPOSITORY_ROOT
        / "apps"
        / "backend"
        / "contracts"
        / "fixtures"
        / "agent-chat-stream-approval-v1"
    )
    replay_fixture = fixture_dir / "provider-replay.json"
    output_root = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else REPOSITORY_ROOT
        / "output"
        / "agent-runtime-contract"
        / "control-resilience-current"
    )
    output_root.mkdir(parents=True, exist_ok=True)
    report_path = output_root / "acceptance-report.json"
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "contractId": "agent.control.resilience.v1",
        "status": "failed",
        "providerMode": "replay",
        "providerId": str(args.provider_id),
        "model": str(args.model),
        "stableSwitched": False,
        "cases": {},
    }
    try:
        with tempfile.TemporaryDirectory(prefix="storydex-agentd-control-") as raw_root:
            root = Path(raw_root).resolve()
            workspace = root / "workspace"
            (workspace / "chapters").mkdir(parents=True)
            report["cases"]["multiProcessMailbox"] = _mailbox_contention(
                root=root,
                workspace=workspace,
                source_config=source_config,
                agentd_binary=agentd_binary,
                bridge=bridge,
                replay_fixture=replay_fixture,
                provider_id=str(args.provider_id),
                model=str(args.model),
            )
            report["cases"]["concurrentApprovals"] = _concurrent_approvals(
                root=root,
                workspace=workspace,
                source_config=source_config,
                agentd_binary=agentd_binary,
                replay_fixture=replay_fixture,
                provider_id=str(args.provider_id),
                model=str(args.model),
            )
            report["cases"]["processCrashCancellation"] = _process_crash_cancellation(
                root=root,
                workspace=workspace,
                source_config=source_config,
                agentd_binary=agentd_binary,
                bridge=bridge,
                replay_fixture=replay_fixture,
                provider_id=str(args.provider_id),
                model=str(args.model),
            )
        report["status"] = "passed"
        _write_json(report_path, report)
        return {**report, "reportPath": report_path.as_posix()}
    except Exception as exc:
        report["error"] = str(exc)
        _write_json(report_path, report)
        raise AcceptanceError(f"{exc}; report={report_path.as_posix()}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-id", default="OPENCODE")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--config", default="")
    parser.add_argument("--bridge", default="")
    parser.add_argument("--agentd", default="")
    parser.add_argument("--output-dir", default="")
    return parser.parse_args()


def main() -> int:
    try:
        report = run(parse_args())
        print(
            json.dumps(
                {"status": report.get("status"), "report": report.get("reportPath")},
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
