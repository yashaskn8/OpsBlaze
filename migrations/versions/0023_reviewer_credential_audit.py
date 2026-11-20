
"""P2 record local decision credential class; production identity still gated."""

from alembic import op
import sqlalchemy as sa

revision = "0023_reviewer_credential_audit"
down_revision = "0022_outbox_age"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "incident_plan", sa.Column("decided_by", sa.String(48), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("incident_plan", "decided_by")
