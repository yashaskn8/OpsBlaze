
import pytest
from sqlalchemy import select

from backend.adapters.conformance import execute_suite, simulator_digest
from backend.models import (
    AdapterConformanceRun, Incident, PolicyAssignment, SimulatorAppliedEffect, SimulatorTarget,
)


@pytest.mark.asyncio
async def test_adapter_conformance_is_isolated_and_persisted(sessions_fixture):
    run = await execute_suite(sessions_fixture)
    assert run.state == "LAB_PASS"
    assert run.code_digest == simulator_digest()
    names = {scenario["scenario"] for scenario in run.results}
    assert names == {
        "first-apply", "duplicate-apply", "effect-status", "stale-fence",
        "version-cas", "lost-ack-lookup", "expired-lease", "missing-effect",
    }
    assert all(scenario["passed"] for scenario in run.results)
    async with sessions_fixture() as session:
        assert len((await session.scalars(select(AdapterConformanceRun))).all()) == 1
        assert (await session.scalars(select(SimulatorTarget))).all() == []
        assert (await session.scalars(select(SimulatorAppliedEffect))).all() == []
        assert (await session.scalars(select(Incident))).all() == []
        assert (await session.scalars(select(PolicyAssignment))).all() == []
