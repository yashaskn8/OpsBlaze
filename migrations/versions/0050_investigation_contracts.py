"""A1-A3 investigation contracts: probe capabilities, context manifests, supersession, competing hypotheses."""

from alembic import op
import sqlalchemy as sa

revision = "0050_investigation_contracts"
down_revision = "0049_advisory_route_pin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "probe_capability",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column(
            "incident_id",
            sa.Uuid(),
            sa.ForeignKey("incident.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("catalog_id", sa.String(64), nullable=False),
        sa.Column("catalog_version", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("max_calls", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("used_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_response_bytes", sa.Integer(), nullable=False, server_default="512"),
        sa.Column("timeout_ms", sa.Integer(), nullable=False, server_default="2000"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by", sa.String(48)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "incident_id", "catalog_id", name="uq_probe_capability_scope",
        ),
    )
    op.create_index(
        "ix_probe_capability_incident",
        "probe_capability",
        ["tenant_id", "incident_id", "expires_at"],
    )

    op.create_table(
        "context_manifest",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column(
            "incident_id",
            sa.Uuid(),
            sa.ForeignKey("incident.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("runbook_revision", sa.String(32), nullable=False),
        sa.Column("prompt_revision", sa.String(32), nullable=False),
        sa.Column("model_id", sa.String(64), nullable=False),
        sa.Column("token_budget_reserved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("token_budget_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("lesson_ids", sa.JSON(), nullable=False),
        sa.Column("memory_mode", sa.String(24), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "incident_id", "step_no", name="uq_context_manifest_step",
        ),
    )
    op.create_index(
        "ix_context_manifest_incident",
        "context_manifest",
        ["tenant_id", "incident_id", "created_at"],
    )

    op.add_column("evidence", sa.Column("supersedes_evidence_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_evidence_supersedes",
        "evidence",
        "evidence",
        ["supersedes_evidence_id"],
        ["id"],
    )
    op.add_column("hypothesis_step", sa.Column("competing_hypotheses", sa.JSON()))
    op.add_column("hypothesis_step", sa.Column("elapsed_ms", sa.Integer()))


def downgrade() -> None:
    op.drop_column("hypothesis_step", "elapsed_ms")
    op.drop_column("hypothesis_step", "competing_hypotheses")
    op.drop_constraint("fk_evidence_supersedes", "evidence", type_="foreignkey")
    op.drop_column("evidence", "supersedes_evidence_id")
    op.drop_index("ix_context_manifest_incident", table_name="context_manifest")
    op.drop_table("context_manifest")
    op.drop_index("ix_probe_capability_incident", table_name="probe_capability")
    op.drop_table("probe_capability")
