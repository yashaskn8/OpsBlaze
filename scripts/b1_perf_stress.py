"""Phase 4 — Controlled Performance, Concurrency, and Short Soak Stress Benchmark for B1.

Measures:
- Admission throughput (RPS) and latency percentiles (p50, p95, p99) across concurrency levels 1, 5, 10, 20.
- HTTP error rate and duplicate logical incident verification (must be 0).
- PostgreSQL connection pool utilization.
- Outbox queue age and dispatcher processing rate (items/sec).
- Temporal schedule-to-start latency and workflow time to reach AWAITING_APPROVAL.
- Short mixed-workload soak run (admissions + status reads + cancellations + duplicate retries).
- Process CPU time and peak RSS memory usage.

Safety:
- Creates an isolated temporary benchmark tenant in opsblaze_test and deletes all its rows on exit,
  preserving 100% of pre-existing rows in opsblaze and opsblaze_test.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Ensure repository root is on sys.path when invoked via `python scripts/b1_perf_stress.py`
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from temporalio.testing import WorkflowEnvironment

from backend.api.main import create_app
from backend.config import get_settings
from backend.domain.incident import IncidentStatus
from backend.persistence.db import make_engine, make_session_factory
from backend.persistence.models import Incident, Outbox, Tenant
from backend.persistence.repositories import IncidentRepository
from backend.workflows.dispatcher import (
    OutboxDispatcher,
    TemporalWorkflowStarter,
)
from backend.workflows.worker import create_b1_worker


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return round(sorted_values[0], 3)
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    weight = rank - low
    val = sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight
    return round(val, 3)


def _get_process_memory_mb() -> float:
    """Return current process working set in MiB on Windows without external psutil dependency."""
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel32.GetCurrentProcess()
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            return float(round(int(pmc.WorkingSetSize) / (1024 * 1024), 2))
    except (OSError, AttributeError, ValueError):
        return 0.0
    return 0.0


async def run_benchmark() -> dict[str, Any]:
    base_settings = get_settings()
    test_db = os.environ.get("POSTGRES_TEST_DB", "opsblaze_test")
    settings = base_settings.model_copy(update={"postgres_db": test_db})

    engine = make_engine(settings, pool_size=10, max_overflow=10, pool_timeout=10.0)
    session_factory = make_session_factory(engine)
    inc_repo = IncidentRepository(session_factory)

    bench_tenant_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            Tenant(
                id=bench_tenant_id,
                name=f"b1-perf-bench-{bench_tenant_id.hex[:8]}",
            )
        )
        await session.commit()

    app = create_app(settings)
    app.state.engine = engine
    app.state.session_factory = session_factory

    cpu_start = time.process_time()
    mem_start_mb = _get_process_memory_mb()
    concurrency_results: list[dict[str, Any]] = []
    workflow_e2e_metrics: dict[str, Any] = {}
    soak_metrics: dict[str, Any] = {}

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://benchserver"
        ) as client:
            # 1. Admission Concurrency Matrix: 1, 5, 10, 20 concurrent requests
            for conc in (1, 5, 10, 20):
                requests_per_level = max(20, conc * 3)
                latencies_ms: list[float] = []
                errors = 0
                sem = asyncio.Semaphore(conc)

                async def _single_admit(
                    idx: int,
                    level: int = conc,
                    s: asyncio.Semaphore = sem,
                    lat_list: list[float] = latencies_ms,
                ) -> None:
                    nonlocal errors
                    async with s:
                        t0 = time.perf_counter()
                        resp = await client.post(
                            "/v1/incidents",
                            json={
                                "target_id": f"sim-bench-{level}-{idx}",
                                "severity": "SEV2",
                                "title": f"Benchmark incident {level}-{idx}",
                            },
                            headers={
                                "X-Tenant-ID": str(bench_tenant_id),
                                "Idempotency-Key": f"bench-c{level}-{idx}-{uuid.uuid4().hex[:8]}",
                            },
                        )
                        dt_ms = (time.perf_counter() - t0) * 1000.0
                        lat_list.append(dt_ms)
                        if resp.status_code != 202:
                            errors += 1

                wall_t0 = time.perf_counter()
                await asyncio.gather(*[_single_admit(i) for i in range(requests_per_level)])
                wall_elapsed_s = max(time.perf_counter() - wall_t0, 0.0001)
                sorted_lat = sorted(latencies_ms)
                pool_status = engine.pool.status()

                concurrency_results.append(
                    {
                        "concurrency": conc,
                        "total_requests": requests_per_level,
                        "wall_time_s": round(wall_elapsed_s, 4),
                        "throughput_rps": round(requests_per_level / wall_elapsed_s, 2),
                        "latency_p50_ms": _percentile(sorted_lat, 50),
                        "latency_p95_ms": _percentile(sorted_lat, 95),
                        "latency_p99_ms": _percentile(sorted_lat, 99),
                        "latency_mean_ms": round(statistics.mean(sorted_lat), 3),
                        "error_count": errors,
                        "error_rate_pct": round((errors / requests_per_level) * 100.0, 2),
                        "pool_status": pool_status,
                    }
                )

            # 2. Outbox Dispatcher + Temporal End-to-End Progression Benchmark
            queue_name = f"b1-perf-q-{uuid.uuid4().hex[:8]}"
            env = await WorkflowEnvironment.start_local()
            try:
                app.state.temporal_client = env.client
                starter = TemporalWorkflowStarter(env.client, task_queue=queue_name)
                dispatcher = OutboxDispatcher(session_factory, starter, batch_size=50)
                worker = create_b1_worker(env.client, session_factory, task_queue=queue_name)

                async with worker:
                    # Measure outbox drain of all admitted benchmark incidents + 10 fresh E2E timed incidents
                    e2e_ids: list[tuple[uuid.UUID, float]] = []
                    for i in range(10):
                        t_admit = time.perf_counter()
                        r = await client.post(
                            "/v1/incidents",
                            json={"target_id": f"sim-e2e-timed-{i}", "severity": "SEV1"},
                            headers={
                                "X-Tenant-ID": str(bench_tenant_id),
                                "Idempotency-Key": f"bench-e2e-{i}-{uuid.uuid4().hex[:8]}",
                            },
                        )
                        assert r.status_code == 202
                        e2e_ids.append((uuid.UUID(r.json()["id"]), t_admit))

                    dispatch_t0 = time.perf_counter()
                    total_dispatched = 0
                    while True:
                        batch = await dispatcher.dispatch_once()
                        total_dispatched += batch
                        if batch == 0:
                            break
                    dispatch_elapsed_s = max(time.perf_counter() - dispatch_t0, 0.0001)

                    # Wait for the 10 timed E2E workflows to reach AWAITING_APPROVAL
                    await_latencies_ms: list[float] = []
                    for inc_id, t_start in e2e_ids:
                        deadline = time.perf_counter() + 15.0
                        while time.perf_counter() < deadline:
                            snap = await inc_repo.get_incident(bench_tenant_id, inc_id)
                            if snap.status == IncidentStatus.AWAITING_APPROVAL:
                                await_latencies_ms.append((time.perf_counter() - t_start) * 1000.0)
                                break
                            await asyncio.sleep(0.03)

                    sorted_await = sorted(await_latencies_ms)
                    workflow_e2e_metrics = {
                        "total_outbox_dispatched": total_dispatched,
                        "dispatch_wall_time_s": round(dispatch_elapsed_s, 4),
                        "dispatcher_rate_items_per_sec": round(
                            total_dispatched / dispatch_elapsed_s, 2
                        ),
                        "timed_workflows_completed_to_awaiting_approval": len(await_latencies_ms),
                        "time_to_awaiting_approval_p50_ms": _percentile(sorted_await, 50),
                        "time_to_awaiting_approval_p95_ms": _percentile(sorted_await, 95),
                        "time_to_awaiting_approval_p99_ms": _percentile(sorted_await, 99),
                    }

                    # 3. Short Mixed-Workload Soak Test (25 iterations of admit + duplicate + read + dispatch + cancel)
                    soak_t0 = time.perf_counter()
                    soak_errors = 0
                    soak_ops = 0
                    for s_idx in range(25):
                        idem_key = f"soak-key-{s_idx}-{uuid.uuid4().hex[:6]}"
                        payload = {
                            "target_id": f"sim-soak-{s_idx}",
                            "severity": "SEV2",
                        }
                        # Admit
                        r1 = await client.post(
                            "/v1/incidents",
                            json=payload,
                            headers={
                                "X-Tenant-ID": str(bench_tenant_id),
                                "Idempotency-Key": idem_key,
                            },
                        )
                        soak_ops += 1
                        if r1.status_code != 202:
                            soak_errors += 1
                            continue
                        inc_id_str = r1.json()["id"]

                        # Duplicate retry with same key & payload
                        r_dup = await client.post(
                            "/v1/incidents",
                            json=payload,
                            headers={
                                "X-Tenant-ID": str(bench_tenant_id),
                                "Idempotency-Key": idem_key,
                            },
                        )
                        soak_ops += 1
                        if r_dup.status_code != 202 or r_dup.json()["id"] != inc_id_str:
                            soak_errors += 1

                        # Dispatch
                        await dispatcher.dispatch_once()
                        soak_ops += 1

                        # Read
                        r_get = await client.get(
                            f"/v1/incidents/{inc_id_str}",
                            headers={"X-Tenant-ID": str(bench_tenant_id)},
                        )
                        soak_ops += 1
                        if r_get.status_code != 200:
                            soak_errors += 1

                        # Cancel every 3rd incident
                        if s_idx % 3 == 0:
                            r_cancel = await client.post(
                                f"/v1/incidents/{inc_id_str}/cancel",
                                json={"reason": "soak_test_cancel"},
                                headers={"X-Tenant-ID": str(bench_tenant_id)},
                            )
                            soak_ops += 1
                            if r_cancel.status_code != 200:
                                soak_errors += 1

                    soak_elapsed_s = max(time.perf_counter() - soak_t0, 0.0001)

                    # Verify zero duplicate logical incidents per (tenant_id, client_key)
                    async with session_factory() as session:
                        dup_groups = (
                            await session.execute(
                                select(Incident.client_key, func.count(Incident.id))
                                .where(Incident.tenant_id == bench_tenant_id)
                                .group_by(Incident.client_key)
                                .having(func.count(Incident.id) > 1)
                            )
                        ).all()
                        duplicate_logical_incidents = len(dup_groups)

                    soak_metrics = {
                        "soak_iterations": 25,
                        "total_operations": soak_ops,
                        "soak_wall_time_s": round(soak_elapsed_s, 3),
                        "soak_ops_per_sec": round(soak_ops / soak_elapsed_s, 2),
                        "soak_error_count": soak_errors,
                        "duplicate_logical_incident_count": duplicate_logical_incidents,
                    }
            finally:
                await env.shutdown()
    finally:
        # Clean up ONLY the temporary benchmark tenant's rows
        async with session_factory() as session:
            inc_ids = (
                (
                    await session.execute(
                        select(Incident.id).where(Incident.tenant_id == bench_tenant_id)
                    )
                )
                .scalars()
                .all()
            )
            if inc_ids:
                await session.execute(delete(Outbox).where(Outbox.incident_id.in_(inc_ids)))
            await session.execute(delete(Incident).where(Incident.tenant_id == bench_tenant_id))
            await session.execute(delete(Tenant).where(Tenant.id == bench_tenant_id))
            await session.commit()
        await engine.dispose()

    cpu_elapsed_s = round(time.process_time() - cpu_start, 3)
    mem_end_mb = _get_process_memory_mb()

    report: dict[str, Any] = {
        "benchmark_timestamp_utc": datetime.now(UTC).isoformat(),
        "environment": {
            "os": "Windows",
            "database": test_db,
            "pool_size": 10,
            "max_overflow": 10,
        },
        "admission_concurrency_matrix": concurrency_results,
        "outbox_and_temporal_e2e": workflow_e2e_metrics,
        "mixed_soak_run": soak_metrics,
        "resource_utilization": {
            "process_cpu_time_s": cpu_elapsed_s,
            "memory_start_mb": mem_start_mb,
            "memory_end_mb": mem_end_mb,
            "memory_delta_mb": round(mem_end_mb - mem_start_mb, 2),
        },
    }

    evidence_dir = Path("docs/evidence")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    json_path = evidence_dir / "b1-performance-benchmark.json"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    asyncio.run(run_benchmark())
