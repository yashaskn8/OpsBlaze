"""B1 lost-ack replay against native Temporal, with a durable outbox row."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from temporalio.testing import WorkflowEnvironment

from backend.models import OutboxEntry
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.workers.dispatcher import dispatch_once, workflow_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lost_start_ack_reuses_one_temporal_workflow(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "lost-ack", IncidentCreate(target_id="checkout")
        )
    async with await WorkflowEnvironment.start_local() as env:
        class LostAckClient:
            async def start_workflow(self, *args, **kwargs):
                await env.client.start_workflow(*args, **kwargs)
                raise ConnectionError("Temporal start committed, acknowledgment lost")

        # The outbox is not marked delivered after the simulated lost response.
        assert await dispatch_once(LostAckClient(), sessions_fixture)
        async with sessions_fixture() as session:
            pending = await session.scalar(select(OutboxEntry))
            assert pending.attempt == 1
            assert pending.delivered_at is None
            # Advance eligibility without sleep; the original Temporal
            # execution remains running since no worker has been started.
            async with session.begin_nested():
                pending.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()

        # Temporal reports the existing stable execution; the dispatcher
        # treats it as delivered rather than creating another logical start.
        assert await dispatch_once(env.client, sessions_fixture)
        async with sessions_fixture() as session:
            delivered = await session.scalar(select(OutboxEntry))
            assert delivered.attempt == 2
            assert delivered.delivered_at is not None
            assert await session.scalar(
                select(OutboxEntry.id).where(OutboxEntry.delivered_at.is_(None))
            ) is None
        assert await env.client.get_workflow_handle(
            workflow_id("t", incident.id)
        ).describe() is not None
