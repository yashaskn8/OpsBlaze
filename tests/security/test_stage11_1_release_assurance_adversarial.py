"""Adversarial and security integration tests for Stage 11.1 (Gates B through H)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from backend.domain.approval import (
    ApprovalState,
)
from backend.domain.auth import (
    ActorKind,
    PrincipalRole,
    mint_principal_token,
)
from backend.domain.release import (
    CanaryArm,
)
from backend.persistence.models import Incident
from backend.releases.bundle_service import PolicyBundleService
from backend.releases.release_service import PolicyReleaseService
from backend.releases.router_service import PolicyRouterService


def make_test_auth_header(
    *,
    tenant_id: uuid.UUID,
    actor_id: str = "test-user",
    actor_kind: ActorKind = ActorKind.HUMAN,
    roles: list[str] | None = None,
    secret_key: str = "opsblaze-dev-auth-secret-key-32bytes-hex-safe",
) -> dict[str, str]:
    token = mint_principal_token(
        actor_id=actor_id,
        actor_kind=actor_kind,
        tenant_id=tenant_id,
        roles=roles or [PrincipalRole.OPERATOR.value],
        secret_key=secret_key,
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-ID": str(tenant_id),
    }


# ==============================================================================
# Gate B Tests: Legacy API Migration & P2/E5 Closure
# ==============================================================================

@pytest.mark.asyncio
async def test_tenant_header_alone_cannot_authenticate(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
) -> None:
    """X-Tenant-ID alone without Bearer token must return HTTP 401 Unauthorized across APIs."""
    headers_no_token = {"X-Tenant-ID": str(isolated_tenant)}

    # Incidents API
    r_inc = await api_client.post(
        "/v1/incidents",
        json={"target_id": "sim-db-primary", "title": "Test", "severity": "SEV2"},
        headers=headers_no_token,
    )
    assert r_inc.status_code == 401

    # Evaluations API
    r_eval = await api_client.get("/v1/evaluations/manifests", headers=headers_no_token)
    assert r_eval.status_code == 401

    # Approvals API
    r_app = await api_client.get(f"/v1/approvals/{uuid.uuid4()}", headers=headers_no_token)
    assert r_app.status_code == 401


@pytest.mark.asyncio
async def test_p2_approval_derives_reviewer_from_principal(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
    session_factory,
) -> None:
    """P2 approval must derive reviewer_id and reviewer_role from verified principal, rejecting client assertions."""
    # Create an incident and pending approval in DB
    inc_id = uuid.uuid4()
    app_id = uuid.uuid4()
    async with session_factory() as session:
        inc = Incident(
            id=inc_id,
            tenant_id=isolated_tenant,
            client_key=f"p2-test-{uuid.uuid4()}",
            request_sha256="0" * 64,
            target_id="sim-db-primary",
            status_projection="AWAITING_APPROVAL",
            status_version=1,
        )
        session.add(inc)
        await session.flush()
        from datetime import UTC, datetime, timedelta

        from backend.persistence.models import ApprovalRequest
        app_req = ApprovalRequest(
            id=app_id,
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            plan_id="plan-1",
            canonical_plan_digest="a" * 64,
            action_digest="b" * 64,
            target_id="sim-db-primary",
            expected_resource_version="v1",
            policy_version="v1",
            state=ApprovalState.PENDING.value,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(app_req)
        await session.commit()

    # Authenticate as verified incident_commander
    auth_hdr = make_test_auth_header(
        tenant_id=isolated_tenant,
        actor_id="verified-commander-42",
        roles=[PrincipalRole.INCIDENT_COMMANDER.value, PrincipalRole.OPERATOR.value],
    )

    # Client tries to assert a fake reviewer_id and spoofed role in body
    res = await api_client.post(
        f"/v1/approvals/{app_id}/approve",
        json={
            "reviewer_id": "spoofed-reviewer",
            "reviewer_role": "spoofed-role",
            "reason": "Authorized mitigation",
        },
        headers=auth_hdr,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["state"] == "APPROVED"
    # Reviewer ID and role must be derived from the authentic Principal!
    assert data["reviewer_id"] == "verified-commander-42"
    assert data["reviewer_role"] == PrincipalRole.INCIDENT_COMMANDER.value


@pytest.mark.asyncio
async def test_bot_cannot_approve_p2_plan(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
) -> None:
    """A BOT or MODEL principal must be rejected with 403 Forbidden from approving plans."""
    bot_auth = make_test_auth_header(
        tenant_id=isolated_tenant,
        actor_id="bot-assistant",
        actor_kind=ActorKind.BOT,
        roles=[PrincipalRole.INCIDENT_COMMANDER.value],
    )
    res = await api_client.post(
        f"/v1/approvals/{uuid.uuid4()}/approve",
        json={"reason": "Bot self-approval"},
        headers=bot_auth,
    )
    assert res.status_code == 403


# ==============================================================================
# Gate C Tests: E6 Role Authorization Matrix
# ==============================================================================

@pytest.mark.asyncio
async def test_unauthorized_role_cannot_promote_release(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
    session_factory,
) -> None:
    """An operator without release_owner or incident_commander role cannot promote a release."""
    bundle_svc = PolicyBundleService(session_factory)
    release_svc = PolicyReleaseService(session_factory, bundle_service=bundle_svc)

    incumbent = await bundle_svc.ensure_bootstrap_incumbent(
        tenant_id=isolated_tenant,
        target_component="investigator_planner",
        created_by="system",
    )
    candidate = await bundle_svc.create_bundle(
        tenant_id=isolated_tenant,
        bundle_name="Cand-Promote-Auth",
        base_policy_version="v2.0.0",
        target_component="investigator_planner",
        created_by="eng",
        model_id="gpt-4o",
        model_version="2026-11",
        tool_catalog_version="v2",
        a2_grant_version="v2",
        source_provenance_digest="2" * 64,
    )
    await bundle_svc.seal_bundle(tenant_id=isolated_tenant, bundle_id=candidate.id)
    rel = await release_svc.create_release(
        tenant_id=isolated_tenant,
        bundle_id=candidate.id,
        incumbent_bundle_id=incumbent.id,
        target_component="investigator_planner",
        drafter_id="eng",
    )

    # Authenticate as operator ONLY (lacks release_owner / incident_commander)
    operator_auth = make_test_auth_header(
        tenant_id=isolated_tenant,
        actor_id="operator-joe",
        roles=[PrincipalRole.OPERATOR.value],
    )

    res = await api_client.post(
        f"/v1/releases/{rel.id}/promote-active",
        json={"expected_version": rel.version, "target_state": "ACTIVE"},
        headers=operator_auth,
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_unauthorized_role_cannot_modify_canary_percentage(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
) -> None:
    """Only release_owner or incident_commander can modify canary percentage."""
    eng_auth = make_test_auth_header(
        tenant_id=isolated_tenant,
        actor_id="eng-bob",
        roles=[PrincipalRole.PLATFORM_ENGINEER.value, PrincipalRole.OPERATOR.value],
    )
    res = await api_client.post(
        f"/v1/releases/{uuid.uuid4()}/update-canary",
        json={"expected_version": 1, "target_state": "CANARY", "canary_percentage": 25},
        headers=eng_auth,
    )
    assert res.status_code == 403


# ==============================================================================
# Gate D & E Tests: Guardrail Realism & Automated Runtime Execution
# ==============================================================================

@pytest.mark.asyncio
async def test_public_guardrail_endpoint_rejects_simulated_breaches(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
) -> None:
    """The public /v1/releases/{id}/guardrail-check endpoint must reject client-supplied simulated breaches."""
    auth_hdr = make_test_auth_header(
        tenant_id=isolated_tenant,
        actor_id="op-1",
        roles=[PrincipalRole.RELEASE_OWNER.value],
    )
    res = await api_client.post(
        f"/v1/releases/{uuid.uuid4()}/guardrail-check",
        json={"simulated_invariant_breaches": 5, "invariant_breach_detail": "Malicious trigger"},
        headers=auth_hdr,
    )
    # Extra/unsupported fields should be rejected with 422 Unprocessable Entity
    assert res.status_code == 422


# ==============================================================================
# Gate F Tests: B2/P2 Fencing and Active Incident Safety
# ==============================================================================

@pytest.mark.asyncio
async def test_rolled_back_release_blocks_candidate_consequential_mutation(
    session_factory,
    isolated_tenant: uuid.UUID,
) -> None:
    """If candidate release is ROLLED_BACK, verify_consequential_mutation_allowed fails closed."""
    bundle_svc = PolicyBundleService(session_factory)
    release_svc = PolicyReleaseService(session_factory, bundle_service=bundle_svc)
    router_svc = PolicyRouterService(session_factory)

    incumbent = await bundle_svc.ensure_bootstrap_incumbent(
        tenant_id=isolated_tenant,
        target_component="investigator_planner",
        created_by="system",
    )
    candidate = await bundle_svc.create_bundle(
        tenant_id=isolated_tenant,
        bundle_name="Cand-Fencing-Test",
        base_policy_version="v2.0.0",
        target_component="investigator_planner",
        created_by="eng",
        model_id="gpt-4o",
        model_version="2026-11",
        tool_catalog_version="v2",
        a2_grant_version="v2",
        source_provenance_digest="2" * 64,
    )
    await bundle_svc.seal_bundle(tenant_id=isolated_tenant, bundle_id=candidate.id)
    rel = await release_svc.create_release(
        tenant_id=isolated_tenant,
        bundle_id=candidate.id,
        incumbent_bundle_id=incumbent.id,
        target_component="investigator_planner",
        drafter_id="eng",
    )
    await release_svc.submit_for_approval(
        tenant_id=isolated_tenant,
        release_id=rel.id,
        expected_version=1,
        actor_id="eng",
        actor_role=PrincipalRole.PLATFORM_ENGINEER.value,
    )
    await release_svc.review_release(
        tenant_id=isolated_tenant,
        release_id=rel.id,
        expected_version=2,
        decision="APPROVED",
        reviewer_id="boss",
        reviewer_role=PrincipalRole.RELEASE_OWNER.value,
    )
    await release_svc.activate_canary(
        tenant_id=isolated_tenant,
        release_id=rel.id,
        expected_version=3,
        canary_percentage=50,
        actor_id="boss",
        actor_role=PrincipalRole.RELEASE_OWNER.value,
    )

    # Create candidate incident pinned to this release
    inc_id = uuid.uuid4()
    async with session_factory() as session:
        inc = Incident(
            id=inc_id,
            tenant_id=isolated_tenant,
            client_key=f"fence-cand-{uuid.uuid4()}",
            request_sha256="0" * 64,
            target_id="sim-db-primary",
            status_projection="INVESTIGATING",
            status_version=1,
            policy_release_id=rel.id,
            canary_arm=CanaryArm.CANDIDATE.value,
            pinned_policy_bundle_id=candidate.id,
        )
        session.add(inc)
        await session.commit()

    # While CANARY, mutations are permitted
    allowed, _ = await router_svc.verify_consequential_mutation_allowed(
        tenant_id=isolated_tenant,
        incident_id=inc_id,
    )
    assert allowed is True

    # Roll back release
    await release_svc.trigger_rollback(
        tenant_id=isolated_tenant,
        release_id=rel.id,
        expected_version=4,
        rollback_reason="Safety trip",
        actor_id="sre",
        actor_role=PrincipalRole.SRE_LEAD.value,
    )

    # Now verify_consequential_mutation_allowed must fail closed!
    allowed_post, reason = await router_svc.verify_consequential_mutation_allowed(
        tenant_id=isolated_tenant,
        incident_id=inc_id,
    )
    assert allowed_post is False
    assert "consequential mutations blocked" in reason.lower()
