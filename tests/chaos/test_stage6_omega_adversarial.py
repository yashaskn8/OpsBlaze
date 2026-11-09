"""Stage 6 Omega Adversarial Chaos Campaign.

Stress-tests:
1. Target Version Drift & Stale Approval Invalidation (Split-Brain Prevention)
2. Autonomous Agent / LLM Self-Approval Injection Prevention
3. Two-Phase Dispatch Lease Expiry, Zombie Worker Rejection & Idempotent Re-entry
4. S1 Negative Control Adversarial Telemetry Noise Immunity (100% Pass Rate Gate)
5. Concurrent Operator Approve vs Reject Serialization Barrier (Row-Level Locking)
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.approvals.service import ApprovalService
from backend.config import get_settings
from backend.domain.approval import (
    ApprovalCreateRequest,
    ApprovalDecisionRequest,
    ApprovalError,
    ApprovalState,
    ApprovalVersionMismatchError,
)
from backend.domain.auth import ActorKind, mint_principal_token
from backend.experiments.oracle import HiddenLabel, IndependentOracle
from backend.experiments.s1_replay import DiagnosticFixture, StrategyExecutor
from backend.persistence.repositories import IncidentRepository


@pytest.mark.asyncio
async def test_adversarial_1_target_version_drift_invalidates_approval(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Drill 1: Plan approved for version 'v1', but target drifts to 'v2' out-of-band.
    Dispatch lease claim must detect version mismatch, transition state to INVALIDATED,
    and fail closed to prevent split-brain mutation on drifted state.
    """
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"drift-{uuid.uuid4()}",
        target_id="k8s:pod:drift-svc",
        request_sha256="d" * 64,
    )
    inc_id = admit.response.id
    service = ApprovalService(session_factory)

    # 1. Create and approve for target version 'v1'
    req = ApprovalCreateRequest(
        plan_id="plan-restart-drift",
        canonical_plan_digest="1" * 64,
        action_digest="2" * 64,
        target_id="k8s:pod:drift-svc",
        expected_resource_version="v1",
    )
    app = await service.create_approval_request(isolated_tenant, inc_id, req)
    await service.approve(
        isolated_tenant,
        app.id,
        ApprovalDecisionRequest(
            reviewer_id="commander-bob",
            reviewer_role="incident_commander",
            reason="Approved against observed v1 state",
        ),
    )

    # 2. Dispatch lease claimed against drifted target version 'v2'
    with pytest.raises(ApprovalVersionMismatchError) as exc_info:
        await service.claim_dispatch_lease(
            tenant_id=isolated_tenant,
            approval_id=app.id,
            target_version="v2",  # Target drifted!
            plan_digest="1" * 64,
            action_digest="2" * 64,
        )

    assert "mismatch" in str(exc_info.value).lower()

    # Verify approval state in DB transitioned to INVALIDATED
    app_after = await service.get_approval(isolated_tenant, app.id)
    assert app_after.state == ApprovalState.INVALIDATED


