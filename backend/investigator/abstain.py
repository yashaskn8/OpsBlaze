
"""A4 deterministic hard gates; risk scores are NOT calibrated probabilities."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.simulator import utc
from backend.models import (
    DecisionAssessment, Evidence, Incident, ProbeCall, SimulatorTarget,
)
from backend.probes.gateway import evidence_links_intact
from backend.settings import get_settings

FEATURE_VERSION = "simulator-evidence-v1"
THRESHOLD_VERSION = "UNCALIBRATED-hard-gates-v1"


async def accepted_snapshot_is_still_safe(
    session: AsyncSession, incident: Incident, tenant_id: str,
    evidence_ids: list[str],
) -> bool:
    """Never replay a stale A4 positive after later probes or source edits."""
    if (
        len(evidence_ids) != 2
        or len(set(evidence_ids)) != 2
        or incident.probe_used > get_settings().max_probe_calls_per_incident
    ):
        return False
    try:
        refs = [UUID(item) for item in evidence_ids]
    except (TypeError, ValueError, AttributeError):
        return False
    if not await evidence_links_intact(session, incident.id, refs):
        return False
    denied = await session.scalar(
        select(ProbeCall.id).where(
            ProbeCall.incident_id == incident.id,
            ProbeCall.outcome == "DENIED",
        ).limit(1)
    )
    if denied is not None:
        return False
    target = await session.get(
        SimulatorTarget, (tenant_id, incident.target_id)
    )
    if target is None or target.healthy:
        return False
    rows = (await session.scalars(
        select(Evidence).where(
            Evidence.tenant_id == tenant_id,
            Evidence.incident_id == incident.id,
            Evidence.id.in_(refs),
        )
    )).all()
    if len(rows) != 2 or {row.source for row in rows} != {
        "simulator.health", "simulator.version",
    }:
        return False
    now = datetime.now(timezone.utc)
    if any(
        row.validity != "VALID"
        or row.target_id != incident.target_id
        or row.target_version != target.version
        or row.source_version != "simulator-v1"
        or row.classification != "INTERNAL"
        or not timedelta(0) <= now - utc(row.observed_at) <= timedelta(minutes=5)
        or hashlib.sha256(row.redacted_excerpt.encode()).hexdigest() != row.sha256
        for row in rows
    ):
        return False
    observed = {row.source: row.redacted_excerpt for row in rows}
    return (
        observed["simulator.health"] == '{"healthy":false}'
        and observed["simulator.version"] == json.dumps(
            {"version": target.version}, sort_keys=True, separators=(",", ":"),
        )
    )


async def assess_decision(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str, incident_id: UUID,
    step_no: int, evidence_ids: list[str],
) -> DecisionAssessment | None:
    if step_no < 1:
        raise ValueError("assessment step must be positive")
    canonical = json.dumps(evidence_ids, separators=(",", ":"), sort_keys=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()

    async with sessions() as session:
        async with session.begin():
            incident = await session.scalar(
                select(Incident).where(
                    Incident.tenant_id == tenant_id,
                    Incident.id == incident_id,
                ).with_for_update()
            )
            if incident is None:
                return None
            existing = await session.scalar(
                select(DecisionAssessment).where(
                    DecisionAssessment.incident_id == incident_id,
                    DecisionAssessment.step_no == step_no,
                )
            )
            if existing is not None:
                # An old positive decision is never reusable for a new snapshot.
                # Keep the durable assessment immutable; the caller must abstain.
                if existing.evidence_digest != digest:
                    return None
                if existing.outcome == "PROPOSED" and not await accepted_snapshot_is_still_safe(
                    session, incident, tenant_id, evidence_ids,
                ):
                    return None
                return existing

            reason = "ACCEPTED"
            if len(evidence_ids) != 2 or len(set(evidence_ids)) != 2:
                reason = "MISSING_EVIDENCE"
            elif incident.probe_used > get_settings().max_probe_calls_per_incident:
                reason = "BUDGET_EXHAUSTED"
            else:
                probes = (await session.scalars(
                    select(ProbeCall).where(
                        ProbeCall.incident_id == incident_id,
                        ProbeCall.outcome == "DENIED",
                    ).limit(1)
                )).first()
                if probes is not None:
                    reason = "TOOL_BLOCKED"
                else:
                    try:
                        refs = [UUID(value) for value in evidence_ids]
                    except (ValueError, TypeError):
                        reason = "INVALID_REFERENCE"
                    else:
                        evidence = (await session.scalars(
                            select(Evidence).where(
                                Evidence.tenant_id == tenant_id,
                                Evidence.incident_id == incident_id,
                                Evidence.id.in_(refs),
                            )
                        )).all()
                        sources = {row.source for row in evidence}
                        if (
                            len(evidence) != 2
                            or sources != {"simulator.health", "simulator.version"}
                            or any(
                                row.validity != "VALID"
                                or row.source_version != "simulator-v1"
                                or row.classification != "INTERNAL"
                                for row in evidence
                            )
                        ):
                            reason = "INVALID_EVIDENCE"
                        elif any(
                            hashlib.sha256(row.redacted_excerpt.encode()).hexdigest()
                            != row.sha256
                            for row in evidence
                        ):
                            reason = "HASH_MISMATCH"
                        elif any(
                            not timedelta(0)
                            <= datetime.now(timezone.utc) - utc(row.observed_at)
                            <= timedelta(minutes=5)
                            for row in evidence
                        ):
                            reason = "STALE_EVIDENCE"
                        elif not await evidence_links_intact(
                            session, incident_id, refs
                        ):
                            reason = "UNLINKED_EVIDENCE"
                        else:
                            target = await session.get(
                                SimulatorTarget, (tenant_id, incident.target_id)
                            )
                            versions = {row.target_version for row in evidence}
                            if (
                                target is None or len(versions) != 1
                                or target.version not in versions
                                or any(row.target_id != incident.target_id for row in evidence)
                            ):
                                reason = "VERSION_CHANGED"
                            else:
                                observations = {
                                    row.source: row.redacted_excerpt for row in evidence
                                }
                                if (
                                    observations["simulator.health"] != '{"healthy":false}'
                                    or observations["simulator.version"] != json.dumps(
                                        {"version": target.version},
                                        sort_keys=True, separators=(",", ":"),
                                    )
                                ):
                                    reason = "INVALID_OBSERVATION"

            assessment = DecisionAssessment(
                id=uuid4(), tenant_id=tenant_id, incident_id=incident_id,
                step_no=step_no, feature_version=FEATURE_VERSION,
                threshold_version=THRESHOLD_VERSION,
                evidence_digest=digest,
                risk_score=0 if reason == "ACCEPTED" else 100,
                reason=reason,
                outcome="PROPOSED" if reason == "ACCEPTED" else "ABSTAINED",
            )
            session.add(assessment)
        return assessment
