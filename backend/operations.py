"""B4: exact tenant-scoped queue-age and retry-pressure snapshots.

All counts and percentile positions come from durable PostgreSQL/SQLite rows.
No unbounded row list is moved into API memory and no tenant identifier is
exported as a metric label.
"""

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.adapters.simulator import utc
from backend.models import EffectReceipt, Incident, OutboxEntry
from backend.services import ACTIVE_STATES
from backend.workers.state_age import UNRESOLVED_EFFECT_STATES


def elapsed_seconds(now: datetime, then: datetime | None) -> int:
    if then is None:
        return 0
    return max(0, int((now - utc(then)).total_seconds()))


async def queue_health(
    session: AsyncSession, tenant_id: str, stuck_after_seconds: int,
    now: datetime | None = None,
) -> dict[str, int]:
    """Report durable age distribution, not a claim of worker health or HA."""
    if stuck_after_seconds < 1:
        raise ValueError("stuck threshold must be positive")
    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise ValueError("observation time must have a timezone")
    observed = observed.astimezone(timezone.utc)
    cutoff = observed - timedelta(seconds=stuck_after_seconds)

    pending = (
        select(OutboxEntry.created_at, OutboxEntry.attempt)
        .join(Incident, Incident.id == OutboxEntry.incident_id)
        .where(
            Incident.tenant_id == tenant_id,
            OutboxEntry.delivered_at.is_(None),
        )
    )
    active = select(Incident.phase_entered_at).where(
        Incident.tenant_id == tenant_id,
        Incident.status_projection.in_(ACTIVE_STATES),
    )
    unresolved = select(EffectReceipt.updated_at).where(
        EffectReceipt.tenant_id == tenant_id,
        EffectReceipt.state.in_(UNRESOLVED_EFFECT_STATES),
    )

    pending_count = int((await session.scalar(
        select(func.count()).select_from(pending.subquery())
    )) or 0)
    active_count = int((await session.scalar(
        select(func.count()).select_from(active.subquery())
    )) or 0)
    unresolved_count = int((await session.scalar(
        select(func.count()).select_from(unresolved.subquery())
    )) or 0)

    oldest_pending = await session.scalar(
        pending.with_only_columns(func.min(OutboxEntry.created_at))
    )
    oldest_active = await session.scalar(
        active.with_only_columns(func.min(Incident.phase_entered_at))
    )
    aged_pending = int((await session.scalar(
        select(func.count()).select_from(
            pending.where(OutboxEntry.created_at < cutoff).subquery()
        )
    )) or 0)
    aged_active = int((await session.scalar(
        select(func.count()).select_from(
            active.where(Incident.phase_entered_at < cutoff).subquery()
        )
    )) or 0)
    aged_unresolved = int((await session.scalar(
        select(func.count()).select_from(
            unresolved.where(EffectReceipt.updated_at < cutoff).subquery()
        )
    )) or 0)
    max_attempt = await session.scalar(
        pending.with_only_columns(func.max(OutboxEntry.attempt))
    )

    # Sorting by newest-first sorts elapsed ages from small to large.
    # Fetch just the exact nearest-rank 95th percentile row. This avoids
    # claiming an approximate percentile from a silently truncated sample.
    p95_created = None
    if pending_count:
        p95_created = await session.scalar(
            pending.with_only_columns(OutboxEntry.created_at)
            .order_by(OutboxEntry.created_at.desc(), OutboxEntry.id.desc())
            .offset(math.ceil(0.95 * pending_count) - 1).limit(1)
        )

    return {
        "pending_outbox": pending_count,
        "active_incidents": active_count,
        "unresolved_effects": unresolved_count,
        "aged_unresolved_effects": aged_unresolved,
        "oldest_pending_seconds": elapsed_seconds(observed, oldest_pending),
        "oldest_active_phase_seconds": elapsed_seconds(observed, oldest_active),
        "pending_age_p95_seconds": elapsed_seconds(observed, p95_created),
        "aged_pending_outbox": aged_pending,
        "aged_active_incidents": aged_active,
        "max_pending_attempts": int(max_attempt or 0),
        "stuck_after_seconds": stuck_after_seconds,
    }
