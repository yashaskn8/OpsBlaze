"""Create B1 incident admission and transactional outbox tables.

Revision ID: 0001_incident_intake
Revises:
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_incident_intake"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incident",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("client_key", sa.String(128), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(256), nullable=False),
        sa.Column("status_projection", sa.String(32), nullable=False, server_default="DISPATCH_PENDING"),
        sa.Column("status_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "client_key", name="uq_incident_tenant_client_key"),
    )
    op.create_index(
        "ix_incident_tenant_created", "incident", ["tenant_id", "created_at", "id"]
    )
    op.create_index(
        "ix_incident_tenant_target_status",
        "incident",
        ["tenant_id", "target_id", "status_projection"],
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_outbox_pending",
        "outbox",
        ["available_at", "id"],
        postgresql_where=sa.text("delivered_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_pending", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index("ix_incident_tenant_target_status", table_name="incident")
    op.drop_index("ix_incident_tenant_created", table_name="incident")
    op.drop_table("incident")
