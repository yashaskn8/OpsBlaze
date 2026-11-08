"""Unit tests for Feature E5 Candidate Manifest Service, Provenance Validation, and Holdout Gate."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.evaluation import (
    CandidateBundle,
    CandidateProvenanceType,
    CandidateProvenanceValidationError,
    EvaluationConcurrencyConflictError,
    HoldoutContaminationError,
    IncumbentBundle,
    ManifestLifecycleError,
    ManifestLifecycleState,
    MetricSpec,
    PromptRefinementDiff,
)
from backend.evaluations.manifest_service import CandidateManifestService
from backend.persistence.models import (
    ConsentEpoch,
    FailureCase,
    FailureCluster,
    ImprovementProposal,
    OperationalLesson,
    Outbox,
    OutcomeRevision,
)
from backend.persistence.repositories import IncidentRepository


@pytest.fixture
async def manifest_fixture(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    tenant_id = uuid.uuid4()
    cluster_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    lesson_id = uuid.uuid4()
    outcome_rev_id = uuid.uuid4()

    inc_repo = IncidentRepository(session_factory)
    await inc_repo.ensure_tenant(tenant_id, f"tenant-e5-{tenant_id.hex[:8]}")
    admit = await inc_repo.admit_incident(
        tenant_id=tenant_id,
        client_key=f"inc-e5-{uuid.uuid4()}",
        target_id="target-1",
        request_sha256="1" * 64,
    )
    source_incident_id = admit.response.id

    async with session_factory() as session:
        # Mark outbox delivered
        await session.execute(
            update(Outbox).where(Outbox.incident_id == source_incident_id).values(delivered_at=func.now())
        )

        epoch = ConsentEpoch(
            tenant_id=tenant_id,
            current_epoch=1,
            reuse_allowed=True,
        )
        session.add(epoch)

        rev = OutcomeRevision(
            id=outcome_rev_id,
            tenant_id=tenant_id,
            incident_id=source_incident_id,
            revision_no=1,
            rule_version="v1.0",
            label="VERIFIED_SUCCESS",
            confidence_band="HIGH",
        )
        session.add(rev)

        fc = FailureCluster(
            id=cluster_id,
            tenant_id=tenant_id,
            signature_hash=f"sig-{uuid.uuid4().hex[:8]}",
            failure_origin_code="TEST_ORIGIN",
            subsystem_version="1.0.0",
            first_divergent_step_category="OBSERVATION",
            cluster_status="ACTIVE",
            case_count=1,
            version=1,
        )
        session.add(fc)
        await session.flush()

        fcase = FailureCase(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            incident_id=source_incident_id,
            outcome_revision_id=outcome_rev_id,
            cluster_id=cluster_id,
            failure_origin_code="TEST_ORIGIN",
            subsystem_version="1.0.0",
            first_divergent_step_category="OBSERVATION",
            classification_status="CLUSTERED",
            classification_version="1.0",
            evidence_digest="digest-123",
        )
        session.add(fcase)

        prop = ImprovementProposal(
            id=proposal_id,
            tenant_id=tenant_id,
            cluster_id=cluster_id,
            proposal_type="PROMPT_REFINEMENT",
            base_policy_version="v1.0",
            target_component="investigator_prompt",
            structured_diff={"template_name": "investigator", "section_name": "main", "original_text": "a", "proposed_text": "b"},
            hypothesis="better prompt",
            expected_benefit="fewer tokens",
            risk_assessment="low",
            evaluation_plan="paired test",
            status="EVALUATION_READY",
            version=1,
            drafter_id="model-1",
        )
        session.add(prop)

        lesson = OperationalLesson(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            lesson_id=lesson_id,
            version=1,
            lesson_type="VERIFIED_RECOVERY",
            title="Sample lesson for evaluation",
            redacted_summary="Summary",
            lifecycle_state="ACTIVE",
            consent_epoch=1,
            confidence_band="MEDIUM",
            valid_from=datetime.now(UTC) - timedelta(hours=1),
            valid_until=datetime.now(UTC) + timedelta(days=30),
            source_outcome_revision_id=outcome_rev_id,
            created_by="reviewer-1",
        )
        session.add(lesson)

        await session.commit()

    return {
        "tenant_id": tenant_id,
        "proposal_id": proposal_id,
        "lesson_id": lesson_id,
        "cluster_id": cluster_id,
        "source_incident_id": source_incident_id,
        "outcome_rev_id": outcome_rev_id,
    }


@pytest.mark.asyncio
async def test_register_manifest_with_proposal_provenance(
    session_factory: async_sessionmaker[AsyncSession],
    manifest_fixture: dict[str, Any],
) -> None:
    svc = CandidateManifestService(session_factory)
    tenant_id = manifest_fixture["tenant_id"]
    proposal_id = manifest_fixture["proposal_id"]

    cand_bundle = CandidateBundle(
        base_policy_version="v1.0",
        target_component="investigator_prompt",
        proposal_diff=PromptRefinementDiff(
            template_name="investigator",
            section_name="main",
            original_text="a",
            proposed_text="b",
        ),
    )
    inc_bundle = IncumbentBundle(
        policy_version="v1.0",
        tool_catalog_version="v1",
    )

    manifest = await svc.register_manifest(
        tenant_id=tenant_id,
        title="Test E3 Proposal Evaluation",
        incumbent_bundle=inc_bundle,
        candidate_bundle=cand_bundle,
        candidate_provenance_type=CandidateProvenanceType.E3_PROPOSAL,
        source_proposal_id=proposal_id,
        model_id="gemini-test",
        model_version="1.0",
        tool_catalog_version="v1",
        a2_grant_version="v1",
        fixture_family="k8s_pod_restart",
        fixture_set_digest="fix-digest-1",
        holdout_set_digest="holdout-digest-1",
        oracle_version="v1",
        adjudication_rule_version="v1",
        source_outcome_revision_ids=[],
        metric_spec=MetricSpec(),
        sample_size_plan=20,
        random_seed=42,
        budget_ceiling_micros=100000,
        max_tokens_ceiling=50000,
        created_by="tester",
    )

    assert manifest.lifecycle_state == ManifestLifecycleState.REGISTERED.value
    assert manifest.manifest_sha256 is not None
    assert manifest.source_proposal_id == proposal_id
    assert manifest.version == 1


@pytest.mark.asyncio
async def test_register_manifest_with_lesson_set_provenance(
    session_factory: async_sessionmaker[AsyncSession],
    manifest_fixture: dict[str, Any],
) -> None:
    svc = CandidateManifestService(session_factory)
    tenant_id = manifest_fixture["tenant_id"]
    lesson_id = manifest_fixture["lesson_id"]

    cand_bundle = CandidateBundle(
        base_policy_version="v1.0",
        target_component="investigator_prompt",
        active_lesson_ids=[lesson_id],
        active_lesson_epoch=1,
    )
    inc_bundle = IncumbentBundle(
        policy_version="v1.0",
        tool_catalog_version="v1",
    )

    manifest = await svc.register_manifest(
        tenant_id=tenant_id,
        title="Test E4 Lesson Set Evaluation",
        incumbent_bundle=inc_bundle,
        candidate_bundle=cand_bundle,
        candidate_provenance_type=CandidateProvenanceType.E4_LESSON_SET,
        source_lesson_ids=[lesson_id],
        model_id="gemini-test",
        model_version="1.0",
        tool_catalog_version="v1",
        a2_grant_version="v1",
        fixture_family="k8s_pod_restart",
        fixture_set_digest="fix-digest-1",
        holdout_set_digest="holdout-digest-1",
        oracle_version="v1",
        adjudication_rule_version="v1",
        source_outcome_revision_ids=[],
        metric_spec=MetricSpec(),
        sample_size_plan=20,
        random_seed=42,
        budget_ceiling_micros=100000,
        max_tokens_ceiling=50000,
        created_by="tester",
    )

    assert manifest.lifecycle_state == ManifestLifecycleState.REGISTERED.value
    assert manifest.candidate_provenance_type == "E4_LESSON_SET"
    assert str(lesson_id) in manifest.source_lesson_ids


@pytest.mark.asyncio
async def test_register_manifest_rejects_holdout_contamination(
    session_factory: async_sessionmaker[AsyncSession],
    manifest_fixture: dict[str, Any],
) -> None:
    svc = CandidateManifestService(session_factory)
    tenant_id = manifest_fixture["tenant_id"]
    proposal_id = manifest_fixture["proposal_id"]
    source_incident_id = manifest_fixture["source_incident_id"]

    cand_bundle = CandidateBundle(
        base_policy_version="v1.0",
        target_component="investigator_prompt",
    )
    inc_bundle = IncumbentBundle(
        policy_version="v1.0",
        tool_catalog_version="v1",
    )

    # Supply holdout incident list containing the candidate's source incident
    with pytest.raises(HoldoutContaminationError) as exc_info:
        await svc.register_manifest(
            tenant_id=tenant_id,
            title="Contaminated Evaluation",
            incumbent_bundle=inc_bundle,
            candidate_bundle=cand_bundle,
            candidate_provenance_type=CandidateProvenanceType.E3_PROPOSAL,
            source_proposal_id=proposal_id,
            model_id="gemini-test",
            model_version="1.0",
            tool_catalog_version="v1",
            a2_grant_version="v1",
            fixture_family="k8s_pod_restart",
            fixture_set_digest="fix-digest-1",
            holdout_set_digest="holdout-digest-1",
            holdout_incident_ids=[source_incident_id],  # Overlaps!
            oracle_version="v1",
            adjudication_rule_version="v1",
            source_outcome_revision_ids=[],
            sample_size_plan=20,
            random_seed=42,
            budget_ceiling_micros=100000,
            max_tokens_ceiling=50000,
            created_by="tester",
        )
    assert "Holdout contamination detected" in str(exc_info.value)


@pytest.mark.asyncio
async def test_freeze_and_cancel_manifest(
    session_factory: async_sessionmaker[AsyncSession],
    manifest_fixture: dict[str, Any],
) -> None:
    svc = CandidateManifestService(session_factory)
    tenant_id = manifest_fixture["tenant_id"]
    proposal_id = manifest_fixture["proposal_id"]

    cand_bundle = CandidateBundle(
        base_policy_version="v1.0",
        target_component="investigator_prompt",
    )
    inc_bundle = IncumbentBundle(
        policy_version="v1.0",
        tool_catalog_version="v1",
    )

    manifest = await svc.register_manifest(
        tenant_id=tenant_id,
        title="Lifecycle Manifest",
        incumbent_bundle=inc_bundle,
        candidate_bundle=cand_bundle,
        candidate_provenance_type=CandidateProvenanceType.E3_PROPOSAL,
        source_proposal_id=proposal_id,
        model_id="gemini-test",
        model_version="1.0",
        tool_catalog_version="v1",
        a2_grant_version="v1",
        fixture_family="k8s_pod_restart",
        fixture_set_digest="fix-digest-1",
        holdout_set_digest="holdout-digest-1",
        oracle_version="v1",
        adjudication_rule_version="v1",
        source_outcome_revision_ids=[],
        sample_size_plan=20,
        random_seed=42,
        budget_ceiling_micros=100000,
        max_tokens_ceiling=50000,
        created_by="tester",
    )

    # 1. Freeze manifest
    frozen = await svc.freeze_manifest(tenant_id=tenant_id, manifest_id=manifest.id, expected_version=1)
    assert frozen.lifecycle_state == ManifestLifecycleState.FROZEN.value
    assert frozen.frozen_at is not None
    assert frozen.version == 2

    # 2. Concurrency Conflict on unexpected version
    with pytest.raises(EvaluationConcurrencyConflictError):
        await svc.freeze_manifest(tenant_id=tenant_id, manifest_id=manifest.id, expected_version=1)

    # 3. Cancel manifest
    cancelled = await svc.cancel_manifest(tenant_id=tenant_id, manifest_id=manifest.id)
    assert cancelled.lifecycle_state == ManifestLifecycleState.CANCELLED.value
    assert cancelled.version == 3


@pytest.mark.asyncio
async def test_register_manifest_with_combined_offline_study_provenance(
    session_factory: async_sessionmaker[AsyncSession],
    manifest_fixture: dict[str, Any],
) -> None:
    svc = CandidateManifestService(session_factory)
    tenant_id = manifest_fixture["tenant_id"]
    proposal_id = manifest_fixture["proposal_id"]
    lesson_id = manifest_fixture["lesson_id"]

    cand_bundle = CandidateBundle(
        base_policy_version="v1.0",
        target_component="investigator_prompt",
        active_lesson_ids=[lesson_id],
    )
    inc_bundle = IncumbentBundle(
        policy_version="v1.0",
        tool_catalog_version="v1",
    )

    manifest = await svc.register_manifest(
        tenant_id=tenant_id,
        title="Test Combined Offline Study Evaluation",
        incumbent_bundle=inc_bundle,
        candidate_bundle=cand_bundle,
        candidate_provenance_type=CandidateProvenanceType.COMBINED_OFFLINE_STUDY,
        source_proposal_id=proposal_id,
        source_lesson_ids=[lesson_id],
        model_id="gemini-test",
        model_version="1.0",
        tool_catalog_version="v1",
        a2_grant_version="v1",
        fixture_family="k8s_pod_restart",
        fixture_set_digest="fix-digest-1",
        holdout_set_digest="holdout-digest-1",
        oracle_version="v1",
        adjudication_rule_version="v1",
        sample_size_plan=20,
        random_seed=42,
        budget_ceiling_micros=100000,
        max_tokens_ceiling=50000,
        created_by="tester",
    )

    assert manifest.lifecycle_state == ManifestLifecycleState.REGISTERED.value
    assert manifest.candidate_provenance_type == "COMBINED_OFFLINE_STUDY"
    assert manifest.source_proposal_id == proposal_id
    assert str(lesson_id) in manifest.source_lesson_ids


@pytest.mark.asyncio
async def test_register_manifest_rejects_cross_tenant_outcome_revision(
    session_factory: async_sessionmaker[AsyncSession],
    manifest_fixture: dict[str, Any],
) -> None:
    svc = CandidateManifestService(session_factory)
    tenant_id = manifest_fixture["tenant_id"]
    proposal_id = manifest_fixture["proposal_id"]
    foreign_outcome_rev_id = uuid.uuid4()  # Does not exist for this tenant

    cand_bundle = CandidateBundle(
        base_policy_version="v1.0",
        target_component="investigator_prompt",
    )
    inc_bundle = IncumbentBundle(
        policy_version="v1.0",
        tool_catalog_version="v1",
    )

    with pytest.raises(CandidateProvenanceValidationError) as exc_info:
        await svc.register_manifest(
            tenant_id=tenant_id,
            title="Foreign Revision Manifest",
            incumbent_bundle=inc_bundle,
            candidate_bundle=cand_bundle,
            candidate_provenance_type=CandidateProvenanceType.E3_PROPOSAL,
            source_proposal_id=proposal_id,
            source_outcome_revision_ids=[foreign_outcome_rev_id],
            fixture_set_digest="fix-1",
            holdout_set_digest="holdout-1",
            created_by="attacker",
        )

    assert "not found for tenant" in str(exc_info.value)

