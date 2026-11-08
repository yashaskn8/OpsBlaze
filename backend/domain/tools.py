"""Feature A2 — Capability-Gated Read-Only Tools domain contracts and exceptions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from backend.domain.incident import DomainError


class ToolStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    REJECTED = "REJECTED"


class ToolError(DomainError):
    """Base domain exception for tool operations."""


class ToolSecurityError(ToolError):
    """Security or capability authorization violation."""


class CapabilityExpiredError(ToolSecurityError):
    """Capability grant has expired."""


class CapabilityRevokedError(ToolSecurityError):
    """Capability grant has been explicitly revoked."""


class ToolNotFoundError(ToolError):
    """Requested tool ID is not present in the catalog allowlist."""


class MutationToolRejectedError(ToolSecurityError):
    """Attempted to register or execute a non-read-only tool in A2."""


class OutputLimitExceededError(ToolError):
    """Tool output exceeds maximum allowable size bounds."""


class ToolDefinition(BaseModel):
    """Registered tool specification in ToolCatalog."""

    id: str
    version: str = "v1"
    input_schema_hash: str
    output_schema_hash: str
    read_only: bool = True
    max_cost_micros: int = 0
    description: str
    egress_allowlist: list[str] = Field(default_factory=list)


class CapabilityGrantSpec(BaseModel):
    """Authorized grant context minted by deterministic backend."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    incident_id: uuid.UUID
    tool_id: str
    tool_version: str = "v1"
    target_id: str
    granted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    revoked_at: datetime | None = None


class ToolCallRequest(BaseModel):
    """Typed invocation request submitted by the investigator."""

    tenant_id: uuid.UUID
    incident_id: uuid.UUID
    step_no: int
    grant_id: uuid.UUID
    tool_id: str
    tool_version: str = "v1"
    target_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolCallResult(BaseModel):
    """Result of a capability-gated tool execution."""

    call_id: uuid.UUID
    status: ToolStatus
    args_digest: str
    output_digest: str | None = None
    result_evidence_id: uuid.UUID | None = None
    payload: dict[str, Any] | None = None
    duration_ms: float = 0.0
    error_detail: str | None = None
