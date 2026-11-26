"""Persist the strategy version, model, and token reservation of new routes."""

from alembic import op
import sqlalchemy as sa

revision = "0049_advisory_route_pin"
down_revision = "0048_lineage_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("route_decision", sa.Column("router_version", sa.String(32)))
    op.add_column("route_decision", sa.Column("model_id", sa.String(64)))
    op.add_column("route_decision", sa.Column("model_token_reservation", sa.Integer()))


def downgrade() -> None:
    op.drop_column("route_decision", "model_token_reservation")
    op.drop_column("route_decision", "model_id")
    op.drop_column("route_decision", "router_version")
