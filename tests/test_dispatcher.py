from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models import OutboxEntry
from backend.workers.dispatcher import dispatch_once, workflow_id


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
