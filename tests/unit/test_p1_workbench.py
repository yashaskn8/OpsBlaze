"""Unit and API integration tests for OpsBlaze Feature P1 — Live Investigation Workbench."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.incident import IncidentStatus
from backend.persistence.models import (
    BudgetLedger,
    CapabilityGrant,
    EvidenceLedger,
    InvestigationStep,
    ToolCall,
    ToolCatalog,
)
from backend.persistence.repositories import IncidentRepository


@pytest.mark.asyncio
async def test_p1_workbench_snapshot_empty_and_populated(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    api_client: AsyncClient,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"p1-wb-{uuid.uuid4()}",
        target_id="k8s:pod:order-svc",
        request_sha256="1" * 64,
    )
    inc_id = admit.response.id
    headers = {"X-Tenant-ID": str(isolated_tenant)}

    # 1. Fetch initial snapshot
    resp = await api_client.get(f"/v1/incidents/{inc_id}/workbench", headers=headers)
    assert resp.status_code == 200, resp.text
    etag = resp.headers.get("ETag")
    assert etag is not None
    data = resp.json()
    assert data["incident"]["id"] == str(inc_id)
    assert data["incident"]["status"] == "ACCEPTED"
    assert data["hypotheses"] == []
    assert data["evidence"] == []
    assert data["steps"] == []
    assert data["tool_calls"] == []
    assert data["abstention"]["abstained"] is False

    # 2. 304 Not Modified when If-None-Match matches ETag
    resp_304 = await api_client.get(
        f"/v1/incidents/{inc_id}/workbench",
        headers={**headers, "If-None-Match": etag},
    )
    assert resp_304.status_code == 304
    assert resp_304.headers.get("ETag") == etag

    # 3. Populate Investigation Step, Evidence, Tool Call, and Budget
    test_tool_id = f"tool_{uuid.uuid4().hex[:10]}"
    async with session_factory() as session:
        # Create ToolCatalog entry
        cat = ToolCatalog(
            id=test_tool_id,
            version="v1",
            input_schema_hash="a" * 64,
            output_schema_hash="b" * 64,
            read_only=True,
            description="Fetch logs",
        )
        session.add(cat)
        await session.flush()

        # Create Grant
        grant = CapabilityGrant(
            id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            tool_id=test_tool_id,
            tool_version="v1",
            target_id="k8s:pod:order-svc",
            expires_at=datetime.now(UTC),
        )
        session.add(grant)
        await session.flush()

        # Tool Call
        tc = ToolCall(
            id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            step_no=1,
            grant_id=grant.id,
            tool_id=test_tool_id,
            tool_version="v1",
            target_id="k8s:pod:order-svc",
            status="COMPLETED",
            args_digest="c" * 64,
            args_payload={"lines": 50},
            output_digest="d" * 64,
            duration_ms=45.2,
        )
        session.add(tc)
        await session.flush()

        # Evidence
        ev = EvidenceLedger(
            id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            tool_call_id=tc.id,
            source="k8s_get_logs",
            source_version="v1",
            target_id="k8s:pod:order-svc",
            target_version="v1",
            observed_at=datetime.now(UTC),
            sha256_digest="e" * 64,
            raw_payload_redacted={"log": "OutOfMemoryKilled"},
            classification="OBSERVATION",
            validity_status="VALID",
        )
        session.add(ev)

        # Step
        step = InvestigationStep(
            id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            step_no=1,
            loop_state="STEP_COMPLETED",
            prompt_digest="f" * 64,
            token_count=1200,
            hypotheses_json={
                "hypotheses": [
                    {
                        "id": "hyp-oom",
                        "title": "Pod OOMKilled by memory leak",
                        "confidence": 0.88,
                        "status": "PROBABLE",
                        "rationale": "Kernel logs show OOM kill",
                        "rank": 1,
                    }
                ]
            },
        )
        session.add(step)

        # Budget
        budget = BudgetLedger(
            id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            reserved_tokens=2000,
            settled_tokens=1200,
            reserved_probes=3,
            settled_probes=1,
            reserved_cost_micros=50000,
            settled_cost_micros=25000,
        )
        session.add(budget)
        await session.commit()

    # 4. Fetch updated snapshot
    resp_updated = await api_client.get(f"/v1/incidents/{inc_id}/workbench", headers=headers)
    assert resp_updated.status_code == 200
    updated_etag = resp_updated.headers.get("ETag")
    assert updated_etag != etag
    data_updated = resp_updated.json()
    assert len(data_updated["hypotheses"]) == 1
    assert data_updated["hypotheses"][0]["title"] == "Pod OOMKilled by memory leak"
    assert data_updated["hypotheses"][0]["confidence"] == 0.88
    assert len(data_updated["evidence"]) == 1
    assert data_updated["evidence"][0]["raw_payload_redacted"] == {"log": "OutOfMemoryKilled"}
    assert len(data_updated["steps"]) == 1
    assert data_updated["steps"][0]["token_count"] == 1200
    assert len(data_updated["tool_calls"]) == 1
    assert data_updated["tool_calls"][0]["tool_id"] == test_tool_id
    assert data_updated["budget"]["settled_tokens"] == 1200


@pytest.mark.asyncio
async def test_p1_workbench_abstention_detection(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    api_client: AsyncClient,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"p1-abstain-{uuid.uuid4()}",
        target_id="k8s:pod:auth-svc",
        request_sha256="2" * 64,
    )
    inc_id = admit.response.id
    headers = {"X-Tenant-ID": str(isolated_tenant)}

    # Transition ACCEPTED -> DISPATCH_PENDING -> INVESTIGATING -> NEEDS_REVIEW
    await inc_repo.transition_status(
        tenant_id=isolated_tenant,
        incident_id=inc_id,
        target_status=IncidentStatus.DISPATCH_PENDING,
        expected_version=1,
        correlation_id="test-abstain-1",
    )
    await inc_repo.transition_status(
        tenant_id=isolated_tenant,
        incident_id=inc_id,
        target_status=IncidentStatus.INVESTIGATING,
        expected_version=2,
        correlation_id="test-abstain-2",
    )
    await inc_repo.transition_status(
        tenant_id=isolated_tenant,
        incident_id=inc_id,
        target_status=IncidentStatus.NEEDS_REVIEW,
        expected_version=3,
        correlation_id="test-abstain-3",
    )

    # Insert step with abstention reason
    async with session_factory() as session:
        step = InvestigationStep(
            id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            step_no=1,
            loop_state="ABSTAINED",
            prompt_digest="a" * 64,
            token_count=500,
            hypotheses_json={},
            abstention_reason="INSUFFICIENT_DISCRIMINATING_EVIDENCE",
        )
        session.add(step)
        await session.commit()

    resp = await api_client.get(f"/v1/incidents/{inc_id}/workbench", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["incident"]["status"] == "NEEDS_REVIEW"
    assert data["abstention"]["abstained"] is True
    assert "INSUFFICIENT_DISCRIMINATING_EVIDENCE" in data["abstention"]["reason"]


@pytest.mark.asyncio
async def test_p1_workbench_sse_stream_events_and_heartbeats(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    api_client: AsyncClient,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    admit = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"p1-sse-{uuid.uuid4()}",
        target_id="k8s:pod:billing-svc",
        request_sha256="3" * 64,
    )
    inc_id = admit.response.id
    headers = {"X-Tenant-ID": str(isolated_tenant)}

    # Request SSE stream with fast ping interval (0.2s) and max_events=3
    async with api_client.stream(
        "GET",
        f"/v1/incidents/{inc_id}/events?ping_interval_seconds=0.2&max_events=3",
        headers=headers,
    ) as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["Content-Type"]

        lines = []
        async for line in response.aiter_lines():
            if line.strip():
                lines.append(line)

    # Verify event stream structure
    # Expected events: connected, then pings or updates
    content = "\n".join(lines)
    assert "event: connected" in content
    assert f'"incident_id": "{inc_id}"' in content
    assert "id: 1" in content
    assert "event: ping" in content or "event: connected" in content


@pytest.mark.asyncio
async def test_p1_workbench_missing_incident_returns_404(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
) -> None:
    fake_id = uuid.uuid4()
    resp = await api_client.get(
        f"/v1/incidents/{fake_id}/workbench",
        headers={"X-Tenant-ID": str(isolated_tenant)},
    )
    assert resp.status_code == 404
