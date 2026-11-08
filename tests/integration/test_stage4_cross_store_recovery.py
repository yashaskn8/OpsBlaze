"""Stage 4 Phase 4 — Cross-Store Recovery Integration Tests (Scenarios A, B, C).

Verifies:
1. Scenario A: PostgreSQL restored to an earlier snapshot (e.g. at ACCEPTED / DISPATCH_PENDING / AWAITING_APPROVAL),
   while Temporal is ahead (e.g. at AWAITING_APPROVAL or VERIFIED). CrossStoreRecoveryCoordinator
   projects PostgreSQL monotonically forward, marks outbox delivered, and prevents duplicate executions.
2. Scenario B: Temporal restored to an earlier snapshot or empty (workflow lost / terminated),
   while PostgreSQL is ahead:
   - For an incident with a terminal effect receipt (VERIFIED), PostgreSQL converges to VERIFIED and marks outbox delivered.
   - For a pre-execution incident (AWAITING_APPROVAL), PostgreSQL resets status to DISPATCH_PENDING and
     outbox to undelivered so OutboxDispatcher cleanly re-runs B1 read-only investigation to AWAITING_APPROVAL.
   - For an incident stuck in EXECUTING with no receipt and no Temporal workflow, quarantined to NEEDS_REVIEW.
3. Scenario C: Both restored independently to divergent points with live target adapter ahead:
   - Live target saw higher fence -> epoch fence watermark advances to max(epoch, target, reservations, receipts).
   - Invariant R4 triggers: conflicting reservations are released/expired and incidents quarantined to NEEDS_REVIEW.
   - Full 10-step sequence exits degraded mode and unblocks normal mutation operations.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.conformance import ConformanceRunner
from backend.adapters.simulator import DisposableTargetSimulator
from backend.domain.incident import (
    IncidentStatus,
)
from backend.effects.fencing import (
    ApprovedPlanContext,
    ReservationState,
    ResourceReservationManager,
)
from backend.effects.receipts import (
    EffectState,
)
from backend.persistence.models import (
    EffectReceipt,
    ResourceEpoch,
    ResourceReservation,
)
from backend.persistence.repositories import IncidentRepository
from backend.workflows.recovery import (
    CrossStoreRecoveryCoordinator,
)


async def test_scenario_a_postgres_behind_temporal(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Scenario A: PostgreSQL snapshot was restored to an earlier point in time where
    incidents were only ACCEPTED or INVESTIGATING, but Temporal workflow already ran to
    AWAITING_APPROVAL or VERIFIED.
    Recovery coordinator must advance PostgreSQL status monotonically without regressing,
    mark outbox delivered, and avoid re-dispatching.
    """
    inc_repo = IncidentRepository(session_factory)
    coordinator = CrossStoreRecoveryCoordinator(session_factory)
    now = datetime.now(UTC)

    # Incident 1: PG restored to ACCEPTED, Temporal at AWAITING_APPROVAL
    out1 = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"scen-a-1-{uuid.uuid4()}",
        target_id="sim-target-a1",
        request_sha256="a" * 64,
    )
    inc1_id = out1.response.id
    wf1_id = f"incident:{isolated_tenant}:{inc1_id}"

    # Incident 2: PG restored to INVESTIGATING, Temporal at VERIFIED
    out2 = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"scen-a-2-{uuid.uuid4()}",
        target_id="sim-target-a2",
        request_sha256="b" * 64,
    )
    inc2_id = out2.response.id
    wf2_id = f"incident:{isolated_tenant}:{inc2_id}"
    await inc_repo.transition_status(
        tenant_id=isolated_tenant,
        incident_id=inc2_id,
        target_status=IncidentStatus.DISPATCH_PENDING,
    )
    await inc_repo.transition_status(
        tenant_id=isolated_tenant,
        incident_id=inc2_id,
        target_status=IncidentStatus.INVESTIGATING,
    )

    temporal_state = {
        wf1_id: {"status": IncidentStatus.AWAITING_APPROVAL.value},
        wf2_id: {"status": IncidentStatus.VERIFIED.value},
    }

    report = await coordinator.run_recovery(
        tenant_id=isolated_tenant,
        temporal_workflows=temporal_state,
        now=now,
    )
    assert report.degraded_mode_exited is True

    snap1 = await inc_repo.get_incident(isolated_tenant, inc1_id)
    assert snap1.status == IncidentStatus.AWAITING_APPROVAL
    assert snap1.outbox_delivered is True

    snap2 = await inc_repo.get_incident(isolated_tenant, inc2_id)
    assert snap2.status == IncidentStatus.VERIFIED
    assert snap2.outbox_delivered is True


