"""Unit, barrier-race, and integration tests for Feature B2 — Version-Fenced Resource Reservation."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.conformance import (
    AdapterNotConformantError,
    ConformanceRunner,
)
from backend.adapters.protocol import AdapterApplyRequest, AdapterApplyStatus
from backend.adapters.simulator import DisposableTargetSimulator
from backend.domain.incident import IncidentStatus
from backend.effects.fencing import (
    ApprovalRequiredError,
    ApprovedPlanContext,
    InvalidReservationStateTransitionError,
    MutationPermit,
    ReservationState,
    ResourceAlreadyReservedError,
    ResourceReservationManager,
    StaleFenceError,
    TargetVersionConflictError,
    validate_reservation_transition,
    verify_approved_plan_context,
)
from backend.observability.metrics import metrics
from backend.persistence.models import AdapterConformance
from backend.persistence.repositories import IncidentRepository


@pytest.fixture
async def certified_simulator(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[DisposableTargetSimulator]:
    adapter_id = f"sim-b2-{uuid.uuid4().hex[:8]}"
    sim = DisposableTargetSimulator(adapter_id=adapter_id, version="1.0.0")
    runner = ConformanceRunner(session_factory)
    report = await runner.run_conformance_suite(sim)
    assert report.passed is True
    try:
        yield sim
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(AdapterConformance).where(AdapterConformance.adapter_id == adapter_id)
            )
            await session.commit()


def _make_approval(
    *,
    tenant_id: uuid.UUID,
    incident_id: uuid.UUID,
    target_id: str,
    plan_digest: str = "plan-dig-1",
    action_digest: str = "act-dig-1",
    expected_version: str = "v1",
    approved_by_role: str = "incident_commander",
    execution_mode: str = "SIMULATOR_APPROVED",
    expires_at: datetime | None = None,
) -> ApprovedPlanContext:
    now = datetime.now(UTC)
    return ApprovedPlanContext(
        approval_id=uuid.uuid4(),
        tenant_id=tenant_id,
        incident_id=incident_id,
        target_id=target_id,
        plan_digest=plan_digest,
        action_digest=action_digest,
        expected_version=expected_version,
        approved_by_role=approved_by_role,
        execution_mode=execution_mode,
        approved_at=now,
        expires_at=expires_at or (now + timedelta(minutes=10)),
    )


async def _create_awaiting_approval_incident(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    target_id: str,
) -> uuid.UUID:
    repo = IncidentRepository(session_factory)
    outcome = await repo.admit_incident(
        tenant_id=tenant_id,
        client_key=f"b2-inc-{uuid.uuid4()}",
        target_id=target_id,
        request_sha256="a" * 64,
    )
    inc_id = outcome.response.id
    await repo.transition_status(
        tenant_id=tenant_id,
        incident_id=inc_id,
        target_status=IncidentStatus.DISPATCH_PENDING,
    )
    await repo.transition_status(
        tenant_id=tenant_id,
        incident_id=inc_id,
        target_status=IncidentStatus.INVESTIGATING,
    )
    await repo.transition_status(
        tenant_id=tenant_id,
        incident_id=inc_id,
        target_status=IncidentStatus.AWAITING_APPROVAL,
    )
    return inc_id


def test_b2_reservation_state_machine_and_approval_validator() -> None:
    """Verify B2 reservation state transitions and fail-closed P2 approval validator."""
    assert validate_reservation_transition(ReservationState.RESERVED, ReservationState.DISPATCHED)
    assert validate_reservation_transition(ReservationState.DISPATCHED, ReservationState.RELEASED)
    assert validate_reservation_transition(ReservationState.RESERVED, ReservationState.EXPIRED)
    assert validate_reservation_transition(ReservationState.RESERVED, ReservationState.CANCELLED)

    # Illegal transitions
    with pytest.raises(InvalidReservationStateTransitionError):
        validate_reservation_transition(ReservationState.RELEASED, ReservationState.RESERVED)
    with pytest.raises(InvalidReservationStateTransitionError):
        validate_reservation_transition(ReservationState.EXPIRED, ReservationState.DISPATCHED)

    # Fail-closed P2 approval checks
    t_id = uuid.uuid4()
    inc_id = uuid.uuid4()
    with pytest.raises(ApprovalRequiredError):
        verify_approved_plan_context(
            None,
            tenant_id=t_id,
            incident_id=inc_id,
            target_id="t1",
            plan_digest="p1",
            action_digest="a1",
            expected_version="v1",
            is_simulator_adapter=True,
        )

    # Model/agent self-approval is strictly rejected
    model_approval = _make_approval(
        tenant_id=t_id,
        incident_id=inc_id,
        target_id="t1",
        approved_by_role="agent",
    )
    with pytest.raises(ApprovalRequiredError):
        verify_approved_plan_context(
            model_approval,
            tenant_id=t_id,
            incident_id=inc_id,
            target_id="t1",
            plan_digest="plan-dig-1",
            action_digest="act-dig-1",
            expected_version="v1",
            is_simulator_adapter=True,
        )


async def test_b2_barrier_synchronized_same_target_contention_and_different_targets(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    certified_simulator: DisposableTargetSimulator,
) -> None:
    """Scenarios 1-3: Barrier-synchronized contention on same (tenant_id, target_id) across 5 workers
    allows exactly 1 reservation (fence=1) and rejects 4 with reservation.conflict, while
    concurrent reservations across different targets all succeed in parallel.
    """
    mgr = ResourceReservationManager(session_factory)
    target_id = "sim-contended-target"

    inc_ids = [
        await _create_awaiting_approval_incident(session_factory, isolated_tenant, target_id)
        for _ in range(5)
    ]
    barrier = asyncio.Barrier(5)

    async def worker_try_reserve(idx: int) -> MutationPermit:
        inc_id = inc_ids[idx]
        approval = _make_approval(
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            target_id=target_id,
            action_digest=f"act-{idx}",
        )
        await barrier.wait()
        return await mgr.acquire_reservation(
            approval=approval,
            adapter=certified_simulator,
            lease_seconds=30,
        )

    results = await asyncio.gather(
        *(worker_try_reserve(i) for i in range(5)), return_exceptions=True
    )
    successes = [r for r in results if isinstance(r, MutationPermit)]
    conflicts = [r for r in results if isinstance(r, ResourceAlreadyReservedError)]

    assert len(successes) == 1
    assert len(conflicts) == 4
    assert successes[0].fence == 1
    assert metrics.snapshot()["counters"]["reservation_conflicts_total"] == 4

    # Different targets succeed concurrently without blocking each other
    diff_barrier = asyncio.Barrier(5)

    async def worker_diff_target(idx: int) -> MutationPermit:
        t_name = f"sim-distinct-target-{idx}"
        inc_id = await _create_awaiting_approval_incident(session_factory, isolated_tenant, t_name)
        approval = _make_approval(
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            target_id=t_name,
        )
        await diff_barrier.wait()
        return await mgr.acquire_reservation(
            approval=approval,
            adapter=certified_simulator,
            lease_seconds=30,
        )

    diff_results = await asyncio.gather(*(worker_diff_target(i) for i in range(5)))
    assert len(diff_results) == 5
    assert all(p.fence == 1 for p in diff_results)


async def test_b2_lease_expiry_fence_monotonicity_and_delayed_zombie_worker_rejection(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    certified_simulator: DisposableTargetSimulator,
) -> None:
    """Scenarios 4-7: Worker 1 acquires fence=1, stalls past lease expiration, Worker 2 acquires
    fence=2 and mutates target to v2, and delayed Worker 1 is rejected both in DB and at simulator.
    Also verifies fences never decrease or reuse after release/cancel.
    """
    mgr = ResourceReservationManager(session_factory)
    target_id = "sim-zombie-target"
    t0 = datetime(2026, 11, 7, 17, 0, 0, tzinfo=UTC)

    inc1 = await _create_awaiting_approval_incident(session_factory, isolated_tenant, target_id)
    inc2 = await _create_awaiting_approval_incident(session_factory, isolated_tenant, target_id)

    app1 = _make_approval(
        tenant_id=isolated_tenant,
        incident_id=inc1,
        target_id=target_id,
        action_digest="act-w1",
        expires_at=t0 + timedelta(minutes=30),
    )
    permit1 = await mgr.acquire_reservation(
        approval=app1,
        adapter=certified_simulator,
        lease_seconds=10,
        now=t0,
    )
    assert permit1.fence == 1

    # Advance time past Worker 1's 10s lease -> Worker 2 acquires fence=2
    t1 = t0 + timedelta(seconds=15)
    app2 = _make_approval(
        tenant_id=isolated_tenant,
        incident_id=inc2,
        target_id=target_id,
        action_digest="act-w2",
        expires_at=t0 + timedelta(minutes=30),
    )
    permit2 = await mgr.acquire_reservation(
        approval=app2,
        adapter=certified_simulator,
        lease_seconds=30,
        now=t1,
    )
    assert permit2.fence == 2
    assert metrics.snapshot()["counters"]["lease_expired_total"] >= 1

    # Worker 2 marks DISPATCHED, mutates simulator to v2, and releases reservation
    await mgr.mark_dispatched(permit2, now=t1)
    apply_res2 = await certified_simulator.apply(
        AdapterApplyRequest(
            tenant_id=isolated_tenant,
            target_id=target_id,
            effect_id="eff-w2",
            fence=permit2.fence,
            expected_version="v1",
            action_kind="restart_service",
            action_digest="act-w2",
        )
    )
    assert apply_res2.status == AdapterApplyStatus.APPLIED
    assert apply_res2.observed_version == "v2"
    await mgr.release_reservation(permit2, observed_version="v2", now=t1 + timedelta(seconds=1))

    # Delayed Worker 1 wakes up and tries to dispatch with stale permit1 (fence=1):
    # 1) Rejected by DB pre-dispatch check
    with pytest.raises(StaleFenceError):
        await mgr.mark_dispatched(permit1, now=t1 + timedelta(seconds=2))
    assert metrics.snapshot()["counters"]["fence_stale_rejects_total"] >= 1

    # 2) Even if Worker 1 bypassed DB and hit the simulator directly with fence=1, simulator rejects!
    stale_sim_res = await certified_simulator.apply(
        AdapterApplyRequest(
            tenant_id=isolated_tenant,
            target_id=target_id,
            effect_id="eff-w1-zombie",
            fence=permit1.fence,
            expected_version="v1",
            action_kind="restart_service",
            action_digest="act-w1",
        )
    )
    assert stale_sim_res.status == AdapterApplyStatus.REJECTED_STALE_FENCE

    # 3) Next reservation on the same target gets fence=3 (never resets to 1)
    inc3 = await _create_awaiting_approval_incident(session_factory, isolated_tenant, target_id)
    app3 = _make_approval(
        tenant_id=isolated_tenant,
        incident_id=inc3,
        target_id=target_id,
        expected_version="v2",
        action_digest="act-w3",
        expires_at=t0 + timedelta(minutes=30),
    )
    permit3 = await mgr.acquire_reservation(
        approval=app3,
        adapter=certified_simulator,
        lease_seconds=30,
        now=t1 + timedelta(seconds=5),
    )
    assert permit3.fence == 3


async def test_b2_target_version_drift_cross_tenant_isolation_and_unverified_adapter_rejection(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    second_isolated_tenant: uuid.UUID,
    certified_simulator: DisposableTargetSimulator,
) -> None:
    """Scenarios 8-12: Verify ABA / out-of-band target version change rejection, cross-tenant
    target isolation, and unverified adapter rejection.
    """
    mgr = ResourceReservationManager(session_factory)
    target_id = "sim-shared-name-target"

    inc_t1 = await _create_awaiting_approval_incident(session_factory, isolated_tenant, target_id)
    inc_t2 = await _create_awaiting_approval_incident(
        session_factory, second_isolated_tenant, target_id
    )

    # Tenant 1 and Tenant 2 can both reserve 'sim-shared-name-target' independently (fence=1 each)
    p_t1 = await mgr.acquire_reservation(
        approval=_make_approval(tenant_id=isolated_tenant, incident_id=inc_t1, target_id=target_id),
        adapter=certified_simulator,
    )
    p_t2 = await mgr.acquire_reservation(
        approval=_make_approval(
            tenant_id=second_isolated_tenant, incident_id=inc_t2, target_id=target_id
        ),
        adapter=certified_simulator,
    )
    assert p_t1.fence == 1
    assert p_t2.fence == 1

    # Cancel Tenant 1's reservation and simulate out-of-band version drift on Tenant 1's target
    await mgr.cancel_reservation(p_t1)
    certified_simulator.advance_version_out_of_band(isolated_tenant, target_id)

    # Attempt to reserve with stale expected_version="v1" fails with TargetVersionConflictError
    inc_t1_b = await _create_awaiting_approval_incident(session_factory, isolated_tenant, target_id)
    with pytest.raises(TargetVersionConflictError):
        await mgr.acquire_reservation(
            approval=_make_approval(
                tenant_id=isolated_tenant,
                incident_id=inc_t1_b,
                target_id=target_id,
                expected_version="v1",
            ),
            adapter=certified_simulator,
        )
    assert metrics.snapshot()["counters"]["target_version_conflicts_total"] >= 1

    # Unverified adapter is rejected before allocating a reservation
    unverified_sim = DisposableTargetSimulator(
        adapter_id=f"sim-unverified-{uuid.uuid4().hex[:8]}", version="1.0.0"
    )
    with pytest.raises(AdapterNotConformantError):
        await mgr.acquire_reservation(
            approval=_make_approval(
                tenant_id=isolated_tenant,
                incident_id=inc_t1_b,
                target_id=target_id,
                expected_version="v2",
            ),
            adapter=unverified_sim,
        )
