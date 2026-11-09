"""Stage 12 / Capability E7: Budgeted Strategy Router Schema.

Creates `strategy_route` and `strategy_decision` tables with strict tenant isolation,
singular naming conventions, and immutable append-only decision logging.

Revision ID: stage12_001_e7_strategy_router
Revises: stage11_2_003_reconcile
Create Date: 2026-11-09 20:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "stage12_001_e7_strategy_router"
down_revision: str | None = "stage11_2_003_reconcile"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    existing_tables = set(inspector.get_table_names(schema=current_schema))

    # -----------------------------------------------------------------------
    # 1. Create `strategy_route` table
    # -----------------------------------------------------------------------
    if "strategy_route" not in existing_tables:
        op.create_table(
            "strategy_route",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("tenant.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("route_key", sa.String(length=64), nullable=False),
            sa.Column("model_tier", sa.String(length=32), nullable=False),
            sa.Column(
                "allowed_tool_capabilities",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'::json"),
            ),
            sa.Column(
                "max_cost_micros",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "max_tokens",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "risk_ceiling",
                sa.Float(),
                nullable=False,
                server_default=sa.text("1.0"),
            ),
            sa.Column(
                "priority",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("tenant_id", "route_key", name="uq_strategy_route_tenant_key"),
        )

    inspector = sa.inspect(bind)
    existing_route_indexes = {
        idx["name"] for idx in inspector.get_indexes("strategy_route", schema=current_schema)
    }
    if "ix_strategy_route_tenant_active" not in existing_route_indexes:
        op.create_index(
            "ix_strategy_route_tenant_active",
            "strategy_route",
            ["tenant_id", "is_active", "priority"],
        )

    # -----------------------------------------------------------------------
    # 2. Create `strategy_decision` table
    # -----------------------------------------------------------------------
    if "strategy_decision" not in existing_tables:
        op.create_table(
            "strategy_decision",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("tenant.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "incident_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("incident.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("route_key", sa.String(length=64), nullable=False),
            sa.Column("model_tier", sa.String(length=32), nullable=False),
            sa.Column(
                "allowed_tool_capabilities",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'::json"),
            ),
            sa.Column("propensity", sa.Float(), nullable=False),
            sa.Column(
                "budget_snapshot_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'::json"),
            ),
            sa.Column("estimated_cost_micros", sa.BigInteger(), nullable=False),
            sa.Column("actual_cost_micros", sa.BigInteger(), nullable=True),
            sa.Column("fallback_reason", sa.String(length=64), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )

    inspector = sa.inspect(bind)
    existing_dec_indexes = {
        idx["name"] for idx in inspector.get_indexes("strategy_decision", schema=current_schema)
    }
    if "ix_strategy_decision_incident" not in existing_dec_indexes:
        op.create_index(
            "ix_strategy_decision_incident",
            "strategy_decision",
            ["tenant_id", "incident_id", "created_at"],
        )
    if "ix_strategy_decision_route" not in existing_dec_indexes:
        op.create_index(
            "ix_strategy_decision_route",
            "strategy_decision",
            ["tenant_id", "route_key", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    existing_tables = set(inspector.get_table_names(schema=current_schema))

    if "strategy_decision" in existing_tables:
        op.drop_table("strategy_decision")
    if "strategy_route" in existing_tables:
        op.drop_table("strategy_route")
