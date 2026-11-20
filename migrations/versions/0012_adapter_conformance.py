
"""E8 simulator adapter conformance evidence, no production enablement."""

from alembic import op
import sqlalchemy as sa

revision = "0012_adapter_conformance"
down_revision = "0011_pinned_routes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "adapter_conformance_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("adapter_id", sa.String(48), nullable=False),
        sa.Column("code_digest", sa.String(64), nullable=False),
        sa.Column("suite_version", sa.String(32), nullable=False),
        sa.Column("retention_seconds", sa.Integer(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_adapter_runs", "adapter_conformance_run", ["adapter_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_adapter_runs", table_name="adapter_conformance_run")
    op.drop_table("adapter_conformance_run")
