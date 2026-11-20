"""Budget cleanup never changes admission/effect audit facts."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.models import AdmissionUsage, Incident, ModelTokenWindow
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.workers.budget_retention import prune_budget_windows


@pytest.mark.asyncio
async def test_prunes_only_old_counter_windows_and_is_idempotent(sessions_fixture):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    stale = now - timedelta(days=15)
    current = now - timedelta(minutes=1)
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "alpha", "keep-incident", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        async with session.begin():
            session.add_all([
                AdmissionUsage(tenant_id="alpha", window_start=stale, accepted=7),
                AdmissionUsage(tenant_id="beta", window_start=current, accepted=1),
                ModelTokenWindow(
                    tenant_id="alpha", window_start=stale,
                    reserved_tokens=2000, used_tokens=1750,
                ),
                ModelTokenWindow(
                    tenant_id="beta", window_start=current,
                    reserved_tokens=500, used_tokens=350,
                ),
            ])
    cutoff = now - timedelta(days=7)
    assert await prune_budget_windows(sessions_fixture, cutoff) == {
        "admission_usage": 1,
        "model_token_window": 1,
    }
    assert await prune_budget_windows(sessions_fixture, cutoff) == {
        "admission_usage": 0,
        "model_token_window": 0,
    }
    async with sessions_fixture() as session:
        rate_rows = (await session.scalars(select(AdmissionUsage))).all()
        budget_rows = (await session.scalars(select(ModelTokenWindow))).all()
        assert all(row.window_start != stale for row in rate_rows)
        assert all(row.window_start != stale for row in budget_rows)
        assert any(row.tenant_id == "beta" for row in rate_rows)
        assert any(row.tenant_id == "beta" for row in budget_rows)
        assert await session.get(Incident, incident.id) is not None


@pytest.mark.asyncio
async def test_budget_cleanup_rejects_unsafe_cutoffs(sessions_fixture):
    with pytest.raises(ValueError, match="timezone"):
        await prune_budget_windows(sessions_fixture, datetime(2025, 1, 1))
    with pytest.raises(ValueError, match="batch size"):
        await prune_budget_windows(
            sessions_fixture, datetime.now(timezone.utc), batch_size=0
        )
