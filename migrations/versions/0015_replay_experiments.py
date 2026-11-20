
"""S1 isolated deterministic paired replay records."""

from alembic import op
import sqlalchemy as sa

revision = "0015_replay_experiments"
down_revision = "0014_ui_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "replay_experiment",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("fixture_id", sa.String(48), nullable=False),
        sa.Column("fixture_digest", sa.String(64), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("budget", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_replay_tenant_created", "replay_experiment", ["tenant_id", "created_at"]
    )
    op.create_table(
        "replay_arm",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("experiment_id", sa.Uuid(), sa.ForeignKey("replay_experiment.id"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.Column("virtual_time", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("probe_cost", sa.Integer(), nullable=False),
        sa.Column("oracle_passed", sa.Boolean(), nullable=False),
        sa.Column("trace", sa.JSON(), nullable=False),
        sa.UniqueConstraint("experiment_id", "kind", name="uq_replay_arm"),
    )


def downgrade() -> None:
    op.drop_table("replay_arm")
    op.drop_index("ix_replay_tenant_created", table_name="replay_experiment")
    op.drop_table("replay_experiment")
