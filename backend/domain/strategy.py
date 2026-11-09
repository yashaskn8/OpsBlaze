"""Stage 12 / Feature E7: Budgeted Strategy Router Domain Contracts and Exceptions.

Defines the core data contracts, invariants, and algorithms for budgeted investigative
route selection, server-side tool capability subsetting, and off-policy propensity logging.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class ModelTier(str, Enum):
    """Supported model capabilities and runtime tiers."""

    FLASH_LITE = "flash_lite"
    FLASH = "flash"
    PRO = "pro"
    DETERMINISTIC = "deterministic"
    HUMAN = "human"


class FallbackReason(str, Enum):
    """Reasons triggering deterministic route fallback or human escalation."""

    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    RISK_CEILING_BREACH = "RISK_CEILING_BREACH"
    PERMISSION_RESTRICTION = "PERMISSION_RESTRICTION"
    NO_ELIGIBLE_ROUTE = "NO_ELIGIBLE_ROUTE"


class StrategyRouterError(Exception):
    """Base exception for all strategy routing errors."""


class StrategyPermissionEscalationError(StrategyRouterError):
    """Raised when a route attempts to grant capabilities outside the verified base set."""


class StrategyBudgetExceededError(StrategyRouterError):
    """Raised when an incident's required route exceeds the tenant's remaining budget headroom."""


class StrategyNoEligibleRouteError(StrategyRouterError):
    """Raised when no active strategy route satisfies risk and cost constraints."""


class StrategyRouteNotFoundError(StrategyRouterError):
    """Raised when an explicitly requested strategy route key does not exist."""


@dataclass(frozen=True)
class StrategyRouteDefinition:
    """Pre-approved investigative strategy route specification."""

    route_key: str
    model_tier: ModelTier | str
    allowed_tool_capabilities: frozenset[str]
    max_cost_micros: int
    max_tokens: int
    risk_ceiling: float
    priority: int = 0
    is_active: bool = True

    def __post_init__(self) -> None:
        if not self.route_key or not self.route_key.strip():
            raise ValueError("route_key cannot be empty")
        if self.max_cost_micros < 0:
            raise ValueError(f"max_cost_micros must be non-negative, got {self.max_cost_micros}")
        if self.max_tokens < 0:
            raise ValueError(f"max_tokens must be non-negative, got {self.max_tokens}")
        if not (0.0 <= self.risk_ceiling <= 1.0):
            raise ValueError(f"risk_ceiling must be between 0.0 and 1.0, got {self.risk_ceiling}")


@dataclass(frozen=True)
class RouteSelectionRequest:
    """Inbound request to select an optimal, budgeted investigative route."""

    tenant_id: uuid.UUID
    incident_id: uuid.UUID
    incident_severity: str
    estimated_risk: float
    requested_tools: frozenset[str] = field(default_factory=frozenset)
    available_budget_micros: int = 0


@dataclass(frozen=True)
class RouteSelectionResult:
    """Authoritative outcome of strategy route selection."""

    route_key: str
    model_tier: str
    granted_tool_capabilities: frozenset[str]
    propensity: float
    estimated_cost_micros: int
    is_fallback: bool = False
    fallback_reason: FallbackReason | None = None


@dataclass(frozen=True)
class StrategyDecisionRecord:
    """Append-only decision ledger entry for off-policy S1/E5 evaluation and attribution."""

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
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def validate_tool_capabilities_subset(
    *,
    base_capabilities: frozenset[str],
    route_capabilities: frozenset[str],
) -> None:
    """Enforces the Class 1 Invariant: A route may only SUBSET tools, never EXPAND them."""
    unauthorized = route_capabilities - base_capabilities
    if unauthorized:
        raise StrategyPermissionEscalationError(
            f"Strategy route cannot expand tool capabilities beyond base set. "
            f"Unauthorized capabilities requested: {sorted(unauthorized)}"
        )


def intersect_tool_capabilities(
    *,
    base_capabilities: frozenset[str],
    route_capabilities: frozenset[str],
) -> frozenset[str]:
    """Computes the effective tool capabilities granted to an investigator."""
    validate_tool_capabilities_subset(
        base_capabilities=base_capabilities,
        route_capabilities=route_capabilities,
    )
    return base_capabilities & route_capabilities


def compute_route_propensities(
    eligible_routes: Sequence[StrategyRouteDefinition],
    estimated_risk: float,
    temperature: float = 1.0,
) -> dict[str, float]:
    """Computes normalized selection propensities P(route | state) for off-policy evaluation.

    Uses a soft-max over negative cost and risk headroom across eligible routes.
    """
    if not eligible_routes:
        raise StrategyNoEligibleRouteError("Cannot compute propensities for an empty route set.")

    if len(eligible_routes) == 1:
        return {eligible_routes[0].route_key: 1.0}

    # Normalize costs to [0, 1]
    max_cost = max(r.max_cost_micros for r in eligible_routes) or 1
    scores: list[float] = []

    for r in eligible_routes:
        cost_norm = r.max_cost_micros / max_cost
        # Risk penalty: route is penalized if risk exceeds ceiling or rewarded if generous headroom
        risk_slack = max(0.0, r.risk_ceiling - estimated_risk)
        # Higher score = lower cost and higher risk slack + priority weight
        score = (-1.0 * cost_norm) + (2.0 * risk_slack) + (0.1 * r.priority)
        scores.append(score / max(0.1, temperature))

    # Stable Softmax
    max_score = max(scores)
    exp_scores = [math.exp(s - max_score) for s in scores]
    sum_exp = sum(exp_scores)

    return {r.route_key: round(exp / sum_exp, 6) for r, exp in zip(eligible_routes, exp_scores)}
