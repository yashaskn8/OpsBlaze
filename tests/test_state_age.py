"""B4 pending age must not reset on retry lease updates."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.models import Incident, OutboxEntry
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import Settings
from backend.workers.state_age import scan_stuck


@pytest.mark.asyncio
async def test_monitor_finds_executing_incident_and_old_retry(sessions_fixture, monkeypatch):
    monkeypatch.setattr(
        "backend.workers.state_age.get_session_maker",
        lambda: sessions_fixture,
    )
    monkeypatch.setattr(
        "backend.workers.state_age.get_settings",
        lambda: Settings(stuck_after_seconds=300),
    )
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "stuck", IncidentCreate(target_id="checkout")
        )
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    async with sessions_fixture() as session:
        async with session.begin():
            stored = await session.get(Incident, incident.id)
            stored.status_projection = "EXECUTING"
            stored.phase_entered_at = old
            intent = await session.scalar(
                select(OutboxEntry).where(OutboxEntry.incident_id == incident.id)
            )
            intent.created_at = old
            intent.available_at = later
            intent.attempt = 4
    assert await scan_stuck() == 2

    async with sessions_fixture() as session:
        async with session.begin():
            stored = await session.get(Incident, incident.id)
            stored.status_projection = "VERIFIED"
            intent = await session.scalar(
                select(OutboxEntry).where(OutboxEntry.incident_id == incident.id)
            )
            intent.delivered_at = datetime.now(timezone.utc)
    assert await scan_stuck() == 0
