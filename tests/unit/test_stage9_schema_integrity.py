"""Unit tests for Stage 9 schema integrity and historical data preservation."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.config import get_settings


@pytest.mark.asyncio
async def test_stage9_tables_and_columns_exist() -> None:
    """Verify that all Stage 9 tables and columns exist in the active database."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    async with engine.connect() as conn:

        def check_schema(sync_conn: Any) -> None:
            inspector = inspect(sync_conn)
            tables = set(inspector.get_table_names())
            assert "candidate_evaluation_manifest" in tables
            assert "candidate_evaluation_trial" in tables
            assert "candidate_evaluation_report" in tables

            # Verify candidate_evaluation_manifest columns
            cem_cols = {c["name"]: c for c in inspector.get_columns("candidate_evaluation_manifest")}
            assert "id" in cem_cols
            assert "tenant_id" in cem_cols
            assert "title" in cem_cols
            assert "manifest_sha256" in cem_cols
            assert "incumbent_bundle" in cem_cols
            assert "incumbent_digest" in cem_cols
            assert "candidate_bundle" in cem_cols
            assert "candidate_digest" in cem_cols
            assert "candidate_provenance_type" in cem_cols
            assert "source_proposal_id" in cem_cols
            assert "source_proposal_version" in cem_cols
            assert "source_lesson_ids" in cem_cols
            assert "model_id" in cem_cols
            assert "model_version" in cem_cols
            assert "tool_catalog_version" in cem_cols
            assert "a2_grant_version" in cem_cols
            assert "fixture_family" in cem_cols
            assert "fixture_set_digest" in cem_cols
            assert "holdout_set_digest" in cem_cols
            assert "oracle_version" in cem_cols
            assert "adjudication_rule_version" in cem_cols
            assert "source_outcome_revision_ids" in cem_cols
            assert "metric_spec" in cem_cols
            assert "primary_endpoint" in cem_cols
            assert "sample_size_plan" in cem_cols
            assert "random_seed" in cem_cols
            assert "budget_ceiling_micros" in cem_cols
            assert "max_tokens_ceiling" in cem_cols
            assert "lifecycle_state" in cem_cols
            assert "version" in cem_cols
            assert "created_by" in cem_cols
            assert "created_at" in cem_cols
            assert "updated_at" in cem_cols
            assert "frozen_at" in cem_cols

            # Verify candidate_evaluation_trial columns
            cet_cols = {c["name"]: c for c in inspector.get_columns("candidate_evaluation_trial")}
            assert "id" in cet_cols
            assert "tenant_id" in cet_cols
            assert "manifest_id" in cet_cols
            assert "pair_id" in cet_cols
            assert "arm" in cet_cols
            assert "fixture_id" in cet_cols
            assert "is_negative_control" in cet_cols
            assert "trial_status" in cet_cols
            assert "outcome_label" in cet_cols
            assert "decision_correct" in cet_cols
            assert "probes_count" in cet_cols
            assert "tokens_consumed" in cet_cols
            assert "cost_micros" in cet_cols
            assert "duration_ms" in cet_cols
            assert "validity_state" in cet_cols
            assert "validity_reason" in cet_cols
            assert "unsafe_action_detected" in cet_cols
            assert "unsafe_action_count" in cet_cols
            assert "safety_violation_details" in cet_cols
            assert "worker_id" in cet_cols
            assert "lease_expires_at" in cet_cols
            assert "heartbeat_at" in cet_cols
            assert "raw_artifact_digest" in cet_cols
            assert "created_at" in cet_cols
            assert "updated_at" in cet_cols
            assert "completed_at" in cet_cols

            # Verify candidate_evaluation_report columns
            cer_cols = {c["name"]: c for c in inspector.get_columns("candidate_evaluation_report")}
            assert "id" in cer_cols
            assert "tenant_id" in cer_cols
            assert "manifest_id" in cer_cols
            assert "report_status" in cer_cols
            assert "promotion_eligible" in cer_cols
            assert "invalidation_reason" in cer_cols
            assert "total_pairs_planned" in cer_cols
            assert "total_pairs_completed" in cer_cols
            assert "invalid_pairs_count" in cer_cols
            assert "metrics_summary" in cer_cols
            assert "mcnemar_statistic" in cer_cols
            assert "mcnemar_p_value" in cer_cols
            assert "recovery_rate_incumbent" in cer_cols
            assert "recovery_rate_candidate" in cer_cols
            assert "recovery_delta" in cer_cols
            assert "recovery_ci_low" in cer_cols
            assert "recovery_ci_high" in cer_cols
            assert "cost_per_verified_incumbent" in cer_cols
            assert "cost_per_verified_candidate" in cer_cols
            assert "negative_control_pass_rate" in cer_cols
            assert "unsafe_action_count" in cer_cols
            assert "gate_verdicts" in cer_cols
            assert "version" in cer_cols
            assert "reviewer_id" in cer_cols
            assert "reviewer_role" in cer_cols
            assert "review_decision" in cer_cols
            assert "review_comments" in cer_cols
            assert "source_eligibility_rechecked" in cer_cols
            assert "reviewed_at" in cer_cols
            assert "evaluated_at" in cer_cols
            assert "created_at" in cer_cols
            assert "updated_at" in cer_cols
            assert "superseded_by_report_id" in cer_cols

            # Check unique and foreign key constraints
            uqs_manifest = {u["name"] for u in inspector.get_unique_constraints("candidate_evaluation_manifest")}
            assert "uq_candidate_eval_manifest_tenant" in uqs_manifest

            uqs_trial = {u["name"] for u in inspector.get_unique_constraints("candidate_evaluation_trial")}
            assert "uq_candidate_eval_trial_pair_arm" in uqs_trial

            uqs_report = {u["name"] for u in inspector.get_unique_constraints("candidate_evaluation_report")}
            assert "uq_candidate_eval_report_tenant" in uqs_report
            assert "uq_candidate_eval_report_version" in uqs_report

        await conn.run_sync(check_schema)

    await engine.dispose()


@pytest.mark.asyncio
async def test_stage9_historical_data_preserved() -> None:
    """Verify that migration did not drop or clear historical data from earlier stages."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    async with engine.connect() as conn:
        tenant_count = await conn.scalar(text("SELECT COUNT(*) FROM tenant"))
        incident_count = await conn.scalar(text("SELECT COUNT(*) FROM incident"))
        assert tenant_count is not None and tenant_count > 0
        assert incident_count is not None and incident_count > 0

    await engine.dispose()
