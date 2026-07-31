from __future__ import annotations

import subprocess
import os
import re
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache, wraps
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, TypeVar

from core.exceptions import GitServiceError

_WORKTREE_LOCKS: Dict[str, threading.RLock] = {}
_WORKTREE_LOCKS_GUARD = threading.Lock()

_T = TypeVar("_T")


def _worktree_lock(workspace_root: Path) -> threading.RLock:
    """Return the process-wide re-entrant lock guarding one worktree.

    A Git worktree has exactly one index, so `add`/`commit` pairs from
    different threads corrupt each other: the second `add -A` stages the
    first caller's files, and whichever `commit` runs first sweeps changes it
    never intended to record. Serializing per worktree keeps every public
    operation atomic. The lock is re-entrant because the mutating helpers
    call each other (`commit_all` -> `initialize_repository` -> `read_summary`).
    """
    key = os.path.normcase(os.path.realpath(os.fspath(workspace_root)))
    with _WORKTREE_LOCKS_GUARD:
        lock = _WORKTREE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _WORKTREE_LOCKS[key] = lock
        return lock


def _serialized(method: Callable[..., _T]) -> Callable[..., _T]:
    """Serialize a GitService method on the worktree it operates on."""

    @wraps(method)
    def wrapper(self: "GitService", workspace_root: Path, *args: Any, **kwargs: Any) -> _T:
        with _worktree_lock(Path(workspace_root)):
            return method(self, workspace_root, *args, **kwargs)

    return wrapper


