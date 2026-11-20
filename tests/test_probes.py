import hashlib
from uuid import UUID

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


@pytest.mark.asyncio
async def test_new_probe_steps_are_monotonic_without_spending_on_gaps(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t1", "step-gap", IncidentCreate(target_id="checkout")
        )
    with pytest.raises(ProbeDenied, match="sequential"):
        await invoke_probe(
            sessions_fixture, "t1", incident.id, 2, "simulator.health"
        )
    async with sessions_fixture() as session:
        incident_row = await session.get(Incident, incident.id)
        assert incident_row.probe_used == 0
        assert (await session.scalars(select(ProbeCall))).all() == []
    first = await invoke_probe(
        sessions_fixture, "t1", incident.id, 1, "simulator.health"
    )
    assert first.outcome == "UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["hash", "source", "tenant", "schema"])
async def test_replayed_probe_rejects_tampered_evidence(sessions_fixture, damage):
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(
                SimulatorTarget(
                    tenant_id="t1", target_id="checkout",
                    version=3, healthy=False, last_fence=0,
                )
            )
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t1", f"tamper-{damage}", IncidentCreate(target_id="checkout")
        )
    first = await invoke_probe(
        sessions_fixture, "t1", incident.id, 1, "simulator.health"
    )
    assert first.outcome == "OK"

    async with sessions_fixture() as session:
        async with session.begin():
            evidence = await session.get(Evidence, first.evidence_id)
            if damage == "hash":
                evidence.redacted_excerpt = '{"healthy":true}'
            elif damage == "source":
                evidence.source = "simulator.version"
            elif damage == "tenant":
                evidence.tenant_id = "other"
            else:
                evidence.redacted_excerpt = '{"healthy":"false"}'
                evidence.sha256 = hashlib.sha256(
                    evidence.redacted_excerpt.encode()
                ).hexdigest()

    with pytest.raises(ProbeDenied, match="recorded probe"):
        await invoke_probe(
            sessions_fixture, "t1", incident.id, 1, "simulator.health"
        )
    async with sessions_fixture() as session:
        incident_row = await session.get(Incident, incident.id)
        assert incident_row.probe_used == 1


@pytest.mark.asyncio
async def test_replay_rejects_cross_incident_evidence_link(sessions_fixture):
    async with sessions_fixture() as session:
        first = await admit_incident(
            session, "t", "probe-a", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        second = await admit_incident(
            session, "t", "probe-b", IncidentCreate(target_id="checkout")
        )
    await invoke_probe(sessions_fixture, "t", first.id, 1, "simulator.health")
    other_result = await invoke_probe(
        sessions_fixture, "t", second.id, 1, "simulator.health"
    )
    async with sessions_fixture() as session:
        async with session.begin():
            row = await session.scalar(
                select(ProbeCall).where(ProbeCall.incident_id == first.id)
            )
            row.evidence_id = other_result.evidence_id
    with pytest.raises(ProbeDenied, match="integrity"):
        await invoke_probe(sessions_fixture, "t", first.id, 1, "simulator.health")
