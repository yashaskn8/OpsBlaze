"""P1 bounded operator view over authoritative incident records."""

from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.lineage_refs import MAX_EVIDENCE_REFS, bounded_evidence_refs, reference_uuid
from backend.models import DecisionAssessment, EffectReceipt, Evidence, HypothesisStep
from backend.outcomes import recorded_recovery_timing
from backend.schemas import IncidentView
from backend.services import find_incident

STEP_LIMIT = 20
LAB_CAUSES = ("simulated_target_unhealthy", "no_simulated_fault")


async def read_investigation(
    session: AsyncSession, tenant_id: str, incident_id: UUID, probe_limit: int,
) -> dict | None:
    if session.bind.dialect.name == "postgresql":
        # All bounded reads in one operator response see the same durable snapshot.
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    incident = await find_incident(session, tenant_id, incident_id)
    if incident is None:
        return None
    rows = (await session.scalars(select(HypothesisStep).where(
        HypothesisStep.incident_id == incident.id,
    ).order_by(HypothesisStep.step_no.desc()).limit(STEP_LIMIT + 1))).all()
    steps = []
    remaining = MAX_EVIDENCE_REFS
    evidence_ids = set()
    for row in reversed(rows[:STEP_LIMIT]):
        candidates, truncated, valid = bounded_evidence_refs(row.evidence_ids, remaining)
        remaining -= len(candidates)
        parsed = [reference_uuid(item) for item in candidates]
        refs = list(dict.fromkeys(item for item in parsed if item is not None))
        evidence_ids.update(refs)
        scores = row.cause_scores if isinstance(row.cause_scores, dict) else {}
        steps.append({
            "step_no": row.step_no, "decision": row.decision, "probe_id": row.probe_id,
            "hypotheses": [name for name in LAB_CAUSES if name in scores],
            "evidence_ids": refs, "incomplete_evidence": truncated or not valid or None in parsed,
        })
    evidence = {}
    if evidence_ids:
        evidence = {
            row.id: {"id": str(row.id), "source": row.source, "validity": row.validity,
                     "redacted_excerpt": row.redacted_excerpt}
            for row in (await session.scalars(select(Evidence).where(
                Evidence.incident_id == incident.id, Evidence.tenant_id == tenant_id,
                Evidence.id.in_(evidence_ids),
            ))).all()
        }
    for step in steps:
        refs = step.pop("evidence_ids")
        step["evidence"] = [evidence[item] for item in refs if item in evidence]
        step["incomplete_evidence"] |= any(item not in evidence for item in refs)

    assessment = (await session.execute(select(
        DecisionAssessment.reason, DecisionAssessment.outcome, DecisionAssessment.step_no,
    ).where(DecisionAssessment.incident_id == incident.id, DecisionAssessment.tenant_id == tenant_id)
        .order_by(DecisionAssessment.step_no.desc()).limit(1))).one_or_none()
    latest_effect = await session.scalar(select(EffectReceipt.state).where(
        EffectReceipt.incident_id == incident.id, EffectReceipt.tenant_id == tenant_id,
    ).order_by(EffectReceipt.created_at.desc(), EffectReceipt.id.desc()).limit(1))
    recovery = await recorded_recovery_timing(session, tenant_id, incident.id)
    return {
        "incident": IncidentView.model_validate(incident),
        "probe_budget": {"used": incident.probe_used, "limit": probe_limit,
                         "remaining": max(0, probe_limit - incident.probe_used)},
        "steps": steps,
        "steps_truncated": len(rows) > STEP_LIMIT,
        "latest_assessment": dict(assessment._mapping) if assessment else None,
        "latest_effect_state": latest_effect,
        "recovery": recovery,
    }
