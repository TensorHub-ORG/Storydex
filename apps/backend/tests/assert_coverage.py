from __future__ import annotations

import argparse
import json
from pathlib import Path


CRITICAL_MODULES = (
    "api/routes_agent.py",
    "services/agent_git_autocommit_service.py",
    "services/coomi_agent_service.py",
    "services/git_service.py",
    "services/storydex_intent_service.py",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Enforce Storydex coverage quality gates.")
    parser.add_argument("coverage_json", type=Path)
    parser.add_argument("--global-lines", type=float, default=80.0)
    parser.add_argument("--global-branches", type=float, default=70.0)
    parser.add_argument("--critical-lines", type=float, default=90.0)
    args = parser.parse_args()

    payload = json.loads(args.coverage_json.read_text(encoding="utf-8"))
    totals = payload["totals"]
    failures: list[str] = []
    line_percent = float(totals.get("percent_statements_covered", totals.get("percent_covered", 0.0)))
    branch_percent = 100.0 * float(totals.get("covered_branches", 0)) / max(
        1, float(totals.get("num_branches", 0))
    )
    if line_percent < args.global_lines:
        failures.append(f"global lines {line_percent:.2f}% < {args.global_lines:.2f}%")
    if branch_percent < args.global_branches:
        failures.append(f"global branches {branch_percent:.2f}% < {args.global_branches:.2f}%")

    files = {name.replace("\\", "/"): value for name, value in payload.get("files", {}).items()}
    for expected in CRITICAL_MODULES:
        match = next((value for name, value in files.items() if name.endswith(expected)), None)
        summary = (match or {}).get("summary", {})
        percent = float(summary.get("percent_statements_covered", summary.get("percent_covered", 0.0)))
        if percent < args.critical_lines:
            failures.append(f"{expected} lines {percent:.2f}% < {args.critical_lines:.2f}%")

    if failures:
        print("Coverage gate failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"Coverage gate passed: lines={line_percent:.2f}% branches={branch_percent:.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
