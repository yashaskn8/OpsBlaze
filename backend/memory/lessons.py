
"""E4 typed operational lessons: source-backed, revocable, never instructions.

The activation gate is closed until the offline evaluation subsystem is ready.
"""

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.adapters.simulator import utc
from backend.models import (
    ExperienceCapsule, ExperienceConsent, Incident, OperationalLesson,
    OutcomeRevision,
)

LESSON_RULES = {
    "PROBE_HEALTH_FIRST": "Observe simulator target health before proposing any action.",
    "CHECK_VERSION": "Require a matching target version before the proposed simulator change.",
    "ESCALATE_UNCERTAIN": "Use operator review when the effect result is unknown or disputed.",
}


class LessonConflict(Exception):
    pass


async def valid_source(
    session: AsyncSession, tenant_id: str, capsule_id: UUID,
) -> ExperienceCapsule | None:
    capsule = await session.scalar(
        select(ExperienceCapsule).where(
            ExperienceCapsule.id == capsule_id,
            ExperienceCapsule.tenant_id == tenant_id,
        )
    )
    if capsule is None or capsule.status != "VALID":
        return None
    if utc(capsule.expires_at) <= datetime.now(timezone.utc):
        return None
    consent = await session.get(ExperienceConsent, capsule.incident_id)
    if (
        consent is None or consent.tenant_id != tenant_id
        or not consent.allowed or consent.epoch != capsule.consent_epoch
    ):
        return None
    newest = await session.scalar(
        select(OutcomeRevision)
        .where(
            OutcomeRevision.tenant_id == tenant_id,
            OutcomeRevision.incident_id == capsule.incident_id,
        )
        .order_by(OutcomeRevision.revision.desc())
        .limit(1)
    )
    if (
        newest is None or newest.id != capsule.outcome_revision_id
        or newest.label != "VERIFIED"
    ):
        return None
    return capsule


async def create_draft(
    session: AsyncSession, tenant_id: str, capsule_id: UUID,
    rule_id: str,
) -> OperationalLesson:
    if rule_id not in LESSON_RULES:
        raise LessonConflict("lesson rule is not allowlisted")
    async with session.begin():
        source_incident = await session.scalar(
            select(ExperienceCapsule.incident_id).where(
                ExperienceCapsule.id == capsule_id,
                ExperienceCapsule.tenant_id == tenant_id,
            )
        )
        if source_incident is None:
            raise LessonConflict("source consent or outcome is not valid")
        # Serialize with E1 consent revocation and E2 adjudication, which
        # both lock the incident before invalidating derived artifacts.
        incident = await session.scalar(
            select(Incident.id).where(
                Incident.id == source_incident, Incident.tenant_id == tenant_id,
            ).with_for_update()
        )
        if incident is None:
            raise LessonConflict("source incident missing")
        capsule = await valid_source(session, tenant_id, capsule_id)
        if capsule is None:
            raise LessonConflict("source consent or outcome is not valid")
        existing = await session.scalar(
            select(OperationalLesson).where(
                OperationalLesson.tenant_id == tenant_id,
                OperationalLesson.source_capsule_id == capsule_id,
                OperationalLesson.rule_id == rule_id,
            )
        )
        if existing is not None:
            return existing
        lesson = OperationalLesson(
            id=uuid5(NAMESPACE_URL, f"opsblaze/lesson/{tenant_id}/{capsule_id}/{rule_id}"),
            tenant_id=tenant_id, source_capsule_id=capsule_id,
            source_outcome_id=capsule.outcome_revision_id,
            target_digest=capsule.target_digest, rule_id=rule_id,
            summary_redacted=LESSON_RULES[rule_id],
            consent_epoch=capsule.consent_epoch, status="DRAFT",
            version=1, valid_from=datetime.now(timezone.utc),
            valid_until=min(
                utc(capsule.expires_at),
                datetime.now(timezone.utc) + timedelta(days=7),
            ),
        )
        session.add(lesson)
    return lesson


