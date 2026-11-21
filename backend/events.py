"""P1 tenant-scoped SSE notifications with bounded replay.

Notification data is advisory: clients must refetch the versioned snapshot.
Each tick owns a fresh database session; streaming never holds a transaction.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID
from threading import Lock


class StreamCapacity:
    """Per-process stream cap; a durable cursor survives rejected reconnects."""

    def __init__(self, maximum: int = 32):
        if maximum < 1:
            raise ValueError("stream limit must be positive")
        self.maximum = maximum
        self.active = 0
        self._lock = Lock()

    def claim(self) -> bool:
        with self._lock:
            if self.active >= self.maximum:
                return False
            self.active += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self.active < 1:
                raise RuntimeError("stream capacity over-released")
            self.active -= 1


STREAM_CAPACITY = StreamCapacity()


from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import RevokedIdentity, UIEvent, UIEventWatermark


class StreamAccessRevoked(Exception):
    """Stop an established stream when its bearer identity is revoked."""


class ExpiredCursor(Exception):
    def __init__(self, min_after_id: int) -> None:
        self.min_after_id = min_after_id
        super().__init__("notification cursor requires snapshot resync")


async def validate_event_cursor(
    session: AsyncSession, tenant_id: str, incident_id: UUID, after_id: int,
) -> None:
    watermark = await session.get(
        UIEventWatermark, (tenant_id, incident_id)
    )
    if watermark is not None and after_id < watermark.last_purged_id:
        raise ExpiredCursor(watermark.last_purged_id)


async def read_event_batch(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    incident_id: UUID,
    after_id: int,
    *,
    actor_id: str | None = None,
) -> list[UIEvent]:
    async with sessions() as session:
        if actor_id is not None and await session.get(
            RevokedIdentity, (tenant_id, actor_id)
        ) is not None:
            raise StreamAccessRevoked()
        await validate_event_cursor(session, tenant_id, incident_id, after_id)
        return list((await session.scalars(
            select(UIEvent)
            .where(
                UIEvent.tenant_id == tenant_id,
                UIEvent.incident_id == incident_id,
                UIEvent.id > after_id,
            )
            .order_by(UIEvent.id)
            .limit(100)
        )).all())


async def is_stream_identity_revoked(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str, actor_id: str,
) -> bool:
    # A batch can hold 100 prefetched events. Recheck between frames, not
    # just before the batch, so revocations do not leak its remaining rows.
    async with sessions() as session:
        return await session.get(
            RevokedIdentity, (tenant_id, actor_id),
        ) is not None


def encode_sse(event: UIEvent) -> str:
    body = json.dumps(
        {
            "id": event.id,
            "kind": event.kind,
            "projection_version": event.projection_version,
            "created_at": event.created_at.isoformat(),
        },
        separators=(",", ":"),
    )
    return f"id: {event.id}\nevent: incident\ndata: {body}\n\n"


async def stream_incident_events(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    incident_id: UUID,
    after_id: int,
    disconnected: Callable[[], Awaitable[bool]],
    poll_seconds: float = 2.0,
    max_ticks: int = 30,
    actor_id: str | None = None,
    token_expires_at: int | None = None,
) -> AsyncIterator[str]:
    """Bound each stream lifetime so a reconnect can recover missed events."""
    for _ in range(max_ticks):
        if token_expires_at is not None and time.time() >= token_expires_at:
            return
        if await disconnected():
            return
        try:
            rows = await read_event_batch(
                sessions, tenant_id, incident_id, after_id, actor_id=actor_id,
            )
        except StreamAccessRevoked:
            return
        except ExpiredCursor as expired:
            yield (
                "event: gap\n"
                + "data: " + json.dumps(
                    {"min_after_id": expired.min_after_id}
                ) + "\n\n"
            )
            return
        if rows:
            for event in rows:
                if token_expires_at is not None and time.time() >= token_expires_at:
                    return
                if actor_id is not None:
                    try:
                        if await is_stream_identity_revoked(
                            sessions, tenant_id, actor_id,
                        ):
                            return
                    except Exception:
                        # Failure of the deny-list must close, not fail open.
                        return
                if await disconnected():
                    return
                after_id = event.id
                yield encode_sse(event)
        else:
            yield ": heartbeat\n\n"
        await asyncio.sleep(poll_seconds)
