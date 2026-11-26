"""Tests for executable local comparative benchmarks across A1, E4, E5, and E7."""

import pytest

from backend.evaluation.local_benchmarks import (
    run_a1_strategy_comparison,
    run_e4_memory_ablation,
    run_e5_paired_recovery_cost,
    run_e7_routing_comparison,
)


@pytest.fixture
async def benchmark_database():
    from backend.database import get_engine, get_session_maker

    try:
        yield get_session_maker()
    finally:
        await get_engine().dispose()


@pytest.mark.asyncio
async def test_executable_local_comparative_benchmarks_all_pass(sessions_fixture, record_property):
    import json

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
    repeated = await run_e4_memory_ablation(sessions_fixture, "bench-e4")
    assert repeated["active_lesson_id"] != e4["active_lesson_id"]
    assert repeated["design_sha256"] == e4["design_sha256"]
    assert repeated["probe_counts"] == e4["probe_counts"] == {"with_memory": 10, "no_memory": 10}
    assert repeated["decision_counts"] == e4["decision_counts"] == {
        "with_memory_correct": 6, "no_memory_correct": 6, "with_memory_unsafe": 0, "no_memory_unsafe": 0,
    }
    assert by_case["expired-control"]["with_memory_lesson_ids"] == []
    assert by_case["healthy-with-lesson"]["with_memory_lesson_ids"]
    assert by_case["healthy-with-lesson"]["with_memory_outcome"] == "ABSTAINED"
    assert by_case["missing-target"]["with_memory_outcome"] == "ABSTAINED"
    assert all(row["retrieval"]["iterations"] == 6 for row in e4["pairs"])
    assert all(row["with_memory_effect_attempts"] == row["no_memory_effect_attempts"] == 0 for row in e4["pairs"])
    assert all(row["with_memory_outcome"] == row["no_memory_outcome"] for row in repeated["pairs"])

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
    for cost in (cand_cost, inc_cost):
        assert cost["outcomes"] == {"DISPUTED": 0, "FAILED": 36, "UNKNOWN": 0, "VERIFIED": 0}
        assert cost["verified_per_million_microcredits"] == 0
        assert cost["total_microcredits"] > 0

    e7 = await run_e7_routing_comparison(sessions_fixture, "bench-e7")
    assert e7["scope"] == "SIMULATOR_LAB_ONLY"
    assert e7["promotion_eligible"] is False
    for arm_name, summary in e7["arms"].items():
        assert summary["attribution_matches"] is True, arm_name
        assert summary["policy_pins_valid"] is True, arm_name
        assert summary["unsafe_proposals"] == 0, arm_name
    assert e7["arms"]["ADAPTIVE"]["route_counts"].get("HANDOFF", 0) >= 3
    assert e7["arms"]["ADAPTIVE"]["executed_strategy_counts"] == {"ADAPTIVE": 3, "NO_INVESTIGATION": 3}
    assert e7["arms"]["FIXED_RUNBOOK"]["executed_strategy_counts"] == {
        "FIXED_RUNBOOK": 3, "NO_INVESTIGATION": 3,
    }
    assert e7["arms"]["CHEAPEST_FIRST"]["executed_strategy_counts"] == {
        "CHEAPEST_FIRST": 3, "NO_INVESTIGATION": 3,
    }
    assert e7["arms"]["FIXED_RUNBOOK"]["total_probes"] == e7["arms"]["ADAPTIVE"]["total_probes"] + 1
    record_property("local_comparisons", json.dumps({"a1": a1, "e4": e4, "e7": e7}))


@pytest.mark.integration
@pytest.mark.pg_e2e
async def test_postgres_e4_memory_ablation_uses_scoped_retrieval_and_controls(
    benchmark_database, record_property, monkeypatch,
):
    import json

    from sqlalchemy import event

    from backend.database import get_engine
    from backend.evaluation import local_benchmarks

    evaluate_lookup = local_benchmarks.evaluate_memory_vs_no_memory

    async def measure_lookup_statements(*args, **kwargs):
        statements = 0

        def count(*_):
            nonlocal statements
            statements += 1

        engine = get_engine().sync_engine
        event.listen(engine, "before_cursor_execute", count)
        try:
            result = await evaluate_lookup(*args, **kwargs)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        result["sql_statements_per_pair"] = statements / result["iterations"]
        return result

    monkeypatch.setattr(local_benchmarks, "evaluate_memory_vs_no_memory", measure_lookup_statements)
    report = await run_e4_memory_ablation(benchmark_database, "native-e4")
    cases = {row["case"]: row for row in report["pairs"]}
    assert report["scope"] == "SIMULATOR_LAB_ONLY" and report["promotion_eligible"] is False
    assert report["decision_counts"] == {
        "with_memory_correct": 6, "no_memory_correct": 6, "with_memory_unsafe": 0, "no_memory_unsafe": 0,
    }
    assert report["probe_counts"] == {"with_memory": 10, "no_memory": 10}
    assert cases["active-lesson"]["retrieval"]["cited_lesson_count"] == 1
    assert cases["healthy-with-lesson"]["with_memory_outcome"] == "ABSTAINED"
    for name in ("revoked-control", "expired-control", "no-lesson", "missing-target"):
        assert cases[name]["with_memory_lesson_ids"] == []
        assert cases[name]["retrieval"]["cited_lesson_count"] == 0
    assert all(row["no_memory_lesson_ids"] == [] for row in report["pairs"])
    assert all(row["with_memory_effect_attempts"] == row["no_memory_effect_attempts"] == 0 for row in report["pairs"])
    record_property("lab_e4_summary", json.dumps(report))
    assert cases["active-lesson"]["retrieval"]["sql_statements_per_pair"] <= 3


