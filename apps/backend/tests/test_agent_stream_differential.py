from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

from scripts.run_agent_stream_replay_contract import (
    _prepare_contract_workspace,
    _workspace_effect_delta,
    _workspace_effect_snapshot,
)
from scripts.run_agent_stream_differential import compare_reports
from services.story_word_count_service import count_story_text_words


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


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
    shared = ".storydex/memory/length_tier_calibration.json"
    shared_facts = {
        "sampleCount": 1,
        "observationCount": 1,
        "updatedAtPresent": True,
    }
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
        "changedPaths": [target, derived, shared],
        "createdPaths": [shared],
        "modifiedPaths": [target, derived],
        "gitStatusAfter": [f" M {target}", f" M {derived}", f"?? {shared}"],
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
                shared: {
                    "exists": True,
                    "size": 100,
                    "sha256": "dynamic-python",
                    "jsonMetadata": {
                        "_type": "StoryLengthTierCalibration",
                        "_version": 2,
                    },
                    "jsonFacts": shared_facts,
                },
            },
        },
    }
    rust["workspaceEffects"] = {
        **effects,
        "changedPaths": [target, shared],
        "createdPaths": [shared],
        "gitStatusAfter": [f" M {target}", f"?? {shared}"],
        "after": {
            **effects["after"],
            "files": {
                **effects["after"]["files"],
                shared: {
                    "exists": True,
                    "size": 110,
                    "sha256": "dynamic-rust",
                    "jsonMetadata": {
                        "_type": "StoryLengthTierCalibration",
                        "_version": 2,
                    },
                    "jsonFacts": shared_facts,
                },
            },
        },
    }

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
                    "sharedDerivedPaths": [shared],
                    "sharedDerivedFiles": {
                        shared: {
                            "jsonMetadata": {
                                "_type": "StoryLengthTierCalibration",
                                "_version": 2,
                            },
                            "jsonFacts": shared_facts,
                        }
                    },
                },
            }
        },
    )

    assert parity["status"] == "passed"
    assert parity["differences"] == []


def test_compare_reports_requires_matching_story_event_facts() -> None:
    python = _report(tools=[])
    rust = _report(tools=[])
    expected_story_events = {
        "StoryDraftMeasured": [
            {
                "actualWordCount": 1519,
                "chapterLengthTier": "short",
                "tierHit": True,
            }
        ],
        "StoryCallAccounting": [
            {
                "logicalStoryCalls": 1,
                "providerAttempts": 1,
                "transportRetries": 0,
            }
        ],
    }
    python["observation"]["storyEvents"] = copy.deepcopy(expected_story_events)
    rust["observation"]["storyEvents"] = copy.deepcopy(expected_story_events)

    parity = compare_reports(
        python,
        rust,
        {"expected": {"storyEvents": expected_story_events}},
    )

    assert parity["status"] == "passed"
    rust["observation"]["storyEvents"]["StoryCallAccounting"][0][
        "providerAttempts"
    ] = 2
    parity = compare_reports(
        python,
        rust,
        {"expected": {"storyEvents": expected_story_events}},
    )
    assert parity["status"] == "failed"
    assert any(
        difference["field"] == "storyEvents"
        for difference in parity["differences"]
    )


def test_compare_reports_enforces_required_and_forbidden_event_contracts() -> None:
    expected_sequence = [
        "StoryProviderAttempt",
        "StoryCommitStarted",
        "StoryCommitFinished",
        "StoryDraftMeasured",
        "StoryGenerationValidation",
        "StoryCallAccounting",
    ]
    python = _report(tools=[])
    rust = _report(tools=[])
    for report in (python, rust):
        names = report["observation"]["eventNames"]
        names[-2:-2] = expected_sequence
        report["observation"]["eventCount"] = len(names)
    fixture = {
        "expected": {
            "httpStatus": 200,
            "requiredEventSequence": expected_sequence,
            "forbiddenEvents": ["StoryGenerationFailed"],
        }
    }

    assert compare_reports(python, rust, fixture)["status"] == "passed"

    rust["observation"]["eventNames"].remove("StoryCommitFinished")
    rust["observation"]["eventNames"].insert(-2, "StoryCommitFinished")
    parity = compare_reports(python, rust, fixture)
    assert parity["status"] == "failed"
    assert any(
        difference["field"] == "requiredEventSequence"
        for difference in parity["differences"]
    )

    rust["observation"]["eventNames"] = list(
        python["observation"]["eventNames"]
    )
    rust["observation"]["eventNames"].insert(-2, "StoryGenerationFailed")
    parity = compare_reports(python, rust, fixture)
    assert parity["status"] == "failed"
    assert any(
        difference["field"] == "forbiddenEvents"
        for difference in parity["differences"]
    )


