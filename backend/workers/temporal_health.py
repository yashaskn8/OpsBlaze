"""Bounded live Temporal service probe for production readiness.

A heartbeat proves process liveness, not workflow-server reachability.
Neither network connect nor health RPC occurs inside a DB transaction.
"""

import asyncio
from datetime import timedelta

from backend.settings import Settings
from backend.temporal_connection import connect_temporal


async def check_temporal_health(settings: Settings) -> bool:
    client = await asyncio.wait_for(
        connect_temporal(
            settings, connect_timeout=timedelta(seconds=2),
        ),
        timeout=3,
    )
    return await asyncio.wait_for(
        client.service_client.check_health(), timeout=2,
    )
