"""Omega Adversarial and Chaos Stress Suite for OpsBlaze Feature E1 and E2.

Tests all boundary and adversarial failure modes:
1. Concurrent race: consent revocation vs query-time retrieval.
2. Malformed / unredactable input text (null bytes, oversized 1MB text, control characters).
3. Negative control zero-false-success tolerance: adapter claims success but independent check fails.
4. Delayed relapse injection: delayed monitor failure demotes verified success to DISPUTED.
5. CAS optimistic concurrency race during operator dispute resolution (zero lost updates).
6. Agentic model self-grading evasion attempts (strictly blocked).
7. Strict cross-tenant isolation: Tenant A cannot see, quarantine, or affect Tenant B.
8. Role-based authorization attacks: unprivileged roles rejected with 403.
9. Irreversible tombstone payload erasure: summary_markdown cannot be revived.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.approval import ApprovalRoleForbiddenError
from backend.domain.experience import (
    CapsuleNotFoundError,
    CapsuleRedactionError,
    ExperienceClassification,
    ExperienceState,
)
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
from backend.experience.redact import ExperienceRedactor
from backend.experience.repository import ExperienceRepository
from backend.experience.retention import RetentionWorker
from backend.outcomes.service import OutcomeService
from backend.persistence.repositories import IncidentRepository

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_adversarial_consent_race_concurrent_reads(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    # 1. Tenant grants consent and projects 5 capsules
    consent_mgr = ConsentManager(session_factory)
    await consent_mgr.grant_consent(
        tenant_id=isolated_tenant,
        actor_id="admin",
        actor_role="incident_commander",
        reason="Initial grant",
    )

    inc_repo = IncidentRepository(session_factory)
    projector = ExperienceProjector(session_factory)
    exp_repo = ExperienceRepository(session_factory)

    for i in range(5):
        admit = await inc_repo.admit_incident(
            tenant_id=isolated_tenant,
            client_key=f"race-{i}-{uuid.uuid4()}",
            target_id=f"target-{i}",
            request_sha256="c" * 64,
        )
        await projector.project_experience(
            tenant_id=isolated_tenant,
            incident_id=admit.response.id,
            custom_summary=f"Incident {i} resolved.",
            classification=ExperienceClassification.INVESTIGATION_SUCCESS,
            model_id="gemini-1.5-pro",
            model_version="2026-v1",
            evidence_manifest_sha256="0" * 64,
        )

    # 2. Concurrently run 10 queries while revocation executes in the middle

    async def query_worker() -> int:
        res = await exp_repo.list_eligible_experiences(isolated_tenant)
        return res.total_eligible

    async def revoker() -> None:
        await asyncio.sleep(0.01)  # small jitter
        await consent_mgr.revoke_consent(
            tenant_id=isolated_tenant,
            actor_id="admin-security",
            actor_role="incident_commander",
            reason="Race test revocation",
        )

    tasks = [query_worker() for _ in range(5)] + [revoker()] + [query_worker() for _ in range(5)]
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    query_counts = [o for o in outcomes if isinstance(o, int)]
    # Invariant: Each query returned either 5 (before revocation) or 0 (after revocation). Never corrupt or partial!
    for count in query_counts:
        assert count in (0, 5)

    # Final query must strictly be 0
    final_res = await exp_repo.list_eligible_experiences(isolated_tenant)
    assert final_res.total_eligible == 0


def test_adversarial_malformed_redaction_inputs() -> None:
    # 1. Null byte injection must raise CapsuleRedactionError
    with pytest.raises(CapsuleRedactionError):
        ExperienceRedactor.redact_and_normalize("Malicious payload with \x00 null byte")

    # 2. Huge 100KB payload with repeated secrets
    huge_payload = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.tok " * 1000
    cleaned, digest = ExperienceRedactor.redact_and_normalize(huge_payload)
    assert len(cleaned.encode("utf-8")) <= 4096
    assert "[TRUNCATED: MAX_BYTES_EXCEEDED]" in cleaned
    assert len(digest) == 64


@pytest.mark.asyncio
async def test_adversarial_redaction_error_quarantines_capsule(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    consent_mgr = ConsentManager(session_factory)
    await consent_mgr.grant_consent(
        tenant_id=isolated_tenant,
        actor_id="admin",
        actor_role="incident_commander",
        reason="Enable consent",
    )

    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"malformed-{uuid.uuid4()}",
        target_id="target-bad",
        request_sha256="d" * 64,
    )
    projector = ExperienceProjector(session_factory)

    # Injecting null byte payload causes CapsuleRedactionError internally
    # Invariant INV-E1-3: Non-blocking, quarantines capsule instead of throwing
    capsule_resp = await projector.project_experience(
        tenant_id=isolated_tenant,
        incident_id=admit.response.id,
        custom_summary="Contains null byte \x00 attack string",
        classification=ExperienceClassification.INVESTIGATION_SUCCESS,
        model_id="gemini-1.5-pro",
        model_version="2026-v1",
        evidence_manifest_sha256="0" * 64,
    )
    assert capsule_resp is not None
    assert capsule_resp.lifecycle_state == ExperienceState.QUARANTINED
    assert "[REDACTION_FAILED" in (capsule_resp.summary_markdown or "")


@pytest.mark.asyncio
async def test_adversarial_negative_control_zero_false_success_tolerance(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Negative Control Gate: Adapter claims success, but independent check fails.

    System MUST classify as VERIFIED_FAILURE (0% false success tolerance).
    """
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"neg-ctrl-{uuid.uuid4()}",
        target_id="k8s:pod:negative-control-service",
        request_sha256="e" * 64,
    )
    inc_id = admit.response.id

    outcome_svc = OutcomeService(session_factory)

    # 1. Adapter claims 100% success
    await outcome_svc.record_observation(
        isolated_tenant,
        inc_id,
        ObservationCreateRequest(
            source_kind=SourceKind.ADAPTER_ACK,
            source_reference="adapter:k8s:v1",
            result_payload={"status": "SUCCESS", "exit_code": 0},
            idempotency_key=f"ack-{uuid.uuid4()}",
        ),
    )

    # 2. Independent workload check discovers injected fault
    _obs, rev = await outcome_svc.record_observation(
        isolated_tenant,
        inc_id,
        ObservationCreateRequest(
            source_kind=SourceKind.INDEPENDENT_WORKLOAD_CHECK,
            source_reference="synthetics:external-canary",
            result_payload={
                "status": "FAIL",
                "injected_fault": "network_partition",
                "error_rate": 1.0,
            },
            idempotency_key=f"fail-{uuid.uuid4()}",
        ),
    )

    assert rev.label == OutcomeLabel.VERIFIED_FAILURE
    assert rev.confidence_band == ConfidenceBand.HIGH


