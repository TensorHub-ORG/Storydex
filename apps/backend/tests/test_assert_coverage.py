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


def test_legacy_coverage_json_uses_statement_percentage(tmp_path: Path) -> None:
    summary = {
        "covered_lines": 90,
        "num_statements": 100,
        "percent_covered": 85.0,
        "missing_lines": 10,
        "excluded_lines": 0,
        "covered_branches": 70,
        "num_branches": 100,
        "missing_branches": 30,
        "num_partial_branches": 0,
    }
    report = {
        "totals": summary,
        "files": {module: {"summary": summary} for module in CRITICAL_MODULES},
    }
    report_path = tmp_path / "coverage.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("assert_coverage.py")), str(report_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Coverage gate passed: lines=90.00% branches=70.00%" in result.stdout
