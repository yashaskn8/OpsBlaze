
"""A1 optional single-spend advisory model reservation."""

from alembic import op
import sqlalchemy as sa

revision = "0016_ai_advisory"
down_revision = "0015_replay_experiments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_advisory",
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id"), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("next_probe", sa.String(32), nullable=True),
        sa.Column("reason_code", sa.String(32), nullable=True),
        sa.Column("tokens_reserved", sa.Integer(), nullable=False),
        sa.Column("tokens_used", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("model_advisory")
