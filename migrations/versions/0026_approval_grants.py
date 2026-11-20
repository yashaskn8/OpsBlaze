"""P2 immutable grants, revocation evidence and consumed dispatch boundary."""

from alembic import op
import sqlalchemy as sa

revision = "0026_approval_grants"
down_revision = "0025_admission_actor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approval_grant",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("incident_plan.id"), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("reviewer_id", sa.String(48), nullable=False),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(256), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("plan_id", name="uq_approval_plan"),
    )
    op.create_index(
        "ix_approval_tenant_state", "approval_grant",
        ["tenant_id", "state", "expires_at"],
    )
    op.create_table(
        "approval_revocation",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("approval_id", sa.Uuid(), sa.ForeignKey("approval_grant.id"),
                  nullable=False),
        sa.Column("revoked_by", sa.String(48), nullable=False),
        sa.Column("reason", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    # Old approved plans remain without grant and cannot initiate a new effect.
    # A distinct reviewer must make a new plan decision under current policy.


def downgrade() -> None:
    op.drop_table("approval_revocation")
    op.drop_index("ix_approval_tenant_state", table_name="approval_grant")
    op.drop_table("approval_grant")
