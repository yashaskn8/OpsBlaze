"""Domain contracts, enums, and validators for E3 Failure Taxonomy & Proposals."""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class FailureOriginCode(str, Enum):
    """Categorized origin of an incident failure or uncertainty."""

    VERIFIED_FAILURE = "VERIFIED_FAILURE"
    DISPUTED_OUTCOME = "DISPUTED_OUTCOME"
    UNCERTAIN_EVIDENCE = "UNCERTAIN_EVIDENCE"
    TOOL_AUTHORIZATION_DENIAL = "TOOL_AUTHORIZATION_DENIAL"
    TOOL_VALIDATION_FAILURE = "TOOL_VALIDATION_FAILURE"
    EVIDENCE_PROVENANCE_FAILURE = "EVIDENCE_PROVENANCE_FAILURE"
    RESOURCE_FENCING_FAILURE = "RESOURCE_FENCING_FAILURE"
    EFFECT_RECONCILIATION_UNKNOWN = "EFFECT_RECONCILIATION_UNKNOWN"
    WORKFLOW_RECOVERY_FAILURE = "WORKFLOW_RECOVERY_FAILURE"
    BUDGET_ADMISSION_CONSTRAINT = "BUDGET_ADMISSION_CONSTRAINT"
    INVESTIGATION_PLANNING_FAILURE = "INVESTIGATION_PLANNING_FAILURE"
    UNCLASSIFIED = "UNCLASSIFIED"


class FailureClassificationStatus(str, Enum):
    """Lifecycle state of a failure case."""

    DETECTED = "DETECTED"
    CLASSIFIED = "CLASSIFIED"
    UNCERTAIN = "UNCERTAIN"
    CLUSTERED = "CLUSTERED"
    IGNORED = "IGNORED"


class FailureClusterStatus(str, Enum):
    """Lifecycle state of a failure cluster."""

    ACTIVE = "ACTIVE"
    QUARANTINED = "QUARANTINED"
    RESOLVED = "RESOLVED"
    MERGED = "MERGED"


class DivergentStepCategory(str, Enum):
    """Standardized categories for the first divergent or failing step."""

    ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"
    PROBE_TIMEOUT = "PROBE_TIMEOUT"
    FENCE_REJECTED = "FENCE_REJECTED"
    PLAN_VALIDATION_FAILED = "PLAN_VALIDATION_FAILED"
    READBACK_MISMATCH = "READBACK_MISMATCH"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    UNKNOWN_CATEGORY = "UNKNOWN_CATEGORY"


class FailureCaseNotFoundError(ValueError):
    """Raised when a failure case cannot be found for tenant."""


class FailureClusterNotFoundError(ValueError):
    """Raised when a failure cluster cannot be found for tenant."""


class ProposalNotFoundError(ValueError):
    """Raised when an improvement proposal cannot be found for tenant."""


class ProposalValidationError(ValueError):
    """Raised when proposal fails schema or allowlist validation."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class ProposalConflictError(ValueError):
    """Raised on CAS version conflict or concurrent proposal modification."""


class ProposalLifecycleError(ValueError):
    """Raised when proposal transition violates allowed lifecycle state machine."""


class ProposalType(str, Enum):
    """Approved change classes for model/human improvement proposals."""

    PROMPT_REFINEMENT = "PROMPT_REFINEMENT"
    CONTEXT_WINDOW_BUDGET = "CONTEXT_WINDOW_BUDGET"
    TOOL_DESCRIPTION = "TOOL_DESCRIPTION"


class ProposalStatus(str, Enum):
    """Guarded lifecycle states for improvement proposals."""

    DRAFT = "DRAFT"
    PROPOSED = "PROPOSED"
    REVIEWED = "REVIEWED"
    EVALUATION_READY = "EVALUATION_READY"
    REJECTED = "REJECTED"


class ReviewDecision(str, Enum):
    """Human reviewer decision for an improvement proposal."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class TargetComponent(str, Enum):
    """Strict server-side allowlist of components targetable by proposals."""

    INVESTIGATOR_A1_PROMPT = "investigator.a1.prompt_template"
    TOOL_CATALOG_DESCRIPTION = "tools.catalog.descriptions"
    CONTEXT_WINDOW_BUDGET = "investigator.a1.budget_bounds"


# ---------------------------------------------------------------------------
# Strict Allowlist Pydantic Schemas for Structured Diffs
# ---------------------------------------------------------------------------


class PromptRefinementDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=3, max_length=128)
    target_section: str = Field(min_length=2, max_length=64)
    proposed_content: str = Field(min_length=5, max_length=2000)
    variable_deltas: dict[str, Any] = Field(default_factory=dict)


class BudgetTuningDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: str = Field(min_length=3, max_length=128)
    max_tokens: int = Field(ge=500, le=16000)
    max_steps: int = Field(ge=1, le=50)


class ToolDescriptionDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=2, max_length=64)
    proposed_description: str = Field(min_length=10, max_length=500)


# Forbidden pattern detector (defense-in-depth against code/SQL smuggling)
_FORBIDDEN_PATTERNS = [
    re.compile(
        r"\b(SELECT\s+.+\s+FROM|INSERT\s+INTO|DROP\s+(?:TABLE|DATABASE|INDEX|VIEW|SCHEMA)|ALTER\s+TABLE|DELETE\s+FROM|TRUNCATE\s+(?:TABLE)?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(def\s+\w+|class\s+\w+|import\s+\w+|from\s+\w+\s+import|lambda\s*[:\w]|__init__)\b"),
    re.compile(r"\b(chmod\s+|chown\s+|rm\s+-rf\b|sudo\s+|curl\s+|wget\s+|bash\s+|powershell\s+|cmd\s+)"),
    re.compile(r"\b(GRANT\s+.+\s+TO|REVOKE\s+.+\s+FROM)\b", re.IGNORECASE),
    re.compile(r"(\.py|\.ts|\.tsx|\.sql|\.sh|\.bat|\.ps1|Dockerfile|alembic/)", re.IGNORECASE),
]


class ProposalValidator:
    """Deterministic server-side validator enforcing strict proposal boundaries."""

    @classmethod
    def validate_proposal(
        cls,
        proposal_type: str,
        target_component: str,
        structured_diff: dict[str, Any],
    ) -> tuple[bool, str | None, str | None]:
        """Validate an improvement proposal against strict allowlists.

        Returns:
            (is_valid, rejection_code, rejection_reason)
        """
        # 1. Validate Target Component against strict server allowlist
        allowed_targets = {t.value for t in TargetComponent}
        if target_component not in allowed_targets:
            return (
                False,
                "REJECTED_FORBIDDEN_COMPONENT",
                f"Target component '{target_component}' is not in approved allowlist: {sorted(allowed_targets)}",
            )

        # 2. Validate Proposal Type mapping
        expected_components = {
            ProposalType.PROMPT_REFINEMENT.value: TargetComponent.INVESTIGATOR_A1_PROMPT.value,
            ProposalType.CONTEXT_WINDOW_BUDGET.value: TargetComponent.CONTEXT_WINDOW_BUDGET.value,
            ProposalType.TOOL_DESCRIPTION.value: TargetComponent.TOOL_CATALOG_DESCRIPTION.value,
        }
        if proposal_type not in expected_components:
            return (
                False,
                "REJECTED_FORBIDDEN_ACTION",
                f"Proposal type '{proposal_type}' is not supported.",
            )

        if expected_components[proposal_type] != target_component:
            return (
                False,
                "REJECTED_COMPONENT_MISMATCH",
                f"Proposal type '{proposal_type}' cannot target component '{target_component}'.",
            )

        # 3. Validate Structured Diff against typed schema with extra='forbid'
        try:
            if proposal_type == ProposalType.PROMPT_REFINEMENT.value:
                parsed = PromptRefinementDiff.model_validate(structured_diff)
                diff_text = f"{parsed.template_id} {parsed.target_section} {parsed.proposed_content} {json.dumps(parsed.variable_deltas)}"
            elif proposal_type == ProposalType.CONTEXT_WINDOW_BUDGET.value:
                BudgetTuningDiff.model_validate(structured_diff)
                diff_text = ""
            elif proposal_type == ProposalType.TOOL_DESCRIPTION.value:
                parsed_tool = ToolDescriptionDiff.model_validate(structured_diff)
                diff_text = f"{parsed_tool.tool_name} {parsed_tool.proposed_description}"
            else:
                return False, "REJECTED_FORBIDDEN_ACTION", f"Unknown proposal type {proposal_type}"
        except ValidationError as e:
            return (
                False,
                "REJECTED_MALFORMED_DIFF",
                f"Structured diff failed schema validation: {e.errors()}",
            )

        # 4. Scan content for forbidden code smuggling or SQL injection attempts
        for pat in _FORBIDDEN_PATTERNS:
            if pat.search(diff_text):
                return (
                    False,
                    "REJECTED_FORBIDDEN_ACTION",
                    f"Structured diff contains forbidden directive pattern: {pat.pattern}",
                )

        return True, None, None
