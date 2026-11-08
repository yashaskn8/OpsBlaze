"""Unit and API tests for Feature E2 — Independent Outcome Adjudication Ledger."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.approval import ApprovalRoleForbiddenError
from backend.domain.experience import ExperienceClassification, ExperienceState
from backend.domain.outcome import (
    ConfidenceBand,
    ObservationCreateRequest,
    OperatorFeedbackRequest,
    OutcomeConflictError,
    OutcomeLabel,
    SelfGradingForbiddenError,
    SourceKind,
)
from backend.experience.consent import ConsentManager
from backend.experience.projector import ExperienceProjector
from backend.experience.repository import ExperienceRepository
from backend.outcomes.service import OutcomeService
from backend.persistence.repositories import IncidentRepository

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_e2_adapter_ack_only_produces_observed_not_success(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"e2-ack-{uuid.uuid4()}",
        target_id="k8s:pod:order-svc-01",
        request_sha256="a" * 64,
    )
    inc_id = admit.response.id

    service = OutcomeService(session_factory)

    # 1. Adapter ACK reports success
    req = ObservationCreateRequest(
        source_kind=SourceKind.ADAPTER_ACK,
        source_reference="adapter:k8s:v1",
        rule_version="v1",
        result_payload={"status": "SUCCESS", "message": "Pod restarted by adapter"},
        idempotency_key=f"ack-{uuid.uuid4()}",
    )
    obs, rev = await service.record_observation(isolated_tenant, inc_id, req)

    # INVARIANT: Adapter ACK alone cannot claim VERIFIED_SUCCESS
    assert obs.source_kind == SourceKind.ADAPTER_ACK
    assert rev.label == OutcomeLabel.OBSERVED
    assert rev.confidence_band == ConfidenceBand.LOW
    assert rev.revision_no == 1


@pytest.mark.asyncio
async def test_e2_independent_check_passes_produces_verified_success(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"e2-indep-pass-{uuid.uuid4()}",
        target_id="k8s:pod:order-svc-02",
        request_sha256="b" * 64,
    )
    inc_id = admit.response.id

    service = OutcomeService(session_factory)

    # 1. Adapter ACK first -> OBSERVED
    ack_req = ObservationCreateRequest(
        source_kind=SourceKind.ADAPTER_ACK,
        source_reference="adapter:k8s:v1",
        result_payload={"status": "SUCCESS"},
        idempotency_key=f"ack-{uuid.uuid4()}",
    )
    await service.record_observation(isolated_tenant, inc_id, ack_req)

    # 2. Independent synthetic health check passes -> VERIFIED_SUCCESS
    check_req = ObservationCreateRequest(
        source_kind=SourceKind.INDEPENDENT_WORKLOAD_CHECK,
        source_reference="synthetics:probe:http-get-orders",
        rule_version="v1",
        result_payload={"status": "PASS", "latency_ms": 45, "http_code": 200},
        idempotency_key=f"probe-{uuid.uuid4()}",
    )
    _obs, rev = await service.record_observation(isolated_tenant, inc_id, check_req)

    assert rev.label == OutcomeLabel.VERIFIED_SUCCESS
    assert rev.confidence_band == ConfidenceBand.HIGH
    assert rev.revision_no == 2
    assert rev.prior_revision_id is not None


@pytest.mark.asyncio
async def test_e2_negative_control_independent_check_fails_produces_verified_failure(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"e2-neg-ctrl-{uuid.uuid4()}",
        target_id="k8s:pod:order-svc-03",
        request_sha256="c" * 64,
    )
    inc_id = admit.response.id

    service = OutcomeService(session_factory)

    # Adapter claims success
    await service.record_observation(
        isolated_tenant,
        inc_id,
        ObservationCreateRequest(
            source_kind=SourceKind.ADAPTER_ACK,
            source_reference="adapter:k8s:v1",
            result_payload={"status": "SUCCESS"},
            idempotency_key=f"ack-{uuid.uuid4()}",
        ),
    )

    # But independent check fails (Negative Control Gate: 0% false success tolerance)
    fail_req = ObservationCreateRequest(
        source_kind=SourceKind.INDEPENDENT_WORKLOAD_CHECK,
        source_reference="synthetics:probe:http-get-orders",
        result_payload={"status": "FAIL", "error": "HTTP 503 Service Unavailable"},
        idempotency_key=f"probe-fail-{uuid.uuid4()}",
    )
    _obs, rev = await service.record_observation(isolated_tenant, inc_id, fail_req)

    assert rev.label == OutcomeLabel.VERIFIED_FAILURE
    assert rev.confidence_band == ConfidenceBand.HIGH


@pytest.mark.asyncio
async def test_e2_model_self_grading_prohibited(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"e2-self-grade-{uuid.uuid4()}",
        target_id="k8s:pod:order-svc-04",
        request_sha256="d" * 64,
    )
    inc_id = admit.response.id

    service = OutcomeService(session_factory)

    # Model attempting to self-grade its own action as success must be rejected
    self_grade_req = ObservationCreateRequest(
        source_kind=SourceKind.MODEL_ANNOTATION,
        source_reference="model:gemini-1.5-pro",
        result_payload={"status": "SUCCESS", "analysis": "I believe the bug is fixed"},
        idempotency_key=f"self-grade-{uuid.uuid4()}",
    )
    with pytest.raises(SelfGradingForbiddenError):
        await service.record_observation(isolated_tenant, inc_id, self_grade_req)


@pytest.mark.asyncio
async def test_e2_delayed_relapse_produces_disputed(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"e2-relapse-{uuid.uuid4()}",
        target_id="k8s:pod:order-svc-05",
        request_sha256="e" * 64,
    )
    inc_id = admit.response.id

    service = OutcomeService(session_factory)

    # 1. Independent check passed
    await service.record_observation(
        isolated_tenant,
        inc_id,
        ObservationCreateRequest(
            source_kind=SourceKind.INDEPENDENT_WORKLOAD_CHECK,
            source_reference="synthetics:probe:http-get-orders",
            result_payload={"status": "PASS"},
            idempotency_key=f"probe-{uuid.uuid4()}",
        ),
    )

    # 2. Delayed monitor (5 mins later) reports crash loop relapse
    relapse_req = ObservationCreateRequest(
        source_kind=SourceKind.DELAYED_MONITOR,
        source_reference="monitor:k8s:crashloop-detector",
        result_payload={"status": "FAIL", "restart_count": 5},
        idempotency_key=f"delayed-{uuid.uuid4()}",
    )
    _obs, rev = await service.record_observation(isolated_tenant, inc_id, relapse_req)

    assert rev.label == OutcomeLabel.DISPUTED
    assert rev.confidence_band == ConfidenceBand.LOW
    assert "relapse" in (rev.dispute_reason or "").lower()


@pytest.mark.asyncio
async def test_e2_operator_feedback_cas_and_dispute_resolution(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"e2-feedback-{uuid.uuid4()}",
        target_id="k8s:pod:order-svc-06",
        request_sha256="f" * 64,
    )
    inc_id = admit.response.id

    service = OutcomeService(session_factory)

    # Create initial observation
    await service.record_observation(
        isolated_tenant,
        inc_id,
        ObservationCreateRequest(
            source_kind=SourceKind.ADAPTER_ACK,
            source_reference="adapter:k8s:v1",
            result_payload={"status": "SUCCESS"},
            idempotency_key=f"ack-{uuid.uuid4()}",
        ),
    )

    # 1. Human reviewer adjudicates as VERIFIED_SUCCESS with expected_revision_no = 1
    feedback_req = OperatorFeedbackRequest(
        label=OutcomeLabel.VERIFIED_SUCCESS,
        reason="Manual canary inspection confirms orders processing normally",
        actor_id="lead-sre-1",
        actor_role="incident_commander",
        expected_revision_no=1,
    )
    resolved_rev = await service.submit_operator_feedback(isolated_tenant, inc_id, feedback_req)
    assert resolved_rev.revision_no == 2
    assert resolved_rev.label == OutcomeLabel.VERIFIED_SUCCESS
    assert resolved_rev.reviewer_id == "lead-sre-1"

    # 2. CAS Conflict: Attempting to submit with stale expected_revision_no = 1 raises OutcomeConflictError
    stale_req = OperatorFeedbackRequest(
        label=OutcomeLabel.DISPUTED,
        reason="Stale update attempt",
        actor_id="lead-sre-2",
        actor_role="incident_commander",
        expected_revision_no=1,  # Stale! Current is 2
    )
    with pytest.raises(OutcomeConflictError):
        await service.submit_operator_feedback(isolated_tenant, inc_id, stale_req)

    # 3. Role validation: unauthorized role raises ApprovalRoleForbiddenError
    with pytest.raises(ApprovalRoleForbiddenError):
        await service.submit_operator_feedback(
            isolated_tenant,
            inc_id,
            OperatorFeedbackRequest(
                label=OutcomeLabel.DISPUTED,
                reason="Unauthorized attempt",
                actor_id="guest",
                actor_role="viewer",
                expected_revision_no=2,
            ),
        )


@pytest.mark.asyncio
async def test_e2_dependent_capsule_quarantine_on_dispute(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    # 1. Enable consent & project eligible capsule
    consent_mgr = ConsentManager(session_factory)
    await consent_mgr.grant_consent(
        tenant_id=isolated_tenant,
        actor_id="admin-user-1",
        actor_role="incident_commander",
        reason="Enable consent",
    )

    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"e2-capsule-sync-{uuid.uuid4()}",
        target_id="k8s:pod:order-svc-07",
        request_sha256="1" * 64,
    )
    inc_id = admit.response.id

    projector = ExperienceProjector(session_factory)
    capsule_resp = await projector.project_experience(
        tenant_id=isolated_tenant,
        incident_id=inc_id,
        custom_summary="Pod restart resolved order flow.",
        classification=ExperienceClassification.INVESTIGATION_SUCCESS,
        model_id="gemini-1.5-pro",
        model_version="2026-v1",
        evidence_manifest_sha256="1" * 64,
    )
    assert capsule_resp is not None
    assert capsule_resp.lifecycle_state == ExperienceState.ELIGIBLE

    # 2. Record independent check failure (Negative Control / Fault)
    outcome_svc = OutcomeService(session_factory)
    await outcome_svc.record_observation(
        isolated_tenant,
        inc_id,
        ObservationCreateRequest(
            source_kind=SourceKind.INDEPENDENT_WORKLOAD_CHECK,
            source_reference="synthetics:orders",
            result_payload={"status": "FAIL", "reason": "Order drop rate 100%"},
            idempotency_key=f"fail-{uuid.uuid4()}",
        ),
    )

    # 3. Check that linked capsule was automatically quarantined
    exp_repo = ExperienceRepository(session_factory)
    capsule = await exp_repo.get_capsule(isolated_tenant, capsule_resp.id)
    assert capsule is not None
    assert capsule.lifecycle_state == ExperienceState.QUARANTINED
    assert capsule.classification == ExperienceClassification.INVESTIGATION_FAILURE


@pytest.mark.asyncio
async def test_e2_api_routes(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    headers = {"X-Tenant-ID": str(isolated_tenant)}

    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"e2-api-{uuid.uuid4()}",
        target_id="k8s:pod:order-svc-08",
        request_sha256="2" * 64,
    )
    inc_id = admit.response.id

    # 1. POST observation via API
    obs_payload = {
        "source_kind": "INDEPENDENT_WORKLOAD_CHECK",
        "source_reference": "synthetics:api-orders",
        "rule_version": "v1",
        "result_payload": {"status": "PASS", "latency_ms": 30},
        "idempotency_key": f"api-obs-{uuid.uuid4()}",
    }
    res = await api_client.post(
        f"/v1/incidents/{inc_id}/outcome/observations",
        json=obs_payload,
        headers=headers,
    )
    assert res.status_code == 201
    data = res.json()
    assert data["current_revision"]["label"] == "VERIFIED_SUCCESS"

    # 2. GET outcome summary via API
    res = await api_client.get(f"/v1/incidents/{inc_id}/outcome", headers=headers)
    assert res.status_code == 200
    summary = res.json()
    assert summary["current_revision"]["label"] == "VERIFIED_SUCCESS"
    assert summary["total_observations"] == 1
    assert summary["is_disputed"] is False

    # 3. POST operator feedback via API
    feedback_payload = {
        "label": "DISPUTED",
        "reason": "Customer reported intermittent transaction drops",
        "actor_id": "operator-bob",
        "actor_role": "sre_lead",
        "expected_revision_no": 1,
    }
    res = await api_client.post(
        f"/v1/incidents/{inc_id}/outcome/feedback",
        json=feedback_payload,
        headers=headers,
    )
    assert res.status_code == 200
    rev_data = res.json()
    assert rev_data["label"] == "DISPUTED"
    assert rev_data["revision_no"] == 2

    # 4. CAS Conflict via API
    res = await api_client.post(
        f"/v1/incidents/{inc_id}/outcome/feedback",
        json=feedback_payload,  # expected_revision_no is 1, but current is now 2
        headers=headers,
    )
    assert res.status_code == 409

    # 5. Model self-grading blocked via API -> 403 Forbidden
    self_grade = {
        "source_kind": "MODEL_ANNOTATION",
        "source_reference": "model:llm",
        "rule_version": "v1",
        "result_payload": {"status": "PASS"},
        "idempotency_key": f"sg-{uuid.uuid4()}",
    }
    res = await api_client.post(
        f"/v1/incidents/{inc_id}/outcome/observations",
        json=self_grade,
        headers=headers,
    )
    assert res.status_code == 403
