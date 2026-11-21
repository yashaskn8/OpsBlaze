"""E1 terminal local projector intents and tenant-scoped review.

A dead letter is a durable failure record, never a delivered message. Only
PROJECT_EXPERIENCE may become terminal here; Temporal recovery signals retain
their existing durable retry semantics. No raw exception is persisted.
"""

from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import Incident, OutboxEntry

MAX_PROJECTOR_ATTEMPTS = 5
DEAD_LETTER_CODES = frozenset({"INVALID_PAYLOAD", "RETRY_EXHAUSTED"})


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
    return {
        "records": [
            {
                "id": row.id,
                "incident_id": str(row.incident_id),
                "attempts": row.attempt,
                "code": row.dead_letter_code,
                "dead_lettered_at": row.dead_lettered_at.isoformat(),
                "reviewed": row.dead_letter_reviewed_at is not None,
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
