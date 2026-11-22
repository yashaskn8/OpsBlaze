import asyncio
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


@pytest.mark.asyncio
async def test_later_signals_wait_for_oldest_unacknowledged_intent(sessions_fixture):
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    async with sessions_fixture() as session:
        first = await admit_incident(
            session, "tenant-a", "a", IncidentCreate(target_id="checkout"),
        )
    async with sessions_fixture() as session:
        second = await admit_incident(
            session, "tenant-b", "b", IncidentCreate(target_id="checkout"),
        )
    async with sessions_fixture() as session:
        async with session.begin():
            starts = (await session.scalars(
                select(OutboxEntry).where(OutboxEntry.kind == "START_WORKFLOW")
            )).all()
            for row in starts:
                row.delivered_at = datetime.now(timezone.utc)
            oldest = OutboxEntry(
                incident_id=first.id, kind="ACKNOWLEDGE_REVIEW",
                available_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
            session.add(oldest)
            await session.flush()
            later = OutboxEntry(
                incident_id=first.id, kind="CANCEL_WORKFLOW",
            )
            other = OutboxEntry(
                incident_id=second.id, kind="CANCEL_WORKFLOW",
            )
            session.add_all([later, other])
            await session.flush()
            oldest_id, later_id, other_id = oldest.id, later.id, other.id

    claimed = await claim_intent(sessions_fixture)
    assert claimed is not None and claimed[0] == other_id
    # The earlier signal is backed off; the later one must not leapfrog it.
    assert await claim_intent(sessions_fixture) is None

    async with sessions_fixture() as session:
        async with session.begin():
            (await session.get(OutboxEntry, oldest_id)).available_at = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            )
            (await session.get(OutboxEntry, other_id)).delivered_at = (
                datetime.now(timezone.utc)
            )

    assert (await claim_intent(sessions_fixture))[0] == oldest_id
    assert await claim_intent(sessions_fixture) is None

    async with sessions_fixture() as session:
        async with session.begin():
            (await session.get(OutboxEntry, oldest_id)).delivered_at = (
                datetime.now(timezone.utc)
            )
    assert (await claim_intent(sessions_fixture))[0] == later_id


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"plan_id": str(uuid4()), "approved": "false"},
    {"plan_id": str(uuid4()), "approved": 1},
    {"plan_id": "not-a-uuid", "approved": True},
    {"plan_id": str(uuid4()), "approved": True, "override": "yes"},
    {"approved": True},
    None,
])
async def test_redteam_poisoned_approval_outbox_cannot_signal_temporal(payload):
    from backend.workers.dispatcher import deliver_intent

    client = AsyncMock()
    intent = (1, "tenant", str(uuid4()), "PLAN_DECIDED", payload)
    with pytest.raises(ValueError):
        await deliver_intent(client, intent)
    client.get_workflow_handle.assert_not_called()


@pytest.mark.asyncio
async def test_redteam_older_dispatcher_cannot_ack_a_newer_delivery_claim(
    sessions_fixture,
):
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    sessions = sessions_fixture
    async with sessions() as session:
        incident = await admit_incident(
            session, "tenant", "stale-ack", IncidentCreate(target_id="synthetic"),
        )
    async with sessions() as session:
        async with session.begin():
            start = await session.scalar(select(OutboxEntry).where(
                OutboxEntry.incident_id == incident.id,
                OutboxEntry.kind == "START_WORKFLOW",
            ))
            start.delivered_at = datetime.now(timezone.utc)
            followup = OutboxEntry(
                incident_id=incident.id, kind="ACKNOWLEDGE_REVIEW",
            )
            session.add(followup)
            await session.flush()
            followup_id = followup.id

    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_delivery(*args, **kwargs):
        started.set()
        await release.wait()

    with patch(
        "backend.workers.dispatcher.deliver_intent",
        side_effect=delayed_delivery,
    ):
        older_task = asyncio.create_task(dispatch_once(AsyncMock(), sessions))
        await asyncio.wait_for(started.wait(), timeout=5)
        async with sessions() as session:
            async with session.begin():
                row = await session.get(OutboxEntry, followup_id)
                assert row.attempt == 1
                row.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        newer_claim = await claim_intent(sessions)
        assert newer_claim is not None
        assert newer_claim[0] == followup_id and newer_claim[5] == 2
        release.set()
        assert await asyncio.wait_for(older_task, timeout=5)

    async with sessions() as session:
        row = await session.get(OutboxEntry, followup_id)
        assert row.attempt == 2
        assert row.delivered_at is None


@pytest.mark.asyncio
async def test_delivery_errors_never_log_provider_or_payload_secrets(
    sessions_fixture, caplog,
):
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    async with sessions_fixture() as session:
        await admit_incident(
            session, "tenant", "log-redaction",
            IncidentCreate(target_id="private-service"),
        )
    with patch(
        "backend.workers.dispatcher.deliver_intent",
        new_callable=AsyncMock,
        side_effect=ConnectionError("PRIVATE_PROVIDER_TOKEN=do-not-log"),
    ), caplog.at_level("WARNING", logger="backend.workers.dispatcher"):
        assert await dispatch_once(AsyncMock(), sessions_fixture)
    assert "do-not-log" not in caplog.text
    assert "PRIVATE_PROVIDER_TOKEN" not in caplog.text
    assert "ConnectionError" in caplog.text
    async with sessions_fixture() as session:
        row = await session.scalar(select(OutboxEntry))
        assert row.delivered_at is None and row.attempt == 1
