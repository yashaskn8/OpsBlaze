"""B2 stateful randomized & lease-renewal invariant tests."""

import asyncio
from datetime import datetime, timedelta, timezone
import random
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.adapters.simulator import SimulatorAdapter, StaleFence, TargetVersionConflict
from backend.effects import reservations
from backend.effects.reservations import (
    ReservationBusy,
    expire_stale_reservations,
    release_simulator_reservation,
    renew_simulator_reservation,
    reserve_simulator_target,
)
from backend.models import ResourceEpoch, ResourceReservation, SimulatorAppliedEffect, SimulatorTarget
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import get_settings


@pytest.mark.asyncio
async def test_b2_lease_renewal_enforces_fence_owner_and_expiry(sessions_fixture):
    async with sessions_fixture.begin() as session:
        session.add(SimulatorTarget(
            tenant_id="t-b2", target_id="svc-a", version=1, healthy=False, last_fence=0,
        ))
        session.add(ResourceEpoch(
            tenant_id="t-b2", target_id="svc-a", latest_fence=0, version_seen=1,
        ))
    async with sessions_fixture() as session:
        inc_a = await admit_incident(session, "t-b2", "k-a", IncidentCreate(target_id="svc-a"))
    async with sessions_fixture() as session:
        inc_b = await admit_incident(session, "t-b2", "k-b", IncidentCreate(target_id="svc-a"))

    base = datetime.now(timezone.utc)
    permit = await reserve_simulator_target(
        sessions_fixture, "t-b2", "svc-a", inc_a.id, "a" * 64, lease_seconds=30,
    )
    renewed = await renew_simulator_reservation(
        sessions_fixture, "t-b2", "svc-a", inc_a.id, permit.fence,
        lease_seconds=120, now=base,
    )
    assert renewed is not None
    assert renewed.fence == permit.fence
    assert renewed.expires_at > permit.expires_at

    # Wrong incident and stale fence must be refused.
    assert await renew_simulator_reservation(
        sessions_fixture, "t-b2", "svc-a", inc_b.id, permit.fence, lease_seconds=60, now=base,
    ) is None
    assert await renew_simulator_reservation(
        sessions_fixture, "t-b2", "svc-a", inc_a.id, permit.fence - 1, lease_seconds=60, now=base,
    ) is None

    # Expired lease cannot be renewed even by original holder.
    assert await renew_simulator_reservation(
        sessions_fixture, "t-b2", "svc-a", inc_a.id, permit.fence,
        lease_seconds=60, now=renewed.expires_at + timedelta(seconds=1),
    ) is None


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["renew", "reserve"])
async def test_b2_checks_expiry_after_waiting_for_postgres_lock(operation, monkeypatch):
    database_url = get_settings().database_url
    assert database_url.startswith("postgresql+asyncpg:")
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"lease-clock-{uuid4().hex}"
    target = "checkout"
    lock_requested = asyncio.Event()

    class WaitingSession(AsyncSession):
        async def get(self, entity, ident, **kwargs):
            if entity is ResourceEpoch:
                lock_requested.set()
            return await super().get(entity, ident, **kwargs)

    contenders = async_sessionmaker(engine, class_=WaitingSession, expire_on_commit=False)
    try:
        async with sessions.begin() as session:
            session.add_all([
                SimulatorTarget(tenant_id=tenant, target_id=target, version=1, healthy=False, last_fence=0),
                ResourceEpoch(tenant_id=tenant, target_id=target, latest_fence=0, version_seen=1),
            ])
        async with sessions() as session:
            holder = await admit_incident(session, tenant, "holder", IncidentCreate(target_id=target))
        original = await reserve_simulator_target(
            sessions, tenant, target, holder.id, "a" * 64, lease_seconds=30,
        )
        clock = original.expires_at - timedelta(seconds=1)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.astimezone(tz)

        monkeypatch.setattr(reservations, "datetime", Clock)
        async with asyncio.TaskGroup() as tasks:
            async with sessions.begin() as session:
                await session.get(ResourceEpoch, (tenant, target), with_for_update=True)
                if operation == "renew":
                    waiting = tasks.create_task(renew_simulator_reservation(
                        contenders, tenant, target, holder.id, original.fence,
                    ))
                else:
                    waiting = tasks.create_task(reserve_simulator_target(
                        contenders, tenant, target, holder.id, original.action_digest,
                    ))
                await asyncio.wait_for(lock_requested.wait(), timeout=5)
                assert not waiting.done()
                clock = original.expires_at + timedelta(seconds=1)

        result = waiting.result()
        async with sessions() as session:
            stored = await session.get(ResourceReservation, (tenant, target))
            if operation == "renew":
                assert result is None
                assert stored.expires_at == original.expires_at
                assert stored.fence == original.fence
            else:
                assert result.fence > original.fence
                assert stored.expires_at == result.expires_at == clock + timedelta(seconds=60)
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["renew", "reserve"])
async def test_b2_contention_times_out_without_mutating_the_reservation(operation):
    database_url = get_settings().database_url
    assert database_url.startswith("postgresql+asyncpg:")
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"lease-contention-{uuid4().hex}"
    target = "checkout"
    digest = "c" * 64
    try:
        async with sessions.begin() as session:
            session.add_all([
                SimulatorTarget(tenant_id=tenant, target_id=target, version=1, healthy=False, last_fence=0),
                ResourceEpoch(tenant_id=tenant, target_id=target, latest_fence=0, version_seen=1),
            ])
        async with sessions() as session:
            holder = await admit_incident(session, tenant, "holder", IncidentCreate(target_id=target))
        permit = await reserve_simulator_target(sessions, tenant, target, holder.id, digest)
        async with sessions.begin() as session:
            epoch = await session.get(ResourceEpoch, (tenant, target), with_for_update=True)
            if operation == "renew":
                request = renew_simulator_reservation(sessions, tenant, target, holder.id, permit.fence)
            else:
                request = reserve_simulator_target(sessions, tenant, target, holder.id, digest)
            with pytest.raises(ReservationBusy):
                await asyncio.wait_for(request, timeout=3)
            assert epoch.latest_fence == permit.fence

        async with sessions() as session:
            row = await session.get(ResourceReservation, (tenant, target))
            assert row.fence == permit.fence and row.expires_at == permit.expires_at
        assert await reserve_simulator_target(sessions, tenant, target, holder.id, digest) == permit
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_b2_concurrent_simulator_retries_return_the_committed_effect():
    database_url = get_settings().database_url
    assert database_url.startswith("postgresql+asyncpg:")
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"effect-retry-{uuid4().hex}"
    target = "checkout"
    requests = asyncio.Barrier(2)

    class ConcurrentSession(AsyncSession):
        async def get(self, entity, ident, **kwargs):
            if entity is ResourceEpoch:
                await asyncio.wait_for(requests.wait(), timeout=5)
            return await super().get(entity, ident, **kwargs)

    try:
        async with sessions.begin() as session:
            session.add_all([
                SimulatorTarget(tenant_id=tenant, target_id=target, version=1, healthy=False, last_fence=0),
                ResourceEpoch(tenant_id=tenant, target_id=target, latest_fence=0, version_seen=1),
            ])
        async with sessions() as session:
            incident = await admit_incident(session, tenant, "retry", IncidentCreate(target_id=target))
        permit = await reserve_simulator_target(sessions, tenant, target, incident.id, "b" * 64)
        effect_id = uuid4()
        contenders = async_sessionmaker(engine, class_=ConcurrentSession, expire_on_commit=False)
        adapter = SimulatorAdapter(contenders)
        results = await asyncio.wait_for(asyncio.gather(
            adapter.apply(effect_id, permit), adapter.apply(effect_id, permit),
            return_exceptions=True,
        ), timeout=10)

        assert results == [2, 2]
        async with sessions() as session:
            current = await session.get(SimulatorTarget, (tenant, target))
            durable = await session.get(SimulatorAppliedEffect, effect_id)
            assert current.version == durable.observed_version == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_b2_stateful_fencing_invariants_across_120_transitions(sessions_fixture):
    async with sessions_fixture.begin() as session:
        session.add(SimulatorTarget(
            tenant_id="t-prop", target_id="svc-p", version=1, healthy=False, last_fence=0,
        ))
        session.add(ResourceEpoch(
            tenant_id="t-prop", target_id="svc-p", latest_fence=0, version_seen=1,
        ))
    incidents = []
    for i in range(3):
        async with sessions_fixture() as session:
            inc = await admit_incident(
                session, "t-prop", f"prop-{i}", IncidentCreate(target_id="svc-p"),
            )
        incidents.append(inc.id)

    rng = random.Random(2027)
    adapter = SimulatorAdapter(sessions_fixture)
    active_permits: dict = {}
    prev_fence = 0

    for step in range(120):
        op = rng.choice(["reserve", "renew", "release", "expire", "apply"])
        inc_id = rng.choice(incidents)
        digest = f"{step:064x}"
        if op == "reserve":
            try:
                p = await reserve_simulator_target(
                    sessions_fixture, "t-prop", "svc-p", inc_id, digest, lease_seconds=60,
                )
                active_permits[inc_id] = p
            except ReservationBusy:
                pass
        elif op == "renew" and inc_id in active_permits:
            p = active_permits[inc_id]
            updated = await renew_simulator_reservation(
                sessions_fixture, "t-prop", "svc-p", inc_id, p.fence, lease_seconds=90,
            )
            if updated is not None:
                active_permits[inc_id] = updated
        elif op == "release" and inc_id in active_permits:
            p = active_permits[inc_id]
            await release_simulator_reservation(
                sessions_fixture, "t-prop", "svc-p", inc_id, p.fence,
            )
        elif op == "expire":
            await expire_stale_reservations(
                sessions_fixture, limit=10, now=datetime.now(timezone.utc) + timedelta(seconds=5),
            )
        elif op == "apply" and inc_id in active_permits:
            p = active_permits[inc_id]
            try:
                ver = await adapter.apply(uuid4(), p)
                assert ver == p.expected_version + 1
            except (StaleFence, TargetVersionConflict):
                pass

        async with sessions_fixture() as session:
            epoch = await session.get(ResourceEpoch, ("t-prop", "svc-p"))
            reserved_rows = (await session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.tenant_id == "t-prop",
                    ResourceReservation.target_id == "svc-p",
                    ResourceReservation.state == "RESERVED",
                )
            )).all()
            assert epoch.latest_fence >= prev_fence
            prev_fence = epoch.latest_fence
            assert len(reserved_rows) <= 1
            if reserved_rows:
                assert reserved_rows[0].fence <= epoch.latest_fence
