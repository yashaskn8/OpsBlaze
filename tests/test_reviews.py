import pytest
from sqlalchemy import select, update

from backend.models import Incident, OutboxEntry
from backend.schemas import IncidentCreate
from backend.services import (
    VersionConflict,
    acknowledge_review,
    admit_incident,
    request_cancellation,
)


@pytest.mark.asyncio
async def test_cancel_requires_current_version(sessions_fixture) -> None:
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "a", "key", IncidentCreate(target_id="db")
        )
    async with sessions_fixture() as session:
        with pytest.raises(VersionConflict):
            await request_cancellation(session, "a", incident.id, 99)
    async with sessions_fixture() as session:
        cancelled = await request_cancellation(session, "a", incident.id, 1)
        assert cancelled.status_projection == "CANCEL_REQUESTED"
        assert cancelled.status_version == 2
    async with sessions_fixture() as session:
        intents = list((await session.scalars(select(OutboxEntry).order_by(OutboxEntry.id))).all())
        assert [i.kind for i in intents] == ["START_WORKFLOW", "CANCEL_WORKFLOW"]


@pytest.mark.asyncio
async def test_review_requires_current_state_and_version(sessions_fixture) -> None:
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "a", "key", IncidentCreate(target_id="db")
        )
    async with sessions_fixture() as session:
        with pytest.raises(VersionConflict):
            await acknowledge_review(session, "a", incident.id, 1)
    async with sessions_fixture() as session:
        async with session.begin():
            await session.execute(
                update(Incident)
                .where(Incident.id == incident.id)
                .values(status_projection="AWAITING_APPROVAL", status_version=2)
            )
    async with sessions_fixture() as session:
        updated = await acknowledge_review(session, "a", incident.id, 2)
        assert updated.status_projection == "REVIEW_ACKNOWLEDGED"
        assert updated.status_version == 3
    async with sessions_fixture() as session:
        with pytest.raises(VersionConflict):
            await acknowledge_review(session, "a", incident.id, 2)


@pytest.mark.asyncio
async def test_operator_typed_handoff_endpoint_cites_evidence_and_next_steps(
    sessions_fixture,
):
    from httpx import ASGITransport, AsyncClient
    from backend.api import app
    from backend.database import get_session
    from backend.models import SimulatorTarget
    from backend.probes.gateway import invoke_probe
    from backend.settings import Settings, get_settings

    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(
                tenant_id="handoff-t", target_id="checkout", version=1, healthy=False, last_fence=0,
            ))
        incident = await admit_incident(
            session, "handoff-t", "handoff-1", IncidentCreate(target_id="checkout")
        )
    probe = await invoke_probe(sessions_fixture, "handoff-t", incident.id, 1, "simulator.health")

    async def override_session():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(local_token="handoff-token")
    try:
        headers = {"X-Tenant-Id": "handoff-t", "X-OpsBlaze-Token": "handoff-token"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post(
                f"/v1/incidents/{incident.id}/handoff",
                json={
                    "reason_code": "LOW_CONFIDENCE",
                    "note": "Model confidence below threshold for automated proposal",
                    "next_steps": ["Verify replica lag", "Approve manual restart"],
                },
                headers={**headers, "If-Match": "1"},
            )
            assert res.status_code == 202, res.text
            body = res.json()
            assert body["status_projection"] == "NEEDS_REVIEW"
            assert body["status_version"] == 2
            assert body["reason_code"] == "LOW_CONFIDENCE"
            assert body["next_steps"] == ["Verify replica lag", "Approve manual restart"]
            assert body["evidence_ids"] == [str(probe.evidence_id)]
            stale = await client.post(
                f"/v1/incidents/{incident.id}/handoff",
                headers={**headers, "If-Match": "1"},
            )
            assert stale.status_code == 409
    finally:
        app.dependency_overrides.clear()
