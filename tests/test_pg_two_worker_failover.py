"""Native PostgreSQL + Temporal failover across independent worker instances.

This exercises a two-worker handoff and concurrent dispatcher calls. It is
a single-host CI simulation, not a multi-zone availability certification.
"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.adapters.conformance import execute_suite
from backend.models import (
    EffectReceipt, Incident, IncidentPlan, OutboxEntry, ResourceEpoch,
    SimulatorAppliedEffect, SimulatorTarget,
)
from backend.plans import decide_plan
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import get_settings
from backend.workers.dispatcher import dispatch_once
from backend.workflows.activities import (
    apply_approved_simulator_plan, inspect_incident_read_only,
    project_incident_status, select_incident_route,
)
from backend.workflows.incident import IncidentWorkflow


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_two_dispatchers_then_worker_handoff_preserves_one_effect():
    settings = get_settings()
    assert settings.database_url.startswith("postgresql+asyncpg:")
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "worker-handoff-" + uuid4().hex[:12]
    try:
        assert (await execute_suite(sessions)).state == "LAB_PASS"
        async with sessions() as session:
            async with session.begin():
                session.add(SimulatorTarget(
                    tenant_id=tenant, target_id="checkout",
                    healthy=False, version=1, last_fence=0,
                ))
                session.add(ResourceEpoch(
                    tenant_id=tenant, target_id="checkout",
                    latest_fence=0, version_seen=1,
                ))
        async with sessions() as session:
            incident = await admit_incident(
                session, tenant, "stable", IncidentCreate(target_id="checkout"),
            )

        async def until_state(expected):
            for _ in range(150):
                async with sessions() as session:
                    state = await session.scalar(
                        select(Incident.status_projection).where(
                            Incident.id == incident.id,
                            Incident.tenant_id == tenant,
                        )
                    )
                if state == expected:
                    return
                await asyncio.sleep(0.1)
            raise AssertionError(f"incident did not reach {expected}: {state}")

        async with await WorkflowEnvironment.start_local() as env:
            queue = settings.temporal_task_queue
            config = dict(
                client=env.client, task_queue=queue,
                workflows=[IncidentWorkflow],
                activities=[
                    inspect_incident_read_only, project_incident_status,
                    apply_approved_simulator_plan, select_incident_route,
                ],
            )

            # Two separately-claimed outbox reads target the same database.
            # The first worker performs investigation, then exits.
            async with Worker(**config):
                await asyncio.gather(
                    dispatch_once(env.client, sessions, tenant_filter=tenant),
                    dispatch_once(env.client, sessions, tenant_filter=tenant),
                )
                await until_state("AWAITING_APPROVAL")

            async with sessions() as session:
                plan = await session.scalar(
                    select(IncidentPlan).where(
                        IncidentPlan.tenant_id == tenant,
                        IncidentPlan.incident_id == incident.id,
                    )
                )
                assert plan is not None
                plan_id = plan.id
                await session.rollback()
            async with sessions() as session:
                await decide_plan(session, tenant, plan_id, 1, "APPROVE")

            # Both dispatcher instances run while no incident worker exists.
            # The approval signal is durable in Temporal and is replayed by
            # the replacement worker without re-running initial probes.
            await asyncio.gather(
                dispatch_once(env.client, sessions, tenant_filter=tenant),
                dispatch_once(env.client, sessions, tenant_filter=tenant),
            )
            async with Worker(**config):
                await until_state("VERIFIED")

        async with sessions() as session:
            target = await session.get(SimulatorTarget, (tenant, "checkout"))
            assert target.version == 2 and target.healthy
            receipts = (await session.scalars(
                select(EffectReceipt).where(
                    EffectReceipt.incident_id == incident.id,
                    EffectReceipt.tenant_id == tenant,
                )
            )).all()
            assert len(receipts) == 1
            assert receipts[0].state == "VERIFIED"
            assert receipts[0].attempt_count == 1
            assert await session.scalar(
                select(func.count()).select_from(SimulatorAppliedEffect).where(
                    SimulatorAppliedEffect.id == receipts[0].id,
                )
            ) == 1
            outbox = (await session.scalars(
                select(OutboxEntry).where(
                    OutboxEntry.incident_id == incident.id
                ).order_by(OutboxEntry.id)
            )).all()
            assert [item.kind for item in outbox] == [
                "START_WORKFLOW", "PLAN_DECIDED",
            ]
            assert all(item.delivered_at is not None for item in outbox)
    finally:
        await engine.dispose()
