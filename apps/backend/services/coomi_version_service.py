from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path
from typing import Any

_COOMI_REQUIREMENT = re.compile(
    r"^\s*coomi-agent\s*==\s*([A-Za-z0-9_.+!-]+)\s*(?:#.*)?$",
    re.IGNORECASE | re.MULTILINE,
)
_legacy_supported_version: str | None = None


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def packaged_requirements_path() -> Path:
    return Path(__file__).resolve().parents[1] / "requirements-runtime.txt"


def read_expected_coomi_version(requirements_path: Path | None = None) -> str:
    repository_requirements = repository_root() / "requirements.txt"
    if requirements_path is not None:
        path = Path(requirements_path)
    elif repository_requirements.is_file():
        path = repository_requirements
    elif packaged_requirements_path().is_file():
        path = packaged_requirements_path()
    else:
        path = repository_requirements
    if not path.is_file():
        if requirements_path is None and _legacy_supported_version:
            return _legacy_supported_version
        raise FileNotFoundError(path)
    matches = _COOMI_REQUIREMENT.findall(path.read_text(encoding="utf-8-sig"))
    if len(matches) != 1:
        raise RuntimeError(f"requirements.txt must pin coomi-agent with == exactly once: {path}")
    return matches[0]


def __getattr__(name: str) -> Any:
    """Expose the removed version constant for legacy callers without duplicating its value."""
    if name != "SUPPORTED_COOMI_VERSION":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    global _legacy_supported_version
    if _legacy_supported_version is None:
        _legacy_supported_version = read_expected_coomi_version()
    return _legacy_supported_version


def check_coomi_version(
    *,
    requirements_path: Path | None = None,
    metadata_version: str | None = None,
    module_version: str | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        expected = read_expected_coomi_version(requirements_path)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        expected = ""
        errors.append(f"Coomi version source is invalid: {type(exc).__name__}: {exc}")
    try:
        installed = metadata_version or importlib.metadata.version("coomi-agent")
    except importlib.metadata.PackageNotFoundError:
        installed = ""
        errors.append("coomi-agent package metadata is not installed")

    if module_version is None:
        try:
            import coomi

            module_version = str(getattr(coomi, "__version__", "") or "")
        except Exception as exc:
            module_version = ""
            errors.append(f"coomi import failed: {type(exc).__name__}: {exc}")

    if expected and installed and installed != expected:
        errors.append(f"coomi-agent metadata version {installed} != expected {expected}")
    if expected and module_version != expected:
        errors.append(f"coomi.__version__ {module_version or '<missing>'} != expected {expected}")

    return {
        "ok": not errors,
        "expected": expected,
        "metadataVersion": installed,
        "moduleVersion": module_version,
        "warnings": errors,
    }
