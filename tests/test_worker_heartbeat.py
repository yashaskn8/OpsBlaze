"""Worker heartbeats are durable and prevent false production readiness."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from backend.api import app
from backend.database import get_session
from backend.settings import Settings, get_settings
from backend.workers.heartbeat import present_workers, record_heartbeat


@pytest.mark.asyncio
async def test_heartbeat_presence_is_tenant_independent_and_age_bounded(sessions_fixture):
    now = datetime.now(timezone.utc)
    await record_heartbeat(
        sessions_fixture, "dispatcher", now - timedelta(seconds=10),
    )
    await record_heartbeat(sessions_fixture, "incident_worker", now)
    async with sessions_fixture() as session:
        assert await present_workers(session, now, 30) == frozenset({
            "dispatcher", "incident_worker",
        })
        assert await present_workers(session, now, 5) == frozenset({
            "incident_worker",
        })
    await record_heartbeat(sessions_fixture, "dispatcher", now)
    async with sessions_fixture() as session:
        assert await present_workers(session, now, 5) == frozenset({
            "dispatcher", "incident_worker",
        })
    with pytest.raises(ValueError):
        await record_heartbeat(sessions_fixture, "unknown_role")


@pytest.mark.asyncio
async def test_production_readiness_checks_both_worker_roles(sessions_fixture, monkeypatch):
    async def healthy_temporal(_settings):
        return True

    monkeypatch.setattr(
        "backend.workers.temporal_health.check_temporal_health",
        healthy_temporal,
    )
    async def session_override():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="production", worker_stale_seconds=30,
        temporal_tls_enabled=True,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://api.test",
        ) as client:
            missing = await client.get("/v1/health/ready")
            assert missing.status_code == 503
            await record_heartbeat(sessions_fixture, "dispatcher")
            one = await client.get("/v1/health/ready")
            assert one.status_code == 503
            await record_heartbeat(sessions_fixture, "incident_worker")
            both = await client.get("/v1/health/ready")
            assert both.status_code == 200 and both.json() == {"status": "ready"}
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_readiness_fails_closed_if_temporal_unreachable(sessions_fixture, monkeypatch):
    async def session_override():
        async with sessions_fixture() as session:
            yield session

    async def unavailable_temporal(_settings):
        raise ConnectionError("synthetic outage")

    monkeypatch.setattr(
        "backend.workers.temporal_health.check_temporal_health",
        unavailable_temporal,
    )
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="production", worker_stale_seconds=30,
    )
    try:
        await record_heartbeat(sessions_fixture, "dispatcher")
        await record_heartbeat(sessions_fixture, "incident_worker")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://api.test"
        ) as client:
            response = await client.get("/v1/health/ready")
            assert response.status_code == 503
            assert response.json()["detail"] == "Temporal unreachable"
    finally:
        app.dependency_overrides.clear()
