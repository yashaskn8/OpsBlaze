"""Unit and API integration tests for OpsBlaze Feature P3 — Evidence-to-Effect Trace Explorer."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.approval import ApprovalState
from backend.domain.trace import TraceNodeKind, TraceRelation
from backend.persistence.models import (
    ApprovalRequest,
    CapabilityGrant,
    EffectReceipt,
    EvidenceLedger,
    InvestigationStep,
    ToolCall,
    ToolCatalog,
)
from backend.persistence.repositories import IncidentRepository


@pytest.mark.asyncio
async def test_p3_trace_graph_dag_projection_and_acyclicity(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    api_client: AsyncClient,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"p3-trace-{uuid.uuid4()}",
        target_id="k8s:pod:cart-svc",
        request_sha256="4" * 64,
    )
    inc_id = admit.response.id
    headers = {"X-Tenant-ID": str(isolated_tenant)}

    tool_id = f"tool_{uuid.uuid4().hex[:10]}"
    tc_id = uuid.uuid4()
    ev_id = uuid.uuid4()
    step1_id = uuid.uuid4()
    step2_id = uuid.uuid4()
    app_id = uuid.uuid4()
    receipt_id = uuid.uuid4()

    async with session_factory() as session:
        # Tool Catalog & Grant
        cat = ToolCatalog(
            id=tool_id,
            version="v1",
            input_schema_hash="1" * 64,
            output_schema_hash="2" * 64,
            read_only=True,
            description="Diagnostic query",
        )
        session.add(cat)
        await session.flush()

        grant = CapabilityGrant(
            id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            tool_id=tool_id,
            tool_version="v1",
            target_id="k8s:pod:cart-svc",
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
            token_count=800,
            hypotheses_json={},
        )
        session.add(s1)
        await session.flush()

        # Tool Call in Step 1
        tc = ToolCall(
            id=tc_id,
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            step_no=1,
            grant_id=grant.id,
            tool_id=tool_id,
            tool_version="v1",
            target_id="k8s:pod:cart-svc",
            status="COMPLETED",
            args_digest="a" * 64,
            args_payload={"auth_token": "secret_token_12345"},
            output_digest="b" * 64,
            duration_ms=30.0,
        )
        session.add(tc)
        await session.flush()

        # Evidence produced by Tool Call
        ev = EvidenceLedger(
            id=ev_id,
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            tool_call_id=tc_id,
            source=tool_id,
            source_version="v1",
            target_id="k8s:pod:cart-svc",
            target_version="v1",
            observed_at=datetime.now(UTC),
            sha256_digest="e" * 64,
            raw_payload_redacted={"jwt_secret": "my-secret-key", "finding": "corrupt_cart_state"},
            classification="OBSERVATION",
            validity_status="VALID",
        )
        session.add(ev)
        await session.flush()

        # Step 2 using Evidence and proposing a plan
        s2 = InvestigationStep(
            id=step2_id,
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            step_no=2,
            loop_state="PLAN_PROPOSED",
            prompt_digest="p2" * 32,
            token_count=1500,
            evidence_ids_used=[str(ev_id)],
            hypotheses_json={},
            proposed_plan_json={"plan_id": "plan-cart-evict", "action": "evict_pod"},
        )
        session.add(s2)
        await session.flush()

        # Approval Request for Plan
        approval = ApprovalRequest(
            id=app_id,
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            step_id=step2_id,
            plan_id="plan-cart-evict",
            canonical_plan_digest="d" * 64,
            action_digest="c" * 64,
            action_payload={"api_key": "sensitive_api_key_xyz"},
            target_id="k8s:pod:cart-svc",
            expected_resource_version="v1",
            policy_version="v1",
            state=ApprovalState.APPROVED.value,
            reviewer_id="commander-bob",
            reviewer_role="incident_commander",
            expires_at=datetime.now(UTC),
        )
        session.add(approval)
        await session.flush()

        # Effect Receipt
        rec = EffectReceipt(
            id=receipt_id,
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            plan_digest="d" * 64,
            action_digest="c" * 64,
            target_id="k8s:pod:cart-svc",
            adapter_id="k8s-adapter",
            fence=1,
            state="VERIFIED",
            idempotency_key="rec-key-cart-1",
            expected_version="v1",
            observed_version="v2",
        )
        session.add(rec)
        await session.commit()

    # Query Trace API
    resp = await api_client.get(f"/v1/incidents/{inc_id}/trace", headers=headers)
    assert resp.status_code == 200, resp.text
    graph = resp.json()

    assert graph["root_node_id"] == str(inc_id)
    assert graph["has_cycles"] is False

    node_kinds = {n["kind"] for n in graph["nodes"]}
    assert TraceNodeKind.INCIDENT.value in node_kinds
    assert TraceNodeKind.STEP.value in node_kinds
    assert TraceNodeKind.TOOL_CALL.value in node_kinds
    assert TraceNodeKind.EVIDENCE.value in node_kinds
    assert TraceNodeKind.PLAN.value in node_kinds
    assert TraceNodeKind.APPROVAL.value in node_kinds
    assert TraceNodeKind.EFFECT_RECEIPT.value in node_kinds

    # Verify edge relations
    relations = {e["relation"] for e in graph["edges"]}
    assert TraceRelation.INITIATED.value in relations
    assert TraceRelation.INVOKED.value in relations
    assert TraceRelation.PRODUCED.value in relations
    assert TraceRelation.NEXT_STEP.value in relations
    assert TraceRelation.USED_IN.value in relations
    assert TraceRelation.PROPOSED.value in relations
    assert TraceRelation.REQUESTED_APPROVAL.value in relations
    assert TraceRelation.AUTHORIZED_EFFECT.value in relations

    # Verify Secret Redaction in all nodes
    for node in graph["nodes"]:
        meta_str = str(node.get("metadata", {}))
        assert "secret_token_12345" not in meta_str
        assert "my-secret-key" not in meta_str
        assert "sensitive_api_key_xyz" not in meta_str


@pytest.mark.asyncio
async def test_p3_trace_graph_missing_link_annotations(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    api_client: AsyncClient,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"p3-missing-{uuid.uuid4()}",
        target_id="k8s:pod:isolated-db",
        request_sha256="5" * 64,
    )
    inc_id = admit.response.id
    headers = {"X-Tenant-ID": str(isolated_tenant)}

    # Add an unapproved EffectReceipt directly into the target
    async with session_factory() as session:
        rec = EffectReceipt(
            id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            plan_digest="9" * 64,
            action_digest="8" * 64,
            target_id="k8s:pod:isolated-db",
            adapter_id="k8s-adapter",
            fence=99,
            state="CONFIRMED",
            idempotency_key="unapproved-receipt-1",
            expected_version="v1",
            observed_version="v2",
        )
        session.add(rec)
        await session.commit()

    resp = await api_client.get(f"/v1/incidents/{inc_id}/trace", headers=headers)
    assert resp.status_code == 200
    graph = resp.json()
    assert graph["has_cycles"] is False
    assert len(graph["missing_links"]) >= 1
    assert any("has no matching approval request" in ml for ml in graph["missing_links"])

    receipt_node = next(n for n in graph["nodes"] if n["kind"] == "EFFECT_RECEIPT")
    assert receipt_node["warning"] is not None
    assert "Unapproved" in receipt_node["warning"]


@pytest.mark.asyncio
async def test_p3_trace_graph_missing_incident_returns_404(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
) -> None:
    fake_id = uuid.uuid4()
    resp = await api_client.get(
        f"/v1/incidents/{fake_id}/trace",
        headers={"X-Tenant-ID": str(isolated_tenant)},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_p3_trace_graph_cross_incident_receipt_isolation_and_digest_matching(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    inc_repo = IncidentRepository(session_factory)

    # 1. Admit Incident A
    admit_a = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"trace-a-{uuid.uuid4()}",
        target_id="k8s:pod:shared-target",
        request_sha256="a" * 64,
    )
    inc_a_id = admit_a.response.id

    # 2. Admit Incident B on the same target
    admit_b = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"trace-b-{uuid.uuid4()}",
        target_id="k8s:pod:shared-target",
        request_sha256="b" * 64,
    )
    inc_b_id = admit_b.response.id

    app_a_id = uuid.uuid4()
    app_b_id = uuid.uuid4()
    rec_a_id = uuid.uuid4()
    rec_b_id = uuid.uuid4()

    async with session_factory() as session:
        # Approvals for A and B
        session.add(
            ApprovalRequest(
                id=app_a_id,
                tenant_id=isolated_tenant,
                incident_id=inc_a_id,
                plan_id="plan-a",
                canonical_plan_digest="a" * 64,
                action_digest="action-a" + "0" * 56,
                action_payload={"cmd": "restart"},
                target_id="k8s:pod:shared-target",
                expected_resource_version="v1",
                policy_version="v1",
                state=ApprovalState.CONSUMED.value,
                reviewer_id="lead-a",
                reviewer_role="platform_engineer",
                expires_at=datetime.now(UTC),
            )
        )
        session.add(
            ApprovalRequest(
                id=app_b_id,
                tenant_id=isolated_tenant,
                incident_id=inc_b_id,
                plan_id="plan-b",
                canonical_plan_digest="b" * 64,
                action_digest="action-b" + "0" * 56,
                action_payload={"cmd": "scale"},
                target_id="k8s:pod:shared-target",
                expected_resource_version="v1",
                policy_version="v1",
                state=ApprovalState.CONSUMED.value,
                reviewer_id="lead-b",
                reviewer_role="platform_engineer",
                expires_at=datetime.now(UTC),
            )
        )
        # Receipts for A and B
        session.add(
            EffectReceipt(
                id=rec_a_id,
                tenant_id=isolated_tenant,
                incident_id=inc_a_id,
                plan_digest="a" * 64,
                action_digest="action-a" + "0" * 56,
                target_id="k8s:pod:shared-target",
                adapter_id="k8s-adapter",
                fence=1,
                state="VERIFIED",
                idempotency_key=f"rec-key-a-{uuid.uuid4().hex[:6]}",
                expected_version="v1",
                observed_version="v2",
            )
        )
        session.add(
            EffectReceipt(
                id=rec_b_id,
                tenant_id=isolated_tenant,
                incident_id=inc_b_id,
                plan_digest="b" * 64,
                action_digest="action-b" + "0" * 56,
                target_id="k8s:pod:shared-target",
                adapter_id="k8s-adapter",
                fence=2,
                state="VERIFIED",
                idempotency_key=f"rec-key-b-{uuid.uuid4().hex[:6]}",
                expected_version="v1",
                observed_version="v2",
            )
        )
        await session.commit()

    # Query Incident B trace
    headers = {"X-Tenant-ID": str(isolated_tenant)}
    resp = await api_client.get(f"/v1/incidents/{inc_b_id}/trace", headers=headers)
    assert resp.status_code == 200
    graph_b = resp.json()

    node_ids = {n["id"] for n in graph_b["nodes"]}
    # Receipt B MUST be in Graph B, Receipt A MUST NOT be in Graph B
    assert str(rec_b_id) in node_ids, "Receipt B should be in Incident B trace"
    assert str(rec_a_id) not in node_ids, "Receipt A from prior incident must NOT leak into Incident B trace"

    # Edge MUST connect app_b to rec_b
    auth_edges = [
        e for e in graph_b["edges"]
        if e["relation"] == TraceRelation.AUTHORIZED_EFFECT.value
    ]
    assert len(auth_edges) == 1
    assert auth_edges[0]["source"] == str(app_b_id)
    assert auth_edges[0]["target"] == str(rec_b_id)
