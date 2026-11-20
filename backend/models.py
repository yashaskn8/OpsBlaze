from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Incident(Base):
    __tablename__ = "incident"
    __table_args__ = (
        UniqueConstraint("tenant_id", "client_key", name="uq_incident_tenant_client_key"),
        Index("ix_incident_tenant_created", "tenant_id", "created_at", "id"),
        Index(
            "ix_incident_tenant_target_status",
            "tenant_id",
            "target_id",
            "status_projection",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    client_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(256), nullable=False)
    status_projection: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="DISPATCH_PENDING"
    )
    status_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    probe_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    phase_entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OutboxEntry(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        Index(
            "ix_outbox_pending",
            "available_at",
            "id",
            postgresql_where=text("delivered_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    incident_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incident.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict | None] = mapped_column(JSON)

# B2/B3 simulator and effect records. No production target connector is enabled.
from sqlalchemy import Boolean, JSON


class ResourceEpoch(Base):
    __tablename__ = "resource_epoch"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    latest_fence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version_seen: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class ResourceReservation(Base):
    __tablename__ = "resource_reservation"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    holder_incident: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    action_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)


class SimulatorTarget(Base):
    __tablename__ = "simulator_target"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    healthy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_fence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class SimulatorAppliedEffect(Base):
    __tablename__ = "simulator_applied_effect"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(256), nullable=False)
    observed_version: Mapped[int] = mapped_column(BigInteger, nullable=False)


class EffectReceipt(Base):
    __tablename__ = "effect_receipt"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_effect_tenant_key"),
        Index("ix_effect_incident", "tenant_id", "incident_id", "created_at"),
        Index("ix_effect_unresolved", "state", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("incident.id"), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(256), nullable=False)
    adapter_id: Mapped[str] = mapped_column(String(32), nullable=False, default="simulator")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observed_version: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class AdmissionUsage(Base):
    __tablename__ = "admission_usage"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)



class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        Index("ix_evidence_scoped", "tenant_id", "incident_id", "captured_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("incident.id"), nullable=False)
    target_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[str] = mapped_column(String(32), nullable=False)
    target_version: Mapped[int | None] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    redacted_excerpt: Mapped[str] = mapped_column(String(512), nullable=False)
    classification: Mapped[str] = mapped_column(String(24), nullable=False)
    validity: Mapped[str] = mapped_column(String(24), nullable=False)


class ProbeCall(Base):
    __tablename__ = "probe_call"
    __table_args__ = (UniqueConstraint("incident_id", "step_no", name="uq_probe_incident_step"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("incident.id"), nullable=False)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    catalog_id: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    evidence_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("evidence.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class HypothesisStep(Base):
    __tablename__ = "hypothesis_step"
    __table_args__ = (UniqueConstraint("incident_id", "step_no", name="uq_hypothesis_incident_step"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("incident.id"), nullable=False)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    cause_scores: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False)
    probe_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(48), nullable=False)
    token_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

class IncidentPlan(Base):
    __tablename__ = "incident_plan"
    __table_args__ = (
        UniqueConstraint("incident_id", name="uq_plan_incident"),
        Index("ix_plan_tenant_incident", "tenant_id", "incident_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("incident.id"), nullable=False)
    target_id: Mapped[str] = mapped_column(String(256), nullable=False)
    action_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
