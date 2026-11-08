"""Domain models and exceptions for OpsBlaze Feature E2 — Outcome Adjudication."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.domain.approval import validate_reviewer_role
from backend.domain.incident import DomainError


class OutcomeLabel(str, Enum):
    """Authoritative outcome labels for an incident."""

    PENDING = "PENDING"
    OBSERVED = "OBSERVED"
    VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
    VERIFIED_FAILURE = "VERIFIED_FAILURE"
    DISPUTED = "DISPUTED"
    REVISED = "REVISED"
    UNKNOWN = "UNKNOWN"


class ConfidenceBand(str, Enum):
    """Confidence band for outcome classification."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class SourceKind(str, Enum):
    """Provenance source of an outcome observation."""

    INDEPENDENT_WORKLOAD_CHECK = "INDEPENDENT_WORKLOAD_CHECK"
    TARGET_READBACK = "TARGET_READBACK"
    DELAYED_MONITOR = "DELAYED_MONITOR"
    OPERATOR_FEEDBACK = "OPERATOR_FEEDBACK"
    ADAPTER_ACK = "ADAPTER_ACK"
    MODEL_ANNOTATION = "MODEL_ANNOTATION"


def compute_observation_digest(
    source_kind: str,
    source_reference: str,
    rule_version: str,
    result_payload: dict[str, Any],
) -> str:
    """Computes a deterministic SHA-256 digest over observation payload."""
    canonical = json.dumps(
        {
            "source_kind": source_kind,
            "source_reference": source_reference,
            "rule_version": rule_version,
            "result_payload": result_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ObservationCreateRequest(BaseModel):
    """Request to record an append-only outcome observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_kind: SourceKind
    source_reference: str = Field(..., min_length=1, max_length=256)
    rule_version: str = Field("v1", min_length=1, max_length=64)
    result_payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(..., min_length=1, max_length=128)
    observed_at: datetime | None = None


class ObservationResponse(BaseModel):
    """Public representation of an outcome observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    incident_id: uuid.UUID
    source_kind: SourceKind
    source_reference: str
    rule_version: str
    result_payload: dict[str, Any]
    source_digest: str
    idempotency_key: str
    observed_at: datetime
    created_at: datetime


class OutcomeRevisionResponse(BaseModel):
    """Public representation of an outcome adjudication revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    incident_id: uuid.UUID
    revision_no: int
    rule_version: str
    label: OutcomeLabel
    confidence_band: ConfidenceBand
    dispute_reason: str | None = None
    prior_revision_id: uuid.UUID | None = None
    reviewer_id: str | None = None
    reviewer_role: str | None = None
    supporting_observation_ids: list[str]
    adjudication_metadata: dict[str, Any]
    decided_at: datetime
    created_at: datetime


class OperatorFeedbackRequest(BaseModel):
    """Human reviewer feedback or dispute submission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: OutcomeLabel
    reason: str = Field(..., min_length=3, max_length=1024)
    actor_id: str = Field(..., min_length=1, max_length=128)
    actor_role: str = Field(..., min_length=1, max_length=64)
    expected_revision_no: int = Field(..., ge=0)

    def model_post_init(self, context: object, /) -> None:
        validate_reviewer_role(self.actor_role)


class OutcomeSummaryResponse(BaseModel):
    """Comprehensive outcome status for an incident."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: uuid.UUID
    tenant_id: uuid.UUID
    current_revision: OutcomeRevisionResponse | None
    is_disputed: bool
    total_observations: int
    observations: list[ObservationResponse]


# Domain Exceptions
class OutcomeError(DomainError):
    """Base error for outcome adjudication operations."""


class OutcomeNotFoundError(OutcomeError):
    """Raised when an outcome revision or observation is not found."""

    def __init__(self, incident_id: uuid.UUID) -> None:
        super().__init__(f"No outcome records found for incident {incident_id}")
        self.incident_id = incident_id


class OutcomeConflictError(OutcomeError):
    """Raised on CAS revision mismatch (concurrency conflict)."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(
            f"Outcome revision conflict: expected revision {expected}, but found {actual}"
        )
        self.expected = expected
        self.actual = actual


class InvalidOutcomeTransitionError(OutcomeError):
    """Raised when an illegal outcome transition is attempted."""

    def __init__(self, message: str) -> None:
        super().__init__(f"Invalid outcome transition: {message}")


class SelfGradingForbiddenError(OutcomeError):
    """Raised when an untrusted model attempts to mark its own action as success."""

    def __init__(self) -> None:
        super().__init__(
            "Agentic AI model self-grading is strictly prohibited. VERIFIED_SUCCESS requires independent black-box verification."
        )