def test_compare_reports_enforces_fixture_http_status() -> None:
    python = _report()
    rust = _report()

    parity = compare_reports(
        python,
        rust,
        {"expected": {"httpStatus": 201}},
    )

    assert parity["status"] == "failed"
    assert any(
        difference["field"] == "httpStatus"
        for difference in parity["differences"]
    )


def test_create_new_tier_fixtures_are_registered_and_freeze_disk_event_calibration() -> None:
    contract = json.loads(
        (
            REPOSITORY_ROOT
            / "apps/backend/contracts/agent-chat-stream-v1.json"
        ).read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (
            REPOSITORY_ROOT
            / "apps/backend/contracts/agent-runtime-manifest.json"
        ).read_text(encoding="utf-8")
    )
    stream_contract = next(
        item
        for item in manifest["contracts"]
        if item["id"] == "agent.chat.stream.v1"
    )
    expected = {
        "short": {
            "count": 1519,
            "tokens": 980,
            "preferred": (1000, 3000),
            "hard": 700,
            "ceiling": 4000,
            "size": 4603,
            "sha256": "8b136a984e034b1919d681c6b9b14653e8821fe0a7d2983d6ed224ed147409ae",
            "sampleCounts": {"short": 1, "medium": 0, "long": 0},
        },
        "medium": {
            "count": 2202,
            "tokens": 1420,
            "preferred": (2200, 5000),
            "hard": 1800,
            "ceiling": 7200,
            "size": 6680,
            "sha256": "027f907cf66b7dea870d779a4c077d48588c53dc369beb6286a469669a19f65f",
            "sampleCounts": {"short": 0, "medium": 1, "long": 0},
        },
        "long": {
            "count": 3181,
            "tokens": 2050,
            "preferred": (3000, 6000),
            "hard": 2500,
            "ceiling": 9000,
            "size": 9645,
            "sha256": "bc1e7417a7ca01e6106d085ef689c9673f166c925a68b39e63ba5d58a9205e7d",
            "sampleCounts": {"short": 0, "medium": 0, "long": 1},
        },
    }
    required_sequence = [
        "StoryProviderAttempt",
        "StoryCommitStarted",
        "StoryCommitFinished",
        "StoryDraftMeasured",
        "StoryGenerationValidation",
        "StoryCallAccounting",
    ]

    for tier, facts in expected.items():
        relative = (
            "apps/backend/contracts/fixtures/"
            f"agent-chat-stream-story-create-new-{tier}-v1"
        )
        assert relative in contract["replay"]["fixtures"]
        assert relative in stream_contract["replayFixtures"]
        fixture_root = REPOSITORY_ROOT / relative
        scenario = json.loads(
            (fixture_root / "scenario.json").read_text(encoding="utf-8")
        )
        replay = json.loads(
            (fixture_root / "provider-replay.json").read_text(encoding="utf-8")
        )
        expected_contract = scenario["expected"]
        draft = expected_contract["storyEvents"]["StoryDraftMeasured"][0]
        validation = expected_contract["storyEvents"][
            "StoryGenerationValidation"
        ][0]
        accounting = expected_contract["storyEvents"]["StoryCallAccounting"][0]
        chapter = expected_contract["workspaceEffects"]["files"][
            "chapters/第1章 未命名/001.md"
        ]
        calibration = expected_contract["workspaceEffects"][
            "sharedDerivedFiles"
        ][".storydex/memory/length_tier_calibration.json"]["jsonFacts"]
        response = replay["steps"][0]["response"]

        assert scenario["request"]["storyGeneration"]["chapterLengthTier"] == tier
        assert expected_contract["requiredEventSequence"] == required_sequence
        assert count_story_text_words(response["content"]) == facts["count"]
        assert draft["actualWordCount"] == facts["count"]
        assert draft["completionTokens"] == facts["tokens"]
        assert response["usage"]["output_tokens"] == facts["tokens"]
        assert validation["hardMinimum"] == facts["hard"]
        assert validation["runtimeSafetyMaximum"] == facts["ceiling"]
        assert accounting["chapterLengthTier"] == tier
        assert accounting["logicalStoryCalls"] == 1
        assert accounting["providerAttempts"] == 1
        assert accounting["transportRetries"] == 0
        assert chapter["size"] == facts["size"]
        assert chapter["sha256"] == facts["sha256"]
        observation = calibration["observations"][0]
        assert observation["sampleCounts"] == facts["sampleCounts"]
        assert observation["medians"][tier] == facts["count"]
        assert observation["bands"][tier] == list(facts["preferred"])


