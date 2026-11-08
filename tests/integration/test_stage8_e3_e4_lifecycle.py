"""Full end-to-end integration lifecycle test for Stage 8: E3 & E4 with P3 DAG projection."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.failure import (
    DivergentStepCategory,
    ProposalStatus,
    ProposalType,
    ReviewDecision,
    TargetComponent,
)
from backend.domain.lesson import LessonLifecycle, LessonType
from backend.learning.clusters import FailureClusterService
from backend.learning.failure_taxonomy import FailureClassifier
from backend.learning.lesson_registry import LessonRegistry
from backend.learning.proposal_service import ProposalService
from backend.learning.retrieval import LessonRetriever
from backend.persistence.models import ConsentEpoch, OutcomeRevision
from backend.persistence.repositories import IncidentRepository
from backend.trace.service import TraceService

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_stage8_complete_e3_e4_p3_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Validate full flow: incident -> E2 outcome -> E3 case -> cluster -> proposal -> E4 lesson -> P3 trace DAG."""
    # 1. Tenant consent enabled
    async with session_factory() as session:
        epoch = ConsentEpoch(
            tenant_id=isolated_tenant,
            current_epoch=1,
            reuse_allowed=True,
        )
        session.add(epoch)
        await session.commit()

    # 2. Admit Incident
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"stage8-life-{uuid.uuid4()}",
        target_id="k8s:deploy:payment-processor",
        request_sha256="8" * 64,
    )
    inc_id = admit.response.id

    # 3. Adjudicate Outcome as VERIFIED_SUCCESS
    async with session_factory() as session:
        rev = OutcomeRevision(
            id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            revision_no=1,
            rule_version="v1",
            label="VERIFIED_SUCCESS",
            confidence_band="HIGH",
            supporting_observation_ids=[],
            adjudication_metadata={"check": "synthetic-prober-green"},
        )
        session.add(rev)
        await session.commit()
        await session.refresh(rev)
        outcome_rev_id = rev.id

    # 4. E3: Classify Failure Case (counterexample/failure analysis)
    classifier = FailureClassifier(session_factory)
    case = await classifier.classify_case(
        tenant_id=isolated_tenant,
        incident_id=inc_id,
        outcome_revision_id=outcome_rev_id,
        outcome_label="VERIFIED_SUCCESS",
        divergent_step="probe_connection_pool",
        first_divergent_step_category=DivergentStepCategory.READBACK_MISMATCH.value,
        subsystem_version="v1.0.0",
    )
    assert case.classification_status in ("CLASSIFIED", "UNCERTAIN")

    # 5. E3: Assign to Signature Cluster
    cluster_service = FailureClusterService(session_factory)
    cluster = await cluster_service.assign_to_cluster(
        tenant_id=isolated_tenant,
        case_id=case.id,
        failure_origin_code=case.failure_origin_code,
        first_divergent_step_category=case.first_divergent_step_category,
        subsystem_version=case.subsystem_version,
    )
    assert cluster.case_count >= 1
    assert cluster.exemplar_case_id is not None

    # 6. E3: Draft and Review Improvement Proposal
    prop_service = ProposalService(session_factory)
    proposal = await prop_service.create_draft(
        tenant_id=isolated_tenant,
        cluster_id=cluster.id,
        proposal_type=ProposalType.PROMPT_REFINEMENT.value,
        base_policy_version="v1",
        target_component=TargetComponent.INVESTIGATOR_A1_PROMPT.value,
        structured_diff={
            "template_id": "prompt_v1",
            "target_section": "reconciliation_guidelines",
            "proposed_content": "Enforce explicit 5s wait before issuing connection pool readback.",
        },
        hypothesis="Eliminates readback race condition false failures.",
        expected_benefit="Stabilizes probe confirmation.",
        risk_assessment="Adds 5s to probe completion.",
        evaluation_plan="Evaluate via S1 replay across 100 historical incidents.",
        drafter_id="llm-drafter-7",
    )
    assert proposal.status == ProposalStatus.DRAFT.value

    # Submit proposal
    submitted_prop = await prop_service.submit_proposal(
        tenant_id=isolated_tenant,
        proposal_id=proposal.id,
        expected_version=1,
    )
    assert submitted_prop.status == ProposalStatus.PROPOSED.value

    # Review proposal: human approval
    approved_prop = await prop_service.review_proposal(
        tenant_id=isolated_tenant,
        proposal_id=proposal.id,
        expected_version=2,
        reviewer_id="human-sre-lead",
        reviewer_role="sre_lead",
        decision=ReviewDecision.APPROVED.value,
        comments="Approved for offline evaluation.",
    )
    assert approved_prop.status == ProposalStatus.EVALUATION_READY.value

    # 7. E4: Draft and Activate Operational Lesson
    lesson_registry = LessonRegistry(session_factory)
    lesson = await lesson_registry.draft_lesson(
        tenant_id=isolated_tenant,
        title="Payment Processor Pool Drain Procedure",
        summary="Always issue connection pool drain signal before scaling down replicas.",
        source_outcome_revision_id=outcome_rev_id,
        lesson_type=LessonType.VERIFIED_RECOVERY.value,
        source_cluster_id=cluster.id,
        proposal_id=approved_prop.id,
        confidence_band="HIGH",
    )
    assert lesson.lifecycle_state == LessonLifecycle.SOURCE_CHECKED.value

    # Validate and activate lesson
    active_lesson = await lesson_registry.validate_and_activate(
        tenant_id=isolated_tenant,
        lesson_id=lesson.lesson_id,
        version=1,
        reviewer_id="human-commander",
        reviewer_role="incident_commander",
    )
    assert active_lesson.lifecycle_state == LessonLifecycle.ACTIVE.value

    # 8. E4: Retrieve lesson with PostgreSQL FTS & cite usage
    retriever = LessonRetriever(session_factory)
    citations = await retriever.retrieve_lessons(
        tenant_id=isolated_tenant,
        query_text="payment processor pool drain",
        incident_id=inc_id,
    )
    assert len(citations) == 1
    assert citations[0].lesson_id == active_lesson.lesson_id

    # 9. P3: Project Trace DAG and verify acyclicity
    trace_service = TraceService(session_factory)
    trace_graph = await trace_service.get_incident_trace_graph(isolated_tenant, inc_id)

    assert trace_graph.has_cycles is False
    node_kinds = {n.kind.value for n in trace_graph.nodes}
    assert "INCIDENT" in node_kinds
    assert "OUTCOME" in node_kinds
    assert "FAILURE_CASE" in node_kinds
    assert "FAILURE_CLUSTER" in node_kinds
    assert "PROPOSAL" in node_kinds
    assert "LESSON" in node_kinds
