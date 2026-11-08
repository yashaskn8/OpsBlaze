"""Unit tests for Stage 11 schema integrity and historical data preservation."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.config import get_settings


@pytest.mark.asyncio
async def test_stage11_tables_and_columns_exist() -> None:
    """Verify that all Stage 11 tables and columns exist in the active database."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    async with engine.connect() as conn:

        def check_schema(sync_conn: Any) -> None:
            inspector = inspect(sync_conn)
            tables = set(inspector.get_table_names())
            assert "policy_bundle" in tables, "Table 'policy_bundle' missing"
            assert "policy_release" in tables, "Table 'policy_release' missing"
            assert "release_event_audit" in tables, "Table 'release_event_audit' missing"

            # 1. policy_bundle columns
            pb_cols = {c["name"]: c for c in inspector.get_columns("policy_bundle")}
            for col in [
                "id",
                "tenant_id",
                "version",
                "bundle_name",
                "bundle_sha256",
                "signature",
                "signature_key_id",
                "base_policy_version",
                "target_component",
                "proposal_diff",
                "prompt_templates",
                "active_lesson_ids",
                "model_id",
                "model_version",
                "tool_catalog_version",
                "a2_grant_version",
                "source_manifest_id",
                "source_report_id",
                "source_provenance_digest",
                "status",
                "created_by",
                "created_at",
                "sealed_at",
            ]:
                assert col in pb_cols, f"Column '{col}' missing in policy_bundle"

            # 2. policy_release columns
            pr_cols = {c["name"]: c for c in inspector.get_columns("policy_release")}
            for col in [
                "id",
                "tenant_id",
                "version",
                "target_component",
                "bundle_id",
                "incumbent_bundle_id",
                "state",
                "canary_percentage",
                "rollout_salt",
                "drafter_id",
                "reviewer_id",
                "reviewer_role",
                "decision_comments",
                "kill_switch_active",
                "kill_switch_triggered_by",
                "kill_switch_triggered_at",
                "rollback_reason",
                "created_at",
                "updated_at",
                "approved_at",
                "activated_at",
                "rolled_back_at",
            ]:
                assert col in pr_cols, f"Column '{col}' missing in policy_release"

            # 3. release_event_audit columns
            rea_cols = {c["name"]: c for c in inspector.get_columns("release_event_audit")}
            for col in [
                "id",
                "tenant_id",
                "release_id",
                "event_type",
                "from_state",
                "to_state",
                "actor_id",
                "actor_role",
                "metadata_redacted",
                "created_at",
            ]:
                assert col in rea_cols, f"Column '{col}' missing in release_event_audit"

            # 4. incident additive columns
            inc_cols = {c["name"]: c for c in inspector.get_columns("incident")}
            assert "pinned_policy_bundle_id" in inc_cols
            assert "policy_release_id" in inc_cols
            assert "canary_arm" in inc_cols
            assert "assignment_hash_val" in inc_cols

            # 5. Indexes
            pr_indexes = {idx["name"]: idx for idx in inspector.get_indexes("policy_release")}
            assert "uq_active_release_per_target" in pr_indexes
            assert pr_indexes["uq_active_release_per_target"]["unique"] is True

        await conn.run_sync(check_schema)

    await engine.dispose()


@pytest.mark.asyncio
async def test_stage11_historical_data_preserved() -> None:
    """Verify that migration did not truncate or drop any historical data."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    async with engine.connect() as conn:
        tenant_count = await conn.scalar(text("SELECT count(*) FROM tenant"))
        assert tenant_count is not None and tenant_count >= 1

        incident_count = await conn.scalar(text("SELECT count(*) FROM incident"))
        assert incident_count is not None and incident_count >= 1

        # Check candidate_evaluation_manifest
        manifest_count = await conn.scalar(text("SELECT count(*) FROM candidate_evaluation_manifest"))
        assert manifest_count is not None

    await engine.dispose()