@pytest.mark.asyncio
async def test_adversarial_2_model_and_agent_self_approval_rejected_sever_side(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    api_client: AsyncClient,
) -> None:
    """Drill 2: Adversarial model or prompt-injected agent attempts to approve its own plan.
    Deterministic backend must reject all non-human roles with HTTP 403 Forbidden.
    """
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"self-app-{uuid.uuid4()}",
        target_id="k8s:pod:auth-svc",
        request_sha256="e" * 64,
    )
    inc_id = admit.response.id
    headers = {"X-Tenant-ID": str(isolated_tenant)}
    token = mint_principal_token(
        actor_id="stage6-test-operator",
        actor_kind=ActorKind.HUMAN,
        tenant_id=isolated_tenant,
        roles=["incident_commander", "operator"],
        secret_key=get_settings().auth_secret_key or "",
        ttl_seconds=300,
        auth_mechanism="pytest_stage6_omega",
    )
    headers["Authorization"] = f"Bearer {token}"

    # Create pending approval
    create_resp = await api_client.post(
        f"/v1/plans/plan-self/approvals?incident_id={inc_id}",
        json={
            "plan_id": "plan-self",
            "canonical_plan_digest": "3" * 64,
            "action_digest": "4" * 64,
            "target_id": "k8s:pod:auth-svc",
            "expected_resource_version": "v1",
            "policy_version": "v1",
        },
        headers=headers,
    )
    assert create_resp.status_code == 201
    approval_id = create_resp.json()["id"]

    # Multiple forbidden autonomous roles attempted
    forbidden_roles = [
        "model",
        "agent",
        "investigator",
        "llm",
        "system",
        "autonomous_agent",
        "prompt_injection",
    ]
    for role in forbidden_roles:
        model_token = mint_principal_token(
            actor_id=f"adversarial-agent-{role}",
            actor_kind=ActorKind.MODEL,
            tenant_id=isolated_tenant,
            roles=[role],
            secret_key=get_settings().auth_secret_key or "",
            ttl_seconds=300,
            auth_mechanism="pytest_adversarial_model",
        )
        model_headers = {
            "Authorization": f"Bearer {model_token}",
            "X-Tenant-ID": str(isolated_tenant),
        }
        res = await api_client.post(
            f"/v1/approvals/{approval_id}/approve",
            json={
                "reviewer_id": "adversarial-agent-007",
                "reviewer_role": role,
                "reason": "I approve my own proposed action",
            },
            headers=model_headers,
        )
        assert res.status_code == 403, f"MODEL principal with role {role} was not rejected"

    # Verify state remains PENDING
    get_res = await api_client.get(f"/v1/approvals/{approval_id}", headers=headers)
    assert get_res.json()["state"] == "PENDING"


