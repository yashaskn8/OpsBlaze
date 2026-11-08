"""Stage 11 migration for Feature E6 Versioned Policy Canary, Release Control & Rollback.

Revision ID: stage11_001_e6
Revises: stage9_001_e5
Create Date: 2026-11-08 18:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "stage11_001_e6"
down_revision: str | None = "stage9_001_e5"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    existing_tables = set(inspector.get_table_names(schema=current_schema))

    # 1. policy_bundle
    if "policy_bundle" not in existing_tables:
        op.create_table(
            "policy_bundle",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("tenant.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
            sa.Column("bundle_name", sa.String(128), nullable=False),
            sa.Column("bundle_sha256", sa.String(64), nullable=False),
            sa.Column("signature", sa.String(128), nullable=False),
            sa.Column("signature_key_id", sa.String(64), nullable=False),
            sa.Column("base_policy_version", sa.String(64), nullable=False),
            sa.Column("target_component", sa.String(64), nullable=False),
            sa.Column("proposal_diff", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("prompt_templates", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("active_lesson_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("model_id", sa.String(64), nullable=False),
            sa.Column("model_version", sa.String(64), nullable=False),
            sa.Column("tool_catalog_version", sa.String(64), nullable=False),
            sa.Column("a2_grant_version", sa.String(64), nullable=False),
            sa.Column(
                "source_manifest_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("candidate_evaluation_manifest.id", ondelete="RESTRICT"),
                nullable=True,
            ),
            sa.Column(
                "source_report_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("candidate_evaluation_report.id", ondelete="RESTRICT"),
                nullable=True,
            ),
            sa.Column("source_provenance_digest", sa.String(64), nullable=False),
            sa.Column("status", sa.String(32), server_default=sa.text("'DRAFT'"), nullable=False),
            sa.Column("created_by", sa.String(128), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("tenant_id", "id", name="uq_policy_bundle_tenant"),
            sa.UniqueConstraint("tenant_id", "bundle_sha256", name="uq_policy_bundle_digest"),
            sa.CheckConstraint(
                "status IN ('DRAFT', 'SEALED', 'ARCHIVED')",
                name="chk_policy_bundle_status",
            ),
        )
        op.create_index(
            "idx_policy_bundle_target",
            "policy_bundle",
            ["tenant_id", "target_component", "status"],
        )

    # 2. policy_release
    if "policy_release" not in existing_tables:
        op.create_table(
            "policy_release",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("tenant.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
            sa.Column("target_component", sa.String(64), nullable=False),
            sa.Column("bundle_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("incumbent_bundle_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("state", sa.String(32), server_default=sa.text("'DRAFT'"), nullable=False),
            sa.Column("canary_percentage", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("rollout_salt", sa.String(64), nullable=False),
            sa.Column("drafter_id", sa.String(128), nullable=False),
            sa.Column("reviewer_id", sa.String(128), nullable=True),
            sa.Column("reviewer_role", sa.String(64), nullable=True),
            sa.Column("decision_comments", sa.String(2000), nullable=True),
            sa.Column("kill_switch_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("kill_switch_triggered_by", sa.String(128), nullable=True),
            sa.Column("kill_switch_triggered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rollback_reason", sa.String(1024), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("tenant_id", "id", name="uq_policy_release_tenant"),
            sa.ForeignKeyConstraint(
                ["tenant_id", "bundle_id"],
                ["policy_bundle.tenant_id", "policy_bundle.id"],
                name="fk_policy_release_bundle",
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id", "incumbent_bundle_id"],
                ["policy_bundle.tenant_id", "policy_bundle.id"],
                name="fk_policy_release_incumbent",
                ondelete="RESTRICT",
            ),
            sa.CheckConstraint(
                "canary_percentage >= 0 AND canary_percentage <= 100",
                name="chk_release_canary_percentage",
            ),
            sa.CheckConstraint(
                "reviewer_id IS NULL OR drafter_id <> reviewer_id",
                name="chk_release_separation_of_duties",
            ),
            sa.CheckConstraint(
                "state IN ('DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'SHADOW', 'CANARY', 'ACTIVE', 'ROLLED_BACK', 'REVOKED')",
                name="chk_policy_release_state",
            ),
        )
        op.create_index(
            "uq_active_release_per_target",
            "policy_release",
            ["tenant_id", "target_component"],
            unique=True,
            postgresql_where=sa.text("state IN ('SHADOW', 'CANARY', 'ACTIVE')"),
        )

    # 3. release_event_audit
    if "release_event_audit" not in existing_tables:
        op.create_table(
            "release_event_audit",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("tenant.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "release_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("policy_release.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("event_type", sa.String(64), nullable=False),
            sa.Column("from_state", sa.String(32), nullable=True),
            sa.Column("to_state", sa.String(32), nullable=False),
            sa.Column("actor_id", sa.String(128), nullable=False),
            sa.Column("actor_role", sa.String(64), nullable=False),
            sa.Column(
                "metadata_redacted",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index(
            "idx_release_audit_release",
            "release_event_audit",
            ["tenant_id", "release_id", "created_at"],
        )

    # 4. Additive columns on incident
    existing_incident_cols = {col["name"] for col in inspector.get_columns("incident", schema=current_schema)}

    if "pinned_policy_bundle_id" not in existing_incident_cols:
        op.add_column(
            "incident",
            sa.Column(
                "pinned_policy_bundle_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("policy_bundle.id", ondelete="RESTRICT"),
                nullable=True,
            ),
        )
    if "policy_release_id" not in existing_incident_cols:
        op.add_column(
            "incident",
            sa.Column(
                "policy_release_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("policy_release.id", ondelete="RESTRICT"),
                nullable=True,
            ),
        )
    if "canary_arm" not in existing_incident_cols:
        op.add_column(
            "incident",
            sa.Column("canary_arm", sa.String(32), nullable=True),
        )
    if "assignment_hash_val" not in existing_incident_cols:
        op.add_column(
            "incident",
            sa.Column("assignment_hash_val", sa.Integer(), nullable=True),
        )

    existing_incident_indexes = {idx["name"] for idx in inspector.get_indexes("incident", schema=current_schema)}
    if "idx_incident_pinned_bundle" not in existing_incident_indexes:
        op.create_index(
            "idx_incident_pinned_bundle",
            "incident",
            ["tenant_id", "pinned_policy_bundle_id"],
        )


def downgrade() -> None:
    op.drop_index("idx_incident_pinned_bundle", table_name="incident")
    op.drop_column("incident", "assignment_hash_val")
    op.drop_column("incident", "canary_arm")
    op.drop_column("incident", "policy_release_id")
    op.drop_column("incident", "pinned_policy_bundle_id")

    op.drop_index("idx_release_audit_release", table_name="release_event_audit")
    op.drop_table("release_event_audit")

    op.drop_index("uq_active_release_per_target", table_name="policy_release")
    op.drop_table("policy_release")

    op.drop_index("idx_policy_bundle_target", table_name="policy_bundle")
    op.drop_table("policy_bundle")
