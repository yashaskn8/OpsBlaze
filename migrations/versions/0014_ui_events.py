
"""P1 durable monotonic incident UI event notifications."""

from alembic import op
import sqlalchemy as sa

revision = "0014_ui_events"
down_revision = "0013_policy_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ui_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("projection_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_ui_event_incident", "ui_event",
        ["tenant_id", "incident_id", "id"],
    )
    op.create_index("ix_ui_event_created", "ui_event", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ui_event_created", table_name="ui_event")
    op.drop_index("ix_ui_event_incident", table_name="ui_event")
    op.drop_table("ui_event")
