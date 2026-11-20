import pytest
from sqlalchemy import select

from backend.investigator.baseline import inspect
from backend.models import (
    Evidence, HypothesisStep, Incident, ProbeCall, ResourceEpoch, SimulatorTarget
)
from backend.probes.gateway import ProbeDenied, invoke_probe
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.mark.asyncio
async def test_allowlisted_probe_is_tenant_scoped_and_deduplicated(sessions_fixture) -> None:
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "t1", "k1", IncidentCreate(target_id="checkout"))
    first = await invoke_probe(sessions_fixture, "t1", incident.id, 1, "simulator.health")
    second = await invoke_probe(sessions_fixture, "t1", incident.id, 1, "simulator.health")
    assert first == second
    with pytest.raises(ProbeDenied):
        await invoke_probe(sessions_fixture, "other", incident.id, 2, "simulator.health")
    with pytest.raises(ProbeDenied):
        await invoke_probe(sessions_fixture, "t1", incident.id, 1, "simulator.version")
    async with sessions_fixture() as session:
        row = await session.get(Incident, incident.id)
        assert row.probe_used == 1
        assert len(list((await session.scalars(select(Evidence))).all())) == 1


@pytest.mark.asyncio
async def test_forbidden_probe_denied_without_external_effect(sessions_fixture) -> None:
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "t1", "k1", IncidentCreate(target_id="checkout"))
    result = await invoke_probe(sessions_fixture, "t1", incident.id, 1, "shell.exec")
    assert result.outcome == "DENIED"
    assert result.evidence_id is None
    async with sessions_fixture() as session:
        row = await session.get(Incident, incident.id)
        assert row.probe_used == 1
        assert len(list((await session.scalars(select(Evidence))).all())) == 0


@pytest.mark.asyncio
async def test_investigator_adapts_to_first_probe_and_records_steps(sessions_fixture) -> None:
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(tenant_id="t1", target_id="checkout", version=3, healthy=False, last_fence=0))
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "t1", "k1", IncidentCreate(target_id="checkout"))
    assert await inspect(sessions_fixture, "t1", incident.id) == "SIMULATOR_REVIEW_REQUIRED"
    assert await inspect(sessions_fixture, "t1", incident.id) == "SIMULATOR_REVIEW_REQUIRED"
    async with sessions_fixture() as session:
        steps = list((await session.scalars(select(HypothesisStep).order_by(HypothesisStep.step_no))).all())
        assert [step.step_no for step in steps] == [1, 2]
        assert all(step.evidence_ids for step in steps)
        incident = await session.get(Incident, incident.id)
        assert incident.probe_used == 2


@pytest.mark.asyncio
async def test_missing_simulator_target_abstains(sessions_fixture) -> None:
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "t1", "k1", IncidentCreate(target_id="missing"))
    assert await inspect(sessions_fixture, "t1", incident.id) == "ABSTAINED"
