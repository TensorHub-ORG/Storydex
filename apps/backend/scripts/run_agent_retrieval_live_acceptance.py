"""Exercise v3 chunk retrieval through a real OpenCode Agent session."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
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


HEAD_MARKER = "P03_HEAD_A17"
MIDDLE_MARKER = "P03_MIDDLE_C83"
TAIL_MARKER = "P03_TAIL_F29"
ABSENT_MARKER = "P03_ABSENT_X91"
ERROR_PROBE = "P03_ERROR_E47"
SOURCE_PATH = "chapters/p03-long-retrieval.md"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def prepare_retrieval_source(workspace: Path) -> Path:
    content = (
        f"{HEAD_MARKER}\n"
        + ("普通的长篇背景段落，用于把唯一证据推入旧索引无法覆盖的中部。\n" * 3_500)
        + f"{MIDDLE_MARKER}\n"
        + ("另一组普通背景段落，用于让文件尾部保持足够距离。\n" * 3_500)
        + f"{TAIL_MARKER}\n"
    )
    path = workspace / SOURCE_PATH
    write_text(path, content)
    if len(path.read_text(encoding="utf-8")) <= 160_000:
        raise AcceptanceError("P0-3 live source is not long enough to exercise middle coverage")
    return path


async def collect_turn(
    service: Any,
    *,
    workspace: Path,
    session_id: str,
    prompt: str,
) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    turn_contract = {
        "intentFrame": {
            "primary": "general",
            "effect": "respond_only",
            "operationType": "inquiry",
            "canWrite": False,
        },
        "executionPolicy": {
            "directFileWrites": False,
            "allowedWriteRoots": [],
        },
        "reasoningEffort": "low",
    }
    async for name, payload in service.stream_events(
        prompt=prompt,
        trace_id=f"p03-live-{uuid.uuid4().hex[:12]}",
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


def project_search_transcript(session: dict[str, Any]) -> list[dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    transcript: list[dict[str, Any]] = []
    messages = session.get("messages") if isinstance(session.get("messages"), list) else []
    for message in messages:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("name") == "StorydexProjectSearch":
                calls[str(call.get("id") or "")] = dict(call.get("arguments") or {})
        call_id = str(message.get("tool_call_id") or "")
        if not call_id or call_id not in calls:
            continue
        persisted = str(message.get("content") or "")
        persisted_status, separator, raw_output = persisted.partition(": ")
        if not separator or persisted_status not in {"success", "error"}:
            raise AcceptanceError(
                f"StorydexProjectSearch has an unknown persisted status: {persisted[:200]}"
            )
        try:
            output = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise AcceptanceError(
                "StorydexProjectSearch did not preserve its structured JSON envelope: "
                f"{raw_output[:200]}"
            ) from exc
        transcript.append(
            {
                "arguments": calls[call_id],
                "output": output,
                "success": persisted_status == "success",
            }
        )
    return transcript


def require_query(transcript: list[dict[str, Any]], query: str) -> dict[str, Any]:
    matches = [
        item
        for item in transcript
        if str(item["arguments"].get("query") or "").strip() == query
    ]
    if not matches:
        raise AcceptanceError(f"Agent did not call StorydexProjectSearch with exact query {query}")
    return matches[-1]


def validate_hit(path: Path, item: dict[str, Any], marker: str) -> dict[str, Any]:
    output = item["output"]
    if not item["success"] or output.get("status") != "ok" or output.get("resultState") != "hits":
        raise AcceptanceError(f"search for {marker} was not an ok/hits result: {output}")
    results = output.get("results") if isinstance(output.get("results"), list) else []
    if not results:
        raise AcceptanceError(f"search for {marker} returned no result payload")
    hit = results[0]
    if hit.get("path") != SOURCE_PATH or marker not in str(hit.get("snippet") or ""):
        raise AcceptanceError(f"search for {marker} returned the wrong source/snippet: {hit}")
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    span = hit.get("snippetSpan") if isinstance(hit.get("snippetSpan"), dict) else {}
    snippet = str(hit.get("snippet") or "")
    if text[int(span.get("startChar") or 0) : int(span.get("endChar") or 0)] != snippet:
        raise AcceptanceError(f"character span does not reconstruct {marker}")
    if raw[int(span.get("startByte") or 0) : int(span.get("endByte") or 0)].decode("utf-8") != snippet:
        raise AcceptanceError(f"byte span does not reconstruct {marker}")
    expected_revision = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    if hit.get("revision") != expected_revision:
        raise AcceptanceError(f"revision mismatch for {marker}")
    return {
        "revision": hit.get("revision"),
        "chunkId": hit.get("chunkId"),
        "span": hit.get("span"),
        "snippetSpan": span,
    }


async def run_live(args: argparse.Namespace, *, workspace: Path, coomi_home: Path) -> dict[str, Any]:
    os.environ["STORYDEX_COOMI_HOME"] = str(coomi_home)
    bridge = REPOSITORY_ROOT / "vendor" / "coomi-rs" / "target" / "debug" / "storydex-coomi-bridge.exe"
    if not bridge.is_file():
        raise AcceptanceError(f"debug bridge is missing: {bridge}")
    os.environ["STORYDEX_COOMI_BRIDGE"] = str(bridge)

    from services import coomi_agent_service as coomi
    from services.content_pipeline_service import bootstrap_content_workspace
    from services.retrieval_service import get_retrieval_service

    source_path = workspace / SOURCE_PATH
    bootstrap_content_workspace(workspace)
    service = coomi.StorydexCoomiAgentService()
    session_id = f"opencode-p03-{uuid.uuid4().hex[:10]}"
    service.set_plan_mode(session_id=session_id, workspace_root=workspace, active=True)
    first_prompt = (
        "This is a retrieval protocol acceptance test. Use only StorydexProjectSearch and do not "
        "read or write files. Make four separate calls, each with maxResults=1 and "
        "pathPrefix='chapters/': first query exactly "
        f"{HEAD_MARKER}, then {MIDDLE_MARKER}, then {TAIL_MARKER}, then {ABSENT_MARKER}. "
        "Do not combine the queries. Return the three found markers and the exact resultState for "
        "the absent query."
    )
    first_started = time.perf_counter()
    first_events = await asyncio.wait_for(
        collect_turn(service, workspace=workspace, session_id=session_id, prompt=first_prompt),
        timeout=args.turn_timeout,
    )
    require_completed(first_events, "P0-3 retrieval turn")
    first_response = response_text(first_events)
    for expected in (HEAD_MARKER, MIDDLE_MARKER, TAIL_MARKER, "no_hits"):
        if expected not in first_response:
            raise AcceptanceError(f"first response omitted {expected}")

    binding = coomi._read_coomi_session_binding_for_execution(
        workspace_root=workspace,
        storydex_session_id=session_id,
    )
    runtime_id = str(binding.get("runtimeSessionId") or "")
    session_path = coomi._validated_session_path(binding)
    if session_path is None:
        raise AcceptanceError("P0-3 live session binding has no session path")
    session = json.loads(session_path.read_text(encoding="utf-8"))
    transcript = project_search_transcript(session)
    hit_summary = {
        marker: validate_hit(source_path, require_query(transcript, marker), marker)
        for marker in (HEAD_MARKER, MIDDLE_MARKER, TAIL_MARKER)
    }
    absent = require_query(transcript, ABSENT_MARKER)
    absent_output = absent["output"]
    if (
        not absent["success"]
        or absent_output.get("status") != "ok"
        or absent_output.get("resultState") != "no_hits"
    ):
        raise AcceptanceError(f"absent query was not distinguished as ok/no_hits: {absent_output}")

    retrieval = get_retrieval_service(workspace)
    index_before_failure = retrieval.index_status()
    # The next assertion intentionally corrupts the published FTS table.  Stop
    # the background publisher first so it does not repeatedly retry against a
    # deliberately broken database; the query path must surface the error
    # without attempting a repair.
    from services.content_pipeline_service import stop_content_pipeline

    stop_content_pipeline()
    conn = sqlite3.connect(retrieval.db_path)
    try:
        conn.execute("DROP TABLE chunks")
        conn.commit()
    finally:
        conn.close()

    transcript_count = len(transcript)
    second_prompt = (
        "Use only StorydexProjectSearch. Make exactly one call with query exactly "
        f"{ERROR_PROBE}, maxResults=1, and pathPrefix='chapters/'. Do not read files. "
        "Report the exact retrieval status returned by the failed tool."
    )
    second_started = time.perf_counter()
    second_events = await asyncio.wait_for(
        collect_turn(service, workspace=workspace, session_id=session_id, prompt=second_prompt),
        timeout=args.turn_timeout,
    )
    require_completed(second_events, "P0-3 index error turn")
    second_response = response_text(second_events)
    restored = json.loads(session_path.read_text(encoding="utf-8"))
    restored_transcript = project_search_transcript(restored)
    error_item = require_query(restored_transcript[transcript_count:], ERROR_PROBE)
    error_output = error_item["output"]
    if (
        error_item["success"]
        or error_output.get("status") != "index_error"
        or error_output.get("resultState") != "unavailable"
    ):
        raise AcceptanceError(f"broken index was not distinguished from no_hits: {error_output}")
    if "index_error" not in second_response:
        raise AcceptanceError("Agent response did not preserve the index_error status")

    restored_binding = coomi._read_coomi_session_binding_for_execution(
        workspace_root=workspace,
        storydex_session_id=session_id,
    )
    return {
        "status": "passed",
        "provider": args.provider_id,
        "model": args.model,
        "firstTurn": {
            "durationMs": round((second_started - first_started) * 1000),
            "searchCalls": transcript_count,
            "hits": hit_summary,
            "absentStatus": absent_output.get("status"),
            "absentResultState": absent_output.get("resultState"),
        },
        "index": index_before_failure,
        "secondTurn": {
            "durationMs": round((time.perf_counter() - second_started) * 1000),
            "sameRuntimeSession": str(restored_binding.get("runtimeSessionId") or "") == runtime_id,
            "persistedToolSuccess": error_item["success"],
            "status": error_output.get("status"),
            "resultState": error_output.get("resultState"),
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
        else REPOSITORY_ROOT / "output" / "agent-retrieval-live" / uuid.uuid4().hex[:10]
    )
    report_root.mkdir(parents=True, exist_ok=True)
    report_path = report_root / "acceptance-report.json"
    started = now_iso()
    try:
        with tempfile.TemporaryDirectory(prefix="storydex-agent-retrieval-") as temporary:
            try:
                root = Path(temporary)
                workspace = root / "workspace"
                coomi_home = root / "coomi-home"
                prepare_workspace(workspace)
                prepare_retrieval_source(workspace)
                source_config = Path(args.config).resolve() if args.config else provider_config_path()
                provider = load_isolated_provider(
                    source_config,
                    coomi_home,
                    args.provider_id,
                    args.model,
                )
                report = asyncio.run(run_live(args, workspace=workspace, coomi_home=coomi_home))
                report.update({"startedAt": started, "finishedAt": now_iso(), "providerConfig": provider})
            finally:
                # Stop the native watcher before TemporaryDirectory removes the
                # workspace; otherwise its worker can retry against a deleted
                # path and leak noisy errors into the acceptance output.
                from services.content_pipeline_service import stop_content_pipeline

                stop_content_pipeline()
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
