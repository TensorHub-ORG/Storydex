from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Iterable, List

from core.exceptions import GitServiceError
from services.git_service import get_git_service


@dataclass
class AgentGitSnapshot:
    workspace_root: Path
    available: bool
    baseline_status: Dict[str, str] = field(default_factory=dict)
    baseline_fingerprints: Dict[str, str] = field(default_factory=dict)
    error_message: str = ""
    skip_reason: str = ""
    initial_commit: Dict[str, Any] | None = None


class AgentGitAutoCommitService:
    """Turn-scoped local Git checks for Storydex novel-project changes."""

    def __init__(self) -> None:
        self.git_service = get_git_service()

    def begin_turn(self, workspace_root: Path) -> AgentGitSnapshot:
        root = Path(workspace_root).resolve()
        if self._looks_like_storydex_source_repository(root):
            return AgentGitSnapshot(
                workspace_root=root,
                available=False,
                error_message=(
                    "This path is the Storydex application source repository; "
                    "Agent commit prompts only target Storydex novel projects."
                ),
                skip_reason="source_repository",
            )
        if not self.git_service.is_git_available():
            return AgentGitSnapshot(
                workspace_root=root,
                available=False,
                error_message="Storydex bundled Git is not available.",
            )

        try:
            summary = self.git_service.initialize_repository(root)
            initial_commit = self._commit_initial_snapshot_if_needed(root, summary)
            baseline = self.git_service.read_summary(root)
            baseline_status = self._status_map(baseline.get("changedFiles"))
            return AgentGitSnapshot(
                workspace_root=root,
                available=True,
                baseline_status=baseline_status,
                baseline_fingerprints=self._fingerprints_for_paths(root, baseline_status),
                initial_commit=initial_commit,
            )
        except GitServiceError as exc:
            return AgentGitSnapshot(
                workspace_root=root,
                available=False,
                error_message=str(exc),
            )

    def finish_turn(
        self,
        snapshot: AgentGitSnapshot,
        *,
        prompt: str = "",
        commit_prompt_enabled: bool = True,
    ) -> Dict[str, Any]:
        del prompt
        if not snapshot.available:
            return self._payload(
                event_type="GitAutoCommit",
                workspace_root=snapshot.workspace_root,
                created=False,
                status="warning",
                reason=snapshot.skip_reason or "git_unavailable",
                message=snapshot.error_message or "Git is unavailable, so no local version record was created.",
                initial_commit=snapshot.initial_commit,
            )

        try:
            summary = self.git_service.read_summary(snapshot.workspace_root)
            current_status = self._status_map(summary.get("changedFiles"))
            changed_paths = sorted(current_status.keys())
            if not changed_paths:
                return self._payload(
                    event_type="GitAutoCommit",
                    workspace_root=snapshot.workspace_root,
                    created=False,
                    status="info",
                    reason="no_changes",
                    message="本轮结束后没有未提交的小说项目修改。",
                    summary=summary,
                    initial_commit=snapshot.initial_commit,
                )

            added, removed = self._working_tree_totals(snapshot.workspace_root, changed_paths)
            if not commit_prompt_enabled:
                return self._payload(
                    event_type="GitAutoCommit",
                    workspace_root=snapshot.workspace_root,
                    created=False,
                    status="info",
                    reason="commit_prompt_disabled",
                    message="检测到未提交修改，但项目设置已关闭提交问询。",
                    changed_files=changed_paths,
                    added=added,
                    removed=removed,
                    diff_source="working_tree",
                    summary=summary,
                    initial_commit=snapshot.initial_commit,
                )

            return self._payload(
                event_type="GitCommitPrompt",
                workspace_root=snapshot.workspace_root,
                created=False,
                status="pending",
                reason="uncommitted_changes",
                message="检测到未提交修改，请确认是否提交。",
                changed_files=changed_paths,
                added=added,
                removed=removed,
                diff_source="working_tree",
                summary=summary,
                initial_commit=snapshot.initial_commit,
                prompt_required=True,
            )
        except GitServiceError as exc:
            return self._payload(
                event_type="GitAutoCommit",
                workspace_root=snapshot.workspace_root,
                created=False,
                status="error",
                reason="commit_check_failed",
                message=str(exc),
                initial_commit=snapshot.initial_commit,
            )

    def current_changes_payload(
        self,
        workspace_root: Path,
        *,
        event_type: str = "GitCommitResult",
        status: str = "info",
        reason: str = "user_skipped",
        message: str = "已暂不提交本地修改。",
        prompt_required: bool = False,
    ) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        if self._looks_like_storydex_source_repository(root):
            return self._payload(
                event_type=event_type,
                workspace_root=root,
                created=False,
                status="warning",
                reason="source_repository",
                message=(
                    "This path is the Storydex application source repository; "
                    "Agent commit prompts only target Storydex novel projects."
                ),
            )
        try:
            summary = self.git_service.initialize_repository(root)
            current_status = self._status_map(summary.get("changedFiles"))
            changed_paths = sorted(current_status.keys())
            if not changed_paths:
                return self._payload(
                    event_type=event_type,
                    workspace_root=root,
                    created=False,
                    status="info",
                    reason="no_changes",
                    message="没有未提交的小说项目修改。",
                    summary=summary,
                    prompt_required=prompt_required,
                )

            added, removed = self._working_tree_totals(root, changed_paths)
            return self._payload(
                event_type=event_type,
                workspace_root=root,
                created=False,
                status=status,
                reason=reason,
                message=message,
                changed_files=changed_paths,
                added=added,
                removed=removed,
                diff_source="working_tree",
                summary=summary,
                prompt_required=prompt_required,
            )
        except GitServiceError as exc:
            return self._payload(
                event_type=event_type,
                workspace_root=root,
                created=False,
                status="error",
                reason="git_unavailable",
                message=str(exc),
            )

    def commit_current_changes(self, workspace_root: Path, *, message: str) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        if self._looks_like_storydex_source_repository(root):
            return self._payload(
                event_type="GitCommitResult",
                workspace_root=root,
                created=False,
                status="warning",
                reason="source_repository",
                message=(
                    "This path is the Storydex application source repository; "
                    "Agent commit prompts only target Storydex novel projects."
                ),
            )
        try:
            summary = self.git_service.initialize_repository(root)
            current_status = self._status_map(summary.get("changedFiles"))
            changed_paths = sorted(current_status.keys())
            if not changed_paths:
                return self._payload(
                    event_type="GitCommitResult",
                    workspace_root=root,
                    created=False,
                    status="info",
                    reason="no_changes",
                    message="没有未提交的小说项目修改。",
                    summary=summary,
                )

            result = self.git_service.commit_paths(root, paths=changed_paths, message=message)
            commit = result.get("commit") if isinstance(result.get("commit"), dict) else None
            diff_payload: Dict[str, Any] = {}
            if commit and commit.get("id"):
                diff_payload = self.git_service.read_commit_diff(
                    root,
                    commit_id=str(commit.get("id")),
                    paths=changed_paths,
                )
            totals = diff_payload.get("totals") if isinstance(diff_payload.get("totals"), dict) else {}
            return self._payload(
                event_type="GitCommitResult",
                workspace_root=root,
                created=bool(result.get("created")),
                status="success" if result.get("created") else "info",
                reason="committed" if result.get("created") else "no_changes",
                message=message if result.get("created") else "没有未提交的小说项目修改。",
                changed_files=changed_paths,
                added=int(totals.get("added") or 0),
                removed=int(totals.get("removed") or 0),
                diff_source="commit" if result.get("created") else "working_tree",
                commit=commit,
                summary=result.get("summary") if isinstance(result.get("summary"), dict) else summary,
            )
        except GitServiceError as exc:
            return self._payload(
                event_type="GitCommitResult",
                workspace_root=root,
                created=False,
                status="error",
                reason="commit_failed",
                message=str(exc),
            )

    def acknowledge_skip(
        self,
        workspace_root: Path,
        *,
        changed_files: Iterable[str] | None = None,
        added: int = 0,
        removed: int = 0,
    ) -> Dict[str, Any]:
        """Acknowledge a no-write decision without rescanning the repository."""
        return self._payload(
            event_type="GitCommitResult",
            workspace_root=workspace_root,
            created=False,
            status="info",
            reason="user_skipped",
            message="已暂不提交本地修改。",
            changed_files=changed_files,
            added=added,
            removed=removed,
            diff_source="working_tree",
        )

    def _commit_initial_snapshot_if_needed(self, workspace_root: Path, summary: Dict[str, Any]) -> Dict[str, Any] | None:
        recent_commits = summary.get("recentCommits")
        changed_files = summary.get("changedFiles")
        if isinstance(recent_commits, list) and recent_commits:
            return None
        if not isinstance(changed_files, list) or not changed_files:
            return None
        result = self.git_service.commit_all(
            workspace_root,
            message="workspace: initial local snapshot",
        )
        return result.get("commit") if isinstance(result.get("commit"), dict) else None

    @staticmethod
    def _status_map(changed_files: Any) -> Dict[str, str]:
        if not isinstance(changed_files, list):
            return {}
        result: Dict[str, str] = {}
        for item in changed_files:
            if not isinstance(item, dict):
                continue
            relative_path = str(item.get("relativePath") or "").strip().replace("\\", "/")
            if not relative_path:
                continue
            result[relative_path] = str(item.get("status") or "").strip()
        return result

    @staticmethod
    def _changed_paths_since(
        baseline: Dict[str, str],
        current: Dict[str, str],
        baseline_fingerprints: Dict[str, str] | None = None,
        current_fingerprints: Dict[str, str] | None = None,
    ) -> List[str]:
        paths: List[str] = []
        baseline_fingerprints = baseline_fingerprints or {}
        current_fingerprints = current_fingerprints or {}
        for path, status in current.items():
            if baseline.get(path) != status:
                paths.append(path)
                continue
            if baseline_fingerprints.get(path) != current_fingerprints.get(path):
                paths.append(path)
        return sorted(paths)

    @staticmethod
    def _fingerprints_for_paths(workspace_root: Path, paths: Iterable[str] | Dict[str, Any]) -> Dict[str, str]:
        root = Path(workspace_root).resolve()
        if isinstance(paths, dict):
            iterable = paths.keys()
        else:
            iterable = paths
        fingerprints: Dict[str, str] = {}
        for raw_path in iterable:
            relative_path = str(raw_path or "").strip().replace("\\", "/")
            if not relative_path:
                continue
            candidate = (root / relative_path).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if not candidate.exists():
                fingerprints[relative_path] = "missing"
                continue
            if not candidate.is_file():
                fingerprints[relative_path] = "non_file"
                continue
            try:
                fingerprints[relative_path] = sha256(candidate.read_bytes()).hexdigest()
            except OSError:
                fingerprints[relative_path] = "unreadable"
        return fingerprints

    @staticmethod
    def _commit_message_for_prompt(prompt: str) -> str:
        normalized = str(prompt or "").lower()
        if any(token in normalized for token in ("wiki", "百科", "知识图谱")):
            return "agent: update wiki and knowledge graph"
        if any(token in normalized for token in ("变量", "事实", "关系")):
            return "agent: update story memory"
        if any(token in normalized for token in ("角色", "人物")):
            return "agent: update character files"
        if any(token in normalized for token in ("目录", "整理", "结构")):
            return "agent: organize project structure"
        if any(token in normalized for token in ("续写", "剧情", "故事", "章节", "片段", "story", "chapter")):
            return "agent: generate story fragments"
        return "agent: update project files"

    def _working_tree_totals(self, workspace_root: Path, changed_paths: Iterable[str]) -> tuple[int, int]:
        try:
            diff_payload = self.git_service.read_diff(workspace_root, paths=changed_paths)
        except GitServiceError:
            return 0, 0
        totals = diff_payload.get("totals") if isinstance(diff_payload.get("totals"), dict) else {}
        return int(totals.get("added") or 0), int(totals.get("removed") or 0)

    @staticmethod
    def _looks_like_storydex_source_repository(workspace_root: Path) -> bool:
        root = Path(workspace_root).resolve()
        return (
            (root / "apps" / "backend" / "services" / "story_project_service.py").is_file()
            and (root / "apps" / "frontend" / "src").is_dir()
            and (root / "pyproject.toml").is_file()
            and (root / "package.json").is_file()
        )

    @staticmethod
    def _payload(
        *,
        event_type: str,
        workspace_root: Path,
        created: bool,
        status: str,
        reason: str,
        message: str,
        changed_files: Iterable[str] | None = None,
        commit: Dict[str, Any] | None = None,
        summary: Dict[str, Any] | None = None,
        initial_commit: Dict[str, Any] | None = None,
        added: int = 0,
        removed: int = 0,
        diff_source: str = "",
        prompt_required: bool = False,
    ) -> Dict[str, Any]:
        files = [str(item).replace("\\", "/") for item in (changed_files or []) if str(item).strip()]
        return {
            "_type": event_type,
            "_version": 1,
            "target": "story_project_workspace",
            "targetLabel": "Storydex 小说项目",
            "workspaceRoot": Path(workspace_root).resolve().as_posix(),
            "created": bool(created),
            "status": status,
            "reason": reason,
            "message": message,
            "changedFileCount": len(files),
            "changedFiles": files,
            "added": max(0, int(added or 0)),
            "removed": max(0, int(removed or 0)),
            "diffSource": diff_source or ("commit" if commit else "working_tree" if files else ""),
            "commit": commit,
            "commitHash": str((commit or {}).get("id") or ""),
            "shortHash": str((commit or {}).get("shortId") or ""),
            "summary": summary or {},
            "initialCommit": initial_commit,
            "promptRequired": bool(prompt_required),
        }


_SERVICE = AgentGitAutoCommitService()


def get_agent_git_autocommit_service() -> AgentGitAutoCommitService:
    return _SERVICE
