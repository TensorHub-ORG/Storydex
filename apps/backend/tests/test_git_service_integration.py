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


def test_create_branch_before_first_commit(git_service: GitService, tmp_path: Path):
    workspace = tmp_path / "unborn-branch"
    workspace.mkdir()

    initial = git_service.list_branches(workspace)
    assert initial["current"] == GitService.DEFAULT_BRANCH

    created = git_service.create_branch(workspace, name="draft/opening", checkout=True)
    assert created["current"] == "draft/opening"
    assert created["summary"]["branch"] == "draft/opening"
    assert created["branches"] == [{"name": "draft/opening", "current": True}]

    with pytest.raises(GitServiceError):
        git_service.create_branch(workspace, name="draft/opening", checkout=True)

    (workspace / "opening.md").write_text("first draft\n", encoding="utf-8")
    committed = git_service.commit_all(workspace, message="draft: opening")
    assert committed["created"] is True
    assert committed["summary"]["branch"] == "draft/opening"


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


def test_summary_head_tracks_real_head_after_restore(git_service: GitService, tmp_path: Path):
    """`head` must come from HEAD, never from the newest `log --all` entry.

    History is read with `--all` so restore backups stay recoverable, but that
    makes the newest logged commit unrelated to what is checked out. Deriving
    `head` from the log made the panel mark the wrong row as current and report a
    stale "latest commit" after any restore.
    """
    workspace = tmp_path / "restore"
    workspace.mkdir()
    ids = []
    for index in range(4):
        (workspace / f"c{index}.md").write_text(f"body {index}\n", encoding="utf-8")
        ids.append(git_service.commit_all(workspace, message=f"c{index}")["commit"]["id"])

    target_id = ids[1]
    git_service.restore_to_commit(workspace, commit_id=target_id, create_backup=True)

    summary = git_service.read_summary(workspace)
    real_head = git_service._run_git(workspace, ["rev-parse", "HEAD"]).strip()
    assert summary["head"]["id"] == real_head == target_id
    # The abandoned commits stay listed (recoverable) but are tagged as off-branch
    # so the panel can group them instead of implying they are current history.
    # Do not assert their position: commits created in the same second can be
    # ordered differently by `git log --all` across Git versions and platforms.
    on_branch = [item for item in summary["recentCommits"] if item["onCurrentBranch"]]
    off_branch = [item for item in summary["recentCommits"] if not item["onCurrentBranch"]]
    assert {item["id"] for item in on_branch} == set(ids[:2])
    assert {item["id"] for item in off_branch} == set(ids[2:])
    # Exactly one row can be "current".
    assert sum(1 for item in summary["recentCommits"] if item["id"] == summary["head"]["id"]) == 1


def test_concurrent_commits_do_not_lose_changes(git_service: GitService, tmp_path: Path):
    """Parallel commits must not drop files or collide on the index.

    A worktree has one index, so unsynchronized `add -A`/`commit` pairs raced:
    callers hit `index.lock` failures and files were left uncommitted. That is
    why committing a single new file often produced no history entry while
    pasting many files did — the small change lost the race.
    """
    import threading

    workspace = tmp_path / "parallel"
    workspace.mkdir()
    (workspace / "seed.md").write_text("seed\n", encoding="utf-8")
    git_service.commit_all(workspace, message="seed")

    errors: list[str] = []
    expected_files = [f"file-{index}.md" for index in range(12)]

    def worker(name: str) -> None:
        try:
            (workspace / name).write_text(f"content {name}\n", encoding="utf-8")
            git_service.commit_all(workspace, message=f"commit {name}")
        except Exception as exc:  # noqa: BLE001 - the assertion below reports it
            errors.append(f"{name}: {exc}")

    threads = [threading.Thread(target=worker, args=(name,)) for name in expected_files]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    summary = git_service.read_summary(workspace)
    assert summary["clean"] is True
    assert summary["changedFiles"] == []
    # Every file must be tracked by HEAD; `commit_all` may legitimately batch
    # several files into one commit, but nothing may be left behind.
    tracked = set(git_service._run_git(workspace, ["ls-tree", "-r", "--name-only", "HEAD"]).splitlines())
    assert set(expected_files) <= tracked


def test_single_new_file_commit_appears_in_history(git_service: GitService, tmp_path: Path):
    """The reported symptom: one new file must produce a visible commit."""
    workspace = tmp_path / "single"
    workspace.mkdir()
    (workspace / "start.md").write_text("start\n", encoding="utf-8")
    git_service.commit_all(workspace, message="start")

    (workspace / "brand-new.md").write_text("just created\n", encoding="utf-8")
    result = git_service.commit_all(workspace, message="user: single new file")
    assert result["created"] is True

    summary = git_service.read_summary(workspace)
    assert summary["head"]["subject"] == "user: single new file"
    assert summary["clean"] is True
    subjects = [item["subject"] for item in summary["recentCommits"]]
    assert "user: single new file" in subjects
    # The client relies on this stamp to discard stale summary responses.
    assert float(summary["generatedAt"]) > 0
