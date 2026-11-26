"""Store authenticated reviewer pseudonyms on new review records."""

from alembic import op
import sqlalchemy as sa

revision = "0047_review_attribution"
down_revision = "0046_experience_edge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outcome_revision", sa.Column("reviewer_id", sa.String(48), nullable=True))
    op.add_column("failure_review", sa.Column("reviewer_id", sa.String(48), nullable=True))


def downgrade() -> None:
    op.drop_column("failure_review", "reviewer_id")
    op.drop_column("outcome_revision", "reviewer_id")
