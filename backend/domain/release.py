"""Feature E6 — Versioned Policy Canary, Release Control & Automated Rollback Domain.

Domain contracts, state transitions, bundle integrity, canonicalization, and deterministic cohort assignment.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.domain.incident import DomainError

# ==============================================================================
# Enums
# ==============================================================================

class ReleaseState(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    ACTIVE = "ACTIVE"
    ROLLED_BACK = "ROLLED_BACK"
    REVOKED = "REVOKED"


class BundleStatus(StrEnum):
    DRAFT = "DRAFT"
    SEALED = "SEALED"
    ARCHIVED = "ARCHIVED"


class CanaryArm(StrEnum):
    INCUMBENT = "INCUMBENT"
    CANDIDATE = "CANDIDATE"


class ReleaseEventType(StrEnum):
    DRAFT_CREATED = "DRAFT_CREATED"
    SUBMITTED_FOR_APPROVAL = "SUBMITTED_FOR_APPROVAL"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    SHADOW_ACTIVATED = "SHADOW_ACTIVATED"
    CANARY_ACTIVATED = "CANARY_ACTIVATED"
    CANARY_PERCENTAGE_UPDATED = "CANARY_PERCENTAGE_UPDATED"
    ACTIVE_PROMOTED = "ACTIVE_PROMOTED"
    ROLLBACK_TRIGGERED = "ROLLBACK_TRIGGERED"
    KILL_SWITCH_TRIPPED = "KILL_SWITCH_TRIPPED"


# ==============================================================================
# State Transitions & Constants
# ==============================================================================

ALLOWED_RELEASE_TRANSITIONS: dict[ReleaseState, frozenset[ReleaseState]] = {
    ReleaseState.DRAFT: frozenset({ReleaseState.PENDING_APPROVAL, ReleaseState.REVOKED}),
    ReleaseState.PENDING_APPROVAL: frozenset({ReleaseState.APPROVED, ReleaseState.REVOKED}),
    ReleaseState.APPROVED: frozenset({ReleaseState.SHADOW, ReleaseState.CANARY, ReleaseState.REVOKED}),
    ReleaseState.SHADOW: frozenset({ReleaseState.CANARY, ReleaseState.ROLLED_BACK, ReleaseState.REVOKED}),
    ReleaseState.CANARY: frozenset({ReleaseState.ACTIVE, ReleaseState.ROLLED_BACK}),
    ReleaseState.ACTIVE: frozenset({ReleaseState.ROLLED_BACK}),
    ReleaseState.ROLLED_BACK: frozenset(),
    ReleaseState.REVOKED: frozenset(),
}

ACTIVE_RELEASE_STATES: frozenset[ReleaseState] = frozenset(
    {ReleaseState.SHADOW, ReleaseState.CANARY, ReleaseState.ACTIVE}
)

TERMINAL_RELEASE_STATES: frozenset[ReleaseState] = frozenset(
    {ReleaseState.ROLLED_BACK, ReleaseState.REVOKED}
)


# ==============================================================================
# Domain Exceptions
# ==============================================================================

class ReleaseDomainError(DomainError):
    """Base domain error for E6 Release Control."""


class ReleaseNotFoundError(ReleaseDomainError):
    def __init__(self, release_id: uuid.UUID) -> None:
        super().__init__(f"Policy release {release_id} not found.")


class BundleNotFoundError(ReleaseDomainError):
    def __init__(self, bundle_id: uuid.UUID) -> None:
        super().__init__(f"Policy bundle {bundle_id} not found.")


class InvalidReleaseTransitionError(ReleaseDomainError):
    def __init__(self, current_state: ReleaseState, target_state: ReleaseState) -> None:
        super().__init__(
            f"Cannot transition release from {current_state.value} to {target_state.value}."
        )


class BundleNotSealedError(ReleaseDomainError):
    def __init__(self, bundle_id: uuid.UUID, status: str) -> None:
        super().__init__(
            f"Policy bundle {bundle_id} is in status '{status}'; only SEALED bundles may be released."
        )


class BundleDigestMismatchError(ReleaseDomainError):
    def __init__(self, computed: str, declared: str) -> None:
        super().__init__(
            f"Policy bundle content digest mismatch: computed {computed} != declared {declared}."
        )


class BundleSignatureInvalidError(ReleaseDomainError):
    def __init__(self, bundle_id: uuid.UUID) -> None:
        super().__init__(f"Cryptographic signature verification failed for policy bundle {bundle_id}.")


class E5EligibilityInvalidatedError(ReleaseDomainError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"Release blocked: candidate E5 eligibility is invalid: {reason}.")


class ReleaseSeparationOfDutiesError(ReleaseDomainError):
    def __init__(self, drafter_id: str, reviewer_id: str) -> None:
        super().__init__(
            f"Separation of duties violation: reviewer '{reviewer_id}' cannot be the same as drafter '{drafter_id}'."
        )


class ReleaseKillSwitchActiveError(ReleaseDomainError):
    def __init__(self, release_id: uuid.UUID) -> None:
        super().__init__(f"Operation blocked: kill switch is active on release {release_id}.")


class ActiveReleaseConflictError(ReleaseDomainError):
    def __init__(self, target_component: str, existing_release_id: uuid.UUID) -> None:
        super().__init__(
            f"Cannot activate release: target component '{target_component}' already has an active release {existing_release_id}."
        )


class RolloutPercentageOutOfBoundsError(ReleaseDomainError):
    def __init__(self, percentage: int) -> None:
        super().__init__(f"Canary percentage {percentage} out of bounds (must be 0..100).")


class StaleVersionConflictError(ReleaseDomainError):
    def __init__(self, release_id: uuid.UUID, current_version: int, expected_version: int) -> None:
        super().__init__(
            f"Version conflict on release {release_id}: current version {current_version} != expected {expected_version}."
        )


# ==============================================================================
# Canonicalization and Cryptographic Primitives
# ==============================================================================

def canonicalize_bundle_payload(data: dict[str, Any]) -> bytes:
    """RFC 8785 JSON Canonicalization Scheme (JCS) representation.

    Ensures UTF-8 encoding, deterministic key ordering, minimal whitespace,
    and consistent representation across platforms.
    """
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_bundle_digest(canonical_payload_bytes: bytes) -> str:
    """Compute SHA-256 digest of canonicalized bundle payload."""
    return hashlib.sha256(canonical_payload_bytes).hexdigest()


def compute_bundle_signature(digest: str, secret: str, key_id: str) -> str:
    """Compute HMAC-SHA256 signature over bundle digest with domain separator."""
    message = f"opsblaze:policy_bundle:v1:{key_id}:{digest}".encode()
    key = secret.encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_bundle_signature(digest: str, signature: str, secret: str, key_id: str) -> bool:
    """Constant-time verification of HMAC-SHA256 bundle signature."""
    expected_sig = compute_bundle_signature(digest, secret, key_id)
    return hmac.compare_digest(expected_sig, signature)


# ==============================================================================
# Deterministic Cohort Assignment
# ==============================================================================

def assign_incident_cohort(
    *,
    tenant_id: uuid.UUID,
    incident_id: uuid.UUID,
    target_id: str,
    release: Any | None,
    default_incumbent_bundle_id: uuid.UUID,
) -> tuple[CanaryArm, uuid.UUID, int | None]:
    """Deterministically assign an incident to INCUMBENT or CANDIDATE arm.

    Uses canonical seed string: '{tenant_id}:{target_id}:{release.rollout_salt}:{incident_id}'
    Computes SHA-256 hash value modulo 100 (0..99).
    If hash_val < release.canary_percentage -> CANDIDATE
    Else -> INCUMBENT.
    """
    if release is None or getattr(release, "kill_switch_active", False):
        return (CanaryArm.INCUMBENT, default_incumbent_bundle_id, None)

    state = getattr(release, "state", None)
    if isinstance(state, str):
        try:
            state = ReleaseState(state)
        except ValueError:
            state = None

    if state not in ACTIVE_RELEASE_STATES:
        return (CanaryArm.INCUMBENT, default_incumbent_bundle_id, None)

    if state == ReleaseState.SHADOW:
        # In shadow mode, live incident is served by incumbent; candidate runs asynchronously in shadow
        return (CanaryArm.INCUMBENT, release.incumbent_bundle_id, 0)

    if state == ReleaseState.ACTIVE:
        # 100% routed to candidate
        return (CanaryArm.CANDIDATE, release.bundle_id, 100)

    # CANARY mode: deterministic hash partitioning
    rollout_salt = getattr(release, "rollout_salt", "default-salt")
    seed_str = f"{tenant_id}:{target_id}:{rollout_salt}:{incident_id}"
    hash_val = int(hashlib.sha256(seed_str.encode("utf-8")).hexdigest()[:8], 16) % 100

    canary_pct = getattr(release, "canary_percentage", 0)
    if hash_val < canary_pct:
        return (CanaryArm.CANDIDATE, release.bundle_id, hash_val)
    return (CanaryArm.INCUMBENT, release.incumbent_bundle_id, hash_val)


# ==============================================================================
# Pydantic Schemas
# ==============================================================================

class PolicyBundleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle_name: str = Field(..., min_length=3, max_length=128)
    base_policy_version: str = Field(..., max_length=64)
    target_component: str = Field(..., max_length=64)
    proposal_diff: dict[str, Any] | None = None
    prompt_templates: dict[str, Any] | None = None
    active_lesson_ids: list[str] | None = None
    model_id: str = Field(..., max_length=64)
    model_version: str = Field(..., max_length=64)
    tool_catalog_version: str = Field(..., max_length=64)
    a2_grant_version: str = Field(..., max_length=64)
    source_manifest_id: uuid.UUID | None = None
    source_report_id: uuid.UUID | None = None
    source_provenance_digest: str = Field(..., max_length=64)


class PolicyBundleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    version: int
    bundle_name: str
    bundle_sha256: str
    signature: str
    signature_key_id: str
    base_policy_version: str
    target_component: str
    proposal_diff: dict[str, Any] | None
    prompt_templates: dict[str, Any] | None
    active_lesson_ids: list[str] | None
    model_id: str
    model_version: str
    tool_catalog_version: str
    a2_grant_version: str
    source_manifest_id: uuid.UUID | None
    source_report_id: uuid.UUID | None
    source_provenance_digest: str
    status: str
    created_by: str
    created_at: datetime
    sealed_at: datetime | None


class PolicyReleaseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_component: str = Field(..., min_length=1, max_length=64)
    bundle_id: uuid.UUID
    incumbent_bundle_id: uuid.UUID
    rollout_salt: str | None = Field(None, max_length=64)


class ReleaseTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=1)
    target_state: ReleaseState
    canary_percentage: int | None = Field(None, ge=0, le=100)
    comments: str | None = Field(None, max_length=2000)


class ReleaseReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=1)
    decision: str = Field(..., pattern="^(APPROVED|REJECTED)$")
    decision_comments: str | None = Field(None, max_length=2000)


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=1)
    rollback_reason: str = Field(..., min_length=3, max_length=1024)


class KillSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=1)
    justification: str = Field(..., min_length=5, max_length=1024)


class PolicyReleaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    version: int
    target_component: str
    bundle_id: uuid.UUID
    incumbent_bundle_id: uuid.UUID
    state: str
    canary_percentage: int
    rollout_salt: str
    drafter_id: str
    reviewer_id: str | None
    reviewer_role: str | None
    decision_comments: str | None
    kill_switch_active: bool
    kill_switch_triggered_by: str | None
    kill_switch_triggered_at: datetime | None
    rollback_reason: str | None
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None
    activated_at: datetime | None
    rolled_back_at: datetime | None


class ReleaseAuditEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    release_id: uuid.UUID
    event_type: str
    from_state: str | None
    to_state: str
    actor_id: str
    actor_role: str
    metadata_redacted: dict[str, Any]
    created_at: datetime


class CanaryMetricsResponse(BaseModel):
    release_id: uuid.UUID
    target_component: str
    state: str
    canary_percentage: int
    incumbent_sample_size: int
    candidate_sample_size: int
    incumbent_error_rate: float
    candidate_error_rate: float
    relative_risk: float | None
    invariant_breach_count: int
    recommendation: str
    last_breach_detail: str | None = None
