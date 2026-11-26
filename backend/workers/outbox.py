"""Short, fenced claims for the two disjoint durable outbox consumers.

Temporal dispatch and local experience projection have deliberately separate
claim domains. An unfinished memory projection must not block a later
workflow signal; projection itself still waits for prior workflow signals.
"""

from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from backend.models import Incident, OutboxEntry

OutboxDomain = Literal["temporal", "experience"]
Claim = tuple[int, str, str, str, dict | None, int]


async def claim_intent_batch(
    sessions: async_sessionmaker[AsyncSession],
    tenant_filter: str | None = None,
    *,
    domain: OutboxDomain = "temporal",
    limit: int = 10,
) -> list[Claim]:
    """Claim up to `limit` causally ready outbox intents across distinct incidents."""
    if domain not in ("temporal", "experience"):
        raise ValueError("unrecognized outbox consumer domain")
    if not 1 <= limit <= 20:
        raise ValueError("outbox claim batch limit must be between 1 and 20")
    now = datetime.now(timezone.utc)
    start = aliased(OutboxEntry)
    older = aliased(OutboxEntry)
    older_predicates = [
        older.incident_id == OutboxEntry.incident_id,
        older.id < OutboxEntry.id,
        older.delivered_at.is_(None),
        older.dead_lettered_at.is_(None),
    ]
    if domain == "temporal":
        # A consented projection is orthogonal to workflow signal ordering.
        older_predicates.append(older.kind != "PROJECT_EXPERIENCE")
    earlier_pending = (
        select(older.id).where(*older_predicates)
        .correlate(OutboxEntry).exists()
    )
    start_delivered = (
        select(start.id).where(
            start.incident_id == OutboxEntry.incident_id,
            start.kind == "START_WORKFLOW",
            start.delivered_at.is_not(None),
        ).correlate(OutboxEntry).exists()
    )
    kind_guard = (
        OutboxEntry.kind == "PROJECT_EXPERIENCE"
        if domain == "experience"
        else OutboxEntry.kind != "PROJECT_EXPERIENCE"
    )
    async with sessions() as session:
        # The restricted PostgreSQL projector sees only local projection
        # rows. A SECURITY DEFINER boolean probe checks hidden earlier
        # Temporal signals without exposing their payloads or identities.
        if domain == "experience" and session.bind.dialect.name == "postgresql":
            ordering_guard = func.projector_intent_ready(
                OutboxEntry.incident_id, OutboxEntry.id,
            )
        else:
            ordering_guard = and_(
                ~earlier_pending,
                or_(OutboxEntry.kind == "START_WORKFLOW", start_delivered),
            )
        async with session.begin():
            stmt = (
                select(OutboxEntry, Incident.tenant_id)
                .join(Incident, Incident.id == OutboxEntry.incident_id)
                .where(
                    kind_guard,
                    OutboxEntry.delivered_at.is_(None),
                    OutboxEntry.dead_lettered_at.is_(None),
                    OutboxEntry.available_at <= now,
                    ordering_guard,
                )
                .order_by(OutboxEntry.id)
                .limit(min(limit * 4, 50))
                .with_for_update(of=OutboxEntry, skip_locked=True)
            )
            if tenant_filter is not None:
                stmt = stmt.where(Incident.tenant_id == tenant_filter)
            rows = (await session.execute(stmt)).all()
            if not rows:
                return []
            claims: list[Claim] = []
            seen_incidents: set[UUID] = set()
            for entry, tenant_id in rows:
                # Preserve causal per-incident ordering: at most one intent per incident.
                if entry.incident_id in seen_incidents:
                    continue
                seen_incidents.add(entry.incident_id)
                entry.attempt += 1
                entry.available_at = now + timedelta(
                    seconds=min(300, 2 ** min(entry.attempt, 8))
                )
                claims.append((
                    entry.id, tenant_id, str(entry.incident_id),
                    entry.kind, entry.payload, entry.attempt,
                ))
                if len(claims) >= limit:
                    break
            return claims


async def claim_intent(
    sessions: async_sessionmaker[AsyncSession],
    tenant_filter: str | None = None,
    *,
    domain: OutboxDomain = "temporal",
) -> Claim | None:
    batch = await claim_intent_batch(
        sessions, tenant_filter=tenant_filter, domain=domain, limit=1,
    )
    return batch[0] if batch else None


async def acknowledge_claim(
    sessions: async_sessionmaker[AsyncSession],
    claim: Claim,
) -> bool:
    """A stale consumer must not acknowledge a newer claim or terminal item."""
    async with sessions() as session:
        async with session.begin():
            row = await session.scalar(
                select(OutboxEntry).where(
                    OutboxEntry.id == claim[0],
                    OutboxEntry.incident_id == UUID(claim[2]),
                    OutboxEntry.kind == claim[3],
                ).with_for_update()
            )
            if (
                row is None or row.delivered_at is not None
                or row.dead_lettered_at is not None
                or row.attempt != claim[5]
            ):
                return False
            row.delivered_at = datetime.now(timezone.utc)
            return True
