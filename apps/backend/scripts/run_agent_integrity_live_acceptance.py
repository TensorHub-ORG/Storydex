"""Exercise session integrity and versioned file reads with a real provider."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPOSITORY_ROOT / "apps" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.run_graph_live_acceptance import (  # noqa: E402
    AcceptanceError,
    load_isolated_provider,
    prepare_workspace,
    provider_config_path,
    redact,
    write_json,
    write_text,
)


HEAD_MARKER = "OPENCODE_HEAD_7C91"
LINES_END_MARKER = "OPENCODE_LINES_END_4A2F"
LONG_END_MARKER = "OPENCODE_LONG_END_9D33"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def prepare_integrity_sources(workspace: Path) -> None:
    lines = [f"line-{index:04d} ordinary acceptance text" for index in range(1, 2_002)]
    lines[0] = f"line-0001 {HEAD_MARKER}"
    lines[-1] = f"line-2001 {LINES_END_MARKER}"
    write_text(workspace / "chapters" / "integrity-many-lines.txt", "\n".join(lines))
    write_text(
        workspace / "chapters" / "integrity-long-line.txt",
        ("界" * 40_000) + LONG_END_MARKER,
    )


def exercise_content_catalog(workspace: Path) -> dict[str, Any]:
    from services.content_catalog_service import get_content_catalog_service

    probe = workspace / "chapters" / "catalog-revision-probe.txt"
    write_text(probe, "catalog probe alpha\n")
    service = get_content_catalog_service(workspace)

    initial_started = time.perf_counter()
    initial = service.snapshot()
    initial_ms = (time.perf_counter() - initial_started) * 1000
    initial_entry = initial.get("chapters/catalog-revision-probe.txt")
    if initial_entry is None:
        raise AcceptanceError("content catalog omitted the revision probe")

    warm_started = time.perf_counter()
    warm = service.snapshot()
    warm_ms = (time.perf_counter() - warm_started) * 1000
    if warm is not initial:
        raise AcceptanceError("warm content catalog lookup did not reuse the published snapshot")

    original_stat = probe.stat()
    write_text(probe, "catalog probe bravo\n")
    os.utime(probe, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    service.notify_external_changes([probe])
    if service.dirty_file_count != 1:
        raise AcceptanceError("content catalog did not enqueue the external source change")

    refresh_started = time.perf_counter()
    refreshed = service.refresh_dirty()
    refresh_ms = (time.perf_counter() - refresh_started) * 1000
    refreshed_entry = refreshed.get("chapters/catalog-revision-probe.txt")
    if refreshed_entry is None:
        raise AcceptanceError("content catalog lost the refreshed revision probe")
    if refreshed.generation != initial.generation + 1:
        raise AcceptanceError("content catalog generation did not advance exactly once")
    if refreshed_entry.revision == initial_entry.revision:
        raise AcceptanceError("content catalog missed a same-size same-mtime content replacement")
    if refreshed_entry.mtime_ns != original_stat.st_mtime_ns:
        raise AcceptanceError("content catalog probe did not preserve its test mtime")
    if service.dirty_file_count:
        raise AcceptanceError("content catalog retained acknowledged dirty paths")

    return {
        "initialSnapshotMs": round(initial_ms, 3),
        "warmSnapshotMs": round(warm_ms, 3),
        "dirtyRefreshMs": round(refresh_ms, 3),
        "initialGeneration": initial.generation,
        "refreshedGeneration": refreshed.generation,
        "initialRevision": initial.catalog_revision,
        "refreshedRevision": refreshed.catalog_revision,
        "sourceRevisionChanged": True,
        "sameSizeSameMtimeDetected": True,
        "dirtyFileCount": service.dirty_file_count,
    }


async def collect_turn(
    service: Any,
    *,
    workspace: Path,
    session_id: str,
    prompt: str,
    reasoning_effort: str = "low",
    contract_observations: dict[str, Any] | None = None,
    observation_label: str = "",
) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    from services.context_policy import ContextPolicy
    from services.storydex_orchestration_service import get_storydex_orchestration_service

    trace_id = f"live-{uuid.uuid4().hex[:12]}"
    turn_contract = get_storydex_orchestration_service().build_turn_contract(
        workspace,
        prompt=prompt,
        intent_frame={
            "primary": "general",
            "effect": "respond_only",
            "operationType": "inquiry",
            "canWrite": False,
            "assetTargets": [],
            "matchedSkills": [],
        },
        context_policy=ContextPolicy(
            story_structured_memory=False,
            passive_fts=False,
            wiki_context=False,
            coomi_memory=False,
            active_retrieval_tools=False,
        ),
        provider="OPENCODE",
        model="deepseek-v4-flash",
        trace_id=trace_id,
        session_id=session_id,
    )
    turn_contract["reasoningEffort"] = reasoning_effort
    if contract_observations is not None and observation_label:
        contract_observations[observation_label] = {
            "contentCatalog": turn_contract.get("contentCatalog") or {},
            "performanceTrace": turn_contract.get("performanceTrace") or {},
        }
    async for name, payload in service.stream_events(
        prompt=prompt,
        trace_id=trace_id,
        session_id=session_id,
        workspace_root=workspace,
        turn_contract=turn_contract,
    ):
        events.append((name, payload))
    return events


def require_completed(events: list[tuple[str, dict[str, Any]]], label: str) -> None:
    errors = [payload for name, payload in events if name == "AgentError"]
    if errors:
        raise AcceptanceError(f"{label} failed: {errors[-1].get('message')}")
    if not any(name == "AgentCompleted" for name, _payload in events):
        raise AcceptanceError(f"{label} did not emit AgentCompleted")


def response_text(events: list[tuple[str, dict[str, Any]]]) -> str:
    return "".join(
        str(payload.get("content") or "")
        for name, payload in events
        if name == "TextChunk"
    )


def turn_performance(events: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    model_events = [payload for name, payload in events if name == "ModelCompleted"]
    inputs = [
        int((payload.get("usage") or {}).get("input_tokens") or 0)
        for payload in model_events
    ]
    cached = [
        int((payload.get("usage") or {}).get("cached_input_tokens") or 0)
        for payload in model_events
    ]
    runtime: dict[str, Any] = {}
    for payload in model_events:
        metrics = payload.get("runtimeMetrics")
        if isinstance(metrics, dict):
            runtime.update(metrics)
    return {
        **runtime,
        "modelRounds": len(model_events),
        "toolCalls": sum(1 for name, _payload in events if name == "ToolDone"),
        "logicalInputTokens": max(inputs, default=0),
        "transmittedInputTokens": sum(inputs),
        "cachedInputTokens": sum(cached),
    }


def read_file_transcript(session: dict[str, Any]) -> list[dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    transcript: list[dict[str, Any]] = []
    messages = session.get("messages") if isinstance(session.get("messages"), list) else []
    for message in messages:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("name") == "read_file":
                calls[str(call.get("id") or "")] = dict(call.get("arguments") or {})
        call_id = str(message.get("tool_call_id") or "")
        if not call_id or call_id not in calls:
            continue
        persisted = str(message.get("content") or "")
        status, separator, raw_output = persisted.partition(": ")
        if not separator or status not in {"success", "error"}:
            raise AcceptanceError(
                f"read_file tool message has an unknown persisted status: {persisted[:160]}"
            )
        try:
            output = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise AcceptanceError(
                f"read_file returned non-JSON output after {status}: {raw_output[:160]} ({exc})"
            ) from exc
        transcript.append({"arguments": calls[call_id], "output": output, "success": status == "success"})
    return transcript


def assert_complete_file_reads(transcript: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for expected_path in (
        "chapters/integrity-many-lines.txt",
        "chapters/integrity-long-line.txt",
    ):
        read_errors = [
            item for item in transcript
            if not item["success"] and item["arguments"].get("path") == expected_path
        ]
        if read_errors:
            raise AcceptanceError(
                f"Agent produced read_file errors for {expected_path}: "
                f"{[item['output'].get('error') for item in read_errors]}"
            )
        pages = [
            item for item in transcript
            if item["success"] and item["output"].get("path") == expected_path
        ]
        if not pages:
            raise AcceptanceError(f"Agent never read {expected_path}")
        expected_start_byte = 0
        expected_start_char = 0
        revision = str(pages[0]["output"].get("revision") or "")
        for index, page in enumerate(pages):
            output = page["output"]
            span = output.get("span") if isinstance(output.get("span"), dict) else {}
            start_byte = int(span.get("startByte") or 0)
            end_byte = int(span.get("endByte") or 0)
            start_char = int(span.get("startChar") or 0)
            end_char = int(span.get("endChar") or 0)
            if start_byte != expected_start_byte or end_byte <= start_byte:
                raise AcceptanceError(
                    f"{expected_path} has a gap or non-progressing byte span: "
                    f"{start_byte}..{end_byte}, expected {expected_start_byte}"
                )
            if start_char != expected_start_char or end_char <= start_char:
                raise AcceptanceError(
                    f"{expected_path} has a gap or non-progressing char span: "
                    f"{start_char}..{end_char}, expected {expected_start_char}"
                )
            if str(output.get("revision") or "") != revision:
                raise AcceptanceError(f"{expected_path} changed revision within one read sequence")
            if str(span.get("revision") or "") != revision or span.get("endExclusive") is not True:
                raise AcceptanceError(f"{expected_path} span does not carry the source revision contract")
            if index > 0:
                arguments = page["arguments"]
                if arguments.get("expected_revision") != revision:
                    raise AcceptanceError(f"{expected_path} continuation omitted expected_revision")
                if int(arguments.get("byte_offset") or -1) != start_byte:
                    raise AcceptanceError(f"{expected_path} continuation used the wrong byte_offset")
            expected_start_byte = end_byte
            expected_start_char = end_char
        final = pages[-1]["output"]
        if (
            final.get("hasMore") is not False
            or expected_start_byte != int(final.get("totalBytes") or -1)
            or expected_start_char != int(final.get("totalChars") or -1)
        ):
            raise AcceptanceError(f"Agent stopped before reaching the end of {expected_path}")
        summary[expected_path] = {
            "pages": len(pages),
            "revision": revision,
            "totalBytes": int(final.get("totalBytes") or 0),
            "totalChars": int(final.get("totalChars") or 0),
            "totalLines": int(final.get("totalLines") or 0),
        }
    return summary


async def run_live(args: argparse.Namespace, *, workspace: Path, coomi_home: Path) -> dict[str, Any]:
    os.environ["STORYDEX_COOMI_HOME"] = str(coomi_home)
    bridge = REPOSITORY_ROOT / "apps" / "desktop" / "coomi-rs-desktop" / "target" / "debug" / "storydex-coomi-bridge.exe"
    if not bridge.is_file():
        raise AcceptanceError(f"debug bridge is missing: {bridge}")
    os.environ["STORYDEX_COOMI_BRIDGE"] = str(bridge)

    from services import coomi_agent_service as coomi
    from services.storydex_intent_service import StorydexIntentService

    catalog = exercise_content_catalog(workspace)

    intent_prompt = "请修改第一章" + ("背景资料" * 750) + "。最终约束：不要修改任何项目文件"
    intent_started = time.perf_counter()
    intent = await StorydexIntentService().classify_intent(
        prompt=intent_prompt,
        active_file="chapters/integrity-many-lines.txt",
        workspace_root=workspace,
        session_id="opencode-intent-integrity",
    )
    if intent.get("method") != "llm":
        raise AcceptanceError(f"OpenCode intent call did not complete through the model: {intent.get('method')}")
    if intent.get("canWrite") is not False or "full_prompt_no_project_write" not in (intent.get("signals") or []):
        raise AcceptanceError("full prompt no-write constraint was not enforced")

    service = coomi.StorydexCoomiAgentService()
    contract_observations: dict[str, Any] = {}
    session_id = f"opencode-integrity-{uuid.uuid4().hex[:10]}"
    service.set_plan_mode(session_id=session_id, workspace_root=workspace, active=True)
    first_prompt = (
        "This is a read protocol acceptance test. Use only read_file. Read both "
        "chapters/integrity-many-lines.txt and chapters/integrity-long-line.txt from byte 0. "
        "Whenever hasMore is true, continue with byte_offset=nextByteOffset and "
        "expected_revision=revision until hasMore is false. Do not skip pages and do not use search. "
        f"Return exactly these three markers after finding them: {HEAD_MARKER}, "
        f"{LINES_END_MARKER}, and {LONG_END_MARKER}. Do not write any file."
    )
    first_started = time.perf_counter()
    first_events = await asyncio.wait_for(
        collect_turn(
            service,
            workspace=workspace,
            session_id=session_id,
            prompt=first_prompt,
            contract_observations=contract_observations,
            observation_label="firstTurn",
        ),
        timeout=args.turn_timeout,
    )
    require_completed(first_events, "first live turn")
    first_text = response_text(first_events)
    for marker in (HEAD_MARKER, LINES_END_MARKER, LONG_END_MARKER):
        if marker not in first_text:
            raise AcceptanceError(f"first live turn omitted marker {marker}")

    binding = coomi._read_coomi_session_binding_for_execution(
        workspace_root=workspace,
        storydex_session_id=session_id,
    )
    runtime_id = str(binding.get("runtimeSessionId") or "")
    session_path = coomi._validated_session_path(binding)
    if session_path is None:
        raise AcceptanceError("live session binding has no session path")
    session = json.loads(session_path.read_text(encoding="utf-8"))
    transcript = read_file_transcript(session)
    coverage = assert_complete_file_reads(transcript)
    read_events = [
        payload
        for name, payload in first_events
        if name == "ToolDone"
        and str(payload.get("tool_name") or "") == "read_file"
        and not bool(payload.get("is_error"))
    ]
    if len(read_events) != len([item for item in transcript if item["success"]]):
        raise AcceptanceError("translated read_file events do not match persisted tool results")
    if any(
        not str(payload.get("source_revision") or "")
        or not isinstance(payload.get("source_span"), dict)
        for payload in read_events
    ):
        raise AcceptanceError("translated read_file event omitted revision/span provenance")
    first_message_count = len(session.get("messages") or [])

    second_prompt = (
        "Do not call any tool. Using only this session's prior conversation, return the exact three "
        "acceptance markers you just found."
    )
    second_started = time.perf_counter()
    second_events = await asyncio.wait_for(
        collect_turn(
            service,
            workspace=workspace,
            session_id=session_id,
            prompt=second_prompt,
            contract_observations=contract_observations,
            observation_label="secondTurn",
        ),
        timeout=args.turn_timeout,
    )
    require_completed(second_events, "second live turn")
    second_text = response_text(second_events)
    for marker in (HEAD_MARKER, LINES_END_MARKER, LONG_END_MARKER):
        if marker not in second_text:
            raise AcceptanceError(f"restored session omitted marker {marker}")
    restored_binding = coomi._read_coomi_session_binding_for_execution(
        workspace_root=workspace,
        storydex_session_id=session_id,
    )
    if str(restored_binding.get("runtimeSessionId") or "") != runtime_id:
        raise AcceptanceError("second turn rebound to a different runtime session")
    restored_session = json.loads(session_path.read_text(encoding="utf-8"))
    if len(restored_session.get("messages") or []) <= first_message_count:
        raise AcceptanceError("second turn did not append to the persisted session")
    second_finished = time.perf_counter()

    error_events = await collect_turn(
        service,
        workspace=workspace,
        session_id=session_id,
        prompt="This request must fail bridge validation without replacing the bound session.",
        reasoning_effort="invalid-for-acceptance",
    )
    error_names = [name for name, _payload in error_events]
    if "AgentError" not in error_names or "AgentCompleted" in error_names:
        raise AcceptanceError("controlled bridge error did not terminate as AgentError")
    error_binding = coomi._read_coomi_session_binding_for_execution(
        workspace_root=workspace,
        storydex_session_id=session_id,
    )
    if str(error_binding.get("runtimeSessionId") or "") != runtime_id:
        raise AcceptanceError("controlled bridge error replaced the bound runtime session")
    after_error_session = json.loads(session_path.read_text(encoding="utf-8"))
    messages_after_error = len(after_error_session.get("messages") or [])
    if messages_after_error != len(restored_session.get("messages") or []):
        raise AcceptanceError("controlled bridge validation error mutated persisted history")

    recovery_started = time.perf_counter()
    recovery_events = await asyncio.wait_for(
        collect_turn(
            service,
            workspace=workspace,
            session_id=session_id,
            prompt=(
                "The previous request failed before a model round. Do not call any tool. "
                "Using the intact prior conversation, return the exact three acceptance markers."
            ),
            contract_observations=contract_observations,
            observation_label="recoveryTurn",
        ),
        timeout=args.turn_timeout,
    )
    require_completed(recovery_events, "recovery live turn")
    recovery_text = response_text(recovery_events)
    for marker in (HEAD_MARKER, LINES_END_MARKER, LONG_END_MARKER):
        if marker not in recovery_text:
            raise AcceptanceError(f"recovery turn omitted marker {marker}")
    recovery_binding = coomi._read_coomi_session_binding_for_execution(
        workspace_root=workspace,
        storydex_session_id=session_id,
    )
    if str(recovery_binding.get("runtimeSessionId") or "") != runtime_id:
        raise AcceptanceError("recovery turn rebound to a different runtime session")
    recovered_session = json.loads(session_path.read_text(encoding="utf-8"))
    if len(recovered_session.get("messages") or []) <= messages_after_error:
        raise AcceptanceError("recovery turn did not append to the persisted session")
    recovery_finished = time.perf_counter()

    missing_id = str(uuid.uuid4())
    coomi._write_coomi_session_binding(
        workspace_root=workspace,
        storydex_session_id="missing-history",
        runtime_session_id=missing_id,
    )
    missing_events = await collect_turn(
        service,
        workspace=workspace,
        session_id="missing-history",
        prompt="This must fail before contacting the model.",
    )
    if [name for name, _payload in missing_events] != ["AgentError"]:
        raise AcceptanceError("missing bound history did not fail before Agent startup")
    if (coomi.STORYDEX_COOMI_SESSIONS / f"{missing_id}.json").exists():
        raise AcceptanceError("missing bound history was silently recreated")

    corrupt_id = str(uuid.uuid4())
    corrupt_path = coomi.STORYDEX_COOMI_SESSIONS / f"{corrupt_id}.json"
    write_text(corrupt_path, "{corrupt")
    coomi._write_coomi_session_binding(
        workspace_root=workspace,
        storydex_session_id="corrupt-history",
        runtime_session_id=corrupt_id,
    )
    corrupt_events = await collect_turn(
        service,
        workspace=workspace,
        session_id="corrupt-history",
        prompt="This must fail before a model round.",
    )
    corrupt_names = [name for name, _payload in corrupt_events]
    if "AgentError" not in corrupt_names or "AgentCompleted" in corrupt_names or "TurnPhase" in corrupt_names:
        raise AcceptanceError("corrupt bound history did not fail closed before a model round")
    if corrupt_path.read_text(encoding="utf-8") != "{corrupt":
        raise AcceptanceError("corrupt bound history was overwritten")

    isolated_workspace = workspace.parent / "other-workspace"
    prepare_workspace(isolated_workspace)
    original_session_bytes = session_path.read_bytes()
    coomi._write_coomi_session_binding(
        workspace_root=isolated_workspace,
        storydex_session_id="workspace-mismatch",
        runtime_session_id=runtime_id,
    )
    mismatch_events = await collect_turn(
        service,
        workspace=isolated_workspace,
        session_id="workspace-mismatch",
        prompt="This cross-workspace binding must fail before contacting the model.",
    )
    mismatch_names = [name for name, _payload in mismatch_events]
    mismatch_errors = [payload for name, payload in mismatch_events if name == "AgentError"]
    if "AgentError" not in mismatch_names or "AgentCompleted" in mismatch_names:
        raise AcceptanceError("cross-workspace binding did not fail closed")
    if not mismatch_errors or "session_restore_failed(workspace_mismatch)" not in str(
        mismatch_errors[-1].get("message") or ""
    ):
        raise AcceptanceError("cross-workspace binding did not preserve the stable error code")
    if session_path.read_bytes() != original_session_bytes:
        raise AcceptanceError("cross-workspace binding mutated the original runtime session")

    unavailable_id = str(uuid.uuid4())
    unavailable_path = coomi.STORYDEX_COOMI_SESSIONS / f"{unavailable_id}.json"
    unavailable_workspace = workspace.parent / "removed-session-workspace"
    unavailable_session = dict(recovered_session)
    unavailable_session["id"] = unavailable_id
    unavailable_session["cwd"] = unavailable_workspace.as_posix()
    write_json(unavailable_path, unavailable_session)
    unavailable_before = unavailable_path.read_bytes()
    coomi._write_coomi_session_binding(
        workspace_root=workspace,
        storydex_session_id="workspace-unavailable",
        runtime_session_id=unavailable_id,
    )
    unavailable_events = await collect_turn(
        service,
        workspace=workspace,
        session_id="workspace-unavailable",
        prompt="This missing persisted workspace must fail before contacting the model.",
    )
    unavailable_names = [name for name, _payload in unavailable_events]
    unavailable_errors = [
        payload for name, payload in unavailable_events if name == "AgentError"
    ]
    if "AgentError" not in unavailable_names or "AgentCompleted" in unavailable_names:
        raise AcceptanceError("missing persisted workspace did not fail closed")
    if not unavailable_errors or "session_restore_failed(workspace_unavailable)" not in str(
        unavailable_errors[-1].get("message") or ""
    ):
        raise AcceptanceError("missing persisted workspace did not preserve the stable error code")
    if unavailable_path.read_bytes() != unavailable_before:
        raise AcceptanceError("missing persisted workspace mutated the runtime session")

    return {
        "status": "passed",
        "provider": args.provider_id,
        "model": args.model,
        "contentCatalog": catalog,
        "turnContracts": contract_observations,
        "intent": {
            "durationMs": round((first_started - intent_started) * 1000),
            "method": intent.get("method"),
            "canWrite": intent.get("canWrite"),
            "signals": intent.get("signals"),
        },
        "firstTurn": {
            "durationMs": round((second_started - first_started) * 1000),
            "eventNames": [name for name, _payload in first_events],
            "readCalls": len(transcript),
            "coverage": coverage,
            "performance": turn_performance(first_events),
        },
        "secondTurn": {
            "durationMs": round((second_finished - second_started) * 1000),
            "eventNames": [name for name, _payload in second_events],
            "sameRuntimeSession": True,
            "messagesBefore": first_message_count,
            "messagesAfter": len(restored_session.get("messages") or []),
            "performance": turn_performance(second_events),
        },
        "recoveryAfterError": {
            "durationMs": round((recovery_finished - recovery_started) * 1000),
            "errorEventNames": error_names,
            "recoveryEventNames": [name for name, _payload in recovery_events],
            "sameRuntimeSession": True,
            "messagesAfterError": messages_after_error,
            "messagesAfterRecovery": len(recovered_session.get("messages") or []),
            "performance": turn_performance(recovery_events),
        },
        "failureModes": {
            "missingHistoryEvents": [name for name, _payload in missing_events],
            "corruptHistoryEvents": corrupt_names,
            "workspaceMismatchEvents": mismatch_names,
            "workspaceUnavailableEvents": unavailable_names,
            "silentReplacement": False,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-id", default="OPENCODE")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--config", default="")
    parser.add_argument("--turn-timeout", type=float, default=300.0)
    parser.add_argument("--output-dir", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report_root = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else REPOSITORY_ROOT / "output" / "agent-integrity-live" / uuid.uuid4().hex[:10]
    )
    report_root.mkdir(parents=True, exist_ok=True)
    report_path = report_root / "acceptance-report.json"
    started = now_iso()
    try:
        with tempfile.TemporaryDirectory(prefix="storydex-agent-integrity-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            coomi_home = root / "coomi-home"
            prepare_workspace(workspace)
            prepare_integrity_sources(workspace)
            source_config = Path(args.config).resolve() if args.config else provider_config_path()
            provider = load_isolated_provider(
                source_config,
                coomi_home,
                args.provider_id,
                args.model,
            )
            report = asyncio.run(run_live(args, workspace=workspace, coomi_home=coomi_home))
            report.update({"startedAt": started, "finishedAt": now_iso(), "providerConfig": provider})
        write_json(report_path, redact(report))
        print(json.dumps({"status": "passed", "report": report_path.as_posix()}, ensure_ascii=False))
        return 0
    except Exception as exc:
        write_json(
            report_path,
            {"status": "failed", "startedAt": started, "finishedAt": now_iso(), "error": str(exc)},
        )
        print(
            json.dumps(
                {"status": "failed", "report": report_path.as_posix(), "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
