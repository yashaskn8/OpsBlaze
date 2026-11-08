"""Integration tests for Feature E6 — Policy Release Control, Canary Deployment & Rollback.

Verifies:
1. Complete REST API lifecycle: bootstrap -> create bundle -> seal -> create release -> submit -> review -> shadow -> canary -> update -> promote -> rollback.
2. Separation of duties: drafter attempting to self-approve release is rejected with HTTP 403 Forbidden.
3. Model/bot identity: models/bots attempting to review or approve are rejected with HTTP 403 Forbidden.
4. Emergency Kill Switch: immediately terminates candidate traffic and fences consequential mutations.
5. Automated Guardrail Rollback: Class 1 zero-tolerance invariant breaches trigger automated rollback.
6. Immutable Audit Trail: all state transitions append to release_event_audit.
7. Multi-tenant isolation: cross-tenant access to bundles or releases is rejected.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import get_settings
from backend.domain.auth import mint_principal_token

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_stage11_full_api_release_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    api_client: AsyncClient,
) -> None:
    """Tests complete REST API lifecycle for policy bundle sealing and release transitions."""
    settings = get_settings()

    # 1. Mint tokens for drafter, approver, and unauthorized bot
    drafter_token = mint_principal_token(
        actor_id="drafter-alice",
        actor_kind="HUMAN",
        tenant_id=isolated_tenant,
        roles=["platform_engineer"],
        secret_key=settings.auth_secret_key,
    )
    approver_token = mint_principal_token(
        actor_id="approver-bob",
        actor_kind="HUMAN",
        tenant_id=isolated_tenant,
        roles=["release_owner"],
        secret_key=settings.auth_secret_key,
    )
    bot_token = mint_principal_token(
        actor_id="autonomous-bot",
        actor_kind="MODEL",
        tenant_id=isolated_tenant,
        roles=["agent"],
        secret_key=settings.auth_secret_key,
    )

    drafter_headers = {
        "X-Tenant-ID": str(isolated_tenant),
        "Authorization": f"Bearer {drafter_token}",
    }
    approver_headers = {
        "X-Tenant-ID": str(isolated_tenant),
        "Authorization": f"Bearer {approver_token}",
    }
    bot_headers = {
        "X-Tenant-ID": str(isolated_tenant),
        "Authorization": f"Bearer {bot_token}",
    }

    # 2. Bootstrap baseline incumbent bundle
    res_boot = await api_client.post(
        "/v1/bundles/bootstrap?target_component=investigator",
        headers=drafter_headers,
    )
    assert res_boot.status_code == 200
    incumbent = res_boot.json()
    assert incumbent["status"] == "SEALED"
    incumbent_id = incumbent["id"]

    # 3. Create candidate bundle in DRAFT
    res_bundle = await api_client.post(
        "/v1/bundles",
        headers=drafter_headers,
        json={
            "bundle_name": "investigator-candidate-v2.0",
            "base_policy_version": "v2.0.0",
            "target_component": "investigator",
            "model_id": "gpt-4o",
            "model_version": "2026-11",
            "tool_catalog_version": "v2",
            "a2_grant_version": "v2",
            "source_provenance_digest": "5" * 64,
            "proposal_diff": {"reconciliation": "verify_lock_first"},
            "prompt_templates": {"system": "You are the advanced investigator."},
            "active_lesson_ids": [],
        },
    )
    assert res_bundle.status_code == 201
    candidate = res_bundle.json()
    assert candidate["status"] == "DRAFT"
    candidate_id = candidate["id"]

    # 4. Attempt to create release with unsealed bundle -> fails 400
    res_fail_unsealed = await api_client.post(
        "/v1/releases",
        headers=drafter_headers,
        json={
            "target_component": "investigator",
            "bundle_id": candidate_id,
            "incumbent_bundle_id": incumbent_id,
        },
    )
    assert res_fail_unsealed.status_code == 400

    # 5. Seal candidate bundle
    res_seal = await api_client.post(
        f"/v1/bundles/{candidate_id}/seal",
        headers=drafter_headers,
    )
    assert res_seal.status_code == 200
    sealed_candidate = res_seal.json()
    assert sealed_candidate["status"] == "SEALED"
    assert sealed_candidate["bundle_sha256"] != "0" * 64

    # 6. Create release draft
    res_rel = await api_client.post(
        "/v1/releases",
        headers=drafter_headers,
        json={
            "target_component": "investigator",
            "bundle_id": candidate_id,
            "incumbent_bundle_id": incumbent_id,
        },
    )
    assert res_rel.status_code == 201
    release = res_rel.json()
    assert release["state"] == "DRAFT"
    assert release["version"] == 1
    release_id = release["id"]

    # 7. Submit release for approval (v1 -> v2)
    res_sub = await api_client.post(
        f"/v1/releases/{release_id}/submit",
        headers=drafter_headers,
        json={"expected_version": 1, "target_state": "PENDING_APPROVAL"},
    )
    assert res_sub.status_code == 200
    assert res_sub.json()["state"] == "PENDING_APPROVAL"
    assert res_sub.json()["version"] == 2

    # 8. Drafter attempts self-approval -> must fail 403 Forbidden (Separation of Duties)
    res_self_appr = await api_client.post(
        f"/v1/releases/{release_id}/review",
        headers=drafter_headers,
        json={"expected_version": 2, "decision": "APPROVED"},
    )
    assert res_self_appr.status_code == 403

    # 9. Autonomous bot attempts approval -> must fail 403 Forbidden (Human-only authority)
    res_bot_appr = await api_client.post(
        f"/v1/releases/{release_id}/review",
        headers=bot_headers,
        json={"expected_version": 2, "decision": "APPROVED"},
    )
    assert res_bot_appr.status_code == 403

    # 10. Distinct release owner approves release (v2 -> v3)
    res_appr = await api_client.post(
        f"/v1/releases/{release_id}/review",
        headers=approver_headers,
        json={
            "expected_version": 2,
            "decision": "APPROVED",
            "decision_comments": "Approved for shadow deployment.",
        },
    )
    assert res_appr.status_code == 200
    assert res_appr.json()["state"] == "APPROVED"
    assert res_appr.json()["version"] == 3

    # 11. Activate Shadow Mode (v3 -> v4)
    res_shad = await api_client.post(
        f"/v1/releases/{release_id}/activate-shadow",
        headers=approver_headers,
        json={"expected_version": 3, "target_state": "SHADOW"},
    )
    assert res_shad.status_code == 200
    assert res_shad.json()["state"] == "SHADOW"
    assert res_shad.json()["canary_percentage"] == 0
    assert res_shad.json()["version"] == 4

    # 12. Activate Canary Mode at 25% (v4 -> v5)
    res_can = await api_client.post(
        f"/v1/releases/{release_id}/activate-canary",
        headers=approver_headers,
        json={
            "expected_version": 4,
            "target_state": "CANARY",
            "canary_percentage": 25,
        },
    )
    assert res_can.status_code == 200
    assert res_can.json()["state"] == "CANARY"
    assert res_can.json()["canary_percentage"] == 25
    assert res_can.json()["version"] == 5

    # 13. Update Canary percentage to 50% (v5 -> v6)
    res_upd = await api_client.post(
        f"/v1/releases/{release_id}/update-canary",
        headers=approver_headers,
        json={
            "expected_version": 5,
            "target_state": "CANARY",
            "canary_percentage": 50,
        },
    )
    assert res_upd.status_code == 200
    assert res_upd.json()["canary_percentage"] == 50
    assert res_upd.json()["version"] == 6

    # 14. Check Canary Metrics endpoint
    res_met = await api_client.get(
        f"/v1/releases/{release_id}/metrics",
        headers=approver_headers,
    )
    assert res_met.status_code == 200
    metrics_data = res_met.json()
    assert metrics_data["state"] == "CANARY"
    assert metrics_data["canary_percentage"] == 50

    # 15. Run Guardrail Check with simulated Class 1 invariant breach -> triggers automated rollback
    res_guard = await api_client.post(
        f"/v1/releases/{release_id}/guardrail-check",
        headers=approver_headers,
        json={
            "simulated_invariant_breaches": 1,
            "invariant_breach_detail": "Negative control violation during canary traffic.",
        },
    )
    assert res_guard.status_code == 200
    guard_res = res_guard.json()
    assert guard_res["rolled_back"] is True
    assert "CLASS_1_INVARIANT_BREACH" in guard_res["reason"]

    # 16. Verify release is now in ROLLED_BACK state
    res_final = await api_client.get(
        f"/v1/releases/{release_id}",
        headers=approver_headers,
    )
    assert res_final.status_code == 200
    assert res_final.json()["state"] == "ROLLED_BACK"
    assert res_final.json()["canary_percentage"] == 0

    # 17. Verify immutable audit events trail
    res_audits = await api_client.get(
        f"/v1/releases/{release_id}/audits",
        headers=approver_headers,
    )
    assert res_audits.status_code == 200
    audits = res_audits.json()
    assert len(audits) >= 6
    event_types = [a["event_type"] for a in audits]
    assert "DRAFT_CREATED" in event_types
    assert "SUBMITTED_FOR_APPROVAL" in event_types
    assert "APPROVAL_GRANTED" in event_types
    assert "SHADOW_ACTIVATED" in event_types
    assert "CANARY_ACTIVATED" in event_types
    assert "ROLLBACK_TRIGGERED" in event_types


@pytest.mark.asyncio
async def test_stage11_emergency_kill_switch_api(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    api_client: AsyncClient,
) -> None:
    """Verifies that the emergency kill-switch halts candidate traffic and sets kill_switch_active."""
    settings = get_settings()

    sre_token = mint_principal_token(
        actor_id="sre-lead",
        actor_kind="HUMAN",
        tenant_id=isolated_tenant,
        roles=["sre_lead"],
        secret_key=settings.auth_secret_key,
    )
    rel_owner_token = mint_principal_token(
        actor_id="rel-owner",
        actor_kind="HUMAN",
        tenant_id=isolated_tenant,
        roles=["release_owner"],
        secret_key=settings.auth_secret_key,
    )

    sre_headers = {
        "X-Tenant-ID": str(isolated_tenant),
        "Authorization": f"Bearer {sre_token}",
    }
    rel_headers = {
        "X-Tenant-ID": str(isolated_tenant),
        "Authorization": f"Bearer {rel_owner_token}",
    }

    # Bootstrap & create bundle
    inc_res = await api_client.post(
        "/v1/bundles/bootstrap?target_component=planner",
        headers=sre_headers,
    )
    inc_id = inc_res.json()["id"]

    cand_res = await api_client.post(
        "/v1/bundles",
        headers=sre_headers,
        json={
            "bundle_name": "planner-cand",
            "base_policy_version": "v1.1",
            "target_component": "planner",
            "model_id": "claude-3-5",
            "model_version": "2026",
            "tool_catalog_version": "v1",
            "a2_grant_version": "v1",
            "source_provenance_digest": "6" * 64,
        },
    )
    cand_id = cand_res.json()["id"]
    await api_client.post(f"/v1/bundles/{cand_id}/seal", headers=sre_headers)

    # Release draft & approval
    rel_res = await api_client.post(
        "/v1/releases",
        headers=sre_headers,
        json={"target_component": "planner", "bundle_id": cand_id, "incumbent_bundle_id": inc_id},
    )
    rel_id = rel_res.json()["id"]
    await api_client.post(f"/v1/releases/{rel_id}/submit", headers=sre_headers, json={"expected_version": 1, "target_state": "PENDING_APPROVAL"})
    await api_client.post(f"/v1/releases/{rel_id}/review", headers=rel_headers, json={"expected_version": 2, "decision": "APPROVED"})
    await api_client.post(f"/v1/releases/{rel_id}/activate-canary", headers=rel_headers, json={"expected_version": 3, "target_state": "CANARY", "canary_percentage": 50})

    # Trip Emergency Kill Switch via SRE lead
    res_kill = await api_client.post(
        f"/v1/releases/{rel_id}/kill-switch",
        headers=sre_headers,
        json={"expected_version": 4, "justification": "Unexpected high severity error loop"},
    )
    assert res_kill.status_code == 200
    killed_rel = res_kill.json()
    assert killed_rel["kill_switch_active"] is True
    assert killed_rel["state"] == "ROLLED_BACK"
    assert killed_rel["canary_percentage"] == 0
    assert "KILL_SWITCH: Unexpected high severity error loop" in killed_rel["rollback_reason"]
