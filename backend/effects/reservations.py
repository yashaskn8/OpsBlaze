"""B2: allocate an epoch-fenced permit in a short database transaction."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.simulator import Permit, utc
from backend.models import ResourceEpoch, ResourceReservation, SimulatorTarget


class ReservationBusy(Exception):
    pass


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
    now = datetime.now(timezone.utc)
    async with sessions() as session:
        async with session.begin():
            key = (tenant_id, target_id)
            epoch = await session.get(ResourceEpoch, key, with_for_update=True)
            target = await session.get(SimulatorTarget, key, with_for_update=True)
            if epoch is None or target is None:
                raise ValueError("simulator target must be provisioned first")
            reservation = await session.get(ResourceReservation, key, with_for_update=True)
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