def test_create_new_multi_fragment_fixture_is_registered_and_publishable() -> None:
    relative = (
        "apps/backend/contracts/fixtures/"
        "agent-chat-stream-story-create-new-multi-fragment-v1"
    )
    contract = json.loads(
        (
            REPOSITORY_ROOT
            / "apps/backend/contracts/agent-chat-stream-v1.json"
        ).read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (
            REPOSITORY_ROOT
            / "apps/backend/contracts/agent-runtime-manifest.json"
        ).read_text(encoding="utf-8")
    )
    stream_contract = next(
        item
        for item in manifest["contracts"]
        if item["id"] == "agent.chat.stream.v1"
    )
    fixture_root = REPOSITORY_ROOT / relative
    scenario = json.loads(
        (fixture_root / "scenario.json").read_text(encoding="utf-8")
    )
    replay = json.loads(
        (fixture_root / "provider-replay.json").read_text(encoding="utf-8")
    )

    assert relative in contract["replay"]["fixtures"]
    assert relative in stream_contract["replayFixtures"]
    assert scenario["request"]["storyGeneration"]["fragmentCount"] == 3
    assert "providerCompletionCount" not in scenario["expected"]
    assert scenario["expected"]["storyEvents"]["StoryCallAccounting"][0][
        "providerAttempts"
    ] == 1
    assert scenario["expected"]["storyEvents"]["StoryGenerationValidation"][0][
        "fragmentCount"
    ] == 3
    assert count_story_text_words(replay["steps"][0]["response"]["content"]) >= 700
    assert scenario["expected"]["workspaceEffects"]["changedPaths"] == [
        "chapters/第1章 未命名/001.md",
        "chapters/第1章 未命名/002.md",
        "chapters/第1章 未命名/003.md",
    ]


def test_modify_existing_multi_fragment_fixture_freezes_contiguous_replacements() -> None:
    relative = (
        "apps/backend/contracts/fixtures/"
        "agent-chat-stream-story-modify-existing-multi-fragment-v1"
    )
    contract = json.loads(
        (
            REPOSITORY_ROOT
            / "apps/backend/contracts/agent-chat-stream-v1.json"
        ).read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (
            REPOSITORY_ROOT
            / "apps/backend/contracts/agent-runtime-manifest.json"
        ).read_text(encoding="utf-8")
    )
    stream_contract = next(
        item
        for item in manifest["contracts"]
        if item["id"] == "agent.chat.stream.v1"
    )
    fixture_root = REPOSITORY_ROOT / relative
    scenario = json.loads(
        (fixture_root / "scenario.json").read_text(encoding="utf-8")
    )
    replay = json.loads(
        (fixture_root / "provider-replay.json").read_text(encoding="utf-8")
    )
    expected = scenario["expected"]
    turn_plan = expected["turnContract"]["turnPlan"]
    effects = expected["workspaceEffects"]

    assert relative in contract["replay"]["fixtures"]
    assert relative in stream_contract["replayFixtures"]
    assert scenario["request"]["activeFile"] == "chapters/第1章 既有/002.md"
    assert scenario["request"]["storyGeneration"]["fragmentCount"] == 2
    assert turn_plan["authoritativeFragmentPaths"] == [
        "chapters/第1章 既有/002.md",
        "chapters/第1章 既有/003.md",
    ]
    assert [target["writeMode"] for target in turn_plan["fragmentTargets"]] == [
        "replace",
        "replace",
    ]
    assert effects["changedPaths"] == turn_plan["authoritativeFragmentPaths"]
    assert effects["createdPaths"] == []
    assert effects["files"]["chapters/第1章 既有/004.md"]["exists"] is True
    assert effects["files"]["chapters/第1章 既有/005.md"]["exists"] is False
    assert effects["files"][
        ".storydex/memory/length_tier_calibration.json"
    ]["exists"] is False
    assert len(replay["steps"]) == 1
    assert len(replay["steps"][0]["response"]["content"].split("\n\n")) == 6


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
