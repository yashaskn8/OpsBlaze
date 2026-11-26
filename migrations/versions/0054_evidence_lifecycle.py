"""A3 evidence lifecycle expiry, access audit, and E8 simulator effect retention."""

from alembic import op
import sqlalchemy as sa

revision = "0054_evidence_lifecycle"
down_revision = "0053_eval_canary_adapters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "evidence",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_evidence_expires",
        "evidence",
        ["validity", "expires_at"],
    )

    op.create_table(
        "evidence_access_audit",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column(
            "incident_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("incident.id"),
            nullable=False,
        ),
        sa.Column(
            "evidence_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("evidence.id"),
            nullable=False,
        ),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("classification", sa.String(24), nullable=False),
        sa.Column(
            "accessed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_evidence_access_scoped",
        "evidence_access_audit",
        ["tenant_id", "incident_id", "accessed_at"],
    )

    op.add_column(
        "simulator_applied_effect",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_column("simulator_applied_effect", "created_at")
    op.drop_index("ix_evidence_access_scoped", table_name="evidence_access_audit")
    op.drop_table("evidence_access_audit")
    op.drop_index("ix_evidence_expires", table_name="evidence")
    op.drop_column("evidence", "expires_at")
