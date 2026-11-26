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
from backend.models import (
    EffectReceipt, ExperienceCapsule, ExperienceConsent, FailureCluster,
    ImprovementCandidate, Incident, IncidentPlan, OutboxEntry, OutcomeRevision,
)
from backend.lifecycle import ACTIVE_STATES, UNRESOLVED_EFFECT_STATES


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
            OutboxEntry.dead_lettered_at.is_(None),
        )
    )
    dead_lettered = select(OutboxEntry.id).join(
        Incident, Incident.id == OutboxEntry.incident_id,
    ).where(
        Incident.tenant_id == tenant_id,
        OutboxEntry.dead_lettered_at.is_not(None),
        OutboxEntry.dead_letter_reviewed_at.is_(None),
        OutboxEntry.kind == "PROJECT_EXPERIENCE",
    )
    active = select(Incident.phase_entered_at).where(
        Incident.tenant_id == tenant_id,
        Incident.status_projection.in_(ACTIVE_STATES),
    )
    unresolved = (
        select(EffectReceipt.updated_at)
        .join(Incident, Incident.id == EffectReceipt.incident_id)
        .where(
            EffectReceipt.tenant_id == tenant_id,
            Incident.tenant_id == tenant_id,
            EffectReceipt.state.in_(UNRESOLVED_EFFECT_STATES),
        )
    )
    pending_approvals_q = (
        select(Incident.phase_entered_at, IncidentPlan.id)
        .join(IncidentPlan, IncidentPlan.incident_id == Incident.id)
        .where(
            Incident.tenant_id == tenant_id,
            IncidentPlan.tenant_id == tenant_id,
            Incident.status_projection == "AWAITING_APPROVAL",
            IncidentPlan.status == "PROPOSED",
        )
    )

    pending_count = int((await session.scalar(
        select(func.count()).select_from(pending.subquery())
    )) or 0)
    needs_review_count = int((await session.scalar(
        select(func.count()).select_from(dead_lettered.subquery())
    )) or 0)
    active_count = int((await session.scalar(
        select(func.count()).select_from(active.subquery())
    )) or 0)
    unresolved_count = int((await session.scalar(
        select(func.count()).select_from(unresolved.subquery())
    )) or 0)
    pending_approval_count = int((await session.scalar(
        select(func.count()).select_from(pending_approvals_q.subquery())
    )) or 0)

    oldest_pending = await session.scalar(
        pending.with_only_columns(func.min(OutboxEntry.created_at))
    )
    oldest_active = await session.scalar(
        active.with_only_columns(func.min(Incident.phase_entered_at))
    )
    oldest_pending_approval = await session.scalar(
        pending_approvals_q.with_only_columns(func.min(Incident.phase_entered_at))
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
    aged_pending_approvals = int((await session.scalar(
        select(func.count()).select_from(
            pending_approvals_q.where(Incident.phase_entered_at < cutoff).subquery()
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
    p95_approval_entered = None
    if pending_approval_count:
        p95_approval_entered = await session.scalar(
            pending_approvals_q.with_only_columns(Incident.phase_entered_at)
            .order_by(Incident.phase_entered_at.desc(), IncidentPlan.id.desc())
            .offset(math.ceil(0.95 * pending_approval_count) - 1).limit(1)
        )

    return {
        "pending_outbox": pending_count,
        "experience_dead_letters_unreviewed": needs_review_count,
        "active_incidents": active_count,
        "unresolved_effects": unresolved_count,
        "aged_unresolved_effects": aged_unresolved,
        "pending_approvals": pending_approval_count,
        "aged_pending_approvals": aged_pending_approvals,
        "oldest_pending_approval_seconds": elapsed_seconds(observed, oldest_pending_approval),
        "pending_approval_age_p95_seconds": elapsed_seconds(observed, p95_approval_entered),
        "oldest_pending_seconds": elapsed_seconds(observed, oldest_pending),
        "oldest_active_phase_seconds": elapsed_seconds(observed, oldest_active),
        "pending_age_p95_seconds": elapsed_seconds(observed, p95_created),
        "aged_pending_outbox": aged_pending,
        "aged_active_incidents": aged_active,
        "max_pending_attempts": int(max_attempt or 0),
        "stuck_after_seconds": stuck_after_seconds,
    }


async def learning_health(
    session: AsyncSession, now: datetime | None = None,
) -> dict:
    """Aggregate-only E1/E2/E3 learning metrics with zero tenant identifiers."""
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    pending_proj_q = select(OutboxEntry.created_at).where(
        OutboxEntry.kind == "PROJECT_EXPERIENCE",
        OutboxEntry.delivered_at.is_(None),
        OutboxEntry.dead_lettered_at.is_(None),
    )
    pending_proj = int((await session.scalar(
        select(func.count()).select_from(pending_proj_q.subquery())
    )) or 0)
    oldest_proj_at = await session.scalar(
        pending_proj_q.with_only_columns(func.min(OutboxEntry.created_at))
    )
    dead_proj = int((await session.scalar(
        select(func.count()).select_from(OutboxEntry).where(
            OutboxEntry.kind == "PROJECT_EXPERIENCE",
            OutboxEntry.dead_lettered_at.is_not(None),
        )
    )) or 0)
    valid_capsules = int((await session.scalar(
        select(func.count()).select_from(ExperienceCapsule).where(
            ExperienceCapsule.status == "VALID",
        )
    )) or 0)
    quarantined_capsules = int((await session.scalar(
        select(func.count()).select_from(ExperienceCapsule).where(
            ExperienceCapsule.status == "QUARANTINED",
        )
    )) or 0)
    denied_consents = int((await session.scalar(
        select(func.count()).select_from(ExperienceConsent).where(
            ExperienceConsent.allowed.is_(False),
        )
    )) or 0)
    expired_q = select(ExperienceCapsule.expires_at).where(
        ExperienceCapsule.expires_at < observed,
    )
    expired_count = int((await session.scalar(
        select(func.count()).select_from(expired_q.subquery())
    )) or 0)
    oldest_expired = await session.scalar(
        expired_q.with_only_columns(func.min(ExperienceCapsule.expires_at))
    )

    has_outcome = select(OutcomeRevision.id).where(
        OutcomeRevision.incident_id == EffectReceipt.incident_id,
    ).correlate(EffectReceipt).exists()
    pending_adj_q = select(EffectReceipt.created_at).where(~has_outcome)
    pending_adj = int((await session.scalar(
        select(func.count()).select_from(pending_adj_q.subquery())
    )) or 0)
    oldest_adj_at = await session.scalar(
        pending_adj_q.with_only_columns(func.min(EffectReceipt.created_at))
    )
    total_outcomes = int((await session.scalar(
        select(func.count()).select_from(OutcomeRevision)
    )) or 0)
    disputed_outcomes = int((await session.scalar(
        select(func.count()).select_from(OutcomeRevision).where(
            OutcomeRevision.label == "DISPUTED",
        )
    )) or 0)
    verified_effect_outcome_lags = (await session.execute(
        select(EffectReceipt.created_at, OutcomeRevision.created_at)
        .join(OutcomeRevision, OutcomeRevision.effect_id == EffectReceipt.id)
        .order_by(OutcomeRevision.created_at.desc())
        .limit(200)
    )).all()
    lag_samples = sorted(
        max(0, int((utc(out_ts) - utc(eff_ts)).total_seconds()))
        for eff_ts, out_ts in verified_effect_outcome_lags
    )
    lag_p95 = (
        lag_samples[min(len(lag_samples) - 1, int(0.95 * (len(lag_samples) - 1)))]
        if lag_samples else 0
    )

    active_clusters = int((await session.scalar(
        select(func.count()).select_from(FailureCluster).where(
            FailureCluster.quarantined.is_(False),
        )
    )) or 0)
    quarantined_clusters = int((await session.scalar(
        select(func.count()).select_from(FailureCluster).where(
            FailureCluster.quarantined.is_(True),
        )
    )) or 0)
    draft_proposals = int((await session.scalar(
        select(func.count()).select_from(ImprovementCandidate).where(
            ImprovementCandidate.state == "DRAFT",
        )
    )) or 0)
    ready_proposals = int((await session.scalar(
        select(func.count()).select_from(ImprovementCandidate).where(
            ImprovementCandidate.state == "EVALUATION_READY",
        )
    )) or 0)
    invalid_proposals = int((await session.scalar(
        select(func.count()).select_from(ImprovementCandidate).where(
            ImprovementCandidate.state == "INVALID",
        )
    )) or 0)

    return {
        "tenant_labels_exported": False,
        "experience": {
            "pending_projections": pending_proj,
            "oldest_projection_lag_seconds": elapsed_seconds(observed, oldest_proj_at),
            "dead_letter_projections": dead_proj,
            "valid_capsules": valid_capsules,
            "quarantined_capsules": quarantined_capsules,
            "denied_consents": denied_consents,
            "expired_unpurged_capsules": expired_count,
            "oldest_expired_purge_age_seconds": elapsed_seconds(observed, oldest_expired),
        },
        "outcomes": {
            "pending_adjudication_count": pending_adj,
            "oldest_pending_adjudication_seconds": elapsed_seconds(observed, oldest_adj_at),
            "total_outcomes": total_outcomes,
            "disputed_outcomes": disputed_outcomes,
            "dispute_rate": round(disputed_outcomes / total_outcomes, 6) if total_outcomes else 0.0,
            "adjudication_lag_p95_seconds": lag_p95,
            "oracle_health": "SIMULATOR_WITNESS_ACTIVE",
        },
        "failures": {
            "active_clusters": active_clusters,
            "quarantined_clusters": quarantined_clusters,
            "draft_proposals": draft_proposals,
            "evaluation_ready_proposals": ready_proposals,
            "invalid_proposals": invalid_proposals,
        },
    }
