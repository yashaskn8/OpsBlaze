
import pytest
from sqlalchemy import select

from backend.adapters.conformance import execute_suite, simulator_digest
from backend.models import (
    AdapterConformanceRun, Incident, PolicyAssignment, SimulatorAppliedEffect, SimulatorTarget,
)


@pytest.mark.asyncio
async def test_adapter_conformance_is_isolated_and_persisted(sessions_fixture):
    run = await execute_suite(sessions_fixture)
    assert run.state == "LAB_PASS"
    assert run.code_digest == simulator_digest()
    names = {scenario["scenario"] for scenario in run.results}
    assert names == {
        "first-apply", "duplicate-apply", "effect-status", "stale-fence",
        "version-cas", "lost-ack-lookup", "expired-lease", "missing-effect",
        "effect-identity-binding", "status-store-outage",
        "status-store-recovered", "oracle-outage", "oracle-recovered",
        "retention-gap", "retention-recovered", "external-version-edit",
        "permit-version-binding", "retention-horizon-contract",
    }
    assert all(scenario["passed"] for scenario in run.results)
    async with sessions_fixture() as session:
        assert len((await session.scalars(select(AdapterConformanceRun))).all()) == 1
        assert (await session.scalars(select(SimulatorTarget))).all() == []
        assert (await session.scalars(select(SimulatorAppliedEffect))).all() == []
        assert (await session.scalars(select(Incident))).all() == []
        assert (await session.scalars(select(PolicyAssignment))).all() == []


@pytest.mark.asyncio
async def test_scoped_conformance_registry_and_lab_only_enablement(sessions_fixture):
    from httpx import ASGITransport, AsyncClient
    from backend.api import app
    from backend.auth import Principal, get_principal
    from backend.database import get_session
    from backend.settings import Settings, get_settings

    async def override_session():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(
        local_token="e8-op", environment="development",
    )
    try:
        op_headers = {"X-Tenant-Id": "a", "X-OpsBlaze-Token": "e8-op"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            run_resp = await client.post("/v1/adapters/simulator/conformance/run", headers=op_headers)
            assert run_resp.status_code == 201, run_resp.text
            assert run_resp.json()["state"] == "LAB_PASS"
            assert run_resp.json()["production_enablement"] is False

            hist = await client.get("/v1/adapters/simulator-v1/conformance", headers=op_headers)
            assert hist.status_code == 200
            assert len(hist.json()["records"]) == 1
            assert hist.json()["scope"] == "LAB_ONLY"

            # Operator cannot change adapter enablement (requires security_admin role).
            denied_op = await client.post(
                "/v1/adapters/simulator-v1/enablement",
                json={"enabled": True, "reason": "Operator should not enable adapter"},
                headers={**op_headers, "If-Match": "0"},
            )
            assert denied_op.status_code == 403

            app.dependency_overrides[get_settings] = lambda: Settings(
                local_token="e8-op", environment="production",
            )
            app.dependency_overrides[get_principal] = lambda: Principal(
                tenant_id="a",
                subject="oidc:00000000000000000000000000000001",
                roles=frozenset({"security_admin"}),
            )
            enabled = await client.post(
                "/v1/adapters/simulator-v1/enablement",
                json={"enabled": True, "reason": "Enable simulator in isolated lab only"},
                headers={"If-Match": "0"},
            )
            assert enabled.status_code == 202, enabled.text
            assert enabled.json()["enabled"] is True
            assert enabled.json()["production_enablement"] is False

            revoked = await client.post(
                "/v1/adapters/simulator-v1/enablement",
                json={"enabled": False, "reason": "Revoke lab enablement after check"},
                headers={"If-Match": "1"},
            )
            assert revoked.status_code == 202 and revoked.json()["enabled"] is False

            blocked_ext = await client.post(
                "/v1/adapters/aws-ec2-prod/enablement",
                json={"enabled": True, "reason": "Attempt external adapter enablement"},
                headers={"If-Match": "0"},
            )
            assert blocked_ext.status_code == 403
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_simulator_adapter_refuses_expired_retention_horizon(sessions_fixture):
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4
    from backend.adapters.simulator import SimulatorAdapter
    from backend.models import SimulatorAppliedEffect

    expired_id = uuid4()
    fresh_id = uuid4()
    now = datetime.now(timezone.utc)
    async with sessions_fixture.begin() as session:
        session.add(SimulatorAppliedEffect(
            id=expired_id, tenant_id="t-ret", target_id="checkout",
            observed_version=2, permit_digest="a" * 64,
            created_at=now - timedelta(seconds=700),
        ))
        session.add(SimulatorAppliedEffect(
            id=fresh_id, tenant_id="t-ret", target_id="checkout",
            observed_version=3, permit_digest="b" * 64,
            created_at=now - timedelta(seconds=100),
        ))
    adapter = SimulatorAdapter(sessions_fixture, retention_seconds=600)
    assert await adapter.query_effect(expired_id, now=now) is None
    assert await adapter.query_effect(fresh_id, now=now) == 3
