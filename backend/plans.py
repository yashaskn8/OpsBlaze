
"""Typed simulator plan proposal and persisted operator decision."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.simulator import SimulatorAdapter, utc
from backend.adapters.conformance import current_simulator_is_conformant
from backend.effects.gateway import execute_simulator_effect, reconcile_effect
from backend.effects.reservations import ReservationBusy, reserve_simulator_target
from backend.models import DecisionAssessment, EffectReceipt, Evidence, Incident, IncidentPlan, OutboxEntry, SimulatorTarget, UIEvent
from backend.outcomes import adjudicate_simulator


class PlanConflict(Exception):
    pass


def digest_plan(
    tenant_id: str, incident_id: UUID, target_id: str,
    expected_version: int, evidence_ids: list[str],
) -> str:
    doc = {
        "action": "SIMULATOR_MARK_HEALTHY", "tenant": tenant_id,
        "incident_id": str(incident_id), "target": target_id,
        "version": expected_version, "evidence_ids": evidence_ids,
    }
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def current_plan_evidence_is_intact(
    session: AsyncSession, plan: IncidentPlan,
) -> bool:
    """Recheck durable evidence and assessment before approval and first effect."""
    ids = plan.evidence_ids
    if not isinstance(ids, list) or len(ids) != 2 or len(set(ids)) != 2:
        return False
    try:
        refs = [UUID(value) for value in ids]
    except (ValueError, TypeError, AttributeError):
        return False

    expected_digest = digest_plan(
        plan.tenant_id, plan.incident_id, plan.target_id,
        plan.expected_version, ids,
    )
    if plan.plan_digest != expected_digest:
        return False
    evidence_snapshot = hashlib.sha256(
        json.dumps(ids, separators=(",", ":"), sort_keys=False).encode()
    ).hexdigest()
    assessment = await session.scalar(
        select(DecisionAssessment).where(
            DecisionAssessment.tenant_id == plan.tenant_id,
            DecisionAssessment.incident_id == plan.incident_id,
            DecisionAssessment.step_no == 2,
        )
    )
    if (
        assessment is None or assessment.outcome != "PROPOSED"
        or assessment.reason != "ACCEPTED"
        or assessment.evidence_digest != evidence_snapshot
    ):
        return False
    items = (await session.scalars(
        select(Evidence).where(
            Evidence.id.in_(refs),
            Evidence.tenant_id == plan.tenant_id,
            Evidence.incident_id == plan.incident_id,
        )
    )).all()
    if len(items) != 2:
        return False
    now = datetime.now(timezone.utc)
    if (
        {row.source for row in items} != {"simulator.health", "simulator.version"}
        or any(
            row.validity != "VALID"
            or row.target_id != plan.target_id
            or row.target_version != plan.expected_version
            or not timedelta(0) <= now - utc(row.observed_at) <= timedelta(minutes=5)
            or hashlib.sha256(row.redacted_excerpt.encode()).hexdigest() != row.sha256
            for row in items
        )
    ):
        return False
    target = await session.get(
        SimulatorTarget, (plan.tenant_id, plan.target_id)
    )
    return target is not None and target.version == plan.expected_version


async def propose_simulator_plan(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str, incident_id: UUID, expected_version: int,
    evidence_ids: list[str],
) -> IncidentPlan:
    async with sessions() as session:
        async with session.begin():
            incident = await session.scalar(
                select(Incident).where(
                    Incident.id == incident_id, Incident.tenant_id == tenant_id
                )
            )
            if incident is None:
                raise PlanConflict("incident not found")
            digest = digest_plan(
                tenant_id, incident_id, incident.target_id,
                expected_version, evidence_ids,
            )
            existing = await session.scalar(
                select(IncidentPlan).where(IncidentPlan.incident_id == incident_id)
            )
            if existing is not None:
                if existing.plan_digest != digest:
                    raise PlanConflict("incident already has a different plan")
                return existing
            plan = IncidentPlan(
                id=uuid5(NAMESPACE_URL, f"opsblaze/plan/{incident_id}"),
                tenant_id=tenant_id, incident_id=incident_id,
                target_id=incident.target_id, action_id="SIMULATOR_MARK_HEALTHY",
                plan_digest=digest, expected_version=expected_version,
                evidence_ids=evidence_ids, status="PROPOSED", version=1,
            )
            session.add(plan)
        return plan


async def decide_plan(
    session: AsyncSession, tenant_id: str, plan_id: UUID,
    expected_version: int, decision: str,
) -> IncidentPlan | None:
    if decision not in {"APPROVE", "REJECT"}:
        raise PlanConflict("unsupported decision")
    async with session.begin():
        plan = await session.scalar(
            select(IncidentPlan)
            .where(IncidentPlan.id == plan_id, IncidentPlan.tenant_id == tenant_id)
            .with_for_update()
        )
        if plan is None:
            return None
        incident = await session.scalar(
            select(Incident)
            .where(Incident.id == plan.incident_id, Incident.tenant_id == tenant_id)
            .with_for_update()
        )
        if (
            plan.version != expected_version or plan.status != "PROPOSED"
            or incident is None or incident.status_projection != "AWAITING_APPROVAL"
        ):
            raise PlanConflict("plan or incident version/state changed")
        if decision == "APPROVE" and not await current_plan_evidence_is_intact(
            session, plan
        ):
            raise PlanConflict("plan evidence, assessment, or target version changed")
        plan.status = "APPROVED" if decision == "APPROVE" else "REJECTED"
        plan.version += 1
        plan.decided_at = datetime.now(timezone.utc)
        incident.status_projection = "PLAN_DECIDED"
        incident.status_version += 1
        incident.phase_entered_at = datetime.now(timezone.utc)
        session.add(
            UIEvent(
                tenant_id=tenant_id, incident_id=incident.id,
                kind="PLAN_DECIDED", projection_version=incident.status_version,
            )
        )
        session.add(
            OutboxEntry(
                incident_id=incident.id, kind="PLAN_DECIDED",
                payload={"plan_id": str(plan.id), "approved": decision == "APPROVE"},
            )
        )
    return plan


async def execute_approved_simulator_plan(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str, incident_id: UUID, plan_id: UUID,
) -> str:
    """Internal action gateway. Only the isolated simulator is supported."""
    async with sessions() as session:
        plan = await session.scalar(
            select(IncidentPlan).where(
                IncidentPlan.id == plan_id, IncidentPlan.tenant_id == tenant_id,
                IncidentPlan.incident_id == incident_id,
            )
        )
        if (
            plan is None or plan.status != "APPROVED"
            or plan.action_id != "SIMULATOR_MARK_HEALTHY"
            or plan.version != 2
        ):
            return "NEEDS_REVIEW"
        digest, target_id, target_version = (
            plan.plan_digest, plan.target_id, plan.expected_version
        )
        existing = await session.scalar(
            select(EffectReceipt).where(
                EffectReceipt.tenant_id == tenant_id,
                EffectReceipt.incident_id == incident_id,
                EffectReceipt.plan_digest == digest,
            )
        )
        if existing is None and not await current_plan_evidence_is_intact(
            session, plan
        ):
            return "NEEDS_REVIEW"
    adapter = SimulatorAdapter(sessions)
    if existing is not None:
        state = await reconcile_effect(sessions, adapter, existing.id)
        return (await adjudicate_simulator(sessions, adapter, tenant_id, incident_id, existing.id)) if state == "VERIFIED" else state
    async with sessions() as session:
        if not await current_simulator_is_conformant(session):
            return "NEEDS_REVIEW"
    try:
        permit = await reserve_simulator_target(
            sessions, tenant_id, target_id, incident_id, digest
        )
    except (ReservationBusy, ValueError):
        return "NEEDS_REVIEW"
    if permit.expected_version != target_version:
        return "NEEDS_REVIEW"

    state = await execute_simulator_effect(sessions, adapter, permit)
    if state != "VERIFIED":
        return state
    async with sessions() as session:
        receipt = await session.scalar(
            select(EffectReceipt).where(
                EffectReceipt.tenant_id == tenant_id,
                EffectReceipt.incident_id == incident_id,
                EffectReceipt.plan_digest == digest,
            )
        )
    return await adjudicate_simulator(
        sessions, adapter, tenant_id, incident_id, receipt.id
    )
