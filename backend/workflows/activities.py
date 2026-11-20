from uuid import UUID
from datetime import datetime, timezone

from sqlalchemy import update
from temporalio import activity

from backend.database import get_session_maker
from backend.models import Incident, UIEvent


@activity.defn(name="project_incident_status")
async def project_incident_status(
    tenant_id: str, incident_id: str, state: str
) -> None:
    if state not in {
        "INVESTIGATING", "AWAITING_APPROVAL", "NEEDS_REVIEW", "CANCELLED",
        "EXECUTING", "VERIFIED", "FAILED"
    }:
        raise ValueError("unsupported workflow projection")

    async with get_session_maker()() as session:
        async with session.begin():
            # A previously admitted cancellation must not be overwritten by a
            # late investigation projection. Only CANCELLED can finalize it.
            if state == "CANCELLED":
                valid_states = ["DISPATCH_PENDING", "INVESTIGATING", "AWAITING_APPROVAL", "CANCEL_REQUESTED"]
            else:
                valid_states = ["DISPATCH_PENDING", "INVESTIGATING", "AWAITING_APPROVAL", "PLAN_DECIDED", "EXECUTING"]
                if state == "NEEDS_REVIEW":
                    valid_states.append("REVIEW_ACKNOWLEDGED")
            result = await session.execute(
                update(Incident)
                .where(
                    Incident.id == UUID(incident_id),
                    Incident.tenant_id == tenant_id,
                    Incident.status_projection.in_(valid_states),
                )
                .values(
                    status_projection=state,
                    status_version=Incident.status_version + 1,
                    phase_entered_at=datetime.now(timezone.utc),
                )
            )
            if result.rowcount == 1:
                projected = await session.get(Incident, UUID(incident_id))
                session.add(
                    UIEvent(
                        tenant_id=tenant_id, incident_id=projected.id,
                        kind=state, projection_version=projected.status_version,
                    )
                )


@activity.defn(name="inspect_incident_read_only")
async def inspect_incident_read_only(
    tenant_id: str, incident_id: str
) -> str:
    from backend.investigator.baseline import inspect

    return await inspect(get_session_maker(), tenant_id, UUID(incident_id))


@activity.defn(name="apply_approved_simulator_plan")
async def apply_approved_simulator_plan(
    tenant_id: str, incident_id: str, plan_id: str,
) -> str:
    from backend.plans import execute_approved_simulator_plan

    return await execute_approved_simulator_plan(
        get_session_maker(), tenant_id, UUID(incident_id), UUID(plan_id)
    )


@activity.defn(name="select_incident_route")
async def select_incident_route(tenant_id: str, incident_id: str) -> str:
    from backend.routing.decision import select_route

    decision = await select_route(get_session_maker(), tenant_id, UUID(incident_id))
    return decision.route if decision is not None else "HANDOFF"
