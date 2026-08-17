"""Run the Agent chat/SSE contract through Python Stable with Rust provider replay.

This harness starts the normal FastAPI route and storydex-coomi-bridge against
an isolated workspace and provider config.  Only the model HTTP response is
replayed; orchestration, SSE framing, the Rust Agent loop, and the real
read_file tool all execute normally.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPOSITORY_ROOT / "apps" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.run_agent_runtime_contract import (  # noqa: E402
    DEFAULT_CHAT_STREAM_FIXTURE,
    run_contract,
)
from scripts.run_graph_live_acceptance import (  # noqa: E402
    AcceptanceError,
    BackendProcess,
    free_port,
    load_isolated_provider,
    prepare_workspace,
    provider_config_path,
)
from services.agent_stream_contract import load_fixture  # noqa: E402
from services.git_service import GitService  # noqa: E402


DEFAULT_FIXTURE_DIR = DEFAULT_CHAT_STREAM_FIXTURE.parent


def _prepare_replacement_bridge_injection(
    *,
    bridge: Path,
    root: Path,
    fixture: Mapping[str, Any],
) -> tuple[Path, Callable[[Mapping[str, Any]], None] | None]:
    replacement_setup = fixture.get("replacementSetup")
    replacement_setup = (
        replacement_setup if isinstance(replacement_setup, Mapping) else {}
    )
    after_setup = replacement_setup.get("afterSetup")
    if after_setup is None:
        return bridge, None
    if not isinstance(after_setup, Mapping):
        raise AcceptanceError("replacementSetup.afterSetup must be an object")
    if str(after_setup.get("action") or "") != "invalidateBridgeExecutable":
        raise AcceptanceError("unsupported replacement after-setup action")

    runtime_bridge = root / "runtime" / bridge.name
    runtime_bridge.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bridge, runtime_bridge)

    def apply_injection(action: Mapping[str, Any]) -> None:
        if str(action.get("action") or "") != "invalidateBridgeExecutable":
            raise AcceptanceError("replacement after-setup action changed at runtime")
        runtime_bridge.write_bytes(b"storydex-invalid-bridge-fixture")

    return runtime_bridge, apply_injection
WORKSPACE_SNAPSHOT_EXCLUDES = (
    ".git/",
    ".storydex/.agent/",
    ".storydex/.cache/",
)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _workspace_effect_snapshot(workspace: Path) -> dict[str, Any]:
    root = workspace.resolve()
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(relative == prefix.rstrip("/") or relative.startswith(prefix) for prefix in WORKSPACE_SNAPSHOT_EXCLUDES):
            continue
        content = path.read_bytes()
        snapshot: dict[str, Any] = {
            "exists": True,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        if path.suffix.casefold() == ".json":
            try:
                value = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = None
            if isinstance(value, Mapping):
                metadata = {
                    key: value[key]
                    for key in ("revision", "schemaVersion", "schema_version")
                    if key in value
                    and isinstance(value[key], (str, int, float, bool, type(None)))
                }
                if metadata:
                    snapshot["jsonMetadata"] = metadata
        files[relative] = snapshot

    git_head = ""
    git_status: list[str] = []
    if (root / ".git").exists():
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            status = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            git_head = head.stdout.strip()
            git_status = status.stdout.splitlines()
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = str(getattr(exc, "stderr", "") or exc).strip()
            raise AcceptanceError(
                f"cannot snapshot fixture Git state: {detail[:500]}"
            ) from exc
    return {
        "files": files,
        "gitHead": git_head,
        "gitStatus": git_status,
        "excludedPrefixes": list(WORKSPACE_SNAPSHOT_EXCLUDES),
    }


def _workspace_effect_delta(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    before_files = before.get("files")
    before_files = before_files if isinstance(before_files, Mapping) else {}
    after_files = after.get("files")
    after_files = after_files if isinstance(after_files, Mapping) else {}
    before_paths = set(str(path) for path in before_files)
    after_paths = set(str(path) for path in after_files)
    created = sorted(after_paths - before_paths)
    deleted = sorted(before_paths - after_paths)
    modified = sorted(
        path
        for path in before_paths & after_paths
        if before_files.get(path) != after_files.get(path)
    )
    before_head = str(before.get("gitHead") or "")
    after_head = str(after.get("gitHead") or "")
    return {
        "before": dict(before),
        "after": dict(after),
        "changedPaths": sorted(created + modified + deleted),
        "createdPaths": created,
        "modifiedPaths": modified,
        "deletedPaths": deleted,
        "gitPresent": bool(before_head or after_head),
        "gitHeadUnchanged": bool(before_head and before_head == after_head),
        "gitStatusBefore": list(before.get("gitStatus") or []),
        "gitStatusAfter": list(after.get("gitStatus") or []),
    }


def _stabilize_runtime_workspace_baseline(
    workspace: Path, fixture: Mapping[str, Any]
) -> dict[str, Any]:
    workspace_payload = fixture.get("workspace")
    workspace_payload = (
        workspace_payload if isinstance(workspace_payload, Mapping) else {}
    )
    if workspace_payload.get("stabilizeRuntimeBaseline") is not True:
        return {"stabilized": False, "commitsCreated": 0}
    root = workspace.resolve()
    if not (root / ".git").exists():
        raise AcceptanceError(
            "stabilizeRuntimeBaseline requires workspace.initializeGit=true"
        )

    def wait_until_stable() -> None:
        deadline = time.monotonic() + 12.0
        not_before = time.monotonic() + 0.75
        previous: dict[str, Any] | None = None
        unchanged_samples = 0
        while time.monotonic() < deadline:
            current = _workspace_effect_snapshot(root)
            if current == previous:
                unchanged_samples += 1
            else:
                unchanged_samples = 0
                previous = current
            if time.monotonic() >= not_before and unchanged_samples >= 3:
                return
            time.sleep(0.2)
        raise AcceptanceError(
            "fixture workspace did not stabilize before the Agent request"
        )

    commits_created = 0
    for _ in range(3):
        wait_until_stable()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if not status.stdout.strip():
            snapshot = _workspace_effect_snapshot(root)
            return {
                "stabilized": True,
                "commitsCreated": commits_created,
                "gitHead": snapshot["gitHead"],
                "gitStatus": snapshot["gitStatus"],
            }
        subprocess.run(
            ["git", "add", "-A"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Storydex",
                "-c",
                "user.email=storydex@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--quiet",
                "-m",
                "fixture runtime baseline",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        commits_created += 1
    raise AcceptanceError(
        "fixture workspace kept changing after three runtime baseline attempts"
    )


def _prepare_contract_workspace(workspace: Path, fixture: Mapping[str, Any]) -> None:
    prepare_workspace(workspace)
    workspace_payload = fixture.get("workspace")
    workspace_payload = workspace_payload if isinstance(workspace_payload, Mapping) else {}
    if workspace_payload.get("initializeStorydex") is True:
        from services.story_project_service import get_story_project_service

        get_story_project_service().ensure_project_structure(workspace)
    files = workspace_payload.get("files")
    if not isinstance(files, list) or not files:
        raise AcceptanceError("chat stream fixture workspace.files must be a non-empty array")
    resolved_workspace = workspace.resolve()
    for item in files:
        if not isinstance(item, Mapping):
            raise AcceptanceError("chat stream fixture workspace file must be an object")
        relative = Path(str(item.get("path") or ""))
        if relative.is_absolute() or not relative.parts:
            raise AcceptanceError("chat stream fixture workspace path must be relative")
        destination = (resolved_workspace / relative).resolve()
        try:
            destination.relative_to(resolved_workspace)
        except ValueError as exc:
            raise AcceptanceError("chat stream fixture workspace path escapes its root") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(str(item.get("content") or ""), encoding="utf-8")
    if workspace_payload.get("initializeGit") is True:
        ignore_path = resolved_workspace / ".gitignore"
        ignore_lines = (
            ignore_path.read_text(encoding="utf-8").splitlines()
            if ignore_path.is_file()
            else []
        )
        seen_ignore_lines = {line.strip() for line in ignore_lines if line.strip()}
        for safe_line in GitService.SAFE_GITIGNORE_LINES:
            if safe_line not in seen_ignore_lines:
                ignore_lines.append(safe_line)
        ignore_path.write_text(
            "\n".join(ignore_lines).rstrip() + "\n",
            encoding="utf-8",
        )
        commands = (
            ["git", "init", "--quiet"],
            ["git", "add", "-A"],
            [
                "git",
                "-c",
                "user.name=Storydex",
                "-c",
                "user.email=storydex@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "fixture baseline",
            ],
        )
        try:
            for command in commands:
                subprocess.run(
                    command,
                    cwd=resolved_workspace,
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = str(getattr(exc, "stderr", "") or exc).strip()
            raise AcceptanceError(
                f"cannot initialize fixture Git baseline: {detail[:500]}"
            ) from exc


def _runtime_session_integrity_snapshot(
    *, workspace: Path, coomi_home: Path, session_id: str, runtime_session_id: str
) -> dict[str, Any]:
    normalized_session = str(session_id or "default").strip() or "default"
    digest = hashlib.sha256(normalized_session.encode("utf-8")).hexdigest()[:24]
    binding_path = (
        workspace
        / ".storydex"
        / ".agent"
        / "runtime"
        / "coomi-sessions"
        / f"{digest}.json"
    )
    session_path = coomi_home / "sessions" / f"{runtime_session_id}.json"

    def file_snapshot(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {"exists": False, "size": 0, "sha256": ""}
        content = path.read_bytes()
        return {
            "exists": True,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    return {
        "binding": file_snapshot(binding_path),
        "session": file_snapshot(session_path),
    }


def _prepare_runtime_session_fixture(
    *, workspace: Path, coomi_home: Path, session_id: str, fixture: Mapping[str, Any]
) -> dict[str, Any] | None:
    setup = fixture.get("runtimeSessionSetup")
    if not isinstance(setup, Mapping):
        return None
    kind = str(setup.get("kind") or "").strip()
    if kind not in {
        "corrupt_binding",
        "corrupt_session",
        "missing_session",
        "workspace_mismatch",
    }:
        raise AcceptanceError(f"unsupported runtimeSessionSetup kind: {kind}")
    runtime_session_id = str(
        setup.get("runtimeSessionId") or "11111111-1111-4111-8111-111111111111"
    ).strip()
    try:
        uuid.UUID(runtime_session_id)
    except ValueError as exc:
        raise AcceptanceError("runtimeSessionSetup runtimeSessionId must be a UUID") from exc

    normalized_session = str(session_id or "default").strip() or "default"
    digest = hashlib.sha256(normalized_session.encode("utf-8")).hexdigest()[:24]
    binding_path = (
        workspace
        / ".storydex"
        / ".agent"
        / "runtime"
        / "coomi-sessions"
        / f"{digest}.json"
    )
    session_path = coomi_home / "sessions" / f"{runtime_session_id}.json"
    binding_path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "corrupt_binding":
        binding_path.write_text("{broken", encoding="utf-8")
    elif kind == "corrupt_session":
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text("{broken", encoding="utf-8")
        _write_json(
            binding_path,
            {
                "version": 2,
                "runtime": "storydex-coomi-rs",
                "workspaceRoot": str(workspace.resolve()),
                "storydexSessionId": normalized_session,
                "coomiSessionId": runtime_session_id,
                "runtimeSessionId": runtime_session_id,
                "historyPath": str(session_path.resolve()),
                "sessionPath": str(session_path.resolve()),
            },
        )
    else:
        _write_json(
            binding_path,
            {
                "version": 2,
                "runtime": "storydex-coomi-rs",
                "workspaceRoot": str(
                    (workspace.parent / "workspace-mismatch").resolve()
                    if kind == "workspace_mismatch"
                    else workspace.resolve()
                ),
                "storydexSessionId": normalized_session,
                "coomiSessionId": runtime_session_id,
                "runtimeSessionId": runtime_session_id,
                "historyPath": str(session_path.resolve()),
                "sessionPath": str(session_path.resolve()),
            },
        )
    return {
        "kind": kind,
        "runtimeSessionId": runtime_session_id,
        "before": _runtime_session_integrity_snapshot(
            workspace=workspace,
            coomi_home=coomi_home,
            session_id=normalized_session,
            runtime_session_id=runtime_session_id,
        ),
    }


def _session_persistence_snapshot(
    *, workspace: Path, coomi_home: Path, session_id: str
) -> dict[str, Any]:
    normalized_session = str(session_id or "default").strip() or "default"
    digest = hashlib.sha256(normalized_session.encode("utf-8")).hexdigest()[:24]
    binding_path = (
        workspace
        / ".storydex"
        / ".agent"
        / "runtime"
        / "coomi-sessions"
        / f"{digest}.json"
    )
    snapshot: dict[str, Any] = {
        "bindingExists": binding_path.is_file(),
        "bindingValid": False,
        "sessionExists": False,
        "sessionValid": False,
        "sessionSchemaVersion": 0,
        "providerId": "",
        "model": "",
        "workspaceMatches": False,
        "messageCount": 0,
    }
    if not binding_path.is_file():
        return snapshot
    try:
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        if not isinstance(binding, Mapping):
            return snapshot
        runtime_id = str(
            binding.get("runtimeSessionId") or binding.get("coomiSessionId") or ""
        ).strip()
        uuid.UUID(runtime_id)
        expected_session_path = (coomi_home / "sessions" / f"{runtime_id}.json").resolve()
        bound_session_path = Path(
            str(binding.get("sessionPath") or binding.get("historyPath") or "")
        ).resolve()
        snapshot["workspaceMatches"] = (
            str(binding.get("workspaceRoot") or "") == str(workspace.resolve())
            and str(binding.get("storydexSessionId") or "") == normalized_session
        )
        snapshot["bindingValid"] = bool(
            snapshot["workspaceMatches"]
            and bound_session_path == expected_session_path
        )
        snapshot["sessionExists"] = expected_session_path.is_file()
        if not expected_session_path.is_file():
            return snapshot
        session = json.loads(expected_session_path.read_text(encoding="utf-8"))
        if not isinstance(session, Mapping):
            return snapshot
        snapshot.update(
            {
                "sessionValid": str(session.get("id") or "") == runtime_id,
                "sessionSchemaVersion": int(session.get("schema_version") or 0),
                "providerId": str(session.get("provider_id") or ""),
                "model": str(session.get("model") or ""),
                "messageCount": len(
                    session.get("messages")
                    if isinstance(session.get("messages"), list)
                    else []
                ),
            }
        )
        return snapshot
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return snapshot


def run_replay_contract(args: argparse.Namespace) -> dict[str, Any]:
    fixture_dir = (
        Path(args.fixture_dir).resolve()
        if str(args.fixture_dir or "").strip()
        else DEFAULT_FIXTURE_DIR.resolve()
    )
    scenario_path = fixture_dir / "scenario.json"
    replay_path = fixture_dir / "provider-replay.json"
    fixture = load_fixture(scenario_path)
    if not replay_path.is_file():
        raise AcceptanceError(f"provider replay fixture is missing: {replay_path}")
    source_config = Path(args.config).resolve() if args.config else provider_config_path()
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
    output_root = (
        Path(args.output_dir).resolve()
        if str(args.output_dir or "").strip()
        else REPOSITORY_ROOT
        / "output"
        / "agent-runtime-contract"
        / f"python-replay-{uuid.uuid4().hex[:10]}"
    )
    output_root.mkdir(parents=True, exist_ok=True)
    report_path = output_root / "contract-report.json"
    original_bridge = os.environ.get("STORYDEX_COOMI_BRIDGE")
    original_replay = os.environ.get("STORYDEX_AGENT_PROVIDER_REPLAY_FIXTURE")
    original_routing = os.environ.get("AGENT_INTENT_ROUTING_MODE")
    report: dict[str, Any] = {}
    workspace_effects: dict[str, Any] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="storydex-agent-stream-contract-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            coomi_home = root / "coomi-home"
            _prepare_contract_workspace(workspace, fixture)
            load_isolated_provider(
                source_config,
                coomi_home,
                str(args.provider_id),
                str(args.model),
            )
            runtime_bridge, replacement_after_setup = (
                _prepare_replacement_bridge_injection(
                    bridge=bridge,
                    root=root,
                    fixture=fixture,
                )
            )
            runtime_integrity = _prepare_runtime_session_fixture(
                workspace=workspace,
                coomi_home=coomi_home,
                session_id="agent-stream-contract",
                fixture=fixture,
            )
            os.environ["STORYDEX_COOMI_BRIDGE"] = str(runtime_bridge)
            os.environ["STORYDEX_AGENT_PROVIDER_REPLAY_FIXTURE"] = str(replay_path)
            routing_mode = str(fixture.get("intentRoutingMode") or "hybrid").strip()
            if routing_mode not in {"legacy", "hybrid", "workflow", "direct"}:
                raise AcceptanceError(
                    f"unsupported fixture intentRoutingMode: {routing_mode}"
                )
            os.environ["AGENT_INTENT_ROUTING_MODE"] = routing_mode
            backend = BackendProcess(
                workspace=workspace,
                coomi_home=coomi_home,
                log_path=output_root / "backend.log",
                port=free_port(),
                repository_root=REPOSITORY_ROOT,
            )
            workspace_before: dict[str, Any] | None = None
            try:
                backend.start()
                report["workspaceBaseline"] = _stabilize_runtime_workspace_baseline(
                    workspace, fixture
                )
                workspace_before = _workspace_effect_snapshot(workspace)
                report = run_contract(
                    base_url=backend.base_url,
                    token="",
                    implementation="python-stable-rust-bridge-replay",
                    contract="chat-stream",
                    expected_provider=str(args.provider_id),
                    expected_model=str(args.model),
                    workspace_root=str(workspace),
                    fixture_path=str(scenario_path),
                    session_id="agent-stream-contract",
                    timeout_seconds=float(args.timeout),
                    replacement_after_setup=replacement_after_setup,
                )
                report["sessionPersistence"] = _session_persistence_snapshot(
                    workspace=workspace,
                    coomi_home=coomi_home,
                    session_id="agent-stream-contract",
                )
                if runtime_integrity is not None:
                    after = _runtime_session_integrity_snapshot(
                        workspace=workspace,
                        coomi_home=coomi_home,
                        session_id="agent-stream-contract",
                        runtime_session_id=runtime_integrity["runtimeSessionId"],
                    )
                    report["runtimeSessionIntegrity"] = {
                        **runtime_integrity,
                        "after": after,
                        "unchanged": runtime_integrity["before"] == after,
                    }
            finally:
                backend.stop()
                if workspace_before is not None:
                    workspace_effects = _workspace_effect_delta(
                        workspace_before,
                        _workspace_effect_snapshot(workspace),
                    )
                    if report:
                        report["workspaceEffects"] = workspace_effects
        report = {
            **report,
            "providerMode": "replay",
            "fixture": fixture.get("scenario"),
            "providerId": str(args.provider_id),
            "model": str(args.model),
        }
        _write_json(report_path, report)
        return {**report, "reportPath": report_path.as_posix()}
    except Exception as exc:
        failed_report = {
            "schemaVersion": 1,
            "contractId": "agent.chat.stream.v1",
            "status": "failed",
            "implementation": "python-stable-rust-bridge-replay",
            "providerMode": "replay",
            "fixture": fixture.get("scenario"),
            "providerId": str(args.provider_id),
            "model": str(args.model),
            "error": str(exc),
        }
        if workspace_effects is not None:
            failed_report["workspaceEffects"] = workspace_effects
        _write_json(
            report_path,
            failed_report,
        )
        raise AcceptanceError(f"{exc}; report={report_path.as_posix()}") from exc
    finally:
        for name, value in (
            ("STORYDEX_COOMI_BRIDGE", original_bridge),
            ("STORYDEX_AGENT_PROVIDER_REPLAY_FIXTURE", original_replay),
            ("AGENT_INTENT_ROUTING_MODE", original_routing),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-id", default="OPENCODE")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--config", default="")
    parser.add_argument("--bridge", default="")
    parser.add_argument("--fixture-dir", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = run_replay_contract(args)
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
