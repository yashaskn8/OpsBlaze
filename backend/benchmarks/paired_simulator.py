"""Drive the lab comparison through registered Temporal activities and the outbox."""

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy import select
from temporalio.client import Client

from backend.database import get_session_maker
from backend.evaluation.local_benchmarks import Strategy, run_e5_paired_recovery_cost
from backend.models import Incident, IncidentPlan
from backend.routing.decision import select_route
from backend.workers.dispatcher import dispatch_once, workflow_id


async def run_e5_with_temporal(
    client: Client, tenant_prefix: str,
    *, review_requested: Callable[[str, UUID, int], Awaitable[None]],
) -> dict:
    """Notify a separate reviewer and await their normal version-bound API decision."""
    sessions = get_session_maker()

    async def drive(tenant: str, incident_id: UUID, strategy: Strategy) -> None:
        await select_route(
            sessions, tenant, incident_id,
            strategy_mode="FIXED_RUNBOOK" if strategy == "FIXED_RUNBOOK" else "DEFAULT",
        )
        handle = client.get_workflow_handle(workflow_id(tenant, incident_id))
        notified = False
        async with asyncio.timeout(30):
            while True:
                await dispatch_once(client, sessions, tenant_filter=tenant)
                async with sessions() as session:
                    status, plan_id, plan_version = (await session.execute(
                        select(Incident.status_projection, IncidentPlan.id, IncidentPlan.version)
                        .outerjoin(IncidentPlan, (IncidentPlan.incident_id == Incident.id)
                                   & (IncidentPlan.tenant_id == Incident.tenant_id))
                        .where(Incident.tenant_id == tenant, Incident.id == incident_id)
                    )).one()
                if status in {"NEEDS_REVIEW", "VERIFIED", "CANCELLED", "FAILED"}:
                    await handle.result()
                    return
                if status == "AWAITING_APPROVAL" and plan_id is not None and not notified:
                    notified = True
                    await review_requested(tenant, plan_id, plan_version)
                await asyncio.sleep(0.02)

    return await run_e5_paired_recovery_cost(sessions, tenant_prefix, drive_trial=drive)
