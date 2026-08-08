from __future__ import annotations

import os
import shutil
import subprocess
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


def test_git_process_has_a_bounded_timeout(
    git_service: GitService,
    monkeypatch,
    tmp_path: Path,
):
    observed = {}

    def time_out(command, **kwargs):
        observed.update(kwargs)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("services.git_service.subprocess.run", time_out)

    with pytest.raises(GitServiceError, match="timed out") as exc_info:
        git_service._run_git_process_result(tmp_path, ["status"])

    assert observed["timeout"] == GitService.COMMAND_TIMEOUT_SECONDS
    assert exc_info.value.details["timeoutSeconds"] == GitService.COMMAND_TIMEOUT_SECONDS


def test_full_local_git_lifecycle_and_restore(git_service: GitService, tmp_path: Path):
    workspace = tmp_path / "story"
    workspace.mkdir()
    (workspace / "chapters").mkdir()
    (workspace / "chapters" / "001.md").write_text("first\n", encoding="utf-8")
    runtime = workspace / ".storydex" / ".agent"
    runtime.mkdir(parents=True)
    (runtime / "private.json").write_text("secret", encoding="utf-8")
    cache = workspace / ".storydex" / ".cache"
    cache.mkdir(parents=True)
    (cache / "retrieval.db").write_bytes(b"cache-v1")

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
    assert all(".storydex/.cache" not in item["relativePath"] for item in summary["changedFiles"])

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
    result = git_service.commit_paths(
        workspace,
        paths=["a.md", "a.md", ".storydex/.agent/private", ".storydex/.cache/retrieval.db"],
        message="",
    )
    assert result["created"] is True
    assert result["commit"]["subject"].startswith("story: local snapshot")
    assert git_service.commit_paths(workspace, paths=["a.md"], message="unchanged")["created"] is False
    with pytest.raises(GitServiceError):
        git_service.restore_to_commit(workspace, commit_id="")
    with pytest.raises(GitServiceError):
        git_service.restore_to_commit(workspace, commit_id="does-not-exist")
    with pytest.raises(GitServiceError):
        git_service.read_commit_diff(workspace, commit_id="")


def test_commit_paths_skips_stale_missing_pathspec_during_directory_rename(
    git_service: GitService,
    tmp_path: Path,
):
    workspace = tmp_path / "renamed-chapter"
    old_directory = workspace / "chapters" / "第1章 未命名"
    old_directory.mkdir(parents=True)
    (old_directory / "001.md").write_text("旧正文\n", encoding="utf-8")
    (workspace / "unrelated.md").write_text("baseline\n", encoding="utf-8")
    git_service.initialize_repository(workspace)
    git_service.commit_all(workspace, message="baseline")

    new_directory = workspace / "chapters" / "第1章 新标题"
    old_directory.rename(new_directory)
    (workspace / "unrelated.md").write_text("do not commit me\n", encoding="utf-8")

    result = git_service.commit_paths(
        workspace,
        paths=[
            "chapters/第1章 未命名/001.md",
            "chapters/第1章 新标题/001.md",
            ".storydex/temp/already-removed.md",
        ],
        message="agent: record renamed chapter",
    )

    assert result["created"] is True
    committed = git_service.read_commit_diff(workspace, commit_id=result["commit"]["id"])
    committed_paths = {item["relativePath"] for item in committed["files"]}
    assert "chapters/第1章 新标题/001.md" in committed_paths
    assert "unrelated.md" not in committed_paths
    remaining = {item["relativePath"] for item in git_service.read_summary(workspace)["changedFiles"]}
    assert remaining == {"unrelated.md"}


def test_commit_paths_with_only_stale_missing_paths_is_a_noop(git_service: GitService, tmp_path: Path):
    workspace = tmp_path / "stale-only"
    workspace.mkdir()
    (workspace / "chapter.md").write_text("baseline\n", encoding="utf-8")
    git_service.initialize_repository(workspace)
    git_service.commit_all(workspace, message="baseline")

    result = git_service.commit_paths(
        workspace,
        paths=[".storydex/temp/already-removed.md"],
        message="must not fail",
    )

    assert result["created"] is False


