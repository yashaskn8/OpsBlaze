"""Durable process-presence checks, not proof of Temporal health or HA."""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database import get_session_maker
from backend.models import WorkerHeartbeat, WorkerHostHeartbeat
from backend.settings import get_settings

log = logging.getLogger(__name__)
REQUIRED_ROLES = frozenset({"dispatcher", "incident_worker", "experience_projector"})
MULTIHOST_ROLES = frozenset({"dispatcher", "incident_worker"})
HOST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def record_heartbeat(
    sessions: async_sessionmaker[AsyncSession],
    role: str,
    now: datetime | None = None,
    *,
    host_id: str | None = None,
) -> None:
    if role not in REQUIRED_ROLES:
        raise ValueError("unsupported worker role")
    declared_host = (
        get_settings().worker_host_id
        if host_id is None and role in MULTIHOST_ROLES
        else (host_id or "")
    )
    # Do not accept arbitrary raw hostnames or ids from a caller into SQL.
    if declared_host and (
        role not in MULTIHOST_ROLES
        or HOST_ID_PATTERN.fullmatch(declared_host) is None
    ):
        raise ValueError("invalid or unsupported worker host identity")
    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise ValueError("heartbeat must have timezone")
    async with sessions() as session:
        async with session.begin():
            if session.bind.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            elif session.bind.dialect.name == "sqlite":
                from sqlalchemy.dialects.sqlite import insert
            else:
                raise RuntimeError("unsupported heartbeat database")
            stmt = insert(WorkerHeartbeat).values(
                role=role, last_seen=observed,
            ).on_conflict_do_update(
                index_elements=["role"],
                set_={"last_seen": observed},
                # A delayed heartbeat from another process must never
                # regress a healthy role's last-seen timestamp.
                where=WorkerHeartbeat.last_seen <= observed,
            )
            await session.execute(stmt)
            if declared_host:
                hosted = insert(WorkerHostHeartbeat).values(
                    role=role, host_id=declared_host, last_seen=observed,
                ).on_conflict_do_update(
                    index_elements=["role", "host_id"],
                    set_={"last_seen": observed},
                    where=WorkerHostHeartbeat.last_seen <= observed,
                )
                await session.execute(hosted)


async def present_worker_hosts(
    session: AsyncSession,
    now: datetime | None = None,
    max_age_seconds: int | None = None,
) -> dict[str, frozenset[str]]:
    """Recent self-reported host IDs per operational role (not HA proof)."""
    current = now or datetime.now(timezone.utc)
    max_age = (
        get_settings().worker_stale_seconds
        if max_age_seconds is None else max_age_seconds
    )
    if max_age < 1:
        raise ValueError("worker stale threshold must be positive")
    cutoff = current - timedelta(seconds=max_age)
    # Fetch only fresh rows; never scan every retired host identity.
    rows = (await session.execute(
        select(WorkerHostHeartbeat.role, WorkerHostHeartbeat.host_id).where(
            WorkerHostHeartbeat.role.in_(MULTIHOST_ROLES),
            WorkerHostHeartbeat.last_seen >= cutoff,
            WorkerHostHeartbeat.last_seen <= current,
        )
    )).all()
    return {
        role: frozenset(host for found, host in rows if found == role)
        for role in MULTIHOST_ROLES
    }


async def present_worker_host_counts(
    session: AsyncSession,
    now: datetime | None = None,
    max_age_seconds: int | None = None,
) -> dict[str, int]:
    """Count fresh reported hosts within the database (not HA proof)."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("host presence observation must be timezone-aware")
    max_age = (
        get_settings().worker_stale_seconds
        if max_age_seconds is None else max_age_seconds
    )
    if max_age < 1:
        raise ValueError("worker stale threshold must be positive")
    rows = (await session.execute(
        select(WorkerHostHeartbeat.role, func.count())
        .where(
            WorkerHostHeartbeat.role.in_(MULTIHOST_ROLES),
            WorkerHostHeartbeat.last_seen >= (
                current - timedelta(seconds=max_age)
            ),
            WorkerHostHeartbeat.last_seen <= current,
        )
        .group_by(WorkerHostHeartbeat.role)
    )).all()
    counts = {role: 0 for role in MULTIHOST_ROLES}
    counts.update({role: int(count) for role, count in rows})
    return counts


async def prune_worker_host_presence(
    sessions: async_sessionmaker[AsyncSession],
    cutoff: datetime,
    batch_size: int = 500,
) -> int:
    """Prune only ephemeral stale presence with bounded work."""
    if cutoff.tzinfo is None:
        raise ValueError("host-presence cutoff must include timezone")
    if not 1 <= batch_size <= 1000:
        raise ValueError("host-presence batch size must be 1–1000")
    async with sessions() as session:
        async with session.begin():
            keys = (await session.execute(
                select(
                    WorkerHostHeartbeat.role, WorkerHostHeartbeat.host_id,
                )
                .where(WorkerHostHeartbeat.last_seen < cutoff)
                .order_by(
                    WorkerHostHeartbeat.last_seen,
                    WorkerHostHeartbeat.role,
                    WorkerHostHeartbeat.host_id,
                )
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )).all()
            if not keys:
                return 0
            deleted = await session.execute(
                delete(WorkerHostHeartbeat).where(
                    tuple_(
                        WorkerHostHeartbeat.role,
                        WorkerHostHeartbeat.host_id,
                    ).in_([(role, host) for role, host in keys]),
                    # Never delete a row refreshed since the selection.
                    WorkerHostHeartbeat.last_seen < cutoff,
                )
            )
            return max(0, int(deleted.rowcount or 0))


async def present_workers(
    session: AsyncSession,
    now: datetime | None = None,
    max_age_seconds: int | None = None,
) -> frozenset[str]:
    cutoff_time = now or datetime.now(timezone.utc)
    max_age = (
        get_settings().worker_stale_seconds
        if max_age_seconds is None else max_age_seconds
    )
    if max_age < 1:
        raise ValueError("worker stale threshold must be positive")
    rows = (await session.scalars(select(WorkerHeartbeat))).all()
    return frozenset(
        row.role for row in rows
        if row.role in REQUIRED_ROLES
        and timedelta(0) <= cutoff_time - utc(row.last_seen) <= timedelta(seconds=max_age)
    )


async def heartbeat_loop(
    role: str, sessions: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    if role not in REQUIRED_ROLES:
        raise ValueError("unsupported worker role")
    while True:
        try:
            await record_heartbeat(
                sessions if sessions is not None else get_session_maker(), role,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never claim healthy if the store cannot accept heartbeat.
            # DB exceptions can embed connection strings or SQL values.
            log.warning("worker heartbeat persistence failed: role=%s", role)
        await asyncio.sleep(max(1, get_settings().worker_heartbeat_seconds))
