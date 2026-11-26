"""E7 persist versioned route context features for risk, freshness, and eligible memory."""

from alembic import op
import sqlalchemy as sa

revision = "0051_route_context_features"
down_revision = "0050_investigation_contracts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("route_decision", sa.Column("context_features", sa.JSON()))


def downgrade() -> None:
    op.drop_column("route_decision", "context_features")
