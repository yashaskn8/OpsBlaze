"""E1 terminal local projector intents and tenant-scoped review.

A dead letter is a durable failure record, never a delivered message. Only
PROJECT_EXPERIENCE may become terminal here; Temporal recovery signals retain
their existing durable retry semantics. No raw exception is persisted.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.simulator import utc
from backend.experiences import lock_tenant_consent
from backend.models import (
    ExperienceCapsule, ExperienceConsent, Incident, OutboxEntry,
    OutcomeRevision, TenantExperienceConsent,
)
from backend.settings import get_settings

MAX_PROJECTOR_ATTEMPTS = 5
DEAD_LETTER_CODES = frozenset({"INVALID_PAYLOAD", "RETRY_EXHAUSTED"})
REDRIVE_REASONS = frozenset({"STORE_RESTORED", "TRANSIENT_FIX_VERIFIED"})


class DeadLetterConflict(Exception):
    pass


async def mark_projection_dead_letter(
    sessions: async_sessionmaker[AsyncSession],
    entry_id: int, tenant_id: str, attempt: int, code: str,
) -> bool:
    """Terminalize only the exact still-current claim; stale workers lose."""
    if code not in DEAD_LETTER_CODES:
        raise ValueError("unsupported dead-letter code")
    async with sessions() as session:
        async with session.begin():
            row = await session.scalar(
                select(OutboxEntry)
                .join(Incident, Incident.id == OutboxEntry.incident_id)
                .where(
                    OutboxEntry.id == entry_id,
                    Incident.tenant_id == tenant_id,
                    OutboxEntry.kind == "PROJECT_EXPERIENCE",
                )
                .with_for_update(of=OutboxEntry)
                .execution_options(populate_existing=True)
            )
            if (
                row is None or row.delivered_at is not None
                or row.dead_lettered_at is not None
                or row.attempt != attempt
            ):
                return False
            row.dead_lettered_at = datetime.now(timezone.utc)
            row.dead_letter_code = code
            return True


async def list_projection_dead_letters(
    session: AsyncSession, tenant_id: str,
    cursor: int = 0, limit: int = 50,
) -> dict:
    if not 0 <= cursor <= (1 << 63) - 1:
        raise ValueError("unsupported dead-letter cursor")
    if not 1 <= limit <= 50:
        raise ValueError("dead-letter page size must be 1–50")
    rows = list((await session.scalars(
        select(OutboxEntry)
        .join(Incident, Incident.id == OutboxEntry.incident_id)
        .where(
            Incident.tenant_id == tenant_id,
            OutboxEntry.kind == "PROJECT_EXPERIENCE",
            OutboxEntry.dead_lettered_at.is_not(None),
            OutboxEntry.id > cursor,
        )
        .order_by(OutboxEntry.id)
        .limit(limit + 1)
    )).all())
    visible = rows[:limit]
    redrives = {}
    if visible:
        children = (await session.execute(
            select(OutboxEntry.redrive_source_id, OutboxEntry.id)
            .where(
                OutboxEntry.redrive_source_id.in_([row.id for row in visible]),
                OutboxEntry.kind == "PROJECT_EXPERIENCE",
            )
        )).all()
        redrives = {source_id: child_id for source_id, child_id in children}
    return {
        "records": [
            {
                "id": row.id,
                "incident_id": str(row.incident_id),
                "attempts": row.attempt,
                "code": row.dead_letter_code,
                "dead_lettered_at": row.dead_lettered_at.isoformat(),
                "reviewed": row.dead_letter_reviewed_at is not None,
                "redriven_to": redrives.get(row.id),
            }
            for row in visible
        ],
        "next_cursor": visible[-1].id if len(rows) > limit else None,
        "metadata_only": True,
    }


async def acknowledge_projection_dead_letter(
    session: AsyncSession, tenant_id: str,
    entry_id: int, expected_attempts: int, operator_id: str,
) -> OutboxEntry | None:
    """CAS review acknowledgment: no requeue, no external side effect."""
    if not 1 <= expected_attempts <= (1 << 31) - 1:
        raise DeadLetterConflict("invalid expected attempt")
    async with session.begin():
        row = await session.scalar(
            select(OutboxEntry)
            .join(Incident, Incident.id == OutboxEntry.incident_id)
            .where(
                OutboxEntry.id == entry_id,
                Incident.tenant_id == tenant_id,
                OutboxEntry.kind == "PROJECT_EXPERIENCE",
                OutboxEntry.dead_lettered_at.is_not(None),
            )
            .with_for_update(of=OutboxEntry)
            .execution_options(populate_existing=True)
        )
        if row is None:
            return None
        if row.dead_letter_reviewed_at is not None:
            raise DeadLetterConflict("dead letter already reviewed")
        if row.attempt != expected_attempts:
            raise DeadLetterConflict("dead-letter attempt version changed")
        if not operator_id or len(operator_id) > 48:
            raise DeadLetterConflict("invalid operator identity")
        row.dead_letter_reviewed_at = datetime.now(timezone.utc)
        row.dead_letter_reviewed_by = operator_id
    return row


async def redrive_projection_dead_letter(
    session: AsyncSession, tenant_id: str,
    entry_id: int, expected_attempts: int,
    administrator_id: str, reason: str,
) -> OutboxEntry | None:
    """Create at most one *new* projection intent under independent approval.

    Original terminal records remain immutable. This cannot re-run real
    effects or reconstruct a source whose consent/outcome has changed.
    """
    if reason not in REDRIVE_REASONS:
        raise DeadLetterConflict("unsupported remediation reason")
    if not 1 <= expected_attempts <= (1 << 31) - 1:
        raise DeadLetterConflict("invalid expected delivery attempt")
    if not administrator_id or len(administrator_id) > 48:
        raise DeadLetterConflict("invalid administrator identity")
    now = datetime.now(timezone.utc)
    retention = get_settings().experience_retention_days
    if not 1 <= retention <= 30:
        raise DeadLetterConflict("invalid experience retention policy")

    async with session.begin():
        # Same lock order as capsule capture: tenant consent -> incident.
        # This serializes policy edits and outcome corrections with redrive.
        await lock_tenant_consent(session, tenant_id)
        incident = await session.scalar(
            select(Incident)
            .join(OutboxEntry, OutboxEntry.incident_id == Incident.id)
            .where(
                Incident.tenant_id == tenant_id,
                OutboxEntry.id == entry_id,
                OutboxEntry.kind == "PROJECT_EXPERIENCE",
            )
            .with_for_update(of=Incident)
        )
        if incident is None:
            return None
        source = await session.scalar(
            select(OutboxEntry).where(
                OutboxEntry.id == entry_id,
                OutboxEntry.incident_id == incident.id,
                OutboxEntry.kind == "PROJECT_EXPERIENCE",
            ).with_for_update().execution_options(populate_existing=True)
        )
        if source is None:
            return None
        if (
            source.dead_letter_code != "RETRY_EXHAUSTED"
            or source.dead_lettered_at is None
            or source.delivered_at is not None
            or source.redrive_source_id is not None
            or source.dead_letter_reviewed_at is None
            or not source.dead_letter_reviewed_by
        ):
            raise DeadLetterConflict("only reviewed, exhausted original projections can redrive")
        if source.attempt != expected_attempts:
            raise DeadLetterConflict("dead-letter attempt version changed")
        if source.dead_letter_reviewed_by == administrator_id:
            raise DeadLetterConflict("redrive requires an independent authorizer")
        if utc(source.created_at) < now - timedelta(days=retention):
            raise DeadLetterConflict("original projection source is outside retention")
        duplicate = await session.scalar(
            select(OutboxEntry.id).where(
                OutboxEntry.redrive_source_id == entry_id
            )
        )
        if duplicate is not None:
            raise DeadLetterConflict("projection already redriven")

        payload = source.payload
        if (
            not isinstance(payload, dict)
            or set(payload) != {"outcome_id"}
            or not isinstance(payload["outcome_id"], str)
        ):
            raise DeadLetterConflict("original projection payload cannot be redriven")
        try:
            outcome_id = UUID(payload["outcome_id"])
        except ValueError as exc:
            raise DeadLetterConflict("original projection source is invalid") from exc
        if str(outcome_id) != payload["outcome_id"]:
            raise DeadLetterConflict("original projection source is not canonical")
        latest = await session.scalar(
            select(OutcomeRevision).where(
                OutcomeRevision.tenant_id == tenant_id,
                OutcomeRevision.incident_id == incident.id,
            ).order_by(OutcomeRevision.revision.desc()).limit(1)
        )
        if latest is None or latest.id != outcome_id or latest.label != "VERIFIED":
            raise DeadLetterConflict("source outcome is no longer independently verified")
        consent = await session.get(
            ExperienceConsent, incident.id, with_for_update=True,
        )
        if (
            consent is None or consent.tenant_id != tenant_id
            or not consent.allowed
            or utc(consent.decided_at) > utc(source.created_at)
        ):
            raise DeadLetterConflict("incident reuse consent changed after original projection")
        policy = await session.get(
            TenantExperienceConsent, tenant_id, with_for_update=True,
        )
        if policy is not None and (
            not policy.allowed
            or utc(policy.decided_at) > utc(source.created_at)
        ):
            raise DeadLetterConflict("tenant reuse policy changed after original projection")
        existing = await session.scalar(
            select(ExperienceCapsule.id).where(
                ExperienceCapsule.incident_id == incident.id,
                ExperienceCapsule.outcome_revision_id == outcome_id,
            )
        )
        if existing is not None:
            raise DeadLetterConflict("source already has an experience capsule")

        fresh = OutboxEntry(
            incident_id=incident.id, kind="PROJECT_EXPERIENCE",
            payload={"outcome_id": str(outcome_id)},
            redrive_source_id=source.id,
            redrive_authorized_by=administrator_id,
            redrive_reason=reason,
        )
        session.add(fresh)
        await session.flush()
        return fresh
