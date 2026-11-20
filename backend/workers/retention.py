"""E1 idempotent expiry scrub. Keep minimal audit IDs, not expired content."""

import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database import get_session_maker
from backend.models import ExperienceCapsule, OperationalLesson


async def expire_capsules(
    sessions: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
) -> dict[str, int]:
    cutoff = now or datetime.now(timezone.utc)
    if cutoff.tzinfo is None:
        raise ValueError("expiry cutoff must have timezone")
    async with sessions() as session:
        async with session.begin():
            ids = list((await session.scalars(
                select(ExperienceCapsule.id)
                .where(
                    ExperienceCapsule.expires_at <= cutoff,
                    ExperienceCapsule.status.not_in(("EXPIRED", "REVOKED")),
                )
                .limit(500)
            )).all())
            if not ids:
                return {"capsules_scrubbed": 0, "lessons_revoked": 0}
            await session.execute(
                update(ExperienceCapsule)
                .where(ExperienceCapsule.id.in_(ids))
                .values(status="EXPIRED", target_digest="", evidence_ids=[])
            )
            result = await session.execute(
                update(OperationalLesson)
                .where(
                    OperationalLesson.source_capsule_id.in_(ids),
                    OperationalLesson.status != "REVOKED",
                )
                .values(
                    status="REVOKED",
                    target_digest="",
                    version=OperationalLesson.version + 1,
                )
            )
            return {
                "capsules_scrubbed": len(ids),
                "lessons_revoked": result.rowcount,
            }


async def main() -> None:
    print(json.dumps(await expire_capsules(get_session_maker())))


if __name__ == "__main__":
    asyncio.run(main())
