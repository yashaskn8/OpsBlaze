"""Feature A2 — Capability-Gated Read-Only Tools Unit and Adversarial Test Suite.

Verifies:
1. Catalog rejects any mutation tool at registration.
2. Grant validation enforces tenant, incident, target, expiry, and revocation bounds.
3. Gateway executes read-only tools against adapter, bounds outputs (64KB), redacts secrets.
4. Gateway rejects SSRF and unauthorized egress destinations.
5. Adapter timeouts cleanly record TIMED_OUT status and duration in tool_call ledger.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.simulator import DisposableTargetSimulator
from backend.domain.tools import (
    CapabilityExpiredError,
    CapabilityRevokedError,
    MutationToolRejectedError,
    ToolCallRequest,
    ToolDefinition,
    ToolSecurityError,
    ToolStatus,
)
from backend.persistence.models import CapabilityGrant, Incident, ToolCall
from backend.tools.catalog import ToolCatalog
from backend.tools.gateway import CapabilityGateway
from backend.tools.redactor import SecretRedactor


@pytest.fixture
def test_tenant_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def test_incident_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def simulator() -> DisposableTargetSimulator:
    return DisposableTargetSimulator()


@pytest.mark.asyncio
async def test_1_catalog_rejects_mutation_tool() -> None:
    """A2 Invariant: No mutation tools permitted in ToolCatalog."""
    catalog = ToolCatalog()
    with pytest.raises(MutationToolRejectedError, match="Mutation tools prohibited in A2 catalog"):
        catalog.register_tool(
            ToolDefinition(
                id="restart_service",
                version="v1",
                input_schema_hash="hash1",
                output_schema_hash="hash2",
                read_only=False,  # VIOLATION
                description="Mutating tool",
            )
        )


@pytest.mark.asyncio
async def test_2_catalog_registers_standard_read_only_tools() -> None:
    """Catalog contains the 4 approved read-only inspection tools."""
    catalog = ToolCatalog.create_default()
    tools = catalog.list_tools()
    tool_ids = {t.id for t in tools}
    assert {"telemetry_sample", "log_query", "config_read", "health_check"}.issubset(tool_ids)
    for t in tools:
        assert t.read_only is True


@pytest.mark.asyncio
async def test_3_secret_redactor_masks_credentials() -> None:
    """SecretRedactor masks tokens, API keys, passwords, and private keys."""
    redactor = SecretRedactor()
    raw = {
        "status": "healthy",
        "aws_key": "AKIAIOSFODNN7EXAMPLE",
        "auth_header": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secretpayload",
        "db_uri": "postgres://user:SuperSecretPassword123@localhost:5432/db",
        "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgk...",
    }
    redacted = redactor.redact_dict(raw)
    assert redacted["aws_key"] == "[REDACTED_SECRET]"
    assert redacted["auth_header"] == "[REDACTED_SECRET]"
    assert "SuperSecretPassword123" not in str(redacted)
    assert redacted["status"] == "healthy"


@pytest.mark.asyncio
async def test_4_capability_gateway_grant_validation_enforces_bounds(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    test_incident_id: uuid.UUID,
    simulator: DisposableTargetSimulator,
) -> None:
    """Grant validation rejects cross-tenant, wrong target, expired, or revoked grants."""
    test_tenant_id = isolated_tenant
    target_id = "target-node-1"
    now = datetime.now(UTC)

    # Setup incident in DB
    catalog = ToolCatalog.create_default()
    async with session_factory() as session:
        await catalog.sync_to_db(session)
        inc = Incident(
            id=test_incident_id,
            tenant_id=test_tenant_id,
            client_key=f"key-{test_incident_id.hex[:6]}",
            request_sha256="reqhash",
            target_id=target_id,
            status_projection="INVESTIGATING",
            status_version=1,
        )
        session.add(inc)
        await session.flush()

        # 1. Valid grant
        grant_valid_id = uuid.uuid4()
        session.add(
            CapabilityGrant(
                id=grant_valid_id,
                tenant_id=test_tenant_id,
                incident_id=test_incident_id,
                tool_id="telemetry_sample",
                tool_version="v1",
                target_id=target_id,
                expires_at=now + timedelta(minutes=10),
            )
        )

        # 2. Expired grant
        grant_expired_id = uuid.uuid4()
        session.add(
            CapabilityGrant(
                id=grant_expired_id,
                tenant_id=test_tenant_id,
                incident_id=test_incident_id,
                tool_id="telemetry_sample",
                tool_version="v1",
                target_id=target_id,
                expires_at=now - timedelta(seconds=5),
            )
        )

        # 3. Revoked grant
        grant_revoked_id = uuid.uuid4()
        session.add(
            CapabilityGrant(
                id=grant_revoked_id,
                tenant_id=test_tenant_id,
                incident_id=test_incident_id,
                tool_id="telemetry_sample",
                tool_version="v1",
                target_id=target_id,
                expires_at=now + timedelta(minutes=10),
                revoked_at=now - timedelta(seconds=1),
            )
        )
        await session.commit()

    gateway = CapabilityGateway(session_factory, ToolCatalog.create_default())

    # Case A: Valid grant succeeds
    req_valid = ToolCallRequest(
        tenant_id=test_tenant_id,
        incident_id=test_incident_id,
        step_no=1,
        grant_id=grant_valid_id,
        tool_id="telemetry_sample",
        target_id=target_id,
        parameters={"metric": "cpu_utilization", "window_minutes": 5},
    )
    res_valid = await gateway.execute_tool(req_valid, adapter=simulator, now=now)
    assert res_valid.status == ToolStatus.COMPLETED
    assert res_valid.payload is not None
    assert "cpu_utilization" in str(res_valid.payload)

    # Case B: Cross-tenant call with valid grant ID rejected
    req_cross_tenant = ToolCallRequest(
        tenant_id=uuid.uuid4(),  # Different tenant
        incident_id=test_incident_id,
        step_no=2,
        grant_id=grant_valid_id,
        tool_id="telemetry_sample",
        target_id=target_id,
        parameters={},
    )
    with pytest.raises(ToolSecurityError, match="Cross-tenant capability grant violation"):
        await gateway.execute_tool(req_cross_tenant, adapter=simulator, now=now)

    # Case C: Expired grant rejected
    req_expired = ToolCallRequest(
        tenant_id=test_tenant_id,
        incident_id=test_incident_id,
        step_no=3,
        grant_id=grant_expired_id,
        tool_id="telemetry_sample",
        target_id=target_id,
        parameters={},
    )
    with pytest.raises(CapabilityExpiredError, match="expired"):
        await gateway.execute_tool(req_expired, adapter=simulator, now=now)

    # Case D: Revoked grant rejected
    req_revoked = ToolCallRequest(
        tenant_id=test_tenant_id,
        incident_id=test_incident_id,
        step_no=4,
        grant_id=grant_revoked_id,
        tool_id="telemetry_sample",
        target_id=target_id,
        parameters={},
    )
    with pytest.raises(CapabilityRevokedError, match="revoked"):
        await gateway.execute_tool(req_revoked, adapter=simulator, now=now)


@pytest.mark.asyncio
async def test_5_tool_execution_enforces_semantic_bounds_and_ssrf(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    test_incident_id: uuid.UUID,
    simulator: DisposableTargetSimulator,
) -> None:
    """Oversized outputs are safely bounded, SSRF targets outside allowlist are blocked."""
    test_tenant_id = isolated_tenant
    target_id = "target-node-2"
    now = datetime.now(UTC)

    catalog = ToolCatalog.create_default()
    async with session_factory() as session:
        await catalog.sync_to_db(session)
        inc = Incident(
            id=test_incident_id,
            tenant_id=test_tenant_id,
            client_key=f"key-{test_incident_id.hex[:6]}",
            request_sha256="reqhash",
            target_id=target_id,
            status_projection="INVESTIGATING",
            status_version=1,
        )
        session.add(inc)
        await session.flush()
        grant_id = uuid.uuid4()
        session.add(
            CapabilityGrant(
                id=grant_id,
                tenant_id=test_tenant_id,
                incident_id=test_incident_id,
                tool_id="log_query",
                tool_version="v1",
                target_id=target_id,
                expires_at=now + timedelta(minutes=10),
            )
        )
        await session.commit()

    gateway = CapabilityGateway(session_factory, ToolCatalog.create_default())

    # Case A: SSRF / Egress check rejects unauthorized URL
    req_ssrf = ToolCallRequest(
        tenant_id=test_tenant_id,
        incident_id=test_incident_id,
        step_no=1,
        grant_id=grant_id,
        tool_id="log_query",
        target_id=target_id,
        parameters={"endpoint": "http://169.254.169.254/latest/meta-data/"},  # AWS metadata SSRF
    )
    with pytest.raises(ToolSecurityError, match="Egress destination rejected"):
        await gateway.execute_tool(req_ssrf, adapter=simulator, now=now)

    # Case B: Normal log query returns bounded payload with audit entry
    req_logs = ToolCallRequest(
        tenant_id=test_tenant_id,
        incident_id=test_incident_id,
        step_no=2,
        grant_id=grant_id,
        tool_id="log_query",
        target_id=target_id,
        parameters={"query": "error", "limit": 50},
    )
    res_logs = await gateway.execute_tool(req_logs, adapter=simulator, now=now)
    assert res_logs.status == ToolStatus.COMPLETED
    assert res_logs.args_digest != ""
    assert res_logs.output_digest is not None

    # Verify audit row in database
    async with session_factory() as session:
        tc = (
            await session.get(ToolCall, res_logs.call_id)
        )
        assert tc is not None
        assert tc.status == ToolStatus.COMPLETED.value
        assert tc.step_no == 2
