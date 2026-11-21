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


@pytest.mark.asyncio
async def test_redteam_age_monitor_retries_after_database_failure(monkeypatch, caplog):
    import asyncio
    from backend.workers import state_age

    attempts = []

    async def fake_scan():
        attempts.append("scan")
        if len(attempts) == 1:
            raise ConnectionError("synthetic database outage")
        return 0

    async def fake_sleep(delay):
        assert delay == 30
        if len(attempts) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(state_age, "scan_stuck", fake_scan)
    monkeypatch.setattr(state_age.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await state_age.main()
    assert len(attempts) == 2
    assert "B4 state-age scan failed" in caplog.text
    assert "synthetic database outage" in caplog.text


@pytest.mark.asyncio
async def test_redteam_stuck_monitor_never_reports_100_as_the_total_of_101(
    sessions_fixture, monkeypatch, caplog,
):
    from uuid import uuid4

    monkeypatch.setattr(
        "backend.workers.state_age.get_session_maker", lambda: sessions_fixture,
    )
    monkeypatch.setattr(
        "backend.workers.state_age.get_settings",
        lambda: Settings(stuck_after_seconds=300),
    )
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    async with sessions_fixture() as session:
        async with session.begin():
            session.add_all([
                Incident(
                    id=uuid4(), tenant_id="t", client_key=f"old-{index}",
                    request_sha256="a" * 64, target_id="checkout",
                    status_projection="EXECUTING", status_version=1,
                    phase_entered_at=old,
                )
                for index in range(101)
            ])
    assert await scan_stuck() == 101
    assert "incidents=1 outbox=0" in caplog.text
    assert caplog.text.count("Incident exceeded phase-age threshold") == 100
