
"""E4 source-backed lesson registry."""

from alembic import op
import sqlalchemy as sa

revision = "0009_operational_lessons"
down_revision = "0008_failure_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operational_lesson",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("source_capsule_id", sa.Uuid(), sa.ForeignKey("experience_capsule.id"), nullable=False),
        sa.Column("source_outcome_id", sa.Uuid(), sa.ForeignKey("outcome_revision.id"), nullable=False),
        sa.Column("target_digest", sa.String(64), nullable=False),
        sa.Column("rule_id", sa.String(48), nullable=False),
        sa.Column("summary_redacted", sa.String(200), nullable=False),
        sa.Column("consent_epoch", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "tenant_id", "source_capsule_id", "rule_id", name="uq_lesson_source_rule"
        ),
    )
    op.create_index(
        "ix_lesson_lookup", "operational_lesson",
        ["tenant_id", "status", "valid_until"],
    )


def downgrade() -> None:
    op.drop_index("ix_lesson_lookup", table_name="operational_lesson")
    op.drop_table("operational_lesson")
