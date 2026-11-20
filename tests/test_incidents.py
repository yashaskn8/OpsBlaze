from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api import app
from backend.database import get_session
from backend.models import Base, OutboxEntry
from backend.schemas import IncidentCreate
from backend.services import IdempotencyConflict, admit_incident
from backend.settings import Settings, get_settings


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_admission_has_one_outbox_intent(sessions) -> None:
    async with sessions() as session:
        payload = IncidentCreate(target_id="checkout")
        first = await admit_incident(session, "tenant-a", "retry-1", payload)
    async with sessions() as session:
        second = await admit_incident(session, "tenant-a", "retry-1", payload)
        assert first.id == second.id
        assert await session.scalar(select(func.count()).select_from(OutboxEntry)) == 1


@pytest.mark.asyncio
async def test_changed_payload_is_conflict(sessions) -> None:
    async with sessions() as session:
        await admit_incident(
            session, "tenant-a", "retry-1", IncidentCreate(target_id="checkout")
        )
    async with sessions() as session:
        with pytest.raises(IdempotencyConflict):
            await admit_incident(
                session, "tenant-a", "retry-1", IncidentCreate(target_id="billing")
            )


@pytest.mark.asyncio
async def test_idempotency_key_is_tenant_scoped(sessions) -> None:
    async with sessions() as session:
        first = await admit_incident(
            session, "tenant-a", "same-key", IncidentCreate(target_id="checkout")
        )
    async with sessions() as session:
        second = await admit_incident(
            session, "tenant-b", "same-key", IncidentCreate(target_id="checkout")
        )
        assert first.id != second.id


@pytest.mark.asyncio
async def test_api_rejects_cross_tenant_read_and_bad_token(sessions) -> None:
    async def session_override():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="sqlite+aiosqlite:///:memory:", local_token="test-secret"
    )
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {
                "X-Tenant-Id": "tenant-a",
                "X-OpsBlaze-Token": "test-secret",
                "Idempotency-Key": "request-1",
            }
            created = await client.post(
                "/v1/incidents", json={"target_id": "checkout"}, headers=headers
            )
            assert created.status_code == 202, created.text
            incident_id = created.json()["id"]
            other_headers = {**headers, "X-Tenant-Id": "tenant-b"}
            hidden = await client.get(
                f"/v1/incidents/{incident_id}", headers=other_headers
            )
            assert hidden.status_code == 404
            denied = await client.get(
                f"/v1/incidents/{incident_id}",
                headers={**headers, "X-OpsBlaze-Token": "wrong"},
            )
            assert denied.status_code == 401
    finally:
        app.dependency_overrides.clear()
