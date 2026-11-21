"""Scheduled retention operates in bounded, idempotent transactions."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from backend.models import AdmissionUsage, Incident, ModelTokenWindow, UIEvent, UIEventWatermark
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import Settings
from backend.workers.maintenance import run_maintenance


@pytest.mark.asyncio
async def test_one_shot_maintenance_keeps_durable_incident_records(sessions_fixture):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    old = now - timedelta(days=9)
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "tenant", "maintenance", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        async with session.begin():
            await session.execute(
                update(UIEvent)
                .where(UIEvent.incident_id == incident.id)
                .values(created_at=old)
            )
            session.add(ModelTokenWindow(
                tenant_id="tenant", window_start=old,
                reserved_tokens=100, used_tokens=30,
            ))
            session.add(AdmissionUsage(
                tenant_id="other", window_start=old, accepted=3,
            ))

    result = await run_maintenance(sessions_fixture, Settings(), now=now)
    assert result == {
        "capsules_scrubbed": 0,
        "lessons_revoked": 0,
        "tenant_capsules_scrubbed": 0,
        "tenant_lessons_revoked": 0,
        "ui_events_deleted": 1,
        "admission_windows_deleted": 1,
        "ai_windows_deleted": 1,
    }
    assert all(v == 0 for v in (
        await run_maintenance(sessions_fixture, Settings(), now=now)
    ).values())
    async with sessions_fixture() as session:
        assert await session.get(Incident, incident.id) is not None
        watermark = await session.get(UIEventWatermark, ("tenant", incident.id))
        assert watermark.last_purged_id > 0
        assert not (await session.scalars(
            select(ModelTokenWindow).where(ModelTokenWindow.window_start == old)
        )).all()


@pytest.mark.asyncio
async def test_maintenance_rejects_unbounded_schedule_settings(sessions_fixture):
    with pytest.raises(ValueError, match="batch count"):
        await run_maintenance(sessions_fixture, Settings(), max_batches=0)
    with pytest.raises(ValueError, match="timezone"):
        await run_maintenance(
            sessions_fixture, Settings(), now=datetime(2020, 1, 1)
        )
    with pytest.raises(ValueError, match="UI retention"):
        await run_maintenance(
            sessions_fixture, Settings(ui_event_retention_days=0)
        )
