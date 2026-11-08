"""Stage 7 migration for Features E1 Experience Capture and E2 Outcome Adjudication.

Revision ID: stage7_001_e1_e2
Revises: stage6_001_p2_p3_s1
Create Date: 2026-11-08 13:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "stage7_001_e1_e2"
down_revision: str | None = "stage6_001_p2_p3_s1"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    existing_tables = set(inspector.get_table_names(schema=current_schema))

    # 1. consent_epoch table
    if "consent_epoch" not in existing_tables:
        op.create_table(
            "consent_epoch",
            sa.Column(
                "tenant_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("tenant.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("current_epoch", sa.Integer(), server_default=sa.text("1"), nullable=False),
            sa.Column("reuse_allowed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        )

    # 2. outcome_observation table
    if "outcome_observation" not in existing_tables:
        op.create_table(
            "outcome_observation",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("incident_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("source_kind", sa.String(64), nullable=False),
            sa.Column("source_reference", sa.String(256), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("rule_version", sa.String(64), nullable=False),
            sa.Column(
                "result_payload",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.Column("source_digest", sa.String(64), nullable=False),
            sa.Column("idempotency_key", sa.String(128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id", "incident_id"],
                ["incident.tenant_id", "incident.id"],
                name="fk_outcome_obs_incident",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint("tenant_id", "incident_id", "idempotency_key", name="uq_outcome_obs_idempotency"),
            sa.CheckConstraint(
                "source_kind IN ('INDEPENDENT_WORKLOAD_CHECK', 'TARGET_READBACK', 'DELAYED_MONITOR', 'OPERATOR_FEEDBACK', 'ADAPTER_ACK', 'MODEL_ANNOTATION')",
                name="chk_outcome_obs_source_kind",
            ),
        )
        op.create_index(
            "ix_outcome_obs_lookup",
            "outcome_observation",
            ["tenant_id", "incident_id", "observed_at"],
        )

    # 3. outcome_revision table
    if "outcome_revision" not in existing_tables:
        op.create_table(
            "outcome_revision",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("incident_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("revision_no", sa.Integer(), nullable=False),
            sa.Column("rule_version", sa.String(64), nullable=False),
            sa.Column("label", sa.String(32), nullable=False),
            sa.Column("confidence_band", sa.String(16), server_default=sa.text("'LOW'"), nullable=False),
            sa.Column("dispute_reason", sa.Text(), nullable=True),
            sa.Column("prior_revision_id", sa.Uuid(as_uuid=True), sa.ForeignKey("outcome_revision.id", ondelete="SET NULL"), nullable=True),
            sa.Column("reviewer_id", sa.String(128), nullable=True),
            sa.Column("reviewer_role", sa.String(64), nullable=True),
            sa.Column(
                "supporting_observation_ids",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'[]'::jsonb"),
                nullable=False,
            ),
            sa.Column(
                "adjudication_metadata",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.Column("decided_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id", "incident_id"],
                ["incident.tenant_id", "incident.id"],
                name="fk_outcome_rev_incident",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint("incident_id", "revision_no", name="uq_outcome_rev_incident_rev"),
            sa.CheckConstraint(
                "label IN ('PENDING', 'OBSERVED', 'VERIFIED_SUCCESS', 'VERIFIED_FAILURE', 'DISPUTED', 'REVISED', 'UNKNOWN')",
                name="chk_outcome_label",
            ),
            sa.CheckConstraint(
                "confidence_band IN ('HIGH', 'MEDIUM', 'LOW', 'UNKNOWN')",
                name="chk_outcome_confidence",
            ),
        )
        op.create_index(
            "ix_outcome_rev_lookup",
            "outcome_revision",
            ["tenant_id", "incident_id", "revision_no"],
        )

    # 4. experience_capsule table
    if "experience_capsule" not in existing_tables:
        op.create_table(
            "experience_capsule",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("incident_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column(
                "step_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("investigation_step.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("schema_version", sa.String(32), server_default=sa.text("'v1'"), nullable=False),
            sa.Column("consent_epoch", sa.Integer(), nullable=False),
            sa.Column("policy_bundle_version", sa.String(64), server_default=sa.text("'v1'"), nullable=False),
            sa.Column("model_id", sa.String(128), nullable=False),
            sa.Column("model_version", sa.String(64), nullable=False),
            sa.Column("evidence_manifest_sha256", sa.String(64), nullable=False),
            sa.Column(
                "effect_receipt_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("effect_receipt.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("outcome_id", sa.Uuid(as_uuid=True), nullable=True),
            sa.Column("outcome_revision_no", sa.Integer(), nullable=True),
            sa.Column("redaction_version", sa.String(32), server_default=sa.text("'redact-v1'"), nullable=False),
            sa.Column("redacted_digest", sa.String(64), nullable=False),
            sa.Column("summary_markdown", sa.Text(), nullable=True),
            sa.Column("classification", sa.String(64), nullable=False),
            sa.Column("lifecycle_state", sa.String(32), server_default=sa.text("'CAPTURED'"), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id", "incident_id"],
                ["incident.tenant_id", "incident.id"],
                name="fk_experience_incident",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "incident_id",
                "step_id",
                "schema_version",
                name="uq_experience_idempotency",
            ),
            sa.CheckConstraint(
                "lifecycle_state IN ('CAPTURED', 'ELIGIBLE', 'QUARANTINED', 'TOMBSTONED', 'PURGED')",
                name="chk_experience_state",
            ),
        )
        op.create_index(
            "ix_experience_eligible",
            "experience_capsule",
            ["tenant_id", "consent_epoch", "lifecycle_state"],
            postgresql_where=sa.text("lifecycle_state = 'ELIGIBLE'"),
        )
        op.create_index(
            "ix_experience_expiry",
            "experience_capsule",
            ["lifecycle_state", "expires_at"],
        )
        op.create_index(
            "ix_experience_lookup",
            "experience_capsule",
            ["tenant_id", "incident_id"],
        )

    # 5. experience_audit table
    if "experience_audit" not in existing_tables:
        op.create_table(
            "experience_audit",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("capsule_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("action", sa.String(32), nullable=False),
            sa.Column("actor_id", sa.String(128), nullable=False),
            sa.Column("actor_role", sa.String(64), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.CheckConstraint(
                "action IN ('CAPTURED', 'QUARANTINED', 'TOMBSTONED', 'PURGED', 'CONSENT_REVOKED', 'CONSENT_GRANTED')",
                name="chk_experience_audit_action",
            ),
        )
        op.create_index(
            "ix_experience_audit_lookup",
            "experience_audit",
            ["tenant_id", "capsule_id", "timestamp"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    existing_tables = set(inspector.get_table_names(schema=current_schema))

    if "experience_audit" in existing_tables:
        op.drop_table("experience_audit")
    if "experience_capsule" in existing_tables:
        op.drop_table("experience_capsule")
    if "outcome_revision" in existing_tables:
        op.drop_table("outcome_revision")
    if "outcome_observation" in existing_tables:
        op.drop_table("outcome_observation")
    if "consent_epoch" in existing_tables:
        op.drop_table("consent_epoch")
