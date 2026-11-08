"""Unit and API tests for Feature E1 — Consent-Scoped Experience Capsule."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.approval import ApprovalRoleForbiddenError
from backend.domain.experience import (
    CapsuleNotFoundError,
    CapsuleStateConflictError,
    ConsentDeniedError,
    ExperienceAuditAction,
    ExperienceClassification,
    ExperienceState,
)
from backend.experience.consent import ConsentManager
from backend.experience.projector import ExperienceProjector
from backend.experience.redact import ExperienceRedactor
from backend.experience.repository import ExperienceRepository
from backend.experience.retention import RetentionWorker
from backend.persistence.models import ExperienceAudit
from backend.persistence.repositories import IncidentRepository

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_e1_consent_lifecycle_and_epoch_advance(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    consent_mgr = ConsentManager(session_factory)

    # 1. Uninitialized tenant defaults to reuse_allowed = False (default deny privacy posture)
    initial_status = await consent_mgr.get_consent(isolated_tenant)
    assert initial_status.tenant_id == isolated_tenant
    assert initial_status.current_epoch == 1
    assert initial_status.reuse_allowed is False
    assert initial_status.revoked_at is None

    # 2. Explicitly grant consent -> epoch 1, reuse_allowed = True
    granted_status = await consent_mgr.grant_consent(
        tenant_id=isolated_tenant,
        actor_id="admin-user-1",
        actor_role="incident_commander",
        reason="Tenant accepted telemetry terms",
    )
    assert granted_status.current_epoch == 1
    assert granted_status.reuse_allowed is True
    assert granted_status.revoked_at is None

    # 3. Revoke consent -> epoch increments to 2, reuse_allowed becomes False
    revoked_status = await consent_mgr.revoke_consent(
        tenant_id=isolated_tenant,
        actor_id="admin-user-1",
        actor_role="incident_commander",
        reason="Tenant requested data exclusion",
    )
    assert revoked_status.current_epoch == 2
    assert revoked_status.reuse_allowed is False
    assert revoked_status.revoked_at is not None

    # Verify audit trail
    async with session_factory() as session:
        audits = (
            await session.execute(
                select(ExperienceAudit).where(
                    ExperienceAudit.tenant_id == isolated_tenant,
                    ExperienceAudit.action == ExperienceAuditAction.CONSENT_REVOKED.value,
                )
            )
        ).scalars().all()
        assert len(audits) == 1
        assert audits[0].actor_id == "admin-user-1"
        assert audits[0].reason == "Tenant requested data exclusion"

    # 4. Role validation: unauthorized role raises error
    with pytest.raises(ApprovalRoleForbiddenError):
        await consent_mgr.revoke_consent(
            tenant_id=isolated_tenant,
            actor_id="bad-actor",
            actor_role="viewer",
            reason="Illegal attempt",
        )


def test_e1_deterministic_redaction_and_digest() -> None:
    raw_text = (
        "Investigation summary: encountered auth token Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-ID "
        "and AWS Key AKIAIOSFODNN7EXAMPLE with db connection postgresql://admin:secretPass123@prod-db.internal:5432/core."
    )
    redacted_text, digest = ExperienceRedactor.redact_and_normalize(raw_text)

    # Invariants: No raw credentials remain
    assert "secretPass123" not in redacted_text
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted_text
    assert "[REDACTED:" in redacted_text
    assert len(digest) == 64

    # Idempotent and deterministic
    redacted_again, digest_again = ExperienceRedactor.redact_and_normalize(raw_text)
    assert redacted_text == redacted_again
    assert digest == digest_again

    # Length capping (4096 bytes)
    huge_text = "A" * 6000
    capped_text, _capped_digest = ExperienceRedactor.redact_and_normalize(huge_text)
    assert len(capped_text.encode("utf-8")) <= 4096
    assert "[TRUNCATED: MAX_BYTES_EXCEEDED]" in capped_text


@pytest.mark.asyncio
async def test_e1_experience_projection_and_idempotency(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    # First grant consent so capsule is ELIGIBLE
    consent_mgr = ConsentManager(session_factory)
    await consent_mgr.grant_consent(
        tenant_id=isolated_tenant,
        actor_id="admin-user-1",
        actor_role="incident_commander",
        reason="Grant consent for testing",
    )

    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"e1-test-{uuid.uuid4()}",
        target_id="k8s:pod:payment-svc-01",
        request_sha256="d" * 64,
    )
    inc_id = admit.response.id

    projector = ExperienceProjector(session_factory)
    capsule_resp = await projector.project_experience(
        tenant_id=isolated_tenant,
        incident_id=inc_id,
        custom_summary="Payment service OOM resolved after pod restart. Key: AKIAIOSFODNN7EXAMPLE",
        classification=ExperienceClassification.INVESTIGATION_SUCCESS,
        model_id="gemini-1.5-pro",
        model_version="2026-v1",
        evidence_manifest_sha256="e" * 64,
    )
    assert capsule_resp is not None
    capsule_id = capsule_resp.id

    repo = ExperienceRepository(session_factory)
    capsule = await repo.get_capsule(isolated_tenant, capsule_id)
    assert capsule is not None
    assert capsule.lifecycle_state == ExperienceState.ELIGIBLE
    assert capsule.consent_epoch == 1
    assert "AKIAIOSFODNN7EXAMPLE" not in (capsule.summary_markdown or "")
    assert "[REDACTED:" in (capsule.summary_markdown or "")

    # Idempotent projection does not duplicate rows
    capsule_resp_2 = await projector.project_experience(
        tenant_id=isolated_tenant,
        incident_id=inc_id,
        custom_summary="Payment service OOM resolved after pod restart. Key: AKIAIOSFODNN7EXAMPLE",
        classification=ExperienceClassification.INVESTIGATION_SUCCESS,
        model_id="gemini-1.5-pro",
        model_version="2026-v1",
        evidence_manifest_sha256="e" * 64,
    )
    assert capsule_resp_2 is not None
    assert capsule_resp_2.id == capsule_id


@pytest.mark.asyncio
async def test_e1_query_time_consent_invalidation(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    # Grant consent
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
        client_key=f"e1-consent-{uuid.uuid4()}",
        target_id="k8s:pod:cart-svc-01",
        request_sha256="f" * 64,
    )
    inc_id = admit.response.id

    projector = ExperienceProjector(session_factory)
    capsule_resp = await projector.project_experience(
        tenant_id=isolated_tenant,
        incident_id=inc_id,
        custom_summary="Cart service investigation verified.",
        classification=ExperienceClassification.INVESTIGATION_SUCCESS,
        model_id="gemini-1.5-pro",
        model_version="2026-v1",
        evidence_manifest_sha256="1" * 64,
    )
    assert capsule_resp is not None
    capsule_id = capsule_resp.id

    repo = ExperienceRepository(session_factory)
    list_res = await repo.list_eligible_experiences(isolated_tenant)
    assert list_res.total_eligible >= 1
    assert any(c.id == capsule_id for c in list_res.items)

    # Revoke consent
    await consent_mgr.revoke_consent(
        tenant_id=isolated_tenant,
        actor_id="admin-1",
        actor_role="incident_commander",
        reason="Revoke data sharing",
    )

    # Synchronous query-time invalidation: list immediately returns 0 eligible items
    list_res_revoked = await repo.list_eligible_experiences(isolated_tenant)
    assert list_res_revoked.total_eligible == 0
    assert len(list_res_revoked.items) == 0

    # Bounded export also raises ConsentDeniedError
    with pytest.raises(ConsentDeniedError):
        await repo.export_experiences(isolated_tenant)


@pytest.mark.asyncio
async def test_e1_reviewer_quarantine_cas(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
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
        client_key=f"e1-quarantine-{uuid.uuid4()}",
        target_id="k8s:pod:checkout-svc-01",
        request_sha256="2" * 64,
    )
    inc_id = admit.response.id

    projector = ExperienceProjector(session_factory)
    capsule_resp = await projector.project_experience(
        tenant_id=isolated_tenant,
        incident_id=inc_id,
        custom_summary="Checkout investigation notes.",
        classification=ExperienceClassification.INVESTIGATION_SUCCESS,
        model_id="gemini-1.5-pro",
        model_version="2026-v1",
        evidence_manifest_sha256="2" * 64,
    )
    assert capsule_resp is not None
    capsule_id = capsule_resp.id

    repo = ExperienceRepository(session_factory)

    # 1. Valid reviewer quarantine
    quarantined = await repo.quarantine_capsule(
        tenant_id=isolated_tenant,
        capsule_id=capsule_id,
        actor_id="reviewer-1",
        actor_role="incident_commander",
        reason="Contains sensitive operational context",
        expected_state=ExperienceState.ELIGIBLE,
    )
    assert quarantined.lifecycle_state == ExperienceState.QUARANTINED

    # 2. Second quarantine attempt fails CAS check
    with pytest.raises(CapsuleStateConflictError):
        await repo.quarantine_capsule(
            tenant_id=isolated_tenant,
            capsule_id=capsule_id,
            actor_id="reviewer-2",
            actor_role="incident_commander",
            reason="Duplicate quarantine",
            expected_state=ExperienceState.ELIGIBLE,
        )

    # 3. Non-existent capsule
    with pytest.raises(CapsuleNotFoundError):
        await repo.quarantine_capsule(
            tenant_id=isolated_tenant,
            capsule_id=uuid.uuid4(),
            actor_id="reviewer-1",
            actor_role="incident_commander",
            reason="Missing",
        )


@pytest.mark.asyncio
async def test_e1_retention_worker_purging(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
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
        client_key=f"e1-retention-{uuid.uuid4()}",
        target_id="k8s:pod:auth-svc-01",
        request_sha256="3" * 64,
    )
    inc_id = admit.response.id

    projector = ExperienceProjector(session_factory)
    now = datetime.now(UTC)

    # Create capsule with already expired TTL
    expired_resp = await projector.project_experience(
        tenant_id=isolated_tenant,
        incident_id=inc_id,
        custom_summary="Expired investigation details.",
        classification=ExperienceClassification.INVESTIGATION_SUCCESS,
        model_id="gemini-1.5-pro",
        model_version="2026-v1",
        evidence_manifest_sha256="3" * 64,
        ttl=timedelta(seconds=-10),  # expired in past
        now=now,
    )
    assert expired_resp is not None
    expired_capsule_id = expired_resp.id

    worker = RetentionWorker(session_factory)
    purged_count = await worker.purge_expired_capsules(batch_size=10, now=now)
    assert purged_count >= 1

    repo = ExperienceRepository(session_factory)
    purged_capsule = await repo.get_capsule(isolated_tenant, expired_capsule_id)
    assert purged_capsule is not None
    assert purged_capsule.lifecycle_state == ExperienceState.PURGED
    assert purged_capsule.summary_markdown is None  # Payload erased

    # Test manual tombstoning
    admit2 = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"e1-tombstone-{uuid.uuid4()}",
        target_id="k8s:pod:auth-svc-02",
        request_sha256="4" * 64,
    )
    active_resp = await projector.project_experience(
        tenant_id=isolated_tenant,
        incident_id=admit2.response.id,
        custom_summary="Active investigation to be tombstoned.",
        classification=ExperienceClassification.INVESTIGATION_SUCCESS,
        model_id="gemini-1.5-pro",
        model_version="2026-v1",
        evidence_manifest_sha256="4" * 64,
    )
    assert active_resp is not None
    active_capsule_id = active_resp.id

    await worker.tombstone_capsule(
        tenant_id=isolated_tenant,
        capsule_id=active_capsule_id,
        actor_id="admin-tombstone",
        actor_role="incident_commander",
        reason="Manual tombstone test",
    )
    tombstoned_capsule = await repo.get_capsule(isolated_tenant, active_capsule_id)
    assert tombstoned_capsule is not None
    assert tombstoned_capsule.lifecycle_state == ExperienceState.TOMBSTONED
    assert tombstoned_capsule.summary_markdown is None


@pytest.mark.asyncio
async def test_e1_api_routes(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    headers = {"X-Tenant-ID": str(isolated_tenant)}

    # 1. GET consent status (defaults to false)
    res = await api_client.get("/v1/privacy/experience-consent", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["reuse_allowed"] is False
    assert data["current_epoch"] == 1

    # 2. Grant consent via API
    grant_payload = {
        "reuse_allowed": True,
        "reason": "Granting via API test",
        "actor_id": "api-admin",
        "actor_role": "incident_commander",
    }
    res = await api_client.post(
        "/v1/privacy/experience-consent",
        json=grant_payload,
        headers=headers,
    )
    assert res.status_code == 200
    assert res.json()["reuse_allowed"] is True

    # 3. Project an experience
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"e1-api-{uuid.uuid4()}",
        target_id="k8s:pod:api-svc-01",
        request_sha256="5" * 64,
    )
    projector = ExperienceProjector(session_factory)
    capsule_resp = await projector.project_experience(
        tenant_id=isolated_tenant,
        incident_id=admit.response.id,
        custom_summary="API investigation capsule.",
        classification=ExperienceClassification.INVESTIGATION_SUCCESS,
        model_id="gemini-1.5-pro",
        model_version="2026-v1",
        evidence_manifest_sha256="5" * 64,
    )
    assert capsule_resp is not None
    capsule_id = capsule_resp.id

    # 4. GET /v1/experiences
    res = await api_client.get("/v1/experiences", headers=headers)
    assert res.status_code == 200
    list_data = res.json()
    assert list_data["total_eligible"] >= 1
    assert any(c["id"] == str(capsule_id) for c in list_data["items"])

    # 5. POST /v1/experiences/{id}/quarantine
    q_payload = {
        "reason": "Quarantine via API test",
        "actor_id": "api-reviewer",
        "actor_role": "sre_lead",
        "expected_lifecycle_state": "ELIGIBLE",
    }
    res = await api_client.post(
        f"/v1/experiences/{capsule_id}/quarantine",
        json=q_payload,
        headers=headers,
    )
    assert res.status_code == 200
    assert res.json()["lifecycle_state"] == "QUARANTINED"

    # 6. POST /v1/privacy/experience-consent (revoke)
    c_payload = {
        "reuse_allowed": False,
        "reason": "Revoking via API",
        "actor_id": "api-admin",
        "actor_role": "incident_commander",
    }
    res = await api_client.post(
        "/v1/privacy/experience-consent",
        json=c_payload,
        headers=headers,
    )
    assert res.status_code == 200
    assert res.json()["reuse_allowed"] is False
    assert res.json()["current_epoch"] == 2

    # 7. GET /v1/privacy/export returns 403 when revoked
    res = await api_client.get("/v1/privacy/export", headers=headers)
    assert res.status_code == 403

    # 8. Missing X-Tenant-ID
    res = await api_client.get("/v1/experiences")
    assert res.status_code == 401
