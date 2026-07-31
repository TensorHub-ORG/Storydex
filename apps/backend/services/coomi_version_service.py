from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from services.coomi_bridge_client import (
    STORYDEX_COOMI_RUNTIME_VERSION,
    VENDORED_RUNTIME_ROOT,
    bridge_command,
)


_VERSION_LINE = re.compile(r'^version\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
_BINARY_VERSION = re.compile(r"storydex-coomi-bridge\s+([^\s]+)")
_legacy_supported_version: str | None = None


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def packaged_requirements_path() -> Path:
    return Path(__file__).resolve().parents[1] / "requirements-runtime.txt"


def read_expected_coomi_version(requirements_path: Path | None = None) -> str:
    del requirements_path
    manifest = VENDORED_RUNTIME_ROOT / "Cargo.toml"
    if not manifest.is_file():
        return STORYDEX_COOMI_RUNTIME_VERSION
    match = _VERSION_LINE.search(manifest.read_text(encoding="utf-8-sig"))
    if match is None:
        raise RuntimeError(f"workspace version is missing from {manifest}")
    manifest_version = match.group(1)
    if manifest_version != STORYDEX_COOMI_RUNTIME_VERSION:
        raise RuntimeError(
            f"workspace version {manifest_version} != application version "
            f"{STORYDEX_COOMI_RUNTIME_VERSION}"
        )
    return STORYDEX_COOMI_RUNTIME_VERSION


def __getattr__(name: str) -> Any:
    if name != "SUPPORTED_COOMI_VERSION":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    global _legacy_supported_version
    if _legacy_supported_version is None:
        _legacy_supported_version = read_expected_coomi_version()
    return _legacy_supported_version


def _installed_bridge_version() -> tuple[str, str]:
    command = [*bridge_command(), "--version"]
    completed = subprocess.run(
        command,
        cwd=repository_root(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        detail = (completed.stderr or output).strip()
        raise RuntimeError(detail or f"bridge exited with {completed.returncode}")
    match = _BINARY_VERSION.search(output)
    if match is None:
        raise RuntimeError(f"unexpected bridge version output: {output!r}")
    return match.group(1), command[0]


def check_coomi_version(
    *,
    requirements_path: Path | None = None,
    metadata_version: str | None = None,
    module_version: str | None = None,
) -> dict[str, Any]:
    del metadata_version, module_version
    warnings: list[str] = []
    try:
        expected = read_expected_coomi_version(requirements_path)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        expected = ""
        warnings.append(f"Coomi Rust version source is invalid: {type(exc).__name__}: {exc}")
    try:
        installed, executable = _installed_bridge_version()
    except Exception as exc:
        installed, executable = "", ""
        warnings.append(f"Storydex Coomi Rust bridge is unavailable: {type(exc).__name__}: {exc}")
    if expected and installed and installed != expected:
        warnings.append(f"Storydex Coomi Rust bridge {installed} != expected {expected}")
    return {
        "ok": not warnings,
        "expected": expected,
        "metadataVersion": installed,
        "moduleVersion": installed,
        "binaryVersion": installed,
        "executable": executable,
        "runtime": "storydex-coomi-rs",
        "warnings": warnings,
    }
