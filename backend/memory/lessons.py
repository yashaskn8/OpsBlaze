
"""E4 typed operational lessons: source-backed, revocable, never instructions.

The activation gate is closed until the offline evaluation subsystem is ready.
"""

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.adapters.simulator import utc
from backend.lineage_projection import replace_record_edges
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
    incident = await session.get(Incident, capsule.incident_id)
    if (
        incident is None or incident.tenant_id != tenant_id
        or capsule.target_digest != hashlib.sha256(
            f"{tenant_id}:{incident.target_id}".encode("utf-8")
        ).hexdigest()
    ):
        # The capsule's untrusted stored target tag must never authorize
        # a lesson for a different incident target.
        return None
    consent = await session.get(ExperienceConsent, capsule.incident_id)
    if (
        consent is None or consent.tenant_id != tenant_id
        or not consent.allowed or consent.epoch != capsule.consent_epoch
    ):
        return None
    from backend.experiences import current_tenant_consent

    if not await current_tenant_consent(
        session, tenant_id, capsule.tenant_consent_epoch,
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
    from backend.experiences import capsule_evidence_intact

    if not await capsule_evidence_intact(session, capsule, incident.target_id):
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
        await session.flush()
        await replace_record_edges(session, tenant_id, incident, "lesson", lesson)
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
        now = datetime.now(timezone.utc)
        if (
            not utc(lesson.valid_from) <= now < utc(lesson.valid_until)
            or utc(lesson.valid_until) > utc(source.expires_at)
        ):
            raise LessonConflict("lesson validity window is not source-bounded")
        if (
            lesson.rule_id not in LESSON_RULES
            or lesson.summary_redacted != LESSON_RULES.get(lesson.rule_id)
        ):
            raise LessonConflict("lesson text or rule was changed")
        lesson.status = "VALIDATED"
        lesson.version += 1
    return lesson


async def revoke_lesson(
    session: AsyncSession, tenant_id: str, lesson_id: UUID,
    expected_version: int,
    *,
    conflict_evidence: dict | None = None,
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
        if isinstance(conflict_evidence, dict):
            lesson.conflict_evidence = dict(conflict_evidence)
    return lesson


async def retrieve_active_lessons(
    session: AsyncSession, tenant_id: str, target_id: str, limit: int = 4,
    *, target_family: str = "simulator", policy_id: str = "deterministic-v1",
) -> list[OperationalLesson]:
    """Fail closed on expiry, consent loss, target family/policy mismatch, or corrected outcome."""
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
            OperationalLesson.target_family == target_family,
            OperationalLesson.policy_id == policy_id,
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
            and lesson.target_family == target_family
            and lesson.policy_id == policy_id
            and lesson.summary_redacted == LESSON_RULES.get(lesson.rule_id)
            and utc(lesson.valid_from) <= now < utc(lesson.valid_until)
            and utc(lesson.valid_until) <= utc(capsule.expires_at)
        ):
            allowed.append(lesson)
    return allowed


async def evaluate_memory_vs_no_memory(
    session: AsyncSession, tenant_id: str, target_id: str,
    *, iterations: int = 5,
) -> dict:
    """E4-07: lab latency and context comparison against isolated no-memory control."""
    from time import perf_counter

    if not 1 <= iterations <= 20:
        raise ValueError("iterations must be 1–20")
    with_mem_ms: list[float] = []
    no_mem_ms: list[float] = []
    mem_items: list[dict] = []
    mem_mode = "NO_MEMORY"
    for _ in range(iterations):
        t0 = perf_counter()
        mem_items, mem_mode = await retrieve_cited_memory_context(
            session, tenant_id, target_id, allow_memory=True,
        )
        with_mem_ms.append((perf_counter() - t0) * 1000.0)
        t1 = perf_counter()
        _, ctrl_mode = await retrieve_cited_memory_context(
            session, tenant_id, target_id, allow_memory=False,
        )
        no_mem_ms.append((perf_counter() - t1) * 1000.0)
    with_mem_ms.sort()
    no_mem_ms.sort()
    p50_idx = int(0.5 * (len(with_mem_ms) - 1))
    p95_idx = int(0.95 * (len(with_mem_ms) - 1))
    return {
        "scope": "SIMULATOR_LAB_ONLY",
        "iterations": iterations,
        "memory_mode": mem_mode,
        "control_mode": ctrl_mode,
        "cited_lesson_count": len(mem_items),
        "with_memory_p50_ms": round(with_mem_ms[p50_idx], 3),
        "with_memory_p95_ms": round(with_mem_ms[p95_idx], 3),
        "no_memory_p50_ms": round(no_mem_ms[p50_idx], 3),
        "no_memory_p95_ms": round(no_mem_ms[p95_idx], 3),
        "latency_overhead_p95_ms": round(max(0.0, with_mem_ms[p95_idx] - no_mem_ms[p95_idx]), 3),
        "measured_ai_comparison": False,
        "promotion_eligible": False,
    }


async def retrieve_cited_memory_context(
    session: AsyncSession,
    tenant_id: str,
    target_id: str,
    *,
    limit: int = 4,
    allow_memory: bool = True,
) -> tuple[list[dict], str]:
    """E4-04: bounded cited lower-trust lesson context with explicit no-memory fallback.

    Lessons are strictly lower-trust advisory context; they never authorize effects,
    override fresh probe evidence, or expand tool permissions.
    """
    if not allow_memory:
        return [], "NO_MEMORY_CONTROL"
    try:
        lessons = await retrieve_active_lessons(session, tenant_id, target_id, limit=limit)
    except SQLAlchemyError:
        return [], "MEMORY_FALLBACK_UNAVAILABLE"
    if not lessons:
        return [], "NO_MEMORY"
    cited = [
        {
            "lesson_id": str(lesson.id),
            "rule_id": lesson.rule_id,
            "version": lesson.version,
            "summary_redacted": lesson.summary_redacted,
            "trust_level": "LOWER_TRUST_ADVISORY_ONLY",
            "source_capsule_id": str(lesson.source_capsule_id),
            "source_outcome_id": str(lesson.source_outcome_id),
        }
        for lesson in lessons
    ]
    return cited, "VALIDATED_LESSONS"
