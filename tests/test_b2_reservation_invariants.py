"""B2 stateful randomized & lease-renewal invariant tests."""

from datetime import datetime, timedelta, timezone
import random
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.adapters.simulator import SimulatorAdapter, StaleFence, TargetVersionConflict
from backend.effects.reservations import (
    ReservationBusy,
    expire_stale_reservations,
    release_simulator_reservation,
    renew_simulator_reservation,
    reserve_simulator_target,
)
from backend.models import ResourceEpoch, ResourceReservation, SimulatorTarget
from backend.schemas import IncidentCreate
from backend.services import admit_incident


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
