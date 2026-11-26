"""Tests for executable local comparative benchmarks across A1, E4, E5, and E7."""

import pytest

from backend.evaluation.local_benchmarks import (
    run_a1_strategy_comparison,
    run_e4_memory_ablation,
    run_e5_paired_recovery_cost,
    run_e7_routing_comparison,
)


@pytest.mark.asyncio
async def test_executable_local_comparative_benchmarks_all_pass(sessions_fixture):
    a1 = await run_a1_strategy_comparison(sessions_fixture, "bench-a1")
    assert a1["scope"] == "SIMULATOR_LAB_ONLY"
    assert a1["measured_ai_comparison"] is False
    assert a1["promotion_eligible"] is False
    assert a1["strategies"]["ADAPTIVE"]["unsafe_proposals"] == 0
    assert a1["strategies"]["ADAPTIVE"]["target_mutations"] == 0
    assert a1["strategies"]["ADAPTIVE"]["total_probes"] < a1["strategies"]["FIXED_RUNBOOK"]["total_probes"]
    assert a1["strategies"]["CHEAPEST_FIRST"]["accuracy"] == a1["strategies"]["ADAPTIVE"]["accuracy"] == 1.0

    e4 = await run_e4_memory_ablation(sessions_fixture, "bench-e4")
    assert e4["scope"] == "SIMULATOR_LAB_ONLY"
    assert e4["promotion_eligible"] is False
    by_case = {row["case"]: row for row in e4["pairs"]}
    assert by_case["active-lesson"]["with_memory_modes"] == ["VALIDATED_LESSONS", "VALIDATED_LESSONS"]
    assert e4["active_lesson_id"] in by_case["active-lesson"]["with_memory_lesson_ids"]
    assert by_case["active-lesson"]["no_memory_modes"] == ["NO_MEMORY_CONTROL", "NO_MEMORY_CONTROL"]
    assert by_case["active-lesson"]["no_memory_lesson_ids"] == []
    assert by_case["revoked-control"]["with_memory_modes"] == ["NO_MEMORY", "NO_MEMORY"]
    assert by_case["revoked-control"]["with_memory_lesson_ids"] == []
    assert by_case["no-lesson"]["with_memory_modes"] == ["NO_MEMORY", "NO_MEMORY"]

    e5 = await run_e5_paired_recovery_cost(sessions_fixture, "bench-e5")
    assert e5["scope"] == "SIMULATOR_LAB_ONLY"
    assert e5["promotion_eligible"] is False
    assert e5["pairs"] == 36
    assert e5["heldout_report"]["promotion_eligible"] is False
    assert e5["heldout_report"]["state"] in {"OFFLINE_PASS", "INCONCLUSIVE", "OFFLINE_FAIL"}
    metrics = e5["heldout_report"]["metrics"]
    assert metrics["candidate_unsafe"] == 0
    assert metrics["negative_control_failures"] == 0
    assert metrics["candidate_correct"] == metrics["incumbent_correct"] == 36
    assert metrics["candidate_probe_cost_units"] < metrics["incumbent_probe_cost_units"]
    cand_cost = e5["recovery_cost_report"]["arms"]["candidate"]
    inc_cost = e5["recovery_cost_report"]["arms"]["incumbent"]
    assert cand_cost["outcomes"]["UNKNOWN"] > 0
    assert cand_cost["verified_per_million_microcredits"] > 0
    assert inc_cost["verified_per_million_microcredits"] > 0

    e7 = await run_e7_routing_comparison(sessions_fixture, "bench-e7")
    assert e7["scope"] == "SIMULATOR_LAB_ONLY"
    assert e7["promotion_eligible"] is False
    for arm_name, summary in e7["arms"].items():
        assert summary["attribution_matches"] is True, arm_name
        assert summary["policy_pins_valid"] is True, arm_name
        assert summary["unsafe_proposals"] == 0, arm_name
    assert e7["arms"]["ADAPTIVE"]["route_counts"].get("HANDOFF", 0) >= 3
