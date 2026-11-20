
"""E7 pinned deterministic lab route; never increases tool capabilities."""

import hashlib
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import Incident, RouteDecision, SimulatorTarget
from backend.settings import get_settings

ROUTER_POLICY_ID = "simulator-lab-route-v1"


async def select_route(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str, incident_id: UUID,
) -> RouteDecision | None:
    async with sessions() as session:
        async with session.begin():
            incident = await session.scalar(
                select(Incident).where(
                    Incident.id == incident_id,
                    Incident.tenant_id == tenant_id,
                ).with_for_update()
            )
            if incident is None:
                return None
            existing = await session.scalar(
                select(RouteDecision).where(
                    RouteDecision.tenant_id == tenant_id,
                    RouteDecision.incident_id == incident_id,
                    RouteDecision.decision_no == 1,
                )
            )
            if existing is not None:
                return existing

            target = await session.get(
                SimulatorTarget, (tenant_id, incident.target_id)
            )
            budget = get_settings().max_probe_calls_per_incident
            if target is None:
                route, reason = "HANDOFF", "SIMULATOR_TARGET_MISSING"
                cost_cap = 0
            elif budget < 2:
                route, reason = "HANDOFF", "PROBE_BUDGET_INSUFFICIENT"
                cost_cap = 0
            else:
                route, reason = "RUNBOOK", "APPROVED_SIMULATOR_BASELINE"
                cost_cap = 2

            digest = hashlib.sha256(
                f"{tenant_id}/{incident.target_id}/{ROUTER_POLICY_ID}".encode()
            ).hexdigest()
            selected = RouteDecision(
                id=uuid5(NAMESPACE_URL, f"opsblaze/route/{tenant_id}/{incident_id}/1"),
                tenant_id=tenant_id, incident_id=incident_id, decision_no=1,
                policy_id=ROUTER_POLICY_ID, context_digest=digest,
                risk_band="SIMULATOR_ONLY", route=route,
                selection_reason=reason, propensity=1,
                cost_cap_probes=cost_cap,
            )
            session.add(selected)
        return selected
