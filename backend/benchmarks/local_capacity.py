"""B4 bounded, Docker-free API latency profile with no network destination.

This is a CI regression probe, NOT a production throughput/HA benchmark.
The only transport is HTTPX ASGITransport bound to an in-process FastAPI app.
"""

import argparse
import asyncio
import json
import math
import tempfile
from pathlib import Path
from time import perf_counter
from unittest.mock import patch
from collections.abc import Awaitable, Callable

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api import app
from backend.database import get_session
from backend.models import Base, Incident, OutboxEntry
from backend.settings import Settings, get_settings


def nearest_rank_ms(samples: list[float], percentile: float) -> float:
    if not samples or not 0 < percentile <= 1:
        raise ValueError("percentiles need nonempty bounded samples")
    ordered = sorted(samples)
    return round(ordered[math.ceil(len(ordered) * percentile) - 1], 3)


def describe(samples: list[float]) -> dict[str, float | int]:
    return {
        "samples": len(samples),
        "p50_ms": nearest_rank_ms(samples, 0.5),
        "p95_ms": nearest_rank_ms(samples, 0.95),
        "p99_ms": nearest_rank_ms(samples, 0.99),
    }


async def measure(
    work: Callable[[], Awaitable[int]], expected_status: int,
    readings: list[float],
) -> None:
    start = perf_counter()
    observed = await work()
    duration_ms = (perf_counter() - start) * 1000
    if observed != expected_status:
        raise AssertionError(
            f"isolated profile expected HTTP {expected_status}, received {observed}"
        )
    readings.append(duration_ms)


async def run_local_profile() -> dict:
    """Fixed admission, overload rejection and 1/2/4-concurrency reads."""
    with tempfile.TemporaryDirectory(prefix="opsblaze-lab-capacity-") as root:
        database_url = "sqlite+aiosqlite:///" + str(Path(root) / "capacity.sqlite")
        engine = create_async_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async def scoped_session():
            async with sessions() as session:
                yield session

        settings = Settings(
            environment="development", local_token="isolated-ci-only",
            admission_limit_per_minute=60,
            max_active_incidents_per_tenant=12,
            max_active_incidents_global=50,
        )
        previous = app.dependency_overrides.copy()
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_session] = scoped_session
        token_headers = {
            "X-Tenant-Id": "synthetic",
            "X-OpsBlaze-Token": settings.local_token,
        }
        timings: dict[str, list[float]] = {
            "admission": [], "snapshot": [], "notifications": [],
        }
        ids = []
        read_steps = []
        try:
            # Admission services read settings directly, not via FastAPI DI.
            with patch("backend.services.get_settings", return_value=settings):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://isolated-in-process.invalid",
                ) as client:
                    for i in range(12):
                        headers = {**token_headers, "Idempotency-Key": f"synthetic-{i}"}
                        start = perf_counter()
                        response = await client.post(
                            "/v1/incidents",
                            headers=headers,
                            json={"target_id": "simulator-only"},
                        )
                        timings["admission"].append(
                            (perf_counter() - start) * 1000
                        )
                        if response.status_code != 202:
                            raise AssertionError(
                                f"expected durable 202, got {response.status_code}"
                            )
                        ids.append(response.json()["id"])

                    # The write path is intentionally sequential on SQLite.
                    # At capacity, the concurrent burst can only reject.
                    rejected: list[float] = []

                    async def reject(index: int) -> None:
                        async def work() -> int:
                            response = await client.post(
                                "/v1/incidents",
                                headers={
                                    **token_headers,
                                    "Idempotency-Key": f"overload-{index}",
                                },
                                json={"target_id": "simulator-only"},
                            )
                            assert response.headers.get("Retry-After") == "60"
                            return response.status_code
                        await measure(work, 429, rejected)

                    started = perf_counter()
                    await asyncio.gather(*(reject(i) for i in range(4)))
                    overload_elapsed = perf_counter() - started
                    replay = await client.post(
                        "/v1/incidents",
                        headers={**token_headers, "Idempotency-Key": "synthetic-0"},
                        json={"target_id": "simulator-only"},
                    )
                    assert replay.status_code == 202 and replay.json()["id"] == ids[0]
                    async with sessions() as session:
                        incident_count = await session.scalar(
                            select(func.count()).select_from(Incident)
                        )
                        start_count = await session.scalar(
                            select(func.count()).select_from(OutboxEntry)
                            .where(OutboxEntry.kind == "START_WORKFLOW")
                        )
                    assert incident_count == start_count == len(ids)
                    overload = {
                        "concurrency": 4,
                        "tenant_capacity": settings.max_active_incidents_per_tenant,
                        "status_counts": {"429": len(rejected)},
                        "retry_after_seconds": 60,
                        "elapsed_seconds": overload_elapsed,
                        "requests_per_second": len(rejected) / overload_elapsed,
                        **describe(rejected),
                        "raw_latency_ms": [round(value, 3) for value in rejected],
                        "idempotent_replay_preserved": True,
                        "durable_incidents": incident_count,
                        "durable_start_intents": start_count,
                    }

                    async def phase(kind: str, concurrency: int) -> dict:
                        gate = asyncio.Semaphore(concurrency)
                        samples: list[float] = []

                        async def one(incident_id: str) -> None:
                            async with gate:
                                if kind == "snapshot":
                                    async def work():
                                        response = await client.get(
                                            f"/v1/incidents/{incident_id}",
                                            headers=token_headers,
                                        )
                                        return response.status_code
                                else:
                                    async def work():
                                        response = await client.get(
                                            f"/v1/incidents/{incident_id}/events",
                                            headers=token_headers,
                                        )
                                        if response.status_code == 200:
                                            assert response.json()["events"]
                                        return response.status_code
                                await measure(work, 200, samples)

                        started = perf_counter()
                        await asyncio.gather(*(one(incident_id) for incident_id in ids))
                        elapsed = perf_counter() - started
                        timings[kind].extend(samples)
                        return {
                            "concurrency": concurrency,
                            "operation": kind,
                            **describe(samples),
                            "elapsed_seconds": elapsed,
                            "requests_per_second": len(samples) / elapsed,
                            "raw_latency_ms": [round(value, 3) for value in samples],
                        }

                    for concurrency in (1, 2, 4):
                        read_steps.append(await phase("snapshot", concurrency))
                        read_steps.append(await phase("notifications", concurrency))
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)
            await engine.dispose()

    return {
        "scope": "in-process SQLite/ASGI only",
        "production_slo_certified": False,
        "network_requests": 0,
        "admissions": len(ids),
        "read_concurrency_steps": [1, 2, 4],
        "read_steps": read_steps,
        "overload": overload,
        "measurements": {
            kind: describe(samples) for kind, samples in timings.items()
        },
        "raw_latency_ms": {
            kind: [round(value, 3) for value in samples]
            for kind, samples in timings.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bounded synthetic local OpsBlaze API benchmark; never external traffic",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run_local_profile())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(result["measurements"], sort_keys=True))


if __name__ == "__main__":
    main()
