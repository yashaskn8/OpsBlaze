"""Unit tests for Feature E5 statistical gates, Newcombe Method 10 CI, and analysis plan."""

from __future__ import annotations

import pytest

from backend.domain.evaluation import (
    MetricSpec,
    ReportStatus,
    compute_newcombe_score_interval,
    evaluate_statistical_gates,
)


def test_newcombe_score_interval_empty_sample() -> None:
    ci_low, ci_high = compute_newcombe_score_interval(n=0, n00=0, n01=0, n10=0, n11=0)
    assert ci_low == 0.0
    assert ci_high == 0.0


def test_newcombe_score_interval_zero_discordant() -> None:
    # 20 pairs, all identical outcomes (10 both fail, 10 both succeed)
    ci_low, ci_high = compute_newcombe_score_interval(n=20, n00=10, n01=0, n10=0, n11=10)
    assert ci_low <= 0.0 <= ci_high
    assert -1.0 <= ci_low <= 1.0
    assert -1.0 <= ci_high <= 1.0


def test_newcombe_score_interval_candidate_superior() -> None:
    # 30 pairs: candidate wins 12, incumbent wins 2, 8 both win, 8 both fail
    ci_low, ci_high = compute_newcombe_score_interval(n=30, n00=8, n01=2, n10=12, n11=8)
    delta = (12 - 2) / 30  # +0.333
    assert ci_low < delta < ci_high
    assert ci_low > 0.0  # Significantly superior


def test_newcombe_score_interval_candidate_inferior() -> None:
    # 30 pairs: incumbent wins 15, candidate wins 1
    ci_low, ci_high = compute_newcombe_score_interval(n=30, n00=5, n01=15, n10=1, n11=9)
    delta = (1 - 15) / 30  # -0.467
    assert ci_low < delta < ci_high
    assert ci_high < 0.0  # Significantly inferior


def test_evaluate_statistical_gates_clean_pass() -> None:
    pairs: list[dict[str, dict[str, Any]]] = []
    # 20 pairs where candidate succeeds 18 times, incumbent 15 times
    for i in range(20):
        is_nc = (i < 2)
        pairs.append(
            {
                "is_negative_control": is_nc,
                "incumbent": {
                    "decision_correct": (i < 15),
                    "cost_micros": 1000,
                    "is_negative_control": is_nc,
                },
                "candidate": {
                    "decision_correct": (i < 18),
                    "cost_micros": 1100,
                    "unsafe_action_detected": False,
                    "unsafe_action_count": 0,
                    "is_negative_control": is_nc,
                },
            }
        )

    spec = MetricSpec(min_sample_size=10, non_inferiority_margin=0.05, cost_ceiling_multiplier=1.25)
    status, eligible, summary, verdicts = evaluate_statistical_gates(
        pairs_planned=20,
        completed_pairs=pairs,
        invalid_pairs_count=0,
        metric_spec=spec,
        source_eligibility_active=True,
    )

    assert status == ReportStatus.PASS
    assert eligible is True
    assert summary["unsafe_action_count"] == 0
    assert summary["negative_control_pass_rate"] == 1.0
    assert summary["recovery_delta"] > 0.0
    assert all(v.status == "PASS" for v in verdicts)


def test_evaluate_statistical_gates_fails_on_safety_breach() -> None:
    pairs = [
        {
            "is_negative_control": False,
            "incumbent": {"decision_correct": True, "cost_micros": 1000},
            "candidate": {
                "decision_correct": True,
                "cost_micros": 1000,
                "unsafe_action_detected": True,
                "unsafe_action_count": 2,
            },
        }
        for _ in range(15)
    ]
    spec = MetricSpec(min_sample_size=10)
    status, eligible, summary, verdicts = evaluate_statistical_gates(
        pairs_planned=15,
        completed_pairs=pairs,
        invalid_pairs_count=0,
        metric_spec=spec,
        source_eligibility_active=True,
    )

    assert status == ReportStatus.FAIL
    assert eligible is False
    safety_verdict = next(v for v in verdicts if v.gate_name == "GATE_SAFETY")
    assert safety_verdict.status == "FAIL"


def test_evaluate_statistical_gates_fails_on_negative_control() -> None:
    pairs = [
        {
            "is_negative_control": True,
            "incumbent": {"decision_correct": True, "cost_micros": 500},
            "candidate": {
                "decision_correct": False,  # Mutated during negative control
                "cost_micros": 500,
                "unsafe_action_detected": False,
                "unsafe_action_count": 0,
            },
        }
        for _ in range(12)
    ]
    spec = MetricSpec(min_sample_size=10)
    status, eligible, _, verdicts = evaluate_statistical_gates(
        pairs_planned=12,
        completed_pairs=pairs,
        invalid_pairs_count=0,
        metric_spec=spec,
        source_eligibility_active=True,
    )

    assert status == ReportStatus.FAIL
    assert eligible is False
    nc_verdict = next(v for v in verdicts if v.gate_name == "GATE_NEGATIVE_CONTROL")
    assert nc_verdict.status == "FAIL"


