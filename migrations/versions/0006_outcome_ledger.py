
"""E2 append-only witnessed outcome revisions."""

from alembic import op
import sqlalchemy as sa

revision = "0006_outcome_ledger"
down_revision = "0005_plan_decision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outcome_revision",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id"), nullable=False),
        sa.Column("effect_id", sa.Uuid(), sa.ForeignKey("effect_receipt.id"), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(24), nullable=False),
        sa.Column("witness_source", sa.String(32), nullable=False),
        sa.Column("observed_version", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "incident_id", "revision", name="uq_outcome_revision"),
    )
    op.create_index("ix_outcome_incident", "outcome_revision", ["tenant_id", "incident_id", "revision"])


def downgrade() -> None:
    op.drop_index("ix_outcome_incident", table_name="outcome_revision")
    op.drop_table("outcome_revision")
