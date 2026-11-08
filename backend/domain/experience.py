"""Domain models and exceptions for OpsBlaze Feature E1 — Experience Capsule."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from backend.domain.approval import validate_reviewer_role
from backend.domain.incident import DomainError


class ExperienceState(str, Enum):
    """Lifecycle state of an experience capsule."""

    CAPTURED = "CAPTURED"
    ELIGIBLE = "ELIGIBLE"
    QUARANTINED = "QUARANTINED"
    TOMBSTONED = "TOMBSTONED"
    PURGED = "PURGED"


class ExperienceClassification(str, Enum):
    """Classification of the incident experience."""

    INVESTIGATION_SUCCESS = "INVESTIGATION_SUCCESS"
    INVESTIGATION_FAILURE = "INVESTIGATION_FAILURE"
    ABSTENTION = "ABSTENTION"
    CONSEQUENTIAL_ACTION = "CONSEQUENTIAL_ACTION"
    UNKNOWN = "UNKNOWN"


class ExperienceAuditAction(str, Enum):
    """Audit action for experience lifecycle changes."""

    CAPTURED = "CAPTURED"
    QUARANTINED = "QUARANTINED"
    TOMBSTONED = "TOMBSTONED"
    PURGED = "PURGED"
    CONSENT_REVOKED = "CONSENT_REVOKED"
    CONSENT_GRANTED = "CONSENT_GRANTED"


class ConsentStatusResponse(BaseModel):
    """Tenant experience consent status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: uuid.UUID
    current_epoch: int
    reuse_allowed: bool
    updated_at: datetime
    revoked_at: datetime | None = None


class ConsentUpdateRequest(BaseModel):
    """Request to update or revoke tenant experience consent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reuse_allowed: bool
    reason: str = Field(..., min_length=3, max_length=512)
    actor_id: str = Field(..., min_length=1, max_length=128)
    actor_role: str = Field(..., min_length=1, max_length=64)

    def model_post_init(self, context: object, /) -> None:
        validate_reviewer_role(self.actor_role)


class ExperienceCapsuleResponse(BaseModel):
    """Public representation of an experience capsule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    incident_id: uuid.UUID
    step_id: uuid.UUID | None = None
    schema_version: str
    consent_epoch: int
    policy_bundle_version: str
    model_id: str
    model_version: str
    evidence_manifest_sha256: str
    effect_receipt_id: uuid.UUID | None = None
    outcome_id: uuid.UUID | None = None
    outcome_revision_no: int | None = None
    redaction_version: str
    redacted_digest: str
    summary_markdown: str | None = None
    classification: ExperienceClassification
    lifecycle_state: ExperienceState
    occurred_at: datetime
    expires_at: datetime
    created_at: datetime


class ExperienceListResponse(BaseModel):
    """Paginated list of experience capsules."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[ExperienceCapsuleResponse]
    next_cursor: str | None = None
    total_eligible: int


class ExperienceQuarantineRequest(BaseModel):
    """Request to quarantine an experience capsule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(..., min_length=3, max_length=512)
    expected_lifecycle_state: ExperienceState = ExperienceState.ELIGIBLE
    actor_id: str = Field(..., min_length=1, max_length=128)
    actor_role: str = Field(..., min_length=1, max_length=64)

    def model_post_init(self, context: object, /) -> None:
        validate_reviewer_role(self.actor_role)


class ExperienceExportResponse(BaseModel):
    """Authorized tenant experience export."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: uuid.UUID
    exported_at: datetime
    consent_epoch: int
    count: int
    capsules: list[ExperienceCapsuleResponse]


# Domain Exceptions
class ExperienceError(DomainError):
    """Base error for experience capture operations."""


class ConsentDeniedError(ExperienceError):
    """Raised when experience reuse is denied for the tenant."""

    def __init__(self, tenant_id: uuid.UUID) -> None:
        super().__init__(f"Experience reuse consent is denied for tenant {tenant_id}")
        self.tenant_id = tenant_id


class CapsuleNotFoundError(ExperienceError):
    """Raised when an experience capsule is not found."""

    def __init__(self, capsule_id: uuid.UUID) -> None:
        super().__init__(f"Experience capsule {capsule_id} not found")
        self.capsule_id = capsule_id


class CapsuleStateConflictError(ExperienceError):
    """Raised when capsule lifecycle transition is invalid or CAS check fails."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"Capsule state conflict: expected '{expected}', found '{actual}'")
        self.expected = expected
        self.actual = actual


class CapsuleRedactionError(ExperienceError):
    """Raised when experience payload cannot be safely redacted or normalized."""

    def __init__(self, message: str) -> None:
        super().__init__(f"Capsule redaction failure: {message}")