def test_tracked_cache_file_does_not_break_commit_paths(git_service: GitService, tmp_path: Path):
    """Regression: a .storydex/.cache/ file tracked by an older Storydex build
    must not make commit_paths fail with "The following paths are ignored".

    The cache file shows up in ``git status`` (tracked files bypass
    .gitignore), so the Agent auto-commit service includes it in the pathspec
    list. Without the internal-ignore filter, ``git add -A -- <path>`` rejects
    the explicit ignored path with exit code 1.
    """
    workspace = tmp_path / "novel"
    workspace.mkdir()
    (workspace / "chapters").mkdir()
    (workspace / "chapters" / "001.md").write_text("first\n", encoding="utf-8")

    # Commit the baseline (chapter + .gitignore) so the worktree is clean.
    git_service.initialize_repository(workspace)
    git_service.commit_all(workspace, message="baseline")

    # Simulate a legacy commit that tracked a .cache/ file before the ignore
    # rule existed.
    cache_dir = workspace / ".storydex" / ".cache"
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "retrieval.fts5.v2.db"
    cache_file.write_bytes(b"cache-v1")
    git_service._run_git(workspace, ["add", "-f", "--", ".storydex/.cache/retrieval.fts5.v2.db"])
    git_service._run_git(workspace, ["commit", "--no-gpg-sign", "-m", "legacy: track cache"])
    assert git_service.read_summary(workspace)["clean"] is True

    # Modify both the chapter and the tracked cache file.
    (workspace / "chapters" / "001.md").write_text("first\nsecond\n", encoding="utf-8")
    cache_file.write_bytes(b"cache-v2-regenerated")

    # commit_paths must succeed even when the cache file is in the pathspec
    # list (the auto-commit service passes every path from git status).
    result = git_service.commit_paths(
        workspace,
        paths=["chapters/001.md", ".storydex/.cache/retrieval.fts5.v2.db"],
        message="agent: update chapter",
    )
    assert result["created"] is True

    commit_diff = git_service.read_commit_diff(
        workspace,
        commit_id=result["commit"]["id"],
    )
    committed_paths = {item["relativePath"] for item in commit_diff["files"]}
    assert "chapters/001.md" in committed_paths
    assert all(".storydex/.cache/" not in path for path in committed_paths)


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


def test_switch_branch_with_tracked_cache_file(git_service: GitService, tmp_path: Path):
    """Regression: 旧版 Storydex 提交过 .storydex/.cache/ 文件后切换分支报 500。

    _ensure_internal_paths_untracked 的 git rm --cached 会暂存删除，
    原始 _is_worktree_clean 检测到暂存删除判定为脏→拒绝切换→500。
    修复后 _is_worktree_clean 与 _parse_status 一致过滤内部忽略路径。
    """
    workspace = tmp_path / "branch-switch"
    workspace.mkdir()
    (workspace / "chapters").mkdir()
    (workspace / "chapters" / "001.md").write_text("first\n", encoding="utf-8")
    git_service.initialize_repository(workspace)
    git_service.commit_all(workspace, message="baseline")

    # 模拟旧版遗留的已跟踪缓存文件
    cache_dir = workspace / ".storydex" / ".cache"
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "retrieval.db"
    cache_file.write_bytes(b"cache-v1")
    git_service._run_git(workspace, ["add", "-f", "--", ".storydex/.cache/retrieval.db"])
    git_service._run_git(workspace, ["commit", "--no-gpg-sign", "-m", "legacy: track cache"])

    # 创建第二个分支
    git_service.create_branch(workspace, name="draft/alt-path", checkout=False)

    # initialize_repository 会触发 git rm --cached 暂存删除，
    # switch_branch 必须能成功切换（不被暂存删除阻塞）
    result = git_service.switch_branch(workspace, name="draft/alt-path")
    assert result["current"] == "draft/alt-path"
    assert result["summary"]["branch"] == "draft/alt-path"


def test_switch_branch_status_codes(git_service: GitService, tmp_path: Path):
    """switch_branch 的用户态错误应返回正确的 HTTP 状态码而非 500。"""
    workspace = tmp_path / "status-codes"
    workspace.mkdir()
    (workspace / "a.md").write_text("a\n", encoding="utf-8")
    git_service.initialize_repository(workspace)
    git_service.commit_all(workspace, message="init")

    # 分支不存在 → 404
    with pytest.raises(GitServiceError) as exc_info:
        git_service.switch_branch(workspace, name="nonexistent")
    assert exc_info.value.status_code == 404

    # 非法分支名 → 400
    with pytest.raises(GitServiceError) as exc_info:
        git_service.switch_branch(workspace, name="bad..name")
    assert exc_info.value.status_code == 400


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