class GitService:
    """Local-only Git workflow helpers for Storydex workspaces."""

    DEFAULT_BRANCH = "develop"
    DEFAULT_AUTHOR_NAME = "Storydex Local"
    DEFAULT_AUTHOR_EMAIL = "storydex@local"
    HISTORY_LIMIT = 24
    SAFE_GITIGNORE_LINES = [".storydex/.agent/", ".storydex/.cache/"]
    AGENT_RUNTIME_PREFIX = ".storydex/.agent/"

    @_serialized
    def restore_to_commit(
        self,
        workspace_root: Path,
        *,
        commit_id: str,
        create_backup: bool = True,
    ) -> Dict[str, Any]:
        root = self._resolve_workspace_root(workspace_root)
        self.initialize_repository(root)
        normalized_commit = str(commit_id or "").strip()
        if not normalized_commit:
            raise GitServiceError("Target commit id is required for restore.")
        if not self._has_head_commit(root):
            raise GitServiceError("Repository has no commits yet, so there is nothing to restore.")

        target_commit = self._read_commit(root, normalized_commit)
        current_head = self._read_head_commit(root)
        if current_head is None:
            raise GitServiceError("Repository head could not be resolved.")

        backup_commit = None
        if create_backup and not self._is_worktree_clean(root):
            backup_result = self.commit_all(
                root,
                message=f"workspace: backup before restore to {target_commit.get('shortId') or normalized_commit[:8]}",
            )
            backup_commit = backup_result.get("commit") if isinstance(backup_result.get("commit"), dict) else None
            current_head = self._read_head_commit(root) or current_head

        if current_head.get("id") == target_commit.get("id") and self._is_worktree_clean(root):
            return {
                "restored": False,
                "restoredCommit": current_head,
                "backupCommit": backup_commit,
                "backupRef": "",
                "summary": self.read_summary(root),
            }

        backup_ref = ""
        if create_backup and current_head.get("id"):
            backup_ref = self._create_backup_ref(
                root,
                current_head=str(current_head.get("id") or ""),
                target_short=str(target_commit.get("shortId") or normalized_commit[:8]),
            )

        self._run_git(root, ["reset", "--hard", str(target_commit.get("id") or normalized_commit)])
        self._run_git(root, ["clean", "-fd"])
        restored_head = self._read_head_commit(root)
        return {
            "restored": True,
            "restoredCommit": restored_head,
            "backupCommit": current_head,
            "backupRef": backup_ref,
            "summary": self.read_summary(root),
        }

    def read_summary(self, workspace_root: Path, *, history_limit: int = HISTORY_LIMIT) -> Dict[str, Any]:
        root = self._resolve_workspace_root(workspace_root)
        if not self.is_git_available():
            return {
                "available": False,
                "gitInstalled": False,
                "initialized": False,
                "branch": "",
                "clean": True,
                "changedFiles": [],
                "recentCommits": [],
                "graphLines": [],
                "defaultBranch": self.DEFAULT_BRANCH,
                "message": "Storydex bundled Git is not available.",
            }

        initialized = self.is_repository_initialized(root)
        summary: Dict[str, Any] = {
            "available": True,
            "gitInstalled": True,
            "initialized": initialized,
            "branch": self.DEFAULT_BRANCH if not initialized else "",
            "clean": True,
            "changedFiles": [],
            "recentCommits": [],
            "graphLines": [],
            "defaultBranch": self.DEFAULT_BRANCH,
            "message": "",
        }
        if not initialized:
            summary["message"] = "Local repository is not initialized yet."
            return summary

        status_output = self._run_git(
            root,
            ["-c", "core.quotePath=false", "status", "--porcelain=v1", "--branch", "-uall"],
        )
        branch, changed_files = self._parse_status(status_output)
        commits = self._read_recent_commits(root, limit=history_limit)
        graph_lines = self._read_graph_lines(root, limit=min(history_limit, 16))
        # HEAD must come from HEAD, not from the newest entry in the history
        # list: history is read with `--all`, so a backup branch or a restored
        # older HEAD makes commits[0] a different commit than the one actually
        # checked out. That mismatch is what made the panel mark the wrong row
        # as "current" and show a stale "latest commit".
        head = self._read_head_commit(root)

        summary.update(
            {
                "branch": branch or self._read_current_branch(root),
                "clean": len(changed_files) == 0,
                "changedFiles": changed_files,
                "recentCommits": commits,
                "graphLines": graph_lines,
                "head": head,
                # Monotonic-ish stamp so the client can drop a stale response
                # that lands after a newer one (see stores/git.ts).
                "generatedAt": time.time(),
            }
        )
        return summary

    def read_diff(
        self,
        workspace_root: Path,
        *,
        paths: Optional[Iterable[str]] = None,
        context_lines: int = 3,
    ) -> Dict[str, Any]:
        root = self._resolve_workspace_root(workspace_root)
        if not self.is_git_available():
            return {
                "available": False,
                "gitInstalled": False,
                "initialized": False,
                "branch": "",
                "files": [],
                "totals": {"files": 0, "added": 0, "removed": 0},
                "message": "Storydex bundled Git is not available.",
            }

        initialized = self.is_repository_initialized(root)
        if not initialized:
            return {
                "available": True,
                "gitInstalled": True,
                "initialized": False,
                "branch": self.DEFAULT_BRANCH,
                "files": [],
                "totals": {"files": 0, "added": 0, "removed": 0},
                "message": "Local repository is not initialized yet.",
            }

        selected_paths = set(self._normalize_paths(paths or []))
        summary = self.read_summary(root)
        changed_files = [
            item for item in summary.get("changedFiles", [])
            if isinstance(item, dict)
            and (not selected_paths or str(item.get("relativePath") or "").replace("\\", "/") in selected_paths)
        ]
        has_head = self._has_head_commit(root)
        diff_files: List[Dict[str, Any]] = []
        for item in changed_files:
            relative_path = str(item.get("relativePath") or "").replace("\\", "/").strip()
            status = str(item.get("status") or "").strip() or "M"
            if not relative_path:
                continue
            if status == "??" or not has_head:
                diff_file = self._build_untracked_diff(root, relative_path, status=status)
            else:
                output = self._run_git(
                    root,
                    [
                        "diff",
                        f"--unified={max(0, int(context_lines or 3))}",
                        "--no-ext-diff",
                        "--no-color",
                        "HEAD",
                        "--",
                        relative_path,
                    ],
                    check=False,
                )
                diff_file = self._parse_unified_diff_file(output, relative_path=relative_path, status=status)
            diff_files.append(diff_file)

        totals = {
            "files": len(diff_files),
            "added": sum(int(item.get("added") or 0) for item in diff_files),
            "removed": sum(int(item.get("removed") or 0) for item in diff_files),
        }
        return {
            "available": True,
            "gitInstalled": True,
            "initialized": True,
            "branch": str(summary.get("branch") or self._read_current_branch(root)),
            "files": diff_files,
            "totals": totals,
            "message": "",
        }

    def read_commit_diff(
        self,
        workspace_root: Path,
        *,
        commit_id: str,
        paths: Optional[Iterable[str]] = None,
        context_lines: int = 3,
    ) -> Dict[str, Any]:
        root = self._resolve_workspace_root(workspace_root)
        if not self.is_git_available():
            return {
                "available": False,
                "gitInstalled": False,
                "initialized": False,
                "branch": "",
                "files": [],
                "totals": {"files": 0, "added": 0, "removed": 0},
                "message": "Storydex bundled Git is not available.",
            }

        if not self.is_repository_initialized(root):
            return {
                "available": True,
                "gitInstalled": True,
                "initialized": False,
                "branch": self.DEFAULT_BRANCH,
                "files": [],
                "totals": {"files": 0, "added": 0, "removed": 0},
                "message": "Local repository is not initialized yet.",
            }

        normalized_commit = str(commit_id or "").strip()
        if not normalized_commit:
            raise GitServiceError("Commit id is required for commit diff.")
        commit = self._read_commit(root, normalized_commit)
        selected_paths = set(self._normalize_paths(paths or []))
        changed_files = self._read_commit_changed_files(root, str(commit.get("id") or normalized_commit))
        diff_files: List[Dict[str, Any]] = []
        for item in changed_files:
            relative_path = str(item.get("relativePath") or "").replace("\\", "/").strip()
            if not relative_path:
                continue
            if selected_paths and relative_path not in selected_paths:
                continue
            status = str(item.get("status") or "").strip() or "M"
            output = self._run_git(
                root,
                [
                    "-c",
                    "core.quotePath=false",
                    "show",
                    "--format=",
                    f"--unified={max(0, int(context_lines or 3))}",
                    "--no-ext-diff",
                    "--no-color",
                    "--find-renames",
                    str(commit.get("id") or normalized_commit),
                    "--",
                    relative_path,
                ],
                check=False,
            )
            diff_files.append(self._parse_unified_diff_file(output, relative_path=relative_path, status=status))

        totals = {
            "files": len(diff_files),
            "added": sum(int(item.get("added") or 0) for item in diff_files),
            "removed": sum(int(item.get("removed") or 0) for item in diff_files),
        }
        summary = self.read_summary(root)
        return {
            "available": True,
            "gitInstalled": True,
            "initialized": True,
            "branch": str(summary.get("branch") or self._read_current_branch(root)),
            "files": diff_files,
            "totals": totals,
            "message": "",
        }

    def build_file_snapshot_diff(
        self,
        workspace_root: Path,
        *,
        paths: Iterable[str],
        status: str = "A",
    ) -> Dict[str, Any]:
        root = self._resolve_workspace_root(workspace_root)
        initialized = self.is_repository_initialized(root)
        diff_files = [
            self._build_untracked_diff(root, relative_path, status=status)
            for relative_path in self._normalize_paths(paths)
        ]
        diff_files = [item for item in diff_files if str(item.get("relativePath") or "").strip()]
        totals = {
            "files": len(diff_files),
            "added": sum(int(item.get("added") or 0) for item in diff_files),
            "removed": sum(int(item.get("removed") or 0) for item in diff_files),
        }
        return {
            "available": True,
            "gitInstalled": self.is_git_available(),
            "initialized": initialized,
            "branch": self._read_current_branch(root) if initialized else "",
            "files": diff_files,
            "totals": totals,
            "message": "",
        }

    @_serialized
    def initialize_repository(self, workspace_root: Path) -> Dict[str, Any]:
        root = self._resolve_workspace_root(workspace_root)
        self._ensure_git_installed()
        if not self.is_repository_initialized(root):
            self._run_git(root, ["init"])
            self._ensure_branch_name(root)
        self._ensure_local_identity(root)
        self._ensure_gitignore(root)
        self._ensure_agent_runtime_untracked(root)
        return self.read_summary(root)

    @_serialized
    def list_branches(self, workspace_root: Path) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        self.initialize_repository(root)
        current = self._read_current_branch(root)
        output = self._run_git(root, ["for-each-ref", "--format=%(refname:short)", "refs/heads/"])
        names = sorted({line.strip() for line in output.splitlines() if line.strip()})
        if current and current not in names:
            names.append(current)
        return {"current": current, "branches": [{"name": name, "current": name == current} for name in names]}

    @_serialized
    def create_branch(self, workspace_root: Path, *, name: str, checkout: bool = True) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        self.initialize_repository(root)
        branch = self._validate_branch_name(name)
        if branch == self._read_current_branch(root) or self._branch_exists(root, branch):
            raise GitServiceError("Branch already exists.", details={"branch": branch})
        if not self._has_head_commit(root):
            if checkout:
                # An unborn repository has no commit for `git branch` to copy.
                # Point HEAD at the requested branch so the first commit lands there.
                self._run_git(root, ["symbolic-ref", "HEAD", f"refs/heads/{branch}"])
            else:
                self._run_git(root, ["branch", branch], check=False)
        else:
            self._run_git(root, ["branch", branch])
        if checkout and self._has_head_commit(root):
            self._run_git(root, ["switch", branch])
        return {**self.list_branches(root), "summary": self.read_summary(root)}

    @_serialized
    def switch_branch(self, workspace_root: Path, *, name: str) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        self.initialize_repository(root)
        branch = self._validate_branch_name(name)
        current = self._read_current_branch(root)
        if branch == current:
            return {**self.list_branches(root), "summary": self.read_summary(root)}
        if not self._branch_exists(root, branch):
            raise GitServiceError("Branch does not exist.", details={"branch": branch})
        if not self._is_worktree_clean(root):
            raise GitServiceError(
                "Cannot switch branches with uncommitted changes.",
                details={"branch": branch, "hint": "Commit or discard changes first."},
            )
        self._run_git(root, ["switch", branch])
        return {**self.list_branches(root), "summary": self.read_summary(root)}

    @staticmethod
    def _validate_branch_name(name: str) -> str:
        branch = str(name or "").strip()
        if not branch or len(branch) > 120 or branch.startswith("-") or ".." in branch or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
            raise GitServiceError("Invalid branch name.", details={"branch": branch})
        return branch

    @_serialized
    def commit_all(self, workspace_root: Path, *, message: str = "") -> Dict[str, Any]:
        root = self._resolve_workspace_root(workspace_root)
        self.initialize_repository(root)
        self._run_git(root, ["add", "-A"])
        if self._is_worktree_clean(root):
            return {
                "created": False,
                "commit": None,
                "summary": self.read_summary(root),
            }

        final_message = self._normalize_commit_message(
            message,
            fallback_prefix="workspace: snapshot",
        )
        self._run_git(root, ["commit", "--no-gpg-sign", "-m", final_message])
        return {
            "created": True,
            "commit": self._read_head_commit(root),
            "summary": self.read_summary(root),
        }

    @_serialized
    def commit_paths(self, workspace_root: Path, *, paths: Iterable[str], message: str = "") -> Dict[str, Any]:
        root = self._resolve_workspace_root(workspace_root)
        normalized_paths = self._normalize_paths(paths)
        self.initialize_repository(root)
        if not normalized_paths:
            return {
                "created": False,
                "commit": None,
                "summary": self.read_summary(root),
            }
        if ".gitignore" not in normalized_paths and self._paths_have_changes(root, [".gitignore"]):
            normalized_paths.append(".gitignore")
        is_first_commit = not self._has_head_commit(root)

        if not self._paths_have_changes(root, normalized_paths):
            return {
                "created": False,
                "commit": None,
                "summary": self.read_summary(root),
            }

        self._run_git(root, ["add", "-A", "--", *normalized_paths])
        final_message = self._normalize_commit_message(
            message,
            fallback_prefix="story: local snapshot",
        )
        if is_first_commit:
            self._run_git(root, ["add", "-A"])
            self._run_git(root, ["commit", "--no-gpg-sign", "-m", final_message])
        else:
            self._run_git(root, ["commit", "--no-gpg-sign", "--only", "-m", final_message, "--", *normalized_paths])
        return {
            "created": True,
            "commit": self._read_head_commit(root),
            "summary": self.read_summary(root),
        }

    def is_repository_initialized(self, workspace_root: Path) -> bool:
        root = self._resolve_workspace_root(workspace_root)
        if not self.is_git_available():
            return False
        repository_root = self._repository_top_level(root)
        if repository_root is None:
            return False
        if not self._paths_refer_to_same_location(root, repository_root):
            self._raise_repository_boundary_error(root, repository_root)
        return True

    def _ensure_git_installed(self) -> None:
        if self.is_git_available():
            return
        raise GitServiceError(
            "Storydex bundled Git is not available.",
            details={"hint": "Storydex requires its bundled MinGit runtime for local version control."},
        )

    def is_git_available(self) -> bool:
        return bool(self._resolve_git_executable())

    def _ensure_branch_name(self, workspace_root: Path) -> None:
        branch = self._read_current_branch(workspace_root)
        if branch == self.DEFAULT_BRANCH:
            return
        # A freshly initialized repository inherits the user's global
        # init.defaultBranch. Storydex repositories must remain deterministic,
        # while existing repositories with commits keep their chosen branch.
        if branch and self._has_head_commit(workspace_root):
            return
        self._run_git(workspace_root, ["symbolic-ref", "HEAD", f"refs/heads/{self.DEFAULT_BRANCH}"], check=False)

    def _ensure_local_identity(self, workspace_root: Path) -> None:
        name = self._run_git(workspace_root, ["config", "--get", "user.name"], check=False).strip()
        email = self._run_git(workspace_root, ["config", "--get", "user.email"], check=False).strip()
        if not name:
            self._run_git(workspace_root, ["config", "user.name", self.DEFAULT_AUTHOR_NAME])
        if not email:
            self._run_git(workspace_root, ["config", "user.email", self.DEFAULT_AUTHOR_EMAIL])

    def _ensure_gitignore(self, workspace_root: Path) -> None:
        ignore_path = self._resolve_workspace_root(workspace_root) / ".gitignore"
        existing_lines = []
        if ignore_path.exists():
            existing_lines = [line.rstrip("\n") for line in ignore_path.read_text(encoding="utf-8").splitlines()]
        seen = {line.strip() for line in existing_lines if line.strip()}
        additions = [line for line in self.SAFE_GITIGNORE_LINES if line not in seen]
        if not additions:
            return

        next_lines = list(existing_lines)
        if next_lines and next_lines[-1].strip():
            next_lines.append("")
        next_lines.extend(additions)
        ignore_path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")

    def _ensure_agent_runtime_untracked(self, workspace_root: Path) -> None:
        self._run_git(
            self._resolve_workspace_root(workspace_root),
            ["rm", "-r", "--cached", "--ignore-unmatch", self.AGENT_RUNTIME_PREFIX.rstrip("/")],
            check=False,
        )

    def _read_current_branch(self, workspace_root: Path) -> str:
        return self._run_git(self._resolve_workspace_root(workspace_root), ["branch", "--show-current"], check=False).strip()

    def _read_recent_commits(self, workspace_root: Path, *, limit: int) -> List[Dict[str, Any]]:
        if not self._has_head_commit(workspace_root):
            return []
        root = self._resolve_workspace_root(workspace_root)
        window = max(1, int(limit or self.HISTORY_LIMIT))
        # History is read with `--all` so commits parked on a restore backup
        # branch stay recoverable. Because `git log` sorts by date, those
        # commits interleave with the current branch's own history and the list
        # reads as if unrelated work appeared out of nowhere. Tagging each entry
        # with its reachability lets the panel group them explicitly instead.
        reachable = self._read_head_reachable_ids(root, limit=window * 8)
        output = self._run_git(
            root,
            [
                "log",
                "--all",
                f"-n{window}",
                "--decorate=short",
                "--date=iso-strict",
                "--pretty=format:%H%x1f%h%x1f%an%x1f%ad%x1f%D%x1f%s",
            ],
        )
        commits: List[Dict[str, Any]] = []
        for raw_line in output.splitlines():
            parts = raw_line.split("\x1f")
            if len(parts) != 6:
                continue
            commit_id, short_id, author_name, authored_at, refs, subject = parts
            normalized_id = commit_id.strip()
            commits.append(
                {
                    "id": normalized_id,
                    "shortId": short_id.strip(),
                    "authorName": author_name.strip(),
                    "authoredAt": authored_at.strip(),
                    "refs": refs.strip(),
                    "subject": subject.strip(),
                    # Empty `reachable` means the lookup failed; assume the
                    # commit belongs to the current line rather than banishing
                    # the whole history into a "backup" bucket.
                    "onCurrentBranch": normalized_id in reachable if reachable else True,
                }
            )
        return commits

    def _read_head_reachable_ids(self, workspace_root: Path, *, limit: int) -> set[str]:
        output = self._run_git(
            self._resolve_workspace_root(workspace_root),
            ["rev-list", f"-n{max(1, int(limit or 200))}", "HEAD"],
            check=False,
        )
        return {line.strip() for line in output.splitlines() if line.strip()}

    def _read_graph_lines(self, workspace_root: Path, *, limit: int) -> List[str]:
        if not self._has_head_commit(workspace_root):
            return []
        output = self._run_git(
            self._resolve_workspace_root(workspace_root),
            ["log", "--graph", "--decorate", "--oneline", "--all", f"-n{max(1, int(limit or 12))}"],
            check=False,
        )
        return [line.rstrip() for line in output.splitlines() if line.strip()]

    def _read_commit_changed_files(self, workspace_root: Path, commit_id: str) -> List[Dict[str, Any]]:
        output = self._run_git(
            self._resolve_workspace_root(workspace_root),
            [
                "-c",
                "core.quotePath=false",
                "diff-tree",
                "--no-commit-id",
                "--name-status",
                "-r",
                "--root",
                "-M",
                str(commit_id),
            ],
        )
        items: List[Dict[str, Any]] = []
        for raw_line in output.splitlines():
            parts = raw_line.split("\t")
            if len(parts) < 2:
                continue
            raw_status = parts[0].strip()
            if raw_status.startswith("R") and len(parts) >= 3:
                relative_path = parts[2]
                status = "R"
            else:
                relative_path = parts[-1]
                status = raw_status[:1] or "M"
            normalized_path = self._normalize_status_path(relative_path).replace("\\", "/").strip()
            if not normalized_path:
                continue
            items.append({"status": status, "relativePath": normalized_path})
        return items

    def _read_head_commit(self, workspace_root: Path) -> Dict[str, Any] | None:
        if not self._has_head_commit(workspace_root):
            return None
        return self._read_commit(workspace_root, "HEAD")

    def _read_commit(self, workspace_root: Path, commit_id: str) -> Dict[str, Any]:
        output = self._run_git(
            self._resolve_workspace_root(workspace_root),
            [
                "show",
                "-s",
                "--date=iso-strict",
                "--pretty=format:%H%x1f%h%x1f%an%x1f%ad%x1f%D%x1f%s",
                "--end-of-options",
                f"{commit_id}^{{commit}}",
            ],
        )
        parts = output.split("\x1f")
        if len(parts) != 6:
            raise GitServiceError(
                "Target commit could not be resolved.",
                details={"commitId": commit_id},
            )
        return {
            "id": parts[0].strip(),
            "shortId": parts[1].strip(),
            "authorName": parts[2].strip(),
            "authoredAt": parts[3].strip(),
            "refs": parts[4].strip(),
            "subject": parts[5].strip(),
        }

    def _has_head_commit(self, workspace_root: Path) -> bool:
        result = self._run_git(self._resolve_workspace_root(workspace_root), ["rev-parse", "--verify", "HEAD"], check=False).strip()
        return bool(result)

    def _is_worktree_clean(self, workspace_root: Path) -> bool:
        status = self._run_git(
            self._resolve_workspace_root(workspace_root),
            ["-c", "core.quotePath=false", "status", "--porcelain=v1"],
            check=False,
        )
        return not status.strip()

    def _paths_have_changes(self, workspace_root: Path, paths: List[str]) -> bool:
        status = self._run_git(
            self._resolve_workspace_root(workspace_root),
            ["-c", "core.quotePath=false", "status", "--porcelain=v1", "--", *paths],
            check=False,
        )
        return bool(status.strip())

    @staticmethod
    def _normalize_paths(paths: Iterable[str]) -> List[str]:
        normalized: List[str] = []
        seen = set()
        for item in paths:
            raw_value = str(item or "").strip()
            value = raw_value.replace("\\", "/")
            if (
                "\x00" in value
                or value.startswith("/")
                or re.match(r"^[A-Za-z]:", value)
                or ".." in value.split("/")
            ):
                raise GitServiceError(
                    "Git path must stay inside the Storydex project root.",
                    details={"path": raw_value},
                )
            value = posixpath.normpath(value).strip("/")
            if value == ".":
                value = ""
            if not value or value in seen or GitService._is_agent_runtime_path(value):
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    @staticmethod
    def _normalize_commit_message(message: str, *, fallback_prefix: str) -> str:
        cleaned = str(message or "").strip()
        if cleaned:
            return cleaned
        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        return f"{fallback_prefix} {timestamp}"

    @staticmethod
    def _parse_status(output: str) -> tuple[str, List[Dict[str, Any]]]:
        branch = ""
        changed_files: List[Dict[str, Any]] = []
        for raw_line in output.splitlines():
            line = raw_line.rstrip("\n")
            if not line:
                continue
            if line.startswith("##"):
                header = line[2:].strip()
                if header.startswith("No commits yet on "):
                    branch = header.replace("No commits yet on ", "", 1).strip()
                else:
                    branch = header.split("...", 1)[0].strip()
                continue

            status_code = line[:2]
            relative_path = line[3:]
            if " -> " in relative_path:
                relative_path = relative_path.split(" -> ", 1)[1]
            relative_path = GitService._normalize_status_path(relative_path)
            changed_files.append(
                {
                    "status": status_code,
                    "relativePath": relative_path.replace("\\", "/").strip(),
                    "staged": status_code[0] not in {" ", "?"},
                    "unstaged": status_code[1] != " ",
                }
            )
        return branch, [
            item
            for item in changed_files
            if not GitService._is_agent_runtime_path(str(item.get("relativePath") or ""))
        ]

    @staticmethod
    def _normalize_status_path(relative_path: str) -> str:
        normalized = str(relative_path or "").strip()
        if len(normalized) >= 2 and normalized[0] == '"' and normalized[-1] == '"':
            normalized = normalized[1:-1]
        return normalized.replace('\\"', '"')

    @classmethod
    def _is_agent_runtime_path(cls, relative_path: str) -> bool:
        normalized = str(relative_path or "").replace("\\", "/").strip().lstrip("/")
        return normalized == cls.AGENT_RUNTIME_PREFIX.rstrip("/") or normalized.startswith(cls.AGENT_RUNTIME_PREFIX)

    def _build_untracked_diff(self, workspace_root: Path, relative_path: str, *, status: str) -> Dict[str, Any]:
        normalized_path = str(relative_path or "").replace("\\", "/").strip().strip("/")
        target = self._resolve_workspace_file(workspace_root, normalized_path)
        if not target.exists() or not target.is_file():
            return {
                "relativePath": normalized_path,
                "status": status,
                "added": 0,
                "removed": 0,
                "hunks": [],
                "truncated": False,
            }

        raw = target.read_bytes()
        if b"\x00" in raw[:4096]:
            return {
                "relativePath": normalized_path,
                "status": status,
                "added": 0,
                "removed": 0,
                "hunks": [
                    {
                        "header": "Binary file not shown",
                        "oldStart": 0,
                        "oldLines": 0,
                        "newStart": 0,
                        "newLines": 0,
                        "lines": [
                            {
                                "kind": "context",
                                "oldLine": None,
                                "newLine": None,
                                "content": "Binary file changed.",
                            }
                        ],
                    }
                ],
                "truncated": False,
            }

        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        max_lines = 2000
        visible_lines = lines[:max_lines]
        diff_lines = [
            {
                "kind": "added",
                "oldLine": None,
                "newLine": index + 1,
                "content": line,
            }
            for index, line in enumerate(visible_lines)
        ]
        if not visible_lines and text:
            diff_lines.append({"kind": "added", "oldLine": None, "newLine": 1, "content": text})
        truncated = len(lines) > max_lines
        if truncated:
            diff_lines.append(
                {
                    "kind": "context",
                    "oldLine": None,
                    "newLine": None,
                    "content": f"... truncated {len(lines) - max_lines} lines",
                }
            )
        return {
            "relativePath": normalized_path,
            "status": status,
            "added": len(lines) if lines else (1 if text else 0),
            "removed": 0,
            "hunks": [
                {
                    "header": f"@@ -0,0 +1,{max(1, len(lines))} @@",
                    "oldStart": 0,
                    "oldLines": 0,
                    "newStart": 1,
                    "newLines": max(1, len(lines)),
                    "lines": diff_lines,
                }
            ],
            "truncated": truncated,
        }

    @staticmethod
    def _parse_unified_diff_file(output: str, *, relative_path: str, status: str) -> Dict[str, Any]:
        import re

        normalized_path = str(relative_path or "").replace("\\", "/").strip().strip("/")
        hunks: List[Dict[str, Any]] = []
        current_hunk: Dict[str, Any] | None = None
        old_line = 0
        new_line = 0
        added = 0
        removed = 0
        hunk_pattern = re.compile(
            r"^@@ -(?P<old_start>\d+)(?:,(?P<old_lines>\d+))? "
            r"\+(?P<new_start>\d+)(?:,(?P<new_lines>\d+))? @@(?P<label>.*)$"
        )

        def finish_hunk() -> None:
            nonlocal current_hunk
            if current_hunk is not None:
                hunks.append(current_hunk)
                current_hunk = None

        for raw_line in str(output or "").splitlines():
            match = hunk_pattern.match(raw_line)
            if match:
                finish_hunk()
                old_start = int(match.group("old_start"))
                old_line_count = int(match.group("old_lines") or "1")
                new_start = int(match.group("new_start"))
                new_line_count = int(match.group("new_lines") or "1")
                current_hunk = {
                    "header": raw_line,
                    "oldStart": old_start,
                    "oldLines": old_line_count,
                    "newStart": new_start,
                    "newLines": new_line_count,
                    "lines": [],
                }
                old_line = old_start
                new_line = new_start
                continue

            if current_hunk is None:
                continue

            if raw_line.startswith("\\ No newline"):
                current_hunk["lines"].append(
                    {
                        "kind": "context",
                        "oldLine": None,
                        "newLine": None,
                        "content": raw_line,
                    }
                )
                continue

            marker = raw_line[:1]
            content = raw_line[1:] if marker in {" ", "+", "-"} else raw_line
            if marker == "+":
                current_hunk["lines"].append(
                    {"kind": "added", "oldLine": None, "newLine": new_line, "content": content}
                )
                new_line += 1
                added += 1
            elif marker == "-":
                current_hunk["lines"].append(
                    {"kind": "removed", "oldLine": old_line, "newLine": None, "content": content}
                )
                old_line += 1
                removed += 1
            else:
                current_hunk["lines"].append(
                    {"kind": "context", "oldLine": old_line, "newLine": new_line, "content": content}
                )
                old_line += 1
                new_line += 1

        finish_hunk()
        if not hunks and output.strip():
            hunks.append(
                {
                    "header": "File changed",
                    "oldStart": 0,
                    "oldLines": 0,
                    "newStart": 0,
                    "newLines": 0,
                    "lines": [
                        {
                            "kind": "context",
                            "oldLine": None,
                            "newLine": None,
                            "content": line,
                        }
                        for line in output.splitlines()
                        if line.strip()
                    ],
                }
            )
        return {
            "relativePath": normalized_path,
            "status": status,
            "added": added,
            "removed": removed,
            "hunks": hunks,
            "truncated": False,
        }

    def _create_backup_ref(self, workspace_root: Path, *, current_head: str, target_short: str) -> str:
        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
        base_name = f"storydex-backup-{timestamp}-{target_short}"
        candidate = base_name
        index = 2
        while self._branch_exists(workspace_root, candidate):
            candidate = f"{base_name}-{index}"
            index += 1
        self._run_git(self._resolve_workspace_root(workspace_root), ["branch", candidate, current_head])
        return candidate

    def _branch_exists(self, workspace_root: Path, branch_name: str) -> bool:
        result = self._run_git(
            self._resolve_workspace_root(workspace_root),
            ["show-ref", "--verify", f"refs/heads/{branch_name}"],
            check=False,
        )
        return bool(result.strip())

    @classmethod
    def _run_git(cls, workspace_root: Path, args: List[str], *, check: bool = True) -> str:
        root = cls._resolve_workspace_root(workspace_root)
        cls._assert_repository_root(root)
        return cls._run_git_process(root, args, check=check)

    @classmethod
    def _run_git_process(cls, workspace_root: Path, args: List[str], *, check: bool = True) -> str:
        result = cls._run_git_process_result(workspace_root, args)
        if check and result.returncode != 0:
            raise GitServiceError(
                "Local Git command failed.",
                details={
                    "args": args,
                    "gitExecutable": cls._resolve_git_executable(),
                    "returncode": result.returncode,
                    "stderr": result.stderr.strip(),
                    "stdout": result.stdout.strip(),
                },
            )
        return result.stdout.strip()

    @classmethod
    def _repository_top_level(cls, workspace_root: Path) -> Path | None:
        result = cls._run_git_process_result(
            cls._resolve_workspace_root(workspace_root),
            ["rev-parse", "--path-format=absolute", "--show-toplevel"],
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return cls._canonical_path(Path(result.stdout.strip()))

    @classmethod
    def _assert_repository_root(cls, workspace_root: Path) -> Path:
        root = cls._resolve_workspace_root(workspace_root)
        repository_root = cls._repository_top_level(root)
        if repository_root is None:
            raise GitServiceError(
                "The Storydex project is not an initialized Git repository.",
                details={"projectRoot": str(root)},
            )
        if not cls._paths_refer_to_same_location(root, repository_root):
            cls._raise_repository_boundary_error(root, repository_root)
        return root

    @classmethod
    def _raise_repository_boundary_error(cls, project_root: Path, repository_root: Path) -> None:
        raise GitServiceError(
            "Refusing Git operation because the Storydex project root does not match the Git repository root.",
            details={
                "projectRoot": str(cls._canonical_path(project_root)),
                "gitTopLevel": str(cls._canonical_path(repository_root)),
                "hint": "Initialize Git inside the Storydex project directory instead of using a parent repository.",
            },
        )

    @classmethod
    def _resolve_workspace_file(cls, workspace_root: Path, relative_path: str) -> Path:
        normalized = cls._normalize_paths([relative_path])
        if len(normalized) != 1:
            raise GitServiceError(
                "Git file path is required.",
                details={"path": str(relative_path or "")},
            )
        root = cls._resolve_workspace_root(workspace_root)
        target = cls._canonical_path(root / normalized[0])
        root_key = cls._path_key(root)
        try:
            inside = os.path.commonpath([root_key, cls._path_key(target)]) == root_key
        except ValueError:
            inside = False
        if not inside:
            raise GitServiceError(
                "Git file path resolves outside the Storydex project root.",
                details={"path": normalized[0], "projectRoot": str(root)},
            )
        return target

    @classmethod
    def _resolve_workspace_root(cls, workspace_root: Path) -> Path:
        root = cls._canonical_path(Path(workspace_root).expanduser())
        if not root.exists() or not root.is_dir():
            raise GitServiceError(
                "Storydex project root does not exist or is not a directory.",
                details={"projectRoot": str(root)},
            )
        return root

    @staticmethod
    def _canonical_path(path_value: Path) -> Path:
        return Path(os.path.realpath(os.fspath(path_value)))

    @classmethod
    def _path_key(cls, path_value: Path) -> str:
        return os.path.normcase(os.path.normpath(os.fspath(cls._canonical_path(path_value))))

    @classmethod
    def _paths_refer_to_same_location(cls, left: Path, right: Path) -> bool:
        return cls._path_key(left) == cls._path_key(right)

    @classmethod
    def _run_git_process_result(cls, workspace_root: Path, args: List[str]) -> subprocess.CompletedProcess[str]:
        git_executable = cls._resolve_git_executable()
        if not git_executable:
            raise GitServiceError(
                "Storydex bundled Git is not available.",
                details={"args": args},
            )
        try:
            result = subprocess.run(
                [git_executable, *args],
                cwd=str(cls._resolve_workspace_root(workspace_root)),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise GitServiceError(
                "Local Git command failed to start.",
                details={"args": args, "gitExecutable": git_executable, "reason": str(exc)},
            ) from exc

        return result

    @staticmethod
    @lru_cache(maxsize=1)
    def _resolve_git_executable() -> str:
        configured = os.environ.get("STORYDEX_GIT_EXECUTABLE", "").strip()
        candidates: List[Path] = []
        if configured:
            candidates.append(Path(configured).expanduser())

        mingit_root = os.environ.get("STORYDEX_MINGIT_ROOT", "").strip()
        if mingit_root:
            candidates.extend(GitService._mingit_git_candidates(Path(mingit_root).expanduser()))

        current = Path(__file__).resolve()
        for parent in current.parents:
            candidates.extend(GitService._mingit_git_candidates(parent / "mingit"))
            candidates.extend(GitService._mingit_git_candidates(parent / "vendor" / "mingit"))
            candidates.extend(GitService._mingit_git_candidates(parent / "apps" / "desktop" / "vendor" / "mingit"))
            candidates.extend(GitService._mingit_git_candidates(parent / "apps" / "desktop" / "app" / "mingit"))

        seen: set[str] = set()
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            key = str(resolved).lower()
            if key in seen:
                continue
            seen.add(key)
            if resolved.exists() and resolved.is_file():
                return str(resolved)
        return ""

    @staticmethod
    def _mingit_git_candidates(root: Path) -> List[Path]:
        if os.name == "nt":
            return [
                root / "cmd" / "git.exe",
                root / "bin" / "git.exe",
                root / "mingw64" / "bin" / "git.exe",
            ]
        return [root / "bin" / "git"]


@lru_cache(maxsize=1)
def get_git_service() -> GitService:
    return GitService()
