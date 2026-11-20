from uuid import UUID

from sqlalchemy import update
from temporalio import activity

from backend.database import get_session_maker
from backend.models import Incident


@activity.defn(name="project_incident_status")
async def project_incident_status(
    tenant_id: str, incident_id: str, state: str
) -> None:
    if state not in {
        "INVESTIGATING", "AWAITING_APPROVAL", "NEEDS_REVIEW", "CANCELLED"
    }:
        raise ValueError("unsupported workflow projection")

    async with get_session_maker()() as session:
        async with session.begin():
            # A previously admitted cancellation must not be overwritten by a
            # late investigation projection. Only CANCELLED can finalize it.
            if state == "CANCELLED":
                valid_states = ["DISPATCH_PENDING", "INVESTIGATING", "AWAITING_APPROVAL", "CANCEL_REQUESTED"]
            else:
                valid_states = ["DISPATCH_PENDING", "INVESTIGATING", "AWAITING_APPROVAL"]
            await session.execute(
                update(Incident)
                .where(
                    Incident.id == UUID(incident_id),
                    Incident.tenant_id == tenant_id,
                    Incident.status_projection.in_(valid_states),
                )
                .values(
                    status_projection=state,
                    status_version=Incident.status_version + 1,
                )
            )


@activity.defn(name="inspect_incident_read_only")
async def inspect_incident_read_only(
    tenant_id: str, incident_id: str
) -> str:
    # Minimal safe baseline: never invent a diagnosis or mutate any target.
    # Full A1/A2 probes and A3 evidence provenance are later stages.
    return "NO_PROBES_CONFIGURED"
