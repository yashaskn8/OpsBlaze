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