@pytest.mark.asyncio
async def test_adversarial_3_dispatch_lease_expiry_and_zombie_worker_rejection(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Drill 3: Worker 1 claims dispatch lease, but times out / stalls.
    Lease expires, Worker 2 claims fresh lease. Zombie Worker 1 then attempts
    to finalize consumption with its stale lease ID and must be safely rejected.
    """
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"zombie-{uuid.uuid4()}",
        target_id="k8s:pod:worker-svc",
        request_sha256="f" * 64,
    )
    inc_id = admit.response.id
    service = ApprovalService(session_factory)

    req = ApprovalCreateRequest(
        plan_id="plan-zombie",
        canonical_plan_digest="5" * 64,
        action_digest="6" * 64,
        target_id="k8s:pod:worker-svc",
        expected_resource_version="v1",
    )
    app = await service.create_approval_request(isolated_tenant, inc_id, req)
    await service.approve(
        isolated_tenant,
        app.id,
        ApprovalDecisionRequest(
            reviewer_id="commander-bob",
            reviewer_role="incident_commander",
        ),
    )

    t0 = datetime.now(UTC)
    # Worker 1 claims with 10s lease
    lease_w1, _ = await service.claim_dispatch_lease(
        tenant_id=isolated_tenant,
        approval_id=app.id,
        target_version="v1",
        plan_digest="5" * 64,
        action_digest="6" * 64,
        lease_seconds=10.0,
        now=t0,
    )

    # Fast forward time past lease expiration (15 seconds later)
    t1 = t0 + timedelta(seconds=15)

    # Worker 2 reclaims the expired dispatch lease
    lease_w2, _ = await service.claim_dispatch_lease(
        tenant_id=isolated_tenant,
        approval_id=app.id,
        target_version="v1",
        plan_digest="5" * 64,
        action_digest="6" * 64,
        lease_seconds=30.0,
        now=t1,
    )
    assert lease_w2 != lease_w1

    # Zombie Worker 1 attempts to finalize consumption with stale lease_w1
    await service.finalize_consumed(
        tenant_id=isolated_tenant,
        approval_id=app.id,
        lease_id=lease_w1,  # Stale!
        now=t1,
    )

    # Approval must NOT be consumed by stale Worker 1
    state_after_zombie = await service.get_approval(isolated_tenant, app.id, now=t1)
    assert state_after_zombie.state == ApprovalState.DISPATCHING
    assert state_after_zombie.dispatch_lease_id == lease_w2

    # Legitimate Worker 2 finalizes consumption
    await service.finalize_consumed(
        tenant_id=isolated_tenant,
        approval_id=app.id,
        lease_id=lease_w2,
        now=t1,
    )
    state_final = await service.get_approval(isolated_tenant, app.id, now=t1)
    assert state_final.state == ApprovalState.CONSUMED


def test_adversarial_4_s1_negative_control_noisy_telemetry_immunity() -> None:
    """Drill 4: S1 Negative control with high-variance noisy telemetry (simulated HTTP 500 blip).
    Strategy C must correctly distinguish transient noise from actionable root causes
    and maintain a strict 100% negative control pass rate.
    """
    oracle = IndependentOracle()

    # 10 noisy negative control variations
    noisy_negative_controls = [
        DiagnosticFixture(
            fixture_id=f"noisy-neg-{i}",
            target_id=f"k8s:pod:service-{i}",
            is_negative_control=True,
            symptom_description=f"Transient single spike: {i}% error rate returning to 0 within 50ms",
            available_probes=[
                {"id": "deep_probe", "cost": 100, "discriminates": True, "finding": "healthy_after_blip"},
                {"id": "surface_probe", "cost": 10, "discriminates": False, "finding": "sporadic_noise"},
            ],
            hidden_label=HiddenLabel(
                fixture_id=f"noisy-neg-{i}",
                is_negative_control=True,
                true_root_cause="transient_network_blip",
                expected_action_kind="NO_OP",
                target_id=f"k8s:pod:service-{i}",
            ),
        )
        for i in range(10)
    ]

    for fix in noisy_negative_controls:
        # Run Strategy A (Runbook: triggers mutation blindly)
        act_a, _, _, _ = StrategyExecutor.run_strategy_a_runbook(fix)
        corr_a, out_a = oracle.adjudicate(fix.hidden_label, act_a, abstained=act_a is None)
        assert corr_a is False  # Runbook fails on negative controls
        assert out_a == "FALSE_POSITIVE_MUTATION_ON_NEGATIVE_CONTROL"

        # Run Strategy C (Adaptive Planner: identifies benign blip)
        act_c, _, _, _ = StrategyExecutor.run_strategy_c_adaptive_planner(fix)
        corr_c, out_c = oracle.adjudicate(fix.hidden_label, act_c, abstained=act_c is None)
        assert corr_c is True  # Strategy C passes 100%
        assert out_c == "CORRECT_NO_OP_ON_NEGATIVE_CONTROL"


@pytest.mark.asyncio
async def test_adversarial_5_concurrent_approve_and_reject_serialization(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Drill 5: Two operators concurrently race to Approve and Reject the same pending approval.
    PostgreSQL row-level locking (SELECT ... FOR UPDATE) must strictly serialize the actions:
    exactly one transition succeeds, while the other fails with InvalidApprovalTransitionError.
    """
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"race-app-{uuid.uuid4()}",
        target_id="k8s:pod:race-svc",
        request_sha256="7" * 64,
    )
    inc_id = admit.response.id
    service = ApprovalService(session_factory)

    req = ApprovalCreateRequest(
        plan_id="plan-race",
        canonical_plan_digest="8" * 64,
        action_digest="9" * 64,
        target_id="k8s:pod:race-svc",
        expected_resource_version="v1",
    )
    app = await service.create_approval_request(isolated_tenant, inc_id, req)

    # Barrier synchronization
    barrier = asyncio.Barrier(2)

    async def op_approve() -> tuple[bool, str]:
        await barrier.wait()
        try:
            res = await service.approve(
                isolated_tenant,
                app.id,
                ApprovalDecisionRequest(
                    reviewer_id="commander-alice",
                    reviewer_role="incident_commander",
                    reason="I approve",
                ),
            )
            return True, res.state.value
        except ApprovalError as exc:
            return False, str(exc)

    async def op_reject() -> tuple[bool, str]:
        await barrier.wait()
        try:
            res = await service.reject(
                isolated_tenant,
                app.id,
                ApprovalDecisionRequest(
                    reviewer_id="commander-bob",
                    reviewer_role="incident_commander",
                    reason="I reject",
                ),
            )
            return True, res.state.value
        except ApprovalError as exc:
            return False, str(exc)

    res_approve, res_reject = await asyncio.gather(op_approve(), op_reject())

    # Exactly one must succeed, exactly one must fail with conflict / illegal transition
    success_count = sum([res_approve[0], res_reject[0]])
    assert success_count == 1

    winner = "APPROVED" if res_approve[0] else "REJECTED"
    loser_err = res_reject[1] if res_approve[0] else res_approve[1]
    assert "illegal approval transition" in loser_err.lower()

    # Final DB state must match the winner
    final_app = await service.get_approval(isolated_tenant, app.id)
    assert final_app.state.value == winner
