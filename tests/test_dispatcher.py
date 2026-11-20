from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy import select

from backend.models import OutboxEntry
from backend.workers.dispatcher import claim_intent, dispatch_once, workflow_id


def test_workflow_identity_is_stable() -> None:
    incident_id = uuid4()
    assert workflow_id("tenant-1", incident_id) == f"opsblaze/tenant-1/{incident_id}"


@pytest.mark.asyncio
async def test_dispatch_failure_retains_outbox(sessions_fixture) -> None:
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    sessions = sessions_fixture
    async with sessions() as session:
        await admit_incident(session, "a", "key", IncidentCreate(target_id="test"))

    with patch(
        "backend.workers.dispatcher.deliver_intent",
        new_callable=AsyncMock,
        side_effect=ConnectionError("Temporal unavailable"),
    ):
        assert await dispatch_once(AsyncMock(), sessions)
    async with sessions() as session:
        row = await session.scalar(select(OutboxEntry))
        assert row.delivered_at is None
        assert row.attempt == 1


@pytest.mark.asyncio
async def test_cancel_signal_cannot_overtake_unacknowledged_start(sessions_fixture):
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident, request_cancellation

    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "race", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        await request_cancellation(session, "t", incident.id, 1)
    async with sessions_fixture() as session:
        async with session.begin():
            start = await session.scalar(
                select(OutboxEntry).where(OutboxEntry.kind == "START_WORKFLOW")
            )
            start.available_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            start_id = start.id

    assert await claim_intent(sessions_fixture) is None

    async with sessions_fixture() as session:
        async with session.begin():
            start = await session.get(OutboxEntry, start_id)
            start.delivered_at = datetime.now(timezone.utc)

    claimed = await claim_intent(sessions_fixture)
    assert claimed is not None and claimed[3] == "CANCEL_WORKFLOW"
