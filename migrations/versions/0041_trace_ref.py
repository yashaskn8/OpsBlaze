"""P3 local span references for durable incident and outcome records."""

from alembic import op
import sqlalchemy as sa

revision = "0041_trace_ref"
down_revision = "0040_worker_host_age_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trace_ref",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id", ondelete="CASCADE"), nullable=False),
        sa.Column("record_type", sa.String(16), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("trace_id", sa.String(32), nullable=False),
        sa.Column("span_id", sa.String(16), nullable=False),
        sa.UniqueConstraint("record_type", "record_id", name="uq_trace_ref_record"),
        sa.CheckConstraint("record_type IN ('incident', 'outcome')", name="ck_trace_ref_kind"),
        sa.CheckConstraint("length(trace_id) = 32 AND length(span_id) = 16", name="ck_trace_ref_length"),
    )
    op.create_index("ix_trace_ref_incident_trace", "trace_ref", ["tenant_id", "incident_id", "trace_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_trace_ref_incident_trace", table_name="trace_ref")
    op.drop_table("trace_ref")
