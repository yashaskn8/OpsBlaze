
"""E1: opt-in, metadata-only experience capsules with revocation and expiry."""

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    Evidence, ExperienceCapsule, ExperienceConsent, Incident, OutcomeRevision
)
from backend.settings import get_settings


class ConsentConflict(Exception):
    pass


async def set_consent(
    session: AsyncSession, tenant_id: str, incident_id: UUID,
    expected_epoch: int, allowed: bool,
) -> ExperienceConsent | None:
    async with session.begin():
        incident = await session.scalar(
            select(Incident).where(
                Incident.tenant_id == tenant_id, Incident.id == incident_id
            )
        )
        if incident is None:
            return None
        consent = await session.get(
            ExperienceConsent, incident_id, with_for_update=True
        )
        actual_epoch = 0 if consent is None else consent.epoch
        if actual_epoch != expected_epoch:
            raise ConsentConflict("consent epoch changed; refetch before retrying")
        if consent is None:
            consent = ExperienceConsent(
                incident_id=incident_id, tenant_id=tenant_id, epoch=1,
                allowed=allowed, decided_at=datetime.now(timezone.utc),
            )
            session.add(consent)
        else:
            consent.epoch += 1
            consent.allowed = allowed
            consent.decided_at = datetime.now(timezone.utc)
        if not allowed:
            # Keep minimum audit metadata; revoke all previously captured content.
            await session.execute(
                update(ExperienceCapsule)
                .where(
                    ExperienceCapsule.tenant_id == tenant_id,
                    ExperienceCapsule.incident_id == incident_id,
                )
                .values(status="REVOKED", evidence_ids=[], target_digest="")
            )
    return consent


async def capture_capsule(
    session: AsyncSession, tenant_id: str, incident_id: UUID,
) -> ExperienceCapsule | None:
    now = datetime.now(timezone.utc)
    retention = get_settings().experience_retention_days
    if not 1 <= retention <= 30:
        raise ValueError("experience retention must be within 1–30 days")
    async with session.begin():
        incident = await session.scalar(
            select(Incident).where(
                Incident.id == incident_id, Incident.tenant_id == tenant_id
            )
        )
        if incident is None:
            return None
        consent = await session.get(
            ExperienceConsent, incident_id, with_for_update=True
        )
        if consent is None or not consent.allowed:
            raise ConsentConflict("incident has not opted in to experience capture")
        outcome = await session.scalar(
            select(OutcomeRevision)
            .where(
                OutcomeRevision.tenant_id == tenant_id,
                OutcomeRevision.incident_id == incident_id,
            )
            .order_by(OutcomeRevision.revision.desc())
            .limit(1)
        )
        if outcome is None or outcome.label != "VERIFIED":
            raise ConsentConflict("independently verified undisputed outcome required")
        existing = await session.scalar(
            select(ExperienceCapsule).where(
                ExperienceCapsule.incident_id == incident_id,
                ExperienceCapsule.outcome_revision_id == outcome.id,
            )
        )
        if existing is not None:
            if existing.status == "REVOKED":
                raise ConsentConflict("revoked capsule cannot be restored")
            return existing
        evidence = (await session.scalars(
            select(Evidence.id)
            .where(
                Evidence.tenant_id == tenant_id,
                Evidence.incident_id == incident_id,
                Evidence.validity == "VALID",
            )
            .order_by(Evidence.captured_at)
            .limit(25)
        )).all()
        digest = hashlib.sha256(
            f"{tenant_id}:{incident.target_id}".encode("utf-8")
        ).hexdigest()
        capsule = ExperienceCapsule(
            id=uuid4(), tenant_id=tenant_id, incident_id=incident_id,
            outcome_revision_id=outcome.id, consent_epoch=consent.epoch,
            target_digest=digest, evidence_ids=[str(item) for item in evidence],
            status="VALID", expires_at=now + timedelta(days=retention),
        )
        session.add(capsule)
    return capsule


async def list_capsules(session: AsyncSession, tenant_id: str) -> list[ExperienceCapsule]:
    # Consent and expiry checks happen at read-time as well as capture-time.
    rows = (await session.scalars(
        select(ExperienceCapsule)
        .join(
            ExperienceConsent,
            ExperienceConsent.incident_id == ExperienceCapsule.incident_id,
        )
        .where(
            ExperienceCapsule.tenant_id == tenant_id,
            ExperienceConsent.tenant_id == tenant_id,
            ExperienceConsent.allowed.is_(True),
            ExperienceCapsule.consent_epoch == ExperienceConsent.epoch,
            ExperienceCapsule.status == "VALID",
            ExperienceCapsule.expires_at > datetime.now(timezone.utc),
        )
        .limit(50)
    )).all()
    return list(rows)
