"""End-to-End Lifecycle Integration Test for Stage 7: E1 Experience Capture and E2 Outcome Adjudication.

Verifies full progression:
Incident Admission -> Step -> Evidence -> Approval -> Receipt -> Outcome Adjudication -> Experience Capture -> Workbench & Trace DAG Projection -> Consent Revocation -> Purge.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.approvals.service import ApprovalService
from backend.domain.approval import ApprovalCreateRequest, ApprovalDecisionRequest
from backend.domain.experience import ExperienceClassification, ExperienceState
from backend.domain.outcome import (
    ConfidenceBand,
    ObservationCreateRequest,
    OperatorFeedbackRequest,
    OutcomeLabel,
    SourceKind,
)
from backend.domain.trace import TraceNodeKind
from backend.experience.consent import ConsentManager
from backend.experience.projector import ExperienceProjector
from backend.experience.repository import ExperienceRepository
from backend.experience.retention import RetentionWorker
from backend.outcomes.service import OutcomeService
from backend.persistence.repositories import IncidentRepository
from backend.trace.service import TraceService
from backend.workbench.service import WorkbenchService

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_stage7_complete_e1_e2_lifecycle_and_view_integration(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    headers = {"X-Tenant-ID": str(isolated_tenant)}

    # 1. Tenant explicitly grants experience reuse consent
    consent_mgr = ConsentManager(session_factory)
    consent_status = await consent_mgr.grant_consent(
        tenant_id=isolated_tenant,
        actor_id="lead-admin",
        actor_role="incident_commander",
        reason="Initial consent grant for lab",
    )
    assert consent_status.reuse_allowed is True
    assert consent_status.current_epoch == 1

    # 2. Admit an incident
    inc_repo = IncidentRepository(session_factory)
    client_key = f"e2e-lifecycle-{uuid.uuid4()}"
    target_id = "k8s:deployment:order-processor"
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=client_key,
        target_id=target_id,
        request_sha256="7" * 64,
    )
    inc_id = admit.response.id

    # 3. Create Approval Request & Approve it
    app_svc = ApprovalService(session_factory)
    plan_digest = "8" * 64
    action_digest = "9" * 64
    app_create = ApprovalCreateRequest(
        plan_id="plan-restart-deployment",
        canonical_plan_digest=plan_digest,
        action_digest=action_digest,
        action_payload={"action_kind": "rollout_restart", "target": target_id},
        target_id=target_id,
        expected_resource_version="v10",
        policy_version="v1",
        ttl_seconds=300,
    )
    app_resp = await app_svc.create_approval_request(isolated_tenant, inc_id, app_create)
    assert app_resp.state.value == "PENDING"

    decision = await app_svc.approve(
        isolated_tenant,
        app_resp.id,
        ApprovalDecisionRequest(
            reviewer_id="lead-sre-alice",
            reviewer_role="incident_commander",
            reason="Approved rollout restart to clear hung queue",
            expected_incident_status_version=1,
        ),
    )
    assert decision.state.value == "APPROVED"

    # 4. Record Outcome Observations
    outcome_svc = OutcomeService(session_factory)

    # Step 4a: Adapter ACK alone reports success -> OBSERVED (not verified success)
    _ack_obs, ack_rev = await outcome_svc.record_observation(
        isolated_tenant,
        inc_id,
        ObservationCreateRequest(
            source_kind=SourceKind.ADAPTER_ACK,
            source_reference="adapter:k8s:v1",
            result_payload={"status": "SUCCESS", "message": "Rollout restart triggered"},
            idempotency_key=f"ack-{uuid.uuid4()}",
        ),
    )
    assert ack_rev.label == OutcomeLabel.OBSERVED
    assert ack_rev.confidence_band == ConfidenceBand.LOW

    # Step 4b: Independent black-box workload probe reports PASS -> VERIFIED_SUCCESS
    _indep_obs, indep_rev = await outcome_svc.record_observation(
        isolated_tenant,
        inc_id,
        ObservationCreateRequest(
            source_kind=SourceKind.INDEPENDENT_WORKLOAD_CHECK,
            source_reference="synthetics:order-throughput-check",
            result_payload={"status": "PASS", "tps": 450, "p99_latency_ms": 32},
            idempotency_key=f"indep-{uuid.uuid4()}",
        ),
    )
    assert indep_rev.label == OutcomeLabel.VERIFIED_SUCCESS
    assert indep_rev.confidence_band == ConfidenceBand.HIGH
    assert indep_rev.revision_no == 2

    # 5. Project Governed Experience Capsule
    projector = ExperienceProjector(session_factory)
    capsule_resp = await projector.project_experience(
        tenant_id=isolated_tenant,
        incident_id=inc_id,
        custom_summary=(
            "Order processor hung queue cleared with rollout restart. "
            "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.dummy and password: secretPass999 removed."
        ),
        classification=ExperienceClassification.INVESTIGATION_SUCCESS,
        model_id="gemini-1.5-pro",
        model_version="2026-v1",
        evidence_manifest_sha256="a" * 64,
    )
    assert capsule_resp is not None
    assert capsule_resp.lifecycle_state == ExperienceState.ELIGIBLE
    assert capsule_resp.outcome_id == indep_rev.id
    assert capsule_resp.outcome_revision_no == 2
    assert "secretPass999" not in (capsule_resp.summary_markdown or "")
    assert "[REDACTED:" in (capsule_resp.summary_markdown or "")

    # 6. Verify P1 Workbench Snapshot & ETag Integration
    wb_svc = WorkbenchService(session_factory)
    snapshot, etag = await wb_svc.get_workbench_snapshot(isolated_tenant, inc_id)
    assert snapshot.outcome_label == "VERIFIED_SUCCESS"
    assert snapshot.outcome_revision_no == 2
    assert snapshot.outcome_is_disputed is False
    assert "VERIFIED_SUCCESS" in etag

    # Conditional GET via HTTP API
    res_wb = await api_client.get(f"/v1/incidents/{inc_id}/workbench", headers=headers)
    assert res_wb.status_code == 200
    assert res_wb.headers.get("ETag") == etag
    assert res_wb.json()["outcome_label"] == "VERIFIED_SUCCESS"

    # Matching If-None-Match returns 304 Not Modified
    res_wb_cached = await api_client.get(
        f"/v1/incidents/{inc_id}/workbench",
        headers={**headers, "If-None-Match": etag},
    )
    assert res_wb_cached.status_code == 304

    # 7. Verify P3 Trace Explorer Graph & Acyclicity
    trace_svc = TraceService(session_factory)
    trace_graph = await trace_svc.get_incident_trace_graph(isolated_tenant, inc_id)
    assert trace_graph.has_cycles is False

    node_kinds = {n.kind for n in trace_graph.nodes}
    assert TraceNodeKind.INCIDENT in node_kinds
    assert TraceNodeKind.APPROVAL in node_kinds
    assert TraceNodeKind.OUTCOME in node_kinds
    assert TraceNodeKind.EXPERIENCE in node_kinds

    # 8. Dispute Injection: Operator reports delayed order drops
    feedback_resp = await outcome_svc.submit_operator_feedback(
        isolated_tenant,
        inc_id,
        OperatorFeedbackRequest(
            label=OutcomeLabel.DISPUTED,
            reason="Customer complaints received regarding duplicate order charges",
            actor_id="incident-lead-bob",
            actor_role="incident_commander",
            expected_revision_no=2,
        ),
    )
    assert feedback_resp.revision_no == 3
    assert feedback_resp.label == OutcomeLabel.DISPUTED

    # Invariant: Linked experience capsule is automatically quarantined
    exp_repo = ExperienceRepository(session_factory)
    quarantined_cap = await exp_repo.get_capsule(isolated_tenant, capsule_resp.id)
    assert quarantined_cap is not None
    assert quarantined_cap.lifecycle_state == ExperienceState.QUARANTINED
    assert quarantined_cap.outcome_revision_no == 3

    # Workbench ETag updated due to dispute
    snapshot_after_dispute, new_etag = await wb_svc.get_workbench_snapshot(
        isolated_tenant, inc_id
    )
    assert snapshot_after_dispute.outcome_label == "DISPUTED"
    assert snapshot_after_dispute.outcome_is_disputed is True
    assert new_etag != etag

    # 9. Tenant Revokes Experience Consent -> Synchronous Invalidation
    revoke_status = await consent_mgr.revoke_consent(
        tenant_id=isolated_tenant,
        actor_id="admin-security",
        actor_role="incident_commander",
        reason="Tenant GDPR / Privacy revocation",
    )
    assert revoke_status.reuse_allowed is False
    assert revoke_status.current_epoch == 2

    # Query immediately reflects zero eligible items with 0ms delay
    eligible_list = await exp_repo.list_eligible_experiences(isolated_tenant)
    assert eligible_list.total_eligible == 0
    assert len(eligible_list.items) == 0

    # 10. Background Retention Worker Purges Payload
    worker = RetentionWorker(session_factory)
    purged = await worker.purge_revoked_capsules(isolated_tenant, batch_size=10)
    assert purged >= 1

    purged_cap = await exp_repo.get_capsule(isolated_tenant, capsule_resp.id)
    assert purged_cap is not None
    assert purged_cap.lifecycle_state == ExperienceState.PURGED
    assert purged_cap.summary_markdown is None  # Payload completely erased
