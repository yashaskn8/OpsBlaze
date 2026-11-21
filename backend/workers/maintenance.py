"""Bounded one-shot retention across E1, P1, and B4.

Each operation commits independently; a crash is safely resumed on the next
scheduled run. Incident/effect/approval/outcome audit rows are not deleted.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database import get_session_maker
from backend.settings import Settings, get_settings
from backend.workers.budget_retention import prune_budget_windows
from backend.workers.event_retention import purge_ui_events
from backend.workers.retention import expire_capsules, scrub_revoked_tenant_capsules


async def run_maintenance(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    now: datetime | None = None,
    max_batches: int = 20,
) -> dict[str, int]:
    today = now or datetime.now(timezone.utc)
    if today.tzinfo is None:
        raise ValueError("maintenance time must include timezone")
    if not 1 <= max_batches <= 100:
        raise ValueError("batch count must be bounded")
    if not 1 <= settings.ui_event_retention_days <= 365:
        raise ValueError("UI retention days must be 1–365")
    if not 1 <= settings.budget_counter_retention_days <= 365:
        raise ValueError("budget retention days must be 1–365")

    totals = {
        "capsules_scrubbed": 0,
        "lessons_revoked": 0,
        "tenant_capsules_scrubbed": 0,
        "tenant_lessons_revoked": 0,
        "ui_events_deleted": 0,
        "admission_windows_deleted": 0,
        "ai_windows_deleted": 0,
    }
    for _ in range(max_batches):
        result = await expire_capsules(sessions, now=today)
        totals["capsules_scrubbed"] += result["capsules_scrubbed"]
        totals["lessons_revoked"] += result["lessons_revoked"]
        if result["capsules_scrubbed"] < 500:
            break

    for _ in range(max_batches):
        scrubbed = await scrub_revoked_tenant_capsules(sessions)
        totals["tenant_capsules_scrubbed"] += scrubbed["capsules_scrubbed"]
        totals["tenant_lessons_revoked"] += scrubbed["lessons_revoked"]
        if scrubbed["capsules_scrubbed"] < 500:
            break

    ui_cutoff = today - timedelta(days=settings.ui_event_retention_days)
    for _ in range(max_batches):
        deleted = await purge_ui_events(sessions, ui_cutoff)
        totals["ui_events_deleted"] += deleted
        if deleted < 500:
            break

    budget_cutoff = today - timedelta(days=settings.budget_counter_retention_days)
    for _ in range(max_batches):
        rows = await prune_budget_windows(sessions, budget_cutoff)
        totals["admission_windows_deleted"] += rows["admission_usage"]
        totals["ai_windows_deleted"] += rows["model_token_window"]
        if max(rows.values()) < 500:
            break
    return totals


async def main() -> None:
    totals = await run_maintenance(get_session_maker(), get_settings())
    print(json.dumps(totals, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
