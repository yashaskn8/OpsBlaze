import pytest
from sqlalchemy import select, update

from backend.models import Incident, OutboxEntry
from backend.schemas import IncidentCreate
from backend.services import (
    VersionConflict,
    acknowledge_review,
    admit_incident,
    request_cancellation,
)


@pytest.mark.asyncio
async def test_cancel_requires_current_version(sessions_fixture) -> None:
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "a", "key", IncidentCreate(target_id="db")
        )
    async with sessions_fixture() as session:
        with pytest.raises(VersionConflict):
            await request_cancellation(session, "a", incident.id, 99)
    async with sessions_fixture() as session:
        cancelled = await request_cancellation(session, "a", incident.id, 1)
        assert cancelled.status_projection == "CANCEL_REQUESTED"
        assert cancelled.status_version == 2
    async with sessions_fixture() as session:
        intents = list((await session.scalars(select(OutboxEntry).order_by(OutboxEntry.id))).all())
        assert [i.kind for i in intents] == ["START_WORKFLOW", "CANCEL_WORKFLOW"]


@pytest.mark.asyncio
async def test_review_requires_current_state_and_version(sessions_fixture) -> None:
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "a", "key", IncidentCreate(target_id="db")
        )
    async with sessions_fixture() as session:
        with pytest.raises(VersionConflict):
            await acknowledge_review(session, "a", incident.id, 1)
    async with sessions_fixture() as session:
        async with session.begin():
            await session.execute(
                update(Incident)
                .where(Incident.id == incident.id)
                .values(status_projection="AWAITING_APPROVAL", status_version=2)
            )
    async with sessions_fixture() as session:
        updated = await acknowledge_review(session, "a", incident.id, 2)
        assert updated.status_projection == "REVIEW_ACKNOWLEDGED"
        assert updated.status_version == 3
    async with sessions_fixture() as session:
        with pytest.raises(VersionConflict):
            await acknowledge_review(session, "a", incident.id, 2)
