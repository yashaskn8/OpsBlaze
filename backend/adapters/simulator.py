"""Conformant local-only simulator. It never touches real infrastructure."""

from dataclasses import dataclass
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import (
    EffectReceipt, ResourceEpoch, ResourceReservation, SimulatorAppliedEffect,
    SimulatorTarget,
)


class StaleFence(Exception):
    pass


class TargetVersionConflict(Exception):
    pass


class EffectIdentityConflict(Exception):
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


def observed_healthy_at_version(
    observation: object, expected_version: int | None,
) -> bool:
    """Never accept Boolean/float coercion as an independent version witness."""
    return (
        type(expected_version) is int
        and isinstance(observation, tuple)
        and len(observation) == 2
        and type(observation[0]) is int
        and type(observation[1]) is bool
        and observation[0] == expected_version
        and observation[1] is True
    )


def permit_digest(permit: Permit) -> str:
    """Bind a reused effect identity to its exact original permit."""
    data = {
        "tenant": permit.tenant_id,
        "target": permit.target_id,
        "incident": str(permit.incident_id),
        "action": permit.action_digest,
        "fence": permit.fence,
        "expected_version": permit.expected_version,
        "expires_at": utc(permit.expires_at).astimezone(timezone.utc).isoformat(),
    }
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class SimulatorAdapter:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions

    async def apply(self, effect_id: UUID, permit: Permit) -> int:
        """Check fence + target version immediately before the simulated effect."""
        async with self.sessions() as session:
            async with session.begin():
                prior = await session.get(SimulatorAppliedEffect, effect_id)
                digest = permit_digest(permit)
                if prior is not None:
                    if (
                        prior.tenant_id != permit.tenant_id
                        or prior.target_id != permit.target_id
                        or prior.permit_digest != digest
                    ):
                        raise EffectIdentityConflict(
                            "effect ID does not match its original permit"
                        )
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
                if (
                    reservation.expected_version != permit.expected_version
                    or target.version != reservation.expected_version
                ):
                    raise TargetVersionConflict("reserved target version changed")
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
                        permit_digest=digest,
                    )
                )
                reservation.state = "RELEASED"
                return target.version

    async def query_effect(self, effect_id: UUID) -> int | None:
        async with self.sessions() as session:
            row = await session.get(SimulatorAppliedEffect, effect_id)
            if row is None:
                return None
            # Direct disposable conformance checks have no receipt. For
            # gateway-managed effects the exact original permit digest is
            # required; a matching version or effect ID is not sufficient.
            receipt = await session.get(EffectReceipt, effect_id)
            if receipt is not None and (
                receipt.adapter_id != "simulator"
                or receipt.tenant_id != row.tenant_id
                or receipt.target_id != row.target_id
                or receipt.permit_digest is None
                or receipt.permit_digest != row.permit_digest
                or row.observed_version != receipt.expected_version + 1
            ):
                raise EffectIdentityConflict(
                    "stored effect does not match durable permit binding"
                )
            return row.observed_version

    async def observe(self, tenant_id: str, target_id: str) -> tuple[int, bool] | None:
        async with self.sessions() as session:
            target = await session.get(SimulatorTarget, (tenant_id, target_id))
            return None if target is None else (target.version, target.healthy)
