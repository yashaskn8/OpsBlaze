
"""Provision a local simulator fixture without granting a public mutation API."""

import argparse
import asyncio
import re

from backend.database import get_session_maker
from backend.models import ResourceEpoch, SimulatorTarget
from backend.settings import get_settings


async def seed(tenant_id: str, target_id: str) -> None:
    if get_settings().environment != "development":
        raise RuntimeError("simulator seeding disabled outside development")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", tenant_id):
        raise ValueError("invalid tenant")
    if not target_id or len(target_id) > 256:
        raise ValueError("invalid target")
    async with get_session_maker()() as session:
        async with session.begin():
            key = (tenant_id, target_id)
            if await session.get(SimulatorTarget, key) is not None:
                raise ValueError("fixture exists; refusing to overwrite it")
            session.add(
                SimulatorTarget(
                    tenant_id=tenant_id, target_id=target_id,
                    healthy=False, version=1, last_fence=0,
                )
            )
            session.add(
                ResourceEpoch(
                    tenant_id=tenant_id, target_id=target_id,
                    latest_fence=0, version_seen=1,
                )
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed one unsafe-to-mutate local simulator target")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    asyncio.run(seed(args.tenant, args.target))