async def validate_draft(
    session: AsyncSession, tenant_id: str, lesson_id: UUID,
    expected_version: int,
) -> OperationalLesson | None:
    async with session.begin():
        # Read the source first but never lock the lesson ahead of Incident:
        # E1/E2 would otherwise lock Incident then wait for Lesson, deadlocking.
        source_incident = await session.scalar(
            select(ExperienceCapsule.incident_id)
            .join(
                OperationalLesson,
                OperationalLesson.source_capsule_id == ExperienceCapsule.id,
            )
            .where(
                OperationalLesson.id == lesson_id,
                OperationalLesson.tenant_id == tenant_id,
                ExperienceCapsule.tenant_id == tenant_id,
            )
        )
        if source_incident is None:
            return None
        incident = await session.scalar(
            select(Incident.id).where(
                Incident.id == source_incident, Incident.tenant_id == tenant_id,
            ).with_for_update()
        )
        if incident is None:
            raise LessonConflict("source incident missing")
        lesson = await session.scalar(
            select(OperationalLesson).where(
                OperationalLesson.id == lesson_id,
                OperationalLesson.tenant_id == tenant_id,
            ).with_for_update().execution_options(populate_existing=True)
        )
        if lesson is None:
            return None
        if lesson.version != expected_version or lesson.status != "DRAFT":
            raise LessonConflict("stale lesson version or state")
        source = await valid_source(session, tenant_id, lesson.source_capsule_id)
        if (
            source is None or source.consent_epoch != lesson.consent_epoch
            or source.target_digest != lesson.target_digest
            or source.outcome_revision_id != lesson.source_outcome_id
        ):
            raise LessonConflict("source lineage changed")
        if utc(lesson.valid_until) <= datetime.now(timezone.utc):
            raise LessonConflict("lesson has expired")
        if lesson.rule_id not in LESSON_RULES:
            raise LessonConflict("rule is no longer allowed")
        lesson.status = "VALIDATED"
        lesson.version += 1
    return lesson


async def revoke_lesson(
    session: AsyncSession, tenant_id: str, lesson_id: UUID,
    expected_version: int,
) -> OperationalLesson | None:
    async with session.begin():
        lesson = await session.scalar(
            select(OperationalLesson).where(
                OperationalLesson.id == lesson_id,
                OperationalLesson.tenant_id == tenant_id,
            ).with_for_update()
        )
        if lesson is None:
            return None
        if lesson.version != expected_version:
            raise LessonConflict("stale lesson version")
        if lesson.status == "REVOKED":
            raise LessonConflict("already revoked")
        lesson.status = "REVOKED"
        lesson.version += 1
    return lesson


async def retrieve_active_lessons(
    session: AsyncSession, tenant_id: str, target_id: str, limit: int = 4,
) -> list[OperationalLesson]:
    """Fail closed on expiry, consent loss, or corrected outcome at read-time."""
    if limit < 1 or limit > 8:
        raise ValueError("lesson result limit must be 1–8")
    digest = hashlib.sha256(
        f"{tenant_id}:{target_id}".encode("utf-8")
    ).hexdigest()
    rows = (await session.scalars(
        select(OperationalLesson)
        .where(
            OperationalLesson.tenant_id == tenant_id,
            OperationalLesson.target_digest == digest,
            OperationalLesson.status == "ACTIVE",
            OperationalLesson.valid_until > datetime.now(timezone.utc),
        )
        .order_by(OperationalLesson.created_at.desc())
        .limit(limit)
    )).all()
    allowed = []
    now = datetime.now(timezone.utc)
    for lesson in rows:
        capsule = await valid_source(session, tenant_id, lesson.source_capsule_id)
        if (
            capsule is not None
            and lesson.consent_epoch == capsule.consent_epoch
            and lesson.source_outcome_id == capsule.outcome_revision_id
            and lesson.target_digest == capsule.target_digest
            and lesson.summary_redacted == LESSON_RULES.get(lesson.rule_id)
            and utc(lesson.valid_from) <= now < utc(lesson.valid_until)
            and utc(lesson.valid_until) <= utc(capsule.expires_at)
        ):
            allowed.append(lesson)
    return allowed
