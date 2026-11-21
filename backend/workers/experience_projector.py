"""E1 idempotent local projector for verified-outcome domain outbox intents."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.experiences import ConsentConflict, capture_capsule
from backend.models import OutcomeRevision


async def project_experience_outbox(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str, incident_id: str, payload: dict | None,
) -> str:
    """Capture eligible source revision only; never touch a real target."""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"outcome_id"}
        or not isinstance(payload["outcome_id"], str)
    ):
        raise ValueError("experience projection intent has invalid payload")
    try:
        outcome_id = UUID(payload["outcome_id"])
        source_id = UUID(incident_id)
    except ValueError as exc:
        raise ValueError("experience projection IDs must be UUIDs") from exc
    if str(outcome_id) != payload["outcome_id"] or str(source_id) != incident_id:
        raise ValueError("experience projection IDs must be canonical")
    async with sessions() as session:
        latest = await session.scalar(
            select(OutcomeRevision).where(
                OutcomeRevision.tenant_id == tenant_id,
                OutcomeRevision.incident_id == source_id,
            ).order_by(OutcomeRevision.revision.desc()).limit(1)
        )
        if latest is None or latest.id != outcome_id or latest.label != "VERIFIED":
            return "SUPERSEDED"
    try:
        async with sessions() as session:
            capsule = await capture_capsule(
                session, tenant_id, source_id,
                expected_outcome_id=outcome_id,
            )
    except ConsentConflict:
        # Missing/revoked consent and subsequently revised evidence are not
        # errors in incident recovery. The source cannot be reused, and
        # an operator can explicitly capture later if newly authorized.
        return "INELIGIBLE"
    if capsule is None:
        return "SUPERSEDED"
    return "CAPTURED"