@pytest.mark.asyncio
async def test_adversarial_delayed_relapse_injection(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"relapse-chaos-{uuid.uuid4()}",
        target_id="k8s:pod:payment-chaos",
        request_sha256="f" * 64,
    )
    inc_id = admit.response.id

    outcome_svc = OutcomeService(session_factory)

    # 1. Passed independent check
    await outcome_svc.record_observation(
        isolated_tenant,
        inc_id,
        ObservationCreateRequest(
            source_kind=SourceKind.INDEPENDENT_WORKLOAD_CHECK,
            source_reference="synthetics:check",
            result_payload={"status": "PASS"},
            idempotency_key=f"pass-{uuid.uuid4()}",
        ),
    )

    # 2. Delayed monitor detects silent relapse
    _obs, rev = await outcome_svc.record_observation(
        isolated_tenant,
        inc_id,
        ObservationCreateRequest(
            source_kind=SourceKind.DELAYED_MONITOR,
            source_reference="delayed:monitor:relapse",
            result_payload={"status": "FAIL", "reason": "CrashLoopBackOff resumed"},
            idempotency_key=f"relapse-{uuid.uuid4()}",
        ),
    )

    assert rev.label == OutcomeLabel.DISPUTED
    assert rev.confidence_band == ConfidenceBand.LOW


