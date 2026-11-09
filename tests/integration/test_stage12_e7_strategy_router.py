"""Integration tests for Stage 12 / Feature E7: Budgeted Strategy Router.

Verifies end-to-end route catalog retrieval, budgeted route selection,
safe fallback under budget and risk constraints, propensity ledger persistence,
and post-execution cost settlement against PostgreSQL 17.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.strategy import (
    FallbackReason,
    ModelTier,
    RouteSelectionRequest,
    StrategyRouteDefinition,
)
from backend.investigator.router import StrategyRouterService
from backend.persistence.models import Incident, StrategyRoute


@pytest.fixture
async def incident_for_test(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> uuid.UUID:
    """Creates a test incident in opsblaze_test."""
    incident_id = uuid.uuid4()
    async with session_factory() as session:
        inc = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"e7-test-{incident_id}",
            request_sha256="test-sha256",
            target_id="service-auth-gateway",
            status_projection="ACTIVE",
            status_version=1,
        )
        session.add(inc)
        await session.commit()
    return incident_id


@pytest.mark.asyncio
async def test_default_routes_listed(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    router = StrategyRouterService(session_factory)
    routes = await router.list_routes(isolated_tenant)
    assert len(routes) >= 5
    route_keys = {r.route_key for r in routes}
    assert "FAST_HEURISTIC" in route_keys
    assert "STANDARD_PROBE" in route_keys
    assert "DEEP_ANALYSIS" in route_keys
    assert "CONSERVATIVE_FALLBACK" in route_keys
    assert "HUMAN_ESCALATION" in route_keys


@pytest.mark.asyncio
async def test_custom_route_registration_and_persistence(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    router = StrategyRouterService(session_factory)
    custom_def = StrategyRouteDefinition(
        route_key="CUSTOM_QUICK_SCAN",
        model_tier=ModelTier.FLASH_LITE,
        allowed_tool_capabilities=frozenset({"log_query"}),
        max_cost_micros=1500,
        max_tokens=512,
        risk_ceiling=0.2,
        priority=50,
        is_active=True,
    )
    saved = await router.register_route(isolated_tenant, custom_def)
    assert saved.route_key == "CUSTOM_QUICK_SCAN"

    # Verify persisted in database
    async with session_factory() as session:
        stmt = select(StrategyRoute).where(
            StrategyRoute.tenant_id == isolated_tenant,
            StrategyRoute.route_key == "CUSTOM_QUICK_SCAN",
        )
        db_route = (await session.execute(stmt)).scalar_one_or_none()
        assert db_route is not None
        assert db_route.max_cost_micros == 1500
        assert db_route.priority == 50


@pytest.mark.asyncio
async def test_route_selection_normal_budget_and_risk(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    incident_for_test: uuid.UUID,
) -> None:
    router = StrategyRouterService(session_factory)

    base_tools = frozenset({"system_metrics", "log_query", "probe_read", "topology_inspect"})
    req = RouteSelectionRequest(
        tenant_id=isolated_tenant,
        incident_id=incident_for_test,
        incident_severity="medium",
        estimated_risk=0.4,
        requested_tools=base_tools,
        available_budget_micros=100000,  # Generous budget
    )

    result = await router.select_route(req, base_tool_allowlist=base_tools)

    assert result.is_fallback is False
    assert result.fallback_reason is None
    assert result.route_key in ("STANDARD_PROBE", "DEEP_ANALYSIS")
    assert 0.0 < result.propensity <= 1.0
    assert result.granted_tool_capabilities.issubset(base_tools)

    # Verify decision recorded in PostgreSQL strategy_decision
    decisions = await router.get_decisions_for_incident(isolated_tenant, incident_for_test)
    assert len(decisions) >= 1
    latest = decisions[0]
    assert latest.route_key == result.route_key
    assert latest.incident_id == incident_for_test
    assert latest.propensity == result.propensity
    assert latest.actual_cost_micros is None


@pytest.mark.asyncio
async def test_route_selection_budget_exhaustion_fallback(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    incident_for_test: uuid.UUID,
) -> None:
    router = StrategyRouterService(session_factory)

    base_tools = frozenset({"system_metrics", "log_query"})
    # Zero available budget
    req = RouteSelectionRequest(
        tenant_id=isolated_tenant,
        incident_id=incident_for_test,
        incident_severity="high",
        estimated_risk=0.3,
        requested_tools=base_tools,
        available_budget_micros=0,
    )

    result = await router.select_route(req, base_tool_allowlist=base_tools)

    # Must safely fall back
    assert result.is_fallback is True
    assert result.fallback_reason == FallbackReason.BUDGET_EXHAUSTED
    assert result.route_key == "CONSERVATIVE_FALLBACK"
    assert result.estimated_cost_micros == 0
    assert result.propensity == 1.0


@pytest.mark.asyncio
async def test_route_selection_risk_ceiling_breach_fallback(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    incident_for_test: uuid.UUID,
) -> None:
    router = StrategyRouterService(session_factory)

    base_tools = frozenset({"system_metrics", "log_query"})
    # Extreme risk exceeding standard investigation ceilings
    req = RouteSelectionRequest(
        tenant_id=isolated_tenant,
        incident_id=incident_for_test,
        incident_severity="critical",
        estimated_risk=0.95,
        requested_tools=base_tools,
        available_budget_micros=100000,
    )

    result = await router.select_route(req, base_tool_allowlist=base_tools)

    # Must escalate to human or conservative fallback
    assert result.is_fallback is True
    assert result.fallback_reason == FallbackReason.RISK_CEILING_BREACH
    assert result.route_key in ("HUMAN_ESCALATION", "CONSERVATIVE_FALLBACK")
    assert result.propensity == 1.0


@pytest.mark.asyncio
async def test_tool_capability_subsetting_enforced(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    incident_for_test: uuid.UUID,
) -> None:
    router = StrategyRouterService(session_factory)

    # Base allowlist permits ONLY system_metrics
    restricted_base = frozenset({"system_metrics"})
    req = RouteSelectionRequest(
        tenant_id=isolated_tenant,
        incident_id=incident_for_test,
        incident_severity="low",
        estimated_risk=0.1,
        requested_tools=restricted_base,
        available_budget_micros=50000,
    )

    result = await router.select_route(req, base_tool_allowlist=restricted_base)

    # Granted tools can NEVER exceed the restricted base
    assert result.granted_tool_capabilities.issubset(restricted_base)
    assert "log_query" not in result.granted_tool_capabilities
    assert "probe_read" not in result.granted_tool_capabilities


@pytest.mark.asyncio
async def test_settle_decision_cost(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    incident_for_test: uuid.UUID,
) -> None:
    router = StrategyRouterService(session_factory)

    base_tools = frozenset({"system_metrics", "log_query"})
    req = RouteSelectionRequest(
        tenant_id=isolated_tenant,
        incident_id=incident_for_test,
        incident_severity="low",
        estimated_risk=0.2,
        requested_tools=base_tools,
        available_budget_micros=50000,
    )

    result = await router.select_route(req, base_tool_allowlist=base_tools)
    assert result.route_key is not None
    decisions = await router.get_decisions_for_incident(isolated_tenant, incident_for_test)
    decision_id = decisions[0].id

    # Settle actual cost
    settled = await router.settle_decision(decision_id, actual_cost_micros=1840)
    assert settled.actual_cost_micros == 1840

    # Re-verify in database
    refreshed = await router.get_decisions_for_incident(isolated_tenant, incident_for_test)
    assert refreshed[0].actual_cost_micros == 1840
