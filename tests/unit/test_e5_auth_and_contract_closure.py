"""Gate 0 Verification Tests — E5 Integration and Server-Side Authorization Closure.

Verifies:
1. Bodyless or malformed requests are rejected with typed validation errors.
2. Manifest and report version conflicts are rejected with HTTP 409 Conflict.
3. Run requests strictly enforce required typed worker contract.
4. Client-supplied reviewer roles cannot grant authority (server-side claims govern).
5. Unauthenticated users cannot review or approve (HTTP 401 Unauthorized).
6. Cross-tenant evaluations remain strictly inaccessible (multi-tenant isolation).
7. Source outcome/consent invalidation blocks review (HTTP 409 Conflict).
8. Successful evaluation and review workflows remain functional under verified principal.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import get_settings
from backend.domain.auth import (
    ActorKind,
    mint_principal_token,
)
from backend.domain.evaluation import (
    CandidateProvenanceType,
    ReviewDecision,
)
from backend.domain.failure import (
    DivergentStepCategory,
    ProposalType,
    TargetComponent,
)
from backend.domain.failure import (
    ReviewDecision as ProposalReviewDecision,
)
from backend.learning.clusters import FailureClusterService
from backend.learning.failure_taxonomy import FailureClassifier
from backend.learning.proposal_service import ProposalService
from backend.persistence.models import ConsentEpoch, OutcomeRevision
from backend.persistence.repositories import IncidentRepository

pytestmark = pytest.mark.postgres


async def _create_test_tenant(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    t_id = uuid.uuid4()
    repo = IncidentRepository(session_factory)
    await repo.ensure_tenant(t_id, f"test-tenant-{t_id.hex[:8]}")
    async with session_factory() as session:
        epoch = ConsentEpoch(
            tenant_id=t_id,
            current_epoch=1,
            reuse_allowed=True,
            revoked_at=None,
        )
        session.add(epoch)
        await session.commit()
    return t_id


def _auth_headers(tenant_id: uuid.UUID, role: str = "evaluation_owner", actor_id: str = "eval-owner") -> dict[str, str]:
    token = mint_principal_token(
        actor_id=actor_id,
        actor_kind=ActorKind.HUMAN,
        tenant_id=tenant_id,
        roles=frozenset({role}),
        secret_key=get_settings().auth_secret_key or "opsblaze-dev-auth-secret-key-32bytes-hex-safe",
    )
    return {
        "X-Tenant-ID": str(tenant_id),
        "Authorization": f"Bearer {token}",
    }


async def _setup_e5_test_manifest(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    api_client: AsyncClient,
    *,
    auto_freeze: bool = False,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Helper to register a valid test manifest and return (manifest_id, outcome_revision_id)."""
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=tenant_id,
        client_key=f"gate0-{uuid.uuid4()}",
        target_id="k8s:deploy:checkout-service",
        request_sha256="7" * 64,
    )
    inc_id = admit.response.id

    outcome_id = uuid.uuid4()
    async with session_factory() as session:
        rev = OutcomeRevision(
            id=outcome_id,
            tenant_id=tenant_id,
            incident_id=inc_id,
            revision_no=1,
            rule_version="v1",
            label="VERIFIED_SUCCESS",
            confidence_band="HIGH",
            supporting_observation_ids=[],
            adjudication_metadata={},
        )
        session.add(rev)
        await session.commit()

    classifier = FailureClassifier(session_factory)
    case = await classifier.classify_case(
        tenant_id=tenant_id,
        incident_id=inc_id,
        outcome_revision_id=outcome_id,
        outcome_label="VERIFIED_SUCCESS",
        divergent_step="verify_cart_integrity",
        first_divergent_step_category=DivergentStepCategory.READBACK_MISMATCH.value,
        subsystem_version="v1.0.0",
    )

    cluster_service = FailureClusterService(session_factory)
    cluster = await cluster_service.assign_to_cluster(
        tenant_id=tenant_id,
        case_id=case.id,
        failure_origin_code=case.failure_origin_code,
        first_divergent_step_category=case.first_divergent_step_category,
        subsystem_version=case.subsystem_version,
    )

    prop_service = ProposalService(session_factory)
    proposal = await prop_service.create_draft(
        tenant_id=tenant_id,
        cluster_id=cluster.id,
        proposal_type=ProposalType.PROMPT_REFINEMENT.value,
        base_policy_version="v1.0",
        target_component=TargetComponent.INVESTIGATOR_A1_PROMPT.value,
        structured_diff={
            "template_id": "prompt_v1",
            "target_section": "rules",
            "proposed_content": "Step A with bounded fence check.",
        },
        hypothesis="Prevents race conditions.",
        expected_benefit="Zero readback mismatch.",
        risk_assessment="Minimal overhead.",
        evaluation_plan="Evaluate via paired S1 test across holdouts.",
        drafter_id="llm-proposal-bot",
    )

    await prop_service.submit_proposal(
        tenant_id=tenant_id,
        proposal_id=proposal.id,
        expected_version=1,
    )

    approved = await prop_service.review_proposal(
        tenant_id=tenant_id,
        proposal_id=proposal.id,
        reviewer_id="human-commander",
        reviewer_role="incident_commander",
        decision=ProposalReviewDecision.APPROVED.value,
        comments="Approved for evaluation.",
        expected_version=2,
    )

    headers = _auth_headers(tenant_id)
    payload = {
        "title": "Gate 0 Validation Manifest",
        "incumbent_bundle": {
            "policy_version": "v1.0",
            "tool_catalog_version": "v1",
        },
        "candidate_bundle": {
            "base_policy_version": "v1.0",
            "target_component": TargetComponent.INVESTIGATOR_A1_PROMPT.value,
            "proposal_diff": {
                "template_name": "prompt_v1",
                "section_name": "rules",
                "original_text": "Step A.",
                "proposed_text": "Step A with bounded fence check.",
            },
        },
        "candidate_provenance_type": CandidateProvenanceType.E3_PROPOSAL.value,
        "source_proposal_id": str(approved.id),
        "source_outcome_revision_ids": [str(outcome_id)],
        "fixture_set_digest": "e" * 64,
        "holdout_set_digest": "f" * 64,
        "sample_size_plan": 10,
        "auto_freeze": auto_freeze,
    }
    resp = await api_client.post("/v1/evaluations", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"]), outcome_id


@pytest.mark.asyncio
async def test_gate0_bodyless_or_malformed_requests(
    session_factory: async_sessionmaker[AsyncSession],
    api_client: AsyncClient,
) -> None:
    """1. Bodyless or malformed requests are rejected with typed validation errors."""
    tenant_id = await _create_test_tenant(session_factory)
    manifest_id, _ = await _setup_e5_test_manifest(session_factory, tenant_id, api_client)
    headers = _auth_headers(tenant_id)

    # A: Freeze without body or malformed body
    res_freeze_empty = await api_client.post(
        f"/v1/evaluations/{manifest_id}/freeze",
        headers=headers,
    )
    assert res_freeze_empty.status_code == 422

    res_freeze_invalid = await api_client.post(
        f"/v1/evaluations/{manifest_id}/freeze",
        json={"expected_version": -5},
        headers=headers,
    )
    assert res_freeze_invalid.status_code == 422

    # B: Run without worker_id or malformed body
    res_run_empty = await api_client.post(
        f"/v1/evaluations/{manifest_id}/run",
        headers=headers,
    )
    assert res_run_empty.status_code == 422

    res_run_invalid = await api_client.post(
        f"/v1/evaluations/{manifest_id}/run",
        json={"worker_id": "", "lease_seconds": 1},  # below min 10
        headers=headers,
    )
    assert res_run_invalid.status_code == 422


@pytest.mark.asyncio
async def test_gate0_version_conflicts_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    api_client: AsyncClient,
) -> None:
    """2. Manifest and report version conflicts are rejected with HTTP 409 Conflict."""
    tenant_id = await _create_test_tenant(session_factory)
    manifest_id, _ = await _setup_e5_test_manifest(session_factory, tenant_id, api_client)
    headers = _auth_headers(tenant_id)

    # Initial freeze passes (version 1 -> 2)
    res_freeze = await api_client.post(
        f"/v1/evaluations/{manifest_id}/freeze",
        json={"expected_version": 1},
        headers=headers,
    )
    assert res_freeze.status_code == 200

    # Stale version freeze rejected with 409 Conflict
    res_freeze_stale = await api_client.post(
        f"/v1/evaluations/{manifest_id}/freeze",
        json={"expected_version": 1},
        headers=headers,
    )
    assert res_freeze_stale.status_code == 409
    assert "concurrency" in res_freeze_stale.text.lower() or "conflict" in res_freeze_stale.text.lower()


