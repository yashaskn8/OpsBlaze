"""Typed simulator plan proposal and digest generation (isolated from effect execution)."""

import hashlib
import json
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.lineage_projection import replace_evidence_edges
from backend.models import Incident, IncidentPlan


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
            await session.flush()
            await replace_evidence_edges(session, tenant_id, incident_id, "plan", plan)
        return plan
