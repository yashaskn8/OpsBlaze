
"""E2: append-only independent simulator witness and bounded operator disputes."""

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.simulator import SimulatorAdapter
from backend.models import EffectReceipt, OutcomeRevision


class OutcomeConflict(Exception):
    pass


async def latest_outcome(session: AsyncSession, tenant_id: str, incident_id: UUID):
    return await session.scalar(
        select(OutcomeRevision)
        .where(
            OutcomeRevision.tenant_id == tenant_id,
            OutcomeRevision.incident_id == incident_id,
        )
        .order_by(OutcomeRevision.revision.desc())
        .limit(1)
    )


async def adjudicate_simulator(
    sessions: async_sessionmaker[AsyncSession],
    adapter: SimulatorAdapter,
    tenant_id: str, incident_id: UUID, effect_id: UUID,
) -> str:
    """A second read-only observation; a receipt by itself is not an oracle."""
    async with sessions() as session:
        receipt = await session.scalar(
            select(EffectReceipt).where(
                EffectReceipt.id == effect_id, EffectReceipt.tenant_id == tenant_id,
                EffectReceipt.incident_id == incident_id,
            )
        )
        if receipt is None:
            raise OutcomeConflict("effect receipt missing")
        target_id = receipt.target_id
        effect_version = receipt.observed_version
        effect_state = receipt.state

    observation = await adapter.observe(tenant_id, target_id)
    verified = (
        effect_state == "VERIFIED" and observation is not None
        and observation == (effect_version, True)
    )
    label = "VERIFIED" if verified else "DISPUTED"
    observed_version = None if observation is None else observation[0]
    async with sessions() as session:
        async with session.begin():
            previous = await latest_outcome(session, tenant_id, incident_id)
            if previous is not None:
                if previous.witness_source == "OPERATOR_DISPUTE":
                    return "DISPUTED"
                if previous.label == label and previous.observed_version == observed_version:
                    return previous.label
            row = OutcomeRevision(
                id=uuid4(), tenant_id=tenant_id, incident_id=incident_id,
                effect_id=effect_id, revision=1 if previous is None else previous.revision + 1,
                label=label, witness_source="SIMULATOR_WITNESS",
                observed_version=observed_version,
                reason="simulator postcondition observed" if verified else "simulator postcondition mismatch",
            )
            session.add(row)
    return label


async def dispute_outcome(
    session: AsyncSession, tenant_id: str, outcome_id: UUID,
    expected_revision: int, reason: str,
) -> OutcomeRevision | None:
    if len(reason.strip()) < 8 or len(reason) > 256:
        raise OutcomeConflict("reason must contain 8 to 256 characters")
    async with session.begin():
        prior = await session.scalar(
            select(OutcomeRevision).where(
                OutcomeRevision.id == outcome_id, OutcomeRevision.tenant_id == tenant_id
            )
        )
        if prior is None:
            return None
        latest = await latest_outcome(session, tenant_id, prior.incident_id)
        if (
            latest is None or latest.id != prior.id
            or latest.revision != expected_revision
        ):
            raise OutcomeConflict("outcome was superseded; refetch before disputing")
        disputed = OutcomeRevision(
            id=uuid4(), tenant_id=tenant_id, incident_id=prior.incident_id,
            effect_id=prior.effect_id, revision=prior.revision + 1,
            label="DISPUTED", witness_source="OPERATOR_DISPUTE",
            observed_version=prior.observed_version, reason=reason.strip(),
        )
        session.add(disputed)
    return disputed
