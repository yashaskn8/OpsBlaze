
"""E2: append-only independent simulator witness and bounded operator disputes."""

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.simulator import SimulatorAdapter, observed_healthy_at_version
from backend.models import EffectReceipt, Incident, OutboxEntry, OutcomeRevision
from backend.learning.invalidation import invalidate_outcome_dependents
from backend.telemetry import trace_operation


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
        incident_target = await session.scalar(
            select(Incident.target_id).where(
                Incident.id == incident_id, Incident.tenant_id == tenant_id,
            )
        )
        if (
            incident_target is None
            or receipt.target_id != incident_target
            or receipt.adapter_id != "simulator"
            or receipt.attempt_count != 1
            or type(receipt.expected_version) is not int
            or type(receipt.observed_version) is not int
            or receipt.observed_version != receipt.expected_version + 1
        ):
            raise OutcomeConflict("effect receipt is not bound to this incident target")
        target_id = receipt.target_id
        effect_version = receipt.observed_version
        effect_state = receipt.state

    with trace_operation("opsblaze.outcome.observe", incident_id=incident_id):
        observation = await adapter.observe(tenant_id, target_id)
    verified = (
        effect_state == "VERIFIED"
        and observed_healthy_at_version(observation, effect_version)
    )
    label = "VERIFIED" if verified else "DISPUTED"
    observed_version = (
        observation[0]
        if isinstance(observation, tuple)
        and len(observation) == 2
        and type(observation[0]) is int
        else None
    )
    async with sessions() as session:
        async with session.begin():
            # Lock the stable parent before allocating a new outcome revision.
            # PostgreSQL serializes all witnesses and disputes for this incident.
            incident = await session.scalar(
                select(Incident.id)
                .where(Incident.id == incident_id, Incident.tenant_id == tenant_id)
                .with_for_update()
            )
            if incident is None:
                raise OutcomeConflict("incident missing")
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
            if label == "VERIFIED":
                # The verified source revision and its projection intent commit
                # together. A crash before the projector is replay-safe.
                session.add(OutboxEntry(
                    incident_id=incident_id, kind="PROJECT_EXPERIENCE",
                    payload={"outcome_id": str(row.id)},
                ))
            else:
                await invalidate_outcome_dependents(session, tenant_id, incident_id)
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
        # The immutable outcome row itself is not a serialization point:
        # new revisions insert a different row. Lock the shared incident
        # first, then recheck the latest revision under that lock.
        incident = await session.scalar(
            select(Incident.id)
            .where(Incident.id == prior.incident_id, Incident.tenant_id == tenant_id)
            .with_for_update()
        )
        if incident is None:
            raise OutcomeConflict("incident missing")
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
        await invalidate_outcome_dependents(session, tenant_id, prior.incident_id)
    return disputed
