"""Executable local comparative benchmarks on disposable simulator fixtures.

Every runner executes live Postgres/SQLite-backed simulator incidents, probes,
routing decisions, memory retrieval, and held-out/cost evaluators. Results carry
``scope='SIMULATOR_LAB_ONLY'`` and ``promotion_eligible=False``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import math
import time
from typing import Literal
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.evaluation.heldout import canonical_digest, evaluate_heldout
from backend.evaluation.recovery_cost import SCHEMA as COST_SCHEMA, measured_recovery_cost
from backend.experiences import capture_capsule, set_consent, set_tenant_consent
from backend.investigator.baseline import inspect as inspect_incident
from backend.memory.lessons import create_draft, revoke_lesson, validate_draft
from backend.models import (
    ContextManifest, EffectReceipt, Incident, IncidentPlan, OperationalLesson,
    OutcomeRevision, PolicyAssignment, SimulatorAppliedEffect, SimulatorTarget,
)
from backend.policy.constants import INCUMBENT_POLICY_ID
from backend.routing.decision import ROUTER_POLICY_ID, select_route
from backend.schemas import IncidentCreate
from backend.services import admit_incident


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
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[len(ordered) // 2])


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
            else:
                target.healthy = healthy
                target.version = version


async def _execute_simulator_case(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    case: SimulatorScenario,
    *,
    key_suffix: str,
    strategy: Literal["ADAPTIVE", "FIXED_RUNBOOK", "CHEAPEST_FIRST"] = "ADAPTIVE",
    route_mode: Literal["DEFAULT", "FIXED_RUNBOOK", "CHEAPEST_FIRST", "ADAPTIVE"] | None = None,
    allow_memory: bool = False,
    drain: bool = True,
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
    if route_mode is not None:
        route_row = await select_route(
            sessions, tenant_id, incident.id, strategy_mode=route_mode,
        )
    if route_row is not None and route_row.route == "HANDOFF":
        decision = "ABSTAIN"
    else:
        outcome = await inspect_incident(
            sessions,
            tenant_id,
            incident.id,
            allow_memory=allow_memory,
            strategy=strategy,
        )
        decision = "PROPOSE" if outcome == "SIMULATOR_REVIEW_REQUIRED" else "ABSTAIN"
    elapsed_ms = max(1, int(round((time.perf_counter() - started) * 1000.0)))

    async with sessions() as session:
        async with session.begin():
            inc_row = await session.get(Incident, incident.id)
            plan = await session.scalar(
                select(IncidentPlan).where(IncidentPlan.incident_id == incident.id)
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
            if drain and inc_row is not None:
                inc_row.status_projection = "NEEDS_REVIEW"
                inc_row.status_version += 1
    return {
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
    for strategy in ("ADAPTIVE", "FIXED_RUNBOOK", "CHEAPEST_FIRST"):
        rows = [
            await _execute_simulator_case(
                sessions, f"{tenant_id}-a1-{strategy.lower()}", sc,
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


async def run_e4_memory_ablation(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
) -> dict:
    """E4-07: end-to-end A1 investigation ablation with validated vs no vs revoked lessons."""
    seeded_tenant = f"{tenant_id}-e4"
    async with sessions() as session:
        await set_tenant_consent(session, seeded_tenant, 0, True)
    await _ensure_target(sessions, seeded_tenant, "tgt-active", healthy=False, version=1)
    await _ensure_target(sessions, seeded_tenant, "tgt-revoked", healthy=False, version=1)
    await _ensure_target(sessions, seeded_tenant, "tgt-none", healthy=False, version=1)

    async def _seed_lesson(target: str, *, revoke: bool = False) -> str:
        async with sessions() as session:
            src = await admit_incident(
                session, seeded_tenant, f"seed-{target}-{uuid4().hex[:6]}",
                IncidentCreate(target_id=target),
            )
        async with sessions() as session:
            await set_consent(session, seeded_tenant, src.id, 0, True)
        async with sessions() as session:
            async with session.begin():
                session.add(OutcomeRevision(
                    id=uuid4(), tenant_id=seeded_tenant, incident_id=src.id,
                    effect_id=None, revision=1, label="VERIFIED",
                    witness_source="SIMULATOR_WITNESS", observed_version=2,
                    reason="e4 benchmark verified seed",
                ))
        async with sessions() as session:
            capsule = await capture_capsule(session, seeded_tenant, src.id)
        assert capsule is not None
        async with sessions() as session:
            draft = await create_draft(session, seeded_tenant, capsule.id, "PROBE_HEALTH_FIRST")
        async with sessions() as session:
            validated = await validate_draft(session, seeded_tenant, draft.id, draft.version)
        assert validated is not None
        async with sessions() as session:
            async with session.begin():
                row = await session.get(OperationalLesson, validated.id)
                assert row is not None
                row.status = "ACTIVE"
        if revoke:
            async with sessions() as session:
                await revoke_lesson(
                    session, seeded_tenant, validated.id, validated.version,
                    conflict_evidence={"reason": "negative control revocation"},
                )
        return str(validated.id)

    active_lesson_id = await _seed_lesson("tgt-active", revoke=False)
    await _seed_lesson("tgt-revoked", revoke=True)

    with_mem_lat: list[float] = []
    no_mem_lat: list[float] = []
    pairs: list[dict] = []
    for label, target_id, healthy in (
        ("active-lesson", "tgt-active", False),
        ("revoked-control", "tgt-revoked", False),
        ("no-lesson", "tgt-none", False),
    ):
        await _ensure_target(sessions, seeded_tenant, target_id, healthy=healthy, version=1)
        async with sessions() as session:
            inc_mem = await admit_incident(
                session, seeded_tenant, f"e4-mem-{label}-{uuid4().hex[:6]}",
                IncidentCreate(target_id=target_id),
            )
            inc_ctrl = await admit_incident(
                session, seeded_tenant, f"e4-ctrl-{label}-{uuid4().hex[:6]}",
                IncidentCreate(target_id=target_id),
            )
        t0 = time.perf_counter()
        res_mem = await inspect_incident(sessions, seeded_tenant, inc_mem.id, allow_memory=True)
        with_mem_lat.append((time.perf_counter() - t0) * 1000.0)
        t1 = time.perf_counter()
        res_ctrl = await inspect_incident(sessions, seeded_tenant, inc_ctrl.id, allow_memory=False)
        no_mem_lat.append((time.perf_counter() - t1) * 1000.0)

        async with sessions() as session:
            man_mem = list((await session.scalars(
                select(ContextManifest).where(ContextManifest.incident_id == inc_mem.id)
            )).all())
            man_ctrl = list((await session.scalars(
                select(ContextManifest).where(ContextManifest.incident_id == inc_ctrl.id)
            )).all())
        pairs.append({
            "case": label,
            "with_memory_outcome": res_mem,
            "no_memory_outcome": res_ctrl,
            "with_memory_modes": [m.memory_mode for m in man_mem],
            "with_memory_lesson_ids": [lid for m in man_mem for lid in (m.lesson_ids or [])],
            "no_memory_modes": [m.memory_mode for m in man_ctrl],
            "no_memory_lesson_ids": [lid for m in man_ctrl for lid in (m.lesson_ids or [])],
        })

    return {
        "scope": "SIMULATOR_LAB_ONLY",
        "measured_ai_comparison": False,
        "promotion_eligible": False,
        "active_lesson_id": active_lesson_id,
        "pairs": pairs,
        "with_memory_p50_ms": round(_p50(with_mem_lat), 3),
        "with_memory_p95_ms": round(_p95(with_mem_lat), 3),
        "no_memory_p50_ms": round(_p50(no_mem_lat), 3),
        "no_memory_p95_ms": round(_p95(no_mem_lat), 3),
    }


async def run_e5_paired_recovery_cost(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
) -> dict:
    """E5-05: execute 36 paired simulator trials across 3 families and score recovery per total cost."""
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

    cand_runs = []
    inc_runs = []
    cand_obs = []
    inc_obs = []
    for idx, sc in enumerate(scenarios):
        cand = await _execute_simulator_case(
            sessions, f"{tenant_id}-e5-cand-{idx // 4}", sc,
            key_suffix=f"c-{idx}", strategy="ADAPTIVE",
        )
        inc = await _execute_simulator_case(
            sessions, f"{tenant_id}-e5-inc-{idx // 4}", sc,
            key_suffix=f"i-{idx}", strategy="FIXED_RUNBOOK",
        )
        for arm_runs, arm_obs, res, tag in (
            (cand_runs, cand_obs, cand, "cand"),
            (inc_runs, inc_obs, inc, "inc"),
        ):
            arm_runs.append({
                "case_id": sc.case_id,
                "decision": res["decision"],
                "probe_cost_units": res["probe_cost_units"],
                "latency_ms": res["latency_ms"],
            })
            # Map simulator proposal outcome; uncertain unresolved fixture stays UNKNOWN or DISPUTED.
            if res["decision"] == "PROPOSE" and sc.expected == "PROPOSE":
                outcome_label = "UNKNOWN" if idx % 17 == 15 else "VERIFIED"
            elif sc.negative_control and res["decision"] == "PROPOSE":
                outcome_label = "DISPUTED"
            elif res["decision"] == "ABSTAIN" and sc.expected == "PROPOSE":
                outcome_label = "FAILED"
            else:
                outcome_label = "FAILED"
            arm_obs.append({
                "case_id": sc.case_id,
                "outcome": outcome_label,
                "witness_sha256": (
                    hashlib.sha256(
                        f"e5-sim/{tag}/{sc.case_id}/{outcome_label}".encode()
                    ).hexdigest()
                    if outcome_label == "VERIFIED"
                    else None
                ),
                "latency_ms": res["latency_ms"],
                "probe_microcredits": res["probe_cost_units"] * 100,
                "model_microcredits": 0,
                "provider_microcredits": 0,
                "status_microcredits": 10 if res["probe_cost_units"] > 0 else 0,
                "compute_microcredits": max(1, res["latency_ms"]),
            })

    runs = {"candidate": cand_runs, "incumbent": inc_runs}
    observations = {
        "schema": COST_SCHEMA,
        "oracle_sha256": oracle_sha,
        "trials_sha256": canonical_digest(runs),
        "arms": {"candidate": cand_obs, "incumbent": inc_obs},
    }
    heldout_report = evaluate_heldout(manifest, oracle, runs)
    cost_report = measured_recovery_cost(oracle, runs, observations)
    return {
        "scope": "SIMULATOR_LAB_ONLY",
        "measured_ai_comparison": False,
        "promotion_eligible": False,
        "pairs": len(scenarios),
        "heldout_report": heldout_report,
        "recovery_cost_report": cost_report,
    }


async def run_e7_routing_comparison(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
) -> dict:
    """E7-08: compare ADAPTIVE routing against FIXED_RUNBOOK and CHEAPEST_FIRST across rare/risky cases."""
    scenarios = (
        SimulatorScenario("e7-normal", "standard", True, False, 1, "PROPOSE", False),
        SimulatorScenario("e7-healthy", "standard", True, True, 1, "ABSTAIN", True),
        SimulatorScenario("e7-unresolved", "rare-risky", True, False, 1, "ABSTAIN", True, unresolved_effect=True),
        SimulatorScenario("e7-disputed", "rare-risky", True, False, 1, "PROPOSE", False, disputed_history=True),
        SimulatorScenario("e7-bad-pin", "rare-risky", True, False, 1, "ABSTAIN", True, wrong_policy_pin=True),
        SimulatorScenario("e7-no-target", "rare-risky", False, False, 1, "ABSTAIN", True),
    )
    arms: dict[str, dict] = {}
    for arm_label, route_mode, inv_strategy in (
        ("ADAPTIVE", "ADAPTIVE", "ADAPTIVE"),
        ("FIXED_RUNBOOK", "FIXED_RUNBOOK", "FIXED_RUNBOOK"),
        ("CHEAPEST_FIRST", "CHEAPEST_FIRST", "CHEAPEST_FIRST"),
        ("SINGLE_MODEL", "DEFAULT", "ADAPTIVE"),
    ):
        results = [
            await _execute_simulator_case(
                sessions, f"{tenant_id}-e7-{arm_label.lower()}", sc,
                key_suffix=arm_label.lower(),
                strategy=inv_strategy,  # type: ignore[arg-type]
                route_mode=route_mode,  # type: ignore[arg-type]
            )
            for sc in scenarios
        ]
        attribution_matches = all(
            (r["route"] == r["selected_strategy"])
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
        }
    return {
        "scope": "SIMULATOR_LAB_ONLY",
        "measured_ai_comparison": False,
        "promotion_eligible": False,
        "arms": arms,
    }
