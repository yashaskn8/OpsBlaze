
import pytest
from sqlalchemy import select

from backend.models import UIEvent
from backend.schemas import IncidentCreate
from backend.services import admit_incident, request_cancellation


@pytest.mark.asyncio
async def test_admission_and_cancel_have_monotonic_durable_events(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "tenant", "key", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        again = await admit_incident(
            session, "tenant", "key", IncidentCreate(target_id="checkout")
        )
        assert again.id == incident.id
    async with sessions_fixture() as session:
        await request_cancellation(session, "tenant", incident.id, 1)
    async with sessions_fixture() as session:
        rows = (await session.scalars(
            select(UIEvent)
            .where(UIEvent.tenant_id == "tenant")
            .order_by(UIEvent.id)
        )).all()
        assert len(rows) == 2
        assert [row.kind for row in rows] == ["ADMITTED", "CANCEL_REQUESTED"]
        assert [row.projection_version for row in rows] == [1, 2]
        assert rows[0].id < rows[1].id
