"""Domain contracts, models, and protocols for A1 Adaptive Probe Planner & Investigator."""

import uuid
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class InvestigationLoopState(StrEnum):
    """Investigation loop state progression."""

    INITIALIZING = "INITIALIZING"
    HYPOTHESIZING = "HYPOTHESIZING"
    PROBING = "PROBING"
    REASSESSING = "REASSESSING"
    PROPOSING = "PROPOSING"
    ABSTAINED = "ABSTAINED"
    COMPLETED = "COMPLETED"


class HypothesisItem(BaseModel):
    """Calibrated incident hypothesis with attributable confidence and evidence references."""

    id: str  # e.g. "H1", "H2"
    title: str
    probability: float  # Normalized between 0.0 and 1.0
    supporting_evidence_ids: list[uuid.UUID] = Field(default_factory=list)
    contradicting_evidence_ids: list[uuid.UUID] = Field(default_factory=list)
    rationale: str = ""


class ProbeSelection(BaseModel):
    """Bounded, discriminating read-only probe selected from the tool catalog."""

    tool_id: str
    target_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    estimated_tokens: int = 1500
    estimated_cost_micros: int = 50000


class ProposedPlan(BaseModel):
    """Typed repair plan proposed for human review in Stage 6 Cockpit."""

    target_id: str
    plan_digest: str
    action_digest: str
    expected_version: str
    action_kind: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    supporting_evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class InvestigationModelResponse(BaseModel):
    """Typed response received from an investigator model adapter."""

    hypotheses: list[HypothesisItem] = Field(default_factory=list)
    selected_probe: ProbeSelection | None = None
    proposed_plan: ProposedPlan | None = None
    rationale: str = ""
    tokens_used: int = 1000


class InvestigationResult(BaseModel):
    """Final outcome of an adaptive investigation loop."""

    model_config = ConfigDict(from_attributes=True)

    status: InvestigationLoopState
    is_proposed: bool
    is_abstained: bool
    proposed_plan: ProposedPlan | None = None
    abstention_reason: str | None = None
    steps_taken: int = 0
    hypotheses: list[HypothesisItem] = Field(default_factory=list)
    token_count_total: int = 0
    cost_micros_total: int = 0
    probes_executed: int = 0


class ModelAdapterProtocol(Protocol):
    """Protocol for model adapters evaluating context and proposing probes or plans."""

    async def analyze_context(
        self,
        incident_id: uuid.UUID,
        rendered_context: str,
        tool_catalog_summary: list[dict[str, Any]],
    ) -> InvestigationModelResponse:
        """Evaluate prompt context and return structured hypotheses and actions."""
        ...
