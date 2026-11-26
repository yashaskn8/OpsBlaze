"""P1-15, P3-18, B4-06 controlled scale, SSE concurrency, mixed-lineage, and overload benchmarks."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from time import perf_counter
import tracemalloc
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api import app
from backend.benchmarks.local_capacity import describe
from backend.database import get_session
from backend.events import STREAM_CAPACITY, StreamCapacity, read_event_batch
from backend.models import (
    ApprovalGrant,
    Base,
    EffectReceipt,
    Evidence,
    ExperienceCapsule,
    FailureCase,
    FailureCluster,
    HypothesisStep,
    Incident,
    IncidentPlan,
    OperationalLesson,
    OutcomeRevision,
)
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import Settings, get_settings
from backend.workers.lineage_recovery import recover_lineage_page


async def measure_sse_fanout(
    sessions: async_sessionmaker,
    *,
    clients: int = 16,
    cap_limit: int = 32,
) -> dict:
    """P1-15: measure concurrent SSE batch/stream handling, 32-stream cap, and memory."""
    async with sessions() as session:
        incident = await admit_incident(
            session, "sse-bench", "sse-load-1", IncidentCreate(target_id="checkout"),
        )
    # Seed 20 UI events for reconnect replay.
    async with sessions() as session:
        async with session.begin():
            from backend.models import UIEvent

            for ver in range(2, 22):
                session.add(UIEvent(
                    tenant_id="sse-bench",
                    incident_id=incident.id,
                    kind="INVESTIGATING",
                    projection_version=ver,
                ))

    tracemalloc.start()
    start_cur, _ = tracemalloc.get_traced_memory()
    latencies: list[float] = []

    async def client_read(_idx: int) -> int:
        t0 = perf_counter()
        rows = await read_event_batch(sessions, "sse-bench", incident.id, 0)
        latencies.append((perf_counter() - t0) * 1000)
        return len(rows)

    counts = await asyncio.gather(*(client_read(i) for i in range(clients)))
    # Verify per-process stream cap enforcement and release.
    cap = StreamCapacity(maximum=cap_limit)
    claimed = sum(1 for _ in range(cap_limit) if cap.claim())
    overflow_denied = not cap.claim()
    for _ in range(claimed):
        cap.release()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    return {
        "workload": f"{clients} concurrent SSE replay clients over 21 persisted events",
        "clients": clients,
        "events_delivered_per_client": counts[0],
        "all_clients_matched": all(c == 21 for c in counts),
        "stream_cap_limit": cap_limit,
        "overflow_rejected": overflow_denied,
        "default_process_cap": STREAM_CAPACITY.maximum,
        "peak_memory_kib": round((peak - start_cur) / 1024, 2),
        **describe(latencies),
    }


async def measure_mixed_lineage_and_overload(
    sessions: async_sessionmaker,
    *,
    evidence_count: int = 120,
    concurrent_readers: int = 4,
    tenant_cap: int = 8,
) -> dict:
    """P3-18 & B4-06: mixed 9-kind large-history graph queries, recovery, and overload/drain."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    async with sessions() as session:
        incident = await admit_incident(
            session, "mix-bench", "mix-1", IncidentCreate(target_id="checkout"),
        )
    ev_ids = [uuid4() for _ in range(evidence_count)]
    plan_id, grant_id, eff_id, out_id, cap_id, lesson_id, cluster_id, fail_id = (
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), uuid4(),
    )
    digest = "a" * 64
    async with sessions() as session:
        async with session.begin():
            session.add_all([
                Evidence(
                    id=eid, tenant_id="mix-bench", incident_id=incident.id,
                    target_id="checkout", source="simulator.health",
                    source_version="simulator-v1", target_version=1,
                    observed_at=now, sha256="b" * 64,
                    redacted_excerpt='{"healthy":false}',
                    classification="INTERNAL", validity="VALID",
                )
                for eid in ev_ids
            ])
            session.add_all([
                HypothesisStep(
                    id=uuid4(), incident_id=incident.id, step_no=i + 1,
                    model_id="deterministic-baseline-v1", prompt_version="none",
                    evidence_ids=[str(ev_ids[i % len(ev_ids)])],
                    cause_scores={"simulated_target_unhealthy": 90},
                    probe_id="simulator.health", decision="NEEDS_SECOND_PROBE",
                    token_reserved=0, token_used=0,
                )
                for i in range(60)
            ])
            session.add(IncidentPlan(
                id=plan_id, tenant_id="mix-bench", incident_id=incident.id,
                target_id="checkout", action_id="SIMULATOR_MARK_HEALTHY",
                plan_digest=digest, expected_version=1,
                evidence_ids=[str(ev_ids[0]), str(ev_ids[1])],
                status="APPROVED", version=2, decided_at=now,
                decided_by="REV", review_epoch=1,
            ))
            session.add(EffectReceipt(
                id=eff_id, tenant_id="mix-bench", incident_id=incident.id,
                plan_digest=digest, target_id="checkout",
                adapter_id="simulator", idempotency_key=str(eff_id),
                fence=1, expected_version=1, state="VERIFIED",
                observed_version=2, attempt_count=1,
            ))
            session.add(FailureCluster(
                id=cluster_id, tenant_id="mix-bench", signature="bench-sig", version=1, case_count=1,
            ))
            await session.flush()
            session.add(ApprovalGrant(
                id=grant_id, tenant_id="mix-bench", plan_id=plan_id,
                plan_digest=digest, reviewer_id="REV",
                scope="SIMULATOR_MARK_HEALTHY", decision="APPROVE",
                state="CONSUMED", reason="synthetic benchmark",
                version=2, expires_at=now + timedelta(minutes=5),
            ))
            session.add(OutcomeRevision(
                id=out_id, tenant_id="mix-bench", incident_id=incident.id,
                effect_id=eff_id, revision=1, label="VERIFIED",
                witness_source="SIMULATOR_WITNESS", observed_version=2,
                reason="synthetic benchmark",
            ))
            await session.flush()
            session.add(ExperienceCapsule(
                id=cap_id, tenant_id="mix-bench", incident_id=incident.id,
                outcome_revision_id=out_id, consent_epoch=1, tenant_consent_epoch=1,
                target_digest="d" * 64, evidence_ids=[str(ev_ids[0])],
                status="VALID", expires_at=now + timedelta(days=7),
            ))
            session.add(FailureCase(
                id=fail_id, tenant_id="mix-bench", incident_id=incident.id,
                outcome_revision_id=out_id, cluster_id=cluster_id,
                origin_code="EXECUTION_FAILURE", first_bad_step=1,
                classification_version=1, review_status="OPEN",
            ))
            await session.flush()
            session.add(OperationalLesson(
                id=lesson_id, tenant_id="mix-bench", source_capsule_id=cap_id,
                source_outcome_id=out_id, target_digest="d" * 64,
                rule_id="CHECK_VERSION", summary_redacted="synthetic rule",
                consent_epoch=1, status="DRAFT", version=1,
                valid_from=now, valid_until=now + timedelta(days=7),
            ))

    # Measure bounded lineage recovery throughput (pages of 20).
    recovery_times: list[float] = []
    for _ in range(12):
        t0 = perf_counter()
        more = await recover_lineage_page(sessions, limit=20)
        recovery_times.append((perf_counter() - t0) * 1000)
        if not more:
            break

    async def scoped_session():
        async with sessions() as session:
            yield session

    settings = Settings(
        environment="development",
        local_token="scale-bench-token",
        admission_limit_per_minute=100,
        max_active_incidents_per_tenant=tenant_cap,
        max_active_incidents_global=100,
    )
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = scoped_session

    summary_ms: list[float] = []
    page_ms: list[float] = []
    summary_bytes: list[int] = []
    overload_ms: list[float] = []
    headers = {"X-Tenant-Id": "mix-bench", "X-OpsBlaze-Token": "scale-bench-token"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://scale.invalid") as client:
            async def query_graph(_idx: int) -> None:
                t0 = perf_counter()
                res = await client.get(f"/v1/incidents/{incident.id}/lineage", headers=headers)
                assert res.status_code == 200
                summary_ms.append((perf_counter() - t0) * 1000)
                summary_bytes.append(len(res.content))

                t1 = perf_counter()
                p = await client.get(
                    f"/v1/incidents/{incident.id}/lineage/records?kind=evidence&limit=50",
                    headers=headers,
                )
                assert p.status_code == 200
                page_ms.append((perf_counter() - t1) * 1000)

            for _ in range(3):
                await asyncio.gather(*(query_graph(i) for i in range(concurrent_readers)))

            # B4-06: fill remaining tenant capacity and send 4 concurrent burst admissions -> 429.
            burst_headers = {"X-Tenant-Id": "burst-tenant", "X-OpsBlaze-Token": "scale-bench-token"}
            admitted_ids = []
            for idx in range(tenant_cap):
                res = await client.post(
                    "/v1/incidents",
                    headers={**burst_headers, "Idempotency-Key": f"burst-fill-{idx}"},
                    json={"target_id": "checkout"},
                )
                assert res.status_code == 202
                admitted_ids.append(res.json()["id"])

            async def send_overload(idx: int) -> int:
                t0 = perf_counter()
                res = await client.post(
                    "/v1/incidents",
                    headers={**burst_headers, "Idempotency-Key": f"burst-over-{idx}"},
                    json={"target_id": "checkout"},
                )
                overload_ms.append((perf_counter() - t0) * 1000)
                assert res.headers.get("Retry-After") == "60"
                return res.status_code

            statuses = await asyncio.gather(*(send_overload(i) for i in range(4)))
            assert statuses == [429, 429, 429, 429]

            # Drain: mark admitted incidents terminal and verify new admission succeeds (202).
            async with sessions() as session:
                async with session.begin():
                    for iid in admitted_ids:
                        row = await session.get(Incident, UUID(iid))
                        row.status_projection = "VERIFIED"
            drained = await client.post(
                "/v1/incidents",
                headers={**burst_headers, "Idempotency-Key": "post-drain-1"},
                json={"target_id": "checkout"},
            )
            assert drained.status_code == 202
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)

    return {
        "mixed_lineage": {
            "workload": f"{evidence_count} evidence + 60 hypotheses + 7 other record kinds, {concurrent_readers} concurrent readers",
            "summary": {**describe(summary_ms), "max_bytes": max(summary_bytes)},
            "page": describe(page_ms),
            "recovery_pages": describe(recovery_times),
        },
        "overload_and_drain": {
            "workload": f"tenant_cap={tenant_cap}, 4 concurrent overload rejections + terminal drain",
            "rejected_429": 4,
            "post_drain_admitted": True,
            **describe(overload_ms),
        },
    }


async def run_scale_profile(database_url: str | None = None) -> dict:
    import tempfile
    from pathlib import Path

    if database_url:
        engine = create_async_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            sse = await measure_sse_fanout(sessions)
            mixed = await measure_mixed_lineage_and_overload(sessions)
        finally:
            await engine.dispose()
        return {"database": "postgresql", "sse": sse, **mixed}

    with tempfile.TemporaryDirectory(prefix="opsblaze-scale-") as root:
        url = "sqlite+aiosqlite:///" + str(Path(root) / "scale.sqlite")
        engine = create_async_engine(url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            sse = await measure_sse_fanout(sessions)
            mixed = await measure_mixed_lineage_and_overload(sessions)
        finally:
            await engine.dispose()
        return {"database": "sqlite", "sse": sse, **mixed}


if __name__ == "__main__":
    import os

    result = asyncio.run(run_scale_profile(os.environ.get("OPSBLAZE_DATABASE_URL")))
    print(json.dumps(result, indent=2, sort_keys=True))
