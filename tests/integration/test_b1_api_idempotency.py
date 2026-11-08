"""Phase 3.1 — Idempotency attacks, canonical payload hashing, and API integration tests for B1."""

from __future__ import annotations

import asyncio
import unicodedata
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.main import create_app
from backend.config import Settings
from backend.persistence.models import Incident, Outbox

pytestmark = pytest.mark.postgres


async def _count_incidents_and_outbox(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    client_key: str,
) -> tuple[int, int]:
    async with session_factory() as session:
        inc_rows = (
            (
                await session.execute(
                    select(Incident.id).where(
                        Incident.tenant_id == tenant_id,
                        Incident.client_key == client_key,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not inc_rows:
            return 0, 0
        outbox_cnt = (
            await session.execute(
                select(func.count(Outbox.id)).where(Outbox.incident_id.in_(inc_rows))
            )
        ).scalar_one()
        return len(inc_rows), int(outbox_cnt)


async def test_1_same_key_same_payload_sequentially(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    key = f"seq-same-{uuid.uuid4()}"
    headers = {"X-Tenant-ID": str(isolated_tenant), "Idempotency-Key": key}
    payload = {"target_id": "sim-db-primary", "title": "Lag spike", "severity": "SEV2"}

    r1 = await api_client.post("/v1/incidents", json=payload, headers=headers)
    r2 = await api_client.post("/v1/incidents", json=payload, headers=headers)

    assert r1.status_code == 202
    assert r2.status_code == 202
    d1, d2 = r1.json(), r2.json()
    assert d1["id"] == d2["id"]
    assert d1["request_sha256"] == d2["request_sha256"]
    assert d1["workflow_id"] == f"incident:{isolated_tenant}:{d1['id']}"

    inc_cnt, outbox_cnt = await _count_incidents_and_outbox(session_factory, isolated_tenant, key)
    assert inc_cnt == 1
    assert outbox_cnt == 1


async def test_2_same_key_same_payload_concurrently_barrier(
    test_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Barrier-synchronized race: 12 concurrent requests with identical tenant, key, and payload."""
    key = f"conc-same-{uuid.uuid4()}"
    headers = {"X-Tenant-ID": str(isolated_tenant), "Idempotency-Key": key}
    payload = {"target_id": "sim-db-primary", "title": "Concurrent race", "severity": "SEV1"}

    app = create_app(test_settings)
    barrier = asyncio.Event()

    async def worker(client: AsyncClient):  # type: ignore[no-untyped-def]
        await barrier.wait()
        return await client.post("/v1/incidents", json=payload, headers=headers)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            tasks = [asyncio.create_task(worker(client)) for _ in range(12)]
            await asyncio.sleep(0.02)
            barrier.set()
            responses = await asyncio.gather(*tasks)
    finally:
        if getattr(app.state, "engine", None) is not None:
            await app.state.engine.dispose()

    assert all(r.status_code == 202 for r in responses)
    ids = {r.json()["id"] for r in responses}
    assert len(ids) == 1

    inc_cnt, outbox_cnt = await _count_incidents_and_outbox(session_factory, isolated_tenant, key)
    assert inc_cnt == 1
    assert outbox_cnt == 1


async def test_3_same_key_different_payload_sequentially_returns_409(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    key = f"seq-diff-{uuid.uuid4()}"
    headers = {"X-Tenant-ID": str(isolated_tenant), "Idempotency-Key": key}

    r1 = await api_client.post(
        "/v1/incidents",
        json={"target_id": "sim-target-a", "title": "Original", "severity": "SEV2"},
        headers=headers,
    )
    r2 = await api_client.post(
        "/v1/incidents",
        json={"target_id": "sim-target-b", "title": "Modified target", "severity": "SEV2"},
        headers=headers,
    )
    r3 = await api_client.post(
        "/v1/incidents",
        json={"target_id": "sim-target-a", "title": "Modified title", "severity": "SEV2"},
        headers=headers,
    )

    assert r1.status_code == 202
    assert r2.status_code == 409
    assert r3.status_code == 409

    inc_cnt, outbox_cnt = await _count_incidents_and_outbox(session_factory, isolated_tenant, key)
    assert inc_cnt == 1
    assert outbox_cnt == 1


async def test_4_same_key_different_payload_concurrently(
    test_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Barrier-synchronized race: 10 concurrent requests with the same key but 2 distinct payloads."""
    key = f"conc-diff-{uuid.uuid4()}"
    headers = {"X-Tenant-ID": str(isolated_tenant), "Idempotency-Key": key}
    payload_a = {"target_id": "sim-target-a", "severity": "SEV1"}
    payload_b = {"target_id": "sim-target-b", "severity": "SEV3"}

    app = create_app(test_settings)
    barrier = asyncio.Event()

    async def worker(client: AsyncClient, idx: int):  # type: ignore[no-untyped-def]
        await barrier.wait()
        body = payload_a if idx % 2 == 0 else payload_b
        return await client.post("/v1/incidents", json=body, headers=headers)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            tasks = [asyncio.create_task(worker(client, i)) for i in range(10)]
            await asyncio.sleep(0.02)
            barrier.set()
            responses = await asyncio.gather(*tasks)
    finally:
        if getattr(app.state, "engine", None) is not None:
            await app.state.engine.dispose()

    accepted = [r for r in responses if r.status_code == 202]
    conflicts = [r for r in responses if r.status_code == 409]
    assert len(accepted) == 5
    assert len(conflicts) == 5
    assert len({r.json()["id"] for r in accepted}) == 1

    inc_cnt, outbox_cnt = await _count_incidents_and_outbox(session_factory, isolated_tenant, key)
    assert inc_cnt == 1
    assert outbox_cnt == 1


async def test_5_multiple_tenants_using_identical_keys(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    second_isolated_tenant: uuid.UUID,
) -> None:
    shared_key = f"shared-tenant-key-{uuid.uuid4()}"
    r1 = await api_client.post(
        "/v1/incidents",
        json={"target_id": "tenant-1-target", "severity": "SEV1"},
        headers={"X-Tenant-ID": str(isolated_tenant), "Idempotency-Key": shared_key},
    )
    r2 = await api_client.post(
        "/v1/incidents",
        json={"target_id": "tenant-2-target", "severity": "SEV3"},
        headers={"X-Tenant-ID": str(second_isolated_tenant), "Idempotency-Key": shared_key},
    )
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r1.json()["id"] != r2.json()["id"]
    assert r1.json()["tenant_id"] == str(isolated_tenant)
    assert r2.json()["tenant_id"] == str(second_isolated_tenant)


async def test_6_7_8_unicode_json_key_order_and_null_vs_missing_normalization(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    from backend.domain.incident import canonical_request_hash
    from backend.persistence.repositories import IncidentRepository

    shared_key = f"unicode-norm-key-{uuid.uuid4()}"
    title_nfc = unicodedata.normalize("NFC", "Réseau dégradé")
    title_nfd = unicodedata.normalize("NFD", "Réseau dégradé")

    # Request 1: NFC strings, key order A, missing optional fields
    body_1 = (
        '{"target_id":"sim-node-1","title":"'
        + title_nfc
        + '","severity":"SEV2","labels":{"b":"2","a":"1"}}'
    )
    # Request 2: NFD strings, reversed JSON key order, explicit null optional fields
    body_2 = (
        '{"labels":{"a":"1","b":"2"},"description":null,"severity":"SEV2","title":"'
        + title_nfd
        + '","target_id":"sim-node-1"}'
    )

    r1 = await api_client.post(
        "/v1/incidents",
        content=body_1.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Tenant-ID": str(isolated_tenant),
            "Idempotency-Key": shared_key,
        },
    )
    r2 = await api_client.post(
        "/v1/incidents",
        content=body_2.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Tenant-ID": str(isolated_tenant),
            "Idempotency-Key": shared_key,
        },
    )
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r1.json()["id"] == r2.json()["id"]
    assert r1.json()["request_sha256"] == r2.json()["request_sha256"]

    # Also verify NFC vs NFD client_key normalization directly at repository boundary
    repo = IncidentRepository(session_factory)
    raw_key_nfc = unicodedata.normalize("NFC", f"repo-key-é-{uuid.uuid4()}")
    raw_key_nfd = unicodedata.normalize("NFD", raw_key_nfc)
    req_sha = canonical_request_hash({"target_id": "sim-node-1", "severity": "SEV2"})
    out1 = await repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=raw_key_nfc,
        target_id="sim-node-1",
        request_sha256=req_sha,
    )
    out2 = await repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=raw_key_nfd,
        target_id="sim-node-1",
        request_sha256=req_sha,
    )
    assert out1.created is True
    assert out2.created is False
    assert out1.response.id == out2.response.id


async def test_9_empty_and_malformed_idempotency_keys_rejected_with_422(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
) -> None:
    payload = {"target_id": "sim-db-1"}
    # Missing Idempotency-Key header
    r_missing = await api_client.post(
        "/v1/incidents",
        json=payload,
        headers={"X-Tenant-ID": str(isolated_tenant)},
    )
    assert r_missing.status_code == 422

    # Whitespace-only or too-long keys
    for invalid_key in ["", "   ", "x" * 130]:
        r_bad = await api_client.post(
            "/v1/incidents",
            json=payload,
            headers={
                "X-Tenant-ID": str(isolated_tenant),
                "Idempotency-Key": invalid_key,
            },
        )
        assert r_bad.status_code == 422


async def test_10_duplicate_requests_following_api_process_restart(
    test_settings: Settings,
    isolated_tenant: uuid.UUID,
) -> None:
    key = f"restart-key-{uuid.uuid4()}"
    headers = {"X-Tenant-ID": str(isolated_tenant), "Idempotency-Key": key}
    payload = {"target_id": "sim-restart-node", "title": "Before restart", "severity": "SEV2"}

    # Process instance 1
    app1 = create_app(test_settings)
    async with AsyncClient(transport=ASGITransport(app=app1), base_url="http://testserver") as c1:
        r1 = await c1.post("/v1/incidents", json=payload, headers=headers)
        assert r1.status_code == 202
        inc_id = r1.json()["id"]
    await app1.state.engine.dispose()

    # Process instance 2 (fresh FastAPI app and engine simulating restart)
    app2 = create_app(test_settings)
    async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://testserver") as c2:
        r2 = await c2.post("/v1/incidents", json=payload, headers=headers)
        assert r2.status_code == 202
        assert r2.json()["id"] == inc_id

        # Fetch via GET after restart
        r_get = await c2.get(
            f"/v1/incidents/{inc_id}",
            headers={"X-Tenant-ID": str(isolated_tenant)},
        )
        assert r_get.status_code == 200
        assert r_get.json()["id"] == inc_id
    await app2.state.engine.dispose()


async def test_11_api_cancellation_stale_expected_version_and_concurrent_race(
    test_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Verify API cancellation optimistic concurrency (409 on stale expected_version) and race safety."""
    from backend.domain.incident import (
        DomainError,
        IncidentStatus,
    )
    from backend.persistence.repositories import IncidentRepository

    repo = IncidentRepository(session_factory)
    key = f"cancel-race-key-{uuid.uuid4()}"
    headers = {"X-Tenant-ID": str(isolated_tenant), "Idempotency-Key": key}
    payload = {"target_id": "sim-cancel-race", "title": "Cancel race test", "severity": "SEV1"}

    app = create_app(test_settings)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            r_create = await client.post("/v1/incidents", json=payload, headers=headers)
            assert r_create.status_code == 202
            inc_id = uuid.UUID(r_create.json()["id"])
            assert r_create.json()["status_version"] == 1

            # Advance incident to DISPATCH_PENDING (status_version becomes 2)
            t1 = await repo.transition_status(
                tenant_id=isolated_tenant,
                incident_id=inc_id,
                target_status=IncidentStatus.DISPATCH_PENDING,
                expected_version=1,
            )
            assert t1.status_version == 2

            # 1. Cancel with stale expected_version=1 must return HTTP 409 Conflict
            r_stale = await client.post(
                f"/v1/incidents/{inc_id}/cancel",
                json={"expected_version": 1, "reason": "stale_cancel"},
                headers={"X-Tenant-ID": str(isolated_tenant)},
            )
            assert r_stale.status_code == 409

            # 2. Barrier-synchronized race: 6 concurrent cancel requests + 1 concurrent status advance
            barrier = asyncio.Event()

            async def cancel_worker(idx: int):  # type: ignore[no-untyped-def]
                await barrier.wait()
                return await client.post(
                    f"/v1/incidents/{inc_id}/cancel",
                    json={"reason": f"concurrent_cancel_{idx}"},
                    headers={"X-Tenant-ID": str(isolated_tenant)},
                )

            async def transition_worker() -> object:
                await barrier.wait()
                try:
                    return await repo.transition_status(
                        tenant_id=isolated_tenant,
                        incident_id=inc_id,
                        target_status=IncidentStatus.INVESTIGATING,
                        expected_version=2,
                    )
                except DomainError as exc:
                    return exc

            tasks = [asyncio.create_task(cancel_worker(i)) for i in range(6)]
            tx_task = asyncio.create_task(transition_worker())
            await asyncio.sleep(0.02)
            barrier.set()
            cancel_responses = await asyncio.gather(*tasks)
            _ = await tx_task

            # Every cancel call must succeed idempotently (200) or conflict cleanly (409), never 500
            assert all(r.status_code in (200, 409) for r in cancel_responses)
            assert any(r.status_code == 200 for r in cancel_responses)

            # Final authoritative PostgreSQL state must be CANCELLED and immutable
            r_final = await client.get(
                f"/v1/incidents/{inc_id}",
                headers={"X-Tenant-ID": str(isolated_tenant)},
            )
            assert r_final.status_code == 200
            assert r_final.json()["status"] == "CANCELLED"
    finally:
        if getattr(app.state, "engine", None) is not None:
            await app.state.engine.dispose()