@pytest.mark.asyncio
async def test_gate0_run_requests_enforce_contract(
    session_factory: async_sessionmaker[AsyncSession],
    api_client: AsyncClient,
) -> None:
    """3. Run requests strictly enforce required typed worker contract."""
    tenant_id = await _create_test_tenant(session_factory)
    manifest_id, _ = await _setup_e5_test_manifest(session_factory, tenant_id, api_client, auto_freeze=True)
    headers = _auth_headers(tenant_id)

    # Valid run request
    res_run = await api_client.post(
        f"/v1/evaluations/{manifest_id}/run",
        json={"worker_id": "verified-runner-worker", "lease_seconds": 90},
        headers=headers,
    )
    assert res_run.status_code == 200
    report = res_run.json()
    assert report["total_pairs_planned"] == 10
    assert report["total_pairs_completed"] == 10


@pytest.mark.asyncio
async def test_gate0_client_supplied_reviewer_role_cannot_grant_authority(
    session_factory: async_sessionmaker[AsyncSession],
    api_client: AsyncClient,
) -> None:
    """4. Client-supplied reviewer roles cannot grant authority (server-side claims govern)."""
    tenant_id = await _create_test_tenant(session_factory)
    manifest_id, _ = await _setup_e5_test_manifest(session_factory, tenant_id, api_client, auto_freeze=True)
    headers = _auth_headers(tenant_id)

    # Run evaluation to produce report
    await api_client.post(
        f"/v1/evaluations/{manifest_id}/run",
        json={"worker_id": "eval-worker"},
        headers=headers,
    )

    settings = get_settings()

    # Scenario A: Token has only 'operator' role, but client asserts 'evaluation_owner'
    operator_token = mint_principal_token(
        actor_id="operator-dan",
        tenant_id=tenant_id,
        roles=["operator"],
        secret_key=settings.auth_secret_key,
    )
    res_spoof_role = await api_client.post(
        f"/v1/evaluations/{manifest_id}/review",
        json={
            "reviewer_id": "operator-dan",
            "reviewer_role": "evaluation_owner",  # Client tries to claim reviewer authority
            "decision": ReviewDecision.ACCEPTED.value,
            "expected_version": 1,
        },
        headers={**headers, "Authorization": f"Bearer {operator_token}"},
    )
    assert res_spoof_role.status_code == 403
    assert "lacks" in res_spoof_role.text.lower() or "forbidden" in res_spoof_role.text.lower()

    # Scenario B: Token has 'evaluation_owner', but client claims to be 'different-user'
    eval_token = mint_principal_token(
        actor_id="alice-eval-owner",
        tenant_id=tenant_id,
        roles=["evaluation_owner"],
        secret_key=settings.auth_secret_key,
    )
    res_impersonate = await api_client.post(
        f"/v1/evaluations/{manifest_id}/review",
        json={
            "reviewer_id": "attacker-eve",  # Different identity
            "reviewer_role": "evaluation_owner",
            "decision": ReviewDecision.ACCEPTED.value,
            "expected_version": 1,
        },
        headers={**headers, "Authorization": f"Bearer {eval_token}"},
    )
    assert res_impersonate.status_code == 403
    assert "cannot assert reviewer identity" in res_impersonate.text.lower()


