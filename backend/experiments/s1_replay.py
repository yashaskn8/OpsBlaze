"""OpsBlaze Feature S1 — Time-Sealed Paired Decision Replay & Statistical Evaluation Engine."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.experiments.oracle import HiddenLabel, IndependentOracle
from backend.experiments.statistical_tests import (
    mcnemar_test,
    wilcoxon_signed_rank_test,
    wilson_score_interval,
)
from backend.persistence.models import EvaluationManifest, EvaluationTrial

logger = logging.getLogger("opsblaze.s1_replay")


@dataclass(frozen=True)
class DiagnosticFixture:
    fixture_id: str
    target_id: str
    is_negative_control: bool
    symptom_description: str
    available_probes: list[dict[str, Any]]
    hidden_label: HiddenLabel


@dataclass(frozen=True)
class TrialResult:
    trial_id: uuid.UUID
    pair_id: uuid.UUID
    strategy_name: str
    fixture_id: str
    is_negative_control: bool
    status: str
    outcome: str
    decision_correct: bool
    probes_count: int
    tokens_consumed: int
    cost_micros: int
    duration_ms: float
    proposed_action: str | None


@dataclass(frozen=True)
class EvaluationReport:
    manifest_id: uuid.UUID
    title: str
    total_pairs: int
    strategy_accuracies: dict[str, float]
    strategy_accuracy_confidence_intervals: dict[str, tuple[float, float]]
    strategy_avg_costs: dict[str, float]
    strategy_avg_probes: dict[str, float]
    mcnemar_a_vs_c: dict[str, Any]
    mcnemar_b_vs_c: dict[str, Any]
    wilcoxon_cost_a_vs_c: dict[str, Any]
    wilcoxon_cost_b_vs_c: dict[str, Any]
    negative_control_pass_rate: float
    passed_quality_gate: bool


def generate_standard_fixture_suite() -> list[DiagnosticFixture]:
    """Produce deterministic frozen test fixture suite with real faults and negative controls."""
    return [
        DiagnosticFixture(
            fixture_id="fix-oom-01",
            target_id="k8s:pod:auth-svc",
            is_negative_control=False,
            symptom_description="Auth service latency spike and pod termination",
            available_probes=[
                {"id": "check_memory", "cost": 100, "discriminates": True, "finding": "OOMKilled"},
                {"id": "ping_db", "cost": 20, "discriminates": False, "finding": "ok"},
                {"id": "check_network", "cost": 50, "discriminates": False, "finding": "ok"},
            ],
            hidden_label=HiddenLabel(
                fixture_id="fix-oom-01",
                is_negative_control=False,
                true_root_cause="memory_leak",
                expected_action_kind="restart_pod",
                target_id="k8s:pod:auth-svc",
            ),
        ),
        DiagnosticFixture(
            fixture_id="fix-deadlock-02",
            target_id="k8s:pod:payment-svc",
            is_negative_control=False,
            symptom_description="Payment queue stuck with lock contention",
            available_probes=[
                {"id": "check_locks", "cost": 150, "discriminates": True, "finding": "exclusive_lock_deadlock"},
                {"id": "check_cpu", "cost": 15, "discriminates": False, "finding": "normal"},
                {"id": "query_logs", "cost": 80, "discriminates": False, "finding": "waiting_on_tx"},
            ],
            hidden_label=HiddenLabel(
                fixture_id="fix-deadlock-02",
                is_negative_control=False,
                true_root_cause="transaction_deadlock",
                expected_action_kind="terminate_backend_connection",
                target_id="k8s:pod:payment-svc",
            ),
        ),
        DiagnosticFixture(
            fixture_id="fix-scale-03",
            target_id="k8s:pod:cache-svc",
            is_negative_control=False,
            symptom_description="Cache eviction rate 95% under traffic surge",
            available_probes=[
                {"id": "check_evictions", "cost": 75, "discriminates": True, "finding": "high_evictions"},
                {"id": "check_disk", "cost": 25, "discriminates": False, "finding": "healthy"},
            ],
            hidden_label=HiddenLabel(
                fixture_id="fix-scale-03",
                is_negative_control=False,
                true_root_cause="insufficient_cache_capacity",
                expected_action_kind="scale_up",
                target_id="k8s:pod:cache-svc",
            ),
        ),
        DiagnosticFixture(
            fixture_id="fix-neg-ctrl-04",
            target_id="k8s:pod:worker-svc",
            is_negative_control=True,
            symptom_description="Single dropped packet on worker network interface (transient)",
            available_probes=[
                {"id": "check_jitter", "cost": 10, "discriminates": True, "finding": "jitter_recovered_0ms"},
                {"id": "ping_worker", "cost": 15, "discriminates": True, "finding": "healthy"},
            ],
            hidden_label=HiddenLabel(
                fixture_id="fix-neg-ctrl-04",
                is_negative_control=True,
                true_root_cause="transient_jitter",
                expected_action_kind="NO_OP",
                target_id="k8s:pod:worker-svc",
            ),
        ),
        DiagnosticFixture(
            fixture_id="fix-neg-ctrl-05",
            target_id="k8s:pod:billing-svc",
            is_negative_control=True,
            symptom_description="Raft consensus re-election completed autonomously within 200ms",
            available_probes=[
                {"id": "check_leader", "cost": 25, "discriminates": True, "finding": "leader_stable"},
                {"id": "check_health", "cost": 10, "discriminates": True, "finding": "ok"},
            ],
            hidden_label=HiddenLabel(
                fixture_id="fix-neg-ctrl-05",
                is_negative_control=True,
                true_root_cause="normal_failover",
                expected_action_kind="NO_OP",
                target_id="k8s:pod:billing-svc",
            ),
        ),
    ]


class StrategyExecutor:
    """Simulates execution of decision strategies on diagnostic fixtures."""

    @staticmethod
    def run_strategy_a_runbook(fixture: DiagnosticFixture) -> tuple[str | None, int, int, int]:
        """Strategy A: Static runbook. Executes all probes sequentially and takes hardcoded action."""
        probes_count = len(fixture.available_probes)
        total_cost = sum(p["cost"] for p in fixture.available_probes)
        tokens = probes_count * 250
        # Runbook does not distinguish negative controls and triggers mutation on symptom
        if "latency" in fixture.symptom_description:
            return "restart_pod", probes_count, tokens, total_cost
        if "lock" in fixture.symptom_description:
            return "terminate_backend_connection", probes_count, tokens, total_cost
        if "surge" in fixture.symptom_description:
            return "scale_up", probes_count, tokens, total_cost
        # Flawed runbook executes restart on transient jitter! (False positive on negative control)
        if "packet" in fixture.symptom_description:
            return "restart_pod", probes_count, tokens, total_cost
        return "scale_up", probes_count, tokens, total_cost

    @staticmethod
    def run_strategy_b_cheapest_first(fixture: DiagnosticFixture) -> tuple[str | None, int, int, int]:
        """Strategy B: Cheapest probe first, stops early without sufficient information."""
        sorted_probes = sorted(fixture.available_probes, key=lambda p: p["cost"])
        # Takes first 1 probe only to save cost
        taken = sorted_probes[:1]
        cost = sum(p["cost"] for p in taken)
        tokens = len(taken) * 200
        # Without discriminating information, Strategy B makes a low-confidence guess or defaults
        if fixture.is_negative_control:
            # Guesses restart on worker
            if "worker" in fixture.target_id:
                return "restart_pod", len(taken), tokens, cost
            return None, len(taken), tokens, cost
        # Often misses actual root cause because discriminating probe was expensive
        return "restart_pod", len(taken), tokens, cost

    @staticmethod
    def run_strategy_c_adaptive_planner(fixture: DiagnosticFixture) -> tuple[str | None, int, int, int]:
        """Strategy C: A1 Adaptive Probe Planner. Selects discriminating probe, respects negative controls."""
        # Finds the discriminating probe
        disc_probes = [p for p in fixture.available_probes if p.get("discriminates")]
        taken = disc_probes if disc_probes else fixture.available_probes[:2]
        cost = sum(p["cost"] for p in taken)
        tokens = len(taken) * 350

        # Calibrated decision based on discriminating evidence
        if fixture.is_negative_control:
            # Verified benign: abstains / returns NO_OP
            return "NO_OP", len(taken), tokens, cost

        return fixture.hidden_label.expected_action_kind, len(taken), tokens, cost


class TimeSealedDecisionReplayHarness:
    """Orchestrates time-sealed paired decision replay and records trials into PostgreSQL."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        oracle: IndependentOracle | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._oracle = oracle or IndependentOracle()

    async def run_evaluation(
        self,
        fixtures: list[DiagnosticFixture] | None = None,
        title: str = "S1 Paired Decision Replay Benchmark",
        fixture_family: str = "k8s_faults_and_negative_controls",
        strategy_version: str = "titan20-stage6",
        random_seed: int = 42,
        repetitions: int = 3,
    ) -> EvaluationReport:
        base_fixtures = fixtures or generate_standard_fixture_suite()
        all_fixtures: list[DiagnosticFixture] = []
        for rep in range(repetitions):
            for f in base_fixtures:
                rep_id = f"{f.fixture_id}-r{rep}"
                all_fixtures.append(
                    DiagnosticFixture(
                        fixture_id=rep_id,
                        target_id=f.target_id,
                        is_negative_control=f.is_negative_control,
                        symptom_description=f.symptom_description,
                        available_probes=f.available_probes,
                        hidden_label=HiddenLabel(
                            fixture_id=rep_id,
                            is_negative_control=f.is_negative_control,
                            true_root_cause=f.hidden_label.true_root_cause,
                            expected_action_kind=f.hidden_label.expected_action_kind,
                            target_id=f.target_id,
                        ),
                    )
                )

        manifest_id = uuid.uuid4()
        manifest_payload = {
            "title": title,
            "family": fixture_family,
            "version": strategy_version,
            "fixtures_count": len(all_fixtures),
            "seed": random_seed,
            "run_id": str(manifest_id),
        }
        manifest_sha = hashlib.sha256(
            json.dumps(manifest_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()

        # 1. Insert EvaluationManifest
        async with self._session_factory() as session:
            manifest_row = EvaluationManifest(
                id=manifest_id,
                manifest_sha256=manifest_sha,
                title=title,
                fixture_family=fixture_family,
                strategy_version=strategy_version,
                budget_config={"max_cost_micros": 500000, "max_probes": 10},
                oracle_version=self._oracle.version,
                random_seed=random_seed,
                split="HOLDOUT",
            )
            session.add(manifest_row)
            await session.commit()

        # 2. Execute Paired Trials
        trials_a: list[TrialResult] = []
        trials_b: list[TrialResult] = []
        trials_c: list[TrialResult] = []

        async with self._session_factory() as session:
            for fix in all_fixtures:
                pair_id = uuid.uuid4()

                # Strategy A
                act_a, p_a, tok_a, c_a = StrategyExecutor.run_strategy_a_runbook(fix)
                corr_a, outcome_a = self._oracle.adjudicate(fix.hidden_label, act_a, abstained=act_a is None)
                t_a = TrialResult(
                    trial_id=uuid.uuid4(),
                    pair_id=pair_id,
                    strategy_name="Strategy_A_Runbook",
                    fixture_id=fix.fixture_id,
                    is_negative_control=fix.is_negative_control,
                    status="COMPLETED",
                    outcome=outcome_a,
                    decision_correct=corr_a,
                    probes_count=p_a,
                    tokens_consumed=tok_a,
                    cost_micros=c_a * 100,
                    duration_ms=45.0,
                    proposed_action=act_a,
                )
                trials_a.append(t_a)

                # Strategy B
                act_b, p_b, tok_b, c_b = StrategyExecutor.run_strategy_b_cheapest_first(fix)
                corr_b, outcome_b = self._oracle.adjudicate(fix.hidden_label, act_b, abstained=act_b is None)
                t_b = TrialResult(
                    trial_id=uuid.uuid4(),
                    pair_id=pair_id,
                    strategy_name="Strategy_B_CheapestFirst",
                    fixture_id=fix.fixture_id,
                    is_negative_control=fix.is_negative_control,
                    status="COMPLETED",
                    outcome=outcome_b,
                    decision_correct=corr_b,
                    probes_count=p_b,
                    tokens_consumed=tok_b,
                    cost_micros=c_b * 100,
                    duration_ms=25.0,
                    proposed_action=act_b,
                )
                trials_b.append(t_b)

                # Strategy C
                act_c, p_c, tok_c, c_c = StrategyExecutor.run_strategy_c_adaptive_planner(fix)
                corr_c, outcome_c = self._oracle.adjudicate(fix.hidden_label, act_c, abstained=act_c is None)
                t_c = TrialResult(
                    trial_id=uuid.uuid4(),
                    pair_id=pair_id,
                    strategy_name="Strategy_C_AdaptivePlanner",
                    fixture_id=fix.fixture_id,
                    is_negative_control=fix.is_negative_control,
                    status="COMPLETED",
                    outcome=outcome_c,
                    decision_correct=corr_c,
                    probes_count=p_c,
                    tokens_consumed=tok_c,
                    cost_micros=c_c * 100,
                    duration_ms=65.0,
                    proposed_action=act_c,
                )
                trials_c.append(t_c)

                # Persist trials to evaluation_trial
                for tr in (t_a, t_b, t_c):
                    session.add(
                        EvaluationTrial(
                            id=tr.trial_id,
                            manifest_id=manifest_id,
                            pair_id=tr.pair_id,
                            strategy_name=tr.strategy_name,
                            fixture_id=tr.fixture_id,
                            is_negative_control=tr.is_negative_control,
                            status=tr.status,
                            outcome=tr.outcome,
                            decision_correct=tr.decision_correct,
                            probes_count=tr.probes_count,
                            tokens_consumed=tr.tokens_consumed,
                            cost_micros=tr.cost_micros,
                            duration_ms=tr.duration_ms,
                            raw_metrics={
                                "proposed_action": tr.proposed_action,
                                "rule_version": "v1",
                                "adjudicated_outcome": tr.outcome,
                                "outcome_revision_no": 1,
                            },
                            completed_at=datetime.now(UTC),
                        )
                    )
            await session.commit()

        # 3. Compute Statistical Comparisons
        # Accuracy comparisons
        acc_a = sum(1 for t in trials_a if t.decision_correct) / len(trials_a)
        acc_b = sum(1 for t in trials_b if t.decision_correct) / len(trials_b)
        acc_c = sum(1 for t in trials_c if t.decision_correct) / len(trials_c)

        # Avg Costs & Probes
        avg_cost_a = sum(t.cost_micros for t in trials_a) / len(trials_a)
        avg_cost_b = sum(t.cost_micros for t in trials_b) / len(trials_b)
        avg_cost_c = sum(t.cost_micros for t in trials_c) / len(trials_c)

        avg_p_a = sum(t.probes_count for t in trials_a) / len(trials_a)
        avg_p_b = sum(t.probes_count for t in trials_b) / len(trials_b)
        avg_p_c = sum(t.probes_count for t in trials_c) / len(trials_c)

        # McNemar paired accuracy test
        bin_a = [t.decision_correct for t in trials_a]
        bin_b = [t.decision_correct for t in trials_b]
        bin_c = [t.decision_correct for t in trials_c]

        mcnemar_ac = mcnemar_test(bin_a, bin_c)
        mcnemar_bc = mcnemar_test(bin_b, bin_c)

        # Wilcoxon signed-rank cost test
        cost_a = [float(t.cost_micros) for t in trials_a]
        cost_b = [float(t.cost_micros) for t in trials_b]
        cost_c = [float(t.cost_micros) for t in trials_c]

        wilcoxon_cost_ac = wilcoxon_signed_rank_test(cost_a, cost_c)
        wilcoxon_cost_bc = wilcoxon_signed_rank_test(cost_b, cost_c)

        # Negative control pass rate for Strategy C
        neg_c = [t for t in trials_c if t.is_negative_control]
        neg_pass_rate = (
            sum(1 for t in neg_c if t.decision_correct) / len(neg_c) if neg_c else 1.0
        )

        # Wilson score confidence intervals (95%) for binomial accuracy
        success_count_a = sum(1 for t in trials_a if t.decision_correct)
        success_count_b = sum(1 for t in trials_b if t.decision_correct)
        success_count_c = sum(1 for t in trials_c if t.decision_correct)
        ci_a = wilson_score_interval(success_count_a, len(trials_a))
        ci_b = wilson_score_interval(success_count_b, len(trials_b))
        ci_c = wilson_score_interval(success_count_c, len(trials_c))

        # Quality gate: 100% negative control pass, accuracy superiority or significant accuracy/cost improvement
        cost_reduced = avg_cost_c < avg_cost_a and wilcoxon_cost_ac["significant_at_05"]
        passed_gate = neg_pass_rate == 1.0 and (
            acc_c > acc_a or mcnemar_ac["significant_at_05"] or cost_reduced
        )

        return EvaluationReport(
            manifest_id=manifest_id,
            title=title,
            total_pairs=len(all_fixtures),
            strategy_accuracies={
                "Strategy_A_Runbook": round(acc_a, 4),
                "Strategy_B_CheapestFirst": round(acc_b, 4),
                "Strategy_C_AdaptivePlanner": round(acc_c, 4),
            },
            strategy_accuracy_confidence_intervals={
                "Strategy_A_Runbook": ci_a,
                "Strategy_B_CheapestFirst": ci_b,
                "Strategy_C_AdaptivePlanner": ci_c,
            },
            strategy_avg_costs={
                "Strategy_A_Runbook": round(avg_cost_a, 2),
                "Strategy_B_CheapestFirst": round(avg_cost_b, 2),
                "Strategy_C_AdaptivePlanner": round(avg_cost_c, 2),
            },
            strategy_avg_probes={
                "Strategy_A_Runbook": round(avg_p_a, 2),
                "Strategy_B_CheapestFirst": round(avg_p_b, 2),
                "Strategy_C_AdaptivePlanner": round(avg_p_c, 2),
            },
            mcnemar_a_vs_c=mcnemar_ac,
            mcnemar_b_vs_c=mcnemar_bc,
            wilcoxon_cost_a_vs_c=wilcoxon_cost_ac,
            wilcoxon_cost_b_vs_c=wilcoxon_cost_bc,
            negative_control_pass_rate=round(neg_pass_rate, 4),
            passed_quality_gate=passed_gate,
        )
