from pathlib import Path
import asyncio

import pytest

from api import routes_agent
from core.exceptions import GitServiceError
from services.agent_git_autocommit_service import AgentGitAutoCommitService


class FakeInitialGitService:
    def __init__(self) -> None:
        self.committed = False
        self.commit_messages: list[str] = []

    def is_git_available(self) -> bool:
        return True

    def initialize_repository(self, root: Path) -> dict:
        return self.read_summary(root)

    def read_summary(self, root: Path) -> dict:
        if self.committed:
            return {
                "changedFiles": [],
                "recentCommits": [{"id": "abc123", "subject": "workspace: initial local snapshot"}],
            }
        return {
            "changedFiles": [{"status": "??", "relativePath": ".storydex/project.json"}],
            "recentCommits": [],
        }

    def commit_all(self, root: Path, *, message: str = "") -> dict:
        self.committed = True
        self.commit_messages.append(message)
        return {"commit": {"id": "abc123", "subject": message}}


def test_begin_turn_commits_initial_snapshot_before_baseline(tmp_path):
    service = AgentGitAutoCommitService()
    fake_git = FakeInitialGitService()
    service.git_service = fake_git

    snapshot = service.begin_turn(tmp_path)

    assert snapshot.available is True
    assert snapshot.initial_commit == {"id": "abc123", "subject": "workspace: initial local snapshot"}
    assert snapshot.baseline_status == {}
    assert fake_git.commit_messages == ["workspace: initial local snapshot"]


def test_agent_commit_decision_falls_back_when_message_generation_fails(monkeypatch, tmp_path):
    class FakeRequest:
        headers = {}

    class FakeAutoCommitService:
        def __init__(self) -> None:
            self.commit_message = ""

        def current_changes_payload(self, workspace_root, **kwargs):
            return {
                "status": "info",
                "changedFiles": [".storydex/project.json"],
                "changedFileCount": 1,
            }

        def commit_current_changes(self, workspace_root, *, message: str):
            self.commit_message = message
            return {
                "_type": "GitCommitResult",
                "created": True,
                "status": "success",
                "reason": "committed",
                "changedFiles": [".storydex/project.json"],
                "changedFileCount": 1,
                "commitHash": "abc123",
            }

        def _commit_message_for_prompt(self, prompt: str) -> str:
            assert prompt == "继续剧情"
            return "agent: update project files"

    class FakeCoomiService:
        async def generate_commit_message(self, **kwargs):
            raise RuntimeError("provider failed")

    fake_auto_commit = FakeAutoCommitService()
    monkeypatch.setattr(routes_agent, "agent_git_autocommit_service", fake_auto_commit)
    monkeypatch.setattr(routes_agent, "get_storydex_coomi_agent_service", lambda: FakeCoomiService())
    monkeypatch.setattr(routes_agent, "_read_agent_run_record", lambda trace_id, session_id: ({"prompt": "继续剧情"}, "s1"))
    monkeypatch.setattr(routes_agent, "_record_workspace_root", lambda record: tmp_path)
    monkeypatch.setattr(routes_agent, "_build_commit_message_diff_summary", lambda workspace_root, changed_files: "")
    monkeypatch.setattr(routes_agent, "_append_git_commit_decision_record", lambda **kwargs: None)

    payload = routes_agent.AgentCommitDecisionRequest(mode="auto", sessionId="s1")
    response = __import__("asyncio").run(
        routes_agent.agent_run_commit_decision("trace-1", payload, FakeRequest(), session_id_query="s1")
    )

    assert response.ok is True
    assert fake_auto_commit.commit_message == "agent: update project files"
    assert response.data["created"] is True
    assert response.data["generatedMessage"] is False
    assert response.data["commitMessageStrategy"] == "deterministic_fallback"


