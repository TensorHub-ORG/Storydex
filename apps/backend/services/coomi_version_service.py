from __future__ import annotations

import json
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
_legacy_supported_version: str | None = None
_BUILD_METADATA_FILENAME = "storydex-coomi-build.json"


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def packaged_requirements_path() -> Path:
    return Path(__file__).resolve().parents[1] / "requirements-runtime.txt"


def packaged_build_metadata_path() -> Path:
    return Path(__file__).resolve().parents[1] / "runtime" / _BUILD_METADATA_FILENAME


def read_expected_coomi_version(requirements_path: Path | None = None) -> str:
    del requirements_path
    manifest = VENDORED_RUNTIME_ROOT / "Cargo.toml"
    if not manifest.is_file():
        metadata_path = packaged_build_metadata_path()
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            packaged_version = str(metadata.get("version") or "").strip()
            if packaged_version != STORYDEX_COOMI_RUNTIME_VERSION:
                raise RuntimeError(
                    f"packaged runtime version {packaged_version or '<missing>'} != application version "
                    f"{STORYDEX_COOMI_RUNTIME_VERSION}"
                )
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


def _git_bytes(repo_root: Path, arguments: list[str], *, input_bytes: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        input=input_bytes,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"git {' '.join(arguments)} exited with {completed.returncode}")
    return completed.stdout


def _repository_source_identity() -> dict[str, str]:
    repo_root = repository_root().resolve()
    runtime_root = VENDORED_RUNTIME_ROOT.resolve()
    try:
        runtime_relative = runtime_root.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"vendored runtime is outside repository: {runtime_root}") from exc
    listed = _git_bytes(
        repo_root,
        [
            "-c",
            "core.quotepath=false",
            "ls-files",
            "--full-name",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            runtime_relative,
        ],
    )
    paths = sorted(
        path.decode("utf-8")
        for path in listed.split(b"\0")
        if path
    )
    if not paths:
        raise RuntimeError(f"no runtime source files found under {runtime_relative}")
    hashes = _git_bytes(
        repo_root,
        ["hash-object", "--stdin-paths"],
        input_bytes=("\n".join(paths) + "\n").encode("utf-8"),
    ).decode("ascii").splitlines()
    if len(hashes) != len(paths):
        raise RuntimeError("runtime source fingerprint returned an incomplete hash list")
    fingerprint_manifest = b"".join(
        path.encode("utf-8") + b"\0" + source_hash.encode("ascii") + b"\n"
        for path, source_hash in zip(paths, hashes)
    )
    fingerprint = _git_bytes(
        repo_root,
        ["hash-object", "--stdin"],
        input_bytes=fingerprint_manifest,
    ).decode("ascii").strip()
    git_sha = _git_bytes(repo_root, ["rev-parse", "HEAD"]).decode("ascii").strip()
    return {"gitSha": git_sha, "sourceFingerprint": fingerprint}


def _expected_build_identity() -> dict[str, str]:
    if (VENDORED_RUNTIME_ROOT / "Cargo.toml").is_file():
        return _repository_source_identity()
    metadata_path = packaged_build_metadata_path()
    if not metadata_path.is_file():
        raise RuntimeError(f"packaged runtime build identity is missing: {metadata_path}")
    value = json.loads(metadata_path.read_text(encoding="utf-8"))
    identity = {
        "gitSha": str(value.get("gitSha") or "").strip(),
        "sourceFingerprint": str(value.get("sourceFingerprint") or "").strip(),
    }
    if not identity["gitSha"] or not identity["sourceFingerprint"]:
        raise RuntimeError(f"packaged runtime build identity is incomplete: {metadata_path}")
    return identity


def _installed_bridge_identity() -> tuple[dict[str, str], str]:
    command = [*bridge_command(), "--build-info"]
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
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"unexpected bridge build-info output: {output!r}") from exc
    identity = {
        "version": str(value.get("version") or "").strip(),
        "gitSha": str(value.get("gitSha") or "").strip(),
        "sourceFingerprint": str(value.get("sourceFingerprint") or "").strip(),
    }
    if not all(identity.values()):
        raise RuntimeError(f"incomplete bridge build-info output: {output!r}")
    return identity, command[0]


def _installed_bridge_version() -> tuple[str, str]:
    identity, executable = _installed_bridge_identity()
    return identity["version"], executable


def check_coomi_version(
    *,
    requirements_path: Path | None = None,
    metadata_version: str | None = None,
    module_version: str | None = None,
) -> dict[str, Any]:
    del metadata_version, module_version
    warnings: list[str] = []
    expected = STORYDEX_COOMI_RUNTIME_VERSION
    expected_identity: dict[str, str] = {"gitSha": "", "sourceFingerprint": ""}
    installed_identity: dict[str, str] = {
        "version": "",
        "gitSha": "",
        "sourceFingerprint": "",
    }
    try:
        read_expected_coomi_version(requirements_path)
        expected_identity = _expected_build_identity()
    except (FileNotFoundError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        warnings.append(f"Coomi Rust version source is invalid: {type(exc).__name__}: {exc}")
    try:
        installed_identity, executable = _installed_bridge_identity()
        installed = installed_identity["version"]
    except Exception as exc:
        installed, executable = "", ""
        warnings.append(f"Storydex Coomi Rust bridge is unavailable: {type(exc).__name__}: {exc}")
    if expected and installed and installed != expected:
        warnings.append(f"Storydex Coomi Rust bridge {installed} != expected {expected}")
    expected_fingerprint = expected_identity["sourceFingerprint"]
    installed_fingerprint = installed_identity["sourceFingerprint"]
    if expected_fingerprint and installed_fingerprint and installed_fingerprint != expected_fingerprint:
        warnings.append(
            "Storydex Coomi Rust bridge source fingerprint "
            f"{installed_fingerprint} != expected {expected_fingerprint}"
        )
    return {
        "ok": not warnings,
        "expected": expected,
        "expectedGitSha": expected_identity["gitSha"],
        "expectedFingerprint": expected_fingerprint,
        "metadataVersion": installed,
        "moduleVersion": installed,
        "binaryVersion": installed,
        "binaryGitSha": installed_identity["gitSha"],
        "binaryFingerprint": installed_fingerprint,
        "executable": executable,
        "runtime": "storydex-coomi-rs",
        "warnings": warnings,
    }
