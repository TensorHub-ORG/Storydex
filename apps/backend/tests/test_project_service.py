from __future__ import annotations

from pathlib import Path

import pytest

from core.exceptions import ProjectPathInvalidError, ProjectPathNotFoundError
from services.project_service import ProjectService


@pytest.fixture
def project_service(monkeypatch: pytest.MonkeyPatch) -> ProjectService:
    service = ProjectService()
    monkeypatch.setattr(service.global_config, "record_recent_project", lambda **kwargs: None)
    return service


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
