"""Bounded P1 notification expiry with persistent per-incident gap cursors."""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database import get_session_maker
from backend.models import UIEvent, UIEventWatermark
from backend.settings import get_settings


async def purge_ui_events(
    sessions: async_sessionmaker[AsyncSession],
    cutoff: datetime,
    batch_size: int = 500,
) -> int:
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be 1–1000")
    async with sessions() as session:
        async with session.begin():
            rows = (await session.scalars(
                select(UIEvent)
                .where(UIEvent.created_at < cutoff)
                .order_by(UIEvent.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )).all()
            if not rows:
                return 0

            latest: dict[tuple[str, object], int] = {}
            for item in rows:
                key = (item.tenant_id, item.incident_id)
                latest[key] = max(latest.get(key, 0), item.id)

            dialect = session.bind.dialect.name
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
                maximum = func.greatest
            elif dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert
                maximum = func.max
            else:
                raise RuntimeError("unsupported retention database")

            for (tenant_id, incident_id), event_id in latest.items():
                stmt = insert(UIEventWatermark).values(
                    tenant_id=tenant_id,
                    incident_id=incident_id,
                    last_purged_id=event_id,
                )
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["tenant_id", "incident_id"],
                        set_={
                            "last_purged_id": maximum(
                                UIEventWatermark.last_purged_id,
                                stmt.excluded.last_purged_id,
                            ),
                        },
                    )
                )
            await session.execute(
                delete(UIEvent).where(UIEvent.id.in_([item.id for item in rows]))
            )
            return len(rows)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Trim old UI notification events")
    parser.add_argument("--days", type=int, default=None)
    args = parser.parse_args()
    days = args.days if args.days is not None else get_settings().ui_event_retention_days
    if not 1 <= days <= 365:
        parser.error("retention days must be 1–365")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    total = 0
    while True:
        count = await purge_ui_events(get_session_maker(), cutoff)
        total += count
        if count < 500:
            break
    print(f"UI notification events deleted: {total}")


if __name__ == "__main__":
    asyncio.run(main())
