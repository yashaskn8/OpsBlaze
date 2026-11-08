"""B1 Durable Incident Workflow — domain state machine, canonical hashing, and Pydantic contracts."""

import hashlib
import json
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IncidentStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    DISPATCH_PENDING = "DISPATCH_PENDING"
    INVESTIGATING = "INVESTIGATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES: frozenset[IncidentStatus] = frozenset(
    {
        IncidentStatus.VERIFIED,
        IncidentStatus.FAILED,
        IncidentStatus.NEEDS_REVIEW,
        IncidentStatus.CANCELLED,
    }
)

CANCELLABLE_STATUSES: frozenset[IncidentStatus] = frozenset(
    {
        IncidentStatus.ACCEPTED,
        IncidentStatus.DISPATCH_PENDING,
        IncidentStatus.INVESTIGATING,
        IncidentStatus.AWAITING_APPROVAL,
    }
)

# Legal state transitions for B1 lifecycle.
ALLOWED_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.ACCEPTED: frozenset(
        {
            IncidentStatus.DISPATCH_PENDING,
            IncidentStatus.CANCELLED,
        }
    ),
    IncidentStatus.DISPATCH_PENDING: frozenset(
        {
            IncidentStatus.INVESTIGATING,
            IncidentStatus.FAILED,
            IncidentStatus.CANCELLED,
        }
    ),
    IncidentStatus.INVESTIGATING: frozenset(
        {
            IncidentStatus.AWAITING_APPROVAL,
            IncidentStatus.FAILED,
            IncidentStatus.NEEDS_REVIEW,
            IncidentStatus.CANCELLED,
        }
    ),
    IncidentStatus.AWAITING_APPROVAL: frozenset(
        {
            IncidentStatus.EXECUTING,
            IncidentStatus.FAILED,
            IncidentStatus.NEEDS_REVIEW,
            IncidentStatus.CANCELLED,
        }
    ),
    IncidentStatus.EXECUTING: frozenset(
        {
            IncidentStatus.VERIFYING,
            IncidentStatus.FAILED,
            IncidentStatus.NEEDS_REVIEW,
        }
    ),
    IncidentStatus.VERIFYING: frozenset(
        {
            IncidentStatus.VERIFIED,
            IncidentStatus.FAILED,
            IncidentStatus.NEEDS_REVIEW,
        }
    ),
    IncidentStatus.VERIFIED: frozenset(),
    IncidentStatus.FAILED: frozenset(),
    IncidentStatus.NEEDS_REVIEW: frozenset(),
    IncidentStatus.CANCELLED: frozenset(),
}


class DomainError(Exception):
    """Base class for deterministic B1 domain errors."""


class DegradedRecoveryModeError(DomainError):
    """Raised when a consequential mutation or dispatch is attempted during degraded recovery mode."""

    def __init__(self, reason: str = "System is in degraded recovery mode") -> None:
        self.reason = reason
        super().__init__(f"Degraded recovery mode active: {reason}")


class DegradedRecoveryGate:
    """Process-safe fail-closed recovery gate blocking new consequential mutations during recovery."""

    _degraded: bool = False
    _reason: str = ""

    @classmethod
    def enter_degraded_mode(cls, reason: str = "cross_store_recovery") -> None:
        cls._degraded = True
        cls._reason = reason

    @classmethod
    def exit_degraded_mode(cls) -> None:
        cls._degraded = False
        cls._reason = ""

    @classmethod
    def is_degraded(cls) -> bool:
        return cls._degraded

    @classmethod
    def reason(cls) -> str:
        return cls._reason

    @classmethod
    def check_mutation_allowed(cls) -> None:
        if cls._degraded:
            raise DegradedRecoveryModeError(cls._reason)


class InvalidStateTransitionError(DomainError):
    def __init__(self, current: IncidentStatus, target: IncidentStatus, reason: str = "") -> None:
        self.current = current
        self.target = target
        msg = f"Illegal state transition from {current} to {target}"
        if reason:
            msg = f"{msg}: {reason}"
        super().__init__(msg)


class StaleStateVersionError(DomainError):
    def __init__(self, current_version: int, expected_version: int) -> None:
        self.current_version = current_version
        self.expected_version = expected_version
        super().__init__(
            f"Stale status_version: expected {expected_version}, found {current_version}"
        )


class IdempotencyConflictError(DomainError):
    def __init__(self, client_key: str) -> None:
        self.client_key = client_key
        super().__init__(
            f"Idempotency key '{client_key}' already exists with a different request payload"
        )


class IncidentNotFoundError(DomainError):
    def __init__(self, incident_id: uuid.UUID) -> None:
        self.incident_id = incident_id
        super().__init__(f"Incident {incident_id} not found")


def validate_transition(
    current_status: IncidentStatus,
    target_status: IncidentStatus,
    current_version: int,
    expected_version: int | None = None,
) -> bool:
    """Validate a B1 state transition and version guard.

    Returns True if a state transition should be applied, or False if it is an idempotent
    same-state retry (where current_status == target_status and expected_version matches
    either current_version or current_version - 1 from an already-applied retry).
    """
    if current_version < 1:
        raise StaleStateVersionError(current_version=current_version, expected_version=1)
    if expected_version is not None and expected_version < 1:
        raise StaleStateVersionError(
            current_version=current_version, expected_version=expected_version
        )

    # Idempotent same-state retry handling for non-terminal or terminal states:
    if current_status == target_status:
        if expected_version is None or expected_version in (current_version, current_version - 1):
            return False
        raise StaleStateVersionError(
            current_version=current_version, expected_version=expected_version
        )

    if expected_version is not None and current_version != expected_version:
        raise StaleStateVersionError(
            current_version=current_version, expected_version=expected_version
        )

    if current_status in TERMINAL_STATUSES:
        raise InvalidStateTransitionError(
            current_status, target_status, "cannot transition out of terminal state"
        )

    allowed = ALLOWED_TRANSITIONS.get(current_status, frozenset())
    if target_status not in allowed:
        raise InvalidStateTransitionError(current_status, target_status)

    return True


