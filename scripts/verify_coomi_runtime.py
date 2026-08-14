from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "apps" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.coomi_bridge_client import (  # noqa: E402
    BRIDGE_PROTOCOL_VERSION,
    STORYDEX_COOMI_RUNTIME_VERSION,
    bridge_command,
)
from services.coomi_version_service import check_coomi_version  # noqa: E402


def _read_build_info() -> dict[str, object]:
    completed = subprocess.run(
        [*bridge_command(), "--build-info"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Storydex Coomi bridge --build-info exited with {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("Storydex Coomi bridge --build-info did not return a JSON object")
    return payload


def main() -> int:
    status = check_coomi_version(requirements_path=REPOSITORY_ROOT / "requirements.txt")
    warnings = list(status.get("warnings") or [])
    try:
        build_info = _read_build_info()
        expected = {
            "runtime": "storydex-coomi-rs",
            "version": STORYDEX_COOMI_RUNTIME_VERSION,
            "gitSha": status.get("expectedGitSha"),
            "sourceFingerprint": status.get("expectedFingerprint"),
            "protocolVersion": BRIDGE_PROTOCOL_VERSION,
        }
        for key, expected_value in expected.items():
            if build_info.get(key) != expected_value:
                warnings.append(
                    f"Storydex Coomi bridge {key} {build_info.get(key)!r} != expected {expected_value!r}"
                )
        status["protocolVersion"] = build_info.get("protocolVersion")
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        warnings.append(f"Storydex Coomi bridge public contract is invalid: {type(exc).__name__}: {exc}")
    status["warnings"] = warnings
    status["ok"] = not warnings
    print(json.dumps(status, ensure_ascii=False, sort_keys=True))
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
