"""Unit tests for Stage 8 schema integrity and historical data preservation."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.config import get_settings


@pytest.mark.asyncio
async def test_stage8_tables_and_columns_exist() -> None:
    """Verify that all Stage 8 tables and columns exist in the active database."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    async with engine.connect() as conn:
        def check_schema(sync_conn: Any) -> None:
            inspector = inspect(sync_conn)
            tables = set(inspector.get_table_names())
            assert "failure_cluster" in tables
            assert "failure_case" in tables
            assert "improvement_proposal" in tables
            assert "operational_lesson" in tables
            assert "lesson_usage" in tables

            # Verify failure_cluster columns
            fc_cols = {c["name"]: c for c in inspector.get_columns("failure_cluster")}
            assert "id" in fc_cols
            assert "tenant_id" in fc_cols
            assert "signature_hash" in fc_cols
            assert "failure_origin_code" in fc_cols
            assert "subsystem_version" in fc_cols
            assert "first_divergent_step_category" in fc_cols
            assert "cluster_status" in fc_cols
            assert "case_count" in fc_cols
            assert "exemplar_case_id" in fc_cols
            assert "version" in fc_cols
            assert "quarantine_reason" in fc_cols

            # Verify failure_case columns
            fcase_cols = {c["name"]: c for c in inspector.get_columns("failure_case")}
            assert "id" in fcase_cols
            assert "tenant_id" in fcase_cols
            assert "incident_id" in fcase_cols
            assert "outcome_revision_id" in fcase_cols
            assert "cluster_id" in fcase_cols
            assert "failure_origin_code" in fcase_cols
            assert "subsystem_version" in fcase_cols
            assert "first_divergent_step_category" in fcase_cols
            assert "classification_status" in fcase_cols

            # Verify improvement_proposal columns
            ip_cols = {c["name"]: c for c in inspector.get_columns("improvement_proposal")}
            assert "id" in ip_cols
            assert "tenant_id" in ip_cols
            assert "cluster_id" in ip_cols
            assert "proposal_type" in ip_cols
            assert "structured_diff" in ip_cols
            assert "status" in ip_cols
            assert "version" in ip_cols
            assert "rejection_code" in ip_cols

            # Verify operational_lesson columns
            ol_cols = {c["name"]: c for c in inspector.get_columns("operational_lesson")}
            assert "id" in ol_cols
            assert "tenant_id" in ol_cols
            assert "lesson_id" in ol_cols
            assert "version" in ol_cols
            assert "search_vector" in ol_cols
            assert "source_outcome_revision_id" in ol_cols
            assert "source_capsule_id" in ol_cols
            assert "source_cluster_id" in ol_cols
            assert "proposal_id" in ol_cols

            # Verify experience_audit.capsule_id is nullable
            ea_cols = {c["name"]: c for c in inspector.get_columns("experience_audit")}
            assert ea_cols["capsule_id"]["nullable"] is True

        await conn.run_sync(check_schema)

    await engine.dispose()


@pytest.mark.asyncio
async def test_historical_data_preservation_opsblaze() -> None:
    """Verify that historical counts in opsblaze are strictly preserved."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    async with engine.connect() as conn:
        tenant_count = (await conn.execute(select(text("COUNT(*) FROM tenant")))).scalar()
        incident_count = (await conn.execute(select(text("COUNT(*) FROM incident")))).scalar()
        receipt_count = (await conn.execute(select(text("COUNT(*) FROM effect_receipt")))).scalar()

        assert tenant_count is not None and tenant_count >= 13, f"Expected >= 13 tenants, got {tenant_count}"
        assert incident_count is not None and incident_count >= 67, f"Expected >= 67 incidents, got {incident_count}"
        assert receipt_count is not None and receipt_count >= 201, f"Expected >= 201 effect receipts, got {receipt_count}"

    await engine.dispose()


@pytest.mark.asyncio
async def test_historical_data_preservation_opsblaze_test() -> None:
    """Verify that historical counts in opsblaze_test are strictly preserved."""
    settings = get_settings()
    test_url = settings.database_url.set(database="opsblaze_test")
    engine = create_async_engine(test_url)

    async with engine.connect() as conn:
        tenant_count = (await conn.execute(select(text("COUNT(*) FROM tenant")))).scalar()
        incident_count = (await conn.execute(select(text("COUNT(*) FROM incident")))).scalar()
        receipt_count = (await conn.execute(select(text("COUNT(*) FROM effect_receipt")))).scalar()

        assert tenant_count is not None and tenant_count >= 29, f"Expected >= 29 tenants, got {tenant_count}"
        assert incident_count is not None and incident_count >= 29, f"Expected >= 29 incidents, got {incident_count}"
        assert receipt_count is not None and receipt_count >= 29, f"Expected >= 29 effect receipts, got {receipt_count}"

    await engine.dispose()
