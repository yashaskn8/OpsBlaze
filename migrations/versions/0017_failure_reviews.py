
"""E3 fact-bound taxonomy review and immutable revision record."""

from alembic import op
import sqlalchemy as sa

revision = "0017_failure_reviews"
down_revision = "0016_ai_advisory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "failure_review",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("case_id", sa.Uuid(), sa.ForeignKey("failure_case.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("previous_code", sa.String(32), nullable=False),
        sa.Column("corrected_code", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("case_id", "revision", name="uq_failure_review_revision"),
    )


def downgrade() -> None:
    op.drop_table("failure_review")