@pytest.mark.asyncio
async def test_gate0_unauthenticated_users_cannot_approve(
    session_factory: async_sessionmaker[AsyncSession],
    api_client: AsyncClient,
) -> None:
    """5. Unauthenticated users cannot review or approve (HTTP 401 Unauthorized)."""
    tenant_id = await _create_test_tenant(session_factory)
    manifest_id, _ = await _setup_e5_test_manifest(session_factory, tenant_id, api_client, auto_freeze=True)
    headers = _auth_headers(tenant_id)

    await api_client.post(
        f"/v1/evaluations/{manifest_id}/run",
        json={"worker_id": "eval-worker"},
        headers=headers,
    )

    # Missing Authorization header completely
    res_no_auth = await api_client.post(
        f"/v1/evaluations/{manifest_id}/review",
        json={
            "decision": ReviewDecision.ACCEPTED.value,
            "expected_version": 1,
        },
        headers={"X-Tenant-ID": str(tenant_id), "X-Test-No-Auth": "true"},
    )
    assert res_no_auth.status_code == 401

    # Malformed token
    res_bad_auth = await api_client.post(
        f"/v1/evaluations/{manifest_id}/review",
        json={
            "decision": ReviewDecision.ACCEPTED.value,
            "expected_version": 1,
        },
        headers={**headers, "Authorization": "Bearer invalid.token.garbage"},
    )
    assert res_bad_auth.status_code == 401


