
"""P2 simulator approval epoch pinned for explicit local revocation."""

from alembic import op
import sqlalchemy as sa

revision = "0024_review_epoch"
down_revision = "0023_reviewer_credential_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "incident_plan", sa.Column("review_epoch", sa.Integer(), nullable=True)
    )
    # Previously approved plans deliberately remain unpinned and must not
    # initiate a new effect until reviewed under the current epoch.


def downgrade() -> None:
    op.drop_column("incident_plan", "review_epoch")