async def test_scenario_b_temporal_behind_or_lost_postgres_intact(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Scenario B: Temporal history was lost, empty, or restored behind PostgreSQL:
    1. Incident with a terminal VERIFIED receipt converges to VERIFIED and marks outbox delivered.
    2. Incident stuck in EXECUTING with no receipt and no Temporal workflow quarantines to NEEDS_REVIEW.
    3. Pre-execution incident in AWAITING_APPROVAL with no Temporal workflow resets to DISPATCH_PENDING
       with outbox undelivered so it can cleanly re-run read-only investigation.
    """
    inc_repo = IncidentRepository(session_factory)
    coordinator = CrossStoreRecoveryCoordinator(session_factory)
    now = datetime.now(UTC)

    # Case 1: Terminal VERIFIED receipt exists in PG, but Temporal workflow lost
    out1 = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"scen-b-1-{uuid.uuid4()}",
        target_id="sim-target-b1",
        request_sha256="1" * 64,
    )
    inc1_id = out1.response.id
    async with session_factory() as session:
        session.add(
            EffectReceipt(
                id=uuid.uuid4(),
                tenant_id=isolated_tenant,
                incident_id=inc1_id,
                plan_digest="plan-b1",
                target_id="sim-target-b1",
                adapter_id="adapter-b1",
                adapter_digest="dig-b1",
                idempotency_key=f"rec-b1-{uuid.uuid4()}",
                fence=1,
                expected_version="v1",
                observed_version="v2",
                state=EffectState.VERIFIED.value,
                attempt_count=1,
                action_digest="act-b1",
                action_payload={"action_kind": "restart"},
                created_at=now - timedelta(minutes=5),
                updated_at=now - timedelta(minutes=5),
            )
        )
        await session.commit()

    # Case 2: Incident in EXECUTING in PG, but no receipt and no Temporal workflow
    out2 = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"scen-b-2-{uuid.uuid4()}",
        target_id="sim-target-b2",
        request_sha256="2" * 64,
    )
    inc2_id = out2.response.id
    for st in (
        IncidentStatus.DISPATCH_PENDING,
        IncidentStatus.INVESTIGATING,
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.EXECUTING,
    ):
        await inc_repo.transition_status(
            tenant_id=isolated_tenant, incident_id=inc2_id, target_status=st
        )

    # Case 3: Incident in AWAITING_APPROVAL in PG, but no Temporal workflow
    out3 = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"scen-b-3-{uuid.uuid4()}",
        target_id="sim-target-b3",
        request_sha256="3" * 64,
    )
    inc3_id = out3.response.id
    for st in (
        IncidentStatus.DISPATCH_PENDING,
        IncidentStatus.INVESTIGATING,
        IncidentStatus.AWAITING_APPROVAL,
    ):
        await inc_repo.transition_status(
            tenant_id=isolated_tenant, incident_id=inc3_id, target_status=st
        )

    # Run recovery with empty Temporal workflows (simulating lost Temporal state)
    report = await coordinator.run_recovery(
        tenant_id=isolated_tenant,
        temporal_workflows={},
        now=now,
    )
    assert report.degraded_mode_exited is True

    snap1 = await inc_repo.get_incident(isolated_tenant, inc1_id)
    assert snap1.status == IncidentStatus.VERIFIED
    assert snap1.outbox_delivered is True

    snap2 = await inc_repo.get_incident(isolated_tenant, inc2_id)
    assert snap2.status == IncidentStatus.NEEDS_REVIEW

    snap3 = await inc_repo.get_incident(isolated_tenant, inc3_id)
    assert snap3.status == IncidentStatus.DISPATCH_PENDING
    assert snap3.outbox_delivered is False


async def test_scenario_c_divergent_restores_and_target_ahead(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Scenario C: Both PostgreSQL and Temporal restored to divergent earlier points,
    and the live target adapter is ahead (e.g. target saw fence=5, version='v6' from post-snapshot activity).
    Verify:
    1. Epoch fence watermark synchronizes to target.highest_fence_seen (5).
    2. Active reservation with fence <= 5 is expired/released and incident quarantined to NEEDS_REVIEW.
    3. Normal mutations resume safely after recovery completes with next fence = 6.
    """
    adapter_id = f"sim-scen-c-{uuid.uuid4().hex[:8]}"
    sim = DisposableTargetSimulator(adapter_id=adapter_id, version="1.0.0")
    runner = ConformanceRunner(session_factory)
    inc_repo = IncidentRepository(session_factory)
    res_mgr = ResourceReservationManager(session_factory)
    coordinator = CrossStoreRecoveryCoordinator(session_factory)
    now = datetime.now(UTC)

    await runner.run_conformance_suite(sim, now=now)

    # Simulate live target that had a post-snapshot external mutation:
    # Target is at fence=5, version="v6", last_effect_id="lost-effect-after-backup"
    sim_res = sim._get_or_create_resource(isolated_tenant, "sim-target-c")
    sim_res.highest_fence_seen = 5
    sim_res.current_version = "v6"
    sim_res.version_counter = 6
    sim_res.last_effect_id = "lost-effect-after-backup"

    # In PostgreSQL, snapshot restored with epoch latest_fence=2, and an active reservation at fence=2
    out = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"scen-c-inc-{uuid.uuid4()}",
        target_id="sim-target-c",
        request_sha256="c" * 64,
    )
    inc_id = out.response.id
    for st in (
        IncidentStatus.DISPATCH_PENDING,
        IncidentStatus.INVESTIGATING,
        IncidentStatus.AWAITING_APPROVAL,
    ):
        await inc_repo.transition_status(
            tenant_id=isolated_tenant, incident_id=inc_id, target_status=st
        )

    res_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            ResourceEpoch(
                tenant_id=isolated_tenant,
                target_id="sim-target-c",
                latest_fence=2,
                version_seen="v3",
            )
        )
        session.add(
            ResourceReservation(
                id=res_id,
                tenant_id=isolated_tenant,
                target_id="sim-target-c",
                holder_incident_id=inc_id,
                action_digest="act-c",
                fence=2,
                expires_at=now + timedelta(minutes=10),
                state=ReservationState.RESERVED.value,
            )
        )
        await session.commit()

    report = await coordinator.run_recovery(
        adapters={adapter_id: sim},
        tenant_id=isolated_tenant,
        now=now,
    )
    assert report.degraded_mode_exited is True

    # 1. Epoch watermark synchronized to target highest_fence_seen (5)
    async with session_factory() as session:
        ep = await session.get(ResourceEpoch, (isolated_tenant, "sim-target-c"))
        assert ep is not None
        assert ep.latest_fence == 5
        assert ep.version_seen == "v6"

        res = await session.get(ResourceReservation, res_id)
        assert res is not None
        assert res.state == ReservationState.EXPIRED.value

    # 2. Holder incident transitioned to NEEDS_REVIEW per Invariant R4
    snap = await inc_repo.get_incident(isolated_tenant, inc_id)
    assert snap.status == IncidentStatus.NEEDS_REVIEW

    # 3. Subsequent reservation on sim-target-c mints next monotonic fence=6 at expected_version="v6"
    new_inc = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"scen-c-new-{uuid.uuid4()}",
        target_id="sim-target-c",
        request_sha256="d" * 64,
    )
    for st in (
        IncidentStatus.DISPATCH_PENDING,
        IncidentStatus.INVESTIGATING,
        IncidentStatus.AWAITING_APPROVAL,
    ):
        await inc_repo.transition_status(
            tenant_id=isolated_tenant, incident_id=new_inc.response.id, target_status=st
        )

    app = ApprovedPlanContext(
        approval_id=uuid.uuid4(),
        tenant_id=isolated_tenant,
        incident_id=new_inc.response.id,
        target_id="sim-target-c",
        plan_digest="plan-c-new",
        action_digest="act-c-new",
        expected_version="v6",
        expires_at=now + timedelta(minutes=15),
    )
    permit = await res_mgr.acquire_reservation(approval=app, adapter=sim, now=now)
    assert permit.fence == 6
