import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Incident, OutboxEntry
from backend.schemas import IncidentCreate


class IdempotencyConflict(Exception):
    pass


def request_fingerprint(payload: IncidentCreate) -> str:
    canonical = json.dumps(
        payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def admit_incident(
    session: AsyncSession, tenant_id: str, client_key: str, payload: IncidentCreate
) -> Incident:
    """Atomically persist the incident and a pending workflow-start intent."""
    fingerprint = request_fingerprint(payload)
    incident = Incident(
        id=uuid4(),
        tenant_id=tenant_id,
        client_key=client_key,
        request_sha256=fingerprint,
        target_id=payload.target_id,
        status_projection="DISPATCH_PENDING",
        status_version=1,
    )
    try:
        async with session.begin():
            session.add(incident)
            session.add(OutboxEntry(incident_id=incident.id, kind="START_WORKFLOW"))
            await session.flush()
        return incident
    except IntegrityError:
        await session.rollback()

    existing = await session.scalar(
        select(Incident).where(
            Incident.tenant_id == tenant_id, Incident.client_key == client_key
        )
    )
    if existing is None:
        raise RuntimeError("incident admission failed without an idempotency collision")
    if existing.request_sha256 != fingerprint:
        raise IdempotencyConflict("idempotency key reused for different payload")
    return existing


async def find_incident(
    session: AsyncSession, tenant_id: str, incident_id: UUID
) -> Incident | None:
    return await session.scalar(
        select(Incident).where(
            Incident.tenant_id == tenant_id, Incident.id == incident_id
        )
    )

class VersionConflict(Exception):
    pass


async def request_cancellation(
    session: AsyncSession, tenant_id: str, incident_id: UUID, expected_version: int
) -> Incident | None:
    async with session.begin():
        incident = await session.scalar(
            select(Incident)
            .where(Incident.id == incident_id, Incident.tenant_id == tenant_id)
            .with_for_update()
        )
        if incident is None:
            return None
        if incident.status_version != expected_version:
            raise VersionConflict("incident version changed; refetch before retrying")
        if incident.status_projection in {"CANCELLED", "VERIFIED", "FAILED", "NEEDS_REVIEW", "CANCEL_REQUESTED"}:
            raise VersionConflict("incident is terminal or cancellation is pending")
        incident.status_projection = "CANCEL_REQUESTED"
        incident.status_version += 1
        session.add(OutboxEntry(incident_id=incident.id, kind="CANCEL_WORKFLOW"))
    return incident
