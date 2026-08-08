from __future__ import annotations

import pytest

from scripts.run_agent_lifecycle_baseline import AcceptanceError, validate_baseline_turn


def valid_turn() -> dict:
    return {
        "toolCallCount": 1,
        "toolNames": ["read_file"],
        "markerObserved": True,
        "lifecycle": {"tools": [{"tool": "read_file", "error": False}]},
    }


def test_baseline_validation_accepts_exact_read_only_turn() -> None:
    validate_baseline_turn(valid_turn())


@pytest.mark.parametrize(
    "change",
    (
        {"toolNames": ["read_file", "update_plan"]},
        {"toolCallCount": 2},
        {"markerObserved": False},
        {"lifecycle": {"tools": [{"tool": "read_file", "error": True}]}},
    ),
)
def test_baseline_validation_rejects_contract_breaks(change: dict) -> None:
    turn = valid_turn()
    turn.update(change)
    with pytest.raises(AcceptanceError):
        validate_baseline_turn(turn)