def test_nested_project_refuses_parent_repository_without_staging_or_committing(
    git_service: GitService,
    tmp_path: Path,
):
    parent = tmp_path / "parent"
    project = parent / "story-project"
    project.mkdir(parents=True)
    git_service.initialize_repository(parent)
    (project / "chapter.md").write_text("outside the project repository boundary\n", encoding="utf-8")

    with pytest.raises(GitServiceError, match="does not match") as summary_error:
        git_service.read_summary(project)
    assert Path(summary_error.value.details["projectRoot"]) == project.resolve()
    assert Path(summary_error.value.details["gitTopLevel"]) == parent.resolve()

    with pytest.raises(GitServiceError, match="does not match"):
        git_service.commit_all(project, message="must not reach the parent repository")

    assert not (project / ".git").exists()
    assert git_service._run_git(parent, ["diff", "--cached", "--name-only"]) == ""
    assert not git_service._has_head_commit(parent)


def test_git_worktree_file_is_accepted_when_its_top_level_matches_project(
    git_service: GitService,
    tmp_path: Path,
):
    source = tmp_path / "source"
    worktree = tmp_path / "linked-worktree"
    source.mkdir()
    (source / "seed.md").write_text("seed\n", encoding="utf-8")
    git_service.commit_all(source, message="seed")
    git_service._run_git(source, ["worktree", "add", "-b", "story-worktree", str(worktree)])

    assert (worktree / ".git").is_file()
    assert git_service.is_repository_initialized(worktree) is True
    (worktree / "chapter.md").write_text("worktree chapter\n", encoding="utf-8")
    result = git_service.commit_paths(worktree, paths=["chapter.md"], message="worktree commit")
    assert result["created"] is True
    assert git_service.read_summary(worktree)["clean"] is True


