"""E1/E2/E3 learning provenance, pinned adjudication rules, and failure attribution."""

from alembic import op
import sqlalchemy as sa

revision = "0052_learning_provenance"
down_revision = "0051_route_context_features"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "experience_capsule",
        sa.Column("source_step_key", sa.String(128), nullable=False, server_default="outcome"),
    )
    op.add_column(
        "experience_capsule",
        sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index(
        "ix_capsule_step_key",
        "experience_capsule",
        ["incident_id", "outcome_revision_id", "source_step_key"],
    )
    op.add_column(
        "outcome_revision",
        sa.Column("rule_version", sa.String(32), nullable=False, server_default="adjudication-v1"),
    )
    op.add_column(
        "outcome_revision",
        sa.Column("source_observations", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "improvement_candidate",
        sa.Column("reviewed_by", sa.String(64), nullable=True),
    )
    with op.batch_alter_table("improvement_candidate") as batch:
        batch.alter_column(
            "created_by",
            existing_type=sa.String(32),
            type_=sa.String(64),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("improvement_candidate") as batch:
        batch.alter_column(
            "created_by",
            existing_type=sa.String(64),
            type_=sa.String(32),
            existing_nullable=False,
        )
    op.drop_column("improvement_candidate", "reviewed_by")
    op.drop_column("outcome_revision", "source_observations")
    op.drop_column("outcome_revision", "rule_version")
    op.drop_index("ix_capsule_step_key", table_name="experience_capsule")
    op.drop_column("experience_capsule", "provenance")
    op.drop_column("experience_capsule", "source_step_key")
