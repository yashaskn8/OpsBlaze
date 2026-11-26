
"""E2: append-only independent simulator witness and bounded operator disputes."""

from uuid import UUID, uuid4

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.simulator import SimulatorAdapter, observed_healthy_at_version, utc
from backend.models import EffectReceipt, Incident, OutboxEntry, OutcomeRevision, UIEvent
from backend.learning.invalidation import invalidate_outcome_dependents
from backend.telemetry import trace_operation
from backend.lineage_projection import replace_record_edges
from backend.trace_identity import recorded_span_ids
from backend.trace_refs import admission_parent, store_trace_ref
from backend.settings import get_settings
from backend.workflows.activity_timing import schedule_to_start_ms


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


async def recorded_recovery_timing(
    session: AsyncSession, tenant_id: str, incident_id: UUID,
    *, include_outcome_source: bool = False,
) -> dict | None:
    """Read the latest scoped outcome; a projection or receipt is not a witness."""
    row = (await session.execute(
        select(
            Incident.created_at, OutcomeRevision.revision, OutcomeRevision.label,
            OutcomeRevision.witness_source, OutcomeRevision.created_at,
            OutcomeRevision.id, OutcomeRevision.observed_version,
        )
        .outerjoin(OutcomeRevision, and_(
            OutcomeRevision.incident_id == Incident.id,
            OutcomeRevision.tenant_id == Incident.tenant_id,
        ))
        .where(Incident.id == incident_id, Incident.tenant_id == tenant_id)
        .order_by(OutcomeRevision.revision.desc()).limit(1)
    )).one_or_none()
    if row is None:
        return None
    admitted, revision, label, witness, recorded, outcome_id, observed_version = row
    duration = None
    if label == "VERIFIED" and witness == "SIMULATOR_WITNESS" and recorded is not None:
        # Database timestamps are UTC, including naive SQLite test timestamps.
        duration = schedule_to_start_ms(utc(admitted), utc(recorded))
    report = {
        "scope": "SIMULATOR_LEDGER_ONLY",
        "latest_outcome_revision": revision,
        "latest_outcome_label": label,
        "admission_to_recorded_witness_ms": duration,
        "production_recovery_certified": False,
    }
    if include_outcome_source:
        report.update(
            latest_outcome_id=str(outcome_id) if outcome_id else None,
            latest_outcome_witness_source=witness,
            latest_outcome_recorded_at=utc(recorded).isoformat() if recorded else None,
            latest_outcome_observed_version=observed_version,
        )
    return report


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

    parent = await admission_parent(
        sessions, tenant_id, incident_id, enabled=bool(get_settings().otel_traces_endpoint),
    )
    with trace_operation("opsblaze.outcome.observe", incident_id=incident_id, parent=parent) as span:
        observation = await adapter.observe(tenant_id, target_id)
        span_ids = recorded_span_ids(span)
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
            projection_version = await session.scalar(
                select(Incident.status_version)
                .where(Incident.id == incident_id, Incident.tenant_id == tenant_id)
                .with_for_update()
            )
            if projection_version is None:
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
            session.add(UIEvent(
                tenant_id=tenant_id, incident_id=incident_id,
                kind="OUTCOME_RECORDED", projection_version=projection_version,
            ))
            if label == "VERIFIED":
                # The verified source revision and its projection intent commit
                # together. A crash before the projector is replay-safe.
                session.add(OutboxEntry(
                    incident_id=incident_id, kind="PROJECT_EXPERIENCE",
                    payload={"outcome_id": str(row.id)},
                ))
            else:
                await invalidate_outcome_dependents(session, tenant_id, incident_id)
            await session.flush()
            await replace_record_edges(session, tenant_id, incident_id, "outcome", row)
            if span_ids is not None:
                await store_trace_ref(session, tenant_id, incident_id, "outcome", row.id, span_ids)
    return label


async def dispute_outcome(
    session: AsyncSession, tenant_id: str, outcome_id: UUID,
    expected_revision: int, reason: str, *, reviewer_id: str,
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
        projection_version = await session.scalar(
            select(Incident.status_version)
            .where(Incident.id == prior.incident_id, Incident.tenant_id == tenant_id)
            .with_for_update()
        )
        if projection_version is None:
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
            reviewer_id=reviewer_id,
        )
        session.add(disputed)
        session.add(UIEvent(
            tenant_id=tenant_id, incident_id=prior.incident_id,
            kind="OUTCOME_DISPUTED", projection_version=projection_version,
        ))
        await session.flush()
        await replace_record_edges(session, tenant_id, prior.incident_id, "outcome", disputed)
        await invalidate_outcome_dependents(session, tenant_id, prior.incident_id)
    return disputed
