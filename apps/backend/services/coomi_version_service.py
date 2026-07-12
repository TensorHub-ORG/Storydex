from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path
from typing import Any


_COOMI_REQUIREMENT = re.compile(
    r"^\s*coomi-agent\s*==\s*([A-Za-z0-9_.+!-]+)\s*(?:#.*)?$",
    re.IGNORECASE | re.MULTILINE,
)


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def read_expected_coomi_version(requirements_path: Path | None = None) -> str:
    path = Path(requirements_path or repository_root() / "requirements.txt")
    match = _COOMI_REQUIREMENT.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise RuntimeError(f"requirements.txt must pin coomi-agent with ==: {path}")
    return match.group(1)


def check_coomi_version(
    *,
    requirements_path: Path | None = None,
    metadata_version: str | None = None,
    module_version: str | None = None,
) -> dict[str, Any]:
    expected = read_expected_coomi_version(requirements_path)
    errors: list[str] = []
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

    if installed and installed != expected:
        errors.append(f"coomi-agent metadata version {installed} != expected {expected}")
    if module_version != expected:
        errors.append(f"coomi.__version__ {module_version or '<missing>'} != expected {expected}")

    return {
        "ok": not errors,
        "expected": expected,
        "metadataVersion": installed,
        "moduleVersion": module_version,
        "warnings": errors,
    }