def test_git_paths_cannot_escape_project_root(git_service: GitService, tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    git_service.initialize_repository(project)
    outside = tmp_path / "outside.md"
    outside.write_text("private\n", encoding="utf-8")

    for unsafe_path in ("../outside.md", "/outside.md", "C:\\outside.md", "safe/../../outside.md"):
        with pytest.raises(GitServiceError, match="stay inside"):
            git_service.commit_paths(project, paths=[unsafe_path], message="unsafe")
        with pytest.raises(GitServiceError, match="stay inside"):
            git_service.build_file_snapshot_diff(project, paths=[unsafe_path])

    link = project / "outside-link.md"
    try:
        link.symlink_to(outside)
    except OSError:
        return
    with pytest.raises(GitServiceError, match="resolves outside"):
        git_service.build_file_snapshot_diff(project, paths=["outside-link.md"])


@pytest.mark.skipif(os.name != "nt", reason="Windows path comparison semantics")
def test_windows_repository_path_comparison_accepts_case_variants(git_service: GitService, tmp_path: Path):
    workspace = tmp_path / "MixedCaseStory"
    workspace.mkdir()
    git_service.initialize_repository(workspace)

    case_variant = Path(str(workspace).swapcase())
    assert git_service.is_repository_initialized(case_variant) is True
    assert git_service._paths_refer_to_same_location(workspace, case_variant) is True


# -----------------------------------------------------------------------------
# 平行时空线 (Parallel Timeline)
# -----------------------------------------------------------------------------


def _seed_timeline_workspace(git_service: GitService, workspace: Path) -> list[str]:
    """创建一个 develop 主线 + 一个分叉分支的测试仓库，返回 develop 上的 commit id。"""
    (workspace / "chapters").mkdir()
    (workspace / "chapters" / "001.md").write_text("first\n", encoding="utf-8")
    git_service.initialize_repository(workspace)
    first = git_service.commit_all(workspace, message="c1")["commit"]["id"]
    (workspace / "chapters" / "001.md").write_text("first\nsecond\n", encoding="utf-8")
    git_service.commit_all(workspace, message="c2")
    return [first]


def test_read_timeline_returns_all_branches_and_layout(git_service: GitService, tmp_path: Path):
    """timeline 必须返回所有分支的提交节点、边和 (column, row) 布局坐标。

    Storydex 分支只分不合，节点通常只属于一个分支；当前分支 lane=0。
    """
    workspace = tmp_path / "timeline"
    workspace.mkdir()
    first_ids = _seed_timeline_workspace(git_service, workspace)
    first_id = first_ids[0]

    # 创建分叉分支：从 first commit 拉出新分支
    git_service.create_branch(workspace, name="worldline/alt", checkout=False)

    timeline = git_service.read_timeline(workspace)
    assert timeline["available"] is True
    assert timeline["initialized"] is True
    assert timeline["detached"] is False
    assert timeline["currentBranch"] == GitService.DEFAULT_BRANCH

    branch_names = [b["name"] for b in timeline["branches"]]
    assert GitService.DEFAULT_BRANCH in branch_names
    assert "worldline/alt" in branch_names

    # 当前分支 lane=0
    current_branch_info = next(b for b in timeline["branches"] if b["isCurrent"])
    assert current_branch_info["lane"] == 0

    # 节点：develop 两个 commit + worldline/alt 一个 head（共享 first commit）
    node_ids = {n["id"] for n in timeline["nodes"]}
    assert first_id in node_ids
    assert len(timeline["nodes"]) >= 2

    # 边：至少包含 first->c2 这条
    assert any(e["from"] == first_id for e in timeline["edges"])

    # 布局坐标已分配
    for node in timeline["nodes"]:
        assert isinstance(node["column"], int)
        assert isinstance(node["row"], int)
        assert node["column"] >= 0
        assert node["row"] >= 0

    # 当前 HEAD 节点被标记 isCurrent
    current_nodes = [n for n in timeline["nodes"] if n["isCurrent"]]
    assert len(current_nodes) == 1
    assert current_nodes[0]["id"] == timeline["currentHead"]["id"]

    # column=0 是最旧的提交（树从左向右生长），并且当前 HEAD 一定在最右端。
    columns = {n["subject"]: n["column"] for n in timeline["nodes"]}
    assert columns["c1"] == 0
    assert columns["c2"] == 1
    assert max(columns.values()) == columns["c2"]


def test_jump_to_commit_enters_detached_head(git_service: GitService, tmp_path: Path):
    """jump_to_commit 必须进入 detached HEAD 状态，让用户查看历史节点。"""
    workspace = tmp_path / "jump"
    workspace.mkdir()
    first_ids = _seed_timeline_workspace(git_service, workspace)
    first_id = first_ids[0]

    result = git_service.jump_to_commit(workspace, commit_id=first_id)
    assert result["detached"] is True
    assert result["commit"]["id"] == first_id

    # 当前无分支（detached HEAD）
    assert git_service._read_current_branch(workspace) == ""

    # timeline 反映 detached 状态
    timeline = git_service.read_timeline(workspace)
    assert timeline["detached"] is True
    assert timeline["currentBranch"] == ""
    assert timeline["currentHead"]["id"] == first_id


def test_jump_to_commit_idempotent_when_already_detached(git_service: GitService, tmp_path: Path):
    """重复 jump 到同一个 commit 应当幂等返回，不报错。"""
    workspace = tmp_path / "jump-idempotent"
    workspace.mkdir()
    first_ids = _seed_timeline_workspace(git_service, workspace)
    first_id = first_ids[0]

    first_jump = git_service.jump_to_commit(workspace, commit_id=first_id)
    second_jump = git_service.jump_to_commit(workspace, commit_id=first_id)
    assert first_jump["commit"]["id"] == second_jump["commit"]["id"] == first_id
    assert second_jump["detached"] is True


def test_jump_to_commit_blocked_by_dirty_worktree(git_service: GitService, tmp_path: Path):
    """工作区有未提交改动时 jump 必须拒绝（409），避免丢失改动。"""
    workspace = tmp_path / "jump-dirty"
    workspace.mkdir()
    first_ids = _seed_timeline_workspace(git_service, workspace)
    first_id = first_ids[0]

    (workspace / "chapters" / "001.md").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(GitServiceError) as exc_info:
        git_service.jump_to_commit(workspace, commit_id=first_id)
    assert exc_info.value.status_code == 409


def test_jump_to_commit_validates_arguments(git_service: GitService, tmp_path: Path):
    """jump 的参数校验：空 commit_id → 400；不存在的 commit → 抛 GitServiceError。"""
    workspace = tmp_path / "jump-validate"
    workspace.mkdir()
    (workspace / "a.md").write_text("a\n", encoding="utf-8")
    git_service.initialize_repository(workspace)
    git_service.commit_all(workspace, message="seed")

    with pytest.raises(GitServiceError) as exc_info:
        git_service.jump_to_commit(workspace, commit_id="")
    assert exc_info.value.status_code == 400

    with pytest.raises(GitServiceError):
        git_service.jump_to_commit(workspace, commit_id="deadbeefdeadbeef")


def test_commit_in_detached_head_creates_worldline_branch(git_service: GitService, tmp_path: Path):
    """延迟分叉：detached HEAD 状态下首次提交时自动创建 worldline/{timestamp} 分支。

    这是平行时空线的核心语义：用户 jump 到历史节点查看，然后做改动提交，
    系统自动为新提交建立新分支（新世界线），不污染原分支。
    """
    workspace = tmp_path / "worldline-fork"
    workspace.mkdir()
    first_ids = _seed_timeline_workspace(git_service, workspace)
    first_id = first_ids[0]

    # jump 到历史节点
    git_service.jump_to_commit(workspace, commit_id=first_id)

    # 在历史节点上做改动并提交
    (workspace / "chapters" / "alt.md").write_text("alternate timeline\n", encoding="utf-8")
    result = git_service.commit_all(workspace, message="alt branch commit")

    assert result["created"] is True
    assert "worldlineBranch" in result
    new_branch = result["worldlineBranch"]
    assert new_branch.startswith("worldline/")

    # HEAD 现在指向新分支
    assert git_service._read_current_branch(workspace) == new_branch

    # 新分支的 head 就是这次提交
    summary = git_service.read_summary(workspace)
    assert summary["branch"] == new_branch
    assert summary["head"]["subject"] == "alt branch commit"

    # 新分支出现在分支列表里
    branches = git_service.list_branches(workspace)
    branch_names = [b["name"] for b in branches["branches"]]
    assert new_branch in branch_names
    assert GitService.DEFAULT_BRANCH in branch_names

    # 原 develop 分支的 head 不变（仍是 c2，未被污染）
    develop_head = git_service._run_git(workspace, ["rev-parse", GitService.DEFAULT_BRANCH]).strip()
    c2_summary = git_service.read_summary(workspace)
    # 切回 develop 验证
    git_service.switch_branch(workspace, name=GitService.DEFAULT_BRANCH)
    develop_summary = git_service.read_summary(workspace)
    assert develop_summary["head"]["subject"] == "c2"
    assert develop_head == develop_summary["head"]["id"]


def test_commit_paths_in_detached_head_creates_worldline_branch(git_service: GitService, tmp_path: Path):
    """commit_paths 在 detached HEAD 状态下同样要触发延迟分叉。"""
    workspace = tmp_path / "worldline-fork-paths"
    workspace.mkdir()
    first_ids = _seed_timeline_workspace(git_service, workspace)
    first_id = first_ids[0]

    git_service.jump_to_commit(workspace, commit_id=first_id)

    (workspace / "chapters" / "new.md").write_text("partial\n", encoding="utf-8")
    result = git_service.commit_paths(
        workspace,
        paths=["chapters/new.md"],
        message="partial commit on detached head",
    )
    assert result["created"] is True
    assert result.get("worldlineBranch", "").startswith("worldline/")
    assert git_service._read_current_branch(workspace) == result["worldlineBranch"]


def test_commit_on_branch_does_not_create_worldline(git_service: GitService, tmp_path: Path):
    """正常分支上的提交不应创建 worldline 分支，worldlineBranch 字段不存在。"""
    workspace = tmp_path / "normal-commit"
    workspace.mkdir()
    _seed_timeline_workspace(git_service, workspace)

    (workspace / "chapters" / "extra.md").write_text("extra\n", encoding="utf-8")
    result = git_service.commit_all(workspace, message="normal commit")
    assert result["created"] is True
    assert "worldlineBranch" not in result
    assert git_service._read_current_branch(workspace) == GitService.DEFAULT_BRANCH


def test_timeline_includes_worldline_branch_after_fork(git_service: GitService, tmp_path: Path):
    """延迟分叉后，timeline 必须把新世界线分支纳入树状图。"""
    workspace = tmp_path / "timeline-after-fork"
    workspace.mkdir()
    first_ids = _seed_timeline_workspace(git_service, workspace)
    first_id = first_ids[0]

    git_service.jump_to_commit(workspace, commit_id=first_id)
    (workspace / "chapters" / "alt.md").write_text("alt\n", encoding="utf-8")
    fork_result = git_service.commit_all(workspace, message="fork point")
    new_branch = fork_result["worldlineBranch"]

    timeline = git_service.read_timeline(workspace)
    branch_names = [b["name"] for b in timeline["branches"]]
    assert new_branch in branch_names
    # 新分支是当前分支，lane=0
    new_branch_info = next(b for b in timeline["branches"] if b["name"] == new_branch)
    assert new_branch_info["isCurrent"] is True
    assert new_branch_info["lane"] == 0

    # 新提交节点在 timeline 中，且 isCurrent=True
    current_nodes = [n for n in timeline["nodes"] if n["isCurrent"]]
    assert len(current_nodes) == 1
    assert current_nodes[0]["subject"] == "fork point"

    # 新分支 head 节点的 headBranches 包含新分支名
    head_node = next(n for n in timeline["nodes"] if n["id"] == current_nodes[0]["id"])
    assert new_branch in head_node["headBranches"]


# -----------------------------------------------------------------------------
# 拓扑深度布局 / 智能跳转 / 世界线管理
# -----------------------------------------------------------------------------


def _seed_forked_worldlines(git_service: GitService, workspace: Path) -> dict[str, str]:
    """建一棵有分叉的树，返回 subject -> commit id。

    develop:      c1 - c2 - c3
                   \\
    alt/dark:       a1 - a2
    """
    (workspace / "chapters").mkdir()
    git_service.initialize_repository(workspace)
    ids: dict[str, str] = {}
    for name in ("c1", "c2", "c3"):
        (workspace / "chapters" / "main.md").write_text(f"main {name}\n", encoding="utf-8")
        ids[name] = git_service.commit_all(workspace, message=name)["commit"]["id"]

    git_service.create_worldline(workspace, from_commit=ids["c1"], name="alt/dark")
    for name in ("a1", "a2"):
        (workspace / "chapters" / "alt.md").write_text(f"alt {name}\n", encoding="utf-8")
        ids[name] = git_service.commit_all(workspace, message=name)["commit"]["id"]
    return ids


def test_timeline_column_is_topological_depth_not_commit_index(
    git_service: GitService, tmp_path: Path
):
    """横轴列号必须是拓扑深度，不是全局时间序索引。

    这是重构的核心不变量：总列数取决于最长世界线的长度，而不是提交总数；
    分叉出去的第一个节点与母线上的同代节点必须落在同一列（垂直对齐）。
    """
    workspace = tmp_path / "topology"
    workspace.mkdir()
    ids = _seed_forked_worldlines(git_service, workspace)

    timeline = git_service.read_timeline(workspace)
    column = {n["subject"]: n["column"] for n in timeline["nodes"]}

    # 5 个提交，但最长的世界线只有 3 代，所以只需要 3 列。
    assert len(timeline["nodes"]) == 5
    assert max(column.values()) == 2

    # 同一条线上连续提交的列号连续递增。
    assert column["c1"] == 0
    assert column["c2"] == 1
    assert column["c3"] == 2

    # a1 是 c1 的子节点，必须和 c2 同列——这就是"分叉点垂直对齐"。
    assert column["a1"] == column["c2"] == 1
    assert column["a2"] == 2

    # 每个节点的列号 = 父节点列号 + 1
    by_id = {n["id"]: n for n in timeline["nodes"]}
    for node in timeline["nodes"]:
        parents = [p for p in node["parents"] if p in by_id]
        if parents:
            assert node["column"] == max(by_id[p]["column"] for p in parents) + 1


def test_timeline_reports_fork_column_and_exclusive_counts(
    git_service: GitService, tmp_path: Path
):
    """每条世界线要报告它从哪一列分出去、独有多少个版本。"""
    workspace = tmp_path / "forkmeta"
    workspace.mkdir()
    _seed_forked_worldlines(git_service, workspace)

    timeline = git_service.read_timeline(workspace)
    branches = {b["name"]: b for b in timeline["branches"]}

    # alt/dark 从 c1 之后分出去，它独有的最早节点 a1 在第 1 列。
    assert branches["alt/dark"]["forkColumn"] == 1
    assert branches["alt/dark"]["tipColumn"] == 2
    # a1 + a2 是它独有的；c1 与 develop 共享。
    assert branches["alt/dark"]["commitCount"] == 2
    assert branches["alt/dark"]["totalCount"] == 3

    # develop 独有 c2、c3。
    assert branches[GitService.DEFAULT_BRANCH]["commitCount"] == 2
    assert branches[GitService.DEFAULT_BRANCH]["totalCount"] == 3

    # 当前世界线固定 lane 0。
    assert branches["alt/dark"]["isCurrent"] is True
    assert branches["alt/dark"]["lane"] == 0


def test_timeline_nodes_carry_lane_branch(git_service: GitService, tmp_path: Path):
    """节点要说明自己被画在哪条世界线的轨道上，前端据此做同线高亮。"""
    workspace = tmp_path / "lanebranch"
    workspace.mkdir()
    _seed_forked_worldlines(git_service, workspace)

    timeline = git_service.read_timeline(workspace)
    lane_branch = {n["subject"]: n["laneBranch"] for n in timeline["nodes"]}
    rows = {n["subject"]: n["row"] for n in timeline["nodes"]}

    # 当前线是 alt/dark（lane 0），公共前史 c1 画在它上面。
    assert lane_branch["c1"] == "alt/dark"
    assert rows["c1"] == rows["a1"] == 0
    # develop 独有的节点在自己的轨道上。
    assert lane_branch["c3"] == GitService.DEFAULT_BRANCH
    assert rows["c3"] != 0


def test_jump_to_branch_tip_switches_worldline_instead_of_detaching(
    git_service: GitService, tmp_path: Path
):
    """跳到某条世界线的最新节点 = 切到那条线，而不是掉进观测态。"""
    workspace = tmp_path / "jump-tip"
    workspace.mkdir()
    ids = _seed_forked_worldlines(git_service, workspace)

    # 当前在 alt/dark，跳到 develop 的最新节点 c3。
    result = git_service.jump_to_commit(workspace, commit_id=ids["c3"])
    assert result["detached"] is False
    assert result["branch"] == GitService.DEFAULT_BRANCH
    assert git_service._read_current_branch(workspace) == GitService.DEFAULT_BRANCH

    timeline = git_service.read_timeline(workspace)
    assert timeline["detached"] is False
    assert timeline["currentBranch"] == GitService.DEFAULT_BRANCH


def test_jump_to_middle_node_enters_observing_state(git_service: GitService, tmp_path: Path):
    """跳到线中间的历史节点仍然进入观测态（detached HEAD）。"""
    workspace = tmp_path / "jump-middle"
    workspace.mkdir()
    ids = _seed_forked_worldlines(git_service, workspace)

    result = git_service.jump_to_commit(workspace, commit_id=ids["c2"])
    assert result["detached"] is True
    assert result["branch"] == ""
    assert git_service._read_current_branch(workspace) == ""


def test_detached_summary_does_not_report_a_fake_branch_name(
    git_service: GitService, tmp_path: Path
):
    """观测态下 branch 必须是空串。

    Git 的 porcelain 输出是 `## HEAD (no branch)`，旧实现把这行字面量当成分支
    名存进 summary，面板上就会显示「当前世界线：HEAD (no branch)」。
    """
    workspace = tmp_path / "detached-branch"
    workspace.mkdir()
    ids = _seed_forked_worldlines(git_service, workspace)

    git_service.jump_to_commit(workspace, commit_id=ids["c2"])
    summary = git_service.read_summary(workspace)
    assert summary["branch"] == ""
    assert "no branch" not in str(summary["branch"])


def test_create_worldline_from_any_node(git_service: GitService, tmp_path: Path):
    """从任意节点开辟命名世界线，并立即切过去。"""
    workspace = tmp_path / "create-worldline"
    workspace.mkdir()
    ids = _seed_forked_worldlines(git_service, workspace)

    result = git_service.create_worldline(workspace, from_commit=ids["c2"], name="alt/redemption")
    assert result["worldline"] == "alt/redemption"
    assert result["fromCommit"] == ids["c2"]
    assert git_service._read_current_branch(workspace) == "alt/redemption"

    # 新线的起点就是 c2，原线不受影响。
    head = git_service._read_head_commit(workspace)
    assert head["id"] == ids["c2"]
    develop_head = git_service._run_git(
        workspace, ["rev-parse", GitService.DEFAULT_BRANCH]
    ).strip()
    assert develop_head == ids["c3"]


def test_create_worldline_rejects_duplicates_and_dirty_worktree(
    git_service: GitService, tmp_path: Path
):
    workspace = tmp_path / "create-worldline-guard"
    workspace.mkdir()
    ids = _seed_forked_worldlines(git_service, workspace)

    with pytest.raises(GitServiceError) as exc_info:
        git_service.create_worldline(workspace, from_commit=ids["c1"], name="alt/dark")
    assert exc_info.value.status_code == 409

    with pytest.raises(GitServiceError) as exc_info:
        git_service.create_worldline(workspace, from_commit=ids["c1"], name="bad name!")
    assert exc_info.value.status_code == 400

    (workspace / "chapters" / "dirty.md").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(GitServiceError) as exc_info:
        git_service.create_worldline(workspace, from_commit=ids["c1"], name="alt/another")
    assert exc_info.value.status_code == 409


def test_rename_worldline(git_service: GitService, tmp_path: Path):
    workspace = tmp_path / "rename-worldline"
    workspace.mkdir()
    _seed_forked_worldlines(git_service, workspace)

    result = git_service.rename_worldline(workspace, name="alt/dark", new_name="alt/dark-ending")
    assert result["renamedTo"] == "alt/dark-ending"
    names = [b["name"] for b in git_service.list_branches(workspace)["branches"]]
    assert "alt/dark-ending" in names
    assert "alt/dark" not in names

    with pytest.raises(GitServiceError) as exc_info:
        git_service.rename_worldline(workspace, name="does/not-exist", new_name="whatever")
    assert exc_info.value.status_code == 404

    with pytest.raises(GitServiceError) as exc_info:
        git_service.rename_worldline(
            workspace, name="alt/dark-ending", new_name=GitService.DEFAULT_BRANCH
        )
    assert exc_info.value.status_code == 409


def test_delete_worldline_reports_lost_versions(git_service: GitService, tmp_path: Path):
    """删除世界线要报告丢掉了多少个独有版本，并拒绝删当前线。"""
    workspace = tmp_path / "delete-worldline"
    workspace.mkdir()
    _seed_forked_worldlines(git_service, workspace)

    # 当前在 alt/dark 上，不能删自己。
    with pytest.raises(GitServiceError) as exc_info:
        git_service.delete_worldline(workspace, name="alt/dark")
    assert exc_info.value.status_code == 409

    git_service.switch_branch(workspace, name=GitService.DEFAULT_BRANCH)
    result = git_service.delete_worldline(workspace, name="alt/dark")
    # a1 + a2 是 alt/dark 独有的两个版本。
    assert result["exclusiveCommits"] == 2
    assert result["deleted"] == "alt/dark"
    names = [b["name"] for b in result["branches"]]
    assert "alt/dark" not in names

    # 只剩一条线时不允许再删。
    with pytest.raises(GitServiceError) as exc_info:
        git_service.delete_worldline(workspace, name=GitService.DEFAULT_BRANCH)
    assert exc_info.value.status_code == 409


def test_delete_worldline_rejects_unknown_name(git_service: GitService, tmp_path: Path):
    workspace = tmp_path / "delete-unknown"
    workspace.mkdir()
    _seed_forked_worldlines(git_service, workspace)

    with pytest.raises(GitServiceError) as exc_info:
        git_service.delete_worldline(workspace, name="alt/nope")
    assert exc_info.value.status_code == 404
