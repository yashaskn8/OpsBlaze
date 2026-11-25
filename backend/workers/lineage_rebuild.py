"""Explicit, one-page P3 repair command; never changes source records."""

import argparse
import asyncio
import json
from uuid import UUID

from backend.database import get_session_maker
from backend.lineage_projection import prune_orphan_edges, rebuild_evidence_page


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--incident", required=True, type=UUID)
    parser.add_argument("--kind", required=True, choices=("hypothesis", "plan", "approval", "outcome", "effect", "experience", "lesson", "failure", "orphans"))
    parser.add_argument("--cursor", type=UUID)
    parser.add_argument("--limit", type=int, default=20, choices=range(1, 21))
    args = parser.parse_args()
    if args.kind == "orphans" and args.cursor is not None:
        parser.error("orphan cleanup does not take a cursor; repeat until more is false")
    async with get_session_maker()() as session:
        async with session.begin():
            if args.kind == "orphans":
                report = await prune_orphan_edges(
                    session, args.tenant, args.incident, limit=args.limit,
                )
            else:
                report = await rebuild_evidence_page(
                    session, args.tenant, args.incident, args.kind,
                    cursor=args.cursor, limit=args.limit,
                )
    if report is None:
        parser.exit(1, "incident not found in requested scope\n")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
