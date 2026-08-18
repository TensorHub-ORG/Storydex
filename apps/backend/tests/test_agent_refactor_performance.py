from __future__ import annotations

from scripts.run_agent_refactor_performance import (
    _evaluate_performance_gate,
    _comparison,
    _percentile,
    _summarize_samples,
)


def test_percentile_uses_same_nearest_rank_rule_as_existing_baseline() -> None:
    assert _percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0
    assert _percentile([], 0.95) == 0.0


def test_sample_summary_ignores_non_timing_fields_and_missing_metrics() -> None:
    summary = _summarize_samples(
        [
            {"firstEventMs": 10.0, "terminalEvent": "AgentCompleted"},
            {"firstEventMs": 30.0, "toolStartMs": 20.0},
        ]
    )

    assert summary["sampleCount"] == 2
    assert summary["metrics"]["firstEventMs"] == {
        "sampleCount": 2,
        "medianMs": 20.0,
        "p95Ms": 30.0,
        "minMs": 10.0,
        "maxMs": 30.0,
    }
    assert summary["metrics"]["toolStartMs"]["sampleCount"] == 1
    assert "terminalEvent" not in summary["metrics"]


def test_comparison_reports_median_and_p95_ratios() -> None:
    python_summary = _summarize_samples(
        [{"firstEventMs": 100.0}, {"firstEventMs": 200.0}]
    )
    rust_summary = _summarize_samples(
        [{"firstEventMs": 50.0}, {"firstEventMs": 100.0}]
    )

    comparison = _comparison(python_summary, rust_summary)

    assert comparison["firstEventMs"]["medianRatio"] == 0.5
    assert comparison["firstEventMs"]["p95Ratio"] == 0.5


def test_decision_gate_passes_end_to_end_improvement_and_flags_component_noise() -> None:
    read_metrics = (
        "startupToHealthMs",
        "firstEventMs",
        "turnContractMs",
        "agentStartedMs",
        "runtimeMetricsMs",
        "toolStartMs",
        "terminalMs",
    )
    cancel_metrics = (
        "firstEventMs",
        "turnContractMs",
        "agentStartedMs",
        "runtimeMetricsMs",
        "stopRequestToHttpAcceptedMs",
        "stopRequestToTerminalMs",
        "terminalMs",
    )

    def metrics(names, value):
        result = {
            name: {
                "sampleCount": 20,
                "medianMs": value,
                "p95Ms": value,
            }
            for name in names
        }
        result["componentInitMs"] = {
            "sampleCount": 20,
            "medianMs": value,
            "p95Ms": value,
        }
        return result

    report = {
        "implementations": {
            "pythonStable": {
                "readOnly": {"metrics": metrics(read_metrics, 100.0)},
                "cancellation": {"metrics": metrics(cancel_metrics, 100.0)},
                "idleProcessTree": {
                    "idleSeconds": 60,
                    "totalRssBytes": 1000,
                },
            },
            "rustRefactor": {
                "readOnly": {"metrics": metrics(read_metrics, 50.0)},
                "cancellation": {"metrics": metrics(cancel_metrics, 50.0)},
                "idleProcessTree": {
                    "idleSeconds": 60,
                    "totalRssBytes": 500,
                },
            },
        }
    }
    report["implementations"]["rustRefactor"]["readOnly"]["metrics"][
        "componentInitMs"
    ]["p95Ms"] = 600.0

    gate = _evaluate_performance_gate(report)

    assert gate["status"] == "passed"
    assert gate["failedChecks"] == []
    assert gate["diagnosticInvestigations"] == [
        {
            "scenario": "readOnly",
            "metric": "componentInitMs",
            "pythonP95Ms": 100.0,
            "rustP95Ms": 600.0,
            "p95Ratio": 6.0,
            "status": "investigate_before_beta",
        }
    ]
