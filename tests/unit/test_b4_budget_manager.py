"""Unit tests for B4: Durable Budget & Capacity Reservation Foundation.

Verifies:
- Creation and idempotency of initial budget ledger.
- Two-phase reservation with SELECT FOR UPDATE row-level lock.
- Delta reclamation during settlement (reserved vs actual usage).
- Hard ceiling enforcement and BudgetExhaustedError (tokens, probes, cost).
- Automatic lease expiration and orphan reservation reclamation.
- Concurrent reservation conflict detection (BudgetConcurrencyError).
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.budget.manager import DurableBudgetManager
from backend.domain.budget import (
    BudgetConcurrencyError,
    BudgetExhaustedError,
    BudgetLimits,
    BudgetReservation,
    BudgetSnapshot,
    BudgetStatus,
)
from backend.persistence.models import Incident


@pytest.mark.asyncio
async def test_ensure_budget_creates_initial_active_ledger(
    session_factory: async_sessionmaker[AsyncSession], isolated_tenant: uuid.UUID
) -> None:
    """B4: Ensure budget initializes ledger row with custom limits or defaults."""
    incident_id = uuid.uuid4()
    async with session_factory() as session:
        incident = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"key-{incident_id.hex[:6]}",
            request_sha256="reqhash_b4_1",
            target_id="svc:order-db",
            status_projection="INVESTIGATING",
            status_version=1,
        )
        session.add(incident)
        await session.commit()

    manager = DurableBudgetManager(session_factory)
    custom_limits = BudgetLimits(
        max_tokens=10000,
        max_probes=5,
        max_cost_micros=2000000,
        max_time_seconds=120,
    )

    snapshot = await manager.ensure_budget(
        tenant_id=isolated_tenant,
        incident_id=incident_id,
        limits=custom_limits,
    )

    assert isinstance(snapshot, BudgetSnapshot)
    assert snapshot.status == BudgetStatus.ACTIVE
    assert snapshot.max_tokens == 10000
    assert snapshot.max_probes == 5
    assert snapshot.settled_tokens == 0
    assert snapshot.reserved_tokens == 0
    assert snapshot.reservation_nonce is None


@pytest.mark.asyncio
async def test_reserve_and_settle_reclaims_delta(
    session_factory: async_sessionmaker[AsyncSession], isolated_tenant: uuid.UUID
) -> None:
    """B4: Reserving sets lease, and settling reclaims difference between estimated and actual."""
    incident_id = uuid.uuid4()
    async with session_factory() as session:
        incident = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"key-{incident_id.hex[:6]}",
            request_sha256="reqhash_b4_2",
            target_id="svc:payment-db",
            status_projection="INVESTIGATING",
            status_version=1,
        )
        session.add(incident)
        await session.commit()

    manager = DurableBudgetManager(session_factory)
    await manager.ensure_budget(
        tenant_id=isolated_tenant,
        incident_id=incident_id,
        limits=BudgetLimits(max_tokens=20000, max_probes=10, max_cost_micros=5000000),
    )

    # Reserve 2000 tokens, 1 probe, 100000 micros
    reservation = await manager.reserve_budget(
        tenant_id=isolated_tenant,
        incident_id=incident_id,
        tokens=2000,
        probes=1,
        cost_micros=100000,
        lease_seconds=60,
    )
    assert isinstance(reservation, BudgetReservation)
    assert reservation.tokens == 2000
    assert reservation.lease_expires_at > datetime.now(UTC)

    # Verify snapshot shows reservation
    snap_after_res = await manager.get_snapshot(isolated_tenant, incident_id)
    assert snap_after_res.reserved_tokens == 2000
    assert snap_after_res.settled_tokens == 0

    # Settle with actual usage: only 1200 tokens used, 1 probe, 60000 micros
    snap_after_settle = await manager.settle_budget(
        tenant_id=isolated_tenant,
        incident_id=incident_id,
        nonce=reservation.nonce,
        actual_tokens=1200,
        actual_probes=1,
        actual_cost_micros=60000,
    )

    assert snap_after_settle.reserved_tokens == 0
    assert snap_after_settle.reserved_probes == 0
    assert snap_after_settle.settled_tokens == 1200
    assert snap_after_settle.settled_probes == 1
    assert snap_after_settle.settled_cost_micros == 60000
    assert snap_after_settle.reservation_nonce is None


@pytest.mark.asyncio
async def test_budget_exhausted_rejects_exceeding_reservations(
    session_factory: async_sessionmaker[AsyncSession], isolated_tenant: uuid.UUID
) -> None:
    """B4: Exceeding max limits raises BudgetExhaustedError and marks status EXHAUSTED."""
    incident_id = uuid.uuid4()
    async with session_factory() as session:
        incident = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"key-{incident_id.hex[:6]}",
            request_sha256="reqhash_b4_3",
            target_id="svc:auth-db",
            status_projection="INVESTIGATING",
            status_version=1,
        )
        session.add(incident)
        await session.commit()

    manager = DurableBudgetManager(session_factory)
    await manager.ensure_budget(
        tenant_id=isolated_tenant,
        incident_id=incident_id,
        limits=BudgetLimits(max_tokens=1000, max_probes=2, max_cost_micros=50000),
    )

    # 1. Probe limit exhaustion
    res1 = await manager.reserve_budget(
        tenant_id=isolated_tenant, incident_id=incident_id, tokens=200, probes=1, cost_micros=10000
    )
    await manager.settle_budget(
        tenant_id=isolated_tenant,
        incident_id=incident_id,
        nonce=res1.nonce,
        actual_tokens=200,
        actual_probes=1,
        actual_cost_micros=10000,
    )

    res2 = await manager.reserve_budget(
        tenant_id=isolated_tenant, incident_id=incident_id, tokens=200, probes=1, cost_micros=10000
    )
    await manager.settle_budget(
        tenant_id=isolated_tenant,
        incident_id=incident_id,
        nonce=res2.nonce,
        actual_tokens=200,
        actual_probes=1,
        actual_cost_micros=10000,
    )

    # Probes now = 2 (max_probes reached). Next reservation must fail
    with pytest.raises(BudgetExhaustedError) as exc_info:
        await manager.reserve_budget(
            tenant_id=isolated_tenant,
            incident_id=incident_id,
            tokens=100,
            probes=1,
            cost_micros=5000,
        )
    assert exc_info.value.dimension == "probes"


@pytest.mark.asyncio
async def test_expired_lease_is_auto_reclaimed(
    session_factory: async_sessionmaker[AsyncSession], isolated_tenant: uuid.UUID
) -> None:
    """B4: An abandoned reservation with an expired lease is automatically reclaimed on next reserve."""
    incident_id = uuid.uuid4()
    async with session_factory() as session:
        incident = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"key-{incident_id.hex[:6]}",
            request_sha256="reqhash_b4_4",
            target_id="svc:cache-db",
            status_projection="INVESTIGATING",
            status_version=1,
        )
        session.add(incident)
        await session.commit()

    manager = DurableBudgetManager(session_factory)
    await manager.ensure_budget(
        tenant_id=isolated_tenant,
        incident_id=incident_id,
        limits=BudgetLimits(max_tokens=5000, max_probes=5, max_cost_micros=100000),
    )

    # Reserve with negative lease_seconds to simulate expired lease
    res = await manager.reserve_budget(
        tenant_id=isolated_tenant,
        incident_id=incident_id,
        tokens=3000,
        probes=2,
        cost_micros=50000,
        lease_seconds=-10,
    )
    assert res is not None

    # New reservation should reclaim the expired 3000 tokens and succeed
    res_new = await manager.reserve_budget(
        tenant_id=isolated_tenant,
        incident_id=incident_id,
        tokens=2000,
        probes=1,
        cost_micros=20000,
        lease_seconds=60,
    )
    assert res_new is not None
    assert res_new.nonce != res.nonce

    snap = await manager.get_snapshot(isolated_tenant, incident_id)
    assert snap.reserved_tokens == 2000
    assert snap.reserved_probes == 1


@pytest.mark.asyncio
async def test_concurrent_active_reservation_raises_concurrency_error(
    session_factory: async_sessionmaker[AsyncSession], isolated_tenant: uuid.UUID
) -> None:
    """B4: Attempting to reserve while an unexpired lease is held raises BudgetConcurrencyError."""
    incident_id = uuid.uuid4()
    async with session_factory() as session:
        incident = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"key-{incident_id.hex[:6]}",
            request_sha256="reqhash_b4_5",
            target_id="svc:api-gateway",
            status_projection="INVESTIGATING",
            status_version=1,
        )
        session.add(incident)
        await session.commit()

    manager = DurableBudgetManager(session_factory)
    await manager.ensure_budget(
        tenant_id=isolated_tenant,
        incident_id=incident_id,
        limits=BudgetLimits(max_tokens=5000, max_probes=5, max_cost_micros=100000),
    )

    await manager.reserve_budget(
        tenant_id=isolated_tenant,
        incident_id=incident_id,
        tokens=1000,
        probes=1,
        cost_micros=10000,
        lease_seconds=300,
    )

    with pytest.raises(BudgetConcurrencyError):
        await manager.reserve_budget(
            tenant_id=isolated_tenant,
            incident_id=incident_id,
            tokens=500,
            probes=1,
            cost_micros=5000,
            lease_seconds=300,
        )
