"""B1 state projections are monotonic, idempotent, and phase-safe."""

import pytest
from sqlalchemy import select, update

from backend.models import Incident, UIEvent
from backend.schemas import IncidentCreate
from backend.services import VersionConflict, admit_incident, request_cancellation
from backend.workflows.activities import project_incident_status


@pytest.mark.asyncio
async def test_projection_rejects_out_of_order_terminal_and_duplicate_delivery(
    sessions_fixture, monkeypatch,
):
    monkeypatch.setattr(
        "backend.workflows.activities.get_session_maker",
        lambda: sessions_fixture,
    )
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "ordered", IncidentCreate(target_id="checkout")
        )
    incident_id = str(incident.id)
    await project_incident_status("t", incident_id, "VERIFIED")
    await project_incident_status("t", incident_id, "AWAITING_APPROVAL")
    await project_incident_status("t", incident_id, "INVESTIGATING")
    await project_incident_status("t", incident_id, "INVESTIGATING")
    await project_incident_status("t", incident_id, "AWAITING_APPROVAL")
    await project_incident_status("t", incident_id, "EXECUTING")
    await project_incident_status("t", incident_id, "VERIFIED")

    async with sessions_fixture() as session:
        current = await session.get(Incident, incident.id)
        assert current.status_projection == "AWAITING_APPROVAL"
        assert current.status_version == 3
        events = (await session.scalars(
            select(UIEvent).where(
                UIEvent.tenant_id == "t",
                UIEvent.incident_id == incident.id,
            ).order_by(UIEvent.id)
        )).all()
        assert [event.kind for event in events] == [
            "ADMITTED", "INVESTIGATING", "AWAITING_APPROVAL",
        ]


@pytest.mark.asyncio
async def test_approval_to_execution_to_verification_is_one_way(
    sessions_fixture, monkeypatch,
):
    monkeypatch.setattr(
        "backend.workflows.activities.get_session_maker",
        lambda: sessions_fixture,
    )
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "execute", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        async with session.begin():
            await session.execute(
                update(Incident).where(Incident.id == incident.id)
                .values(status_projection="PLAN_DECIDED", status_version=7)
            )

    await project_incident_status("t", str(incident.id), "EXECUTING")
    await project_incident_status("t", str(incident.id), "EXECUTING")
    await project_incident_status("t", str(incident.id), "VERIFIED")
    await project_incident_status("t", str(incident.id), "NEEDS_REVIEW")
    async with sessions_fixture() as session:
        current = await session.get(Incident, incident.id)
        assert current.status_projection == "VERIFIED"
        assert current.status_version == 9
        entries = (await session.scalars(
            select(UIEvent).where(
                UIEvent.incident_id == incident.id,
                UIEvent.tenant_id == "t",
            )
        )).all()
        assert [entry.kind for entry in entries] == [
            "ADMITTED", "EXECUTING", "VERIFIED",
        ]


@pytest.mark.asyncio
async def test_cancel_rejected_after_effect_checkpoint(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "past-checkpoint", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        async with session.begin():
            await session.execute(
                update(Incident).where(Incident.id == incident.id)
                .values(status_projection="EXECUTING", status_version=3)
            )
    async with sessions_fixture() as session:
        with pytest.raises(VersionConflict):
            await request_cancellation(session, "t", incident.id, 3)
    async with sessions_fixture() as session:
        current = await session.get(Incident, incident.id)
        assert current.status_projection == "EXECUTING"
