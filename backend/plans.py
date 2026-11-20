
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
from backend.models import ApprovalGrant, ApprovalRevocation, RevokedIdentity, DecisionAssessment, EffectReceipt, Evidence, Incident, IncidentPlan, OutboxEntry, ProbeCall, SimulatorTarget, UIEvent
from backend.outcomes import adjudicate_simulator
from backend.probes.gateway import evidence_links_intact
from backend.settings import get_settings


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
    if not await evidence_links_intact(session, plan.incident_id, refs):
        return False
    # A denied tool call after the original assessment invalidates permission
    # to act; a stale positive assessment cannot hide it.
    denied = await session.scalar(
        select(ProbeCall.id).where(
            ProbeCall.incident_id == plan.incident_id,
            ProbeCall.outcome == "DENIED",
        ).limit(1)
    )
    if denied is not None:
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
    if (
        target is None or target.version != plan.expected_version
        or target.healthy
    ):
        return False
    observed = {row.source: row.redacted_excerpt for row in items}
    # Check the exact typed observation, not only its self-reported hash.
    return (
        observed["simulator.health"] == '{"healthy":false}'
        and observed["simulator.version"]
            == json.dumps({"version": plan.expected_version}, separators=(",", ":"))
    )


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
    decided_by: str = "LOCAL_SERVICE_CALL",
    review_epoch: int | None = None,
    reason: str = "simulator plan reviewed",
) -> IncidentPlan | None:
    if decision not in {"APPROVE", "REJECT"}:
        raise PlanConflict("unsupported decision")
    epoch = get_settings().review_epoch if review_epoch is None else review_epoch
    if epoch < 1:
        raise PlanConflict("review policy epoch must be positive")
    if not 8 <= len(reason.strip()) <= 256:
        raise PlanConflict("review reason must have 8–256 characters")
    ttl = get_settings().approval_ttl_seconds
    if not 1 <= ttl <= 3600:
        raise PlanConflict("approval TTL must be 1–3600 seconds")
    async with session.begin():
        if decided_by.startswith("oidc:"):
            from backend.security.revocation import lock_reviewer_subject

            await lock_reviewer_subject(session, tenant_id, decided_by)
            if await session.get(
                RevokedIdentity, (tenant_id, decided_by)
            ) is not None:
                raise PlanConflict("reviewer identity has been revoked")
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
        if decided_by.startswith("oidc:") and incident.created_by is None:
            raise PlanConflict("legacy incident has no authenticated creator")
        if (
            incident.created_by is not None
            and decided_by == incident.created_by
        ):
            raise PlanConflict("incident creator cannot decide the same plan")
        if decision == "APPROVE" and not await current_plan_evidence_is_intact(
            session, plan
        ):
            raise PlanConflict("plan evidence, assessment, or target version changed")
        plan.status = "APPROVED" if decision == "APPROVE" else "REJECTED"
        plan.version += 1
        decided_at = datetime.now(timezone.utc)
        plan.decided_at = decided_at
        plan.decided_by = decided_by
        plan.review_epoch = epoch
        grant = ApprovalGrant(
            id=uuid5(NAMESPACE_URL, f"opsblaze/approval/{plan.id}"),
            tenant_id=tenant_id, plan_id=plan.id,
            plan_digest=plan.plan_digest, reviewer_id=decided_by,
            scope=plan.action_id,
            decision=decision,
            state="APPROVED" if decision == "APPROVE" else "REJECTED",
            reason=reason.strip(), version=1,
            expires_at=decided_at + timedelta(seconds=ttl),
        )
        session.add(grant)
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



async def revoke_approval(
    session: AsyncSession, tenant_id: str, approval_id: UUID,
    expected_version: int, reviewer_identity: str,
    reason: str,
) -> ApprovalGrant | None:
    if not 8 <= len(reason.strip()) <= 256:
        raise PlanConflict("revocation reason must be 8–256 characters")
    async with session.begin():
        grant = await session.scalar(
            select(ApprovalGrant).where(
                ApprovalGrant.id == approval_id,
                ApprovalGrant.tenant_id == tenant_id,
            ).with_for_update()
        )
        if grant is None:
            return None
        if grant.version != expected_version or grant.state != "APPROVED":
            raise PlanConflict("approval already consumed, revoked or version changed")
        plan = await session.scalar(
            select(IncidentPlan).where(
                IncidentPlan.id == grant.plan_id,
                IncidentPlan.tenant_id == tenant_id,
            ).with_for_update()
        )
        if plan is None or plan.status != "APPROVED":
            raise PlanConflict("approved plan no longer available")
        grant.state = "REVOKED"
        grant.revoked_at = datetime.now(timezone.utc)
        grant.version += 1
        plan.status = "REVOKED"
        plan.version += 1
        # Preserve the original approving actor and use a separate revocation
        # audit record for attributed action, without overwriting the decision.
        session.add(
            ApprovalRevocation(
                id=uuid5(NAMESPACE_URL, f"opsblaze/revocation/{grant.id}"),
                tenant_id=tenant_id,
                approval_id=grant.id,
                revoked_by=reviewer_identity,
                reason=reason.strip(),
            )
        )
    return grant


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
        if existing is None:
            grant = await session.scalar(
                select(ApprovalGrant).where(
                    ApprovalGrant.plan_id == plan_id,
                    ApprovalGrant.tenant_id == tenant_id,
                )
            )
            if (
                grant is None or grant.state != "APPROVED"
                or grant.plan_digest != plan.plan_digest
                or grant.scope != "SIMULATOR_MARK_HEALTHY"
                or utc(grant.expires_at) <= datetime.now(timezone.utc)
                or plan.review_epoch != get_settings().review_epoch
                or not await current_plan_evidence_is_intact(session, plan)
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

    state = await execute_simulator_effect(
        sessions, adapter, permit, plan_id=plan_id,
    )
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
