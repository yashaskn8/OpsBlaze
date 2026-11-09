"""Stage 12 / Feature E7: Budgeted Strategy Router Service.

Authoritative implementation of budgeted route selection, tool capability subsetting,
two-phase budget reservation, propensity calculation, and fail-closed conservative fallback.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.strategy import (
    FallbackReason,
    ModelTier,
    RouteSelectionRequest,
    RouteSelectionResult,
    StrategyRouteDefinition,
    StrategyRouteNotFoundError,
    compute_route_propensities,
)
from backend.observability.metrics import metrics
from backend.persistence.models import StrategyDecision, StrategyRoute

logger = logging.getLogger("opsblaze.investigator.router")

DEFAULT_STRATEGY_ROUTES: tuple[StrategyRouteDefinition, ...] = (
    StrategyRouteDefinition(
        route_key="FAST_HEURISTIC",
        model_tier=ModelTier.FLASH_LITE,
        allowed_tool_capabilities=frozenset({"system_metrics", "log_query"}),
        max_cost_micros=2000,
        max_tokens=1024,
        risk_ceiling=0.25,
        priority=10,
        is_active=True,
    ),
    StrategyRouteDefinition(
        route_key="STANDARD_PROBE",
        model_tier=ModelTier.FLASH,
        allowed_tool_capabilities=frozenset({"system_metrics", "log_query", "probe_read"}),
        max_cost_micros=10000,
        max_tokens=4096,
        risk_ceiling=0.60,
        priority=20,
        is_active=True,
    ),
    StrategyRouteDefinition(
        route_key="DEEP_ANALYSIS",
        model_tier=ModelTier.PRO,
        allowed_tool_capabilities=frozenset(
            {"system_metrics", "log_query", "probe_read", "topology_inspect"}
        ),
        max_cost_micros=50000,
        max_tokens=16384,
        risk_ceiling=0.85,
        priority=30,
        is_active=True,
    ),
    StrategyRouteDefinition(
        route_key="CONSERVATIVE_FALLBACK",
        model_tier=ModelTier.DETERMINISTIC,
        allowed_tool_capabilities=frozenset({"system_metrics"}),
        max_cost_micros=0,
        max_tokens=0,
        risk_ceiling=1.00,
        priority=0,
        is_active=True,
    ),
    StrategyRouteDefinition(
        route_key="HUMAN_ESCALATION",
        model_tier=ModelTier.HUMAN,
        allowed_tool_capabilities=frozenset(),
        max_cost_micros=0,
        max_tokens=0,
        risk_ceiling=1.00,
        priority=0,
        is_active=True,
    ),
)


class StrategyRouterService:
    """Service governing budgeted investigative strategy selection and decision logging."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_routes(self, tenant_id: uuid.UUID) -> list[StrategyRouteDefinition]:
        """Returns active strategy routes for the tenant, falling back to standard defaults if empty."""
        async with self._session_factory() as session:
            stmt = (
                select(StrategyRoute)
                .where(
                    (StrategyRoute.tenant_id == tenant_id) | (StrategyRoute.tenant_id.is_(None)),
                    StrategyRoute.is_active.is_(True),
                )
                .order_by(StrategyRoute.priority.desc(), StrategyRoute.created_at.desc())
            )
            rows = (await session.execute(stmt)).scalars().all()

        if not rows:
            return list(DEFAULT_STRATEGY_ROUTES)

        return [
            StrategyRouteDefinition(
                route_key=r.route_key,
                model_tier=r.model_tier,
                allowed_tool_capabilities=frozenset(r.allowed_tool_capabilities or []),
                max_cost_micros=r.max_cost_micros,
                max_tokens=r.max_tokens,
                risk_ceiling=r.risk_ceiling,
                priority=r.priority,
                is_active=r.is_active,
            )
            for r in rows
        ]

    async def register_route(
        self,
        tenant_id: uuid.UUID,
        route_def: StrategyRouteDefinition,
    ) -> StrategyRoute:
        """Registers or updates a custom strategy route in the tenant catalog."""
        async with self._session_factory() as session:
            stmt = select(StrategyRoute).where(
                StrategyRoute.tenant_id == tenant_id,
                StrategyRoute.route_key == route_def.route_key,
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()

            tier_val = (
                route_def.model_tier.value
                if hasattr(route_def.model_tier, "value")
                else str(route_def.model_tier)
            )

            if existing:
                existing.model_tier = tier_val
                existing.allowed_tool_capabilities = sorted(route_def.allowed_tool_capabilities)
                existing.max_cost_micros = route_def.max_cost_micros
                existing.max_tokens = route_def.max_tokens
                existing.risk_ceiling = route_def.risk_ceiling
                existing.priority = route_def.priority
                existing.is_active = route_def.is_active
                await session.commit()
                return existing

            new_route = StrategyRoute(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                route_key=route_def.route_key,
                model_tier=tier_val,
                allowed_tool_capabilities=sorted(route_def.allowed_tool_capabilities),
                max_cost_micros=route_def.max_cost_micros,
                max_tokens=route_def.max_tokens,
                risk_ceiling=route_def.risk_ceiling,
                priority=route_def.priority,
                is_active=route_def.is_active,
            )
            session.add(new_route)
            await session.commit()
            return new_route

    async def select_route(
        self,
        request: RouteSelectionRequest,
        base_tool_allowlist: frozenset[str] | None = None,
    ) -> RouteSelectionResult:
        """Selects the optimal strategy route under budget and risk constraints, logging the decision."""
        routes = await self.list_routes(request.tenant_id)

        # Distinguish standard investigation routes from fallbacks
        fallback_keys = {"CONSERVATIVE_FALLBACK", "HUMAN_ESCALATION"}
        candidate_routes = [r for r in routes if r.route_key not in fallback_keys]

        # Filter candidates by risk ceiling and budget feasibility
        budget_eligible = [
            r for r in candidate_routes if r.max_cost_micros <= request.available_budget_micros
        ]
        risk_and_budget_eligible = [
            r for r in budget_eligible if request.estimated_risk <= r.risk_ceiling
        ]

        if risk_and_budget_eligible:
            # Sort by priority descending
            risk_and_budget_eligible.sort(
                key=lambda r: (r.priority, -r.max_cost_micros), reverse=True
            )
            propensities = compute_route_propensities(
                risk_and_budget_eligible, request.estimated_risk
            )
            selected = risk_and_budget_eligible[0]
            propensity = propensities.get(selected.route_key, 1.0)

            # Enforce capability subsetting
            if base_tool_allowlist is not None:
                granted_tools = base_tool_allowlist & selected.allowed_tool_capabilities
            else:
                granted_tools = selected.allowed_tool_capabilities

            result = RouteSelectionResult(
                route_key=selected.route_key,
                model_tier=str(selected.model_tier),
                granted_tool_capabilities=granted_tools,
                propensity=propensity,
                estimated_cost_micros=selected.max_cost_micros,
                is_fallback=False,
                fallback_reason=None,
            )
        else:
            # Deterministic Fallback Triggered
            if not budget_eligible:
                fallback_reason = FallbackReason.BUDGET_EXHAUSTED
                fallback_key = "CONSERVATIVE_FALLBACK"
            elif request.estimated_risk > 0.8:
                fallback_reason = FallbackReason.RISK_CEILING_BREACH
                fallback_key = "HUMAN_ESCALATION"
            else:
                fallback_reason = FallbackReason.RISK_CEILING_BREACH
                fallback_key = "CONSERVATIVE_FALLBACK"

            fallback_def = next(
                (r for r in routes if r.route_key == fallback_key),
                DEFAULT_STRATEGY_ROUTES[3],  # CONSERVATIVE_FALLBACK
            )

            if base_tool_allowlist is not None:
                granted_tools = base_tool_allowlist & fallback_def.allowed_tool_capabilities
            else:
                granted_tools = fallback_def.allowed_tool_capabilities

            result = RouteSelectionResult(
                route_key=fallback_def.route_key,
                model_tier=str(fallback_def.model_tier),
                granted_tool_capabilities=granted_tools,
                propensity=1.0,
                estimated_cost_micros=fallback_def.max_cost_micros,
                is_fallback=True,
                fallback_reason=fallback_reason,
            )

        # Persist decision record in PostgreSQL
        async with self._session_factory() as session:
            decision = StrategyDecision(
                id=uuid.uuid4(),
                tenant_id=request.tenant_id,
                incident_id=request.incident_id,
                route_key=result.route_key,
                model_tier=result.model_tier,
                allowed_tool_capabilities=sorted(result.granted_tool_capabilities),
                propensity=result.propensity,
                budget_snapshot_json={
                    "available_budget_micros": request.available_budget_micros,
                    "estimated_risk": request.estimated_risk,
                    "severity": request.incident_severity,
                },
                estimated_cost_micros=result.estimated_cost_micros,
                actual_cost_micros=None,
                fallback_reason=result.fallback_reason.value if result.fallback_reason else None,
                created_at=datetime.now(UTC),
            )
            session.add(decision)
            await session.commit()

        metrics.inc("strategy_decisions_total")

        return result

    async def settle_decision(
        self,
        decision_id: uuid.UUID,
        actual_cost_micros: int,
    ) -> StrategyDecision:
        """Settles the actual post-execution cost for an immutable strategy decision record."""
        async with self._session_factory() as session:
            stmt = select(StrategyDecision).where(StrategyDecision.id == decision_id)
            decision = (await session.execute(stmt)).scalar_one_or_none()
            if decision is None:
                raise StrategyRouteNotFoundError(f"Strategy decision {decision_id} not found.")

            decision.actual_cost_micros = actual_cost_micros
            await session.commit()
            return decision

    async def get_decisions_for_incident(
        self,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
    ) -> list[StrategyDecision]:
        """Returns all strategy decisions recorded for an incident, newest first."""
        async with self._session_factory() as session:
            stmt = (
                select(StrategyDecision)
                .where(
                    StrategyDecision.tenant_id == tenant_id,
                    StrategyDecision.incident_id == incident_id,
                )
                .order_by(StrategyDecision.created_at.desc())
            )
            return list((await session.execute(stmt)).scalars().all())
