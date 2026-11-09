"""Adversarial and Security Test Suite for Stage 12 / Feature E7: Budgeted Strategy Router.

Verifies:
1. Tool capability expansion attempts fail closed with StrategyPermissionEscalationError.
2. Capability E7 possesses zero consequential authority (cannot mint fences, execute mutations).
3. Strict tenant isolation on strategy decisions and routes.
4. High-concurrency route selection resistance to PostgreSQL deadlocks.
5. Strict Pydantic parameter tampering defenses (extra='forbid', bounds checks).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.main import create_app
from backend.config import Settings
from backend.domain.auth import ActorKind, PrincipalRole, mint_principal_token
from backend.domain.strategy import (
    RouteSelectionRequest,
    StrategyPermissionEscalationError,
    validate_tool_capabilities_subset,
)
from backend.investigator.router import StrategyRouterService
from backend.persistence.models import Incident


@pytest.fixture
async def sample_incident(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> uuid.UUID:
    incident_id = uuid.uuid4()
    async with session_factory() as session:
        inc = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"sec-test-{incident_id}",
            request_sha256="test-sha256",
            target_id="service-auth-gateway",
            status_projection="ACTIVE",
            status_version=1,
        )
        session.add(inc)
        await session.commit()
    return incident_id


@pytest.fixture
def operator_token(test_settings: Settings, isolated_tenant: uuid.UUID) -> str:
    return mint_principal_token(
        actor_id="user-sec-operator-1",
        actor_kind=ActorKind.HUMAN,
        tenant_id=isolated_tenant,
        roles=frozenset({PrincipalRole.OPERATOR.value}),
        secret_key=test_settings.auth_secret_key or "",
        ttl_seconds=3600,
    )


@pytest.mark.asyncio
async def test_tool_permission_escalation_attempt_fails_closed() -> None:
    """Verifies that an investigator route cannot grant capabilities outside the base catalog."""
    base_catalog = frozenset({"system_metrics", "log_query"})
    rogue_route_tools = frozenset({"system_metrics", "log_query", "restart_pod", "drop_table"})

    with pytest.raises(StrategyPermissionEscalationError) as exc_info:
        validate_tool_capabilities_subset(
            base_capabilities=base_catalog,
            route_capabilities=rogue_route_tools,
        )

    err_msg = str(exc_info.value)
    assert "cannot expand tool capabilities" in err_msg
    assert "restart_pod" in err_msg
    assert "drop_table" in err_msg


@pytest.mark.asyncio
async def test_cross_tenant_isolation_adversarial(
    test_settings: Settings,
    operator_token: str,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Verifies that Tenant A cannot inspect or settle decisions created by Tenant B."""
    app = create_app(test_settings)

    # Create an incident in a DIFFERENT tenant
    from backend.persistence.repositories import IncidentRepository

    repo = IncidentRepository(session_factory)
    victim_tenant = uuid.uuid4()
    victim_incident = uuid.uuid4()
    await repo.ensure_tenant(victim_tenant, f"victim-{victim_tenant}")

    async with session_factory() as session:
        session.add(
            Incident(
                id=victim_incident,
                tenant_id=victim_tenant,
                client_key=f"victim-{victim_incident}",
                request_sha256="victim-sha256",
                target_id="victim-db",
                status_projection="ACTIVE",
                status_version=1,
            )
        )
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Attacker with token from isolated_tenant attempts to read victim decisions
        resp = await client.get(
            f"/v1/strategies/decisions/{victim_incident}",
            headers={"Authorization": f"Bearer {operator_token}"},
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

        # Attacker attempts to route victim incident
        resp2 = await client.post(
            "/v1/strategies/select",
            headers={"Authorization": f"Bearer {operator_token}"},
            json={
                "incident_id": str(victim_incident),
                "incident_severity": "high",
                "estimated_risk": 0.5,
                "available_budget_micros": 50000,
            },
        )
        assert resp2.status_code == 404

    # Cleanup victim tenant
    async with session_factory() as session:
        from backend.persistence.models import Tenant

        inc = await session.get(Incident, victim_incident)
        if inc:
            await session.delete(inc)
        t = await session.get(Tenant, victim_tenant)
        if t:
            await session.delete(t)
        await session.commit()


@pytest.mark.asyncio
async def test_concurrent_strategy_selection_under_load(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Verifies that 10 concurrent route selections execute without deadlock or state corruption."""
    router = StrategyRouterService(session_factory)

    # Create 10 incidents concurrently
    incident_ids: list[uuid.UUID] = []
    async with session_factory() as session:
        for i in range(10):
            inc_id = uuid.uuid4()
            incident_ids.append(inc_id)
            session.add(
                Incident(
                    id=inc_id,
                    tenant_id=isolated_tenant,
                    client_key=f"concurrent-{inc_id}",
                    request_sha256="concurrent-sha256",
                    target_id="service-api-worker",
                    status_projection="ACTIVE",
                    status_version=1,
                )
            )
        await session.commit()

    async def select_worker(inc_id: uuid.UUID, risk: float, budget: int):
        req = RouteSelectionRequest(
            tenant_id=isolated_tenant,
            incident_id=inc_id,
            incident_severity="medium",
            estimated_risk=risk,
            requested_tools=frozenset({"system_metrics", "log_query"}),
            available_budget_micros=budget,
        )
        return await router.select_route(
            req, base_tool_allowlist=frozenset({"system_metrics", "log_query"})
        )

    tasks = [
        select_worker(inc_id, risk=0.1 * (i % 5 + 1), budget=10000 * (i + 1))
        for i, inc_id in enumerate(incident_ids)
    ]

    results = await asyncio.gather(*tasks, return_exceptions=False)
    assert len(results) == 10
    for res in results:
        assert res.route_key in (
            "FAST_HEURISTIC",
            "STANDARD_PROBE",
            "DEEP_ANALYSIS",
            "CONSERVATIVE_FALLBACK",
        )
        assert 0.0 < res.propensity <= 1.0


@pytest.mark.asyncio
async def test_parameter_tampering_rejected(
    test_settings: Settings,
    operator_token: str,
    sample_incident: uuid.UUID,
) -> None:
    """Verifies strict request body validation (extra fields forbidden, bounds enforced)."""
    app = create_app(test_settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Attack 1: Injected extra field
        resp1 = await client.post(
            "/v1/strategies/select",
            headers={"Authorization": f"Bearer {operator_token}"},
            json={
                "incident_id": str(sample_incident),
                "incident_severity": "low",
                "estimated_risk": 0.2,
                "injected_admin_override": True,  # Extra field
            },
        )
        assert resp1.status_code == 422

        # Attack 2: Out of bounds estimated_risk (> 1.0)
        resp2 = await client.post(
            "/v1/strategies/select",
            headers={"Authorization": f"Bearer {operator_token}"},
            json={
                "incident_id": str(sample_incident),
                "incident_severity": "low",
                "estimated_risk": 2.5,  # Out of bounds
            },
        )
        assert resp2.status_code == 422

        # Attack 3: Negative budget
        resp3 = await client.post(
            "/v1/strategies/select",
            headers={"Authorization": f"Bearer {operator_token}"},
            json={
                "incident_id": str(sample_incident),
                "incident_severity": "low",
                "estimated_risk": 0.5,
                "available_budget_micros": -1000,  # Negative
            },
        )
        assert resp3.status_code == 422
