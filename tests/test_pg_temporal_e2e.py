from datetime import datetime, timedelta, timezone

"""Native PostgreSQL + Temporal vertical slice; no Docker, no production effects."""

import asyncio
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.api import app
from backend.adapters.conformance import execute_suite
from backend.database import get_session_maker
from backend.settings import get_settings
from backend.workers.dispatcher import dispatch_once
from backend.workers.seed_simulator import seed
from backend.workflows.activities import (
    apply_approved_simulator_plan,
    inspect_incident_read_only,
    project_incident_status,
    select_incident_route,
)
from backend.workflows.incident import IncidentWorkflow


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_postgres_temporal_simulator_recovery() -> None:
    settings = get_settings()
    assert settings.database_url.startswith("postgresql+asyncpg:")
    await seed("demo", "checkout")
    sessions = get_session_maker()
    assert (await execute_suite(sessions)).state == "LAB_PASS"

    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue=settings.temporal_task_queue,
            workflows=[IncidentWorkflow],
            activities=[
                inspect_incident_read_only,
                project_incident_status,
                apply_approved_simulator_plan,
                select_incident_route,
            ],
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {
                    "X-Tenant-Id": "demo",
                    "X-OpsBlaze-Token": settings.local_token,
                }
                created = await client.post(
                    "/v1/incidents",
                    headers={**headers, "Idempotency-Key": str(uuid4())},
                    json={"target_id": "checkout"},
                )
                assert created.status_code == 202, created.text
                incident_id = created.json()["id"]
                assert await dispatch_once(env.client, sessions)

                async def await_state(expected: str) -> dict:
                    for _ in range(100):
                        response = await client.get(
                            f"/v1/incidents/{incident_id}", headers=headers
                        )
                        assert response.status_code == 200, response.text
                        body = response.json()
                        if body["status_projection"] == expected:
                            return body
                        await asyncio.sleep(0.1)
                    raise AssertionError(f"incident never reached {expected}")

                await await_state("AWAITING_APPROVAL")
                evidence = await client.get(
                    f"/v1/incidents/{incident_id}/evidence", headers=headers
                )
                assert evidence.status_code == 200
                assert len(evidence.json()) == 2

                plans = await client.get(
                    f"/v1/incidents/{incident_id}/plans", headers=headers
                )
                assert plans.status_code == 200 and len(plans.json()) == 1
                plan = plans.json()[0]
                assert plan["status"] == "PROPOSED"
                decision = await client.post(
                    f"/v1/plans/{plan['id']}/decisions",
                    headers={**headers, "If-Match": str(plan["version"])},
                    json={"decision": "APPROVE"},
                )
                assert decision.status_code == 201, decision.text
                assert await dispatch_once(env.client, sessions)

                final = await await_state("VERIFIED")
                assert final["status_projection"] == "VERIFIED"
                effects = await client.get(
                    f"/v1/incidents/{incident_id}/effects", headers=headers
                )
                assert effects.status_code == 200 and len(effects.json()) == 1
                assert effects.json()[0]["state"] == "VERIFIED"
                target = await client.get(
                    "/v1/targets/checkout/simulator", headers=headers
                )
                assert target.status_code == 200
                assert target.json()["healthy"] is True
                assert target.json()["version"] == 2


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_parallel_postgres_admission_is_one_logical_incident() -> None:
    """One incident, intent, event and quota slot for concurrent retries."""
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.models import AdmissionUsage, Incident, OutboxEntry, PolicyAssignment, UIEvent
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    settings = get_settings()
    assert settings.database_url.startswith("postgresql+asyncpg:")
    tenant = "concurrent-" + uuid4().hex[:12]
    # A dedicated engine is owned by this test and disposed on its event loop.
    # The other integration test's cached app engine belongs to a prior loop.
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        payload = IncidentCreate(target_id="checkout")

        async def admit():
            async with sessions() as session:
                return await admit_incident(session, tenant, "stable-key", payload)

        admitted = await asyncio.gather(*(admit() for _ in range(8)))
        ids = {item.id for item in admitted}
        assert len(ids) == 1
        incident_id = next(iter(ids))

        async with sessions() as session:
            count = lambda model: select(func.count()).select_from(model)
            assert await session.scalar(
                count(Incident).where(Incident.tenant_id == tenant)
            ) == 1
            assert await session.scalar(
                count(OutboxEntry).where(OutboxEntry.incident_id == incident_id)
            ) == 1
            assert await session.scalar(
                count(UIEvent).where(UIEvent.incident_id == incident_id)
            ) == 1
            assert await session.scalar(
                count(PolicyAssignment).where(
                    PolicyAssignment.tenant_id == tenant,
                    PolicyAssignment.incident_id == incident_id,
                )
            ) == 1
            assert await session.scalar(
                select(func.sum(AdmissionUsage.accepted)).where(
                    AdmissionUsage.tenant_id == tenant
                )
            ) == 1
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_active_capacity_is_atomic_under_parallel_admissions() -> None:
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.models import Incident
    from backend.schemas import IncidentCreate
    from backend.services import RateLimited, admit_incident
    from backend.settings import Settings

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "cap-" + uuid4().hex[:12]

    # A local settings override applies only to service capacity accounting,
    # not the database URL, and is restored before returning.
    import backend.services as services
    original_settings = services.get_settings
    services.get_settings = lambda: Settings(
        max_active_incidents_per_tenant=2,
        max_active_incidents_global=100,
    )
    try:
        async def attempt(index: int) -> bool:
            async with sessions() as session:
                try:
                    await admit_incident(
                        session, tenant, f"key-{index}",
                        IncidentCreate(target_id=f"fixture-{index}"),
                    )
                    return True
                except RateLimited:
                    return False

        accepted = await asyncio.gather(*(attempt(i) for i in range(6)))
        assert sum(accepted) == 2
        async with sessions() as session:
            assert await session.scalar(
                select(func.count()).select_from(Incident)
                .where(Incident.tenant_id == tenant)
            ) == 2
    finally:
        services.get_settings = original_settings
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_parallel_target_reservations_are_epoch_fenced() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.adapters.simulator import SimulatorAdapter, StaleFence
    from backend.effects.reservations import (
        ReservationBusy, reserve_simulator_target,
    )
    from backend.models import ResourceEpoch, ResourceReservation, SimulatorTarget
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "fence-" + uuid4().hex[:12]
    target_id = "shared-fixture"
    try:
        async with sessions() as session:
            async with session.begin():
                session.add(SimulatorTarget(
                    tenant_id=tenant, target_id=target_id,
                    version=1, healthy=False, last_fence=0,
                ))
                session.add(ResourceEpoch(
                    tenant_id=tenant, target_id=target_id,
                    latest_fence=0, version_seen=1,
                ))
        ids = []
        for i in range(5):
            async with sessions() as session:
                incident = await admit_incident(
                    session, tenant, f"fence-{i}",
                    IncidentCreate(target_id=target_id),
                )
                ids.append(incident.id)

        async def attempt(index: int):
            try:
                permit = await reserve_simulator_target(
                    sessions, tenant, target_id, ids[index],
                    f"{index + 1:064x}",
                )
                return (index, permit)
            except ReservationBusy:
                return (index, None)

        results = await asyncio.gather(*(attempt(i) for i in range(5)))
        winners = [(i, permit) for i, permit in results if permit is not None]
        assert len(winners) == 1
        winner_index, original = winners[0]
        assert original.fence == 1

        async with sessions() as session:
            reservation = await session.get(
                ResourceReservation, (tenant, target_id)
            )
            assert reservation.holder_incident == ids[winner_index]
            assert reservation.fence == original.fence

        async with sessions() as session:
            async with session.begin():
                reservation = await session.get(
                    ResourceReservation, (tenant, target_id), with_for_update=True
                )
                reservation.expires_at = (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                )

        successor = (winner_index + 1) % len(ids)
        newer = await reserve_simulator_target(
            sessions, tenant, target_id, ids[successor],
            f"{successor + 1:064x}",
        )
        assert newer.fence > original.fence
        adapter = SimulatorAdapter(sessions)
        with pytest.raises(StaleFence):
            await adapter.apply(uuid4(), original)
        assert await adapter.observe(tenant, target_id) == (1, False)
    finally:
        await engine.dispose()
