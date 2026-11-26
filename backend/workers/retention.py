"""E1 idempotent expiry scrub. Keep minimal audit IDs, not expired content."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database import get_session_maker
from backend.models import (
    Evidence, ExperienceCapsule, OperationalLesson, TenantExperienceConsent,
)
from backend.probes.gateway import EVIDENCE_RETENTION_DAYS, EXPIRED_EXCERPT


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


async def expire_evidence_payloads(
    sessions: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
    *,
    default_ttl_days: int = EVIDENCE_RETENTION_DAYS,
) -> dict[str, int]:
    """A3-08: scrub expired evidence excerpts while preserving authoritative history IDs."""
    if default_ttl_days < 1:
        raise ValueError("evidence retention TTL must be at least 1 day")
    cutoff = now or datetime.now(timezone.utc)
    if cutoff.tzinfo is None:
        raise ValueError("expiry cutoff must have timezone")
    fallback_cutoff = cutoff - timedelta(days=default_ttl_days)
    async with sessions() as session:
        async with session.begin():
            ids = list((await session.scalars(
                select(Evidence.id)
                .where(
                    Evidence.validity != "EXPIRED",
                    or_(
                        and_(
                            Evidence.expires_at.is_not(None),
                            Evidence.expires_at <= cutoff,
                        ),
                        and_(
                            Evidence.expires_at.is_(None),
                            Evidence.captured_at <= fallback_cutoff,
                        ),
                    ),
                )
                .order_by(Evidence.id)
                .limit(500)
            )).all())
            if not ids:
                return {"evidence_scrubbed": 0}
            await session.execute(
                update(Evidence)
                .where(Evidence.id.in_(ids))
                .values(
                    validity="EXPIRED",
                    redacted_excerpt=EXPIRED_EXCERPT,
                    expires_at=cutoff,
                )
            )
            return {"evidence_scrubbed": len(ids)}


async def main() -> None:
    sessions = get_session_maker()
    capsules = await expire_capsules(sessions)
    evidence = await expire_evidence_payloads(sessions)
    print(json.dumps({**capsules, **evidence}))


if __name__ == "__main__":
    asyncio.run(main())
