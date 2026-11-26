"""Executable local comparative benchmarks on disposable simulator fixtures.

Every runner executes live Postgres/SQLite-backed simulator incidents, probes,
routing decisions, memory retrieval, and held-out/cost evaluators. Results carry
``scope='SIMULATOR_LAB_ONLY'`` and ``promotion_eligible=False``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import math
from statistics import median
import time
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.evaluation.heldout import canonical_digest, evaluate_heldout
from backend.evaluation.recovery_cost import SCHEMA as COST_SCHEMA, measured_recovery_cost
from backend.experiences import capture_capsule, set_consent, set_tenant_consent
from backend.investigator.baseline import inspect as inspect_incident
from backend.memory.lessons import create_draft, evaluate_memory_vs_no_memory, revoke_lesson, validate_draft
from backend.models import (
    ApprovalGrant, ContextManifest, EffectReceipt, Incident, IncidentPlan, OperationalLesson,
    OutcomeRevision, PolicyAssignment, ProbeCall, ResourceEpoch, SimulatorAppliedEffect, SimulatorTarget,
)
from backend.policy.constants import INCUMBENT_POLICY_ID
from backend.routing.decision import ROUTER_POLICY_ID, pinned_runbook_strategy, select_route
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import get_settings


Strategy = Literal["ADAPTIVE", "FIXED_RUNBOOK", "CHEAPEST_FIRST"]
DriveTrial = Callable[[str, UUID, Strategy], Awaitable[None]]


@dataclass(frozen=True)
class SimulatorScenario:
    case_id: str
    family: str
    create_target: bool
    healthy: bool
    version: int
    expected: Literal["PROPOSE", "ABSTAIN"]
    negative_control: bool
    unresolved_effect: bool = False
    disputed_history: bool = False
    wrong_policy_pin: bool = False


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[math.ceil(0.95 * len(ordered)) - 1])


def _p50(values: list[float]) -> float:
    return float(median(values)) if values else 0.0


async def _ensure_target(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    target_id: str,
    *,
    healthy: bool,
    version: int,
) -> None:
    async with sessions() as session:
        async with session.begin():
            target = await session.get(SimulatorTarget, (tenant_id, target_id))
            if target is None:
                session.add(
                    SimulatorTarget(
                        tenant_id=tenant_id,
                        target_id=target_id,
                        version=version,
                        healthy=healthy,
                        last_fence=0,
                    )
                )
                session.add(ResourceEpoch(
                    tenant_id=tenant_id, target_id=target_id, latest_fence=0, version_seen=version,
                ))
            else:
                target.healthy = healthy
                target.version = version


async def _execute_simulator_case(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    case: SimulatorScenario,
    *,
    key_suffix: str,
    strategy: Strategy = "ADAPTIVE",
    route_mode: Literal["DEFAULT", "FIXED_RUNBOOK", "CHEAPEST_FIRST", "ADAPTIVE"] | None = None,
    allow_memory: bool = False,
    drain: bool = True,
    drive_trial: DriveTrial | None = None,
) -> dict:
    target_id = f"{case.case_id}-{key_suffix}"
    if case.create_target:
        await _ensure_target(
            sessions, tenant_id, target_id, healthy=case.healthy, version=case.version,
        )

    async with sessions() as session:
        incident = await admit_incident(
            session, tenant_id, f"{case.case_id}-{key_suffix}-{uuid4().hex[:8]}",
            IncidentCreate(target_id=target_id),
        )
    if case.wrong_policy_pin:
        async with sessions() as session:
            async with session.begin():
                pin = await session.get(PolicyAssignment, (tenant_id, incident.id))
                if pin is not None:
                    pin.policy_id = "unapproved-policy-v0"
    if case.unresolved_effect:
        async with sessions() as session:
            async with session.begin():
                eid = uuid4()
                session.add(EffectReceipt(
                    id=eid, tenant_id=tenant_id, incident_id=incident.id,
                    plan_digest="d" * 64, target_id=target_id,
                    adapter_id="simulator", idempotency_key=str(eid),
                    fence=1, expected_version=case.version, state="UNKNOWN", attempt_count=1,
                ))
    if case.disputed_history:
        async with sessions() as session:
            prior_inc = await admit_incident(
                session, tenant_id, f"disp-{uuid4().hex[:8]}",
                IncidentCreate(target_id=target_id),
            )
        async with sessions() as session:
            async with session.begin():
                prior_row = await session.get(Incident, prior_inc.id)
                if drain and prior_row is not None:
                    prior_row.status_projection = "NEEDS_REVIEW"
                session.add(OutcomeRevision(
                    id=uuid4(), tenant_id=tenant_id, incident_id=prior_inc.id,
                    effect_id=None, revision=1, label="DISPUTED",
                    witness_source="SIMULATOR_WITNESS", observed_version=case.version,
                    reason="disputed simulator fixture",
                ))

    started = time.perf_counter()
    route_row = None
    executed_strategy = None
    if route_mode is not None:
        route_row = await select_route(
            sessions, tenant_id, incident.id, strategy_mode=route_mode,
        )
    if drive_trial is not None:
        await drive_trial(tenant_id, incident.id, strategy)
        decision = None
    elif route_row is not None and route_row.route == "HANDOFF":
        decision = "ABSTAIN"
    else:
        if route_row is not None:
            if route_row.route == "RUNBOOK":
                strategy = pinned_runbook_strategy(route_row)
            elif route_row.route in {"ADAPTIVE", "CHEAPEST_FIRST"}:
                strategy = route_row.route
            else:
                raise ValueError("local comparison cannot execute the selected route")
        outcome = await inspect_incident(
            sessions,
            tenant_id,
            incident.id,
            allow_memory=allow_memory,
            strategy=strategy,
        )
        executed_strategy = strategy
        decision = "PROPOSE" if outcome == "SIMULATOR_REVIEW_REQUIRED" else "ABSTAIN"
    elapsed_ms = max(1, int(round((time.perf_counter() - started) * 1000.0)))

    async with sessions() as session:
        async with session.begin():
            inc_row = await session.get(Incident, incident.id)
            plan = await session.scalar(
                select(IncidentPlan).where(
                    IncidentPlan.tenant_id == tenant_id, IncidentPlan.incident_id == incident.id,
                )
            )
            manifests = list((await session.scalars(
                select(ContextManifest)
                .where(ContextManifest.incident_id == incident.id)
                .order_by(ContextManifest.step_no)
            )).all())
            applied = int((await session.scalar(
                select(func.count(SimulatorAppliedEffect.id)).where(
                    SimulatorAppliedEffect.tenant_id == tenant_id,
                    SimulatorAppliedEffect.target_id == target_id,
                )
            )) or 0)
            probes = inc_row.probe_used if inc_row is not None else 0
            if drain and drive_trial is None and inc_row is not None:
                inc_row.status_projection = "NEEDS_REVIEW"
                inc_row.status_version += 1
    if decision is None:
        decision = "PROPOSE" if plan is not None else "ABSTAIN"
    return {
        "incident_id": incident.id,
        "case_id": case.case_id,
        "family": case.family,
        "decision": decision,
        "expected": case.expected,
        "correct": decision == case.expected,
        "unsafe": decision == "PROPOSE" and case.expected == "ABSTAIN",
        "probe_cost_units": probes,
        "latency_ms": elapsed_ms,
        "has_plan": plan is not None,
        "target_mutations": applied,
        "route": route_row.route if route_row is not None else None,
        "policy_id": route_row.policy_id if route_row is not None else None,
        "executed_strategy": executed_strategy,
        "selected_strategy": (
            (route_row.context_features or {}).get("selected_strategy")
            if route_row is not None
            else strategy
        ),
        "manifests": [
            {"memory_mode": m.memory_mode, "lesson_ids": list(m.lesson_ids or [])}
            for m in manifests
        ],
    }


async def run_a1_strategy_comparison(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
) -> dict:
    """A1-10: repeatable local simulator comparison of ADAPTIVE vs FIXED_RUNBOOK vs CHEAPEST_FIRST."""
    scenarios = (
        SimulatorScenario("a1-unhealthy-1", "recoverable", True, False, 1, "PROPOSE", False),
        SimulatorScenario("a1-unhealthy-2", "recoverable", True, False, 2, "PROPOSE", False),
        SimulatorScenario("a1-healthy-1", "already-healthy", True, True, 1, "ABSTAIN", True),
        SimulatorScenario("a1-healthy-2", "already-healthy", True, True, 2, "ABSTAIN", True),
        SimulatorScenario("a1-missing-1", "missing-target", False, False, 1, "ABSTAIN", True),
        SimulatorScenario("a1-unhealthy-3", "recoverable", True, False, 3, "PROPOSE", False),
    )
    strategies: dict[str, dict] = {}
    prefix = "a1-" + canonical_digest(tenant_id)[:10] + "-" + uuid4().hex[:10]
    for strategy in ("ADAPTIVE", "FIXED_RUNBOOK", "CHEAPEST_FIRST"):
        rows = [
            await _execute_simulator_case(
                sessions, f"{prefix}-{strategy.lower()}", sc,
                key_suffix=strategy.lower(), strategy=strategy,  # type: ignore[arg-type]
            )
            for sc in scenarios
        ]
        latencies = [float(r["latency_ms"]) for r in rows]
        strategies[strategy] = {
            "cases": len(rows),
            "correct": sum(1 for r in rows if r["correct"]),
            "accuracy": round(sum(1 for r in rows if r["correct"]) / len(rows), 4),
            "unsafe_proposals": sum(1 for r in rows if r["unsafe"]),
            "total_probes": sum(r["probe_cost_units"] for r in rows),
            "p50_latency_ms": round(_p50(latencies), 3),
            "p95_latency_ms": round(_p95(latencies), 3),
            "target_mutations": sum(r["target_mutations"] for r in rows),
        }
    return {
        "scope": "SIMULATOR_LAB_ONLY",
        "measured_ai_comparison": False,
        "promotion_eligible": False,
        "strategies": strategies,
    }


E4_DEVELOPMENT_SOURCES = (
    ("tgt-active", "ACTIVE"), ("tgt-healthy", "ACTIVE"),
    ("tgt-revoked", "REVOKED"), ("tgt-expired", "EXPIRED"),
)
E4_EVALUATION_CASES = (
    ("active-lesson", "tgt-active", "SIMULATOR_REVIEW_REQUIRED"),
    ("revoked-control", "tgt-revoked", "SIMULATOR_REVIEW_REQUIRED"),
    ("expired-control", "tgt-expired", "SIMULATOR_REVIEW_REQUIRED"),
    ("no-lesson", "tgt-none", "SIMULATOR_REVIEW_REQUIRED"),
    ("healthy-with-lesson", "tgt-healthy", "ABSTAINED"),
    ("missing-target", "tgt-missing", "ABSTAINED"),
)
E4_LOOKUP_ITERATIONS = 6
E4_DEVELOPMENT_RULE = "PROBE_HEALTH_FIRST"


async def run_e4_memory_ablation(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
) -> dict:
    """E4-07: evaluate cited memory against isolated no-memory and safety controls."""
    if get_settings().environment != "development":
        raise ValueError("E4 local memory fixtures require development settings")
    design_sha256 = canonical_digest({
        "development_sources": E4_DEVELOPMENT_SOURCES, "development_rule": E4_DEVELOPMENT_RULE,
        "evaluation_cases": E4_EVALUATION_CASES, "lookup_iterations": E4_LOOKUP_ITERATIONS,
        "arm_order": "ALTERNATING_MEMORY_FIRST", "healthy_control_version": 2,
    })
    seeded_tenant = "e4-" + canonical_digest(tenant_id)[:10] + "-" + uuid4().hex[:10]
    async with sessions() as session:
        await set_tenant_consent(session, seeded_tenant, 0, True)
    for target in ("tgt-active", "tgt-revoked", "tgt-expired", "tgt-healthy", "tgt-none"):
        await _ensure_target(sessions, seeded_tenant, target, healthy=False, version=1)

    async def _seed_lesson(target: str, *, state: Literal["ACTIVE", "REVOKED", "EXPIRED"] = "ACTIVE") -> str:
        async with sessions() as session:
            src = await admit_incident(
                session, seeded_tenant, f"seed-{target}-{uuid4().hex[:6]}",
                IncidentCreate(target_id=target),
            )
        async with sessions() as session:
            await set_consent(session, seeded_tenant, src.id, 0, True)
        if await inspect_incident(sessions, seeded_tenant, src.id, allow_memory=False) != "SIMULATOR_REVIEW_REQUIRED":
            raise RuntimeError("E4 fixture source investigation failed")
        async with sessions() as session:
            async with session.begin():
                session.add(OutcomeRevision(
                    id=uuid4(), tenant_id=seeded_tenant, incident_id=src.id,
                    effect_id=None, revision=1, label="VERIFIED",
                    witness_source="LAB_FIXTURE", observed_version=None,
                    reason="synthetic prerequisite for retrieval ablation",
                ))
        async with sessions() as session:
            capsule = await capture_capsule(session, seeded_tenant, src.id)
        assert capsule is not None
        async with sessions() as session:
            draft = await create_draft(session, seeded_tenant, capsule.id, E4_DEVELOPMENT_RULE)
        async with sessions() as session:
            validated = await validate_draft(session, seeded_tenant, draft.id, draft.version)
        assert validated is not None
        async with sessions() as session:
            async with session.begin():
                row = await session.get(OperationalLesson, validated.id)
                assert row is not None
                # Seed the retrieval fixture without activating a release policy.
                row.status = "ACTIVE"
                if state == "EXPIRED":
                    row.valid_from = datetime.now(UTC) - timedelta(days=2)
                    row.valid_until = datetime.now(UTC) - timedelta(days=1)
        if state == "REVOKED":
            async with sessions() as session:
                await revoke_lesson(
                    session, seeded_tenant, validated.id, validated.version,
                    conflict_evidence={"reason": "negative control revocation"},
                )
        return str(validated.id)

    seeded_lessons = {target: await _seed_lesson(target, state=state) for target, state in E4_DEVELOPMENT_SOURCES}
    await _ensure_target(sessions, seeded_tenant, "tgt-healthy", healthy=True, version=2)

    with_mem_lat: list[float] = []
    no_mem_lat: list[float] = []
    pairs: list[dict] = []
    for index, (label, target_id, expected) in enumerate(E4_EVALUATION_CASES):
        async with sessions() as session:
            inc_mem = await admit_incident(
                session, seeded_tenant, f"e4-mem-{label}-{uuid4().hex[:6]}",
                IncidentCreate(target_id=target_id),
            )
            inc_ctrl = await admit_incident(
                session, seeded_tenant, f"e4-ctrl-{label}-{uuid4().hex[:6]}",
                IncidentCreate(target_id=target_id),
            )
        outcomes = {}
        arms = ((inc_mem, True, with_mem_lat), (inc_ctrl, False, no_mem_lat))
        for incident, use_memory, times in (arms if index % 2 == 0 else reversed(arms)):
            started = time.perf_counter()
            outcomes[use_memory] = await inspect_incident(sessions, seeded_tenant, incident.id, allow_memory=use_memory)
            times.append((time.perf_counter() - started) * 1000.0)

        async with sessions() as session:
            man_mem = list((await session.scalars(
                select(ContextManifest).where(ContextManifest.incident_id == inc_mem.id).order_by(ContextManifest.step_no)
            )).all())
            man_ctrl = list((await session.scalars(
                select(ContextManifest).where(ContextManifest.incident_id == inc_ctrl.id).order_by(ContextManifest.step_no)
            )).all())
            probes = dict((await session.execute(select(ProbeCall.incident_id, func.count()).where(
                ProbeCall.incident_id.in_((inc_mem.id, inc_ctrl.id)),
            ).group_by(ProbeCall.incident_id))).all())
            effects = dict((await session.execute(select(EffectReceipt.incident_id, func.sum(EffectReceipt.attempt_count)).where(
                EffectReceipt.tenant_id == seeded_tenant,
                EffectReceipt.incident_id.in_((inc_mem.id, inc_ctrl.id)),
                EffectReceipt.attempt_count > 0,
            ).group_by(EffectReceipt.incident_id))).all())
            retrieval = await evaluate_memory_vs_no_memory(
                session, seeded_tenant, target_id, iterations=E4_LOOKUP_ITERATIONS,
            )
        pairs.append({
            "case": label,
            "expected_outcome": expected,
            "with_memory_outcome": outcomes[True],
            "no_memory_outcome": outcomes[False],
            "with_memory_probe_calls": probes.get(inc_mem.id, 0),
            "no_memory_probe_calls": probes.get(inc_ctrl.id, 0),
            "with_memory_effect_attempts": effects.get(inc_mem.id, 0),
            "no_memory_effect_attempts": effects.get(inc_ctrl.id, 0),
            "with_memory_modes": [m.memory_mode for m in man_mem],
            "with_memory_lesson_ids": [lid for m in man_mem for lid in (m.lesson_ids or [])],
            "no_memory_modes": [m.memory_mode for m in man_ctrl],
            "no_memory_lesson_ids": [lid for m in man_ctrl for lid in (m.lesson_ids or [])],
            "retrieval": retrieval,
        })

    return {
        "scope": "SIMULATOR_LAB_ONLY",
        "measured_ai_comparison": False,
        "promotion_eligible": False,
        "design_sha256": design_sha256,
        "active_lesson_id": seeded_lessons["tgt-active"],
        "pairs": pairs,
        "decision_counts": {
            **{f"{arm}_correct": sum(row[f"{arm}_outcome"] == row["expected_outcome"] for row in pairs)
               for arm in ("with_memory", "no_memory")},
            **{f"{arm}_unsafe": sum(row[f"{arm}_outcome"] == "SIMULATOR_REVIEW_REQUIRED"
                                   and row["expected_outcome"] == "ABSTAINED" for row in pairs)
               for arm in ("with_memory", "no_memory")},
        },
        "probe_counts": {
            "with_memory": sum(row["with_memory_probe_calls"] for row in pairs),
            "no_memory": sum(row["no_memory_probe_calls"] for row in pairs),
        },
        "with_memory_p50_ms": round(_p50(with_mem_lat), 3),
        "with_memory_p95_ms": round(_p95(with_mem_lat), 3),
        "no_memory_p50_ms": round(_p50(no_mem_lat), 3),
        "no_memory_p95_ms": round(_p95(no_mem_lat), 3),
    }


async def _e5_recorded_recovery(
    sessions: async_sessionmaker[AsyncSession], tenant_id: str, incident_id: UUID,
) -> dict:
    async with sessions() as session:
        latest_revision = select(func.max(OutcomeRevision.revision)).where(
            OutcomeRevision.tenant_id == tenant_id, OutcomeRevision.incident_id == incident_id,
        ).scalar_subquery()
        plan, grant, receipt, applied, witness = (await session.execute(
            select(IncidentPlan, ApprovalGrant, EffectReceipt, SimulatorAppliedEffect, OutcomeRevision)
            .select_from(Incident)
            .outerjoin(IncidentPlan, and_(IncidentPlan.tenant_id == Incident.tenant_id,
                                         IncidentPlan.incident_id == Incident.id))
            .outerjoin(ApprovalGrant, and_(ApprovalGrant.tenant_id == Incident.tenant_id,
                                           ApprovalGrant.plan_id == IncidentPlan.id))
            .outerjoin(EffectReceipt, and_(
                EffectReceipt.tenant_id == Incident.tenant_id, EffectReceipt.incident_id == Incident.id,
                EffectReceipt.plan_digest == IncidentPlan.plan_digest, EffectReceipt.target_id == IncidentPlan.target_id,
                EffectReceipt.adapter_id == "simulator",
            ))
            .outerjoin(SimulatorAppliedEffect, SimulatorAppliedEffect.id == EffectReceipt.id)
            .outerjoin(OutcomeRevision, and_(
                OutcomeRevision.tenant_id == Incident.tenant_id, OutcomeRevision.incident_id == Incident.id,
                OutcomeRevision.revision == latest_revision,
            ))
            .where(Incident.tenant_id == tenant_id, Incident.id == incident_id)
        )).one()

    dispatched = bool(
        plan and grant and receipt and plan.action_id == grant.scope == "SIMULATOR_MARK_HEALTHY"
        and plan.status == "APPROVED" and plan.version == 2
        and grant.decision == "APPROVE" and grant.state == "CONSUMED"
        and grant.consumed_at is not None
        and grant.plan_digest == plan.plan_digest and receipt.attempt_count == 1
        and receipt.expected_version == plan.expected_version and receipt.permit_digest
    )
    matching_witness = witness is not None and receipt is not None and witness.effect_id == receipt.id
    label = "FAILED"
    witness_digest = None
    if matching_witness and witness.label == "DISPUTED":
        label = "DISPUTED"
    elif receipt and receipt.state in {"PREPARED", "SENT", "UNKNOWN", "CONFIRMED"}:
        label = "UNKNOWN"
    elif dispatched and receipt.state == "VERIFIED":
        label = "UNKNOWN"
        if (
            matching_witness and witness.label == "VERIFIED" and witness.witness_source == "SIMULATOR_WITNESS"
            and applied and applied.tenant_id == tenant_id and applied.target_id == plan.target_id
            and applied.permit_digest == receipt.permit_digest
            and applied.observed_version == receipt.observed_version == witness.observed_version == plan.expected_version + 1
        ):
            label = "VERIFIED"
            witness_digest = canonical_digest({
                "incident_id": str(incident_id), "effect_id": str(receipt.id),
                "witness_id": str(witness.id), "revision": witness.revision,
                "source": witness.witness_source, "recorded_at": witness.created_at.isoformat(),
                "observed_version": witness.observed_version, "permit_digest": receipt.permit_digest,
            })
    return {
        "outcome": label, "witness_sha256": witness_digest, "authorized_dispatch": dispatched,
        "approval_state": grant.state if grant else None,
        "receipt_state": receipt.state if receipt else None,
        "effect_attempts": receipt.attempt_count if receipt else 0,
        "outcome_recorded": matching_witness,
        "simulator_applied": bool(applied and receipt and applied.tenant_id == tenant_id
                                  and applied.target_id == receipt.target_id
                                  and applied.permit_digest == receipt.permit_digest),
    }


async def run_e5_paired_recovery_cost(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    *,
    drive_trial: DriveTrial | None = None,
) -> dict:
    """Score 36 paired lab trials. Without a workflow driver, investigate only."""
    settings = get_settings()
    if settings.environment != "development" or settings.ai_advisory_enabled:
        raise ValueError("E5 local pairs require development settings with advisory disabled")
    scenarios: list[SimulatorScenario] = []
    # 3 disjoint families x 12 cases = 36 pairs; 12 negative controls (>=10, >=10%)
    for i in range(12):
        scenarios.append(SimulatorScenario(
            f"fam-rec-{i}", "sim-recoverable", True, i >= 8, 1 + (i % 3),
            "ABSTAIN" if i >= 8 else "PROPOSE", i >= 8,
        ))
        scenarios.append(SimulatorScenario(
            f"fam-healthy-{i}", "sim-healthy-mix", True, i >= 8, 2 + (i % 2),
            "ABSTAIN" if i >= 8 else "PROPOSE", i >= 8,
        ))
        scenarios.append(SimulatorScenario(
            f"fam-missing-{i}", "sim-missing-mix", i < 8, False, 1,
            "PROPOSE" if i < 8 else "ABSTAIN", i >= 8,
        ))

    oracle_cases = [
        {
            "case_id": sc.case_id,
            "family": sc.family,
            "expected": sc.expected,
            "negative_control": sc.negative_control,
        }
        for sc in scenarios
    ]
    oracle = {"cases": oracle_cases}
    oracle_sha = canonical_digest(oracle)
    manifest = {
        "version": 1,
        "oracle_sha256": oracle_sha,
        "min_pairs": 36,
        "training_families": ["dev-only-family"],
    }

    runs = {"candidate": [], "incumbent": []}
    arm_observations = {"candidate": [], "incumbent": []}
    details = {"candidate": [], "incumbent": []}
    prefix = "e5-" + canonical_digest(tenant_id)[:10] + "-" + uuid4().hex[:10]
    strategies: tuple[tuple[str, Strategy], ...] = (
        ("candidate", "ADAPTIVE"), ("incumbent", "FIXED_RUNBOOK"),
    )
    for idx, sc in enumerate(scenarios):
        # Alternate execution order while preserving identical fixtures in each arm.
        for arm, strategy in (strategies if idx % 2 == 0 else reversed(strategies)):
            tenant = f"{prefix}-{arm}-{idx // 4}"
            res = await _execute_simulator_case(
                sessions, tenant, sc, key_suffix=str(arm), strategy=strategy,
                drive_trial=drive_trial,
            )
            recovery = await _e5_recorded_recovery(sessions, tenant, res["incident_id"])
            runs[arm].append({
                "case_id": sc.case_id,
                "decision": res["decision"],
                "probe_cost_units": res["probe_cost_units"],
                "latency_ms": res["latency_ms"],
            })
            details[arm].append({
                "case_id": sc.case_id, "diagnosis_correct": res["correct"],
                "decision": res["decision"], "requested_strategy": strategy,
                "probe_calls": res["probe_cost_units"], "elapsed_ms": res["latency_ms"],
                **recovery,
            })
            arm_observations[arm].append({
                "case_id": sc.case_id,
                "outcome": recovery["outcome"],
                "witness_sha256": recovery["witness_sha256"],
                "latency_ms": res["latency_ms"],
                "probe_microcredits": res["probe_cost_units"] * 100,
                "model_microcredits": 0,
                "provider_microcredits": recovery["effect_attempts"] * 100,
                "status_microcredits": 10 if recovery["outcome_recorded"] else 0,
                "compute_microcredits": max(1, res["latency_ms"]),
            })

    observations = {
        "schema": COST_SCHEMA,
        "oracle_sha256": oracle_sha,
        "trials_sha256": canonical_digest(runs),
        "arms": arm_observations,
    }
    heldout_report = evaluate_heldout(manifest, oracle, runs)
    cost_report = measured_recovery_cost(oracle, runs, observations)
    return {
        "scope": "SIMULATOR_LAB_ONLY",
        "measured_ai_comparison": False,
        "promotion_eligible": False,
        "pairs": len(scenarios),
        "trial_details": details,
        "cost_basis": "CONFIGURED_SYNTHETIC_RATES_ON_OBSERVED_LAB_COUNTS_AND_ELAPSED_TIME",
        "heldout_report": heldout_report,
        "recovery_cost_report": cost_report,
    }


async def run_e7_routing_comparison(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
) -> dict:
    """E7-08: compare ADAPTIVE routing against FIXED_RUNBOOK and CHEAPEST_FIRST across rare/risky cases."""
    if get_settings().ai_advisory_enabled:
        raise ValueError("E7 deterministic comparisons require advisory disabled")
    scenarios = (
        SimulatorScenario("e7-normal", "standard", True, False, 1, "PROPOSE", False),
        SimulatorScenario("e7-healthy", "standard", True, True, 1, "ABSTAIN", True),
        SimulatorScenario("e7-unresolved", "rare-risky", True, False, 1, "ABSTAIN", True, unresolved_effect=True),
        SimulatorScenario("e7-disputed", "rare-risky", True, False, 1, "PROPOSE", False, disputed_history=True),
        SimulatorScenario("e7-bad-pin", "rare-risky", True, False, 1, "ABSTAIN", True, wrong_policy_pin=True),
        SimulatorScenario("e7-no-target", "rare-risky", False, False, 1, "ABSTAIN", True),
    )
    arms: dict[str, dict] = {}
    prefix = "e7-" + canonical_digest(tenant_id)[:10] + "-" + uuid4().hex[:10]
    for arm_label, route_mode in (
        ("ADAPTIVE", "ADAPTIVE"), ("FIXED_RUNBOOK", "FIXED_RUNBOOK"),
        ("CHEAPEST_FIRST", "CHEAPEST_FIRST"), ("DEFAULT_RUNBOOK", "DEFAULT"),
    ):
        results = [
            await _execute_simulator_case(
                sessions, f"{prefix}-{arm_label.lower()}", sc,
                key_suffix=arm_label.lower(),
                route_mode=route_mode,  # type: ignore[arg-type]
            )
            for sc in scenarios
        ]
        attribution_matches = all(
            (r["executed_strategy"] == {"HANDOFF": None, "RUNBOOK": "ADAPTIVE"}.get(
                r["selected_strategy"], r["selected_strategy"],
            )) and (r["executed_strategy"] is not None or r["probe_cost_units"] == 0)
            for r in results
        )
        policy_pins_valid = all(r["policy_id"] == ROUTER_POLICY_ID == INCUMBENT_POLICY_ID for r in results)
        arms[arm_label] = {
            "cases": len(results),
            "attribution_matches": attribution_matches,
            "policy_pins_valid": policy_pins_valid,
            "correct": sum(1 for r in results if r["correct"]),
            "unsafe_proposals": sum(1 for r in results if r["unsafe"]),
            "total_probes": sum(r["probe_cost_units"] for r in results),
            "route_counts": dict(Counter(r["route"] for r in results)),
            "executed_strategy_counts": dict(Counter(
                r["executed_strategy"] or "NO_INVESTIGATION" for r in results
            )),
        }
    return {
        "scope": "SIMULATOR_LAB_ONLY",
        "measured_ai_comparison": False,
        "promotion_eligible": False,
        "arms": arms,
    }
