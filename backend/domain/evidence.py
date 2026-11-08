"""Domain models, enums, and contracts for A3 Versioned Evidence & Context Manifests."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EvidenceClassification(StrEnum):
    """Semantic classification of an evidence observation."""

    TELEMETRY = "TELEMETRY"
    LOG_SAMPLE = "LOG_SAMPLE"
    CONFIG_SNAPSHOT = "CONFIG_SNAPSHOT"
    RUNBOOK_EXTRACT = "RUNBOOK_EXTRACT"


class EvidenceValidity(StrEnum):
    """Validity lifecycle state of an evidence record."""

    VALID = "VALID"
    STALE = "STALE"
    CORRUPTED = "CORRUPTED"
    SUPERSEDED = "SUPERSEDED"
    UNAVAILABLE = "UNAVAILABLE"


class EvidenceRecord(BaseModel):
    """Immutable, versioned, attributable evidence observation."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    incident_id: uuid.UUID
    tool_call_id: uuid.UUID | None = None
    source: str
    source_version: str
    target_id: str
    target_version: str
    observed_at: datetime
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    sha256_digest: str
    raw_payload_redacted: dict[str, Any]
    classification: EvidenceClassification
    validity_status: EvidenceValidity = EvidenceValidity.VALID
    supersedes_id: uuid.UUID | None = None


class ContextManifest(BaseModel):
    """Versioned, digested prompt context manifest constructed from verified evidence."""

    prompt_digest: str
    evidence_ids_used: list[uuid.UUID]
    token_count: int
    runbook_revision: str | None = None
    rendered_context: str