def test_skip_commit_decision_does_not_rescan_repository(monkeypatch, tmp_path):
    class FakeRequest:
        headers = {}

    class FakeAutoCommitService:
        rescanned = False

        def acknowledge_skip(self, workspace_root, **kwargs):
            return {
                "_type": "GitCommitResult",
                "created": False,
                "status": "info",
                "reason": "user_skipped",
                "changedFiles": kwargs["changed_files"],
                "changedFileCount": len(kwargs["changed_files"]),
            }

        def current_changes_payload(self, workspace_root, **kwargs):
            self.rescanned = True
            raise AssertionError("skip must not rescan Git status")

    fake_auto_commit = FakeAutoCommitService()
    monkeypatch.setattr(routes_agent, "agent_git_autocommit_service", fake_auto_commit)
    monkeypatch.setattr(
        routes_agent,
        "_read_agent_run_record",
        lambda trace_id, session_id: (
            {
                "workspaceRoot": str(tmp_path),
                "changeLedger": {
                    "changedFiles": ["chapters/001.md"],
                    "added": 12,
                    "removed": 3,
                },
            },
            "s1",
        ),
    )
    monkeypatch.setattr(routes_agent, "_record_workspace_root", lambda record: tmp_path)
    monkeypatch.setattr(routes_agent, "_append_git_commit_decision_record", lambda **kwargs: None)

    response = asyncio.run(
        routes_agent.agent_run_commit_decision(
            "trace-skip",
            routes_agent.AgentCommitDecisionRequest(mode="skip", sessionId="s1"),
            FakeRequest(),
            session_id_query="s1",
        )
    )

    assert response.ok is True
    assert response.data["reason"] == "user_skipped"
    assert fake_auto_commit.rescanned is False


def test_auto_commit_message_timeout_uses_deterministic_fallback(monkeypatch, tmp_path):
    class FakeRequest:
        headers = {}

    class FakeAutoCommitService:
        commit_message = ""

        def current_changes_payload(self, workspace_root, **kwargs):
            return {"status": "info", "changedFiles": ["chapters/001.md"], "changedFileCount": 1}

        def commit_current_changes(self, workspace_root, *, message: str):
            self.commit_message = message
            return {"_type": "GitCommitResult", "created": True, "status": "success", "changedFiles": []}

        def _commit_message_for_prompt(self, prompt: str) -> str:
            return "agent: deterministic fallback"

    class SlowCoomiService:
        async def generate_commit_message(self, **kwargs):
            await asyncio.sleep(0.05)
            return "agent: too late"

    fake_auto_commit = FakeAutoCommitService()
    monkeypatch.setattr(routes_agent, "agent_git_autocommit_service", fake_auto_commit)
    monkeypatch.setattr(routes_agent, "get_storydex_coomi_agent_service", lambda: SlowCoomiService())
    monkeypatch.setattr(routes_agent, "_COMMIT_MESSAGE_TIMEOUT_SECONDS", 0.005)
    monkeypatch.setattr(routes_agent, "_read_agent_run_record", lambda trace_id, session_id: ({"prompt": "继续剧情"}, "s1"))
    monkeypatch.setattr(routes_agent, "_record_workspace_root", lambda record: tmp_path)
    monkeypatch.setattr(routes_agent, "_build_commit_message_diff_summary", lambda *args, **kwargs: "")
    monkeypatch.setattr(routes_agent, "_append_git_commit_decision_record", lambda **kwargs: None)

    response = asyncio.run(
        routes_agent.agent_run_commit_decision(
            "trace-timeout",
            routes_agent.AgentCommitDecisionRequest(mode="auto", sessionId="s1"),
            FakeRequest(),
            session_id_query="s1",
        )
    )

    assert response.data["commitMessageStrategy"] == "deterministic_fallback"
    assert fake_auto_commit.commit_message == "agent: deterministic fallback"


class ScriptedGitService:
    def __init__(self, summaries=None) -> None:
        self.summaries = list(summaries or [])
        self.available = True
        self.commits = []
        self.diff_payload = {"totals": {"added": 7, "removed": 2}}
        self.commit_result = {
            "created": True,
            "commit": {"id": "abcdef123456", "shortId": "abcdef1"},
            "summary": {"changedFiles": []},
        }
        self.raise_on = set()

    def _fail(self, operation: str) -> None:
        if operation in self.raise_on:
            raise GitServiceError(f"{operation} failed")

    def is_git_available(self) -> bool:
        return self.available

    def initialize_repository(self, root: Path) -> dict:
        self._fail("initialize")
        return self.read_summary(root)

    def read_summary(self, root: Path) -> dict:
        self._fail("summary")
        if self.summaries:
            return self.summaries.pop(0)
        return {"changedFiles": [], "recentCommits": [{"id": "head"}]}

    def commit_all(self, root: Path, *, message: str = "") -> dict:
        self._fail("commit_all")
        self.commits.append(("all", message))
        return {"commit": {"id": "initial", "shortId": "initial"}}

    def read_diff(self, root: Path, *, paths) -> dict:
        self._fail("diff")
        return self.diff_payload

    def commit_paths(self, root: Path, *, paths, message: str) -> dict:
        self._fail("commit_paths")
        self.commits.append((list(paths), message))
        return self.commit_result

    def read_commit_diff(self, root: Path, *, commit_id: str, paths) -> dict:
        self._fail("commit_diff")
        return self.diff_payload


