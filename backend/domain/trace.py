"""Domain models and Pydantic schemas for Feature P3 — Evidence-to-Effect Trace Explorer."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TraceNodeKind(str, Enum):
    INCIDENT = "INCIDENT"
    STEP = "STEP"
    TOOL_CALL = "TOOL_CALL"
    EVIDENCE = "EVIDENCE"
    PLAN = "PLAN"
    APPROVAL = "APPROVAL"
    EFFECT_RECEIPT = "EFFECT_RECEIPT"
    OUTCOME = "OUTCOME"
    EXPERIENCE = "EXPERIENCE"
    FAILURE_CASE = "FAILURE_CASE"
    FAILURE_CLUSTER = "FAILURE_CLUSTER"
    PROPOSAL = "PROPOSAL"
    LESSON = "LESSON"
    EVALUATION_MANIFEST = "EVALUATION_MANIFEST"
    EVALUATION_REPORT = "EVALUATION_REPORT"


class TraceRelation(str, Enum):
    INITIATED = "INITIATED"
    NEXT_STEP = "NEXT_STEP"
    INVOKED = "INVOKED"
    PRODUCED = "PRODUCED"
    USED_IN = "USED_IN"
    PROPOSED = "PROPOSED"
    REQUESTED_APPROVAL = "REQUESTED_APPROVAL"
    AUTHORIZED_EFFECT = "AUTHORIZED_EFFECT"
    RECONCILED = "RECONCILED"
    ADJUDICATED = "ADJUDICATED"
    CAPTURED_IN = "CAPTURED_IN"
    CLASSIFIED_AS = "CLASSIFIED_AS"
    CLUSTERED_IN = "CLUSTERED_IN"
    PROPOSED_FROM = "PROPOSED_FROM"
    DERIVED_LESSON = "DERIVED_LESSON"
    CITED_LESSON = "CITED_LESSON"
    EVALUATED_IN = "EVALUATED_IN"
    REPORTED_BY = "REPORTED_BY"


class TraceNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: TraceNodeKind
    label: str
    timestamp: datetime
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    warning: str | None = None


class TraceEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    relation: TraceRelation


class TraceGraphResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: uuid.UUID
    tenant_id: uuid.UUID
    root_node_id: str
    nodes: list[TraceNode]
    edges: list[TraceEdge]
    missing_links: list[str] = Field(default_factory=list)
    has_cycles: bool = False
