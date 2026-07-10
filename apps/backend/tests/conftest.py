from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))


_ISOLATED_ROOT = Path(tempfile.mkdtemp(prefix="storydex-pytest-"))
_HOME = _ISOLATED_ROOT / "home"
_WORKSPACE = _ISOLATED_ROOT / "workspace"
_GLOBAL = _ISOLATED_ROOT / "global"
for _path in (_HOME, _WORKSPACE, _GLOBAL):
    _path.mkdir(parents=True, exist_ok=True)

# These values are established before application modules are imported. Tests must
# never discover or mutate a developer's real Storydex configuration or projects.
os.environ.update(
    {
        "HOME": str(_HOME),
        "USERPROFILE": str(_HOME),
        "STORYDEX_WORKSPACE_ROOT": str(_WORKSPACE),
        "STORYDEX_GLOBAL_ROOT": str(_GLOBAL),
        "STORYDEX_DISABLE_NETWORK": "1",
        "STORYDEX_TESTING": "1",
        "PYTHONUTF8": "1",
    }
)


@pytest.fixture(autouse=True)
def isolated_process_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    global_root = tmp_path / "global"
    for path in (home, workspace, global_root):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("STORYDEX_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("STORYDEX_GLOBAL_ROOT", str(global_root))
    monkeypatch.setenv("STORYDEX_DISABLE_NETWORK", "1")
    monkeypatch.setenv("STORYDEX_TESTING", "1")
    yield


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    shutil.rmtree(_ISOLATED_ROOT, ignore_errors=True)
