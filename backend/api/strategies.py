"""FastAPI routes for Capability E7 — Budgeted Strategy Router.

Provides tenant-scoped endpoints for strategy route discovery, budgeted route selection,
decision ledger queries, and post-execution cost settlement.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.auth_deps import get_authenticated_principal
from backend.domain.auth import (
    AuthorizationForbiddenError,
    Principal,
    PrincipalRole,
)
from backend.domain.strategy import (
    RouteSelectionRequest,
    StrategyNoEligibleRouteError,
    StrategyPermissionEscalationError,
)
from backend.investigator.router import StrategyRouterService
from backend.persistence.models import Incident, StrategyDecision

router = APIRouter(prefix="/v1/strategies", tags=["strategies"])

STRATEGY_OPERATOR_ROLES: frozenset[str] = frozenset(
    {
        PrincipalRole.OPERATOR.value,
        PrincipalRole.RELEASE_OWNER.value,
        PrincipalRole.SRE_LEAD.value,
        PrincipalRole.INCIDENT_COMMANDER.value,
        PrincipalRole.PLATFORM_ENGINEER.value,
    }
)


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database session factory unavailable",
        )
    return cast(async_sessionmaker[AsyncSession], factory)


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------


class StrategyRouteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_key: str
    model_tier: str
    allowed_tool_capabilities: list[str]
    max_cost_micros: int
    max_tokens: int
    risk_ceiling: float
    priority: int
    is_active: bool


class StrategySelectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: uuid.UUID
    incident_severity: str = Field(default="medium", min_length=1, max_length=32)
    estimated_risk: float = Field(default=0.5, ge=0.0, le=1.0)
    requested_tools: list[str] = Field(default_factory=list)
    available_budget_micros: int = Field(default=50000, ge=0)


class StrategySelectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_key: str
    model_tier: str
    granted_tool_capabilities: list[str]
    propensity: float
    estimated_cost_micros: int
    is_fallback: bool
    fallback_reason: str | None = None


class StrategyDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    tenant_id: uuid.UUID
    incident_id: uuid.UUID
    route_key: str
    model_tier: str
    allowed_tool_capabilities: list[str]
    propensity: float
    budget_snapshot_json: dict[str, Any]
    estimated_cost_micros: int
    actual_cost_micros: int | None = None
    fallback_reason: str | None = None
    created_at: datetime


class StrategySettleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actual_cost_micros: int = Field(..., ge=0)


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------


@router.get("/routes", response_model=list[StrategyRouteResponse])
async def list_strategy_routes(
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> list[StrategyRouteResponse]:
    """Retrieves all active strategy routes available to the authenticated tenant."""
    router_service = StrategyRouterService(session_factory)
    routes = await router_service.list_routes(principal.tenant_id)
    return [
        StrategyRouteResponse(
            route_key=r.route_key,
            model_tier=str(r.model_tier.value if hasattr(r.model_tier, "value") else r.model_tier),
            allowed_tool_capabilities=sorted(r.allowed_tool_capabilities),
            max_cost_micros=r.max_cost_micros,
            max_tokens=r.max_tokens,
            risk_ceiling=r.risk_ceiling,
            priority=r.priority,
            is_active=r.is_active,
        )
        for r in routes
    ]


@router.post("/select", response_model=StrategySelectResponse)
async def select_strategy_route(
    req: StrategySelectRequest,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> StrategySelectResponse:
    """Executes budgeted strategy route selection for an active incident."""
    try:
        principal.require_any_role(STRATEGY_OPERATOR_ROLES, action_name="select_strategy_route")
    except AuthorizationForbiddenError as auth_err:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(auth_err),
        ) from auth_err

    # Verify incident exists and belongs to the authenticated tenant
    async with session_factory() as session:
        stmt = select(Incident).where(
            Incident.id == req.incident_id,
            Incident.tenant_id == principal.tenant_id,
        )
        incident = (await session.execute(stmt)).scalar_one_or_none()
        if incident is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Incident {req.incident_id} not found in authoritative tenant store.",
            )

    router_service = StrategyRouterService(session_factory)
    domain_request = RouteSelectionRequest(
        tenant_id=principal.tenant_id,
        incident_id=req.incident_id,
        incident_severity=req.incident_severity,
        estimated_risk=req.estimated_risk,
        requested_tools=frozenset(req.requested_tools),
        available_budget_micros=req.available_budget_micros,
    )

    base_tools = frozenset(req.requested_tools) if req.requested_tools else None
    try:
        result = await router_service.select_route(domain_request, base_tool_allowlist=base_tools)
    except StrategyPermissionEscalationError as esc_err:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Route tool escalation blocked: {esc_err}",
        ) from esc_err
    except StrategyNoEligibleRouteError as no_route_err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"No eligible strategy route: {no_route_err}",
        ) from no_route_err

    return StrategySelectResponse(
        route_key=result.route_key,
        model_tier=result.model_tier,
        granted_tool_capabilities=sorted(result.granted_tool_capabilities),
        propensity=result.propensity,
        estimated_cost_micros=result.estimated_cost_micros,
        is_fallback=result.is_fallback,
        fallback_reason=result.fallback_reason.value if result.fallback_reason else None,
    )


@router.get("/decisions/{incident_id}", response_model=list[StrategyDecisionResponse])
async def get_strategy_decisions(
    incident_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> list[StrategyDecisionResponse]:
    """Retrieves all strategy decisions and logged propensities for an incident."""
    # Verify incident exists and belongs to the authenticated tenant
    async with session_factory() as session:
        stmt = select(Incident).where(
            Incident.id == incident_id,
            Incident.tenant_id == principal.tenant_id,
        )
        incident = (await session.execute(stmt)).scalar_one_or_none()
        if incident is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Incident {incident_id} not found in authoritative tenant store.",
            )

    router_service = StrategyRouterService(session_factory)
    decisions = await router_service.get_decisions_for_incident(principal.tenant_id, incident_id)
    return [
        StrategyDecisionResponse(
            id=d.id,
            tenant_id=d.tenant_id,
            incident_id=d.incident_id,
            route_key=d.route_key,
            model_tier=d.model_tier,
            allowed_tool_capabilities=d.allowed_tool_capabilities or [],
            propensity=d.propensity,
            budget_snapshot_json=d.budget_snapshot_json or {},
            estimated_cost_micros=d.estimated_cost_micros,
            actual_cost_micros=d.actual_cost_micros,
            fallback_reason=d.fallback_reason,
            created_at=d.created_at,
        )
        for d in decisions
    ]


@router.post("/decisions/{decision_id}/settle", response_model=StrategyDecisionResponse)
async def settle_strategy_decision(
    decision_id: uuid.UUID,
    req: StrategySettleRequest,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> StrategyDecisionResponse:
    """Settles the actual cost of an executed strategy decision."""
    try:
        principal.require_any_role(STRATEGY_OPERATOR_ROLES, action_name="settle_strategy_decision")
    except AuthorizationForbiddenError as auth_err:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(auth_err),
        ) from auth_err

    # Verify decision belongs to the authenticated tenant
    async with session_factory() as session:
        stmt = select(StrategyDecision).where(
            StrategyDecision.id == decision_id,
            StrategyDecision.tenant_id == principal.tenant_id,
        )
        decision = (await session.execute(stmt)).scalar_one_or_none()
        if decision is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Strategy decision {decision_id} not found.",
            )

    router_service = StrategyRouterService(session_factory)
    updated = await router_service.settle_decision(decision_id, req.actual_cost_micros)

    return StrategyDecisionResponse(
        id=updated.id,
        tenant_id=updated.tenant_id,
        incident_id=updated.incident_id,
        route_key=updated.route_key,
        model_tier=updated.model_tier,
        allowed_tool_capabilities=updated.allowed_tool_capabilities or [],
        propensity=updated.propensity,
        budget_snapshot_json=updated.budget_snapshot_json or {},
        estimated_cost_micros=updated.estimated_cost_micros,
        actual_cost_micros=updated.actual_cost_micros,
        fallback_reason=updated.fallback_reason,
        created_at=updated.created_at,
    )
