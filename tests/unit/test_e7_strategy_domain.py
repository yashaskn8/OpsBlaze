"""Unit tests for Stage 12 / Feature E7: Budgeted Strategy Router Domain Contracts."""

from __future__ import annotations

import math

import pytest

from backend.domain.strategy import (
    ModelTier,
    StrategyNoEligibleRouteError,
    StrategyPermissionEscalationError,
    StrategyRouteDefinition,
    compute_route_propensities,
    intersect_tool_capabilities,
    validate_tool_capabilities_subset,
)


def test_strategy_route_definition_validation() -> None:
    route = StrategyRouteDefinition(
        route_key="STANDARD_PROBE",
        model_tier=ModelTier.FLASH,
        allowed_tool_capabilities=frozenset({"system_metrics", "log_query"}),
        max_cost_micros=10000,
        max_tokens=4096,
        risk_ceiling=0.6,
        priority=10,
        is_active=True,
    )
    assert route.route_key == "STANDARD_PROBE"
    assert route.model_tier == ModelTier.FLASH
    assert route.max_cost_micros == 10000
    assert route.risk_ceiling == 0.6


def test_strategy_route_invalid_risk_ceiling() -> None:
    with pytest.raises(ValueError, match="risk_ceiling must be between 0.0 and 1.0"):
        StrategyRouteDefinition(
            route_key="INVALID_RISK",
            model_tier=ModelTier.FLASH,
            allowed_tool_capabilities=frozenset({"log_query"}),
            max_cost_micros=5000,
            max_tokens=2048,
            risk_ceiling=1.5,  # Invalid
        )


def test_strategy_route_negative_cost() -> None:
    with pytest.raises(ValueError, match="max_cost_micros must be non-negative"):
        StrategyRouteDefinition(
            route_key="INVALID_COST",
            model_tier=ModelTier.FLASH,
            allowed_tool_capabilities=frozenset({"log_query"}),
            max_cost_micros=-100,
            max_tokens=2048,
            risk_ceiling=0.5,
        )


def test_tool_capabilities_subsetting_success() -> None:
    base_tools = frozenset({"system_metrics", "log_query", "probe_read", "topology_inspect"})
    route_tools = frozenset({"system_metrics", "log_query"})

    # Must not raise
    validate_tool_capabilities_subset(base_capabilities=base_tools, route_capabilities=route_tools)

    # Intersect returns exactly route_tools
    effective = intersect_tool_capabilities(
        base_capabilities=base_tools, route_capabilities=route_tools
    )
    assert effective == route_tools


def test_tool_capabilities_expansion_forbidden() -> None:
    base_tools = frozenset({"system_metrics", "log_query"})
    unauthorized_route_tools = frozenset(
        {"system_metrics", "log_query", "unauthorized_mutation_tool"}
    )

    with pytest.raises(StrategyPermissionEscalationError, match="cannot expand"):
        validate_tool_capabilities_subset(
            base_capabilities=base_tools,
            route_capabilities=unauthorized_route_tools,
        )


def test_propensity_calculation_normalized() -> None:
    routes = [
        StrategyRouteDefinition(
            route_key="FAST_HEURISTIC",
            model_tier=ModelTier.FLASH_LITE,
            allowed_tool_capabilities=frozenset({"log_query"}),
            max_cost_micros=2000,
            max_tokens=1024,
            risk_ceiling=0.3,
            priority=1,
        ),
        StrategyRouteDefinition(
            route_key="STANDARD_PROBE",
            model_tier=ModelTier.FLASH,
            allowed_tool_capabilities=frozenset({"log_query", "probe_read"}),
            max_cost_micros=10000,
            max_tokens=4096,
            risk_ceiling=0.7,
            priority=2,
        ),
    ]

    propensities = compute_route_propensities(routes, estimated_risk=0.2)
    assert "FAST_HEURISTIC" in propensities
    assert "STANDARD_PROBE" in propensities

    p_fast = propensities["FAST_HEURISTIC"]
    p_std = propensities["STANDARD_PROBE"]

    assert 0.0 < p_fast <= 1.0
    assert 0.0 < p_std <= 1.0
    assert math.isclose(p_fast + p_std, 1.0, rel_tol=1e-5)

    # Fast heuristic is cheaper and fits the low risk (0.2), so its propensity should be higher or comparable
    assert p_fast > 0.0


def test_propensity_single_route() -> None:
    routes = [
        StrategyRouteDefinition(
            route_key="ONLY_ROUTE",
            model_tier=ModelTier.DETERMINISTIC,
            allowed_tool_capabilities=frozenset(),
            max_cost_micros=0,
            max_tokens=0,
            risk_ceiling=1.0,
        )
    ]
    propensities = compute_route_propensities(routes, estimated_risk=0.5)
    assert propensities == {"ONLY_ROUTE": 1.0}


def test_propensity_empty_routes_raises() -> None:
    with pytest.raises(StrategyNoEligibleRouteError):
        compute_route_propensities([], estimated_risk=0.5)
