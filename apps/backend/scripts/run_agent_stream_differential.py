"""Compare Python Stable and Rust Refactor against the same Agent SSE replay."""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping, TextIO

import httpx


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPOSITORY_ROOT / "apps" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.run_agent_runtime_contract import run_contract  # noqa: E402
from scripts.run_agent_stream_replay_contract import (  # noqa: E402
    DEFAULT_FIXTURE_DIR,
    _prepare_contract_workspace,
    _prepare_replacement_bridge_injection,
    _prepare_runtime_session_fixture,
    _runtime_session_integrity_snapshot,
    _session_persistence_snapshot,
    _stabilize_runtime_workspace_baseline,
    _workspace_effect_delta,
    _workspace_effect_snapshot,
    _write_json,
    run_replay_contract,
)
from scripts.run_graph_live_acceptance import (  # noqa: E402
    AcceptanceError,
    load_isolated_provider,
    provider_config_path,
)
from services.agent_stream_contract import load_fixture  # noqa: E402


CRITICAL_EVENTS = frozenset(
    {
        "RunAccepted",
        "TurnContract",
        "AgentStarted",
        "ToolStart",
        "ToolDone",
        "AgentCompleted",
        "AgentError",
        "AgentCancelled",
        "done",
    }
)
PARITY_FIELDS = (
    "httpStatus",
    "terminalEvent",
    "terminalReason",
    "doneCount",
    "toolSequence",
    "toolErrorSequence",
    "interruptedToolSequence",
    "providerIds",
    "models",
    "providerModes",
    "errorCount",
    "replyPreview",
)


class AgentdProcess:
    def __init__(
        self,
        *,
        binary: Path,
        bridge: Path,
        coomi_home: Path,
        refactor_root: Path,
        replay_fixture: Path,
        log_path: Path,
    ) -> None:
        self.binary = binary
        self.bridge = bridge
        self.coomi_home = coomi_home
        self.refactor_root = refactor_root
        self.replay_fixture = replay_fixture
        self.log_path = log_path
        self.process: subprocess.Popen[str] | None = None
        self.stderr: TextIO | None = None
        self.port = 0
        self.token = ""

    @property
    def base_url(self) -> str:
        if not self.port:
            raise AcceptanceError("storydex-agentd has not reported a loopback port")
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.stderr = self.log_path.open("w", encoding="utf-8")
        environment = os.environ.copy()
        environment.update(
            {
                "STORYDEX_COOMI_BRIDGE": str(self.bridge),
                "STORYDEX_AGENTD_REFACTOR_ROOT": str(self.refactor_root),
                "STORYDEX_AGENT_PROVIDER_REPLAY_FIXTURE": str(self.replay_fixture),
            }
        )
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [
                str(self.binary),
                "--port",
                "0",
                "--coomi-home",
                str(self.coomi_home),
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_flags,
        )
        if self.process.stdout is None:
            raise AcceptanceError("storydex-agentd stdout is unavailable")
        ready_lines: queue.Queue[str] = queue.Queue(maxsize=1)
        reader = threading.Thread(
            target=lambda: ready_lines.put(self.process.stdout.readline()),
            name="storydex-agentd-ready",
            daemon=True,
        )
        reader.start()
        try:
            line = ready_lines.get(timeout=15.0)
        except queue.Empty as exc:
            self.stop()
            raise AcceptanceError(
                "storydex-agentd did not report ready within 15 seconds"
            ) from exc
        try:
            ready = json.loads(line)
        except json.JSONDecodeError as exc:
            return_code = self.process.poll()
            self.stop()
            raise AcceptanceError(
                f"storydex-agentd emitted an invalid ready packet; exit={return_code}"
            ) from exc
        if not isinstance(ready, Mapping) or ready.get("event") != "ready":
            self.stop()
            raise AcceptanceError("storydex-agentd first stdout packet is not ready")
        self.port = int(ready.get("port") or 0)
        self.token = str(ready.get("token") or "")
        if not self.port or not self.token:
            self.stop()
            raise AcceptanceError(
                "storydex-agentd ready packet is missing port or token"
            )

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None and self.port and self.token:
            try:
                with httpx.Client(timeout=5.0, trust_env=False) as client:
                    client.post(
                        self.base_url + "/api/v1/sys/shutdown",
                        headers={"Authorization": f"Bearer {self.token}"},
                    )
            except httpx.HTTPError:
                pass
        if process.poll() is None:
            try:
                process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)
        if process.stdout is not None:
            process.stdout.close()
        if self.stderr is not None:
            self.stderr.close()
        self.process = None


