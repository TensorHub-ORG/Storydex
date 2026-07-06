from pathlib import Path

from api import routes_agent
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
