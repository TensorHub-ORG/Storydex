from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from scripts import run_graph_live_acceptance as acceptance


def test_live_acceptance_removes_isolated_provider_copy_after_setup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_id = "test-provider"
    model = "test-model"
    source = tmp_path / "source-providers.json"
    source.write_text(
        json.dumps(
            {
                "version": 1,
                "active": provider_id,
                "providers": {
                    provider_id: {
                        "type": "openai_compatible",
                        "model": model,
                        "api_key": "test-secret-that-must-not-survive",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "acceptance-output"

    def fail_workspace_setup(_root: Path) -> dict:
        raise RuntimeError("stop after isolated provider setup")

    monkeypatch.setattr(acceptance, "prepare_workspace", fail_workspace_setup)
    args = argparse.Namespace(
        provider_id=provider_id,
        model=model,
        reasoning_effort="high",
        config=str(source),
        output_dir=str(output_root),
    )

    with pytest.raises(acceptance.AcceptanceError, match="stop after isolated provider setup"):
        acceptance.run_acceptance(args)

    reports = list(output_root.glob("*/acceptance-report.json"))
    assert len(reports) == 1
    report_text = reports[0].read_text(encoding="utf-8")
    assert "test-secret-that-must-not-survive" not in report_text
    report = json.loads(report_text)
    isolated_config = Path(report["provider"]["isolatedConfig"])
    assert report["provider"]["isolatedConfigRetained"] is False
    assert not isolated_config.exists()
    assert not isolated_config.parent.parent.exists()
