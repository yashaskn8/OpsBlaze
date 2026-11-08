"""SQLAlchemy 2.0 Declarative ORM models matching the PostgreSQL schema at b3_005_claim_payload."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
class Incident(Base):
    __tablename__ = "incident"
    __table_args__ = (
        UniqueConstraint("tenant_id", "client_key", name="ux_incident_key"),
        UniqueConstraint("tenant_id", "id", name="ux_incident_tenant_id"),
        Index("ix_incident_target_status", "tenant_id", "target_id", "status_projection"),
        Index("ix_incident_tenant_history", "tenant_id", text("created_at DESC"), "id"),
        Index("ix_incident_tenant_id", "tenant_id"),
        Index("idx_incident_pinned_bundle", "tenant_id", "pinned_policy_bundle_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    client_key: Mapped[str] = mapped_column(String, nullable=False)
    request_sha256: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    status_projection: Mapped[str] = mapped_column(String, nullable=False)
    status_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    pinned_policy_bundle_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("policy_bundle.id", ondelete="RESTRICT"), nullable=True
    )
    policy_release_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("policy_release.id", ondelete="RESTRICT"), nullable=True
    )
    canary_arm: Mapped[str | None] = mapped_column(String(32), nullable=True)
    assignment_hash_val: Mapped[int | None] = mapped_column(Integer, nullable=True)


outbox_id_seq = Sequence("outbox_id_seq")


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint("attempt >= 0", name="chk_outbox_attempt_nonnegative"),
        Index("ix_outbox_incident_id", "incident_id"),
        Index(
            "ix_outbox_pending",
            "available_at",
            "id",
            postgresql_where=text("delivered_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, outbox_id_seq, primary_key=True, server_default=outbox_id_seq.next_value()
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incident.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResourceEpoch(Base):
    """Monotonic per-target fencing epoch table for Feature B2."""

    __tablename__ = "resource_epoch"
    __table_args__ = (CheckConstraint("latest_fence >= 0", name="chk_epoch_fence_nonnegative"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    )
    target_id: Mapped[str] = mapped_column(String, primary_key=True, nullable=False)
    latest_fence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    version_seen: Mapped[str | None] = mapped_column(String, nullable=True)


class ResourceReservation(Base):
    """Version-fenced resource reservation table for Feature B2."""

    __tablename__ = "resource_reservation"
    __table_args__ = (
        CheckConstraint("fence > 0", name="chk_reservation_fence_positive"),
        CheckConstraint(
            "state IN ('RESERVED', 'DISPATCHED', 'RELEASED', 'EXPIRED', 'CANCELLED')",
            name="chk_reservation_state_valid",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "holder_incident_id"],
            ["incident.tenant_id", "incident.id"],
            name="fk_reservation_incident_tenant",
            ondelete="CASCADE",
        ),
        Index(
            "ux_active_reservation_per_target",
            "tenant_id",
            "target_id",
            unique=True,
            postgresql_where=text(
                "(state)::text = ANY ((ARRAY['RESERVED'::character varying, 'DISPATCHED'::character varying])::text[])"
            ),
        ),
        Index(
            "ix_reservation_active_expiry",
            "expires_at",
            postgresql_where=text(
                "(state)::text = ANY ((ARRAY['RESERVED'::character varying, 'DISPATCHED'::character varying])::text[])"
            ),
        ),
        Index("ix_reservation_tenant_target_fence", "tenant_id", "target_id", "fence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    holder_incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incident.id", ondelete="CASCADE"), nullable=False
    )
    action_digest: Mapped[str] = mapped_column(String, nullable=False)
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'RESERVED'::character varying")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
class EffectReceipt(Base):
    """Durable external effect receipt and reconciliation table for Feature B3."""

    __tablename__ = "effect_receipt"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="ux_effect_tenant_idempotency"),
        CheckConstraint(
            "state IN ('PREPARED', 'SENT', 'UNKNOWN', 'CONFIRMED', 'VERIFIED', 'FAILED', 'NEEDS_REVIEW')",
            name="chk_effect_receipt_state_valid",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incident.tenant_id", "incident.id"],
            name="fk_receipt_incident_tenant",
            ondelete="CASCADE",
        ),
        Index(
            "ix_effect_receipt_pending_recon",
            "state",
            "updated_at",
            postgresql_where=text(
                "(state)::text = ANY ((ARRAY['UNKNOWN'::character varying, 'SENT'::character varying])::text[])"
            ),
        ),
        Index("ix_effect_receipt_history", "tenant_id", "incident_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incident.id", ondelete="CASCADE"), nullable=False
    )
    plan_digest: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    adapter_id: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_version: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'PREPARED'::character varying")
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    observed_version: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    action_digest: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("''::character varying")
    )
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    action_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    adapter_digest: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("''::character varying")
    )
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdapterConformance(Base):
    """Durable E8 adapter conformance and capability enablement registry."""

    __tablename__ = "adapter_conformance"
    __table_args__ = (
        CheckConstraint(
            "status IN ('READ_ONLY_UNVERIFIED', 'CONFORMANT', 'QUARANTINED', 'REVOKED_DIGEST_MISMATCH', 'EXPIRED')",
            name="chk_adapter_conformance_status_valid",
        ),
        CheckConstraint(
            "retention_horizon_seconds >= 0",
            name="chk_adapter_retention_nonnegative",
        ),
    )

    adapter_id: Mapped[str] = mapped_column(String, primary_key=True, nullable=False)
    adapter_version: Mapped[str] = mapped_column(String, nullable=False)
    code_digest: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default=text("'READ_ONLY_UNVERIFIED'::character varying"),
    )
    retention_horizon_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("300")
    )
    capabilities_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    conformance_evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ToolCatalog(Base):
    """A2 Capability-Gated Read-Only Tool Catalog."""

    __tablename__ = "tool_catalog"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, nullable=False)
    version: Mapped[str] = mapped_column(String(32), primary_key=True, nullable=False)
    input_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    max_cost_micros: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    egress_allowlist: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
class CapabilityGrant(Base):
    """A2 Tenant and Target-Scoped Tool Capability Grant."""

    __tablename__ = "capability_grant"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incident.tenant_id", "incident.id"],
            name="fk_capability_grant_incident",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tool_id", "tool_version"],
            ["tool_catalog.id", "tool_catalog.version"],
            name="fk_capability_grant_tool",
            ondelete="RESTRICT",
        ),
        Index("ix_capability_grant_lookup", "tenant_id", "incident_id", "tool_id", "target_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tool_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'v1'"))
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ToolCall(Base):
    """A2 Auditable Tool Invocation Record."""

    __tablename__ = "tool_call"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incident.tenant_id", "incident.id"],
            name="fk_tool_call_incident",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["grant_id"],
            ["capability_grant.id"],
            name="fk_tool_call_grant",
            ondelete="RESTRICT",
        ),
        Index("ix_tool_call_incident", "tenant_id", "incident_id", "step_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    grant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tool_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'v1'"))
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    args_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    args_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    output_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_evidence_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0.0"))
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
class EvidenceLedger(Base):
    """A3 Versioned, Immutable, Attributable Evidence Record."""

    __tablename__ = "evidence_ledger"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incident.tenant_id", "incident.id"],
            name="fk_evidence_incident",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tool_call_id"],
            ["tool_call.id"],
            name="fk_evidence_tool_call",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["supersedes_id"],
            ["evidence_ledger.id"],
            name="fk_evidence_supersedes",
            ondelete="SET NULL",
        ),
        Index("ix_evidence_lookup", "tenant_id", "incident_id", "validity_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_version: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sha256_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    classification: Mapped[str] = mapped_column(String(32), nullable=False)
    validity_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'VALID'")
    )
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class InvestigationStep(Base):
    """A1 / A3 Unified Reasoning and Hypothesis Step Record."""

    __tablename__ = "investigation_step"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incident.tenant_id", "incident.id"],
            name="fk_investigation_step_incident",
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "incident_id", "step_no", name="uq_investigation_step"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    loop_state: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ids_used: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, server_default=text("'[]'::jsonb")
    )
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    hypotheses_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    selected_probe: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    proposed_plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    abstention_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
class BudgetLedger(Base):
    """B4 Durable Budget & Capacity Reservation Ledger."""

    __tablename__ = "budget_ledger"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incident.tenant_id", "incident.id"],
            name="fk_budget_ledger_incident",
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "incident_id", name="uq_budget_ledger_incident"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    settled_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    reserved_probes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    settled_probes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    reserved_cost_micros: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    settled_cost_micros: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("50000"))
    max_probes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("10"))
    max_cost_micros: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("10000000")
    )
    max_time_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("300")
    )
    reservation_nonce: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'ACTIVE'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ApprovalRequest(Base):
    """P2 Version-Bound Human Approval Request."""

    __tablename__ = "approval_request"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incident.tenant_id", "incident.id"],
            name="fk_approval_incident",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["step_id"],
            ["investigation_step.id"],
            name="fk_approval_step",
            ondelete="SET NULL",
        ),
        CheckConstraint(
            "state IN ('PENDING', 'APPROVED', 'DISPATCHING', 'REJECTED', 'REVOKED', 'INVALIDATED', 'EXPIRED', 'CONSUMED')",
            name="chk_approval_state",
        ),
        Index(
            "ux_active_approval_per_incident",
            "tenant_id",
            "incident_id",
            unique=True,
            postgresql_where=text("state IN ('PENDING', 'APPROVED', 'DISPATCHING')"),
        ),
        Index("ix_approval_lookup", "tenant_id", "incident_id", "state"),
        Index(
            "ix_approval_expiry",
            "state",
            "expires_at",
            postgresql_where=text("state IN ('PENDING', 'APPROVED', 'DISPATCHING')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    step_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    plan_id: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    action_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    action_payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, server_default=text("'{}'::jsonb")
    )
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    expected_resource_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'PENDING'")
    )
    reviewer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewer_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    dispatch_lease_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    dispatch_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ApprovalAudit(Base):
    """P2 Immutable Approval Decision Audit Log."""

    __tablename__ = "approval_audit"
    __table_args__ = (
        ForeignKeyConstraint(
            ["approval_id"],
            ["approval_request.id"],
            name="fk_approval_audit_approval",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "action IN ('CREATED', 'APPROVED', 'REJECTED', 'REVOKED', 'INVALIDATED', 'EXPIRED', 'DISPATCHED', 'CONSUMED')",
            name="chk_approval_audit_action",
        ),
        Index("ix_approval_audit_lookup", "tenant_id", "approval_id", "timestamp"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    approval_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, server_default=text("'{}'::jsonb")
    )


class EvaluationManifest(Base):
    """S1 Immutable Frozen Evaluation Experiment Manifest."""

    __tablename__ = "evaluation_manifest"
    __table_args__ = (
        CheckConstraint(
            "split IN ('DEV', 'VALIDATION', 'HOLDOUT', 'NEGATIVE_CONTROL')",
            name="chk_manifest_split",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    manifest_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    fixture_family: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    budget_config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    oracle_version: Mapped[str] = mapped_column(String(64), nullable=False)
    random_seed: Mapped[int] = mapped_column(Integer, nullable=False)
    split: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'DEV'")
    )
    frozen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvaluationTrial(Base):
    """S1 Paired Experiment Trial Execution Record."""

    __tablename__ = "evaluation_trial"
    __table_args__ = (
        ForeignKeyConstraint(
            ["manifest_id"],
            ["evaluation_manifest.id"],
            name="fk_eval_trial_manifest",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "manifest_id", "pair_id", "strategy_name", name="ux_trial_manifest_pair_strategy"
        ),
        CheckConstraint(
            "status IN ('RUNNING', 'COMPLETED', 'FAILED', 'INVALID')",
            name="chk_trial_status",
        ),
        Index("ix_eval_trial_manifest", "manifest_id", "strategy_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    manifest_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pair_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(64), nullable=False)
    fixture_id: Mapped[str] = mapped_column(String(128), nullable=False)
    is_negative_control: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'RUNNING'")
    )
    outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    probes_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tokens_consumed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0.0"))
    raw_metrics: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)



class ConsentEpoch(Base):
    """E1 Tenant Experience Capture Consent Epoch."""

    __tablename__ = "consent_epoch"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), primary_key=True
    )
    current_epoch: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    reuse_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExperienceCapsule(Base):
    """E1 Consent-Scoped Experience Capsule."""

    __tablename__ = "experience_capsule"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incident.tenant_id", "incident.id"],
            name="fk_experience_incident",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "incident_id",
            "step_id",
            "schema_version",
            name="uq_experience_idempotency",
        ),
        CheckConstraint(
            "lifecycle_state IN ('CAPTURED', 'ELIGIBLE', 'QUARANTINED', 'TOMBSTONED', 'PURGED')",
            name="chk_experience_state",
        ),
        Index(
            "ix_experience_eligible",
            "tenant_id",
            "consent_epoch",
            "lifecycle_state",
            postgresql_where=text("lifecycle_state = 'ELIGIBLE'"),
        ),
        Index("ix_experience_expiry", "lifecycle_state", "expires_at"),
        Index("ix_experience_lookup", "tenant_id", "incident_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("investigation_step.id", ondelete="SET NULL"), nullable=True
    )
    schema_version: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'v1'")
    )
    consent_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_bundle_version: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'v1'")
    )
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    effect_receipt_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("effect_receipt.id", ondelete="SET NULL"), nullable=True
    )
    outcome_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    outcome_revision_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    redaction_version: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'redact-v1'")
    )
    redacted_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    summary_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'CAPTURED'")
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExperienceAudit(Base):
    """E1 Immutable Experience Capsule Audit Log."""

    __tablename__ = "experience_audit"
    __table_args__ = (
        Index("ix_experience_audit_lookup", "tenant_id", "capsule_id", "timestamp"),
        CheckConstraint(
            "action IN ('CAPTURED', 'QUARANTINED', 'TOMBSTONED', 'PURGED', 'CONSENT_REVOKED', 'CONSENT_GRANTED')",
            name="chk_experience_audit_action",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    capsule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OutcomeObservation(Base):
    """E2 Append-Only Outcome Observation."""

    __tablename__ = "outcome_observation"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incident.tenant_id", "incident.id"],
            name="fk_outcome_obs_incident",
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "incident_id", "idempotency_key", name="uq_outcome_obs_idempotency"),
        Index("ix_outcome_obs_lookup", "tenant_id", "incident_id", "observed_at"),
        CheckConstraint(
            "source_kind IN ('INDEPENDENT_WORKLOAD_CHECK', 'TARGET_READBACK', 'DELAYED_MONITOR', 'OPERATOR_FEEDBACK', 'ADAPTER_ACK', 'MODEL_ANNOTATION')",
            name="chk_outcome_obs_source_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_reference: Mapped[str] = mapped_column(String(256), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    result_payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, server_default=text("'{}'::jsonb")
    )
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
class OutcomeRevision(Base):
    """E2 Immutable Outcome Adjudication Revision."""

    __tablename__ = "outcome_revision"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incident.tenant_id", "incident.id"],
            name="fk_outcome_rev_incident",
            ondelete="CASCADE",
        ),
        UniqueConstraint("incident_id", "revision_no", name="uq_outcome_rev_incident_rev"),
        Index("ix_outcome_rev_lookup", "tenant_id", "incident_id", "revision_no"),
        CheckConstraint(
            "label IN ('PENDING', 'OBSERVED', 'VERIFIED_SUCCESS', 'VERIFIED_FAILURE', 'DISPUTED', 'REVISED', 'UNKNOWN')",
            name="chk_outcome_label",
        ),
        CheckConstraint(
            "confidence_band IN ('HIGH', 'MEDIUM', 'LOW', 'UNKNOWN')",
            name="chk_outcome_confidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence_band: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'LOW'")
    )
    dispute_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    prior_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("outcome_revision.id", ondelete="SET NULL"), nullable=True
    )
    reviewer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewer_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supporting_observation_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, server_default=text("'[]'::jsonb")
    )
    adjudication_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, server_default=text("'{}'::jsonb")
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
class FailureCluster(Base):
    """E3 Grouping of recurring failure cases sharing a deterministic signature."""

    __tablename__ = "failure_cluster"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "failure_origin_code",
            "first_divergent_step_category",
            "subsystem_version",
            name="uq_failure_cluster_sig",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_failure_cluster_tenant"),
        CheckConstraint(
            "cluster_status IN ('ACTIVE', 'QUARANTINED', 'RESOLVED', 'MERGED')",
            name="chk_failure_cluster_status",
        ),
        Index("ix_failure_cluster_tenant_status", "tenant_id", "cluster_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    signature_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    failure_origin_code: Mapped[str] = mapped_column(String(64), nullable=False)
    subsystem_version: Mapped[str] = mapped_column(String(64), nullable=False)
    first_divergent_step_category: Mapped[str] = mapped_column(String(64), nullable=False)
    cluster_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'ACTIVE'")
    )
    case_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    exemplar_case_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("failure_case.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FailureCase(Base):
    """E3 Deterministically classified failure case derived from E2 outcome revisions."""

    __tablename__ = "failure_case"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incident.tenant_id", "incident.id"],
            name="fk_failure_case_incident",
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "outcome_revision_id", name="uq_failure_case_outcome"),
        UniqueConstraint("tenant_id", "id", name="uq_failure_case_tenant"),
        CheckConstraint(
            "classification_status IN ('DETECTED', 'CLASSIFIED', 'UNCERTAIN', 'CLUSTERED', 'IGNORED')",
            name="chk_failure_case_status",
        ),
        Index("ix_failure_case_cluster", "tenant_id", "cluster_id"),
        Index("ix_failure_case_tenant_origin", "tenant_id", "failure_origin_code"),
        Index("ix_failure_case_incident", "tenant_id", "incident_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    outcome_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("outcome_revision.id", ondelete="RESTRICT"), nullable=False
    )
    capsule_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("experience_capsule.id", ondelete="SET NULL"), nullable=True
    )
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("failure_cluster.id", ondelete="SET NULL"), nullable=True
    )
    failure_origin_code: Mapped[str] = mapped_column(String(64), nullable=False)
    subsystem_version: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'v1'")
    )
    first_divergent_step_category: Mapped[str] = mapped_column(String(64), nullable=False)
    first_divergent_step: Mapped[str | None] = mapped_column(String(128), nullable=True)
    classification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'DETECTED'")
    )
    classification_version: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'v1'")
    )
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ImprovementProposal(Base):
    """E3 Bounded improvement proposal derived from failure clusters."""

    __tablename__ = "improvement_proposal"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "cluster_id"],
            ["failure_cluster.tenant_id", "failure_cluster.id"],
            name="fk_proposal_cluster",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_improvement_proposal_tenant"),
        CheckConstraint(
            "status IN ('DRAFT', 'PROPOSED', 'REVIEWED', 'EVALUATION_READY', 'REJECTED')",
            name="chk_improvement_proposal_status",
        ),
        CheckConstraint(
            "proposal_type IN ('PROMPT_REFINEMENT', 'CONTEXT_WINDOW_BUDGET', 'TOOL_DESCRIPTION')",
            name="chk_improvement_proposal_type",
        ),
        Index("ix_improvement_proposal_lookup", "tenant_id", "cluster_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    cluster_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    proposal_type: Mapped[str] = mapped_column(String(64), nullable=False)
    base_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    target_component: Mapped[str] = mapped_column(String(128), nullable=False)
    structured_diff: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    expected_benefit: Mapped[str] = mapped_column(Text, nullable=False)
    risk_assessment: Mapped[str] = mapped_column(Text, nullable=False)
    evaluation_plan: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'DRAFT'")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    drafter_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'MODEL'")
    )
    drafter_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewer_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    review_comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OperationalLesson(Base):
    """E4 Validated operational lesson with PostgreSQL full-text search."""

    __tablename__ = "operational_lesson"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "source_cluster_id"],
            ["failure_cluster.tenant_id", "failure_cluster.id"],
            name="fk_lesson_cluster",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "proposal_id"],
            ["improvement_proposal.tenant_id", "improvement_proposal.id"],
            name="fk_lesson_proposal",
            ondelete="SET NULL",
        ),
        UniqueConstraint("tenant_id", "lesson_id", "version", name="uq_lesson_tenant_version"),
        CheckConstraint(
            "lifecycle_state IN ('DRAFT', 'SOURCE_CHECKED', 'VALIDATED', 'ACTIVE', 'EXPIRED', 'QUARANTINED', 'REVOKED')",
            name="chk_lesson_lifecycle",
        ),
        CheckConstraint(
            "lesson_type IN ('VERIFIED_RECOVERY', 'FAILURE_PATTERN', 'COUNTEREXAMPLE')",
            name="chk_lesson_type",
        ),
        CheckConstraint(
            "confidence_band IN ('HIGH', 'MEDIUM', 'LOW')",
            name="chk_lesson_confidence",
        ),
        Index("ix_operational_lesson_search", "tenant_id", "search_vector", postgresql_using="gin"),
        Index("ix_operational_lesson_lookup", "tenant_id", "lifecycle_state", "valid_until"),
        Index("ix_operational_lesson_epoch", "tenant_id", "consent_epoch"),
        Index("ix_operational_lesson_source_outcome", "tenant_id", "source_outcome_revision_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    lesson_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    lesson_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    redacted_summary: Mapped[str] = mapped_column(Text, nullable=False)
    applicability_predicate: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, server_default=text("'{}'::jsonb")
    )
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(redacted_summary, ''))",
            persisted=True,
        ),
        nullable=False,
    )
    consent_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence_band: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'MEDIUM'")
    )
    lifecycle_state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'DRAFT'")
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    validated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_outcome_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("outcome_revision.id", ondelete="RESTRICT"), nullable=False
    )
    source_capsule_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("experience_capsule.id", ondelete="SET NULL"), nullable=True
    )
    source_cluster_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LessonUsage(Base):
    """E4 Lesson usage record linking an incident investigation step to retrieved lessons."""

    __tablename__ = "lesson_usage"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incident.tenant_id", "incident.id"],
            name="fk_lesson_usage_incident",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "incident_id",
            "step_id",
            "lesson_id",
            "lesson_version",
            name="uq_lesson_usage_step",
        ),
        Index("ix_lesson_usage_lookup", "tenant_id", "lesson_id", "lesson_version"),
        Index("ix_lesson_usage_incident", "tenant_id", "incident_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("investigation_step.id", ondelete="SET NULL"), nullable=True
    )
    lesson_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    lesson_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_bundle_version: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'v1'")
    )
    retrieval_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CandidateEvaluationManifest(Base):
    """E5 Immutable Frozen Candidate-to-Incumbent Evaluation Manifest."""

    __tablename__ = "candidate_evaluation_manifest"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_candidate_eval_manifest_tenant"),
        ForeignKeyConstraint(
            ["tenant_id", "source_proposal_id"],
            ["improvement_proposal.tenant_id", "improvement_proposal.id"],
            name="fk_manifest_proposal",
            ondelete="SET NULL",
        ),
        CheckConstraint(
            "lifecycle_state IN ('REGISTERED', 'FROZEN', 'RUNNING', 'ANALYZING', 'COMPLETED', 'INVALIDATED', 'CANCELLED')",
            name="chk_candidate_eval_manifest_state",
        ),
        CheckConstraint(
            "candidate_provenance_type IN ('E3_PROPOSAL', 'E4_LESSON_SET', 'COMBINED_OFFLINE_STUDY')",
            name="chk_candidate_eval_provenance",
        ),
        Index("ix_candidate_eval_manifest_tenant", "tenant_id", "lifecycle_state"),
        Index("ix_candidate_eval_manifest_outcome_ids", "source_outcome_revision_ids", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    incumbent_bundle: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    incumbent_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_bundle: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    candidate_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_provenance_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_proposal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    source_proposal_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_lesson_ids: Mapped[list[Any] | None] = mapped_column(
        JSON, nullable=True, server_default=text("'[]'::jsonb")
    )
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_catalog_version: Mapped[str] = mapped_column(String(64), nullable=False)
    a2_grant_version: Mapped[str] = mapped_column(String(64), nullable=False)
    fixture_family: Mapped[str] = mapped_column(String(128), nullable=False)
    fixture_set_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    holdout_set_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    oracle_version: Mapped[str] = mapped_column(String(64), nullable=False)
    adjudication_rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_outcome_revision_ids: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, server_default=text("'[]'::jsonb")
    )
    metric_spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    primary_endpoint: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'verified_recovery'")
    )
    sample_size_plan: Mapped[int] = mapped_column(Integer, nullable=False)
    random_seed: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_ceiling_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_tokens_ceiling: Mapped[int] = mapped_column(Integer, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'REGISTERED'")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CandidateEvaluationTrial(Base):
    """E5 Candidate Evaluation Trial Record with worker leasing and double-blind safety auditing."""

    __tablename__ = "candidate_evaluation_trial"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "manifest_id"],
            ["candidate_evaluation_manifest.tenant_id", "candidate_evaluation_manifest.id"],
            name="fk_candidate_eval_trial_manifest",
            ondelete="CASCADE",
        ),
        UniqueConstraint("manifest_id", "pair_id", "arm", name="uq_candidate_eval_trial_pair_arm"),
        CheckConstraint("arm IN ('INCUMBENT', 'CANDIDATE')", name="chk_candidate_eval_trial_arm"),
        CheckConstraint(
            "trial_status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'INVALID')",
            name="chk_candidate_eval_trial_status",
        ),
        CheckConstraint(
            "validity_state IN ('VALID', 'INVALID_TIMEOUT', 'INVALID_ORACLE_OUTAGE', 'INVALID_FIXTURE_CORRUPT')",
            name="chk_candidate_eval_trial_validity",
        ),
        Index("ix_candidate_eval_trial_lookup", "tenant_id", "manifest_id", "arm"),
        Index(
            "ix_candidate_eval_trial_pending",
            "manifest_id",
            "trial_status",
            postgresql_where=text("trial_status IN ('PENDING', 'RUNNING')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pair_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    arm: Mapped[str] = mapped_column(String(32), nullable=False)
    fixture_id: Mapped[str] = mapped_column(String(128), nullable=False)
    is_negative_control: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    trial_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'PENDING'")
    )
    outcome_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    probes_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tokens_consumed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0.0"))
    validity_state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'VALID'")
    )
    validity_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    unsafe_action_detected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    unsafe_action_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    safety_violation_details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_artifact_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CandidateEvaluationReport(Base):
    """E5 Candidate Evaluation Report with CAS Human Review Sign-Off."""

    __tablename__ = "candidate_evaluation_report"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_candidate_eval_report_tenant"),
        ForeignKeyConstraint(
            ["tenant_id", "manifest_id"],
            ["candidate_evaluation_manifest.tenant_id", "candidate_evaluation_manifest.id"],
            name="fk_candidate_eval_report_manifest",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "report_status IN ('PASS', 'FAIL', 'INVALID', 'INCONCLUSIVE')",
            name="chk_candidate_eval_report_status",
        ),
        CheckConstraint(
            "review_decision IS NULL OR review_decision IN ('ACCEPTED', 'REJECTED', 'INCONCLUSIVE_ACKNOWLEDGED')",
            name="chk_candidate_eval_review_decision",
        ),
        UniqueConstraint("manifest_id", "version", name="uq_candidate_eval_report_version"),
        Index("ix_candidate_eval_report_manifest", "tenant_id", "manifest_id", "report_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    report_status: Mapped[str] = mapped_column(String(32), nullable=False)
    promotion_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    invalidation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_pairs_planned: Mapped[int] = mapped_column(Integer, nullable=False)
    total_pairs_completed: Mapped[int] = mapped_column(Integer, nullable=False)
    invalid_pairs_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    metrics_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    mcnemar_statistic: Mapped[float | None] = mapped_column(Float, nullable=True)
    mcnemar_p_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    recovery_rate_incumbent: Mapped[float] = mapped_column(Float, nullable=False)
    recovery_rate_candidate: Mapped[float] = mapped_column(Float, nullable=False)
    recovery_delta: Mapped[float] = mapped_column(Float, nullable=False)
    recovery_ci_low: Mapped[float] = mapped_column(Float, nullable=False)
    recovery_ci_high: Mapped[float] = mapped_column(Float, nullable=False)
    cost_per_verified_incumbent: Mapped[float] = mapped_column(Float, nullable=False)
    cost_per_verified_candidate: Mapped[float | None] = mapped_column(Float, nullable=True)
    negative_control_pass_rate: Mapped[float] = mapped_column(Float, nullable=False)
    unsafe_action_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    gate_verdicts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    reviewer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewer_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    review_comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_eligibility_rechecked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    superseded_by_report_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("candidate_evaluation_report.id", ondelete="SET NULL"), nullable=True
    )


class PolicyBundle(Base):
    """E6 Immutable Signed Policy Bundle."""

    __tablename__ = "policy_bundle"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_policy_bundle_tenant"),
        UniqueConstraint("tenant_id", "bundle_sha256", name="uq_policy_bundle_digest"),
        CheckConstraint("status IN ('DRAFT', 'SEALED', 'ARCHIVED')", name="chk_policy_bundle_status"),
        Index("idx_policy_bundle_target", "tenant_id", "target_component", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    bundle_name: Mapped[str] = mapped_column(String(128), nullable=False)
    bundle_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(128), nullable=False)
    signature_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    base_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    target_component: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_diff: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    prompt_templates: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    active_lesson_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_catalog_version: Mapped[str] = mapped_column(String(64), nullable=False)
    a2_grant_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_manifest_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("candidate_evaluation_manifest.id", ondelete="RESTRICT"), nullable=True
    )
    source_report_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("candidate_evaluation_report.id", ondelete="RESTRICT"), nullable=True
    )
    source_provenance_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'DRAFT'"))
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PolicyRelease(Base):
    """E6 Governed Policy Release with atomic CAS transitions."""

    __tablename__ = "policy_release"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_policy_release_tenant"),
        ForeignKeyConstraint(
            ["tenant_id", "bundle_id"],
            ["policy_bundle.tenant_id", "policy_bundle.id"],
            name="fk_policy_release_bundle",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "incumbent_bundle_id"],
            ["policy_bundle.tenant_id", "policy_bundle.id"],
            name="fk_policy_release_incumbent",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "canary_percentage >= 0 AND canary_percentage <= 100",
            name="chk_release_canary_percentage",
        ),
        CheckConstraint(
            "reviewer_id IS NULL OR drafter_id <> reviewer_id",
            name="chk_release_separation_of_duties",
        ),
        CheckConstraint(
            "state IN ('DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'SHADOW', 'CANARY', 'ACTIVE', 'ROLLED_BACK', 'REVOKED')",
            name="chk_policy_release_state",
        ),
        Index(
            "uq_active_release_per_target",
            "tenant_id",
            "target_component",
            unique=True,
            postgresql_where=text("state IN ('SHADOW', 'CANARY', 'ACTIVE')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    target_component: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incumbent_bundle_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'DRAFT'"))
    canary_percentage: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rollout_salt: Mapped[str] = mapped_column(String(64), nullable=False)
    drafter_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewer_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_comments: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    kill_switch_triggered_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kill_switch_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rollback_reason: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReleaseEventAudit(Base):
    """E6 Append-only audit trail for policy releases."""

    __tablename__ = "release_event_audit"
    __table_args__ = (
        Index("idx_release_audit_release", "tenant_id", "release_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    release_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("policy_release.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_redacted: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, server_default=text("'{}'::json")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

