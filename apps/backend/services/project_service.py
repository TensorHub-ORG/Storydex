from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Union

from core.config import get_settings
from core.exceptions import ProjectPathInvalidError, ProjectPathNotFoundError
from services.global_config_service import get_global_config_service
from services.story_project_service import get_story_project_service
from services.storydex_manifest import ensure_manifest as ensure_storydex_manifest
from services.storydex_manifest import manifest_paths


class ProjectService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.global_config = get_global_config_service()
        self.story_project_service = get_story_project_service()
        self._lock = Lock()
        self._default_workspace_root = self.settings.workspace_root.resolve()
        self._current_workspace_root = self._load_initial_workspace_root()
        self._opened_at = datetime.now(timezone.utc).isoformat()

        if self._current_workspace_root == self._default_workspace_root:
            self.ensure_project_structure(self._current_workspace_root)

    @property
    def workspace_root(self) -> Path:
        with self._lock:
            return self._current_workspace_root

    @property
    def storydex_root(self) -> Path:
        inspection = self.inspect_project(self.workspace_root)
        return Path(inspection["storydexRoot"])

    @property
    def agent_root(self) -> Path:
        return self.story_project_service.agent_root(self.workspace_root)

    def current_project(self) -> Dict[str, Any]:
        return self.describe_project(self.workspace_root, opened_at=self._opened_at)

    def create_project(self, project_path: str, architecture: str = "standard") -> Dict[str, Any]:
        target = self._normalize_path(project_path, must_exist=False)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProjectPathInvalidError(
                "Unable to create project directory.",
                details={"projectPath": target.as_posix(), "reason": str(exc)},
            ) from exc
        if architecture == "free":
            self.ensure_free_project_structure(target)
        else:
            self.ensure_project_structure(target)
        self._set_current_workspace_root(target)
        return self.current_project()

    def open_project(self, project_path: str) -> Dict[str, Any]:
        target = self._normalize_path(project_path, must_exist=True)
        self._set_current_workspace_root(target)
        return self.current_project()

    def initialize_project(self, project_path: str = "", architecture: str = "standard") -> Dict[str, Any]:
        target = self.workspace_root if not project_path else self._normalize_path(project_path, must_exist=True)
        if architecture == "free": self.ensure_free_project_structure(target)
        else: self.ensure_project_structure(target)
        self._set_current_workspace_root(target)
        return self.current_project()

    def inspect_project(self, project_path: Union[str, Path]) -> Dict[str, Any]:
        path = Path(project_path).resolve()
        storydex_root = path / self.settings.storydex_dir_name

        free_ready = (storydex_root / ".agent").is_dir() and (storydex_root / ".cache").is_dir()

        required_directories = manifest_paths(only_create_on_init=True, directories_only=True)
        missing_directories = [
            relative_dir
            for relative_dir in required_directories
            if not (path / relative_dir).exists()
        ]
        architecture = "standard" if not missing_directories else "free" if free_ready else "unconfigured"
        has_storydex_config = architecture != "unconfigured"
        if architecture == "free": missing_directories = []
        project_name = path.name or path.as_posix()

        return {
            "projectName": project_name,
            "workspaceRoot": path.as_posix(),
            "storydexRoot": storydex_root.as_posix(),
            "storydexDirName": storydex_root.name,
            "hasStorydexConfig": has_storydex_config,
            "requiresInitialization": not has_storydex_config,
            "missingDirectories": missing_directories,
            "projectState": "ready" if has_storydex_config else "needs_init",
            "architecture": architecture,
            "openedAt": datetime.now(timezone.utc).isoformat(),
        }

    def describe_project(self, project_path: Union[str, Path], *, opened_at: str = "") -> Dict[str, Any]:
        inspection = self.inspect_project(project_path)
        inspection["openedAt"] = opened_at or inspection["openedAt"]
        return inspection

    def ensure_project_structure(self, project_path: Union[str, Path]) -> None:
        root = Path(project_path).resolve()
        # WP-0.3: 目录骨架统一委托给 .storydex Manifest（services/storydex_manifest.py）。
        # 旧 v1 目录暂时保留，不再在此显式创建；保留行为由 StoryProjectService
        # 内部的兼容 reader 负责，由 WP-5.1 完成最终迁移。
        ensure_storydex_manifest(root)

        storydex_root = root / self.settings.storydex_dir_name
        self.story_project_service.ensure_project_structure(root)

        starter_files = {
            storydex_root / "memory" / "MEMORY.md": (
                "# Storydex Memory\n\n"
                "- 在这里记录项目级长期约束、禁忌、设定规则与常驻提醒。\n"
            ),
            storydex_root / "README.md": (
                "# Storydex Workspace\n\n"
                "- 这里保存 Storydex 项目规则、内置 skills、记忆、执行记录和运行日志。\n"
                "- Coomi 负责 Agent Runtime；Storydex 专用 Coomi 配置保存在用户级 `.storydex/.coomi` 目录。\n"
            ),
            storydex_root / "worldbook" / "README.md": "# 世界书\n\n在这里维护世界规则、地点、势力与历史资料。\n",
            storydex_root / "characters" / "README.md": "# 角色库\n\n在这里维护角色设定、状态与关系。\n",
            root / "chapters" / "README.md": "# 正文章节\n\n本目录用于存放章节与正文片段。\n",
        }
        for file_path, content in starter_files.items():
            if not file_path.exists():
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content, encoding="utf-8")

        self._ensure_project_manifest(root)

    def ensure_free_project_structure(self, project_path: Union[str, Path]) -> None:
        root = Path(project_path).resolve()
        (root / self.settings.storydex_dir_name / ".agent").mkdir(parents=True, exist_ok=True)
        (root / self.settings.storydex_dir_name / ".cache").mkdir(parents=True, exist_ok=True)
        ignore_path = root / ".gitignore"
        lines = ignore_path.read_text(encoding="utf-8").splitlines() if ignore_path.exists() else []
        for item in (".storydex/.agent/", ".storydex/.cache/"):
            if item not in {line.strip() for line in lines}: lines.append(item)
        ignore_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")

    def _set_current_workspace_root(self, workspace_root: Path) -> None:
        normalized = workspace_root.resolve()
        with self._lock:
            self._current_workspace_root = normalized
            self._opened_at = datetime.now(timezone.utc).isoformat()
            self._persist_state(normalized)

    def _load_initial_workspace_root(self) -> Path:
        forced_root = str(os.environ.get("STORYDEX_FORCE_WORKSPACE_ROOT") or "").strip()
        if forced_root:
            return Path(forced_root).expanduser().resolve()

        restore_last_workspace = str(os.environ.get("STORYDEX_RESTORE_LAST_WORKSPACE") or "").strip().lower()
        if restore_last_workspace in {"1", "true", "yes", "on"}:
            try:
                workspace_state = self.global_config.read_workspace_state()
            except OSError:
                workspace_state = {}
            last_project_path = (
                str(workspace_state.get("lastProjectPath") or "").strip()
                if isinstance(workspace_state, dict)
                else ""
            )
            if last_project_path:
                try:
                    candidate = Path(last_project_path).expanduser()
                    if candidate.exists() and candidate.is_dir():
                        return candidate.resolve()
                except (OSError, RuntimeError):
                    pass
        return self._default_workspace_root

    def _persist_state(self, workspace_root: Path) -> None:
        self.global_config.record_recent_project(
            project_name=workspace_root.name or workspace_root.as_posix(),
            workspace_root=workspace_root.as_posix(),
            opened_at=datetime.now(timezone.utc).isoformat(),
        )

    def _ensure_project_manifest(self, workspace_root: Path) -> None:
        manifest_path = workspace_root / self.settings.storydex_dir_name / "project.json"
        now_iso = datetime.now(timezone.utc).isoformat()
        payload: Dict[str, Any] = {}
        if manifest_path.exists():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                loaded = {}
            if isinstance(loaded, dict):
                payload = loaded

        payload.setdefault("name", workspace_root.name or "Storydex Project")
        payload.setdefault("created_at", now_iso)
        payload["storydex_version"] = "2026.04"

        story_settings = payload.get("storySettings") if isinstance(payload.get("storySettings"), dict) else {}
        fragment_format = str(story_settings.get("fragmentFormat") or "md").strip().lower().lstrip(".")
        if fragment_format not in {"md", "txt"}:
            fragment_format = "md"
        story_settings["fragmentFormat"] = fragment_format
        story_settings.setdefault("updatedAt", now_iso)
        payload["storySettings"] = story_settings

        manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _normalize_path(self, project_path: str, *, must_exist: bool) -> Path:
        raw_path = str(project_path or "").strip()
        if not raw_path:
            raise ProjectPathInvalidError("Project path is required.")

        path = Path(raw_path).expanduser()
        if must_exist and not path.exists():
            raise ProjectPathNotFoundError(
                "Project path does not exist.",
                details={"projectPath": raw_path},
            )
        if must_exist and not path.is_dir():
            raise ProjectPathInvalidError(
                "Project path must be a directory.",
                details={"projectPath": raw_path},
            )

        if not must_exist:
            if path.exists() and not path.is_dir():
                raise ProjectPathInvalidError(
                    "Project path must be a directory.",
                    details={"projectPath": raw_path},
                )

        return path.resolve()


@lru_cache(maxsize=1)
def get_project_service() -> ProjectService:
    return ProjectService()
