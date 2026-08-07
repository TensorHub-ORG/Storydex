"""Run the real Storydex knowledge-graph acceptance workflow.

This script intentionally exercises the HTTP/SSE route and the configured
Coomi bridge.  It does not probe ``/models``, send a health prompt to the
provider, switch providers, or fabricate a successful result.  A run gets its
own workspace and Coomi home; only the requested provider entry is copied into
the isolated provider document.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

import httpx


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPOSITORY_ROOT / "apps" / "backend"
# The script is normally launched from the repository root, while the
# backend's package imports are rooted at ``apps/backend``.  The subprocess
# gets this through PYTHONPATH, but the parent acceptance process also imports
# backend services during later assertions.
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
API_PREFIX = "/api/v1"
DEFAULT_PROVIDER = os.environ.get("STORYDEX_LIVE_PROVIDER", "").strip()
DEFAULT_MODEL = os.environ.get("STORYDEX_LIVE_MODEL", "").strip()
SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "apiKey",
    "authorization",
    "access_token",
    "refresh_token",
    "secret",
    "password",
    "token",
}


class AcceptanceError(RuntimeError):
    pass


class TurnFailure(AcceptanceError):
    """A failed SSE turn together with its redacted, machine-readable result."""

    def __init__(self, message: str, result: Dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def redact(value: Any, *, key: str = "") -> Any:
    if key in SENSITIVE_KEYS or key.lower() in {item.lower() for item in SENSITIVE_KEYS}:
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def provider_config_path() -> Path:
    user_root = Path(os.environ.get("USERPROFILE") or Path.home())
    return user_root / ".storydex" / ".coomi" / "config" / "providers.json"


def load_isolated_provider(source: Path, destination: Path, provider_id: str, model: str) -> Dict[str, Any]:
    try:
        document = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"cannot read provider config: {exc}") from exc
    providers = document.get("providers") if isinstance(document, dict) else None
    if not isinstance(providers, dict) or not isinstance(providers.get(provider_id), dict):
        raise AcceptanceError(f"provider {provider_id} is missing from {source}")
    provider = dict(providers[provider_id])
    configured_model = str(provider.get("model") or "").strip()
    if configured_model != model:
        raise AcceptanceError(
            f"provider {provider_id} is configured for {configured_model or '<empty>'}, expected {model}"
        )
    isolated = {
        "version": int(document.get("version") or 1),
        "active": provider_id,
        "providers": {provider_id: provider},
    }
    config_path = destination / "config" / "providers.json"
    write_json(config_path, isolated)
    return {
        "providerId": provider_id,
        "model": configured_model,
        "type": str(provider.get("type") or ""),
        "display": str(provider.get("display") or ""),
        "baseUrl": str(provider.get("base_url") or ""),
        "toolProtocol": str(provider.get("tool_protocol") or ""),
        "sourceConfig": source.as_posix(),
        "isolatedConfig": config_path.as_posix(),
        "isolatedConfigRetained": False,
    }


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def prepare_workspace(root: Path) -> Dict[str, Any]:
    for relative in (
        ".storydex/worldbook",
        ".storydex/characters",
        ".storydex/scripts",
        ".storydex/memory/current",
        ".storydex/memory/review",
        ".storydex/.agent/runtime",
        ".storydex/wiki",
        ".storydex/.cache",
        "chapters",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    write_json(
        root / ".storydex/project.json",
        {"name": root.name, "storydex_version": "2026.04", "storySettings": {"fragmentFormat": "md"}},
    )
    write_text(root / ".storydex/worldbook/README.md", "# 世界书\n")
    write_text(root / "chapters/README.md", "# 正文章节\n")

    planets = [
        "夜港星", "晨辉星", "霜环星", "赤潮星", "灰烬星",
        "远潮星", "雾原星", "蓝穹星", "鸣沙星", "寒渊星",
    ]
    creatures = [f"潮汐兽-{index:02d}" for index in range(1, 53)]
    for name in planets + creatures:
        write_text(root / ".storydex/worldbook" / f"{name}.md", f"# {name}\n\n验收设定条目。\n")
    write_json(root / ".storydex/memory/current/entities.json", {"version": 2, "entities": []})
    write_json(root / ".storydex/memory/current/facts.json", {"version": 2, "facts": []})
    return {"planets": planets, "creatures": creatures}


def append_worldbook_entity(root: Path, name: str) -> None:
    path = root / ".storydex/worldbook" / f"{name}.md"
    if not path.exists():
        write_text(path, f"# {name}\n\n正文抽取验收实体。\n")


def prepare_extraction_sources(root: Path) -> None:
    for name in ("潮汐兽", "灰翼兽", "赤甲虫", "雾鲸"):
        append_worldbook_entity(root, name)
    chapter = (
        "# 关系抽取验收\n\n"
        "潮汐兽长期栖息在夜港星浅海。\n\n"
        "灰翼兽并不生活在夜港星。\n\n"
        "如果赤甲虫迁往晨辉星，它或许能存活。\n\n"
        "水手传闻雾鲸来自远潮星。\n"
    )
    write_text(root / "chapters/关系抽取验收.md", chapter)


def install_project_skills_for_acceptance(workspace: Path, coomi_home: Path) -> None:
    """Expose project-local skill documents through the Rust read_skill tool.

    The bridge intentionally resolves read_skill from Coomi's installed-skill
    directory, while Storydex projects keep their authored skills under
    ``.storydex/.agent/skills``.  The live run uses an isolated Coomi home, so
    copy those read-only instruction documents into that home without
    changing the source workspace or provider configuration.
    """
    source_root = workspace / ".storydex" / ".agent" / "skills"
    if not source_root.is_dir():
        return
    for source in source_root.glob("*.md"):
        destination = coomi_home / "skills" / source.stem / "SKILL.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class BackendProcess:
    def __init__(self, *, workspace: Path, coomi_home: Path, log_path: Path, port: int) -> None:
        self.workspace = workspace
        self.coomi_home = coomi_home
        self.log_path = log_path
        self.port = port
        self.process: subprocess.Popen[Any] | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(BACKEND_ROOT) + os.pathsep + str(REPOSITORY_ROOT)
        env["STORYDEX_FORCE_WORKSPACE_ROOT"] = str(self.workspace)
        env["STORYDEX_COOMI_HOME"] = str(self.coomi_home)
        bridge = BACKEND_ROOT / "runtime" / "storydex-coomi-bridge.exe"
        if bridge.is_file():
            env["STORYDEX_COOMI_BRIDGE"] = str(bridge)
        log_stream = self.log_path.open("w", encoding="utf-8")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"],
            cwd=str(BACKEND_ROOT),
            env=env,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        deadline = time.time() + 90
        last_error = ""
        with httpx.Client(timeout=5.0) as client:
            while time.time() < deadline:
                if self.process.poll() is not None:
                    break
                try:
                    response = client.get(self.base_url + API_PREFIX + "/sys/health")
                    if response.status_code < 500:
                        return
                    last_error = f"health HTTP {response.status_code}"
                except httpx.HTTPError as exc:
                    last_error = str(exc)
                time.sleep(0.5)
        detail = last_error or "backend exited before health check"
        raise AcceptanceError(f"backend did not start: {detail}; log={self.log_path}")

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def unwrap(response: httpx.Response, *, operation: str) -> Any:
    try:
        payload = response.json()
    except ValueError as exc:
        raise AcceptanceError(f"{operation} returned non-JSON HTTP {response.status_code}") from exc
    if response.status_code >= 400 or not payload.get("ok"):
        raise AcceptanceError(
            f"{operation} failed HTTP {response.status_code}: "
            f"{payload.get('message') or payload.get('error') or payload}"
        )
    return payload.get("data")


def summarize_event(name: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"event": name}
    for key in (
        "traceId", "sessionId", "providerId", "llmProvider", "llmModel", "responseModel",
        "toolName", "tool_name", "toolCallId", "tool_call_id", "isError", "is_error",
        "status", "phase", "message", "error_type", "approvalId", "approval_id",
        "code", "noRestorePoint", "confirmNoSnapshotRequired",
    ):
        if key in payload and payload[key] not in (None, ""):
            value = payload[key]
            summary[key] = str(value)[:300] if isinstance(value, str) else value
    usage = payload.get("usage")
    if isinstance(usage, Mapping):
        summary["usage"] = {
            key: int(value or 0)
            for key, value in usage.items()
            if key in {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens", "reasoning_tokens"}
        }
    details = payload.get("details")
    if isinstance(details, Mapping):
        if "confirmNoSnapshotRequired" in details:
            summary["confirmNoSnapshotRequired"] = bool(details.get("confirmNoSnapshotRequired"))
        if "available" in details:
            summary["snapshotAvailable"] = bool(details.get("available"))
    return redact(summary)


def parse_sse_events(response: httpx.Response) -> Iterable[tuple[str, Dict[str, Any]]]:
    event_name = ""
    data_lines: List[str] = []
    for line in response.iter_lines():
        line = str(line)
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif not line and event_name:
            raw = "\n".join(data_lines)
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"message": raw[:500], "parseError": True}
            yield event_name, payload if isinstance(payload, dict) else {"value": payload}
            event_name = ""
            data_lines = []


def merge_usage(target: Dict[str, int], payload: Mapping[str, Any]) -> None:
    usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else payload
    if not isinstance(usage, Mapping):
        return
    aliases = {
        "prompt_tokens": ("prompt_tokens", "input_tokens", "promptTokens"),
        "completion_tokens": ("completion_tokens", "output_tokens", "completionTokens"),
        "total_tokens": ("total_tokens", "totalTokens"),
        "reasoning_tokens": ("reasoning_tokens", "reasoningTokens"),
    }
    for normalized, keys in aliases.items():
        for key in keys:
            if usage.get(key) is not None:
                try:
                    target[normalized] = max(target.get(normalized, 0), int(usage.get(key) or 0))
                except (TypeError, ValueError):
                    pass
                break
    if not target.get("total_tokens"):
        target["total_tokens"] = target.get("prompt_tokens", 0) + target.get("completion_tokens", 0)


def run_turn(
    client: httpx.Client,
    *,
    base_url: str,
    workspace: Path,
    session_id: str,
    prompt: str,
    reasoning_effort: str,
    label: str,
    expected_provider: str,
    expected_model: str,
    confirm_no_snapshot: bool = False,
    timeout_seconds: int = 900,
) -> Dict[str, Any]:
    trace_id = str(uuid.uuid4())
    started = time.time()
    headers = {"x-session-id": session_id, "x-trace-id": trace_id}
    payload = {
        "prompt": prompt,
        "activeFile": "",
        "workspaceRoot": str(workspace),
        "reasoningEffort": reasoning_effort,
        "confirmNoSnapshot": bool(confirm_no_snapshot),
    }
    reply_parts: List[str] = []
    events: List[Dict[str, Any]] = []
    tool_calls: List[Dict[str, Any]] = []
    tool_failures: List[Dict[str, Any]] = []
    tool_successes: List[Dict[str, Any]] = []
    usage: Dict[str, int] = {}
    provider_ids: set[str] = set()
    models: set[str] = set()
    errors: List[str] = []
    completed = False
    with client.stream(
        "POST",
        base_url + API_PREFIX + "/agent/chat/stream",
        headers=headers,
        json=payload,
        timeout=httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0),
    ) as response:
        if response.status_code >= 400:
            body = response.read().decode("utf-8", errors="replace")[:1000]
            raise AcceptanceError(f"{label} HTTP {response.status_code}: {body}")
        for name, packet in parse_sse_events(response):
            if time.time() - started > timeout_seconds:
                raise AcceptanceError(f"{label} exceeded {timeout_seconds}s timeout")
            events.append(summarize_event(name, packet))
            for key in ("providerId", "llmProvider"):
                value = str(packet.get(key) or "").strip()
                if value:
                    provider_ids.add(value)
            for key in ("model", "llmModel", "responseModel"):
                value = str(packet.get(key) or "").strip()
                if value:
                    models.add(value)
            merge_usage(usage, packet)
            if name in {"ModelCompleted", "UsageUpdate", "AgentCompleted"}:
                merge_usage(usage, packet)
            if name == "TextChunk":
                reply_parts.append(str(packet.get("content") or ""))
            elif name in {"ToolCall", "ToolStart", "ToolStarted", "ToolDone"}:
                tool_name = str(packet.get("tool_name") or packet.get("toolName") or "").strip()
                if tool_name:
                    call = {
                        "event": name,
                        "toolName": tool_name,
                        "toolCallId": str(packet.get("tool_call_id") or packet.get("toolCallId") or ""),
                        "isError": bool(packet.get("is_error") or packet.get("isError")),
                    }
                    arguments = packet.get("arguments")
                    if isinstance(arguments, Mapping):
                        call["arguments"] = redact(dict(arguments))
                    preview = str(packet.get("result_preview") or packet.get("resultPreview") or "").strip()
                    if preview:
                        call["resultPreview"] = redact(preview[:1600])
                    if call not in tool_calls:
                        tool_calls.append(call)
                    if name == "ToolDone":
                        outcome = {
                            "toolName": tool_name,
                            "toolCallId": call["toolCallId"],
                            "eventIndex": len(events),
                            "resultPreview": call.get("resultPreview", ""),
                        }
                        if call["isError"]:
                            tool_failures.append(outcome)
                        else:
                            tool_successes.append(outcome)
            elif name == "PermissionRequest":
                approval_id = str(packet.get("approvalId") or packet.get("approval_id") or "").strip()
                if approval_id:
                    approval_response = client.post(
                        base_url + API_PREFIX + "/agent/coomi/approval",
                        headers={"x-session-id": session_id, "x-trace-id": trace_id},
                        json={"approvalId": approval_id, "decision": "approve"},
                        timeout=30.0,
                    )
                    unwrap(approval_response, operation=f"{label} permission approval")
            elif name == "AgentError":
                error_code = str(packet.get("code") or packet.get("error_type") or "").strip()
                error_message = str(packet.get("message") or "AgentError")
                errors.append(f"{error_code}: {error_message}" if error_code else error_message)
            elif name in {"AgentCompleted", "RunCompleted"}:
                completed = True
    recovered_tool_errors: List[Dict[str, Any]] = []
    unrecovered_tool_errors: List[Dict[str, Any]] = []
    for failure in tool_failures:
        recovered = any(
            success["toolName"] == failure["toolName"]
            and int(success["eventIndex"]) > int(failure["eventIndex"])
            for success in tool_successes
        )
        (recovered_tool_errors if recovered else unrecovered_tool_errors).append(failure)
    errors.extend(
        f"{item['toolName']}: tool reported unrecovered error"
        for item in unrecovered_tool_errors
    )
    if not completed:
        errors.append("stream ended without AgentCompleted")
    result = {
        "label": label,
        "traceId": trace_id,
        "sessionId": session_id,
        "providerIds": sorted(provider_ids),
        "models": sorted(models),
        "usage": usage,
        "toolCalls": tool_calls,
        "recoveredToolErrors": recovered_tool_errors,
        "unrecoveredToolErrors": unrecovered_tool_errors,
        "eventCount": len(events),
        "replyPreview": "".join(reply_parts)[-1600:],
        "completed": completed and not errors,
        "errors": errors,
        "elapsedMs": int((time.time() - started) * 1000),
        "events": events[-160:],
    }
    if errors:
        raise TurnFailure(f"{label} failed: {'; '.join(errors[:4])}", result)
    if provider_ids and provider_ids != {expected_provider}:
        raise AcceptanceError(f"{label} used unexpected provider(s): {sorted(provider_ids)}")
    if models and {value.casefold() for value in models} != {expected_model.casefold()}:
        raise AcceptanceError(f"{label} used unexpected model(s): {sorted(models)}")
    return result


def started_tool_names(turn: Mapping[str, Any]) -> List[str]:
    """Return one ordered name per actual tool call, excluding lifecycle duplicates."""

    names: List[str] = []
    seen_ids: set[str] = set()
    for item in turn.get("toolCalls", []) if isinstance(turn.get("toolCalls"), list) else []:
        if not isinstance(item, Mapping) or str(item.get("event") or "") not in {"ToolCall", "ToolStart", "ToolStarted"}:
            continue
        call_id = str(item.get("toolCallId") or "").strip()
        name = str(item.get("toolName") or "").strip()
        identity = call_id or f"{name}:{len(names)}"
        if not name or identity in seen_ids:
            continue
        seen_ids.add(identity)
        names.append(name)
    return names


def run_turn_with_snapshot_confirmation(
    client: httpx.Client,
    *,
    base_url: str,
    workspace: Path,
    session_id: str,
    prompt: str,
    reasoning_effort: str,
    label: str,
    expected_provider: str,
    expected_model: str,
    timeout_seconds: int = 900,
) -> Dict[str, Any]:
    """Run a turn and handle the explicit no-restore-point confirmation.

    The first request intentionally follows the normal UI path.  If the
    backend reports ``SNAPSHOT_FAILED`` with
    ``confirmNoSnapshotRequired=true``, the harness records that paused trace
    and sends a second request carrying the explicit confirmation flag.  Any
    other failure is surfaced immediately; this is not a generic retry.
    """

    try:
        first = run_turn(
            client,
            base_url=base_url,
            workspace=workspace,
            session_id=session_id,
            prompt=prompt,
            reasoning_effort=reasoning_effort,
            label=label,
            expected_provider=expected_provider,
            expected_model=expected_model,
            confirm_no_snapshot=False,
            timeout_seconds=timeout_seconds,
        )
        first["snapshotConfirmation"] = {"required": False, "confirmed": False}
        first["attempts"] = [first.copy()]
        return first
    except TurnFailure as first_error:
        paused = first_error.result
        snapshot_events = [
            event
            for event in (paused.get("events") or [])
            if isinstance(event, Mapping)
            and event.get("event") == "AgentError"
            and str(event.get("code") or event.get("error_type") or "").strip() == "SNAPSHOT_FAILED"
        ]
        required = any(bool(event.get("confirmNoSnapshotRequired")) for event in snapshot_events)
        if not required:
            raise first_error
        try:
            confirmed = run_turn(
                client,
                base_url=base_url,
                workspace=workspace,
                session_id=session_id,
                prompt=prompt,
                reasoning_effort=reasoning_effort,
                label=label,
                expected_provider=expected_provider,
                expected_model=expected_model,
                confirm_no_snapshot=True,
                timeout_seconds=timeout_seconds,
            )
        except TurnFailure as confirmed_error:
            combined = dict(confirmed_error.result)
            combined["snapshotConfirmation"] = {
                "required": True,
                "confirmed": True,
                "initialTraceId": paused.get("traceId", ""),
                "confirmationTraceId": confirmed_error.result.get("traceId", ""),
                "initialErrors": list(paused.get("errors") or []),
            }
            combined["attempts"] = [paused, confirmed_error.result]
            raise TurnFailure(str(confirmed_error), combined) from confirmed_error
        confirmed["snapshotConfirmation"] = {
            "required": True,
            "confirmed": True,
            "initialTraceId": paused.get("traceId", ""),
            "confirmationTraceId": confirmed.get("traceId", ""),
            "initialErrors": list(paused.get("errors") or []),
        }
        confirmed["attempts"] = [paused, confirmed.copy()]
        return confirmed


def get_data(client: httpx.Client, base_url: str, path: str, *, params: Optional[Dict[str, Any]] = None) -> Any:
    response = client.get(base_url + API_PREFIX + path, params=params, timeout=60.0)
    return unwrap(response, operation=f"GET {path}")


def post_data(client: httpx.Client, base_url: str, path: str, payload: Any) -> Any:
    response = client.post(base_url + API_PREFIX + path, json=payload, timeout=120.0)
    return unwrap(response, operation=f"POST {path}")


def read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def relation_files(root: Path) -> List[str]:
    output: List[str] = []
    for path in (root / ".storydex/worldbook").glob("*.md"):
        if "## 关联对象" in path.read_text(encoding="utf-8"):
            output.append(path.relative_to(root).as_posix())
    return sorted(output)


def graph_snapshot(client: httpx.Client, base_url: str, *, include_review: bool = True) -> Dict[str, Any]:
    pages = []
    offset = 0
    while True:
        page = get_data(
            client,
            base_url,
            "/story/wiki/graph",
            params={"category": "setting", "limit": 60, "offset": offset, "includeReview": str(include_review).lower()},
        )
        pages.append(page)
        if not page.get("hasMore"):
            break
        next_offset = page.get("nextOffset")
        if next_offset is None or int(next_offset) <= offset:
            raise AcceptanceError("graph pagination did not advance")
        offset = int(next_offset)
    nodes = {str(node.get("id")): node for page in pages for node in (page.get("graph") or {}).get("nodes", []) if node.get("id")}
    edges = {
        str(edge.get("id") or edge.get("fingerprint") or f"{edge.get('source')}|{edge.get('target')}|{edge.get('label')}"): edge
        for page in pages
        for edge in (page.get("graph") or {}).get("edges", [])
        if edge.get("source") and edge.get("target")
    }
    total = pages[0].get("total") if pages else {}
    return {
        "pages": len(pages),
        "pageSizes": [len((page.get("graph") or {}).get("nodes", [])) for page in pages],
        "nodeIds": sorted(nodes),
        "edgeIds": sorted(edges),
        "nodeCount": len(nodes),
        "edgeCount": len(edges),
        "total": dict(total or {}),
    }


def find_pending_plans(
    workspace: Path,
    before: set[str],
    *,
    session_id: str = "",
    trace_ids: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    directory = workspace / ".storydex/.agent/runtime/knowledge-write-plans"
    paths = sorted(path for path in directory.glob("krp_*.json") if path.name not in before)
    if not paths:
        raise AcceptanceError("no new explicit plan was created")
    plans: List[Dict[str, Any]] = []
    allowed_traces = {str(item).strip() for item in (trace_ids or set()) if str(item).strip()}
    for path in paths:
        payload = read_json(path, {})
        if not isinstance(payload, dict) or not payload.get("planId"):
            raise AcceptanceError(f"explicit plan file is invalid: {path.name}")
        plan_id = str(payload.get("planId") or "").strip()
        if plan_id != path.stem:
            raise AcceptanceError(f"explicit plan filename does not match planId: {path.name}")
        if session_id and str(payload.get("sessionId") or "").strip() != session_id:
            raise AcceptanceError(f"explicit plan belongs to an unexpected session: {plan_id}")
        if allowed_traces and str(payload.get("traceId") or "").strip() not in allowed_traces:
            raise AcceptanceError(f"explicit plan belongs to an unexpected trace: {plan_id}")
        relations = payload.get("relations")
        if not isinstance(relations, list) or not relations:
            raise AcceptanceError(f"explicit plan has no relations: {plan_id}")
        plans.append(payload)
    return plans


def facts_payload(workspace: Path) -> Dict[str, Any]:
    payload = read_json(workspace / ".storydex/memory/current/facts.json", {})
    return payload if isinstance(payload, dict) else {}


def run_acceptance(args: argparse.Namespace) -> Dict[str, Any]:
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    output_root = (Path(args.output_dir).resolve() if args.output_dir else REPOSITORY_ROOT / "output" / "live-acceptance") / run_id
    workspace = output_root / "workspace"
    output_root.mkdir(parents=True, exist_ok=True)
    coomi_runtime = tempfile.TemporaryDirectory(prefix=f"storydex-live-coomi-{run_id}-")
    coomi_home = Path(coomi_runtime.name)
    scenario: Dict[str, Any] = {
        "startedAt": now_iso(),
        "runId": run_id,
        "workspace": workspace.as_posix(),
        "provider": {},
        "turns": [],
    }
    log_path = output_root / "backend.log"
    backend: BackendProcess | None = None
    client: httpx.Client | None = None
    try:
        provider_summary = load_isolated_provider(
            Path(args.config).resolve() if args.config else provider_config_path(),
            coomi_home,
            args.provider_id,
            args.model,
        )
        scenario["provider"] = provider_summary
        names = prepare_workspace(workspace)
        install_project_skills_for_acceptance(workspace, coomi_home)
        port = free_port()
        backend = BackendProcess(workspace=workspace, coomi_home=coomi_home, log_path=log_path, port=port)
        backend.start()
        client = httpx.Client()
        base_url = backend.base_url
        session_id = "live-knowledge-graph-" + uuid.uuid4().hex[:12]

        def execute_turn(*, prompt: str, label: str) -> Dict[str, Any]:
            try:
                result = run_turn_with_snapshot_confirmation(
                    client,
                    base_url=base_url,
                    workspace=workspace,
                    session_id=session_id,
                    prompt=prompt,
                    reasoning_effort=args.reasoning_effort,
                    label=label,
                    expected_provider=args.provider_id,
                    expected_model=args.model,
                )
            except TurnFailure as exc:
                scenario["turns"].append(exc.result)
                raise
            scenario["turns"].append(result)
            return result

        mapping = "\n".join(f"- {creature} → {names['planets'][index % 10]}" for index, creature in enumerate(names["creatures"]))
        first_prompt = (
            "请把下面 52 个生物分别绑定到对应星球，关系谓词统一使用‘栖息于’，并长期保存到 Storydex 项目。"
            "这是明确知识绑定：请先调用 StorydexApplyKnowledgeUpdate 的 prepare_explicit，"
            "列出所有正式目标文件、planId 和 fingerprint，等待我下一轮确认；本轮禁止 apply_explicit。\n\n"
            "完整映射如下：\n" + mapping
        )
        before_plans = {path.name for path in (workspace / ".storydex/.agent/runtime/knowledge-write-plans").glob("krp_*.json")}
        first_turn = execute_turn(prompt=first_prompt, label="explicit-prepare")
        prepare_trace_ids = {
            str(first_turn.get("traceId") or "").strip(),
            *{
                str(attempt.get("traceId") or "").strip()
                for attempt in (first_turn.get("attempts") or [])
                if isinstance(attempt, Mapping)
            },
        }
        prepare_plans = find_pending_plans(
            workspace,
            before_plans,
            session_id=session_id,
            trace_ids=prepare_trace_ids,
        )
        prepared_relations = [
            relation
            for plan in prepare_plans
            for relation in (plan.get("relations") or [])
            if isinstance(relation, Mapping)
        ]
        relation_keys = [str(relation.get("relationKey") or "").strip() for relation in prepared_relations]
        if any(not key for key in relation_keys):
            raise AcceptanceError("prepare plans contain a relation without relationKey")
        if len(relation_keys) != len(set(relation_keys)):
            raise AcceptanceError("prepare plans contain duplicate relationKey values")
        if len(relation_keys) != 52:
            raise AcceptanceError(
                f"prepare plans contain {len(relation_keys)} unique relations, expected 52"
            )
        if facts_payload(workspace).get("facts"):
            raise AcceptanceError("prepare turn wrote facts before confirmation")
        if relation_files(workspace):
            raise AcceptanceError("prepare turn wrote formal Markdown relations before confirmation")

        plan_lines = "\n".join(
            f"- planId `{plan['planId']}`，fingerprint `{plan['fingerprint']}`，关系数 {len(plan.get('relations') or [])}"
            for plan in prepare_plans
        )
        second_prompt = (
            "用户已确认下面列出的全部显式知识写入计划。请在本轮逐个调用 "
            "StorydexApplyKnowledgeUpdate 的 apply_explicit：每个 planId 只调用一次，并严格使用对应 fingerprint。\n"
            "不要调用 prepare_explicit，不要遗漏任何计划，也不要改写计划内容。\n\n"
            "待应用计划：\n"
            + plan_lines
        )
        second_turn = execute_turn(prompt=second_prompt, label="explicit-apply")
        for plan in prepare_plans:
            persisted = read_json(
                workspace / ".storydex/.agent/runtime/knowledge-write-plans" / f"{plan['planId']}.json",
                {},
            )
            if str(persisted.get("status") or "") != "applied":
                raise AcceptanceError(f"explicit plan was not applied: {plan['planId']}")
        facts = facts_payload(workspace)
        relation_facts = [item for item in facts.get("facts", []) if isinstance(item, dict)]
        formal_paths = relation_files(workspace)
        if len(relation_facts) != 52 or len(formal_paths) != 52:
            raise AcceptanceError(f"explicit apply produced facts={len(relation_facts)}, formalFiles={len(formal_paths)}, expected 52/52")
        if {str(item.get("predicate") or "") for item in relation_facts} != {"栖息于"}:
            raise AcceptanceError("explicit bindings did not preserve the unified predicate 栖息于")
        if any(str(item.get("reviewStatus") or "") != "confirmed" for item in relation_facts):
            raise AcceptanceError("explicit bindings did not all become confirmed facts")
        stage_a_graph = graph_snapshot(client, base_url, include_review=True)
        if stage_a_graph["total"].get("nodeCount") != 62 or stage_a_graph["total"].get("confirmedEdgeCount") != 52 or stage_a_graph["total"].get("isolatedNodeCount") != 0:
            raise AcceptanceError(f"stage A graph invariant failed: {stage_a_graph['total']}")
        scenario["stageA"] = {
            "graph": stage_a_graph,
            "factCount": len(relation_facts),
            "formalRelationFileCount": len(formal_paths),
            "plans": [
                {
                    "planId": plan["planId"],
                    "fingerprint": plan.get("fingerprint"),
                    "relationCount": len(plan.get("relations") or []),
                }
                for plan in prepare_plans
            ],
        }

        prepare_extraction_sources(workspace)
        from services.story_knowledge_relation_service import StoryKnowledgeRelationService

        relation_service = StoryKnowledgeRelationService()
        for name in ("潮汐兽", "灰翼兽", "赤甲虫", "雾鲸"):
            relation_service.ensure_entity(workspace, name, source_path=f".storydex/worldbook/{name}.md", kind="setting")
        post_data(client, base_url, "/story/wiki/sync", {})
        extraction_prompt = (
            "请深度分析当前 Storydex 小说项目的全部章节、角色、设定、事件和关系，重新生成完整知识图谱与 WIKI。"
            "请先检查现有数据，保留有证据的内容，标注冲突与待确认项，并通过 Storydex 项目接口写入结果。"
            "正文抽取必须调用 StorydexApplyKnowledgeUpdate 的 submit_candidates；候选只能进入待确认账本，"
            "不得直接写入 facts 或正式 Markdown。请特别核对否定、假设和传闻句。"
            "项目内 `.storydex/.agent/skills/` 下的技能是普通工作区文件，请用 read_file 读取；不要对项目技能名调用 read_skill。"
            "关系图权威路径是 `.storydex/memory/current/relationship_graph.json`，不要读取旧的 `.storydex/memory/relationship_graph.json`。"
            "本轮只能使用 read_file、list_dir、grep_files、StorydexProjectSearch、StorydexWikiQuery、"
            "StorydexApplyKnowledgeUpdate、StorydexSyncWiki 和计划工具。"
            "绝对不要生成 shell、local_shell、apply_patch、write_file、edit_file 或 Git/version 工具调用，"
            "即使只是计数、列目录或验证结果也不允许；验收程序会在回合结束后独立校验数量与落盘状态。"
        )
        extraction_turn = execute_turn(prompt=extraction_prompt, label="prose-candidate-extraction")
        review = get_data(client, base_url, "/story/wiki/relations/review", params={"status": "review_required", "limit": 500, "offset": 0})
        candidates = review.get("relations") if isinstance(review, dict) else []
        candidates = [item for item in candidates if isinstance(item, dict)]
        facts_after_extraction = facts_payload(workspace).get("facts") or []
        if len(facts_after_extraction) != 52:
            raise AcceptanceError("prose extraction wrote facts without review")
        tide = next((item for item in candidates if "潮汐兽" in str(item.get("subject") or "") and "夜港星" in str(item.get("object") or "")), None)
        if tide is None:
            raise AcceptanceError("explicit prose relation 潮汐兽→夜港星 was not submitted as a review candidate")
        forbidden_subjects = {"灰翼兽", "赤甲虫"}
        if any(str(item.get("subject") or "") in forbidden_subjects for item in candidates):
            raise AcceptanceError("negated or hypothetical prose was incorrectly submitted as a relation candidate")
        rumor = next((item for item in candidates if "雾鲸" in str(item.get("subject") or "")), None)
        if rumor is not None and str(rumor.get("knowledgeStatus") or "") != "inferred":
            raise AcceptanceError("rumor candidate was not downgraded to inferred")
        confirm_response = post_data(
            client,
            base_url,
            f"/story/wiki/relations/{tide['id']}/confirm",
            {
                "expectedFingerprint": tide.get("fingerprint"),
                "subjectId": tide.get("subjectId"),
                "predicate": tide.get("predicate"),
                "objectId": tide.get("objectId"),
                "targetSourcePath": ".storydex/worldbook/潮汐兽.md",
            },
        )
        remaining = [item for item in candidates if item.get("id") != tide.get("id")]
        rejected = None
        if remaining:
            rejected = remaining[0]
            post_data(
                client,
                base_url,
                f"/story/wiki/relations/{rejected['id']}/reject",
                {"expectedFingerprint": rejected.get("fingerprint"), "reason": "not_canon", "note": "live acceptance rejection"},
            )
        else:
            raise AcceptanceError("deep extraction produced no second candidate to exercise rejection")
        facts_after_confirm = facts_payload(workspace).get("facts") or []
        if len(facts_after_confirm) != 53:
            raise AcceptanceError(f"candidate confirmation produced {len(facts_after_confirm)} facts, expected 53")
        scenario["stageB"] = {
            "candidateCount": len(candidates),
            "confirmedCandidateId": tide.get("id"),
            "rejectedCandidateId": rejected.get("id") if rejected else "",
            "confirmResponse": redact(confirm_response),
            "reviewQueueAfter": get_data(client, base_url, "/story/wiki/relations/review", params={"status": "review_required", "limit": 500, "offset": 0}),
        }

        update_prompt = (
            "请扫描当前 Storydex 小说项目自上次 WIKI 更新后的所有改动，增量更新知识图谱与 WIKI。"
            "第一步必须调用 StorydexSyncWiki，且在看到返回结果前不要 list_dir、read_file、grep_files、"
            "StorydexProjectSearch 或 StorydexWikiQuery。若返回 status=ready 且 noChanges=true，立即结束本轮，"
            "不要继续扫描、审计或重复生成；本轮没有要求深度审计。若 changedSourcePaths 非空，只优先读取这些文件，"
            "仅在实体端点或连续性证据确有需要时扩展范围。不要覆盖未受影响的人工内容；为新增或变化的角色、事件、"
            "关系补充来源证据，并标注冲突与待确认项。"
        )
        update_one = execute_turn(prompt=update_prompt, label="incremental-update-1")
        update_two = execute_turn(prompt=update_prompt, label="incremental-update-2")
        incremental_metrics = []
        for index, turn in enumerate((update_one, update_two)):
            tool_names = started_tool_names(turn)
            if tool_names.count("StorydexSyncWiki") != 1:
                raise AcceptanceError(
                    f"{turn.get('label')} must call StorydexSyncWiki exactly once before ending: {tool_names}"
                )
            allowed = {
                "update_plan",
                "StorydexSyncWiki",
                # The first incremental turn may need to inspect a changed
                # ledger/fact source and perform a small number of endpoint /
                # projection checks. The repeated no-op turn must not read.
                "read_file",
                "grep_files",
                "StorydexWikiQuery",
            } if index == 0 else {"update_plan", "StorydexSyncWiki"}
            unexpected = [
                name for name in tool_names
                if name not in allowed
            ]
            if unexpected:
                raise AcceptanceError(
                    f"{turn.get('label')} used disallowed broad-scan tools after StorydexSyncWiki: {unexpected}"
                )
            if index == 0 and (
                tool_names.count("read_file") > 4
                or tool_names.count("grep_files") > 8
                or tool_names.count("StorydexWikiQuery") > 3
            ):
                raise AcceptanceError(
                    f"{turn.get('label')} exceeded bounded changed-source verification: {tool_names}"
                )
            incremental_metrics.append({
                "label": turn.get("label"),
                "toolNames": tool_names,
                "toolCallCount": len(tool_names),
                "readFileCount": tool_names.count("read_file"),
                "targetedVerificationCount": sum(
                    tool_names.count(name)
                    for name in ("read_file", "grep_files", "StorydexWikiQuery")
                ),
                "broadScanCount": sum(
                    tool_names.count(name)
                    for name in ("list_dir", "search", "StorydexProjectSearch")
                ),
                "usage": dict(turn.get("usage") or {}),
                "elapsedMs": int(turn.get("elapsedMs") or 0),
            })
        post_data(client, base_url, "/story/wiki/sync", {})
        local_graph = post_data(client, base_url, "/story/wiki/rebuild", {})
        final_before_restart = graph_snapshot(client, base_url, include_review=True)
        fact_ids_before_restart = sorted(str(item.get("id") or "") for item in facts_payload(workspace).get("facts", []) if isinstance(item, dict))
        backend.stop()
        backend = BackendProcess(workspace=workspace, coomi_home=coomi_home, log_path=log_path, port=port)
        backend.start()
        reloaded_graph = graph_snapshot(client, backend.base_url, include_review=True)
        fact_ids_after_restart = sorted(str(item.get("id") or "") for item in facts_payload(workspace).get("facts", []) if isinstance(item, dict))
        review_after_restart = get_data(client, backend.base_url, "/story/wiki/relations/review", params={"status": "review_required", "limit": 500, "offset": 0})
        if fact_ids_before_restart != fact_ids_after_restart:
            raise AcceptanceError("fact IDs changed after backend restart")
        scenario["stageC"] = {
            "localRebuild": redact(local_graph),
            "finalBeforeRestart": final_before_restart,
            "afterBackendRestart": reloaded_graph,
            "factIdsStable": True,
            "reviewQueueAfterRestart": review_after_restart,
            "incrementalNoChangeMetrics": incremental_metrics,
        }
        scenario["status"] = "passed"
        scenario["finishedAt"] = now_iso()
        write_json(output_root / "acceptance-report.json", redact(scenario))
        return scenario
    except Exception as exc:
        scenario["status"] = "failed"
        scenario["error"] = str(exc)
        scenario["finishedAt"] = now_iso()
        write_json(output_root / "acceptance-report.json", redact(scenario))
        raise AcceptanceError(f"{exc} (report: {output_root / 'acceptance-report.json'})") from exc
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            try:
                if backend is not None:
                    backend.stop()
            finally:
                coomi_runtime.cleanup()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider-id",
        default=DEFAULT_PROVIDER or None,
        required=not DEFAULT_PROVIDER,
        help="Provider ID; defaults to STORYDEX_LIVE_PROVIDER",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL or None,
        required=not DEFAULT_MODEL,
        help="Model ID; defaults to STORYDEX_LIVE_MODEL",
    )
    parser.add_argument("--reasoning-effort", default="high", choices=("auto", "low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--config", default="", help="Optional source providers.json; only the requested provider is copied")
    parser.add_argument("--output-dir", default="", help="Defaults to output/live-acceptance")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report_root: Path | None = None
    try:
        # Derive the run path before executing so a failure still leaves a
        # machine-readable report.  The actual run function creates its own
        # unique directory and returns it in the report.
        report = run_acceptance(args)
        report_root = Path(str(report.get("workspace") or "")).parent
        write_json(report_root / "acceptance-report.json", redact(report))
        print(json.dumps({"status": report["status"], "report": (report_root / "acceptance-report.json").as_posix(), "provider": args.provider_id, "model": args.model}, ensure_ascii=False))
        return 0
    except Exception as exc:
        # run_acceptance normally has already created a unique output folder;
        # if setup failed before it could return, use a separate failure file.
        fallback = (Path(args.output_dir).resolve() if args.output_dir else REPOSITORY_ROOT / "output" / "live-acceptance") / ("failed-" + uuid.uuid4().hex[:8])
        fallback.mkdir(parents=True, exist_ok=True)
        write_json(fallback / "acceptance-report.json", {"status": "failed", "provider": args.provider_id, "model": args.model, "error": str(exc), "finishedAt": now_iso()})
        print(json.dumps({"status": "failed", "report": (fallback / "acceptance-report.json").as_posix(), "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