def _source_repository(root: Path) -> Path:
    (root / "apps/backend/services").mkdir(parents=True)
    (root / "apps/backend/services/story_project_service.py").write_text("", encoding="utf-8")
    (root / "apps/frontend/src").mkdir(parents=True)
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    (root / "package.json").write_text("{}", encoding="utf-8")
    return root


def test_begin_turn_rejects_source_repository_and_unavailable_git(tmp_path):
    service = AgentGitAutoCommitService()
    service.git_service = ScriptedGitService()
    source = service.begin_turn(_source_repository(tmp_path / "source"))
    assert source.available is False
    assert source.skip_reason == "source_repository"

    git = ScriptedGitService()
    git.available = False
    service.git_service = git
    unavailable = service.begin_turn(tmp_path / "novel")
    assert unavailable.available is False
    assert "not available" in unavailable.error_message


def test_begin_turn_handles_git_failure_and_existing_repository(tmp_path):
    service = AgentGitAutoCommitService()
    failing = ScriptedGitService()
    failing.raise_on.add("initialize")
    service.git_service = failing
    snapshot = service.begin_turn(tmp_path)
    assert snapshot.available is False
    assert snapshot.error_message == "initialize failed"

    changed = tmp_path / "chapter.md"
    changed.write_text("draft", encoding="utf-8")
    existing = ScriptedGitService(
        [
            {
                "changedFiles": [{"relativePath": "chapter.md", "status": "M"}],
                "recentCommits": [{"id": "head"}],
            },
            {
                "changedFiles": [{"relativePath": "chapter.md", "status": "M"}],
                "recentCommits": [{"id": "head"}],
            },
        ]
    )
    service.git_service = existing
    snapshot = service.begin_turn(tmp_path)
    assert snapshot.available is True
    assert snapshot.initial_commit is None
    assert snapshot.baseline_status == {"chapter.md": "M"}
    assert len(snapshot.baseline_fingerprints["chapter.md"]) == 64


def test_finish_turn_all_states(tmp_path):
    service = AgentGitAutoCommitService()
    unavailable = service.begin_turn(_source_repository(tmp_path / "source"))
    warning = service.finish_turn(unavailable)
    assert warning["status"] == "warning"
    assert warning["reason"] == "source_repository"

    git = ScriptedGitService([{"changedFiles": [], "recentCommits": [{"id": "head"}]}])
    service.git_service = git
    available = type(unavailable)(workspace_root=tmp_path, available=True, initial_commit={"id": "initial"})
    no_changes = service.finish_turn(available)
    assert no_changes["reason"] == "no_changes"
    assert no_changes["initialCommit"] == {"id": "initial"}

    summary = {
        "changedFiles": [
            {"relativePath": "chapters/2.md", "status": "M"},
            {"relativePath": "chapters/1.md", "status": "??"},
        ],
        "recentCommits": [{"id": "head"}],
    }
    git.summaries = [summary]
    disabled = service.finish_turn(available, commit_prompt_enabled=False)
    assert disabled["reason"] == "commit_prompt_disabled"
    assert disabled["changedFiles"] == ["chapters/1.md", "chapters/2.md"]
    assert (disabled["added"], disabled["removed"]) == (7, 2)

    git.summaries = [summary]
    prompt = service.finish_turn(available)
    assert prompt["_type"] == "GitCommitPrompt"
    assert prompt["promptRequired"] is True

    git.raise_on.add("summary")
    failure = service.finish_turn(available)
    assert failure["reason"] == "commit_check_failed"