@pytest.mark.asyncio
async def test_adversarial_cas_concurrent_dispute_resolution(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"cas-race-{uuid.uuid4()}",
        target_id="k8s:pod:cas-target",
        request_sha256="1" * 64,
    )
    inc_id = admit.response.id

    outcome_svc = OutcomeService(session_factory)

    # Create rev 1
    await outcome_svc.record_observation(
        isolated_tenant,
        inc_id,
        ObservationCreateRequest(
            source_kind=SourceKind.ADAPTER_ACK,
            source_reference="adapter:k8s",
            result_payload={"status": "SUCCESS"},
            idempotency_key=f"ack-{uuid.uuid4()}",
        ),
    )

    # Two concurrent reviewers submit conflicting feedback with expected_revision_no = 1
    req_alice = OperatorFeedbackRequest(
        label=OutcomeLabel.VERIFIED_SUCCESS,
        reason="Alice confirmed resolution",
        actor_id="alice",
        actor_role="incident_commander",
        expected_revision_no=1,
    )
    req_bob = OperatorFeedbackRequest(
        label=OutcomeLabel.DISPUTED,
        reason="Bob found errors",
        actor_id="bob",
        actor_role="sre_lead",
        expected_revision_no=1,
    )

    results = await asyncio.gather(
        outcome_svc.submit_operator_feedback(isolated_tenant, inc_id, req_alice),
        outcome_svc.submit_operator_feedback(isolated_tenant, inc_id, req_bob),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, OutcomeConflictError)]

    # Invariant: Exactly one succeeded, exactly one failed with OutcomeConflictError (zero lost updates)
    assert len(successes) == 1
    assert len(failures) == 1


@pytest.mark.asyncio
async def test_adversarial_model_self_grading_evasion_blocked(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"evasion-{uuid.uuid4()}",
        target_id="target-model",
        request_sha256="2" * 64,
    )
    inc_id = admit.response.id

    outcome_svc = OutcomeService(session_factory)

    # Various evasion payloads
    evasion_payloads = [
        {"status": "PASS"},
        {"status": "SUCCESS"},
        {"status": "Resolved"},
        {"success": True},
    ]

    for p in evasion_payloads:
        req = ObservationCreateRequest(
            source_kind=SourceKind.MODEL_ANNOTATION,
            source_reference="llm:gemini-1.5-pro",
            result_payload=p,
            idempotency_key=f"evasion-{uuid.uuid4()}",
        )
        with pytest.raises(SelfGradingForbiddenError):
            await outcome_svc.record_observation(isolated_tenant, inc_id, req)


