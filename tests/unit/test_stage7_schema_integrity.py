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

        # 3. Verify Historical Data Rows Preserved (never dropped/truncated)
        t_cnt = (await session.execute(text("SELECT COUNT(*) FROM tenant"))).scalar()
        i_cnt = (await session.execute(text("SELECT COUNT(*) FROM incident"))).scalar()
        r_cnt = (await session.execute(text("SELECT COUNT(*) FROM effect_receipt"))).scalar()

        # In Stage 4 & 6 baseline: 13 historical tenants, 29 historical incidents, 29+ receipts
        assert t_cnt is not None and t_cnt >= 13
        assert i_cnt is not None and i_cnt >= 29
        assert r_cnt is not None and r_cnt >= 29
