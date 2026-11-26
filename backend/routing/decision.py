
"""E7 pinned simulator routes using persisted risk, freshness, and eligible memory context."""

import hashlib
import json
from typing import Literal
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.investigator.advisory import MAX_RESERVE_TOKENS
from backend.lifecycle import UNRESOLVED_EFFECT_STATES
from backend.memory.lessons import retrieve_active_lessons
from backend.models import (
    EffectReceipt,
    Evidence,
    Incident,
    OutcomeRevision,
    PolicyAssignment,
    RouteDecision,
    SimulatorTarget,
)
from backend.policy.constants import INCUMBENT_POLICY_ID
from backend.probes.gateway import classify_evidence_freshness
from backend.settings import get_settings

ROUTER_POLICY_ID = INCUMBENT_POLICY_ID
ROUTER_VERSION = "simulator-routing-v2"


async def read_route(session: AsyncSession, tenant_id: str, incident_id: UUID) -> RouteDecision | None:
    return await session.scalar(select(RouteDecision).where(
        RouteDecision.tenant_id == tenant_id, RouteDecision.incident_id == incident_id,
        RouteDecision.decision_no == 1,
    ))


async def select_route(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str, incident_id: UUID,
    *,
    strategy_mode: Literal["DEFAULT", "FIXED_RUNBOOK", "CHEAPEST_FIRST", "ADAPTIVE"] = "DEFAULT",
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
            existing = await read_route(session, tenant_id, incident_id)
            if existing is not None:
                return existing

            assignment = await session.get(
                PolicyAssignment, (tenant_id, incident_id)
            )
            target = await session.get(
                SimulatorTarget, (tenant_id, incident.target_id)
            )
            unresolved_on_target = int((await session.scalar(
                select(func.count(EffectReceipt.id)).where(
                    EffectReceipt.tenant_id == tenant_id,
                    EffectReceipt.target_id == incident.target_id,
                    EffectReceipt.state.in_(UNRESOLVED_EFFECT_STATES),
                )
            )) or 0)
            disputed_on_target = int((await session.scalar(
                select(func.count(OutcomeRevision.id))
                .join(Incident, Incident.id == OutcomeRevision.incident_id)
                .where(
                    OutcomeRevision.tenant_id == tenant_id,
                    Incident.tenant_id == tenant_id,
                    Incident.target_id == incident.target_id,
                    OutcomeRevision.label == "DISPUTED",
                )
            )) or 0)
            latest_ev = await session.scalar(
                select(Evidence)
                .where(Evidence.tenant_id == tenant_id, Evidence.incident_id == incident_id)
                .order_by(Evidence.captured_at.desc(), Evidence.id.desc())
                .limit(1)
            )
            source_freshness = (
                classify_evidence_freshness(latest_ev) if latest_ev is not None else "NONE"
            )
            try:
                eligible_lessons = await retrieve_active_lessons(
                    session, tenant_id, incident.target_id, limit=4,
                )
                eligible_memory_ids = [str(lesson.id) for lesson in eligible_lessons]
                memory_status = "VALIDATED_LESSONS" if eligible_memory_ids else "NO_MEMORY"
            except SQLAlchemyError:
                eligible_memory_ids = []
                memory_status = "MEMORY_FALLBACK_UNAVAILABLE"

            settings = get_settings()
            budget = settings.max_probe_calls_per_incident
            model = settings.ai_advisory_model.strip() if (
                settings.environment == "development" and settings.ai_advisory_enabled
            ) else ""
            model_id = None
            reservation = 0
            risk_band = (
                "ELEVATED_UNRESOLVED_EFFECT"
                if unresolved_on_target > 0
                else ("ELEVATED_DISPUTED_HISTORY" if disputed_on_target > 0 else "SIMULATOR_ONLY")
            )
            if (
                assignment is None
                or assignment.policy_id != INCUMBENT_POLICY_ID
                or assignment.bundle_id is not None
                or assignment.rollout_id is not None
            ):
                route, reason = "HANDOFF", "POLICY_PIN_INVALID"
                cost_cap = 0
            elif target is None:
                route, reason = "HANDOFF", "SIMULATOR_TARGET_MISSING"
                cost_cap = 0
            elif unresolved_on_target > 0:
                route, reason = "HANDOFF", "UNRESOLVED_EFFECT_ACTIVE"
                cost_cap = 0
            elif budget < 2:
                route, reason = "HANDOFF", "PROBE_BUDGET_INSUFFICIENT"
                cost_cap = 0
            elif 1 <= len(model) <= 64 and settings.ai_token_limit_per_minute >= MAX_RESERVE_TOKENS and strategy_mode in {"DEFAULT", "ADAPTIVE"}:
                route, reason = "AGENT_NO_MEMORY", "DEVELOPMENT_READ_ONLY_ADVISORY"
                cost_cap = 2
                model_id = model
                reservation = MAX_RESERVE_TOKENS
            elif strategy_mode == "CHEAPEST_FIRST":
                route, reason = "CHEAPEST_FIRST", "APPROVED_CHEAPEST_FIRST_BASELINE"
                cost_cap = 2
            elif strategy_mode == "ADAPTIVE":
                route, reason = "ADAPTIVE", "APPROVED_SIMULATOR_ADAPTIVE"
                cost_cap = 2
            else:
                route, reason = "RUNBOOK", "APPROVED_SIMULATOR_BASELINE"
                cost_cap = 2

            selected_strategy = (
                "AGENT_NO_MEMORY"
                if route == "AGENT_NO_MEMORY"
                else ("HANDOFF" if route == "HANDOFF" else route)
            )
            context = {
                "tenant": tenant_id,
                "target": incident.target_id,
                "target_available": target is not None,
                "policy": assignment.policy_id if assignment is not None else None,
                "baseline_assignment": assignment is not None and assignment.bundle_id is None
                    and assignment.rollout_id is None,
                "risk_band": risk_band,
                "unresolved_effects_on_target": unresolved_on_target,
                "disputed_outcomes_on_target": disputed_on_target,
                "source_freshness": source_freshness,
                "memory_status": memory_status,
                "eligible_memory_count": len(eligible_memory_ids),
                "eligible_lesson_ids": eligible_memory_ids,
                "probe_limit": budget,
                "advisory_model": model,
                "tenant_token_limit": settings.ai_token_limit_per_minute,
                "router_version": ROUTER_VERSION,
                **(
                    {"strategy_mode": strategy_mode}
                    if strategy_mode != "DEFAULT"
                    else {}
                ),
            }
            encoded = json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            persisted_features = {
                "risk_band": risk_band,
                "unresolved_effects_on_target": unresolved_on_target,
                "disputed_outcomes_on_target": disputed_on_target,
                "source_freshness": source_freshness,
                "memory_status": memory_status,
                "eligible_memory_count": len(eligible_memory_ids),
                "eligible_lesson_ids": eligible_memory_ids,
                "probe_limit": budget,
                "target_available": target is not None,
                "selected_strategy": selected_strategy,
            }
            selected = RouteDecision(
                id=uuid5(NAMESPACE_URL, f"opsblaze/route/{tenant_id}/{incident_id}/1"),
                tenant_id=tenant_id, incident_id=incident_id, decision_no=1,
                policy_id=ROUTER_POLICY_ID, context_digest=digest,
                risk_band=risk_band, route=route,
                selection_reason=reason, propensity=1,
                cost_cap_probes=cost_cap, router_version=ROUTER_VERSION,
                model_id=model_id, model_token_reservation=reservation,
                context_features=persisted_features,
            )
            session.add(selected)
        return selected
