"""Worker heartbeats are durable and prevent false production readiness."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api import app
from backend.database import get_session
from backend.settings import Settings, get_settings
from backend.workers.heartbeat import present_workers, record_heartbeat


@pytest.mark.asyncio
async def test_readiness_times_out_a_stalled_database_and_recovers(sessions_fixture, monkeypatch):
    stall = False
    query_cancelled = False

    async def session_override():
        async with sessions_fixture() as session:
            execute = session.execute

            async def run_query(*args, **kwargs):
                nonlocal query_cancelled
                result = await execute(*args, **kwargs)
                try:
                    await asyncio.Event().wait()
                finally:
                    query_cancelled = True
                return result

            if stall:
                monkeypatch.setattr(session, "execute", run_query)
            yield session

    monkeypatch.setitem(app.dependency_overrides, get_session, session_override)
    monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: Settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.test") as client:
        initial = await client.get("/v1/health/ready")
        assert initial.status_code == 200
        monkeypatch.setattr(
            "backend.routers.operations.READINESS_DATABASE_TIMEOUT_SECONDS", 0.15,
        )
        stall = True
        response = await asyncio.wait_for(client.get("/v1/health/ready"), timeout=1)
        assert response.status_code == 503
        assert response.json() == {"detail": "database unavailable"}
        assert query_cancelled

        stall = False
        response = await client.get("/v1/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_readiness_releases_postgres_connection_after_cancelled_probe(monkeypatch):
    settings = get_settings()
    assert settings.database_url.startswith("postgresql+asyncpg:")
    engine = create_async_engine(settings.database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine)
    stall = True

    async def session_override():
        async with sessions() as session:
            original_execute = session.execute

            async def execute_stalled_probe(*_args, **_kwargs):
                return await original_execute(text("SELECT pg_sleep(8)"))

            if stall:
                monkeypatch.setattr(session, "execute", execute_stalled_probe)
            yield session

    monkeypatch.setattr("backend.routers.operations.READINESS_DATABASE_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setitem(app.dependency_overrides, get_session, session_override)
    monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: Settings())
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.test") as client:
            response = await asyncio.wait_for(client.get("/v1/health/ready"), timeout=3)
            assert response.status_code == 503
            assert response.json() == {"detail": "database unavailable"}
            assert engine.pool.checkedout() == 0

            stall = False
            response = await asyncio.wait_for(client.get("/v1/health/ready"), timeout=3)
            assert response.status_code == 200
            assert response.json() == {"status": "ready"}
    finally:
        await engine.dispose()


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
        oidc_issuer="https://idp.example.test/",
        oidc_audience="opsblaze-api",
        oidc_jwks_url="https://idp.example.test/jwks",
        projector_database_url=(
            "postgresql+asyncpg://opsblaze_projector@localhost:5432/opsblaze"
        ),
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
            assert both.status_code == 503  # Dedicated projector not present.
            await record_heartbeat(sessions_fixture, "experience_projector")
            only_roles = await client.get("/v1/health/ready")
            assert only_roles.status_code == 503
            assert only_roles.json()["detail"] == (
                "configured multi-host worker presence not observed"
            )
            for host in ("native-a", "native-b"):
                await record_heartbeat(
                    sessions_fixture, "dispatcher", host_id=host,
                )
                await record_heartbeat(
                    sessions_fixture, "incident_worker", host_id=host,
                )
            all_workers = await client.get("/v1/health/ready")
            assert all_workers.status_code == 200
            assert all_workers.json() == {"status": "ready"}
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
        temporal_tls_enabled=True,
        oidc_issuer="https://idp.example.test/",
        oidc_audience="opsblaze-api",
        oidc_jwks_url="https://idp.example.test/jwks",
        projector_database_url=(
            "postgresql+asyncpg://opsblaze_projector:restricted@localhost:5432/opsblaze"
        ),
    )
    try:
        for host in ("native-a", "native-b"):
            await record_heartbeat(
                sessions_fixture, "dispatcher", host_id=host,
            )
            await record_heartbeat(
                sessions_fixture, "incident_worker", host_id=host,
            )
        await record_heartbeat(sessions_fixture, "experience_projector")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://api.test"
        ) as client:
            response = await client.get("/v1/health/ready")
            assert response.status_code == 503
            assert response.json()["detail"] == "Temporal unreachable"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_production_readiness_rejects_unconfigured_identity(sessions_fixture):
    async def session_override():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="production", temporal_tls_enabled=True,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://api.test",
        ) as client:
            response = await client.get("/v1/health/ready")
            assert response.status_code == 503
            assert response.json()["detail"] == (
                "production identity provider is not configured"
            )
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_redteam_readiness_rejects_general_app_database_for_projector(
    sessions_fixture, monkeypatch,
):
    async def session_override():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="production", temporal_tls_enabled=True,
        oidc_issuer="https://idp.example.test/",
        oidc_audience="opsblaze-api",
        oidc_jwks_url="https://idp.example.test/jwks",
        projector_database_url=(
            "postgresql+asyncpg://opsblaze:opsblaze@localhost:5432/opsblaze"
        ),
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://api.test",
        ) as client:
            response = await client.get("/v1/health/ready")
            assert response.status_code == 503
            assert response.json()["detail"] == (
                "dedicated experience projector credentials not configured"
            )
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delayed_heartbeat_cannot_overwrite_newer_presence(sessions_fixture):
    from backend.models import WorkerHeartbeat

    now = datetime.now(timezone.utc)
    await record_heartbeat(sessions_fixture, "dispatcher", now)
    await record_heartbeat(
        sessions_fixture, "dispatcher", now - timedelta(hours=1),
    )
    async with sessions_fixture() as session:
        row = await session.get(WorkerHeartbeat, "dispatcher")
        assert row.last_seen.replace(tzinfo=timezone.utc) >= now
        assert "dispatcher" in await present_workers(session, now, 30)
