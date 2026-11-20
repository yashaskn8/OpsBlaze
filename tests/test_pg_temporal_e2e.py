
"""Native PostgreSQL + Temporal vertical slice; no Docker, no production effects."""

import asyncio
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.api import app
from backend.database import get_session_maker
from backend.settings import get_settings
from backend.workers.dispatcher import dispatch_once
from backend.workers.seed_simulator import seed
from backend.workflows.activities import (
    apply_approved_simulator_plan,
    inspect_incident_read_only,
    project_incident_status,
)
from backend.workflows.incident import IncidentWorkflow


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_postgres_temporal_simulator_recovery() -> None:
    settings = get_settings()
    assert settings.database_url.startswith("postgresql+asyncpg:")
    await seed("demo", "checkout")
    sessions = get_session_maker()

    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue=settings.temporal_task_queue,
            workflows=[IncidentWorkflow],
            activities=[
                inspect_incident_read_only,
                project_incident_status,
                apply_approved_simulator_plan,
            ],
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {
                    "X-Tenant-Id": "demo",
                    "X-OpsBlaze-Token": settings.local_token,
                }
                created = await client.post(
                    "/v1/incidents",
                    headers={**headers, "Idempotency-Key": str(uuid4())},
                    json={"target_id": "checkout"},
                )
                assert created.status_code == 202, created.text
                incident_id = created.json()["id"]
                assert await dispatch_once(env.client, sessions)

                async def await_state(expected: str) -> dict:
                    for _ in range(100):
                        response = await client.get(
                            f"/v1/incidents/{incident_id}", headers=headers
                        )
                        assert response.status_code == 200, response.text
                        body = response.json()
                        if body["status_projection"] == expected:
                            return body
                        await asyncio.sleep(0.1)
                    raise AssertionError(f"incident never reached {expected}")

                await await_state("AWAITING_APPROVAL")
                evidence = await client.get(
                    f"/v1/incidents/{incident_id}/evidence", headers=headers
                )
                assert evidence.status_code == 200
                assert len(evidence.json()) == 2

                plans = await client.get(
                    f"/v1/incidents/{incident_id}/plans", headers=headers
                )
                assert plans.status_code == 200 and len(plans.json()) == 1
                plan = plans.json()[0]
                assert plan["status"] == "PROPOSED"
                decision = await client.post(
                    f"/v1/plans/{plan['id']}/decisions",
                    headers={**headers, "If-Match": str(plan["version"])},
                    json={"decision": "APPROVE"},
                )
                assert decision.status_code == 201, decision.text
                assert await dispatch_once(env.client, sessions)

                final = await await_state("VERIFIED")
                assert final["status_projection"] == "VERIFIED"
                effects = await client.get(
                    f"/v1/incidents/{incident_id}/effects", headers=headers
                )
                assert effects.status_code == 200 and len(effects.json()) == 1
                assert effects.json()[0]["state"] == "VERIFIED"
                target = await client.get(
                    "/v1/targets/checkout/simulator", headers=headers
                )
                assert target.status_code == 200
                assert target.json()["healthy"] is True
                assert target.json()["version"] == 2
