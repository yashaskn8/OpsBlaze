"""Run a persisted, Docker-free Temporal dev server (uses the temporalio-managed CLI binary).

State persists to TEMPORAL_DB_FILE so restart tests are possible. This is a local dev
server, not production durability evidence (AGENTS.md 12.2).
"""

import asyncio
from pathlib import Path

from temporalio.testing import WorkflowEnvironment

from backend.config import get_settings


async def main() -> None:
    s = get_settings()
    Path(s.temporal_db_file).parent.mkdir(parents=True, exist_ok=True)
    env = await WorkflowEnvironment.start_local(
        namespace=s.temporal_namespace,
        ip="127.0.0.1",
        port=int(s.temporal_host.rsplit(":", 1)[1]),
        dev_server_database_filename=s.temporal_db_file,
    )
    print(f"Temporal dev server on {s.temporal_host} (db: {s.temporal_db_file}); Ctrl+C to stop")
    try:
        await asyncio.Event().wait()
    finally:
        await env.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
