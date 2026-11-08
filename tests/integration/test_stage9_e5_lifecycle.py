"""Integration tests for Stage 9 Feature E5: Candidate-to-Incumbent Regression Gate.

Verifies:
1. Full end-to-end API lifecycle: create -> freeze -> run -> trials -> get -> review.
2. CAS optimistic concurrency and RBAC on review submissions.
3. Synchronous query-time dynamic invalidation on outcome supersession (INV-E5-9).
4. P3 Trace DAG projection containing EVALUATION_MANIFEST and EVALUATION_REPORT.
5. Strict multi-tenant isolation across all evaluation resources.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.failure import (
    DivergentStepCategory,
    ProposalStatus,
    ProposalType,
    ReviewDecision as ProposalReviewDecision,
    TargetComponent,
)
from backend.domain.evaluation import (
    CandidateProvenanceType,
    ManifestLifecycleState,
    ReviewDecision as EvalReviewDecision,
)
from backend.learning.clusters import FailureClusterService
from backend.learning.failure_taxonomy import FailureClassifier
from backend.learning.proposal_service import ProposalService
from backend.persistence.models import ConsentEpoch, OutcomeRevision
from backend.persistence.repositories import IncidentRepository
from backend.trace.service import TraceService

pytestmark = pytest.mark.postgres


async def _setup_e3_proposal_hierarchy(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Helper to admit an incident, adjudicate an outcome, and create an approved proposal."""
    # 1. Tenant consent enabled
    async with session_factory() as session:
        epoch = ConsentEpoch(
            tenant_id=tenant_id,
            current_epoch=1,
            reuse_allowed=True,
        )
        session.add(epoch)
        await session.commit()

    # 2. Admit Incident
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=tenant_id,
        client_key=f"stage9-int-{uuid.uuid4()}",
        target_id="k8s:deploy:checkout-service",
        request_sha256="9" * 64,
    )
    inc_id = admit.response.id

    # 3. Adjudicate Outcome as VERIFIED_SUCCESS
    async with session_factory() as session:
        rev = OutcomeRevision(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            incident_id=inc_id,
            revision_no=1,
            rule_version="v1",
            label="VERIFIED_SUCCESS",
            confidence_band="HIGH",
            supporting_observation_ids=[],
            adjudication_metadata={"check": "checkout-probe-ok"},
        )
        session.add(rev)
        await session.commit()
        await session.refresh(rev)
        outcome_rev_id = rev.id

    # 4. Classify Failure Case
    classifier = FailureClassifier(session_factory)
    case = await classifier.classify_case(
        tenant_id=tenant_id,
        incident_id=inc_id,
        outcome_revision_id=outcome_rev_id,
        outcome_label="VERIFIED_SUCCESS",
        divergent_step="verify_cart_integrity",
        first_divergent_step_category=DivergentStepCategory.READBACK_MISMATCH.value,
        subsystem_version="v1.0.0",
    )

    # 5. Assign to Cluster
    cluster_service = FailureClusterService(session_factory)
    cluster = await cluster_service.assign_to_cluster(
        tenant_id=tenant_id,
        case_id=case.id,
        failure_origin_code=case.failure_origin_code,
        first_divergent_step_category=case.first_divergent_step_category,
        subsystem_version=case.subsystem_version,
    )

    # 6. Draft, Submit, and Approve Proposal
    prop_service = ProposalService(session_factory)
    proposal = await prop_service.create_draft(
        tenant_id=tenant_id,
        cluster_id=cluster.id,
        proposal_type=ProposalType.PROMPT_REFINEMENT.value,
        base_policy_version="v1.0",
        target_component=TargetComponent.INVESTIGATOR_A1_PROMPT.value,
        structured_diff={
            "template_id": "prompt_v1",
            "target_section": "reconciliation_guidelines",
            "proposed_content": "Always verify cart session lock before releasing read reservation.",
        },
        hypothesis="Prevents cart session lock races.",
        expected_benefit="Zero stale cart checkouts.",
        risk_assessment="Adds 100ms lock check latency.",
        evaluation_plan="Evaluate via paired S1 test across holdouts.",
        drafter_id="llm-proposal-bot",
    )

    submitted = await prop_service.submit_proposal(
        tenant_id=tenant_id,
        proposal_id=proposal.id,
        expected_version=1,
    )

    approved = await prop_service.review_proposal(
        tenant_id=tenant_id,
        proposal_id=submitted.id,
        expected_version=2,
        reviewer_id="human-sre-lead",
        reviewer_role="sre_lead",
        decision=ProposalReviewDecision.APPROVED.value,
        comments="Approved for candidate regression evaluation.",
    )

    return inc_id, outcome_rev_id, approved.id