@pytest.mark.integration
@pytest.mark.pg_e2e
async def test_postgres_temporal_e5_counts_only_authorized_witnessed_recovery(
    benchmark_database, monkeypatch, record_property,
):
    import json

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from backend.adapters.conformance import execute_suite
    from backend.adapters.simulator import SimulatorAdapter
    from backend.api import app
    from backend.benchmarks.paired_simulator import run_e5_with_temporal
    from backend.evaluation.local_benchmarks import _e5_recorded_recovery
    from backend.models import IncidentPlan, OutcomeRevision
    from backend.settings import get_settings
    from backend.workflows.activities import (
        apply_approved_simulator_plan, inspect_incident_read_only, project_incident_status,
        select_incident_route,
    )
    from backend.workflows.incident import IncidentWorkflow

    settings = get_settings()
    sessions = benchmark_database
    assert settings.database_url.startswith("postgresql+asyncpg:")
    assert (await execute_suite(sessions)).state == "LAB_PASS"

    apply = SimulatorAdapter.apply

    async def lost_ack(self, effect_id, permit):
        version = await apply(self, effect_id, permit)
        if permit.target_id.startswith("fam-rec-0-"):
            raise TimeoutError("synthetic loss after durable simulator apply")
        return version

    monkeypatch.setattr(SimulatorAdapter, "apply", lost_ack)
    dispute_case = None
    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(env.client, task_queue=settings.temporal_task_queue, workflows=[IncidentWorkflow],
               activities=[apply_approved_simulator_plan, inspect_incident_read_only,
                           project_incident_status, select_incident_route]),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        async def review(tenant, plan_id, version):
            nonlocal dispute_case
            async with sessions() as session:
                plan = await session.scalar(select(IncidentPlan).where(
                    IncidentPlan.id == plan_id, IncidentPlan.tenant_id == tenant,
                ))
            if plan.target_id == "fam-rec-2-candidate":
                dispute_case = (tenant, plan.incident_id)
            response = await http.post(
                f"/v1/plans/{plan_id}/decisions",
                headers={"X-Tenant-Id": tenant, "X-OpsBlaze-Token": settings.local_token,
                         "X-OpsBlaze-Reviewer-Token": settings.review_token, "If-Match": str(version)},
                json={"decision": "REJECT" if plan.target_id.startswith("fam-rec-1-") else "APPROVE",
                      "reason": "Disposable paired simulator fixture reviewed"},
            )
            assert response.status_code == 201, response.text

        report = await run_e5_with_temporal(env.client, "native-e5", review_requested=review)
        assert dispute_case is not None
        tenant, incident_id = dispute_case
        async with sessions() as session:
            witness = (await session.scalars(select(OutcomeRevision).where(
                OutcomeRevision.tenant_id == tenant, OutcomeRevision.incident_id == incident_id,
                OutcomeRevision.label == "VERIFIED",
            ))).one()
        dispute = await http.post(
            f"/v1/outcomes/{witness.id}/feedback",
            headers={"X-Tenant-Id": witness.tenant_id, "X-OpsBlaze-Token": settings.local_token,
                     "X-OpsBlaze-Reviewer-Token": settings.review_token, "If-Match": str(witness.revision)},
            json={"reason": "Independent lab fixture reviewer disputed the recovery observation"},
        )
        assert dispute.status_code == 202, dispute.text
        disputed = await _e5_recorded_recovery(sessions, witness.tenant_id, witness.incident_id)
        assert disputed["outcome"] == "DISPUTED" and disputed["witness_sha256"] is None
        assert disputed["authorized_dispatch"] and disputed["effect_attempts"] == 1

    assert report["scope"] == "SIMULATOR_LAB_ONLY" and not report["promotion_eligible"]
    record_property("lab_e5_summary", json.dumps({
        "diagnosis": report["heldout_report"]["metrics"],
        "cost": report["recovery_cost_report"]["arms"],
    }))
    assert report["heldout_report"]["metrics"]["candidate_probe_cost_units"] < (
        report["heldout_report"]["metrics"]["incumbent_probe_cost_units"]
    )
    for arm, rows in report["trial_details"].items():
        assert report["recovery_cost_report"]["arms"][arm]["outcomes"] == {
            "DISPUTED": 0, "FAILED": 13, "UNKNOWN": 1, "VERIFIED": 22,
        }
        assert report["recovery_cost_report"]["arms"][arm]["component_microcredits"]["provider_microcredits"] == 2300
        by_id = {row["case_id"]: row for row in rows}
        assert by_id["fam-rec-0"]["receipt_state"] == "UNKNOWN"
        assert by_id["fam-rec-0"]["authorized_dispatch"]
        assert by_id["fam-rec-0"]["simulator_applied"]
        assert by_id["fam-rec-0"]["witness_sha256"] is None
        assert by_id["fam-rec-1"]["decision"] == "PROPOSE"
        assert by_id["fam-rec-1"]["approval_state"] == "REJECTED"
        assert not by_id["fam-rec-1"]["authorized_dispatch"]
        assert by_id["fam-rec-1"]["effect_attempts"] == 0
        assert by_id["fam-rec-2"]["authorized_dispatch"]
        assert by_id["fam-rec-2"]["witness_sha256"] is not None
        assert all(row["witness_sha256"] is None for row in rows if row["outcome"] != "VERIFIED")
