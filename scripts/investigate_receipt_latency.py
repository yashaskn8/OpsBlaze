"""Investigate Stage 3 get_receipt latency under concurrency.

Runs:
1. Schema & index analysis on `effect_receipt`.
2. EXPLAIN (ANALYZE, BUFFERS) on tenant_id + receipt_id query.
3. Concurrency profiling: measure engine connection acquisition wait time vs. database execution time
   under C=1, C=5, C=10, C=20 concurrent tasks with engine pool_size=10, max_overflow=10.
"""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import text

from backend.config import get_settings
from backend.effects.receipts import ActionGateway
from backend.persistence.db import make_engine, make_session_factory


async def main() -> None:
    settings = get_settings()
    engine = make_engine(settings, pool_size=10, max_overflow=10)
    session_factory = make_session_factory(engine)
    gateway = ActionGateway(session_factory)

    print("==================================================")
    print("1. INDEXES ON effect_receipt")
    print("==================================================")
    async with engine.connect() as conn:
        res = await conn.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'effect_receipt';")
        )
        for row in res.fetchall():
            print(f"- {row[0]}: {row[1]}")

        # Fetch sample tenant_id and receipt_id
        sample = (
            await conn.execute(text("SELECT tenant_id, id FROM effect_receipt LIMIT 1;"))
        ).fetchone()
        assert sample is not None, "Need at least one effect_receipt row"
        t_id, r_id = sample[0], sample[1]

        print("\n==================================================")
        print("2. EXPLAIN (ANALYZE, BUFFERS)")
        print(f"Target: tenant_id={t_id}, receipt_id={r_id}")
        print("==================================================")
        explain_sql = text("""
            EXPLAIN (ANALYZE, BUFFERS)
            SELECT * FROM effect_receipt WHERE tenant_id = :t_id AND id = :r_id;
        """)
        expl_res = await conn.execute(explain_sql, {"t_id": t_id, "r_id": r_id})
        for line in expl_res.fetchall():
            print(line[0])

        print("\n--- Compare with Primary Key lookup (WHERE id = :r_id) ---")
        expl_pk = await conn.execute(
            text("EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM effect_receipt WHERE id = :r_id;"),
            {"r_id": r_id},
        )
        for line in expl_pk.fetchall():
            print(line[0])

    print("\n==================================================")
    print("3. CONCURRENCY LATENCY DECOMPOSITION (C=1, 5, 10, 20)")
    print("Pool: pool_size=10, max_overflow=10 (total 20)")
    print("==================================================")

    for c in [1, 5, 10, 20]:
        barrier = asyncio.Barrier(c)
        latencies: list[float] = []
        checkout_waits: list[float] = []

        async def _worker(
            b: asyncio.Barrier = barrier,
            cw: list[float] = checkout_waits,
            lats: list[float] = latencies,
        ) -> None:
            await b.wait()
            t0 = time.perf_counter()
            # Measure checkout time
            async with session_factory():
                t1 = time.perf_counter()
                cw.append((t1 - t0) * 1000.0)
                await gateway.get_receipt(t_id, r_id)
                t2 = time.perf_counter()
                lats.append((t2 - t0) * 1000.0)

        tasks = [_worker() for _ in range(c)]
        await asyncio.gather(*tasks)

        avg_total = sum(latencies) / len(latencies)
        p95_total = (
            sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) > 1 else latencies[0]
        )
        avg_checkout = sum(checkout_waits) / len(checkout_waits)
        p95_checkout = (
            sorted(checkout_waits)[int(len(checkout_waits) * 0.95)]
            if len(checkout_waits) > 1
            else checkout_waits[0]
        )
        query_time = avg_total - avg_checkout

        print(
            f"C={c:2d} | Total avg: {avg_total:6.2f}ms (p95: {p95_total:6.2f}ms) | Pool checkout avg: {avg_checkout:6.2f}ms (p95: {p95_checkout:6.2f}ms) | DB query avg: {query_time:6.2f}ms"
        )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