@pytest.mark.asyncio
async def test_stage9_full_api_evaluation_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    api_client: AsyncClient,
) -> None:
    """Tests complete REST API lifecycle for candidate evaluation."""
    inc_id, outcome_rev_id, proposal_id = await _setup_e3_proposal_hierarchy(
        session_factory, isolated_tenant
    )
    headers = {"X-Tenant-ID": str(isolated_tenant)}

    payload: dict[str, Any] = {
        "title": "Cart Session Lock Refinement Evaluation",
        "incumbent_bundle": {
            "policy_version": "v1.0",
            "tool_catalog_version": "v1",
        },
        "candidate_bundle": {
            "base_policy_version": "v1.0",
            "target_component": TargetComponent.INVESTIGATOR_A1_PROMPT.value,
            "proposal_diff": {
                "template_name": "prompt_v1",
                "section_name": "reconciliation_guidelines",
                "original_text": "Verify cart session.",
                "proposed_text": "Always verify cart session lock before releasing read reservation.",
            },
        },
        "candidate_provenance_type": CandidateProvenanceType.E3_PROPOSAL.value,
        "source_proposal_id": str(proposal_id),
        "source_outcome_revision_ids": [str(outcome_rev_id)],
        "fixture_family": "k8s_container_crash",
        "fixture_set_digest": "a" * 64,
        "holdout_set_digest": "b" * 64,
        "sample_size_plan": 10,
        "budget_ceiling_micros": 500000,
        "max_tokens_ceiling": 50000,
        "auto_freeze": False,
    }

    # 1. Create Evaluation Manifest (POST /v1/evaluations)
    resp = await api_client.post("/v1/evaluations", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    manifest_data = resp.json()
    manifest_id = manifest_data["id"]
    assert manifest_data["lifecycle_state"] == ManifestLifecycleState.REGISTERED.value
    assert manifest_data["version"] == 1
    assert manifest_data["manifest_sha256"] is not None

    # 2. List Evaluations (GET /v1/evaluations)
    list_resp = await api_client.get("/v1/evaluations", headers=headers)
    assert list_resp.status_code == 200
    assert any(m["id"] == manifest_id for m in list_resp.json())

    # 3. Freeze Manifest (POST /v1/evaluations/{id}/freeze)
    freeze_resp = await api_client.post(
        f"/v1/evaluations/{manifest_id}/freeze",
        json={"expected_version": 1},
        headers=headers,
    )
    assert freeze_resp.status_code == 200
    frozen_data = freeze_resp.json()
    assert frozen_data["lifecycle_state"] == ManifestLifecycleState.FROZEN.value
    assert frozen_data["version"] == 2
    assert frozen_data["frozen_at"] is not None

    # Concurrency conflict on freeze
    conflict_resp = await api_client.post(
        f"/v1/evaluations/{manifest_id}/freeze",
        json={"expected_version": 1},
        headers=headers,
    )
    assert conflict_resp.status_code == 409

    # 4. Dispatch Run (POST /v1/evaluations/{id}/run)
    run_resp = await api_client.post(
        f"/v1/evaluations/{manifest_id}/run",
        json={"worker_id": "test-worker-alpha", "lease_seconds": 60},
        headers=headers,
    )
    assert run_resp.status_code == 200, run_resp.text
    report_data = run_resp.json()
    assert report_data["manifest_id"] == manifest_id
    assert report_data["total_pairs_planned"] == 10
    assert report_data["total_pairs_completed"] == 10
    assert report_data["invalid_pairs_count"] == 0
    assert "recovery_delta" in report_data
    assert len(report_data["gate_verdicts"]) == 6

    # 5. List Trials (GET /v1/evaluations/{id}/trials)
    trials_resp = await api_client.get(
        f"/v1/evaluations/{manifest_id}/trials",
        headers=headers,
    )
    assert trials_resp.status_code == 200
    trials = trials_resp.json()
    assert len(trials) == 20  # 10 pairs * 2 arms
    arms = {t["arm"] for t in trials}
    assert arms == {"INCUMBENT", "CANDIDATE"}

    # 6. Fetch Evaluation Detail (GET /v1/evaluations/{id})
    detail_resp = await api_client.get(f"/v1/evaluations/{manifest_id}", headers=headers)
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["manifest"]["lifecycle_state"] == ManifestLifecycleState.COMPLETED.value
    assert detail["trials_total"] == 20
    assert detail["trials_completed"] == 20
    assert detail["latest_report"]["id"] == report_data["id"]

    # 7. Submit Review: RBAC validation
    # Unauthorized role
    unauth_review = await api_client.post(
        f"/v1/evaluations/{manifest_id}/review",
        json={
            "reviewer_id": "guest-user",
            "reviewer_role": "guest_observer",
            "decision": EvalReviewDecision.ACCEPTED.value,
            "comments": "Looks fine to me.",
            "expected_version": 1,
        },
        headers=headers,
    )
    assert unauth_review.status_code == 403

    # Authorized role with valid expected_version
    auth_review = await api_client.post(
        f"/v1/evaluations/{manifest_id}/review",
        json={
            "reviewer_id": "lead-evaluator",
            "reviewer_role": "incident_commander",
            "decision": EvalReviewDecision.ACCEPTED.value,
            "comments": "Statistically verified superior recovery with zero safety violations.",
            "expected_version": 1,
        },
        headers=headers,
    )
    assert auth_review.status_code == 200
    reviewed_data = auth_review.json()
    assert reviewed_data["review_decision"] == EvalReviewDecision.ACCEPTED.value
    assert reviewed_data["reviewer_role"] == "incident_commander"
    assert reviewed_data["version"] == 2

    # Repeat review rejected
    repeat_review = await api_client.post(
        f"/v1/evaluations/{manifest_id}/review",
        json={
            "reviewer_id": "lead-evaluator-2",
            "reviewer_role": "incident_commander",
            "decision": EvalReviewDecision.REJECTED.value,
            "comments": "Duplicate review attempt.",
            "expected_version": 2,
        },
        headers=headers,
    )
    assert repeat_review.status_code == 400


@pytest.mark.asyncio
async def test_stage9_dynamic_query_time_invalidation(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    api_client: AsyncClient,
) -> None:
    """Verifies synchronous query-time invalidation (INV-E5-9) on source outcome supersession."""
    inc_id, outcome_rev_id, proposal_id = await _setup_e3_proposal_hierarchy(
        session_factory, isolated_tenant
    )
    headers = {"X-Tenant-ID": str(isolated_tenant)}

    # Register and run evaluation
    create_resp = await api_client.post(
        "/v1/evaluations",
        json={
            "title": "Dynamic Invalidation Test Manifest",
            "incumbent_bundle": {
                "policy_version": "v1.0",
                "tool_catalog_version": "v1",
            },
            "candidate_bundle": {
                "base_policy_version": "v1.0",
                "target_component": TargetComponent.INVESTIGATOR_A1_PROMPT.value,
                "proposal_diff": {
                    "template_name": "prompt_v1",
                    "section_name": "reconciliation_guidelines",
                    "original_text": "Verify cart session.",
                    "proposed_text": "Always verify cart session lock before releasing read reservation.",
                },
            },
            "candidate_provenance_type": CandidateProvenanceType.E3_PROPOSAL.value,
            "source_proposal_id": str(proposal_id),
            "source_outcome_revision_ids": [str(outcome_rev_id)],
            "fixture_set_digest": "c" * 64,
            "holdout_set_digest": "d" * 64,
            "sample_size_plan": 10,
            "auto_freeze": True,
        },
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    manifest_id = create_resp.json()["id"]

    # Run evaluation
    run_resp = await api_client.post(
        f"/v1/evaluations/{manifest_id}/run",
        json={"worker_id": "worker-dyn-inval"},
        headers=headers,
    )
    assert run_resp.status_code == 200
    initial_report = run_resp.json()
    assert initial_report["promotion_eligible"] is True

    # Now simulate source outcome revision supersession (revision 2 created for same incident)
    async with session_factory() as session:
        rev2 = OutcomeRevision(
            id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            revision_no=2,
            rule_version="v2",
            label="DISPUTED",
            confidence_band="LOW",
            supporting_observation_ids=[],
            adjudication_metadata={"check": "superseded-by-audit"},
        )
        session.add(rev2)
        await session.commit()

    # Query GET /v1/evaluations/{manifest_id}
    detail_resp = await api_client.get(f"/v1/evaluations/{manifest_id}", headers=headers)
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    latest_report = detail["latest_report"]
    assert latest_report is not None
    assert latest_report["promotion_eligible"] is False
    assert "superseded" in latest_report["invalidation_reason"].lower()

    # Attempt to review report on invalidated source returns HTTP 409 Conflict
    review_resp = await api_client.post(
        f"/v1/evaluations/{manifest_id}/review",
        json={
            "reviewer_id": "sre-ops",
            "reviewer_role": "sre_lead",
            "decision": EvalReviewDecision.ACCEPTED.value,
            "comments": "Should fail due to invalid source.",
            "expected_version": 1,
        },
        headers=headers,
    )
    assert review_resp.status_code == 409
    assert "invalidated" in review_resp.text.lower() or "superseded" in review_resp.text.lower()


@pytest.mark.asyncio
async def test_stage9_p3_trace_dag_projection_e5(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    api_client: AsyncClient,
) -> None:
    """Verifies P3 Trace DAG incorporates EVALUATION_MANIFEST and EVALUATION_REPORT nodes and edges."""
    inc_id, outcome_rev_id, proposal_id = await _setup_e3_proposal_hierarchy(
        session_factory, isolated_tenant
    )
    headers = {"X-Tenant-ID": str(isolated_tenant)}

    # Register, freeze, and run evaluation
    create_resp = await api_client.post(
        "/v1/evaluations",
        json={
            "title": "Trace DAG Projection Manifest",
            "incumbent_bundle": {
                "policy_version": "v1.0",
                "tool_catalog_version": "v1",
            },
            "candidate_bundle": {
                "base_policy_version": "v1.0",
                "target_component": TargetComponent.INVESTIGATOR_A1_PROMPT.value,
                "proposal_diff": {
                    "template_name": "prompt_v1",
                    "section_name": "reconciliation_guidelines",
                    "original_text": "Verify cart session.",
                    "proposed_text": "Always verify cart session lock before releasing read reservation.",
                },
            },
            "candidate_provenance_type": CandidateProvenanceType.E3_PROPOSAL.value,
            "source_proposal_id": str(proposal_id),
            "source_outcome_revision_ids": [str(outcome_rev_id)],
            "fixture_set_digest": "e" * 64,
            "holdout_set_digest": "f" * 64,
            "sample_size_plan": 10,
            "auto_freeze": True,
        },
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    manifest_id = create_resp.json()["id"]

    await api_client.post(
        f"/v1/evaluations/{manifest_id}/run",
        json={"worker_id": "worker-trace-test"},
        headers=headers,
    )

    # Project incident trace graph via TraceService
    trace_service = TraceService(session_factory)
    trace_graph = await trace_service.get_incident_trace_graph(isolated_tenant, inc_id)

    assert trace_graph.has_cycles is False
    node_kinds = {n.kind.value for n in trace_graph.nodes}
    assert "EVALUATION_MANIFEST" in node_kinds
    assert "EVALUATION_REPORT" in node_kinds

    relations = {e.relation.value for e in trace_graph.edges}
    assert "EVALUATED_IN" in relations
    assert "REPORTED_BY" in relations


@pytest.mark.asyncio
async def test_stage9_multi_tenant_isolation(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    second_isolated_tenant: uuid.UUID,
    api_client: AsyncClient,
) -> None:
    """Verifies that evaluation manifests and reports are strictly tenant-isolated."""
    _, outcome_rev_id, proposal_id = await _setup_e3_proposal_hierarchy(
        session_factory, isolated_tenant
    )
    tenant_a_headers = {"X-Tenant-ID": str(isolated_tenant)}
    tenant_b_headers = {"X-Tenant-ID": str(second_isolated_tenant)}

    # Create manifest in Tenant A
    create_resp = await api_client.post(
        "/v1/evaluations",
        json={
            "title": "Tenant A Secret Candidate",
            "incumbent_bundle": {
                "policy_version": "v1.0",
                "tool_catalog_version": "v1",
            },
            "candidate_bundle": {
                "base_policy_version": "v1.0",
                "target_component": TargetComponent.INVESTIGATOR_A1_PROMPT.value,
                "proposal_diff": {
                    "template_name": "prompt_v1",
                    "section_name": "reconciliation_guidelines",
                    "original_text": "Verify cart session.",
                    "proposed_text": "Always verify cart session lock before releasing read reservation.",
                },
            },
            "candidate_provenance_type": CandidateProvenanceType.E3_PROPOSAL.value,
            "source_proposal_id": str(proposal_id),
            "source_outcome_revision_ids": [str(outcome_rev_id)],
            "fixture_set_digest": "1" * 64,
            "holdout_set_digest": "2" * 64,
            "sample_size_plan": 5,
            "auto_freeze": False,
        },
        headers=tenant_a_headers,
    )
    assert create_resp.status_code == 201
    manifest_id = create_resp.json()["id"]

    # Tenant B tries to fetch Tenant A's manifest -> 404
    get_b_resp = await api_client.get(f"/v1/evaluations/{manifest_id}", headers=tenant_b_headers)
    assert get_b_resp.status_code == 404

    # Tenant B tries to freeze Tenant A's manifest -> 404
    freeze_b_resp = await api_client.post(
        f"/v1/evaluations/{manifest_id}/freeze",
        json={"expected_version": 1},
        headers=tenant_b_headers,
    )
    assert freeze_b_resp.status_code == 404

    # Tenant B tries to run Tenant A's manifest -> 404
    run_b_resp = await api_client.post(
        f"/v1/evaluations/{manifest_id}/run",
        json={"worker_id": "malicious-worker"},
        headers=tenant_b_headers,
    )
    assert run_b_resp.status_code == 404

    # Missing X-Tenant-ID -> 401 Unauthorized
    anon_resp = await api_client.get(f"/v1/evaluations/{manifest_id}")
    assert anon_resp.status_code == 401
