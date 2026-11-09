"""Schema and data integrity verification for Stage 7 (E1/E2)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_stage7_schema_and_historical_data_integrity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        # 1. Verify Alembic Head
        rev = (
            await session.execute(
                text("SELECT version_num FROM public.alembic_version LIMIT 1")
            )
        ).scalar_one()
        assert rev in (
            "stage7_001_e1_e2",
            "stage8_001_e3_e4",
            "stage9_001_e5",
            "stage11_001_e6",
            "stage11_1_001_revoked_tokens",
            "stage11_2_001_guardrail_outbox",
            "s11_2_002_identity",
            "stage11_2_001_outbox_identity",
            "stage11_2_003_reconcile",
            "stage12_001_e7_strategy_router",
        )

        # 2. Verify New Tables Exist
        rows = (
            await session.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            )
        ).scalars().all()
        table_names = set(rows)
        expected_new_tables = {
            "consent_epoch",
            "experience_capsule",
            "experience_audit",
            "outcome_observation",
            "outcome_revision",
        }
        assert expected_new_tables.issubset(table_names)

        # The CI database is intentionally empty apart from migrations and test fixtures.
        # Historical row-count preservation is audited separately against a provisioned snapshot.


@pytest.mark.historical
@pytest.mark.asyncio
async def test_stage7_historical_rows_preserved_in_provisioned_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Check the immutable historical baseline only when its snapshot is provisioned."""
    async with session_factory() as session:
        tenant_count = (await session.execute(text("SELECT COUNT(*) FROM tenant"))).scalar()
        incident_count = (await session.execute(text("SELECT COUNT(*) FROM incident"))).scalar()
        receipt_count = (await session.execute(text("SELECT COUNT(*) FROM effect_receipt"))).scalar()

        assert tenant_count is not None and tenant_count >= 13
        assert incident_count is not None and incident_count >= 29
        assert receipt_count is not None and receipt_count >= 29
