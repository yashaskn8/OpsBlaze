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
    JSON,
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


class OutcomeRevision(Base):
    __tablename__ = "outcome_revision"
    __table_args__ = (
        UniqueConstraint("tenant_id", "incident_id", "revision", name="uq_outcome_revision"),
        Index("ix_outcome_incident", "tenant_id", "incident_id", "revision"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("incident.id"), nullable=False)
    effect_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("effect_receipt.id"))
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(24), nullable=False)
    witness_source: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_version: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ExperienceConsent(Base):
    __tablename__ = "experience_consent"

    incident_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incident.id"), primary_key=True
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExperienceCapsule(Base):
    __tablename__ = "experience_capsule"
    __table_args__ = (
        UniqueConstraint("incident_id", "outcome_revision_id", name="uq_capsule_outcome"),
        Index("ix_experience_scoped", "tenant_id", "incident_id", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("incident.id"), nullable=False)
    outcome_revision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("outcome_revision.id"), nullable=False)
    consent_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    target_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FailureCluster(Base):
    __tablename__ = "failure_cluster"
    __table_args__ = (
        UniqueConstraint("tenant_id", "signature", "version", name="uq_failure_cluster_signature"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class FailureCase(Base):
    __tablename__ = "failure_case"
    __table_args__ = (
        UniqueConstraint("tenant_id", "outcome_revision_id", name="uq_failure_outcome"),
        Index("ix_failure_tenant_origin", "tenant_id", "origin_code", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("incident.id"), nullable=False)
    outcome_revision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("outcome_revision.id"), nullable=False
    )
    cluster_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("failure_cluster.id"), nullable=False)
    origin_code: Mapped[str] = mapped_column(String(32), nullable=False)
    first_bad_step: Mapped[int | None] = mapped_column(Integer)
    classification_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    review_status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ImprovementCandidate(Base):
    __tablename__ = "improvement_candidate"
    __table_args__ = (
        Index("ix_improvement_state", "tenant_id", "state", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_cluster_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("failure_cluster.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    base_policy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    change_spec: Mapped[dict] = mapped_column(JSON, nullable=False)
    hypothesis: Mapped[str] = mapped_column(String(512), nullable=False)
    risk: Mapped[str] = mapped_column(String(16), nullable=False)
    evaluation_manifest_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="DRAFT")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str] = mapped_column(String(32), nullable=False, default="local_operator")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OperationalLesson(Base):
    __tablename__ = "operational_lesson"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_capsule_id", "rule_id", name="uq_lesson_source_rule"),
        Index("ix_lesson_lookup", "tenant_id", "status", "valid_until"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_capsule_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("experience_capsule.id"), nullable=False
    )
    source_outcome_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("outcome_revision.id"), nullable=False
    )
    target_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(48), nullable=False)
    summary_redacted: Mapped[str] = mapped_column(String(200), nullable=False)
    consent_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="DRAFT")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvaluationManifest(Base):
    __tablename__ = "evaluation_manifest"
    __table_args__ = (
        UniqueConstraint("tenant_id", "candidate_id", "candidate_version", "fixture_digest", name="uq_eval_frozen"),
        Index("ix_evaluation_candidate", "tenant_id", "candidate_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("improvement_candidate.id"), nullable=False
    )
    candidate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    incumbent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fixture_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    oracle_version: Mapped[str] = mapped_column(String(32), nullable=False)
    trial_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvaluationTrial(Base):
    __tablename__ = "evaluation_trial"
    __table_args__ = (
        UniqueConstraint("manifest_id", "pair_id", "arm", name="uq_evaluation_pair_arm"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    manifest_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("evaluation_manifest.id"), nullable=False)
    pair_id: Mapped[str] = mapped_column(String(48), nullable=False)
    arm: Mapped[str] = mapped_column(String(16), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    oracle_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    unsafe: Mapped[bool] = mapped_column(Boolean, nullable=False)
    probe_cost_units: Mapped[int] = mapped_column(Integer, nullable=False)


class EvaluationGate(Base):
    __tablename__ = "evaluation_gate"
    __table_args__ = (
        UniqueConstraint("manifest_id", "rule_id", name="uq_evaluation_gate"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    manifest_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("evaluation_manifest.id"), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(48), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence: Mapped[str] = mapped_column(String(256), nullable=False)


class RouteDecision(Base):
    __tablename__ = "route_decision"
    __table_args__ = (
        UniqueConstraint("tenant_id", "incident_id", "decision_no", name="uq_route_once"),
        Index("ix_route_tenant_created", "tenant_id", "created_at", "route"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("incident.id"), nullable=False)
    decision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    context_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_band: Mapped[str] = mapped_column(String(16), nullable=False)
    route: Mapped[str] = mapped_column(String(32), nullable=False)
    selection_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    propensity: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_cap_probes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdapterConformanceRun(Base):
    __tablename__ = "adapter_conformance_run"
    __table_args__ = (
        Index("ix_adapter_runs", "adapter_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    adapter_id: Mapped[str] = mapped_column(String(48), nullable=False)
    code_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    suite_version: Mapped[str] = mapped_column(String(32), nullable=False)
    retention_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    results: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PolicyBundle(Base):
    __tablename__ = "policy_bundle"
    __table_args__ = (
        UniqueConstraint("tenant_id", "content_digest", name="uq_policy_content"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("improvement_candidate.id"), nullable=False
    )
    evaluation_manifest_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("evaluation_manifest.id"), nullable=False
    )
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PolicyRollout(Base):
    __tablename__ = "policy_rollout"
    __table_args__ = (
        Index("ix_rollout_tenant_state", "tenant_id", "state"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("policy_bundle.id"), nullable=False
    )
    cohort_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    salt: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UIEvent(Base):
    __tablename__ = "ui_event"
    __table_args__ = (
        Index("ix_ui_event_incident", "tenant_id", "incident_id", "id"),
        Index("ix_ui_event_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True, autoincrement=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incident.id"), nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    projection_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReplayExperiment(Base):
    __tablename__ = "replay_experiment"
    __table_args__ = (
        Index("ix_replay_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fixture_id: Mapped[str] = mapped_column(String(48), nullable=False)
    fixture_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    budget: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReplayArm(Base):
    __tablename__ = "replay_arm"
    __table_args__ = (
        UniqueConstraint("experiment_id", "kind", name="uq_replay_arm"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    experiment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("replay_experiment.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    virtual_time: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    probe_cost: Mapped[int] = mapped_column(Integer, nullable=False)
    oracle_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    trace: Mapped[list[dict]] = mapped_column(JSON, nullable=False)


class ModelAdvisory(Base):
    __tablename__ = "model_advisory"

    incident_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incident.id"), primary_key=True
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    budget_window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    next_probe: Mapped[str | None] = mapped_column(String(32))
    reason_code: Mapped[str | None] = mapped_column(String(32))
    tokens_reserved: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FailureReview(Base):
    __tablename__ = "failure_review"
    __table_args__ = (
        UniqueConstraint("case_id", "revision", name="uq_failure_review_revision"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    case_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("failure_case.id"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_code: Mapped[str] = mapped_column(String(32), nullable=False)
    corrected_code: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DecisionAssessment(Base):
    __tablename__ = "decision_assessment"
    __table_args__ = (
        UniqueConstraint("incident_id", "step_no", name="uq_assessment_step"),
        Index("ix_assessment_tenant_incident", "tenant_id", "incident_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incident.id"), nullable=False,
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    feature_version: Mapped[str] = mapped_column(String(32), nullable=False)
    threshold_version: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PolicyAssignment(Base):
    __tablename__ = "policy_assignment"
    __table_args__ = (
        Index("ix_policy_assignment_policy", "tenant_id", "policy_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incident.id"), primary_key=True
    )
    policy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("policy_bundle.id")
    )
    rollout_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("policy_rollout.id")
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ModelTokenWindow(Base):
    __tablename__ = "model_token_window"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
