"""Feature P2 — Domain contracts, state machine, and validators for Version-Bound Approval Cockpit."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.domain.incident import DomainError


class ApprovalState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DISPATCHING = "DISPATCHING"
    REJECTED = "REJECTED"
    REVOKED = "REVOKED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"


TERMINAL_APPROVAL_STATES: frozenset[ApprovalState] = frozenset(
    {
        ApprovalState.REJECTED,
        ApprovalState.REVOKED,
        ApprovalState.INVALIDATED,
        ApprovalState.EXPIRED,
        ApprovalState.CONSUMED,
    }
)

ALLOWED_APPROVAL_TRANSITIONS: dict[ApprovalState, frozenset[ApprovalState]] = {
    ApprovalState.PENDING: frozenset(
        {
            ApprovalState.APPROVED,
            ApprovalState.REJECTED,
            ApprovalState.EXPIRED,
        }
    ),
    ApprovalState.APPROVED: frozenset(
        {
            ApprovalState.DISPATCHING,
            ApprovalState.REVOKED,
            ApprovalState.INVALIDATED,
            ApprovalState.EXPIRED,
        }
    ),
    ApprovalState.DISPATCHING: frozenset(
        {
            ApprovalState.CONSUMED,
            ApprovalState.REVOKED,
            ApprovalState.INVALIDATED,
            ApprovalState.EXPIRED,
        }
    ),
    ApprovalState.REJECTED: frozenset(),
    ApprovalState.REVOKED: frozenset(),
    ApprovalState.INVALIDATED: frozenset(),
    ApprovalState.EXPIRED: frozenset(),
    ApprovalState.CONSUMED: frozenset(),
}

FORBIDDEN_APPROVAL_ROLES: frozenset[str] = frozenset(
    {
        "agent",
        "model",
        "investigator",
        "llm",
        "system",
        "autonomous_agent",
        "bot",
    }
)

ALLOWED_APPROVAL_ROLES: frozenset[str] = frozenset(
    {
        "incident_commander",
        "platform_engineer",
        "sre_lead",
        "operator",
        "oncall_engineer",
        "evaluation_owner",
        "release_owner",
    }
)


class ApprovalError(DomainError):
    """Base exception for P2 approval domain failures."""


class ApprovalNotFoundError(ApprovalError):
    def __init__(self, approval_id: uuid.UUID) -> None:
        self.approval_id = approval_id
        super().__init__(f"Approval request {approval_id} not found")


class InvalidApprovalTransitionError(ApprovalError):
    def __init__(self, current: ApprovalState, target: ApprovalState) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Illegal approval transition from {current} to {target}")


class ApprovalExpiredError(ApprovalError):
    def __init__(self, approval_id: uuid.UUID) -> None:
        super().__init__(f"Approval request {approval_id} has expired")


class ApprovalRevokedError(ApprovalError):
    def __init__(self, approval_id: uuid.UUID) -> None:
        super().__init__(f"Approval request {approval_id} has been revoked")


class ApprovalRoleForbiddenError(ApprovalError):
    def __init__(self, role: str) -> None:
        self.role = role
        super().__init__(f"Caller role '{role}' is forbidden from approving incident actions")


class ApprovalVersionMismatchError(ApprovalError):
    def __init__(self, expected: str, observed: str) -> None:
        super().__init__(f"Target version mismatch: expected '{expected}', found '{observed}'")


class ApprovalDigestMismatchError(ApprovalError):
    def __init__(self, expected: str, observed: str) -> None:
        super().__init__(f"Plan digest mismatch: expected '{expected}', found '{observed}'")


class StaleContextError(ApprovalError):
    def __init__(self, expected_version: int, current_version: int) -> None:
        super().__init__(
            f"Stale incident context: expected status_version {expected_version}, current {current_version}"
        )


def validate_approval_transition(current: ApprovalState, target: ApprovalState) -> bool:
    """Validate P2 approval state transition. Returns True if valid, False on idempotent no-op."""
    if current == target:
        return False
    allowed = ALLOWED_APPROVAL_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidApprovalTransitionError(current, target)
    return True


def validate_reviewer_role(role: str) -> str:
    """Validate reviewer role is permitted human operator and non-model."""
    norm = role.strip().lower()
    if not norm or norm in FORBIDDEN_APPROVAL_ROLES or norm not in ALLOWED_APPROVAL_ROLES:
        raise ApprovalRoleForbiddenError(role)
    return norm


class ApprovalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(..., min_length=1, max_length=128)
    canonical_plan_digest: str = Field(..., min_length=64, max_length=64)
    action_digest: str = Field(..., min_length=64, max_length=64)
    action_payload: dict[str, Any] = Field(default_factory=dict)
    target_id: str = Field(..., min_length=1, max_length=128)
    expected_resource_version: str = Field(..., min_length=1, max_length=64)
    policy_version: str = Field(default="v1", min_length=1, max_length=64)
    step_id: uuid.UUID | None = Field(default=None)
    ttl_seconds: int = Field(default=900, ge=60, le=86400)


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer_id: str = Field(..., min_length=1, max_length=128)
    reviewer_role: str = Field(..., min_length=1, max_length=64)
    reason: str = Field(default="", max_length=1000)
    expected_incident_status_version: int | None = Field(default=None)

    def __init__(self, **data: Any) -> None:
        if "reviewer_role" in data:
            validate_reviewer_role(data["reviewer_role"])
        super().__init__(**data)


class ApprovalRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer_id: str = Field(..., min_length=1, max_length=128)
    reviewer_role: str = Field(..., min_length=1, max_length=64)
    reason: str = Field(default="", max_length=1000)

    def __init__(self, **data: Any) -> None:
        if "reviewer_role" in data:
            validate_reviewer_role(data["reviewer_role"])
        super().__init__(**data)


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    tenant_id: uuid.UUID
    incident_id: uuid.UUID
    step_id: uuid.UUID | None
    plan_id: str
    canonical_plan_digest: str
    action_digest: str
    action_payload: dict[str, Any]
    target_id: str
    expected_resource_version: str
    policy_version: str
    state: ApprovalState
    reviewer_id: str | None = None
    reviewer_role: str | None = None
    expires_at: datetime
    decision_at: datetime | None = None
    reason: str | None = None
    dispatch_lease_id: uuid.UUID | None = None
    dispatch_lease_expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
