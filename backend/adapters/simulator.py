"""Conformant local-only simulator. It never touches real infrastructure."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import (
    ResourceEpoch, ResourceReservation, SimulatorAppliedEffect, SimulatorTarget
)


class StaleFence(Exception):
    pass


class TargetVersionConflict(Exception):
    pass


@dataclass(frozen=True)
class Permit:
    tenant_id: str
    target_id: str
    incident_id: UUID
    action_digest: str
    fence: int
    expected_version: int
    expires_at: datetime


def utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class SimulatorAdapter:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions

    async def apply(self, effect_id: UUID, permit: Permit) -> int:
        """Check fence + target version immediately before the simulated effect."""
        async with self.sessions() as session:
            async with session.begin():
                prior = await session.get(SimulatorAppliedEffect, effect_id)
                if prior is not None:
                    if prior.tenant_id != permit.tenant_id or prior.target_id != permit.target_id:
                        raise ValueError("effect ID used for another target")
                    return prior.observed_version

                key = (permit.tenant_id, permit.target_id)
                epoch = await session.get(ResourceEpoch, key, with_for_update=True)
                reservation = await session.get(ResourceReservation, key, with_for_update=True)
                target = await session.get(SimulatorTarget, key, with_for_update=True)
                if epoch is None or reservation is None or target is None:
                    raise StaleFence("target or reservation does not exist")
                if (
                    epoch.latest_fence != permit.fence
                    or reservation.fence != permit.fence
                    or reservation.holder_incident != permit.incident_id
                    or reservation.action_digest != permit.action_digest
                    or reservation.state != "RESERVED"
                    or datetime.now(timezone.utc) >= utc(reservation.expires_at)
                    or datetime.now(timezone.utc) >= utc(permit.expires_at)
                ):
                    raise StaleFence("permit is stale or expired")
                if target.version != permit.expected_version:
                    raise TargetVersionConflict("target version changed")
                target.healthy = True
                target.version += 1
                target.last_fence = permit.fence
                epoch.version_seen = target.version
                session.add(
                    SimulatorAppliedEffect(
                        id=effect_id,
                        tenant_id=permit.tenant_id,
                        target_id=permit.target_id,
                        observed_version=target.version,
                    )
                )
                reservation.state = "RELEASED"
                return target.version

    async def query_effect(self, effect_id: UUID) -> int | None:
        async with self.sessions() as session:
            row = await session.get(SimulatorAppliedEffect, effect_id)
            return None if row is None else row.observed_version

    async def observe(self, tenant_id: str, target_id: str) -> tuple[int, bool] | None:
        async with self.sessions() as session:
            target = await session.get(SimulatorTarget, (tenant_id, target_id))
            return None if target is None else (target.version, target.healthy)
