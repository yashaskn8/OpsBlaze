"""P1 bounded SSE parser and tenant-isolated replay."""

import pytest

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


@pytest.mark.asyncio
async def test_redteam_oversized_sse_and_poll_cursors_are_rejected(sessions_fixture):
    from httpx import ASGITransport, AsyncClient

    from backend.api import app
    from backend.database import get_session
    from backend.settings import Settings, get_settings

    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "alpha", "cursor-bounds",
            IncidentCreate(target_id="checkout"),
        )

    async def override_session():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(
        local_token="cursor-test-token", review_token="distinct-reviewer-token",
    )
    headers = {"X-Tenant-Id": "alpha", "X-OpsBlaze-Token": "cursor-test-token"}
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            events_url = f"/v1/incidents/{incident.id}/events"
            stream_url = f"/v1/incidents/{incident.id}/stream"
            for cursor in ("-1", "9223372036854775808", "9" * 80):
                event_response = await client.get(
                    events_url, params={"after_id": cursor}, headers=headers,
                )
                assert event_response.status_code == 422
                stream_response = await client.get(
                    stream_url, params={"after_id": cursor}, headers=headers,
                )
                assert stream_response.status_code in {400, 422}
            for header in ("9" * 80, "9223372036854775808", "not-a-number"):
                response = await client.get(
                    stream_url, headers={**headers, "Last-Event-ID": header},
                )
                assert response.status_code == 400
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_redteam_sse_expires_between_frames_even_inside_one_event_batch(
    sessions_fixture, monkeypatch,
):
    """A valid login must not grant indefinite streaming after expiry."""
    from backend.models import UIEvent

    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "alpha", "expiry-stream",
            IncidentCreate(target_id="checkout"),
        )
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(UIEvent(
                tenant_id="alpha", incident_id=incident.id,
                kind="INVESTIGATING", projection_version=2,
            ))

    clock = [1000.0]
    monkeypatch.setattr("backend.events.time.time", lambda: clock[0])

    async def connected():
        return False

    stream = stream_incident_events(
        sessions_fixture, "alpha", incident.id, 0, connected,
        poll_seconds=0, max_ticks=2, token_expires_at=1001,
    )
    first = await stream.__anext__()
    assert '"kind":"ADMITTED"' in first

    clock[0] = 1001.0
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()

    already_expired = stream_incident_events(
        sessions_fixture, "alpha", incident.id, 0, connected,
        poll_seconds=0, max_ticks=1, token_expires_at=1000,
    )
    with pytest.raises(StopAsyncIteration):
        await already_expired.__anext__()


@pytest.mark.asyncio
async def test_redteam_revocation_blocks_second_prefetched_sse_frame(
    sessions_fixture,
):
    from datetime import datetime, timezone
    from backend.models import RevokedIdentity

    actor = "oidc:" + "8" * 32
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "alpha", "stream-revoke-middle",
            IncidentCreate(target_id="checkout"),
        )
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(UIEvent(
                tenant_id="alpha", incident_id=incident.id,
                kind="FOLLOW_UP", projection_version=2,
            ))

    async def connected():
        return False

    stream = stream_incident_events(
        sessions_fixture, "alpha", incident.id, 0, connected,
        poll_seconds=0, max_ticks=1, actor_id=actor,
    )
    first = await stream.__anext__()
    assert '"kind":"ADMITTED"' in first
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(RevokedIdentity(
                tenant_id="alpha", actor_id=actor,
                revoked_at=datetime.now(timezone.utc),
                revoked_by="oidc:" + "9" * 32,
                reason="mid-batch revocation",
            ))
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()


@pytest.mark.asyncio
async def test_redteam_sse_denylist_outage_never_fails_open(
    sessions_fixture, monkeypatch,
):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "alpha", "revoke-store-offline",
            IncidentCreate(target_id="checkout"),
        )
    async def unavailable(*args, **kwargs):
        raise ConnectionError("revocation storage unavailable")

    async def connected():
        return False

    monkeypatch.setattr(
        "backend.events.is_stream_identity_revoked", unavailable,
    )
    stream = stream_incident_events(
        sessions_fixture, "alpha", incident.id, 0, connected,
        poll_seconds=0, max_ticks=1, actor_id="oidc:" + "a" * 32,
    )
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
