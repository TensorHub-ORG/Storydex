from __future__ import annotations

import subprocess

from scripts.run_agent_stream_replay_contract import (
    _prepare_contract_workspace,
    _workspace_effect_delta,
    _workspace_effect_snapshot,
)
from scripts.run_agent_stream_differential import compare_reports


def _report(
    *, terminal: str = "AgentCompleted", tools: list[str] | None = None
) -> dict:
    tool_sequence = tools if tools is not None else ["read_file"]
    critical = ["RunAccepted", "TurnContract", "AgentStarted"]
    if tool_sequence:
        critical.extend(["ToolStart", "ToolDone"])
    critical.extend([terminal, "done"])
    return {
        "observation": {
            "httpStatus": 200,
            "terminalEvent": terminal,
            "doneCount": 1,
            "toolSequence": tool_sequence,
            "providerIds": ["OPENCODE"],
            "models": ["deepseek-v4-flash"],
            "providerModes": ["replay"],
            "errorCount": int(terminal == "AgentError"),
            "replyPreview": "STORYDEX_AGENT_STREAM_CONTRACT_FILE_91C7"
            if tool_sequence
            else "",
            "eventCount": len(critical),
            "eventNames": critical,
            "phaseFirstSeen": {
                "intent_classification": 1,
                "context_assembly": 2,
                "workspace_snapshot": 3,
                "task_planning": 4,
                "model_execution": 5,
                "model": 6,
            },
        }
    }


def test_compare_reports_accepts_critical_parity_and_reports_extra_event_kinds() -> (
    None
):
    python = _report()
    rust = _report()
    python["observation"]["eventNames"].insert(-2, "GitCommitPrompt")
    python["observation"]["eventCount"] += 1

    parity = compare_reports(
        python,
        rust,
        {"expected": {"replyContains": ["STORYDEX_AGENT_STREAM_CONTRACT_FILE_91C7"]}},
    )

    assert parity["status"] == "passed"
    assert parity["differences"] == []
    assert parity["eventKindDifferences"] == {
        "pythonOnly": ["GitCommitPrompt"],
        "rustOnly": [],
    }


def test_compare_reports_fails_on_terminal_or_tool_divergence() -> None:
    python = _report()
    rust = _report(terminal="AgentError", tools=[])

    parity = compare_reports(python, rust, {"expected": {}})

    assert parity["status"] == "failed"
    fields = {difference["field"] for difference in parity["differences"]}
    assert {"terminalEvent", "toolSequence", "errorCount", "replyPreview"}.issubset(
        fields
    )


