from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.models import AdmissionUsage, Incident, OutboxEntry
from backend.schemas import IncidentCreate
from backend.services import (
    IdempotencyConflict, RateLimited, admit_incident, reserve_admission_slot
)


@pytest.mark.asyncio
async def test_quota_slot_is_atomic_and_bounded(sessions_fixture) -> None:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
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