def test_current_changes_payload_all_states(tmp_path):
    service = AgentGitAutoCommitService()
    source = service.current_changes_payload(_source_repository(tmp_path / "source"))
    assert source["reason"] == "source_repository"

    git = ScriptedGitService([{"changedFiles": [], "recentCommits": []}])
    service.git_service = git
    empty = service.current_changes_payload(tmp_path / "novel", prompt_required=True)
    assert empty["reason"] == "no_changes"
    assert empty["promptRequired"] is True

    git.summaries = [
        {
            "changedFiles": [{"relativePath": "notes.md", "status": "M"}],
            "recentCommits": [{"id": "head"}],
        }
    ]
    changed = service.current_changes_payload(
        tmp_path / "novel",
        event_type="GitCommitPrompt",
        status="pending",
        reason="manual",
        message="confirm",
        prompt_required=True,
    )
    assert changed["status"] == "pending"
    assert changed["changedFiles"] == ["notes.md"]
    assert changed["added"] == 7

    git.raise_on.add("initialize")
    failed = service.current_changes_payload(tmp_path / "novel")
    assert failed["status"] == "error"
    assert failed["reason"] == "git_unavailable"


def test_commit_current_changes_success_noop_and_failures(tmp_path):
    service = AgentGitAutoCommitService()
    source = service.commit_current_changes(_source_repository(tmp_path / "source"), message="ignored")
    assert source["reason"] == "source_repository"

    git = ScriptedGitService([{"changedFiles": [], "recentCommits": []}])
    service.git_service = git
    empty = service.commit_current_changes(tmp_path / "novel", message="empty")
    assert empty["created"] is False
    assert empty["reason"] == "no_changes"

    summary = {
        "changedFiles": [{"relativePath": "chapter.md", "status": "M"}],
        "recentCommits": [{"id": "head"}],
    }
    git.summaries = [summary]
    committed = service.commit_current_changes(tmp_path / "novel", message="agent: update")
    assert committed["created"] is True
    assert committed["commitHash"] == "abcdef123456"
    assert committed["diffSource"] == "commit"
    assert (committed["added"], committed["removed"]) == (7, 2)

    git.summaries = [summary]
    git.commit_result = {"created": False, "summary": summary}
    noop = service.commit_current_changes(tmp_path / "novel", message="noop")
    assert noop["reason"] == "no_changes"
    assert noop["diffSource"] == "working_tree"

    git.summaries = [summary]
    git.raise_on.add("commit_paths")
    failed = service.commit_current_changes(tmp_path / "novel", message="fail")
    assert failed["status"] == "error"
    assert failed["reason"] == "commit_failed"


def test_helpers_cover_status_fingerprints_messages_and_payload(monkeypatch, tmp_path):
    status = AgentGitAutoCommitService._status_map(
        [None, "bad", {}, {"relativePath": "a\\b.md", "status": " M "}]
    )
    assert status == {"a/b.md": "M"}
    assert AgentGitAutoCommitService._status_map(None) == {}
    assert AgentGitAutoCommitService._changed_paths_since(
        {"a": "M", "b": "M"},
        {"a": "M", "b": "A", "c": "??"},
        {"a": "one"},
        {"a": "two"},
    ) == ["a", "b", "c"]

    (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    original_read_bytes = Path.read_bytes

    def broken_read_bytes(path):
        if path.name == "unreadable.txt":
            raise OSError("denied")
        return original_read_bytes(path)

    (tmp_path / "unreadable.txt").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(Path, "read_bytes", broken_read_bytes)
    fingerprints = AgentGitAutoCommitService._fingerprints_for_paths(
        tmp_path,
        ["", "file.txt", "missing.txt", "folder", "unreadable.txt", "../escape.txt"],
    )
    assert len(fingerprints["file.txt"]) == 64
    assert fingerprints["missing.txt"] == "missing"
    assert fingerprints["folder"] == "non_file"
    assert fingerprints["unreadable.txt"] == "unreadable"
    assert "../escape.txt" not in fingerprints

    messages = {
        "wiki": "agent: update wiki and knowledge graph",
        "变量": "agent: update story memory",
        "角色": "agent: update character files",
        "目录": "agent: organize project structure",
        "chapter": "agent: generate story fragments",
        "unrelated": "agent: update project files",
    }
    for prompt, expected in messages.items():
        assert AgentGitAutoCommitService._commit_message_for_prompt(prompt) == expected

    service = AgentGitAutoCommitService()
    git = ScriptedGitService()
    git.raise_on.add("diff")
    service.git_service = git
    assert service._working_tree_totals(tmp_path, ["file.txt"]) == (0, 0)

    acknowledged = service.acknowledge_skip(
        tmp_path,
        changed_files=["a\\b.md", ""],
        added=-3,
        removed=-2,
    )
    assert acknowledged["changedFiles"] == ["a/b.md"]
    assert acknowledged["added"] == 0
    assert acknowledged["removed"] == 0