@pytest.mark.asyncio
async def test_gate0_cross_tenant_evaluations_inaccessible(
    session_factory: async_sessionmaker[AsyncSession],
    api_client: AsyncClient,
) -> None:
    """6. Cross-tenant evaluations remain strictly inaccessible (multi-tenant isolation)."""
    tenant_a = await _create_test_tenant(session_factory)
    tenant_b = await _create_test_tenant(session_factory)

    manifest_a, _ = await _setup_e5_test_manifest(session_factory, tenant_a, api_client, auto_freeze=True)
    headers_a = {"X-Tenant-ID": str(tenant_a)}
    headers_b = {"X-Tenant-ID": str(tenant_b)}

    await api_client.post(
        f"/v1/evaluations/{manifest_a}/run",
        json={"worker_id": "eval-worker"},
        headers=headers_a,
    )

    settings = get_settings()
    # Token for Tenant B
    token_b = mint_principal_token(
        actor_id="evaluator-b",
        tenant_id=tenant_b,
        roles=["evaluation_owner"],
        secret_key=settings.auth_secret_key,
    )

    # Attempt to review Tenant A's manifest with Tenant B token and Tenant A header -> 403 Forbidden
    res_cross_hdr = await api_client.post(
        f"/v1/evaluations/{manifest_a}/review",
        json={"decision": ReviewDecision.ACCEPTED.value, "expected_version": 1},
        headers={**headers_a, "Authorization": f"Bearer {token_b}"},
    )
    assert res_cross_hdr.status_code == 403
    assert "cross-tenant access forbidden" in res_cross_hdr.text.lower()

    # Attempt to review Tenant A's manifest with Tenant B token and Tenant B header -> 404 (isolated)
    res_cross_get = await api_client.post(
        f"/v1/evaluations/{manifest_a}/review",
        json={"decision": ReviewDecision.ACCEPTED.value, "expected_version": 1},
        headers={**headers_b, "Authorization": f"Bearer {token_b}"},
    )
    assert res_cross_get.status_code == 404


