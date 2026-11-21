import pytest
from sqlalchemy import select

from backend.models import AdmissionUsage
from backend.schemas import IncidentCreate
from backend.services import (
    IdempotencyConflict, RateLimited, admit_incident, reserve_admission_slot
)


@pytest.mark.asyncio
async def test_quota_slot_is_atomic_and_bounded(sessions_fixture) -> None:
    async with sessions_fixture() as session:
        async with session.begin():
            await reserve_admission_slot(session, "tenant-a", limit=1)
    async with sessions_fixture() as session:
        with pytest.raises(RateLimited):
            async with session.begin():
                await reserve_admission_slot(session, "tenant-a", limit=1)
    async with sessions_fixture() as session:
        async with session.begin():
            await reserve_admission_slot(session, "tenant-b", limit=1)
    async with sessions_fixture() as session:
        usage = list((await session.scalars(select(AdmissionUsage))).all())
        assert sum(item.accepted for item in usage if item.tenant_id == "tenant-a") == 1
        assert sum(item.accepted for item in usage if item.tenant_id == "tenant-b") == 1


@pytest.mark.asyncio
async def test_retrying_same_key_does_not_consume_another_slot(sessions_fixture) -> None:
    payload = IncidentCreate(target_id="checkout")
    async with sessions_fixture() as session:
        first = await admit_incident(session, "a", "same", payload)
    async with sessions_fixture() as session:
        duplicate = await admit_incident(session, "a", "same", payload)
        assert first.id == duplicate.id
    async with sessions_fixture() as session:
        usage = list((await session.scalars(select(AdmissionUsage))).all())
        assert sum(item.accepted for item in usage) == 1


@pytest.mark.asyncio
async def test_changed_payload_rejected_without_quota_increment(sessions_fixture) -> None:
    async with sessions_fixture() as session:
        await admit_incident(session, "a", "same", IncidentCreate(target_id="one"))
    async with sessions_fixture() as session:
        with pytest.raises(IdempotencyConflict):
            await admit_incident(session, "a", "same", IncidentCreate(target_id="two"))
    async with sessions_fixture() as session:
        usage = list((await session.scalars(select(AdmissionUsage))).all())
        assert sum(item.accepted for item in usage) == 1


@pytest.mark.asyncio
async def test_active_capacity_rejects_then_reopens_after_terminal(
    sessions_fixture, monkeypatch,
):
    from sqlalchemy import update
    from backend.models import Incident
    from backend.schemas import IncidentCreate
    from backend.services import RateLimited, admit_incident
    from backend.settings import Settings

    monkeypatch.setattr(
        "backend.services.get_settings",
        lambda: Settings(
            max_active_incidents_per_tenant=1,
            max_active_incidents_global=2,
        ),
    )
    async with sessions_fixture() as session:
        first = await admit_incident(
            session, "t", "first", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        # An idempotent replay is not a new capacity reservation.
        replay = await admit_incident(
            session, "t", "first", IncidentCreate(target_id="checkout")
        )
        assert replay.id == first.id
    async with sessions_fixture() as session:
        with pytest.raises(RateLimited, match="tenant active"):
            await admit_incident(
                session, "t", "second", IncidentCreate(target_id="billing")
            )

    async with sessions_fixture() as session:
        async with session.begin():
            await session.execute(
                update(Incident)
                .where(Incident.id == first.id)
                .values(status_projection="NEEDS_REVIEW")
            )
    async with sessions_fixture() as session:
        second = await admit_incident(
            session, "t", "second", IncidentCreate(target_id="billing")
        )
        assert second.id != first.id


@pytest.mark.asyncio
async def test_global_active_capacity_blocks_other_tenants(
    sessions_fixture, monkeypatch,
):
    from backend.schemas import IncidentCreate
    from backend.services import RateLimited, admit_incident
    from backend.settings import Settings

    monkeypatch.setattr(
        "backend.services.get_settings",
        lambda: Settings(
            max_active_incidents_per_tenant=10,
            max_active_incidents_global=1,
        ),
    )
    async with sessions_fixture() as session:
        await admit_incident(
            session, "alpha", "key-a", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        with pytest.raises(RateLimited, match="global active"):
            await admit_incident(
                session, "beta", "key-b", IncidentCreate(target_id="checkout")
            )
