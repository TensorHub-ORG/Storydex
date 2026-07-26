from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.exceptions import ProjectPathInvalidError, ProjectPathNotFoundError
import services.project_service as project_service_module
from services.project_service import ProjectService


@pytest.fixture
def project_service(monkeypatch: pytest.MonkeyPatch) -> ProjectService:
    service = ProjectService()
    monkeypatch.setattr(service.global_config, "record_recent_project", lambda **kwargs: None)
    return service


def test_process_restart_restores_last_opened_existing_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    default_root = tmp_path / "default"
    last_project_root = tmp_path / "last-project"
    default_root.mkdir()
    last_project_root.mkdir()
    global_config = SimpleNamespace(
        read_workspace_state=lambda: {"lastProjectPath": last_project_root.as_posix()},
        record_recent_project=lambda **kwargs: None,
    )
    ensured_roots: list[Path] = []

    monkeypatch.delenv("STORYDEX_FORCE_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("STORYDEX_RESTORE_LAST_WORKSPACE", "1")
    monkeypatch.setattr(
        project_service_module,
        "get_settings",
        lambda: SimpleNamespace(workspace_root=default_root),
    )
    monkeypatch.setattr(project_service_module, "get_global_config_service", lambda: global_config)
    monkeypatch.setattr(project_service_module, "get_story_project_service", lambda: SimpleNamespace())
    monkeypatch.setattr(
        ProjectService,
        "ensure_project_structure",
        lambda self, root: ensured_roots.append(Path(root).resolve()),
    )

    service = ProjectService()

    assert service.workspace_root == last_project_root.resolve()
    assert ensured_roots == []


def test_process_restart_ignores_missing_last_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    default_root = tmp_path / "default"
    default_root.mkdir()
    missing_project_root = tmp_path / "missing-project"
    global_config = SimpleNamespace(
        read_workspace_state=lambda: {"lastProjectPath": missing_project_root.as_posix()},
        record_recent_project=lambda **kwargs: None,
    )
    ensured_roots: list[Path] = []

    monkeypatch.delenv("STORYDEX_FORCE_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("STORYDEX_RESTORE_LAST_WORKSPACE", "1")
    monkeypatch.setattr(
        project_service_module,
        "get_settings",
        lambda: SimpleNamespace(workspace_root=default_root),
    )
    monkeypatch.setattr(project_service_module, "get_global_config_service", lambda: global_config)
    monkeypatch.setattr(project_service_module, "get_story_project_service", lambda: SimpleNamespace())
    monkeypatch.setattr(
        ProjectService,
        "ensure_project_structure",
        lambda self, root: ensured_roots.append(Path(root).resolve()),
    )

    service = ProjectService()

    assert service.workspace_root == default_root.resolve()
    assert ensured_roots == [default_root.resolve()]


def test_initial_workspace_does_not_restore_without_desktop_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    default_root = tmp_path / "explicit-startup-root"
    last_project_root = tmp_path / "last-project"
    default_root.mkdir()
    last_project_root.mkdir()
    service = ProjectService.__new__(ProjectService)
    service._default_workspace_root = default_root.resolve()
    service.global_config = SimpleNamespace(
        read_workspace_state=lambda: {"lastProjectPath": last_project_root.as_posix()}
    )

    monkeypatch.delenv("STORYDEX_FORCE_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("STORYDEX_RESTORE_LAST_WORKSPACE", raising=False)

    assert service._load_initial_workspace_root() == default_root.resolve()


def test_create_project_creates_missing_parent_directories(
    project_service: ProjectService,
    tmp_path: Path,
) -> None:
    target = tmp_path / "missing" / "nested" / "story"

    created = project_service.create_project(str(target))

    assert target.is_dir()
    assert Path(created["workspaceRoot"]) == target.resolve()
    assert (target / ".storydex" / "project.json").is_file()


def test_open_project_still_rejects_a_missing_path(
    project_service: ProjectService,
    tmp_path: Path,
) -> None:
    target = tmp_path / "missing-project"

    with pytest.raises(ProjectPathNotFoundError, match="Project path does not exist"):
        project_service.open_project(str(target))


def test_create_project_translates_mkdir_oserror_to_domain_error(
    project_service: ProjectService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = (tmp_path / "unavailable" / "story").resolve()
    original_mkdir = Path.mkdir

    def fail_target_mkdir(path: Path, *args, **kwargs) -> None:
        if path == target:
            raise OSError("drive is unavailable")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_target_mkdir)

    with pytest.raises(ProjectPathInvalidError, match="Unable to create project directory") as exc_info:
        project_service.create_project(str(target))

    assert exc_info.value.details == {
        "projectPath": target.as_posix(),
        "reason": "drive is unavailable",
    }
