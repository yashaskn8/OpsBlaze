"""Stage 6 End-to-End Control Plane Integration Tests.

Integrates:
- P2 Version-Bound Approval Cockpit
- P1 Live Investigation Workbench & SSE
- P3 Evidence-to-Effect Trace Explorer
- B3 Effect Receipt Consumption & Monotonic Fencing
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.approvals.service import ApprovalService
from backend.config import get_settings
from backend.domain.approval import ApprovalState
from backend.domain.auth import mint_principal_token
from backend.persistence.models import (
    CapabilityGrant,
    EffectReceipt,
    EvidenceLedger,
    InvestigationStep,
    ToolCall,
    ToolCatalog,
)


@pytest.mark.asyncio
async def test_stage6_full_operator_control_plane_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    api_client: AsyncClient,
) -> None:
    token = mint_principal_token(
        actor_id="commander-sarah",
        tenant_id=isolated_tenant,
        roles=["incident_commander", "operator"],
        secret_key=get_settings().auth_secret_key or "",
        ttl_seconds=300,
        auth_mechanism="pytest_stage6_control_plane",
    )
    headers = {
        "X-Tenant-ID": str(isolated_tenant),
        "Authorization": f"Bearer {token}",
    }
    approval_service = ApprovalService(session_factory)

    target_id = f"k8s:pod:payment-processor-{uuid.uuid4().hex[:6]}"
    plan_id = "plan-restart-and-scale"
    plan_digest = "a" * 64
    action_digest = "b" * 64

    # -------------------------------------------------------------
    # 1. Admit Incident (B1 Foundation)
    # -------------------------------------------------------------
    admit_resp = await api_client.post(
        "/v1/incidents",
        json={"target_id": target_id, "title": "CrashLoopBackOff on payment processor"},
        headers={"X-Tenant-ID": str(isolated_tenant), "Idempotency-Key": f"key-{uuid.uuid4()}"},
    )
    assert admit_resp.status_code == 202, admit_resp.text
    inc_id = uuid.UUID(admit_resp.json()["id"])

    # -------------------------------------------------------------
    # 2. Advance through Investigation Steps & Capture Evidence (A1/A2/A3)
    # -------------------------------------------------------------
    tool_id = f"tool_{uuid.uuid4().hex[:8]}"
    tc_id = uuid.uuid4()
    ev_id = uuid.uuid4()
    step1_id = uuid.uuid4()
    step2_id = uuid.uuid4()

    async with session_factory() as session:
        # Tool catalog and grant
        cat = ToolCatalog(
            id=tool_id,
            version="v1",
            input_schema_hash="1" * 64,
            output_schema_hash="2" * 64,
            read_only=True,
            description="Diagnostic probe",
        )
        session.add(cat)
        await session.flush()

        grant = CapabilityGrant(
            id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            tool_id=tool_id,
            tool_version="v1",
            target_id=target_id,
            expires_at=datetime.now(UTC),
        )
        session.add(grant)
        await session.flush()

        # Step 1
        s1 = InvestigationStep(
            id=step1_id,
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            step_no=1,
            loop_state="PROBING",
            prompt_digest="p1" * 32,
            token_count=1100,
            hypotheses_json={},
        )
        session.add(s1)
        await session.flush()

        # Tool Call
        tc = ToolCall(
            id=tc_id,
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            step_no=1,
            grant_id=grant.id,
            tool_id=tool_id,
            tool_version="v1",
            target_id=target_id,
            status="COMPLETED",
            args_digest="3" * 64,
            args_payload={"lines": 100},
            output_digest="4" * 64,
            duration_ms=52.3,
        )
        session.add(tc)
        await session.flush()

        # Evidence
        ev = EvidenceLedger(
            id=ev_id,
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            tool_call_id=tc_id,
            source=tool_id,
            source_version="v1",
            target_id=target_id,
            target_version="v1",
            observed_at=datetime.now(UTC),
            sha256_digest="5" * 64,
            raw_payload_redacted={"stacktrace": "NullPointerException in payment handler"},
            classification="OBSERVATION",
            validity_status="VALID",
        )
        session.add(ev)
        await session.flush()

        # Step 2 proposing recovery plan
        s2 = InvestigationStep(
            id=step2_id,
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            step_no=2,
            loop_state="PLAN_PROPOSED",
            prompt_digest="p2" * 32,
            token_count=1800,
            evidence_ids_used=[str(ev_id)],
            hypotheses_json={
                "hypotheses": [
                    {
                        "id": "hyp-payment-crash",
                        "title": "Payment processor JVM memory exhaustion",
                        "confidence": 0.94,
                        "status": "CONFIRMED",
                        "rationale": "Memory dumps and heap traces match OOM kill",
                        "rank": 1,
                    }
                ]
            },
            proposed_plan_json={
                "plan_id": plan_id,
                "action": "restart_and_scale",
                "replicas": 4,
            },
        )
        session.add(s2)
        await session.commit()

    # -------------------------------------------------------------
    # 3. Verify Live Workbench Snapshot & ETag Validation (P1)
    # -------------------------------------------------------------
    wb_resp = await api_client.get(f"/v1/incidents/{inc_id}/workbench", headers=headers)
    assert wb_resp.status_code == 200, wb_resp.text
    etag_1 = wb_resp.headers["ETag"]
    wb_data = wb_resp.json()
    assert len(wb_data["hypotheses"]) == 1
    assert wb_data["hypotheses"][0]["confidence"] == 0.94
    assert len(wb_data["evidence"]) == 1
    assert len(wb_data["steps"]) == 2
    assert wb_data["active_approval"] is None

    # 304 Validation
    resp_304 = await api_client.get(
        f"/v1/incidents/{inc_id}/workbench", headers={**headers, "If-None-Match": etag_1}
    )
    assert resp_304.status_code == 304

    # -------------------------------------------------------------
    # 4. Create Version-Bound Approval Request (P2)
    # -------------------------------------------------------------
    create_app_resp = await api_client.post(
        f"/v1/plans/{plan_id}/approvals?incident_id={inc_id}",
        json={
            "plan_id": plan_id,
            "canonical_plan_digest": plan_digest,
            "action_digest": action_digest,
            "action_payload": {"restart": True, "scale": 4},
            "target_id": target_id,
            "expected_resource_version": "v1",
            "policy_version": "v1",
            "step_id": str(step2_id),
            "ttl_seconds": 900,
        },
        headers=headers,
    )
    assert create_app_resp.status_code == 201, create_app_resp.text
    approval_id = uuid.UUID(create_app_resp.json()["id"])

    # Workbench snapshot ETag must now change and show active pending approval
    wb_resp_2 = await api_client.get(f"/v1/incidents/{inc_id}/workbench", headers=headers)
    assert wb_resp_2.status_code == 200
    assert wb_resp_2.headers["ETag"] != etag_1
    assert wb_resp_2.json()["active_approval"]["id"] == str(approval_id)
    assert wb_resp_2.json()["active_approval"]["state"] == "PENDING"

    # -------------------------------------------------------------
    # 5. Human Approval Decision (P2 Cockpit)
    # -------------------------------------------------------------
    approve_resp = await api_client.post(
        f"/v1/approvals/{approval_id}/approve",
        json={
            "reviewer_id": "commander-sarah",
            "reviewer_role": "incident_commander",
            "reason": "Verified hypothesis and confirmed headroom for 4 replicas",
        },
        headers=headers,
    )
    assert approve_resp.status_code == 200, approve_resp.text
    assert approve_resp.json()["state"] == "APPROVED"
    assert approve_resp.json()["reviewer_id"] == "commander-sarah"

    # -------------------------------------------------------------
    # 6. Two-Phase Dispatch Lease Claiming & Execution (P2 -> B3)
    # -------------------------------------------------------------
    lease_id, ctx = await approval_service.claim_dispatch_lease(
        tenant_id=isolated_tenant,
        approval_id=approval_id,
        target_version="v1",
        plan_digest=plan_digest,
        action_digest=action_digest,
        lease_seconds=60,
    )
    assert lease_id is not None
    assert ctx.approval_id == approval_id

    # Simulate effect execution inserting EffectReceipt in target
    receipt_id = uuid.uuid4()
    async with session_factory() as session:
        rec = EffectReceipt(
            id=receipt_id,
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            plan_digest=plan_digest,
            action_digest=action_digest,
            target_id=target_id,
            adapter_id="k8s-adapter",
            fence=1,
            state="VERIFIED",
            idempotency_key=f"rec-{uuid.uuid4()}",
            expected_version="v1",
            observed_version="v2",
        )
        session.add(rec)
        await session.commit()

    # Finalize approval consumption
    await approval_service.finalize_consumed(
        tenant_id=isolated_tenant,
        approval_id=approval_id,
        lease_id=lease_id,
    )
    final_app = await approval_service.get_approval(isolated_tenant, approval_id)
    assert final_app.state == ApprovalState.CONSUMED

    # -------------------------------------------------------------
    # 7. Trace Explorer Projection (P3)
    # -------------------------------------------------------------
    trace_resp = await api_client.get(f"/v1/incidents/{inc_id}/trace", headers=headers)
    assert trace_resp.status_code == 200, trace_resp.text
    trace_data = trace_resp.json()

    assert trace_data["root_node_id"] == str(inc_id)
    assert trace_data["has_cycles"] is False

    node_kinds = [n["kind"] for n in trace_data["nodes"]]
    assert "INCIDENT" in node_kinds
    assert "STEP" in node_kinds
    assert "TOOL_CALL" in node_kinds
    assert "EVIDENCE" in node_kinds
    assert "PLAN" in node_kinds
    assert "APPROVAL" in node_kinds
    assert "EFFECT_RECEIPT" in node_kinds

    # Verify complete causal link from plan -> approval -> effect receipt
    edge_map = {(e["source"], e["target"]): e["relation"] for e in trace_data["edges"]}
    assert (f"plan:{step2_id}", str(approval_id)) in edge_map
    assert edge_map[(f"plan:{step2_id}", str(approval_id))] == "REQUESTED_APPROVAL"
    assert (str(approval_id), str(receipt_id)) in edge_map
    assert edge_map[(str(approval_id), str(receipt_id))] == "AUTHORIZED_EFFECT"
