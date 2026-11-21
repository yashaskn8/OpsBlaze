"""E1 idempotent expiry scrub. Keep minimal audit IDs, not expired content."""

import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database import get_session_maker
from backend.models import (
    ExperienceCapsule, OperationalLesson, TenantExperienceConsent,
)


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


async def scrub_revoked_tenant_capsules(
    sessions: async_sessionmaker[AsyncSession],
) -> dict[str, int]:
    """Bounded, idempotent scrub of capsules superseded by tenant consent."""
    stale = or_(
        and_(
            TenantExperienceConsent.tenant_id.is_(None),
            ExperienceCapsule.tenant_consent_epoch != 0,
        ),
        and_(
            TenantExperienceConsent.tenant_id.is_not(None),
            or_(
                TenantExperienceConsent.allowed.is_(False),
                ExperienceCapsule.tenant_consent_epoch
                != TenantExperienceConsent.epoch,
            ),
        ),
    )
    async with sessions() as session:
        async with session.begin():
            ids = list((await session.scalars(
                select(ExperienceCapsule.id)
                .outerjoin(
                    TenantExperienceConsent,
                    TenantExperienceConsent.tenant_id == ExperienceCapsule.tenant_id,
                )
                .where(ExperienceCapsule.status == "VALID", stale)
                .order_by(ExperienceCapsule.id)
                .limit(500)
                .with_for_update(of=ExperienceCapsule, skip_locked=True)
            )).all())
            if not ids:
                return {"capsules_scrubbed": 0, "lessons_revoked": 0}
            await session.execute(
                update(ExperienceCapsule).where(
                    ExperienceCapsule.id.in_(ids),
                ).values(
                    status="REVOKED", target_digest="", evidence_ids=[],
                )
            )
            result = await session.execute(
                update(OperationalLesson).where(
                    OperationalLesson.source_capsule_id.in_(ids),
                    OperationalLesson.status != "REVOKED",
                ).values(
                    status="REVOKED", target_digest="",
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
