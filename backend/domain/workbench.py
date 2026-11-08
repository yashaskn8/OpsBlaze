"""Domain models and Pydantic schemas for Feature P1 — Live Investigation Workbench."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.domain.approval import ApprovalResponse
from backend.domain.incident import IncidentResponse


class HypothesisItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str
    title: str
    confidence: float
    status: str
    rationale: str
    rank: int


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    tool_call_id: uuid.UUID | None
    source: str
    source_version: str
    target_id: str
    target_version: str
    observed_at: datetime
    captured_at: datetime
    sha256_digest: str
    raw_payload_redacted: dict[str, Any]
    classification: str
    validity_status: str


class InvestigationStepItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    step_no: int
    loop_state: str
    prompt_digest: str
    token_count: int
    abstention_reason: str | None = None
    created_at: datetime


class ToolCallItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    step_no: int
    tool_id: str
    tool_version: str
    target_id: str
    status: str
    duration_ms: float
    args_digest: str
    output_digest: str | None = None
    error_detail: str | None = None
    created_at: datetime


class BudgetHeadroom(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reserved_tokens: int = 0
    settled_tokens: int = 0
    reserved_probes: int = 0
    settled_probes: int = 0
    reserved_cost_micros: int = 0
    settled_cost_micros: int = 0
    max_steps: int = 10
    current_step: int = 0


class AbstentionDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    abstained: bool
    reason: str | None = None
    confidence: float = 0.0
    missing_evidence_types: list[str] = Field(default_factory=list)


class WorkbenchSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident: IncidentResponse
    hypotheses: list[HypothesisItem] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    steps: list[InvestigationStepItem] = Field(default_factory=list)
    tool_calls: list[ToolCallItem] = Field(default_factory=list)
    budget: BudgetHeadroom = Field(default_factory=BudgetHeadroom)
    active_approval: ApprovalResponse | None = None
    abstention: AbstentionDetail
    projection_lag_ms: float = 0.0
    etag: str
    outcome_label: str | None = None
    outcome_revision_no: int | None = None
    outcome_is_disputed: bool = False
