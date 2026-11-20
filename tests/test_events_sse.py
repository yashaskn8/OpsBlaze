"""P1 bounded SSE parser and tenant-isolated replay."""

from uuid import UUID

import pytest
from sqlalchemy import select

from backend.events import encode_sse, read_event_batch, stream_incident_events
from backend.models import UIEvent
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.mark.asyncio
async def test_sse_resumes_in_order_without_cross_tenant_records(sessions_fixture):
    async with sessions_fixture() as session:
        first = await admit_incident(
            session, "alpha", "stream-a", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        await admit_incident(
            session, "beta", "stream-b", IncidentCreate(target_id="checkout")
        )

    items = await read_event_batch(sessions_fixture, "alpha", first.id, 0)
    assert len(items) == 1 and items[0].kind == "ADMITTED"
    assert await read_event_batch(sessions_fixture, "beta", first.id, 0) == []
    payload = encode_sse(items[0])
    assert payload.startswith(f"id: {items[0].id}\nevent: incident\ndata: ")
    assert '"kind":"ADMITTED"' in payload
    assert await read_event_batch(sessions_fixture, "alpha", first.id, items[0].id) == []


@pytest.mark.asyncio
async def test_stream_yields_bounded_frames_and_respects_disconnect(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "alpha", "stream", IncidentCreate(target_id="checkout")
        )

    async def connected() -> bool:
        return False

    iterator = stream_incident_events(
        sessions_fixture, "alpha", incident.id, 0,
        connected, poll_seconds=0, max_ticks=1,
    )
    first = await iterator.__anext__()
    assert "event: incident" in first and '"kind":"ADMITTED"' in first
    with pytest.raises(StopAsyncIteration):
        await iterator.__anext__()

    async def disconnected() -> bool:
        return True

    iterator = stream_incident_events(
        sessions_fixture, "alpha", incident.id, 0,
        disconnected, poll_seconds=0,
    )
    with pytest.raises(StopAsyncIteration):
        await iterator.__anext__()


def test_per_process_stream_capacity_is_bounded_and_released():
    from backend.events import StreamCapacity

    cap = StreamCapacity(maximum=1)
    assert cap.claim()
    assert cap.active == 1
    assert not cap.claim()
    cap.release()
    assert cap.active == 0
    assert cap.claim()
    cap.release()
    with pytest.raises(RuntimeError):
        cap.release()


@pytest.mark.asyncio
async def test_retention_watermark_requires_snapshot_and_preserves_other_tenants(sessions_fixture):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import update
    from backend.events import ExpiredCursor, read_event_batch
    from backend.models import UIEventWatermark
    from backend.workers.event_retention import purge_ui_events

    now = datetime.now(timezone.utc)
    async with sessions_fixture() as session:
        first = await admit_incident(
            session, "alpha", "old-ui", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        second = await admit_incident(
            session, "beta", "fresh-ui", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        async with session.begin():
            await session.execute(
                update(UIEvent)
                .where(UIEvent.incident_id == first.id)
                .values(created_at=now - timedelta(days=10))
            )
    deleted = await purge_ui_events(
        sessions_fixture, now - timedelta(days=7),
    )
    assert deleted == 1
    assert await purge_ui_events(
        sessions_fixture, now - timedelta(days=7),
    ) == 0

    async with sessions_fixture() as session:
        watermark = await session.get(
            UIEventWatermark, ("alpha", first.id)
        )
        assert watermark.last_purged_id > 0
    with pytest.raises(ExpiredCursor) as error:
        await read_event_batch(sessions_fixture, "alpha", first.id, 0)
    assert error.value.min_after_id == watermark.last_purged_id
    assert await read_event_batch(
        sessions_fixture, "alpha", first.id, watermark.last_purged_id
    ) == []
    assert len(await read_event_batch(sessions_fixture, "beta", second.id, 0)) == 1

    async def connected():
        return False

    stream = stream_incident_events(
        sessions_fixture, "alpha", first.id, 0, connected,
        poll_seconds=0, max_ticks=1,
    )
    frame = await stream.__anext__()
    assert "event: gap" in frame
    assert str(watermark.last_purged_id) in frame


@pytest.mark.asyncio
async def test_redteam_revocation_terminates_open_sse_before_next_event(sessions_fixture):
    from datetime import datetime, timezone

    from backend.events import StreamAccessRevoked
    from backend.models import RevokedIdentity

    actor = "oidc:" + "b" * 32
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "alpha", "stream-revoked",
            IncidentCreate(target_id="checkout"),
        )

    async def connected():
        return False

    stream = stream_incident_events(
        sessions_fixture, "alpha", incident.id, 0, connected,
        poll_seconds=0, max_ticks=3, actor_id=actor,
    )
    frame = await stream.__anext__()
    assert "ADMITTED" in frame

    async with sessions_fixture() as session:
        async with session.begin():
            session.add(RevokedIdentity(
                tenant_id="alpha", actor_id=actor,
                revoked_at=datetime.now(timezone.utc),
                revoked_by="oidc:" + "c" * 32,
                reason="security authority revoked this active stream",
            ))
    with pytest.raises(StreamAccessRevoked):
        await read_event_batch(
            sessions_fixture, "alpha", incident.id, 0, actor_id=actor,
        )
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
    # An unrelated tenant's streaming identity remains independently scoped.
    assert len(await read_event_batch(
        sessions_fixture, "alpha", incident.id, 0,
        actor_id="oidc:" + "d" * 32,
    )) == 1
