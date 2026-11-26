from uuid import UUID
from datetime import datetime, timezone

from opentelemetry.context import Context
from sqlalchemy import update
from temporalio import activity

from backend.database import get_session_maker
from backend.models import Incident, UIEvent
from backend.settings import get_settings
from backend.trace_refs import admission_parent
from backend.telemetry import trace_operation
from backend.workflows.activity_timing import schedule_to_start_ms


def _trace_activity(operation: str, incident_id: str, parent: Context | None = None):
    delay = None
    if activity.in_activity():
        info = activity.info()
        delay = schedule_to_start_ms(
            info.current_attempt_scheduled_time, info.started_time,
        )
    return trace_operation(
        operation, incident_id=UUID(incident_id), activity_schedule_to_start_ms=delay, parent=parent,
    )


async def _activity_parent(tenant_id: str, incident_id: str) -> Context:
    if not get_settings().otel_traces_endpoint:
        return Context()
    return await admission_parent(
        get_session_maker(), tenant_id, UUID(incident_id), enabled=True,
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

    parent = await _activity_parent(tenant_id, incident_id)
    with _trace_activity("opsblaze.incident.project", incident_id, parent):
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
    from backend.investigator.advisory import MAX_RESERVE_TOKENS
    from backend.routing.decision import ROUTER_POLICY_ID, ROUTER_VERSION, pinned_runbook_strategy, read_route

    parent = await _activity_parent(tenant_id, incident_id)
    with _trace_activity("opsblaze.incident.inspect", incident_id, parent):
        sessions = get_session_maker()
        source_id = UUID(incident_id)
        async with sessions() as session:
            decision = await read_route(session, tenant_id, source_id)
        model_id = None
        reuse_legacy_advice = False
        strategy = "ADAPTIVE"
        if decision is not None:
            if decision.policy_id != ROUTER_POLICY_ID or decision.cost_cap_probes != 2:
                return "ABSTAINED"
            if decision.route == "AGENT_NO_MEMORY":
                if (
                    decision.router_version != ROUTER_VERSION or not decision.model_id
                    or decision.model_token_reservation != MAX_RESERVE_TOKENS
                ):
                    return "ABSTAINED"
                model_id = decision.model_id
            elif decision.route == "RUNBOOK":
                reuse_legacy_advice = decision.router_version is None
                strategy = pinned_runbook_strategy(decision)
            else:
                return "ABSTAINED"
        return await inspect(
            sessions, tenant_id, source_id, advisory_model=model_id,
            reuse_legacy_advice=reuse_legacy_advice,
            allow_memory=(decision is None or decision.route != "AGENT_NO_MEMORY"),
            strategy=strategy,
        )


@activity.defn(name="apply_approved_simulator_plan")
async def apply_approved_simulator_plan(
    tenant_id: str, incident_id: str, plan_id: str,
) -> str:
    from backend.plans import execute_approved_simulator_plan

    parent = await _activity_parent(tenant_id, incident_id)
    with _trace_activity("opsblaze.incident.simulator_apply", incident_id, parent):
        return await execute_approved_simulator_plan(
            get_session_maker(), tenant_id, UUID(incident_id), UUID(plan_id)
        )


@activity.defn(name="select_incident_route")
async def select_incident_route(tenant_id: str, incident_id: str) -> str:
    from backend.routing.decision import select_route

    parent = await _activity_parent(tenant_id, incident_id)
    with _trace_activity("opsblaze.incident.route", incident_id, parent):
        decision = await select_route(get_session_maker(), tenant_id, UUID(incident_id))
        return decision.route if decision is not None else "HANDOFF"