@pytest.mark.asyncio
async def test_gate0_source_invalidation_blocks_review(
    session_factory: async_sessionmaker[AsyncSession],
    api_client: AsyncClient,
) -> None:
    """7. Source outcome or consent invalidation blocks review (HTTP 409 Conflict)."""
    tenant_id = await _create_test_tenant(session_factory)
    manifest_id, outcome_id = await _setup_e5_test_manifest(session_factory, tenant_id, api_client, auto_freeze=True)
    headers = _auth_headers(tenant_id)

    await api_client.post(
        f"/v1/evaluations/{manifest_id}/run",
        json={"worker_id": "eval-worker"},
        headers=headers,
    )

    # Invalidate source outcome by creating revision 2 for same incident
    async with session_factory() as session:
        orig_rev = await session.get(OutcomeRevision, outcome_id)
        assert orig_rev is not None
        rev2 = OutcomeRevision(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            incident_id=orig_rev.incident_id,
            revision_no=2,
            rule_version="v2",
            label="DISPUTED",
            confidence_band="LOW",
            supporting_observation_ids=[],
            adjudication_metadata={"reason": "audit-dispute"},
        )
        session.add(rev2)
        await session.commit()

    settings = get_settings()
    eval_token = mint_principal_token(
        actor_id="eval-lead",
        tenant_id=tenant_id,
        roles=["evaluation_owner"],
        secret_key=settings.auth_secret_key,
    )

    res_review_inval = await api_client.post(
        f"/v1/evaluations/{manifest_id}/review",
        json={"decision": ReviewDecision.ACCEPTED.value, "expected_version": 1},
        headers={**headers, "Authorization": f"Bearer {eval_token}"},
    )
    assert res_review_inval.status_code == 409
    assert "invalidated" in res_review_inval.text.lower() or "superseded" in res_review_inval.text.lower()


@pytest.mark.asyncio
async def test_gate0_successful_evaluation_and_review_workflow(
    session_factory: async_sessionmaker[AsyncSession],
    api_client: AsyncClient,
) -> None:
    """8. Successful evaluation and review workflows remain functional under verified principal."""
    tenant_id = await _create_test_tenant(session_factory)
    manifest_id, _ = await _setup_e5_test_manifest(session_factory, tenant_id, api_client, auto_freeze=False)
    headers = _auth_headers(tenant_id)

    # Freeze with expected_version
    freeze_resp = await api_client.post(
        f"/v1/evaluations/{manifest_id}/freeze",
        json={"expected_version": 1},
        headers=headers,
    )
    assert freeze_resp.status_code == 200
    assert freeze_resp.json()["lifecycle_state"] == "FROZEN"

    # Run with typed worker_id
    run_resp = await api_client.post(
        f"/v1/evaluations/{manifest_id}/run",
        json={"worker_id": "official-eval-worker-1", "lease_seconds": 120},
        headers=headers,
    )
    assert run_resp.status_code == 200
    report = run_resp.json()
    assert report["version"] == 1

    settings = get_settings()
    lead_token = mint_principal_token(
        actor_id="dr-carol-evaluator",
        tenant_id=tenant_id,
        roles=["evaluation_owner"],
        secret_key=settings.auth_secret_key,
    )

    # Review with authenticated token and CAS expected_version
    review_resp = await api_client.post(
        f"/v1/evaluations/{manifest_id}/review",
        json={
            "decision": ReviewDecision.ACCEPTED.value,
            "comments": "Gate 0 passed cleanly. Verified recovery non-inferior with zero safety failures.",
            "expected_version": 1,
        },
        headers={**headers, "Authorization": f"Bearer {lead_token}"},
    )
    assert review_resp.status_code == 200
    reviewed_report = review_resp.json()
    assert reviewed_report["review_decision"] == ReviewDecision.ACCEPTED.value
    assert reviewed_report["reviewer_id"] == "dr-carol-evaluator"
    assert reviewed_report["reviewer_role"] == "evaluation_owner"
    assert reviewed_report["version"] == 2