def test_prepare_contract_workspace_can_create_clean_git_baseline(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    _prepare_contract_workspace(
        workspace,
        {
            "workspace": {
                "initializeGit": True,
                "files": [
                    {
                        "path": "chapters/fixture.md",
                        "content": "fixture baseline\n",
                    }
                ],
            }
        },
    )

    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tracked = subprocess.run(
        [
            "git",
            "ls-files",
            "--error-unmatch",
            ".gitignore",
            "chapters/fixture.md",
        ],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert status.stdout == ""
    assert tracked.stdout.splitlines() == [".gitignore", "chapters/fixture.md"]
    assert (workspace / ".gitignore").read_text(encoding="utf-8").splitlines() == [
        ".storydex/.agent/",
        ".storydex/.cache/",
    ]


def test_workspace_effect_snapshot_tracks_project_changes_and_excludes_runtime(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    target = ".storydex/characters/fixture.md"
    _prepare_contract_workspace(
        workspace,
        {
            "workspace": {
                "initializeGit": True,
                "files": [{"path": target, "content": "before\n"}],
            }
        },
    )
    before = _workspace_effect_snapshot(workspace)
    (workspace / target).write_text("after\n", encoding="utf-8")
    internal = workspace / ".storydex/.agent/runtime/session.json"
    internal.parent.mkdir(parents=True, exist_ok=True)
    internal.write_text('{"revision": 9}\n', encoding="utf-8")

    effects = _workspace_effect_delta(before, _workspace_effect_snapshot(workspace))

    assert effects["changedPaths"] == [target]
    assert effects["modifiedPaths"] == [target]
    assert effects["gitHeadUnchanged"] is True
    assert effects["gitStatusAfter"] == [f" M {target}"]
    assert ".storydex/.agent/runtime/session.json" not in effects["after"]["files"]


def test_compare_reports_requires_matching_workspace_effects() -> None:
    target = ".storydex/characters/fixture.md"
    derived = ".storydex/wiki/index.json"
    effects = {
        "changedPaths": [target],
        "createdPaths": [],
        "modifiedPaths": [target],
        "deletedPaths": [],
        "gitPresent": True,
        "gitHeadUnchanged": True,
        "gitStatusBefore": [],
        "gitStatusAfter": [f" M {target}"],
        "after": {
            "excludedPrefixes": [".git/", ".storydex/.agent/", ".storydex/.cache/"],
            "files": {
                target: {"exists": True, "size": 6, "sha256": "fixed"},
            },
        },
    }
    python = _report(tools=["write_file"])
    rust = _report(tools=["write_file"])
    python["workspaceEffects"] = {
        **effects,
        "changedPaths": [target, derived],
        "modifiedPaths": [target, derived],
        "gitStatusAfter": [f" M {target}", f" M {derived}"],
        "after": {
            **effects["after"],
            "files": {
                **effects["after"]["files"],
                derived: {
                    "exists": True,
                    "size": 9,
                    "sha256": "derived",
                    "jsonMetadata": {"schemaVersion": 3},
                },
            },
        },
    }
    rust["workspaceEffects"] = effects

    parity = compare_reports(
        python,
        rust,
        {
            "expected": {
                "replyContains": ["STORYDEX_AGENT_STREAM_CONTRACT_FILE_91C7"],
                "workspaceEffects": {
                    "changedPaths": [target],
                    "gitHeadUnchanged": True,
                    "files": {target: {"sha256": "fixed"}},
                    "pythonStableDerivedPaths": [derived],
                    "pythonStableDerivedFiles": {
                        derived: {"jsonMetadata": {"schemaVersion": 3}}
                    },
                },
            }
        },
    )

    assert parity["status"] == "passed"
    assert parity["differences"] == []


def test_compare_reports_requires_matching_replacement_persistence() -> None:
    python = _report()
    rust = _report()
    python["observation"]["replacementSetup"] = dict(python["observation"])
    rust["observation"]["replacementSetup"] = dict(rust["observation"])
    persistence = {
        "oldTrace": {
            "status": "superseded",
            "superseded": True,
            "replacementStatus": "accepted",
            "replacementTraceId": "new-trace",
        },
        "newTrace": {
            "exists": True,
            "traceId": "new-trace",
            "status": "completed",
        },
        "runtimeSessionChanged": True,
        "runtimeSessionUnchanged": False,
        "sessionMessageCount": 2,
        "sessionContentMarkers": ["REPLACEMENT_NEW"],
    }
    python["observation"]["replacementPersistence"] = persistence
    rust["observation"]["replacementPersistence"] = {
        **persistence,
        "oldTrace": dict(persistence["oldTrace"]),
        "newTrace": dict(persistence["newTrace"]),
        "sessionContentMarkers": list(persistence["sessionContentMarkers"]),
    }
    fixture = {
        "replacementSetup": {"request": {"prompt": "REPLACEMENT_OLD"}},
        "expected": {
            "replacementPersistence": {
                "oldStatus": "superseded",
                "oldReplacementStatus": "accepted",
                "sessionContains": ["REPLACEMENT_NEW"],
                "sessionAbsent": ["REPLACEMENT_OLD"],
            }
        },
    }

    parity = compare_reports(python, rust, fixture)

    assert parity["status"] == "passed"
    rust["observation"]["replacementPersistence"]["oldTrace"][
        "replacementStatus"
    ] = "restored"
    parity = compare_reports(python, rust, fixture)
    assert parity["status"] == "failed"
    assert any(
        difference["field"] == "replacementPersistence"
        for difference in parity["differences"]
    )
