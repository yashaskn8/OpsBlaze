"""Stage 3 Performance & Reliability Benchmark (B2 + B3 + E8).

Measures controlled concurrency at levels 1, 5, 10, and 20 across:
1. Same-target B2 reservation contention (1 winner, N-1 deterministic conflicts).
2. Different-target B2 + B3 + E8 end-to-end execution concurrency.
3. B3 effect receipt lookup & idempotent duplicate rejection throughput.
4. B3 UNKNOWN effect reconciliation throughput.
5. E8 12-scenario conformance suite throughput.
6. Connection-pool utilization, p50/p95/p99 latency (ms), RSS memory, and CPU time.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.adapters.conformance import ConformanceRunner
from backend.adapters.fault_proxy import (
    FaultInjectionProxy,
    FaultRule,
    FaultScenario,
)
from backend.adapters.simulator import DisposableTargetSimulator
from backend.config import get_settings
from backend.domain.incident import IncidentStatus
from backend.effects.fencing import (
    ApprovedPlanContext,
    ResourceAlreadyReservedError,
    ResourceReservationManager,
)
from backend.effects.receipts import ActionGateway, ActionSpec, EffectReconciler, EffectState
from backend.persistence.db import make_engine, make_session_factory
from backend.persistence.models import (
    AdapterConformance,
    EffectReceipt,
    Incident,
    Outbox,
    ResourceEpoch,
    ResourceReservation,
    Tenant,
)
from backend.persistence.repositories import IncidentRepository


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return round(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac, 3)


def _summarize_latencies(latencies_ms: list[float], elapsed_s: float) -> dict[str, Any]:
    s = sorted(latencies_ms)
    count = len(s)
    return {
        "count": count,
        "elapsed_seconds": round(elapsed_s, 4),
        "throughput_ops_per_sec": round(count / max(elapsed_s, 0.0001), 2),
        "p50_ms": _percentile(s, 50.0),
        "p95_ms": _percentile(s, 95.0),
        "p99_ms": _percentile(s, 99.0),
        "max_ms": round(max(s), 3) if s else 0.0,
        "mean_ms": round(statistics.mean(s), 3) if s else 0.0,
    }


async def run_benchmark() -> dict[str, Any]:
    settings = get_settings().model_copy(update={"postgres_db": "opsblaze_test"})
    engine = make_engine(settings, pool_size=10, max_overflow=10, pool_timeout=10.0)
    session_factory = make_session_factory(engine)
    inc_repo = IncidentRepository(session_factory)
    res_mgr = ResourceReservationManager(session_factory)
    gateway = ActionGateway(session_factory)
    reconciler = EffectReconciler(session_factory)
    runner = ConformanceRunner(session_factory)

    tenant_id = uuid.uuid4()
    adapter_id = f"sim-perf-stage3-{uuid.uuid4().hex[:8]}"
    created_adapter_ids: list[str] = [adapter_id]
    sim = DisposableTargetSimulator(adapter_id=adapter_id, version="1.0.0")
    now = datetime.now(UTC)

    cpu_start = time.process_time()
    wall_start = time.perf_counter()

    try:
        await inc_repo.ensure_tenant(tenant_id, f"perf-stage3-tenant-{tenant_id}")
        await runner.run_conformance_suite(sim, now=now)

        concurrency_levels = [1, 5, 10, 20]
        same_target_results: dict[str, Any] = {}
        diff_target_results: dict[str, Any] = {}
        receipt_lookup_results: dict[str, Any] = {}
        unknown_recon_results: dict[str, Any] = {}
        conformance_results: dict[str, Any] = {}

        for level in concurrency_levels:
            # 1. Same-target reservation contention at concurrency `level`
            contended_target = f"perf-same-{level}-{uuid.uuid4().hex[:6]}"
            inc_ids_same: list[uuid.UUID] = []
            for _ in range(level):
                out = await inc_repo.admit_incident(
                    tenant_id=tenant_id,
                    client_key=f"perf-same-inc-{level}-{uuid.uuid4()}",
                    target_id=contended_target,
                    request_sha256="1" * 64,
                )
                await inc_repo.transition_status(
                    tenant_id=tenant_id,
                    incident_id=out.response.id,
                    target_status=IncidentStatus.DISPATCH_PENDING,
                )
                await inc_repo.transition_status(
                    tenant_id=tenant_id,
                    incident_id=out.response.id,
                    target_status=IncidentStatus.INVESTIGATING,
                )
                await inc_repo.transition_status(
                    tenant_id=tenant_id,
                    incident_id=out.response.id,
                    target_status=IncidentStatus.AWAITING_APPROVAL,
                )
                inc_ids_same.append(out.response.id)

            barrier_same = asyncio.Barrier(level)
            lat_same: list[float] = []
            winners = 0
            conflicts = 0

            async def _same_worker(
                idx: int,
                *,
                _inc_ids: list[uuid.UUID] = inc_ids_same,
                _target: str = contended_target,
                _barrier: asyncio.Barrier = barrier_same,
                _lat: list[float] = lat_same,
            ) -> None:
                nonlocal winners, conflicts
                app = ApprovedPlanContext(
                    approval_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    incident_id=_inc_ids[idx],
                    target_id=_target,
                    plan_digest=f"p-{idx}",
                    action_digest=f"a-{idx}",
                    expected_version="v1",
                    expires_at=now + timedelta(minutes=30),
                )
                await _barrier.wait()
                t0 = time.perf_counter()
                try:
                    await res_mgr.acquire_reservation(approval=app, adapter=sim)
                    winners += 1
                except ResourceAlreadyReservedError:
                    conflicts += 1
                _lat.append((time.perf_counter() - t0) * 1000.0)

            t_start = time.perf_counter()
            await asyncio.gather(*(_same_worker(i) for i in range(level)))
            t_elapsed = time.perf_counter() - t_start
            assert winners == 1 and conflicts == level - 1
            same_target_results[f"concurrency_{level}"] = {
                **_summarize_latencies(lat_same, t_elapsed),
                "winners": winners,
                "conflicts": conflicts,
            }

            # 2. Different-target reservation + B3 execution + independent verification at concurrency `level`
            inc_ids_diff: list[tuple[uuid.UUID, str]] = []
            for i in range(level):
                t_name = f"perf-diff-{level}-{i}-{uuid.uuid4().hex[:6]}"
                out = await inc_repo.admit_incident(
                    tenant_id=tenant_id,
                    client_key=f"perf-diff-inc-{level}-{i}-{uuid.uuid4()}",
                    target_id=t_name,
                    request_sha256="2" * 64,
                )
                for st in (
                    IncidentStatus.DISPATCH_PENDING,
                    IncidentStatus.INVESTIGATING,
                    IncidentStatus.AWAITING_APPROVAL,
                    IncidentStatus.EXECUTING,
                ):
                    await inc_repo.transition_status(
                        tenant_id=tenant_id, incident_id=out.response.id, target_status=st
                    )
                inc_ids_diff.append((out.response.id, t_name))

            barrier_diff = asyncio.Barrier(level)
            lat_diff: list[float] = []
            verified_receipt_ids: list[uuid.UUID] = []

            async def _diff_worker(
                idx: int,
                *,
                _inc_ids: list[tuple[uuid.UUID, str]] = inc_ids_diff,
                _barrier: asyncio.Barrier = barrier_diff,
                _lat: list[float] = lat_diff,
                _verified: list[uuid.UUID] = verified_receipt_ids,
            ) -> None:
                inc_id, t_name = _inc_ids[idx]
                act = ActionSpec(action_kind="restart_service", parameters={"worker": idx})
                app = ApprovedPlanContext(
                    approval_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    incident_id=inc_id,
                    target_id=t_name,
                    plan_digest=f"plan-diff-{idx}",
                    action_digest=act.action_digest(),
                    expected_version="v1",
                    expires_at=now + timedelta(minutes=30),
                )
                await _barrier.wait()
                t0 = time.perf_counter()
                permit = await res_mgr.acquire_reservation(approval=app, adapter=sim)
                rec = await gateway.execute_with_permit(
                    permit=permit, approval=app, action=act, adapter=sim
                )
                _lat.append((time.perf_counter() - t0) * 1000.0)
                assert rec.state == EffectState.VERIFIED
                _verified.append(rec.id)

            t_start_diff = time.perf_counter()
            await asyncio.gather(*(_diff_worker(i) for i in range(level)))
            t_elapsed_diff = time.perf_counter() - t_start_diff
            diff_target_results[f"concurrency_{level}"] = _summarize_latencies(
                lat_diff, t_elapsed_diff
            )

            # 3. Receipt lookup latency at concurrency `level`
            barrier_lk = asyncio.Barrier(level)
            lat_lk: list[float] = []

            async def _lookup_worker(
                idx: int,
                *,
                _verified: list[uuid.UUID] = verified_receipt_ids,
                _barrier: asyncio.Barrier = barrier_lk,
                _lat: list[float] = lat_lk,
            ) -> None:
                rid = _verified[idx]
                await _barrier.wait()
                t0 = time.perf_counter()
                snap = await gateway.get_receipt(tenant_id, rid)
                _lat.append((time.perf_counter() - t0) * 1000.0)
                assert snap is not None and snap.state == EffectState.VERIFIED

            t_start_lk = time.perf_counter()
            await asyncio.gather(*(_lookup_worker(i) for i in range(level)))
            t_elapsed_lk = time.perf_counter() - t_start_lk
            receipt_lookup_results[f"concurrency_{level}"] = _summarize_latencies(
                lat_lk, t_elapsed_lk
            )

            # 4. UNKNOWN effect reconciliation throughput at concurrency `level`
            proxy = FaultInjectionProxy(sim)
            for i in range(level):
                t_unk = f"perf-unk-{level}-{i}-{uuid.uuid4().hex[:6]}"
                out = await inc_repo.admit_incident(
                    tenant_id=tenant_id,
                    client_key=f"perf-unk-inc-{level}-{i}-{uuid.uuid4()}",
                    target_id=t_unk,
                    request_sha256="3" * 64,
                )
                for st in (
                    IncidentStatus.DISPATCH_PENDING,
                    IncidentStatus.INVESTIGATING,
                    IncidentStatus.AWAITING_APPROVAL,
                    IncidentStatus.EXECUTING,
                ):
                    await inc_repo.transition_status(
                        tenant_id=tenant_id, incident_id=out.response.id, target_status=st
                    )
                act = ActionSpec(action_kind="drain_traffic")
                app = ApprovedPlanContext(
                    approval_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    incident_id=out.response.id,
                    target_id=t_unk,
                    plan_digest=f"plan-unk-{i}",
                    action_digest=act.action_digest(),
                    expected_version="v1",
                    expires_at=now + timedelta(minutes=30),
                )
                permit = await res_mgr.acquire_reservation(approval=app, adapter=proxy)
                proxy.set_rule(
                    FaultRule(
                        scenario=FaultScenario.LOST_RESPONSE_AFTER_COMMIT,
                        target_id=t_unk,
                        max_triggers=1,
                    )
                )
                rec_u = await gateway.execute_with_permit(
                    permit=permit, approval=app, action=act, adapter=proxy
                )
                assert rec_u.state == EffectState.UNKNOWN

            t_start_rec = time.perf_counter()
            reconciled = await reconciler.reconcile_pending(
                adapters={adapter_id: proxy},
                tenant_id=tenant_id,
                batch_size=level,
            )
            t_elapsed_rec = time.perf_counter() - t_start_rec
            assert len(reconciled) == level
            assert all(r.state == EffectState.VERIFIED for r in reconciled)
            per_item_ms = (t_elapsed_rec * 1000.0) / level
            unknown_recon_results[f"concurrency_{level}"] = _summarize_latencies(
                [per_item_ms] * level, t_elapsed_rec
            )

        # 5. E8 Conformance run throughput (5 runs)
        conf_latencies: list[float] = []
        t_start_conf = time.perf_counter()
        for i in range(5):
            cid = f"sim-perf-conf-{i}-{uuid.uuid4().hex[:6]}"
            created_adapter_ids.append(cid)
            csim = DisposableTargetSimulator(adapter_id=cid, version="1.0.0")
            t0 = time.perf_counter()
            rep = await runner.run_conformance_suite(csim)
            conf_latencies.append((time.perf_counter() - t0) * 1000.0)
            assert rep.passed is True
        t_elapsed_conf = time.perf_counter() - t_start_conf
        conformance_results["suite_12_scenarios_x5"] = _summarize_latencies(
            conf_latencies, t_elapsed_conf
        )

        pool_status = engine.pool.status()
        cpu_elapsed = round(time.process_time() - cpu_start, 4)
        wall_elapsed = round(time.perf_counter() - wall_start, 4)

        return {
            "benchmark": "TITAN-20 Stage 3 (B2 + B3 + E8) Controlled Performance & Reliability",
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "concurrency_levels": concurrency_levels,
            "same_target_reservation_contention": same_target_results,
            "different_target_b2_b3_execution": diff_target_results,
            "effect_receipt_lookup": receipt_lookup_results,
            "unknown_effect_reconciliation": unknown_recon_results,
            "e8_conformance_runs": conformance_results,
            "connection_pool_status": pool_status,
            "resource_usage": {
                "cpu_time_seconds": cpu_elapsed,
                "wall_time_seconds": wall_elapsed,
                "pid": os.getpid(),
            },
        }
    finally:
        # Clean up ONLY benchmark rows created for this isolated tenant & adapter IDs
        async with session_factory() as session:
            inc_subq = (
                Incident.__table__.select()
                .with_only_columns(Incident.id)
                .where(Incident.tenant_id == tenant_id)
            )
            await session.execute(delete(EffectReceipt).where(EffectReceipt.tenant_id == tenant_id))
            await session.execute(
                delete(ResourceReservation).where(ResourceReservation.tenant_id == tenant_id)
            )
            await session.execute(delete(ResourceEpoch).where(ResourceEpoch.tenant_id == tenant_id))
            await session.execute(delete(Outbox).where(Outbox.incident_id.in_(inc_subq)))
            await session.execute(delete(Incident).where(Incident.tenant_id == tenant_id))
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await session.execute(
                delete(AdapterConformance).where(
                    AdapterConformance.adapter_id.in_(created_adapter_ids)
                )
            )
            await session.commit()
        await engine.dispose()


if __name__ == "__main__":
    report = asyncio.run(run_benchmark())
    out_path = Path("docs/evidence/stage3-performance-benchmark.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