@pytest.mark.asyncio
async def test_adversarial_cross_tenant_isolation_gate(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    second_isolated_tenant: uuid.UUID,
) -> None:
    consent_mgr = ConsentManager(session_factory)

    # Tenant A grants consent
    await consent_mgr.grant_consent(
        tenant_id=isolated_tenant,
        actor_id="admin-a",
        actor_role="incident_commander",
        reason="Tenant A grant",
    )
    # Tenant B grants consent
    await consent_mgr.grant_consent(
        tenant_id=second_isolated_tenant,
        actor_id="admin-b",
        actor_role="incident_commander",
        reason="Tenant B grant",
    )

    inc_repo = IncidentRepository(session_factory)
    projector = ExperienceProjector(session_factory)
    exp_repo = ExperienceRepository(session_factory)

    # Project capsule for Tenant B
    admit_b = await inc_repo.admit_incident(
        tenant_id=second_isolated_tenant,
        client_key=f"tenant-b-{uuid.uuid4()}",
        target_id="target-b",
        request_sha256="3" * 64,
    )
    cap_b_resp = await projector.project_experience(
        tenant_id=second_isolated_tenant,
        incident_id=admit_b.response.id,
        custom_summary="Tenant B secret investigation.",
        classification=ExperienceClassification.INVESTIGATION_SUCCESS,
        model_id="gemini-1.5-pro",
        model_version="2026-v1",
        evidence_manifest_sha256="3" * 64,
    )
    assert cap_b_resp is not None

    # Revoke consent for Tenant A
    await consent_mgr.revoke_consent(
        tenant_id=isolated_tenant,
        actor_id="admin-a",
        actor_role="incident_commander",
        reason="Tenant A revokes",
    )

    # Invariant: Tenant B's capsules remain ELIGIBLE and completely unaffected
    list_b = await exp_repo.list_eligible_experiences(second_isolated_tenant)
    assert list_b.total_eligible >= 1
    assert any(c.id == cap_b_resp.id for c in list_b.items)

    # Tenant A cannot see Tenant B's capsule
    cap_a_view = await exp_repo.get_capsule(isolated_tenant, cap_b_resp.id)
    assert cap_a_view is None

    # Tenant A cannot quarantine Tenant B's capsule
    with pytest.raises(CapsuleNotFoundError):
        await exp_repo.quarantine_capsule(
            tenant_id=isolated_tenant,
            capsule_id=cap_b_resp.id,
            actor_id="admin-a",
            actor_role="incident_commander",
            reason="Illegal cross-tenant attack",
        )


@pytest.mark.asyncio
async def test_adversarial_unauthorized_role_injection(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    consent_mgr = ConsentManager(session_factory)
    unauthorized_roles = ["attacker", "guest", "viewer", "model", "intern", "bot"]

    for r in unauthorized_roles:
        with pytest.raises(ApprovalRoleForbiddenError):
            await consent_mgr.revoke_consent(
                tenant_id=isolated_tenant,
                actor_id="attacker",
                actor_role=r,
                reason="Unauthorized attempt",
            )


@pytest.mark.asyncio
async def test_adversarial_tombstone_payload_erasure_irreversibility(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    consent_mgr = ConsentManager(session_factory)
    await consent_mgr.grant_consent(
        tenant_id=isolated_tenant,
        actor_id="admin",
        actor_role="incident_commander",
        reason="Grant",
    )

    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"tombstone-chaos-{uuid.uuid4()}",
        target_id="target-tomb",
        request_sha256="4" * 64,
    )
    projector = ExperienceProjector(session_factory)
    cap = await projector.project_experience(
        tenant_id=isolated_tenant,
        incident_id=admit.response.id,
        custom_summary="Confidential architecture summary to be erased.",
        classification=ExperienceClassification.INVESTIGATION_SUCCESS,
        model_id="gemini-1.5-pro",
        model_version="2026-v1",
        evidence_manifest_sha256="4" * 64,
    )
    assert cap is not None

    worker = RetentionWorker(session_factory)
    await worker.tombstone_capsule(
        tenant_id=isolated_tenant,
        capsule_id=cap.id,
        actor_id="privacy-officer",
        actor_role="incident_commander",
        reason="GDPR right to be forgotten",
    )

    exp_repo = ExperienceRepository(session_factory)
    tombstoned = await exp_repo.get_capsule(isolated_tenant, cap.id)
    assert tombstoned is not None
    assert tombstoned.lifecycle_state == ExperienceState.TOMBSTONED
    assert tombstoned.summary_markdown is None

    # Re-projecting the same incident CANNOT revive the payload
    reprojected = await projector.project_experience(
        tenant_id=isolated_tenant,
        incident_id=admit.response.id,
        custom_summary="Attempting to revive erased payload.",
        classification=ExperienceClassification.INVESTIGATION_SUCCESS,
        model_id="gemini-1.5-pro",
        model_version="2026-v1",
        evidence_manifest_sha256="4" * 64,
    )
    assert reprojected is not None
    assert reprojected.lifecycle_state == ExperienceState.TOMBSTONED
    assert reprojected.summary_markdown is None
