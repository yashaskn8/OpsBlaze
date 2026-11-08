"""Feature P2 — Unit and API tests for Version-Bound Approval Cockpit."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.approvals.service import ApprovalService
from backend.config import get_settings
from backend.domain.auth import ActorKind, mint_principal_token
from backend.domain.approval import (
    ApprovalCreateRequest,
    ApprovalDecisionRequest,
    ApprovalDigestMismatchError,
    ApprovalRevokeRequest,
    ApprovalRoleForbiddenError,
    ApprovalState,
    ApprovalVersionMismatchError,
    InvalidApprovalTransitionError,
    StaleContextError,
)
from backend.persistence.models import ApprovalAudit
from backend.persistence.repositories import IncidentRepository

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_p2_approval_lifecycle_pending_to_approved(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"p2-test-{uuid.uuid4()}",
        target_id="k8s:pod:order-svc-1",
        request_sha256="a" * 64,
    )
    inc_id = admit.response.id

    service = ApprovalService(session_factory)
    create_req = ApprovalCreateRequest(
        plan_id="plan-restart-01",
        canonical_plan_digest="b" * 64,
        action_digest="c" * 64,
        action_payload={"action_kind": "restart_pod", "parameters": {"grace_period": 10}},
        target_id="k8s:pod:order-svc-1",
        expected_resource_version="v1",
        policy_version="v1",
        ttl_seconds=300,
    )

    created = await service.create_approval_request(isolated_tenant, inc_id, create_req)
    assert created.state == ApprovalState.PENDING
    assert created.canonical_plan_digest == "b" * 64

    # Idempotent re-creation returns existing pending
    recreated = await service.create_approval_request(isolated_tenant, inc_id, create_req)
    assert recreated.id == created.id

    # Approve with valid human reviewer
    decision = ApprovalDecisionRequest(
        reviewer_id="human-commander-alice",
        reviewer_role="incident_commander",
        reason="Verified telemetry indicates restart is safe",
        expected_incident_status_version=1,
    )
    approved = await service.approve(isolated_tenant, created.id, decision)
    assert approved.state == ApprovalState.APPROVED
    assert approved.reviewer_id == "human-commander-alice"
    assert approved.reviewer_role == "incident_commander"

    # Fetch approval
    fetched = await service.get_approval(isolated_tenant, created.id)
    assert fetched.state == ApprovalState.APPROVED


@pytest.mark.asyncio
async def test_p2_reviewer_role_authorization_rejects_model(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"p2-model-reject-{uuid.uuid4()}",
        target_id="k8s:pod:auth-svc",
        request_sha256="d" * 64,
    )
    inc_id = admit.response.id

    service = ApprovalService(session_factory)
    create_req = ApprovalCreateRequest(
        plan_id="plan-auth-fix",
        canonical_plan_digest="e" * 64,
        action_digest="f" * 64,
        target_id="k8s:pod:auth-svc",
        expected_resource_version="v1",
    )
    pending = await service.create_approval_request(isolated_tenant, inc_id, create_req)

    # Models or agents attempting to approve must fail at the authoritative service boundary.
    for forbidden_role in ["model", "agent", "investigator", "llm", "system", "autonomous_agent"]:
        with pytest.raises(ApprovalRoleForbiddenError):
            await service.approve(
                isolated_tenant,
                pending.id,
                ApprovalDecisionRequest(
                    reviewer_id="claude-agent",
                    reviewer_role=forbidden_role,
                    reason="Self-approving my proposed plan",
                ),
            )


@pytest.mark.asyncio
async def test_p2_approval_rejection_and_revocation(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"p2-reject-{uuid.uuid4()}",
        target_id="k8s:pod:payment-svc",
        request_sha256="1" * 64,
    )
    inc_id = admit.response.id
    service = ApprovalService(session_factory)

    # 1. Rejection test
    req1 = ApprovalCreateRequest(
        plan_id="plan-1",
        canonical_plan_digest="1" * 64,
        action_digest="2" * 64,
        target_id="k8s:pod:payment-svc",
        expected_resource_version="v1",
    )
    app1 = await service.create_approval_request(isolated_tenant, inc_id, req1)
    rejected = await service.reject(
        isolated_tenant,
        app1.id,
        ApprovalDecisionRequest(
            reviewer_id="bob",
            reviewer_role="platform_engineer",
            reason="Unsafe action for payment pod",
        ),
    )
    assert rejected.state == ApprovalState.REJECTED

    # Attempting to approve a rejected request raises error
    with pytest.raises(InvalidApprovalTransitionError):
        await service.approve(
            isolated_tenant,
            app1.id,
            ApprovalDecisionRequest(reviewer_id="alice", reviewer_role="incident_commander"),
        )

    # 2. Revocation test on approved request
    req2 = ApprovalCreateRequest(
        plan_id="plan-2",
        canonical_plan_digest="3" * 64,
        action_digest="4" * 64,
        target_id="k8s:pod:payment-svc",
        expected_resource_version="v1",
    )
    app2 = await service.create_approval_request(isolated_tenant, inc_id, req2)
    await service.approve(
        isolated_tenant,
        app2.id,
        ApprovalDecisionRequest(reviewer_id="alice", reviewer_role="incident_commander"),
    )
    revoked = await service.revoke(
        isolated_tenant,
        app2.id,
        ApprovalRevokeRequest(
            reviewer_id="bob",
            reviewer_role="platform_engineer",
            reason="New telemetry arrived showing different root cause",
        ),
    )
    assert revoked.state == ApprovalState.REVOKED


@pytest.mark.asyncio
async def test_p2_stale_incident_context_fencing(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"p2-stale-{uuid.uuid4()}",
        target_id="k8s:pod:cache-svc",
        request_sha256="5" * 64,
    )
    inc_id = admit.response.id
    service = ApprovalService(session_factory)

    app = await service.create_approval_request(
        isolated_tenant,
        inc_id,
        ApprovalCreateRequest(
            plan_id="plan-cache",
            canonical_plan_digest="6" * 64,
            action_digest="7" * 64,
            target_id="k8s:pod:cache-svc",
            expected_resource_version="v1",
        ),
    )

    # Incident status version is 1. If reviewer passes expected_incident_status_version=2, reject
    with pytest.raises(StaleContextError):
        await service.approve(
            isolated_tenant,
            app.id,
            ApprovalDecisionRequest(
                reviewer_id="alice",
                reviewer_role="incident_commander",
                expected_incident_status_version=99,
            ),
        )


@pytest.mark.asyncio
async def test_p2_dispatch_lease_two_phase_claiming_and_consumption(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"p2-dispatch-{uuid.uuid4()}",
        target_id="k8s:pod:worker-svc",
        request_sha256="8" * 64,
    )
    inc_id = admit.response.id
    service = ApprovalService(session_factory)

    plan_digest = "a1b2" * 16
    action_digest = "c3d4" * 16
    app = await service.create_approval_request(
        isolated_tenant,
        inc_id,
        ApprovalCreateRequest(
            plan_id="plan-worker",
            canonical_plan_digest=plan_digest,
            action_digest=action_digest,
            target_id="k8s:pod:worker-svc",
            expected_resource_version="v1",
        ),
    )
    await service.approve(
        isolated_tenant,
        app.id,
        ApprovalDecisionRequest(reviewer_id="alice", reviewer_role="incident_commander"),
    )

    # 1. Successful dispatch claim
    lease_id, ctx = await service.claim_dispatch_lease(
        isolated_tenant,
        app.id,
        target_version="v1",
        plan_digest=plan_digest,
        action_digest=action_digest,
    )
    assert lease_id is not None
    assert ctx.approval_id == app.id
    assert ctx.target_id == "k8s:pod:worker-svc"

    # 2. Idempotent re-entry for same lease
    lease_id_2, _ = await service.claim_dispatch_lease(
        isolated_tenant,
        app.id,
        target_version="v1",
        plan_digest=plan_digest,
        action_digest=action_digest,
    )
    assert lease_id_2 == lease_id

    # 3. Finalize consumed
    await service.finalize_consumed(isolated_tenant, app.id, lease_id)
    final_app = await service.get_approval(isolated_tenant, app.id)
    assert final_app.state == ApprovalState.CONSUMED

    # 4. Fresh approval for mismatch tests
    admit2 = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"p2-dispatch-mismatch-{uuid.uuid4()}",
        target_id="k8s:pod:worker-svc-2",
        request_sha256="8" * 64,
    )
    app2 = await service.create_approval_request(
        isolated_tenant,
        admit2.response.id,
        ApprovalCreateRequest(
            plan_id="plan-worker-2",
            canonical_plan_digest=plan_digest,
            action_digest=action_digest,
            target_id="k8s:pod:worker-svc-2",
            expected_resource_version="v1",
        ),
    )
    await service.approve(
        isolated_tenant,
        app2.id,
        ApprovalDecisionRequest(reviewer_id="alice", reviewer_role="incident_commander"),
    )

    # Digest mismatch rejection
    with pytest.raises(ApprovalDigestMismatchError):
        await service.claim_dispatch_lease(
            isolated_tenant,
            app2.id,
            target_version="v1",
            plan_digest="mismatched" * 6 + "0000",
            action_digest=action_digest,
        )

    # Version mismatch rejection
    with pytest.raises(ApprovalVersionMismatchError):
        await service.claim_dispatch_lease(
            isolated_tenant,
            app2.id,
            target_version="v2-advanced",
            plan_digest=plan_digest,
            action_digest=action_digest,
        )

    # Verify audit trail records the invalidation event
    async with session_factory() as session:
        audits = (
            await session.execute(
                select(ApprovalAudit).where(
                    ApprovalAudit.tenant_id == isolated_tenant,
                    ApprovalAudit.approval_id == app2.id,
                )
            )
        ).scalars().all()
        audit_actions = [a.action for a in audits]
        assert "INVALIDATED" in audit_actions
        inv_audit = next(a for a in audits if a.action == "INVALIDATED")
        assert "drift" in inv_audit.reason


@pytest.mark.asyncio
async def test_p2_approval_api_endpoints_via_async_client(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    api_client: AsyncClient,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"p2-api-{uuid.uuid4()}",
        target_id="k8s:pod:api-svc",
        request_sha256="9" * 64,
    )
    inc_id = admit.response.id

    headers = {"X-Tenant-ID": str(isolated_tenant)}
    plan_digest = "f" * 64
    action_digest = "e" * 64

    # 1. Create approval request
    create_resp = await api_client.post(
        f"/v1/plans/plan-api-restart/approvals?incident_id={inc_id}",
        json={
            "plan_id": "plan-api-restart",
            "canonical_plan_digest": plan_digest,
            "action_digest": action_digest,
            "action_payload": {"action_kind": "restart_pod"},
            "target_id": "k8s:pod:api-svc",
            "expected_resource_version": "v1",
            "policy_version": "v1",
            "ttl_seconds": 600,
        },
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    app_data = create_resp.json()
    approval_id = app_data["id"]
    assert app_data["state"] == "PENDING"

    # 2. Get approval
    get_resp = await api_client.get(f"/v1/approvals/{approval_id}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == approval_id

    # 3. A real MODEL principal must be denied; a body-only "model" label
    # cannot impersonate an actor kind or change the verified caller identity.
    model_token = mint_principal_token(
        actor_id="llm-bot",
        actor_kind=ActorKind.MODEL,
        tenant_id=isolated_tenant,
        roles=["release_owner"],
        secret_key=get_settings().auth_secret_key or "",
        ttl_seconds=300,
        auth_mechanism="pytest_model_actor",
    )
    model_headers = {
        "Authorization": f"Bearer {model_token}",
        "X-Tenant-ID": str(isolated_tenant),
    }
    bad_role_resp = await api_client.post(
        f"/v1/approvals/{approval_id}/approve",
        json={
            "reviewer_id": "llm-bot",
            "reviewer_role": "incident_commander",
            "reason": "self approval",
        },
        headers=model_headers,
    )
    assert bad_role_resp.status_code in (422, 403)

    # 4. Valid human approve
    commander_token = mint_principal_token(
        actor_id="commander-bob",
        actor_kind=ActorKind.HUMAN,
        tenant_id=isolated_tenant,
        roles=["incident_commander"],
        secret_key=get_settings().auth_secret_key or "",
        ttl_seconds=300,
        auth_mechanism="pytest_human_actor",
    )
    commander_headers = {
        "Authorization": f"Bearer {commander_token}",
        "X-Tenant-ID": str(isolated_tenant),
    }
    ok_approve = await api_client.post(
        f"/v1/approvals/{approval_id}/approve",
        json={
            "reviewer_id": "commander-bob",
            "reviewer_role": "incident_commander",
            "reason": "Approved for execution",
            "expected_incident_status_version": 1,
        },
        headers=commander_headers,
    )
    assert ok_approve.status_code == 200
    assert ok_approve.json()["state"] == "APPROVED"
    assert ok_approve.json()["reviewer_id"] == "commander-bob"

    # 5. Revoke approval
    revoke_resp = await api_client.post(
        f"/v1/approvals/{approval_id}/revoke",
        json={
            "reviewer_id": "commander-bob",
            "reviewer_role": "incident_commander",
            "reason": "Revoking decision",
        },
        headers=commander_headers,
    )
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["state"] == "REVOKED"
