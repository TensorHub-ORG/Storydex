from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


CRITICAL_MODULES = (
    "api/routes_agent.py",
    "services/agent_git_autocommit_service.py",
    "services/coomi_agent_service.py",
    "services/git_service.py",
    "services/storydex_intent_service.py",
)


def _write_report(tmp_path: Path, summary: dict[str, float | int]) -> Path:
    report = {
        "totals": summary,
        "files": {module: {"summary": summary} for module in CRITICAL_MODULES},
    }
    report_path = tmp_path / "coverage.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return report_path


def _summary(*, lines: int, branches: int) -> dict[str, float | int]:
    return {
        "covered_lines": lines,
        "num_statements": 100,
        "percent_covered": float(lines - 5),
        "missing_lines": 100 - lines,
        "excluded_lines": 0,
        "covered_branches": branches,
        "num_branches": 100,
        "missing_branches": 100 - branches,
        "num_partial_branches": 0,
    }


def test_legacy_coverage_json_uses_statement_percentage(tmp_path: Path) -> None:
    report_path = _write_report(tmp_path, _summary(lines=90, branches=70))

    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("assert_coverage.py")), str(report_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Coverage gate passed: lines=90.00% branches=70.00%" in result.stdout


def test_coverage_threshold_miss_remains_strict_by_default(tmp_path: Path) -> None:
    report_path = _write_report(tmp_path, _summary(lines=79, branches=69))

    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("assert_coverage.py")), str(report_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "Coverage gate failed:" in result.stdout


def test_warn_only_reports_threshold_miss_without_failing(tmp_path: Path) -> None:
    report_path = _write_report(tmp_path, _summary(lines=79, branches=69))

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("assert_coverage.py")),
            str(report_path),
            "--warn-only",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Coverage warning (non-blocking):" in result.stdout
