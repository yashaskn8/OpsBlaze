"""Domain contracts, models, and exceptions for B4 Durable Budget & Capacity."""

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class BudgetStatus(StrEnum):
    """Lifecycle state of an incident budget."""

    ACTIVE = "ACTIVE"
    EXHAUSTED = "EXHAUSTED"
    SETTLED = "SETTLED"


class BudgetLimits(BaseModel):
    """Ceiling limits allocated to an incident investigation."""

    max_tokens: int = 50000
    max_probes: int = 10
    max_cost_micros: int = 10_000_000  # $10.00 USD
    max_time_seconds: int = 300


class BudgetReservation(BaseModel):
    """Two-phase reservation lease for a single observation or model step."""

    nonce: uuid.UUID
    tokens: int
    probes: int
    cost_micros: int
    lease_expires_at: datetime


class BudgetSnapshot(BaseModel):
    """Point-in-time state of an incident's budget ledger."""

    model_config = ConfigDict(from_attributes=True)

    tenant_id: uuid.UUID
    incident_id: uuid.UUID
    reserved_tokens: int
    settled_tokens: int
    reserved_probes: int
    settled_probes: int
    reserved_cost_micros: int
    settled_cost_micros: int
    max_tokens: int
    max_probes: int
    max_cost_micros: int
    max_time_seconds: int
    status: BudgetStatus = Field(default=BudgetStatus.ACTIVE)
    reservation_nonce: uuid.UUID | None = None
    lease_expires_at: datetime | None = None


class BudgetError(Exception):
    """Base exception for budget violations."""


class BudgetExhaustedError(BudgetError):
    """Raised when an operation would exceed budget limits."""

    def __init__(self, dimension: str, current_val: int, max_val: int) -> None:
        self.dimension = dimension
        self.current_val = current_val
        self.max_val = max_val
        super().__init__(
            f"Budget exhausted on dimension '{dimension}': projected {current_val} exceeds limit {max_val}"
        )


class BudgetConcurrencyError(BudgetError):
    """Raised when an unexpired lease lock is held concurrently."""


class BudgetReservationNotFoundError(BudgetError):
    """Raised when a reservation nonce does not exist or has already been cleared."""
