import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import AdmissionUsage, Incident, OutboxEntry, UIEvent
from backend.schemas import IncidentCreate
from backend.settings import get_settings


class IdempotencyConflict(Exception):
    pass


class RateLimited(Exception):
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
            existing = await session.scalar(
                select(Incident).where(
                    Incident.tenant_id == tenant_id,
                    Incident.client_key == client_key,
                )
            )
            if existing is not None:
                if existing.request_sha256 != fingerprint:
                    raise IdempotencyConflict("idempotency key reused for different payload")
                return existing
            await reserve_admission_slot(session, tenant_id)
            session.add(incident)
            session.add(OutboxEntry(incident_id=incident.id, kind="START_WORKFLOW"))
            session.add(UIEvent(tenant_id=tenant_id, incident_id=incident.id, kind="ADMITTED", projection_version=1))
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
        if incident.status_projection in {"CANCELLED", "VERIFIED", "FAILED", "NEEDS_REVIEW", "CANCEL_REQUESTED", "REVIEW_ACKNOWLEDGED"}:
            raise VersionConflict("incident is terminal or cancellation is pending")
        incident.status_projection = "CANCEL_REQUESTED"
        incident.status_version += 1
        session.add(OutboxEntry(incident_id=incident.id, kind="CANCEL_WORKFLOW"))
        session.add(UIEvent(tenant_id=tenant_id, incident_id=incident.id, kind="CANCEL_REQUESTED", projection_version=incident.status_version))
    return incident

async def acknowledge_review(
    session: AsyncSession, tenant_id: str, incident_id: UUID, expected_version: int
) -> Incident | None:
    """Durably record an operator acknowledgement before signaling Temporal."""
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
        if incident.status_projection != "AWAITING_APPROVAL":
            raise VersionConflict("incident is not awaiting review")
        incident.status_projection = "REVIEW_ACKNOWLEDGED"
        incident.status_version += 1
        session.add(OutboxEntry(incident_id=incident.id, kind="ACKNOWLEDGE_REVIEW"))
        session.add(UIEvent(tenant_id=tenant_id, incident_id=incident.id, kind="REVIEW_ACKNOWLEDGED", projection_version=incident.status_version))
    return incident


async def reserve_admission_slot(
    session: AsyncSession,
    tenant_id: str,
    limit: int | None = None,
) -> None:
    """Atomic per-tenant fixed-window counter in the incident transaction."""
    if limit is None:
        limit = get_settings().admission_limit_per_minute
    if limit < 1:
        raise ValueError("admission limit must be positive")

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif session.bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError("unsupported admission database dialect")

    stmt = insert(AdmissionUsage).values(
        tenant_id=tenant_id, window_start=now, accepted=0
    ).on_conflict_do_nothing(index_elements=["tenant_id", "window_start"])
    await session.execute(stmt)
    result = await session.execute(
        update(AdmissionUsage)
        .where(
            AdmissionUsage.tenant_id == tenant_id,
            AdmissionUsage.window_start == now,
            AdmissionUsage.accepted < limit,
        )
        .values(accepted=AdmissionUsage.accepted + 1)
    )
    if result.rowcount != 1:
        raise RateLimited("tenant admission limit reached")
