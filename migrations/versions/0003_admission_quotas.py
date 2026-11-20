"""B4 fixed-window admission quota and incident phase timestamp."""

from alembic import op
import sqlalchemy as sa

revision = "0003_admission_quotas"
down_revision = "0002_simulator_effects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admission_usage",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("accepted", sa.Integer(), nullable=False),
    )
    op.add_column(
        "incident",
        sa.Column("phase_entered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_column("incident", "phase_entered_at")
    op.drop_table("admission_usage")
