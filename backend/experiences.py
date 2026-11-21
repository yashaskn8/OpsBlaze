
"""E1: opt-in, metadata-only experience capsules with revocation and expiry."""

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.adapters.simulator import utc
from backend.models import (
    Evidence, ExperienceCapsule, ExperienceConsent, Incident, OperationalLesson,
    OutcomeRevision, TenantExperienceConsent,
)
from backend.settings import get_settings
from backend.telemetry import trace_operation


class ConsentConflict(Exception):
    pass


async def lock_tenant_consent(session: AsyncSession, tenant_id: str) -> None:
    """Serialize tenant policy change against new capsule capture on PostgreSQL."""
    if session.bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(923411, hashtext(:tenant))"),
            {"tenant": tenant_id},
        )


async def set_tenant_consent(
    session: AsyncSession, tenant_id: str,
    expected_epoch: int, allowed: bool,
) -> TenantExperienceConsent:
    """CAS tenant-wide reuse consent. Re-enabling never restores old capsules."""
    if expected_epoch < 0:
        raise ConsentConflict("invalid tenant consent epoch")
    with trace_operation("opsblaze.experience.capture", incident_id=incident_id):
        async with session.begin():
            await lock_tenant_consent(session, tenant_id)
            policy = await session.get(
                TenantExperienceConsent, tenant_id, with_for_update=True,
            )
            actual = 0 if policy is None else policy.epoch
            if actual != expected_epoch:
                raise ConsentConflict("tenant consent epoch changed")
            now = datetime.now(timezone.utc)
            if policy is None:
                policy = TenantExperienceConsent(
                    tenant_id=tenant_id, epoch=1, allowed=allowed, decided_at=now,
                )
                session.add(policy)
            else:
                policy.epoch += 1
                policy.allowed = allowed
                policy.decided_at = now
        return policy
    
    
    async def current_tenant_consent(
        session: AsyncSession, tenant_id: str,
        recorded_epoch: int,
    ) -> bool:
        """Legacy epoch zero uses explicit incident consent until a tenant rule exists."""
        policy = await session.get(TenantExperienceConsent, tenant_id)
        return (
            recorded_epoch == (0 if policy is None else policy.epoch)
            and (policy is None or policy.allowed)
        )
    
    
    async def set_consent(
        session: AsyncSession, tenant_id: str, incident_id: UUID,
        expected_epoch: int, allowed: bool,
    ) -> ExperienceConsent | None:
        async with session.begin():
            incident = await session.scalar(
                select(Incident).where(
                    Incident.tenant_id == tenant_id, Incident.id == incident_id
                ).with_for_update()
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
                ids = list((await session.scalars(
                    select(ExperienceCapsule.id).where(
                        ExperienceCapsule.tenant_id == tenant_id,
                        ExperienceCapsule.incident_id == incident_id,
                    )
                )).all())
                await session.execute(
                    update(ExperienceCapsule)
                    .where(
                        ExperienceCapsule.tenant_id == tenant_id,
                        ExperienceCapsule.incident_id == incident_id,
                    )
                    .values(status="REVOKED", evidence_ids=[], target_digest="")
                )
                if ids:
                    await session.execute(
                        update(OperationalLesson)
                        .where(
                            OperationalLesson.tenant_id == tenant_id,
                            OperationalLesson.source_capsule_id.in_(ids),
                            OperationalLesson.status != "REVOKED",
                        )
                        .values(
                            status="REVOKED",
                            version=OperationalLesson.version + 1,
                            target_digest="",
                        )
                    )
        return consent
    
    
    async def capture_capsule(
        session: AsyncSession, tenant_id: str, incident_id: UUID,
        expected_outcome_id: UUID | None = None,
    ) -> ExperienceCapsule | None:
        now = datetime.now(timezone.utc)
        retention = get_settings().experience_retention_days
        if not 1 <= retention <= 30:
            raise ValueError("experience retention must be within 1–30 days")
        async with session.begin():
            await lock_tenant_consent(session, tenant_id)
            tenant_policy = await session.get(
                TenantExperienceConsent, tenant_id, with_for_update=True,
            )
            if tenant_policy is not None and not tenant_policy.allowed:
                raise ConsentConflict("tenant has revoked experience reuse")
            tenant_epoch = 0 if tenant_policy is None else tenant_policy.epoch
            incident = await session.scalar(
                select(Incident).where(
                    Incident.id == incident_id, Incident.tenant_id == tenant_id
                ).with_for_update()
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
            if expected_outcome_id is not None and outcome.id != expected_outcome_id:
                raise ConsentConflict("source outcome was superseded before projection")
            existing = await session.scalar(
                select(ExperienceCapsule).where(
                    ExperienceCapsule.incident_id == incident_id,
                    ExperienceCapsule.outcome_revision_id == outcome.id,
                )
            )
            if existing is not None:
                if (
                    existing.status != "VALID"
                    or existing.tenant_id != tenant_id
                    or existing.consent_epoch != consent.epoch
                    or existing.tenant_consent_epoch != tenant_epoch
                    or existing.target_digest != hashlib.sha256(
                        f"{tenant_id}:{incident.target_id}".encode("utf-8")
                    ).hexdigest()
                    or utc(existing.expires_at) <= now
                ):
                    raise ConsentConflict("stale, altered or revoked capsule cannot be reused")
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
                tenant_consent_epoch=tenant_epoch, target_digest=digest, evidence_ids=[str(item) for item in evidence],
                status="VALID", expires_at=now + timedelta(days=retention),
            )
            session.add(capsule)
    return capsule


async def read_capsules_page(
    session: AsyncSession, tenant_id: str,
    cursor: UUID | None = None, limit: int = 50,
) -> tuple[list[ExperienceCapsule], str | None]:
    # Consent and expiry checks happen at read-time as well as capture-time.
    if not 1 <= limit <= 50:
        raise ValueError("experience page size must be 1–50")
    latest_outcome_id = (
        select(OutcomeRevision.id)
        .where(
            OutcomeRevision.tenant_id == tenant_id,
            OutcomeRevision.incident_id == ExperienceCapsule.incident_id,
        )
        .order_by(OutcomeRevision.revision.desc())
        .limit(1)
        .correlate(ExperienceCapsule)
        .scalar_subquery()
    )
    query = (
        select(ExperienceCapsule, Incident.target_id)
        .join(
            ExperienceConsent,
            ExperienceConsent.incident_id == ExperienceCapsule.incident_id,
        )
        .join(
            Incident,
            Incident.id == ExperienceCapsule.incident_id,
        )
        .join(
            OutcomeRevision,
            OutcomeRevision.id == ExperienceCapsule.outcome_revision_id,
        )
        .outerjoin(
            TenantExperienceConsent,
            TenantExperienceConsent.tenant_id == ExperienceCapsule.tenant_id,
        )
        .where(
            or_(
                and_(
                    TenantExperienceConsent.tenant_id.is_(None),
                    ExperienceCapsule.tenant_consent_epoch == 0,
                ),
                and_(
                    TenantExperienceConsent.allowed.is_(True),
                    TenantExperienceConsent.epoch
                    == ExperienceCapsule.tenant_consent_epoch,
                ),
            ),
            ExperienceCapsule.tenant_id == tenant_id,
            Incident.tenant_id == tenant_id,
            ExperienceConsent.tenant_id == tenant_id,
            ExperienceConsent.allowed.is_(True),
            ExperienceCapsule.consent_epoch == ExperienceConsent.epoch,
            ExperienceCapsule.status == "VALID",
            ExperienceCapsule.outcome_revision_id == latest_outcome_id,
            OutcomeRevision.tenant_id == tenant_id,
            OutcomeRevision.label == "VERIFIED",
            ExperienceCapsule.expires_at > datetime.now(timezone.utc),
        )
    )
    if cursor is not None:
        query = query.where(ExperienceCapsule.id > cursor)
    rows = (await session.execute(
        query.order_by(ExperienceCapsule.id).limit(limit + 1)
    )).all()
    scanned = rows[:limit]
    visible = [
        capsule for capsule, target_id in scanned
        if capsule.target_digest == hashlib.sha256(
            f"{tenant_id}:{target_id}".encode("utf-8")
        ).hexdigest()
    ]
    # Advance past scanned (possibly invalid) rows, not only returned rows.
    # Otherwise a forged digest could produce an infinite cursor replay.
    next_cursor = str(scanned[-1][0].id) if len(rows) > limit else None
    return visible, next_cursor


async def list_capsules(
    session: AsyncSession, tenant_id: str,
) -> list[ExperienceCapsule]:
    # Internal callers retain the bounded, first-page compatibility API.
    capsules, _ = await read_capsules_page(session, tenant_id)
    return capsules


async def quarantine_capsule(
    session: AsyncSession, tenant_id: str, capsule_id: UUID,
    expected_consent_epoch: int,
) -> ExperienceCapsule | None:
    """One-winner E1 quarantine; immediately invalidate derived lessons."""
    if expected_consent_epoch < 1:
        raise ConsentConflict("invalid capsule consent epoch")
    async with session.begin():
        source = await session.scalar(
            select(ExperienceCapsule.incident_id).where(
                ExperienceCapsule.id == capsule_id,
                ExperienceCapsule.tenant_id == tenant_id,
            )
        )
        if source is None:
            return None
        incident = await session.scalar(
            select(Incident.id).where(
                Incident.id == source,
                Incident.tenant_id == tenant_id,
            ).with_for_update()
        )
        if incident is None:
            return None
        capsule = await session.scalar(
            select(ExperienceCapsule).where(
                ExperienceCapsule.id == capsule_id,
                ExperienceCapsule.tenant_id == tenant_id,
                ExperienceCapsule.incident_id == incident,
            ).with_for_update().execution_options(populate_existing=True)
        )
        if capsule is None:
            return None
        if (
            capsule.status != "VALID"
            or capsule.consent_epoch != expected_consent_epoch
        ):
            raise ConsentConflict("capsule status or consent epoch changed")
        consent = await session.get(ExperienceConsent, incident)
        if (
            consent is None or consent.tenant_id != tenant_id
            or not consent.allowed or consent.epoch != capsule.consent_epoch
        ):
            raise ConsentConflict("capsule source consent is no longer valid")
        capsule.status = "QUARANTINED"
        capsule.target_digest = ""
        capsule.evidence_ids = []
        await session.execute(
            update(OperationalLesson).where(
                OperationalLesson.tenant_id == tenant_id,
                OperationalLesson.source_capsule_id == capsule_id,
                OperationalLesson.status != "REVOKED",
            ).values(
                status="REVOKED", target_digest="",
                version=OperationalLesson.version + 1,
            )
        )
    return capsule


async def export_privacy_page(
    session: AsyncSession, tenant_id: str, kind: str,
    cursor: UUID | None = None, limit: int = 50,
) -> dict:
    """Bounded redacted consent/capsule audit, including revoked source rows.

    This is a metadata export, not a raw evidence or backup erasure API.
    No target digest, probe excerpt, token, or unredacted prompt leaves it.
    """
    if kind not in {"consents", "capsules"} or not 1 <= limit <= 50:
        raise ValueError("unsupported privacy export page")
    if kind == "consents":
        model = ExperienceConsent
        key = ExperienceConsent.incident_id
    else:
        model = ExperienceCapsule
        key = ExperienceCapsule.id
    stmt = (
        select(model).join(Incident, Incident.id == model.incident_id)
        .where(model.tenant_id == tenant_id, Incident.tenant_id == tenant_id)
    )
    if cursor is not None:
        stmt = stmt.where(key > cursor)
    rows = list((await session.scalars(
        stmt.order_by(key).limit(limit + 1)
    )).all())
    visible = rows[:limit]
    if kind == "consents":
        records = [
            {
                "incident_id": str(row.incident_id),
                "epoch": row.epoch,
                "reuse_allowed": row.allowed,
                "decided_at": row.decided_at.isoformat(),
            }
            for row in visible
        ]
    else:
        records = [
            {
                "id": str(row.id),
                "incident_id": str(row.incident_id),
                "outcome_revision_id": str(row.outcome_revision_id),
                "consent_epoch": row.consent_epoch,
                "status": row.status,
                "created_at": row.created_at.isoformat(),
                "expires_at": row.expires_at.isoformat(),
            }
            for row in visible
        ]
    return {
        "kind": kind,
        "records": records,
        "next_cursor": str(getattr(visible[-1], key.key))
        if len(rows) > limit else None,
        "metadata_only": True,
    }
