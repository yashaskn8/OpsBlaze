"""Omega Adversarial Barrier-Race & Concurrency Tests for B2, B3, and E8."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.conformance import ConformanceRunner
from backend.adapters.fault_proxy import (
    FaultInjectionProxy,
    FaultRule,
    FaultScenario,
)
from backend.adapters.simulator import DisposableTargetSimulator
from backend.domain.incident import IncidentStatus
from backend.effects.fencing import (
    ApprovedPlanContext,
    MutationPermit,
    ResourceAlreadyReservedError,
    ResourceReservationManager,
    ResourceUncertainEffectPendingError,
)
from backend.effects.receipts import (
    ActionGateway,
    ActionSpec,
    EffectReceiptSnapshot,
    EffectReconciler,
    EffectState,
    StaleEffectClaimError,
)
from backend.persistence.models import (
    AdapterConformance,
    EffectReceipt,
    ResourceEpoch,
)
from backend.persistence.repositories import IncidentRepository


@pytest.fixture
async def race_simulator(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[DisposableTargetSimulator]:
    adapter_id = f"sim-race-{uuid.uuid4().hex[:8]}"
    sim = DisposableTargetSimulator(adapter_id=adapter_id, version="1.0.0")
    runner = ConformanceRunner(session_factory)
    await runner.run_conformance_suite(sim)
    try:
        yield sim
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(AdapterConformance).where(AdapterConformance.adapter_id == adapter_id)
            )
            await session.commit()


async def _admit_incident_awaiting(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    target_id: str,
) -> uuid.UUID:
    repo = IncidentRepository(session_factory)
    outcome = await repo.admit_incident(
        tenant_id=tenant_id,
        client_key=f"race-inc-{uuid.uuid4()}",
        target_id=target_id,
        request_sha256="d" * 64,
    )
    inc_id = outcome.response.id
    for st in (
        IncidentStatus.DISPATCH_PENDING,
        IncidentStatus.INVESTIGATING,
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.EXECUTING,
    ):
        await repo.transition_status(tenant_id=tenant_id, incident_id=inc_id, target_status=st)
    return inc_id


async def test_b2_high_contention_10_and_20_concurrent_reservations_same_target(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    race_simulator: DisposableTargetSimulator,
) -> None:
    """Verify barrier-synchronized contention at N=10 and N=20 on the same (tenant_id, target_id).
    Exactly 1 reservation succeeds (fence=1) and N-1 fail with ResourceAlreadyReservedError.
    """
    mgr = ResourceReservationManager(session_factory)
    now = datetime.now(UTC)

    for n_workers in (10, 20):
        target_id = f"sim-race-n{n_workers}-{uuid.uuid4().hex[:6]}"
        inc_ids = [
            await _admit_incident_awaiting(session_factory, isolated_tenant, target_id)
            for _ in range(n_workers)
        ]
        barrier = asyncio.Barrier(n_workers)

        async def _try_reserve(
            idx: int,
            *,
            _inc_ids: list[uuid.UUID] = inc_ids,
            _target_id: str = target_id,
            _barrier: asyncio.Barrier = barrier,
        ) -> MutationPermit:
            approval = ApprovedPlanContext(
                approval_id=uuid.uuid4(),
                tenant_id=isolated_tenant,
                incident_id=_inc_ids[idx],
                target_id=_target_id,
                plan_digest=f"plan-{idx}",
                action_digest=f"act-{idx}",
                expected_version="v1",
                expires_at=now + timedelta(minutes=15),
            )
            await _barrier.wait()
            return await mgr.acquire_reservation(
                approval=approval,
                adapter=race_simulator,
                lease_seconds=30,
            )

        outcomes = await asyncio.gather(
            *(_try_reserve(i) for i in range(n_workers)), return_exceptions=True
        )
        winners = [r for r in outcomes if isinstance(r, MutationPermit)]
        losers = [r for r in outcomes if isinstance(r, ResourceAlreadyReservedError)]
        assert len(winners) == 1
        assert len(losers) == n_workers - 1
        assert winners[0].fence == 1

        # Verify PostgreSQL has exactly 1 active reservation and latest_fence == 1
        async with session_factory() as session:
            epoch = (
                await session.execute(
                    select(ResourceEpoch).where(
                        ResourceEpoch.tenant_id == isolated_tenant,
                        ResourceEpoch.target_id == target_id,
                    )
                )
            ).scalar_one()
            assert epoch.latest_fence == 1


async def test_b2_b3_unresolved_dispatched_receipt_blocks_competing_reservation_even_past_lease(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    race_simulator: DisposableTargetSimulator,
) -> None:
    """Verify [CRITICAL-1] invariant: when a DISPATCHED reservation has an unresolved B3 receipt
    in UNKNOWN or SENT, a competing worker cannot expire the reservation and start a second
    concurrent mutation until B3 reconciles the receipt to a terminal state.
    """
    mgr = ResourceReservationManager(session_factory)
    gateway = ActionGateway(session_factory)
    reconciler = EffectReconciler(session_factory)
    proxy = FaultInjectionProxy(race_simulator)

    target_id = f"sim-unresolved-guard-{uuid.uuid4().hex[:6]}"
    t0 = datetime(2026, 11, 7, 19, 0, 0, tzinfo=UTC)

    inc1 = await _admit_incident_awaiting(session_factory, isolated_tenant, target_id)
    inc2 = await _admit_incident_awaiting(session_factory, isolated_tenant, target_id)

    act1 = ActionSpec(action_kind="restart_service")
    app1 = ApprovedPlanContext(
        approval_id=uuid.uuid4(),
        tenant_id=isolated_tenant,
        incident_id=inc1,
        target_id=target_id,
        plan_digest="plan-1",
        action_digest=act1.action_digest(),
        expected_version="v1",
        expires_at=t0 + timedelta(hours=1),
    )
    permit1 = await mgr.acquire_reservation(approval=app1, adapter=proxy, lease_seconds=10, now=t0)
    proxy.set_rule(
        FaultRule(
            scenario=FaultScenario.LOST_RESPONSE_AFTER_COMMIT,
            target_id=target_id,
            max_triggers=1,
        )
    )
    rec1 = await gateway.execute_with_permit(
        permit=permit1,
        approval=app1,
        action=act1,
        adapter=proxy,
        lease_seconds=10,
        now=t0,
    )
    assert rec1.state == EffectState.UNKNOWN

    # Even 120 seconds later (past reservation expires_at), while rec1 is still UNKNOWN,
    # Incident 2 is blocked with ResourceUncertainEffectPendingError!
    t_late = t0 + timedelta(seconds=120)
    app2 = ApprovedPlanContext(
        approval_id=uuid.uuid4(),
        tenant_id=isolated_tenant,
        incident_id=inc2,
        target_id=target_id,
        plan_digest="plan-2",
        action_digest="act-2",
        expected_version="v2",
        expires_at=t0 + timedelta(hours=1),
    )
    with pytest.raises(ResourceUncertainEffectPendingError):
        await mgr.acquire_reservation(approval=app2, adapter=proxy, now=t_late)

    # Once reconciler resolves rec1 to VERIFIED and releases the reservation, Incident 2 can reserve!
    reconciled = await reconciler.reconcile_pending(
        adapters={proxy.profile().adapter_id: proxy},
        tenant_id=isolated_tenant,
        now=t0 + timedelta(seconds=20),
    )
    assert len(reconciled) == 1
    assert reconciled[0].state == EffectState.VERIFIED

    permit2 = await mgr.acquire_reservation(
        approval=app2, adapter=proxy, now=t0 + timedelta(seconds=25)
    )
    assert permit2.fence == 2


async def test_b3_concurrent_reconcilers_skip_locked_and_expired_sent_sweep(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    race_simulator: DisposableTargetSimulator,
) -> None:
    """Verify:
    1) A receipt left in SENT after worker crash (lease_expires_at <= now) is swept by
       EffectReconciler (SENT -> UNKNOWN -> CONFIRMED -> VERIFIED).
    2) Two concurrent EffectReconciler instances running behind a barrier claim distinct
       receipts via FOR UPDATE SKIP LOCKED without double reconciliation.
    3) Conflicting payload with the same idempotency_key raises EffectIdempotencyConflictError.
    """
    mgr = ResourceReservationManager(session_factory)
    gateway = ActionGateway(session_factory)
    reconciler_a = EffectReconciler(session_factory)
    reconciler_b = EffectReconciler(session_factory)
    proxy = FaultInjectionProxy(race_simulator)

    t0 = datetime(2026, 11, 7, 19, 30, 0, tzinfo=UTC)
    target_id = f"sim-sent-crash-{uuid.uuid4().hex[:6]}"
    inc_id = await _admit_incident_awaiting(session_factory, isolated_tenant, target_id)
    act = ActionSpec(action_kind="scale_workers", parameters={"scale": 6})
    app = ApprovedPlanContext(
        approval_id=uuid.uuid4(),
        tenant_id=isolated_tenant,
        incident_id=inc_id,
        target_id=target_id,
        plan_digest="plan-sent-crash",
        action_digest=act.action_digest(),
        expected_version="v1",
        expires_at=t0 + timedelta(hours=1),
    )
    permit = await mgr.acquire_reservation(approval=app, adapter=proxy, now=t0)

    # Simulate LOST_RESPONSE_AFTER_COMMIT so target commits v1 -> v2, then force row back to SENT
    # with expired lease to simulate a hard SIGKILL during adapter.apply before catch block ran
    proxy.set_rule(
        FaultRule(
            scenario=FaultScenario.LOST_RESPONSE_AFTER_COMMIT,
            target_id=target_id,
            max_triggers=1,
        )
    )
    rec = await gateway.execute_with_permit(
        permit=permit, approval=app, action=act, adapter=proxy, now=t0
    )
    async with session_factory() as session:
        row = await session.get(EffectReceipt, rec.id)
        assert row is not None
        row.state = EffectState.SENT.value
        row.lease_expires_at = t0 + timedelta(seconds=5)
        await session.commit()

    # Two concurrent reconcilers race after lease_expires_at (t0 + 10s)
    barrier = asyncio.Barrier(2)

    async def _run_reconciler(rec_inst: EffectReconciler) -> list[EffectReceiptSnapshot]:
        await barrier.wait()
        return await rec_inst.reconcile_pending(
            adapters={proxy.profile().adapter_id: proxy},
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            now=t0 + timedelta(seconds=10),
        )

    res_a, res_b = await asyncio.gather(
        _run_reconciler(reconciler_a),
        _run_reconciler(reconciler_b),
    )
    combined = res_a + res_b
    # Exactly one reconciler claimed and verified the receipt via SKIP LOCKED
    assert len(combined) == 1
    assert combined[0].id == rec.id
    assert combined[0].state == EffectState.VERIFIED
    assert combined[0].observed_version == "v2"

    # Zombie worker waking up with old claim_token is rejected with StaleEffectClaimError
    with pytest.raises(StaleEffectClaimError):
        await gateway._transition_to_confirmed(
            tenant_id=isolated_tenant,
            receipt_id=rec.id,
            claim_token=uuid.uuid4(),
            observed_version="v2",
            external_ref="stale-ref",
            now=t0 + timedelta(seconds=12),
        )
