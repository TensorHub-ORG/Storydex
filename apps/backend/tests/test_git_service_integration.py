from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.exceptions import GitServiceError
from services.git_service import GitService


pytestmark = [pytest.mark.integration, pytest.mark.security]


@pytest.fixture
def git_service(monkeypatch):
    executable = shutil.which("git")
    if not executable:
        pytest.skip("git executable is unavailable")
    monkeypatch.setenv("STORYDEX_GIT_EXECUTABLE", executable)
    GitService._resolve_git_executable.cache_clear()
    service = GitService()
    yield service
    GitService._resolve_git_executable.cache_clear()


def test_full_local_git_lifecycle_and_restore(git_service: GitService, tmp_path: Path):
    workspace = tmp_path / "story"
    workspace.mkdir()
    (workspace / "chapters").mkdir()
    (workspace / "chapters" / "001.md").write_text("first\n", encoding="utf-8")
    runtime = workspace / ".storydex" / ".agent"
    runtime.mkdir(parents=True)
    (runtime / "private.json").write_text("secret", encoding="utf-8")

    initialized = git_service.initialize_repository(workspace)
    assert initialized["initialized"] is True
    assert initialized["branch"] == GitService.DEFAULT_BRANCH
    gitignore_content = (workspace / ".gitignore").read_text(encoding="utf-8")
    assert ".storydex/.agent/" in gitignore_content
    assert ".storydex/.cache/" in gitignore_content

    first = git_service.commit_all(workspace, message="story: first")
    assert first["created"] is True
    first_id = first["commit"]["id"]
    assert first["commit"]["subject"] == "story: first"
    assert git_service.commit_all(workspace, message="nothing")["created"] is False

    chapter = workspace / "chapters" / "001.md"
    chapter.write_text("first\nsecond\n", encoding="utf-8")
    (workspace / "notes.md").write_text("untracked\n", encoding="utf-8")
    summary = git_service.read_summary(workspace)
    assert summary["clean"] is False
    assert {item["relativePath"] for item in summary["changedFiles"]} == {"chapters/001.md", "notes.md"}
    assert all(".storydex/.agent" not in item["relativePath"] for item in summary["changedFiles"])

    working = git_service.read_diff(workspace)
    assert working["totals"]["files"] == 2
    assert working["totals"]["added"] >= 2
    selected = git_service.read_diff(workspace, paths=["chapters/001.md"], context_lines=0)
    assert [item["relativePath"] for item in selected["files"]] == ["chapters/001.md"]

    second = git_service.commit_paths(workspace, paths=["chapters/001.md"], message="story: second")
    assert second["created"] is True
    second_id = second["commit"]["id"]
    assert (workspace / "notes.md").exists()
    assert git_service.read_summary(workspace)["clean"] is False
    git_service.commit_paths(workspace, paths=["notes.md"], message="story: notes")

    commit_diff = git_service.read_commit_diff(workspace, commit_id=second_id)
    assert commit_diff["files"][0]["relativePath"] == "chapters/001.md"
    assert commit_diff["totals"]["added"] >= 1
    assert git_service.read_commit_diff(workspace, commit_id=second_id, paths=["missing.md"])["files"] == []

    chapter.write_text("dirty backup\n", encoding="utf-8")
    restored = git_service.restore_to_commit(workspace, commit_id=first_id, create_backup=True)
    assert restored["restored"] is True
    assert restored["backupCommit"]
    assert restored["backupRef"].startswith("storydex-backup-")
    assert chapter.read_text(encoding="utf-8") == "first\n"
    no_op = git_service.restore_to_commit(workspace, commit_id=first_id)
    assert no_op["restored"] is False


def test_first_commit_paths_empty_paths_and_validation(git_service: GitService, tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    assert git_service.read_summary(workspace)["initialized"] is False
    assert git_service.read_diff(workspace)["initialized"] is False
    assert git_service.read_commit_diff(workspace, commit_id="HEAD")["initialized"] is False
    assert git_service.commit_paths(workspace, paths=[], message="empty")["created"] is False
    (workspace / "a.md").write_text("a", encoding="utf-8")
    result = git_service.commit_paths(workspace, paths=["a.md", "a.md", ".storydex/.agent/private"], message="")
    assert result["created"] is True
    assert result["commit"]["subject"].startswith("story: local snapshot")
    assert git_service.commit_paths(workspace, paths=["a.md"], message="unchanged")["created"] is False
    with pytest.raises(GitServiceError):
        git_service.restore_to_commit(workspace, commit_id="")
    with pytest.raises(GitServiceError):
        git_service.restore_to_commit(workspace, commit_id="does-not-exist")
    with pytest.raises(GitServiceError):
        git_service.read_commit_diff(workspace, commit_id="")


def test_snapshot_and_diff_parsers_cover_text_binary_truncation_and_renames(git_service: GitService, tmp_path: Path):
    workspace = tmp_path / "files"
    workspace.mkdir()
    (workspace / "text.md").write_text("one\ntwo", encoding="utf-8")
    (workspace / "empty.md").write_text("", encoding="utf-8")
    (workspace / "binary.bin").write_bytes(b"abc\x00def")
    (workspace / "huge.md").write_text("\n".join(str(i) for i in range(2010)), encoding="utf-8")
    snapshot = git_service.build_file_snapshot_diff(
        workspace,
        paths=["text.md", "empty.md", "binary.bin", "huge.md", "missing.md"],
    )
    by_name = {item["relativePath"]: item for item in snapshot["files"]}
    assert by_name["text.md"]["added"] == 2
    assert by_name["binary.bin"]["hunks"][0]["header"] == "Binary file not shown"
    assert by_name["huge.md"]["truncated"] is True
    assert by_name["missing.md"]["hunks"] == []

    patch = """diff --git a/a.md b/a.md
--- a/a.md
+++ b/a.md
@@ -1,2 +1,2 @@ heading
 same
-old
+new
\\ No newline at end of file
"""
    parsed = GitService._parse_unified_diff_file(patch, relative_path="a.md", status="M")
    assert parsed["added"] == 1 and parsed["removed"] == 1
    assert {line["kind"] for line in parsed["hunks"][0]["lines"]} == {"context", "removed", "added"}
    fallback = GitService._parse_unified_diff_file("Binary files differ", relative_path="b.bin", status="M")
    assert fallback["hunks"][0]["header"] == "File changed"

    branch, changes = GitService._parse_status(
        '## No commits yet on develop\nR  "old.md" -> "new.md"\n?? .storydex/.agent/run.json\n M normal.md\n'
    )
    assert branch == "develop"
    assert [item["relativePath"] for item in changes] == ["new.md", "normal.md"]
    assert GitService._parse_status("## develop...origin/develop\n")[0] == "develop"


def test_unavailable_and_command_failure_paths(monkeypatch, tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    service = GitService()
    monkeypatch.setattr(service, "_resolve_git_executable", lambda: "")
    assert service.read_summary(workspace)["available"] is False
    assert service.read_diff(workspace)["available"] is False
    assert service.read_commit_diff(workspace, commit_id="HEAD")["available"] is False
    with pytest.raises(GitServiceError):
        service.initialize_repository(workspace)
    with pytest.raises(GitServiceError):
        service._run_git(workspace, ["status"])

    monkeypatch.setattr(service, "_resolve_git_executable", lambda: str(tmp_path / "missing-git"))
    with pytest.raises(GitServiceError) as started:
        service._run_git(workspace, ["status"])
    assert started.value.code == "git_service_error"
