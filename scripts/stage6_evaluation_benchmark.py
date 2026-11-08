"""Stage 6 S1 Paired Decision Replay and Operator Control Plane Benchmark Script.

Executes Feature S1 (Time-Sealed Paired Decision Replay & Statistical Evaluation Engine)
against frozen diagnostic fixtures and negative controls.

Evaluates:
- Strategy A (Static Runbook)
- Strategy B (Cheapest-First Heuristic)
- Strategy C (A1 Adaptive Probe Planner)

Adjudicates with IndependentOracle (Hidden labels, zero false-positive tolerance on negative controls).
Performs:
- McNemar's paired test for decision accuracy (exact binomial / continuity-corrected chi2)
- Wilcoxon signed-rank test for cost distribution comparisons
- 100% Negative Control Noise Immunity Gate

Outputs structured JSON to `docs/evidence/stage6-evaluation-benchmark.json`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.config import get_settings
from backend.experiments.oracle import IndependentOracle
from backend.experiments.s1_replay import TimeSealedDecisionReplayHarness
from backend.persistence.db import make_engine, make_session_factory

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("opsblaze.stage6_benchmark")


def _save_benchmark_report(output_path: Path, output_data: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output_data, indent=2), encoding="utf-8")


async def main() -> None:
    settings = get_settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    try:
        oracle = IndependentOracle(version="oracle-v1.0.0")
        harness = TimeSealedDecisionReplayHarness(session_factory, oracle=oracle)

        logger.info("Executing Stage 6 S1 Time-Sealed Decision Replay Benchmark...")
        report = await harness.run_evaluation(
            title="Stage 6 Production S1 Decision Replay & Statistical Evaluation",
            fixture_family="omega_faults_and_negative_controls",
            strategy_version="titan20-stage6",
            random_seed=1337,
            repetitions=5,
        )

        output_data: dict[str, Any] = {
            "manifest_id": str(report.manifest_id),
            "title": report.title,
            "total_pairs": report.total_pairs,
            "total_trials": report.total_pairs * 3,
            "strategy_accuracies": report.strategy_accuracies,
            "strategy_accuracy_confidence_intervals": {
                k: list(v) for k, v in report.strategy_accuracy_confidence_intervals.items()
            },
            "strategy_avg_costs": report.strategy_avg_costs,
            "strategy_avg_probes": report.strategy_avg_probes,
            "mcnemar_a_vs_c": report.mcnemar_a_vs_c,
            "mcnemar_b_vs_c": report.mcnemar_b_vs_c,
            "wilcoxon_cost_a_vs_c": report.wilcoxon_cost_a_vs_c,
            "wilcoxon_cost_b_vs_c": report.wilcoxon_cost_b_vs_c,
            "negative_control_pass_rate": report.negative_control_pass_rate,
            "passed_quality_gate": report.passed_quality_gate,
        }

        output_path = REPO_ROOT / "docs" / "evidence" / "stage6-evaluation-benchmark.json"
        _save_benchmark_report(output_path, output_data)

        logger.info(f"Benchmark results successfully written to {output_path}")

        print("\n" + "=" * 75)
        print("STAGE 6 S1 TIME-SEALED PAIRED DECISION REPLAY BENCHMARK SUMMARY")
        print("=" * 75)
        print(f"Manifest ID:                {report.manifest_id}")
        print(f"Total Evaluated Pairs:      {report.total_pairs} ({report.total_pairs * 3} individual trials)")
        print(f"Negative Control Gate:      {report.negative_control_pass_rate * 100:.1f}% Pass Rate")
        print(f"Passed Overall Quality Gate:{report.passed_quality_gate}")
        print("-" * 75)
        print(f"{'Strategy':<28} | {'Accuracy (95% CI)':<22} | {'Avg Cost':<10} | {'Avg Probes':<10}")
        print("-" * 75)
        for strat in ["Strategy_A_Runbook", "Strategy_B_CheapestFirst", "Strategy_C_AdaptivePlanner"]:
            acc = report.strategy_accuracies.get(strat, 0.0) * 100
            ci = report.strategy_accuracy_confidence_intervals.get(strat, (0.0, 1.0))
            ci_str = f"{acc:>5.1f}% [{ci[0]*100:.1f}%, {ci[1]*100:.1f}%]"
            cost = report.strategy_avg_costs.get(strat, 0.0)
            probes = report.strategy_avg_probes.get(strat, 0.0)
            print(f"{strat:<28} | {ci_str:<22} | {cost:>10.1f} | {probes:>10.2f}")
        print("-" * 75)
        print("Hypothesis Testing (McNemar Paired Test for Accuracy vs Strategy C):")
        sig_a = report.mcnemar_a_vs_c.get('significant_at_05', False)
        sig_b = report.mcnemar_b_vs_c.get('significant_at_05', False)
        print(f"  - Strategy A vs C: p-value = {report.mcnemar_a_vs_c['p_value']:.6f} (Significant @ p<0.05: {sig_a})")
        print(f"  - Strategy B vs C: p-value = {report.mcnemar_b_vs_c['p_value']:.6f} (Significant @ p<0.05: {sig_b})")
        print("Cost Comparison (Wilcoxon Signed-Rank Test vs Strategy C):")
        w_sig_a = report.wilcoxon_cost_a_vs_c.get('significant_at_05', False)
        w_sig_b = report.wilcoxon_cost_b_vs_c.get('significant_at_05', False)
        print(f"  - Strategy A vs C: p-value = {report.wilcoxon_cost_a_vs_c['p_value']:.6f} (Significant @ p<0.05: {w_sig_a})")
        print(f"  - Strategy B vs C: p-value = {report.wilcoxon_cost_b_vs_c['p_value']:.6f} (Significant @ p<0.05: {w_sig_b})")
        print("=" * 75 + "\n")

    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
