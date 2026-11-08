"""Unit and statistical verification tests for OpsBlaze Feature S1 — Paired Decision Replay."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.experiments.oracle import HiddenLabel, IndependentOracle
from backend.experiments.s1_replay import TimeSealedDecisionReplayHarness
from backend.experiments.statistical_tests import (
    mcnemar_test,
    wilcoxon_signed_rank_test,
    wilson_score_interval,
)
from backend.persistence.models import EvaluationManifest, EvaluationTrial


def test_s1_mcnemar_statistical_test_properties() -> None:
    # 1. Identical performance
    res_ident = mcnemar_test([True, False, True], [True, False, True])
    assert res_ident["p_value"] == 1.0
    assert res_ident["significant_at_05"] is False

    # 2. Strong divergence (small sample exact binomial: candidate correct 10 times where baseline failed, baseline correct 0)
    b_small = [False] * 10
    c_small = [True] * 10
    res_small = mcnemar_test(b_small, c_small)
    assert res_small["method"] == "exact_binomial"
    assert res_small["p_value"] < 0.05
    assert res_small["significant_at_05"] is True

    # 3. Large sample continuity-corrected chi2 (discordant >= 25)
    b_large = [False] * 30 + [True] * 5
    c_large = [True] * 30 + [False] * 5
    res_large = mcnemar_test(b_large, c_large)
    assert res_large["method"] == "chi2_continuity_corrected"
    assert res_large["statistic"] > 10.0
    assert res_large["p_value"] < 0.001
    assert res_large["significant_at_05"] is True


def test_s1_wilcoxon_signed_rank_test_properties() -> None:
    # 1. Identical metrics
    res_ident = wilcoxon_signed_rank_test([10.0, 20.0, 30.0], [10.0, 20.0, 30.0])
    assert res_ident["p_value"] == 1.0
    assert res_ident["significant_at_05"] is False

    # 2. Consistent cost reduction (baseline higher cost than candidate)
    baseline_costs = [100.0, 150.0, 200.0, 120.0, 180.0, 210.0, 140.0, 190.0, 160.0, 220.0]
    candidate_costs = [30.0, 40.0, 50.0, 35.0, 45.0, 55.0, 40.0, 50.0, 45.0, 60.0]
    res_cost = wilcoxon_signed_rank_test(baseline_costs, candidate_costs)
    assert res_cost["w_minus"] == 0.0  # all diffs positive
    assert res_cost["p_value"] < 0.05
    assert res_cost["significant_at_05"] is True


def test_s1_wilson_score_interval_properties() -> None:
    # 1. Zero successes: lower bound must be 0.0, upper bound positive
    lower_0, upper_0 = wilson_score_interval(0, 20)
    assert lower_0 == 0.0
    assert 0.0 < upper_0 < 0.25

    # 2. Perfect score: lower bound < 1.0, upper bound must be 1.0
    lower_100, upper_100 = wilson_score_interval(20, 20)
    assert 0.75 < lower_100 < 1.0
    assert upper_100 == 1.0

    # 3. 50% score: center symmetric around 0.5
    lower_50, upper_50 = wilson_score_interval(10, 20)
    assert lower_50 < 0.5 < upper_50
    assert abs((0.5 - lower_50) - (upper_50 - 0.5)) < 0.01


def test_s1_independent_oracle_adjudication() -> None:
    oracle = IndependentOracle()

    # Negative control
    neg_label = HiddenLabel(
        fixture_id="neg-1",
        is_negative_control=True,
        true_root_cause="benign_jitter",
        expected_action_kind="NO_OP",
        target_id="k8s:pod:worker-1",
    )

    # False positive on negative control
    corr_fp, out_fp = oracle.adjudicate(neg_label, proposed_action="restart_pod", abstained=False)
    assert corr_fp is False
    assert out_fp == "FALSE_POSITIVE_MUTATION_ON_NEGATIVE_CONTROL"

    # Correct NO_OP on negative control
    corr_ok, out_ok = oracle.adjudicate(neg_label, proposed_action="NO_OP", abstained=False)
    assert corr_ok is True
    assert out_ok == "CORRECT_NO_OP_ON_NEGATIVE_CONTROL"

    # Real fault
    fault_label = HiddenLabel(
        fixture_id="fault-1",
        is_negative_control=False,
        true_root_cause="memory_leak",
        expected_action_kind="restart_pod",
        target_id="k8s:pod:auth-1",
    )

    # Correct action
    corr_fault, out_fault = oracle.adjudicate(fault_label, proposed_action="restart_pod", abstained=False)
    assert corr_fault is True
    assert out_fault == "CORRECT_RECOVERY_DECISION"

    # Incorrect action
    corr_wrong, out_wrong = oracle.adjudicate(fault_label, proposed_action="scale_up", abstained=False)
    assert corr_wrong is False
    assert out_wrong == "INCORRECT_ACTION"


@pytest.mark.asyncio
async def test_s1_time_sealed_decision_replay_harness_execution(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = TimeSealedDecisionReplayHarness(session_factory)
    report = await harness.run_evaluation(
        title="Stage 6 S1 Paired Evaluation Test",
        repetitions=2,  # 10 fixtures total
    )

    assert report.total_pairs == 10
    assert report.negative_control_pass_rate == 1.0  # Strategy C must pass 100% negative controls
    assert report.passed_quality_gate is True

    # Strategy C accuracy should exceed Strategy A and B
    acc = report.strategy_accuracies
    assert acc["Strategy_C_AdaptivePlanner"] >= acc["Strategy_A_Runbook"]
    assert acc["Strategy_C_AdaptivePlanner"] >= acc["Strategy_B_CheapestFirst"]

    # Verify Wilson score confidence intervals
    cis = report.strategy_accuracy_confidence_intervals
    assert "Strategy_C_AdaptivePlanner" in cis
    assert len(cis["Strategy_C_AdaptivePlanner"]) == 2
    lower_c, upper_c = cis["Strategy_C_AdaptivePlanner"]
    assert 0.0 <= lower_c <= upper_c <= 1.0

    # Verify persistence in PostgreSQL
    async with session_factory() as session:
        man = (
            await session.execute(
                select(EvaluationManifest).where(EvaluationManifest.id == report.manifest_id)
            )
        ).scalar_one()
        assert man.manifest_sha256 is not None
        assert man.split == "HOLDOUT"

        trials = (
            (
                await session.execute(
                    select(EvaluationTrial).where(EvaluationTrial.manifest_id == report.manifest_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(trials) == 30  # 10 fixtures * 3 strategies
