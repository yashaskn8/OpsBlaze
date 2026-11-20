"""B1 native Temporal health probe verifies actual Temporal connection."""

import pytest
from temporalio.testing import WorkflowEnvironment

from backend.settings import Settings
from backend.workers.temporal_health import check_temporal_health


@pytest.mark.integration
@pytest.mark.asyncio
async def test_native_temporal_health_rpc() -> None:
    async with await WorkflowEnvironment.start_local() as env:
        # The local test environment provides a server connection.
        target = env.client.service_client.config.target_host
        assert await check_temporal_health(
            Settings(temporal_address=target, temporal_namespace="default")
        )