def _observation(report: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    observation = report.get("observation")
    if not isinstance(observation, Mapping):
        raise AcceptanceError(f"{label} report has no contract observation")
    return observation


def _critical_sequence(observation: Mapping[str, Any]) -> list[str]:
    names = observation.get("eventNames")
    if not isinstance(names, list):
        return []
    return [str(name) for name in names if str(name) in CRITICAL_EVENTS]


def _phase_sequence(observation: Mapping[str, Any]) -> list[str]:
    phases = observation.get("phaseFirstSeen")
    if not isinstance(phases, Mapping):
        return []
    indexed: list[tuple[int, str]] = []
    for name, index in phases.items():
        if isinstance(index, int) and not isinstance(index, bool):
            indexed.append((index, str(name)))
    return [name for _, name in sorted(indexed)]


def compare_reports(
    python_report: Mapping[str, Any],
    rust_report: Mapping[str, Any],
    fixture: Mapping[str, Any],
) -> dict[str, Any]:
    python_observation = _observation(python_report, "Python Stable")
    rust_observation = _observation(rust_report, "Rust Refactor")
    differences: list[dict[str, Any]] = []
    compared: dict[str, dict[str, Any]] = {}
    for field in PARITY_FIELDS:
        python_value = python_observation.get(
            field, 0 if field == "errorCount" else None
        )
        rust_value = rust_observation.get(field, 0 if field == "errorCount" else None)
        compared[field] = {"pythonStable": python_value, "rustRefactor": rust_value}
        if python_value != rust_value:
            differences.append(
                {
                    "field": field,
                    "pythonStable": python_value,
                    "rustRefactor": rust_value,
                }
            )

    python_critical = _critical_sequence(python_observation)
    rust_critical = _critical_sequence(rust_observation)
    compared["criticalSequence"] = {
        "pythonStable": python_critical,
        "rustRefactor": rust_critical,
    }
    if python_critical != rust_critical:
        differences.append(
            {
                "field": "criticalSequence",
                "pythonStable": python_critical,
                "rustRefactor": rust_critical,
            }
        )

    python_phases = _phase_sequence(python_observation)
    rust_phases = _phase_sequence(rust_observation)
    compared["phaseSequence"] = {
        "pythonStable": python_phases,
        "rustRefactor": rust_phases,
    }
    if python_phases != rust_phases:
        differences.append(
            {
                "field": "phaseSequence",
                "pythonStable": python_phases,
                "rustRefactor": rust_phases,
            }
        )

    python_names = {str(name) for name in python_observation.get("eventNames") or []}
    rust_names = {str(name) for name in rust_observation.get("eventNames") or []}
    expected = fixture.get("expected")
    expected = expected if isinstance(expected, Mapping) else {}
    if isinstance(fixture.get("interaction"), Mapping):
        python_interaction = python_observation.get("interaction")
        rust_interaction = rust_observation.get("interaction")
        compared["interaction"] = {
            "pythonStable": python_interaction,
            "rustRefactor": rust_interaction,
        }
        if python_interaction != rust_interaction:
            differences.append(
                {
                    "field": "interaction",
                    "pythonStable": python_interaction,
                    "rustRefactor": rust_interaction,
                }
            )
    if isinstance(fixture.get("setupInteractions"), list):
        python_setup = python_observation.get("setupInteractions")
        rust_setup = rust_observation.get("setupInteractions")
        compared["setupInteractions"] = {
            "pythonStable": python_setup,
            "rustRefactor": rust_setup,
        }
        if python_setup != rust_setup:
            differences.append(
                {
                    "field": "setupInteractions",
                    "pythonStable": python_setup,
                    "rustRefactor": rust_setup,
                }
            )
    if isinstance(fixture.get("replacementSetup"), Mapping):
        python_setup = python_observation.get("replacementSetup")
        rust_setup = rust_observation.get("replacementSetup")
        python_setup = python_setup if isinstance(python_setup, Mapping) else {}
        rust_setup = rust_setup if isinstance(rust_setup, Mapping) else {}
        setup_fields = PARITY_FIELDS
        python_values = {field: python_setup.get(field) for field in setup_fields}
        rust_values = {field: rust_setup.get(field) for field in setup_fields}
        compared["replacementSetup"] = {
            "pythonStable": python_values,
            "rustRefactor": rust_values,
        }
        if python_values != rust_values:
            differences.append(
                {
                    "field": "replacementSetup",
                    "pythonStable": python_values,
                    "rustRefactor": rust_values,
                }
            )
    expected_replacement = expected.get("replacementPersistence")
    if isinstance(expected_replacement, Mapping):
        marker_contains = [
            str(value) for value in expected_replacement.get("sessionContains") or []
        ]
        marker_absent = [
            str(value) for value in expected_replacement.get("sessionAbsent") or []
        ]

        def replacement_values(observation: Mapping[str, Any]) -> dict[str, Any]:
            persistence = observation.get("replacementPersistence")
            persistence = persistence if isinstance(persistence, Mapping) else {}
            old_trace = persistence.get("oldTrace")
            old_trace = old_trace if isinstance(old_trace, Mapping) else {}
            new_trace = persistence.get("newTrace")
            new_trace = new_trace if isinstance(new_trace, Mapping) else {}
            markers = [str(value) for value in persistence.get("sessionContentMarkers") or []]
            return {
                "oldStatus": old_trace.get("status"),
                "oldReplacementStatus": old_trace.get("replacementStatus"),
                "oldSuperseded": old_trace.get("superseded"),
                "replacementTargetsNewTrace": bool(new_trace.get("traceId"))
                and old_trace.get("replacementTraceId") == new_trace.get("traceId"),
                "newTracePresent": new_trace.get("exists"),
                "newTraceStatus": new_trace.get("status"),
                "runtimeSessionChanged": persistence.get("runtimeSessionChanged"),
                "runtimeSessionUnchanged": persistence.get("runtimeSessionUnchanged"),
                "sessionMessageCount": persistence.get("sessionMessageCount"),
                "sessionContains": {
                    marker: any(marker in value for value in markers)
                    for marker in marker_contains
                },
                "sessionAbsent": {
                    marker: not any(marker in value for value in markers)
                    for marker in marker_absent
                },
            }

        python_values = replacement_values(python_observation)
        rust_values = replacement_values(rust_observation)
        compared["replacementPersistence"] = {
            "expected": dict(expected_replacement),
            "pythonStable": python_values,
            "rustRefactor": rust_values,
        }
        if python_values != rust_values:
            differences.append(
                {
                    "field": "replacementPersistence",
                    "pythonStable": python_values,
                    "rustRefactor": rust_values,
                }
            )
    if expected.get("followupPersistence") is True:
        python_mailbox = python_observation.get("followupMailbox")
        rust_mailbox = rust_observation.get("followupMailbox")
        python_mailbox = python_mailbox if isinstance(python_mailbox, Mapping) else {}
        rust_mailbox = rust_mailbox if isinstance(rust_mailbox, Mapping) else {}
        mailbox_fields = (
            "revision",
            "revisionPositive",
            "paused",
            "pauseReason",
            "activeTraceEmpty",
            "messages",
            "eventTypeCounts",
        )
        python_values = {field: python_mailbox.get(field) for field in mailbox_fields}
        rust_values = {field: rust_mailbox.get(field) for field in mailbox_fields}
        compared["followupMailbox"] = {
            "pythonStable": python_values,
            "rustRefactor": rust_values,
        }
        required_events = [str(value) for value in expected.get("followupEvents") or []]
        python_events = {str(value) for value in python_mailbox.get("eventTypes") or []}
        rust_events = {str(value) for value in rust_mailbox.get("eventTypes") or []}
        missing_events = {
            "pythonStable": sorted(set(required_events) - python_events),
            "rustRefactor": sorted(set(required_events) - rust_events),
        }
        compared["followupEvents"] = {
            "required": required_events,
            "pythonStable": sorted(python_events),
            "rustRefactor": sorted(rust_events),
        }
        if python_values != rust_values or any(missing_events.values()):
            differences.append(
                {
                    "field": "followupMailbox",
                    "pythonStable": python_values,
                    "rustRefactor": rust_values,
                    "missingEvents": missing_events,
                }
            )
    if expected.get("sessionPersistence") is True:
        persistence_fields = (
            "bindingExists",
            "bindingValid",
            "sessionExists",
            "sessionValid",
            "sessionSchemaVersion",
            "providerId",
            "model",
            "workspaceMatches",
            "messageCount",
        )
        python_persistence = python_report.get("sessionPersistence")
        rust_persistence = rust_report.get("sessionPersistence")
        python_persistence = (
            python_persistence if isinstance(python_persistence, Mapping) else {}
        )
        rust_persistence = (
            rust_persistence if isinstance(rust_persistence, Mapping) else {}
        )
        python_values = {
            field: python_persistence.get(field) for field in persistence_fields
        }
        rust_values = {
            field: rust_persistence.get(field) for field in persistence_fields
        }
        compared["sessionPersistence"] = {
            "pythonStable": python_values,
            "rustRefactor": rust_values,
        }
        if python_values != rust_values or not all(
            python_values.get(field) is True
            for field in (
                "bindingExists",
                "bindingValid",
                "sessionExists",
                "sessionValid",
                "workspaceMatches",
            )
        ):
            differences.append(
                {
                    "field": "sessionPersistence",
                    "pythonStable": python_values,
                    "rustRefactor": rust_values,
                }
            )
    expected_integrity = expected.get("runtimeSessionIntegrity")
    if isinstance(expected_integrity, Mapping):
        python_integrity = python_report.get("runtimeSessionIntegrity")
        rust_integrity = rust_report.get("runtimeSessionIntegrity")
        python_integrity = (
            python_integrity if isinstance(python_integrity, Mapping) else {}
        )
        rust_integrity = rust_integrity if isinstance(rust_integrity, Mapping) else {}

        def integrity_values(value: Mapping[str, Any]) -> dict[str, Any]:
            before = value.get("before")
            before = before if isinstance(before, Mapping) else {}
            binding = before.get("binding")
            binding = binding if isinstance(binding, Mapping) else {}
            session = before.get("session")
            session = session if isinstance(session, Mapping) else {}
            return {
                "kind": value.get("kind"),
                "unchanged": value.get("unchanged"),
                "bindingExists": bool(binding.get("exists")),
                "sessionExists": bool(session.get("exists")),
            }

        python_values = integrity_values(python_integrity)
        rust_values = integrity_values(rust_integrity)
        expected_values = {
            str(key): value for key, value in expected_integrity.items()
        }
        compared["runtimeSessionIntegrity"] = {
            "expected": expected_values,
            "pythonStable": python_values,
            "rustRefactor": rust_values,
        }
        if (
            python_values != rust_values
            or any(python_values.get(key) != value for key, value in expected_values.items())
            or any(rust_values.get(key) != value for key, value in expected_values.items())
        ):
            differences.append(
                {
                    "field": "runtimeSessionIntegrity",
                    "expected": expected_values,
                    "pythonStable": python_values,
                    "rustRefactor": rust_values,
                }
            )
    expected_effects = expected.get("workspaceEffects")
    if isinstance(expected_effects, Mapping):
        expected_files = expected_effects.get("files")
        expected_files = expected_files if isinstance(expected_files, Mapping) else {}
        expected_derived_files = expected_effects.get("pythonStableDerivedFiles")
        expected_derived_files = (
            expected_derived_files
            if isinstance(expected_derived_files, Mapping)
            else {}
        )
        python_derived_paths = sorted(
            str(path)
            for path in expected_effects.get("pythonStableDerivedPaths") or []
        )
        target_paths = {
            str(path)
            for path in expected_effects.get("changedPaths") or []
        } | {str(path) for path in expected_files}

        def status_path(value: Any) -> str:
            raw = str(value or "")
            path = raw[3:].strip() if len(raw) >= 3 else raw.strip()
            if " -> " in path:
                path = path.rsplit(" -> ", 1)[-1]
            return path.strip('"')

        def effect_values(
            report: Mapping[str, Any], derived_paths: list[str]
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            effects = report.get("workspaceEffects")
            effects = effects if isinstance(effects, Mapping) else {}
            after = effects.get("after")
            after = after if isinstance(after, Mapping) else {}
            files = after.get("files")
            files = files if isinstance(files, Mapping) else {}
            derived = set(derived_paths)
            raw_changed = list(effects.get("changedPaths") or [])
            raw_created = list(effects.get("createdPaths") or [])
            raw_modified = list(effects.get("modifiedPaths") or [])
            raw_deleted = list(effects.get("deletedPaths") or [])
            raw_status_before = list(effects.get("gitStatusBefore") or [])
            raw_status_after = list(effects.get("gitStatusAfter") or [])
            normalized = {
                "changedPaths": [path for path in raw_changed if path not in derived],
                "createdPaths": [path for path in raw_created if path not in derived],
                "modifiedPaths": [path for path in raw_modified if path not in derived],
                "deletedPaths": [path for path in raw_deleted if path not in derived],
                "gitPresent": bool(effects.get("gitPresent")),
                "gitHeadUnchanged": bool(effects.get("gitHeadUnchanged")),
                "gitStatusBefore": [
                    value
                    for value in raw_status_before
                    if status_path(value) not in derived
                ],
                "gitStatusAfter": [
                    value
                    for value in raw_status_after
                    if status_path(value) not in derived
                ],
                "excludedPrefixes": list(after.get("excludedPrefixes") or []),
                "files": {
                    path: dict(files.get(path))
                    if isinstance(files.get(path), Mapping)
                    else {"exists": False, "size": 0, "sha256": ""}
                    for path in sorted(target_paths)
                },
            }
            raw = {
                "changedPaths": raw_changed,
                "createdPaths": raw_created,
                "modifiedPaths": raw_modified,
                "deletedPaths": raw_deleted,
                "gitStatusBefore": raw_status_before,
                "gitStatusAfter": raw_status_after,
                "derivedChangedPaths": sorted(
                    path for path in raw_changed if path in derived
                ),
                "derivedFiles": {
                    path: dict(files.get(path))
                    if isinstance(files.get(path), Mapping)
                    else {"exists": False, "size": 0, "sha256": ""}
                    for path in derived_paths
                },
            }
            return normalized, raw

        def expected_mismatches(actual: Mapping[str, Any]) -> list[dict[str, Any]]:
            mismatches: list[dict[str, Any]] = []
            for key in (
                "changedPaths",
                "createdPaths",
                "modifiedPaths",
                "deletedPaths",
                "gitPresent",
                "gitHeadUnchanged",
                "gitStatusBefore",
                "gitStatusAfter",
                "excludedPrefixes",
            ):
                if key in expected_effects and actual.get(key) != expected_effects.get(key):
                    mismatches.append(
                        {
                            "field": key,
                            "expected": expected_effects.get(key),
                            "actual": actual.get(key),
                        }
                    )
            actual_files = actual.get("files")
            actual_files = actual_files if isinstance(actual_files, Mapping) else {}
            for path, expected_file in expected_files.items():
                if not isinstance(expected_file, Mapping):
                    continue
                actual_file = actual_files.get(str(path))
                actual_file = actual_file if isinstance(actual_file, Mapping) else {}
                for key, expected_value in expected_file.items():
                    if actual_file.get(str(key)) != expected_value:
                        mismatches.append(
                            {
                                "field": f"files.{path}.{key}",
                                "expected": expected_value,
                                "actual": actual_file.get(str(key)),
                            }
                        )
            return mismatches

        def derived_mismatches(actual: Mapping[str, Any]) -> list[dict[str, Any]]:
            mismatches: list[dict[str, Any]] = []
            if actual.get("derivedChangedPaths") != python_derived_paths:
                mismatches.append(
                    {
                        "field": "pythonStableDerivedPaths",
                        "expected": python_derived_paths,
                        "actual": actual.get("derivedChangedPaths"),
                    }
                )
            actual_files = actual.get("derivedFiles")
            actual_files = actual_files if isinstance(actual_files, Mapping) else {}
            for path, expected_file in expected_derived_files.items():
                if not isinstance(expected_file, Mapping):
                    continue
                actual_file = actual_files.get(str(path))
                actual_file = actual_file if isinstance(actual_file, Mapping) else {}
                for key, expected_value in expected_file.items():
                    if actual_file.get(str(key)) != expected_value:
                        mismatches.append(
                            {
                                "field": f"pythonStableDerivedFiles.{path}.{key}",
                                "expected": expected_value,
                                "actual": actual_file.get(str(key)),
                            }
                        )
            return mismatches

        python_values, python_raw = effect_values(
            python_report, python_derived_paths
        )
        rust_values, rust_raw = effect_values(rust_report, python_derived_paths)
        python_mismatches = expected_mismatches(python_values)
        rust_mismatches = expected_mismatches(rust_values)
        python_derived_mismatches = derived_mismatches(python_raw)
        rust_derived_mismatches = (
            []
            if rust_raw.get("derivedChangedPaths") == []
            else [
                {
                    "field": "rustRefactorDerivedPaths",
                    "expected": [],
                    "actual": rust_raw.get("derivedChangedPaths"),
                }
            ]
        )
        compared["workspaceEffects"] = {
            "expected": dict(expected_effects),
            "pythonStable": {"normalized": python_values, "raw": python_raw},
            "rustRefactor": {"normalized": rust_values, "raw": rust_raw},
        }
        if (
            python_values != rust_values
            or python_mismatches
            or rust_mismatches
            or python_derived_mismatches
            or rust_derived_mismatches
        ):
            differences.append(
                {
                    "field": "workspaceEffects",
                    "pythonStable": {"normalized": python_values, "raw": python_raw},
                    "rustRefactor": {"normalized": rust_values, "raw": rust_raw},
                    "pythonExpectedMismatches": python_mismatches,
                    "rustExpectedMismatches": rust_mismatches,
                    "pythonDerivedMismatches": python_derived_mismatches,
                    "rustDerivedMismatches": rust_derived_mismatches,
                }
            )
    actual_event_kind_differences = {
        "pythonOnly": sorted(python_names - rust_names),
        "rustOnly": sorted(rust_names - python_names),
    }
    expected_event_kind_differences = expected.get("eventKindDifferences")
    if (
        isinstance(expected_event_kind_differences, Mapping)
        and actual_event_kind_differences != dict(expected_event_kind_differences)
    ):
        differences.append(
            {
                "field": "eventKindDifferences",
                "expected": dict(expected_event_kind_differences),
                "actual": actual_event_kind_differences,
            }
        )
    return {
        "status": "passed" if not differences else "failed",
        "compared": compared,
        "differences": differences,
        "replyMarkers": [str(value) for value in expected.get("replyContains") or []],
        "eventKindDifferences": actual_event_kind_differences,
        "eventCounts": {
            "pythonStable": python_observation.get("eventCount"),
            "rustRefactor": rust_observation.get("eventCount"),
        },
    }


def run_rust_replay_contract(
    args: argparse.Namespace,
    *,
    fixture: Mapping[str, Any],
    fixture_dir: Path,
    binary: Path,
    bridge: Path,
    output_root: Path,
) -> dict[str, Any]:
    scenario_path = fixture_dir / "scenario.json"
    replay_path = fixture_dir / "provider-replay.json"
    report_path = output_root / "contract-report.json"
    source_config = (
        Path(args.config).resolve() if args.config else provider_config_path()
    )
    report: dict[str, Any] = {}
    workspace_effects: dict[str, Any] | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="storydex-agentd-stream-contract-"
        ) as temporary:
            root = Path(temporary).resolve()
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
            agentd = AgentdProcess(
                binary=binary,
                bridge=runtime_bridge,
                coomi_home=coomi_home,
                refactor_root=root,
                replay_fixture=replay_path,
                log_path=output_root / "agentd.log",
            )
            workspace_before: dict[str, Any] | None = None
            try:
                agentd.start()
                report["workspaceBaseline"] = _stabilize_runtime_workspace_baseline(
                    workspace, fixture
                )
                workspace_before = _workspace_effect_snapshot(workspace)
                report = run_contract(
                    base_url=agentd.base_url,
                    token=agentd.token,
                    implementation="rust-refactor-agentd-replay",
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
                agentd.stop()
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
            "implementation": "rust-refactor-agentd-replay",
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


def run_differential(args: argparse.Namespace) -> dict[str, Any]:
    fixture_dir = (
        Path(args.fixture_dir).resolve()
        if str(args.fixture_dir or "").strip()
        else DEFAULT_FIXTURE_DIR.resolve()
    )
    fixture = load_fixture(fixture_dir / "scenario.json")
    suffix = ".exe" if os.name == "nt" else ""
    runtime_target = (
        REPOSITORY_ROOT / "apps" / "desktop" / "agent-runtime" / "target" / "debug"
    )
    binary = (
        Path(args.agentd).resolve()
        if args.agentd
        else runtime_target / f"storydex-agentd{suffix}"
    )
    bridge = (
        Path(args.bridge).resolve()
        if args.bridge
        else runtime_target / f"storydex-coomi-bridge{suffix}"
    )
    if not binary.is_file():
        raise AcceptanceError(f"debug storydex-agentd is missing: {binary}")
    if not bridge.is_file():
        raise AcceptanceError(f"debug storydex-coomi-bridge is missing: {bridge}")
    output_root = (
        Path(args.output_dir).resolve()
        if str(args.output_dir or "").strip()
        else REPOSITORY_ROOT
        / "output"
        / "agent-runtime-contract"
        / f"differential-{str(fixture.get('scenario') or 'fixture')}"
    )
    output_root.mkdir(parents=True, exist_ok=True)
    report_path = output_root / "differential-report.json"
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "contractId": "agent.chat.stream.v1",
        "status": "failed",
        "providerMode": "replay",
        "fixture": fixture.get("scenario"),
        "providerId": str(args.provider_id),
        "model": str(args.model),
        "implementations": {},
    }
    try:
        python_args = argparse.Namespace(
            provider_id=str(args.provider_id),
            model=str(args.model),
            config=str(args.config),
            bridge=str(bridge),
            fixture_dir=str(fixture_dir),
            output_dir=str(output_root / "python-stable"),
            timeout=float(args.timeout),
        )
        python_report = run_replay_contract(python_args)
        report["implementations"]["pythonStable"] = python_report
        rust_report = run_rust_replay_contract(
            args,
            fixture=fixture,
            fixture_dir=fixture_dir,
            binary=binary,
            bridge=bridge,
            output_root=output_root / "rust-refactor",
        )
        report["implementations"]["rustRefactor"] = rust_report
        parity = compare_reports(python_report, rust_report, fixture)
        report["parity"] = parity
        if parity["status"] != "passed":
            raise AcceptanceError(
                f"Python/Rust Agent SSE parity failed: {parity['differences']}"
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
    parser.add_argument("--fixture-dir", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    return parser.parse_args()


def main() -> int:
    try:
        report = run_differential(parse_args())
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
