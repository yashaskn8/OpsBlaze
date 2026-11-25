from uuid import UUID
from datetime import datetime, timezone

from sqlalchemy import update
from temporalio import activity

from backend.database import get_session_maker
from backend.models import Incident, UIEvent
from backend.telemetry import trace_operation
from backend.workflows.activity_timing import schedule_to_start_ms


def _trace_activity(operation: str, incident_id: str):
    delay = None
    if activity.in_activity():
        info = activity.info()
        delay = schedule_to_start_ms(
            info.current_attempt_scheduled_time, info.started_time,
        )
    return trace_operation(
        operation, incident_id=UUID(incident_id), activity_schedule_to_start_ms=delay,
    )


# A projection is only a cached view of Temporal history. These state guards
# block stale activity retries and impossible terminal transitions.
PREDECESSORS: dict[str, tuple[str, ...]] = {
    "INVESTIGATING": ("DISPATCH_PENDING",),
    "AWAITING_APPROVAL": ("INVESTIGATING",),
    "EXECUTING": ("PLAN_DECIDED",),
    "VERIFIED": ("EXECUTING",),
    "FAILED": ("EXECUTING",),
    "NEEDS_REVIEW": (
        "INVESTIGATING", "PLAN_DECIDED", "EXECUTING", "REVIEW_ACKNOWLEDGED",
    ),
    "CANCELLED": (
        "DISPATCH_PENDING", "INVESTIGATING", "AWAITING_APPROVAL",
        "CANCEL_REQUESTED", "REVIEW_ACKNOWLEDGED",
    ),
}


@activity.defn(name="project_incident_status")
async def project_incident_status(
    tenant_id: str, incident_id: str, state: str,
) -> None:
    try:
        predecessors = PREDECESSORS[state]
    except KeyError as exc:
        raise ValueError("unsupported workflow projection") from exc

    with _trace_activity("opsblaze.incident.project", incident_id):
        async with get_session_maker()() as session:
            async with session.begin():
                result = await session.execute(
                    update(Incident)
                    .where(
                        Incident.id == UUID(incident_id),
                        Incident.tenant_id == tenant_id,
                        Incident.status_projection.in_(predecessors),
                    )
                    .values(
                        status_projection=state,
                        status_version=Incident.status_version + 1,
                        phase_entered_at=datetime.now(timezone.utc),
                    )
                )
                if result.rowcount == 1:
                    incident = await session.get(Incident, UUID(incident_id))
                    session.add(UIEvent(
                        tenant_id=tenant_id, incident_id=incident.id,
                        kind=state, projection_version=incident.status_version,
                    ))
                # Duplicate deliveries, stale projections and cancellation races
                # change neither status_version nor the event ledger.


@activity.defn(name="inspect_incident_read_only")
async def inspect_incident_read_only(
    tenant_id: str, incident_id: str
) -> str:
    from backend.investigator.baseline import inspect

    with _trace_activity("opsblaze.incident.inspect", incident_id):
        return await inspect(get_session_maker(), tenant_id, UUID(incident_id))


@activity.defn(name="apply_approved_simulator_plan")
async def apply_approved_simulator_plan(
    tenant_id: str, incident_id: str, plan_id: str,
) -> str:
    from backend.plans import execute_approved_simulator_plan

    with _trace_activity("opsblaze.incident.simulator_apply", incident_id):
        return await execute_approved_simulator_plan(
            get_session_maker(), tenant_id, UUID(incident_id), UUID(plan_id)
        )


@activity.defn(name="select_incident_route")
async def select_incident_route(tenant_id: str, incident_id: str) -> str:
    from backend.routing.decision import select_route

    with _trace_activity("opsblaze.incident.route", incident_id):
        decision = await select_route(get_session_maker(), tenant_id, UUID(incident_id))
        return decision.route if decision is not None else "HANDOFF"
