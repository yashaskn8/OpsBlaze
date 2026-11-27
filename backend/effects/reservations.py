"""B2: allocate an epoch-fenced permit in a short database transaction."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.simulator import Permit, utc
from backend.models import ResourceEpoch, ResourceReservation, SimulatorTarget


class ReservationBusy(Exception):
    pass


async def _bound_reservation_lock_wait(session: AsyncSession) -> None:
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text("SET LOCAL lock_timeout = '1s'"))


def _is_lock_contention(exc: DBAPIError) -> bool:
    return getattr(exc.orig, "sqlstate", None) == "55P03" or (
        isinstance(exc, OperationalError) and "locked" in str(exc.orig).lower()
    )


def _project_reservation(
    reservation: ResourceReservation,
    epoch: ResourceEpoch | None,
    observed: datetime,
) -> dict:
    expires = utc(reservation.expires_at)
    expired = reservation.state == "RESERVED" and expires <= observed
    effective_state = "EXPIRED" if expired else reservation.state
    remaining = (
        max(0, int((expires - observed).total_seconds()))
        if effective_state == "RESERVED"
        else 0
    )
    return {
        "target_id": reservation.target_id,
        "holder_incident_id": str(reservation.holder_incident),
        "action_digest": reservation.action_digest,
        "fence": reservation.fence,
        "expected_version": reservation.expected_version,
        "stored_state": reservation.state,
        "effective_state": effective_state,
        "expired": expired,
        "expires_at": expires.astimezone(timezone.utc).isoformat(),
        "lease_remaining_seconds": remaining,
        "latest_fence": epoch.latest_fence if epoch is not None else reservation.fence,
        "version_seen": epoch.version_seen if epoch is not None else None,
    }


async def release_simulator_reservation(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    target_id: str,
    incident_id: UUID,
    fence: int,
) -> bool:
    """Explicitly release an active lease without weakening monotonic fencing."""
    async with sessions() as session:
        async with session.begin():
            reservation = await session.get(
                ResourceReservation, (tenant_id, target_id), with_for_update=True,
            )
            if (
                reservation is None
                or reservation.state != "RESERVED"
                or reservation.holder_incident != incident_id
                or reservation.fence != fence
            ):
                return False
            reservation.state = "RELEASED"
            return True


async def expire_stale_reservations(
    sessions: async_sessionmaker[AsyncSession],
    *,
    limit: int = 50,
    now: datetime | None = None,
) -> int:
    """Bounded durable projection of expired leases into EXPIRED without altering fences."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("reservation expiry batch must be 1–50")
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    async with sessions() as session:
        async with session.begin():
            rows = (await session.scalars(
                select(ResourceReservation)
                .where(
                    ResourceReservation.state == "RESERVED",
                    ResourceReservation.expires_at <= observed,
                )
                .order_by(ResourceReservation.expires_at, ResourceReservation.target_id)
                .limit(limit)
                .with_for_update()
            )).all()
            for row in rows:
                row.state = "EXPIRED"
            return len(rows)


async def read_reservation_status(
    session: AsyncSession,
    tenant_id: str,
    target_id: str,
    *,
    now: datetime | None = None,
) -> dict | None:
    """B2-05: tenant-scoped read-only reservation status with explicit expiry projection."""
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    key = (tenant_id, target_id)
    reservation = await session.get(ResourceReservation, key)
    if reservation is None:
        return None
    epoch = await session.get(ResourceEpoch, key)
    return _project_reservation(reservation, epoch, observed)


