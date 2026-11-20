"""B1 discrepancy scan never guesses workflow state or mutates incidents."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.models import Incident, OutboxEntry
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.workers.reconciler import compare_temporal_projection


class FakeWorkflow:
    def __init__(self, status=None, error=None):
        self.status = status
        self.error = error

    async def describe(self):
        if self.error:
            raise self.error
        return type("Description", (), {"status": self.status})()


class FakeTemporal:
    def __init__(self, status=None, error=None):
        self.status = status
        self.error = error

    def get_workflow_handle(self, workflow_id):
        return FakeWorkflow(self.status, self.error)


@pytest.mark.asyncio
async def test_aged_temporal_projection_report_is_read_only(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "tenant", "reconcile", IncidentCreate(target_id="checkout")
        )
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    async with sessions_fixture() as session:
        async with session.begin():
            current = await session.get(Incident, incident.id)
            current.status_projection = "EXECUTING"
            current.phase_entered_at = old
            start = await session.scalar(
                select(OutboxEntry).where(
                    OutboxEntry.incident_id == incident.id,
                    OutboxEntry.kind == "START_WORKFLOW",
                )
            )
            start.delivered_at = old

    completed = await compare_temporal_projection(
        sessions_fixture,
        FakeTemporal(status=type("Status", (), {"name": "COMPLETED"})()),
        stale_seconds=30,
    )
    assert len(completed) == 1
    assert completed[0]["reason"] == "PROJECTION_LAGS_TERMINAL_HISTORY"
    assert completed[0]["safe_action"] == "INSPECT_ONLY"

    unknown = await compare_temporal_projection(
        sessions_fixture, FakeTemporal(error=ConnectionError("offline")),
        stale_seconds=30,
    )
    assert unknown[0]["reason"] == "TEMPORAL_HISTORY_UNAVAILABLE_OR_UNKNOWN"
    assert unknown[0]["temporal_status"] == "UNKNOWN"

    async with sessions_fixture() as session:
        still = await session.get(Incident, incident.id)
        assert still.status_projection == "EXECUTING"


@pytest.mark.asyncio
async def test_human_approval_wait_is_not_misdiagnosed_as_worker_stall(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "tenant", "human", IncidentCreate(target_id="checkout")
        )
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    async with sessions_fixture() as session:
        async with session.begin():
            current = await session.get(Incident, incident.id)
            current.status_projection = "AWAITING_APPROVAL"
            current.phase_entered_at = old
            start = await session.scalar(
                select(OutboxEntry).where(OutboxEntry.incident_id == incident.id)
            )
            start.delivered_at = old

    assert await compare_temporal_projection(
        sessions_fixture, FakeTemporal(status="RUNNING"), stale_seconds=30,
    ) == []