def make_incident_workflow_id(tenant_id: uuid.UUID, incident_id: uuid.UUID) -> str:
    """Return the deterministic Temporal workflow ID for a tenant-scoped incident."""
    return f"incident:{tenant_id}:{incident_id}"


def _normalize_canonical_value(value: Any) -> Any:
    """Recursively normalize strings (NFC), sort dictionary keys, and omit None values."""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        normalized_dict: dict[str, Any] = {}
        for k, v in value.items():
            if v is None:
                continue
            norm_key = unicodedata.normalize("NFC", str(k))
            normalized_dict[norm_key] = _normalize_canonical_value(v)
        return {k: normalized_dict[k] for k in sorted(normalized_dict.keys())}
    if isinstance(value, list):
        return [_normalize_canonical_value(item) for item in value]
    return value


def canonical_request_hash(payload: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 hex digest of a normalized request payload."""
    normalized = _normalize_canonical_value(payload)
    canonical_json = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def validate_client_key(key: str) -> str:
    """Normalize and validate an idempotency client key."""
    if not isinstance(key, str):
        raise TypeError("Idempotency key must be a string")
    normalized = unicodedata.normalize("NFC", key).strip()
    if not normalized:
        raise ValueError("Idempotency key must not be empty or whitespace")
    if len(normalized) > 128:
        raise ValueError("Idempotency key must be at most 128 characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized):
        raise ValueError("Idempotency key must not contain control characters")
    return normalized


class IncidentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str = Field(..., min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=256)
    severity: Literal["SEV1", "SEV2", "SEV3", "SEV4"] = Field(default="SEV2")
    description: str | None = Field(default=None, max_length=2000)
    labels: dict[str, str] | None = Field(default=None)

    @field_validator("target_id")
    @classmethod
    def _validate_target_id(cls, v: str) -> str:
        norm = unicodedata.normalize("NFC", v).strip()
        if not norm:
            raise ValueError("target_id must not be empty or whitespace")
        if any(ord(c) < 32 or ord(c) == 127 for c in norm):
            raise ValueError("target_id must not contain control characters")
        return norm

    @field_validator("title", "description")
    @classmethod
    def _normalize_optional_text(cls, v: str | None) -> str | None:
        if v is None:
            return None
        norm = unicodedata.normalize("NFC", v).strip()
        return norm if norm else None

    @field_validator("labels")
    @classmethod
    def _validate_labels(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        if len(v) > 20:
            raise ValueError("labels may contain at most 20 entries")
        cleaned: dict[str, str] = {}
        for k, val in v.items():
            nk = unicodedata.normalize("NFC", str(k)).strip()
            nv = unicodedata.normalize("NFC", str(val)).strip()
            if not nk or len(nk) > 64 or len(nv) > 256:
                raise ValueError("invalid label key or value length")
            cleaned[nk] = nv
        return cleaned if cleaned else None

    def to_canonical_payload(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "title": self.title,
            "severity": self.severity,
            "description": self.description,
            "labels": self.labels,
        }


class IncidentCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=512)


class InvestigationObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    probe_id: str
    target_id: str
    finding_code: str
    summary: str
    read_only: Literal[True] = True
    deterministic_digest: str


class InvestigationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str
    tenant_id: str
    target_id: str
    hypothesis: str
    recommended_status: IncidentStatus
    requires_human_approval: Literal[True] = True
    observations: list[InvestigationObservation]


class IncidentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    tenant_id: uuid.UUID
    client_key: str
    target_id: str
    request_sha256: str
    status: IncidentStatus
    status_version: int
    workflow_id: str
    created_at: datetime
    updated_at: datetime
    outbox_delivered: bool
    outbox_attempts: int
    projection_lag_ms: float
    projection_stale: bool = False
    investigation: InvestigationResult | None = None


@dataclass(frozen=True)
class WorkflowIncidentInput:
    """Small, typed, serializable input for Temporal IncidentWorkflow."""

    tenant_id: str
    incident_id: str
    target_id: str
    request_sha256: str
    correlation_id: str = ""
    adaptive_investigation: bool = False


@dataclass(frozen=True)
class StatusProjectionActivityInput:
    tenant_id: str
    incident_id: str
    target_status: str
    expected_version: int | None = None
    correlation_id: str = ""


@dataclass(frozen=True)
class StatusProjectionActivityOutput:
    incident_id: str
    status: str
    status_version: int
    cancelled: bool = False


@dataclass(frozen=True)
class ApprovedExecutionSignalInput:
    """Serializable Temporal signal/activity input for Stage 3 approved action execution."""

    approval_id: str
    tenant_id: str
    incident_id: str
    target_id: str
    adapter_id: str
    plan_digest: str
    action_kind: str
    action_digest: str
    expected_version: str
    parameters: dict[str, Any] = field(default_factory=dict)
    approved_by_role: str = "incident_commander"
    execution_mode: str = "SIMULATOR_APPROVED"
    expires_at_iso: str = ""
    correlation_id: str = ""


@dataclass(frozen=True)
class Stage3DispatchActivityOutput:
    receipt_id: str
    effect_state: str
    reservation_id: str
    fence: int
    adapter_id: str
    observed_version: str = ""
    error_detail: str = ""


@dataclass(frozen=True)
class Stage3VerifyActivityOutput:
    receipt_id: str
    effect_state: str
    final_incident_status: str
    observed_version: str = ""
    detail: str = ""
