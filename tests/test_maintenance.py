"""Scheduled retention operates in bounded, idempotent transactions."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from backend.models import AdmissionUsage, Evidence, Incident, ModelTokenWindow, UIEvent, UIEventWatermark
from backend.probes.gateway import EXPIRED_EXCERPT
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
        "evidence_scrubbed": 0,
        "ui_events_deleted": 1,
        "admission_windows_deleted": 1,
        "ai_windows_deleted": 1,
        "worker_hosts_pruned": 0,
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
async def test_scheduled_maintenance_scrubs_only_expired_evidence(sessions_fixture):
    await assert_scheduled_evidence_scrub(sessions_fixture)


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_native_maintenance_scrubs_only_expired_evidence(native_sessions_fixture):
    await assert_scheduled_evidence_scrub(native_sessions_fixture)


async def assert_scheduled_evidence_scrub(sessions):
    now = datetime.now(timezone.utc)
    tenant = f"evidence-maintenance-{uuid4().hex}"
    expired_at = now - timedelta(days=1)
    expired_id, fresh_id = uuid4(), uuid4()
    digest = "b" * 64
    async with sessions() as session:
        incident = await admit_incident(session, tenant, "retention", IncidentCreate(target_id="checkout"))
        async with session.begin():
            for record_id, expires_at in ((expired_id, expired_at), (fresh_id, now + timedelta(days=1))):
                session.add(Evidence(
                    id=record_id, tenant_id=tenant, incident_id=incident.id, target_id="checkout",
                    source="simulator.health", source_version="simulator-v1", observed_at=now,
                    expires_at=expires_at, sha256=digest, redacted_excerpt='{"healthy":false}',
                    classification="INTERNAL", validity="VALID",
                ))

    first = await run_maintenance(sessions, Settings(), now=now, max_batches=1)
    assert first["evidence_scrubbed"] >= 1
    again = await run_maintenance(sessions, Settings(), now=now, max_batches=1)
    assert again["evidence_scrubbed"] == 0
    async with sessions() as session:
        expired = await session.get(Evidence, expired_id)
        fresh = await session.get(Evidence, fresh_id)
        assert expired.validity == "EXPIRED" and expired.redacted_excerpt == EXPIRED_EXCERPT
        assert expired.sha256 == digest and expired.expires_at.replace(tzinfo=timezone.utc) == expired_at
        assert fresh.validity == "VALID" and fresh.redacted_excerpt == '{"healthy":false}'


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


@pytest.mark.asyncio
async def test_maintenance_prunes_only_retired_host_presence(sessions_fixture):
    from backend.workers.heartbeat import (
        present_worker_host_counts, record_heartbeat,
    )

    now = datetime.now(timezone.utc)
    await record_heartbeat(
        sessions_fixture, "dispatcher", now - timedelta(days=8),
        host_id="retired",
    )
    await record_heartbeat(
        sessions_fixture, "dispatcher", now, host_id="current",
    )
    totals = await run_maintenance(sessions_fixture, Settings(), now=now)
    assert totals["worker_hosts_pruned"] == 1
    async with sessions_fixture() as session:
        assert await present_worker_host_counts(session, now, 30) == {
            "dispatcher": 1, "incident_worker": 0,
        }
    again = await run_maintenance(sessions_fixture, Settings(), now=now)
    assert again["worker_hosts_pruned"] == 0