async def list_reservations(
    session: AsyncSession,
    tenant_id: str,
    *,
    cursor: str | None = None,
    limit: int = 50,
    now: datetime | None = None,
) -> dict:
    """Bounded, tenant-scoped reservation list ordered by target_id."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("reservation page size must be 1–50")
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stmt = select(ResourceReservation).where(
        ResourceReservation.tenant_id == tenant_id,
    )
    if cursor is not None:
        stmt = stmt.where(ResourceReservation.target_id > cursor)
    rows = list((await session.scalars(
        stmt.order_by(ResourceReservation.target_id).limit(limit + 1)
    )).all())
    visible = rows[:limit]
    targets = [row.target_id for row in visible]
    epochs = {
        row.target_id: row
        for row in (await session.scalars(
            select(ResourceEpoch).where(
                ResourceEpoch.tenant_id == tenant_id,
                ResourceEpoch.target_id.in_(targets),
            )
        )).all()
    } if targets else {}
    return {
        "reservations": [
            _project_reservation(row, epochs.get(row.target_id), observed)
            for row in visible
        ],
        "next_cursor": visible[-1].target_id if len(rows) > limit else None,
        "observed_at": observed.isoformat(),
    }


async def reserve_simulator_target(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    target_id: str,
    incident_id: UUID,
    action_digest: str,
    lease_seconds: int = 60,
) -> Permit:
    if not 1 <= lease_seconds <= 600:
        raise ValueError("lease must be between 1 and 600 seconds")
    try:
        async with sessions() as session:
            async with session.begin():
                key = (tenant_id, target_id)
                await _bound_reservation_lock_wait(session)
                epoch = await session.get(ResourceEpoch, key, with_for_update=True)
                reservation = await session.get(ResourceReservation, key, with_for_update=True)
                target = await session.get(SimulatorTarget, key, with_for_update=True)
                if epoch is None or target is None:
                    raise ValueError("simulator target must be provisioned first")
                # Row-lock waits count against the incumbent lease.
                now = datetime.now(timezone.utc)
                if (
                    reservation is not None
                    and reservation.state == "RESERVED"
                    and utc(reservation.expires_at) > now
                ):
                    if (
                        reservation.holder_incident == incident_id
                        and reservation.action_digest == action_digest
                    ):
                        return Permit(
                            tenant_id=tenant_id,
                            target_id=target_id,
                            incident_id=incident_id,
                            action_digest=action_digest,
                            fence=reservation.fence,
                            expected_version=reservation.expected_version,
                            expires_at=utc(reservation.expires_at),
                        )
                    raise ReservationBusy("target is already reserved")
                epoch.latest_fence += 1
                expiration = now + timedelta(seconds=lease_seconds)
                if reservation is None:
                    reservation = ResourceReservation(
                        tenant_id=tenant_id,
                        target_id=target_id,
                        holder_incident=incident_id,
                        action_digest=action_digest,
                        fence=epoch.latest_fence,
                        expected_version=target.version,
                        expires_at=expiration,
                        state="RESERVED",
                    )
                    session.add(reservation)
                else:
                    reservation.holder_incident = incident_id
                    reservation.action_digest = action_digest
                    reservation.fence = epoch.latest_fence
                    reservation.expected_version = target.version
                    reservation.expires_at = expiration
                    reservation.state = "RESERVED"
                return Permit(
                    tenant_id=tenant_id,
                    target_id=target_id,
                    incident_id=incident_id,
                    action_digest=action_digest,
                    fence=epoch.latest_fence,
                    expected_version=target.version,
                    expires_at=expiration,
                )
    except IntegrityError as exc:
        raise ReservationBusy("target is already reserved") from exc
    except DBAPIError as exc:
        if _is_lock_contention(exc):
            raise ReservationBusy("target is already reserved") from exc
        raise


async def renew_simulator_reservation(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    target_id: str,
    incident_id: UUID,
    fence: int,
    lease_seconds: int = 60,
    *,
    now: datetime | None = None,
) -> Permit | None:
    """Extend an active reservation only for its current valid fenced holder."""
    if not 1 <= lease_seconds <= 600:
        raise ValueError("lease must be between 1 and 600 seconds")
    try:
        async with sessions() as session:
            async with session.begin():
                key = (tenant_id, target_id)
                await _bound_reservation_lock_wait(session)
                epoch = await session.get(ResourceEpoch, key, with_for_update=True)
                reservation = await session.get(ResourceReservation, key, with_for_update=True)
                target = await session.get(SimulatorTarget, key, with_for_update=True)
                observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
                if (
                    epoch is None
                    or reservation is None
                    or target is None
                    or epoch.latest_fence != fence
                    or reservation.fence != fence
                    or reservation.holder_incident != incident_id
                    or reservation.state != "RESERVED"
                    or utc(reservation.expires_at) <= observed
                ):
                    return None
                extended = max(
                    utc(reservation.expires_at),
                    observed + timedelta(seconds=lease_seconds),
                )
                reservation.expires_at = extended
                return Permit(
                    tenant_id=tenant_id,
                    target_id=target_id,
                    incident_id=incident_id,
                    action_digest=reservation.action_digest,
                    fence=reservation.fence,
                    expected_version=reservation.expected_version,
                    expires_at=extended,
                )
    except DBAPIError as exc:
        if _is_lock_contention(exc):
            raise ReservationBusy("target reservation lock contention") from exc
        raise