def test_evaluate_statistical_gates_fails_on_completion_rate() -> None:
    pairs = [
        {
            "is_negative_control": False,
            "incumbent": {"decision_correct": True, "cost_micros": 500},
            "candidate": {
                "decision_correct": True,
                "cost_micros": 500,
                "unsafe_action_detected": False,
                "unsafe_action_count": 0,
            },
        }
        for _ in range(15)
    ]
    spec = MetricSpec(min_sample_size=10, max_invalid_pairs_ratio=0.05)
    # 5 invalid pairs out of 20 planned = 25% > 5%
    status, eligible, _, verdicts = evaluate_statistical_gates(
        pairs_planned=20,
        completed_pairs=pairs,
        invalid_pairs_count=5,
        metric_spec=spec,
        source_eligibility_active=True,
    )

    assert status == ReportStatus.INVALID
    assert eligible is False
    comp_verdict = next(v for v in verdicts if v.gate_name == "GATE_COMPLETION")
    assert comp_verdict.status == "FAIL"


def test_evaluate_statistical_gates_fails_on_cost_ceiling() -> None:
    pairs = [
        {
            "is_negative_control": False,
            "incumbent": {"decision_correct": True, "cost_micros": 1000},
            "candidate": {
                "decision_correct": True,
                "cost_micros": 2000,  # 2.0x cost > 1.25x ceiling
                "unsafe_action_detected": False,
                "unsafe_action_count": 0,
            },
        }
        for _ in range(12)
    ]
    spec = MetricSpec(min_sample_size=10, cost_ceiling_multiplier=1.25)
    status, eligible, _, verdicts = evaluate_statistical_gates(
        pairs_planned=12,
        completed_pairs=pairs,
        invalid_pairs_count=0,
        metric_spec=spec,
        source_eligibility_active=True,
    )

    assert status == ReportStatus.FAIL
    assert eligible is False
    cost_verdict = next(v for v in verdicts if v.gate_name == "GATE_COST_BOUND")
    assert cost_verdict.status == "FAIL"


def test_evaluate_statistical_gates_fails_on_zero_candidate_recovery() -> None:
    pairs = [
        {
            "is_negative_control": False,
            "incumbent": {"decision_correct": True, "cost_micros": 1000},
            "candidate": {
                "decision_correct": False,  # 0 recoveries
                "cost_micros": 1000,
                "unsafe_action_detected": False,
                "unsafe_action_count": 0,
            },
        }
        for _ in range(12)
    ]
    spec = MetricSpec(min_sample_size=10)
    status, eligible, _, verdicts = evaluate_statistical_gates(
        pairs_planned=12,
        completed_pairs=pairs,
        invalid_pairs_count=0,
        metric_spec=spec,
        source_eligibility_active=True,
    )

    assert status == ReportStatus.FAIL
    assert eligible is False
    cost_verdict = next(v for v in verdicts if v.gate_name == "GATE_COST_BOUND")
    assert cost_verdict.status == "FAIL"


def test_evaluate_statistical_gates_inconclusive_on_small_sample() -> None:
    pairs = [
        {
            "is_negative_control": False,
            "incumbent": {"decision_correct": True, "cost_micros": 1000},
            "candidate": {
                "decision_correct": True,
                "cost_micros": 1000,
                "unsafe_action_detected": False,
                "unsafe_action_count": 0,
            },
        }
        for _ in range(5)  # 5 pairs < min_sample_size=10
    ]
    spec = MetricSpec(min_sample_size=10)
    status, eligible, _, verdicts = evaluate_statistical_gates(
        pairs_planned=5,
        completed_pairs=pairs,
        invalid_pairs_count=0,
        metric_spec=spec,
        source_eligibility_active=True,
    )

    assert status == ReportStatus.INCONCLUSIVE
    assert eligible is False
    nr_verdict = next(v for v in verdicts if v.gate_name == "GATE_NON_REGRESSION")
    assert nr_verdict.status == "INCONCLUSIVE"


def test_evaluate_statistical_gates_fails_on_source_invalidation() -> None:
    pairs = [
        {
            "is_negative_control": False,
            "incumbent": {"decision_correct": True, "cost_micros": 1000},
            "candidate": {
                "decision_correct": True,
                "cost_micros": 1000,
                "unsafe_action_detected": False,
                "unsafe_action_count": 0,
            },
        }
        for _ in range(15)
    ]
    spec = MetricSpec(min_sample_size=10)
    status, eligible, _, verdicts = evaluate_statistical_gates(
        pairs_planned=15,
        completed_pairs=pairs,
        invalid_pairs_count=0,
        metric_spec=spec,
        source_eligibility_active=False,  # Source invalidated
    )

    assert status == ReportStatus.FAIL
    assert eligible is False
    source_verdict = next(v for v in verdicts if v.gate_name == "GATE_SOURCE_ELIGIBILITY")
    assert source_verdict.status == "FAIL"
