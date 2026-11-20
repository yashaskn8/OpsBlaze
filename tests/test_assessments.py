
import pytest
from sqlalchemy import select

from backend.investigator.abstain import assess_decision
from backend.investigator.baseline import inspect
from backend.models import (
    DecisionAssessment, Evidence, SimulatorTarget,
)
from backend.probes.gateway import invoke_probe
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.fixture
async def assessment_lab(sessions_fixture):
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(
                SimulatorTarget(
                    tenant_id="t", target_id="checkout",
                    version=2, healthy=False, last_fence=0,
                )
            )
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "assessment", IncidentCreate(target_id="checkout")
        )
    first = await invoke_probe(
        sessions_fixture, "t", incident.id, 1, "simulator.health"
    )
    second = await invoke_probe(
        sessions_fixture, "t", incident.id, 2, "simulator.version"
    )
    return sessions_fixture, incident.id, [str(first.evidence_id), str(second.evidence_id)]


@pytest.mark.asyncio
async def test_fresh_verified_evidence_permits_simulator_review(assessment_lab):
    sessions, incident_id, ids = assessment_lab
    result = await assess_decision(sessions, "t", incident_id, 2, ids)
    assert result.outcome == "PROPOSED"
    assert result.reason == "ACCEPTED" and result.risk_score == 0
    repeated = await assess_decision(sessions, "t", incident_id, 2, ids)
    assert repeated.id == result.id
    assert await assess_decision(sessions, "other", incident_id, 2, ids) is None


@pytest.mark.asyncio
async def test_target_changed_since_evidence_forces_handoff(assessment_lab):
    sessions, incident_id, ids = assessment_lab
    async with sessions() as session:
        async with session.begin():
            target = await session.get(SimulatorTarget, ("t", "checkout"))
            target.version += 1
    assessment = await assess_decision(sessions, "t", incident_id, 2, ids)
    assert assessment.outcome == "ABSTAINED"
    assert assessment.reason == "VERSION_CHANGED"


@pytest.mark.asyncio
async def test_evidence_hash_mismatch_is_not_repaired_or_ignored(assessment_lab):
    sessions, incident_id, ids = assessment_lab
    async with sessions() as session:
        async with session.begin():
            row = await session.get(Evidence, __import__("uuid").UUID(ids[0]))
            row.redacted_excerpt = '{"healthy":true}'
    assessment = await assess_decision(sessions, "t", incident_id, 2, ids)
    assert assessment.outcome == "ABSTAINED"
    assert assessment.reason == "HASH_MISMATCH"
