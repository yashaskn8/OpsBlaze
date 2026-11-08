"""Stage 6 migration for Features P2, P1, P3, and S1 evaluation foundation.

Revision ID: stage6_001_p2_p3_s1
Revises: stage5_001_a1_a2_a3_a4_b4
Create Date: 2026-11-08 01:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "stage6_001_p2_p3_s1"
down_revision: str | None = "stage5_001_a1_a2_a3_a4_b4"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    existing_tables = set(inspector.get_table_names(schema=current_schema))

    # 1. approval_request table
    if "approval_request" not in existing_tables:
        op.create_table(
            "approval_request",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("incident_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("step_id", sa.Uuid(as_uuid=True), sa.ForeignKey("investigation_step.id", ondelete="SET NULL"), nullable=True),
            sa.Column("plan_id", sa.String(128), nullable=False),
            sa.Column("canonical_plan_digest", sa.String(64), nullable=False),
            sa.Column("action_digest", sa.String(64), nullable=False),
            sa.Column("action_payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
            sa.Column("target_id", sa.String(128), nullable=False),
            sa.Column("expected_resource_version", sa.String(64), nullable=False),
            sa.Column("policy_version", sa.String(64), nullable=False),
            sa.Column("state", sa.String(32), server_default=sa.text("'PENDING'"), nullable=False),
            sa.Column("reviewer_id", sa.String(128), nullable=True),
            sa.Column("reviewer_role", sa.String(64), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("decision_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("dispatch_lease_id", sa.Uuid(as_uuid=True), nullable=True),
            sa.Column("dispatch_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id", "incident_id"],
                ["incident.tenant_id", "incident.id"],
                name="fk_approval_incident",
                ondelete="CASCADE",
            ),
            sa.CheckConstraint(
                "state IN ('PENDING', 'APPROVED', 'DISPATCHING', 'REJECTED', 'REVOKED', 'INVALIDATED', 'EXPIRED', 'CONSUMED')",
                name="chk_approval_state",
            ),
        )
        op.create_index(
            "ux_active_approval_per_incident",
            "approval_request",
            ["tenant_id", "incident_id"],
            unique=True,
            postgresql_where=sa.text("state IN ('PENDING', 'APPROVED', 'DISPATCHING')"),
        )
        op.create_index(
            "ix_approval_lookup",
            "approval_request",
            ["tenant_id", "incident_id", "state"],
        )
        op.create_index(
            "ix_approval_expiry",
            "approval_request",
            ["state", "expires_at"],
            postgresql_where=sa.text("state IN ('PENDING', 'APPROVED', 'DISPATCHING')"),
        )

    # 2. approval_audit table
    if "approval_audit" not in existing_tables:
        op.create_table(
            "approval_audit",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("approval_id", sa.Uuid(as_uuid=True), sa.ForeignKey("approval_request.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("action", sa.String(32), nullable=False),
            sa.Column("actor_id", sa.String(128), nullable=False),
            sa.Column("actor_role", sa.String(64), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
            sa.CheckConstraint(
                "action IN ('CREATED', 'APPROVED', 'REJECTED', 'REVOKED', 'INVALIDATED', 'EXPIRED', 'DISPATCHED', 'CONSUMED')",
                name="chk_approval_audit_action",
            ),
        )
        op.create_index(
            "ix_approval_audit_lookup",
            "approval_audit",
            ["tenant_id", "approval_id", "timestamp"],
        )

    # 3. evaluation_manifest table
    if "evaluation_manifest" not in existing_tables:
        op.create_table(
            "evaluation_manifest",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("manifest_sha256", sa.String(64), unique=True, nullable=False),
            sa.Column("title", sa.String(256), nullable=False),
            sa.Column("fixture_family", sa.String(128), nullable=False),
            sa.Column("strategy_version", sa.String(64), nullable=False),
            sa.Column("budget_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("oracle_version", sa.String(64), nullable=False),
            sa.Column("random_seed", sa.Integer(), nullable=False),
            sa.Column("split", sa.String(32), server_default=sa.text("'DEV'"), nullable=False),
            sa.Column("frozen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.CheckConstraint(
                "split IN ('DEV', 'VALIDATION', 'HOLDOUT', 'NEGATIVE_CONTROL')",
                name="chk_manifest_split",
            ),
        )

    # 4. evaluation_trial table
    if "evaluation_trial" not in existing_tables:
        op.create_table(
            "evaluation_trial",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("manifest_id", sa.Uuid(as_uuid=True), sa.ForeignKey("evaluation_manifest.id", ondelete="CASCADE"), nullable=False),
            sa.Column("pair_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("strategy_name", sa.String(64), nullable=False),
            sa.Column("fixture_id", sa.String(128), nullable=False),
            sa.Column("is_negative_control", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("status", sa.String(32), server_default=sa.text("'RUNNING'"), nullable=False),
            sa.Column("outcome", sa.String(64), nullable=True),
            sa.Column("decision_correct", sa.Boolean(), nullable=True),
            sa.Column("probes_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("tokens_consumed", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("cost_micros", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
            sa.Column("duration_ms", sa.Float(), server_default=sa.text("0.0"), nullable=False),
            sa.Column("raw_metrics", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("manifest_id", "pair_id", "strategy_name", name="ux_trial_manifest_pair_strategy"),
            sa.CheckConstraint(
                "status IN ('RUNNING', 'COMPLETED', 'FAILED', 'INVALID')",
                name="chk_trial_status",
            ),
        )
        op.create_index(
            "ix_eval_trial_manifest",
            "evaluation_trial",
            ["manifest_id", "strategy_name"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    existing_tables = set(inspector.get_table_names(schema=current_schema))

    if "evaluation_trial" in existing_tables:
        op.drop_table("evaluation_trial")
    if "evaluation_manifest" in existing_tables:
        op.drop_table("evaluation_manifest")
    if "approval_audit" in existing_tables:
        op.drop_table("approval_audit")
    if "approval_request" in existing_tables:
        op.drop_table("approval_request")
