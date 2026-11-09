"""Integration tests for Stage 12 / Feature E7 Strategy Router HTTP API endpoints."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.main import create_app
from backend.config import Settings
from backend.domain.auth import ActorKind, PrincipalRole, mint_principal_token
from backend.persistence.models import Incident


@pytest.fixture
async def test_incident(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> uuid.UUID:
    incident_id = uuid.uuid4()
    async with session_factory() as session:
        inc = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"api-test-{incident_id}",
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
        actor_id="user-operator-1",
        actor_kind=ActorKind.HUMAN,
        tenant_id=isolated_tenant,
        roles=frozenset({PrincipalRole.OPERATOR.value}),
        secret_key=test_settings.auth_secret_key or "",
        ttl_seconds=3600,
    )


@pytest.fixture
def unauthorized_token(test_settings: Settings, isolated_tenant: uuid.UUID) -> str:
    return mint_principal_token(
        actor_id="user-guest-1",
        actor_kind=ActorKind.HUMAN,
        tenant_id=isolated_tenant,
        roles=frozenset({"GUEST"}),
        secret_key=test_settings.auth_secret_key or "",
        ttl_seconds=3600,
    )


@pytest.mark.asyncio
async def test_api_list_routes(
    test_settings: Settings,
    operator_token: str,
) -> None:
    app = create_app(test_settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/v1/strategies/routes",
            headers={"Authorization": f"Bearer {operator_token}"},
        )
        assert resp.status_code == 200, resp.text
        routes = resp.json()
        assert len(routes) >= 5
        keys = {r["route_key"] for r in routes}
        assert "STANDARD_PROBE" in keys


@pytest.mark.asyncio
async def test_api_select_route_success(
    test_settings: Settings,
    operator_token: str,
    test_incident: uuid.UUID,
) -> None:
    app = create_app(test_settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/strategies/select",
            headers={"Authorization": f"Bearer {operator_token}"},
            json={
                "incident_id": str(test_incident),
                "incident_severity": "medium",
                "estimated_risk": 0.4,
                "requested_tools": ["system_metrics", "log_query"],
                "available_budget_micros": 50000,
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["route_key"] in ("STANDARD_PROBE", "DEEP_ANALYSIS")
        assert 0.0 < data["propensity"] <= 1.0
        assert "system_metrics" in data["granted_tool_capabilities"]


@pytest.mark.asyncio
async def test_api_select_route_unauthorized_forbidden(
    test_settings: Settings,
    unauthorized_token: str,
    test_incident: uuid.UUID,
) -> None:
    app = create_app(test_settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/strategies/select",
            headers={"Authorization": f"Bearer {unauthorized_token}"},
            json={
                "incident_id": str(test_incident),
                "incident_severity": "medium",
                "estimated_risk": 0.4,
                "requested_tools": ["system_metrics"],
                "available_budget_micros": 50000,
            },
        )
        assert resp.status_code == 403
        assert "lacks required role" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_api_select_route_cross_tenant_rejected(
    test_settings: Settings,
    operator_token: str,
) -> None:
    app = create_app(test_settings)
    # Different tenant's incident ID
    other_incident_id = uuid.uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/strategies/select",
            headers={"Authorization": f"Bearer {operator_token}"},
            json={
                "incident_id": str(other_incident_id),
                "incident_severity": "low",
                "estimated_risk": 0.2,
                "available_budget_micros": 50000,
            },
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_api_decisions_and_settle_lifecycle(
    test_settings: Settings,
    operator_token: str,
    test_incident: uuid.UUID,
) -> None:
    app = create_app(test_settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Step 1: Select route
        resp1 = await client.post(
            "/v1/strategies/select",
            headers={"Authorization": f"Bearer {operator_token}"},
            json={
                "incident_id": str(test_incident),
                "incident_severity": "medium",
                "estimated_risk": 0.3,
                "requested_tools": ["system_metrics", "log_query"],
                "available_budget_micros": 50000,
            },
        )
        assert resp1.status_code == 200

        # Step 2: Query decisions
        resp2 = await client.get(
            f"/v1/strategies/decisions/{test_incident}",
            headers={"Authorization": f"Bearer {operator_token}"},
        )
        assert resp2.status_code == 200
        decisions = resp2.json()
        assert len(decisions) >= 1
        decision_id = decisions[0]["id"]
        assert decisions[0]["actual_cost_micros"] is None

        # Step 3: Settle cost
        resp3 = await client.post(
            f"/v1/strategies/decisions/{decision_id}/settle",
            headers={"Authorization": f"Bearer {operator_token}"},
            json={"actual_cost_micros": 4200},
        )
        assert resp3.status_code == 200
        settled = resp3.json()
        assert settled["actual_cost_micros"] == 4200
