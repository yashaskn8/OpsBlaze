"""B4: durable queue p95, aged work and retry pressure with tenant isolation."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from backend.api import app
from backend.database import get_session
from backend.models import Incident, OutboxEntry
from backend.operations import queue_health
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import Settings, get_settings


@pytest.mark.asyncio
async def test_redteam_queue_health_exact_p95_and_tenant_scope(sessions_fixture):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    own_ids = []
    for index in range(5):
        async with sessions_fixture() as session:
            incident = await admit_incident(
                session, "alpha", f"queue-{index}",
                IncidentCreate(target_id="checkout"),
            )
            own_ids.append(incident.id)
    async with sessions_fixture() as session:
        foreign = await admit_incident(
            session, "beta", "separate-tenant",
            IncidentCreate(target_id="private-target"),
        )

    async with sessions_fixture() as session:
        async with session.begin():
            for index, incident_id in enumerate(own_ids):
                age = now - timedelta(minutes=(index + 1) * 10)
                stored = await session.get(Incident, incident_id)
                stored.phase_entered_at = age
                intent = await session.scalar(
                    __import__("sqlalchemy").select(OutboxEntry)
                    .where(OutboxEntry.incident_id == incident_id)
                )
                intent.created_at = age
                intent.attempt = index + 1
            another = await session.get(Incident, foreign.id)
            another.phase_entered_at = now - timedelta(days=2)
            beta_intent = await session.scalar(
                __import__("sqlalchemy").select(OutboxEntry)
                .where(OutboxEntry.incident_id == foreign.id)
            )
            beta_intent.created_at = now - timedelta(days=2)
            beta_intent.attempt = 99

    async with sessions_fixture() as session:
        health = await queue_health(session, "alpha", 900, now)
        assert health == {
            "pending_outbox": 5,
            "active_incidents": 5,
            "oldest_pending_seconds": 3000,
            "oldest_active_phase_seconds": 3000,
            "pending_age_p95_seconds": 3000,
            "aged_pending_outbox": 4,
            "aged_active_incidents": 4,
            "max_pending_attempts": 5,
            "stuck_after_seconds": 900,
        }
    async with sessions_fixture() as session:
        health = await queue_health(session, "beta", 900, now)
        assert health["pending_outbox"] == 1
        assert health["max_pending_attempts"] == 99

    async with sessions_fixture() as session:
        for incident_id in own_ids:
            intent = await session.scalar(
                __import__("sqlalchemy").select(OutboxEntry)
                .where(OutboxEntry.incident_id == incident_id)
            )
            async with session.begin_nested():
                intent.delivered_at = now

    async with sessions_fixture() as session:
        health = await queue_health(session, "alpha", 900, now)
        assert health["pending_outbox"] == 0
        assert health["oldest_pending_seconds"] == 0
        assert health["pending_age_p95_seconds"] == 0
        assert health["aged_pending_outbox"] == 0
        assert health["max_pending_attempts"] == 0

    async with sessions_fixture() as session:
        with pytest.raises(ValueError, match="positive"):
            await queue_health(session, "alpha", 0, now)
        with pytest.raises(ValueError, match="timezone"):
            await queue_health(session, "alpha", 900, datetime.utcnow())


@pytest.mark.asyncio
async def test_redteam_queue_api_never_exposes_other_tenant_data(sessions_fixture):
    async with sessions_fixture() as session:
        await admit_incident(
            session, "alpha", "own-queue", IncidentCreate(target_id="checkout"),
        )
    async with sessions_fixture() as session:
        await admit_incident(
            session, "beta", "foreign-queue", IncidentCreate(target_id="private"),
        )

    async def override_session():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: Settings(
        local_token="queue-metrics-test",
    )
    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            own = {"X-Tenant-Id": "alpha", "X-OpsBlaze-Token": "queue-metrics-test"}
            response = await client.get("/v1/operations/queues", headers=own)
            assert response.status_code == 200, response.text
            report = response.json()
            assert report["pending_outbox"] == 1
            assert report["active_incidents"] == 1
            assert all(type(value) is int for value in report.values())
            assert "private" not in str(report)
            denied = await client.get(
                "/v1/operations/queues",
                headers={"X-Tenant-Id": "alpha", "X-OpsBlaze-Token": "wrong"},
            )
            assert denied.status_code == 401
    finally:
        app.dependency_overrides.clear()
