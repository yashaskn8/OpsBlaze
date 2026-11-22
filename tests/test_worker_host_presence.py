"""G3 multi-host presence tracking is a self-report, not HA certification."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from backend.api import app
from backend.database import get_session
from backend.settings import Settings, get_settings
from backend.workers.heartbeat import (
    present_worker_hosts, present_workers, record_heartbeat,
)


@pytest.mark.asyncio
async def test_worker_host_presence_tracks_two_hosts_without_collapsing_roles(
    sessions_fixture,
):
    now = datetime.now(timezone.utc)
    for host in ("site-a", "site-b"):
        await record_heartbeat(sessions_fixture, "dispatcher", now, host_id=host)
        await record_heartbeat(sessions_fixture, "incident_worker", now, host_id=host)
    async with sessions_fixture() as session:
        assert await present_worker_hosts(session, now, 30) == {
            "dispatcher": frozenset({"site-a", "site-b"}),
            "incident_worker": frozenset({"site-a", "site-b"}),
        }
        assert await present_workers(session, now, 30) == frozenset({
            "dispatcher", "incident_worker",
        })
    # Older delayed heartbeat from the same host cannot make it stale.
    await record_heartbeat(
        sessions_fixture, "dispatcher", now - timedelta(hours=2),
        host_id="site-a",
    )
    async with sessions_fixture() as session:
        assert (await present_worker_hosts(session, now, 30))["dispatcher"] == {
            "site-a", "site-b",
        }


@pytest.mark.asyncio
async def test_worker_host_presence_ignores_expired_and_future_rows(sessions_fixture):
    now = datetime.now(timezone.utc)
    await record_heartbeat(
        sessions_fixture, "dispatcher", now - timedelta(minutes=10),
        host_id="stale",
    )
    await record_heartbeat(
        sessions_fixture, "dispatcher", now + timedelta(minutes=1),
        host_id="future",
    )
    await record_heartbeat(
        sessions_fixture, "dispatcher", now,
        host_id="valid",
    )
    async with sessions_fixture() as session:
        assert (await present_worker_hosts(session, now, 30))["dispatcher"] == {
            "valid",
        }


@pytest.mark.asyncio
async def test_invalid_host_ids_rejected_before_database_write(sessions_fixture):
    for invalid in ("../../../etc", "line\nfeed", "a" * 65, "leading space"):
        with pytest.raises(ValueError, match="host identity"):
            await record_heartbeat(
                sessions_fixture, "dispatcher", host_id=invalid,
            )
    with pytest.raises(ValueError, match="host identity"):
        await record_heartbeat(
            sessions_fixture, "experience_projector", host_id="site-a",
        )
    async with sessions_fixture() as session:
        assert await present_worker_hosts(session) == {
            "dispatcher": frozenset(), "incident_worker": frozenset(),
        }


@pytest.mark.asyncio
async def test_production_topology_readiness_fails_closed_until_each_role_reports_two_hosts(
    sessions_fixture, monkeypatch,
):
    async def healthy_temporal(_settings):
        return True

    async def session_override():
        async with sessions_fixture() as session:
            yield session

    monkeypatch.setattr(
        "backend.workers.temporal_health.check_temporal_health",
        healthy_temporal,
    )
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="production",
        temporal_tls_enabled=True,
        oidc_issuer="https://idp.example.test/",
        oidc_audience="opsblaze-api",
        oidc_jwks_url="https://idp.example.test/jwks",
        projector_database_url=(
            "postgresql+asyncpg://opsblaze_projector@localhost:5432/opsblaze"
        ),
        min_distinct_worker_hosts=2,
    )
    try:
        for role in ("dispatcher", "incident_worker"):
            await record_heartbeat(
                sessions_fixture, role, host_id="site-a",
            )
        await record_heartbeat(sessions_fixture, "experience_projector")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://api.test",
        ) as client:
            early = await client.get("/v1/health/ready")
            assert early.status_code == 503
            assert early.json()["detail"] == (
                "configured multi-host worker presence not observed"
            )
            await record_heartbeat(
                sessions_fixture, "dispatcher", host_id="site-b",
            )
            still_missing = await client.get("/v1/health/ready")
            assert still_missing.status_code == 503
            await record_heartbeat(
                sessions_fixture, "incident_worker", host_id="site-b",
            )
            reported = await client.get("/v1/health/ready")
            assert reported.status_code == 200
            assert reported.json() == {"status": "ready"}
    finally:
        app.dependency_overrides.clear()


def test_required_worker_hosts_parameter_is_bounded():
    from pydantic import ValidationError

    for invalid in (0, 1, 17):
        with pytest.raises(ValidationError):
            Settings(min_distinct_worker_hosts=invalid)
